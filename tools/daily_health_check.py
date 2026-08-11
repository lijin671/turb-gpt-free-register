#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日链路体检聚合（cron 友好）。

依次执行：
  1. 代理池体检   tools/check_proxy_pool.py --count N
  2. 协议链路体检 tools/check_protocol_chain.py（非破坏性，不发 OTP）
  3. 账号 token 体检 tools/check_accounts_valid.py --limit N（默认同 IP 校验）
  4. 接码平台预检 tools/check_sms_provider.py（非破坏性，未配置则跳过）
  5. 账号库存水位 tools/check_account_pool.py（DB 口径，--pool-min-usable 低于阈值告警）

任一阶段失败/异常时以非 0 退出码结束，便于 cron/监控告警。
用法:
  python3 tools/daily_health_check.py
  python3 tools/daily_health_check.py --proxy-count 6 --accounts-limit 5
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _run(name: str, args: list[str]) -> int:
    print(f"\n===== [{name}] =====", flush=True)
    try:
        proc = subprocess.run(
            [PY, *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        print(f"[{name}] 超时（600s）", flush=True)
        return 1
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if out:
        print(out, flush=True)
    if err:
        print(err, flush=True)
    return proc.returncode


CRON_MARKER = "turb-gpt-free-register daily health check"
CRON_DEFAULT_LOG = "/tmp/turb-gpt-health.log"


def _parse_cron_time(spec: str) -> tuple[str, str]:
    """把 HH:MM 解析成 cron 的 (minute, hour) 字段。"""
    spec = (spec or "").strip()
    if not spec:
        return "0", "3"
    if ":" in spec:
        hh, mm = spec.split(":", 1)
    else:
        hh, mm = spec, "0"
    try:
        hour = int(hh)
        minute = int(mm)
    except ValueError as exc:
        raise ValueError(f"非法 cron 时间: {spec!r}（需 HH:MM，如 03:00）") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"非法 cron 时间: {spec!r}（需 HH:MM，如 03:00）")
    return str(minute), str(hour)


def _cron_python() -> str:
    """优先使用 venv 解释器，保证 cron 环境下依赖齐全。"""
    venv_py = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    if venv_py.is_file():
        return str(venv_py)
    return sys.executable


def _read_crontab() -> list[str]:
    """读取当前 crontab；空表时返回空列表。"""
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"读取 crontab 失败: {exc}") from exc
    if proc.returncode != 0:
        return []
    return proc.stdout.splitlines()


def _write_crontab(lines: list[str]) -> None:
    payload = "\n".join(lines).rstrip() + "\n"
    try:
        proc = subprocess.run(["crontab", "-"], input=payload, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"写入 crontab 失败: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"crontab - 写入失败: {proc.stderr.strip()[:200]}")


def cron_entry(time_spec: str = "03:00", log_file: str = CRON_DEFAULT_LOG) -> str:
    """构造带标记的 cron 行（便于幂等安装/卸载）。"""
    minute, hour = _parse_cron_time(time_spec)
    return (
        f"{minute} {hour} * * * cd {ROOT} && {_cron_python()} tools/daily_health_check.py "
        f">> {log_file} 2>&1  # {CRON_MARKER}"
    )


def install_cron(time_spec: str = "03:00", log_file: str = CRON_DEFAULT_LOG) -> bool:
    """幂等安装每日体检 cron；返回是否发生变更。"""
    lines = _read_crontab()
    entry = cron_entry(time_spec, log_file)
    # 幂等：完全相同（含时间/日志/解释器）的条目已存在则不重复写入
    if entry in lines:
        return False
    kept = [ln for ln in lines if CRON_MARKER not in ln]
    kept.append(entry)
    _write_crontab(kept)
    print(f"✅ 已安装每日体检 cron: {entry}", flush=True)
    return True


def uninstall_cron() -> bool:
    """幂等卸载每日体检 cron；返回是否发生变更。"""
    lines = _read_crontab()
    kept = [ln for ln in lines if CRON_MARKER not in ln]
    if len(kept) == len(lines):
        return False
    _write_crontab(kept)
    print("✅ 已卸载每日体检 cron", flush=True)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="每日链路体检聚合")
    ap.add_argument("--proxy-count", type=int, default=6, help="代理池体检数量")
    ap.add_argument("--accounts-limit", type=int, default=5, help="账号 token 体检数量")
    ap.add_argument("--skip-proxy", action="store_true", help="跳过代理池体检")
    ap.add_argument("--skip-chain", action="store_true", help="跳过协议链路体检")
    ap.add_argument("--skip-accounts", action="store_true", help="跳过账号 token 体检")
    ap.add_argument("--skip-sms", action="store_true", help="跳过接码平台预检")
    ap.add_argument("--skip-pool", action="store_true", help="跳过账号库存水位统计")
    ap.add_argument("--pool-min-usable", type=int, default=0,
                    help="潜在可用账号低于该值时库存水位步骤退出码 1（默认 0 = 只报告不告警）")
    ap.add_argument("--install-cron", action="store_true", help="幂等安装每日体检 cron 后退出")
    ap.add_argument("--uninstall-cron", action="store_true", help="卸载每日体检 cron 后退出")
    ap.add_argument("--cron-time", default="03:00", help="cron 执行时间 HH:MM（默认 03:00）")
    ap.add_argument("--no-cool-down-failed", action="store_true",
                    help="代理池体检不把失败的静态代理冷却（默认冷却 30min）")
    ap.add_argument("--cron-log", default=CRON_DEFAULT_LOG, help="cron 日志文件（默认 %s）" % CRON_DEFAULT_LOG)
    args = ap.parse_args()

    if args.install_cron or args.uninstall_cron:
        try:
            if args.install_cron:
                install_cron(args.cron_time, args.cron_log)
            if args.uninstall_cron:
                uninstall_cron()
        except Exception as exc:
            print(f"❌ cron 操作失败: {exc}", flush=True)
            return 1
        return 0

    failures: list[str] = []
    if not args.skip_proxy:
        proxy_args = ["tools/check_proxy_pool.py", "--count", str(args.proxy_count)]
        if not args.no_cool_down_failed:
            proxy_args.append("--cool-down-failed")
        rc = _run("代理池体检", proxy_args)
        if rc != 0:
            failures.append("代理池体检")
    if not args.skip_chain:
        rc = _run("协议链路体检", ["tools/check_protocol_chain.py"])
        if rc != 0:
            failures.append("协议链路体检")
    if not args.skip_accounts:
        rc = _run("账号 token 体检", ["tools/check_accounts_valid.py", "--limit", str(args.accounts_limit)])
        if rc != 0:
            failures.append("账号 token 体检")
    if not args.skip_sms:
        rc = _run("接码平台预检", ["tools/check_sms_provider.py"])
        if rc != 0:
            failures.append("接码平台预检")
    if not args.skip_pool:
        pool_args = ["tools/check_account_pool.py"]
        if args.pool_min_usable > 0:
            pool_args += ["--min-usable", str(args.pool_min_usable)]
        rc = _run("账号库存水位", pool_args)
        if rc != 0:
            failures.append("账号库存水位")

    if failures:
        print(f"\n❌ 体检失败阶段: {', '.join(failures)}", flush=True)
        return 1
    print("\n✅ 每日体检全部通过", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
