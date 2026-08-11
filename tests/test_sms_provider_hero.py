# -*- coding: utf-8 -*-
"""sms_provider 的 HeroSMS 平台（SMS_PROVIDER="hero"）测试：mock 网络层。"""
import unittest
from unittest.mock import MagicMock, patch

from core import sms_provider
from core.hero_sms import Activation, CodeTimeoutError, HeroSMSError


def _plus_cfg(**over):
    cfg = {
        "HERO_SMS_API_KEY": "hero-key",
        "HERO_SMS_BASE_URL": "https://hero-sms.com/stubs/handler_api.php",
        "HERO_SMS_PROXY": "",
        "HERO_SMS_SERVICE": "gcash",
        "HERO_SMS_COUNTRY": 6,
        "HERO_SMS_MAX_PRICE": 0.0,
        "HERO_SMS_WAIT_TIMEOUT": 240,
        "HERO_SMS_POLL_INTERVAL": 5,
        "HERO_SMS_PREFER_ALL_SMS": True,
    }
    cfg.update(over)
    return cfg


class HeroProviderTest(unittest.TestCase):
    def setUp(self):
        sms_provider._HERO_PROXY_BY_ACTIVATION.clear()
        sms_provider._ACQUIRED_AT.clear()

    def _patch(self, plus=None, provider="hero"):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("config.codex.SMS_PROVIDER", provider))
        stack.enter_context(patch.dict("config.plus.__dict__", plus or _plus_cfg(), clear=False))
        self.addCleanup(stack.close)

    def test_acquire_number_hero(self):
        self._patch()
        act = Activation(id=12345, phone="+6391712345678", cost=0.35)
        client = MagicMock()
        client.proxy = "http://p1:8080"
        client.get_number.return_value = act
        with patch("core.sms_provider.HeroSMSClient", return_value=client):
            aid, phone = sms_provider.acquire_number()
        self.assertEqual(aid, "12345")
        self.assertEqual(phone, "6391712345678")
        client.get_number.assert_called_once_with(service="gcash", country=6, max_price=None)
        self.assertEqual(sms_provider._HERO_PROXY_BY_ACTIVATION["12345"], "http://p1:8080")
        self.assertIn("12345", sms_provider._ACQUIRED_AT)

    def test_acquire_number_uses_env_proxy(self):
        self._patch(plus=_plus_cfg(HERO_SMS_PROXY="http://env:3128"))
        act = Activation(id=1, phone="6391")
        client = MagicMock()
        client.proxy = "http://env:3128"
        client.get_number.return_value = act
        with patch("core.sms_provider.HeroSMSClient", return_value=client) as cls, \
             patch("config.proxy.pick_proxy", return_value="http://pool:8080") as pool:
            sms_provider.acquire_number()
        self.assertEqual(cls.call_args.kwargs["proxy"], "http://env:3128")
        pool.assert_not_called()

    def test_acquire_number_rotates_from_pool(self):
        self._patch()
        act = Activation(id=2, phone="6392")
        client = MagicMock()
        client.proxy = "http://pool:8080"
        client.get_number.return_value = act
        with patch("core.sms_provider.HeroSMSClient", return_value=client) as cls, \
             patch("config.proxy.pick_proxy", return_value="http://pool:8080"):
            sms_provider.acquire_number()
        self.assertEqual(cls.call_args.kwargs["proxy"], "http://pool:8080")

    def test_wait_for_sms_code_hero(self):
        self._patch()
        client = MagicMock()
        client.wait_for_code.return_value = "481516"
        with patch("core.sms_provider.HeroSMSClient", return_value=client):
            code = sms_provider.wait_for_sms_code("12345")
        self.assertEqual(code, "481516")
        client.wait_for_code.assert_called_once()
        self.assertEqual(client.wait_for_code.call_args.args[0], 12345)

    def test_wait_for_sms_code_timeout_maps(self):
        self._patch()
        client = MagicMock()
        client.wait_for_code.side_effect = CodeTimeoutError("等待验证码超时")
        with patch("core.sms_provider.HeroSMSClient", return_value=client):
            with self.assertRaises(sms_provider.SmsCodeTimeout):
                sms_provider.wait_for_sms_code("12345")

    def test_wait_for_sms_code_api_error_maps(self):
        self._patch()
        client = MagicMock()
        client.wait_for_code.side_effect = HeroSMSError("NO_ACTIVATION")
        with patch("core.sms_provider.HeroSMSClient", return_value=client):
            with self.assertRaises(sms_provider.SmsProviderError):
                sms_provider.wait_for_sms_code("12345")

    def test_complete_and_cancel_hero(self):
        self._patch()
        sms_provider._HERO_PROXY_BY_ACTIVATION["9"] = "http://p9:8080"
        sms_provider._ACQUIRED_AT["9"] = 0.0
        client = MagicMock()
        with patch("core.sms_provider.HeroSMSClient", return_value=client):
            sms_provider.complete("9")
            sms_provider.cancel("9", background=False)
        client.complete.assert_called_once_with(9)
        client.cancel.assert_called_once_with(9)
        self.assertNotIn("9", sms_provider._HERO_PROXY_BY_ACTIVATION)

    def test_cancel_hero_background_dispatches_thread(self):
        self._patch()
        sms_provider._HERO_PROXY_BY_ACTIVATION["10"] = "http://p10:8080"
        sms_provider._ACQUIRED_AT["10"] = 0.0
        client = MagicMock()
        fake_thread = MagicMock()
        with patch("core.sms_provider.HeroSMSClient", return_value=client), \
             patch("core.sms_provider.threading.Thread", return_value=fake_thread) as thread_cls:
            sms_provider.cancel("10", background=True)
        fake_thread.start.assert_called_once()
        self.assertEqual(thread_cls.call_args.kwargs["target"], sms_provider._do_hero_cancel_sync)
        client.cancel.assert_not_called()  # 取消在后台线程执行，主流程不阻塞

    def test_set_status_hero(self):
        self._patch()
        client = MagicMock()
        with patch("core.sms_provider.HeroSMSClient", return_value=client):
            out = sms_provider.set_status("9", 6)
        self.assertEqual(out, "OK")
        client.set_status.assert_called_once_with(9, 6)

    def test_check_availability_hero_no_key_skips(self):
        self._patch(plus=_plus_cfg(HERO_SMS_API_KEY=""))
        r = sms_provider.check_sms_availability()
        self.assertTrue(r["ok"])
        self.assertIn("跳过", r["message"])

    def test_check_availability_hero_ok(self):
        self._patch()
        client = MagicMock()
        client.get_balance.return_value = 4.2
        with patch("core.sms_provider.HeroSMSClient", return_value=client):
            r = sms_provider.check_sms_availability()
        self.assertTrue(r["ok"])
        self.assertEqual(r["balance"], 4.2)

    def test_check_availability_hero_zero_balance(self):
        self._patch()
        client = MagicMock()
        client.get_balance.return_value = 0.0
        with patch("core.sms_provider.HeroSMSClient", return_value=client):
            r = sms_provider.check_sms_availability()
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
