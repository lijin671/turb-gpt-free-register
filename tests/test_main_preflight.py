# -*- coding: utf-8 -*-
"""main._preflight_batch_ip_capacity 批量 1ip1号 容量预检单测（纯本地、无网络）。"""
import unittest
from unittest.mock import patch

import main as main_mod
import config.proxy as cproxy
import core.ip_discipline as ipd


class BatchIpCapacityPreflightTests(unittest.TestCase):
    def _free_map(self, mapping):
        def fake_free(proxy, **kw):
            return (mapping.get(proxy, False), "" if mapping.get(proxy, False) else "cooldown")
        return fake_free

    def test_discipline_disabled_skips(self):
        with patch.object(cproxy, "IP_DISCIPLINE_ENABLED", False):
            r = main_mod._preflight_batch_ip_capacity(5)
        self.assertTrue(r["ok"])
        self.assertIn("未开启", r["message"])

    def test_dynamic_session_pool_unlimited(self):
        pool = [
            "socks5://Pokemon.cli-session-abc123:tok@127.0.0.1:2260",
            "socks5://Pokemon.cli-session-def456:tok@127.0.0.1:2260",
        ]
        with patch.object(cproxy, "IP_DISCIPLINE_ENABLED", True), \
             patch.object(cproxy, "PROXY_POOL", pool):
            r = main_mod._preflight_batch_ip_capacity(50)
        self.assertTrue(r["ok"])
        self.assertEqual(r["static_total"], 0)
        self.assertIn("无上限", r["message"])

    def test_static_pool_capacity_sufficient(self):
        pool = ["socks5://1.1.1.1:1080", "socks5://2.2.2.2:1080", "socks5://3.3.3.3:1080"]
        with patch.object(cproxy, "IP_DISCIPLINE_ENABLED", True), \
             patch.object(cproxy, "PROXY_POOL", pool), \
             patch.object(ipd, "is_ip_free", side_effect=self._free_map({
                 "socks5://1.1.1.1:1080": True,
                 "socks5://2.2.2.2:1080": True,
                 "socks5://3.3.3.3:1080": False,
             })):
            r = main_mod._preflight_batch_ip_capacity(2)
        self.assertTrue(r["ok"])
        self.assertEqual(r["static_free"], 2)
        self.assertEqual(r["static_total"], 3)

    def test_static_pool_capacity_insufficient_warns(self):
        pool = ["socks5://1.1.1.1:1080", "socks5://2.2.2.2:1080"]
        with patch.object(cproxy, "IP_DISCIPLINE_ENABLED", True), \
             patch.object(cproxy, "PROXY_POOL", pool), \
             patch.object(ipd, "is_ip_free", side_effect=self._free_map({
                 "socks5://1.1.1.1:1080": True,
                 "socks5://2.2.2.2:1080": False,
             })):
            r = main_mod._preflight_batch_ip_capacity(3)
        self.assertFalse(r["ok"])
        self.assertIn("容量不足", r["message"])

    def test_static_pool_dedupe_and_password_masked(self):
        # 同 host:port 不同密码视为同一 IP，只统计一次
        pool = [
            "socks5://u1:p1@1.1.1.1:1080",
            "socks5://u2:p2@1.1.1.1:1080",
            "socks5://u3:p3@2.2.2.2:1080",
        ]
        with patch.object(cproxy, "IP_DISCIPLINE_ENABLED", True), \
             patch.object(cproxy, "PROXY_POOL", pool), \
             patch.object(ipd, "is_ip_free", return_value=(True, "")):
            r = main_mod._preflight_batch_ip_capacity(2)
        self.assertTrue(r["ok"])  # 去重后 2 个独立 IP 满足 2 个目标
        self.assertEqual(r["static_total"], 2)
        self.assertEqual(r["static_free"], 2)


if __name__ == "__main__":
    unittest.main()


class EmailPoolPreflightTests(unittest.TestCase):
    def test_temp_sources_skip(self):
        with patch.object(cproxy, "PROXY_POOL", []), \
             patch("config.email.EMAIL_SOURCE", "gptmail"), \
             patch("core.email_provider.parse_email_sources", return_value=["gptmail"]):
            self.assertEqual(main_mod._preflight_email_pool(5), "")

    def test_outlook_pool_shortfall_warns(self):
        with patch("config.email.EMAIL_SOURCE", "outlook"), \
             patch("core.email_provider.parse_email_sources", return_value=["outlook"]), \
             patch("core.db.outlook_pool_summary",
                   return_value={"total": 2, "available": 1, "used": 1, "failed": 0}):
            msg = main_mod._preflight_email_pool(3)
        self.assertIn("少于目标", msg)
        self.assertIn("1 个", msg)

    def test_outlook_pool_enough_no_warning(self):
        with patch("config.email.EMAIL_SOURCE", "outlook"), \
             patch("core.email_provider.parse_email_sources", return_value=["outlook"]), \
             patch("core.db.outlook_pool_summary",
                   return_value={"total": 5, "available": 4, "used": 1, "failed": 0}):
            self.assertEqual(main_mod._preflight_email_pool(3), "")

    def test_multi_source_sums_available(self):
        with patch("config.email.EMAIL_SOURCE", "outlook,generic_api"), \
             patch("core.email_provider.parse_email_sources", return_value=["outlook", "generic_api"]), \
             patch("core.db.outlook_pool_summary",
                   return_value={"available": 1}), \
             patch("core.db.generic_api_email_pool_summary",
                   return_value={"available": 2}):
            msg = main_mod._preflight_email_pool(5)
        self.assertIn("3 个", msg)


class WaterLevelTests(unittest.TestCase):
    def test_water_level_returns_pool_stats(self):
        with patch("core.db.list_accounts",
                   return_value=[{"email": "a@x.com", "access_token": "t"}]):
            w = main_mod._water_level()
        self.assertEqual(w["potential_usable"], 1)
        self.assertEqual(w["total"], 1)

    def test_water_level_none_on_failure(self):
        with patch("core.db.list_accounts", side_effect=RuntimeError("db down")):
            self.assertIsNone(main_mod._water_level())
