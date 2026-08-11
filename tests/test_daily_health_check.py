# -*- coding: utf-8 -*-
"""tools.daily_health_check 聚合逻辑测试（mock subprocess，无网络）。

覆盖：全部通过→0；任一阶段失败→非 0；skip 开关不执行任何子进程；
子进程超时→按失败处理。
"""
import unittest
from unittest.mock import patch

import tools.daily_health_check as dhc


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _argv(*extra):
    return ["daily_health_check.py", *extra]


class DailyHealthCheckTests(unittest.TestCase):
    def test_all_pass_exit_zero(self):
        with patch.object(dhc.subprocess, "run",
                          return_value=_FakeProc(0, "ok")) as run, \
             patch.object(dhc.sys, "argv", _argv("--skip-accounts")):
            rc = dhc.main()
        self.assertEqual(rc, 0)
        # 代理池体检 + 协议链路体检 + 接码平台预检 + 账号库存水位 各一次
        self.assertEqual(run.call_count, 4)
        cmds = [c.args[0][1] for c in run.call_args_list]
        self.assertIn("tools/check_proxy_pool.py", cmds)
        self.assertIn("tools/check_protocol_chain.py", cmds)
        self.assertIn("tools/check_sms_provider.py", cmds)
        self.assertIn("tools/check_account_pool.py", cmds)

    def test_any_failure_exit_nonzero(self):
        def _fake(cmd, **kw):
            rc = 1 if "check_proxy_pool.py" in cmd[1] else 0
            return _FakeProc(rc, "x")

        with patch.object(dhc.subprocess, "run", side_effect=_fake), \
             patch.object(dhc.sys, "argv", _argv("--skip-accounts")):
            rc = dhc.main()
        self.assertEqual(rc, 1)

    def test_skip_all_no_subprocess(self):
        with patch.object(dhc.subprocess, "run",
                          return_value=_FakeProc(0, "ok")) as run, \
             patch.object(dhc.sys, "argv",
                          _argv("--skip-proxy", "--skip-chain", "--skip-accounts", "--skip-sms", "--skip-pool")):
            rc = dhc.main()
        self.assertEqual(rc, 0)
        run.assert_not_called()

    def test_timeout_counts_as_failure(self):
        with patch.object(dhc.subprocess, "run",
                          side_effect=dhc.subprocess.TimeoutExpired("cmd", 600)), \
             patch.object(dhc.sys, "argv", _argv("--skip-accounts")):
            rc = dhc.main()
        self.assertEqual(rc, 1)



class DailyHealthCronTests(unittest.TestCase):
    def test_parse_cron_time(self):
        self.assertEqual(dhc._parse_cron_time("03:00"), ("0", "3"))
        self.assertEqual(dhc._parse_cron_time("23:59"), ("59", "23"))
        with self.assertRaises(ValueError):
            dhc._parse_cron_time("24:00")
        with self.assertRaises(ValueError):
            dhc._parse_cron_time("12:99")

    def test_cron_entry_has_marker_and_venv_python(self):
        entry = dhc.cron_entry("03:00", "/tmp/health.log")
        self.assertIn(dhc.CRON_MARKER, entry)
        self.assertIn(".venv/bin/python", entry)
        self.assertIn("tools/daily_health_check.py", entry)
        self.assertIn("/tmp/health.log", entry)

    def test_install_cron_idempotent(self):
        calls = {"n": 0}
        state = {"payload": ""}

        def _fake(cmd, **kw):
            calls["n"] += 1
            if cmd[0] == "crontab" and cmd[1] == "-l":
                return _FakeProc(0, state["payload"])
            if cmd[0] == "crontab" and cmd[1] == "-":
                state["payload"] = kw.get("input", "")
                return _FakeProc(0, "")
            return _FakeProc(1, "")

        with patch.object(dhc.subprocess, "run", side_effect=_fake):
            first = dhc.install_cron("03:00", "/tmp/health.log")
            second = dhc.install_cron("03:00", "/tmp/health.log")
        self.assertTrue(first)
        self.assertFalse(second)
        # 首次 1 读+1 写，二次幂等命中仅 1 读
        self.assertEqual(calls["n"], 3)

    def test_install_cron_replaces_stale_entry(self):
        stale = [
            "0 2 * * * cd /old && python3 tools/daily_health_check.py >> /tmp/a.log 2>&1  # "
            + dhc.CRON_MARKER,
            "# 其他任务",
        ]
        written = {}

        def _fake(cmd, **kw):
            if cmd[0] == "crontab" and cmd[1] == "-l":
                return _FakeProc(0, "\n".join(stale) + "\n")
            if cmd[0] == "crontab" and cmd[1] == "-":
                written["payload"] = kw.get("input", "")
                return _FakeProc(0, "")
            return _FakeProc(1, "")

        with patch.object(dhc.subprocess, "run", side_effect=_fake):
            changed = dhc.install_cron("03:00", "/tmp/health.log")

        self.assertTrue(changed)
        payload = written["payload"]
        self.assertNotIn("0 2 * * * cd /old", payload)
        self.assertIn("# 其他任务", payload)
        self.assertEqual(payload.count(dhc.CRON_MARKER), 1)

    def test_uninstall_cron_removes_marker(self):
        lines = [
            "0 3 * * * cd /x && python3 tools/daily_health_check.py >> /tmp/h.log 2>&1  # "
            + dhc.CRON_MARKER,
        ]
        written = {}

        def _fake(cmd, **kw):
            if cmd[0] == "crontab" and cmd[1] == "-l":
                return _FakeProc(0, "\n".join(lines) + "\n")
            if cmd[0] == "crontab" and cmd[1] == "-":
                written["payload"] = kw.get("input", "")
                return _FakeProc(0, "")
            return _FakeProc(1, "")

        with patch.object(dhc.subprocess, "run", side_effect=_fake):
            changed = dhc.uninstall_cron()

        self.assertTrue(changed)
        self.assertNotIn(dhc.CRON_MARKER, written["payload"])
        self.assertEqual(written["payload"].strip(), "")

    def test_uninstall_cron_noop_when_absent(self):
        with patch.object(dhc.subprocess, "run",
                          return_value=_FakeProc(0, "# 其他任务\n")):
            changed = dhc.uninstall_cron()
        self.assertFalse(changed)

    def test_main_install_cron_flag_routes_to_install(self):
        state = {"payload": ""}

        def _fake(cmd, **kw):
            if cmd[0] == "crontab" and cmd[1] == "-l":
                return _FakeProc(0, state["payload"])
            if cmd[0] == "crontab" and cmd[1] == "-":
                state["payload"] = kw.get("input", "")
                return _FakeProc(0, "")
            return _FakeProc(1, "")

        with patch.object(dhc.subprocess, "run", side_effect=_fake), \
             patch.object(dhc.sys, "argv", _argv("--install-cron", "--cron-time", "04:30")):
            rc = dhc.main()
        self.assertEqual(rc, 0)
        self.assertIn("30 4 * * *", state["payload"])

    def test_main_uninstall_cron_flag_routes_to_uninstall(self):
        lines = [
            "0 3 * * * cd /x && python3 tools/daily_health_check.py >> /tmp/h.log 2>&1  # "
            + dhc.CRON_MARKER,
        ]
        written = {}

        def _fake(cmd, **kw):
            if cmd[0] == "crontab" and cmd[1] == "-l":
                return _FakeProc(0, "\n".join(lines) + "\n")
            if cmd[0] == "crontab" and cmd[1] == "-":
                written["payload"] = kw.get("input", "")
                return _FakeProc(0, "")
            return _FakeProc(1, "")

        with patch.object(dhc.subprocess, "run", side_effect=_fake), \
             patch.object(dhc.sys, "argv", _argv("--uninstall-cron")):
            rc = dhc.main()
        self.assertEqual(rc, 0)
        self.assertNotIn(dhc.CRON_MARKER, written["payload"])


if __name__ == "__main__":
    unittest.main()


    def test_proxy_step_passes_cool_down_flag_by_default(self):
        with patch.object(dhc.subprocess, "run",
                          return_value=_FakeProc(0, "ok")) as run, \
             patch.object(dhc.sys, "argv", _argv("--skip-chain", "--skip-accounts")):
            rc = dhc.main()
        self.assertEqual(rc, 0)
        proxy_cmd = next(c.args.args for c in run.call_args_list
                         if "check_proxy_pool.py" in c.args.args[1])
        self.assertIn("--cool-down-failed", proxy_cmd)

    def test_proxy_step_omits_cool_down_flag_when_disabled(self):
        with patch.object(dhc.subprocess, "run",
                          return_value=_FakeProc(0, "ok")) as run, \
             patch.object(dhc.sys, "argv",
                          _argv("--skip-chain", "--skip-accounts", "--no-cool-down-failed")):
            rc = dhc.main()
        self.assertEqual(rc, 0)
        proxy_cmd = next(c.args.args for c in run.call_args_list
                         if "check_proxy_pool.py" in c.args.args[1])
        self.assertNotIn("--cool-down-failed", proxy_cmd)

    def test_pool_step_passes_min_usable_threshold(self):
        with patch.object(dhc.subprocess, "run",
                          return_value=_FakeProc(0, "ok")) as run, \
             patch.object(dhc.sys, "argv",
                          _argv("--skip-proxy", "--skip-chain", "--skip-accounts",
                                "--skip-sms", "--pool-min-usable", "5")):
            rc = dhc.main()
        self.assertEqual(rc, 0)
        pool_cmd = next(c.args[0] for c in run.call_args_list
                        if "check_account_pool.py" in c.args[0][1])
        self.assertIn("--min-usable", pool_cmd)
        self.assertIn("5", pool_cmd)

    def test_pool_shortfall_fails_health_check(self):
        def _fake(cmd, **kw):
            rc = 1 if "check_account_pool.py" in cmd[1] else 0
            return _FakeProc(rc, "shortfall 2")

        with patch.object(dhc.subprocess, "run", side_effect=_fake), \
             patch.object(dhc.sys, "argv",
                          _argv("--skip-proxy", "--skip-chain", "--skip-accounts",
                                "--skip-sms", "--pool-min-usable", "5")):
            rc = dhc.main()
        self.assertEqual(rc, 1)
