#!/usr/bin/env python3
"""Playwright token revival 测试脚本。

用真实 Chromium 过 CF challenge，走完整 NextAuth 流程复活 token。
"""
import asyncio
import json
import logging
import os
import sys
import time

# 设置路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

PROXY_URL = os.environ.get("PROXY_URL", "http://Pokemon.cli:9624f371e464ba2b8a73c4f42e841135f0a969d21aaec6d1@127.0.0.1:2260")

async def test_playwright_cf():
    """测试 playwright 能否过 auth.openai.com 的 CF challenge。"""
    from playwright.async_api import async_playwright
    
    # 解析代理
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
        
        # 隐藏 webdriver 标志
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            window.chrome = {runtime: {}};
        """)
        
        page = await context.new_page()
        
        # 测试 1: GET auth.openai.com/log-in
        logger.info("GET auth.openai.com/log-in ...")
        try:
            resp = await page.goto("https://auth.openai.com/log-in", wait_until="networkidle", timeout=30000)
            logger.info(f"Status: {resp.status}, URL: {page.url}")
            
            # 检查是否过了 CF
            content = await page.content()
            if "Just a moment" in content or "challenge" in content.lower():
                logger.warning("CF challenge 页面，等待 10s...")
                await page.wait_for_timeout(10000)
                content = await page.content()
                if "Just a moment" in content:
                    logger.error("❌ CF challenge 未通过")
                    # 截图
                    await page.screenshot(path="/tmp/cf_challenge.png")
                    await browser.close()
                    return False
                else:
                    logger.info("✅ CF challenge 已通过（等待后）")
            
            logger.info(f"✅ auth.openai.com/log-in 访问成功")
            
            # 获取 cookies
            cookies = await context.cookies()
            cookie_names = [c["name"] for c in cookies]
            logger.info(f"Cookies: {cookie_names}")
            
            # 测试 2: 执行 JS fetch
            logger.info("测试 JS fetch...")
            result = await page.evaluate("""
                async () => {
                    try {
                        const resp = await fetch('https://auth.openai.com/api/accounts/email-otp/validate', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({code: '000000'}),
                            credentials: 'include'
                        });
                        const text = await resp.text();
                        return {status: resp.status, body: text.substring(0, 300)};
                    } catch(e) {
                        return {error: e.toString()};
                    }
                }
            """)
            logger.info(f"JS fetch result: {json.dumps(result, ensure_ascii=False)[:300]}")
            
            if result.get("status") == 200:
                body = result.get("body", "")
                if "unsupported_country_region_territory" in body:
                    logger.warning("⚠️ 仍然返回 unsupported_country_region_territory")
                elif "invalid" in body.lower() or "expired" in body.lower():
                    logger.info("✅ POST validate 端点可达！返回 OTP 错误（预期，因为 code=000000）")
                    logger.info("这意味着 CF 已通过，可以走完整 revival 流程")
                else:
                    logger.info(f"返回: {body[:200]}")
            else:
                logger.warning(f"POST 返回 HTTP {result.get('status')}")
                
        except Exception as e:
            logger.error(f"测试失败: {type(e).__name__}: {e}")
            await browser.close()
            return False
        
        await browser.close()
        return True

if __name__ == "__main__":
    result = asyncio.run(test_playwright_cf())
    print(f"\n结果: {'✅ 成功' if result else '❌ 失败'}")
    sys.exit(0 if result else 1)
