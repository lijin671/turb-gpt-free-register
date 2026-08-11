# -*- coding: utf-8 -*-
"""零元 Plus 绑卡后台任务服务（WebUI 用，避免同步阻塞）。

用法：
  1. enqueue_bind(account_id, ...) 入队并立即返回 task_id（后台线程执行 run_zero_plus）
  2. list_tasks() / get_task(task_id) 轮询状态
  3. 同一账号同时只允许一个 pending/running 任务

纯进程内注册表（与 core.manual_otp 同级），WebUI 重启后任务丢失属预期。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_TASKS: dict[str, dict] = {}
_MAX_TASKS = 200


def _new_task(account_id: int, email: str, params: dict) -> dict:
    return {
        "task_id": uuid.uuid4().hex[:12],
        "account_id": int(account_id),
        "email": email or "",
        "status": "pending",  # pending / running / done / error
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "error": "",
        "params": dict(params),
    }


def enqueue_bind(
    account_id: int,
    *,
    access_token: str,
    email: str = "",
    proxy: str = "",
    device_id: str = "",
    card_number: str = "",
    exp_month: str = "",
    exp_year: str = "",
    cvc: str = "",
    card_name: str = "CHATGPT USER",
) -> dict:
    """入队一个零元 Plus 绑卡任务，立即返回；后台线程异步执行。

    Returns:
        {accepted: True, task_id, status} 或
        {accepted: False, busy: True, task_id: 已存在任务}
    """
    account_id = int(account_id)
    params = {
        "access_token": str(access_token or "").strip(),
        "account_id": str(account_id),
        "email": email or "",
        "proxy": proxy or "",
        "device_id": device_id or "",
        "card_number": card_number or "",
        "exp_month": exp_month or "",
        "exp_year": exp_year or "",
        "cvc": cvc or "",
        "card_name": card_name or "CHATGPT USER",
    }
    with _lock:
        for t in _TASKS.values():
            if t["account_id"] == account_id and t["status"] in ("pending", "running"):
                return {"accepted": False, "busy": True, "task_id": t["task_id"]}
        task = _new_task(account_id, params["email"], params)
        _TASKS[task["task_id"]] = task
        keys = list(_TASKS.keys())
        if len(keys) > _MAX_TASKS:
            for k in keys[: len(keys) - _MAX_TASKS]:
                _TASKS.pop(k, None)
    thread = threading.Thread(target=_run, args=(task["task_id"],), daemon=True)
    thread.start()
    logger.info("[PlusBind] 已入队任务 %s (account=%s)", task["task_id"], account_id)
    return {"accepted": True, "task_id": task["task_id"], "status": "pending"}


def _run(task_id: str) -> None:
    """后台线程入口：执行 run_zero_plus 并回写结果。"""
    from core.plus_zero import run_zero_plus

    with _lock:
        task = _TASKS.get(task_id)
        if task is None:
            return
        task["status"] = "running"
        params = dict(task.get("params") or {})
    try:
        result = run_zero_plus(**params)
        with _lock:
            task = _TASKS.get(task_id)
            if task is not None:
                task["status"] = "done"
                task["result"] = result
                task["finished_at"] = time.time()
    except Exception as exc:
        logger.warning("[PlusBind] 任务 %s 失败: %s", task_id, exc)
        with _lock:
            task = _TASKS.get(task_id)
            if task is not None:
                task["status"] = "error"
                task["error"] = f"{type(exc).__name__}: {exc}"
                task["finished_at"] = time.time()


def get_task(task_id: str) -> dict | None:
    with _lock:
        t = _TASKS.get(str(task_id or ""))
        if t is None:
            return None
        out = dict(t)
        out.pop("params", None)
        return out


def list_tasks(limit: int = 50) -> list[dict]:
    with _lock:
        rows = sorted(_TASKS.values(), key=lambda x: float(x.get("started_at") or 0), reverse=True)
        out = []
        for t in rows[: max(1, int(limit))]:
            item = dict(t)
            item.pop("params", None)
            out.append(item)
        return out


def clear_finished() -> int:
    """清理已结束（done/error）任务，返回清理数量。"""
    with _lock:
        keys = [k for k, t in _TASKS.items() if t.get("status") in ("done", "error")]
        for k in keys:
            _TASKS.pop(k, None)
        return len(keys)
