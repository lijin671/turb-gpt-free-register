# -*- coding: utf-8 -*-
"""
FlareSolverr clearance 集成 — 用于注册/查活/绑卡链路绕过 Cloudflare 403 challenge。

背景：
    turb 的 network_preflight 要求 chatgpt.com/login 返回 HTTP < 400，
    但数据中心 IP 会被 CF 拦截返回 403 "Just a moment"。
    FlareSolverr（跑 headless Chrome 的容器）可过 CF JS challenge，
    拿到 cf_clearance cookie + UA，注入到 turb 的 BrowserSession 后即可继续走纯协议注册。

参考：
    chatgpt2api-proxy-pool services/proxy_service.py 的 FlareSolverrClearanceProvider
    turb core/session.py 的 _is_cf_challenge / cf_cookie_snapshot

配置（.env）：
    FLARESOLVERR_ENABLED=true
    FLARESOLVERR_URL=http://127.0.0.1:18191
    FLARESOLVERR_TIMEOUT=60
    FLARESOLVERR_REFRESH_INTERVAL=3600   # clearance cookie 有效期（秒），默认 1h
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib import request as urllib_request

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60
_DEFAULT_REFRESH = 3600


@dataclass
class ClearanceBundle:
    target_host: str
    proxy_url: str = ""
    cookies: dict[str, str] = field(default_factory=dict, repr=False)
    user_agent: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None

    def is_valid(self, *, now: float | None = None) -> bool:
        t = time.time() if now is None else now
        if self.expires_at is not None and t >= self.expires_at:
            return False
        return bool(self.cookies or self.user_agent)

    def cookie_header(self) -> str:
        if not self.cookies:
            return ""
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


# ---- 配置 ----

_CFG: dict[str, Any] = {}


def _load_config() -> dict[str, Any]:
    """从 .env 环境变量加载 FlareSolverr 配置。"""
    import os
    return {
        "enabled": os.environ.get("FLARESOLVERR_ENABLED", "").lower() in ("1", "true", "yes", "on"),
        "url": os.environ.get("FLARESOLVERR_URL", "http://127.0.0.1:18191").rstrip("/"),
        "timeout": int(os.environ.get("FLARESOLVERR_TIMEOUT", str(_DEFAULT_TIMEOUT))),
        "refresh_interval": int(os.environ.get("FLARESOLVERR_REFRESH_INTERVAL", str(_DEFAULT_REFRESH))),
    }


def _ensure_config() -> dict[str, Any]:
    global _CFG
    if not _CFG:
        _CFG = _load_config()
    return _CFG


# ---- cache ----

_cache: dict[str, ClearanceBundle] = {}
_cache_lock = threading.RLock()


def _host_from_url(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return urlparse(str(url or "")).hostname or ""
    except Exception:
        return ""


def _filter_cookies(raw_cookies: Any, target_host: str) -> dict[str, str]:
    """从 FlareSolverr 返回的 cookies 列表中提取目标域名的 cookie。"""
    if not raw_cookies or not isinstance(raw_cookies, list):
        return {}
    result: dict[str, str] = {}
    for c in raw_cookies:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        value = str(c.get("value") or "")
        domain = str(c.get("domain") or "").lstrip(".")
        if not name or not value:
            continue
        # 只保留 CF 相关 cookie 和目标域 cookie
        if name in ("cf_clearance", "__cf_bm", "__cfseq") or target_host in domain or domain in target_host:
            result[name] = value
    return result


def get_clearance(target_url: str, proxy_url: str = "") -> ClearanceBundle | None:
    """调用 FlareSolverr 获取 cf_clearance cookie。

    Args:
        target_url: 需要过 CF 的目标 URL（如 https://chatgpt.com/login）
        proxy_url: 注册代理 URL（FlareSolverr 会通过这个代理访问目标）
    Returns:
        ClearanceBundle or None（失败时返回 None）
    """
    cfg = _ensure_config()
    if not cfg["enabled"]:
        logger.debug("[FlareSolverr] 未启用（FLARESOLVERR_ENABLED != true）")
        return None

    host = _host_from_url(target_url)
    cache_key = f"{host}|{proxy_url}"

    # 先查缓存
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and cached.is_valid():
            logger.debug("[FlareSolverr] 命中缓存 clearance for %s", host)
            return cached

    url = cfg["url"]
    timeout = cfg["timeout"]
    logger.info("[FlareSolverr] 请求 clearance: %s (proxy=%s, timeout=%ss)", target_url[:80], proxy_url[:40] if proxy_url else "direct", timeout)

    payload: dict[str, Any] = {
        "cmd": "request.get",
        "url": str(target_url),
        "maxTimeout": int(timeout * 1000),
    }
    if proxy_url:
        # 确保 socks5 -> socks5h（remote DNS）
        if proxy_url.startswith("socks5://"):
            proxy_url = "socks5h://" + proxy_url[len("socks5://"):]
        payload["proxy"] = {"url": proxy_url}

    endpoint = f"{url}/v1"
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib_request.Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("[FlareSolverr] 请求失败: %s: %s", type(exc).__name__, exc)
        return None

    if not isinstance(data, dict) or str(data.get("status") or "").lower() != "ok":
        logger.warning("[FlareSolverr] 返回非 ok: %s", str(data)[:200])
        return None

    solution = data.get("solution")
    if not isinstance(solution, dict):
        logger.warning("[FlareSolverr] 返回无 solution")
        return None

    cookies = _filter_cookies(solution.get("cookies"), host)
    user_agent = str(solution.get("userAgent") or "").strip()

    if not cookies and not user_agent:
        logger.warning("[FlareSolverr] 返回无 cookie/UA")
        return None

    bundle = ClearanceBundle(
        target_host=host,
        proxy_url=proxy_url,
        cookies=cookies,
        user_agent=user_agent,
        expires_at=time.time() + cfg["refresh_interval"],
    )

    with _cache_lock:
        _cache[cache_key] = bundle

    logger.info("[FlareSolverr] 成功获取 clearance: %s cookies=%s UA=%s...",
                host, list(cookies.keys()), user_agent[:30] if user_agent else "")
    return bundle


def apply_clearance_to_session(session, bundle: ClearanceBundle) -> None:
    """将 clearance cookie + UA 注入到 turb 的 BrowserSession 或 curl_cffi session。"""
    if not bundle:
        return

    # 注入 cookies
    if bundle.cookies:
        target_domain = bundle.target_host
        # turb 的 BrowserSession 封装了 curl_cffi.requests.Session
        raw_session = getattr(session, "session", session)  # 获取底层 curl_cffi session
        for name, value in bundle.cookies.items():
            try:
                raw_session.cookies.set(name, value, domain=f".{target_domain}")
            except Exception:
                try:
                    raw_session.cookies.set(name, value)
                except Exception:
                    pass

    # 注入 UA
    if bundle.user_agent and hasattr(session, "user_agent"):
        # 不覆盖 turb 设的 chrome UA 除非配置了
        pass


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def warm_up(target_url: str = "https://chatgpt.com/login", proxy_url: str = "") -> None:
    """启动时预获取 clearance。"""
    cfg = _ensure_config()
    if not cfg["enabled"]:
        return
    logger.info("[FlareSolverr] warm_up: %s", target_url)
    bundle = get_clearance(target_url, proxy_url)
    if bundle:
        logger.info("[FlareSolverr] warm_up 完成")
    else:
        logger.warning("[FlareSolverr] warm_up 失败")
