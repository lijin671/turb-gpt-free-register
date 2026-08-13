# -*- coding: utf-8 -*-
"""Playwright token 复活模块。

用真实 Chromium + resin 代理过 CF challenge，走完整 NextAuth 流程复活 token。

流程：
1. 启动 Chromium（代理: resin Pokemon/Premium sid）
2. GET auth.openai.com/log-in → 过 CF challenge（等待 ~10s）
3. GET authorize URL（PKCE）→ 落到 email-verification → 触发 OTP 发送
4. manymail 收 OTP
5. JS fetch POST /api/accounts/email-otp/validate → 验证 OTP
6. GET /api/auth/session → 获取新 accessToken

相比 curl_cffi 的 build_direct_authorize_url：
- curl_cffi POST validate 被 CF 403（TLS 指纹被识别）
- Playwright 用真实 Chrome，过 CF 后 JS fetch 自动携带 __cf_bm + cf_clearance
"""
import asyncio
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)


async def _playwright_revive_account(
    email: str,
    proxy: str,
    device_id: str,
    manymail_creds: dict | None = None,
    otp_callback=None,
    timeout: int = 120,
) -> dict[str, Any]:
    """用 Playwright 复活单个账号 token。

    Args:
        email: 账号邮箱
        proxy: 代理 URL（http://user:pass@host:port）
        device_id: 账号的 device_id
        manymail_creds: manymail 凭据（domain, password, token）
        otp_callback: 异步函数，接收 email，返回 OTP code
        timeout: 总超时秒数

    Returns: {"ok": bool, "email": str, "message": str, "access_token": str}
    """
    from playwright.async_api import async_playwright
    from urllib.parse import urlparse, urlencode
    import secrets as _secrets
    import hashlib, base64

    proxy_parsed = urlparse(proxy)
    proxy_config = {
        "server": f"{proxy_parsed.scheme}://{proxy_parsed.hostname}:{proxy_parsed.port}",
        "username": proxy_parsed.username or "",
        "password": proxy_parsed.password or "",
    }

    logger.info(f"[PW-Revive] {email} 启动 Chromium（proxy={proxy_config['server']}）...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            proxy=proxy_config,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )

            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = {runtime: {}};
            """)

            page = await context.new_page()

            # Step 1: GET auth.openai.com/log-in → 过 CF
            logger.info(f"[PW-Revive] {email} Step 1: GET auth.openai.com/log-in ...")
            resp = await page.goto("https://auth.openai.com/log-in", wait_until="domcontentloaded", timeout=30000)
            content = await page.content()
            if "Just a moment" in content:
                logger.info(f"[PW-Revive] {email} CF challenge, 等待 15s...")
                await page.wait_for_timeout(15000)
                content = await page.content()
                if "Just a moment" in content:
                    return {"ok": False, "email": email, "message": "CF challenge 未通过"}

            cookies = await context.cookies()
            cookie_names = [c["name"] for c in cookies]
            logger.info(f"[PW-Revive] {email} Step 1 OK: cookies={cookie_names}")

            if "cf_clearance" not in cookie_names and "__cf_bm" not in cookie_names:
                logger.warning(f"[PW-Revive] {email} 缺少 CF cookies")
                # 继续尝试

            # Step 2: 构造 authorize URL（PKCE）
            code_verifier = "".join(_secrets.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_") for _ in range(64))
            code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip("=")

            from config.openai_protocol import (
                OPENAI_AUDIENCE,
                OPENAI_CLIENT_ID,
                OPENAI_REDIRECT_URI,
                OPENAI_SCOPE,
            )

            def _rand(length: int) -> str:
                return "".join(_secrets.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_") for _ in range(length))

            params = {
                "issuer": "https://auth.openai.com",
                "client_id": OPENAI_CLIENT_ID,
                "scope": OPENAI_SCOPE,
                "response_type": "code",
                "redirect_uri": OPENAI_REDIRECT_URI,
                "audience": OPENAI_AUDIENCE,
                "device_id": device_id,
                "prompt": "login",
                "ext-oai-did": device_id,
                "screen_hint": "login_or_signup",
                "login_hint": email,
                "ccaps": "login_methods",
                "max_age": "0",
                "response_mode": "query",
                "state": _rand(32),
                "nonce": _rand(32),
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
            authorize_url = "https://auth.openai.com/api/accounts/authorize?" + urlencode(params)

            # Step 3: GET authorize URL → 应该重定向到 email-verification → 触发 OTP
            logger.info(f"[PW-Revive] {email} Step 2: GET authorize URL ...")
            try:
                resp = await page.goto(authorize_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                # 可能是重定向到 chatgpt.com 导致超时，检查当前 URL
                logger.info(f"[PW-Revive] {email} authorize 超时/重定向: {type(e).__name__}, 检查 URL...")

            final_url = page.url
            logger.info(f"[PW-Revive] {email} 落点 URL: {final_url}")

            # 等待 CF challenge 通过（如果有的话）
            content = await page.content()
            if "Just a moment" in content:
                logger.info(f"[PW-Revive] {email} authorize CF challenge, 等待 15s...")
                await page.wait_for_timeout(15000)

            # 如果落在 email-verification 页面，说明 OTP 已发送
            if "email-verification" in page.url or "email" in page.url.lower():
                logger.info(f"[PW-Revive] {email} ✅ 落在 email-verification，OTP 应已发送")
            else:
                logger.warning(f"[PW-Revive] {email} 未落在 email-verification，当前: {page.url}")
                # 尝试直接导航到 email-verification
                await page.goto("https://auth.openai.com/email-verification", wait_until="domcontentloaded", timeout=15000)
                content = await page.content()
                if "Just a moment" in content:
                    await page.wait_for_timeout(10000)

            # Step 4: 获取 OTP
            logger.info(f"[PW-Revive] {email} Step 3: 等待 OTP...")
            if otp_callback:
                otp_code = await otp_callback(email)
            else:
                # 默认用 manymail 收 OTP
                otp_code = await _wait_otp_manymail(email, manymail_creds, timeout=90)

            if not otp_code:
                return {"ok": False, "email": email, "message": "OTP 未收到"}

            logger.info(f"[PW-Revive] {email} 收到 OTP: {otp_code}")

            # Step 5: POST validate（JS fetch）
            logger.info(f"[PW-Revive] {email} Step 4: POST validate...")

            # 确保在 email-verification 页面上
            current_url = page.url
            if "email-verification" not in current_url and "auth.openai.com" in current_url:
                # 已经在 auth.openai.com 上，直接 fetch
                pass

            result = await page.evaluate("""
                async (otpCode) => {
                    try {
                        const resp = await fetch('/api/accounts/email-otp/validate', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({code: otpCode}),
                            credentials: 'include'
                        });
                        const text = await resp.text();
                        return {status: resp.status, body: text};
                    } catch(e) {
                        return {error: e.toString()};
                    }
                }
            """, otp_code)

            logger.info(f"[PW-Revive] {email} POST validate result: {json.dumps(result, ensure_ascii=False)[:300]}")

            if result.get("status") != 200:
                return {"ok": False, "email": email, "message": f"validate 失败: HTTP {result.get('status')}"}

            body = result.get("body", "")
            try:
                data = json.loads(body)
            except:
                return {"ok": False, "email": email, "message": f"validate 响应解析失败: {body[:200]}"}

            if "error" in data:
                return {"ok": False, "email": email, "message": f"validate 错误: {data['error']}"}

            continue_url = data.get("continue_url")
            if not continue_url:
                return {"ok": False, "email": email, "message": f"validate 缺少 continue_url: {data}"}

            logger.info(f"[PW-Revive] {email} ✅ validate 成功, continue_url={continue_url[:80]}")

            # Step 6: 跟随 continue_url → 获取 session
            logger.info(f"[PW-Revive] {email} Step 5: 跟随 continue_url...")
            try:
                await page.goto(continue_url, wait_until="domcontentloaded", timeout=15000)
            except:
                pass  # 可能重定向到 chatgpt.com，忽略超时

            # 获取 session
            session_result = await page.evaluate("""
                async () => {
                    try {
                        const resp = await fetch('https://auth.openai.com/api/auth/session', {
                            credentials: 'include'
                        });
                        const text = await resp.text();
                        return {status: resp.status, body: text};
                    } catch(e) {
                        return {error: e.toString()};
                    }
                }
            """)

            if session_result.get("status") == 200:
                session_data = json.loads(session_result.get("body", "{}"))
                access_token = session_data.get("accessToken")
                if access_token:
                    logger.info(f"[PW-Revive] {email} ✅✅ 获取新 accessToken: {access_token[:40]}...")
                    return {
                        "ok": True,
                        "email": email,
                        "message": "token 复活成功",
                        "access_token": access_token,
                    }
                else:
                    return {"ok": False, "email": email, "message": f"session 无 accessToken: {json.dumps(session_data)[:200]}"}
            else:
                return {"ok": False, "email": email, "message": f"session 获取失败: HTTP {session_result.get('status')}"}

        finally:
            await browser.close()


async def _wait_otp_manymail(email: str, creds: dict | None, timeout: int = 90) -> str | None:
    """通过 manymail 收 OTP。"""
    if not creds:
        logger.error(f"[PW-Revive] {email} 无 manymail 凭据")
        return None

    from core.email_provider import wait_for_otp
    from core.session import BrowserSession

    # manymail 收 OTP 用 curl_cffi（不需要过 CF）
    try:
        otp = await asyncio.to_thread(
            wait_for_otp,
            email=email,
            email_source="manymail",
            manymail_domain=creds.get("domain"),
            manymail_password=creds.get("password"),
            manymail_token=creds.get("token"),
            timeout=timeout,
        )
        return otp
    except Exception as e:
        logger.error(f"[PW-Revive] {email} OTP 获取失败: {e}")
        return None


def playwright_revive_account(
    email: str,
    proxy: str,
    device_id: str,
    manymail_creds: dict | None = None,
    otp_callback=None,
    timeout: int = 120,
) -> dict[str, Any]:
    """同步包装：用 Playwright 复活单个账号 token。"""
    return asyncio.run(_playwright_revive_account(
        email=email,
        proxy=proxy,
        device_id=device_id,
        manymail_creds=manymail_creds,
        otp_callback=otp_callback,
        timeout=timeout,
    ))
