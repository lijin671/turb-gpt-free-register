#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量校验已注册账号 access_token 是否仍然有效（走代理池 + 项目会话指纹）。

用法:
  python3 tools/check_accounts_valid.py [accounts目录] [--proxy http://...] [--limit N]

输出: 每个账号 email | 状态(ok/revoked/error) | 实际 email / plan
"""
import argparse, glob, json, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'), override=True)

from core.session import BrowserSession


def check_token(token, proxy, timeout=25, device_id=""):
    """用 /backend-api/me 校验 token；403/网络异常时换新代理重试 1 次。"""
    for attempt in (1, 2):
        try:
            session = BrowserSession(proxy=proxy, detect_exit_geo=False, device_id=device_id or None)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            resp = session.get("https://chatgpt.com/backend-api/me", headers=headers, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                return ("ok", data.get("email", ""), data.get("plan_type", ""),
                        f"user={data.get('id','')[:8]}")
            if resp.status_code == 401:
                code = ""
                try:
                    code = resp.json().get("error", {}).get("code", "")
                except Exception:
                    pass
                return ("revoked", "", "", f"HTTP401 code={code}")
            if attempt == 1 and resp.status_code in (403, 429, 500, 502, 503):
                time.sleep(1)
                continue
            return ("error", "", "", f"HTTP{resp.status_code} {resp.text[:120]}")
        except Exception as e:
            if attempt == 1:
                time.sleep(1)
                continue
            return ("error", "", "", f"{type(e).__name__}: {e}")
    return ("error", "", "", "重试后仍失败")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default="accounts")
    ap.add_argument("--proxy", default="", help="覆盖代理；默认用每个账号自己的 proxy_used（同 IP 校验）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-mark", action="store_true",
                    help="不把失效账号的出口 IP 标记为连坐风险（默认标记，隔离同 IP 其余账号）")
    args = ap.parse_args()

    dirs = sorted(glob.glob(os.path.join(args.root, "*", "")), reverse=True)
    ok, revoked, errors = [], [], []
    marked_ips = set()
    total = 0
    for d in dirs:
        acct_file = os.path.join(d, "注册成功账号.json")
        if not os.path.exists(acct_file):
            continue
        with open(acct_file) as f:
            data = json.load(f)
        accounts = data if isinstance(data, list) else [data]
        for a in accounts:
            if not a.get("access_token"):
                continue
            total += 1
            if args.limit and total > args.limit:
                break
            email = a.get("email", "")
            acct_proxy = a.get("proxy_used") or args.proxy
            status, email2, plan, note = check_token(
                a["access_token"], acct_proxy,
                device_id=(a.get("extra") or {}).get("device_id", ""),
            )
            line = f"[{os.path.basename(d.rstrip('/'))}] {email} -> {status}"
            if email2:
                line += f" (实际email={email2}, plan={plan})"
            if note:
                line += f" {note}"
            print(line, flush=True)
            if status == "ok":
                ok.append((d, a, email2 or email, plan))
            elif status == "revoked":
                revoked.append((d, email))
                if not args.no_mark:
                    # 论坛经验：同 IP 多号连坐死。账号确认失效时，把该出口 IP
                    # 标记为连坐风险，隔离 DB 中同 IP 的其余账号。
                    try:
                        from core.ip_discipline import mark_ip_co_risk
                        ip_ref = a.get("ip_key") or a.get("exit_ip") or a.get("proxy_used") or ""
                        if ip_ref:
                            mark_ip_co_risk(ip_ref, "account_revoked_validation")
                            marked_ips.add(ip_ref)
                    except Exception as exc:
                        print(f"  [IP纪律] 标记连坐风险失败: {exc}", flush=True)
            else:
                errors.append((d, email, note))
            time.sleep(0.5)

    print(f"\n总计 {total} | 有效 {len(ok)} | 失效 {len(revoked)} | 异常 {len(errors)}"
          + (f" | 连坐风险IP {len(marked_ips)}" if marked_ips else ""))
    if ok:
        print("\n有效账号清单:")
        for d, a, email, plan in ok:
            print(f"  {os.path.basename(d.rstrip('/'))} {email} plan={plan}")


if __name__ == "__main__":
    main()
