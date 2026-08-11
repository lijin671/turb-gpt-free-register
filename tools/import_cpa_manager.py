#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量导入注册账号到 CPA Manager Plus（127.0.0.1:18317 /v0/management）。

用法:
  python3 tools/import_cpa_manager.py --accounts accounts/20260805-1个-27 --name chatgpt-20260805.json
  python3 tools/import_cpa_manager.py --scan --name all.json      # 扫描全部账号目录
  python3 tools/import_cpa_manager.py --filter 20260805 --dry-run # 只看生成内容

配置（.env）:
  CPA_MANAGER_PLUS_BASE=http://127.0.0.1:18317
  CPA_MANAGER_PLUS_KEY=cpamp_...   # 管理面板 admin key（docker logs cpa-manager-plus 可见）

注意：CPA Manager Plus(18317) 的管理密钥与 cli-proxy-api(8317) 的
CPA_MANAGEMENT_KEY 不是同一个；本工具优先读 CPA_MANAGER_PLUS_KEY。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.cpa_manager_import import api, build_auth_file, scan_account_dirs  # noqa: E402

logger = logging.getLogger(__name__)


def _load_dotenv():
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    except Exception:
        pass


def main() -> int:
    _load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description="批量导入注册账号到 CPA Manager Plus")
    ap.add_argument("--base", default=os.getenv("CPA_MANAGER_PLUS_BASE", os.getenv("CPA_MANAGEMENT_BASE", "http://127.0.0.1:18317")))
    ap.add_argument("--key", default=os.getenv("CPA_MANAGER_PLUS_KEY", os.getenv("CPA_MANAGEMENT_KEY", "")))
    ap.add_argument("--accounts", default="", help="指定账号目录（默认扫描 accounts/）")
    ap.add_argument("--filter", default="", help="按目录名/邮箱过滤")
    ap.add_argument("--name", default="", help="上传的 auth 文件名（默认 chatgpt-YYYYMMDD-HHMMSS.json）")
    ap.add_argument("--no-verify", action="store_true", help="上传后不调 models 验证")
    ap.add_argument("--dry-run", action="store_true", help="只生成文件内容不上传")
    args = ap.parse_args()

    base = Path(__file__).resolve().parent.parent
    accounts_dir = Path(args.accounts) if args.accounts else base / "accounts"

    accounts = scan_account_dirs(accounts_dir, args.filter)
    if not accounts:
        logger.error("未扫描到任何账号（目录: %s）", accounts_dir)
        return 1

    logger.info("扫描到 %d 个账号", len(accounts))
    content = build_auth_file(accounts)
    if not content.strip():
        logger.error("没有可导出的 access_token")
        return 1

    if args.dry_run:
        print(content)
        return 0

    if not args.key:
        logger.error("缺少管理密钥：--key 或 .env CPA_MANAGER_PLUS_KEY（docker logs cpa-manager-plus 首启日志 cpamp_...）")
        return 1

    from core.cpa_manager_import import import_batch
    name = args.name or ""
    result = import_batch(
        accounts=accounts,
        base=args.base.rstrip("/"),
        key=args.key,
        name=name,
        verify=not args.no_verify,
        delete_dup=True,
    )
    if not result.get("ok"):
        logger.error("%s: %s", result.get("message"), json.dumps(result.get("data", {}), ensure_ascii=False)[:400])
        return 1
    logger.info("✅ %s（模型 %s）", result["message"], result.get("model_count"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
