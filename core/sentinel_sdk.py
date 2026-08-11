# -*- coding: utf-8 -*-
"""
Sentinel SDK 版本自动发现与本地缓存

参考 napnow/sleep-reg protocol/sentinel_vm.py 的 _ensure_sdk 思路（MIT）：
1. GET {SENTINEL_SDK_BOOTSTRAP_URL}，从返回的 JS 源码里正则提取当前版本号
   https://chatgpt.com/sentinel/<version>/sdk.js
2. 按版本缓存到本地 sentinel/sdk-<version>.js，OpenAI 升级 SDK 后自动换新
3. 发现/下载失败时回退到项目自带 sentinel/sdk.js + 配置 SENTINEL_SV，
   保证流程不因升级探测失败而中断

所有调用点（runner --sdk / --script-src、frame.html?sv=、p[5] script_src 指纹）
应统一走 current_sentinel_sv() / ensure_sentinel_sdk()，避免版本不一致。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from curl_cffi import requests as curl_requests

from config import (
    SENTINEL_SV,
    SENTINEL_SDK_AUTO_UPDATE,
    SENTINEL_SDK_BOOTSTRAP_URL,
    SENTINEL_SDK_CDN_HOSTS,
    SENTINEL_SDK_TTL_SECONDS,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VENDOR_SDK = _PROJECT_ROOT / "sentinel" / "sdk.js"
_DEFAULT_CACHE_DIR = _PROJECT_ROOT / "sentinel"
MAX_SDK_BYTES = 4 * 1024 * 1024
_BOOTSTRAP_RE = re.compile(r"https://chatgpt\.com/sentinel/([A-Za-z0-9_-]+)/sdk\.js")

_lock = threading.Lock()
_discovered_version: str = ""
_discovered_at: float = 0.0
# version -> (sdk_path, script_src)
_sdk_cache: dict[str, Tuple[Path, str]] = {}


def _cache_dir() -> Path:
    override = os.environ.get("SENTINEL_SDK_CACHE_DIR", "").strip()
    if override:
        return Path(override)
    return _DEFAULT_CACHE_DIR


def script_src_for_version(version: str) -> str:
    """构造与真实前端一致的 SDK script src（sentinel frame 从 chatgpt.com 加载）。"""
    return f"{SENTINEL_SDK_CDN_HOSTS[0]}/sentinel/{version}/sdk.js"


def clear_cache() -> None:
    """清空内存缓存（主要用于测试与热重载）。"""
    global _discovered_version, _discovered_at
    with _lock:
        _discovered_version = ""
        _discovered_at = 0.0
        _sdk_cache.clear()


def current_sentinel_sv(timeout: float = 10.0, session=None) -> str:
    """返回当前应使用的 Sentinel SDK 版本号（带 TTL 缓存，失败回退 SENTINEL_SV）。

    session 可选：传入 BrowserSession 时复用其代理探测版本，保证出口一致。
    """
    global _discovered_version, _discovered_at
    if not SENTINEL_SDK_AUTO_UPDATE:
        return SENTINEL_SV
    with _lock:
        if _discovered_version and (time.time() - _discovered_at) < SENTINEL_SDK_TTL_SECONDS:
            return _discovered_version
    proxies = None
    if session is not None:
        inner = getattr(session, "session", session)
        proxies = getattr(inner, "proxies", None)
    try:
        resp = curl_requests.get(SENTINEL_SDK_BOOTSTRAP_URL, timeout=timeout, impersonate="chrome", proxies=proxies)
        if getattr(resp, "status_code", 0) == 200:
            match = _BOOTSTRAP_RE.search(str(getattr(resp, "text", "") or ""))
            if match:
                version = match.group(1)
                with _lock:
                    _discovered_version = version
                    _discovered_at = time.time()
                logger.info(f"[SentinelSDK] 自动发现版本 {version}")
                return version
    except Exception as exc:
        logger.warning(f"[SentinelSDK] 版本发现失败，回退 SENTINEL_SV={SENTINEL_SV}: {exc}")
    # 失败也短暂缓存，避免每次调用都重试网络
    with _lock:
        if not _discovered_version:
            _discovered_version = SENTINEL_SV
            _discovered_at = time.time()
    return SENTINEL_SV


def ensure_sentinel_sdk(session=None, timeout: float = 15.0) -> Tuple[Path, str, str]:
    """
    确保当前版本的 SDK 本地可用，返回 (sdk_path, version, script_src)。

    - 版本来自 current_sentinel_sv()（自动发现 / SENTINEL_SV 回退）
    - 按版本缓存到 sentinel/sdk-<version>.js，命中直接复用
    - 下载失败时回退项目自带 sentinel/sdk.js（与 SENTINEL_SV 配套）

    session 可选：传入 BrowserSession 时用其代理下载，保证出口一致。
    """
    version = current_sentinel_sv(session=session)
    with _lock:
        cached = _sdk_cache.get(version)
    if cached:
        return cached[0], version, cached[1]

    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"sdk-{version}.js"
    if target.is_file() and 0 < target.stat().st_size <= MAX_SDK_BYTES:
        result = (target, script_src_for_version(version))
        with _lock:
            _sdk_cache[version] = result
        return result[0], version, result[1]

    # 版本与项目自带 sdk.js 一致时直接复用，避免每次启动都无谓下载
    if (
        version == SENTINEL_SV
        and _VENDOR_SDK.is_file()
        and 0 < _VENDOR_SDK.stat().st_size <= MAX_SDK_BYTES
    ):
        result = (_VENDOR_SDK, script_src_for_version(version))
        with _lock:
            _sdk_cache[version] = result
        return result[0], version, result[1]

    proxies = None
    if session is not None:
        inner = getattr(session, "session", session)
        proxies = getattr(inner, "proxies", None)

    for host in SENTINEL_SDK_CDN_HOSTS:
        url = f"{host}/sentinel/{version}/sdk.js"
        try:
            resp = curl_requests.get(url, timeout=timeout, impersonate="chrome", proxies=proxies)
            content = getattr(resp, "content", None) or b""
            if getattr(resp, "status_code", 0) == 200 and 0 < len(content) <= MAX_SDK_BYTES:
                tmp = target.with_suffix(".tmp")
                tmp.write_bytes(content)
                tmp.replace(target)
                result = (target, script_src_for_version(version))
                with _lock:
                    _sdk_cache[version] = result
                logger.info(f"[SentinelSDK] 已下载 sdk.js v{version} -> {target.name} ({len(content)} bytes)")
                return result[0], version, result[1]
        except Exception as exc:
            logger.warning(f"[SentinelSDK] 下载 {url} 失败: {exc}")
            continue

    # 回退：项目自带 sdk.js（与 SENTINEL_SV 配套）
    if not _VENDOR_SDK.is_file():
        raise FileNotFoundError(f"缺少本地 sdk.js: {_VENDOR_SDK}")
    fallback_src = script_src_for_version(SENTINEL_SV)
    with _lock:
        _sdk_cache[SENTINEL_SV] = (_VENDOR_SDK, fallback_src)
    logger.warning(
        f"[SentinelSDK] 下载 v{version} 失败，回退本地 sdk.js + SENTINEL_SV={SENTINEL_SV}"
    )
    return _VENDOR_SDK, SENTINEL_SV, fallback_src
