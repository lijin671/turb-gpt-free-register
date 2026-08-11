# -*- coding: utf-8 -*-
"""
ChatGPT Plus 零元开通模块（日区注册 → 美区提取 Token → 切菲结算 → Stripe 绑卡）。

基于帖子《【篡改猴脚本】零元Plus直卡绑定 懒人专享》和油猴脚本 1.5.0 的
实际 API 调用流程提取为 Python 模块，集成到 turb-gpt-free-register 项目。

流程：
  阶段 0: 前置准备 - 指纹浏览器、JP/US 节点、卡段 BIN 523686/4513
  阶段 1: 日区注册 - 现有注册流程（由外部调用）
  阶段 2: 提取 Token - 从 /api/auth/session 获取 accessToken + accountId
  阶段 3: 切菲结算 - POST /backend-api/payments/checkout 设置 country=PH, currency=PHP
  阶段 4: 创建 SetupIntent - POST /backend-api/payments/payment_method 获取 client_secret
  阶段 5: Stripe 绑卡 - 用 Stripe.js API 执行 confirmCardSetup
  阶段 6: 验证绑定 - GET /backend-api/payments/payment_methods 检查绑定状态
  阶段 7: 改美址支付 - 提交 US 地址触发 0 PHP 账单 → Plus 激活

依赖:
  pip install requests curl_cffi
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════
# 常量（来自油猴脚本 1.5.0）
# ══════════════════════════════════════════════════════════════════════════

ROUTE_CHECKOUT = "/backend-api/payments/checkout"
ROUTE_INTENT   = "/backend-api/payments/payment_method"
ROUTE_METHODS  = "/backend-api/payments/payment_methods"
# 2026-08 前端实测端点（chatgpt.com bundle 挖出）：绑卡成功后的订阅激活链路
ROUTE_CHECKOUT_STATE   = "/backend-api/payments/checkout/{entity}/{checkout_session_id}"
ROUTE_CHECKOUT_UPDATE  = "/backend-api/payments/checkout/update"
ROUTE_CHECKOUT_SNAPSHOT = "/backend-api/payments/checkout/snapshot"
ROUTE_CHECKOUT_CONFIRM = "/backend-api/payments/checkout/confirm"
ROUTE_CHECKOUT_CUSTOM_START = "/backend-api/payments/checkout/custom_payment_method/start"
ROUTE_CHECKOUT_CUSTOM_CONTINUE = "/backend-api/payments/checkout/custom_payment_method/continue"
STRIPE_API_BASE = "https://api.stripe.com/v1"

# 油猴脚本中已知的 Stripe Publishable Key
# 脚本通过动态发现 + 已知 key 兜底
KNOWN_STRIPE_KEYS = [
    "pk_live_51Pj377KslHRdbaPgTJYjThzH3f5dt1N1vK7LUp0qh0yNSarhfZ6nfbG7FFlh8KLxVkvdMWN5o6Mc4Vda6NHaSnaV00C2Sbl8Zs",
    # 完整版（checkout API 返回，旧版被截断导致 401）
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n",
]

# 菲律宾定价切换 payload（来自油猴脚本实际测试通过的 payload）
PH_SWITCH_PAYLOAD_TEMPLATE = {
    "entry_point": "all_plans_pricing_modal",
    "plan_name": "chatgptplusplan",
    "billing_details": {
        "country": "PH",
        "currency": "PHP",
    },
    "promo_campaign": {
        "promo_campaign_id": "plus-1-month-free",
        "is_coupon_from_query_param": False,
    },
    "checkout_ui_mode": "custom",
}

# US 备用地址（油猴绑卡后填写地址用）
BACKUP_US_ADDRESSES = [
    {"street": "221B Baker Street",  "city": "New York",      "state": "NY", "zip": "10001", "country": "US"},
    {"street": "350 Fifth Avenue",   "city": "New York",      "state": "NY", "zip": "10118", "country": "US"},
    {"street": "1600 Amphitheatre Parkway", "city": "Mountain View", "state": "CA", "zip": "94043", "country": "US"},
    {"street": "123 Main Street",    "city": "Los Angeles",   "state": "CA", "zip": "90012", "country": "US"},
    {"street": "456 Oak Avenue",     "city": "Chicago",       "state": "IL", "zip": "60607", "country": "US"},
]

# ══════════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════════

class PlusSession:
    """一个 Plus 开通会话的上下文。"""
    def __init__(self, access_token: str, account_id: str, email: str = "", proxy: str = "",
                 device_id: str = ""):
        self.access_token = access_token
        self.account_id = account_id
        self.email = email
        self.proxy = proxy
        self.device_id = device_id
        self.base_url = "https://chatgpt.com"
        self.checkout_session_id: str | None = None
        self.checkout_url: str | None = None
        self.client_secret: str | None = None
        self.publishable_key: str | None = None
        self.payment_method_id: str | None = None
        self.cards_bound: list[dict] = []
        self.subscription_active: bool = False
        self.steps_log: list[str] = []
        # 订阅激活（checkout confirm）链路状态
        self.checkout_state: dict | None = None
        self.confirm_token: str | None = None
        self.billing_address: dict | None = None
        self.checkout_confirm: dict | None = None
        # OpenAI 国家锁定降级状态：country_locked=True 表示非0元降级结算
        self.country_locked: bool = False
        self.billing_country: str = ""
        self.zero_price: bool = False

    def log(self, msg: str):
        self.steps_log.append(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}")
        logger.info("[PlusZero] %s", msg)


def _result(
    ok: bool = False,
    status: str = "failed",
    message: str = "",
    checkout_url: str | None = None,
    subscription_active: bool = False,
    cards_bound: list | None = None,
    steps_log: list | None = None,
    card_used: str | None = None,
    address_used: dict | None = None,
    client_secret: str | None = None,
) -> dict:
    return {
        "ok": ok,
        "status": status,
        "message": message,
        "checkout_url": checkout_url,
        "subscription_active": subscription_active,
        "cards_bound": cards_bound or [],
        "steps_log": steps_log or [],
        "card_used": card_used,
        "address_used": address_used,
        "client_secret": client_secret,
    }


# ══════════════════════════════════════════════════════════════════════════
# 阶段 2: 提取 Token（/api/auth/session）
# ══════════════════════════════════════════════════════════════════════════

def _jwt_claim_account_id(access_token: str) -> str:
    """从 accessToken JWT payload 提取 chatgpt_account_id（不校验签名，仅取声明）。"""
    try:
        payload_b64 = str(access_token).split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        import base64 as _b64
        payload = _b64.urlsafe_b64decode(payload_b64.encode())
        data = json.loads(payload)
        return str(data.get("https://api.openai.com/auth", {}).get("chatgpt_account_id") or "") or ""
    except Exception:
        return ""


def fetch_session(access_token: str, proxy: str = "", device_id: str = "") -> dict:
    """
    从 ChatGPT 获取会话信息。

    对应油猴脚本 fetchSession():
      GET /api/auth/session
      → { accessToken, account: { id } }

    部分数据中心 IP 上 /api/auth/session 会被门禁（200 + WARNING_BANNER 或缺字段），
    此时降级用 /backend-api/me（实测同 IP 下可用）+ JWT 内 chatgpt_account_id 兜底。
    """
    from core.session import BrowserSession
    session = BrowserSession(proxy=proxy, device_id=device_id or None)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    url = "https://chatgpt.com/api/auth/session"
    logger.info("[阶段2] 提取 session...")
    resp = session.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("accessToken") and data.get("account", {}).get("id"):
        logger.info("[阶段2] session OK, user=%s, accountId=%s",
                    data.get("user", {}).get("email", "?"),
                    data["account"]["id"])
        return data

    # 门禁/WARNING_BANNER → /backend-api/me + JWT account id 兜底
    logger.warning("[阶段2] /api/auth/session 缺必要字段（疑似 WARNING_BANNER 门禁），降级 /backend-api/me...")
    me_resp = session.get("https://chatgpt.com/backend-api/me", headers=headers)
    me_resp.raise_for_status()
    me_data = me_resp.json()
    aid = _jwt_claim_account_id(access_token)
    if not aid:
        raise ValueError(f"session 缺少必要字段且无法从 JWT 提取 account id: {json.dumps(data, ensure_ascii=False)[:200]}")
    fallback = {
        "accessToken": access_token,
        "user": {
            "id": me_data.get("id", ""),
            "email": me_data.get("email", ""),
            "name": me_data.get("name", ""),
        },
        "account": {"id": aid},
    }
    logger.info("[阶段2] session 降级成功, user=%s, accountId=%s", fallback["user"]["email"], aid)
    return fallback



# ── 账号国家锁定降级（2026-08-05 OpenAI 新策略） ──
# 切菲换区返回 400 "Billing country must match request country"，
# billing country 必须匹配账号注册国家。此处映射出口 geo → ISO/货币用于降级结算。

_COUNTRY_CODE_MAP = {
    "HONG KONG": "HK", "SINGAPORE": "SG", "UNITED STATES": "US", "JAPAN": "JP",
    "SOUTH KOREA": "KR", "KOREA": "KR", "PHILIPPINES": "PH", "TAIWAN": "TW",
    "AUSTRALIA": "AU", "CANADA": "CA", "GERMANY": "DE", "UNITED KINGDOM": "GB",
}
_COUNTRY_CURRENCY = {
    "US": "USD", "SG": "SGD", "JP": "JPY", "KR": "KRW", "HK": "HKD", "PH": "PHP",
    "TW": "TWD", "AU": "AUD", "CA": "CAD", "DE": "EUR", "GB": "GBP",
}


# 国家锁定降级候选顺序（detect_account_country 出口 geo 优先，再探测常见注册国）。
# 实测：OpenAI 按 OTP/注册信号判定账号国家，可能与出口 IP geo 不一致
# （出口 US 但账号国 JP），逐个候选探测，用第一个返回 checkout_session_id 的。
_FALLBACK_COUNTRY_ORDER = ["US", "JP", "SG", "HK", "KR", "TW", "GB", "DE"]


def detect_account_country(ps: PlusSession) -> tuple[str, str]:
    """探测账号注册国家（用注册同 IP 的出口 geo），返回 (ISO国家码, 货币)。"""
    from core.session import BrowserSession
    s = BrowserSession(proxy=ps.proxy, device_id=ps.device_id or None)
    geo = s.exit_geo or {}
    cc = str(geo.get("country") or "").upper()
    cc = _COUNTRY_CODE_MAP.get(cc, cc)
    currency = _COUNTRY_CURRENCY.get(cc, "USD")
    return cc, currency


# ── 瞬态错误重试（代理 TLS/连接失败、5xx、CF challenge 时换新代理会话重试） ──

def _is_transient_error(exc) -> bool:
    """判断是否值得换代理重试：网络层/代理层错误，而非业务/鉴权失败。"""
    name = type(exc).__name__
    text = str(exc)
    if "CurlError" in name or "SSLError" in name or "ConnectionError" in name or "Timeout" in name:
        return True
    if "HTTP 5" in text or "502" in text or "503" in text or "504" in text or "cf_chl" in text:
        return True
    if "401" in text or "403" in text or "token" in text.lower() and "invalid" in text.lower():
        return False
    return False


def _plus_request_with_retry(
    ps: PlusSession, method: str, url: str, headers: dict,
    payload: dict | None = None, *, label: str, max_attempts: int = 3,
):
    """执行 Plus 流程 HTTP 请求，瞬态错误时换新代理（新出口 IP）重试。"""
    from core.session import BrowserSession
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            session = BrowserSession(proxy=ps.proxy, device_id=ps.device_id or None)
            if method.upper() == "POST":
                resp = session.post(url, headers=headers, json=payload)
            else:
                resp = session.get(url, headers=headers)
            return resp
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts and _is_transient_error(exc):
                ps.log(f"⚠️ {label} 第 {attempt} 次瞬态失败（{type(exc).__name__}: {str(exc)[:100]}），换代理重试...")
                logger.warning("[PlusZero] %s 瞬态失败，换代理重试 (%s/%s): %s",
                               label, attempt, max_attempts, str(exc)[:120])
                import time as _t
                _t.sleep(2)
            else:
                break
    raise last_exc if last_exc else RuntimeError(f"{label} 未知错误")

# ══════════════════════════════════════════════════════════════════════════
# 阶段 3: 切菲律宾结算（/backend-api/payments/checkout）
# ══════════════════════════════════════════════════════════════════════════

def switch_to_philippines(ps: PlusSession) -> str:
    """
    调用 checkout API 切换到菲律宾结算，返回 checkout_session_id。

    对应油猴脚本 switchToPhilippines():
      POST /backend-api/payments/checkout
      payload: { entry_point, plan_name, billing_details: { country: PH, currency: PHP }, ... }
      → { checkout_session_id }

    返回的短链形如 /checkout/openai_llc/{checkout_session_id}
    """
    headers = {
        "Authorization": f"Bearer {ps.access_token}",
        "Content-Type": "application/json",
    }
    url = f"{ps.base_url}{ROUTE_CHECKOUT}"

    payload = dict(PH_SWITCH_PAYLOAD_TEMPLATE)  # 浅拷贝

    ps.log("阶段3: 切菲律宾结算...")
    logger.info("[阶段3] POST %s  country=PH, currency=PHP", url)

    resp = _plus_request_with_retry(ps, "POST", url, headers, payload, label="切菲律宾结算")
    data = resp.json() if resp.text else {}

    # OpenAI 新策略：billing country 必须匹配账号注册国家 → 降级为账号国家结算
    if resp.status_code == 400 and "Billing country must match request country" in resp.text:
        from config import plus as _plus_cfg
        if getattr(_plus_cfg, "ZERO_PLUS_COUNTRY_LOCK_FALLBACK", True):
            candidates: list[tuple[str, str]] = []
            try:
                cc, currency = detect_account_country(ps)
                if cc:
                    candidates.append((cc, currency))
            except Exception:
                pass
            # 出口 geo 优先，再逐个探测常见注册国（账号国由 OpenAI 判定，可能与 geo 不同）
            for _c in _FALLBACK_COUNTRY_ORDER:
                pair = (_c, _COUNTRY_CURRENCY.get(_c, "USD"))
                if pair not in candidates:
                    candidates.append(pair)
            if candidates:
                last_fail: tuple | None = None
                for cc, currency in candidates:
                    ps.log(f"⚠️ 切菲被 OpenAI 国家锁定，尝试 {cc} 结算（非0元）...")
                    logger.warning("[PlusZero] 切菲 400 国家锁定，尝试 %s/%s", cc, currency)
                    payload2 = dict(payload)
                    payload2["billing_details"] = {"country": cc, "currency": currency}
                    resp2 = _plus_request_with_retry(ps, "POST", url, headers, payload2, label=f"切{cc}(降级)")
                    data2 = resp2.json() if resp2.text else {}
                    if resp2.ok and data2.get("checkout_session_id"):
                        ps.country_locked = True
                        ps.billing_country = cc
                        ps.zero_price = False
                        ps.checkout_session_id = data2["checkout_session_id"]
                        ps.checkout_url = f"/checkout/openai_llc/{data2['checkout_session_id']}"
                        ps.log(f"✅ 降级切{cc}成功，checkout_session_id={data2['checkout_session_id']}")
                        return data2["checkout_session_id"]
                    last_fail = (cc, resp2.status_code, data2)
                detail = last_fail or data.get("detail", data)
                err_msg = f"切菲失败（降级国家均被拒）: {json.dumps(detail, ensure_ascii=False)[:300]}"
                ps.log(f"❌ {err_msg}")
                raise RuntimeError(err_msg)
            ps.log("❌ 切菲被国家锁定且无法探测账号国家")
        detail = data.get("detail", data)
        err_msg = f"切菲失败 HTTP {resp.status_code}: {json.dumps(detail, ensure_ascii=False)[:300]}"
        ps.log(f"❌ {err_msg}")
        raise RuntimeError(err_msg)

    if not resp.ok or not data.get("checkout_session_id"):
        detail = data.get("detail", data)
        err_msg = f"切菲失败 HTTP {resp.status_code}: {json.dumps(detail, ensure_ascii=False)[:300]}"
        ps.log(f"❌ {err_msg}")
        raise RuntimeError(err_msg)

    ps.country_locked = False
    ps.billing_country = "PH"
    ps.zero_price = True

    checkout_id = data["checkout_session_id"]
    ps.checkout_session_id = checkout_id
    ps.checkout_url = f"/checkout/openai_llc/{checkout_id}"

    # 从 checkout 响应提取 publishable_key（API 直接返回，最可靠）
    pk_from_api = data.get("publishable_key") or data.get("publishableKey") or ""
    if pk_from_api and pk_from_api.startswith("pk_live_"):
        ps.publishable_key = pk_from_api
        ps.log(f"✅ 从 checkout API 提取 publishable_key: {pk_from_api[:25]}...")
        logger.info("[阶段3] publishable_key 从 API 提取成功")

    ps.log(f"✅ 切菲成功，checkout_session_id={checkout_id}")
    logger.info("[阶段3] 成功，短链: %s", ps.checkout_url)
    return checkout_id


# ══════════════════════════════════════════════════════════════════════════
# 阶段 4: 创建 Stripe SetupIntent（/backend-api/payments/payment_method）
# ══════════════════════════════════════════════════════════════════════════

def create_setup_intent(ps: PlusSession) -> str:
    """
    创建 Stripe SetupIntent，返回 client_secret。

    对应油猴脚本 bindCard() 中的:
      POST /backend-api/payments/payment_method
      headers: { authorization, chatgpt-account-id }
      body: { account_id }
      → { client_secret }
    """
    headers = {
        "Authorization": f"Bearer {ps.access_token}",
        "chatgpt-account-id": ps.account_id,
        "Content-Type": "application/json",
    }
    url = f"{ps.base_url}{ROUTE_INTENT}"
    payload = {"account_id": ps.account_id}

    ps.log("阶段4: 创建 SetupIntent...")
    logger.info("[阶段4] POST %s", url)

    resp = _plus_request_with_retry(ps, "POST", url, headers, payload, label="创建 SetupIntent")
    data = resp.json() if resp.text else {}

    if not resp.ok or not data.get("client_secret"):
        detail = data.get("detail", data)
        err_msg = f"SetupIntent 创建失败 HTTP {resp.status_code}: {json.dumps(detail, ensure_ascii=False)[:300]}"
        ps.log(f"❌ {err_msg}")
        raise RuntimeError(err_msg)

    ps.client_secret = data["client_secret"]
    ps.log(f"✅ SetupIntent 创建成功，client_secret 前40字={data['client_secret'][:40]}...")
    logger.info("[阶段4] SetupIntent OK")
    return data["client_secret"]


# ══════════════════════════════════════════════════════════════════════════
# 阶段 5: Stripe 绑卡
# ══════════════════════════════════════════════════════════════════════════


def _extract_pk_from_checkout_page(ps: "PlusSession") -> str | None:
    """
    从 ChatGPT checkout 页面中提取 Stripe Publishable Key。

    查找方式（按优先级）：
    1. window.__stripePublishableKey / window.stripePublishableKey
    2. Stripe.js script URL 中的 ?publishableKey= 参数
    3. __NEXT_DATA__ JSON 中的 stripe 配置
    4. 任意 script 中的 pk_live_ 字符串
    """
    from core.session import BrowserSession
    session = BrowserSession(proxy=ps.proxy, device_id=ps.device_id or None)
    headers = {
        "Authorization": f"Bearer {ps.access_token}",
    }
    url = f"{ps.base_url}{ps.checkout_url}"

    logger.info("[StripeKey] 从 checkout 页面提取 publishable_key: %s", url)
    try:
        resp = session.get(url, headers=headers)
        html = resp.text
    except Exception as exc:
        logger.warning("[StripeKey] 页面请求失败: %s", exc)
        return None

    # 方法1: 查找 window.__stripePublishableKey / window.stripePublishableKey
    patterns = [
        r'window\.__stripePublishableKey\s*=\s*["\'](pk_live_[^"\']+)["\']',
        r'window\.stripePublishableKey\s*=\s*["\'](pk_live_[^"\']+)["\']',
        r'["\']stripePublishableKey["\']\s*:\s*["\'](pk_live_[^"\']+)["\']',
        r'["\']publishableKey["\']\s*:\s*["\'](pk_live_[^"\']+)["\']',
        # 直接匹配 pk_live_ 密钥（至少 80 字符）
        r'pk_live_[a-zA-Z0-9]{50,}',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, html)
        for match in matches:
            key = match if isinstance(match, str) else match[0]
            if len(key) >= 80 and key.startswith("pk_live_"):
                logger.info("[StripeKey] 从页面提取成功: %s...", key[:30])
                return key

    # 方法2: 在 script 标签中查找
    for script_match in re.finditer(r'<script[^>]*>([^<]+pk_live_[^<]+)</script>', html, re.IGNORECASE):
        script_content = script_match.group(1)
        pk_match = re.search(r'pk_live_[a-zA-Z0-9]{50,}', script_content)
        if pk_match:
            key = pk_match.group(0)
            if len(key) >= 80:
                logger.info("[StripeKey] 从 script 标签提取成功: %s...", key[:30])
                return key

    logger.warning("[StripeKey] 未从页面提取到 publishable_key，使用已知密钥兜底")
    return None


def _resolve_publishable_key(client_secret: str, ps: "PlusSession | None" = None) -> str:
    """
    获取 Stripe Publishable Key。

    优先级：
    1. ps.publishable_key（从 checkout API 响应提取，最可靠）
    2. 从 client_secret 中的 fragment 匹配已知密钥
    3. 使用第一个已知密钥兜底
    """
    # 优先使用 checkout API 返回的 publishable_key
    if ps is not None and getattr(ps, 'publishable_key', ''):
        pk = ps.publishable_key
        if pk.startswith("pk_live_") and len(pk) >= 100:
            logger.info("[StripeKey] 使用 checkout API 返回的 key: %s...", pk[:25])
            return pk

    # 从 client_secret 中的 fragment 匹配（完整版 key2 现在可用）
    for frag, key in [
        ("KslHRdbaPg", KNOWN_STRIPE_KEYS[0]),
        ("C6h1nxGoI3", KNOWN_STRIPE_KEYS[1]),
    ]:
        if frag in client_secret:
            return key

    # 兜底
    return KNOWN_STRIPE_KEYS[0]


def _stripe_api_request(method: str, path: str, publishable_key: str, data: dict | None = None, proxy: str = "") -> dict:
    """
    直接调用 Stripe API（无需加载 Stripe.js，纯 HTTP）。

    替代油猴脚本中的 stripe.confirmCardSetup()。
    使用 Stripe 的 PaymentIntents API 来确认 SetupIntent。

    Args:
        proxy: 代理 URL（如 http://user:pass@host:port）。空串表示直连。
    """
    import requests as req_lib
    url = f"{STRIPE_API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {publishable_key}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Stripe/v1 PythonBindings",
    }
    kwargs: dict = {"headers": headers, "timeout": 30}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    if method.upper() == "GET":
        resp = req_lib.get(url, params=data, **kwargs)
    else:
        resp = req_lib.post(url, data=data, **kwargs)
    resp.raise_for_status()
    return resp.json()


def bind_card_via_stripe_api(
    ps: PlusSession,
    card_number: str,
    exp_month: str,
    exp_year: str,
    cvc: str,
    card_name: str = "CHATGPT USER",
) -> str:
    """
    通过 Stripe API 直接绑卡，不依赖浏览器/Stripe.js。

    流程（替代油猴脚本的 stripe.confirmCardSetup）：
      1. 用 publishable_key 创建 Stripe PaymentMethod
      2. 用 PaymentMethod 确认 SetupIntent

    Args:
        ps: Plus 会话
        card_number: 卡号
        exp_month: 有效期月 (2位)
        exp_year: 有效期年 (4位)
        cvc: CVV
        card_name: 持卡人姓名

    Returns:
        payment_method_id
    """
    import requests as req_lib

    pk = _resolve_publishable_key(ps.client_secret, ps)
    ps.publishable_key = pk
    ps.log(f"阶段5: 使用 Stripe key={pk[:20]}...")

    # 步骤 5a: 创建 PaymentMethod
    ps.log("阶段5a: 创建 Stripe PaymentMethod...")
    pm_data = {
        "type": "card",
        "card[number]": card_number,
        "card[exp_month]": exp_month,
        "card[exp_year]": exp_year,
        "card[cvc]": cvc,
        "billing_details[name]": card_name,
    }
    pm_resp = _stripe_api_request("POST", "/payment_methods", pk, pm_data, proxy=ps.proxy)
    pm_id = pm_resp.get("id", "")
    if not pm_id:
        raise RuntimeError(f"创建 PaymentMethod 失败: {json.dumps(pm_resp, ensure_ascii=False)[:200]}")
    ps.log(f"✅ PaymentMethod 创建成功: {pm_id}")

    # 步骤 5b: 确认 SetupIntent
    ps.log("阶段5b: 确认 SetupIntent...")
    si_data = {
        "payment_method": pm_id,
        "use_stripe_sdk": "false",
        "key": pk,
    }
    # 从 client_secret 提取 setup_intent id
    si_id = ps.client_secret.split("_secret_")[0]
    si_resp = _stripe_api_request("POST", f"/setup_intents/{si_id}/confirm", pk, si_data, proxy=ps.proxy)

    status = si_resp.get("status", "")
    if status != "succeeded":
        error_msg = si_resp.get("last_setup_error", {}).get("message", status)
        raise RuntimeError(f"SetupIntent 确认失败: {error_msg} (status={status})")

    ps.payment_method_id = pm_id
    ps.log(f"✅ Stripe 绑卡成功，status={status}")
    logger.info("[阶段5] Stripe 绑卡成功, pm_id=%s", pm_id)
    return pm_id


# ══════════════════════════════════════════════════════════════════════════
# 阶段 6: 验证绑定（/backend-api/payments/payment_methods）
# ══════════════════════════════════════════════════════════════════════════

def verify_payment_methods(ps: PlusSession) -> list[dict]:
    """
    验证支付方式是否已绑定成功。

    对应油猴脚本:
      GET /backend-api/payments/payment_methods?account_id={account_id}
      headers: { authorization, chatgpt-account-id }
      → { payment_methods: [...], default_payment_method_id }
    """
    from core.session import BrowserSession
    session = BrowserSession(proxy=ps.proxy, device_id=ps.device_id or None)
    headers = {
        "Authorization": f"Bearer {ps.access_token}",
        "chatgpt-account-id": ps.account_id,
    }
    url = f"{ps.base_url}{ROUTE_METHODS}?account_id={ps.account_id}"

    ps.log("阶段6: 验证支付方式绑定...")
    resp = session.get(url, headers=headers)
    data = resp.json() if resp.text else {}

    methods = data.get("payment_methods", [])
    default_id = data.get("default_payment_method_id", "")

    cards = []
    for m in methods:
        card_info = {
            "id": m.get("id", ""),
            "brand": (m.get("card", {}) or {}).get("brand", m.get("type", "?")),
            "last4": (m.get("card", {}) or {}).get("last4", ""),
            "exp": f"{(m.get('card', {}) or {}).get('exp_month', '??')}/{(m.get('card', {}) or {}).get('exp_year', '??')}",
            "default": m.get("id") == default_id,
        }
        cards.append(card_info)

    ps.cards_bound = cards
    if cards:
        ps.log(f"✅ 支付方式绑定成功，共 {len(cards)} 张卡")
        for c in cards:
            logger.info("  → %s %s (%s) %s", c["brand"], c["last4"], c["exp"], "✓默认" if c["default"] else "")
    else:
        ps.log("⚠️  未找到已绑定的支付方式")
        logger.warning("[阶段6] 未找到支付方式")

    return cards


# ══════════════════════════════════════════════════════════════════════════
# 阶段 7: 验证 Plus 订阅状态
# ══════════════════════════════════════════════════════════════════════════

def verify_subscription(ps: PlusSession) -> dict:
    """
    验证当前账号的 Plus 订阅状态。

    通过 GET /api/auth/session 中的 plan 信息判断。
    """
    from core.session import BrowserSession
    session = BrowserSession(proxy=ps.proxy, device_id=ps.device_id or None)
    headers = {
        "Authorization": f"Bearer {ps.access_token}",
    }
    url = "https://chatgpt.com/api/auth/session"

    ps.log("阶段7: 验证订阅状态...")
    resp = session.get(url, headers=headers)
    data = resp.json() if resp.text else {}

    user = data.get("user", {})
    plan = (user.get("plan", {}) or {}).get("title", "") or user.get("plan", "")
    plan_str = str(plan).lower()
    is_plus = any(kw in plan_str for kw in ["plus", "pro", "enterprise"])

    ps.subscription_active = is_plus
    if is_plus:
        ps.log(f"✅ ChatGPT {plan} 已激活！")
    else:
        ps.log(f"ℹ️  当前计划: {plan}，暂未检测到 Plus")

    logger.info("[阶段7] plan=%s, is_plus=%s", plan, is_plus)
    return {"is_plus": is_plus, "plan": plan, "raw": data}


# ══════════════════════════════════════════════════════════════════════════
# 阶段 7.5: 激活订阅（checkout confirm 链路，2026-08 前端实测端点）
# ══════════════════════════════════════════════════════════════════════════

def _checkout_processor_entity(checkout_session_id: str) -> str:
    """根据 checkout_session_id 前缀判断 processor_entity。"""
    sid = str(checkout_session_id or "")
    if sid.startswith("oaics_"):
        return "oaics"
    return "stripe"  # cs_ 或未知默认 stripe


def _checkout_auth_headers(ps: PlusSession) -> dict:
    return {
        "Authorization": f"Bearer {ps.access_token}",
        "Content-Type": "application/json",
    }


def _fetch_checkout_state(ps: PlusSession) -> dict:
    """
    GET /backend-api/payments/checkout/{entity}/{checkout_session_id}
    获取 checkout 会话状态（诊断 + 供后续 update 使用）。
    """
    entity = _checkout_processor_entity(ps.checkout_session_id)
    url = ps.base_url + ROUTE_CHECKOUT_STATE.format(entity=entity, checkout_session_id=ps.checkout_session_id)
    ps.log("阶段7.1: 获取 checkout 状态...")
    resp = _plus_request_with_retry(ps, "GET", url, _checkout_auth_headers(ps), label="获取 checkout 状态")
    data = resp.json() if resp.text else {}
    if not resp.ok:
        ps.log(f"⚠️ 获取 checkout 状态失败 HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:200]}")
    else:
        ps.log("✅ 获取 checkout 状态成功")
    ps.checkout_state = data
    return data


def _update_checkout_plan(ps: PlusSession) -> bool:
    """
    POST /backend-api/payments/checkout/update —— 设置 plus_plan 意图。
    前端对应 updateCheckout(intent={type:'plus_plan', planName:'plus',
    priceInterval:'month', seatQuantity:1, discountCode, promoCampaign})。
    best-effort：失败仅告警，不阻断后续 confirm。
    """
    from config import plus as _plus_cfg
    entity = _checkout_processor_entity(ps.checkout_session_id)
    promo_id = getattr(_plus_cfg, "ZERO_PLUS_PROMO_CAMPAIGN_ID", "plus-1-month-free")
    payload = {
        "checkout_session_id": ps.checkout_session_id,
        "processor_entity": entity,
        "plan_name": "plus",
        "price_interval": "month",
        "seat_quantity": 1,
        "discount_code": None,
        "promo_campaign": {"promo_campaign_id": promo_id, "is_coupon_from_query_param": False},
    }
    url = ps.base_url + ROUTE_CHECKOUT_UPDATE
    ps.log("阶段7.2: 更新 checkout 计划（plus/month）...")
    try:
        resp = _plus_request_with_retry(ps, "POST", url, _checkout_auth_headers(ps), payload, label="更新 checkout 计划")
        data = resp.json() if resp.text else {}
        if resp.ok:
            ps.checkout_state = data.get("checkout_session") or data
            ps.log("✅ checkout 计划更新成功")
            return True
        ps.log(f"⚠️ checkout 计划更新失败 HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:200]}")
    except Exception as exc:
        ps.log(f"⚠️ checkout 计划更新异常（{type(exc).__name__}: {str(exc)[:120]}），继续尝试 confirm")
    return False


def _build_billing_address(ps: PlusSession) -> dict:
    """构造后端 billing_address（前端字段：line1/line2/city/state/postal_code/country）。"""
    addr = get_us_address()
    country = ps.billing_country or "US"
    billing = {
        "name": "CHATGPT USER",
        "line1": addr.get("street", addr.get("line1", "")),
        "line2": addr.get("line2", ""),
        "city": addr.get("city", ""),
        "state": addr.get("state", ""),
        "postal_code": addr.get("zip", addr.get("postal_code", "")),
        "country": country,
    }
    ps.billing_address = billing
    return billing


def _submit_checkout_billing_address(ps: PlusSession) -> bool:
    """
    POST /backend-api/payments/checkout/snapshot —— 提交账单地址。
    前端对应 updateCheckoutSnapshot({billingName, billingAddress})。
    best-effort：失败仅告警；confirm 的 confirmation_token 会再带 billing_details。
    """
    billing = _build_billing_address(ps)
    payload = {
        "snapshot": {
            "billing_address": {
                "name": billing["name"],
                "address": {
                    "line1": billing["line1"],
                    "line2": billing["line2"],
                    "city": billing["city"],
                    "state": billing["state"],
                    "postal_code": billing["postal_code"],
                    "country": billing["country"],
                },
            }
        }
    }
    url = ps.base_url + ROUTE_CHECKOUT_SNAPSHOT
    ps.log(f"阶段7.3: 提交账单地址（{billing['country']}）...")
    try:
        resp = _plus_request_with_retry(ps, "POST", url, _checkout_auth_headers(ps), payload, label="提交账单地址")
        if resp.ok:
            ps.log("✅ 账单地址提交成功")
            return True
        data = resp.json() if resp.text else {}
        ps.log(f"⚠️ 账单地址提交失败 HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:200]}")
    except Exception as exc:
        ps.log(f"⚠️ 账单地址提交异常（{type(exc).__name__}: {str(exc)[:120]}），继续尝试 confirm")
    return False


def _create_stripe_confirmation_token(ps: PlusSession) -> str:
    """
    POST https://api.stripe.com/v1/confirmation_tokens —— 用已绑定 pm 生成 ct_ token。
    替代前端 stripe.createConfirmationToken({elements})（实测：
    payment_method=pm_xxx 即可，无需 Stripe SDK 上下文）。
    """
    pk = _resolve_publishable_key(ps.client_secret, ps)
    ps.publishable_key = pk
    data = {"payment_method": ps.payment_method_id}
    billing = ps.billing_address or {}
    if billing.get("line1") or billing.get("city"):
        data.update({
            "billing_details[name]": billing.get("name", "CHATGPT USER"),
            "billing_details[address][line1]": billing.get("line1", ""),
            "billing_details[address][line2]": billing.get("line2", ""),
            "billing_details[address][city]": billing.get("city", ""),
            "billing_details[address][state]": billing.get("state", ""),
            "billing_details[address][postal_code]": billing.get("postal_code", ""),
            "billing_details[address][country]": billing.get("country", ps.billing_country or "US"),
        })
    ps.log(f"阶段7.4: 生成 Stripe ConfirmationToken（pm={ps.payment_method_id[:12]}...）...")
    resp = _stripe_api_request("POST", "/confirmation_tokens", pk, data, proxy=ps.proxy)
    ct_id = resp.get("id", "")
    if not ct_id:
        raise RuntimeError(f"创建 ConfirmationToken 失败: {json.dumps(resp, ensure_ascii=False)[:200]}")
    ps.confirm_token = ct_id
    ps.log(f"✅ ConfirmationToken 创建成功: {ct_id}")
    logger.info("[阶段7.4] ConfirmationToken OK: %s", ct_id)
    return ct_id


def _build_checkout_sentinel_headers(ps: PlusSession) -> dict:
    """
    构造 checkout confirm 的 sentinel 头（flow=checkout_session_approval）。
    复用 sentinel_runner（Node VM 跑官方 SDK），page_url 指向 checkout 页面。
    失败时返回空 dict（不阻断，confirm 可能仍可过）。
    """
    from core.openai_auth import request_sentinel_token
    from core.sentinel_runner import generate_sentinel_token
    from core.session import BrowserSession
    from config import USER_AGENT

    flow = "checkout_session_approval"
    try:
        session = BrowserSession(proxy=ps.proxy, device_id=ps.device_id or None)
        sentinel_resp = request_sentinel_token(session, flow)
        page_url = f"{ps.base_url}{ps.checkout_url or ''}" or "https://chatgpt.com/"
        header_value = generate_sentinel_token(
            challenge=sentinel_resp,
            flow=flow,
            device_id=session.device_id,
            user_agent=(getattr(session, "browser_profile", {}) or {}).get("user_agent") or USER_AGENT,
            page_url=page_url,
            browser_profile=getattr(session, "browser_profile", None),
            sentinel_sid=getattr(session, "sentinel_sid", None),
            react_listening_key=getattr(session, "react_listening_key", None),
            react_container_key=getattr(session, "react_container_key", None),
            react_resources_key=getattr(session, "react_resources_key", None),
            cookie=f"oai-did={session.device_id}",
        )
        headers = {"openai-sentinel-token": header_value}
        try:
            parsed = json.loads(header_value)
            if parsed.get("so"):
                headers["openai-sentinel-so-token"] = json.dumps(
                    {"so": parsed["so"], "c": parsed.get("c", sentinel_resp.get("token", "")),
                     "id": session.device_id, "flow": flow}, separators=(',', ':'))
        except (ValueError, TypeError):
            pass
        ps.log("✅ checkout sentinel 头生成成功")
        return headers
    except Exception as exc:
        ps.log(f"⚠️ checkout sentinel 头生成失败（{type(exc).__name__}: {str(exc)[:100]}），confirm 不带 sentinel 头")
        logger.warning("[PlusZero] checkout sentinel 头生成失败: %s", exc)
        return {}


def _confirm_checkout(ps: PlusSession, *, custom_payment_method: bool = False) -> dict:
    """
    POST /backend-api/payments/checkout/confirm —— 确认订阅。

    前端 confirmCheckout 载荷（_2t）：
      - confirmation_token: {checkout_session_id, confirm_token, selected_payment_method_type}
      - custom_payment_method: {checkout_session_id, selected_payment_method_type}
      - conditional_offer_continuation: {checkout_session_id}
    响应若 conditional_offer_preflight=true 且 type=setup_intent，需先确认
    SetupIntent 再以 conditional_offer_continuation 二次 confirm。
    """
    payload = {"checkout_session_id": ps.checkout_session_id}
    if custom_payment_method or not ps.confirm_token:
        payload["selected_payment_method_type"] = "card"
    else:
        payload["confirm_token"] = ps.confirm_token
        payload["selected_payment_method_type"] = "card"

    headers = _checkout_auth_headers(ps)
    headers.update(_build_checkout_sentinel_headers(ps))
    url = ps.base_url + ROUTE_CHECKOUT_CONFIRM

    ps.log(f"阶段7.5: 确认订阅（confirm_token={'有' if ps.confirm_token else '无(custom)'}）...")
    resp = _plus_request_with_retry(ps, "POST", url, headers, payload, label="确认订阅")
    data = resp.json() if resp.text else {}

    if not resp.ok:
        detail = data.get("detail", data)
        err_msg = f"确认订阅失败 HTTP {resp.status_code}: {json.dumps(detail, ensure_ascii=False)[:300]}"
        ps.log(f"❌ {err_msg}")
        logger.warning("[阶段7.5] %s", err_msg)
        raise RuntimeError(err_msg)

    ps.checkout_confirm = data
    ps.log(f"✅ confirm 响应: status={data.get('status', '?')} type={data.get('type', '?')}")

    # conditional_offer_preflight：先完成 SetupIntent 鉴权再二次 confirm
    if data.get("conditional_offer_preflight") and data.get("type") == "setup_intent" and data.get("client_secret"):
        ps.log("阶段7.5b: 检测到 conditional_offer_preflight，确认 SetupIntent 后二次 confirm...")
        pk = _resolve_publishable_key(ps.client_secret, ps)
        si_id = data["client_secret"].split("_secret_")[0]
        si_resp = _stripe_api_request(
            "POST", f"/setup_intents/{si_id}/confirm", pk,
            {"payment_method": ps.payment_method_id, "use_stripe_sdk": "false", "key": pk},
            proxy=ps.proxy,
        )
        if si_resp.get("status") != "succeeded":
            raise RuntimeError(f"conditional_offer SetupIntent 确认失败: {json.dumps(si_resp, ensure_ascii=False)[:200]}")
        ps.log("✅ conditional_offer SetupIntent 确认成功，二次 confirm...")
        resp2 = _plus_request_with_retry(
            ps, "POST", url, headers,
            {"checkout_session_id": ps.checkout_session_id},
            label="确认订阅(conditional_offer_continuation)",
        )
        data2 = resp2.json() if resp2.text else {}
        ps.checkout_confirm = data2
        return data2

    return data


def activate_plus_subscription(ps: PlusSession) -> dict:
    """
    阶段7.5 主入口：绑卡成功后，调用 checkout update/snapshot/confirm 激活订阅。

    Returns:
        {ok, status, message, confirm_data}
    """
    steps_log_before = len(ps.steps_log)
    try:
        # 1) 获取 checkout 状态（诊断，失败不阻断）
        try:
            _fetch_checkout_state(ps)
        except Exception as exc:
            ps.log(f"⚠️ 获取 checkout 状态异常: {exc}")

        # 2) 更新计划（best-effort）
        _update_checkout_plan(ps)

        # 3) 提交账单地址（best-effort）
        _submit_checkout_billing_address(ps)

        # 4) 生成 ConfirmationToken（需要已绑定的 payment_method_id）
        if not ps.payment_method_id:
            return {
                "ok": False, "status": "no_payment_method",
                "message": "缺少 payment_method_id，无法生成 ConfirmationToken",
            }
        try:
            _create_stripe_confirmation_token(ps)
        except Exception as exc:
            ps.log(f"⚠️ ConfirmationToken 创建失败（{type(exc).__name__}: {str(exc)[:120]}），改走 custom_payment_method confirm")
            logger.warning("[阶段7.5] ConfirmationToken 失败，降级 custom 路径: %s", exc)
            ps.confirm_token = None

        # 5) confirm
        data = _confirm_checkout(ps, custom_payment_method=(not ps.confirm_token))

        status = data.get("status", "")
        if status in ("error", "blocked", "expired"):
            return {
                "ok": False, "status": status,
                "message": f"confirm 返回 {status}: {json.dumps(data, ensure_ascii=False)[:300]}",
                "confirm_data": data,
            }
        if status in ("succeeded", "confirmed", "requires_action", "processing"):
            ps.log("✅ 订阅确认提交成功，等待验证订阅状态...")
            return {"ok": True, "status": status, "message": f"confirm 成功 (status={status})", "confirm_data": data}
        # 其他状态（如 requires_action 已处理）也视为已提交
        return {"ok": True, "status": status or "submitted", "message": "订阅已提交", "confirm_data": data}
    except Exception as exc:
        logger.exception("[PlusZero] 订阅激活失败")
        return {
            "ok": False, "status": "error",
            "message": f"{type(exc).__name__}: {exc}",
            "steps_log": ps.steps_log[steps_log_before:],
        }


# ══════════════════════════════════════════════════════════════════════════
# 辅助: 生成 / 获取 US 地址
# ══════════════════════════════════════════════════════════════════════════

def get_us_address() -> dict:
    """
    获取一个美国地址（随机从备用地址池选一个）。

    也可以对接 usaddressgen.com API，但考虑到稳定性直接用内置池。
    """
    addr = random.choice(BACKUP_US_ADDRESSES)
    logger.info("[地址] 使用 US 地址: %s, %s, %s %s", addr["street"], addr["city"], addr["state"], addr["zip"])
    return dict(addr)


# ══════════════════════════════════════════════════════════════════════════
# 辅助: 卡号校验 / 格式化
# ══════════════════════════════════════════════════════════════════════════

def normalize_card(card_number: str) -> str:
    """去掉卡号中的空格和横线。"""
    return re.sub(r"[\s\-]", "", card_number)


def validate_card_number(card_number: str) -> bool:
    """Luhn 算法校验卡号。"""
    digits = [int(d) for d in normalize_card(card_number)]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    alt = False
    for d in reversed(digits):
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
        alt = not alt
    return checksum % 10 == 0


# ══════════════════════════════════════════════════════════════════════════
# 浏览器模式绑卡（调用油猴脚本逻辑的 Selenium 实现）
# ══════════════════════════════════════════════════════════════════════════

def _browser_bind_card(ps: PlusSession, card_number: str, exp_month: str,
                       exp_year: str, cvc: str, card_name: str = "CHATGPT USER") -> bool:
    """
    使用 Playwright 浏览器在 checkout 页面执行 Stripe confirmCardSetup 绑卡。

    适用场景：Stripe API 直连被限制（publishable_key tokenization restricted），
    必须通过 Stripe.js 客户端 SDK 绑卡时使用此兜底方案。

    流程：
      1. 用 Playwright 打开 headless Chromium
      2. 设置 cookie（accessToken）登录 ChatGPT
      3. 导航到 checkout 页面
      4. 从页面提取 client_secret 和 publishable_key
      5. 注入 Stripe.js 直接调用 confirmCardSetup
      6. 返回绑卡结果
    """
    from core.plus_browser_bind import browser_bind_card_via_playwright
    # 账单地址：优先已构建的 ps.billing_address，否则随机取一个 US 备用地址
    addr = ps.billing_address or get_us_address()
    pm_id = browser_bind_card_via_playwright(
        ps, card_number, exp_month, exp_year, cvc, card_name,
        proxy=ps.proxy,
        address=addr,
    )
    ps.payment_method_id = pm_id
    ps.log(f"✅ 浏览器绑卡成功: {pm_id}")
    return True


_DIRTY_IP_FAILURE_MARKERS = (
    "payment_method_not_approved",
    "payment was not approved",
    "card_declined",
    "transaction_not_allowed",
    "not approved",
    "do not honor",
    "try another card",
)

_CHALLENGE_FAILURE_MARKERS = (
    "requires_action",
    "authentication",
    "3ds",
    "hcaptcha",
    "challenge",
    "use_stripe_sdk",
)


def classify_bind_failure(error_text: str) -> str:
    """把 Stripe 绑卡失败归类为 dirty_ip / challenge / other。

    - dirty_ip：脏 IP 或卡被拒类（换出口 IP 重试可能直接解决，参考论坛经验
      "Payment was not approved 说明 ip 不干净，换代理/warp 能过"）
    - challenge：需要 3DS/hCaptcha/SDK 挑战（走浏览器兜底）
    - other：其他业务错误（不盲目重试）
    """
    text = str(error_text or "").lower()
    if any(m in text for m in _DIRTY_IP_FAILURE_MARKERS):
        return "dirty_ip"
    if any(m in text for m in _CHALLENGE_FAILURE_MARKERS):
        return "challenge"
    return "other"


def _mask_proxy(proxy: str) -> str:
    """日志脱敏：只保留 scheme://host:port，去掉用户名密码。"""
    if not proxy:
        return "(直连)"
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(proxy)
        host = parts.hostname or ""
        port = parts.port or ""
        return f"{parts.scheme}://{host}:{port}" if port else f"{parts.scheme}://{host}"
    except Exception:
        return str(proxy)[:60]


def _rotate_plus_proxy(exclude: str = "") -> str:
    """从代理池抽一个新代理；与 exclude 相同则最多重抽 5 次。无池返回空串。"""
    from config.proxy import pick_proxy
    for _ in range(5):
        candidate = pick_proxy() or ""
        if not candidate or candidate != exclude:
            return candidate
    return ""


# ══════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════

def run_zero_plus(
    access_token: str,
    account_id: str,
    email: str = "",
    proxy: str = "",
    *,
    card_number: str = "",
    exp_month: str = "",
    exp_year: str = "",
    cvc: str = "",
    card_name: str = "CHATGPT USER",
    force_browser_mode: bool = False, session_info: dict | None = None,
    device_id: str = "",
) -> dict:
    """
    零元 Plus 开通主流程。

    Args:
        access_token: ChatGPT accessToken（美区提取）
        account_id: ChatGPT account id（从 session 获取）
        email: 注册邮箱（仅用于日志）
        proxy: 代理
        card_number: 卡号
        exp_month: 有效期月 (2位)
        exp_year: 有效期年 (4位或2位)
        cvc: CVV
        card_name: 持卡人姓名
        force_browser_mode: 强制使用浏览器绑卡模式

    Returns:
        dict: { ok, status, message, checkout_url, subscription_active, cards_bound, steps_log, card_used, address_used }
    """
    ps = PlusSession(access_token=access_token, account_id=account_id,
                     email=email, proxy=proxy, device_id=device_id)

    try:
        ps.log("🚀 开始零元 Plus 开通流程")

        # ── 阶段 2: 验证 session ──
        try:
            if session_info is not None:
                session_data = session_info
            else:
                session_data = fetch_session(access_token, proxy, device_id=device_id)
            ps.account_id = session_data.get("account", {}).get("id", account_id)
            ps.log(f"✅ Session 验证通过, accountId={ps.account_id}")
        except Exception as exc:
            return _result(ok=False, status="failed",
                           message=f"Session 验证失败: {exc}",
                           steps_log=ps.steps_log)

        # ── 阶段 3: 切菲律宾结算 ──
        try:
            switch_to_philippines(ps)
        except Exception as exc:
            return _result(ok=False, status="failed",
                           message=f"切菲律宾结算失败: {exc}",
                           steps_log=ps.steps_log)

        # ── 阶段 4: 创建 SetupIntent ──
        # "No payment account exists" = 账号的 Stripe 支付账户尚未异步开通，
        # 重新触发 checkout 会话 + 等待后重试可自愈（run23 实测偶发）。
        _si_attempt = 0
        while True:
            _si_attempt += 1
            try:
                create_setup_intent(ps)
                break
            except Exception as exc:
                if "No payment account exists" not in str(exc) or _si_attempt >= 3:
                    return _result(ok=False, status="failed",
                                   message=f"创建 SetupIntent 失败: {exc}",
                                   checkout_url=ps.checkout_url,
                                   steps_log=ps.steps_log)
                ps.log(f"⚠️  支付账户未就绪(第{_si_attempt}次)，重切结算+等待后重试 SetupIntent")
                time.sleep(12)
                try:
                    switch_to_philippines(ps)
                except Exception:
                    pass
                time.sleep(12)

        # ── 阶段 5: 绑卡 ──
        if not card_number:
            return _result(ok=False, status="need_card",
                           message="需要提供卡号信息",
                           checkout_url=ps.checkout_url,
                           client_secret=ps.client_secret,
                           steps_log=ps.steps_log)

        # 标准化有效期
        if len(exp_year) == 2:
            exp_year = f"20{exp_year}"

        # 执行绑卡：API 直连优先；识别为脏 IP/卡被拒时自动换出口 IP 重试
        # （参考论坛经验：Payment was not approved = ip 不干净，换代理/warp 能过），
        # 换完仍失败或非脏 IP 失败才走浏览器兜底（处理 3DS/hCaptcha 挑战）。
        from config import plus as _plus_cfg
        bind_retries = max(0, int(getattr(_plus_cfg, "ZERO_PLUS_BIND_PROXY_RETRIES", 2)))
        bind_ok = False
        bind_error = ""
        last_error = None
        proxy_attempt = 0

        while not bind_ok:
            if proxy_attempt > 0:
                new_proxy = _rotate_plus_proxy(exclude=ps.proxy)
                if new_proxy:
                    ps.proxy = new_proxy
                    ps.log(f"🔄 已换出口 IP 重试绑卡: {_mask_proxy(new_proxy)}")
                else:
                    ps.log("⚠️  代理池无可用新代理，继续当前出口重试")
            proxy_attempt += 1

            # 确定绑卡模式（每次重试重新读配置，允许 WebUI 热改）
            use_browser = force_browser_mode
            if not use_browser:
                try:
                    use_browser = (_plus_cfg.ZERO_PLUS_BIND_MODE == "browser")
                except Exception:
                    use_browser = False

            api_error = None
            if not use_browser:
                # API 模式：先尝试 Stripe API 直连
                try:
                    bind_card_via_stripe_api(ps, normalize_card(card_number),
                                             exp_month, exp_year, cvc, card_name)
                    bind_ok = True
                except Exception as exc:
                    api_error = exc
                    last_error = exc
                    bind_error = f"API 绑卡失败: {exc}"
                    ps.log(f"⚠️  {bind_error}")

            if not bind_ok and api_error is not None:
                kind = classify_bind_failure(str(api_error))
                if kind == "dirty_ip" and proxy_attempt <= bind_retries:
                    ps.log(f"🧹 识别为脏 IP/卡被拒，换出口 IP 重试（{proxy_attempt}/{bind_retries}）...")
                    continue
                ps.log(f"⚠️  API 绑卡失败（{kind}），尝试浏览器兜底...")

            if not bind_ok:
                # 浏览器兜底模式（3DS/hCaptcha 挑战或换代理后仍失败）
                try:
                    _browser_bind_card(ps, normalize_card(card_number),
                                       exp_month, exp_year, cvc, card_name)
                    bind_ok = True
                except Exception as exc:
                    last_error = exc
                    bind_error = f"浏览器绑卡也失败: {exc}"
                break

        if not bind_ok:
            return _result(ok=False, status="bind_failed",
                           message=bind_error,
                           checkout_url=ps.checkout_url,
                           steps_log=ps.steps_log)

        # ── 阶段 6: 验证绑定 ──
        try:
            verify_payment_methods(ps)
        except Exception as exc:
            ps.log(f"⚠️  验证支付方式时出错: {exc}")

        # ── 阶段 7: 验证订阅状态 ──
        try:
            verify_subscription(ps)
        except Exception as exc:
            ps.log(f"⚠️  验证订阅状态时出错: {exc}")

        # ── 阶段 7.5: 若绑卡成功但未激活，走 checkout confirm 激活链路 ──
        act: dict = {}
        if not ps.subscription_active:
            from config import plus as _plus_cfg
            _do_activate = bool(getattr(_plus_cfg, "ZERO_PLUS_ACTIVATE_AFTER_BIND", True))
            if not _do_activate:
                ps.log("ℹ️  ZERO_PLUS_ACTIVATE_AFTER_BIND=False，跳过自动激活")
            else:
                ps.log("ℹ️  绑卡成功但未检测到 Plus 激活，尝试 checkout confirm 激活订阅...")
                act = activate_plus_subscription(ps)
            if act.get("ok"):
                ps.log(f"✅ 订阅激活已提交（{act.get('status')}），重新验证订阅状态...")
                try:
                    verify_subscription(ps)
                except Exception as exc:
                    ps.log(f"⚠️  重新验证订阅状态时出错: {exc}")
            else:
                ps.log(f"⚠️  订阅激活未完成: {act.get('message', act.get('status', '?'))[:200]}")

        # ── 结果汇总 ──
        card_used = f"{card_number[:6]}******{card_number[-4:]}" if card_number else None
        address_used = get_us_address()

        if ps.subscription_active:
            if getattr(ps, "country_locked", False):
                msg = f"⚠️ ChatGPT Plus 绑定成功（账号国家锁定降级为 {ps.billing_country}，非0元）"
            else:
                msg = "🎉 ChatGPT Plus 零元开通成功！"
            return _result(
                ok=True, status="success",
                message=msg,
                checkout_url=ps.checkout_url,
                subscription_active=True,
                cards_bound=ps.cards_bound,
                steps_log=ps.steps_log,
                card_used=card_used,
                address_used=address_used,
            )
        else:
            return _result(
                ok=False, status="pending",
                message="绑卡成功，但未检测到 Plus 激活（自动激活链路已执行，见 steps_log；可手动在 ChatGPT 中完成地址填写和订阅确认）。",
                checkout_url=ps.checkout_url,
                subscription_active=False,
                cards_bound=ps.cards_bound,
                steps_log=ps.steps_log,
                card_used=card_used,
                address_used=address_used,
            )

    except Exception as exc:
        logger.exception("[PlusZero] 未知异常")
        return _result(ok=False, status="exception",
                       message=f"异常: {type(exc).__name__}: {exc}",
                       checkout_url=ps.checkout_url,
                       steps_log=ps.steps_log)


# ══════════════════════════════════════════════════════════════════════════
# CLI 入口
# �═════════════════════════════════════════════════════════════════════════

def main():
    """命令行入口。"""
    import argparse
    parser = argparse.ArgumentParser(description="ChatGPT Plus 零元开通")
    parser.add_argument("--token", required=True, help="ChatGPT accessToken")
    parser.add_argument("--account-id", required=True, help="ChatGPT account id")
    parser.add_argument("--email", default="", help="注册邮箱")
    parser.add_argument("--proxy", default="", help="代理")
    parser.add_argument("--card", default="", help="卡号")
    parser.add_argument("--exp-month", default="", help="有效期月")
    parser.add_argument("--exp-year", default="", help="有效期年")
    parser.add_argument("--cvc", default="", help="CVV")
    parser.add_argument("--card-name", default="CHATGPT USER", help="持卡人姓名")
    parser.add_argument("--gcash", action="store_true",
                        help="GCash 渠道：切菲结算并输出结算链接/二维码（真实菲律宾钱包支付）")
    parser.add_argument("--gcash-wait-paid", action="store_true",
                        help="GCash 渠道：输出链接后持续轮询直到支付完成并验证 Plus")
    parser.add_argument("--gcash-qr-port", type=int, default=8787,
                        help="GCash 二维码展示页端口（默认 8787）")
    parser.add_argument("--bind-proxy-retries", type=int, default=None,
                        help="脏 IP 绑卡换出口代理重试次数（默认读 ZERO_PLUS_BIND_PROXY_RETRIES）")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")

    args = parser.parse_args()

    if args.bind_proxy_retries is not None:
        from config import plus as _plus_cfg
        _plus_cfg.ZERO_PLUS_BIND_PROXY_RETRIES = max(0, int(args.bind_proxy_retries))

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s %(module)s:%(lineno)d] %(message)s",
    )

    if getattr(args, "gcash", False):
        result = run_gcash_checkout(
            access_token=args.token,
            account_id=args.account_id,
            email=args.email,
            proxy=args.proxy,
            qr_port=getattr(args, "gcash_qr_port", 8787),
            wait_paid=getattr(args, "gcash_wait_paid", False),
        )
    else:
        result = run_zero_plus(
            access_token=args.token,
            account_id=args.account_id,
            email=args.email,
            proxy=args.proxy,
            card_number=args.card,
            exp_month=getattr(args, "exp_month"),
            exp_year=getattr(args, "exp_year"),
            cvc=args.cvc,
            card_name=args.card_name,
        )

    print("\n" + "=" * 60)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print("=" * 60)

    if result.get("checkout_url"):
        url = result["checkout_url"]
        print(f"\n🔗 结算链接: {url if url.startswith('http') else 'https://chatgpt.com' + url}")

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())


# ══════════════════════════════════════════════════════════════════════════
# GCash 渠道（真实菲律宾钱包支付 Plus）
# ══════════════════════════════════════════════════════════════════════════

def _full_checkout_url(ps: PlusSession) -> str:
    """把短链拼成可打开的完整结算 URL。"""
    if not ps.checkout_url:
        return ""
    if ps.checkout_url.startswith("http"):
        return ps.checkout_url
    return f"{ps.base_url}{ps.checkout_url}"


def _start_qr_server(ps: PlusSession, port: int) -> Any:
    """起一个本地 HTTP 服务，把结算 URL/二维码展示出来供 GCash App 扫码。

    优先用 qrcode 库生成内嵌 QR；未安装时回退为纯 URL 展示页。
    """
    import http.server
    import threading

    checkout_url = _full_checkout_url(ps)
    qr_svg = ""
    try:
        import qrcode
        import qrcode.image.svg
        qr = qrcode.QRCode(border=1)
        qr.add_data(checkout_url)
        qr.make(fit=True)
        img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
        import io
        buf = io.BytesIO()
        img.save(buf)
        qr_svg = buf.getvalue().decode("utf-8")
    except Exception as exc:
        logger.info("[GCash] qrcode 库不可用（%s），仅展示 URL", exc)

    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>GCash 扫码支付</title>
<style>body{{font-family:system-ui;display:flex;flex-direction:column;align-items:center;padding:40px;background:#111;color:#eee}}
a{{color:#7ec8ff;word-break:break-all;max-width:640px}} .box{{background:#222;padding:24px;border-radius:12px;margin-top:20px}}</style>
</head><body><h2>GCash 扫码支付（菲律宾结算）</h2>
<div class="box">{qr_svg if qr_svg else ''}</div>
<p>未显示二维码？请用手机 GCash App 扫码下方链接的二维码：</p>
<p><a href="{checkout_url}" target="_blank">{checkout_url}</a></p>
<p>支付完成后本页自动提示（轮询 /api/auth/session）。</p>
</body></html>"""

    handler = type("QRHandler", (http.server.BaseHTTPRequestHandler,), {
        "do_GET": lambda self: (
            self.send_response(200),
            self.send_header("Content-Type", "text/html; charset=utf-8"),
            self.end_headers(),
            self.wfile.write(page.encode("utf-8")),
        ),
        "log_message": lambda *a, **k: None,
    })
    srv = http.server.HTTPServer(("0.0.0.0", port), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    ps.log(f"🖥️  扫码页已启动: http://127.0.0.1:{port}  （Ctrl+C 停止）")
    return srv


def wait_gcash_payment(
    ps: PlusSession,
    timeout: float = 900.0,
    poll_interval: float = 15.0,
) -> dict:
    """轮询等待 GCash 支付完成并激活 Plus。

    支付完成后 ChatGPT 会异步落账：先出现 payment_methods，再 plan 变 plus。
    """
    deadline = time.monotonic() + timeout
    last_plan = ""
    while time.monotonic() < deadline:
        try:
            verify_payment_methods(ps)
        except Exception as exc:
            logger.debug("[GCash] payment_methods 查询失败: %s", exc)
        try:
            sub = verify_subscription(ps)
            plan = str(sub.get("plan") or "")
            last_plan = plan
            ps.log(f"⏳ 等待 GCash 支付... plan={plan!r}")
            if plan == "plus":
                ps.subscription_active = True
                ps.log("✅ GCash 支付完成，Plus 已激活！")
                return sub
        except Exception as exc:
            logger.debug("[GCash] verify_subscription 失败: %s", exc)
        time.sleep(poll_interval)
    raise RuntimeError(f"等待 GCash 支付超时（{timeout:.0f}s），最后 plan={last_plan!r}")


def run_gcash_checkout(
    access_token: str,
    account_id: str = "",
    email: str = "",
    proxy: str = "",
    *,
    device_id: str = "",
    session_info: dict | None = None,
    qr_port: int = 8787,
    wait_paid: bool = False,
    payment_timeout: float = 900.0,
    poll_interval: float = 15.0,
) -> dict:
    """GCash 渠道：切菲结算 → 输出结算 URL/二维码 →（可选）等待支付并验证 Plus。

    Returns:
        dict: { ok, status, message, checkout_url, checkout_session_id,
                subscription_active, steps_log }
    """
    ps = PlusSession(access_token=access_token, account_id=account_id,
                     email=email, proxy=proxy, device_id=device_id)
    srv = None
    try:
        ps.log("🚀 开始 GCash 支付渠道")

        try:
            session_data = session_info if session_info is not None else fetch_session(
                access_token, proxy, device_id=device_id)
            ps.account_id = session_data.get("account", {}).get("id", account_id)
            ps.log(f"✅ Session 验证通过, accountId={ps.account_id}")
        except Exception as exc:
            return _result(ok=False, status="failed",
                           message=f"Session 验证失败: {exc}", steps_log=ps.steps_log)

        try:
            switch_to_philippines(ps)
        except Exception as exc:
            return _result(ok=False, status="failed",
                           message=f"切菲律宾结算失败: {exc}", steps_log=ps.steps_log)

        checkout_url = _full_checkout_url(ps)
        ps.log(f"✅ 结算链接: {checkout_url}")

        try:
            srv = _start_qr_server(ps, qr_port)
        except Exception as exc:
            ps.log(f"⚠️  二维码页面启动失败（不影响使用）: {exc}")

        if not wait_paid:
            ps.log("⏸️  已输出结算链接，等待扫码端完成支付（wait_paid=False 不轮询）")
            return _result(
                ok=True, status="checkout_ready",
                message=f"结算链接已生成，用 GCash App 扫码支付: {checkout_url}",
                checkout_url=checkout_url,
                checkout_session_id=ps.checkout_session_id,
                subscription_active=False,
                steps_log=ps.steps_log,
            )

        try:
            sub = wait_gcash_payment(ps, timeout=payment_timeout, poll_interval=poll_interval)
            return _result(
                ok=True, status="paid",
                message="GCash 支付完成，Plus 已激活",
                checkout_url=checkout_url,
                checkout_session_id=ps.checkout_session_id,
                subscription_active=True,
                plan=sub.get("plan"),
                steps_log=ps.steps_log,
            )
        except Exception as exc:
            return _result(ok=False, status="payment_timeout",
                           message=str(exc),
                           checkout_url=checkout_url,
                           checkout_session_id=ps.checkout_session_id,
                           steps_log=ps.steps_log)
    finally:
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
