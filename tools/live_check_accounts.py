#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量查活：重新邮箱 OTP 登录已注册账号，刷新最新 accessToken（AT）。

上游 myfanhua/turb-gpt-free-register 的「查活」功能移植版（CLI）。
原理：Providers → CSRF → Signin → Authorize → 邮箱 OTP → OAuth callback → Session/AT，
成功即把最新 AT 写回本地账号库（core/db.update_account_liveness），
并同步 user_id/plan_type/expires 等字段。网络预检失败自动换新 IP 重试。

用法:
  python3 tools/live_check_accounts.py --limit 10                 # 查活最近 10 个账号
  python3 tools/live_check_accounts.py --emails a@x.com,b@y.com   # 指定邮箱
  python3 tools/live_check_accounts.py --all --threads 3          # 全部账号并发 3
  python3 tools/live_check_accounts.py --only-invalid              # 只查活 AT 已失效/无 AT 的账号
  python3 tools/live_check_accounts.py --proxy "http://user:pass@127.0.0.1:2260"
"""
import argparse
import concurrent.futures
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'), override=True)

from core import db
from core import account_liveness


def _load_accounts(args) -> list[dict]:
    if args.emails:
        wanted = {e.strip().lower() for e in args.emails.split(",") if e.strip()}
        rows = [r for r in db.list_accounts(limit=100000) if (r.get("email") or "").lower() in wanted]
        return rows
    rows = db.list_accounts(limit=100000)
    if not args.all:
        # 默认只取最近 N 个（按 id 倒序）
        rows = sorted(rows, key=lambda r: int(r.get("id") or 0), reverse=True)
        rows = rows[: args.limit]
    else:
        rows = sorted(rows, key=lambda r: int(r.get("id") or 0), reverse=True)
        if args.limit:
            rows = rows[: args.limit]
    if args.only_invalid:
        out = []
        for r in rows:
            at = str(r.get("access_token") or "").strip()
            if not at:
                out.append(r)
                continue
            # 轻量预判：JWT exp 或本地 codex 状态
            status = str(r.get("codex_status") or "")
            if status == "deactivated":
                out.append(r)
                continue
            out.append(r)
        rows = out
    return rows


def _one(acc: dict, proxy: str | None) -> dict:
    email = str(acc.get("email") or "").strip()
    acc_id = int(acc.get("id") or 0)
    started = time.time()
    try:
        result = account_liveness.check_account_liveness(email, proxy=proxy)
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    try:
        db.update_account_liveness(acc_id, result)
    except Exception as exc:
        result["_db_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    result["id"] = acc_id
    result["email"] = email
    result["elapsed_s"] = round(time.time() - started, 1)
    return result


def main():
    ap = argparse.ArgumentParser(description="批量查活 ChatGPT 免费账号并刷新 AT")
    ap.add_argument("--emails", default="", help="逗号分隔的邮箱列表")
    ap.add_argument("--limit", type=int, default=10, help="默认取最近 N 个账号（id 倒序）")
    ap.add_argument("--all", action="store_true", help="处理全部账号")
    ap.add_argument("--only-invalid", action="store_true", help="只查活 AT 为空/疑似失效的账号")
    ap.add_argument("--proxy", default="", help="固定代理；留空由 BrowserSession 自动选路")
    ap.add_argument("--threads", type=int, default=1, help="并发数（默认 1，慢但稳）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    accounts = _load_accounts(args)
    if not accounts:
        print("没有可查活的账号", file=sys.stderr)
        return 0
    print(f"待查活 {len(accounts)} 个账号（并发 {args.threads}）", flush=True)

    results = []
    if args.threads > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as ex:
            futs = [ex.submit(_one, acc, args.proxy or None) for acc in accounts]
            for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
                r = fut.result()
                results.append(r)
                print(f"[{i}/{len(accounts)}] {r['email']} -> {r.get('status')} ok={r.get('ok')} "
                      f"err={str(r.get('error') or '')[:80]} {r.get('elapsed_s')}s", flush=True)
    else:
        for i, acc in enumerate(accounts, 1):
            r = _one(acc, args.proxy or None)
            results.append(r)
            print(f"[{i}/{len(accounts)}] {r['email']} -> {r.get('status')} ok={r.get('ok')} "
                  f"err={str(r.get('error') or '')[:80]} {r.get('elapsed_s')}s", flush=True)

    live = sum(1 for r in results if r.get("ok"))
    dead = sum(1 for r in results if r.get("status") == "deactivated")
    failed = sum(1 for r in results if r.get("status") == "failed")
    summary = {"total": len(results), "live": live, "deactivated": dead, "failed": failed}
    if args.json:
        print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1))
    else:
        print(f"\n汇总: 共 {len(results)} | 正常刷新 {live} | 已废 {dead} | 失败 {failed}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
