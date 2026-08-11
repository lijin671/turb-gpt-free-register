# -*- coding: utf-8 -*-
"""注册成功后即时导出到 ChatGPT-to-API（access_tokens.json + proxies.txt）。

背景：新注册账号的 access_token 会被 OpenAI 在 ~30 分钟内吊销，
必须注册成功当场写入，否则导入时 token 已失效。
"""
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK = None


def _atomic_write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def export_account_to_chatgpt2api(
    email: str,
    access_token: str,
    tokens_file: str = "",
    update_proxies: bool = True,
    proxy: str = "",
) -> dict:
    """把单个账号写入 access_tokens.json；返回 {ok, message, added, total}。"""
    from config import export as _cfg

    tokens_file = tokens_file or _cfg.CHATGPT2API_TOKENS_FILE
    path = Path(tokens_file)
    if not path.exists():
        return {"ok": False, "message": f"tokens 文件不存在: {path}"}
    if not email or not access_token:
        return {"ok": False, "message": "email/token 为空"}

    # 备份一次（防止误写覆盖历史）
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

    msg = f"已写入 {email} (added={added}, total={len(data)}) -> {path}"

    if update_proxies:
        try:
            from config.proxy import PROXY_POOL
            proxies_file = path.parent / "proxies.txt"
            proxy_line = proxy or PROXY_POOL or ""
            if proxy_line:
                proxies_file.write_text(proxy_line + "\n", encoding="utf-8")
                msg += f"; proxies.txt 已更新"
        except Exception as e:
            logger.warning("[ChatGPT2API] proxies.txt 更新失败: %s", e)

    logger.info("[ChatGPT2API] %s", msg)
    return {"ok": True, "message": msg, "added": added, "total": len(data)}
