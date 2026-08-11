#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloudflare Turnstile token 真浏览器 mint（参考 sleep-reg scripts/turnstile_mint.py）。

用法:
  python3 tools/turnstile_mint.py --page-url https://example.com [--site-key xxx]
                                  [--headed] [--timeout 60] [--proxy http://127.0.0.1:7890]
                                  [--json]
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.turnstile_browser_mint import mint_turnstile_token  # noqa: E402


def main() -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--site-key", default="", help="Turnstile site key（缺省自动从页面发现）")
    ap.add_argument("--page-url", required=True, help="目标页面 URL")
    ap.add_argument("--headed", action="store_true", help="有头模式（可人工过验证码）")
    ap.add_argument("--timeout", type=int, default=60, help="等待 token 秒数")
    ap.add_argument("--proxy", default="", help="代理，如 http://127.0.0.1:7890")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

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
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())
