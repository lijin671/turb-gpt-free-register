# -*- coding: utf-8 -*-
"""core.sms_provider.check_sms_availability 非破坏性接码预检单测（mock 网络）。"""
import unittest
from unittest.mock import patch

from core import sms_provider
from config import codex as codex_config


class SmsPreflightTests(unittest.TestCase):
    def test_l_fully_configured_reachable_ok(self):
        with patch.object(codex_config, "SMS_PROVIDER", "l"), \
             patch.object(codex_config, "L_API_BASE", "http://localhost:8788"), \
             patch.object(codex_config, "L_ADMIN_AUTH_CODE", "adm"), \
             patch.object(codex_config, "SMS_SERVICE", "openai"), \
             patch.object(codex_config, "SMS_COUNTRY", "10"), \
             patch.object(sms_provider, "_probe_reachable",
                          return_value=(True, "L 后端可达（HTTP 200）")):
            r = sms_provider.check_sms_availability()
        self.assertTrue(r["ok"])
        self.assertIn("可达", r["message"])

    def test_l_not_configured_skips(self):
        with patch.object(codex_config, "SMS_PROVIDER", "l"), \
             patch.object(codex_config, "L_API_BASE", ""), \
             patch.object(codex_config, "L_ADMIN_AUTH_CODE", ""):
            r = sms_provider.check_sms_availability()
        self.assertTrue(r["ok"])
        self.assertIn("跳过", r["message"])

    def test_l_missing_service_country_fails(self):
        with patch.object(codex_config, "SMS_PROVIDER", "l"), \
             patch.object(codex_config, "L_API_BASE", "http://localhost:8788"), \
             patch.object(codex_config, "L_ADMIN_AUTH_CODE", "adm"), \
             patch.object(codex_config, "SMS_SERVICE", ""), \
             patch.object(codex_config, "SMS_COUNTRY", ""):
            r = sms_provider.check_sms_availability()
        self.assertFalse(r["ok"])
        self.assertIn("配置不完整", r["message"])

    def test_l_backend_down_fails(self):
        with patch.object(codex_config, "SMS_PROVIDER", "l"), \
             patch.object(codex_config, "L_API_BASE", "http://localhost:8788"), \
             patch.object(codex_config, "L_ADMIN_AUTH_CODE", "adm"), \
             patch.object(codex_config, "SMS_SERVICE", "openai"), \
             patch.object(codex_config, "SMS_COUNTRY", "10"), \
             patch.object(sms_provider, "_probe_reachable",
                          return_value=(False, "L 后端不可达：ConnectionRefused")):
            r = sms_provider.check_sms_availability()
        self.assertFalse(r["ok"])
        self.assertIn("不可达", r["message"])

    def test_grizzly_balance_ok(self):
        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"), \
             patch.object(codex_config, "SMS_API_KEY", "key-1"), \
             patch.object(sms_provider, "_request_grizzly",
                          return_value="ACCESS_BALANCE:47.06"):
            r = sms_provider.check_sms_availability()
        self.assertTrue(r["ok"])
        self.assertEqual(r["balance"], 47.06)

    def test_grizzly_no_balance_fails(self):
        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"), \
             patch.object(codex_config, "SMS_API_KEY", "key-1"), \
             patch.object(sms_provider, "_request_grizzly",
                          side_effect=sms_provider.SmsNoBalanceError("余额不足")):
            r = sms_provider.check_sms_availability()
        self.assertFalse(r["ok"])
        self.assertEqual(r["balance"], 0.0)

    def test_grizzly_not_configured_skips(self):
        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"), \
             patch.object(codex_config, "SMS_API_KEY", ""):
            r = sms_provider.check_sms_availability()
        self.assertTrue(r["ok"])
        self.assertIn("跳过", r["message"])

    def test_h_reachable_ok(self):
        with patch.object(codex_config, "SMS_PROVIDER", "h"), \
             patch.object(codex_config, "H_API_BASE", "http://localhost:8789"), \
             patch.object(codex_config, "H_ADMIN_AUTH_CODE", "adm"), \
             patch.object(codex_config, "SMS_SERVICE", "openai"), \
             patch.object(codex_config, "SMS_COUNTRY", "10"), \
             patch.object(sms_provider, "_probe_reachable",
                          return_value=(True, "H 后端可达（HTTP 200）")):
            r = sms_provider.check_sms_availability()
        self.assertTrue(r["ok"])
        self.assertIn("可达", r["message"])

    def test_unknown_provider_skips(self):
        with patch.object(codex_config, "SMS_PROVIDER", "weird"):
            r = sms_provider.check_sms_availability()
        self.assertTrue(r["ok"])
        self.assertIn("未知", r["message"])


if __name__ == "__main__":
    unittest.main()
