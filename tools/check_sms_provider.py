#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接码平台非破坏性预检（不取号、不产生费用）。

grizzly: getBalance 查余额；l/h: 配置完整性 + 后端可达性。
完全未配置时视为跳过（退出码 0）；配置存在但余额为 0 / 不可达 / 缺参数时退出码 1。

用法:
  python3 tools/check_sms_provider.py [--json]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'), override=True)

from core.sms_provider import check_sms_availability


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    r = check_sms_availability()
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(f"接码平台: {r['provider']}")
        print(f"  状态: {'✅ 可用' if r['ok'] else '❌ 不可用'} — {r['message']}")
        if r.get("balance") is not None:
            print(f"  余额: {r['balance']}")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
