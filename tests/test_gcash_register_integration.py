# -*- coding: utf-8 -*-
"""GCash 注册流程集成测试：ChatGPT 号成功后自动接码（HeroSMS）编排。"""
import unittest
from unittest.mock import MagicMock, patch

from core.gcash_registrar import GcashAccount


class MaybeRegisterGcashTest(unittest.TestCase):
    def _run(self, plus_cfg_patch: dict, gcash_result=None, gcash_error=None):
        from main import _maybe_register_gcash
        result = {"success": True, "email": "u@example.com", "account_id": 7}
        with patch("config.plus.ENABLE_GCASH_REGISTER", plus_cfg_patch.get("enable", False)), \
             patch("config.plus.HERO_SMS_API_KEY", plus_cfg_patch.get("api_key", "")), \
             patch("config.plus.GCASH_REGISTER_ROTATE_PROXY", plus_cfg_patch.get("rotate", True)), \
             patch("core.gcash_registrar.pick_hero_proxy", return_value="http://p1:8080"), \
             patch("core.gcash_registrar.register_gcash_account",
                   side_effect=gcash_error if gcash_error else (lambda **kw: gcash_result)):
            return _maybe_register_gcash("u@example.com", result)

    def test_disabled_returns_result_unchanged(self):
        out = self._run({"enable": False})
        self.assertTrue(out["success"])
        self.assertNotIn("gcash_phone", out)

    def test_enabled_no_api_key_marks_skipped(self):
        out = self._run({"enable": True, "api_key": ""})
        self.assertTrue(out["success"])
        self.assertEqual(out["gcash_status"], "skipped_no_api_key")

    def test_enabled_success_merges_fields(self):
        acc = GcashAccount(phone="6391712345678", first_name="Juan", last_name="Cruz", status="registered")
        out = self._run({"enable": True, "api_key": "k"}, gcash_result=acc)
        self.assertTrue(out["success"])
        self.assertEqual(out["gcash_phone"], "6391712345678")
        self.assertEqual(out["gcash_status"], "registered")
        self.assertEqual(out["gcash_first_name"], "Juan")

    def test_enabled_failure_does_not_block_chatgpt(self):
        out = self._run({"enable": True, "api_key": "k"},
                        gcash_error=RuntimeError("NO_NUMBERS"))
        self.assertTrue(out["success"])
        self.assertIn("error", out["gcash_status"])

    def test_proxy_passed_from_pick(self):
        from main import _maybe_register_gcash
        result = {"success": True, "email": "u@example.com"}
        acc = GcashAccount(phone="6391")
        with patch("config.plus.ENABLE_GCASH_REGISTER", True), \
             patch("config.plus.HERO_SMS_API_KEY", "k"), \
             patch("config.plus.GCASH_REGISTER_ROTATE_PROXY", True), \
             patch("core.gcash_registrar.pick_hero_proxy", return_value="http://p2:8080") as pick, \
             patch("core.gcash_registrar.register_gcash_account", return_value=acc) as reg:
            _maybe_register_gcash("u@example.com", result)
        pick.assert_called_once()
        self.assertEqual(reg.call_args.kwargs.get("proxy"), "http://p2:8080")


class RegisterGcashAccountConfigTest(unittest.TestCase):
    def test_builds_from_config_and_runs(self):
        acc = GcashAccount(phone="6391711112222", status="kyc_done")
        fake_reg = MagicMock()
        fake_reg.run.return_value = acc
        cfg = {
            "HERO_SMS_API_KEY": "cfg-key",
            "HERO_SMS_BASE_URL": "https://hero-sms.com/stubs/handler_api.php",
            "HERO_SMS_PROXY": "",
            "HERO_SMS_SERVICE": "gcash",
            "HERO_SMS_COUNTRY": 6,
            "HERO_SMS_MAX_PRICE": 0.0,
            "HERO_SMS_WAIT_TIMEOUT": 240,
            "HERO_SMS_POLL_INTERVAL": 5,
            "GCASH_REGISTER_PROFILE": {"first_name": "A"},
            "GCASH_ADB_SERIAL": "",
            "GCASH_APP_PACKAGE": "com.globe.gcash.android",
        }
        with patch.dict("config.plus.__dict__", cfg, clear=False), \
             patch("core.gcash_registrar.GcashRegistrar", return_value=fake_reg) as reg_cls:
            from core.gcash_registrar import register_gcash_account
            out = register_gcash_account(proxy="http://px:8080")
        self.assertEqual(out.phone, "6391711112222")
        fake_reg.run.assert_called_once_with(service="gcash", country=6, max_price=None)
        hero = reg_cls.call_args.kwargs["hero"]
        self.assertEqual(hero.api_key, "cfg-key")
        self.assertEqual(hero.proxy, "http://px:8080")

    def test_pick_hero_proxy_prefers_env_proxy(self):
        from core.gcash_registrar import pick_hero_proxy
        with patch("config.plus.HERO_SMS_PROXY", "http://env:3128"), \
             patch("config.proxy.pick_proxy", return_value="http://pool:8080") as pool:
            out = pick_hero_proxy()
        self.assertEqual(out, "http://env:3128")
        pool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
