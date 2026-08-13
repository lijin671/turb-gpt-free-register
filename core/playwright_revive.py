# -*- coding: utf-8 -*-
"""Playwright token 复活模块 v2。

用真实 Chromium + resin 代理过 CF challenge，走完整 NextAuth 流程复活 token。

流程：
1. GET chatgpt.com/api/auth/csrf → CSRF token
2. POST chatgpt.com/api/auth/signin/login-openai → authorize URL
3. GET authorize URL → email-verification → OTP 发送
4. manymail 收 OTP
5. APIRequestContext POST validate → continue_url
6. page.goto continue_url → chatgpt.com 回调 → session cookie
7. GET chatgpt.com/api/auth/session → accessToken
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
    from playwright.async_api import async_playwright
    from urllib.parse import urlparse, urlencode, parse_qs
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
            headless=True, proxy=proxy_config,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080}, locale="en-US",
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); window.chrome = {runtime: {}};"
            )
            page = await context.new_page()

            # Step 1: GET chatgpt.com/api/auth/csrf → CSRF token（带重试）
            csrf_token = ""
            for _step1_attempt in range(3):
                try:
                    if _step1_attempt > 0:
                        await browser.close()
                        _new_sid = _secrets.token_hex(8)
                        _new_proxy = f"http://Pokemon.cli-session-pw{_new_sid}:9624f371e464ba2b8a73c4f42e841135f0a969d21aaec6d1@127.0.0.1:2260"
                        _np = urlparse(_new_proxy)
                        proxy_config = {"server": f"{_np.scheme}://{_np.hostname}:{_np.port}", "username": _np.username or "", "password": _np.password or ""}
                        logger.info(f"[PW-Revive] {email} Step 1 重试 {_step1_attempt+1}/3 (换代理)...")
                        browser = await p.chromium.launch(headless=True, proxy=proxy_config, args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"])
                        context = await browser.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36", viewport={"width": 1920, "height": 1080}, locale="en-US")
                        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); window.chrome = {runtime: {}};")
                        page = await context.new_page()
                    
                    logger.info(f"[PW-Revive] {email} Step 1: GET chatgpt.com/api/auth/csrf ...")
                    resp = await page.goto("https://chatgpt.com/api/auth/csrf", wait_until="domcontentloaded", timeout=30000)
                    content = await page.content()
                    if "Just a moment" in content:
                        logger.info(f"[PW-Revive] {email} CF challenge, 等待 15s...")
                        await page.wait_for_timeout(15000)
                        content = await page.content()
                        if "Just a moment" in content:
                            if _step1_attempt < 2:
                                logger.warning(f"[PW-Revive] {email} CF challenge 未通过, 换代理重试...")
                                continue
                            return {"ok": False, "email": email, "message": "chatgpt.com CF challenge 未通过"}
                    
                    # 解析 CSRF token
                    try:
                        csrf_data = json.loads(content.split("<pre>")[1].split("</pre>")[0])
                        csrf_token = csrf_data.get("csrfToken", "")
                    except:
                        pass
                    if csrf_token:
                        break
                except Exception as step1_exc:
                    logger.warning(f"[PW-Revive] {email} Step 1 失败 ({_step1_attempt+1}/3): {type(step1_exc).__name__}")
                    if _step1_attempt >= 2:
                        return {"ok": False, "email": email, "message": f"Step 1 连接失败: {type(step1_exc).__name__}"}

            if not csrf_token:
                return {"ok": False, "email": email, "message": "未获取到 CSRF token"}


            logger.info(f"[PW-Revive] {email} ✅ CSRF token: {csrf_token[:30]}...")

            # Step 2: POST chatgpt.com/api/auth/signin/openai → authorize URL
            # 用 page.evaluate 发 POST（Chrome 自动携带 cookies）
            signin_url = (
                "/api/auth/signin/openai?"
                + urlencode({
                    "prompt": "login",
                    "ext-oai-did": device_id,
                    "ext-passkey-client-capabilities": "11111",
                    "screen_hint": "login_or_signup",
                    "login_hint": email,
                })
            )
            logger.info(f"[PW-Revive] {email} Step 2: POST signin/openai (page.evaluate)...")
            signin_result = await page.evaluate("""
                async ({url, csrf, callbackUrl}) => {
                    try {
                        const body = new URLSearchParams();
                        body.set('callbackUrl', callbackUrl);
                        body.set('csrfToken', csrf);
                        body.set('json', 'true');
                        const resp = await fetch(url, {
                            method: 'POST',
                            headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                            body: body,
                            credentials: 'include',
                        });
                        const text = await resp.text();
                        return {status: resp.status, body: text};
                    } catch(e) {
                        return {error: e.toString()};
                    }
                }
            """, {"url": signin_url, "csrf": csrf_token, "callbackUrl": "https://chatgpt.com/"})
            logger.info(f"[PW-Revive] {email} signin: {json.dumps(signin_result, ensure_ascii=False)[:300]}")

            authorize_url = None
            if signin_result.get("status") == 200:
                try:
                    signin_data = json.loads(signin_result.get("body", ""))
                    authorize_url = signin_data.get("url", "")
                except:
                    pass

            if not authorize_url:
                return {"ok": False, "email": email, "message": f"signin 未返回 authorize URL: HTTP {signin_result.get('status')}"}
            logger.info(f"[PW-Revive] {email} ✅ authorize URL: {authorize_url[:100]}...")
            logger.info(f"[PW-Revive] {email} authorize URL: {authorize_url[:100]}...")

            # Step 3: GET authorize URL → email-verification → OTP 发送
            logger.info(f"[PW-Revive] {email} Step 3: GET authorize URL ...")
            try:
                resp = await page.goto(authorize_url, wait_until="domcontentloaded", timeout=30000)
            except:
                pass  # 可能重定向

            content = await page.content()
            if "Just a moment" in content:
                logger.info(f"[PW-Revive] {email} authorize CF challenge, 等待 15s...")
                await page.wait_for_timeout(15000)

            final_url = page.url
            logger.info(f"[PW-Revive] {email} 落点: {final_url}")

            if "email-verification" in page.url or "email" in page.url.lower():
                logger.info(f"[PW-Revive] {email} ✅ 落在 email-verification，OTP 已发送")
            else:
                logger.warning(f"[PW-Revive] {email} 未落在 email-verification: {page.url}")
                await page.goto("https://auth.openai.com/email-verification", wait_until="domcontentloaded", timeout=15000)
                content = await page.content()
                if "Just a moment" in content:
                    await page.wait_for_timeout(15000)

            # Step 4: 获取 OTP
            logger.info(f"[PW-Revive] {email} Step 4: 等待 OTP...")
            if otp_callback:
                otp_code = await otp_callback(email)
            else:
                otp_code = await _wait_otp_manymail(email, manymail_creds, timeout=90)

            if not otp_code:
                return {"ok": False, "email": email, "message": "OTP 未收到"}
            logger.info(f"[PW-Revive] {email} 收到 OTP: {otp_code}")

            # Step 5: POST validate（APIRequestContext）
            logger.info(f"[PW-Revive] {email} Step 5: POST validate...")
            current_url = page.url
            if "email-verification" not in current_url:
                try:
                    await page.goto("https://auth.openai.com/email-verification", wait_until="domcontentloaded", timeout=15000)
                    c = await page.content()
                    if "Just a moment" in c:
                        await page.wait_for_timeout(15000)
                except:
                    pass

            cookies = await context.cookies()
            logger.info(f"[PW-Revive] {email} POST 前 cookies: {[c['name'] for c in cookies]}")

            api_resp = await context.request.post(
                "https://auth.openai.com/api/accounts/email-otp/validate",
                data=json.dumps({"code": otp_code}),
                headers={
                    "Content-Type": "application/json",
                    "Referer": "https://auth.openai.com/email-verification",
                    "Origin": "https://auth.openai.com",
                },
                timeout=15000,
            )
            api_status = api_resp.status
            api_body = await api_resp.text()
            logger.info(f"[PW-Revive] {email} POST validate: status={api_status}, body={api_body[:300]}")

            if api_status != 200:
                return {"ok": False, "email": email, "message": f"validate 失败: HTTP {api_status}"}

            try:
                data = json.loads(api_body)
            except:
                return {"ok": False, "email": email, "message": f"validate 响应解析失败"}

            if "error" in data:
                return {"ok": False, "email": email, "message": f"validate 错误: {data['error']}"}

            continue_url = data.get("continue_url")
            if not continue_url:
                return {"ok": False, "email": email, "message": "validate 缺少 continue_url"}

            logger.info(f"[PW-Revive] {email} ✅ validate 成功, continue_url={continue_url[:80]}...")

            # Step 6: page.goto continue_url → chatgpt.com 回调 → session cookie
            logger.info(f"[PW-Revive] {email} Step 6: page.goto continue_url ...")
            try:
                await page.goto(continue_url, wait_until="domcontentloaded", timeout=20000)
                c = await page.content()
                if "Just a moment" in c:
                    logger.info(f"[PW-Revive] {email} chatgpt.com CF challenge, 等待 15s...")
                    await page.wait_for_timeout(15000)
                logger.info(f"[PW-Revive] {email} continue_url 跳转后: {page.url}")
            except Exception as goto_exc:
                logger.warning(f"[PW-Revive] {email} page.goto continue_url: {goto_exc}")

            await page.wait_for_timeout(2000)

            # Step 7: GET chatgpt.com/api/auth/session → accessToken
            logger.info(f"[PW-Revive] {email} Step 7: GET session ...")
            session_result = await page.evaluate("""
                async () => {
                    try {
                        const resp = await fetch('/api/auth/session', {credentials: 'include'});
                        const text = await resp.text();
                        return {status: resp.status, body: text};
                    } catch(e) {
                        return {error: e.toString()};
                    }
                }
            """)
            logger.info(f"[PW-Revive] {email} session: {json.dumps(session_result, ensure_ascii=False)[:300]}")

            if session_result.get("status") == 200:
                try:
                    session_data = json.loads(session_result.get("body", "{}"))
                    access_token = (
                        session_data.get("accessToken")
                        or session_data.get("access_token")
                        or ""
                    )
                    if access_token:
                        logger.info(f"[PW-Revive] {email} ✅✅✅ 获取 accessToken: {access_token[:40]}...")
                        return {
                            "ok": True, "email": email,
                            "message": "Playwright NextAuth 复活成功",
                            "access_token": access_token,
                        }
                    else:
                        keys = list(session_data.keys())
                        return {"ok": False, "email": email, "message": f"session 无 accessToken, keys={keys}"}
                except:
                    return {"ok": False, "email": email, "message": "session JSON 解析失败"}
            else:
                return {"ok": False, "email": email, "message": f"session HTTP {session_result.get('status')}"}

        finally:
            await browser.close()


async def _wait_otp_manymail(email: str, creds: dict | None, timeout: int = 90) -> str | None:
    if not creds:
        logger.error(f"[PW-Revive] {email} 无 manymail 凭据")
        return None
    try:
        from core.manymail_client import restore_context, get_account_context
        ctx = get_account_context(email)
        if ctx is None:
            password = creds.get("password", "")
            domain = creds.get("domain", "")
            if password:
                restore_context(email, password=password, domain=domain)
                logger.info(f"[PW-Revive] {email} 已恢复 manymail 上下文")
            else:
                logger.error(f"[PW-Revive] {email} manymail 密码缺失")
                return None
        from core.email_provider import wait_for_otp
        otp = await asyncio.to_thread(wait_for_otp, email=email, after_ts=time.time() - 5, max_wait=timeout)
        return otp
    except Exception as e:
        logger.error(f"[PW-Revive] {email} OTP 获取失败: {type(e).__name__}: {e}")
        return None


def playwright_revive_account(
    email: str, proxy: str, device_id: str,
    manymail_creds: dict | None = None,
    otp_callback=None, timeout: int = 120,
) -> dict[str, Any]:
    return asyncio.run(_playwright_revive_account(
        email=email, proxy=proxy, device_id=device_id,
        manymail_creds=manymail_creds, otp_callback=otp_callback, timeout=timeout,
    ))
