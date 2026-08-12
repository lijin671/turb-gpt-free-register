# -*- coding: utf-8 -*-
"""
OpenAI Auth 模块
处理 auth.openai.com 域名下的注册请求（步骤4-5、7-8、10、12）
以及 sentinel token 请求（步骤6、9、11，端点在 config.openai_protocol.SENTINEL_REQ_URL）
"""
import json
import logging
import time

from config import SENTINEL_REQ_ORIGIN, SENTINEL_REQ_URL
from core.session import BrowserSession
from core.sentinel import (
    generate_requirements_token,
    build_sentinel_request_body,
)
from core.sentinel_runner import generate_sentinel_token
from core.sentinel_sdk import current_sentinel_sv, ensure_sentinel_sdk

logger = logging.getLogger(__name__)

# FlareSolverr clearance 集成（CF 403 自动过墙）
try:
    from core.flaresolverr_clearance import get_clearance, apply_clearance_to_session
except ImportError:
    get_clearance = None
    apply_clearance_to_session = None


class EmailOtpInvalidError(RuntimeError):
    """邮箱验证码无效/过期，可重新发送后重试。"""


class AccountUnusableError(Exception):
    """
    邮箱对应的 OpenAI 账号已废（删除/停用/封禁），再试也是同样结果。

    与普通网络/风控错误区分：这类错误意味着这个邮箱素材本身不可用，
    上层应把邮箱标成 failed 直接剔除，而不是放回 available 反复重试。

    携带 error_code 便于日志与排查（如 account_deactivated）。
    """

    def __init__(self, message: str, error_code: str = ""):
        super().__init__(message)
        self.error_code = error_code


# 远端返回这些 error code 时，判定邮箱素材已废，不再重试。
_ACCOUNT_DEAD_CODES = frozenset({
    "account_deactivated",   # 账号已删除/停用
    "account_deleted",
    "account_banned",
})

_ACCOUNT_DEAD_TEXT_MARKERS = (
    "account_deactivated",
    "account_deleted",
    "account_banned",
    "account deactivated",
    "account deleted",
    "account banned",
    "account has been deactivated",
    "account has been deleted",
    "account was deactivated",
    "account was deleted",
    "your account has been deactivated",
    "your account has been deleted",
    "your account was deactivated",
    "your account was deleted",
    "账号已停用",
    "账号已禁用",
    "账号已删除",
    "账号已废",
    "账号已废弃",
    "账户已停用",
    "账户已禁用",
    "账户已删除",
    "账户已废",
    "账户已废弃",
)


def detect_account_unusable_text(text: str) -> str:
    """从浏览器页面/异常文本里识别账号已废，返回规范 error_code；未命中返回空串。"""
    low = str(text or "").lower()
    for code in _ACCOUNT_DEAD_CODES:
        if code in low:
            return code
    if any(marker in low for marker in _ACCOUNT_DEAD_TEXT_MARKERS):
        if "delete" in low or "删除" in low:
            return "account_deleted"
        if "ban" in low or "封" in low:
            return "account_banned"
        return "account_deactivated"
    return ""


def detect_account_unusable_response_body(body: str) -> str:
    """
    按纯协议模式同源逻辑，从接口响应 JSON 的 error.code 识别账号已废。

    这不是页面文字识别；用于浏览器/指纹浏览器拦截
    /api/accounts/email-otp/validate 响应后，读取响应体里的结构化错误码。
    """
    try:
        payload = json.loads(body or "")
    except Exception:
        return ""
    err = payload.get("error") if isinstance(payload, dict) else None
    code = ""
    if isinstance(err, dict):
        code = str(err.get("code") or "")
    elif isinstance(payload, dict):
        code = str(payload.get("code") or payload.get("error_code") or "")
    return code if code in _ACCOUNT_DEAD_CODES else ""


def _extract_error_code(resp) -> str:
    """从响应体 JSON 里抽 error.code（拿不到返回空串）。"""
    try:
        payload = resp.json()
    except Exception:
        return ""
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or "")
    return ""


# 步骤4 网络层临时性错误（代理抽风 / TLS 握手失败 / 重置等）的重试参数
_FOLLOW_AUTH_MAX_ATTEMPTS = 3
_FOLLOW_AUTH_BACKOFF_BASE = 2.0  # 第 N 次重试前等 2^(N-1) 秒


def _is_transient_network_error(exc: Exception) -> bool:
    """识别可重试的临时性网络错误（TLS / 连接超时 / 连接重置 / 代理拒绝）。"""
    name = type(exc).__name__
    msg = str(exc).lower()
    transient_classes = ("SSLError", "ConnectionError", "Timeout", "CurlError", "ProxyError")
    if any(t.lower() in name.lower() for t in transient_classes):
        return True
    transient_keywords = (
        "wrong_version_number",      # 代理给了非 TLS 响应
        "tls connect",
        "ssl",
        "connection reset",
        "connection refused",
        "timed out",
        "proxy",
        "curl: (35)",
        "curl: (52)",                # empty reply from server
        "curl: (56)",                # network recv failure
    )
    return any(k in msg for k in transient_keywords)


def _try_flaresolverr_clearance(session: BrowserSession, target_url: str, proxy_url: str = "") -> bool:
    """遇到 CF 403 时调 FlareSolverr 获取 clearance cookie 并注入 session。

    Returns True 如果成功获取并注入了 clearance。
    """
    if get_clearance is None:
        logger.debug("[FlareSolverr] 模块未加载，跳过 clearance")
        return False
    try:
        bundle = get_clearance(target_url, proxy_url)
        if bundle is None:
            logger.warning("[FlareSolverr] 未获取到 clearance")
            return False
        apply_clearance_to_session(session, bundle)
        logger.info("[FlareSolverr] clearance 已注入 session，重试请求")
        return True
    except Exception as exc:
        logger.warning("[FlareSolverr] clearance 获取异常: %s: %s", type(exc).__name__, exc)
        return False


def _get_session_proxy_url(session: BrowserSession) -> str:
    """从 BrowserSession 获取当前代理 URL。"""
    proxy = getattr(session, "proxy", "") or ""
    if not proxy:
        proxy = getattr(session, "_proxy", "") or ""
    return str(proxy).strip()


def network_preflight(session: BrowserSession) -> None:
    """
    注册前网络预检：只建立边缘节点/cookie/基础连通性，不携带邮箱、不触发 OTP。

    这样真正会"烧邮箱"的 authorize 重定向发生前，已经确认当前代理、TLS
    impersonate、ChatGPT/Auth/Sentinel 三段链路都可达。

    CF 403 处理：如果预检请求返回 403 且响应体含 Cloudflare challenge 特征，
    自动调 FlareSolverr 获取 cf_clearance cookie 注入 session 后重试。
    """
    proxy_url = _get_session_proxy_url(session)
    checks = [
        ("chatgpt-login", lambda: session.get(
            "https://chatgpt.com/login",
            headers=session.get_chatgpt_navigate_headers(referer="https://chatgpt.com/"),
            allow_redirects=True,
        )),
        ("auth-login", lambda: session.get(
            "https://auth.openai.com/log-in",
            headers=session.get_auth_navigate_headers(referer="https://chatgpt.com/login"),
            allow_redirects=True,
        )),
        ("sentinel-frame", lambda: session.get(
            f"{SENTINEL_REQ_ORIGIN}/backend-api/sentinel/frame.html?sv={current_sentinel_sv(session=session)}",
            headers=session.get_auth_navigate_headers(referer="https://auth.openai.com/log-in", target_origin=SENTINEL_REQ_ORIGIN),
            allow_redirects=True,
        )),
    ]
    for label, fn in checks:
        last_exc = None
        clearance_attempted = False
        for attempt in range(1, _FOLLOW_AUTH_MAX_ATTEMPTS + 1):
            try:
                logger.info(f"[预检] {label} ({attempt}/{_FOLLOW_AUTH_MAX_ATTEMPTS})")
                resp = fn()
                status = getattr(resp, "status_code", 0)
                if status >= 400:
                    # 检查是否是 CF challenge（403 + "just a moment" 等）
                    body_text = str(getattr(resp, "text", "") or "").lower()
                    is_cf = status in (403, 503) and (
                        "just a moment" in body_text
                        or "cf-chl-" in body_text
                        or "__cf_chl_" in body_text
                        or "challenge-platform" in body_text
                    )
                    # chatgpt-login 的 403 直接跳过（注册流程走 auth.openai.com，不经过 chatgpt.com）
                    # 这是 curl_cffi TLS 指纹被 CF 识别的问题，不是 CF challenge
                    if label == "chatgpt-login" and status == 403:
                        logger.warning(f"[预检] {label} 返回 HTTP {status}（TLS 指纹拦截），注册走 auth.openai.com，跳过此步")
                        session.blocked_until = 0
                        session.blocked_reason = ""
                        break
                    # auth-login / sentinel 的 CF challenge → 换代理（新出口 IP）重试
                    # FlareSolverr 的 Chrome 能过但 curl_cffi TLS 指纹可能过不了同一 IP，
                    # 所以最优策略是换一个不拦 CF 的出口 IP
                    if is_cf:
                        logger.warning(f"[预检] {label} 遇到 CF challenge (HTTP {status})，换代理重试...")
                        # 重置熔断
                        session.blocked_until = 0
                        session.blocked_reason = ""
                        # 换新代理（新 sid = 新出口 IP）
                        try:
                            from config.proxy import pick_proxy
                            new_proxy = pick_proxy()
                            if new_proxy and new_proxy != proxy_url:
                                logger.info(f"[预检] 换代理: {new_proxy[:50]}...")
                                # 直接更新 session 的代理
                                session.proxy = new_proxy
                                session.session.proxies = {"http": new_proxy, "https": new_proxy}
                                session.blocked_until = 0
                                session.blocked_reason = ""
                                session.cf_challenge_count = 0
                                proxy_url = new_proxy
                                continue  # 重试当前 check
                        except Exception:
                            pass
                    raise RuntimeError(f"{label} status={status}, body={body_text[:180]}")
                break
            except Exception as exc:
                last_exc = exc
                if not _is_transient_network_error(exc) or attempt >= _FOLLOW_AUTH_MAX_ATTEMPTS:
                    raise
                backoff = _FOLLOW_AUTH_BACKOFF_BASE ** (attempt - 1)
                logger.warning(f"[预检] {label} 临时失败：{type(exc).__name__}: {str(exc)[:120]}，{backoff:.1f}s 后重试")
                time.sleep(backoff)
        else:
            raise last_exc if last_exc else RuntimeError(f"[预检] {label} 未完成")

def is_password_branch_url(url: str) -> bool:
    """
    判断 authorize 重定向是否落入"密码注册分支"。

    OpenAI A/B 分流：新邮箱可能被路由到 /create-account/password
    （需要先设密码再收 OTP），而不是默认的 /email-verification 直接收码。
    """
    value = str(url or "")
    return "/create-account/password" in value or "/api/accounts/user/register" in value


def register_user(session: BrowserSession, email: str, password: str, sentinel_header: str, so_header: str = None) -> dict:
    """
    密码注册分支：设置账号密码。
    POST https://auth.openai.com/api/accounts/user/register
    body: {"username": email, "password": password}

    参考 sleep-reg gpt_register.py 的 password 分支实现。
    """
    url = "https://auth.openai.com/api/accounts/user/register"
    headers = session.get_auth_headers(referer="https://auth.openai.com/create-account/password")
    headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
    body = json.dumps({"username": email, "password": password})

    logger.info(f"[密码分支] 提交账号密码: {email}")
    resp = session.post(url, headers=headers, data=body)
    if resp.status_code != 200:
        err_code = _extract_error_code(resp)
        if err_code in _ACCOUNT_DEAD_CODES:
            raise AccountUnusableError(
                f"账号已废弃（{err_code}），邮箱不可再用", error_code=err_code,
            )
        logger.error(f"[密码分支] user/register 失败 status={resp.status_code}: {(resp.text or '')[:300]}")
        resp.raise_for_status()
    data = resp.json() if resp.text else {}
    logger.info(f"[密码分支] user/register 成功: {email}")
    return data


def follow_authorize(session: BrowserSession, authorize_url: str) -> str:
    """
    步骤4: 跟随 authorize URL 重定向。
    GET auth.openai.com/api/accounts/authorize?...

    这个请求会产生一系列重定向，建立 auth.openai.com 的 session cookies。
    遇到临时性网络错误（代理抽风 / TLS 握手失败 等）会自动重试。

    Args:
        session: 浏览器会话
        authorize_url: 从步骤3获取的 authorize URL
    """
    headers = session.get_auth_navigate_headers(referer="https://chatgpt.com/")

    last_exc: Exception | None = None
    for attempt in range(1, _FOLLOW_AUTH_MAX_ATTEMPTS + 1):
        try:
            logger.info(f"[步骤4] 跟随 authorize URL 重定向 (尝试 {attempt}/{_FOLLOW_AUTH_MAX_ATTEMPTS})...")
            resp = session.get(authorize_url, headers=headers, allow_redirects=True)
            resp.raise_for_status()
            final_url = str(getattr(resp, "url", "") or "")
            if is_password_branch_url(final_url):
                logger.info(f"[步骤4] 落入密码注册分支（create-account/password），后续走 user/register: {final_url}")
            else:
                logger.info(f"[步骤4] 重定向完成, 最终URL: {final_url}")
            return final_url
        except Exception as exc:
            last_exc = exc
            if not _is_transient_network_error(exc):
                # 非临时性错误（比如 4xx 业务错误）直接抛出，不重试
                raise
            if attempt >= _FOLLOW_AUTH_MAX_ATTEMPTS:
                break
            backoff = _FOLLOW_AUTH_BACKOFF_BASE ** (attempt - 1)
            logger.warning(
                f"[步骤4] 临时性网络错误 ({type(exc).__name__}: {str(exc)[:120]})，"
                f"{backoff:.1f}s 后重试..."
            )
            time.sleep(backoff)

    # 三次都失败：抛出最后一次异常
    raise last_exc if last_exc else RuntimeError("步骤4 重试耗尽但无异常记录")


def request_sentinel_token(session: BrowserSession, flow: str) -> dict:
    """
    步骤6/9/11: 请求 Sentinel Token。
    POST config.openai_protocol.SENTINEL_REQ_URL（默认 sentinel.openai.com，
    可通过 SENTINEL_REQ_ORIGIN 覆盖为 chatgpt.com 等）。

    Args:
        session: 浏览器会话
        flow: 流程类型
            - "username_password_create": 步骤6
            - "authorize_continue": 步骤9
            - "oauth_create_account": 步骤11

    Returns:
        sentinel 响应 JSON，包含 token、turnstile、proofofwork 等
    """
    url = SENTINEL_REQ_URL

    # SDK 版本自动发现：优先使用最新版 sdk.js；session.sentinel_sv 供
    # get_sentinel_headers 的 frame.html?sv= 与 runner --script-src 保持一致。
    _sdk_path, sdk_sv, sdk_script_src = ensure_sentinel_sdk(session=session)
    session.sentinel_sv = sdk_sv
    session.sentinel_script_src = sdk_script_src

    # 生成 p 字段（浏览器指纹），script_src 样本与 runner 实际使用的 SDK 版本对齐
    profile = dict(getattr(session, "browser_profile", None) or {})
    profile["script_src_samples"] = [sdk_script_src]
    p = generate_requirements_token(getattr(session, "sentinel_sid", session.device_id), profile=profile)
    # 记录本次 req 使用的 requirements token，供 Node runner 失败时纯 Python 降级解 turnstile
    session.sentinel_req_p = p

    # 构建请求体
    body = build_sentinel_request_body(p, session.device_id, flow)

    headers = session.get_sentinel_headers()

    logger.info(f"[Sentinel] 请求 sentinel token, flow={flow}")
    resp = session.post(url, headers=headers, data=body)
    resp.raise_for_status()

    data = resp.json()
    logger.info(f"[Sentinel] 获取 sentinel token 成功, persona={data.get('persona')}")

    if data.get("proofofwork", {}).get("required"):
        seed = data["proofofwork"]["seed"]
        difficulty = data["proofofwork"]["difficulty"]
        logger.info(f"[Sentinel] 需要 PoW: seed={seed}, difficulty={difficulty}")

    # 增强诊断：哪些反爬机制被要求
    requires = []
    if data.get("turnstile", {}).get("required"):
        requires.append("turnstile")
    if data.get("so", {}).get("required"):
        requires.append("so")
    if data.get("proofofwork", {}).get("required"):
        requires.append("pow")
    logger.info(f"[Sentinel] 服务端要求项: {requires or '无'}")

    return data


def build_sentinel_header(session: BrowserSession, sentinel_resp: dict, flow: str) -> tuple:
    """
    根据 sentinel 响应构建 openai-sentinel-token 和 openai-sentinel-so-token 请求头值。

    实现策略：把 challenge 喂给 sentinel-runner.js（Node + sdk.js 在 vm 沙箱中执行），
    让真实 SDK 自己产出包含 turnstile / so / pow 的最终 token，避免硬塞 dx 被风控拒绝。

    Args:
        session: 浏览器会话（提供 device_id 与 user_agent，必须与后续 HTTP 请求保持一致）
        sentinel_resp: sentinel/req 的响应 JSON
        flow: 流程类型，必须与请求 challenge 时传入的 flow 完全一致

    Returns:
        (sentinel_header, so_header) 元组
        sentinel_header: openai-sentinel-token 请求头的值（runner 直接产出的 JSON 字符串）
        so_header: openai-sentinel-so-token 请求头的值（若 SDK 输出含 so 字段则填充，否则为 None）
    """
    from config import USER_AGENT

    try:
        header_value = generate_sentinel_token(
            challenge=sentinel_resp,
            flow=flow,
            device_id=session.device_id,
            user_agent=(getattr(session, "browser_profile", {}) or {}).get("user_agent") or USER_AGENT,
            browser_profile=getattr(session, "browser_profile", None),
            sentinel_sid=getattr(session, "sentinel_sid", None),
            react_listening_key=getattr(session, "react_listening_key", None),
            react_container_key=getattr(session, "react_container_key", None),
            react_resources_key=getattr(session, "react_resources_key", None),
            cookie=session.auth_cookie_header() if hasattr(session, "auth_cookie_header") else f"oai-did={session.device_id}",
        )
    except Exception as exc:
        # Node VM 不可用/失败时降级：纯 Python 算 PoW + turnstile（SO 留空）。
        # 生产优先走官方 SDK；这里保证流程不因缺 Node 直接中断。
        logger.warning(f"[Sentinel] Node runner 失败({exc})，降级纯 Python 构建 sentinel token")
        return _build_sentinel_header_python(session, sentinel_resp, flow)

    # 解析 runner 输出，单独抽出 so 字段填充 openai-sentinel-so-token
    so_header = None
    try:
        parsed = json.loads(header_value)
        so_value = parsed.get("so")
        if so_value:
            so_header = json.dumps(
                {
                    "so": so_value,
                    "c": parsed.get("c", sentinel_resp.get("token", "")),
                    "id": session.device_id,
                    "flow": flow,
                },
                separators=(',', ':'),
            )
            logger.info(f"[Sentinel] 检测到 SO 字段，已构建 so-token 头")
    except (ValueError, TypeError) as exc:
        logger.warning(f"[Sentinel] runner 输出解析失败: {exc}")

    return header_value, so_header


SENTINEL_TURNSTILE_FAIL_MARKER = "sentinel_turnstile_token_failed"


def request_sentinel_header_with_retry(
    session: BrowserSession,
    flow: str,
    attempts: int = 2,
) -> tuple:
    """请求 sentinel token 并构建请求头；纯 Python turnstile 求解失败时自动重试。

    每次重试都会重新请求 /req（拿到新的 challenge/dx），多数 turnstile 求解失败是
    瞬态（dx 与当次 requirements token 绑定），重试一次即可恢复；非 turnstile 错误
    直接抛出不重试，避免掩盖真实原因。

    Args:
        session: 浏览器会话
        flow: sentinel flow（"username_password_create" / "authorize_continue" / "oauth_create_account"）
        attempts: 最大尝试次数（默认 2）

    Returns:
        (sentinel_header, so_header) 元组
    """
    attempts = max(1, int(attempts))
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            sentinel_resp = request_sentinel_token(session, flow)
            return build_sentinel_header(session, sentinel_resp, flow)
        except Exception as exc:
            last_exc = exc
            text = str(exc)
            if SENTINEL_TURNSTILE_FAIL_MARKER not in text or attempt >= attempts:
                raise
            logger.warning(
                f"[Sentinel] turnstile 求解失败（{text[:140]}），"
                f"重新请求 challenge 重试（{attempt + 1}/{attempts}）"
            )
    raise last_exc if last_exc is not None else RuntimeError("sentinel retry exhausted")


def _build_sentinel_header_python(session: BrowserSession, sentinel_resp: dict, flow: str) -> tuple:
    """
    纯 Python 降级路径：不依赖 Node / sdk.js，用本地算法计算 sentinel token。

    - PoW：core.sentinel.solve_proof_of_work（FNV-1a + nonce 爆破）
    - turnstile：core.turnstile_solver.solve_turnstile_token（纯 Python 模拟 JS VM）
    - SO token：无官方 Session Observer VM 时无法生成，保守留空

    注意：这是兜底方案，token 可能被服务端风控拒绝；日志会标记降级来源。
    """
    from core import sentinel as _sentinel
    from core.turnstile_solver import solve_turnstile_token as _solve_turnstile

    device_id = session.device_id
    sid = getattr(session, "sentinel_sid", None) or device_id
    profile = getattr(session, "browser_profile", None) or {}
    req_p = str(getattr(session, "sentinel_req_p", "") or "")

    pow_data = sentinel_resp.get("proofofwork") or {}
    if pow_data.get("required") and pow_data.get("seed"):
        answer = _sentinel.solve_proof_of_work(
            str(pow_data.get("seed") or ""),
            str(pow_data.get("difficulty") or "0"),
            sid,
            profile=profile,
        )
        p_value = "gAAAAAB" + answer
    else:
        p_value = _sentinel.generate_requirements_token(sid, profile=profile)

    t_value = ""
    turnstile_data = sentinel_resp.get("turnstile") or {}
    if turnstile_data.get("required") and turnstile_data.get("dx"):
        t_value = _solve_turnstile(str(turnstile_data.get("dx") or ""), req_p) or ""
        if not t_value:
            raise RuntimeError("sentinel_turnstile_token_failed（纯Python降级）")

    header_value = json.dumps(
        {"p": p_value, "t": t_value, "c": sentinel_resp.get("token", ""), "id": device_id, "flow": flow},
        separators=(',', ':'),
    )
    logger.warning("[Sentinel] 已使用纯 Python 降级路径构建 token（flow=%s, turnstile=%s）", flow, bool(t_value))
    return header_value, None


# ============================================================
# 密码分支专用函数（已停用，保留作备用）
# 当前 OpenAI 主流程：follow_authorize 自动跳到 /email-verification 并发 OTP，
# 不再走密码注册路径。如未来需要恢复密码注册（点击"使用密码继续"按钮的分支），
# 可参考下方实现解封即可。
# ============================================================

# def get_create_account_page(session: BrowserSession) -> None:
#     """
#     [备用] 步骤5: 访问创建账号-密码页面（密码分支）。
#     GET https://auth.openai.com/create-account/password
#     """
#     url = "https://auth.openai.com/create-account/password"
#     headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/email-verification")
#     headers["sec-fetch-site"] = "same-origin"
#
#     logger.info("[步骤5] 访问创建账号-密码页（切换密码分支）...")
#     resp = session.get(url, headers=headers, allow_redirects=True)
#     resp.raise_for_status()
#     logger.info(f"[步骤5] 创建账号-密码页访问成功, 落点: {resp.url}")


# def register_user(session: BrowserSession, email: str, password: str, sentinel_header: str) -> dict:
#     """
#     [备用] 步骤7: 提交注册请求（邮箱+密码）。
#     POST https://auth.openai.com/api/accounts/user/register
#
#     Returns:
#         注册响应 JSON，例如:
#         {
#             "continue_url": "https://auth.openai.com/api/accounts/email-otp/send",
#             "method": "GET",
#             "page": {"type": "email_otp_send", "backstack_behavior": "default"}
#         }
#     """
#     url = "https://auth.openai.com/api/accounts/user/register"
#
#     headers = session.get_auth_headers(referer="https://auth.openai.com/create-account/password")
#     headers["openai-sentinel-token"] = sentinel_header
#
#     body = json.dumps({
#         "password": password,
#         "username": email,
#     })
#
#     logger.info(f"[步骤7] 提交注册请求, 邮箱: {email}")
#     resp = session.post(url, headers=headers, data=body)
#
#     if resp.status_code != 200:
#         logger.error(f"[步骤7] 请求失败, 状态码: {resp.status_code}")
#         logger.error(f"[步骤7] 响应内容: {resp.text}")
#         resp.raise_for_status()
#
#     data = resp.json()
#     logger.info(f"[步骤7] 注册请求成功: {data.get('page', {}).get('type')}")
#     return data


# def send_email_otp(session: BrowserSession) -> None:
#     """
#     [备用] 步骤8: 触发发送邮箱验证码。
#     GET https://auth.openai.com/api/accounts/email-otp/send
#     """
#     url = "https://auth.openai.com/api/accounts/email-otp/send"
#
#     headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/create-account/password")
#     headers["sec-fetch-site"] = "same-origin"
#     headers["sec-fetch-user"] = "?1"
#
#     logger.info("[步骤8] 触发发送邮箱验证码...")
#     resp = session.get(url, headers=headers, allow_redirects=True)
#     logger.info(f"[步骤8] 验证码发送请求完成, 状态码: {resp.status_code}")


def navigate_about_you(session: BrowserSession, about_url: str | None = None) -> str:
    """进入 about-you 页面状态；服务端未返回 continue_url 时使用默认页面 URL 兜底。"""
    url = str(about_url or "https://auth.openai.com/about-you")
    if url.startswith("/"):
        url = "https://auth.openai.com" + url
    headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/email-verification")
    headers["sec-fetch-site"] = "same-origin"
    logger.info("[步骤10.5] 导航到 about-you 页面，建立资料页状态")
    resp = session.get(url, headers=headers, allow_redirects=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"about-you 导航失败 status={resp.status_code}: {(resp.text or '')[:240]}")
    final_url = str(getattr(resp, "url", "") or url)
    if "/api/accounts/user/register" in final_url or "/create-account/password" in final_url:
        raise RuntimeError(f"about-you 导航落入旧密码注册路径: {final_url}")
    logger.info(f"[步骤10.5] about-you 导航完成，落点: {final_url}")
    return final_url


def send_email_otp(
    session: BrowserSession,
    referer: str = "https://auth.openai.com/email-verification",
    sentinel_header: str | None = None,
    so_header: str | None = None,
) -> None:
    """重新发送邮箱验证码。用于验证码错误/过期后重新取码。

    参考 sleep-reg gpt_register.py::_send_otp：重新发送时可携带
    openai-sentinel-token / openai-sentinel-so-token（复用 authorize_continue
    阶段的 token 或新 mint 均可）。本地默认不带（HAR 对齐），由
    SEND_SENTINEL_ON_EMAIL_OTP_SEND 开关开启 A/B。
    """
    url = "https://auth.openai.com/api/accounts/email-otp/send"
    headers = session.get_auth_navigate_headers(referer=referer)
    headers["sec-fetch-site"] = "same-origin"
    headers["sec-fetch-user"] = "?1"
    if sentinel_header:
        headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
        logger.info("[OTP] 重新发送验证码已附加 openai-sentinel-so-token 头")
    logger.info("[OTP] 请求重新发送邮箱验证码...")
    resp = session.get(url, headers=headers, allow_redirects=True)
    if resp.status_code >= 400:
        logger.warning("[OTP] 重新发送验证码失败 status=%s: %s", resp.status_code, (resp.text or '')[:300])
        resp.raise_for_status()
    logger.info("[OTP] 重新发送验证码请求完成，status=%s", resp.status_code)


def validate_email_otp(session: BrowserSession, code: str, sentinel_header: str | None = None, so_header: str | None = None) -> dict:
    """
    步骤10: 提交邮箱验证码验证。
    POST https://auth.openai.com/api/accounts/email-otp/validate

    Args:
        session: 浏览器会话
        code: 6位数字验证码
        sentinel_header: openai-sentinel-token 头的值（authorize_continue flow）

    Returns:
        验证响应 JSON，例如:
        {
            "continue_url": "https://auth.openai.com/about-you",
            "method": "GET",
            "page": {"type": "about_you", "backstack_behavior": "default"}
        }
    """
    url = "https://auth.openai.com/api/accounts/email-otp/validate"

    headers = session.get_auth_headers(referer="https://auth.openai.com/email-verification")
    if sentinel_header:
        headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
        logger.info("[步骤10] 已添加 openai-sentinel-so-token 头")

    body = json.dumps({"code": code})

    logger.info(f"[步骤10] 提交邮箱验证码: {code}")
    resp = session.post(url, headers=headers, data=body)

    if resp.status_code != 200:
        logger.error(f"[步骤10] 请求失败, 状态码: {resp.status_code}")
        logger.error(f"[步骤10] 响应内容: {resp.text}")
        # 先看是不是"账号已废"——这类邮箱再试也没用，单独抛出让上层标 failed
        err_code = _extract_error_code(resp)
        if err_code in _ACCOUNT_DEAD_CODES:
            raise AccountUnusableError(
                f"账号已废弃（{err_code}），邮箱不可再用", error_code=err_code,
            )
        low = (resp.text or '').lower()
        if resp.status_code in (400, 401, 422) and any(k in low for k in (
            'invalid', 'incorrect', 'expired', 'code', 'otp', 'verification',
            '验证码', '認証コード', '確認コード', 'コード'
        )):
            raise EmailOtpInvalidError(f"邮箱验证码无效或已过期: status={resp.status_code}, body={(resp.text or '')[:240]}")
        resp.raise_for_status()

    data = resp.json()
    page_type = data.get('page', {}).get('type')
    logger.info(f"[步骤10] 验证码验证成功: {page_type}")
    logger.info(f"[步骤10] 验证响应摘要: {json.dumps(data, ensure_ascii=False)[:1000]}")
    return data


def create_account(session: BrowserSession, name: str, birthday: str, sentinel_header: str, so_header: str = None) -> dict:
    """
    步骤12: 提交用户信息，完成注册。
    POST https://auth.openai.com/api/accounts/create_account

    Args:
        session: 浏览器会话
        name: 用户显示名称
        birthday: 生日，格式 "YYYY-MM-DD"
        sentinel_header: openai-sentinel-token 头的值
        so_header: openai-sentinel-so-token 头的值

    Returns:
        创建账号响应 JSON
    """
    url = "https://auth.openai.com/api/accounts/create_account"

    headers = session.get_auth_headers(referer="https://auth.openai.com/about-you")
    headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
        logger.info(f"[步骤12] 已添加 openai-sentinel-so-token 头")

    body = json.dumps({
        "name": name,
        "birthdate": birthday,
    })

    logger.info(f"[步骤12] 提交用户信息, 名称: {name}, 生日: {birthday}")
    resp = session.post(url, headers=headers, data=body)

    if resp.status_code != 200:
        logger.error(f"[步骤12] 请求失败, 状态码: {resp.status_code}")
        logger.error(f"[步骤12] 响应内容: {resp.text}")
        resp.raise_for_status()

    data = resp.json()
    logger.info("[步骤12] 创建接口返回成功，等待 OAuth 回调建立登录态")
    return data
