# -*- coding: utf-8 -*-
"""
HeroSMS 虚拟号码客户端（sms-activate.ru 兼容协议）。

API 文档: https://hero-sms.com/cn/api
Base URL: https://hero-sms.com/stubs/handler_api.php
全部为 GET 请求，参数经 query string 传递，`api_key` 必带。

常用 action:
  getBalance            余额
  getNumberV2           购买号码（V2，失败自动回退 getNumber）
  getStatus / getStatusV2  查询激活状态（含验证码）
  setStatus             设置状态 1=重发 3=再次索取 6=完成 8=取消
  getAllSms             取该激活号全部短信（含 code 字段）
  getCountries          国家列表
  getServicesList       服务列表（lang=cn）
  getOperators          运营商（按国家）
  getPrices             价格表（country + service）
  cancelActivation / finishActivation  取消 / 完成
  reactivate / reactivationPrice       重激活
  getActiveActivations / getHistory    当前激活 / 历史

注意（社区实测 2026-08-07）:
  - 菲律宾 GCash 号源只有 HeroSMS 可稳定接码；
  - 同一出口 IP 约 10 分钟只能接 2 次码，批量时需轮换出口 IP（proxy 参数）。

用法:
  from core.hero_sms import HeroSMSClient
  c = HeroSMSClient(api_key="...", proxy="http://user:pass@host:port")
  act = c.get_number(service="gcash", country=6)
  code = c.wait_for_code(act.id, timeout=180)
  c.complete(act.id)
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://hero-sms.com/stubs/handler_api.php"

# sms-activate 兼容状态码
STATUS_SMS_SENT = 1      # 已收到短信（要求重发）
STATUS_REQUEST_CODE = 3  # 再次索取验证码
STATUS_COMPLETE = 6      # 激活完成（码已用）
STATUS_CANCEL = 8        # 取消激活，释放号码

# 国家: 菲律宾（HeroSMS 实测 2026-08-07：国家表 id=4；仍以 getCountries 返回为准）
COUNTRY_PHILIPPINES = 4

# 通用服务码（以 getServicesList 返回为准；HeroSMS 实测 2026-08-07：GCash 服务码 = "bc"）
SERVICE_GCASH = "bc"
SERVICE_CHATGPT = "openai"   # 若需用 HeroSMS 接 ChatGPT 码时常见码


class HeroSMSError(RuntimeError):
    """HeroSMS API 错误基类。"""


class NoNumbersAvailableError(HeroSMSError):
    """无可用号码（NO_NUMBERS）。"""


class InsufficientFundsError(HeroSMSError):
    """余额不足（NO_BALANCE）。"""


class BadServiceError(HeroSMSError):
    """服务码不存在（BAD_SERVICE）。"""


class BadStatusError(HeroSMSError):
    """setStatus 状态非法（BAD_STATUS）。"""


class InvalidApiKeyError(HeroSMSError):
    """API Key 无效（BAD_KEY）。"""


class ActivationNotFoundError(HeroSMSError):
    """激活不存在（NO_ACTIVATION）。"""


class CodeTimeoutError(HeroSMSError):
    """等待验证码超时。"""


@dataclass
class Activation:
    id: int
    phone: str
    cost: float = 0.0
    service: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def phone_number(self) -> str:
        return self.phone

    def __repr__(self) -> str:  # pragma: no cover
        return f"Activation(id={self.id}, phone={self.phone}, cost={self.cost})"


@dataclass
class SmsItem:
    id: str
    phone_from: str
    code: str | None
    text: str | None
    service: str
    date: str
    type: str

    @classmethod
    def from_payload(cls, item: dict) -> "SmsItem":
        return cls(
            id=str(item.get("id", "")),
            phone_from=str(item.get("phoneFrom", item.get("phone_from", ""))),
            code=item.get("code"),
            text=item.get("text"),
            service=str(item.get("service", "")),
            date=str(item.get("date", "")),
            type=str(item.get("type", "sms")),
        )


class HeroSMSClient:
    """同步 HeroSMS 客户端（sms-activate.ru 兼容）。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        proxy: str = "",
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.proxy = proxy
        self.session = session or requests.Session()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    # ── 底层请求 ──────────────────────────────────────────────────────────
    def _request(self, action: str, **params: Any) -> Any:
        if not self.api_key:
            raise InvalidApiKeyError("HERO_SMS_API_KEY 未配置")
        query = {"api_key": self.api_key, "action": action}
        query.update({k: v for k, v in params.items() if v is not None})
        url = f"{self.base_url}?{urlencode(query)}"
        logger.debug("HeroSMS GET action=%s params=%s", action, {k: ("***" if k == "api_key" else v) for k, v in query.items()})
        try:
            resp = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise HeroSMSError(f"HeroSMS 请求失败 action={action}: {exc}") from exc
        if resp.status_code != 200:
            raise HeroSMSError(f"HeroSMS HTTP {resp.status_code} action={action}: {resp.text[:200]}")
        return self._parse(action, resp.text)

    @staticmethod
    def _parse(action: str, text: str) -> Any:
        """解析 sms-activate 兼容响应（纯文本 ACCESS_* 或 JSON 双格式）。"""
        stripped = text.strip()
        if not stripped:
            raise HeroSMSError(f"HeroSMS 空响应 action={action}")
        # JSON 优先
        if stripped.startswith(("{", "[")):
            try:
                import json
                return json.loads(stripped)
            except Exception:
                pass
        _ERROR_WITHOUT_COLON = {
            "NO_NUMBERS": NoNumbersAvailableError,
            "NO_BALANCE": InsufficientFundsError,
            "BAD_SERVICE": BadServiceError,
            "BAD_STATUS": BadStatusError,
            "BAD_KEY": InvalidApiKeyError,
            "NO_ACTIVATION": ActivationNotFoundError,
            "BAD_ACTION": HeroSMSError,
        }
        upper = stripped.upper()
        for _key, _err in _ERROR_WITHOUT_COLON.items():
            if upper.startswith(_key):
                raise _err(stripped)
        # 无冒号的状态文本（有冒号时走下面的冒号分支）：
        # STATUS_WAIT / STATUS_CANCEL / STATUS_OK（无码）
        if ":" not in stripped and (
            upper.startswith("STATUS_WAIT")
            or upper.startswith("STATUS_CANCEL")
            or upper.startswith("STATUS_OK")
        ):
            return ""
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            if key.startswith("ACCESS_BALANCE"):
                return value
            if key.startswith("ACCESS_NUMBER"):
                # ACCESS_NUMBER:<id>:<phone>
                parts = value.split(":")
                if len(parts) >= 2:
                    return {"id": int(parts[0]), "phone": parts[1]}
                return {"id": int(parts[0]), "phone": ""}
            if key.startswith("ACCESS_ACTIVATION"):
                return value
            if key.startswith("ACCESS_CANCEL"):
                return True
            if key.startswith("ACCESS_RETRY_GET"):
                return True
            if key.startswith("ACCESS_READY"):
                return True
            if key.startswith("STATUS_OK"):
                return value
            if key.startswith("STATUS_WAIT"):
                return ""
            if key.startswith("STATUS_CANCEL"):
                return ""
            if key.startswith("NO_NUMBERS"):
                raise NoNumbersAvailableError(stripped)
            if key.startswith("NO_BALANCE"):
                raise InsufficientFundsError(stripped)
            if key.startswith("BAD_SERVICE"):
                raise BadServiceError(stripped)
            if key.startswith("BAD_STATUS"):
                raise BadStatusError(stripped)
            if key.startswith("BAD_KEY"):
                raise InvalidApiKeyError(stripped)
            if key.startswith("NO_ACTIVATION"):
                raise ActivationNotFoundError(stripped)
            if key.startswith("BAD_ACTION"):
                raise HeroSMSError(f"HeroSMS 未知 action: {stripped}")
        return stripped

    # ── 账户/参考数据 ─────────────────────────────────────────────────────
    def get_balance(self) -> float:
        val = self._request("getBalance")
        if isinstance(val, dict):
            if "amount" in val:
                try:
                    return float(val["amount"])
                except (TypeError, ValueError):
                    raise HeroSMSError(f"getBalance 响应异常: {val!r}") from None
            raise HeroSMSError(f"getBalance 响应异常: {val!r}")
        try:
            return float(val)
        except (TypeError, ValueError):
            raise HeroSMSError(f"getBalance 响应异常: {val!r}") from None

    def get_countries(self) -> dict:
        return self._request("getCountries") or {}

    def get_services(self, lang: str = "cn") -> dict:
        return self._request("getServicesList", lang=lang) or {}

    def get_operators(self, country: int) -> dict:
        return self._request("getOperators", country=country) or {}

    def get_prices(self, country: int, service: str = "", currency: int = 840) -> Any:
        return self._request("getPrices", country=country, service=service or None, currency=currency)

    def find_country_id(self, name_hint: str = "philippines") -> int | None:
        """从 getCountries 里按英文/中文名模糊找国家 id。"""
        countries = self.get_countries()
        items = countries.get("countries", countries) if isinstance(countries, dict) else countries
        if isinstance(items, dict):
            items = items.values()
        for c in items or []:
            if not isinstance(c, dict):
                continue
            hay = " ".join(str(c.get(k, "")) for k in ("eng", "rus", "chn", "name")).lower()
            if name_hint.lower() in hay:
                return int(c.get("id", 0))
        return None

    def find_service_code(self, name_hint: str = "gcash") -> str | None:
        """从 getServicesList 里按名字模糊找服务码。"""
        services = self.get_services()
        items = services.get("services", services) if isinstance(services, dict) else services
        if isinstance(items, dict):
            items = items.values()
        for s in items or []:
            if not isinstance(s, dict):
                continue
            hay = " ".join(str(s.get(k, "")) for k in ("code", "name", "eng", "cn", "rus")).lower()
            if name_hint.lower() in hay:
                return str(s.get("code", ""))
        return None

    # ── 购买号码 ──────────────────────────────────────────────────────────
    def get_number(
        self,
        service: str = SERVICE_GCASH,
        country: int = COUNTRY_PHILIPPINES,
        operator: str | None = None,
        max_price: float | None = None,
        fixed_price: bool = False,
        phone_exception: str | None = None,
        ref: str | None = None,
    ) -> Activation:
        params = dict(
            service=service, country=country, operator=operator,
            maxPrice=max_price, fixedPrice="1" if fixed_price else None,
            phoneException=phone_exception, ref=ref,
        )
        try:
            data = self._request("getNumberV2", **params)
        except (BadServiceError, HeroSMSError):
            data = self._request("getNumber", **params)
        if isinstance(data, dict) and "id" in data:
            return Activation(
                id=int(data["id"]),
                phone=str(data.get("phone", data.get("number", ""))),
                cost=float(data.get("cost", data.get("activationCost", 0)) or 0),
                service=service,
                raw=data,
            )
        if isinstance(data, (list, dict)):
            # JSON 模式可能返回 {activationId, phoneNumber, activationCost}
            cand = data[0] if isinstance(data, list) and data else data
            if isinstance(cand, dict):
                aid = cand.get("activationId", cand.get("id"))
                if aid is not None:
                    return Activation(
                        id=int(aid),
                        phone=str(cand.get("phoneNumber", cand.get("phone", ""))),
                        cost=float(cand.get("activationCost", cand.get("cost", 0)) or 0),
                        service=service, raw=cand,
                    )
        raise HeroSMSError(f"getNumber 响应无法解析: {data!r}")

    # ── 状态/验证码 ───────────────────────────────────────────────────────
    def get_status(self, activation_id: int) -> str:
        """返回 'STATUS_OK:<code>' 的 code 或 ''（等待中/取消）。"""
        val = self._request("getStatus", id=activation_id)
        if isinstance(val, dict):
            status = str(val.get("status", ""))
            if "OK" in status.upper():
                return str(val.get("code", ""))
            return ""
        return str(val or "")

    def get_status_v2(self, activation_id: int) -> dict:
        return self._request("getStatusV2", id=activation_id) or {}

    def set_status(self, activation_id: int, status: int) -> bool:
        val = self._request("setStatus", id=activation_id, status=status)
        return val is True or (isinstance(val, str) and val.upper().startswith("ACCESS"))

    def wait_for_code(
        self,
        activation_id: int,
        timeout: float = 180.0,
        poll_interval: float = 5.0,
        prefer_all_sms: bool = True,
    ) -> str:
        """轮询直到拿到验证码；超时抛 HeroSMSError。"""
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                if prefer_all_sms:
                    items = self.get_all_sms(activation_id)
                    if items:
                        for it in items:
                            if it.code:
                                return it.code
                else:
                    code = self.get_status(activation_id)
                    if code:
                        return code
            except Exception as exc:
                logger.debug("wait_for_code id=%s: %s", activation_id, exc)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
        raise CodeTimeoutError(f"等待验证码超时 activation_id={activation_id} (last={last!r})")

    def get_all_sms(self, activation_id: int) -> list[SmsItem]:
        data = self._request("getAllSms", id=activation_id)
        if isinstance(data, dict):
            data = data.get("data", data)
        if isinstance(data, list):
            return [SmsItem.from_payload(i) for i in data if isinstance(i, dict)]
        if isinstance(data, dict) and data:
            return [SmsItem.from_payload(data)]
        return []

    # ── 生命周期 ──────────────────────────────────────────────────────────
    def cancel(self, activation_id: int, wait_early_cancel: bool = True) -> bool:
        """取消激活。

        HeroSMS 规则：号码取出后约 120 秒内不允许取消
        （HTTP 409 EARLY_CANCEL_DENIED，minActivationTime=120）。
        wait_early_cancel=True 时自动等待 minActivationTime 后重试一次。
        """
        try:
            return self.set_status(activation_id, STATUS_CANCEL)
        except HeroSMSError as exc:
            if not wait_early_cancel or "EARLY_CANCEL_DENIED" not in str(exc):
                raise
            m = re.search(r"minActivationTime[^0-9]*(\d+)", str(exc))
            wait = max(0, int(m.group(1)) if m else 120)
            logger.info("EARLY_CANCEL_DENIED：等待 %ss 后重试取消 id=%s", wait, activation_id)
            time.sleep(wait + 1)
            return self.set_status(activation_id, STATUS_CANCEL)

    def complete(self, activation_id: int) -> bool:
        return self.set_status(activation_id, STATUS_COMPLETE)

    def request_again(self, activation_id: int) -> bool:
        return self.set_status(activation_id, STATUS_REQUEST_CODE)

    def reactivation_price(self, activation_id: int) -> float:
        val = self._request("reactivationPrice", id=activation_id)
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0

    def reactivate(self, activation_id: int) -> Activation:
        data = self._request("reactivate", id=activation_id)
        if isinstance(data, dict) and data.get("id") is not None:
            return Activation(int(data["id"]), str(data.get("phone", "")), service="")
        raise HeroSMSError(f"reactivate 响应异常: {data!r}")


def extract_code_from_text(text: str) -> str | None:
    """从短信文本里提取 4-8 位数字验证码（getAllSms 的 text 兜底）。"""
    m = re.search(r"\b(\d{4,8})\b", text or "")
    return m.group(1) if m else None


if __name__ == "__main__":  # pragma: no cover
    import sys
    from config import plus as plus_cfg
    c = HeroSMSClient(api_key=plus_cfg.HERO_SMS_API_KEY, proxy=plus_cfg.HERO_SMS_PROXY)
    print("balance:", c.get_balance())
    ph = c.find_country_id("philippines")
    print("ph country id:", ph)
    g = c.find_service_code("gcash")
    print("gcash service code:", g)
