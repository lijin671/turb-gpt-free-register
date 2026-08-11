#!/usr/bin/env python3
"""批量创建 ManyMail 邮箱池账号（默认域 lijin.kdns.fr 优先）。

用法:
  python scripts/create_manymail_pool.py --count 20 --domain lijin.kdns.fr
  python scripts/create_manymail_pool.py --count 5 --out data/pool.tsv --dry-run

输出 TSV: email \\t password \\t token
"""
from __future__ import annotations

import argparse
import csv
import secrets
import string
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.manymail_client import _random_local, _random_password, _base_url  # noqa: E402


def create_account(base: str, domain: str, retries: int = 10) -> dict:
    last = None
    for _ in range(retries):
        local = _random_local()
        address = f"{local}@{domain}"
        password = _random_password()
        r = requests.post(
            base.rstrip("/") + "/accounts",
            json={"address": address, "password": password},
            timeout=20,
        )
        if r.status_code in (200, 201):
            tok = requests.post(
                base.rstrip("/") + "/token",
                json={"address": address, "password": password},
                timeout=20,
            )
            token = ""
            if tok.status_code == 200:
                token = str((tok.json() or {}).get("token") or "")
            return {"email": address, "password": password, "token": token,
                    "created_at": datetime.now(timezone.utc).isoformat()}
        if r.status_code == 422 and "already" in r.text.lower():
            last = "conflict"
            continue
        last = f"HTTP {r.status_code}: {r.text[:120]}"
        raise SystemExit(f"创建 {address} 失败: {last}")
    raise SystemExit(f"创建失败（冲突过多）: last={last}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--domain", default="lijin.kdns.fr")
    ap.add_argument("--api-base", default="http://100.73.121.125:8080")
    ap.add_argument("--out", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    out_path = Path(args.out) if args.out else ROOT / "data" / f"manymail_pool_{args.domain}.tsv"
    rows = []
    for i in range(1, args.count + 1):
        if args.dry_run:
            print(f"[dry-run] 会创建: <random>@{args.domain}")
            continue
        acc = create_account(args.api_base, args.domain)
        rows.append(acc)
        print(f"[{i}/{args.count}] {acc['email']}")
        time.sleep(0.1)

    if args.dry_run:
        return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["email", "password", "token", "created_at"])
        for r in rows:
            w.writerow([r["email"], r["password"], r["token"], r["created_at"]])
    out_path.chmod(0o600)
    print(f"已写入 {out_path}（{len(rows)} 个账号，权限 600）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
