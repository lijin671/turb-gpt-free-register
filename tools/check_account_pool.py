#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号库存水位统计（DB 口径，快速）+ 可选 live 抽样校验。

用途：
  - 注册→导出→吊销 闭环里定期看"还有多少可用号"，低于阈值时告警/触发补号
  - 统计口径与论坛经验对齐：连坐风险(co_risk)、codex 失败/缺失的号不算可用

用法:
  python3 tools/check_account_pool.py
  python3 tools/check_account_pool.py --min-usable 5            # 可用 < 5 → 退出码 1
  python3 tools/check_account_pool.py --live-check-limit 5      # 最新 5 个可用号真实校验
  python3 tools/check_account_pool.py --json
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'), override=True)


from core.account_pool import _is_expired, _parse_dt, pool_stats  # noqa: F401  # 复用（tools 与 WebUI 共用）


def _mark_revoked_accounts(revoked_accounts: list[dict]) -> int:
    """live 校验确认失效的账号：按出口 IP 聚合标记连坐风险（与 check_accounts_valid 对齐）。"""
    if not revoked_accounts:
        return 0
    from core.ip_discipline import mark_ip_co_risk
    by_ip: dict[str, list[str]] = {}
    for a in revoked_accounts:
        key = str(a.get("proxy_used") or a.get("ip_key") or "").strip() or "direct"
        by_ip.setdefault(key, []).append(str(a.get("email") or ""))
    marked = 0
    for ip, emails in by_ip.items():
        try:
            marked += mark_ip_co_risk(ip, "live 校验 token 失效", emails=emails)
        except Exception:
            pass
    return marked


def live_check(accounts: list[dict], limit: int, mark_revoked: bool = True) -> dict:
    """对最新 N 个潜在可用账号做真实 /backend-api/me 校验（默认同 IP）。

    mark_revoked=True 时，确认失效的账号按出口 IP 标记连坐风险（DB 口径随之更新）。
    """
    from tools.check_accounts_valid import check_token
    usable = [
        a for a in (accounts or [])
        if str(a.get("access_token") or "") and not a.get("ip_co_risk")
        and str(a.get("codex_status") or "").lower() not in ("failed", "missing")
    ][: max(0, int(limit))]
    ok = revoked = errors = 0
    details: list[dict] = []
    revoked_accounts: list[dict] = []
    for a in usable:
        status, email2, plan, note = check_token(
            a.get("access_token") or "",
            str(a.get("proxy_used") or ""),
            device_id=str(a.get("device_id") or ""),
        )
        if status == "ok":
            ok += 1
        elif status == "revoked":
            revoked += 1
            revoked_accounts.append(a)
        else:
            errors += 1
        details.append({
            "email": a.get("email", ""),
            "status": status,
            "note": note,
        })
    marked = _mark_revoked_accounts(revoked_accounts) if mark_revoked else 0
    return {
        "live_checked": len(usable),
        "live_ok": ok,
        "live_revoked": revoked,
        "live_error": errors,
        "live_marked_co_risk": marked,
        "live_details": details,
    }


def revive_revoked_accounts(accounts: list[dict], revoked_details: list[dict]) -> dict:
    """对 live 校验确认失效（revoked）的账号尝试 token 复活。

    - 复活成功：DB 写回新 token，不标记连坐
    - 复活失败：按出口 IP 标记连坐风险（防死号进生产池）
    Returns: {"revived_ok", "revived_failed", "revived_marked", "revive_results"}。
    """
    from core.token_revival import revive_account
    revoked_emails = {
        str(d.get("email") or "").lower()
        for d in (revoked_details or [])
        if d.get("status") == "revoked"
    }
    by_email = {str(a.get("email") or "").lower(): a for a in (accounts or [])}
    ok = failed = marked = 0
    results: list[dict] = []
    for email in sorted(revoked_emails):
        acc = by_email.get(email)
        if not acc:
            continue
        r = revive_account(email)
        if r.get("ok"):
            ok += 1
            results.append({"email": email, "revived": True, "message": r.get("message", "")})
        else:
            failed += 1
            results.append({"email": email, "revived": False, "message": r.get("message", "")})
            try:
                from core.ip_discipline import mark_ip_co_risk
                marked += mark_ip_co_risk(
                    str(acc.get("proxy_used") or acc.get("ip_key") or "").strip() or "direct",
                    "live 校验 token 失效且复活失败",
                    emails=[email],
                )
            except Exception:
                pass
    return {
        "revived_ok": ok,
        "revived_failed": failed,
        "revived_marked": marked,
        "revive_results": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-usable", type=int, default=0, help="潜在可用数低于该值退出码 1")
    ap.add_argument("--live-check-limit", type=int, default=0, help="对最新 N 个可用号做真实校验")
    ap.add_argument("--no-mark", action="store_true",
                    help="live 校验确认失效时不标记连坐风险（默认标记，对齐 check_accounts_valid）")
    ap.add_argument("--revive-revoked", action="store_true",
                    help="live 校验确认失效的账号自动尝试 token 复活；复活失败才标记连坐"
                         "（需要配合 --live-check-limit）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if args.revive_revoked and args.live_check_limit <= 0:
        print("--revive-revoked 需要配合 --live-check-limit", file=sys.stderr)
        return 2

    from core import db
    accounts = db.list_accounts(limit=100000)
    stats = pool_stats(accounts)
    # --revive-revoked 时标记推迟到复活步骤：复活成功不标记，复活失败才标记
    mark_revoked = (not args.no_mark) and not args.revive_revoked
    live = (live_check(accounts, args.live_check_limit, mark_revoked=mark_revoked)
            if args.live_check_limit > 0 else {})

    revive = {}
    if args.revive_revoked:
        revive = revive_revoked_accounts(accounts, live.get("live_details") or [])
        if revive.get("revived_ok"):
            # 复活写回 DB 后刷新水位（复活失败被标记 co_risk → 潜在可用下降）
            stats = pool_stats(db.list_accounts(limit=100000))

    result = {**stats, **live, **revive}
    shortfall = max(0, int(args.min_usable) - int(stats["potential_usable"]))
    result["shortfall"] = shortfall
    result["ok"] = bool(stats["potential_usable"] >= int(args.min_usable)) and (
        not live or bool(live.get("live_ok", 0) >= int(args.min_usable))
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    print(f"账号库存水位（共 {stats['total']}）")
    print(f"  有 token: {stats['has_token']} | 潜在可用: {stats['potential_usable']} "
          f"({stats['potential_usable_rate'] * 100:.1f}%)")
    print(f"  连坐风险: {stats['co_risk']} | Codex 失败/缺失: {stats['codex_failed']}/{stats['codex_missing']} "
          f"| 已过期: {stats['expired']} | Plus 成功: {stats['plus_success']}")
    if live:
        print(f"  live 校验: 可用 {live['live_ok']} / 失效 {live['live_revoked']} / 异常 {live['live_error']} "
              f"（抽样 {live['live_checked']}，连坐标记 {live['live_marked_co_risk']}）")
    if revive:
        print(f"  复活: 成功 {revive['revived_ok']} / 失败 {revive['revived_failed']} "
              f"（复活失败已标记连坐 {revive['revived_marked']}）")
    if shortfall:
        print(f"⚠️ 可用号缺口 {shortfall} 个（阈值 {args.min_usable}），建议补号：main.py -n {shortfall}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
