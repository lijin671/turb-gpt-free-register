# -*- coding: utf-8 -*-
"""
Playwright 浏览器绑卡模块（Stripe Elements 模式）。

Stripe 要求使用 Elements（iframe）收集卡号，不能直接传卡号。
流程：
  1. 打开 HTTPS 页面（route 拦截 cloudflare.com）
  2. 加载 Stripe.js
  3. 创建 card element 挂载到页面
  4. 用 Playwright 填 iframe 内的卡号/有效期/CVC
  5. 调用 stripe.confirmCardSetup(clientSecret, {payment_method: {card: cardElement}})
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# hCaptcha 图片挑战 frame 定位
# 实跑教训(2026-08-06): Stripe 集成里 b.stripecdn.com/.../hcaptcha.html 引导页
# 文本 "Close One more step before you're done Select the checkbox below" 会随
# 嵌套复选框 iframe 异步加载 —— 扫描时可能只读到 "one more step" 前缀，导致被
# 误判为图片挑战。真实挑战 frame 在 newassets.hcaptcha.com#frame=challenge，
# 文本包含任务指令(click on all / select all / choose all ...)。
# ---------------------------------------------------------------------------


def _normalize_frame_text(f) -> str:
    """读取 frame 的 innerText 并归一化(小写、压缩空白)。"""
    try:
        t = (f.evaluate("document.body ? document.body.innerText : ''") or "")
    except Exception:
        t = ""
    return " ".join(t.split()).lower()


def _is_hcaptcha_interstitial(tl: str) -> bool:
    """判断归一化文本是否为 hCaptcha 引导/中间页（不是图片题）。"""
    return ("select the checkbox" in tl or "checkbox below" in tl
            or ("one more step" in tl and "close" in tl))


_HCAPTCHA_TASK_KEYS = (
    "choose all", "select all", "click on all", "please click",
    "crosses", "taps", "click on the",
    # 2026-08-06 run21 实测出现 "Pick all objects smaller than the one shown"
    "pick all", "tap all", "click all", "choose the", "pick the",
    "smaller than", "larger than", "bigger than", "the one shown",
    "which is smaller", "which is larger", "which is bigger",
)


def _pick_hcaptcha_challenge_frame(frames, wait=None):
    """定位 hCaptcha 图片挑战 frame。

    第一轮优先 newassets.hcaptcha.com#frame=challenge 的真实挑战 frame；
    第二轮兜底任意 hcaptcha/captcha frame。两种都排除引导页文本，且对
    "one more step" 开头的候选做异步复核(等待后重读，仍为引导页则跳过)。

    Returns:
        (frame, 归一化任务文本) 或 (None, "")
    """
    def _probe(f):
        tl = _normalize_frame_text(f)
        if _is_hcaptcha_interstitial(tl):
            return None
        # 排除 JS blob / license 文本帧（run24/26 实测 checkbox-invisible frame
        # 的 body 是 hcaptcha 内联 JS，含 "drag"/"click" 等词会误命中）
        if (tl.startswith("/*") or tl.startswith("!function") or "function" in tl
                or len(tl) > 300 or tl.count("{") > 5):
            return None
        if not any(k in tl for k in _HCAPTCHA_TASK_KEYS):
            return None
        if "one more step" in tl and wait is not None:
            wait()
            tl = _normalize_frame_text(f)
            if _is_hcaptcha_interstitial(tl):
                return None
        return tl

    for f in frames:
        furl = (f.url or "").lower()
        if "hcaptcha" not in furl and "captcha" not in furl:
            continue
        if "frame=challenge" not in furl:
            continue
        tl = _probe(f)
        if tl is not None:
            return f, tl
    for f in frames:
        furl = (f.url or "").lower()
        if "hcaptcha" not in furl and "captcha" not in furl:
            continue
        tl = _probe(f)
        if tl is not None:
            return f, tl
    return None, ""


def browser_bind_card_via_playwright(
    ps: "PlusSession",  # noqa: F821
    card_number: str,
    exp_month: str,
    exp_year: str,
    cvc: str,
    card_name: str = "CHATGPT USER",
    proxy: str = "",
    address: dict | None = None,
) -> str:
    """
    使用 Playwright + Stripe Elements 执行绑卡。

    Args:
        ps: PlusSession（需包含 client_secret）
        card_number: 卡号
        exp_month: 有效期月 (2位)
        exp_year: 有效期年 (4位)
        cvc: CVV
        card_name: 持卡人姓名
        proxy: 代理 URL（如 http://user:pass@host:port），用于 Playwright 访问 Stripe

    Returns:
        payment_method_id

    Raises:
        RuntimeError: 绑卡失败
    """
    from playwright.sync_api import sync_playwright

    # 账单地址（AVS 相关）：默认 US，可用 ps.billing_address / address 覆盖
    addr = dict(address or {})
    addr_line1 = str(addr.get("line1") or addr.get("street") or "221B Baker Street")
    addr_city = str(addr.get("city") or "New York")
    addr_state = str(addr.get("state") or "NY")
    addr_zip = str(addr.get("zip") or addr.get("postal_code") or "10001")
    addr_country = str(addr.get("country") or "US")
    logger.info("[BrowserBind] billing address: %s, %s, %s %s, %s", addr_line1, addr_city, addr_state, addr_zip, addr_country)

    client_secret = ps.client_secret
    if not client_secret:
        raise RuntimeError("client_secret 为空，请先执行阶段 4 创建 SetupIntent")

    from core.plus_zero import _resolve_publishable_key
    pk = _resolve_publishable_key(client_secret, ps)
    ps.publishable_key = pk

    # 代理处理：PLUS_BIND_PROXY（绑卡浏览器专用，如 WARP）> 传入 proxy > ps.proxy > PLUS_PROXY
    effective_proxy = (
        os.environ.get('PLUS_BIND_PROXY', '') or proxy
        or getattr(ps, 'proxy', '') or ''
    )
    if not effective_proxy:
        effective_proxy = os.environ.get('PLUS_PROXY', '') or os.environ.get('PROXY_POOL', '')

    logger.info("[BrowserBind] Stripe Elements 绑卡开始")
    logger.info("[BrowserBind] pk=%s... client_secret=%s...", pk[:25], client_secret[:30])
    logger.info("[BrowserBind] proxy=%s", effective_proxy[:60] if effective_proxy else "(直连)")

    result = {"ok": False, "payment_method_id": "", "status": "", "error": ""}

    # 解析代理 URL（http://user:pass@host:port → Playwright proxy dict）
    proxy_dict = None
    launch_args = []
    if effective_proxy:
        from urllib.parse import urlparse
        try:
            parsed = urlparse(effective_proxy)
            # Playwright/Chromium 只认 socks5://，不认 socks5h://（Chromium socks5 本就远端解析域名）
            _scheme = "socks5" if parsed.scheme == "socks5h" else parsed.scheme
            server = f"{_scheme}://{parsed.hostname}:{parsed.port or 80}"
            proxy_dict = {"server": server}
            if parsed.username:
                proxy_dict["username"] = parsed.username
            if parsed.password:
                proxy_dict["password"] = parsed.password
            logger.info("[BrowserBind] Playwright proxy server=%s user=%s", server, parsed.username or "(无)")
        except Exception as e:
            logger.warning("[BrowserBind] 代理 URL 解析失败，退回 launch arg: %s", e)
            launch_args.append(f'--proxy-server={effective_proxy}')
        launch_args.append('--ignore-certificate-errors')

    try:
        with sync_playwright() as p:
            _headless = os.environ.get("PLUS_BIND_HEADLESS", "1") != "0"
            if not _headless:
                logger.info("[BrowserBind] 有头模式（Xvfb/桌面）")
            browser = p.chromium.launch(
                headless=_headless,
                channel="chromium",
                args=launch_args + [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-infobars",
                    "--window-size=1280,900",
                    "--remote-debugging-port=9223",
                ],
            )

            ctx_kwargs: dict = dict(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/127.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/New_York",
                ignore_https_errors=True,
            )
            if proxy_dict:
                ctx_kwargs["proxy"] = proxy_dict
            ctx = browser.new_context(**ctx_kwargs)
            # ── stealth 补丁：降低 headless 自动化指纹被 Stripe/hCaptcha 识别的概率 ──
            ctx.add_init_script("""
                (() => {
                    try {
                        // 移除 navigator.webdriver
                        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                        // 补齐 window.chrome
                        if (!window.chrome) {
                            window.chrome = { runtime: {}, loadTimes: () => ({}), csi: () => ({}) };
                        }
                        // 补齐 plugins / languages
                        Object.defineProperty(navigator, 'plugins', {
                            get: () => [1, 2, 3, 4, 5].map(() => ({ name: 'PDF Viewer', filename: 'internal-pdf-viewer' })),
                        });
                        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                        // permissions.query 伪装（hCaptcha 常探测 notification 权限）
                        const origQuery = navigator.permissions && navigator.permissions.query;
                        if (origQuery) {
                            navigator.permissions.query = (p) => (
                                p && p.name === 'notifications'
                                    ? Promise.resolve({ state: Notification.permission, onchange: null })
                                    : origQuery(p)
                            );
                        }
                        // WebGL 厂商（常见检测点）
                        const getParameter = WebGLRenderingContext.prototype.getParameter;
                        WebGLRenderingContext.prototype.getParameter = function (param) {
                            if (param === 37445) return 'Intel Inc.';
                            if (param === 37446) return 'Intel Iris OpenGL Engine';
                            return getParameter.call(this, param);
                        };
                    } catch (e) {}
                })();
            """)
            page = ctx.new_page()

            # 记录 Stripe confirm 相关响应体，用于诊断 hCaptcha/风险校验；
            # 同时捕获 hCaptcha 音频挑战的音频 URL（audio/mpeg 或 .mp3/.wav/.ogg 等）
            _audio_urls: list[str] = []
            def _on_response(resp):
                try:
                    url = resp.url or ""
                    if "/setup_intents/" in url and "stripe.com" in url:
                        body = resp.text()[:600]
                        logger.info("[BrowserBind] Stripe confirm 响应 %s: %s", resp.status, body)
                    ctype = (resp.headers.get("content-type") or "").lower()
                    low = url.lower()
                    if ctype.startswith("audio/") or low.endswith((".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac")):
                        if url not in _audio_urls:
                            _audio_urls.append(url)
                            logger.info("[BrowserBind] 捕获音频 URL: %s (ct=%s)", url[:140], ctype[:40])
                except Exception:
                    pass
            page.on("response", _on_response)

            # 拦截 cloudflare.com 返回空白页
            def route_handler(route):
                route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="""<html><head></head><body>
                        <div id="card-element"></div>
                        <button id="submit-btn">Pay</button>
                    </body></html>""",
                )
            page.route("https://www.cloudflare.com/*", route_handler)
            page.route("https://www.cloudflare.com/", route_handler)

            page.goto("https://www.cloudflare.com/", wait_until="load", timeout=20000)

            # 注入 Stripe.js 并创建 Elements
            init_script = f"""
            (async () => {{
                try {{
                    const script = document.createElement('script');
                    script.src = 'https://js.stripe.com/v3/?publishableKey={pk}';
                    await new Promise((res, rej) => {{
                        script.onload = res;
                        script.onerror = () => rej(new Error('Stripe.js load failed'));
                        document.head.appendChild(script);
                    }});
                    await new Promise(r => setTimeout(r, 800));

                    window.__stripe = Stripe('{pk}');
                    const elements = window.__stripe.elements();
                    window.__cardElement = elements.create('card', {{
                        style: {{
                            base: {{
                                fontSize: '16px',
                                color: '#32325d',
                                '::placeholder': {{ color: '#aab7c4' }},
                            }},
                        }},
                    }});
                    window.__cardElement.mount('#card-element');
                    await new Promise((res, rej) => {{
                        const t = setTimeout(() => rej(new Error('card element ready 超时(45s)')), 45000);
                        window.__cardElement.on('ready', () => {{ clearTimeout(t); res(); }});
                    }});
                    return {{ ok: true, ready: true }};
                }} catch (e) {{
                    return {{ ok: false, error: e.message }};
                }}
            }})();
            """
            init_result = page.evaluate(init_script)
            logger.info("[BrowserBind] Elements 初始化: %s", json.dumps(init_result, ensure_ascii=False)[:200])
            if not init_result.get("ok"):
                raise RuntimeError(f"Elements 初始化失败: {init_result.get('error')}")

            # 等待 iframe 加载
            page.wait_for_timeout(3000)

            # 查找 Stripe card element iframe（包含卡号输入框的 frame）
            # 注意：Stripe 有多个 iframe，controller frame 不含输入框，
            # 真正的 card inputs 在 elements-inner-card 或 name 以 __privateStripeFrame 开头的 frame。
            page.wait_for_timeout(1000)
            frames = page.frames
            stripe_frame = None

            # 优先：按 frame name（__privateStripeFrame...）或 URL 特征（elements-inner / card）
            for frame in frames:
                url = (frame.url or "").lower()
                name = (frame.name or "").lower()
                if (
                    "elements-inner" in url
                    or ("elements" in url and "card" in url)
                    or name.startswith("__privatestripeframe")
                ):
                    stripe_frame = frame
                    logger.info("[BrowserBind] 找到 Stripe card frame: url=%s name=%s", (frame.url or "")[:90], frame.name or "")
                    break

            # 其次：遍历所有 frame，找含 cardnumber 输入的
            if not stripe_frame:
                for frame in frames:
                    try:
                        if frame.query_selector('input[name="cardnumber"], input[name="cardNumber"], input[autocomplete="cc-number"]'):
                            stripe_frame = frame
                            logger.info("[BrowserBind] 通过输入框定位 Stripe frame: %s", (frame.url or "")[:90])
                            break
                    except Exception:
                        continue

            # 再次：#card-element 下的 iframe
            if not stripe_frame:
                try:
                    iframe_el = page.query_selector("#card-element iframe")
                    if iframe_el:
                        stripe_frame = iframe_el.content_frame()
                        if stripe_frame:
                            logger.info("[BrowserBind] 通过 #card-element iframe 找到 Stripe frame")
                except Exception as e:
                    logger.warning("[BrowserBind] 查找 iframe 异常: %s", e)

            if not stripe_frame:
                for i, frame in enumerate(frames):
                    logger.info("[BrowserBind] Frame %d: %s (name=%s)", i, (frame.url or "")[:80], frame.name or "")
                raise RuntimeError("未找到 Stripe card iframe")

            # 填写卡信息
            postal_code = addr_zip

            # 卡号（轮询等待输入框渲染，慢代理下 frame 先于 inputs 出现）
            def _fill_with_poll(sel, value, label, max_rounds=4, wait=8000):
                for r in range(1, max_rounds + 1):
                    try:
                        stripe_frame.wait_for_selector(sel, timeout=wait)
                        stripe_frame.fill(sel, value, timeout=wait)
                        logger.info("[BrowserBind] ✅ %s 已填写 (轮询第%s轮)", label, r)
                        return True
                    except Exception as e:
                        logger.warning("[BrowserBind] %s 第%s轮失败: %s", label, r, str(e)[:100])
                        page.wait_for_timeout(2000)
                return False

            if not _fill_with_poll('input[name="cardnumber"]', card_number, "卡号"):
                try:
                    stripe_frame.fill('input[placeholder*="card"]', card_number, timeout=5000)
                    logger.info("[BrowserBind] ✅ 卡号已填写 (placeholder)")
                except Exception as e2:
                    logger.warning("[BrowserBind] 填卡号也失败: %s", e2)

            # 有效期
            if not _fill_with_poll('input[name="exp-date"]', f"{exp_month}/{exp_year[-2:]}", "有效期", max_rounds=3, wait=5000):
                try:
                    stripe_frame.fill('input[name="expiry"]', f"{exp_month}/{exp_year[-2:]}", timeout=5000)
                    logger.info("[BrowserBind] ✅ 有效期已填写 (expiry)")
                except Exception as e2:
                    logger.warning("[BrowserBind] 填有效期也失败: %s", e2)

            # CVC
            if not _fill_with_poll('input[name="cvc"]', cvc, "CVC", max_rounds=3, wait=5000):
                logger.warning("[BrowserBind] 填 CVC 失败")

            # 邮编（字段名因国家/布局而异：postal / zip / postalCode / postal_code）
            postal_selectors = [
                'input[name="postal"]', 'input[name="zip"]',
                'input[name="postalCode"]', 'input[name="postal_code"]',
                'input[name="zipcode"]',
                'input[autocomplete="postal-code"]', 'input[autocomplete="postal_code"]',
            ]
            postal_ok = False
            for _psel in postal_selectors:
                if _fill_with_poll(_psel, postal_code, f"邮编({_psel})", max_rounds=2, wait=4000):
                    postal_ok = True
                    break
                # 主 frame 没有时，搜索其它 frame（SG/US 布局可能拆分到单独 frame）
                for _pf in page.frames:
                    if _pf is stripe_frame:
                        continue
                    try:
                        if _pf.query_selector(_psel):
                            _pf.fill(_psel, postal_code, timeout=4000)
                            logger.info("[BrowserBind] ✅ 邮编已填写(其他frame) %s", _psel)
                            postal_ok = True
                            break
                    except Exception:
                        continue
                if postal_ok:
                    break
            if not postal_ok:
                logger.warning("[BrowserBind] 邮编填写失败（继续尝试 confirm，可能走 billing_details 地址）")

            page.wait_for_timeout(500)

            # 用 JS 触发 input/change 事件，确保 Stripe 内部状态感知卡信息（Playwright fill 有时不触发）
            js_fill_script = f"""
            (() => {{
                const frames = document.querySelectorAll('iframe');
                const setVal = (el, val) => {{
                    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                    setter.call(el, val);
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                }};
                let filled = 0;
                for (const f of frames) {{
                    try {{
                        const doc = f.contentDocument;
                        if (!doc) continue;
                        const inputs = doc.querySelectorAll('input');
                        for (const inp of inputs) {{
                            const n = (inp.name || '').toLowerCase();
                            const type = (inp.type || '').toLowerCase();
                            if (n === 'cardnumber' || n === 'cardnumber' || inp.autocomplete === 'cc-number') {{
                                setVal(inp, '{card_number}'); filled++;
                            }} else if (n === 'exp-date' || n === 'expiry') {{
                                setVal(inp, '{exp_month}/{exp_year[-2:]}'); filled++;
                            }} else if (n === 'cvc') {{
                                setVal(inp, '{cvc}'); filled++;
                            }} else if (n === 'postal' || n === 'zip' || n === 'postalCode' || n === 'postal_code' || n === 'zipcode') {{
                                setVal(inp, '{addr_zip}'); filled++;
                            }}
                        }}
                    }} catch (e) {{}}
                }}
                return {{ filled: filled }};
            }})();
            """
            try:
                js_fill_result = page.evaluate(js_fill_script)
                logger.info("[BrowserBind] JS 事件补发: %s", json.dumps(js_fill_result))
            except Exception as e:
                logger.warning("[BrowserBind] JS 事件补发异常: %s", e)

            page.wait_for_timeout(1500)

            # ── 两阶段 confirmCardSetup ──
            # 阶段 A: use_stripe_sdk=false 直接拿 API 结论（快，且无 hCaptcha 挑战）：
            #   成功 → 直接绑上；card_declined → 秒级报拒付码；requires_action → 回退阶段 B。
            # 阶段 B: SDK 挑战流程（use_stripe_sdk 默认），处理 3DS/hCaptcha，长等待。
            def _confirm_script(use_sdk: bool, timeout_ms: int) -> str:
                sdk_opt = "use_stripe_sdk: true" if use_sdk else "use_stripe_sdk: false"
                script = f"""
                (async () => {{
                    try {{
                        const stripe = window.__stripe;
                        const cardElement = window.__cardElement;
                        console.log("STRIPE: calling confirmCardSetup (use_stripe_sdk={'true' if use_sdk else 'false'})...");
                        const result = await stripe.confirmCardSetup('{client_secret}', {{
                            payment_method: {{
                                card: cardElement,
                                billing_details: {{
                                    name: '{card_name}',
                                    address: {{
                                        line1: '{addr_line1}',
                                        city: '{addr_city}',
                                        state: '{addr_state}',
                                        postal_code: '{addr_zip}',
                                        country: '{addr_country}',
                                    }},
                                }},
                            }},
                            {sdk_opt},
                        }});
                        if (result.error) {{
                            console.log("STRIPE: error=" + JSON.stringify(result.error));
                            return {{
                                error: result.error.message,
                                code: result.error.code,
                                decline: result.error.decline_code,
                            }};
                        }}
                        const si = result.setupIntent || {{}};
                        console.log("STRIPE: status=" + si.status + " next_action=" + JSON.stringify(si.next_action || null));
                        return {{
                            success: si.status === 'succeeded',
                            payment_method: si.payment_method,
                            status: si.status,
                            requires_action: si.status === 'requires_action',
                            next_action: si.next_action,
                        }};
                    }} catch (e) {{
                        return {{ error: e.message, stack: (e.stack || '').substring(0, 300) }};
                    }}
                }})()
                """
                wrapped = (
                    "Promise.race(["
                    + script.strip().rstrip().rstrip(";")
                    + f', new Promise(function(resolve) {{ setTimeout(function() {{ resolve({{error: "timeout_{timeout_ms // 1000}s", code: "timeout"}}); }}, {timeout_ms}); }})'
                    + "])"
                )
                return wrapped

            def _try_click_hcaptcha(timeout_ms: int = 30000) -> bool:
                """轮询 hCaptcha 复选框 frame 并点击（复选框型挑战点击即过）。

                策略（2026-08-06 实跑：裸 el.click() 被 hCaptcha 判机器人 "Please try again"）：
                  1. 拟人化鼠标移动（随机抖动分段轨迹）到复选框中心再点击；
                  2. 鼠标路径失败时改用键盘激活（聚焦 + Enter/Space，hCaptcha 支持无障碍键盘操作）；
                  3. 同一 frame 6s 冷却后再试，避免狂点加剧风控。
                """
                import time as _t
                import random as _r
                deadline = _t.monotonic() + timeout_ms / 1000.0
                acted = False
                selectors = ["#checkbox", "div[role='checkbox']", "a[role='checkbox']",
                             ".checkbox", "#checkbox-container", "button[aria-label*='checkbox' i]"]
                while _t.monotonic() < deadline:
                    acted = False
                    for f in page.frames:
                        furl = (f.url or "").lower()
                        if "hcaptcha" not in furl and "captcha" not in furl:
                            continue
                        last_click = getattr(f, "_hcaptcha_clicked_at", 0.0)
                        if last_click and _t.monotonic() - last_click < 6.0:
                            continue
                        for sel in selectors:
                            try:
                                el = f.query_selector(sel)
                                if not el:
                                    continue
                                try:
                                    box = el.bounding_box()
                                except Exception:
                                    box = None
                                if box:
                                    cx = box["x"] + box["width"] / 2
                                    cy = box["y"] + box["height"] / 2
                                    try:
                                        # 拟人化：从远处分几段移动，带随机抖动，最后点击
                                        page.mouse.move(cx - _r.randint(80, 180), cy + _r.randint(40, 120), steps=_r.randint(8, 16))
                                        page.wait_for_timeout(_r.randint(80, 220))
                                        page.mouse.move(cx + _r.randint(-30, 30), cy + _r.randint(-25, 25), steps=_r.randint(5, 10))
                                        page.wait_for_timeout(_r.randint(60, 180))
                                        page.mouse.move(cx, cy, steps=_r.randint(3, 7))
                                        page.wait_for_timeout(_r.randint(50, 150))
                                        page.mouse.click(cx, cy, delay=_r.randint(40, 120))
                                    except Exception:
                                        el.click(timeout=2000)
                                else:
                                    el.click(timeout=2000)
                                logger.info("[BrowserBind] 🖱 已点击 hCaptcha 复选框 %s (%s)", sel, furl[:90])
                                f._hcaptcha_clicked_at = _t.monotonic()
                                acted = True
                                break
                            except Exception as e2:
                                logger.warning("[BrowserBind] hCaptcha 点击失败 %s: %s", sel, e2)
                        if acted:
                            break
                    # 键盘激活兜底：鼠标路径未生效时聚焦复选框按 Enter/Space
                    if not acted:
                        for f in page.frames:
                            furl = (f.url or "").lower()
                            if "hcaptcha" not in furl and "captcha" not in furl:
                                continue
                            last_click = getattr(f, "_hcaptcha_clicked_at", 0.0)
                            if last_click and _t.monotonic() - last_click < 6.0:
                                continue
                            try:
                                el = f.query_selector("#checkbox")
                                if el:
                                    el.focus()
                                    page.keyboard.press("Enter")
                                    logger.info("[BrowserBind] ⌨️ 键盘激活 hCaptcha 复选框 (%s)", furl[:90])
                                    f._hcaptcha_clicked_at = _t.monotonic()
                                    acted = True
                                    break
                            except Exception:
                                continue
                    if acted:
                        return True
                    page.wait_for_timeout(800)
                return acted

            def _solve_hcaptcha_audio(timeout_ms: int = 60000) -> bool:
                """hCaptcha 音频挑战求解：切到音频通道→下载→whisper 识别→填答案提交。

                2026-08-06 实跑：复选框点击被机器判定拒绝后升级为图片/拖拽挑战，
                hCaptcha 挑战框保留「音频挑战」无障碍入口。本函数：
                  1. 定位 challenge frame（含 Please try again/Skip/One more step 文本）；
                  2. 点击音频按钮（aria-label/class 含 audio，含耳机图标）；
                  3. 从网络响应捕获音频 URL（_audio_urls）或 blob 转 base64；
                  4. faster-whisper 转写 → 归一化答案；
                  5. 填入输入框并点提交。
                """
                import time as _t
                import base64 as _b64
                try:
                    from core.hcaptcha_audio import (
                        transcribe_hcaptcha_audio,
                        normalize_hcaptcha_answer,
                        download_audio,
                    )
                except Exception as e:
                    logger.warning("[BrowserBind] hCaptcha 音频求解依赖缺失: %s", e)
                    return False
                deadline = _t.monotonic() + timeout_ms / 1000.0
                # 1) 定位 challenge frame
                chal = None
                for f in page.frames:
                    furl = (f.url or "").lower()
                    if "hcaptcha" not in furl and "captcha" not in furl:
                        continue
                    try:
                        txt = (f.evaluate("document.body ? document.body.innerText : ''") or "")
                    except Exception:
                        txt = ""
                    tl = " ".join(txt.split()).lower()[:160]
                    if "try again" in tl or "skip" in tl or "one more step" in tl or "verify" in tl:
                        chal = f
                        logger.info("[BrowserBind] hCaptcha challenge frame: %s text=%s", furl[:100], tl[:80])
                        break
                if chal is None:
                    logger.warning("[BrowserBind] hCaptcha challenge frame 未定位")
                    return False
                # 2) 点击音频按钮
                audio_btns = [
                    "button[aria-label*='audio' i]", "button[title*='audio' i]",
                    ".button-audio", "#audio-button", "button[class*='audio' i]",
                    "button[aria-label*='headphone' i]", "button[aria-label*='listen' i]",
                ]
                clicked = False
                for sel in audio_btns:
                    try:
                        el = chal.query_selector(sel)
                        if el and el.is_visible():
                            el.click(timeout=2000)
                            logger.info("[BrowserBind] 已点击 hCaptcha 音频按钮 %s", sel)
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    # 兜底：遍历 challenge frame 内所有 button，找 aria-label/类名含 audio 的
                    try:
                        for btn in chal.query_selector_all("button"):
                            try:
                                al = (btn.get_attribute("aria-label") or "").lower()
                                cl = (btn.get_attribute("class") or "").lower()
                                ti = (btn.get_attribute("title") or "").lower()
                                if "audio" in al or "audio" in cl or "audio" in ti or "headphone" in al:
                                    btn.click(timeout=2000)
                                    logger.info("[BrowserBind] 已点击 hCaptcha 音频按钮(兜底): %s", al or cl or ti)
                                    clicked = True
                                    break
                            except Exception:
                                continue
                    except Exception:
                        pass
                if not clicked:
                    logger.warning("[BrowserBind] hCaptcha 音频按钮未找到")
                    try:
                        _dom = chal.evaluate("document.documentElement ? document.documentElement.outerHTML : ''") or ""
                        _dom_path = "/tmp/hcaptcha_chal_dom.html"
                        with open(_dom_path, "w", encoding="utf-8") as _f:
                            _f.write(_dom)
                        logger.info("[BrowserBind] 挑战 frame DOM 已 dump: %s (%d bytes)", _dom_path, len(_dom))
                    except Exception as _de:
                        logger.warning("[BrowserBind] 挑战 DOM dump 失败: %s", _de)
                    return False
                # 3) 捕获音频 URL（点击后可能自动播放；也可能需要点播放按钮）
                page.wait_for_timeout(1200)
                try:
                    play_btn = chal.query_selector("button[aria-label*='play' i], .button-play, button[title*='play' i]")
                    if play_btn and play_btn.is_visible():
                        play_btn.click(timeout=2000)
                        logger.info("[BrowserBind] 已点击 hCaptcha 播放按钮")
                except Exception:
                    pass
                audio_url = ""
                base_before = len(_audio_urls)
                while _t.monotonic() < deadline:
                    if len(_audio_urls) > base_before:
                        audio_url = _audio_urls[-1]
                        break
                    # blob URL 场景：frame 内把 audio 元素 src 转 base64
                    try:
                        blob = chal.evaluate("""async () => {
                            const a = document.querySelector('audio');
                            if (!a) return '';
                            const src = a.currentSrc || a.src || '';
                            if (!src) return '';
                            try {
                                const r = await fetch(src);
                                const buf = await r.arrayBuffer();
                                const bytes = new Uint8Array(buf);
                                let bin = '';
                                for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
                                return 'data:audio/mpeg;base64,' + btoa(bin);
                            } catch (e) { return ''; }
                        }""")
                        if blob and str(blob).startswith("data:audio"):
                            audio_url = str(blob)
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(1000)
                if not audio_url:
                    logger.warning("[BrowserBind] hCaptcha 音频 URL 未捕获")
                    return False
                # 4) 下载/解码 → 转写
                audio_path = f"/tmp/hcaptcha_audio_{int(_t.monotonic())}.mp3"
                if audio_url.startswith("data:"):
                    try:
                        hdr, _, b64 = audio_url.partition(",")
                        raw = _b64.b64decode(b64)
                        with open(audio_path, "wb") as f:
                            f.write(raw)
                        ok = True
                    except Exception as e:
                        logger.warning("[BrowserBind] 音频 base64 解码失败: %s", e)
                        ok = False
                else:
                    ok = download_audio(audio_url, audio_path)
                if not ok:
                    logger.warning("[BrowserBind] hCaptcha 音频获取失败")
                    return False
                text = transcribe_hcaptcha_audio(audio_path)
                answer = normalize_hcaptcha_answer(text)
                logger.info("[BrowserBind] hCaptcha 音频转写: %r → 答案 %r", text[:80], answer)
                if not answer:
                    return False
                # 5) 填答案 + 提交
                inputs = ["input[type='text']", "#answer", ".answer-input", "input[placeholder*='answer' i]"]
                filled = False
                for sel in inputs:
                    try:
                        el = chal.query_selector(sel)
                        if el and el.is_visible():
                            el.fill(answer)
                            filled = True
                            break
                    except Exception:
                        continue
                if not filled:
                    logger.warning("[BrowserBind] hCaptcha 音频输入框未找到")
                    return False
                submits = ["button[type='submit']", ".button-submit", "#submit", "button[aria-label*='submit' i]", "button[class*='submit' i]"]
                for sel in submits:
                    try:
                        el = chal.query_selector(sel)
                        if el and el.is_visible():
                            el.click(timeout=2000)
                            logger.info("[BrowserBind] hCaptcha 音频答案已提交: %s", answer)
                            return True
                    except Exception:
                        continue
                try:
                    chal.query_selector("input[type='text']").press("Enter")
                    logger.info("[BrowserBind] hCaptcha 音频答案已提交(Enter): %s", answer)
                    return True
                except Exception:
                    logger.warning("[BrowserBind] hCaptcha 音频提交按钮未找到")
                    return False

            def _solve_hcaptcha_image(timeout_ms: int = 90000) -> bool:
                """hCaptcha 图片挑战求解：提取网格图 → CLIP 服务 → 点击格子 → Verify。

                2026-08-06 实跑确认：新版 hCaptcha（v1/9175be...）无障碍菜单已移除
                音频通道（只有 Accessibility Cookie/Report Image/Report Bug/Info），
                图片挑战是唯一通道。本函数：
                  1. 定位 challenge frame（含 choose all/select all/click on/crosses 文本）；
                  2. 提取任务文本（去掉 "Please try again" 等噪声）；
                  3. 收集网格图片（img 元素 / background-image，过滤 logo/图标/重复）；
                  4. 调本地 CLIP 服务（HCAPTCHA_CLIP_URL）选应点击的格子；
                  5. 点击选中格子 → 点 Verify Answers。
                """
                import time as _t
                import urllib.request as _ur
                import json as _json
                clip_url = os.environ.get("HCAPTCHA_CLIP_URL", "http://127.0.0.1:8766/solve")
                deadline = _t.monotonic() + timeout_ms / 1000.0

                # ---- DOM 直取网格求解（优先于截图，2026-08-06 实跑）----
                # 新版 hCaptcha 网格 = .task-image 120x120 单元格，图片异步加载到
                # 单元格内 .image 的 background-image；截图会被防截图反制（iframe
                # 渲染在视口外 y≈-9942 / 内容位移）干扰，直接读 DOM 更稳。
                _DOM_GRID_EXTRACT_JS = r"""() => {
                    const out = [];
                    document.querySelectorAll('.task-image').forEach((c) => {
                        const img = c.querySelector('.image');
                        let url = '';
                        if (img) {
                            // 内联样式是 background 简写（非 background-image），
                            // computed style 可能在图片异步加载后才有值 → 多层回退
                            const raw = (getComputedStyle(img).backgroundImage ||
                                         img.style.backgroundImage ||
                                         img.style.background ||
                                         img.getAttribute('style') || '');
                            const m = raw.match(/url\(["']?([^"')]+)["']?\)/);
                            if (m) url = m[1];
                        }
                        const badge = c.querySelector('.badge');
                        let selected = false;
                        if (badge) {
                            const bs = getComputedStyle(badge);
                            selected = parseFloat(bs.opacity || '0') > 0.1;
                        }
                        const r = c.getBoundingClientRect();
                        out.push({
                            left: r.left, top: r.top, width: r.width, height: r.height,
                            selected: selected,
                            url: url,
                        });
                    });
                    return out;
                }"""

                # 尺寸挑战提取：9 格 + 参考图（非格 background/img，优先 task/prompt/sample 类）
                _SIZE_EXTRACT_JS = r"""() => {
                    const cellUrls = [];
                    const cells = [];
                    document.querySelectorAll('.task-image').forEach((c) => {
                        const img = c.querySelector('.image');
                        let url = '';
                        if (img) {
                            const raw = (getComputedStyle(img).backgroundImage ||
                                         img.style.backgroundImage || img.style.background ||
                                         img.getAttribute('style') || '');
                            const m = raw.match(/url\(["']?([^"')]+)["']?\)/);
                            if (m) url = m[1];
                        }
                        if (url) cellUrls.push(url);
                        const r = c.getBoundingClientRect();
                        cells.push({left: r.left, top: r.top, width: r.width, height: r.height, url: url});
                    });
                    let refUrl = '';
                    // 参考图：class="image" 但不在 .task-image 内的元素（run25 实测：
                    // 参考图 = 头部 <div class="image">，背景来自 CSS 类而非内联样式，
                    // 通用 inline-style 扫描扫不到）
                    const refEls = Array.from(document.querySelectorAll('.image')).filter(
                        el => !el.closest('.task-image'));
                    for (const el of refEls) {
                        let raw = '';
                        try { raw = getComputedStyle(el).backgroundImage || ''; } catch (e) {}
                        raw = raw || el.style.backgroundImage || el.style.background ||
                              (el.getAttribute ? (el.getAttribute('style') || '') : '');
                        const m = raw.match(/url\(["']?([^"')]+)["']?\)/);
                        if (m && m[1] && m[1].length > 15 && !/logo|avatar|icon|menu|close|refresh|check|hcaptcha|loading|spinner/i.test(m[1])) {
                            refUrl = m[1];
                            break;
                        }
                    }
                    // 兜底候选：背景图 / img src / canvas dataURL（含与格重复）
                    const cands = [];
                    const seen = new Set();
                    const grab = (src, cls, el) => {
                        if (!src || src.length < 15 || seen.has(src)) return;
                        if (/logo|avatar|icon|menu|close|refresh|check|hcaptcha|loading|spinner/i.test(src)) return;
                        seen.add(src);
                        const r = el.getBoundingClientRect();
                        cands.push({src: src, cls: String(cls || ''), w: r.width, h: r.height});
                    };
                    document.querySelectorAll('[style*="background"], [style*="background-image"], img').forEach((el) => {
                        let raw = '';
                        try { raw = getComputedStyle(el).backgroundImage || ''; } catch (e) {}
                        raw = raw || el.style.backgroundImage || el.style.background ||
                              (el.getAttribute ? (el.getAttribute('style') || '') : '') ||
                              (el.src || '');
                        const m = raw.match(/url\(["']?([^"')]+)["']?\)/);
                        let cls = '';
                        try { cls = (el.className && el.className.baseVal !== undefined) ? el.className.baseVal : (el.className || ''); } catch (e) {}
                        grab(m ? m[1] : (el.src || ''), cls, el);
                    });
                    document.querySelectorAll('canvas').forEach((cv) => {
                        try {
                            const u = cv.toDataURL('image/jpeg', 0.8);
                            if (u && u.length > 1000) grab(u, cv.className || '', cv);
                        } catch (e) {}
                    });
                    cands.sort((a, b) => {
                        const ka = /task|prompt|example|sample|ref|shown|challenge/i.test(a.cls) ? 1 : 0;
                        const kb = /task|prompt|example|sample|ref|shown|challenge/i.test(b.cls) ? 1 : 0;
                        if (ka !== kb) return kb - ka;
                        return (b.w * b.h) - (a.w * a.h);
                    });
                    if (!refUrl && cands.length) refUrl = cands[0].src;
                    return {cells: cells, refUrl: refUrl, refCands: cands};
                }"""

                def _relocate_challenge_iframe() -> bool:
                    """把挑战 iframe 元素固定定位到可见区域并放大到内容尺寸。"""
                    for _f in page.frames:
                        _fu = (_f.url or "").lower()
                        if "hcaptcha" not in _fu and "captcha" not in _fu:
                            continue
                        try:
                            _ok = _f.evaluate("""() => {
                                const f = document.querySelector('iframe[src*="frame=challenge"]');
                                if (!f) return false;
                                const s = document.createElement('style');
                                s.textContent = '*{animation:none!important;transition:none!important}';
                                document.head.appendChild(s);
                                f.style.position = 'fixed';
                                f.style.top = '40px';
                                f.style.left = '10px';
                                f.style.width = '420px';
                                f.style.height = '640px';
                                f.style.zIndex = '2147483647';
                                f.style.background = '#fff';
                                return true;
                            }""")
                            if _ok:
                                return True
                        except Exception:
                            continue
                    return False

                def _find_challenge_iframe_box():
                    """遍历所有 frame，找包含 frame=challenge iframe 元素的 frame
                    （Stripe 集成里它嵌套在 hcaptcha-inner 内，主页面 querySelector
                    找不到），用其 locator.bounding_box() 换算到页面坐标。
                    实跑 bug(2026-08-06): 旧代码引用了未定义的 inner_frame → NameError
                    被 except 吞掉后回退 page.locator 也查不到 → bbox 恒无效。"""
                    for _f in page.frames:
                        try:
                            _bb = _f.locator('iframe[src*="frame=challenge"]').bounding_box()
                        except Exception:
                            _bb = None
                        if _bb and _bb.get("width", 0) > 20 and _bb.get("height", 0) > 20:
                            return _bb
                    return None

                def _frame_viewport_offset(frame):
                    """计算 frame 视口原点在主页面坐标中的偏移。

                    纯 JS getBoundingClientRect 链式累加（子 iframe 元素在其父文档的
                    rect + 递归父偏移），避免 Playwright bounding_box() 在 hCaptcha
                    布局抖动时的分钟级阻塞（run22 实测 bbox 耗时 210.6s）。
                    """
                    _ox = 0.0
                    _oy = 0.0
                    _f = frame
                    while _f is not None and _f.parent_frame is not None:
                        _p = _f.parent_frame
                        _base = (_f.url or "").split("#")[0]
                        try:
                            _r = _p.evaluate("""(base) => {
                                for (const e of Array.from(document.querySelectorAll('iframe'))) {
                                    let s = '';
                                    try { s = e.src || e.getAttribute('src') || ''; } catch (err) { s = ''; }
                                    if (s && s.split('#')[0] === base) {
                                        const r = e.getBoundingClientRect();
                                        return {x: r.left, y: r.top, w: r.width, h: r.height};
                                    }
                                }
                                return null;
                            }""", _base)
                            if _r and _r.get("w"):
                                _ox += float(_r["x"])
                                _oy += float(_r["y"])
                            else:
                                # src 精确匹配失败：按 URL 前缀兜底
                                _prefix = _base[:80]
                                _r2 = _p.evaluate("""(pfx) => {
                                    for (const e of Array.from(document.querySelectorAll('iframe'))) {
                                        let s = '';
                                        try { s = e.src || e.getAttribute('src') || ''; } catch (err) { s = ''; }
                                        if (s && s.indexOf(pfx) === 0) {
                                            const r = e.getBoundingClientRect();
                                            return {x: r.left, y: r.top, w: r.width, h: r.height};
                                        }
                                    }
                                    return null;
                                }""", _prefix)
                                if _r2 and _r2.get("w"):
                                    _ox += float(_r2["x"])
                                    _oy += float(_r2["y"])
                        except Exception:
                            pass
                        _f = _p
                    return _ox, _oy

                def _try_dom_grid(timeout_ms: int = 15000) -> bool:
                    """DOM 取格 → CLIP 选格 → 重定位 iframe → 真实鼠标点击。"""
                    import time as _tt
                    _end = _tt.monotonic() + timeout_ms / 1000.0
                    # 禁用动画 + 滚动到顶部，保证 rect 稳定
                    try:
                        chal.evaluate("""() => {
                            const s = document.createElement('style');
                            s.textContent = '*{animation:none!important;transition:none!important}';
                            document.head.appendChild(s);
                            window.scrollTo(0, 0);
                        }""")
                    except Exception:
                        pass
                    cells = []
                    while _tt.monotonic() < _end:
                        try:
                            cells = chal.evaluate(_DOM_GRID_EXTRACT_JS)
                        except Exception:
                            cells = []
                        if cells and sum(1 for c in cells if c.get("url")) >= 2:
                            break
                        page.wait_for_timeout(700)
                    if not cells or len(cells) < 2:
                        logger.warning("[BrowserBind] DOM 网格: 单元格不足 (%d)", len(cells))
                        return False
                    logger.info("[BrowserBind] DOM 网格: %d 格 (已选 %d)",
                                len(cells), sum(1 for c in cells if c.get("selected")))
                    unselected = [c for c in cells if not c.get("selected")]
                    if not unselected:
                        unselected = cells
                    # CLIP 选格（data-uri/url 直传）
                    payload = _json.dumps({
                        "task": task,
                        "images": [c["url"] for c in unselected],
                    }).encode("utf-8")
                    picked = []
                    try:
                        req = _ur.Request(clip_url, data=payload, headers={"Content-Type": "application/json"})
                        with _ur.urlopen(req, timeout=60) as resp:
                            r = _json.loads(resp.read().decode("utf-8"))
                        picked = [int(i) for i in r.get("indices", [])]
                        logger.info("[BrowserBind] DOM CLIP 选中格子: %s (elapsed=%s)",
                                    picked, r.get("elapsed"))
                    except Exception as e:
                        logger.warning("[BrowserBind] DOM CLIP 服务调用失败: %s", e)
                        return False
                    if not picked:
                        logger.warning("[BrowserBind] DOM CLIP 未选出格子")
                        return False
                    # 重定位 iframe 到可见区域（计时定位卡点 2026-08-06 run20 实测
                    # CLIP 返回后到首格点击间隔 3.5min）
                    _t_clip_done = _tt.monotonic()
                    _relocate_challenge_iframe()
                    logger.info("[BrowserBind] DOM 重定位耗时 %.1fs", _tt.monotonic() - _t_clip_done)
                    page.wait_for_timeout(500)
                    # 重测单元格 rect（布局可能因 iframe 放大而变化）
                    try:
                        cells2 = chal.evaluate(_DOM_GRID_EXTRACT_JS)
                        if cells2 and len(cells2) == len(cells):
                            # 保留已选状态与 url，rect 用新值
                            for _a, _b in zip(cells2, cells):
                                _b["left"] = _a["left"]
                                _b["top"] = _a["top"]
                                _b["width"] = _a["width"]
                                _b["height"] = _a["height"]
                    except Exception:
                        pass
                    logger.info("[BrowserBind] DOM cells2 重测耗时 %.1fs",
                                _tt.monotonic() - _t_clip_done)
                    _ox, _oy = _frame_viewport_offset(chal)
                    if not (_ox or _oy):
                        # 兜底：bbox（慢但可用）
                        chal_box = _find_challenge_iframe_box()
                        if chal_box is None or chal_box["width"] <= 0:
                            logger.warning("[BrowserBind] DOM 求解: 挑战 iframe 偏移/bbox 均无效")
                            return False
                        _ox, _oy = chal_box["x"], chal_box["y"]
                    logger.info("[BrowserBind] DOM 帧偏移 (%.1f,%.1f) 耗时 %.1fs",
                                _ox, _oy, _tt.monotonic() - _t_clip_done)
                    # 真实鼠标点击（拟人化轨迹）
                    clicked = 0
                    for _idx in picked:
                        if _idx >= len(unselected):
                            continue
                        _c = unselected[_idx]
                        _px = _ox + _c["left"] + _c["width"] / 2
                        _py = _oy + _c["top"] + _c["height"] / 2
                        _t_click0 = _tt.monotonic()
                        try:
                            page.mouse.move(_px - 60, _py + 30, steps=8)
                            page.wait_for_timeout(120)
                            page.mouse.move(_px, _py, steps=4)
                            page.wait_for_timeout(80)
                            page.mouse.click(_px, _py, delay=100)
                            clicked += 1
                            logger.info("[BrowserBind] DOM 已点击格子 %d (%d,%d) 耗时 %.1fs",
                                        _idx, int(_px), int(_py), _tt.monotonic() - _t_click0)
                        except Exception as _e2:
                            logger.warning("[BrowserBind] DOM 格子 %d 点击失败: %s", _idx, _e2)
                        page.wait_for_timeout(500)
                    if clicked == 0:
                        return False
                    # Verify 按钮（新题通常点满即自动提交）
                    try:
                        for _sel in [".button-submit", "button[type='submit']", "#submit",
                                     "[aria-label*='submit' i]", "[aria-label*='verify' i]"]:
                            _el = chal.query_selector(_sel)
                            if _el and _el.is_visible():
                                _el.click(timeout=2000)
                                logger.info("[BrowserBind] DOM 答案已提交")
                                break
                    except Exception:
                        pass
                    return True

                def _try_size_grid(timeout_ms: int = 20000) -> bool:
                    """尺寸类挑战（Pick all objects smaller/larger than the one shown）求解。

                    hCaptcha 尺寸任务：挑战区顶部有参考图（the one shown），9 格各一张
                    物体图。CLIP 的 small/large softmax 完全无区分度（实测全 >0.87），
                    改用前景像素占比对比：下载参考图与 9 格图，统计非白像素占比，
                    smaller → 占比显著低于参考；larger → 显著高于参考。
                    同时把挑战 DOM 与图片 dump 到 /tmp/hcap_size/ 便于人工校准。
                    """
                    import time as _tt
                    import io as _io
                    from PIL import Image as _PILImage
                    _end = _tt.monotonic() + timeout_ms / 1000.0
                    try:
                        chal.evaluate("""() => {
                            const s = document.createElement('style');
                            s.textContent = '*{animation:none!important;transition:none!important}';
                            document.head.appendChild(s);
                            window.scrollTo(0, 0);
                        }""")
                    except Exception:
                        pass
                    info = None
                    while _tt.monotonic() < _end:
                        try:
                            info = chal.evaluate(_SIZE_EXTRACT_JS)
                        except Exception:
                            info = None
                        if info and info.get("cells") and len(info["cells"]) >= 2:
                            break
                        page.wait_for_timeout(700)
                    if not info or not info.get("cells") or len(info["cells"]) < 2:
                        logger.warning("[BrowserBind] 尺寸求解: 单元格不足 cells=%s",
                                       len((info or {}).get("cells") or []))
                        return False
                    # dump 现场（离线校准用）：必须在参考图判断之前，参考图缺失也要能拿 DOM
                    try:
                        _dom = chal.evaluate("document.documentElement ? document.documentElement.outerHTML : ''") or ""
                        import os as _os
                        import json as _json2
                        _os.makedirs("/tmp/hcap_size", exist_ok=True)
                        with open("/tmp/hcap_size/challenge_dom.html", "w", encoding="utf-8") as _f:
                            _f.write(_dom)
                        with open("/tmp/hcap_size/ref_candidates.json", "w", encoding="utf-8") as _f:
                            _f.write(_json2.dumps(info.get("refCands") or [], ensure_ascii=False, indent=1))
                        for _i, _c in enumerate(info["cells"]):
                            if _c.get("url"):
                                try:
                                    _ur.urlretrieve(_c["url"], f"/tmp/hcap_size/cell{_i}.jpg")
                                except Exception:
                                    pass
                        if info.get("refUrl"):
                            try:
                                _ur.urlretrieve(info["refUrl"], "/tmp/hcap_size/ref.jpg")
                            except Exception:
                                pass
                        logger.info("[BrowserBind] 尺寸挑战已 dump: /tmp/hcap_size/ (%d 格, 参考=%s, 候选=%d)",
                                    len(info["cells"]), bool(info.get("refUrl")),
                                    len(info.get("refCands") or []))
                    except Exception as _de:
                        logger.warning("[BrowserBind] 尺寸挑战 dump 失败: %s", _de)
                    # 参考图：优先 JS 选出的 refUrl，否则在候选里挑（非格、面积最大）
                    if not info.get("refUrl"):
                        _cands = info.get("refCands") or []
                        _cand_urls = set(c.get("url") for c in info["cells"])
                        _pool = [c for c in _cands if c.get("src") and c.get("src") not in _cand_urls]
                        if not _pool:
                            _pool = _cands
                        _pool.sort(key=lambda c: (c.get("w") or 0) * (c.get("h") or 0), reverse=True)
                        if _pool:
                            info["refUrl"] = _pool[0].get("src")
                            try:
                                _ur.urlretrieve(info["refUrl"], "/tmp/hcap_size/ref.jpg")
                            except Exception:
                                pass
                            logger.info("[BrowserBind] 尺寸求解: JS 未定参考图，Python 端候选补齐 ref=%s",
                                        str(info["refUrl"])[:80])
                    if not info.get("refUrl"):
                        logger.warning("[BrowserBind] 尺寸求解: 参考图仍未找到 (候选 %d)",
                                       len(info.get("refCands") or []))
                        return False
                    # 尺寸求解主通道：CLIP 分类 + 典型尺寸表（/solve_size）
                    # （run25 实测像素占比法无区分度：驴/墨镜/牛都只占格子 ~1-15%）
                    _picked_idx = []
                    _size_req = _json.dumps({
                        "task": task,
                        "ref": info["refUrl"],
                        "images": [c.get("url") for c in info["cells"]],
                    }).encode("utf-8")
                    try:
                        _sreq = _ur.Request(clip_url.replace("/solve", "/solve_size"),
                                            data=_size_req,
                                            headers={"Content-Type": "application/json"})
                        with _ur.urlopen(_sreq, timeout=60) as _resp:
                            _sr = _json.loads(_resp.read().decode("utf-8"))
                        _picked_idx = [int(i) for i in _sr.get("indices", [])]
                        logger.info("[BrowserBind] 尺寸 CLIP 选中格子: %s ref=%s elapsed=%s",
                                    _picked_idx, _sr.get("ref"), _sr.get("elapsed"))
                    except Exception as _se:
                        logger.warning("[BrowserBind] 尺寸 CLIP 调用失败: %s", _se)
                    if not _picked_idx:
                        # 兜底：前景像素占比（弱信号）
                        def _fg_fraction(_p: str) -> float:
                            try:
                                with open(_p, "rb") as _fh:
                                    _im = _PILImage.open(_io.BytesIO(_fh.read())).convert("RGB")
                                import numpy as _np
                                _a = _np.asarray(_im).astype(int)
                                _bg = (_a[:, :, 0] > 235) & (_a[:, :, 1] > 235) & (_a[:, :, 2] > 235)
                                return float(1.0 - _bg.mean())
                            except Exception:
                                return 0.0
                        _ref_frac = 0.0
                        try:
                            _ref_frac = _fg_fraction("/tmp/hcap_size/ref.jpg")
                        except Exception:
                            pass
                        _fracs = []
                        for _i, _c in enumerate(info["cells"]):
                            if _c.get("url"):
                                _fracs.append((_i, _fg_fraction(f"/tmp/hcap_size/cell{_i}.jpg")))
                        logger.info("[BrowserBind] 尺寸兜底: 参考占比=%.3f 格子占比=%s",
                                    _ref_frac, [(i, round(f, 3)) for i, f in _fracs])
                        if not _fracs or _ref_frac <= 0.01:
                            logger.warning("[BrowserBind] 尺寸求解: 参考图占比异常 ref=%.3f", _ref_frac)
                            return False
                        _tl = (task or "").lower()
                        _want_smaller = any(k in _tl for k in ("smaller", "smallest"))
                        for _i, _f in _fracs:
                            if _want_smaller and _f < _ref_frac * 0.85:
                                _picked_idx.append(_i)
                            elif (not _want_smaller) and _f > _ref_frac * 1.15:
                                _picked_idx.append(_i)
                        if not _picked_idx and _fracs:
                            _extreme = min(_fracs, key=lambda x: x[1]) if _want_smaller else max(_fracs, key=lambda x: x[1])
                            _picked_idx = [_extreme[0]]
                            logger.info("[BrowserBind] 尺寸兜底: 无显著满足项，取最极端格 %d", _extreme[0])
                    logger.info("[BrowserBind] 尺寸求解选中格子: %s", _picked_idx)
                    if not _picked_idx:
                        return False
                    # 复用重定位 + 帧偏移 + 点击
                    _relocate_challenge_iframe()
                    page.wait_for_timeout(500)
                    try:
                        cells2 = chal.evaluate(_DOM_GRID_EXTRACT_JS)
                        if cells2 and len(cells2) == len(info["cells"]):
                            for _a, _b in zip(cells2, info["cells"]):
                                _b["left"] = _a["left"]
                                _b["top"] = _a["top"]
                                _b["width"] = _a["width"]
                                _b["height"] = _a["height"]
                    except Exception:
                        pass
                    _ox, _oy = _frame_viewport_offset(chal)
                    if not (_ox or _oy):
                        chal_box = _find_challenge_iframe_box()
                        if chal_box is None or chal_box["width"] <= 0:
                            logger.warning("[BrowserBind] 尺寸求解: 挑战 iframe 偏移/bbox 均无效")
                            return False
                        _ox, _oy = chal_box["x"], chal_box["y"]
                    _cells_map = {i: c for i, c in enumerate(info["cells"])}
                    clicked = 0
                    for _i in _picked_idx:
                        _c = _cells_map.get(_i)
                        if not _c:
                            continue
                        _px = _ox + _c["left"] + _c["width"] / 2
                        _py = _oy + _c["top"] + _c["height"] / 2
                        try:
                            page.mouse.move(_px - 60, _py + 30, steps=8)
                            page.wait_for_timeout(120)
                            page.mouse.move(_px, _py, steps=4)
                            page.wait_for_timeout(80)
                            page.mouse.click(_px, _py, delay=100)
                            clicked += 1
                            logger.info("[BrowserBind] 尺寸已点击格子 %d (%d,%d)", _i, int(_px), int(_py))
                        except Exception as _e2:
                            logger.warning("[BrowserBind] 尺寸格子 %d 点击失败: %s", _i, _e2)
                        page.wait_for_timeout(500)
                    if clicked == 0:
                        return False
                    try:
                        for _sel in [".button-submit", "button[type='submit']", "#submit",
                                     "[aria-label*='submit' i]", "[aria-label*='verify' i]"]:
                            _el = chal.query_selector(_sel)
                            if _el and _el.is_visible():
                                _el.click(timeout=2000)
                                logger.info("[BrowserBind] 尺寸答案已提交")
                                break
                    except Exception:
                        pass
                    return True

                def _solve_hcaptcha_image_visual(chal, task, timeout_ms=60000) -> bool:
                    """纯视觉求解：截挑战区域 → 检测网格 → CLIP 选格 → 点击。

                    新版 hCaptcha 图片挑战的网格图不在 <img>/canvas 元素里可枚举，
                    因此对挑战区域整块截图，用 OpenCV 检测网格线定位格子。
                    Stripe 集成里挑战 iframe 嵌套在 js.stripe.com hcaptcha-inner 全屏
                    容器内，主页面 querySelectorAll 看不到 → 需进入 inner frame 找。
                    """
                    import io as _io
                    from PIL import Image as _PILImage
                    from core.hcaptcha_grid import detect_grid_cells
                    _d2 = _t.monotonic() + timeout_ms / 1000.0
                    # 1) 找挑战 iframe（先进入 Stripe hcaptcha-inner 容器）。
                    #    实跑教训(2026-08-06): 手动 getBoundingClientRect 取到的是
                    #    inner frame 相对坐标，且挑战布局异步渲染时 w/h=0 →
                    #    page.screenshot(clip) 报 "Clipped area is either empty or
                    #    outside the resulting image"。改用 Locator.bounding_box()
                    #    （Playwright 自动换算到主页面坐标）+ element 截图兜底。
                    chal_box = None
                    inner_frame = None
                    for _f in page.frames:
                        _fu = (_f.url or "").lower()
                        if "hcaptcha-inner" in _fu or "hcaptchainvisible" in _fu:
                            inner_frame = _f
                            break
                    _loc = None
                    if inner_frame is not None:
                        try:
                            _loc = inner_frame.locator('iframe[src*="frame=challenge"]')
                        except Exception:
                            _loc = None
                    if _loc is None:
                        try:
                            _loc = page.locator('iframe[src*="frame=challenge"]')
                        except Exception:
                            _loc = None
                    # 挑战 iframe 可能被渲染在视口外（实跑 y≈-9942，防截图反制）→
                    # 先禁用动画 + scrollIntoView 拉到可见区域，再截图。
                    try:
                        if inner_frame is not None:
                            inner_frame.evaluate("""() => {
                                const s = document.createElement('style');
                                s.textContent = '*{animation:none!important;transition:none!important}';
                                document.head.appendChild(s);
                                const f = Array.from(document.querySelectorAll('iframe')).find(
                                    x => (x.src || '').includes('frame=challenge'));
                                if (f) {
                                    f.scrollIntoView({block: 'center', inline: 'center'});
                                    const r = f.getBoundingClientRect();
                                    if (r.top < 0 || r.bottom > window.innerHeight || r.width < 20) {
                                        f.style.position = 'fixed';
                                        f.style.top = '90px';
                                        f.style.left = Math.max(0, (window.innerWidth - (f.offsetWidth || 300)) / 2) + 'px';
                                        f.style.zIndex = '2147483647';
                                    }
                                }
                            }""")
                        else:
                            page.evaluate("window.scrollTo(0, 0)")
                    except Exception as _sce:
                        logger.warning("[BrowserBind] 视觉求解 挑战滚动失败: %s", _sce)
                    page.wait_for_timeout(600)
                    if _loc is not None:
                        try:
                            _loc.scroll_into_view_if_needed(timeout=5000)
                        except Exception:
                            pass
                    # 轮询等待布局完成（w/h>0 且可见）
                    _poll_end = _t.monotonic() + 6.0
                    while _t.monotonic() < _poll_end:
                        try:
                            chal_box = _loc.bounding_box() if _loc is not None else None
                        except Exception:
                            chal_box = None
                        if chal_box and chal_box["width"] > 20 and chal_box["height"] > 20:
                            break
                        page.wait_for_timeout(500)
                        chal_box = None
                    if chal_box is None:
                        # 兜底：遍历各 frame 找 frame=challenge iframe（非 Stripe 集成场景）
                        for _f in page.frames:
                            _fu = (_f.url or "").lower()
                            if "hcaptcha" not in _fu and "captcha" not in _fu:
                                continue
                            try:
                                _bb = _f.locator('iframe[src*="frame=challenge"]').bounding_box()
                                if _bb and _bb["width"] > 20 and _bb["height"] > 20:
                                    chal_box = _bb
                                    break
                            except Exception:
                                continue
                    if chal_box is None:
                        logger.warning("[BrowserBind] 视觉求解: 未找到挑战 iframe")
                        return False
                    logger.info("[BrowserBind] 视觉求解 挑战区域 bbox: %s",
                                json.dumps(chal_box, ensure_ascii=False))
                    # 2) 截图挑战区域（page clip 优先，失败退 element 截图）
                    shot = None
                    try:
                        shot = page.screenshot(clip={"x": chal_box["x"], "y": chal_box["y"],
                                                    "width": chal_box["width"],
                                                    "height": chal_box["height"]})
                        logger.info("[BrowserBind] 视觉求解 page clip 截图成功 (%d bytes)", len(shot))
                    except Exception as _se:
                        logger.warning("[BrowserBind] 视觉求解 page clip 截图失败: %s", _se)
                    if shot is None and _loc is not None:
                        try:
                            shot = _loc.screenshot(timeout=15000)
                            logger.info("[BrowserBind] 视觉求解 element 截图成功 (%d bytes)", len(shot))
                        except Exception as _se2:
                            logger.warning("[BrowserBind] 视觉求解 element 截图失败: %s", _se2)
                    if shot is None:
                        return False
                    try:
                        with open("/tmp/hcaptcha_chal_visual.png", "wb") as _f:
                            _f.write(shot)
                    except Exception:
                        pass
                    img = _PILImage.open(_io.BytesIO(shot)).convert("RGB")
                    # 3) 网格检测
                    res = detect_grid_cells(img)
                    cells = res["cells"]
                    logger.info("[BrowserBind] 视觉网格 %dx%d cells=%d",
                                res["rows"], res["cols"], len(cells))
                    if len(cells) < 2:
                        return False
                    # 4) 切格存盘
                    paths = []
                    for _i, (_cx, _cy, _cw, _ch) in enumerate(cells):
                        _box = (max(0, _cx - _cw / 2), max(0, _cy - _ch / 2),
                                min(img.width, _cx + _cw / 2), min(img.height, _cy + _ch / 2))
                        cell_img = img.crop(_box)
                        _p = f"/tmp/hcaptcha_cell_{_i}.png"
                        cell_img.save(_p)
                        paths.append(_p)
                    # 5) CLIP 选格
                    payload = _json.dumps({"task": task, "images": paths}).encode("utf-8")
                    picked = []
                    try:
                        req = _ur.Request(clip_url, data=payload, headers={"Content-Type": "application/json"})
                        with _ur.urlopen(req, timeout=60) as resp:
                            r = _json.loads(resp.read().decode("utf-8"))
                        picked = [int(i) for i in r.get("indices", [])]
                        logger.info("[BrowserBind] 视觉 CLIP 选中格子: %s (elapsed=%s)",
                                    picked, r.get("elapsed"))
                    except Exception as _ce:
                        logger.warning("[BrowserBind] 视觉 CLIP 调用失败: %s", _ce)
                        return False
                    if not picked:
                        return False
                    # 6) 点击格中心（页面坐标 = iframe bbox + 格内坐标）
                    clicked = 0
                    for _idx in picked:
                        if _idx >= len(cells):
                            continue
                        _cx, _cy, _cw, _ch = cells[_idx]
                        _px = chal_box["x"] + _cx
                        _py = chal_box["y"] + _cy
                        try:
                            page.mouse.click(_px, _py, delay=120)
                            clicked += 1
                            logger.info("[BrowserBind] 视觉已点击格子 %d (%d,%d)",
                                        _idx, int(_px), int(_py))
                        except Exception as _e2:
                            logger.warning("[BrowserBind] 视觉格子 %d 点击失败: %s", _idx, _e2)
                        page.wait_for_timeout(600)
                    if clicked == 0:
                        return False
                    # 7) Verify Answers（在真正的 challenge frame 里点按钮）
                    verify_frame = None
                    for _f in page.frames:
                        _fu = (_f.url or "").lower()
                        if "hcaptcha" in _fu and "frame=challenge" in _fu:
                            verify_frame = _f
                            break
                    if verify_frame is None:
                        verify_frame = chal
                    for _sel in [".button-submit", "button[type='submit']", "#submit",
                                 "[aria-label*='submit' i]", "[aria-label*='verify' i]"]:
                        try:
                            _el = verify_frame.query_selector(_sel)
                            if _el and _el.is_visible():
                                _el.click(timeout=2000)
                                logger.info("[BrowserBind] 视觉答案已提交")
                                return True
                        except Exception:
                            continue
                    return True

                # 1) 定位 challenge frame（排除引导页 + 异步复核）。
                #    挑战文本是异步渲染的，前几轮可能还没加载出来 → 内部重试。
                chal, _chal_tl = None, ""
                for _p in range(4):
                    chal, _chal_tl = _pick_hcaptcha_challenge_frame(
                        page.frames, wait=lambda: page.wait_for_timeout(1200))
                    if chal is not None:
                        break
                    page.wait_for_timeout(2000)
                if chal is None:
                    logger.warning("[BrowserBind] hCaptcha 图片挑战 frame 未定位")
                    return False
                logger.info("[BrowserBind] hCaptcha 图片挑战 frame: %s text=%s",
                            (chal.url or "")[:100], _chal_tl[:100])
                # 2) 任务文本
                try:
                    task = (chal.evaluate("document.body ? document.body.innerText : ''") or "")
                except Exception:
                    task = ""
                task = " ".join(task.split())
                # 去除引导页残留("Close One more step before you're done Select the checkbox below")
                task = re.sub(r"(?i)(close\s+)?one more step before you.?.re done.*$", "", task)
                task = re.sub(r"(?i)\bplease try again\b.*$", "", task)
                task = re.sub(r"(?i)\b(verify|skip)\b.*$", "", task)
                task = task.strip(" .,:;")
                # 垃圾任务过滤（run24 实测 checkbox-invisible frame 的 JS blob 被当任务文本，
                # 长度数千字符且含 "/* {"，直接送 CLIP 会触发 token 超限 500）
                if (len(task) > 300 or "/* {" in task or task.startswith("/*")
                        or task.count("{") > 5 or "function" in task.lower()):
                    logger.warning("[BrowserBind] 任务文本疑似非挑战文本(截断至 120): %s", task[:120])
                    task = ""
                logger.info("[BrowserBind] hCaptcha 任务文本: %s", task[:160])
                if not task:
                    task = "select all images containing the object"
                # 2.5) 尺寸类挑战优先走像素占比求解（CLIP 无尺寸区分度）
                if any(k in task.lower() for k in ("smaller", "larger", "bigger", "smallest", "largest")):
                    try:
                        if _try_size_grid(timeout_ms=20000):
                            return True
                    except Exception as _se:
                        logger.warning("[BrowserBind] 尺寸求解异常: %s", _se)
                    logger.warning("[BrowserBind] 尺寸求解失败，回退 CLIP 通道")
                # 3) 收集网格图片
                imgs = []
                try:
                    imgs = chal.evaluate("""() => {
                        const out = [];
                        const push = (src, cls, w, h) => {
                            if (!src) return;
                            const s = String(src);
                            if (s.length < 15) return;
                            if (/logo|avatar|hcaptcha-logo|menu|icon|check|close|refresh/i.test(s)) return;
                            out.push({src: s, cls: String(cls||''), w: w||0, h: h||0});
                        };
                        document.querySelectorAll('img').forEach(im => {
                            const src = im.currentSrc || im.src || '';
                            push(src, im.className, im.width, im.height);
                        });
                        document.querySelectorAll('[style*="background-image"]').forEach(el => {
                            const st = el.getAttribute('style') || '';
                            const m = st.match(/url\\(["']?([^"')]+)["']?\\)/);
                            if (m) push(m[1], el.className, el.offsetWidth, el.offsetHeight);
                        });
                        return out;
                    }""")
                except Exception as e:
                    logger.warning("[BrowserBind] 网格图提取失败: %s", e)
                # 去重（同 src）
                seen = set()
                uniq = []
                for im in imgs:
                    k = im.get("src", "")
                    if k and k not in seen:
                        seen.add(k)
                        uniq.append(im)
                # 优先取 class 含 task/challenge 的，否则取全部；上限 12
                task_imgs = [im for im in uniq if "task" in im.get("cls", "").lower() or "challenge" in im.get("cls", "").lower()]
                grid = task_imgs if len(task_imgs) >= 2 else uniq
                grid = grid[:12]
                logger.info("[BrowserBind] 网格图 %d 张 (task=%d total=%d)",
                            len(grid), len(task_imgs), len(uniq))
                if len(grid) < 2:
                    try:
                        _dom = chal.evaluate("document.documentElement ? document.documentElement.outerHTML : ''") or ""
                        with open("/tmp/hcaptcha_image_dom.html", "w", encoding="utf-8") as _f:
                            _f.write(_dom)
                        logger.info("[BrowserBind] 图片挑战 DOM dump: /tmp/hcaptcha_image_dom.html (%d bytes)", len(_dom))
                    except Exception:
                        pass
                    # 新版 hCaptcha 图片不在 img 元素 → DOM 直取（优先）→ 纯视觉兜底
                    try:
                        if _try_dom_grid(timeout_ms=15000):
                            return True
                    except Exception as _de:
                        logger.warning("[BrowserBind] DOM 网格求解异常: %s", _de)
                    try:
                        if _solve_hcaptcha_image_visual(chal, task, timeout_ms=60000):
                            return True
                    except Exception as _ve:
                        logger.warning("[BrowserBind] 视觉求解异常: %s", _ve)
                    return False
                # 4) 调 CLIP 服务
                payload = _json.dumps({"task": task, "images": [im["src"] for im in grid]}).encode("utf-8")
                picked = []
                try:
                    req = _ur.Request(clip_url, data=payload, headers={"Content-Type": "application/json"})
                    with _ur.urlopen(req, timeout=60) as resp:
                        r = _json.loads(resp.read().decode("utf-8"))
                    picked = [int(i) for i in r.get("indices", [])]
                    logger.info("[BrowserBind] CLIP 选中格子: %s (elapsed=%s)", picked, r.get("elapsed"))
                except Exception as e:
                    logger.warning("[BrowserBind] CLIP 服务调用失败: %s", e)
                    return False
                if not picked:
                    logger.warning("[BrowserBind] CLIP 未选出任何格子")
                    return False
                # 5) 点击格子（用元素位置点击）
                clicked = 0
                try:
                    els = chal.query_selector_all("img, [style*='background-image']")
                    cands = [el for el in els if _im_eligible(chal, el)]
                    # 与 grid 顺序对齐：收集 src 匹配
                    for idx in picked:
                        if idx >= len(cands):
                            continue
                        el = cands[idx]
                        try:
                            box = el.bounding_box()
                            if box:
                                cx = box["x"] + box["width"] / 2
                                cy = box["y"] + box["height"] / 2
                                page.mouse.click(cx, cy, delay=80)
                            else:
                                el.click(timeout=2000)
                            clicked += 1
                            logger.info("[BrowserBind] 已点击格子 %d", idx)
                        except Exception as e2:
                            logger.warning("[BrowserBind] 格子 %d 点击失败: %s", idx, e2)
                        page.wait_for_timeout(600)
                except Exception as e:
                    logger.warning("[BrowserBind] 格子点击遍历失败: %s", e)
                if clicked == 0:
                    return False
                # 6) 点 Verify Answers
                submits = [".button-submit", "button[type='submit']", "#submit", "[aria-label*='submit' i]", "[aria-label*='verify' i]"]
                for sel in submits:
                    try:
                        el = chal.query_selector(sel)
                        if el and el.is_visible():
                            el.click(timeout=2000)
                            logger.info("[BrowserBind] hCaptcha 图片答案已提交")
                            return True
                    except Exception:
                        continue
                logger.warning("[BrowserBind] Verify 按钮未找到")
                return True  # 格子已点，交主循环观察

            def _solve_hcaptcha_cross(timeout_ms: int = 60000) -> bool:
                """hCaptcha「click on the crosses」挑战求解：OpenCV 检测 X 标记坐标。

                流程：
                  1. 定位 challenge frame，找挑战大图（class 含 task / 最大图）；
                  2. 调 cross 检测服务（HCAPTCHA_CROSS_URL）得归一化坐标；
                  3. 映射到页面坐标并点击（相对大图 bounding box）；
                  4. 点 Verify Answers。
                """
                import time as _t
                import urllib.request as _ur
                import json as _json
                cross_url = os.environ.get("HCAPTCHA_CROSS_URL", "http://127.0.0.1:8767/detect")
                # 1) 定位 challenge frame + 找大图
                chal = None
                big = None
                for f in page.frames:
                    furl = (f.url or "").lower()
                    if "hcaptcha" not in furl and "captcha" not in furl:
                        continue
                    try:
                        txt = (f.evaluate("document.body ? document.body.innerText : ''") or "")
                    except Exception:
                        txt = ""
                    tl = " ".join(txt.split()).lower()
                    if "cross" in tl or "tap" in tl:
                        chal = f
                        break
                if chal is None:
                    logger.warning("[BrowserBind] 十字挑战 frame 未定位")
                    return False
                # 找挑战大图：class 含 task 的 img/背景元素，或面积最大的 img
                try:
                    info = chal.evaluate("""() => {
                        const cands = [];
                        const push = (el, src, w, h) => {
                            if (!src || src.length < 15) return;
                            cands.push({src: String(src), w: w||0, h: h||0, cls: String(el.className||'')});
                        };
                        document.querySelectorAll('img').forEach(im => {
                            push(im, im.currentSrc || im.src || '', im.width, im.height);
                        });
                        document.querySelectorAll('[style*="background-image"]').forEach(el => {
                            const st = el.getAttribute('style') || '';
                            const m = st.match(/url\\(["']?([^"')]+)["']?\\)/);
                            if (m) push(el, m[1], el.offsetWidth, el.offsetHeight);
                        });
                        return cands;
                    }""")
                except Exception as e:
                    logger.warning("[BrowserBind] 十字图提取失败: %s", e)
                    info = []
                if not info:
                    return False
                # 优先 class 含 task 的，否则按面积排序取最大
                task_c = [c for c in info if "task" in c.get("cls", "").lower()]
                pool = task_c if task_c else sorted(info, key=lambda c: -(c.get("w", 0) * c.get("h", 0)))
                big = pool[0]
                logger.info("[BrowserBind] 十字挑战图: %s (%dx%d)", big["src"][:100], big.get("w"), big.get("h"))
                # 2) 调 cross 服务
                payload = _json.dumps({"image": big["src"], "num": 6}).encode("utf-8")
                pts = []
                try:
                    req = _ur.Request(cross_url, data=payload, headers={"Content-Type": "application/json"})
                    with _ur.urlopen(req, timeout=40) as resp:
                        r = _json.loads(resp.read().decode("utf-8"))
                    pts = [(float(p["x"]), float(p["y"])) for p in r.get("points", [])]
                    logger.info("[BrowserBind] cross 检测点: %s", pts)
                except Exception as e:
                    logger.warning("[BrowserBind] cross 服务调用失败: %s", e)
                    return False
                if not pts:
                    return False
                # 3) 映射到页面坐标点击（相对大图元素）
                try:
                    el = chal.query_selector("img[class*='task'], [class*='task-image'], img")
                    if el is None:
                        # 用 class 含 task 的元素
                        for sel in ["[class*='task'] img", "[class*='challenge'] img", "img"]:
                            el = chal.query_selector(sel)
                            if el:
                                break
                    if el is None:
                        logger.warning("[BrowserBind] 挑战图元素未找到")
                        return False
                    box = el.bounding_box()
                    if not box:
                        logger.warning("[BrowserBind] 挑战图元素无 bounding box")
                        return False
                    for (nx, ny) in pts:
                        cx = box["x"] + nx * box["width"]
                        cy = box["y"] + ny * box["height"]
                        page.mouse.click(cx, cy, delay=80)
                        logger.info("[BrowserBind] 已点击十字标记 (%.3f, %.3f)", nx, ny)
                        page.wait_for_timeout(500)
                except Exception as e:
                    logger.warning("[BrowserBind] 十字点击失败: %s", e)
                    return False
                # 4) Verify
                for sel in [".button-submit", "button[type='submit']", "[aria-label*='submit' i]", "[aria-label*='verify' i]"]:
                    try:
                        el = chal.query_selector(sel)
                        if el and el.is_visible():
                            el.click(timeout=2000)
                            logger.info("[BrowserBind] 十字答案已提交")
                            return True
                    except Exception:
                        continue
                return True

            def _solve_hcaptcha_drag(timeout_ms: int = 40000) -> bool:
                """hCaptcha 拖拽挑战（please drag the icon to the place where it fits）求解。

                挑战 UI 可能渲染在 shadow DOM（run27 实测 documentElement.outerHTML
                只有 script、body 为空但 innerText 有提示），因此：
                1) dump 全帧诊断（/tmp/hcaptcha_frames.txt）与挑战帧非脚本 HTML/截图；
                2) 元素搜索递归遍历 shadowRoot；
                3) 源=class/style 含 drag/source/handle/icon/piece 的定位小元素；
                   目标=含 target/drop/place/zone/slot 或虚线框元素（面积最大）。
                """
                import time as _t
                _end = _t.monotonic() + timeout_ms / 1000.0
                chal = None
                for f in page.frames:
                    furl = (f.url or "").lower()
                    if "hcaptcha" not in furl and "captcha" not in furl:
                        continue
                    try:
                        txt = (f.evaluate("document.body ? document.body.innerText : ''") or "")
                    except Exception:
                        txt = ""
                    tl = " ".join(txt.split()).lower()
                    if "drag" in tl:
                        chal = f
                        break
                if chal is None:
                    logger.warning("[BrowserBind] drag 挑战 frame 未定位")
                    return False
                # ── 诊断 dump：全帧列表 + 挑战帧非脚本 HTML + 截图 ──
                try:
                    _flines = []
                    for f in page.frames:
                        try:
                            _ftxt = (f.evaluate("document.body ? document.body.innerText : ''") or "")[:160]
                        except Exception:
                            _ftxt = ""
                        _flines.append(f"URL={f.url[:160]} | text={_ftxt!r}")
                    with open("/tmp/hcaptcha_frames.txt", "w", encoding="utf-8") as _f:
                        _f.write("\n".join(_flines))
                    logger.info("[BrowserBind] drag 全帧诊断已 dump: /tmp/hcaptcha_frames.txt (%d frames)", len(_flines))
                except Exception as _e:
                    logger.warning("[BrowserBind] drag 帧诊断 dump 失败: %s", _e)
                try:
                    _diag = chal.evaluate("""() => {
                        const out = {html: '', shadowRoots: 0, elems: 0, deep: 0};
                        const walk = (root) => {
                            let nodes = [];
                            try { nodes = Array.from(root.querySelectorAll('*')); } catch (e) { nodes = []; }
                            out.elems += nodes.length;
                            for (const el of nodes) {
                                if (el.shadowRoot) { out.shadowRoots += 1; out.deep += 1; walk(el.shadowRoot); }
                            }
                        };
                        walk(document);
                        const clone = document.documentElement.cloneNode(true);
                        try { clone.querySelectorAll('script, style, link, noscript').forEach(n => n.remove()); } catch (e) {}
                        out.html = (clone.outerHTML || '').slice(0, 300000);
                        return out;
                    }""")
                    with open("/tmp/hcaptcha_drag_diag.html", "w", encoding="utf-8") as _f:
                        _f.write(_diag.get("html", ""))
                    logger.info("[BrowserBind] drag 挑战帧诊断: elems=%s shadowRoots=%s deep=%s html=%d bytes",
                                _diag.get("elems"), _diag.get("shadowRoots"), _diag.get("deep"),
                                len(_diag.get("html") or ""))
                except Exception as _e:
                    logger.warning("[BrowserBind] drag 挑战帧诊断失败: %s", _e)
                try:
                    chal.screenshot(path="/tmp/hcaptcha_drag_shot.png", timeout=8000)
                    logger.info("[BrowserBind] drag 挑战帧截图已保存: /tmp/hcaptcha_drag_shot.png")
                except Exception as _e:
                    logger.warning("[BrowserBind] drag 挑战帧截图失败: %s", _e)
                # ── 元素搜索（递归 shadow DOM）──
                found = None
                while _t.monotonic() < _end:
                    try:
                        found = chal.evaluate("""() => {
                            const all = [];
                            const seenEls = new Set();
                            const walk = (root) => {
                                let nodes = [];
                                try { nodes = Array.from(root.querySelectorAll('*')); } catch (e) { nodes = []; }
                                for (const el of nodes) {
                                    if (seenEls.has(el)) continue;
                                    seenEls.add(el);
                                    all.push(el);
                                    if (el.shadowRoot) walk(el.shadowRoot);
                                }
                            };
                            walk(document);
                            const clsOf = (el) => {
                                try {
                                    const c = el.className;
                                    if (c && c.baseVal !== undefined) return c.baseVal;
                                    return (typeof c === 'string') ? c : '';
                                } catch (e) { return ''; }
                            };
                            const srcs = [], dsts = [];
                            for (const el of all) {
                                const tag = (el.tagName || '').toLowerCase();
                                if (!/^(div|span|img|canvas|svg|button|section|ul|li|figure|picture|path|g)$/.test(tag)) continue;
                                const cls = String(clsOf(el)).toLowerCase();
                                let st = '';
                                try { st = (el.getAttribute && (el.getAttribute('style') || '')) || ''; } catch (e) {}
                                let cs = null;
                                try { cs = getComputedStyle(el); } catch (e) {}
                                if (!cs) continue;
                                const pos = cs.position || '';
                                let r = null;
                                try { r = el.getBoundingClientRect(); } catch (e) {}
                                if (!r || r.width < 8 || r.height < 8) continue;
                                let role = '', draggable = '';
                                try { role = el.getAttribute('role') || ''; } catch (e) {}
                                try { draggable = el.getAttribute('draggable') || ''; } catch (e) {}
                                const both = cls + ' ' + st.toLowerCase() + ' ' + pos + ' ' + role + ' ' + draggable;
                                let inner = '';
                                try { inner = (el.innerText || '').trim().toLowerCase().slice(0, 60); } catch (e) {}
                                const both2 = both + ' ' + inner;
                                if (/(drag|source|handle|icon|piece|item|token|puzzle|game)/.test(both2)
                                    && /(absolute|fixed)/.test(pos)
                                    && r.width < 300 && r.height < 300) {
                                    srcs.push({x: r.left + r.width/2, y: r.top + r.height/2,
                                               w: r.width, h: r.height, tag: tag,
                                               cls: cls.slice(0, 100), st: st.slice(0, 100),
                                               inner: inner.slice(0, 40)});
                                }
                                if (/(target|drop|place|zone|slot|holder|outline|empty|fit|highlight)/.test(both2)
                                    || (cs.borderStyle || '').indexOf('dashed') >= 0
                                    || (cs.outlineStyle || '').indexOf('dashed') >= 0) {
                                    dsts.push({x: r.left + r.width/2, y: r.top + r.height/2,
                                               w: r.width, h: r.height, tag: tag,
                                               cls: cls.slice(0, 100), st: st.slice(0, 100),
                                               inner: inner.slice(0, 40)});
                                }
                            }
                            // 目标候选按面积降序（场景大图上的放置区通常较大）
                            dsts.sort((a, b) => (b.w * b.h) - (a.w * a.h));
                            return {srcs: srcs, dsts: dsts, total: all.length};
                        }""")
                    except Exception:
                        found = None
                    if found and found.get("srcs") and found.get("dsts"):
                        break
                    page.wait_for_timeout(700)
                if not found or not found.get("srcs") or not found.get("dsts"):
                    logger.warning("[BrowserBind] drag 求解: 源/目标元素不足 srcs=%s dsts=%s total=%s",
                                   len((found or {}).get("srcs") or []),
                                   len((found or {}).get("dsts") or []),
                                   (found or {}).get("total"))
                    return False
                # 源取最小的（图标通常小），目标取最大的放置区
                srcs = sorted(found["srcs"], key=lambda s: s["w"] * s["h"])
                src = srcs[0]
                dst = found["dsts"][0]
                logger.info("[BrowserBind] drag 源=(%.0f,%.0f) %s 目标=(%.0f,%.0f) %s",
                            src["x"], src["y"], src["cls"][:40],
                            dst["x"], dst["y"], dst["cls"][:40])
                # 帧偏移换算到页面坐标
                ox, oy = _frame_viewport_offset(chal)
                sx, sy = ox + src["x"], oy + src["y"]
                dx, dy = ox + dst["x"], oy + dst["y"]
                try:
                    page.mouse.move(sx - 40, sy + 25, steps=8)
                    page.wait_for_timeout(150)
                    page.mouse.move(sx, sy, steps=4)
                    page.wait_for_timeout(120)
                    page.mouse.down()
                    page.wait_for_timeout(200)
                    page.mouse.move(dx, dy, steps=18)
                    page.wait_for_timeout(250)
                    page.mouse.up()
                    logger.info("[BrowserBind] drag 已完成 (%d,%d)->(%d,%d)",
                                int(sx), int(sy), int(dx), int(dy))
                    return True
                except Exception as e:
                    logger.warning("[BrowserBind] drag 执行失败: %s", e)
                    return False

            def _im_eligible(chal, el) -> bool:
                """判断元素是否候选网格图（过滤 logo/菜单等）。"""
                try:
                    src = ""
                    if el.tag_name.lower() == "img":
                        src = el.get_attribute("src") or el.get_attribute("currentSrc") or ""
                    else:
                        st = el.get_attribute("style") or ""
                        import re as _re
                        m = _re.search(r"url\((.*?)\)", st)
                        src = m.group(1) if m else ""
                    if not src or len(src) < 15:
                        return False
                    if _re.search(r"logo|avatar|menu|icon|check|close|refresh", src, _re.I):
                        return False
                    return True
                except Exception:
                    return False

            def _debug_screenshot(tag: str):
                if os.environ.get("PLUS_BIND_DEBUG_SCREENSHOT", "0") == "1":
                    try:
                        path = f"/tmp/bind_debug_{tag}.png"
                        page.screenshot(path=path)
                        logger.info("[BrowserBind] 诊断截图: %s", path)
                    except Exception as e:
                        logger.warning("[BrowserBind] 截图失败: %s", e)

            def _probe_hcaptcha_state(tag: str = "") -> None:
                """诊断：列出所有 hCaptcha frame 的复选框/图片任务/文本状态。"""
                try:
                    for f in page.frames:
                        furl = (f.url or "").lower()
                        if "hcaptcha" not in furl and "captcha" not in furl:
                            continue
                        info = {"url": furl[:120]}
                        try:
                            info["checkbox"] = bool(f.query_selector("#checkbox"))
                        except Exception:
                            info["checkbox"] = "?"
                        try:
                            imgs = f.query_selector_all("img")
                            info["img_count"] = len(imgs)
                            task_imgs = 0
                            for i in imgs:
                                attr = ((i.get_attribute("src") or "") + " " + (i.get_attribute("class") or "")).lower()
                                if "task" in attr or "challenge" in attr:
                                    task_imgs += 1
                            info["task_imgs"] = task_imgs
                        except Exception:
                            info["img_count"] = "?"
                        try:
                            txt = (f.evaluate("document.body ? document.body.innerText : ''") or "")[:200]
                            info["text"] = " ".join(txt.split())[:120]
                        except Exception:
                            info["text"] = "?"
                        logger.info("[BrowserBind] hCaptcha 状态[%s]: %s", tag, json.dumps(info, ensure_ascii=False))
                except Exception as e:
                    logger.warning("[BrowserBind] hCaptcha 状态探测失败: %s", e)

            confirm_result = {"error": "unknown"}
            _debug_screenshot("before_confirm")

            # ── 单阶段 SDK confirmCardSetup（多账单地址重试）──
            # 关键认知（2026-08-06 实跑验证）：
            #   1) confirmCardSetup(use_stripe_sdk=false) 遇 3DS/hCaptcha 时 promise 挂起
            #      （SDK 内 in-flight），再次 confirmCardSetup 会报
            #      "You have an in-flight confirmCardSetup!" → 必须单次挂起 + 轮询挑战。
            #   2) 无挑战时 Stripe 直达银行：generic_decline 多为账单地址 AVS 不符，
            #      同一个 card element 可换 billing_details 重新 confirm（每次新建 PM）。
            import time as _t
            # 候选地址：当前地址优先，其余内置 US 地址补位，最多 4 个
            try:
                from core.plus_zero import BACKUP_US_ADDRESSES as _addr_pool
            except Exception:
                _addr_pool = []
            addr_candidates = [addr]
            seen_keys = {(addr.get("street"), addr.get("zip"))}
            for _a0 in _addr_pool:
                _k = (_a0.get("street"), _a0.get("zip"))
                if _k not in seen_keys:
                    addr_candidates.append(_a0)
                    seen_keys.add(_k)
            addr_candidates = addr_candidates[:4]

            def _build_confirm_script(a: dict) -> str:
                l1 = str(a.get("line1") or a.get("street") or addr_line1)
                ct = str(a.get("city") or addr_city)
                st = str(a.get("state") or addr_state)
                zp = str(a.get("zip") or a.get("postal_code") or addr_zip)
                co = str(a.get("country") or addr_country)
                return f"""
                (() => {{
                    try {{
                        const stripe = window.__stripe;
                        const cardElement = window.__cardElement;
                        window.__confirmResult = null;
                        window.__confirmPromise = stripe.confirmCardSetup('{client_secret}', {{
                            payment_method: {{
                                card: cardElement,
                                billing_details: {{
                                    name: '{card_name}',
                                    address: {{
                                        line1: '{l1}',
                                        city: '{ct}',
                                        state: '{st}',
                                        postal_code: '{zp}',
                                        country: '{co}',
                                    }},
                                }},
                            }},
                            use_stripe_sdk: true,
                        }}).then((result) => {{
                            if (result.error) {{
                                window.__confirmResult = {{ error: result.error.message, code: result.error.code, decline: result.error.decline_code }};
                            }} else {{
                                const si = result.setupIntent || {{}};
                                window.__confirmResult = {{
                                    success: si.status === 'succeeded',
                                    payment_method: si.payment_method,
                                    status: si.status,
                                    requires_action: si.status === 'requires_action',
                                    next_action: si.next_action,
                                }};
                            }}
                        }}).catch((e) => {{
                            window.__confirmResult = {{ error: e.message, code: 'exception' }};
                        }});
                        return 'started';
                    }} catch (e) {{
                        window.__confirmResult = {{ error: e.message, code: 'exception' }};
                        return 'failed';
                    }}
                }})()
                """

            _retryable_codes = (
                "card_declined", "card_error", "invalid_number", "incorrect_number",
                "expired_card", "incorrect_cvc", "processing_error", "invalid_expiry_year",
            )
            confirm_result = {"error": "no_attempt", "code": ""}
            for _ai, _a in enumerate(addr_candidates, 1):
                _l1 = str(_a.get("line1") or _a.get("street") or addr_line1)
                _ct = str(_a.get("city") or addr_city)
                _st = str(_a.get("state") or addr_state)
                _zp = str(_a.get("zip") or _a.get("postal_code") or addr_zip)
                _co = str(_a.get("country") or addr_country)
                logger.info("[BrowserBind] SDK confirm 尝试 %d/%d: 地址=%s, %s, %s %s, %s",
                            _ai, len(addr_candidates), _l1, _ct, _st, _zp, _co)
                start_script = _build_confirm_script(_a)
                started = page.evaluate(start_script)
                logger.info("[BrowserBind] confirm 已启动: %s", started)
                _debug_screenshot("challenge_" + str(_ai))
                _try_click_hcaptcha(timeout_ms=30000)
                # 120s→240s：图片挑战求解含 CLIP 抓图（实测 9 图 ~20s）+ 重定位/点击，
                # 120s 在慢网络下会被求解本身耗尽，导致 confirm 结果没机会被读到。
                deadline_b = _t.monotonic() + 240
                confirm_result = {"error": f"timeout_240s_attempt{_ai}", "code": "timeout"}
                _last_shot = 0.0
                _last_click = 0.0
                _audio_tried = False
                _audio_cooldown = 0.0
                _image_attempts = 0
                while _t.monotonic() < deadline_b:
                    try:
                        got = page.evaluate("window.__confirmResult")
                    except Exception:
                        got = None
                    if got:
                        confirm_result = got
                        logger.info("[BrowserBind] confirm 结果(尝试%d): %s",
                                    _ai, json.dumps(confirm_result, ensure_ascii=False)[:600])
                        if confirm_result.get("requires_action") and not confirm_result.get("success"):
                            logger.info("[BrowserBind] SDK 要求 handleNextAction 续接挑战...")
                            page.evaluate(f"""
                            (() => {{
                                try {{
                                    const stripe = window.__stripe;
                                    window.__confirmResult = null;
                                    stripe.handleNextAction({{ clientSecret: '{client_secret}' }})
                                        .then((result) => {{
                                            if (result.error) {{
                                                window.__confirmResult = {{ error: result.error.message, code: result.error.code }};
                                            }} else {{
                                                const si = result.setupIntent || {{}};
                                                window.__confirmResult = {{
                                                    success: si.status === 'succeeded',
                                                    payment_method: si.payment_method,
                                                    status: si.status,
                                                    requires_action: false,
                                                }};
                                            }}
                                        }})
                                        .catch((e) => {{ window.__confirmResult = {{ error: e.message, code: 'exception' }}; }});
                                    return 'started';
                                }} catch (e) {{
                                    window.__confirmResult = {{ error: e.message, code: 'exception' }};
                                    return 'failed';
                                }}
                            }})()
                            """)
                            _try_click_hcaptcha(timeout_ms=10000)
                            continue
                        break
                    _probe_hcaptcha_state("wait_" + str(_ai))
                    if _t.monotonic() - _last_shot >= 8:
                        _last_shot = _t.monotonic()
                        _debug_screenshot("wait_" + str(_ai) + "_" + str(int(_t.monotonic())))
                    if _t.monotonic() - _last_click >= 6:
                        _last_click = _t.monotonic()
                        _try_click_hcaptcha(timeout_ms=4000)
                    # 检测到图片/拖拽挑战（复选框点击被拒后）→ 切音频/图片通道求解。
                    # 图片求解允许重试（挑战网格异步渲染时首轮可能未就绪）。
                    if _t.monotonic() >= _audio_cooldown and _image_attempts < 4:
                        try:
                            _chal_txt = ""
                            _chal_imgs = 0
                            _task_txt = ""
                            _inter_txt = ""
                            _task_frame = None
                            for _f in page.frames:
                                _fu = (_f.url or "").lower()
                                if "hcaptcha" not in _fu and "captcha" not in _fu:
                                    continue
                                try:
                                    _t0 = (_f.evaluate("document.body ? document.body.innerText : ''") or "")
                                except Exception:
                                    _t0 = ""
                                _tl = " ".join(_t0.split()).lower()
                                # blob 帧（checkbox-invisible 的 JS 文本）跳过
                                if (_tl.startswith("/*") or "function" in _tl or len(_tl) > 300):
                                    continue
                                if any(k in _tl for k in _HCAPTCHA_TASK_KEYS) or "drag" in _tl:
                                    _task_txt = _tl[:120]
                                    _task_frame = _f
                                elif ("please try again" in _tl or "one more step" in _tl
                                      or "select the checkbox" in _tl or "checkbox below" in _tl):
                                    if not _inter_txt:
                                        _inter_txt = _tl[:120]
                                try:
                                    _imgs = _f.query_selector_all("img")
                                    for _im in _imgs:
                                        _ia = ((_im.get_attribute("src") or "") + " " + (_im.get_attribute("class") or "")).lower()
                                        if "task" in _ia or "challenge" in _ia:
                                            _chal_imgs += 1
                                except Exception:
                                    pass
                            _chal_txt = _task_txt or _inter_txt
                            if _chal_txt or _chal_imgs > 0:
                                logger.info("[BrowserBind] 检测到 hCaptcha 挑战(音频+图片通道求解): imgs=%d text=%s", _chal_imgs, _chal_txt[:80])
                                if not _audio_tried:
                                    _audio_tried = True
                                    _audio_cooldown = _t.monotonic() + 30
                                    ok_audio = _solve_hcaptcha_audio(timeout_ms=8000)
                                else:
                                    ok_audio = False
                                if not ok_audio:
                                    _image_attempts += 1
                                    logger.info("[BrowserBind] 图片通道求解 尝试 %d/4", _image_attempts)
                                    if "drag" in _chal_txt:
                                        logger.info("[BrowserBind] 检测到拖拽挑战，走 drag 求解")
                                        try:
                                            _solve_hcaptcha_drag(timeout_ms=40000)
                                        except Exception as _dge:
                                            logger.warning("[BrowserBind] drag 求解异常: %s", _dge)
                                    elif "cross" in _chal_txt or "tap" in _chal_txt or "click on the" in _chal_txt:
                                        logger.info("[BrowserBind] 检测到十字标记挑战，走 cross 求解")
                                        _solve_hcaptcha_cross(timeout_ms=60000)
                                    else:
                                        logger.info("[BrowserBind] 音频通道不可用/失败，切换图片通道求解")
                                        _solve_hcaptcha_image(timeout_ms=60000)
                                    _audio_cooldown = _t.monotonic() + 20
                        except Exception as _ae:
                            logger.warning("[BrowserBind] 音频求解异常: %s", _ae)
                            _audio_tried = True
                            _audio_cooldown = _t.monotonic() + 20
                    page.wait_for_timeout(2000)
                else:
                    logger.warning("[BrowserBind] confirm 尝试%d 240s 超时", _ai)
                if confirm_result.get("success"):
                    break
                if confirm_result.get("code") not in _retryable_codes:
                    # 非拒付类错误（挑战超时/未知）不换地址，直接结束
                    break
                logger.warning("[BrowserBind] 卡被拒（%s），换账单地址重试...",
                               confirm_result.get("decline") or confirm_result.get("code"))

                logger.warning("[BrowserBind] confirm 150s 超时")


            if confirm_result.get("success"):
                result["ok"] = True
                result["payment_method_id"] = confirm_result.get("payment_method", "")
                result["status"] = confirm_result.get("status", "")
                logger.info("[BrowserBind] ✅ 绑卡成功: %s (status=%s)",
                            result["payment_method_id"], result["status"])
            else:
                result["error"] = confirm_result.get("error", "未知错误")
                result["code"] = confirm_result.get("code", "")
                decline = confirm_result.get("decline", "")
                if decline:
                    result["error"] = f"{result['error']} (decline_code={decline})"
                logger.warning("[BrowserBind] ❌ 绑卡失败: %s (code=%s)", result["error"], result["code"])

            browser.close()

    except Exception as exc:
        logger.exception("[BrowserBind] 浏览器异常")
        result["error"] = f"{type(exc).__name__}: {exc}"

    if not result.get("ok"):
        raise RuntimeError(f"浏览器绑卡失败: {result.get('error', '未知错误')}")

    return result["payment_method_id"]


if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

    import glob
    acct_dirs = sorted(glob.glob(os.path.join(os.path.dirname(__file__), '..', 'accounts/*/')))
    if not acct_dirs:
        print("没有账号目录")
        exit(1)
    latest = acct_dirs[-1]
    acct_file = os.path.join(latest, '注册成功账号.json')
    import json as _json
    with open(acct_file) as f:
        accounts = _json.load(f)
    acct = accounts[0] if isinstance(accounts, list) else accounts
    token = acct.get('access_token', '') or acct.get('accessToken', '')
    email = acct.get('email', '')
    proxy = os.environ.get('PLUS_PROXY', '')
    card = os.environ.get('PLUS_CARD_NUMBER', '4513118051684191')
    em = os.environ.get('PLUS_CARD_EXP_MONTH', '08')
    ey = os.environ.get('PLUS_CARD_EXP_YEAR', '2027')
    cv = os.environ.get('PLUS_CARD_CVV', '126')
    if not token:
        print("未找到 access_token")
        exit(1)

    from core.plus_zero import PlusSession, switch_to_philippines, create_setup_intent
    ps = PlusSession(access_token=token, account_id='', email=email, proxy=proxy)
    extra = acct.get('extra', {})
    ps.account_id = extra.get('account', {}).get('id', '')
    print("accountId:", ps.account_id)
    switch_to_philippines(ps)
    print("切菲成功, pk:", (ps.publishable_key or '')[:30])
    create_setup_intent(ps)
    print("client_secret:", ps.client_secret[:40])
    try:
        pm_id = browser_bind_card_via_playwright(ps, card, em, ey, cv, proxy=proxy)
        print("✅ 绑卡成功:", pm_id)
    except Exception as e:
        print("❌ 绑卡失败:", e)
        import traceback
        traceback.print_exc()
