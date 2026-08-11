#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""试用账号 checkout confirm 探测：验证 plus-1-month-free 100% 折扣是否免卡。"""
import json
import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

from core.plus_zero import (
    PlusSession, fetch_session, switch_to_philippines,
    _update_checkout_plan, _submit_checkout_billing_address,
    _confirm_checkout, _resolve_publishable_key,
)


def main():
    data = json.load(open(_PROJECT_ROOT / "注册成功的邮箱.json"))
    rows = data if isinstance(data, list) else data.get("accounts", [])
    trials = [r for r in rows if r.get("plus_trial_eligible") and not r.get("token_expired")]
    if not trials:
        print("无可用 trial 账号")
        return 1
    acc = trials[0]
    email = acc["email"]
    token = acc["access_token"]
    account_id = acc.get("account_id") or ""
    print(f"使用 trial 账号: {email} (trial={acc.get('plus_trial_campaign_id')})")

    import secrets as _secrets
    _sid = _secrets.token_hex(8)
    resin_proxy = f"http://Pokemon.cli-session-{_sid}:9624f371e464ba2b8a73c4f42e841135f0a969d21aaec6d1@127.0.0.1:2260"
    print(f"使用树脂动态会话代理 sid={_sid}")
    ps = PlusSession(access_token=token, account_id=account_id, email=email,
                     proxy=resin_proxy)
    import time as _t
    sess = None
    for attempt in range(1, 4):
        try:
            sess = fetch_session(token, proxy=resin_proxy)
            ps.account_id = sess.get("account", {}).get("id", account_id)
            print(f"session OK accountId={ps.account_id}")
            break
        except Exception as e:
            print(f"session 失败(第{attempt}次): {e}")
            if attempt < 3:
                _t.sleep(35)
    if sess is None:
        return 1

    # 阶段3: checkout（优先尝试保持 US + promo，而非强制切菲）
    try:
        switch_to_philippines(ps)
        print(f"checkout OK sid={ps.checkout_session_id} zero_price={ps.zero_price} country={ps.billing_country}")
    except Exception as e:
        print(f"checkout 失败: {e}")
        return 1

    # 阶段7.2: 更新 checkout 计划（带 promo campaign）
    try:
        ok = _update_checkout_plan(ps)
        print(f"update_checkout_plan OK={ok} state={json.dumps(ps.checkout_state, ensure_ascii=False)[:200]}")
    except Exception as e:
        print(f"update_checkout_plan 异常: {e}")

    # 阶段7.3: 提交账单地址
    try:
        _submit_checkout_billing_address(ps)
        print("billing address submitted")
    except Exception as e:
        print(f"billing address 异常: {e}")

    # 阶段7.5: 直接 confirm（无卡，看试用是否免卡）
    try:
        result = _confirm_checkout(ps)
        print(f"CONFIRM 响应: {json.dumps(result, ensure_ascii=False)[:600]}")
    except Exception as e:
        print(f"confirm 失败: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
