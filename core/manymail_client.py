# -*- coding: utf-8 -*-
"""ManyMail 自建邮箱客户端（DuckMail 兼容 REST：/domains /accounts /token /messages）。

定位：邮箱池 **保底** 来源。仅当 EMAIL_SOURCE 中排在它前面的来源全部领取失败时，
才会走到 manymail。一般用于 GPT 注册兜底，不抢高优先级 outlook/generic_api 等。
"""
from __future__ import annotations

import logging
import secrets
import string
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from config import email as _email_cfg
from core.otp_utils import extract_otp, looks_like_openai_email, rejected_otp_codes

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20


class ManyMailError(RuntimeError):
    """ManyMail 服务请求或邮箱取码失败。"""


@dataclass
class ManyMailAccount:
    email: str
    password: str
    token: str
    domain: str


_CONTEXT_CACHE: dict[str, ManyMailAccount] = {}
_DOMAIN_CACHE: tuple[float, list[str]] | None = None
DOMAIN_CACHE_TTL = 300


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _base_url(value: str | None = None) -> str:
    base = str(
        value if value is not None else getattr(_email_cfg, "MANYMAIL_API_BASE", "") or ""
    ).strip().rstrip("/")
    if not base:
        raise ManyMailError(
            "ManyMail API 地址未配置，请填写 MANYMAIL_API_BASE（例如 http://100.64.229.45:8080）。"
        )
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    return base


def _request(method: str, path: str, *, token: str | None = None, json_body: dict | None = None) -> requests.Response:
    url = _base_url() + (path if path.startswith("/") else "/" + path)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        return requests.request(
            method,
            url,
            headers=headers,
            json=json_body,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ManyMailError(f"ManyMail 请求失败 ({method} {path}): {type(exc).__name__}: {exc}") from exc


def _random_local(length: int | None = None) -> str:
    n = int(length if length is not None else getattr(_email_cfg, "MANYMAIL_RANDOM_LOCAL_LENGTH", 12) or 12)
    n = max(6, min(32, n))
    alphabet = string.ascii_lowercase + string.digits
    # 避免纯数字开头被部分站点嫌弃
    first = secrets.choice(string.ascii_lowercase)
    rest = "".join(secrets.choice(alphabet) for _ in range(n - 1))
    return first + rest


def _random_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def list_domains(force: bool = False) -> list[str]:
    """从 ManyMail /domains 拉取可用域名（带短缓存）。"""
    global _DOMAIN_CACHE
    now = time.time()
    if not force and _DOMAIN_CACHE and now - _DOMAIN_CACHE[0] < DOMAIN_CACHE_TTL:
        return list(_DOMAIN_CACHE[1])

    configured = getattr(_email_cfg, "MANYMAIL_DOMAINS", None) or []
    # 兼容 .env 两种写法：单行逗号分隔 / 多行（list_str_multiline）
    if isinstance(configured, str):
        configured = [configured]
    flattened: list[str] = []
    for item in configured:
        for x in str(item).replace(";", ",").split(","):
            x = x.strip()
            if x:
                flattened.append(x)
    fixed = [str(x).strip().lower() for x in flattened if str(x).strip()]
    if fixed:
        _DOMAIN_CACHE = (now, fixed)
        return list(fixed)

    resp = _request("GET", "/domains")
    if resp.status_code != 200:
        raise ManyMailError(f"ManyMail /domains 失败: HTTP {resp.status_code}; {resp.text[:160]}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise ManyMailError("ManyMail /domains 响应不是 JSON") from exc

    members = payload.get("hydra:member") if isinstance(payload, dict) else None
    domains: list[str] = []
    if isinstance(members, list):
        for item in members:
            if not isinstance(item, dict):
                continue
            d = str(item.get("domain") or "").strip().lower()
            if d and item.get("isActive", True) is not False:
                domains.append(d)
    if not domains:
        raise ManyMailError(f"ManyMail 未返回可用域名: {str(payload)[:200]}")
    _DOMAIN_CACHE = (now, domains)
    return list(domains)


def pick_account() -> ManyMailAccount:
    """创建并缓存一个 ManyMail 邮箱（POST /accounts + /token）。"""
    domains = list_domains()
    if not domains:
        raise ManyMailError("ManyMail 无可用域名")
    last_exc: Exception | None = None
    for attempt in range(8):
        domain = domains[attempt % len(domains)]
        local = _random_local()
        address = f"{local}@{domain}"
        password = _random_password()
        try:
            create = _request("POST", "/accounts", json_body={"address": address, "password": password})
            if create.status_code not in (200, 201):
                # 地址冲突则换一个
                if create.status_code == 422 and "already" in create.text.lower():
                    continue
                raise ManyMailError(f"ManyMail 创建邮箱失败: HTTP {create.status_code}; {create.text[:200]}")

            tok_resp = _request("POST", "/token", json_body={"address": address, "password": password})
            if tok_resp.status_code != 200:
                raise ManyMailError(f"ManyMail 登录取 token 失败: HTTP {tok_resp.status_code}; {tok_resp.text[:200]}")
            try:
                tok_payload = tok_resp.json()
            except ValueError as exc:
                raise ManyMailError("ManyMail /token 响应不是 JSON") from exc
            token = str((tok_payload or {}).get("token") or "").strip()
            if not token:
                raise ManyMailError(f"ManyMail /token 缺少 token: {str(tok_payload)[:160]}")

            account = ManyMailAccount(email=address, password=password, token=token, domain=domain)
            _CONTEXT_CACHE[_cache_key(address)] = account
            logger.info("[ManyMail] 已生成保底邮箱: %s (domain=%s)", address, domain)
            return account
        except ManyMailError as exc:
            last_exc = exc
            continue
    raise ManyMailError(f"ManyMail 创建邮箱多次失败: last={last_exc}")


def restore_context(email: str, password: str, token: str = "", domain: str = "") -> ManyMailAccount:
    """从持久化凭据重建进程内上下文（供独立进程查活/取码使用）。"""
    acc = ManyMailAccount(
        email=str(email or "").strip(),
        password=str(password or ""),
        token=str(token or ""),
        domain=str(domain or ""),
    )
    _CONTEXT_CACHE[_cache_key(acc.email)] = acc
    logger.info("[ManyMail] 已从持久化凭据恢复保底邮箱: %s", acc.email)
    return acc


def get_account_context(email: str) -> ManyMailAccount | None:
    return _CONTEXT_CACHE.get(_cache_key(email))


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    _CONTEXT_CACHE.pop(_cache_key(email), None)
    logger.info("[ManyMail] 已释放保底邮箱: %s（status=%s, note=%s）", email, status, note or "")


def _ensure_token(account: ManyMailAccount) -> str:
    if account.token:
        return account.token
    tok_resp = _request("POST", "/token", json_body={"address": account.email, "password": account.password})
    if tok_resp.status_code != 200:
        raise ManyMailError(f"ManyMail 刷新 token 失败: HTTP {tok_resp.status_code}; {tok_resp.text[:160]}")
    token = str((tok_resp.json() or {}).get("token") or "").strip()
    if not token:
        raise ManyMailError("ManyMail 刷新 token 为空")
    account.token = token
    return token


def _parse_ts(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _otp_item(msg: dict) -> dict:
    from_field = msg.get("from") or {}
    if isinstance(from_field, dict):
        from_s = from_field.get("address") or from_field.get("name") or ""
    else:
        from_s = str(from_field)
    return {
        "id": msg.get("id") or msg.get("msgid"),
        "from": from_s,
        "subject": msg.get("subject") or "",
        "text": msg.get("text") or msg.get("intro") or "",
        "html": msg.get("html") or "",
        "createdAt": msg.get("createdAt") or msg.get("created_at"),
    }


def _list_messages(token: str) -> list[dict]:
    resp = _request("GET", "/messages?limit=30", token=token)
    if resp.status_code == 401:
        raise ManyMailError("ManyMail token 失效 (401)")
    if resp.status_code != 200:
        raise ManyMailError(f"ManyMail /messages 失败: HTTP {resp.status_code}; {resp.text[:160]}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise ManyMailError("ManyMail /messages 不是 JSON") from exc
    members = payload.get("hydra:member") if isinstance(payload, dict) else None
    return members if isinstance(members, list) else []


def _get_message(token: str, message_id: str) -> dict:
    resp = _request("GET", f"/messages/{message_id}", token=token)
    if resp.status_code != 200:
        raise ManyMailError(f"ManyMail 消息详情失败: HTTP {resp.status_code}; {resp.text[:160]}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise ManyMailError("ManyMail 消息详情不是 JSON") from exc
    return payload if isinstance(payload, dict) else {}


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """轮询 ManyMail 收件箱，返回领取时间后最新 OpenAI 六位验证码。"""
    target = str(email or "").strip()
    if not target:
        raise ManyMailError("ManyMail 取码缺少邮箱地址")

    account = get_account_context(target)
    if account is None:
        raise ManyMailError(f"ManyMail 进程内无该邮箱上下文: {target}（需由 pick_account 创建）")

    wait_seconds = int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT)
    interval = max(1, int(poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL))
    settle = max(0, int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS))
    deadline = time.monotonic() + max(0, wait_seconds)
    best_otp: str | None = None
    best_timestamp = float("-inf")
    settle_until: float | None = None
    last_error = "收件箱为空或尚未出现新的 OpenAI 验证码"

    logger.info("[ManyMail] 开始轮询保底邮箱 %s，最长 %ss", target, wait_seconds)
    while time.monotonic() <= deadline:
        try:
            token = _ensure_token(account)
            try:
                messages = _list_messages(token)
            except ManyMailError as exc:
                if "401" in str(exc):
                    account.token = ""
                    token = _ensure_token(account)
                    messages = _list_messages(token)
                else:
                    raise

            sortable = sorted(
                [m for m in messages if isinstance(m, dict)],
                key=lambda item: _parse_ts(item.get("createdAt") or item.get("created_at")) or float("-inf"),
                reverse=True,
            )
            for summary in sortable:
                message_time = _parse_ts(summary.get("createdAt") or summary.get("created_at"))
                if after_ts is not None and message_time is not None and message_time < after_ts - 30:
                    continue

                summary_item = _otp_item(summary)
                # 列表可能只有 intro；先粗筛，详情再精筛
                if not looks_like_openai_email(summary_item) and not looks_like_openai_email(
                    {**summary_item, "text": summary.get("intro") or ""}
                ):
                    # 仍拉详情（OpenAI 主题字段可能不全）
                    pass

                message_id = str(summary.get("id") or "").strip()
                if not message_id and isinstance(summary.get("@id"), str):
                    message_id = summary["@id"].rstrip("/").split("/")[-1]
                if not message_id:
                    continue

                detail = _get_message(token, message_id)
                detail_item = _otp_item(detail)
                if not looks_like_openai_email(detail_item):
                    continue
                otp = extract_otp(detail_item, exclude_codes=rejected_otp_codes(email))
                if not otp:
                    continue

                candidate_time = _parse_ts(detail.get("createdAt") or detail.get("created_at"))
                candidate_time = message_time if candidate_time is None else candidate_time
                candidate_time = float("-inf") if candidate_time is None else candidate_time
                is_newer_message = candidate_time > best_timestamp
                is_updated_code = candidate_time == best_timestamp and otp != best_otp
                if best_otp is None or is_newer_message or is_updated_code:
                    best_otp = otp
                    best_timestamp = candidate_time
                    settle_until = time.monotonic() + settle
                    logger.info("[ManyMail] 锁定 OTP 候选，等待 %ss 确认", settle)

            now = time.monotonic()
            if best_otp and settle_until is not None and now >= settle_until:
                return best_otp
        except ManyMailError as exc:
            last_error = str(exc)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    if best_otp:
        return best_otp
    raise ManyMailError(f"等待 ManyMail 验证码超时: {target}; {last_error}")
