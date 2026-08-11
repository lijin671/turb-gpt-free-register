# -*- coding: utf-8 -*-
"""
OTP 检测与抽取通用工具，被 outlook_client（Outlook 邮箱）使用。

要求：
    - 多语言关键字识别（英 / 中 / 日 / 韩）
    - 字段名容错（不同邮件 API 用不同的字段命名约定）
    - 上下文优先：在多个 6 位数中，选择离"验证码"等关键字最近的那个
"""
import re
import threading

_OPENAI_SENDER_HINT = "openai"

# 多语言关键字（用于判断是否是 OpenAI 邮件）
_OPENAI_KEYWORDS = (
    "chatgpt", "openai",
    # 英文
    "verification code", "code is", "your code", "verify your email",
    # 中文
    "代码", "验证码", "确认码",
    # 日文
    "認証コード", "検証コード", "確認コード", "一時検証", "認証",
    # 韩文
    "인증 코드", "확인 코드",
)

# OTP 上下文关键字（用于在多个 6 位数中挑出真正的验证码）
_OTP_CONTEXT_KEYWORDS = (
    "code", "verify", "verification",
    "代码", "验证", "确认",
    "コード", "認証", "検証", "確認",
    "코드", "인증",
)

_OTP_REGEX = re.compile(r"\b(\d{6})\b")

# ---- 6 位码噪声过滤（参考 at-maker verification-matcher.ts looksLikeJunkCode）----
_JUNK_DATE_RE = re.compile(r"^20[2-3]\d(0[1-9]|1[0-2])$")   # YYYYMM，如 202608
_JUNK_YEAR_RE = re.compile(r"^20[2-3]\d\d\d$")             # 年份前缀 2020-2039xxxx
_JUNK_CODE_SET = {"000000", "111111", "123456"}


def looks_like_junk_code(code: str | None) -> bool:
    """判断 6 位数字是否为噪声而非 OTP。

    覆盖 at-maker 的常见噪声：YYYYMM 日期（202001-203912）、年份前缀、
    跟踪号片段（12000x）、全零/连号等。避免把邮件正文里的日期当验证码提交。
    """
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code or ""):
        return True
    if _JUNK_DATE_RE.match(code):
        return True
    if _JUNK_YEAR_RE.match(code):
        return True
    if code.startswith("12000"):
        return True
    return code in _JUNK_CODE_SET


# ---- 拒绝码记忆（参考 at-maker markVerificationCodeRejected）----
# 同一邮箱被 OpenAI 判为无效/过期的验证码记入黑名单，后续轮询不再重复提交。
_REJECTED_LOCK = threading.Lock()
_REJECTED_CODES: dict[str, set[str]] = {}


def _reject_key(email: str) -> str:
    return normalize_mailbox(email or "")


def mark_otp_rejected(email: str, code: str | None) -> None:
    """把某个邮箱的验证码标记为已拒绝（无效/过期），后续轮询跳过。"""
    key = _reject_key(email)
    if not key or not code:
        return
    digits = re.sub(r"\D", "", str(code))[:6]
    if not digits:
        return
    with _REJECTED_LOCK:
        _REJECTED_CODES.setdefault(key, set()).add(digits)


def rejected_otp_codes(email: str) -> set[str]:
    """返回该邮箱已被标记拒绝的验证码集合（副本，安全迭代）。"""
    key = _reject_key(email)
    with _REJECTED_LOCK:
        return set(_REJECTED_CODES.get(key, ()))


def clear_otp_rejected(email: str | None = None) -> None:
    """清空拒绝码记忆；email 为空时清空全部。"""
    with _REJECTED_LOCK:
        if email:
            _REJECTED_CODES.pop(_reject_key(email), None)
        else:
            _REJECTED_CODES.clear()
_QP_HEX_RE = re.compile(r"=[0-9A-Fa-f]{2}")


def decode_quoted_printable(value: str) -> str:
    """解码 quoted-printable 文本（参考 at-maker verification-matcher.ts）。

    QP 里非 ASCII 按字节编码（如 UTF-8 中文 =E4=BD=A0），必须先把字节累积起来
    再整体 UTF-8 解码，逐字节 fromCharCode 会破坏多字节字符。
    """
    s = re.sub(r"=\r?\n", "", str(value or ""))
    out = bytearray()
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "=" and i + 2 < n and re.fullmatch(r"[0-9A-Fa-f]{2}", s[i + 1:i + 3]):
            out.append(int(s[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(ord(s[i]) & 0xFF)
            i += 1
    try:
        return out.decode("utf-8", errors="replace")
    except Exception:
        return str(value or "")


def _maybe_decode_qp(value: str) -> str:
    """含 =XX 字节序列时按 QP 解码（避免破坏普通纯文本）。"""
    if not value or "=" not in value:
        return value
    if not _QP_HEX_RE.search(value):
        return value
    return decode_quoted_printable(value)


def normalize_mailbox(value: str) -> str:
    """归一化邮箱：去首尾空白、去 <xxx> 包裹、转小写。"""
    s = str(value or "").strip().lower()
    angle = re.search(r"<([^>]+)>", s)
    return (angle.group(1) if angle else s).strip()


def base_mailbox(value: str) -> str:
    """取邮箱基址：去 <xxx>、去 +alias（user+foo@x.com -> user@x.com）。"""
    s = normalize_mailbox(value)
    at = s.find("@")
    if at < 0:
        return s
    return s[:at].split("+")[0] + s[at:]


def _get_field(item: dict, *names: str) -> str:
    """
    从邮件 dict 中按顺序尝试多个可能的字段名，返回第一个非空字符串。
    用于兼容不同邮件 API 的字段命名约定（例如 sendEmail / from / fromEmail / from.address）。
    """
    for name in names:
        if "." in name:
            # 支持 "from.emailAddress.address" 这种点路径
            value = item
            for part in name.split("."):
                if not isinstance(value, dict):
                    value = None
                    break
                value = value.get(part)
            if isinstance(value, str) and value:
                return value
        else:
            value = item.get(name)
            if isinstance(value, str) and value:
                return value
    return ""


def looks_like_openai_email(item: dict) -> bool:
    """
    判断邮件是否来自 OpenAI / ChatGPT。多语言、多字段名兼容。

    字段名容错（不同 API 返回风格不一）：
        发件人:  sendEmail / from / fromEmail / from.emailAddress.address
        发件人名:sendName / fromName / from.emailAddress.name
        纯文本:  text / bodyPreview / bodyText
        HTML:    content / body / html / body.content / bodyHtml
    """
    sender = _get_field(item, "sendEmail", "from", "fromEmail", "from.emailAddress.address").lower()
    sender_name = _get_field(item, "sendName", "fromName", "from.emailAddress.name").lower()
    subject = _maybe_decode_qp(_get_field(item, "subject")).lower()
    text = _maybe_decode_qp(_get_field(item, "text", "bodyPreview", "bodyText")).lower()
    content = _maybe_decode_qp(_get_field(item, "content", "body", "html", "body.content", "bodyHtml")).lower()

    if _OPENAI_SENDER_HINT in sender or _OPENAI_SENDER_HINT in sender_name:
        return True

    return any(k in s for s in (subject, text, content) for k in _OPENAI_KEYWORDS)


def extract_otp(item: dict, exclude_codes: set[str] | None = None) -> str | None:
    """
    从邮件中抽出 6 位 OTP。

    抽取顺序：
        1. subject（OpenAI 部分邮件直接把 6 位数放在主题里，例 "Your OpenAI code is 525210"）
        2. 纯文本字段（text / bodyPreview / bodyText）
        3. HTML 字段（content / html / body / body.content / bodyHtml，去标签后）

    若 body 中含多个 6 位数，优先选择离 "验证码 / code / 認証" 等关键字最近的那个。
    所有分支都会跳过噪声码（looks_like_junk_code，如 YYYYMM 日期）与
    exclude_codes 中已拒绝/已使用的码。
    """
    exclude = set()
    for code in exclude_codes or ():
        digits = re.sub(r"\D", "", str(code))[:6]
        if digits:
            exclude.add(digits)

    def _usable(code: str) -> bool:
        return (not looks_like_junk_code(code)) and code not in exclude

    # 1. 主题里如果直接有 6 位数，最可信
    subject = _maybe_decode_qp(_get_field(item, "subject"))
    if subject:
        codes_in_subject = _OTP_REGEX.findall(subject)
        if len(codes_in_subject) == 1 and _usable(codes_in_subject[0]):
            # 主题里恰好只有一个 6 位数，几乎肯定就是 OTP
            return codes_in_subject[0]

    # 2. body 字段
    candidates = [
        ("text", _maybe_decode_qp(_get_field(item, "text", "bodyPreview", "bodyText"))),
        ("html", _maybe_decode_qp(_get_field(item, "content", "html", "body", "body.content", "bodyHtml"))),
    ]

    for kind, body in candidates:
        if not body:
            continue
        # 无论 text 还是 html，都先去 HTML 标签和 style 属性
        # （QQ 邮箱转发的 OpenAI 邮件，text 字段也可能含 HTML）
        body = re.sub(r"<style[^>]*>.*?</style>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<[^>]+>", " ", body)
        all_codes = _OTP_REGEX.findall(body)
        if not all_codes:
            continue
        body_lower = body.lower()
        # 优先选离上下文关键字最近的 6 位数
        for code in all_codes:
            if not _usable(code):
                continue
            idx = body_lower.find(code)
            if idx < 0:
                continue
            window = body_lower[max(0, idx - 60): idx + 6 + 60]
            if any(k.lower() in window for k in _OTP_CONTEXT_KEYWORDS):
                return code
        # 兜底：取第一个非噪声、非拒绝的 6 位数
        for code in all_codes:
            if _usable(code):
                return code
    return None
