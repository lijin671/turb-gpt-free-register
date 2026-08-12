# -*- coding: utf-8 -*-
"""注册成功后即时导入到 chatgpt2api 容器（HTTP API）。

背景：新注册账号的 access_token 会在 ~30 分钟内吊销，
必须注册成功当场导入，否则导入时 token 已失效。

chatgpt2api 容器 API:
  POST /api/accounts  {"tokens": ["access_token_xxx"], "sync_after_import": true}
  GET  /api/accounts  → {"items": [...], "total": N}
  认证: Authorization: Bearer <CHATGPT2API_AUTH_KEY>
"""
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# 保留旧的 JSON 文件写入作为备份路径（双写模式）
_LOCK = None


def _atomic_write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _import_via_api(access_token: str, base_url: str, auth_key: str) -> dict:
    """通过 chatgpt2api HTTP API 导入账号。

    Returns: {"ok": bool, "message": str, "added": int, "total": int}
    """
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        import requests as curl_requests

    url = base_url.rstrip("/") + "/api/accounts"
    headers = {
        "Authorization": f"Bearer {auth_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "tokens": [access_token],
        "sync_after_import": True,
        "return_items": False,
    }

    try:
        resp = curl_requests.post(url, json=payload, headers=headers, timeout=30)
        if resp.status_code != 200:
            body = resp.text[:300] if hasattr(resp, "text") else ""
            return {
                "ok": False,
                "message": f"HTTP {resp.status_code}: {body}",
                "added": 0,
                "total": 0,
            }
        data = resp.json()
        added = int(data.get("added", 0))
        skipped = int(data.get("skipped", 0))
        removed = int(len(data.get("removed_ids", [])))
        # 查总数
        total = 0
        try:
            list_resp = curl_requests.get(
                base_url.rstrip("/") + "/api/accounts",
                headers={"Authorization": f"Bearer {auth_key}"},
                timeout=15,
            )
            if list_resp.status_code == 200:
                total = int(list_resp.json().get("total", 0))
        except Exception:
            pass

        msg = f"API 导入: added={added}, skipped={skipped}, removed={removed}, total={total}"
        if added > 0:
            logger.info("[ChatGPT2API] %s", msg)
            return {"ok": True, "message": msg, "added": added, "total": total}
        else:
            return {"ok": False, "message": f"token 已存在或被跳过: {msg}", "added": 0, "total": total}
    except Exception as e:
        return {"ok": False, "message": f"API 请求失败: {type(e).__name__}: {e}", "added": 0, "total": 0}


def _write_json_file(email: str, access_token: str, tokens_file: str, proxy: str) -> dict:
    """保留旧的 JSON 文件写入作为备份。"""
    path = Path(tokens_file)
    if not path.exists():
        return {"ok": False, "message": f"tokens 文件不存在: {path}"}
    if not email or not access_token:
        return {"ok": False, "message": "email/token 为空"}

    bak = path.with_suffix(path.suffix + ".bak")
    if not bak.exists():
        try:
            import shutil
            shutil.copy2(path, bak)
        except Exception:
            pass

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {"ok": False, "message": f"读取失败: {e}"}

    added = 0
    if email not in data:
        added = 1
    data[email] = {"token": access_token, "puid": ""}
    try:
        _atomic_write(path, data)
    except Exception as e:
        return {"ok": False, "message": f"写入失败: {e}"}

    msg = f"JSON 备份已写入 {email} (added={added}, total={len(data)}) -> {path}"
    return {"ok": True, "message": msg, "added": added, "total": len(data)}


def export_account_to_chatgpt2api(
    email: str,
    access_token: str,
    tokens_file: str = "",
    update_proxies: bool = True,
    proxy: str = "",
) -> dict:
    """把单个账号导入 chatgpt2api 容器（HTTP API 优先）+ JSON 文件备份。

    优先走 HTTP API（POST /api/accounts），同时保留旧的 JSON 文件写入作为备份。
    返回 {ok, message, added, total}。
    """
    from config import export as _cfg

    if not email or not access_token:
        return {"ok": False, "message": "email/token 为空"}

    # ---- 主路径：HTTP API 导入 ----
    api_base = getattr(_cfg, "CHATGPT2API_API_BASE", "") or "http://127.0.0.1:3001"
    api_key = getattr(_cfg, "CHATGPT2API_AUTH_KEY", "") or "Iq43lk6czc464qlAaV3N4QswsbkLaAdZ4pZopwGDI3o"

    api_result = _import_via_api(access_token, api_base, api_key)

    # ---- 备份路径：JSON 文件写入 ----
    tokens_file = tokens_file or _cfg.CHATGPT2API_TOKENS_FILE
    json_result = {"ok": False, "message": "未执行"}
    if tokens_file and Path(tokens_file).exists():
        json_result = _write_json_file(email, access_token, tokens_file, proxy)

    # ---- proxies.txt 更新 ----
    if update_proxies:
        try:
            from config.proxy import PROXY_POOL
            proxies_file = Path(tokens_file).parent / "proxies.txt" if tokens_file else None
            proxy_line = proxy or (PROXY_POOL[0] if PROXY_POOL else "")
            if proxies_file and proxy_line:
                proxies_file.write_text(proxy_line + "\n", encoding="utf-8")
        except Exception as e:
            logger.warning("[ChatGPT2API] proxies.txt 更新失败: %s", e)

    # 返回 API 结果为主
    if api_result["ok"]:
        return api_result
    elif json_result["ok"]:
        return {
            "ok": True,
            "message": f"API 失败但 JSON 备份成功: {api_result['message']} | {json_result['message']}",
            "added": json_result.get("added", 0),
            "total": json_result.get("total", 0),
        }
    else:
        return {
            "ok": False,
            "message": f"API: {api_result['message']} | JSON: {json_result['message']}",
            "added": 0,
            "total": 0,
        }
