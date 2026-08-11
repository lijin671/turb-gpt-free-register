# -*- coding: utf-8 -*-
"""
iCloud 邮箱池客户端（教程 8.8：iCloud 邮箱注册 GPT 存活率更高，约 ¥0.2/个）。

邮箱池导入格式（每行）：
    email====password

    password 为 iCloud 账户的 App 专用密码（用于 IMAP 登录），不是网页登录密码。
    获取路径：appleid.apple.com → 登录与安全 → App 专用密码 → 生成。

取码：imap.mail.me.com:993（SSL）按收件地址过滤 + otp_utils 提取 OpenAI 验证码。

池状态：
    - 领取时写入 used 集合（sidecar 文件持久化），避免多 worker 重复领取
    - release(status="available") 释放回池（注册失败）；否则标记已用（注册成功）
"""
from __future__ import annotations

import imaplib
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import email as email_lib
import time

from config import email as _email_cfg
from core.otp_utils import extract_otp, looks_like_openai_email, rejected_otp_codes
from core.qqmail_client import (
    _decode_email_header,
    _get_msg_text,
    _msg_to_dict,
    _parse_email_date,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class IcloudMailError(RuntimeError):
    """iCloud 邮箱服务异常。"""


@dataclass
class IcloudAccount:
    email: str
    password: str

    @property
    def mail(self) -> str:
        return self.email


def _pool_file() -> Path:
    return _PROJECT_ROOT / str(getattr(_email_cfg, "ICLOUD_ACCOUNTS_FILE", "用于注册的icloud邮箱.txt"))


def _used_file() -> Path:
    return _PROJECT_ROOT / (str(getattr(_email_cfg, "ICLOUD_ACCOUNTS_FILE", "用于注册的icloud邮箱.txt")) + ".used")


def _load_pool() -> list[IcloudAccount]:
    path = _pool_file()
    if not path.exists():
        return []
    accounts = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("====")
        if len(parts) < 2:
            continue
        email = parts[0].strip()
        password = parts[1].strip()
        if email and password:
            accounts.append(IcloudAccount(email=email, password=password))
    return accounts


def _load_used() -> set[str]:
    path = _used_file()
    if not path.exists():
        return set()
    return {ln.strip().lower() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()}


def _save_used(used: set[str]) -> None:
    try:
        _used_file().write_text("\n".join(sorted(used)) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning("[iCloud] 写入 used 集合失败: %s", exc)


def _mark_used(email: str) -> None:
    used = _load_used()
    used.add(email.lower())
    _save_used(used)


def _unmark_used(email: str) -> None:
    used = _load_used()
    used.discard(email.lower())
    _save_used(used)


def pick_account() -> IcloudAccount:
    """从 iCloud 邮箱池随机领取一个未使用账号（立即标记 used 防重复领取）。"""
    accounts = _load_pool()
    if not accounts:
        raise IcloudMailError(
            f"iCloud 邮箱池为空，请把 email====password 写入 {_pool_file().name}"
        )
    used = _load_used()
    candidates = [a for a in accounts if a.email.lower() not in used]
    if not candidates:
        raise IcloudMailError("iCloud 邮箱池全部已使用，请补充新账号")
    acc = random.choice(candidates)
    _mark_used(acc.email)
    logger.info("[iCloud] 领取邮箱: %s", acc.email)
    return acc


def get_account_context(email: str) -> IcloudAccount | None:
    """按邮箱地址找回账号（供取码 / 释放用）。"""
    target = str(email or "").lower()
    for acc in _load_pool():
        if acc.email.lower() == target:
            return acc
    return None


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    """释放 iCloud 账号：status=available 回池复用；否则保持已用。"""
    if str(status or "").lower() == "available":
        _unmark_used(email)
        logger.info("[iCloud] 释放回池: %s", email)
    else:
        _mark_used(email)
        logger.info("[iCloud] 标记已用: %s (status=%s)", email, status)


def _connect_imap(acc: IcloudAccount) -> imaplib.IMAP4_SSL:
    server = str(getattr(_email_cfg, "ICLOUD_IMAP_SERVER", "imap.mail.me.com") or "imap.mail.me.com")
    port = int(getattr(_email_cfg, "ICLOUD_IMAP_PORT", 993) or 993)
    try:
        mail = imaplib.IMAP4_SSL(server, port)
        mail.login(acc.email, acc.password)
        mail.select("INBOX")
        return mail
    except imaplib.IMAP4.error as exc:
        raise IcloudMailError(f"iCloud IMAP 登录失败 ({acc.email}): {exc}")
    except Exception as exc:
        raise IcloudMailError(f"iCloud IMAP 连接失败: {exc}")


def _search_messages(mail: imaplib.IMAP4_SSL, after_dt: datetime | None = None) -> list[dict]:
    search_criteria = "ALL"
    if after_dt is not None:
        search_criteria = f'(SINCE {after_dt.strftime("%d-%b-%Y")})'
    status, msg_ids = mail.search(None, search_criteria)
    if status != "OK":
        logger.warning("[iCloud] IMAP search 失败: %s", status)
        return []
    ids = msg_ids[0].split() if msg_ids[0] else []
    if not ids:
        return []
    recent_ids = ids[-15:]
    messages = []
    for mid in recent_ids:
        status, data = mail.fetch(mid, "(RFC822)")
        if status != "OK":
            continue
        raw_email = data[0][1]
        try:
            msg = email_lib.message_from_bytes(raw_email)
            messages.append(_msg_to_dict(msg))
        except Exception as exc:
            logger.debug("[iCloud] 解析邮件 %s 失败: %s", mid, exc)
            continue
    return messages


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """通过 iCloud IMAP 轮询取 OTP（按收件地址过滤 + settle 防旧码）。"""
    acc = get_account_context(email)
    if acc is None:
        raise IcloudMailError(f"iCloud 池中找不到账号: {email}")
    if not after_ts:
        after_ts = time.time()
    deadline = time.time() + (max_wait or _email_cfg.OTP_MAX_WAIT)
    interval = poll_interval or _email_cfg.OTP_POLL_INTERVAL
    settle = settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS
    after_dt = datetime.fromtimestamp(after_ts - 30, tz=timezone.utc)
    target_lower = email.lower()

    logger.info(
        "[iCloud] 开始轮询 %s 收件箱，最长 %ss, settle=%ss...",
        email, max_wait or _email_cfg.OTP_MAX_WAIT, settle,
    )

    best_otp: str | None = None
    best_ts: float = 0.0
    best_subject: str = ""
    settle_until: float | None = None

    while time.time() < deadline:
        mail = None
        try:
            mail = _connect_imap(acc)
            messages = _search_messages(mail, after_dt=after_dt)
        except IcloudMailError as exc:
            logger.warning("[iCloud] IMAP 连接失败: %s", exc)
            messages = []
        finally:
            if mail:
                try:
                    mail.logout()
                except Exception:
                    pass

        messages.sort(key=lambda m: m.get("date") or "", reverse=True)
        for item in messages:
            if not looks_like_openai_email(item):
                continue
            to_field = (item.get("to") or "").lower()
            if target_lower not in to_field:
                continue
            subject = item.get("subject") or ""
            otp = extract_otp(item, exclude_codes=rejected_otp_codes(email))
            if not otp:
                continue
            ts = 0.0
            raw_ts = item.get("date") or item.get("receivedDateTime") or ""
            if raw_ts:
                try:
                    ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00")).timestamp()
                except Exception:
                    ts = 0.0
            if after_ts and ts < after_ts - 30:
                continue
            if ts > best_ts:
                if best_otp:
                    logger.info("[iCloud] 发现更晚 OTP=%s (ts=%s)，替换 %s", otp, raw_ts, best_otp)
                else:
                    logger.info("[iCloud] 首次锁定 OTP=%s, ts=%s, 等 %ss settle...", otp, raw_ts, settle)
                best_otp = otp
                best_ts = ts
                best_subject = subject
                settle_until = time.time() + settle
            break

        now = time.time()
        if best_otp and settle_until is not None and now >= settle_until:
            logger.info("[iCloud] settle 完成，返回 OTP=%s, subject=%r", best_otp, best_subject)
            return best_otp
        time.sleep(interval)

    if best_otp:
        logger.info("[iCloud] 超时但已有候选 OTP，返回 %s", best_otp)
        return best_otp
    raise IcloudMailError(f"等待 iCloud OTP 超时（{max_wait or _email_cfg.OTP_MAX_WAIT}s）: {email}")
