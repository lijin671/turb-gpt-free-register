# -*- coding: utf-8 -*-
"""run_zero_plus 主流程编排测试（阶段2→7.5）。

mock 掉网络/绑卡子步骤，验证主流程的编排逻辑：
session 校验、切菲/SetupIntent/绑卡/验证/激活的调用顺序与失败分支、
缺卡、双路径绑卡兜底、激活开关、country_locked 消息等。
"""
import unittest
from unittest.mock import patch

from core import plus_zero as pz


def _switch(ps):
    ps.checkout_session_id = "cs_1"
    ps.checkout_url = "/checkout/openai_llc/cs_1"
    ps.billing_country = "PH"
    ps.zero_price = True
    ps.country_locked = False


def _switch_locked(ps):
    ps.checkout_session_id = "cs_1"
    ps.checkout_url = "/checkout/openai_llc/cs_1"
    ps.billing_country = "SG"
    ps.zero_price = False
    ps.country_locked = True


def _setup_intent(ps):
    ps.client_secret = "seti_1_secret_xyz"


def _bind(ps, *args):
    ps.payment_method_id = "pm_1"


def _verify_cards(ps):
    ps.cards_bound = [{"id": "pm_1", "brand": "visa", "last4": "4242", "default": True}]


def _verify_plus(ps):
    ps.subscription_active = True


def _verify_free(ps):
    ps.subscription_active = False


@patch("config.plus.ZERO_PLUS_BIND_MODE", "api")
class RunZeroPlusTests(unittest.TestCase):
    COMMON = dict(access_token="tok", account_id="acc", email="a@b.com",
                  proxy="http://127.0.0.1:2260", device_id="dev-1",
                  session_info={"account": {"id": "acc"}})

    def _call(self, **kw):
        return pz.run_zero_plus(**{**self.COMMON, **kw})

    def test_full_success(self):
        with patch.object(pz, "switch_to_philippines", side_effect=_switch) as sw, \
             patch.object(pz, "create_setup_intent", side_effect=_setup_intent) as si, \
             patch.object(pz, "bind_card_via_stripe_api", side_effect=_bind) as bc, \
             patch.object(pz, "verify_payment_methods", side_effect=_verify_cards), \
             patch.object(pz, "verify_subscription", side_effect=_verify_plus), \
             patch.object(pz, "get_us_address", return_value={"street": "1 Test St"}):
            r = self._call(card_number="4242424242424242", exp_month="12", exp_year="2029", cvc="123")
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "success")
        self.assertIn("零元", r["message"])
        self.assertTrue(r["subscription_active"])
        sw.assert_called_once(); si.assert_called_once(); bc.assert_called_once()
        # 已激活则不触发 7.5 激活
        with patch.object(pz, "activate_plus_subscription") as act:
            pass
        self.assertEqual(r["card_used"], "424242******4242")

    def test_success_no_activation_call_when_already_plus(self):
        with patch.object(pz, "switch_to_philippines", side_effect=_switch), \
             patch.object(pz, "create_setup_intent", side_effect=_setup_intent), \
             patch.object(pz, "bind_card_via_stripe_api", side_effect=_bind), \
             patch.object(pz, "verify_payment_methods", side_effect=_verify_cards), \
             patch.object(pz, "verify_subscription", side_effect=_verify_plus), \
             patch.object(pz, "activate_plus_subscription") as act:
            r = self._call(card_number="4242424242424242", exp_month="12", exp_year="2029", cvc="123")
        self.assertTrue(r["ok"])
        act.assert_not_called()

    def test_activation_triggered_when_not_active(self):
        calls = {"n": 0}
        def _verify_sequence(ps):
            calls["n"] += 1
            ps.subscription_active = calls["n"] >= 2  # 第一次 free，激活后 plus
        with patch.object(pz, "switch_to_philippines", side_effect=_switch), \
             patch.object(pz, "create_setup_intent", side_effect=_setup_intent), \
             patch.object(pz, "bind_card_via_stripe_api", side_effect=_bind), \
             patch.object(pz, "verify_payment_methods", side_effect=_verify_cards), \
             patch.object(pz, "verify_subscription", side_effect=_verify_sequence), \
             patch.object(pz, "activate_plus_subscription",
                          return_value={"ok": True, "status": "succeeded"}) as act:
            r = self._call(card_number="4242424242424242", exp_month="12", exp_year="2029", cvc="123")
        self.assertTrue(r["ok"])
        act.assert_called_once()
        self.assertEqual(calls["n"], 2)

    def test_activation_disabled_skips(self):
        with patch.object(pz, "switch_to_philippines", side_effect=_switch), \
             patch.object(pz, "create_setup_intent", side_effect=_setup_intent), \
             patch.object(pz, "bind_card_via_stripe_api", side_effect=_bind), \
             patch.object(pz, "verify_payment_methods", side_effect=_verify_cards), \
             patch.object(pz, "verify_subscription", side_effect=_verify_free), \
             patch("config.plus.ZERO_PLUS_ACTIVATE_AFTER_BIND", False), \
             patch.object(pz, "activate_plus_subscription") as act:
            r = self._call(card_number="4242424242424242", exp_month="12", exp_year="2029", cvc="123")
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], "pending")
        act.assert_not_called()

    def test_country_locked_message(self):
        with patch.object(pz, "switch_to_philippines", side_effect=_switch_locked), \
             patch.object(pz, "create_setup_intent", side_effect=_setup_intent), \
             patch.object(pz, "bind_card_via_stripe_api", side_effect=_bind), \
             patch.object(pz, "verify_payment_methods", side_effect=_verify_cards), \
             patch.object(pz, "verify_subscription", side_effect=_verify_plus), \
             patch.object(pz, "get_us_address", return_value={}):
            r = self._call(card_number="4242424242424242", exp_month="12", exp_year="2029", cvc="123")
        self.assertTrue(r["ok"])
        self.assertIn("降级", r["message"])

    def test_session_validation_failure(self):
        with patch.object(pz, "fetch_session", side_effect=RuntimeError("token revoked")):
            r = self._call(session_info=None)
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], "failed")
        self.assertIn("Session", r["message"])

    def test_switch_failure(self):
        with patch.object(pz, "switch_to_philippines", side_effect=RuntimeError("400")), \
             patch.object(pz, "fetch_session") as fs:
            fs.return_value = {"account": {"id": "acc"}}
            r = self._call()
        self.assertFalse(r["ok"])
        self.assertIn("切菲律宾", r["message"])

    def test_missing_card_returns_need_card(self):
        with patch.object(pz, "switch_to_philippines", side_effect=_switch), \
             patch.object(pz, "create_setup_intent", side_effect=_setup_intent):
            r = self._call()
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], "need_card")
        self.assertEqual(r["checkout_url"], "/checkout/openai_llc/cs_1")

    def test_api_bind_fail_browser_fallback(self):
        with patch.object(pz, "switch_to_philippines", side_effect=_switch), \
             patch.object(pz, "create_setup_intent", side_effect=_setup_intent), \
             patch.object(pz, "bind_card_via_stripe_api", side_effect=RuntimeError("declined")), \
             patch.object(pz, "_browser_bind_card", side_effect=_bind), \
             patch.object(pz, "verify_payment_methods", side_effect=_verify_cards), \
             patch.object(pz, "verify_subscription", side_effect=_verify_plus), \
             patch("config.plus.ZERO_PLUS_BIND_MODE", "api"):
            r = self._call(card_number="4242424242424242", exp_month="12", exp_year="2029", cvc="123")
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "success")

    def test_bind_both_fail(self):
        with patch.object(pz, "switch_to_philippines", side_effect=_switch), \
             patch.object(pz, "create_setup_intent", side_effect=_setup_intent), \
             patch.object(pz, "bind_card_via_stripe_api", side_effect=RuntimeError("declined")), \
             patch.object(pz, "_browser_bind_card", side_effect=RuntimeError("timeout")), \
             patch("config.plus.ZERO_PLUS_BIND_MODE", "api"):
            r = self._call(card_number="4242424242424242", exp_month="12", exp_year="2029", cvc="123")
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], "bind_failed")

    def test_fetch_session_called_when_no_session_info(self):
        with patch.object(pz, "switch_to_philippines", side_effect=_switch), \
             patch.object(pz, "create_setup_intent", side_effect=_setup_intent), \
             patch.object(pz, "bind_card_via_stripe_api", side_effect=_bind), \
             patch.object(pz, "verify_payment_methods", side_effect=_verify_cards), \
             patch.object(pz, "verify_subscription", side_effect=_verify_plus), \
             patch.object(pz, "fetch_session",
                          return_value={"account": {"id": "acc_2"}}) as fs:
            r = pz.run_zero_plus(access_token="tok", account_id="acc_1", email="a@b.com",
                                 card_number="4242424242424242", exp_month="12",
                                 exp_year="2029", cvc="123")
        self.assertTrue(r["ok"])
        fs.assert_called_once()


if __name__ == "__main__":
    unittest.main()
