#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Token 复活 CLI：对已注册账号走邮箱重认证重新获取 access_token。

背景：注册后 access_token 常 ~30min 被服务端吊销（token_revoked），账号本身仍有效；
本工具复用 2FA 重认证链路（reauth → 邮箱 OTP → validate → exchange）拿回全新 token，
并写回 DB。要求邮箱仍可收件、会话 cookie 仍有效；全程使用账号自身 proxy + device_id。

用法:
  python3 tools/revive_accounts.py --email a@x.com
  python3 tools/revive_accounts.py --limit 5                    # 最新 5 个账号
  python3 tools/revive_accounts.py --limit 5 --live-check-first # 只复活 live 校验失效的
  python3 tools/revive_accounts.py --email a@x.com --otp-code 123456
  python3 tools/revive_accounts.py --limit 3 --json
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'), override=True)


def _pick_targets(emails: list[str], limit: int, live_check_first: bool) -> list[str]:
    """确定复活目标：显式邮箱 > 最新 N 个（可选 live 校验过滤）。"""
    if emails:
        return emails
    from core import db
    accounts = db.list_accounts(limit=100000)
    candidates = [
        a for a in accounts
        if str(a.get("access_token") or "").strip() and str(a.get("proxy_used") or "").strip()
    ][: max(0, int(limit))]
    if not live_check_first:
        return [str(a.get("email") or "") for a in candidates if str(a.get("email") or "").strip()]
    from tools.check_accounts_valid import check_token
    revoked = []
    for a in candidates:
        status, _, _, _ = check_token(
            a.get("access_token") or "",
            str(a.get("proxy_used") or ""),
            device_id=str(a.get("device_id") or ""),
        )
        if status == "revoked":
            revoked.append(a)
    return [str(a.get("email") or "") for a in revoked if str(a.get("email") or "").strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", action="append", default=[], help="目标邮箱（可多次；也可逗号分隔）")
    ap.add_argument("--limit", type=int, default=1, help="未指定 --email 时复活最新 N 个账号")
    ap.add_argument("--live-check-first", action="store_true",
                    help="先 live 校验，只复活确认失效（revoked）的账号")
    ap.add_argument("--otp-code", default="", help="重认证邮箱验证码（仅单个 --email 时可用）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    emails: list[str] = []
    for raw in args.email or []:
        emails.extend(e.strip() for e in str(raw).split(",") if e.strip())
    if args.otp_code and len(emails) != 1:
        print("--otp-code 仅支持单个 --email", file=sys.stderr)
        return 2

    targets = _pick_targets(emails, args.limit, args.live_check_first)
    if not targets:
        print("没有可复活的账号（未找到目标）")
        return 0

    from core.token_revival import revive_accounts
    otp_codes = {emails[0]: args.otp_code} if args.otp_code else None
    results = revive_accounts(targets, otp_codes=otp_codes)

    ok_count = sum(1 for r in results if r.get("ok"))
    if args.json:
        print(json.dumps({"total": len(results), "ok": ok_count, "results": results},
                         ensure_ascii=False, indent=2))
    else:
        for r in results:
            mark = "✅" if r.get("ok") else "❌"
            print(f"{mark} {r.get('email')}: {r.get('message')}")
        print(f"\n复活完成：成功 {ok_count} / 尝试 {len(results)}")

    return 0 if ok_count == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
