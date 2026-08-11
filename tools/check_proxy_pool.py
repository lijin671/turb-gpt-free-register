#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量体检代理池可用率（每次取全新 sid = 新出口 IP）。

用法:
  python3 tools/check_proxy_pool.py [--count 10] [--target https://www.cloudflare.com/cdn-cgi/trace] [--pool Pokemon.cli]
  python3 tools/check_proxy_pool.py --count 6 --min-ok-rate 0.6 --min-distinct-ip 2

输出: 每个代理 | ok/fail | 出口IP/地区 | 耗时；末尾汇总可用率 + 退出码判定。

退出码（供 daily_health_check / cron 使用）：
  0 = 通过；1 = 可用率不足 / 有失败 / 旋转失效（多次取样同一出口 IP）。
"""
import argparse, os, re, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'), override=True)

from config.proxy import pick_proxy, _ensure_fresh_session, PROXY_POOL
from curl_cffi import requests as curl_requests


def test_proxy(proxy: str, target: str, timeout: int = 20):
    t0 = time.time()
    try:
        r = curl_requests.get(
            target,
            proxies={"http": proxy, "https": proxy},
            timeout=timeout,
            impersonate="chrome",
        )
        dt = time.time() - t0
        if r.status_code != 200:
            return ("fail", f"HTTP{r.status_code}", f"{dt:.1f}s")
        text = r.text
        ip = re.search(r"ip=(\S+)", text)
        colo = re.search(r"colo=(\S+)", text)
        loc = re.search(r"loc=(\S+)", text)
        info = f"ip={ip.group(1) if ip else '?'} colo={colo.group(1) if colo else '?'} loc={loc.group(1) if loc else '?'}"
        return ("ok", info, f"{dt:.1f}s")
    except Exception as e:
        dt = time.time() - t0
        return ("fail", f"{type(e).__name__}: {str(e)[:90]}", f"{dt:.1f}s")


def extract_exit_ip(info: str) -> str:
    """从测试结果 info 里提取出口 IP（ip=1.2.3.4 colo=... loc=...）。"""
    m = re.search(r"ip=(\S+)", info or "")
    return (m.group(1) if m else "").strip()


def rotation_verdict(
    *,
    total: int,
    ok: int,
    fail: int,
    unique_ips: int,
    min_ok_rate: float = 0.6,
    min_distinct_ip: int = 2,
) -> tuple[bool, str]:
    """代理池体检判定（含旋转失效检测）。

    论坛经验（2708795 等）：oxy/lumi 的 proxy rotate 可能静默失效——
    有流量、请求 200，但多次取样全是同一出口 IP。这种池子不能用于
    1ip1号 注册，体检必须报失败。

    Returns: (pass?, message)；pass=True 时 message 为空或提示性文字。
    """
    if total <= 0:
        return False, "未取样任何代理"
    ok_rate = ok / total
    if ok_rate < min_ok_rate:
        return False, f"可用率 {ok_rate * 100:.0f}% < {min_ok_rate * 100:.0f}%"
    if fail > 0:
        return False, f"{fail} 个代理请求失败"
    if min_distinct_ip > 0 and ok >= 2 and unique_ips < min_distinct_ip:
        return False, (
            f"旋转失效：{ok} 个代理仅 {unique_ips} 个不同出口 IP"
            f"（< {min_distinct_ip}），rotate 可能已死"
        )
    return True, ""


def is_resin_dynamic_proxy(proxy: str) -> bool:
    """树脂动态会话（含 -session-{sid}，每次领取换新出口 IP）不参与静态冷却。"""
    p = str(proxy or "")
    return "cli-session-" in p or "-session-" in p


def cool_down_failed_proxy(proxy: str) -> bool:
    """体检失败的静态代理进入基础冷却（默认 30min），避免批量任务反复撞坏 IP。

    树脂动态会话 sid 每次领取都换新出口，无法预冷却，跳过。
    Returns: 是否已冷却。
    """
    p = str(proxy or "").strip()
    if not p or is_resin_dynamic_proxy(p):
        return False
    try:
        from core.ip_discipline import record_ip_use
        record_ip_use(p, outcome="failure")
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--target", default="https://www.cloudflare.com/cdn-cgi/trace")
    ap.add_argument("--pool", default="", help="指定池名，如 Pokemon.cli / Premium.cli；空则用 PROXY_POOL")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--min-ok-rate", type=float, default=0.6,
                    help="可用率低于该值视为体检失败（默认 0.6）")
    ap.add_argument("--min-distinct-ip", type=int, default=2,
                    help="成功代理中最少不同出口 IP 数；低于则判定旋转失效（0 关闭）")
    ap.add_argument("--cool-down-failed", action="store_true",
                    help="体检失败的静态代理进入基础冷却（30min），树脂动态会话自动跳过")
    args = ap.parse_args()

    proxies = []
    for _ in range(args.count):
        p = pick_proxy()
        if args.pool:
            p = _ensure_fresh_session(f"http://{args.pool}:{os.environ.get('RESIN_TOKEN', '')}@127.0.0.1:2260")
            # 从 PROXY_POOL 里取 token 拼上（_ensure_fresh_session 只处理 sid，不处理 token）
            token = re.search(r"://([^:@]+)@", PROXY_POOL or p)
            if token:
                p = re.sub(r"://[^:@]+@", f"://{token.group(1)}@", p)
        proxies.append(p)

    ok, fail, ips = [], [], []
    for i, p in enumerate(proxies, 1):
        status, info, dt = test_proxy(p, args.target, args.timeout)
        short = re.sub(r":(token|[^:@/]+)@", ":***@", p)
        print(f"[{i}/{len(proxies)}] {short} -> {status} {info} ({dt})", flush=True)
        if status == "ok":
            ok.append(p)
            ip = extract_exit_ip(info)
            if ip:
                ips.append(ip)
        else:
            fail.append(p)
            if args.cool_down_failed and cool_down_failed_proxy(p):
                print(f"      🧊 静态代理已冷却（基础冷却窗口）: {short}", flush=True)
        time.sleep(0.3)

    unique_ips = len(set(ips))
    passed, message = rotation_verdict(
        total=len(proxies),
        ok=len(ok),
        fail=len(fail),
        unique_ips=unique_ips,
        min_ok_rate=args.min_ok_rate,
        min_distinct_ip=args.min_distinct_ip,
    )
    print(f"\n总计 {len(proxies)} | 可用 {len(ok)} ({len(ok)/max(len(proxies),1)*100:.0f}%) | "
          f"失败 {len(fail)} | 不同出口IP {unique_ips}")
    if not passed:
        print(f"❌ 体检失败: {message}")
        return 1
    if message:
        print(f"⚠️ {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
