#!/usr/bin/env python3
"""Playwright token revival 测试 2 - 完整 NextAuth 流程。"""
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

PROXY_URL = os.environ.get("PROXY_URL", "http://Pokemon.cli:9624f371e464ba2b8a73c4f42e841135f0a969d21aaec6d1@127.0.0.1:2260")

async def test_full_flow():
    from playwright.async_api import async_playwright
    from urllib.parse import urlparse
    
    proxy_parsed = urlparse(PROXY_URL)
    proxy_config = {
        "server": f"{proxy_parsed.scheme}://{proxy_parsed.hostname}:{proxy_parsed.port}",
        "username": proxy_parsed.username or "",
        "password": proxy_parsed.password or "",
    }
    
    logger.info(f"启动 Chromium（代理: {proxy_config['server']}）...")
    
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
        logger.info("Step 1: GET auth.openai.com/log-in ...")
        resp = await page.goto("https://auth.openai.com/log-in", wait_until="networkidle", timeout=30000)
        logger.info(f"  Status: {resp.status}")
        content = await page.content()
        if "Just a moment" in content:
            logger.info("  CF challenge, 等待 15s...")
            await page.wait_for_timeout(15000)
            content = await page.content()
            if "Just a moment" in content:
                logger.error("  ❌ CF challenge 未通过")
                await browser.close()
                return False
        logger.info("  ✅ CF passed")
        
        # Step 2: 直接导航到 authorize URL（用 build_direct_authorize_url 构造）
        logger.info("Step 2: 构造 authorize URL...")
        
        # 构造 authorize URL
        import secrets as _secrets
        from urllib.parse import urlencode, quote
        
        code_verifier = "".join(_secrets.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_") for _ in range(64))
        import hashlib, base64
        code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip("=")
        
        device_id = "550e8400-e29b-41d4-a716-446655440000"  # 测试用
        email = "test@example.com"  # 测试用，不会真的触发 OTP
        
        params = {
            "issuer": "https://auth.openai.com",
            "client_id": "app_X8zY6vW2pQ9tR3dE7nK1jL5gH",
            "scope": "openid email profile offline_access model.request model.read organization.read organization.write",
            "response_type": "code",
            "redirect_uri": "https://chatgpt.com/api/auth/callback/login-openai",
            "audience": "https://api.openai.com/v1",
            "device_id": device_id,
            "prompt": "login",
            "ext-oai-did": device_id,
            "screen_hint": "login_or_signup",
            "login_hint": email,
            "ccaps": "login_methods",
            "max_age": "0",
            "response_mode": "query",
            "state": "".join(_secrets.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_") for _ in range(32)),
            "nonce": "".join(_secrets.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_") for _ in range(32)),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        
        authorize_url = "https://auth.openai.com/api/accounts/authorize?" + urlencode(params)
        logger.info(f"  authorize URL: {authorize_url[:100]}...")
        
        # Step 3: GET authorize URL → 应该重定向到 email-verification
        logger.info("Step 3: GET authorize URL ...")
        resp = await page.goto(authorize_url, wait_until="networkidle", timeout=30000)
        logger.info(f"  Status: {resp.status}, URL: {page.url}")
        content = await page.content()
        if "Just a moment" in content:
            logger.info("  CF challenge, 等待 15s...")
            await page.wait_for_timeout(15000)
        
        # 看看落在哪个页面
        final_url = page.url
        logger.info(f"  Final URL: {final_url}")
        
        if "email-verification" in final_url:
            logger.info("  ✅ 落在 email-verification 页面！authorize 流程正确")
        elif "log-in" in final_url:
            logger.info("  落在 log-in 页面（可能需要先登录）")
        else:
            logger.info(f"  落在: {final_url}")
        
        # Step 4: 测试 POST validate（用 page.evaluate JS fetch）
        # 先在当前页面（email-verification）上执行 JS fetch
        logger.info("Step 4: 测试 POST validate (JS fetch)...")
        
        # 先导航到 email-verification 页面（如果不在）
        if "email-verification" not in page.url:
            await page.goto("https://auth.openai.com/email-verification", wait_until="networkidle", timeout=15000)
            content = await page.content()
            if "Just a moment" in content:
                await page.wait_for_timeout(10000)
        
        result = await page.evaluate("""
            async () => {
                try {
                    const resp = await fetch('/api/accounts/email-otp/validate', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify({code: '000000'}),
                        credentials: 'include'
                    });
                    const text = await resp.text();
                    return {status: resp.status, body: text.substring(0, 500)};
                } catch(e) {
                    return {error: e.toString()};
                }
            }
        """)
        logger.info(f"  POST result: {json.dumps(result, ensure_ascii=False)[:400]}")
        
        if result.get("status") == 200:
            body = result.get("body", "")
            if "unsupported_country" in body:
                logger.warning("  ⚠️ unsupported_country_region_territory")
            elif "invalid" in body.lower() or "expired" in body.lower():
                logger.info("  ✅✅ POST validate 成功！返回 OTP 错误（预期）")
                logger.info("  ✅ CF 已通过，可以走完整 revival 流程！")
            else:
                logger.info(f"  返回: {body[:200]}")
        elif result.get("status") == 403:
            logger.warning("  ❌ POST validate 被 CF 403")
            # 尝试用 page.request.post（APIRequestContext）
            logger.info("  尝试用 APIRequestContext...")
            api_resp = await context.request.post(
                "https://auth.openai.com/api/accounts/email-otp/validate",
                data={"code": "000000"},
                headers={"Content-Type": "application/json"},
            )
            logger.info(f"  APIRequest status: {api_resp.status}")
            body = await api_resp.text()
            logger.info(f"  APIRequest body: {body[:300]}")
        else:
            logger.warning(f"  POST 返回 HTTP {result.get('status')}")
        
        await browser.close()
        return True

if __name__ == "__main__":
    result = asyncio.run(test_full_flow())
    print(f"\n结果: {'✅ 成功' if result else '❌ 失败'}")
    sys.exit(0 if result else 1)
