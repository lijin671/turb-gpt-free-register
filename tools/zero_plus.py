#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
零元 Plus 开通 CLI 工具。

用法:
  # 仅切菲（获取结算链接）
  python tools/zero_plus.py --token YOUR_TOKEN --account-id YOUR_ACCOUNT_ID --switch-only

  # 完整流程（切菲 + 绑卡 + 验证）
  python tools/zero_plus.py --token YOUR_TOKEN --account-id YOUR_ACCOUNT_ID \
    --card 5236861234567890 --exp-month 12 --exp-year 29 --cvc 123

  # 从文件读取 token 和 card
  python tools/zero_plus.py --config account.json

  # 交互式
  python tools/zero_plus.py --token YOUR_TOKEN --account-id YOUR_ACCOUNT_ID --interactive
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# 确保项目根在 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.plus_zero import run_zero_plus, main as module_main

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

# 简化的 CLI 入口
if __name__ == "__main__":
    sys.exit(module_main())
