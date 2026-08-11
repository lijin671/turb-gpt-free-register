#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""协议注册链路体检（非破坏性，默认不触发 OTP 邮件）。

按 main.py 的 protocol 注册模式逐段验证：
  1. 代理预检 network_preflight（TLS 指纹 + 出口 geo）
  2. chatgpt.com CF 放行 anonymous_bootstrap
  3. GET /api/auth/providers
  4. GET /api/auth/csrf
  5. POST /api/auth/signin/openai → 拿 authorize_url
  6. sentinel/req + build_sentinel_header（Node VM，失败自动纯Python降级）
  7. (可选 --otp-send) follow_authorize 走到 /email-verification（会发 OTP 邮件）

不注册账号、不消耗邮箱（默认）；用于体检协议链路是否还活着，
对应 main.py 默认 REGISTRATION_DRIVER=protocol 的注册路径。

用法:
  python3 tools/check_protocol_chain.py
  python3 tools/check_protocol_chain.py --proxy http://user:pass@host:port
  python3 tools/check_protocol_chain.py --email foo@bar.com --otp-send
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

logger = logging.getLogger("check_protocol_chain")


def _pick_proxy(explicit: str = "") -> str:
    if explicit:
        return explicit
    from config import PROXY_POOL
    if isinstance(PROXY_POOL, (list, tuple)):
        lines = [str(l).strip() for l in PROXY_POOL if str(l).strip()]
    else:
        lines = [l.strip() for l in str(PROXY_POOL or "").splitlines() if l.strip()]
    if not lines:
        raise RuntimeError("PROXY_POOL 为空，请传 --proxy")
    return lines[0]


def _mask_proxy(proxy: str) -> str:
    try:
        head, tail = proxy.split("@", 1)
        return head.split("://")[0] + "://***@" + tail
    except Exception:
        return "<proxy>"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="协议注册链路体检（非破坏性）")
    ap.add_argument("--proxy", default="", help="代理（默认 PROXY_POOL 第一行）")
    ap.add_argument("--email", default="protocol.check@example.com", help="signin 用的 login_hint（不发 OTP）")
    ap.add_argument("--otp-send", action="store_true", help="跟随 authorize 触发 OTP 邮件（会消耗一次 OTP 发送）")
    ap.add_argument("--flow", default="authorize_continue", help="sentinel flow（默认 authorize_continue）")
    ap.add_argument("--no-vm", action="store_true", help="强制走纯Python降级路径（模拟 Node runner 失败，验证 turnstile 求解器）")
    args = ap.parse_args()

    if args.no_vm:
        import core.openai_auth as _oa
        def _raise(**kwargs):
            raise RuntimeError("--no-vm 模拟 Node runner 不可用")
        _oa.generate_sentinel_token = _raise
        print("== 已启用 --no-vm：sentinel 走纯Python降级路径 ==")

    from core.session import BrowserSession
    from core.openai_auth import network_preflight, request_sentinel_token, build_sentinel_header
    from core.chatgpt_bootstrap import anonymous_bootstrap
    from core.chatgpt_auth import get_providers, get_csrf_token, signin_openai

    proxy = _pick_proxy(args.proxy)
    print(f"== 协议注册链路体检 ==\n代理: {_mask_proxy(proxy)}")
    results: list[dict] = []

    def stage(name: str, fn):
        t0 = time.time()
        try:
            value = fn()
            results.append({"stage": name, "ok": True, "detail": value, "ms": int((time.time() - t0) * 1000)})
            print(f"  ✅ {name}（{int((time.time() - t0) * 1000)}ms）")
            return value
        except Exception as exc:
            results.append({"stage": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}", "ms": int((time.time() - t0) * 1000)})
            print(f"  ❌ {name}: {type(exc).__name__}: {str(exc)[:160]}")
            return None

    session = stage("1.代理预检", lambda: _preflight(BrowserSession, network_preflight, proxy))
    if session is None:
        print("\n链路中断：代理预检失败，无法继续。")
        _report(results)
        return 1

    providers = stage("2.CF放行+providers", lambda: get_providers(session))
    if providers:
        names = sorted(providers.keys())
        results[-1]["detail"] = f"providers={names}"

    csrf = stage("3.CSRF", lambda: get_csrf_token(session))
    auth_url = stage("4.signin(authorize_url)", lambda: signin_openai(session, csrf, args.email)) if csrf else None
    if auth_url:
        results[-1]["detail"] = auth_url.split("?")[0]

    sentinel = stage("5.sentinel req+build", lambda: _sentinel_chain(session, request_sentinel_token, build_sentinel_header, args.flow))
    if sentinel:
        results[-1]["detail"] = sentinel

    if args.otp_send and auth_url:
        final = stage("6.follow_authorize(OTP触发)", lambda: _follow(session, auth_url))
        if final:
            results[-1]["detail"] = final

    _report(results)
    ok_count = sum(1 for r in results if r["ok"])
    print(f"\n结果: {ok_count}/{len(results)} 阶段通过")
    return 0 if ok_count >= 4 else 1


def _preflight(BrowserSession, network_preflight, proxy):
    session = BrowserSession(proxy=proxy, detect_exit_geo=True)
    network_preflight(session)
    geo = getattr(session, "exit_geo", None) or {}
    print(f"     出口: {geo}")
    return session


def _sentinel_chain(session, request_sentinel_token, build_sentinel_header, flow):
    sentinel_resp = request_sentinel_token(session, flow)
    header, so = build_sentinel_header(session, sentinel_resp, flow)
    parsed = json.loads(header)
    return {
        "t": bool(parsed.get("t")), "c": len(parsed.get("c", "")),
        "p": parsed.get("p", "")[:8], "so": bool(so),
        "flow": parsed.get("flow"),
    }


def _follow(session, auth_url):
    from core.openai_auth import follow_authorize
    final = follow_authorize(session, auth_url)
    return final.split("?")[0] if final else ""


def _report(results: list[dict]) -> None:
    print("\n== 明细 ==")
    for r in results:
        detail = r.get("detail")
        if isinstance(detail, dict):
            detail = json.dumps(detail, ensure_ascii=False)[:200]
        print(f"  [{'+' if r['ok'] else '-'}] {r['stage']}: {detail}")


if __name__ == "__main__":
    sys.exit(main())
