#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Token 复活守护进程：定时扫描过期 token 并自动复活。

用法:
  nohup python3 tools/revive_daemon.py --interval 1200 --limit 5 >> /tmp/revive_daemon.log 2>&1 &

参数:
  --interval  循环间隔（秒），默认 1200（20min）
  --limit     每轮最多复活 N 个账号，默认 5
  --once      只跑一次然后退出（测试用）
"""
import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'), override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("revive_daemon")


def run_one_with_retry(email: str, max_retries: int = 3) -> dict:
    """对单个账号执行 token 复活，403 时换代理重试。"""
    from core import db
    from core.token_revival import revive_account
    from config.proxy import pick_proxy

    for attempt in range(max_retries):
        try:
            res = revive_account(email)
            if res.get("ok"):
                return res
            # 403 时换代理重试
            msg = str(res.get("message", ""))
            if "403" in msg or "CF" in msg or "challenge" in msg.lower():
                new_proxy = pick_proxy()
                logger.info("[Revive] %s 第 %d 次遇到 403，换代理重试: %s...", email, attempt + 1, new_proxy[:50])
                # 更新 DB 中的 proxy_used
                acc = db.get_account_by_email(email)
                if acc and new_proxy:
                    # 直接修改 proxy_used 并重试
                    db.update_account_proxy(email, new_proxy)
                    continue
            return res
        except Exception as e:
            msg = str(e)
            if "403" in msg or "CF" in msg:
                new_proxy = pick_proxy()
                logger.info("[Revive] %s 第 %d 次异常 403，换代理重试: %s...", email, attempt + 1, new_proxy[:50])
                db.update_account_proxy(email, new_proxy)
                continue
            raise
    return {"ok": False, "email": email, "message": f"重试 {max_retries} 次后仍失败"}


def run_once(limit: int) -> dict:
    """执行一轮 token 复活。返回 {scanned, revived, failed, skipped}。"""
    from core import db
    from core.token_revival import revive_account

    accounts = db.list_accounts(limit=100000)
    candidates = [
        a for a in accounts
        if str(a.get("access_token") or "").strip()
        and str(a.get("proxy_used") or "").strip()
        and str(a.get("email") or "").strip()
    ][:limit]

    result = {"scanned": len(candidates), "revived": 0, "failed": 0, "skipped": 0}

    for a in candidates:
        email = a.get("email", "")
        try:
            res = run_one_with_retry(email)
            if res.get("ok"):
                logger.info("[Revive] ✅ %s: %s", email, res.get("message", ""))
                result["revived"] += 1
            else:
                logger.warning("[Revive] ⚠️ %s: %s", email, res.get("message", ""))
                result["failed"] += 1
        except Exception as e:
            logger.error("[Revive] ❌ %s: %s: %s", email, type(e).__name__, e)
            result["failed"] += 1
        time.sleep(5)  # 低 CPU 间隔

    logger.info("[Revive] 本轮完成: %s", json.dumps(result))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=int, default=1200, help="循环间隔（秒），默认 1200")
    ap.add_argument("--limit", type=int, default=5, help="每轮最多复活 N 个，默认 5")
    ap.add_argument("--once", action="store_true", help="只跑一次")
    args = ap.parse_args()

    logger.info("Token 复活守护进程启动: interval=%ds, limit=%d", args.interval, args.limit)

    if args.once:
        run_once(args.limit)
        return 0

    while True:
        try:
            run_once(args.limit)
        except Exception as e:
            logger.error("守护进程异常: %s: %s", type(e).__name__, e)
        logger.info("等待 %ds 后下一轮...", args.interval)
        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    sys.exit(main())
