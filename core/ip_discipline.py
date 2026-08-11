# -*- coding: utf-8 -*-
"""
IP 关联控制（1ip1号）

论坛经验（linux.do 2708795「古法 gpt 基本宣告死亡，1ip1号了基本」）：
同一出口 IP 短时间注册多个号会连坐死号——1ip5号时第 5 个接完码瞬间全死。
本模块在注册管线落地三条纪律：

1. 唯一 IP 分配：每个注册任务占用一个独立 ip_key
   - resin 动态会话（-session-{sid}）：sid 即 IP，天然唯一
   - 静态代理：按 scheme://host:port 去重，MAX_ACCOUNTS_PER_IP=1 时绝不复用
2. 冷却窗口：IP 使用后进入冷却（失败 IP_COOLDOWN_SECONDS；成功注册
   IP_SUCCESS_COOLDOWN_SECONDS，默认 24h，参考论坛 2708795 的连坐经验），冷却期内不再分配
3. 连坐风险标记：某 IP 下账号被确认死亡时，同 IP 其余账号标记 ip_co_risk，
   导出/校验工具据此隔离，避免把连坐号导进生产池

状态持久化到 data/ip_discipline.json（gitignore 已排除），进程重启不丢冷却记录。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config.proxy import (
    IP_COOLDOWN_SECONDS,
    IP_SUCCESS_COOLDOWN_SECONDS,
    MAX_ACCOUNTS_PER_IP,
    proxy_ip_key,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STATE_FILE = Path(os.environ.get("IP_DISCIPLINE_FILE") or (_PROJECT_ROOT / "data" / "ip_discipline.json"))
_lock = threading.RLock()


def _now() -> datetime:
    return datetime.now()


def _load() -> dict:
    if not _STATE_FILE.exists():
        return {"leases": {}, "usage": {}, "co_risk": {}}
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"leases": {}, "usage": {}, "co_risk": {}}


def _save(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def clear_state() -> None:
    """清空全部纪律状态（测试用）。"""
    with _lock:
        if _STATE_FILE.exists():
            _STATE_FILE.unlink()
        _save({"leases": {}, "usage": {}, "co_risk": {}})


def _usage_blocked_until(record: dict, base_cooldown: int) -> datetime:
    """单条使用记录的冷却到期时间：成功注册用 IP_SUCCESS_COOLDOWN_SECONDS，
    失败/其他用基础冷却（base_cooldown 兼容调用方覆盖）。"""
    cooldown = IP_SUCCESS_COOLDOWN_SECONDS if str(record.get("outcome") or "") == "success" else int(base_cooldown)
    return _parse_ts(record.get("used_at")) + timedelta(seconds=max(1, cooldown))


def _prune_usage(state: dict, cooldown: int) -> None:
    """清理冷却窗口外的历史使用记录（按每条记录自身的冷却窗口）。"""
    now = _now()
    for key in list(state.get("usage", {})):
        keep = [u for u in state.get("usage", {}).get(key, []) if _usage_blocked_until(u, cooldown) > now]
        if keep:
            state["usage"][key] = keep
        else:
            state["usage"].pop(key, None)


def _parse_ts(value: str | None) -> datetime:
    try:
        return datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return datetime.min


def is_ip_free(proxy: str, *, cooldown: int | None = None, max_per_ip: int | None = None) -> tuple[bool, str]:
    """
    判断代理的出口 IP 当前是否可用（无冷却/未超账号数上限/未被占用）。

    Returns: (free, reason)；reason 为 "" 表示可用。
    """
    key = proxy_ip_key(proxy or "")
    cooldown = int(cooldown if cooldown is not None else IP_COOLDOWN_SECONDS)
    max_per_ip = int(max_per_ip if max_per_ip is not None else MAX_ACCOUNTS_PER_IP)
    if not key:
        # 直连（无代理）：也按固定出口处理，同样受纪律约束
        key = "direct"
    with _lock:
        state = _load()
        _prune_usage(state, cooldown)
        if key in state.get("co_risk", {}):
            return False, "co_risk"
        lease = state.get("leases", {}).get(key)
        if lease and _parse_ts(lease.get("expires_at")) > _now():
            return False, "leased"
        recent = [u for u in state.get("usage", {}).get(key, []) if _usage_blocked_until(u, cooldown) > _now()]
        if max_per_ip > 0 and len(recent) >= max_per_ip:
            return False, "max_accounts"
        return True, ""


def claim_proxy(proxy: str, *, owner: str = "", cooldown: int | None = None) -> bool:
    """占用代理（lease），防止并发任务复用同一出口 IP。返回是否占用成功。"""
    key = proxy_ip_key(proxy or "") or "direct"
    cooldown = int(cooldown if cooldown is not None else IP_COOLDOWN_SECONDS)
    with _lock:
        free, reason = is_ip_free(proxy, cooldown=cooldown)
        if not free:
            return False
        state = _load()
        state.setdefault("leases", {})[key] = {
            "claimed_at": _now().isoformat(timespec="seconds"),
            "owner": owner or "",
            "expires_at": (_now() + timedelta(seconds=cooldown)).isoformat(timespec="seconds"),
        }
        _save(state)
        return True


def release_proxy(proxy: str, *, owner: str = "") -> None:
    """释放代理占用（幂等）；owner 不匹配时仍强制释放（防泄漏）。"""
    key = proxy_ip_key(proxy or "") or "direct"
    with _lock:
        state = _load()
        lease = state.get("leases", {}).get(key)
        if lease and owner and lease.get("owner") not in ("", owner):
            logger.warning(f"[IP纪律] 释放 {key} 时 owner 不匹配（{lease.get('owner')} vs {owner}），强制释放")
        state.get("leases", {}).pop(key, None)
        _save(state)


def record_ip_use(proxy: str, email: str = "", outcome: str = "failure") -> None:
    """记录一次 IP 使用（注册完成/失败后调用），触发冷却计时。

    outcome:
      - success: 注册成功，冷却按 IP_SUCCESS_COOLDOWN_SECONDS（论坛经验 1ip1号：
        成功号 token 常 ~30 分钟内被吊销，同 IP 短时间再注册会连坐）
      - failure: 失败/其他，冷却按 IP_COOLDOWN_SECONDS（网络失败可较快重试）
    """
    key = proxy_ip_key(proxy or "") or "direct"
    record_outcome = "success" if str(outcome or "").strip().lower() == "success" else "failure"
    with _lock:
        state = _load()
        state.setdefault("usage", {}).setdefault(key, []).append({
            "used_at": _now().isoformat(timespec="seconds"),
            "email": email or "",
            "outcome": record_outcome,
        })
        _prune_usage(state, IP_COOLDOWN_SECONDS)
        _save(state)


def acquire_proxy(owner: str = "", *, timeout: float = 0, poll_interval: float = 10.0,
                 interrupt=None) -> str | None:
    """
    带等待的 1ip1号 选代理：池里没有可用 IP 时轮询等待，直到拿到或超时。

    - timeout <= 0：只试一次，拿不到立即返回 None
    - interrupt：可选回调，每次等待前调用；抛异常（如 StopRequested）可中断等待
    """
    from config.proxy import pick_disciplined_proxy
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        if interrupt is not None:
            interrupt()
        proxy = pick_disciplined_proxy(owner=owner)
        if proxy is not None:
            return proxy
        if timeout <= 0 or time.time() >= deadline:
            return None
        time.sleep(min(max(0.1, poll_interval), max(0.1, deadline - time.time())))


def _resolve_key(proxy_or_key: str) -> str:
    """把代理 URL 或裸 IP 归一成 ip_key 形态（裸 IP 视为静态代理 host）。"""
    value = (proxy_or_key or "").strip()
    if not value:
        return "direct"
    if "://" in value:
        return proxy_ip_key(value)
    return value


def accounts_sharing_ip(proxy_or_key: str) -> list[dict]:
    """找出与给定代理/IP 共用同一出口 IP 的账号（DB 行）。"""
    key = _resolve_key(proxy_or_key)
    from core import db
    rows = db.list_accounts(limit=100000)
    out = []
    for row in rows:
        row_key = row.get("ip_key") or proxy_ip_key(row.get("proxy_used") or "")
        if row_key and row_key == key:
            out.append(row)
            continue
        exit_ip = str(row.get("exit_ip") or "")
        if exit_ip and exit_ip == key:
            out.append(row)
    return out


def mark_ip_co_risk(proxy_or_key: str, reason: str, *, emails: list[str] | None = None) -> int:
    """
    把某出口 IP 标记为连坐风险：同 IP 账号全部打 ip_co_risk 标。
    返回被标记的账号数（DB 中匹配到的）。
    """
    key = _resolve_key(proxy_or_key)
    with _lock:
        state = _load()
        state.setdefault("co_risk", {})[key] = {
            "reason": reason or "",
            "marked_at": _now().isoformat(timespec="seconds"),
        }
        _save(state)

    if emails is None:
        emails = [r.get("email") for r in accounts_sharing_ip(key) if r.get("email")]
    emails = [e for e in emails if e]
    marked = 0
    if emails:
        from core import db
        marked = db.mark_accounts_ip_co_risk(emails, reason)
    logger.warning(
        f"[IP纪律] 出口 IP {key} 标记连坐风险（{reason}），同 IP 账号 {marked} 个已隔离"
    )
    return marked


def status_summary() -> dict:
    """当前纪律状态摘要（供 WebUI/工具展示）。"""
    with _lock:
        state = _load()
        leases = state.get("leases", {})
        usage = state.get("usage", {})
        co_risk = state.get("co_risk", {})
        return {
            "active_leases": len(leases),
            "ips_in_cooldown": sum(1 for k, v in usage.items() if v),
            "co_risk_ips": len(co_risk),
            "state_file": str(_STATE_FILE),
            "cooldown_seconds": IP_COOLDOWN_SECONDS,
            "success_cooldown_seconds": IP_SUCCESS_COOLDOWN_SECONDS,
            "max_accounts_per_ip": MAX_ACCOUNTS_PER_IP,
        }
