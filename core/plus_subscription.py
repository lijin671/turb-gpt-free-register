# -*- coding: utf-8 -*-
"""
ChatGPT Plus 低成本订阅模块。

根据帖子教程流程：
  1. 提取 session → https://chatgpt.com/api/auth/session
  2. 提交到支付网关 → https://pay.153.ink/
  3. 生成 US 地址 → https://usaddressgen.com/（或内置备用地址）
  4. 获取虚拟卡 → 卡头 451311，成本约 ¥3.90
  5. 浏览器自动化绑卡：
     a. 打开发支付网关页面
     b. 注入控制台脚本（来自蓝奏云下载或本地配置）
     c. 填写卡号 / 有效期 / CVV / 地址
     d. 点击绑定
     e. 刷新页面
     f. 填写用户信息（地址改为美国）
     g. 再次刷新 → 点击订阅

  6. 验证 Plus 状态

本模块的浏览器(浏览器自动化依赖于项目已有的 RoxyBrowser/CloakBrowser。
纯协议模式下通过 HTTP提提交支付网关。
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


# ───────────── 结果结构 ─────────────

def _plus_result(
    *,
    status: str,
    ok: bool = False,
    message: str = "",
    card_used: str | None = None,
    address_used: dict | None = None,
    subscription_active: bool = False,
) -> dict:
    return {
        "status": status,
        "ok": ok,
        "message": message,
        "card_used": card_used,
        "address_used": address_used,
        "subscription_active": subscription_active,
    }


# ══════════════════════════════════════════════════════════════════════
# 阶段 1：提取 session 凭证
# ══════════════════════════════════════════════════════════════════════

def extract_session(access_token: str, proxy: str = "") -> dict:
    """
    从 chatgpt.com/api/auth/session 提取 session 凭整。

    帖子标准：https://chatgpt.com/api/auth/session
    """
    from core.session import BrowserSession

    session = BrowserSession(proxy=proxy)
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["Authorization"] = f"Bearer {access_token}"

    url = "https://chatgpt.com/api/auth/session"
    logger.info("[Plus 步骤1] 提取 session...")

    resp = session.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    logger.info(
        "[Plus 步骤1] session OK，user=%s",
        data.get("user", {}).get("email", "?"),
    )
    return data


# ══════════════════════════════════════════════════════════════════════
# 阶段 2：支付网关提交
# ══════════════════════════════════════════════════════════════════════

def _pick_plus_proxy() -> str:
    """为 Plus 选择代理。"""
    from config import plus as _cfg
    mode = _cfg.PLUS_PROXY_MODE
    if mode == "direct":
        return ""
    if mode == "pool" and _cfg.PLUS_PROXY_POOL:
        return random.choice(_cfg.PLUS_PROXY_POOL)
    if _cfg.PLUS_PROXY:
        return _cfg.PLUS_PROXY
    # auto 模式：跟随注册代理池
    from config.proxy import pick_proxy
    return pick_proxy()


def submit_to_payment_gateway(session_data: dict) -> dict:
    """
    提交 session 到 pay.153.ink/。

    教程：「控制台输入脚本，回车」这部分 在阶段 5 的浏览器里完成。
    HTTP POST 提交 session 是前置验证。
    """
    from config import plus as _cfg
    import requests

    url = _cfg.PLUS_PAYMENT_GATEWAY.rstrip("/")
    proxy = _pick_plus_proxy()
    proxies = {"http": proxy, "https": proxy} if proxy else None

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ),
    }

    payload = {
        "session": session_data,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info("[Plus 步骤2] 提交 session 到 %s ...", url)

    last_error = ""
    for attempt in range(1, _cfg.PLUS_PAYMENT_MAX_RETRIES + 1):
        try:
            resp = requests.post(
                url, headers=headers, json=payload,
                proxies=proxies, timeout=_cfg.PLUS_PAYMENT_TIMEOUT, verify=False,
            )
            logger.info("[Plus 步骤2] HTTP %s (尝试 %s/%s)", resp.status_code, attempt, _cfg.PLUS_PAYMENT_MAX_RETRIES)
            if resp.status_code in (200, 201, 302):
                try:
                    return resp.json()
                except Exception:
                    return {"raw": resp.text[:500]}
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:
            last_error = str(exc)
        if attempt < _cfg.PLUS_PAYMENT_MAX_RETRIES:
            time.sleep(_cfg.PLUS_PAYMENT_RETRY_DELAY)

    return {"error": last_error or "unknown"}


# ══════════════════════════════════════════════════════════════════════
# 阶段 3：US 地址
# ══════════════════════════════════════════════════════════════════════

def generate_us_address() -> dict:
    """
    教程用 https://usaddressgen.com/ 。它是 CSR（客户端渲染），静态请求不带地址数据。
    因此优先尝试 fetch 页面然后用内置备用地址代替。

    只在跑浏览器流程时才可能通过 JS 取到页面上渲染出来的随机地址。
    """
    from config import plus as _cfg

    logger.info("[Plus 步骤3] 取 US 地址...")

    # 尝试 usaddressgen API（部分情况下带 referer + cookie 可过）
    import requests
    try:
        resp = requests.post(
            "https://usaddressgen.com/api/generate",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://usaddressgen.com",
                "Referer": "https://usaddressgen.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
                ),
            },
            json={"count": 1},
            timeout=10,
            verify=False,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and data.get("ok"):
                addr = data.get("addresses", [{}])[0]
                if addr:
                    logger.info("[Plus 步骤3] API 返回地址：%s, %s %s", addr.get("city", "?"), addr.get("state", "?"), addr.get("zip", "?"))
                    return addr
    except Exception:
        pass

    # 回退：内置地址
    backup = random.choice(_cfg.PLUS_BACKUP_ADDRESSES)
    logger.info("[Plus 步骤3] 用内置地址：%s, %s %s", backup["city"], backup["state"], backup["zip"])
    return backup


# ══════════════════════════════════════════════════════════════════════
# 阶段 4：虚拟卡
# ══════════════════════════════════════════════════════════════════════

def _luhn_checksum(card_number: str) -> bool:
    digits = [int(d) for d in card_number]
    for i in range(len(digits) - 2, -1, -2):
        digits[i] *= 2
        if digits[i] > 9:
            digits[i] -= 9
    return sum(digits) % 10 == 0


def _generate_valid_card(bin_prefix: str, length: int = 16) -> str:
    """以 BIN 前缀生成 Luhn 通过卡号。"""
    if len(bin_prefix) >= length:
        return bin_prefix[:length]
    card = list(bin_prefix)
    while len(card) < length - 1:
        card.append(str(random.randint(0, 9)))
    # 校验位
    digits = [int(d) for d in card]
    for i in range(len(digits) - 1, -1, -2):
        digits[i] *= 2
        if digits[i] > 9:
            digits[i] -= 9
    total = sum(digits)
    check = (10 - (total % 10)) % 10
    card.append(str(check))
    return "".join(card)


def get_card_info() -> dict:
    """
    教程：「卡头是 451311，成本 3.90，去找人才市场购买。」

    三种来源（PLUS_CARD_SOURCE）：
     - "manual"  — 配置直接填
     - "pool"    — 从卡池文件换行读
     - "generated" — 自动生成（仅供测试）
    """
    from config import plus as _cfg

    source = _cfg.PLUS_CARD_SOURCE

    if source == "manual":
        if not _cfg.PLUS_CARD_NUMBER:
            raise ValueError("PLUS_CARD_SOURCE=manual 但 PLUS_CARD_NUMBER 为空")
        return {
            "number": _cfg.PLUS_CARD_NUMBER,
            "exp_month": _cfg.PLUS_CARD_EXP_MONTH,
            "exp_year": _cfg.PLUS_CARD_EXP_YEAR,
            "cvv": _cfg.PLUS_CARD_CVV,
        }

    if source == "pool":
        if not _cfg.PLUS_CARD_POOL_FILE:
            raise ValueError("PLUS_CARD_SOURCE=pool 但 PLUS_CARD_POOL_FILE 为空")
        with open(_cfg.PLUS_CARD_POOL_FILE, "r") as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        if not lines:
            raise ValueError("卡池文件为空")
        line = random.choice(lines)
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            raise ValueError(f"卡池格式错误（期望 number|exp_month|exp_year|cvv）：{line}")
        return {
            "number": parts[0],
            "exp_month": parts[1],
            "exp_year": parts[2],
            "cvv": parts[3],
        }

    if source == "generated":
        number = _generate_valid_card(_cfg.PLUS_CARD_BIN, _cfg.PLUS_CARD_NUMBER_LENGTH)
        now = datetime.now()
        exp_year = str(now.year + 3)
        exp_month = f"{random.randint(1, 12):02d}"
        cvv = f"{random.randint(100, 999)}"
        logger.warning("[Plus] 自动生成测试卡号: %s...%s", number[:6], number[-4:])
        return {"number": number, "exp_month": exp_month, "exp_year": exp_year, "cvv": cvv}

    raise ValueError(f"未知卡来源: {source}")


# ══════════════════════════════════════════════════════════════════════
# 阶段 5：浏览器自动化绑卡
# ══════════════════════════════════════════════════════════════════════

def _build_payment_form_script(card: dict, address: dict) -> str:
    """
    生成在支付网关 pay.153.ink 注入绑卡脚本。

    教程全步骤：
      打开支付连接 → 控制台输脚本回车 → 填绑卡信息 →
      点击绑定 → 刷新 → 填写信息（地址美国）→ 刷新 → 订阅
    """
    return f"""\
// ===== Plus 绑卡脚本 =====
(function() {{
'use strict';
console.log('[Plus] 绑卡开始');
var CARD = {json.dumps(card)};
var ADDR = {json.dumps(address)};

function triggerReact(el, value) {{
    var setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    setter.call(el, value);
    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
}}

function fillSelect(el, value) {{
    // React 受控 select
    var setter = Object.getOwnPropertyDescriptor(
        window.HTMLSelectElement.prototype, 'value'
    ).set;
    setter.call(el, value);
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
}}

function findByAny(selectors) {{
    for (var i = 0; i < selectors.length; i++) {{
        var el = document.querySelector(selectors[i]);
        if (el) return el;
    }}
    return null;
}}

function step(desc, fn) {{
    console.log('[Plus] ' + desc);
    return fn();
}}

// ── 卡号 ──
step('填卡号', function() {{
    var el = findByAny(['input[name="cardNumber"]','input[id="cardNumber"]',
        'input[placeholder*="Card"]','input[placeholder*="卡"]','#card-number']);
    if (el) triggerReact(el, CARD.number);
    else console.warn('未找到卡号输入框');
}});

// ── 有效期 ──
step('填有效期', function() {{
    var el = findByAny(['input[name="expiry"]','input[name="exp"]',
        'input[placeholder*="MM/YY"]','#expiry','input[placeholder*="Expir"]']);
    if (el) triggerReact(el, CARD.exp_month + '/' + CARD.exp_year.slice(-2));
    else {{
        var m = findByAny(['select[name="expMonth"]','#exp-month']);
        var y = findByAny(['select[name="expYear"]','#exp-year']);
        if (m) fillSelect(m, CARD.exp_month);
        if (y) fillSelect(y, CARD.exp_year);
    }}
}});

// ── CVC ──
step('填CVC', function() {{
    var el = findByAny(['input[name="cvc"]','input[name="cvv"]',
        'input[placeholder*="CVC"]','input[placeholder*="安全"]','#cvc']);
    if (el) triggerReact(el, CARD.cvv);
    else console.warn('未找到CVC输入框');
}});

// ── 地址 ──
step('填地址', function() {{
    var streetEl = findByAny(['input[name="address"]','input[name="street"]',
        'input[placeholder*="Address"]','input[placeholder*="Street"]','#address']);
    if (streetEl) triggerReact(streetEl, ADDR.street);

    var cityEl = findByAny(['input[name="city"]',
        'input[placeholder*="City"]','#city']);
    if (cityEl) triggerReact(cityEl, ADDR.city);

    var stateEl = findByAny(['select[name="state"]','input[name="state"]',
        'input[placeholder*="State"]','#state']);
    if (stateEl) {{
        if (stateEl.tagName === 'SELECT') fillSelect(stateEl, ADDR.state);
        else triggerReact(stateEl, ADDR.state);
    }}

    var zipEl = findByAny(['input[name="zip"]','input[name="postal"]','input[name="postalCode"]',
        'input[placeholder*="ZIP"]','input[placeholder*="zip"]','input[name="zipCode"]','#zip']);
    if (zipEl) triggerReact(zipEl, ADDR.zip);

    var countryEl = findByAny(['select[name="country"]','#country']);
    if (countryEl) fillSelect(countryEl, 'US');
}});

console.log('[Plus] 表单已填充，请手动点击"绑定"或"订阅"按钮');
}})();
"""


def run_browser_plus_flow(
    email: str,
    card: dict,
    address: dict,
) -> dict:
    """
    在有可用浏览器的条件下打开支付网关，注入脚本辅助填。

    实际交互：脚本填好所有字段后，由人工在控制台/页面上点 "绑定"→"订阅"。
    返回网关是否能正常打开和注入。
    """
    from config import plus as _cfg

    driver_mode = _get_available_browser()
    script = _build_payment_form_script(card, address)

    logger.info(
        "[Plus 步骤5] 浏览器模式=%s，卡=%s...%s，城市=%s",
        driver_mode,
        card["number"][:6], card["number"][-4:],
        address.get("city", ""),
    )

    if driver_mode == "cloak":
        return _run_cloak_payment_flow(script)
    elif driver_mode == "browser_use":
        return _run_bu_payment_flow(script)
    else:
        # 没有可用浏览器 → 仅记录脚本
        logger.info("[Plus] 无可用浏览器，跳过页面交互（脚本已就绪可手动执行）")
        return False, "无可用浏览器"

    return False, "未知驱动"


def _get_available_browser() -> str:
    """检测当前环境具有哪些浏览器驱动可用。"""
    import importlib
    # 优先 Cloak（无 GUI 服务器友好）
    try:
        from config import cloakbrowser as cc
        if getattr(cc, "CLOAK_API_BASE", ""):
            return "cloak"
    except Exception:
        pass
    # Browser Use
    try:
        from config import browser_use as bc2
        api_key = getattr(bc2, "BROWSER_USE_API_KEY", "")
        if api_key and api_key not in ("", "your-api-key-here"):
            return "browser_use"
    except Exception:
        _ = bc2
    return "protocol"


def _run_cloak_payment_flow(script: str) -> tuple[bool, str]:
    """CloakBrowser 打开支付网关页面。"""
    try:
        from core.cloakbrowser_registration import build_cloak_driver
        from config import plus as _cfg

        logger.info("[Plus Cloak] 启动浏览器...")
        driver, opened = build_cloak_driver(proxy=None)
        logger.info("[Plus Cloak] profile=%s", opened.profile_id)

        driver.get(_cfg.PLUS_PAYMENT_GATEWAY)
        logger.info("[Plus Cloak] 支付网关已加载")

        driver.execute_script(script)
        logger.info("[Plus Cloak] 注入（<1 s）已执行")
        time.sleep(2)  # 给 React 响应加上

        # 注入后保持页面 1 min 便于人工确认
        logger.info("[Plus Cloak] 保持 60 s 供人工核对/点击")
        time.sleep(60)

        driver.quit()
        return True, "done"
    except ImportError:
        return False, "CloakBrowser 依赖未安装"
    except Exception as exc:
        return False, f"CloakBrowser 驱动异常: {exc}"


def _run_browser_use_payment_flow(script: str) -> tuple[bool, str]:
    """Browser Use Cloud CDP 打开支付网关页面。"""
    try:
        from core.browser_use_client import BrowserUseClient
        from config import plus as _cfg

        client = BrowserUseClient()
        page = client.new_page()
        page.goto(_cfg.PLUS_PAYMENT_GATEWAY, timeout=30000, wait_until="networkidle")
        page.evaluate(script)
        page.wait_for_timeout(3000)
        logger.info("[Plus BU] 脚本注入完成，等待 60 s 以供人工确认")
        page.wait_for_timeout(60000)
        client.close()
        return True, "done"
    except ImportError:
        return False, "Browser Use 依赖未安装"
    except Exception as exc:
        return False, f"Browser Use 驱动异常: {exc}"


# ══════════════════════════════════════════════════════════════════════
# 阶段 6：验证 Plus
# ══════════════════════════════════════════════════════════════════════

def verify_plus_status(access_token: str, proxy: str = "") -> dict:
    """
    账号切换到 Plus 服务后验证当前套餐。
    """
    from core.session import BrowserSession

    session = BrowserSession(proxy=proxy)
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["Authorization"] = f"Bearer {access_token}"
    headers["Content-Type"] = "application/json"

    url = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
    logger.info("[Plus 验证] 查询套餐...")

    try:
        resp = session.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        plan = (data.get("account") or {}).get("plan_type", "free")
        is_plus = plan in ("plus", "pro", "enterprise")
        logger.info("[Plus 验证] %s → is_plus=%s", plan, is_plus)
        return {"is_plus": is_plus, "plan": plan, "raw": data}
    except Exception as exc:
        logger.error("[Plus 验证] 失败: %s", exc)
        return {"is_plus": False, "plan": "unknown", "raw": {"error": str(exc)}}


# ══════════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════════

def run_plus_subscription(
    email: str,
    access_token: str,
    proxy: str = "",
    session_info: dict | None = None,
) -> dict:
    """
    在已注册的 ChatGPT 账号上执行 Plus 订阅。

    Args:
        email:        注册邮箱
        access_token: ChatGPT accessToken
        proxy:        代理
        session_info: 已有的 /api/auth/session（避免重复请求）
    """
    from config import plus as _cfg

    if not _cfg.ENABLE_PLUS_SUBSCRIPTION:
        return _plus_result(status="skipped", message="ENABLE_PLUS_SUBSCRIPTION=False")

    if not access_token:
        return _plus_result(status="skipped", message="无 access_token")

    logger.info("[Plus] 开始订阅" % email)

    try:
        # 步骤1：提取 session
        if not session_info:
            try:
                session_info = extract_session(access_token, proxy)
            except Exception as exc:
                return _plus_result(status="failed", ok=False,
                    message=f"session 提取失败: {exc}")

        # 步骤2：提交到支付网关
        payment = submit_to_payment_gateway(session_info)
        if isinstance(payment, dict) and payment.get("error"):
            return _plus_result(status="failed", ok=False,
                message=f"payment gateway error: {payment['error']}")

        # 步骤3：US 地址
        address = generate_us_address()

        # 步骤4：虚拟卡
        try:
            card = get_card_info()
        except Exception as exc:
            return _plus_result(status="failed", ok=False,
                message=f"获取卡号失败: {exc}")

        # 步骤5：浏览器绑卡
        ok, msg = run_browser_plus_flow(email, card, address)

        # 步骤6：状态验证
        plan = verify_plus_status(access_token, proxy)

        result = _plus_result(
            status=("success" if (ok and plan.get("is_plus")) else
                    ("pending" if ok else "failed")),
            ok=(ok and plan.get("is_plus")),
            message=msg,
            card_used=f"{card['number'][:6]}...{card['number'][-4:]}",
            address_used=address,
            subscription_active=plan.get("is_plus", False),
        )

        logger.info("[Plus] 结束：%s → status=%s", email, result["status"])
        return result

    except Exception as exc:
        logger.error("[Plus] 异常: %s", type(exc).__name__, exc_info=True)
        return _plus_result(status="failed", ok=False,
            message=f"{type(exc).__name__}: {str(exc)[:200]}")


# ══════════════════════════════════════════════════════════════════════
# 辅助：proxy-checker 集成（帖子推荐的免费代理池）
# ══════════════════════════════════════════════════════════════════════

def fetch_free_proxies_from_checker() -> list[str]:
    """
    从 proxy-checker（github.com/strongshuai/proxy-checker）拉取免费代理列表。

    Returns:
        ["socks5://1.2.3.4:1080", ...]
    """
    from config import plus as _cfg
    import requests

    if not _cfg.PLUS_PROXY_CHECKER_ENABLED:
        return []

    url = (_cfg.PLUS_PROXY_CHECKER_URL.rstrip("/")
           + "/" + _cfg.PLUS_PROXY_CHECKER_API_PATH.lstrip("/"))

    try:
        resp = requests.get(url, timeout=_cfg.PLUS_PROXY_CHECKER_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("[Plus Proxy] checker HTTP %s", resp.status_code)
            return []
        lines = [l.strip() for l in resp.text.splitlines() if l.strip()]
        proxy_type = _cfg.PLUS_PROXY_CHECKER_TYPE
        proxies = []
        for line in lines:
            if "://" in line:
                proxies.append(line)
            else:
                proxies.append(f"{proxy_type}://{line}")
        logger.info("[Plus Proxy] checker 返回 %s 个代理", len(proxies))
        return proxies
    except Exception as exc:
        logger.warning("[Plus Proxy] checker 调用失败: %s", exc)
        return []
