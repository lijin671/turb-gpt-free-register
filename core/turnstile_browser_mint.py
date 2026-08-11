# -*- coding: utf-8 -*-
"""
Cloudflare Turnstile token 真浏览器 mint 工具（参考 sleep-reg scripts/turnstile_mint.py）。

用途：
  - 纯 Python 本地求解（core.turnstile_solver）失败时的人工/半自动兜底；
  - 调试/辅助流程：对带 Turnstile 控件的页面，用真实 Chromium 拿到
    `cf-turnstile-response` token（自动化点击或人工过验证码后自动提取）。

不依赖任何站点私密信息：只需 site_key（可从页面 DOM 自动发现）与页面 URL。
Playwright 未安装时安全降级返回 None，不抛异常。
"""
from __future__ import annotations

import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)

# Turnstile 控件常见 selector（按顺序尝试）
_WIDGET_SELECTORS = (
    '[data-sitekey]',
    '.cf-turnstile',
    'iframe[src*="challenges.cloudflare.com"]',
)

_TOKEN_EXPR = (
    '(function(){'
    'var el=document.querySelector("textarea[name=\\"cf-turnstile-response\\"]");'
    'var v=el&&el.value?el.value:"";'
    'if(!v&&window.turnstile&&window.turnstile.getResponse){'
    'try{v=window.turnstile.getResponse()||""}catch(e){v=""}'
    '}'
    'return v||"";'
    '})()'
)

_SITEKEY_EXPR = (
    '(function(){'
    'var el=document.querySelector("[data-sitekey]");'
    'return el?el.getAttribute("data-sitekey")||"":"";'
    '})()'
)

_LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--disable-web-security",
    "--disable-features=IsolateOrigins,site-process",
)


def _playwright_sync():
    """懒加载 Playwright；未安装时抛出 ImportError。"""
    from playwright.sync_api import sync_playwright  # type: ignore
    return sync_playwright


def _discover_site_key(page) -> str:
    try:
        value = str(page.evaluate(_SITEKEY_EXPR) or "")
        if value:
            return value
    except Exception:
        pass
    return ""


def _read_token(page) -> str:
    try:
        return str(page.evaluate(_TOKEN_EXPR) or "").strip()
    except Exception:
        return ""


def mint_turnstile_token(
    site_key: str | None = None,
    page_url: str | None = None,
    *,
    headless: bool = True,
    timeout: int = 60,
    proxy: str = "",
    on_log: Callable[[str], None] | None = None,
) -> str | None:
    """在真实 Chromium 中打开 page_url，等待 Turnstile 控件并提取 token。

    Args:
        site_key: 可省略，页面存在 [data-sitekey] 时自动发现
        page_url: 目标页面 URL（必填）
        headless: 是否无头运行；False 时可人工过验证码
        timeout: 等待 token 的最大秒数
        proxy: 可选代理（http://host:port）
        on_log: 日志回调

    Returns:
        Turnstile token 字符串；失败/未安装 Playwright 返回 None
    """
    log = on_log or (lambda msg: logger.info("[TurnstileMint] %s", msg))
    if not page_url:
        log("缺少 page_url，无法 mint")
        return None
    try:
        sync_playwright = _playwright_sync()
    except Exception as exc:
        log(f"Playwright 未安装或不可用，跳过浏览器 mint: {exc}")
        return None

    token = ""
    try:
        with sync_playwright() as p:
            launch_kwargs = {
                "headless": bool(headless),
                "args": list(_LAUNCH_ARGS),
            }
            if proxy:
                launch_kwargs["proxy"] = {"server": str(proxy)}
            browser = p.chromium.launch(**launch_kwargs)
            try:
                page = browser.new_page()
                log(f"打开页面: {page_url}")
                page.goto(page_url, wait_until="domcontentloaded", timeout=min(60_000, max(5_000, timeout * 1000)))
            except Exception as exc:
                log(f"打开页面失败: {exc}")
                return None

            # 等待 Turnstile 控件出现
            try:
                page.wait_for_selector(_WIDGET_SELECTORS[0], timeout=10_000)
            except Exception:
                try:
                    page.wait_for_selector(_WIDGET_SELECTORS[1], timeout=5_000)
                except Exception:
                    try:
                        page.wait_for_selector(_WIDGET_SELECTORS[2], timeout=5_000)
                    except Exception:
                        log("未检测到 Turnstile 控件")
                        return None

            # 自动发现 site_key
            effective_key = site_key or _discover_site_key(page)
            if not effective_key:
                log("未找到 site_key（页面缺少 [data-sitekey]），请显式传入 --site-key")
                return None
            log(f"site_key: {effective_key}")

            # 轮询 token；headless=False 时留足时间给人过验证码
            deadline = time.time() + max(5, int(timeout))
            while time.time() < deadline:
                token = _read_token(page)
                if token:
                    log(f"获取到 Turnstile token（{len(token)} 字符）")
                    return token
                time.sleep(1)
            log("等待 Turnstile token 超时")
            return None
    except Exception as exc:
        log(f"浏览器 mint 异常: {exc}")
        return None
    finally:
        if token:
            log("浏览器已关闭")


if __name__ == "__main__":
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="Cloudflare Turnstile token 浏览器 mint")
    ap.add_argument("--site-key", default="", help="Turnstile site key（缺省自动从页面发现）")
    ap.add_argument("--page-url", required=True, help="目标页面 URL")
    ap.add_argument("--headed", action="store_true", help="有头模式（可人工过验证码）")
    ap.add_argument("--timeout", type=int, default=60, help="等待 token 秒数")
    ap.add_argument("--proxy", default="", help="代理，如 http://127.0.0.1:7890")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    result = mint_turnstile_token(
        site_key=args.site_key or None,
        page_url=args.page_url,
        headless=not args.headed,
        timeout=args.timeout,
        proxy=args.proxy,
    )
    if args.json:
        print(json.dumps({"ok": bool(result), "token": result or ""}, ensure_ascii=False))
    else:
        print("✅ token:" if result else "❌ 未获取到 token")
        if result:
            print(result)
    sys.exit(0 if result else 1)
