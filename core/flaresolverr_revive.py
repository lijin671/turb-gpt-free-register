# -*- coding: utf-8 -*-
"""FlareSolverr session 模式的 token 复活。

核心原理：FlareSolverr 的 Chrome 浏览器可以过 CF challenge，
在同一 session 中执行 GET → POST 链路时 cookie 自动保持。
curl_cffi 的 TLS 指纹在 POST 时被 CF 识别为 bot，但真实 Chrome 不会。

用法：flaresolverr_revive_account(email, proxy) → {"ok", "access_token", ...}
"""
from __future__ import annotations

import json
import logging
import os
import time
from urllib import request as urllib_request
from urllib.parse import urlencode, urlparse

logger = logging.getLogger(__name__)

_FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://127.0.0.1:18191").rstrip("/")
_FLARESOLVERR_TIMEOUT = int(os.environ.get("FLARESOLVERR_TIMEOUT", "60"))


def _fs_request(payload: dict, timeout: int = 60) -> dict | None:
    """调用 FlareSolverr API。"""
    payload["maxTimeout"] = int(timeout * 1000)
    body = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        f"{_FLARESOLVERR_URL}/v1",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout + 5) as resp:
            data = json.loads(resp.read())
        if str(data.get("status", "")).lower() != "ok":
            logger.warning("[FS-Revive] FlareSolverr 返回非 ok: %s", str(data)[:200])
            return None
        return data.get("solution", {})
    except Exception as exc:
        logger.warning("[FS-Revive] FlareSolverr 请求失败: %s: %s", type(exc).__name__, exc)
        return None


def _extract_cookies(solution: dict) -> dict:
    """从 FlareSolverr solution 中提取 cookies。"""
    cookies = {}
    for c in solution.get("cookies", []):
        name = c.get("name", "")
        value = c.get("value", "")
        domain = c.get("domain", "")
        if name and value:
            cookies[name] = value
    return cookies


def flaresolverr_revive_account(email: str, proxy: str = "", device_id: str = "") -> dict:
    """用 FlareSolverr session 模式执行完整 reauth 流程。

    流程：
    1. 创建 FlareSolverr session
    2. GET authorize URL → 触发 OTP 发送
    3. 等待 OTP 到达（通过 manymail）
    4. POST email-otp/validate → 获取 continue_url
    5. GET continue_url → 刷新 session-token
    6. GET chatgpt.com/api/auth/session → 获取新 accessToken
    7. 清理 session
    """
    from core.chatgpt_auth import build_direct_authorize_url
    from core.session import BrowserSession

    # 需要一个 turb BrowserSession 来生成 authorize URL（PKCE 等）
    bs = BrowserSession(proxy=proxy, detect_exit_geo=False, device_id=device_id or None)
    auth_url = build_direct_authorize_url(bs, email)
# BrowserSession 无需显式关闭

    # FlareSolverr proxy 格式转换
    fs_proxy = proxy.replace("127.0.0.1:2260", "resin:2260") if proxy else ""
    proxy_dict = {"url": fs_proxy} if fs_proxy else None

    session_id = f"revive_{int(time.time())}_{email.split('@')[0][:8]}"

    try:
        # 1. 创建 session
        logger.info("[FS-Revive] %s 创建 FlareSolverr session: %s", email, session_id)
        _fs_request({"cmd": "sessions.create", "session": session_id}, timeout=10)

        # 2. GET authorize URL → 触发 OTP 发送
        logger.info("[FS-Revive] %s GET authorize URL...", email)
        params = {"cmd": "request.get", "url": auth_url, "session": session_id}
        if proxy_dict:
            params["proxy"] = proxy_dict
        sol = _fs_request(params, timeout=_FLARESOLVERR_TIMEOUT)
        if not sol:
            return {"ok": False, "email": email, "message": "FlareSolverr GET authorize 失败"}

        landing_url = sol.get("url", "")
        logger.info("[FS-Revive] %s 落点 URL: %s", email, landing_url[:100])

        cookies = _extract_cookies(sol)
        logger.info("[FS-Revive] %s cookies: %s", email, list(cookies.keys()))

        # 检查是否落到 email-verification 页面
        if "email-verification" not in landing_url and "error" in landing_url:
            # 可能是 rate_limit_exceeded
            return {"ok": False, "email": email, "message": f"authorize 落点异常: {landing_url[:100]}"}

        # 3. 等待 OTP
        from core.email_provider import wait_for_otp
        otp_after_ts = time.time()

        # 恢复 manymail 上下文
        try:
            from core.email_provider import resolve_email_source
            email_source = resolve_email_source(email)
            if email_source == "manymail":
                from core.manymail_client import get_account_context, restore_context
                from core import db
                ctx = get_account_context(email)
                if ctx is None:
                    acc = db.get_account_by_email(email)
                    import json as _json
                    extra_raw = acc.get("extra_json", "") if acc else ""
                    extra = _json.loads(extra_raw) if extra_raw else {}
                    mm_creds = extra.get("manymail", {})
                    password = mm_creds.get("password", "") if isinstance(mm_creds, dict) else ""
                    if password:
                        restore_context(email, password=password)
                        logger.info("[FS-Revive] %s 已恢复 manymail 上下文", email)
        except Exception as exc:
            logger.warning("[FS-Revive] %s 恢复 manymail 上下文失败: %s", email, exc)

        logger.info("[FS-Revive] %s 等待 OTP...", email)
        otp_code = wait_for_otp(email, after_ts=otp_after_ts)
        if not otp_code:
            return {"ok": False, "email": email, "message": "等待 OTP 超时"}
        logger.info("[FS-Revive] %s 收到 OTP: %s", email, otp_code)

        # 4. POST email-otp/validate
        validate_url = "https://auth.openai.com/api/accounts/email-otp/validate"
        post_data = json.dumps({"code": otp_code})
        logger.info("[FS-Revive] %s POST email-otp/validate...", email)
        params = {
            "cmd": "request.post",
            "url": validate_url,
            "postData": post_data,
            "session": session_id,
        }
        if proxy_dict:
            params["proxy"] = proxy_dict
        sol = _fs_request(params, timeout=_FLARESOLVERR_TIMEOUT)
        if not sol:
            return {"ok": False, "email": email, "message": "FlareSolverr POST validate 失败"}

        response_text = sol.get("response", "")
        landing_url = sol.get("url", "")
        logger.info("[FS-Revive] %s POST validate 落点: %s, status: %s", email, landing_url[:80], sol.get("status"))

        # 解析 JSON 响应
        try:
            resp_data = json.loads(response_text)
        except json.JSONDecodeError:
            # 可能是 HTML（重定向页面）
            return {"ok": False, "email": email, "message": f"validate 响应非 JSON: {response_text[:100]}"}

        if resp_data.get("error"):
            err_code = resp_data["error"].get("code", "")
            err_msg = resp_data["error"].get("message", "")
            return {"ok": False, "email": email, "message": f"validate 错误: {err_code} - {err_msg}"}

        continue_url = resp_data.get("continue_url", "")
        if not continue_url:
            return {"ok": False, "email": email, "message": f"validate 响应缺少 continue_url: {resp_data}"}

        logger.info("[FS-Revive] %s validate 成功, continue_url: %s...", email, continue_url[:60])

        # 5. GET continue_url → 刷新 session-token
        params = {
            "cmd": "request.get",
            "url": continue_url,
            "session": session_id,
        }
        if proxy_dict:
            params["proxy"] = proxy_dict
        _fs_request(params, timeout=_FLARESOLVERR_TIMEOUT)

        # 6. GET chatgpt.com/api/auth/session → 获取新 accessToken
        session_url = "https://chatgpt.com/api/auth/session"
        params = {
            "cmd": "request.get",
            "url": session_url,
            "session": session_id,
        }
        if proxy_dict:
            params["proxy"] = proxy_dict
        sol = _fs_request(params, timeout=_FLARESOLVERR_TIMEOUT)
        if not sol:
            return {"ok": False, "email": email, "message": "FlareSolverr GET session 失败"}

        response_text = sol.get("response", "")
        try:
            session_data = json.loads(response_text)
        except json.JSONDecodeError:
            return {"ok": False, "email": email, "message": f"session 响应非 JSON: {response_text[:100]}"}

        new_token = session_data.get("accessToken", "")
        if not new_token:
            return {"ok": False, "email": email, "message": "session 响应缺少 accessToken"}

        logger.info("[FS-Revive] %s 成功获取新 accessToken: %s...", email, new_token[:32])
        return {"ok": True, "email": email, "access_token": new_token, "message": "FlareSolverr 复活成功"}

    except Exception as exc:
        logger.warning("[FS-Revive] %s 异常: %s: %s", email, type(exc).__name__, exc)
        return {"ok": False, "email": email, "message": f"{type(exc).__name__}: {exc}"}
    finally:
        # 7. 清理 session
        try:
            _fs_request({"cmd": "sessions.destroy", "session": session_id}, timeout=10)
            logger.debug("[FS-Revive] %s session 已清理: %s", email, session_id)
        except Exception:
            pass
