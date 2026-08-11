#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地注册成功的有效账号导出到 ChatGPT-to-API 的 access_tokens.json。

用法:
  python3 tools/export_chatgpt2api.py [--accounts-dir accounts] [--target /home/zzx/research-repos/ChatGPT-to-API/access_tokens.json] [--update-proxies]

行为:
  1. 扫描 accounts/ 下所有 注册成功账号.json
  2. 逐个用代理池校验 access_token 是否仍有效（/backend-api/me）
  3. 有效账号合并写入目标 access_tokens.json（{email: {token, puid: ""}}），先备份
  4. --update-proxies 时把 ChatGPT-to-API/proxies.txt 更新为 Pokemon.cli 代理
"""
import argparse, glob, json, os, re, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'), override=True)

from core.session import BrowserSession
from config.proxy import PROXY_POOL


def check_token(token, proxy, timeout=25, device_id=""):
    try:
        s = BrowserSession(proxy=proxy, detect_exit_geo=False, device_id=device_id or None)
        r = s.get("https://chatgpt.com/backend-api/me",
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                  timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            return True, data.get("email", "")
        return False, f"HTTP{r.status_code}"
    except Exception as e:
        return False, f"{type(e).__name__}"


def load_accounts(root: str):
    out = []
    for d in sorted(glob.glob(os.path.join(root, "*", ""))):
        f = os.path.join(d, "注册成功账号.json")
        if not os.path.exists(f):
            continue
        try:
            with open(f) as fh:
                data = json.load(fh)
        except Exception:
            continue
        accounts = data if isinstance(data, list) else [data]
        for a in accounts:
            if a.get("access_token"):
                out.append((os.path.basename(d.rstrip("/")), a))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts-dir", default="accounts")
    ap.add_argument("--target", default="/home/zzx/research-repos/ChatGPT-to-API/access_tokens.json")
    ap.add_argument("--proxy", default="", help="覆盖代理；默认用每个账号自己的 proxy_used（同 IP 校验）")
    ap.add_argument("--update-proxies", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--include-co-risk", action="store_true",
                    help="连带坐风险标记的账号一起导出（默认跳过：同 IP 多号连坐死）")
    args = ap.parse_args()

    accounts = load_accounts(args.accounts_dir)
    print(f"扫描到 {len(accounts)} 个账号记录", flush=True)

    # 连坐风险隔离：默认跳过 DB 中 ip_co_risk=True 的账号
    co_risk_emails = set()
    if not args.include_co_risk:
        try:
            from core import db
            for row in db.list_accounts(limit=100000):
                if row.get("ip_co_risk"):
                    co_risk_emails.add((row.get("email") or "").lower())
        except Exception:
            pass
        if co_risk_emails:
            print(f"已隔离 {len(co_risk_emails)} 个连坐风险账号（--include-co-risk 可强制导出）", flush=True)

    if not os.path.exists(args.target):
        print(f"目标文件不存在: {args.target}", flush=True)
        sys.exit(2)

    with open(args.target) as f:
        existing = json.load(f)

    valid, invalid = [], []
    for i, (batch, a) in enumerate(accounts):
        if args.limit and i >= args.limit:
            break
        email = a.get("email", "")
        if (email or "").lower() in co_risk_emails:
            print(f"[{batch}] {email} -> ⏭️ 连坐风险已隔离，跳过", flush=True)
            continue
        acct_proxy = a.get("proxy_used") or args.proxy
        ok, info = check_token(
            a["access_token"], acct_proxy,
            device_id=(a.get("extra") or {}).get("device_id", ""),
        )
        print(f"[{batch}] {email} -> {'✅ 有效' if ok else '❌ ' + info}", flush=True)
        if ok:
            valid.append((email, a["access_token"]))
        else:
            invalid.append((batch, email, info))
        time.sleep(0.4)

    if not valid:
        print("\n没有有效账号，跳过写入", flush=True)
        sys.exit(1)

    # 备份 + 合并
    bak = args.target + ".bak"
    if not os.path.exists(bak):
        with open(args.target) as f:
            open(bak, "w").write(f.read())
        print(f"已备份原文件 -> {bak}", flush=True)

    added = 0
    for email, token in valid:
        if email not in existing:
            added += 1
        existing[email] = {"token": token, "puid": ""}
    with open(args.target, "w") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 已写入 {len(valid)} 个有效账号（新增 {added}），当前总量 {len(existing)} -> {args.target}", flush=True)

    if args.update_proxies:
        proxies_file = os.path.join(os.path.dirname(args.target), "proxies.txt")
        if os.path.exists(proxies_file):
            with open(proxies_file, "w") as f:
                f.write((args.proxy or PROXY_POOL or "") + "\n")
            print(f"✅ proxies.txt 已更新 -> {proxies_file}", flush=True)

    if invalid:
        print("\n失效账号（可忽略）:", flush=True)
        for batch, email, info in invalid[:10]:
            print(f"  [{batch}] {email} {info}", flush=True)


if __name__ == "__main__":
    main()
