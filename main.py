# -*- coding: utf-8 -*-
"""
ChatGPT 协议注册全流程入口
串联 12 个步骤，自动完成 ChatGPT 账号注册
"""
import sys
import argparse
import logging
import random
import string
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from config import REGISTER_EMAIL, REGISTER_NAME  # 这两个一般不在 WebUI 改
# 可热改的，按模块属性方式读
from config import twofa as _twofa_cfg
from config import email as _email_cfg
from config import roxybrowser as _roxy_cfg
from config import openai_protocol as _protocol_cfg
from core.session import BrowserSession
from core.chatgpt_auth import get_providers, get_csrf_token, signin_openai
from core.openai_auth import (
    follow_authorize,
    is_password_branch_url,
    request_sentinel_header_with_retry,
    validate_email_otp,
    send_email_otp,
    network_preflight,
    navigate_about_you,
    EmailOtpInvalidError,
    create_account,
)
from core.account_export import (
    follow_oauth_callback,
    fetch_session,
    setup_2fa,
    save_account_data,
    create_batch_archive_dir,
)
from core.email_provider import acquire_email, wait_for_otp
from core.humanize import delay as human_delay
from core.profile_utils import generate_random_birthday

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_FINALIZE_SESSION_MAX_ATTEMPTS = 5
_FINALIZE_SESSION_BACKOFF_BASE = 2.0


def _resend_otp(session: BrowserSession) -> None:
    """重新发送 OTP，按 SEND_SENTINEL_ON_EMAIL_OTP_SEND 开关决定是否携带 Sentinel 头。

    参考 sleep-reg gpt_register.py::_send_otp：email-otp/send 可带
    openai-sentinel-token + openai-sentinel-so-token。本地默认关闭（HAR 对齐）；
    开启时先 mint authorize_continue 的 sentinel（失败则降级为不带头重发，
    不阻断主流程）。
    """
    sentinel_header = so_header = None
    if getattr(_protocol_cfg, "SEND_SENTINEL_ON_EMAIL_OTP_SEND", False):
        try:
            sentinel_header, so_header = request_sentinel_header_with_retry(
                session, "authorize_continue"
            )
            human_delay("challenge")
            logger.info("[OTP] 已 mint sentinel 用于重新发送验证码")
        except Exception as exc:
            logger.warning("[OTP] mint sentinel 失败，重发不带 sentinel 头: %s", str(exc)[:160])
            sentinel_header = so_header = None
    send_email_otp(session, sentinel_header=sentinel_header, so_header=so_header)


def configure_logging(verbose: bool = False) -> None:
    """配置 CLI 日志：默认简洁，--verbose 时显示完整步骤细节。"""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def _stage1_authorize_url(session: BrowserSession, email: str) -> tuple[str, bool]:
    """阶段1: 获取 ChatGPT authorize URL。

    步骤1-3：providers → CSRF token → OAuth signin（NextAuth 握手）。
    握手失败且配置 AUTHORIZE_SHORT_PATH_FALLBACK=True 时，降级为直接构造
    auth.openai.com/api/accounts/authorize 短路径（参考 sleep-reg
    gpt_register.py _chatgpt_web_authorize），返回 (url, used_short_path)。

    Returns:
        (authorize_url, used_short_path)
    """
    try:
        # 步骤1: 获取 providers
        get_providers(session)
        human_delay("api")

        # 步骤2: 获取 CSRF token
        csrf_token = get_csrf_token(session)
        human_delay("api")

        # 步骤3: 发起 OAuth signin
        authorize_url = signin_openai(session, csrf_token, email)
        human_delay("api")
        return authorize_url, False
    except Exception as exc:
        if not getattr(_protocol_cfg, "AUTHORIZE_SHORT_PATH_FALLBACK", False):
            raise
        logger.warning(
            f"[注册] 阶段1 NextAuth 握手失败（{type(exc).__name__}: {str(exc)[:160]}），"
            "降级为直接构造 authorize URL"
        )
        from core.chatgpt_auth import build_direct_authorize_url
        authorize_url = build_direct_authorize_url(session, email)
        human_delay("api")
        return authorize_url, True
    for handler in root.handlers:
        handler.setLevel(logging.DEBUG if verbose else logging.INFO)

    if verbose:
        logging.getLogger("core").setLevel(logging.DEBUG)
        return

    logging.getLogger("core").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def _is_success(result: dict) -> bool:
    """判断单次注册结果是否成功，集中收敛批量统计规则。"""
    return isinstance(result, dict) and bool(result.get("success"))


_FAILURE_BUCKET_LABELS = {
    "account_dead": "账号已废",
    "ip_discipline": "IP纪律无可用IP",
    "otp_timeout": "OTP超时/无效",
    "codex": "Codex未完成",
    "other": "其他",
    "unknown": "未知",
}


def _failure_reason_bucket(error: str) -> str:
    """把失败 error 文本归入可操作的分类桶（批量汇总用）。"""
    text = str(error or "")
    if not text:
        return "unknown"
    from core.openai_auth import detect_account_unusable_text
    if detect_account_unusable_text(text):
        return "account_dead"
    low = text.lower()
    if "废弃" in text or "停用" in text or "封" in text or "deactivated" in low:
        return "account_dead"
    if "ip" in low and ("纪律" in text or "无可用出口" in text or "冷却" in text):
        return "ip_discipline"
    if "验证码" in text or "otp" in low or ("等待" in text and "码" in text):
        return "otp_timeout"
    if "codex" in low:
        return "codex"
    return "other"


def _summarize_failures(results: list[dict]) -> dict[str, int]:
    """统计批量失败结果的分类桶计数（成功结果不参与）。"""
    buckets: dict[str, int] = {}
    for r in results:
        if _is_success(r):
            continue
        bucket = _failure_reason_bucket(r.get("error") or "")
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return buckets


def _preflight_email_pool(count: int) -> str:
    """批量启动前邮箱池容量提示（只警告不阻塞，与 WebUI /api/jobs 同口径）。

    Returns: 提示文本；空串表示无需提示。
    """
    from config import email as _email_cfg
    from core.email_provider import parse_email_sources
    sources = parse_email_sources(_email_cfg.EMAIL_SOURCE)
    if not sources:
        return ""
    # 按需动态生成的临时邮箱源不需要容量提示
    if any(src in ("gptmail", "mailnest", "cloudmail", "cloudflare") for src in sources):
        return ""
    from core import db
    available = 0
    if "outlook" in sources:
        available += int(db.outlook_pool_summary().get("available", 0) or 0)
    if "generic_api" in sources:
        available += int(db.generic_api_email_pool_summary().get("available", 0) or 0)
    if "cloudflare_domain" in sources:
        available += int(db.domain_email_pool_summary().get("available", 0) or 0)
    if available < int(count):
        return (f"邮箱池可用 {available} 个，少于目标 {int(count)} 个"
                "（不足的会失败，建议补充邮箱或调低 --count）")
    return ""


def _water_level() -> dict | None:
    """当前账号库存水位（potential_usable 等）；读取失败返回 None。"""
    try:
        from core import db
        from core.account_pool import pool_stats
        return pool_stats(db.list_accounts(limit=100000))
    except Exception:
        return None


def _preflight_batch_ip_capacity(count: int) -> dict:
    """批量启动前的 1ip1号 容量预检（纯本地、无网络请求）。

    论坛经验（2708795）：同一静态出口 IP 短时间注册多号会连坐死号，必须 1ip1号。
    静态代理池在冷却窗口内不可复用，批量目标数超过空闲 IP 数时会排队等冷却甚至失败；
    树脂动态会话（cli-session-{sid}）每次领取换新出口，容量无上限，不参与统计。

    Returns: {"ok", "message", "static_free", "static_total"}。
    """
    from config.proxy import IP_DISCIPLINE_ENABLED, PROXY_POOL, proxy_ip_key
    if not IP_DISCIPLINE_ENABLED:
        return {"ok": True, "message": "IP 纪律未开启，跳过容量预检",
                "static_free": None, "static_total": None}
    from core.ip_discipline import is_ip_free
    seen: set[str] = set()
    static_pool: list[str] = []
    for p in PROXY_POOL or []:
        p = str(p or "").strip()
        if not p:
            continue
        key = proxy_ip_key(p)
        if not key or key == "direct":
            continue
        if key in seen:
            continue
        seen.add(key)
        # 树脂动态会话（sid 轮换）不占静态容量
        if "cli-session-" in p or "-session-" in p:
            continue
        static_pool.append(p)
    if not static_pool:
        return {"ok": True, "message": "代理池为动态会话或无静态 IP，1ip1号 容量无上限",
                "static_free": None, "static_total": 0}
    free = sum(1 for p in static_pool if is_ip_free(p)[0])
    ok = free >= int(count)
    message = (
        f"静态池 {free}/{len(static_pool)} 个 IP 空闲，目标 {int(count)} 个账号（1ip1号）"
        + ("" if ok else "：容量不足，批量会排队等冷却甚至失败，建议扩充代理池或改用树脂动态会话")
    )
    return {"ok": ok, "message": message, "static_free": free, "static_total": len(static_pool)}


def _finalize_registration_session(
    session: BrowserSession,
    continue_url: str,
    email: str,
    callback_referer: str = "https://auth.openai.com/about-you",
) -> tuple[dict, str]:
    """
    完成 OAuth 回调并拉取 accessToken。

    create_account 返回只代表创建接口通过，真正可用必须等 chatgpt.com
    写入登录态 cookie 且 /api/auth/session 返回 accessToken。
    """
    if not continue_url:
        raise RuntimeError("create_account 响应缺少 continue_url，无法完成 OAuth 回调")

    last_exc: Exception | None = None
    for attempt in range(1, _FINALIZE_SESSION_MAX_ATTEMPTS + 1):
        try:
            logger.info(
                f"[登录态] 完成 OAuth 回调并拉取 Token：{email} "
                f"(尝试 {attempt}/{_FINALIZE_SESSION_MAX_ATTEMPTS})"
            )
            follow_oauth_callback(session, continue_url, referer=callback_referer)
            human_delay("post_auth")
            session_info = fetch_session(session)
            access_token = session_info.get("accessToken")
            if not access_token:
                raise RuntimeError("session 响应缺少 accessToken")
            logger.info(f"[登录态] 已拿到 accessToken：{email}")
            return session_info, access_token
        except Exception as exc:
            last_exc = exc
            if attempt >= _FINALIZE_SESSION_MAX_ATTEMPTS:
                break
            backoff = _FINALIZE_SESSION_BACKOFF_BASE ** (attempt - 1)
            logger.warning(
                f"[登录态] 回调或拉取 Token 失败：{email}，"
                f"{type(exc).__name__}: {str(exc)[:180]}，{backoff:.1f}s 后重试"
            )
            time.sleep(backoff)

    raise RuntimeError(
        f"OAuth 回调/拉取 Token 重试耗尽：{email}，"
        f"最后错误：{type(last_exc).__name__ if last_exc else 'Unknown'}: {last_exc}"
    ) from last_exc


def generate_display_name() -> str:
    """生成只包含英文字母和空格的显示名，符合注册接口限制。"""
    first = random.choice(string.ascii_uppercase) + "".join(
        random.choices(string.ascii_lowercase, k=random.randint(3, 6))
    )
    last = random.choice(string.ascii_uppercase) + "".join(
        random.choices(string.ascii_lowercase, k=random.randint(3, 6))
    )
    return f"{first} {last}"


def prepare_registration_inputs() -> tuple[str, str, str]:
    """按 CLI 规则准备一次注册所需的邮箱、显示名和生日。"""
    email = REGISTER_EMAIL
    name = REGISTER_NAME
    birthday = generate_random_birthday()

    # 邮箱：留空 + USE_EMAIL_SERVICE=True 时从 Outlook 池领取
    if not email:
        if _email_cfg.USE_EMAIL_SERVICE:
            email = acquire_email()
            logger.debug(f"自动获取邮箱: {email}")
        else:
            email = input("请输入注册邮箱: ").strip()

    # 显示名称：未填则随机生成
    # OpenAI 限制：name_invalid_chars —— 只允许字母和空格，不能含数字/标点
    if not name:
        if _email_cfg.USE_EMAIL_SERVICE:
            name = generate_display_name()
            logger.debug(f"自动生成显示名称: {name}")
        else:
            name = input("请输入显示名称: ").strip()

    if not all([email, name]):
        raise RuntimeError("邮箱和名称不能为空")

    return email, name, birthday



# 注册前代理预检最多尝试次数（每次新建 BrowserSession → 全新 resin session IP）
_PREFLIGHT_MAX_ATTEMPTS = 5


def _is_proxy_block_error(session, exc: Exception) -> bool:
    """判断预检失败是否为代理/网络/封禁类错误（可通过换新代理解决）。"""
    if exc is None:
        return False
    name = type(exc).__name__
    msg = str(exc).lower()
    # 网络/连接/TLS/代理错误
    if any(t in name for t in ("SSLError", "ConnectionError", "Timeout", "CurlError", "ProxyError")):
        return True
    if any(k in msg for k in ("timed out", "connection", "proxy", "ssl", "tls", "wrong_version_number", "curl:")):
        return True
    # 熔断（HTTP 403/429 封禁或 CF challenge）
    if "熔断" in msg or "cf-challenge" in msg or "blocked" in msg:
        return True
    # 预检返回 4xx/5xx（网络可达但被风控）
    if "status=" in msg or "http_" in msg:
        return True
    return False


def _create_session_with_preflight(proxy: str | None, email: str, prefer_region: str = ""):
    """
    创建 BrowserSession 并完成网络预检；预检失败（代理封禁/网络异常）时
    自动换新代理重试（每次新建 session 会从 PROXY_POOL 抽取全新 resin session IP）。

    prefer_region: "jp"/"us" 等出口地区偏好；换代理时也保持地区偏好。
    """
    from core.openai_auth import network_preflight
    from core.session import BrowserSession

    last_exc = None
    for attempt in range(1, _PREFLIGHT_MAX_ATTEMPTS + 1):
        session = BrowserSession(proxy=proxy)
        try:
            network_preflight(session)
            return session
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "[代理] %s 网络预检失败（第 %s/%s 次）: %s: %s",
                email, attempt, _PREFLIGHT_MAX_ATTEMPTS,
                type(exc).__name__, str(exc)[:150],
            )
            if attempt >= _PREFLIGHT_MAX_ATTEMPTS or not _is_proxy_block_error(session, exc):
                break
            # 换新代理：有地区偏好时按地区重挑；否则纪律释放重领 / 随机新 sid
            logger.info("[代理] 预检失败疑似代理/封禁，换新代理重试...")
            proxy = _rotate_region_proxy(proxy, email, prefer_region) \
                if prefer_region else _rotate_disciplined_proxy(proxy, email)
    raise last_exc if last_exc else RuntimeError("[代理] 网络预检未完成")


def _rotate_region_proxy(proxy: str | None, email: str, region: str) -> str | None:
    """按地区偏好换新代理：释放旧 IP 后重新探测目标地区并 claim。"""
    if proxy:
        try:
            from core.ip_discipline import release_proxy
            release_proxy(proxy, owner=email or "registration")
        except Exception:
            pass
    from config.proxy import pick_region_proxy
    try:
        new_proxy = pick_region_proxy(region, owner=email or "registration")
        if new_proxy:
            return new_proxy
    except Exception:
        pass
    return proxy  # 探测/池耗尽时继续用旧 IP（同账号重试，不算连坐）

def _maybe_refresh_decode(email: str, result: dict) -> dict:
    """注册成功后按 REFRESH_DECODE_ENABLED 开关决定是否「解码保号」。

    解码 = AT → GrizzlySMS 接码 → Codex OAuth → refresh_token + CPA 兼容凭证
    （codex_accounts/codex-{email}.json，可直接导入 CPA/CLIProxyAPI 长期使用）。

    默认 False：不调用、不产生任何接码费用；只有用户明确说「解码保号」
    （把 .env 的 REFRESH_DECODE_ENABLED 置 true）才会执行。
    """
    try:
        from config import codex as _codex_cfg
        if not getattr(_codex_cfg, "REFRESH_DECODE_ENABLED", False):
            result["refresh_decode"] = "skipped_disabled"
            return result
        at = (result or {}).get("access_token") or ""
        if not at:
            logger.warning("[解码保号] 注册结果缺少 access_token，跳过 refresh_token 解码")
            result["refresh_decode"] = "skipped_no_token"
            return result
        from core.phone_verify_refresh import run_phone_verify_refresh
        logger.info("[解码保号] %s 开始解码（AT→refresh_token，GrizzlySMS 接码）", email)
        out = run_phone_verify_refresh(access_token=at, email=email, save_credential=True)
        if out.get("ok"):
            result["refresh_decode"] = "ok"
            result["refresh_token"] = out.get("refresh_token", "")
            result["cpa_credential_file"] = out.get("file_path", "")
            logger.info("[解码保号] %s 完成，refresh_token=%s...，凭证=%s",
                        email, str(out.get("refresh_token", ""))[:16],
                        out.get("file_path", ""))
        else:
            result["refresh_decode"] = f"error: {out.get('error')}"
            logger.warning("[解码保号] %s 失败: %s", email, out.get("error"))
    except Exception as exc:
        logger.warning("[解码保号] %s 异常（不影响注册结果）: %s", email, exc)
        result["refresh_decode"] = f"error: {type(exc).__name__}: {exc}"[:200]
    return result


def _maybe_extract_link(email: str, result: dict) -> dict:
    """注册成功后按配置自动「提链」（教程 8.8：先提链，后注册 GCash）。

    提链 = 用注册号去支付平台提取 GCash 结算链接（入口 US / 出口 JP 由提链
    服务端控制）。不阻塞 ChatGPT 号结果：提链失败只记 extract_link_status=error。
    """
    try:
        from config import plus as _plus_cfg
        if not getattr(_plus_cfg, "ENABLE_EXTRACT_AFTER_REGISTER", False):
            result["extract_link_status"] = "skipped_disabled"
            return result
        at = (result or {}).get("access_token") or ""
        if not at:
            logger.warning("[提链] 注册结果缺少 access_token，跳过自动提链")
            result["extract_link_status"] = "skipped_no_token"
            return result
        from core import extract_link_service
        acc = None
        try:
            from core import db
            acc = db.get_account_by_email(email)
        except Exception:
            acc = None
        logger.info("[提链] %s 注册成功，开始提链（先提链后 GCash）", email)
        out = extract_link_service.extract_link_now(
            account_id=(acc or {}).get("id") if acc else None,
            email=email,
            access_token=at,
            trigger="post_register",
        )
        ok = bool(out.get("ok"))
        result["extract_link_status"] = "ok" if ok else "failed"
        result["extract_link_error"] = out.get("error") or ""
        result["extract_link_result"] = (out.get("result") or {}) if ok else {}
        if ok:
            logger.info("[提链] %s 成功，link_type=%s", email, out.get("link_type"))
        else:
            logger.warning("[提链] %s 失败（不阻塞后续 GCash）: %s",
                           email, out.get("error"))
    except Exception as exc:
        logger.warning("[提链] %s 异常（不影响注册结果）: %s", email, exc)
        result["extract_link_status"] = f"error: {type(exc).__name__}: {exc}"[:200]
    return result


def _maybe_register_gcash(email: str, result: dict) -> dict:
    """ChatGPT 号注册成功后，按配置自动补跑 GCash 号注册（HeroSMS 接码）。

    不阻塞 ChatGPT 号结果：GCash 失败只记 gcash_status=error，不改变 success。
    批量并发时每个 worker 默认轮换出口代理（绕 HeroSMS 10分钟2码/IP 限制）。
    """
    try:
        from config import plus as _plus_cfg
        if not _plus_cfg.ENABLE_GCASH_REGISTER:
            return result
        if not _plus_cfg.HERO_SMS_API_KEY:
            logger.warning("[GCash] ENABLE_GCASH_REGISTER=true 但 HERO_SMS_API_KEY 未配置，跳过 GCash 注册")
            result["gcash_status"] = "skipped_no_api_key"
            return result
        from core.gcash_registrar import register_gcash_account, pick_hero_proxy
        proxy = ""
        if _plus_cfg.GCASH_REGISTER_ROTATE_PROXY:
            proxy = pick_hero_proxy()
        logger.info("[GCash] ChatGPT 号 %s 注册成功，开始 GCash 号注册（HeroSMS 接码，proxy=%s）",
                    email, proxy or "本机IP")
        acc = register_gcash_account(proxy=proxy)
        result["gcash_phone"] = acc.phone
        result["gcash_status"] = acc.status
        result["gcash_first_name"] = acc.first_name
        result["gcash_last_name"] = acc.last_name
        logger.info("[GCash] %s 关联 GCash 号 %s（%s）完成", email, acc.phone, acc.status)
    except Exception as exc:
        logger.warning("[GCash] %s 的 GCash 注册失败（不阻塞 ChatGPT 号）: %s", email, exc)
        result["gcash_status"] = f"error: {type(exc).__name__}: {exc}"[:200]
    return result


def run_registration(
    email: str,
    name: str,
    birthday: str | None = None,
    proxy: str = None,
    otp_code: str = None,
    batch_dir=None,
    prefer_region: str | None = None,
):
    """
    执行完整的 ChatGPT 注册流程（OTP-only，无密码），带 1ip1号 纪律。

    OpenAI 当前默认流程：signin 时携带 login_hint+screen_hint=login_or_signup
    → follow_authorize 重定向链自动落到 /email-verification 并触发 OTP 发送
    → 用户输入验证码 → validate_email_otp → about-you 提交昵称生日 → 完成。

    IP 纪律（论坛经验：同 IP 多号连坐死）：未显式传 proxy 时，先按 1ip1号
    纪律领取独立出口 IP（树脂动态会话 = 全新 sid；静态代理遵守冷却/一 IP 一号），
    注册全程占用，结束后记录使用并释放。代理池无可用 IP 时自节流等待
    IP_DISCIPLINE_MAX_WAIT_SECONDS，超时任务失败。

    Args:
        email: 注册邮箱
        name: 用户显示名称
        birthday: 生日，格式 YYYY-MM-DD
        proxy: 代理地址（不传则按 IP 纪律从 PROXY_POOL 领取）
        otp_code: 邮箱验证码（如果为None，会等待手动输入）
    """
    disciplined = None
    if not proxy:
        if prefer_region is None:
            try:
                from config.proxy import REGISTRATION_PREFER_REGION
                prefer_region = str(REGISTRATION_PREFER_REGION or "").strip()
            except Exception:
                prefer_region = ""
        if prefer_region:
            from config.proxy import pick_region_proxy
            proxy = pick_region_proxy(prefer_region, owner=email)
            if not proxy:
                raise RuntimeError(
                    f"[IP纪律] 未找到 {prefer_region.upper()} 出口代理"
                    "（池冷却/占满或地区命中率低），任务失败未消耗邮箱"
                )
            disciplined = proxy
        else:
            disciplined = _wait_disciplined_proxy(email)
            proxy = disciplined
    try:
        result = _run_registration_impl(
            email=email,
            name=name,
            birthday=birthday,
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
            prefer_region=prefer_region or "",
        )
        # 连坐防护统一收口：任一驱动（protocol/roxy/cloak/browser_use/skyvern）
        # 返回的失败错误命中"账号已废"特征时，把出口 IP 标记 co_risk，
        # 隔离同 IP 兄弟账号（论坛经验：同 IP 批量号会连坐全死）。
        if not result.get("success"):
            _quarantine_if_dead_account(email, proxy, result.get("error") or "")
        else:
            # 可选：提链（教程 8.8：先提链，后注册 GCash，HeroSMS 收码时间充裕）
            result = _maybe_extract_link(email, result)
            # 可选：ChatGPT 号成功后自动跑 GCash 号注册（HeroSMS 接码），
            # 产出 {email, gcash_phone} 供 run_gcash_checkout 绑 Plus。
            result = _maybe_register_gcash(email, result)
            # 可选：解码保号（REFRESH_DECODE_ENABLED=true 才执行，默认关）
            result = _maybe_refresh_decode(email, result)
        return result
    finally:
        if disciplined:
            try:
                from core.ip_discipline import record_ip_use, release_proxy
                # 成功注册的 IP 冷却更长（1ip1号）：成功号 token 常 ~30min 内被吊销，
                # 同 IP 短时间再注册会连坐；失败则按基础冷却，可较快重试
                record_ip_use(
                    disciplined,
                    email=email,
                    outcome="success" if result.get("success") else "failure",
                )
                release_proxy(disciplined, owner=email or "registration")
            except Exception:
                logger.exception("[IP纪律] 释放代理失败")



def _quarantine_if_dead_account(email: str, proxy: str | None, error_text: str) -> None:
    """跨驱动兜底：失败错误命中账号死亡特征时，隔离出口 IP 与同 IP 兄弟账号。

    协议驱动在 impl 内已用 AccountUnusableError 显式处理，这里按错误文本再兜一层，
    覆盖 roxy/cloak/browser_use/skyvern 等以文本/异常形式透出死号信号的驱动。
    """
    if not (email and error_text):
        return
    from core.openai_auth import detect_account_unusable_text
    if not detect_account_unusable_text(error_text):
        return
    try:
        from core.ip_discipline import mark_ip_co_risk
        mark_ip_co_risk(
            proxy or "",
            f"注册确认账号死亡: {str(error_text)[:120]}",
            emails=[email],
        )
    except Exception:
        logger.debug("注册死亡账号标记 IP 连坐风险失败", exc_info=True)


def _wait_disciplined_proxy(email: str) -> str | None:
    """按 1ip1号 纪律领取可用代理（带等待 + 停止检查）；纪律关闭时返回 None。"""
    from config.proxy import IP_DISCIPLINE_ENABLED, IP_DISCIPLINE_MAX_WAIT_SECONDS
    if not IP_DISCIPLINE_ENABLED:
        return None
    from core.ip_discipline import acquire_proxy

    def _check_stop():
        try:
            from core.registration_service import StopRequested, is_stop_requested
            if is_stop_requested():
                raise StopRequested("等待可用 IP 时任务已被用户停止")
        except ImportError:
            pass

    proxy = acquire_proxy(
        owner=email or "registration",
        timeout=IP_DISCIPLINE_MAX_WAIT_SECONDS,
        interrupt=_check_stop,
    )
    if proxy is None:
        raise RuntimeError(
            f"[IP纪律] {IP_DISCIPLINE_MAX_WAIT_SECONDS}s 内无可用出口 IP"
            "（代理池在冷却/占满），请扩充代理池或调低 IP_COOLDOWN_SECONDS"
        )
    return proxy


def _rotate_disciplined_proxy(proxy: str | None, email: str) -> str | None:
    """预检失败换新代理时遵守纪律：释放旧 IP 再领取新 IP；纪律关闭时退回随机抽取。"""
    from config.proxy import IP_DISCIPLINE_ENABLED, pick_proxy
    if proxy:
        try:
            from core.ip_discipline import release_proxy
            release_proxy(proxy, owner=email or "registration")
        except Exception:
            pass
    if not IP_DISCIPLINE_ENABLED:
        return pick_proxy()
    try:
        from core.ip_discipline import acquire_proxy
        new_proxy = acquire_proxy(owner=email or "registration", timeout=30)
        if new_proxy:
            return new_proxy
    except Exception:
        pass
    return proxy  # 池耗尽时继续用旧 IP（同账号重试，不算连坐）


def _run_registration_impl(
    email: str,
    name: str,
    birthday: str | None = None,
    proxy: str = None,
    otp_code: str = None,
    batch_dir=None,
    prefer_region: str = "",
):
    """
    执行完整的 ChatGPT 注册流程（OTP-only，无密码）。

    OpenAI 当前默认流程：signin 时携带 login_hint+screen_hint=login_or_signup
    → follow_authorize 重定向链自动落到 /email-verification 并触发 OTP 发送
    → 用户输入验证码 → validate_email_otp → about-you 提交昵称生日 → 完成。

    Args:
        email: 注册邮箱
        name: 用户显示名称
        birthday: 生日，格式 YYYY-MM-DD
        proxy: 代理地址（不传则从 PROXY_POOL 随机抽）
        otp_code: 邮箱验证码（如果为None，会等待手动输入）
    """
    # 可选注册驱动：
    #   protocol     = 原有纯协议（curl_cffi）
    #   roxy         = RoxyBrowser 指纹浏览器 + Selenium
    #   cloak        = CloakBrowser + Playwright/Selenium 适配层
    #   browser_use  = Browser Use Cloud stealth Chromium + Playwright
    #   skyvern      = Skyvern Browser Sessions + Playwright
    from config.proxy import proxy_ip_key
    driver_mode = str(getattr(_roxy_cfg, "REGISTRATION_DRIVER", "protocol") or "protocol").strip().lower()
    if driver_mode in ("roxy", "roxybrowser", "fingerprint", "browser"):
        from core.roxy_registration import run_roxy_registration
        return run_roxy_registration(
            email=email,
            name=name,
            birthday=birthday or generate_random_birthday(),
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
        )
    if driver_mode in ("cloak", "cloakbrowser"):
        from core.cloakbrowser_registration import run_cloak_registration
        return run_cloak_registration(
            email=email,
            name=name,
            birthday=birthday or generate_random_birthday(),
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
        )
    if driver_mode in ("browser_use", "browseruse", "browser-use", "bu"):
        from core.browser_use_registration import run_browser_use_registration
        return run_browser_use_registration(
            email=email,
            name=name,
            birthday=birthday or generate_random_birthday(),
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
        )
    if driver_mode in ("skyvern", "sv"):
        from core.skyvern_registration import run_skyvern_registration
        return run_skyvern_registration(
            email=email,
            name=name,
            birthday=birthday or generate_random_birthday(),
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
        )
    if driver_mode not in ("protocol", "api", "http"):
        raise RuntimeError(
            f"不支持的 REGISTRATION_DRIVER={driver_mode!r}，可选 protocol / roxy / cloak / browser_use / skyvern"
        )

    # 创建浏览器会话并完成网络预检（proxy=None 时自动从 config.PROXY_POOL 随机抽一个，
    # 预检失败会自动换新代理重试，每次新 session 拿到全新 resin 出口 IP）。
    session = _create_session_with_preflight(proxy, email, prefer_region=prefer_region)

    # 从代理 URL 中抽取 sid 段做日志，避免把账号密码完整打印
    proxy_label = "无"
    if session.proxy:
        # 形如 socks5h://user-region-JP-sid-XXXX-t-5:pass@host:port
        try:
            sid_part = next(
                (seg for seg in session.proxy.split("@")[0].split("-") if len(seg) == 8),
                "***",
            )
            proxy_label = f"{session.proxy.split('://')[0]}://...sid-{sid_part}...@{session.proxy.split('@')[-1]}"
        except Exception:
            proxy_label = "已配置"

    if not birthday:
        birthday = generate_random_birthday()

    logger.info(f"[注册] 开始：{email}，代理={proxy_label}")
    logger.info(f"[注册] 本次随机生日: {birthday}")
    logger.debug(f"[注册] 设备ID={session.device_id}，会话日志ID={session.auth_session_logging_id}")

    create_acknowledged = False
    try:
        human_delay("navigate")

        # 根据 2026-07-19 HAR 补齐匿名态 ChatGPT 首屏/模型预热链路。
        if getattr(_protocol_cfg, "CHATGPT_ANON_BOOTSTRAP_ENABLED", True):
            from core.chatgpt_bootstrap import anonymous_bootstrap
            anonymous_bootstrap(
                session,
                strict=bool(getattr(_protocol_cfg, "CHATGPT_BOOTSTRAP_STRICT", False)),
            )
            human_delay("navigate")

        # ==================== 阶段1: ChatGPT 认证 ====================
        # 步骤1-3: providers → CSRF → signin（NextAuth 握手）；
        # 握手失败且 AUTHORIZE_SHORT_PATH_FALLBACK=True 时降级为直接构造
        # auth.openai.com/api/accounts/authorize 短路径（sleep-reg 参考实现）。
        authorize_url, used_short_path = _stage1_authorize_url(session, email)
        if used_short_path:
            logger.warning(f"[注册] 已使用 authorize 短路径降级（未走 NextAuth 握手）: {email}")

        # 记录"OTP 触发"前的时间戳，自动取信箱时只看此后的邮件，
        # 避免取到上次注册留下的旧 OTP。
        otp_after_ts = time.time()

        # ==================== 阶段2: OpenAI Auth ====================
        # 步骤4: 跟随 authorize URL（建立 auth.openai.com 的 cookies）
        # 由于步骤3已携带 login_hint + screen_hint=login_or_signup，
        # 重定向链通常会直接走到 /email-verification 并自动触发 OTP 发送。
        # 但 OpenAI A/B 分流时新邮箱可能被路由到 /create-account/password
        # （密码注册分支）：此时需先 user/register 设置密码，再显式 send_email_otp。
        auth_final_url = follow_authorize(session, authorize_url)
        human_delay("navigate")

        # ==================== 阶段2.5: 密码注册分支 fallback ====================
        # 参考 sleep-reg gpt_register.py 的 password 分支实现。
        registration_password = None
        if is_password_branch_url(auth_final_url):
            if not getattr(_protocol_cfg, "PROTOCOL_PASSWORD_BRANCH_ENABLED", True):
                raise RuntimeError(
                    f"[注册] 已禁用密码注册分支，但邮箱被分流到密码页: {auth_final_url}"
                )
            from core.profile_utils import registration_password as _gen_reg_password
            from core.openai_auth import register_user as _register_user
            registration_password = _gen_reg_password()
            sentinel_header_pw, so_header_pw = request_sentinel_header_with_retry(
                session, "username_password_create"
            )
            human_delay("challenge")
            _register_user(session, email, registration_password, sentinel_header_pw, so_header_pw)
            logger.info(f"[注册] 密码分支：已为用户设置密码 {email}")
            # 密码分支下 OTP 不会自动发送，显式请求（幂等，重发也安全）
            _resend_otp(session)
            human_delay("api")

        # ==================== 阶段3: 验证码验证 ====================
        # Sentinel Token 不提前生成；等 OTP 到手后紧贴 validate 请求生成，
        # 避免等待邮箱期间 challenge 过期或与重新发送后的状态不一致。

        # 等待验证码：USE_EMAIL_SERVICE=True 时自动从 Outlook 取件，否则人工输入。
        # 如果验证码错误/过期，自动重新发送并重新取最新验证码。
        validate_result = None
        max_otp_attempts = 3
        current_otp = otp_code
        for otp_attempt in range(1, max_otp_attempts + 1):
            if current_otp is None:
                if _email_cfg.USE_EMAIL_SERVICE:
                    logger.info(f"[OTP] 等待验证码：{email}（第 {otp_attempt}/{max_otp_attempts} 次）")
                    try:
                        current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                    except Exception as exc:
                        # 等码超时：显式补发一次再等（参考 sleep-reg gpt_register.py 的
                        # "等不到码 -> 主动 send_otp -> 再等一轮" 容错）。
                        # 手动输入通道不补发，直接透传异常。
                        resend_enabled = bool(getattr(_protocol_cfg, "OTP_RESEND_ON_TIMEOUT", True))
                        if not resend_enabled or otp_attempt >= max_otp_attempts:
                            raise
                        logger.warning(f"[OTP] 等待验证码超时（{str(exc)[:160]}），显式重新发送一次")
                        otp_after_ts = time.time()
                        _resend_otp(session)
                        human_delay("api")
                        continue
                else:
                    logger.info("")
                    logger.info(f"[OTP] 请检查邮箱，输入收到的 6 位验证码（第 {otp_attempt}/{max_otp_attempts} 次）:")
                    current_otp = input(">>> 验证码: ").strip()

            human_delay("otp_input")
            try:
                # HAR 对齐：2026-07-19 抓包中的 email-otp/validate 未携带 Sentinel。
                # 保留开关，必要时可切回旧逻辑。
                sentinel_header_9 = None
                so_header_9 = None
                if getattr(_protocol_cfg, "SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE", False):
                    sentinel_header_9, so_header_9 = request_sentinel_header_with_retry(
                        session, "authorize_continue"
                    )
                    human_delay("challenge")

                # 步骤10: 提交验证码
                validate_result = validate_email_otp(session, current_otp, sentinel_header_9, so_header_9)
                break
            except EmailOtpInvalidError as exc:
                if otp_attempt >= max_otp_attempts:
                    raise
                logger.warning(f"[OTP] 验证码错误/过期：{str(exc)[:180]}，准备重新发送并重新获取验证码")
                # 记录被拒绝的验证码（参考 at-maker markVerificationCodeRejected）：
                # 某些邮箱源时间戳不可靠，重发后旧码可能再次出现，黑名单防止重复提交。
                try:
                    from core.otp_utils import mark_otp_rejected
                    mark_otp_rejected(email, current_otp)
                except Exception:
                    pass
                otp_after_ts = time.time()
                _resend_otp(session)
                human_delay("api")
                current_otp = None

        if validate_result is None:
            raise RuntimeError("OTP 验证未完成")
        human_delay("api")

        # OTP 校验后的下一步由服务端 auth session 决定：
        #   - about_you：新账号，需要继续提交姓名/生日 create_account。
        #   - external_url：通常说明服务端已可直接 OAuth 回调（常见于已有账号/无需资料页），
        #                   此时再强行调用 create_account 会触发 invalid_auth_step。
        page = validate_result.get("page") if isinstance(validate_result, dict) else {}
        page = page if isinstance(page, dict) else {}
        page_type = str(page.get("type") or "")
        otp_continue_url = (
            validate_result.get("continue_url")
            or validate_result.get("external_url")
            or validate_result.get("url")
            or page.get("continue_url")
            or page.get("external_url")
            or page.get("url")
        )
        logger.info(
            f"[步骤10] 后续分支判断: page_type={page_type or '空'}, "
            f"has_continue_url={bool(otp_continue_url)}"
        )

        # ==================== 阶段5/6: 完成注册或直接 OAuth 回调 ====================
        otp_continue_text = str(otp_continue_url or "")
        direct_oauth_after_otp = bool(
            otp_continue_text
            and "about-you" not in otp_continue_text
            and (
                "chatgpt.com/api/auth/callback" in otp_continue_text
                or "auth.openai.com/authorize/continue" in otp_continue_text
                or page_type == "external_url"
            )
        )
        if page_type == "external_url" or direct_oauth_after_otp:
            if not otp_continue_url:
                raise RuntimeError(f"OTP external_url 响应缺少可跟随 URL，无法继续: {validate_result}")
            logger.info(f"[注册] OTP 后进入 OAuth 回调分支，跳过 create_account：{email}")
            create_acknowledged = True
            session_info, access_token = _finalize_registration_session(
                session,
                otp_continue_url,
                email,
                callback_referer="https://auth.openai.com/email-verification",
            )
            if getattr(_protocol_cfg, "CHATGPT_AUTH_BOOTSTRAP_ENABLED", True):
                from core.chatgpt_bootstrap import authenticated_bootstrap
                authenticated_bootstrap(
                    session,
                    access_token,
                    strict=bool(getattr(_protocol_cfg, "CHATGPT_BOOTSTRAP_STRICT", False)),
                )
            human_delay("post_auth")
        else:
            # 兼容服务端只返回 continue_url=/about-you 但 page.type 为空/变化的情况。
            if page_type and page_type not in ("about_you", "about-you"):
                if otp_continue_url and "about-you" not in str(otp_continue_url):
                    raise RuntimeError(
                        f"OTP 后续页面类型未知，不应盲目 create_account: "
                        f"page_type={page_type}, resp={validate_result}"
                    )
                logger.warning(
                    f"[步骤10] 未知 page_type={page_type}，但 continue_url 指向 about-you，继续 create_account"
                )

            # 先真实导航到 about-you，让 auth session/page state 与 create_account 一致。
            about_url = str(otp_continue_url) if otp_continue_url and "about-you" in str(otp_continue_url) else None
            navigate_about_you(session, about_url)
            human_delay("navigate")

            # 步骤11: 获取 Sentinel Token（oauth_create_account）
            sentinel_header_11, so_header_11 = request_sentinel_header_with_retry(
                session, "oauth_create_account"
            )
            human_delay("challenge")

            human_delay("form")

            # 步骤12: 提交用户信息，完成注册
            create_result = create_account(session, name, birthday, sentinel_header_11, so_header_11)
            create_acknowledged = True

            logger.info(f"[注册] 创建接口已通过：{email}，继续完成 OAuth 回调")
            human_delay("post_auth")

            # 步骤12.5: 跟随 create_account 返回的 continue_url 完成 OAuth 回调
            continue_url = create_result.get("continue_url")
            if not continue_url:
                raise RuntimeError(
                    f"create_account 响应缺少 continue_url，无法继续: {create_result}"
                )

            # 步骤13: 拉 /api/auth/session 提取 accessToken
            session_info, access_token = _finalize_registration_session(session, continue_url, email)
            if getattr(_protocol_cfg, "CHATGPT_AUTH_BOOTSTRAP_ENABLED", True):
                from core.chatgpt_bootstrap import authenticated_bootstrap
                authenticated_bootstrap(
                    session,
                    access_token,
                    strict=bool(getattr(_protocol_cfg, "CHATGPT_BOOTSTRAP_STRICT", False)),
                )
            human_delay("post_auth")

        # ==================== 阶段7: 设置 2FA（受 config.ENABLE_2FA 控制）====================
        totp_secret = None
        if _twofa_cfg.ENABLE_2FA:
            # 步骤14-20: 重认证（要再收一次邮箱 OTP）→ enroll TOTP → activate
            try:
                totp_secret = setup_2fa(session, email)
            except Exception as exc:
                logger.error(f"2FA 设置失败: {exc}")
                logger.debug("2FA 错误详情:", exc_info=True)
                logger.warning("将继续保存账号信息（不含 TOTP secret），可后续手动设置")
        else:
            logger.debug("已跳过 2FA 设置 (config.ENABLE_2FA=False)")

        # ==================== 阶段 7.5: Codex OAuth（注册成功→拿回调/CPA凭证）====================
        # 用全新干净 session 从头登录该邮箱，走 邮箱OTP→手机短信验证(接码)→选workspace
        # →拿 code 的标准路径（不复用注册 session，避免撞 choose-an-account）。
        # 产出：
        #   1) codex_result["callback_url"]  命中 redirect_uri 的整条 Location（携带 code/state）
        #   2) codex_result["file_path"]     CPA 可直接导入的 codex-{email}.json
        codex_result = {"status": "skipped", "ok": False, "message": "未触发"}
        try:
            from core.codex_oauth import run_codex_oauth
            codex_result = run_codex_oauth(email)
        except Exception as exc:
            codex_result = {
                "status": "failed",
                "ok": False,
                "message": f"{type(exc).__name__}: {str(exc)[:180]}",
            }

        if codex_result.get("ok"):
            logger.info(
                f"[Codex] 成功：{email}，file={codex_result.get('file_path')}，"
                f"callback={codex_result.get('callback_url')}"
            )
        elif codex_result.get("status") == "skipped":
            logger.info(f"[Codex] 跳过：{email}，原因={codex_result.get('message')}")
        else:
            logger.warning(
                f"[Codex] 失败：{email}，原因={codex_result.get('message')}"
            )

        plus_result = {"status": "skipped", "ok": False, "message": "未触发"}
        # ==================== 阶段8: 持久化账号 ====================
        from core.email_provider import resolve_email_source
        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=session.proxy or None,
            ip_key=proxy_ip_key(session.proxy or ""),
            exit_ip=(session.exit_geo or {}).get("ip") or None,
            batch_dir=batch_dir,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "device_id": session.device_id,
                "password": registration_password,
                "sentinel_sid": getattr(session, "sentinel_sid", None),
                "browser_profile": getattr(session, "browser_profile", None),
                "codex": codex_result,
                "plus": plus_result,
            },
        )

        logger.info(f"[完成] {email}，账号ID={account_id}，Token={access_token[:16]}...")

        # ==================== 阶段8.5: 即时导出到 ChatGPT-to-API ====================
        # token 寿命短（~30min 内吊销），注册成功当场写入 access_tokens.json。
        export_result = {"ok": False, "message": "未触发"}
        try:
            from config import export as _export_cfg
            if _export_cfg.AUTO_EXPORT_TO_CHATGPT2API:
                from core.chatgpt2api_export import export_account_to_chatgpt2api
                export_result = export_account_to_chatgpt2api(
                    email=email,
                    access_token=access_token,
                    update_proxies=_export_cfg.AUTO_EXPORT_PROXIES_TXT,
                    proxy=session.proxy or "",
                )
        except Exception as exc:
            export_result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        if export_result.get("ok"):
            logger.info("[导出] ✅ %s", export_result.get("message"))
        else:
            logger.warning("[导出] 跳过/失败：%s", export_result.get("message"))

        # ==================== 阶段8.6: 即时导入 CPA Manager Plus ====================
        # token 寿命短（~30min 内吊销），注册成功当场写入 CPA 认证文件。
        cpa_result = {"ok": False, "message": "未触发"}
        try:
            from config import export as _export_cfg2
            if _export_cfg2.AUTO_IMPORT_TO_CPA_MANAGER:
                from core.cpa_manager_import import import_single_account
                cpa_result = import_single_account(
                    email=email,
                    access_token=access_token,
                    base=_export_cfg2.CPA_MANAGER_PLUS_BASE,
                    key=_export_cfg2.CPA_MANAGER_PLUS_KEY,
                    verify=_export_cfg2.CPA_IMPORT_VERIFY_MODELS,
                )
        except Exception as exc:
            cpa_result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        if cpa_result.get("ok"):
            logger.info("[CPA导入] ✅ %s", cpa_result.get("message"))
        else:
            logger.warning("[CPA导入] 跳过/失败：%s", cpa_result.get("message"))

        # ==================== 阶段9: 后置自动触发 flow ====================
        # 只有走完回调、拿到 token 并保存成功的账号，才会触发 flow。
        # flow 请求不影响账号保存状态，但会记录结果并参与批量统计。
        flow_result = {"status": "skipped", "ok": False, "message": "未触发"}
        try:
            from core.flow_trigger import trigger_flow
            flow_result = trigger_flow(access_token)
        except Exception as exc:
            flow_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {exc}"}

        if flow_result.get("ok"):
            logger.info(
                f"[Flow] 成功：{email}，HTTP={flow_result.get('http_status')}, "
                f"flow_id={flow_result.get('flow_id') or '未解析'}"
            )
        elif flow_result.get("status") == "skipped":
            logger.info(f"[Flow] 跳过：{email}，原因={flow_result.get('message')}")
        else:
            logger.warning(
                f"[Flow] 失败：{email}，HTTP={flow_result.get('http_status') or '无'}, "
                f"原因={flow_result.get('message')}"
            )

        # ==================== 阶段9.5: 零元 Plus 开通 ====================
        # 注册完成 → 提取 session → 切菲律宾结算 → Stripe API 直绑 → 验证。
        # 独立于 Codex/Flow 的后处理结果,不影响注册成功判定。
        plus_result = {"status": "skipped", "ok": False, "message": "未触发"}
        try:
            from config import plus as _plus_cfg
            from core.plus_integration import try_zero_plus_after_registration
            plus_result = try_zero_plus_after_registration(
                email=email,
                access_token=access_token,
                account_id=session_info.get("account", {}).get("id", ""),
                proxy=session.proxy or "",
                card_number=_plus_cfg.PLUS_CARD_NUMBER or "",
                exp_month=_plus_cfg.PLUS_CARD_EXP_MONTH or "",
                exp_year=_plus_cfg.PLUS_CARD_EXP_YEAR or "",
                cvc=_plus_cfg.PLUS_CARD_CVV or "", session_info=session_info,
                device_id=getattr(session, "device_id", "") or "",
            )
        except Exception as exc:
            plus_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {exc}"}

        if plus_result.get("ok"):
            logger.info(
                f"[Plus] 成功：{email}，"
                f"卡号={plus_result.get('card_used', '未知')}，"
                f"套餐={plus_result.get('plan_type', 'unknown')}"
            )
        elif plus_result.get("status") == "skipped":
            logger.info(f"[Plus] 跳过：{email}，原因={plus_result.get('message')}")
        else:
            logger.warning(
                f"[Plus] 失败：{email}，原因={plus_result.get('message')}"
            )

        logger.debug(f"[完成] TOTP Secret: {totp_secret or '(未设置)'}")

        # 注册任务的成功判定：账号本身(注册+token)+Codex 授权都成功才算 success。
        # Codex 失败时账号仍保存（token 拿到了、有补跑机会），但任务状态标失败，
        # 让 WebUI 任务表能清楚区分"完整成功"和"差 Codex"两种结果。
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        task_success = codex_ok
        task_error = None
        if not task_success:
            task_error = f"Codex 未完成: {codex_result.get('message', '未知')}"
            logger.warning(f"[任务结果] {email} 账号已保存但任务标失败，原因: {task_error}")

        # Outlook 别名复用：注册成功且配置开启时，把 base 收件箱放回 available
        # （同一收件箱可通过新别名继续产出注册邮箱，配合 1ip1号 纪律使用）。
        if task_success:
            try:
                from core.outlook_client import maybe_release_outlook_for_alias_reuse
                maybe_release_outlook_for_alias_reuse(email)
            except Exception:
                pass

        return {"success": task_success, "email": email, "account_id": account_id,
                "access_token": access_token, "totp_secret": totp_secret,
                "flow": flow_result, "codex": codex_result,
                "plus": plus_result,
                "error": task_error}

    except Exception as e:
        logger.error(f"[失败] {email}: {type(e).__name__}: {e}")
        logger.debug("详细错误信息:", exc_info=True)
        # 邮箱状态回收策略，三种情况：
        #   1. 账号已废（account_deactivated 等）：邮箱素材本身不可用，标 failed 直接剔除。
        #   2. 创建接口通过后失败：远端已消耗这个邮箱，直接废弃，避免重复注册。
        #   3. 创建接口通过前的普通失败：邮箱还可以下次继续尝试，放回 available。
        from core.openai_auth import AccountUnusableError
        account_dead = isinstance(e, AccountUnusableError)
        try:
            if email:
                from core.email_provider import release_email
                if account_dead:
                    src = release_email(
                        email, status="failed",
                        note=f"账号已废弃，邮箱不可用: {str(e)[:180]}",
                    )
                    logger.warning(f"[邮箱:{src}] {email} 账号已废弃，标记为 failed，不再重新注册")
                elif create_acknowledged:
                    src = release_email(
                        email, status="failed",
                        note=f"创建接口已通过但后续失败，已废弃: {str(e)[:180]}",
                    )
                    logger.warning(f"[邮箱:{src}] {email} 已创建但后续失败，标记为 failed，不再重新注册")
                else:
                    src = release_email(email, status="available", note=f"上次失败: {str(e)[:180]}")
                    logger.info(f"[邮箱:{src}] {email} 已恢复 available")
        except Exception:
            pass

        return {"success": False, "email": email, "error": str(e)}


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="ChatGPT 协议注册 CLI")
    parser.add_argument("-n", "--count", type=int, default=1, help="连续注册数量，默认 1")
    parser.add_argument("--workers", type=int, default=1, help="并发注册线程数，默认 1（串行）")
    parser.add_argument("--delay", type=float, default=0, help="每次注册结束后的间隔秒数")
    parser.add_argument("--continue-on-fail", action="store_true", help="单个账号失败后继续注册下一个")
    parser.add_argument("--verbose", action="store_true", help="显示详细步骤日志和错误堆栈")
    parser.add_argument(
        "--authorize-short-path", action="store_true",
        help="阶段1 NextAuth 握手失败时降级为直接构造 authorize URL（等价 AUTHORIZE_SHORT_PATH_FALLBACK=true）",
    )
    parser.add_argument(
        "--gcash-register", action="store_true",
        help="ChatGPT 号注册成功后自动跑 GCash 号注册（HeroSMS 接码 + ADB；等价 ENABLE_GCASH_REGISTER=true）",
    )
    args = parser.parse_args()
    configure_logging(args.verbose)

    if args.authorize_short_path:
        _protocol_cfg.AUTHORIZE_SHORT_PATH_FALLBACK = True
        logger.info("[CLI] 已开启 authorize 短路径降级（--authorize-short-path）")

    if args.gcash_register:
        from config import plus as _plus_cfg
        _plus_cfg.ENABLE_GCASH_REGISTER = True
        logger.info("[CLI] 已开启注册流程内 GCash 号注册（--gcash-register）")

    if args.count < 1:
        logger.error("注册数量必须大于 0")
        sys.exit(1)

    if args.workers < 1:
        logger.error("并发线程数必须大于 0")
        sys.exit(1)

    if args.count > 1 and REGISTER_EMAIL:
        logger.error("config.REGISTER_EMAIL 已固定邮箱，不适合批量注册；请留空后再使用 --count")
        sys.exit(1)

    if args.workers > 1 and not _email_cfg.USE_EMAIL_SERVICE:
        logger.error("多线程注册需要启用 Outlook 自动取件；请开启 USE_EMAIL_SERVICE 或改用 --workers 1")
        sys.exit(1)

    if args.workers > args.count:
        logger.info(f"[批量] 并发线程数 {args.workers} 大于目标数量，已按 {args.count} 个任务执行")
        args.workers = args.count

    if args.count > 1:
        try:
            pre = _preflight_batch_ip_capacity(args.count)
            logger.warning(f"[批量] 预检: {pre['message']}")
        except Exception:
            logger.debug("[批量] IP 容量预检失败（不阻塞批量）", exc_info=True)
        try:
            email_msg = _preflight_email_pool(args.count)
            if email_msg:
                logger.warning(f"[批量] 邮箱预检: {email_msg}")
        except Exception:
            logger.debug("[批量] 邮箱池预检失败（不阻塞批量）", exc_info=True)

    water_before = _water_level() if args.count > 1 else None

    if args.workers > 1:
        batch_dir = create_batch_archive_dir(args.count, args.workers)
        logger.info(f"[批量] 本批次归档目录：{batch_dir}")
        results = run_parallel_batch(args.count, args.workers, args.delay, args.continue_on_fail, batch_dir)
    else:
        batch_dir = create_batch_archive_dir(args.count, args.workers)
        logger.info(f"[批量] 本批次归档目录：{batch_dir}")
        results = run_serial_batch(args.count, args.delay, args.continue_on_fail, batch_dir)

    success_count = sum(1 for r in results if _is_success(r))
    flow_success_count = sum(
        1 for r in results
        if _is_success(r) and isinstance(r.get("flow"), dict) and r["flow"].get("ok")
    )
    flow_failed_count = sum(
        1 for r in results
        if _is_success(r)
        and isinstance(r.get("flow"), dict)
        and r["flow"].get("status") == "failed"
    )
    flow_skipped_count = sum(
        1 for r in results
        if _is_success(r)
        and isinstance(r.get("flow"), dict)
        and r["flow"].get("status") == "skipped"
    )
    codex_success_count = sum(
        1 for r in results
        if _is_success(r) and isinstance(r.get("codex"), dict) and r["codex"].get("ok")
    )
    codex_failed_count = sum(
        1 for r in results
        if _is_success(r)
        and isinstance(r.get("codex"), dict)
        and r["codex"].get("status") == "failed"
    )
    codex_skipped_count = sum(
        1 for r in results
        if _is_success(r)
        and isinstance(r.get("codex"), dict)
        and r["codex"].get("status") == "skipped"
    )
    plus_success_count = sum(
        1 for r in results
        if _is_success(r) and isinstance(r.get("plus"), dict) and r["plus"].get("ok")
    )
    plus_failed_count = sum(
        1 for r in results
        if _is_success(r)
        and isinstance(r.get("plus"), dict)
        and r["plus"].get("status") == "failed"
    )
    plus_skipped_count = sum(
        1 for r in results
        if _is_success(r)
        and isinstance(r.get("plus"), dict)
        and r["plus"].get("status") == "skipped"
    )
    logger.info(f"[批量] 完成：成功 {success_count} / 尝试 {len(results)} / 目标 {args.count}")
    if water_before is not None:
        water_after = _water_level()
        if water_after is not None:
            delta = int(water_after.get("potential_usable", 0)) - int(water_before.get("potential_usable", 0))
            logger.info(
                f"[批量] 库存水位: {water_before.get('potential_usable', 0)} → "
                f"{water_after.get('potential_usable', 0)}（净增 {delta:+d}）"
            )
    if success_count < len(results):
        buckets = _summarize_failures(results)
        if buckets:
            detail = "；".join(
                f"{_FAILURE_BUCKET_LABELS.get(k, k)} {v}" for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])
            )
            logger.info(f"[批量] 失败原因：{detail}")
    if success_count:
        logger.info(
            f"[批量] Flow：成功 {flow_success_count} / 失败 {flow_failed_count} / 跳过 {flow_skipped_count}"
        )
        logger.info(
            f"[批量] Codex：成功 {codex_success_count} / 失败 {codex_failed_count} / 跳过 {codex_skipped_count}"
        )
        logger.info(
            f"[批量] Plus：成功 {plus_success_count} / 失败 {plus_failed_count} / 跳过 {plus_skipped_count}"
        )
    sys.exit(0 if success_count == args.count else 1)


def run_one_batch_item(index: int, total: int, batch_dir=None) -> dict:
    """执行批量注册中的一个任务，返回结构化结果。"""
    logger.info(f"[批量] 开始第 {index + 1}/{total} 个注册")
    try:
        email, name, birthday = prepare_registration_inputs()
        return run_registration(
            email=email,
            name=name,
            birthday=birthday,
            batch_dir=batch_dir,
            # proxy 不传 → BrowserSession 会从 PROXY_POOL 随机抽
        )
    except Exception as exc:
        logger.error(f"[批量] 第 {index + 1} 个注册准备阶段失败: {type(exc).__name__}: {exc}")
        logger.debug("准备阶段错误详情:", exc_info=True)
        return {"success": False, "error": str(exc)}


def run_serial_batch(count: int, delay: float, continue_on_fail: bool, batch_dir=None) -> list[dict]:
    """按原有串行方式执行批量注册。"""
    results = []
    for index in range(count):
        result = run_one_batch_item(index, count, batch_dir)
        results.append(result)
        if not _is_success(result) and not continue_on_fail:
            logger.error("[批量] 当前账号失败，已停止。需要继续跑可加 --continue-on-fail")
            break

        if delay > 0 and index < count - 1:
            logger.info(f"[批量] 等待 {delay} 秒后继续")
            time.sleep(delay)
    return results


def run_parallel_batch(
    count: int,
    workers: int,
    delay: float,
    continue_on_fail: bool,
    batch_dir=None,
) -> list[dict]:
    """使用线程池并发执行批量注册。"""
    logger.info(f"[批量] 启用多线程注册：目标 {count}，并发 {workers}")
    if delay > 0:
        logger.info(f"[批量] 并发模式下 --delay={delay} 表示提交任务之间的错峰间隔")

    results: list[dict] = []
    future_to_index = {}
    next_index = 0
    stop_submitting = False

    def submit_next(executor: ThreadPoolExecutor) -> bool:
        nonlocal next_index
        if stop_submitting or next_index >= count:
            return False
        future = executor.submit(run_one_batch_item, next_index, count, batch_dir)
        future_to_index[future] = next_index
        next_index += 1
        if delay > 0 and next_index < count:
            time.sleep(delay)
        return True

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="reg-cli") as executor:
        while len(future_to_index) < workers and submit_next(executor):
            pass

        while future_to_index:
            done, _ = wait(future_to_index, return_when=FIRST_COMPLETED)
            for future in done:
                index = future_to_index.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    logger.error(f"[批量] 第 {index + 1}/{count} 个注册线程异常: {type(exc).__name__}: {exc}")
                    logger.debug("线程错误详情:", exc_info=True)
                    result = {"success": False, "error": str(exc)}
                results.append(result)

                if not _is_success(result) and not continue_on_fail:
                    stop_submitting = True
                    logger.error("[批量] 当前账号失败，已停止提交新任务。已开始的任务会继续跑完。")

            while len(future_to_index) < workers and submit_next(executor):
                pass

    return results


if __name__ == "__main__":
    main()
