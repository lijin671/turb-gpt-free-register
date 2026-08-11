#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HeroSMS 接码 CLI。

用法:
  # 余额
  python tools/hero_sms_cli.py balance

  # 查国家/服务（模糊搜索）
  python tools/hero_sms_cli.py countries --search 菲律宾
  python tools/hero_sms_cli.py services --search gcash

  # 购买 GCash 菲律宾号并等待验证码（自动 complete）
  python tools/hero_sms_cli.py get-number --service gcash --country 6 --wait --timeout 240

  # 只买号不等待
  python tools/hero_sms_cli.py get-number --service gcash --country 6

  # 手动查码 / 完成 / 取消
  python tools/hero_sms_cli.py sms --id 12345678
  python tools/hero_sms_cli.py complete --id 12345678
  python tools/hero_sms_cli.py cancel --id 12345678

  # 指定代理（批量换 IP 绕 10 分钟 2 码限制）
  python tools/hero_sms_cli.py get-number --proxy http://user:pass@host:port
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("hero_sms_cli")


def _client(args) -> HeroSMSClient:
    from config import plus as plus_cfg
    api_key = args.api_key or plus_cfg.HERO_SMS_API_KEY
    proxy = args.proxy or plus_cfg.HERO_SMS_PROXY
    base = args.base_url or plus_cfg.HERO_SMS_BASE_URL
    return HeroSMSClient(api_key=api_key, base_url=base, proxy=proxy, timeout=args.timeout)


def cmd_balance(args) -> int:
    c = _client(args)
    print(json.dumps({"balance": c.get_balance()}, ensure_ascii=False))
    return 0


def cmd_countries(args) -> int:
    c = _client(args)
    data = c.get_countries()
    items = data.get("countries", data) if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = items.values()
    rows = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        row = {"id": it.get("id"), "eng": it.get("eng"), "chn": it.get("chn"), "rus": it.get("rus")}
        hay = " ".join(str(v) for v in row.values()).lower()
        if args.search and args.search.lower() not in hay:
            continue
        rows.append(row)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_services(args) -> int:
    c = _client(args)
    data = c.get_services(lang=args.lang)
    items = data.get("services", data) if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = items.values()
    rows = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        row = {"code": it.get("code"), "name": it.get("name", it.get("eng")), "cn": it.get("cn")}
        hay = " ".join(str(v) for v in row.values()).lower()
        if args.search and args.search.lower() not in hay:
            continue
        rows.append(row)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_get_number(args) -> int:
    from config import plus as plus_cfg
    c = _client(args)
    service = args.service or plus_cfg.HERO_SMS_SERVICE
    country = args.country or plus_cfg.HERO_SMS_COUNTRY
    max_price = args.max_price or plus_cfg.HERO_SMS_MAX_PRICE or None
    act = c.get_number(service=service, country=country,
                       operator=args.operator or None,
                       max_price=max_price,
                       fixed_price=args.fixed_price)
    print(json.dumps({"id": act.id, "phone": act.phone, "cost": act.cost, "service": service, "country": country},
                     ensure_ascii=False, indent=2))
    if args.wait:
        timeout = args.timeout or plus_cfg.HERO_SMS_WAIT_TIMEOUT
        interval = args.poll_interval or plus_cfg.HERO_SMS_POLL_INTERVAL
        logger.info("等待验证码 id=%s timeout=%ss ...", act.id, timeout)
        code = c.wait_for_code(act.id, timeout=timeout, poll_interval=interval,
                               prefer_all_sms=plus_cfg.HERO_SMS_PREFER_ALL_SMS)
        print(json.dumps({"id": act.id, "phone": act.phone, "code": code}, ensure_ascii=False))
        if args.auto_complete:
            c.complete(act.id)
            print(json.dumps({"id": act.id, "completed": True}, ensure_ascii=False))
    return 0


def cmd_sms(args) -> int:
    c = _client(args)
    items = c.get_all_sms(args.id)
    print(json.dumps([vars(i) for i in items], ensure_ascii=False, indent=2))
    return 0


def cmd_status(args) -> int:
    c = _client(args)
    print(json.dumps({"id": args.id, "status": c.get_status(args.id)}, ensure_ascii=False))
    return 0


def cmd_complete(args) -> int:
    c = _client(args)
    print(json.dumps({"id": args.id, "completed": c.complete(args.id)}, ensure_ascii=False))
    return 0


def cmd_cancel(args) -> int:
    c = _client(args)
    print(json.dumps({"id": args.id, "cancelled": c.cancel(args.id)}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="HeroSMS 接码 CLI")
    parser.add_argument("--api-key", default="", help="HeroSMS API Key（默认读 config.plus.HERO_SMS_API_KEY）")
    parser.add_argument("--base-url", default="", help="API Base URL")
    parser.add_argument("--proxy", default="", help="请求出口代理（批量时轮换 IP）")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP 超时（秒）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("balance", help="查询余额")
    p.set_defaults(fn=cmd_balance)

    p = sub.add_parser("countries", help="国家列表")
    p.add_argument("--search", default="", help="模糊搜索（英文/中文）")
    p.set_defaults(fn=cmd_countries)

    p = sub.add_parser("services", help="服务列表")
    p.add_argument("--search", default="", help="模糊搜索")
    p.add_argument("--lang", default="cn", help="语言（cn/en/ru）")
    p.set_defaults(fn=cmd_services)

    p = sub.add_parser("get-number", help="购买号码（可等待验证码）")
    p.add_argument("--service", default="", help="服务码（默认 gcash）")
    p.add_argument("--country", type=int, default=0, help="国家 id（菲律宾=6）")
    p.add_argument("--operator", default="", help="运营商过滤")
    p.add_argument("--max-price", type=float, default=0.0, help="单号最高价（美元）")
    p.add_argument("--fixed-price", action="store_true", help="只接受固定价")
    p.add_argument("--wait", action="store_true", help="购买后等待验证码")
    p.add_argument("--poll-interval", type=int, default=0, help="轮询间隔（秒）")
    p.add_argument("--auto-complete", action="store_true", help="取到码后自动 complete")
    p.set_defaults(fn=cmd_get_number)

    p = sub.add_parser("sms", help="查看激活号短信")
    p.add_argument("--id", type=int, required=True)
    p.set_defaults(fn=cmd_sms)

    p = sub.add_parser("status", help="查询激活状态")
    p.add_argument("--id", type=int, required=True)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("complete", help="完成激活")
    p.add_argument("--id", type=int, required=True)
    p.set_defaults(fn=cmd_complete)

    p = sub.add_parser("cancel", help="取消激活")
    p.add_argument("--id", type=int, required=True)
    p.set_defaults(fn=cmd_cancel)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
