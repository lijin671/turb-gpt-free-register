# -*- coding: utf-8 -*-
"""
零元 Plus 开通集成模块。

将零元 Plus 流程集成到现有的 registration_service.py 中。
注册完成后自动触发：session 提取 → 切菲 → 绑卡 → 验证。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from core.plus_zero import (
    run_zero_plus,
    fetch_session,
)

logger = logging.getLogger(__name__)


def try_zero_plus_after_registration(
    email: str,
    access_token: str,
    account_id: str = "",
    proxy: str = "",
    *,
    card_number: str = "",
    exp_month: str = "",
    exp_year: str = "",
    cvc: str = "",
    card_name: str = "CHATGPT USER", session_info: dict | None = None,
    retry_on_fail: bool = False,
    max_retries: int = 2,
    device_id: str = "",
) -> dict:
    """
    注册成功后自动调用零元 Plus 开通。

    流程:
      1. 从 access_token 提取 session（获取 account_id 如果没传）
      2. 执行 run_zero_plus 完整流程

    Args:
        email: 注册邮箱
        access_token: ChatGPT accessToken
        account_id: account id（可选，不传则从 session 获取）
        proxy: 代理
        card_number: 卡号
        exp_month: 有效期月
        exp_year: 有效期年
        cvc: CVV
        card_name: 持卡人姓名
        retry_on_fail: 绑卡失败后是否重试
        max_retries: 最大重试次数

    Returns:
        dict: plus 开通结果
    """
    from config import plus as _cfg

    if not _cfg.ENABLE_ZERO_PLUS:
        logger.info("[Plus集成] ENABLE_ZERO_PLUS=False，跳过")
        return {"status": "skipped", "message": "ENABLE_ZERO_PLUS=False"}

    if not access_token:
        logger.warning("[Plus集成] 无 access_token，跳过")
        return {"status": "skipped", "message": "无 access_token"}

    if not card_number:
        logger.info("[Plus集成] 未配置卡号，跳过")
        return {"status": "skipped", "message": "未配置卡号"}

    logger.info("[Plus集成] 注册完成，开始零元 Plus 开通: %s", email)

    # 如果没传 account_id，从 session 获取
    if not account_id:
        try:
            session_data = fetch_session(access_token, proxy)
            account_id = session_data.get("account", {}).get("id", "")
            if not account_id:
                return {"status": "failed", "message": "无法从 session 获取 account_id"}
        except Exception as exc:
            logger.error("[Plus集成] session 提取失败: %s", exc)
            return {"status": "failed", "message": f"session 提取失败: {exc}"}

    # 执行零元 Plus
    result = run_zero_plus(
        session_info=session_info,
        access_token=access_token,
        account_id=account_id,
        email=email,
        proxy=proxy,
        card_number=card_number,
        exp_month=exp_month,
        exp_year=exp_year,
        cvc=cvc,
        card_name=card_name,
        device_id=device_id,
    )

    # 如果失败且需要重试
    if retry_on_fail and not result.get("ok") and result.get("status") in ("bind_failed", "exception"):
        for attempt in range(1, max_retries + 1):
            logger.info("[Plus集成] 第 %s 次重试...", attempt)
            time.sleep(3)
            result = run_zero_plus(
                access_token=access_token,
                account_id=account_id,
                email=email,
                proxy=proxy,
                card_number=card_number,
                exp_month=exp_month,
                exp_year=exp_year,
                cvc=cvc,
                card_name=card_name,
                device_id=device_id,
            )
            if result.get("ok"):
                logger.info("[Plus集成] 重试成功")
                break

    return result


def try_zero_plus_with_account_file(
    account_file: str,
    card_number: str = "",
    exp_month: str = "",
    exp_year: str = "",
    cvc: str = "",
) -> list[dict]:
    """
    从账号文件批量执行零元 Plus 开通。

    账号文件格式: 每行一个 JSON，包含 email, access_token, account_id
    或者使用项目现有的账号导出格式。
    """
    import json as _json
    from pathlib import Path

    path = Path(account_file)
    if not path.exists():
        logger.error("账号文件不存在: %s", account_file)
        return []

    results = []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            acct = _json.loads(line)
        except _json.JSONDecodeError:
            logger.warning("跳过无效行: %s", line[:60])
            continue

        email = acct.get("email", "")
        token = acct.get("access_token", "") or acct.get("accessToken", "")
        aid = acct.get("account_id", "") or acct.get("accountId", "")

        if not token:
            logger.warning("跳过无 token 的账号: %s", email)
            continue

        logger.info("处理账号: %s", email)
        result = run_zero_plus(
            access_token=token,
            account_id=aid,
            email=email,
            card_number=card_number,
            exp_month=exp_month,
            exp_year=exp_year,
            cvc=cvc,
        )
        result["email"] = email
        results.append(result)

    return results


if __name__ == "__main__":
    # 简单的测试入口
    import sys
    token = os.environ.get("CHATGPT_TOKEN", "")
    aid = os.environ.get("CHATGPT_ACCOUNT_ID", "")
    card = os.environ.get("CARD_NUMBER", "")
    em = os.environ.get("CARD_EXP_MONTH", "")
    ey = os.environ.get("CARD_EXP_YEAR", "")
    cv = os.environ.get("CARD_CVC", "")

    if not token:
        print("请设置环境变量: CHATGPT_TOKEN, CHATGPT_ACCOUNT_ID")
        print("可选: CARD_NUMBER, CARD_EXP_MONTH, CARD_EXP_YEAR, CARD_CVC")
        sys.exit(1)

    result = try_zero_plus_after_registration(
        email="test@example.com",
        access_token=token,
        account_id=aid,
        card_number=card,
        exp_month=em,
        exp_year=ey,
        cvc=cv,
    )
    print(_json.dumps(result, ensure_ascii=False, indent=2, default=str))
