#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查看 1ip1号 IP 纪律状态：当前占用、冷却中 IP、连坐风险 IP。

用法:
  python3 tools/check_ip_discipline.py [--json]
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'), override=True)

from core.ip_discipline import _load, status_summary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    s = status_summary()
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return

    print(f"IP 纪律状态（{s['state_file']}）")
    print(f"  冷却窗口: 失败 {s['cooldown_seconds']}s / 成功 {s.get('success_cooldown_seconds', '-')}s | 每 IP 上限: {s['max_accounts_per_ip']} 号")
    print(f"  当前占用: {s['active_leases']} | 冷却中 IP: {s['ips_in_cooldown']} | 连坐风险 IP: {s['co_risk_ips']}")

    state = _load()
    for key, info in (state.get("co_risk") or {}).items():
        print(f"  ⚠️ 连坐风险 {key}: {info.get('reason')} @ {info.get('marked_at')}")
    for key, uses in (state.get("usage") or {}).items():
        if uses:
            outcomes = ",".join(u.get("outcome") or "failure" for u in uses)
            print(f"  ⏳ 冷却中 {key}: 最近 {len(uses)} 次使用（{outcomes}），最后 {uses[-1].get('used_at')}")
    for key, lease in (state.get("leases") or {}).items():
        if lease:
            print(f"  🔒 占用中 {key}: owner={lease.get('owner')} 到期={lease.get('expires_at')}")


if __name__ == "__main__":
    main()
