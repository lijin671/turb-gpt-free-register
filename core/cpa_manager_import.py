# -*- coding: utf-8 -*-
"""CPA Manager Plus（127.0.0.1:18317）认证文件导入。

背景：CPA Manager Plus 管理面板的 /v0/management/auth-files 支持上传
"CPA 认证 JSON"（每行一个 {"type":"codex","access_token":"..."}）。
注册成功的账号 access_token 寿命极短（~30 分钟内吊销），必须在注册后
趁热导入。本模块同时服务：
  - main.py 阶段 8.6：单账号即时导入（注册成功当场写入）
  - tools/import_cpa_manager.py：批量扫描账号目录导入

已验证端点（2026-08-05 实测）:
  POST /v0/management/auth-files   (FormData file, .json) -> {"status":"ok"}
  GET  /v0/management/auth-files/models?name=...         -> 模型发现
  GET  /v0/management/auth-files                         -> {"files":[...]}
  DELETE /v0/management/auth-files?name=...              -> {"status":"ok"}
  认证方式: Authorization: Bearer <cpamp_... admin key>
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from curl_cffi import CurlMime
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover
    CurlMime = None
    curl_requests = None


# ---------------------------------------------------------------- 工具函数

def sanitize_email(email: str) -> str:
    """邮箱 → 安全文件名（CPA auth-files 的 name 会被写进磁盘路径）。"""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", email.strip())
    return safe or "unknown"


def _co_risk_emails() -> set[str]:
    """从 DB 读取全部 ip_co_risk=True 账号邮箱（小写）。

    失败时按空集处理（不阻断导入链路），仅记录 warning。
    """
    try:
        from core import db
        rows = db.list_accounts(limit=100000)
        return {(r.get("email") or "").lower() for r in rows if r.get("ip_co_risk")}
    except Exception as exc:
        logger.warning("[CPA] 读取连坐风险账号失败（按无风险处理）: %s", exc)
        return set()


def filter_co_risk_accounts(accounts: list[dict]) -> tuple[list[dict], list[dict]]:
    """按 DB 连坐标记 + 账号 dict 自带 ip_co_risk 过滤。

    Returns:
        (clean, skipped)：clean 为可导入账号；skipped 为连坐风险账号。
    """
    risk_emails = _co_risk_emails()
    clean: list[dict] = []
    skipped: list[dict] = []
    for a in accounts:
        email = (a.get("email") or "").lower()
        if a.get("ip_co_risk") or email in risk_emails:
            skipped.append(a)
        else:
            clean.append(a)
    return clean, skipped


def build_auth_file(accounts: list[dict]) -> str:
    """生成 CPA 认证 JSON 文件内容（每行一个 codex 账号）。

    防御性跳过 dict 自带 ip_co_risk=True 的账号（DB 层过滤见 filter_co_risk_accounts）。
    """
    lines = []
    for a in accounts:
        if a.get("ip_co_risk"):
            continue
        token = str(a.get("access_token") or "").strip()
        if not token:
            continue
        entry = {"type": "codex", "access_token": token}
        lines.append(json.dumps(entry, ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")


def scan_account_dirs(base: Path, name_filter: str = "") -> list[dict]:
    """扫描 accounts/ 下所有注册成功账号（注册成功账号.json / 注册成功的token.txt）。"""
    accounts: list[dict] = []
    seen: set[str] = set()
    if not base.exists():
        return accounts
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        json_file = d / "注册成功账号.json"
        tok_file = d / "注册成功的token.txt"
        email_file = d / "注册成功的邮箱.txt"
        if json_file.exists():
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                entries = data if isinstance(data, list) else [data]
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    email = str(e.get("email") or "")
                    token = str(e.get("access_token") or e.get("accessToken") or "")
                    if email and token and email not in seen:
                        seen.add(email)
                        accounts.append({"email": email, "access_token": token, "source": str(d.name)})
            except Exception as exc:
                logger.warning("解析 %s 失败: %s", json_file, exc)
        elif tok_file.exists() and email_file.exists():
            emails = [l.strip() for l in email_file.read_text(encoding="utf-8").splitlines() if l.strip()]
            tokens = [l.strip() for l in tok_file.read_text(encoding="utf-8").splitlines() if l.strip()]
            for email, token in zip(emails, tokens):
                if email and token and email not in seen:
                    seen.add(email)
                    accounts.append({"email": email, "access_token": token, "source": str(d.name)})
    if name_filter:
        accounts = [a for a in accounts if name_filter.lower() in a["source"].lower() or name_filter.lower() in a["email"].lower()]
    return accounts


# ---------------------------------------------------------------- API 客户端

def _multipart_file(name: str, content: bytes, content_type: str = "application/json"):
    """curl_cffi 0.15+ 的 multipart 上传：files= 已废弃，必须用 CurlMime。"""
    if CurlMime is None:
        raise RuntimeError("需要 curl_cffi：pip install curl_cffi")
    mime = CurlMime()
    mime.addpart(name="file", filename=name, content_type=content_type, data=content)
    return mime


def api(base: str, key: str) -> dict:
    """构造 CPA Manager Plus 管理接口客户端（upload/list/models/delete）。"""

    def _headers():
        return {"Authorization": f"Bearer {key}"}

    def upload(name: str, content: str) -> dict:
        if curl_requests is None:
            raise RuntimeError("需要 curl_cffi：pip install curl_cffi")
        mime = _multipart_file(name, content.encode("utf-8"))
        try:
            r = curl_requests.post(
                f"{base}/v0/management/auth-files",
                headers=_headers(),
                multipart=mime,
                timeout=60,
            )
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text[:300]}
            return {"ok": r.status_code in (200, 201), "status": r.status_code, "data": data}
        finally:
            mime.close()

    def list_files() -> list[dict]:
        if curl_requests is None:
            return []
        r = curl_requests.get(f"{base}/v0/management/auth-files", headers=_headers(), timeout=30)
        data = r.json() if r.text else {}
        return data.get("files", []) if r.status_code == 200 else []

    def models(name: str) -> dict:
        import urllib.parse
        if curl_requests is None:
            return {}
        r = curl_requests.get(
            f"{base}/v0/management/auth-files/models?name={urllib.parse.quote(name)}",
            headers=_headers(),
            timeout=30,
        )
        return r.json() if r.text else {}

    def delete(name: str) -> dict:
        import urllib.parse
        if curl_requests is None:
            return {}
        r = curl_requests.delete(
            f"{base}/v0/management/auth-files?name={urllib.parse.quote(name)}",
            headers=_headers(),
            timeout=30,
        )
        return r.json() if r.text else {}

    return {"upload": upload, "list": list_files, "models": models, "delete": delete}


# ---------------------------------------------------------------- 业务入口

def import_single_account(
    email: str,
    access_token: str,
    base: str,
    key: str,
    name: str = "",
    verify: bool = True,
) -> dict:
    """注册后即时导入单个账号（趁 token 未吊销写入 CPA）。

    Args:
        email: 注册邮箱（用于生成默认文件名）
        access_token: 刚注册拿到的 access_token
        base: CPA Manager Plus 地址，如 http://127.0.0.1:18317
        key: 管理密钥（cpamp_...）
        name: 上传文件名；默认 chatgpt-YYYYMMDD-{safe_email}.json
        verify: 上传后调 models 验证

    Returns:
        {"ok": bool, "name": str, "status": int, "data": dict, "message": str}
    """
    token = str(access_token or "").strip()
    if not email or not token:
        return {"ok": False, "name": name, "status": 0, "data": {}, "message": "email/access_token 为空"}
    if not key:
        return {"ok": False, "name": name, "status": 0, "data": {}, "message": "未配置 CPA_MANAGER_PLUS_KEY"}

    # 连坐风险隔离：DB 已标记 ip_co_risk 的账号拒绝导入 CPA 生产池
    if email.lower() in _co_risk_emails():
        return {
            "ok": False, "name": name, "status": 0, "data": {},
            "message": "账号已标记连坐风险，拒绝导入",
        }

    name = name or f"chatgpt-{datetime.now():%Y%m%d}-{sanitize_email(email)}.json"
    content = build_auth_file([{"email": email, "access_token": token}])
    client = api(base.rstrip("/"), key)

    result = client["upload"](name, content)
    if not result["ok"]:
        return {
            "ok": False, "name": name, "status": result["status"],
            "data": result["data"], "message": f"上传失败 HTTP {result['status']}",
        }

    model_count = 0
    if verify:
        try:
            m = client["models"](name)
            model_count = len(m.get("models", []))
        except Exception as exc:
            logger.warning("[CPA] 模型验证失败: %s", exc)

    return {
        "ok": True, "name": name, "status": result["status"],
        "data": result["data"], "message": f"已导入 {email} → {name}（模型 {model_count}）",
        "model_count": model_count,
    }


def import_batch(
    accounts: list[dict],
    base: str,
    key: str,
    name: str = "",
    verify: bool = True,
    delete_dup: bool = True,
) -> dict:
    """批量导入（工具入口）：全部账号合并为一个 auth 文件。

    同名文件已存在时先删除（避免合并残留），再上传并验证模型发现。
    """
    clean, skipped = filter_co_risk_accounts(accounts)
    content = build_auth_file(clean)
    if not content.strip():
        if skipped:
            return {
                "ok": False, "name": name, "message": "全部账号均为连坐风险，已隔离，无账号可导入",
                "skipped_count": len(skipped),
            }
        return {"ok": False, "name": name, "message": "没有可导出的 access_token"}
    if not key:
        return {"ok": False, "name": name, "message": "缺少管理密钥 CPA_MANAGER_PLUS_KEY"}

    name = name or f"chatgpt-{datetime.now():%Y%m%d-%H%M%S}.json"
    client = api(base.rstrip("/"), key)

    if delete_dup:
        for f in client["list"]():
            if f.get("name") == name:
                logger.info("删除同名旧文件 %s ...", name)
                client["delete"](name)

    logger.info("上传 %s（%d 账号，%.1f KB；隔离 %d 连坐风险）...", name, len(clean), len(content) / 1024, len(skipped))
    result = client["upload"](name, content)
    if not result["ok"]:
        return {
            "ok": False, "name": name, "status": result["status"],
            "data": result["data"], "message": f"上传失败 HTTP {result['status']}",
        }

    model_count = 0
    if verify:
        try:
            m = client["models"](name)
            model_count = len(m.get("models", []))
            logger.info("模型发现: %d 个模型", model_count)
        except Exception as exc:
            logger.warning("[CPA] 模型验证失败: %s", exc)

    files = client["list"]()
    logger.info("当前 auth-files 共 %d 个", len(files))
    extra = f"（隔离 {len(skipped)} 连坐风险）" if skipped else ""
    return {
        "ok": True, "name": name, "status": result["status"],
        "data": result["data"], "message": f"已导入 {len(clean)} 账号 → {name}{extra}",
        "model_count": model_count,
        "total_files": len(files),
        "imported_count": len(clean),
        "skipped_count": len(skipped),
    }
