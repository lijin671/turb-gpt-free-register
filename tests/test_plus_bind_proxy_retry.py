# -*- coding: utf-8 -*-
"""Plus 绑卡脏 IP 自动换出口 IP 重试测试（参考论坛：Payment was not approved = ip 不干净）。"""
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from core import plus_zero as pz
from core.plus_zero import classify_bind_failure


class ClassifyBindFailureTests(unittest.TestCase):
    def test_dirty_ip_markers(self):
        self.assertEqual(classify_bind_failure("Payment was not approved"), "dirty_ip")
        self.assertEqual(classify_bind_failure("card_declined: Your card was declined"), "dirty_ip")
        self.assertEqual(classify_bind_failure("payment_method_not_approved"), "dirty_ip")
        self.assertEqual(classify_bind_failure("do not honor"), "dirty_ip")

    def test_challenge_markers(self):
        self.assertEqual(classify_bind_failure("requires_action"), "challenge")
        self.assertEqual(classify_bind_failure("hcaptcha challenge required"), "challenge")
        self.assertEqual(classify_bind_failure("authentication required (3ds)"), "challenge")

    def test_other(self):
        self.assertEqual(classify_bind_failure("network error"), "other")
        self.assertEqual(classify_bind_failure(""), "other")


def _switch(ps):
    ps.checkout_session_id = "cs_1"
    ps.checkout_url = "/checkout/openai_llc/cs_1"
    ps.billing_country = "PH"
    ps.zero_price = True
    ps.country_locked = False


def _setup_intent(ps):
    ps.client_secret = "seti_1_secret_xyz"


def _bind(ps, *args):
    ps.payment_method_id = "pm_1"


def _verify_cards(ps):
    ps.cards_bound = [{"id": "pm_1"}]


def _verify_plus(ps):
    ps.subscription_active = True


COMMON = dict(access_token="tok", account_id="acc", email="a@b.com",
              proxy="http://127.0.0.1:2260", device_id="dev-1",
              session_info={"account": {"id": "acc"}})


def _stack(patchers):
    st = ExitStack()
    for p in patchers:
        st.enter_context(p)
    return st


class RunZeroPlusBindRetryTests(unittest.TestCase):
    def _call(self, **kw):
        return pz.run_zero_plus(**{**COMMON, **kw})

    def _patches(self, bind_side_effect, retries=2, rotate_return="http://new-proxy:3000"):
        return [
            patch.object(pz, "switch_to_philippines", side_effect=_switch),
            patch.object(pz, "create_setup_intent", side_effect=_setup_intent),
            patch.object(pz, "bind_card_via_stripe_api", side_effect=bind_side_effect),
            patch.object(pz, "_rotate_plus_proxy", return_value=rotate_return),
            patch.object(pz, "verify_payment_methods", side_effect=_verify_cards),
            patch.object(pz, "verify_subscription", side_effect=_verify_plus),
            patch("config.plus.ZERO_PLUS_BIND_MODE", "api"),
            patch("config.plus.ZERO_PLUS_BIND_PROXY_RETRIES", retries),
        ]

    def test_dirty_ip_rotates_proxy_and_retries_api(self):
        calls = {"n": 0}

        def _flaky(ps, *args):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("Payment was not approved")
            _bind(ps, *args)

        with _stack(self._patches(_flaky)):
            r = self._call(card_number="4242424242424242", exp_month="12",
                           exp_year="2029", cvc="123")
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "success")
        self.assertEqual(calls["n"], 3)

    def test_dirty_ip_exhausted_then_browser_fallback(self):
        with _stack(self._patches(
            bind_side_effect=RuntimeError("Payment was not approved"),
            retries=1,
        ) + [patch.object(pz, "_browser_bind_card", side_effect=_bind)]):
            r = self._call(card_number="4242424242424242", exp_month="12",
                           exp_year="2029", cvc="123")
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "success")

    def test_non_dirty_failure_skips_rotation(self):
        with _stack(self._patches(bind_side_effect=RuntimeError("network error"), retries=2)
                    + [patch.object(pz, "_browser_bind_card", side_effect=_bind)]):
            rotate = pz._rotate_plus_proxy
            r = self._call(card_number="4242424242424242", exp_month="12",
                           exp_year="2029", cvc="123")
        self.assertTrue(r["ok"])
        rotate.assert_not_called()

    def test_retries_zero_no_rotation(self):
        with _stack(self._patches(bind_side_effect=RuntimeError("Payment was not approved"), retries=0)
                    + [patch.object(pz, "_browser_bind_card", side_effect=_bind)]):
            rotate = pz._rotate_plus_proxy
            r = self._call(card_number="4242424242424242", exp_month="12",
                           exp_year="2029", cvc="123")
        self.assertTrue(r["ok"])
        rotate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
