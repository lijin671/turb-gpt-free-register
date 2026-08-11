# -*- coding: utf-8 -*-
"""tools/check_proxy_pool 纯逻辑单测（不发起网络请求）。

覆盖：出口 IP 提取、旋转失效判定（同 IP 多次取样）、可用率阈值、
失败代理、min_distinct_ip=0 关闭旋转检查、main 退出码。
"""
import unittest
from unittest.mock import patch

import tools.check_proxy_pool as cpp


class ExtractExitIpTests(unittest.TestCase):
    def test_extracts_ip(self):
        self.assertEqual(cpp.extract_exit_ip("ip=1.2.3.4 colo=HKG loc=HK"), "1.2.3.4")

    def test_missing_ip_returns_empty(self):
        self.assertEqual(cpp.extract_exit_ip("colo=HKG loc=HK"), "")
        self.assertEqual(cpp.extract_exit_ip(""), "")


class RotationVerdictTests(unittest.TestCase):
    def test_pass_with_distinct_ips(self):
        passed, msg = cpp.rotation_verdict(total=3, ok=3, fail=0, unique_ips=3)
        self.assertTrue(passed)
        self.assertEqual(msg, "")

    def test_rotation_dead_all_same_ip(self):
        # 3 个代理全部 200 但同一出口 IP：rotate 静默失效，必须判失败
        passed, msg = cpp.rotation_verdict(total=3, ok=3, fail=0, unique_ips=1)
        self.assertFalse(passed)
        self.assertIn("旋转失效", msg)
        self.assertIn("1 个不同出口 IP", msg)

    def test_ok_rate_below_threshold(self):
        passed, msg = cpp.rotation_verdict(total=10, ok=5, fail=5, unique_ips=5,
                                           min_ok_rate=0.6)
        self.assertFalse(passed)
        self.assertIn("50% < 60%", msg)

    def test_any_failure_fails(self):
        passed, msg = cpp.rotation_verdict(total=4, ok=3, fail=1, unique_ips=3)
        self.assertFalse(passed)
        self.assertIn("1 个代理请求失败", msg)

    def test_rotation_check_disabled(self):
        passed, msg = cpp.rotation_verdict(total=3, ok=3, fail=0, unique_ips=1,
                                           min_distinct_ip=0)
        self.assertTrue(passed)

    def test_single_sample_ok_does_not_claim_rotation(self):
        passed, msg = cpp.rotation_verdict(total=1, ok=1, fail=0, unique_ips=1)
        self.assertTrue(passed)

    def test_zero_total_fails(self):
        passed, msg = cpp.rotation_verdict(total=0, ok=0, fail=0, unique_ips=0)
        self.assertFalse(passed)
        self.assertIn("未取样", msg)


class MainExitCodeTests(unittest.TestCase):
    def test_degraded_pool_returns_nonzero(self):
        # 全部同 IP 且全 ok：旋转失效 → main 返回 1
        with patch.object(cpp, "test_proxy",
                          return_value=("ok", "ip=9.9.9.9 colo=X loc=XX", "0.1s")), \
             patch.object(cpp, "pick_proxy", return_value="http://u:p@127.0.0.1:2260"), \
             patch.object(cpp.time, "sleep"), \
             patch.object(cpp.sys, "argv", ["check_proxy_pool.py", "--count", "3"]):
            rc = cpp.main()
        self.assertEqual(rc, 1)

    def test_healthy_pool_returns_zero(self):
        ips = ["1.1.1.1", "2.2.2.2", "3.3.3.3"]
        def _fake_test(proxy, target, timeout=20):
            ip = ips.pop(0)
            return ("ok", f"ip={ip} colo=X loc=XX", "0.1s")
        with patch.object(cpp, "test_proxy", side_effect=_fake_test), \
             patch.object(cpp, "pick_proxy", return_value="http://u:p@127.0.0.1:2260"), \
             patch.object(cpp.time, "sleep"), \
             patch.object(cpp.sys, "argv", ["check_proxy_pool.py", "--count", "3"]):
            rc = cpp.main()
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()


class CoolDownFailedTests(unittest.TestCase):
    """体检失败的静态代理自动冷却（树脂动态会话跳过）。"""

    def test_is_resin_dynamic_proxy(self):
        self.assertTrue(cpp.is_resin_dynamic_proxy("socks5://Pokemon.cli-session-abc12:tok@127.0.0.1:2260"))
        self.assertTrue(cpp.is_resin_dynamic_proxy("http://Premium-session-def34:tok@127.0.0.1:2260"))
        self.assertFalse(cpp.is_resin_dynamic_proxy("socks5://1.2.3.4:1080"))

    def test_cool_down_failed_static_proxy(self):
        with patch("core.ip_discipline.record_ip_use") as rec:
            ok = cpp.cool_down_failed_proxy("socks5://1.2.3.4:1080")
        self.assertTrue(ok)
        rec.assert_called_once()
        self.assertEqual(rec.call_args.args[0], "socks5://1.2.3.4:1080")
        self.assertEqual(rec.call_args.kwargs.get("outcome"), "failure")

    def test_cool_down_skips_resin_dynamic(self):
        with patch("core.ip_discipline.record_ip_use") as rec:
            ok = cpp.cool_down_failed_proxy("http://Pokemon.cli-session-abc12:tok@127.0.0.1:2260")
        self.assertFalse(ok)
        rec.assert_not_called()

    def test_cool_down_skips_empty(self):
        with patch("core.ip_discipline.record_ip_use") as rec:
            self.assertFalse(cpp.cool_down_failed_proxy(""))
        rec.assert_not_called()

    def test_main_cools_failed_static_when_flag(self):
        def _fake_test(proxy, target, timeout=20):
            return ("fail", "TimeoutError: boom", "0.1s")
        with patch.object(cpp, "test_proxy", side_effect=_fake_test), \
             patch.object(cpp, "pick_proxy",
                          return_value="socks5://1.2.3.4:1080"), \
             patch.object(cpp.time, "sleep"), \
             patch.object(cpp, "cool_down_failed_proxy") as cool, \
             patch.object(cpp.sys, "argv",
                          ["check_proxy_pool.py", "--count", "2", "--cool-down-failed"]):
            rc = cpp.main()
        self.assertEqual(rc, 1)
        self.assertEqual(cool.call_count, 2)
        self.assertEqual(cool.call_args.args[0], "socks5://1.2.3.4:1080")

    def test_main_without_flag_does_not_cool(self):
        def _fake_test(proxy, target, timeout=20):
            return ("fail", "TimeoutError: boom", "0.1s")
        with patch.object(cpp, "test_proxy", side_effect=_fake_test), \
             patch.object(cpp, "pick_proxy",
                          return_value="socks5://1.2.3.4:1080"), \
             patch.object(cpp.time, "sleep"), \
             patch.object(cpp, "cool_down_failed_proxy") as cool, \
             patch.object(cpp.sys, "argv",
                          ["check_proxy_pool.py", "--count", "2"]):
            rc = cpp.main()
        self.assertEqual(rc, 1)
        cool.assert_not_called()
