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


def revive_account(email: str, otp_code: str | None = None, *, session=None, retry_count: int = 0) -> dict:
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
    # 使用 chrome131 impersonate — chrome146 的 POST TLS 指纹被 auth.openai.com CF 403
    from config.browser import IMPERSONATE as _default_imp
    _revive_impersonate = "chrome131" if _default_imp == "chrome146" else _default_imp
    if session is None:
        session = BrowserSession(proxy=proxy, detect_exit_geo=False, device_id=device_id or None)
        # 覆盖 impersonate 为 chrome131 — chrome146 的 POST TLS 指纹被 auth.openai.com CF 403
        if _revive_impersonate != _default_imp:
            from curl_cffi.requests import Session as _CurlSession
            old_cookies = dict(session.session.cookies) if hasattr(session.session, 'cookies') else {}
            old_timeout = getattr(session.session, 'timeout', 30)
            session.session = _CurlSession(impersonate=_revive_impersonate)
            session.session.proxies = {"http": proxy, "https": proxy} if proxy else {}
            session.session.timeout = old_timeout
            # 恢复 cookies（oai-did 等）
            for name, value in old_cookies.items():
                try:
                    session.session.cookies.set(name, value)
                except Exception:
                    pass
            # 重新设置 oai-did cookie
            if device_id:
                for domain in [".auth.openai.com", ".chatgpt.com", ".openai.com"]:
                    session.session.cookies.set("oai-did", device_id, domain=domain, path="/")
            # 更新 browser_profile 中的 UA 以匹配 chrome131（避免 TLS 指纹和 UA 不匹配被 CF 检测）
            # chrome131 对应 Chrome 131，让 curl_cffi 自动设置 UA（不覆盖）
            if hasattr(session, "browser_profile") and isinstance(session.browser_profile, dict):
                # 设置 chrome131 对应的 UA 和 sec-ch-ua（避免 TLS 指纹和 UA 不匹配被 CF 检测）
                session.browser_profile["user_agent"] = (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                )
                session.browser_profile["send_client_hints"] = True
                session.browser_profile["sec_ch_ua"] = '"Google Chrome";v="131", "Chromium";v="131", "Not)A;Brand";v="24"'
                session.browser_profile["sec_ch_ua_mobile"] = "?0"
                session.browser_profile["sec_ch_ua_platform"] = '"macOS"' 

    # 预检 auth.openai.com 可达性 — 如果 CF 403 则换代理（和注册流程 network_preflight 一致）
    for _ in range(3):
        try:
            preflight_resp = session.get(
                "https://auth.openai.com/log-in",
                headers=session.get_auth_navigate_headers(referer="https://chatgpt.com/login"),
                allow_redirects=True,
            )
            if getattr(preflight_resp, "status_code", 0) < 400:
                break
            # CF 403 → 换代理
            logger.warning("[复活] auth.openai.com 预检 HTTP %s，换代理重试...", preflight_resp.status_code)
            session.blocked_until = 0
            session.blocked_reason = ""
            session.cf_challenge_count = 0
            from config.proxy import pick_proxy
            new_proxy = pick_proxy()
            if new_proxy and new_proxy != proxy:
                proxy = new_proxy
                session.proxy = new_proxy
                session.session.proxies = {"http": new_proxy, "https": new_proxy}
                logger.info("[复活] 换代理: %s...", new_proxy[:50])
                break
        except Exception as pf_exc:
            logger.warning("[复活] 预检失败: %s，换代理重试...", pf_exc)
            session.blocked_until = 0
            session.blocked_reason = ""
            session.cf_challenge_count = 0
            from config.proxy import pick_proxy
            new_proxy = pick_proxy()
            if new_proxy and new_proxy != proxy:
                proxy = new_proxy
                session.proxy = new_proxy
                session.session.proxies = {"http": new_proxy, "https": new_proxy}
                logger.info("[复活] 换代理: %s...", new_proxy[:50])
                break

    try:
        reauth_otp_after_ts = time.time()
        logger.info("[复活] %s 发起重认证（proxy=%s device_id=%s）", email, proxy or "-", device_id or "-")

        # 直接走 build_direct_authorize_url（绕过 chatgpt.com CF 403 问题）
        # 这和注册流程的降级路径一致，走 auth.openai.com 短路径
        from core.chatgpt_auth import build_direct_authorize_url
        auth_url = build_direct_authorize_url(session, email)
        logger.info("[复活] 使用 build_direct_authorize_url 短路径（绕过 chatgpt.com CF）")
        human_delay("api")
        _follow_reauth(session, auth_url)
        human_delay("navigate")

        if otp_code is None:
            # manymail 凭据（用于 Playwright 回退时收 OTP）
            manymail_creds = None
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
                        manymail_creds = mm_creds if isinstance(mm_creds, dict) else None
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

        # 重置熔断（OTP 提交可能因 CF 临时 403 被熔断）
        session.blocked_until = 0
        session.blocked_reason = ""
        session.cf_challenge_count = 0
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
        exc_str = str(exc)
        # 如果是 CF 403 / 连接超时 / 409，尝试 FlareSolverr session 模式
        if ("403" in exc_str or "409" in exc_str or "timeout" in exc_str.lower() or 
            "CONNECT tunnel failed" in exc_str or "504" in exc_str):
            if retry_count < 1:
                # 先尝试 FlareSolverr session 模式（真实 Chrome 过 CF）
                logger.info("[复活] %s curl_cffi 失败（%s），尝试 FlareSolverr session 模式...", email, type(exc).__name__)
                try:
                    from core.flaresolverr_revive import flaresolverr_revive_account
                    fs_result = flaresolverr_revive_account(email, proxy=proxy, device_id=device_id)
                    if fs_result.get("ok"):
                        # 成功 — 更新 DB + 重导出
                        from core import db
                        new_token = fs_result["access_token"]
                        db.update_account_access_token(email, new_token, note="FlareSolverr 复活成功")
                        # 重导出
                        reexport = None
                        try:
                            from config import export as _export_cfg
                            if _export_cfg.AUTO_REEXPORT_AFTER_REVIVE:
                                from core.chatgpt2api_export import export_account_to_chatgpt2api
                                reexport = export_account_to_chatgpt2api(email, new_token, proxy=str(acc.get("proxy_used") or ""))
                        except Exception as exc2:
                            reexport = {"ok": False, "message": f"{type(exc2).__name__}: {exc2}"}
                        # CPA 重导入
                        reimport = None
                        try:
                            from config import export as _export_cfg
                            if _export_cfg.AUTO_REIMPORT_AFTER_REVIVE:
                                from core.cpa_manager_import import import_single_account
                                reimport = import_single_account(
                                    email, new_token,
                                    _export_cfg.CPA_MANAGER_PLUS_BASE,
                                    _export_cfg.CPA_MANAGER_PLUS_KEY,
                                    verify=bool(_export_cfg.CPA_IMPORT_VERIFY_MODELS),
                                )
                        except Exception as exc2:
                            reimport = {"ok": False, "message": f"{type(exc2).__name__}: {exc2}"}
                        logger.info("[复活] %s FlareSolverr 模式成功", email)
                        return {
                            "ok": True, "email": email,
                            "access_token": new_token, "updated": True,
                            "reexport": reexport, "reimport": reimport,
                            "message": "FlareSolverr session 模式复活成功",
                        }
                    else:
                        logger.warning("[复活] %s FlareSolverr 模式失败: %s", email, fs_result.get("message", ""))
                except Exception as fs_exc:
                    logger.warning("[复活] %s FlareSolverr 模式异常: %s: %s", email, type(fs_exc).__name__, fs_exc)

                # FlareSolverr 失败后尝试 Playwright 方式（真实 Chrome 过 CF + JS fetch POST）
                try:
                    from core.playwright_revive import playwright_revive_account as _pw_revive
                    logger.info("[复活] %s 尝试 Playwright 方式（真实 Chrome + JS fetch）...", email)
                    pw_result = _pw_revive(
                        email=email,
                        proxy=proxy,
                        device_id=device_id,
                        manymail_creds=manymail_creds,
                        timeout=120,
                    )
                    if pw_result.get("ok"):
                        new_token = pw_result.get("access_token", "")
                        logger.info("[复活] %s ✅ Playwright 模式成功", email)
                        db.update_account_access_token(email, new_token, note="Playwright 复活成功")
                        reexport = False
                        reimport = False
                        try:
                            from config.export import AUTO_REEXPORT_AFTER_REVIVE, AUTO_REIMPORT_AFTER_REVIVE
                            reexport = AUTO_REEXPORT_AFTER_REVIVE
                            reimport = AUTO_REIMPORT_AFTER_REVIVE
                        except Exception:
                            pass
                        if reexport:
                            try:
                                from core.chatgpt2api_export import export_account_to_chatgpt2api
                                export_account_to_chatgpt2api(email=email, access_token=new_token)
                            except Exception:
                                pass
                        if reimport:
                            try:
                                from core.cpa_manager_import import import_single_account
                                from config.export import CPA_MANAGER_PLUS_BASE, CPA_MANAGER_PLUS_KEY
                                import_single_account(email=email, access_token=new_token,
                                    base=CPA_MANAGER_PLUS_BASE, key=CPA_MANAGER_PLUS_KEY)
                            except Exception:
                                pass
                        return {
                            "ok": True, "email": email,
                            "access_token": new_token,
                            "reexport": reexport, "reimport": reimport,
                            "message": "Playwright 模式复活成功",
                        }
                    else:
                        logger.warning("[复活] %s Playwright 模式失败: %s", email, pw_result.get("message", ""))
                except Exception as pw_exc:
                    logger.warning("[复活] %s Playwright 模式异常: %s: %s", email, type(pw_exc).__name__, pw_exc)

                # Playwright 也失败后换代理重试
                if retry_count < 2:
                    logger.warning("[复活] %s 失败（%s），换代理重试 (%d/2)...", email, type(exc).__name__, retry_count + 1)
                    if own_session:
                        try:
                            session.close()
                        except Exception:
                            pass
                    from config.proxy import pick_proxy
                    new_proxy = pick_proxy()
                    new_session = BrowserSession(proxy=new_proxy, detect_exit_geo=False, device_id=device_id or None)
                    return revive_account(email, otp_code=None, session=new_session, retry_count=retry_count + 1)
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
