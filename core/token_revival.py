# -*- coding: utf-8 -*-
"""Token 复活：对已注册账号走邮箱重认证重新获取 access_token。

背景：实测注册后 access_token 常 ~30min 被服务端吊销（token_revoked），但账号本身仍有效；
account_export 的 2FA 重认证链路（reauth → 邮箱 OTP → validate → exchange）能拿回全新
accessToken，此处复用该链路做"复活"，缓解"注册即吊销、号池快速见底"问题。

要求（不满足时按失败返回，不破坏账号）：
- 邮箱仍可收件（email_source 对应通道可达，能收到重认证 OTP）
- chatgpt.com 会话 cookie 仍有效（access_token 吊销 ≠ 会话吊销）
- 全程使用账号自身 proxy_used + device_id（避免跨 IP/换设备触发吊销）

用法：revive_account(email) / revive_accounts([...])；CLI 见 tools/revive_accounts.py。
"""
import logging
import time

logger = logging.getLogger(__name__)


def revive_account(email: str, otp_code: str | None = None, *, session=None) -> dict:
    """复活单个账号 token。

    Args:
        email: 账号邮箱（DB 中须存在）
        otp_code: 重认证邮箱验证码；None 时按 email_source 自动等待
        session: 可选外部 BrowserSession（测试/复用连接用）

    Returns: {"ok", "email", "message", "access_token", "updated"}。
    """
    from core import db
    acc = db.get_account_by_email(email)
    if not acc:
        return {"ok": False, "email": email, "message": "账号不存在"}
    if not str(acc.get("access_token") or "").strip():
        return {"ok": False, "email": email, "message": "账号无 access_token，无法复活"}

    proxy = str(acc.get("proxy_used") or "").strip()
    device_id = str(acc.get("device_id") or "").strip()
    from core.session import BrowserSession
    from core.account_export import (
        _trigger_reauth,
        _follow_reauth,
        _validate_reauth_otp,
        _exchange_new_token,
    )
    from core.email_provider import wait_for_otp
    from core.humanize import delay as human_delay

    own_session = session is None
    session = session or BrowserSession(proxy=proxy, detect_exit_geo=False, device_id=device_id or None)
    try:
        reauth_otp_after_ts = time.time()
        logger.info("[复活] %s 发起重认证（proxy=%s device_id=%s）", email, proxy or "-", device_id or "-")
        try:
            auth_url = _trigger_reauth(session, email)
        except Exception as trigger_exc:
            # chatgpt.com/api/auth/csrf 被 CF 403 拦截时，直接走 auth.openai.com authorize
            if "403" in str(trigger_exc) or "CF" in str(trigger_exc).upper():
                logger.warning("[复活] chatgpt.com CSRF 403，改为直接走 auth.openai.com authorize")
                # 重置熔断，让后续 auth.openai.com 请求能通过
                session.blocked_until = 0
                session.blocked_reason = ""
                session.cf_challenge_count = 0
                from urllib.parse import urlencode
                authorize_params = {
                    "client_id": "app_X8zY6vW2pQ9tR3dE7nK1jL5gH",
                    "scope": "openid email profile offline_access model.request model.read organization.read organization.write",
                    "response_type": "code",
                    "redirect_uri": "https://chatgpt.com/api/auth/callback/openai",
                    "audience": "https://api.openai.com/v1",
                    "device_id": device_id,
                    "connection": "password",
                    "login_hint": email,
                    "reauth": "password",
                    "max_age": "0",
                    "ext-oai-did": device_id,
                    "prompt": "login",
                    "screen_hint": "login_or_signup",
                }
                auth_url = "https://auth.openai.com/api/accounts/authorize?" + urlencode(authorize_params)
            else:
                raise
        human_delay("api")
        _follow_reauth(session, auth_url)
        human_delay("navigate")

        if otp_code is None:
            # 恢复 manymail 进程内上下文（独立进程时 _CONTEXT_CACHE 为空）
            try:
                from core.email_provider import resolve_email_source
                email_source = resolve_email_source(email)
                if email_source == "manymail":
                    from core.manymail_client import get_account_context, restore_context
                    ctx = get_account_context(email)
                    if ctx is None:
                        # 从 DB 的 extra_json 中恢复 password
                        import json as _json
                        extra_raw = acc.get("extra_json", "")
                        extra = _json.loads(extra_raw) if extra_raw else {}
                        # manymail 凭据存在 extra['manymail']['password']
                        mm_creds = extra.get("manymail", {})
                        password = mm_creds.get("password", "") if isinstance(mm_creds, dict) else ""
                        if password:
                            restore_context(email, password=password)
                            logger.info("[复活] 已恢复 manymail 上下文: %s", email)
                        else:
                            logger.warning("[复活] manymail 密码缺失，无法恢复上下文: %s", email)
            except Exception as exc:
                logger.warning("[复活] 恢复 manymail 上下文失败: %s", exc)
            otp_code = wait_for_otp(email, after_ts=reauth_otp_after_ts)
            if not otp_code:
                return {"ok": False, "email": email, "message": "等待重认证 OTP 超时"}
        human_delay("otp_input")

        continue_url = _validate_reauth_otp(session, otp_code)
        human_delay("api")
        new_token = _exchange_new_token(session, continue_url)
        if not new_token:
            return {"ok": False, "email": email, "message": "交换新 token 返回空"}

        updated = db.update_account_access_token(email, new_token, note="token 复活成功")
        # 复活换的新 token 同样寿命短，配置开启时立即重导出到 ChatGPT-to-API 生产池
        reexport = None
        try:
            from config import export as _export_cfg
            if _export_cfg.AUTO_REEXPORT_AFTER_REVIVE:
                from core.chatgpt2api_export import export_account_to_chatgpt2api
                reexport = export_account_to_chatgpt2api(
                    email,
                    new_token,
                    proxy=str(acc.get("proxy_used") or ""),
                )
        except Exception as exc:
            logger.warning("[复活] %s 重导出失败（不影响复活）：%s: %s", email, type(exc).__name__, exc)
            reexport = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        # CPA Manager Plus 重导入（新 token 同样寿命短，趁热写生产面板）
        reimport = None
        try:
            from config import export as _export_cfg
            if _export_cfg.AUTO_REIMPORT_AFTER_REVIVE:
                from core.cpa_manager_import import import_single_account
                reimport = import_single_account(
                    email,
                    new_token,
                    _export_cfg.CPA_MANAGER_PLUS_BASE,
                    _export_cfg.CPA_MANAGER_PLUS_KEY,
                    verify=bool(_export_cfg.CPA_IMPORT_VERIFY_MODELS),
                )
        except Exception as exc:
            logger.warning("[复活] %s 重导入 CPA 失败（不影响复活）：%s: %s", email, type(exc).__name__, exc)
            reimport = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        logger.info("[复活] %s 成功 updated=%s token=%s...", email, updated, new_token[:32])
        return {
            "ok": True,
            "email": email,
            "access_token": new_token,
            "updated": updated,
            "reexport": reexport,
            "reimport": reimport,
            "message": "token 复活成功",
        }
    except Exception as exc:
        logger.warning("[复活] %s 失败：%s: %s", email, type(exc).__name__, exc)
        return {"ok": False, "email": email, "message": f"{type(exc).__name__}: {exc}"}
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass


def revive_accounts(emails: list[str], otp_codes: dict | None = None) -> list[dict]:
    """批量复活账号 token。otp_codes: {email: code}，缺失的自动等邮箱 OTP。"""
    results = []
    for email in [str(e or "").strip() for e in (emails or []) if str(e or "").strip()]:
        code = (otp_codes or {}).get(email)
        results.append(revive_account(email, otp_code=code))
    return results
