# -*- coding: utf-8 -*-
"""订阅激活（checkout confirm）链路测试。

覆盖 plus_zero 阶段 7.1→7.5：fetch checkout state → update plan →
snapshot 账单地址 → Stripe ConfirmationToken → confirm；
以及 conditional_offer_preflight 二次 confirm、custom 降级、blocked 判定、
非 2xx 必须 raise（防止 401 被误判成功）等关键行为。
"""
import json
import unittest
from unittest.mock import patch

from core import plus_zero as pz

PlusSession = pz.PlusSession


class FakeResp:
    def __init__(self, ok=True, status=200, data=None, text=None):
        self.ok = ok
        self.status_code = status
        self._data = data if data is not None else {}
        self.text = text if text is not None else json.dumps(self._data)

    def json(self):
        return self._data


def make_session(payment_method_id="pm_123", checkout_session_id="cs_test_1"):
    ps = PlusSession(access_token="tok_abc", account_id="acc_1", email="a@b.com",
                     proxy="http://127.0.0.1:2260", device_id="dev-1")
    ps.checkout_session_id = checkout_session_id
    ps.payment_method_id = payment_method_id
    ps.client_secret = "seti_123_secret_xyz"
    return ps


class CheckoutProcessorEntityTests(unittest.TestCase):
    def test_entity_prefix(self):
        self.assertEqual(pz._checkout_processor_entity("oaics_abc"), "oaics")
        self.assertEqual(pz._checkout_processor_entity("cs_live_abc"), "stripe")
        self.assertEqual(pz._checkout_processor_entity(""), "stripe")


class FetchCheckoutStateTests(unittest.TestCase):
    def test_fetch_state_stores_and_url(self):
        ps = make_session()
        with patch.object(pz, "_plus_request_with_retry", return_value=FakeResp(200, data={"status": "open"})) as m:
            data = pz._fetch_checkout_state(ps)
        self.assertEqual(data["status"], "open")
        self.assertEqual(ps.checkout_state, data)
        url = m.call_args.args[2]
        self.assertIn("/backend-api/payments/checkout/stripe/cs_test_1", url)

    def test_fetch_state_does_not_raise_on_http_error(self):
        ps = make_session()
        with patch.object(pz, "_plus_request_with_retry", return_value=FakeResp(False, 500, data={"detail": "x"})):
            data = pz._fetch_checkout_state(ps)
        self.assertEqual(data["detail"], "x")
        self.assertEqual(ps.checkout_state, data)


class UpdateCheckoutPlanTests(unittest.TestCase):
    def test_update_plan_payload(self):
        ps = make_session()
        with patch.object(pz, "_plus_request_with_retry",
                          return_value=FakeResp(200, data={"checkout_session": {"id": "cs_test_1"}})) as m:
            self.assertTrue(pz._update_checkout_plan(ps))
        payload = m.call_args.args[4] if len(m.call_args.args) > 4 else m.call_args.kwargs.get("payload")
        self.assertEqual(payload["checkout_session_id"], "cs_test_1")
        self.assertEqual(payload["processor_entity"], "stripe")
        self.assertEqual(payload["plan_name"], "plus")
        self.assertEqual(payload["price_interval"], "month")
        self.assertEqual(payload["promo_campaign"]["promo_campaign_id"], "plus-1-month-free")
        self.assertEqual(ps.checkout_state, {"id": "cs_test_1"})

    def test_update_plan_failure_is_best_effort(self):
        ps = make_session()
        with patch.object(pz, "_plus_request_with_retry", return_value=FakeResp(False, 400, data={"detail": "bad"})):
            self.assertFalse(pz._update_checkout_plan(ps))


class BillingAddressTests(unittest.TestCase):
    def test_build_address_follows_billing_country(self):
        ps = make_session()
        ps.billing_country = "PH"
        with patch.object(pz, "get_us_address",
                          return_value={"street": "1 Test St", "city": "Quezon", "state": "NCR", "zip": "1000"}):
            billing = pz._build_billing_address(ps)
        self.assertEqual(billing["country"], "PH")
        self.assertEqual(billing["line1"], "1 Test St")
        self.assertEqual(ps.billing_address, billing)

    def test_submit_snapshot_ok(self):
        ps = make_session()
        with patch.object(pz, "_plus_request_with_retry", return_value=FakeResp(200)) as m, \
             patch.object(pz, "get_us_address", return_value={"street": "1 Test St", "city": "NY", "state": "NY", "zip": "10001"}):
            self.assertTrue(pz._submit_checkout_billing_address(ps))
        payload = m.call_args.args[4] if len(m.call_args.args) > 4 else m.call_args.kwargs.get("payload")
        self.assertIn("snapshot", payload)
        self.assertEqual(payload["snapshot"]["billing_address"]["address"]["country"], "US")


class ConfirmationTokenTests(unittest.TestCase):
    def test_create_confirmation_token(self):
        ps = make_session()
        with patch.object(pz, "_resolve_publishable_key", return_value="pk_test_x") as rpk, \
             patch.object(pz, "_stripe_api_request", return_value={"id": "ct_999"}) as m:
            ct = pz._create_stripe_confirmation_token(ps)
        self.assertEqual(ct, "ct_999")
        self.assertEqual(ps.confirm_token, "ct_999")
        self.assertEqual(m.call_args.args[1], "/confirmation_tokens")
        data = m.call_args.args[3]
        self.assertEqual(data["payment_method"], "pm_123")

    def test_create_confirmation_token_failure_raises(self):
        ps = make_session()
        with patch.object(pz, "_resolve_publishable_key", return_value="pk_test_x"), \
             patch.object(pz, "_stripe_api_request", return_value={}):
            with self.assertRaises(RuntimeError):
                pz._create_stripe_confirmation_token(ps)


class ConfirmCheckoutTests(unittest.TestCase):
    def test_confirm_non_2xx_raises(self):
        # 关键回归：401 必须 raise，不能静默当成功
        ps = make_session()
        with patch.object(pz, "_plus_request_with_retry",
                          return_value=FakeResp(False, 401, data={"detail": "unauthorized"})), \
             patch.object(pz, "_build_checkout_sentinel_headers", return_value={}):
            with self.assertRaises(RuntimeError) as ctx:
                pz._confirm_checkout(ps)
        self.assertIn("401", str(ctx.exception))

    def test_confirm_success_with_token(self):
        ps = make_session()
        ps.confirm_token = "ct_1"
        with patch.object(pz, "_plus_request_with_retry",
                          return_value=FakeResp(200, data={"status": "succeeded"})) as m, \
             patch.object(pz, "_build_checkout_sentinel_headers", return_value={}):
            data = pz._confirm_checkout(ps)
        self.assertEqual(data["status"], "succeeded")
        payload = m.call_args.args[4] if len(m.call_args.args) > 4 else m.call_args.kwargs.get("payload")
        self.assertEqual(payload["confirm_token"], "ct_1")
        self.assertEqual(payload["selected_payment_method_type"], "card")

    def test_confirm_custom_payment_method(self):
        ps = make_session()
        with patch.object(pz, "_plus_request_with_retry",
                          return_value=FakeResp(200, data={"status": "succeeded"})) as m, \
             patch.object(pz, "_build_checkout_sentinel_headers", return_value={}):
            pz._confirm_checkout(ps, custom_payment_method=True)
        payload = m.call_args.args[4] if len(m.call_args.args) > 4 else m.call_args.kwargs.get("payload")
        self.assertNotIn("confirm_token", payload)
        self.assertEqual(payload["selected_payment_method_type"], "card")

    def test_confirm_conditional_offer_preflight_second_confirm(self):
        ps = make_session()
        first = FakeResp(200, data={
            "conditional_offer_preflight": True,
            "type": "setup_intent",
            "client_secret": "seti_123_secret_xyz",
        })
        second = FakeResp(200, data={"status": "succeeded"})
        with patch.object(pz, "_plus_request_with_retry", side_effect=[first, second]) as m, \
             patch.object(pz, "_build_checkout_sentinel_headers", return_value={}), \
             patch.object(pz, "_resolve_publishable_key", return_value="pk_test_x"), \
             patch.object(pz, "_stripe_api_request", return_value={"status": "succeeded"}) as sm:
            data = pz._confirm_checkout(ps)
        self.assertEqual(data["status"], "succeeded")
        # 两次 confirm + 一次 setup_intent confirm
        self.assertEqual(m.call_count, 2)
        si_path = sm.call_args.args[1]
        self.assertEqual(si_path, "/setup_intents/seti_123/confirm")
        second_payload = m.call_args_list[1].args[4] if len(m.call_args_list[1].args) > 4 else m.call_args_list[1].kwargs.get("payload")
        self.assertEqual(second_payload, {"checkout_session_id": "cs_test_1"})

    def test_confirm_conditional_offer_setupintent_failed_raises(self):
        ps = make_session()
        first = FakeResp(200, data={
            "conditional_offer_preflight": True,
            "type": "setup_intent",
            "client_secret": "seti_123_secret_xyz",
        })
        with patch.object(pz, "_plus_request_with_retry", return_value=first), \
             patch.object(pz, "_build_checkout_sentinel_headers", return_value={}), \
             patch.object(pz, "_resolve_publishable_key", return_value="pk_test_x"), \
             patch.object(pz, "_stripe_api_request", return_value={"status": "requires_action"}):
            with self.assertRaises(RuntimeError):
                pz._confirm_checkout(ps)


class ActivatePlusSubscriptionTests(unittest.TestCase):
    def test_full_chain_success(self):
        ps = make_session()
        with patch.object(pz, "_fetch_checkout_state") as fcs, \
             patch.object(pz, "_update_checkout_plan", return_value=True) as ucp, \
             patch.object(pz, "_submit_checkout_billing_address", return_value=True) as sca, \
             patch.object(pz, "_create_stripe_confirmation_token",
                          side_effect=lambda ps_: ps_.__setattr__("confirm_token", "ct_1")) as cct, \
             patch.object(pz, "_confirm_checkout",
                          return_value={"status": "succeeded"}) as cc:
            result = pz.activate_plus_subscription(ps)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "succeeded")
        fcs.assert_called_once_with(ps)
        ucp.assert_called_once_with(ps)
        sca.assert_called_once_with(ps)
        cct.assert_called_once_with(ps)
        cc.assert_called_once_with(ps, custom_payment_method=False)

    def test_confirmation_token_failure_degrades_to_custom(self):
        ps = make_session()
        with patch.object(pz, "_fetch_checkout_state"), \
             patch.object(pz, "_update_checkout_plan", return_value=True), \
             patch.object(pz, "_submit_checkout_billing_address", return_value=True), \
             patch.object(pz, "_create_stripe_confirmation_token", side_effect=RuntimeError("stripe down")), \
             patch.object(pz, "_confirm_checkout",
                          return_value={"status": "succeeded"}) as cc:
            result = pz.activate_plus_subscription(ps)
        self.assertTrue(result["ok"])
        self.assertIsNone(ps.confirm_token)
        cc.assert_called_once_with(ps, custom_payment_method=True)

    def test_blocked_status_returns_failure(self):
        ps = make_session()
        with patch.object(pz, "_fetch_checkout_state"), \
             patch.object(pz, "_update_checkout_plan", return_value=True), \
             patch.object(pz, "_submit_checkout_billing_address", return_value=True), \
             patch.object(pz, "_create_stripe_confirmation_token",
                          side_effect=lambda ps_: ps_.__setattr__("confirm_token", "ct_1")), \
             patch.object(pz, "_confirm_checkout", return_value={"status": "blocked"}):
            result = pz.activate_plus_subscription(ps)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")

    def test_missing_payment_method_short_circuits(self):
        ps = make_session(payment_method_id=None)
        with patch.object(pz, "_fetch_checkout_state"), \
             patch.object(pz, "_update_checkout_plan", return_value=True), \
             patch.object(pz, "_submit_checkout_billing_address", return_value=True), \
             patch.object(pz, "_confirm_checkout") as cc:
            result = pz.activate_plus_subscription(ps)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "no_payment_method")
        cc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
