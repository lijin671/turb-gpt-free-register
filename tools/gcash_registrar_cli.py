#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GCash 注册机 CLI（HeroSMS 接码 + ADB 驱动，半自动）。

用法:
  # 只验证接码链路（无设备）：买号 → 打印手动步骤 → 等码
  python tools/gcash_registrar_cli.py --dry-run

  # 带真机/模拟器（ADB）
  python tools/gcash_registrar_cli.py --serial EMULATOR_SERIAL \
      --profile profile.json

  # 指定接码参数
  python tools/gcash_registrar_cli.py --service gcash --country 6 \
      --max-price 0.5 --timeout 240
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.hero_sms import HeroSMSClient
from core.gcash_registrar import GcashRegistrar, load_profile

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("gcash_registrar")


def main() -> int:
    from config import plus as plus_cfg

    parser = argparse.ArgumentParser(description="GCash 注册机 CLI")
    parser.add_argument("--api-key", default=plus_cfg.HERO_SMS_API_KEY, help="HeroSMS API Key")
    parser.add_argument("--proxy", default=plus_cfg.HERO_SMS_PROXY, help="HeroSMS 出口代理（批量轮换 IP）")
    parser.add_argument("--service", default=plus_cfg.HERO_SMS_SERVICE, help="服务码（默认 gcash）")
    parser.add_argument("--country", type=int, default=plus_cfg.HERO_SMS_COUNTRY, help="国家 id（菲律宾=6）")
    parser.add_argument("--max-price", type=float, default=plus_cfg.HERO_SMS_MAX_PRICE or None,
                        help="单号最高价（美元）")
    parser.add_argument("--serial", default=plus_cfg.GCASH_ADB_SERIAL, help="ADB 设备串号")
    parser.add_argument("--package", default=plus_cfg.GCASH_APP_PACKAGE, help="GCash App 包名")
    parser.add_argument("--profile", default="", help="注册信息 JSON（姓名/生日/邮箱）")
    parser.add_argument("--dry-run", action="store_true", help="不操作 ADB，只验证接码链路")
    parser.add_argument("--timeout", type=int, default=plus_cfg.HERO_SMS_WAIT_TIMEOUT, help="等码超时（秒）")
    args = parser.parse_args()

    if not args.api_key:
        print("❌ 未配置 HERO_SMS_API_KEY（.env 或 --api-key）")
        return 2

    profile = load_profile(args.profile) if args.profile else plus_cfg.GCASH_REGISTER_PROFILE
    hero = HeroSMSClient(api_key=args.api_key, proxy=args.proxy)
    reg = GcashRegistrar(
        hero=hero,
        profile=profile,
        serial="" if args.dry_run else args.serial,
        package=args.package,
        wait_timeout=args.timeout,
        poll_interval=plus_cfg.HERO_SMS_POLL_INTERVAL,
    )

    logger.info("balance=%s", hero.get_balance())
    acc = reg.run(service=args.service, country=args.country, max_price=args.max_price)
    print("\n" + "=" * 60)
    print(json.dumps(acc.to_dict(), ensure_ascii=False, indent=2))
    print("=" * 60)
    print("✅ GCash 账号注册完成。下一步：")
    print("   python tools/zero_plus.py --token <CHATGPT_TOKEN> --account-id <AID> --gcash --gcash-wait-paid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
