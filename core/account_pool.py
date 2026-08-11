# -*- coding: utf-8 -*-
"""账号库存水位统计（DB 口径，纯函数）。

供 tools/check_account_pool.py 与 WebUI /api/summary 复用：
potential_usable = 有 access_token 且未被连坐标记且 codex 非 failed/missing。
"""
from datetime import datetime


def _parse_dt(value) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _is_expired(expires_at) -> bool:
    parsed = _parse_dt(expires_at)
    return parsed is not None and parsed < datetime.now(parsed.tzinfo)


def pool_stats(accounts: list[dict]) -> dict:
    """统计账号库存水位。potential_usable = 有 token 且未被连坐标记且 codex 非失败/缺失。"""
    total = len(accounts or [])
    has_token = co_risk = codex_failed = codex_missing = plus_success = expired = potential_usable = 0
    for a in accounts or []:
        token = str(a.get("access_token") or "")
        has_token += bool(token)
        if a.get("ip_co_risk"):
            co_risk += 1
        cs = str(a.get("codex_status") or "").lower()
        if cs == "failed":
            codex_failed += 1
        elif cs == "missing":
            codex_missing += 1
        if str(a.get("plus_status") or "").lower() == "success":
            plus_success += 1
        if _is_expired(a.get("expires_at")):
            expired += 1
        if token and not a.get("ip_co_risk") and cs not in ("failed", "missing"):
            potential_usable += 1
    return {
        "total": total,
        "has_token": has_token,
        "co_risk": co_risk,
        "codex_failed": codex_failed,
        "codex_missing": codex_missing,
        "plus_success": plus_success,
        "expired": expired,
        "potential_usable": potential_usable,
        "potential_usable_rate": round(potential_usable / total, 4) if total else 0.0,
    }
