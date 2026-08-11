# -*- coding: utf-8 -*-
"""Plus 绑卡链路（阶段3-7）测试：切菲/降级、SetupIntent、Stripe 绑卡、验证。

覆盖：切菲成功、切菲国家锁定降级、切菲失败、SetupIntent 创建、publishable_key
解析优先级、PaymentMethod+SetupIntent 绑卡、payment_methods 验证、订阅状态验证。
"""
import json
import unittest
from unittest.mock import patch

from core import plus_zero as pz


class FakeResp:
    def __init__(self, ok=True, status=200, data=None, text=None):
        self.ok = ok
        self.status_code = status
        self._data = data if data is not None else {}
        self.text = text if text is not None else json.dumps(self._data)

    def json(self):
        return self._data


class FakeBrowserSession:
    """替代 BrowserSession：构造 + get/post 返回 FakeResp。"""
    def __init__(self, proxy=None, device_id=None, **kwargs):
        self.proxy = proxy
        self.device_id = device_id

    def get(self, url, headers=None, **kwargs):
        raise NotImplementedError

    def post(self, url, headers=None, **kwargs):
        raise NotImplementedError


def make_session():
    ps = pz.PlusSession(access_token="tok_abc", account_id="acc_1", email="a@b.com",
                        proxy="http://127.0.0.1:2260", device_id="dev-1")
    return ps


class SwitchToPhilippinesTests(unittest.TestCase):
    def test_switch_success(self):
        ps = make_session()
        with patch.object(pz, "_plus_request_with_retry", return_value=FakeResp(
                200, data={"checkout_session_id": "cs_ph1",
                           "publishable_key": "pk_live_" + "x" * 120})) as m:
            cid = pz.switch_to_philippines(ps)
        self.assertEqual(cid, "cs_ph1")
        self.assertEqual(ps.checkout_session_id, "cs_ph1")
        self.assertFalse(ps.country_locked)
        self.assertEqual(ps.billing_country, "PH")
        self.assertTrue(ps.zero_price)
        self.assertTrue(ps.publishable_key.startswith("pk_live_"))
        payload = m.call_args.kwargs["payload"] if "payload" in m.call_args.kwargs else m.call_args.args[4]
        self.assertEqual(payload["billing_details"]["country"], "PH")
        self.assertEqual(payload["billing_details"]["currency"], "PHP")

    def test_switch_country_lock_fallback(self):
        ps = make_session()
        locked = FakeResp(False, 400, data={"detail": "Billing country must match request country"},
                          text="Billing country must match request country")
        ok = FakeResp(200, data={"checkout_session_id": "cs_sg1"})
        with patch.object(pz, "_plus_request_with_retry", side_effect=[locked, ok]) as m, \
             patch.object(pz, "detect_account_country", return_value=("SG", "SGD")):
            cid = pz.switch_to_philippines(ps)
        self.assertEqual(cid, "cs_sg1")
        self.assertTrue(ps.country_locked)
        self.assertEqual(ps.billing_country, "SG")
        self.assertFalse(ps.zero_price)
        # 第二次请求 payload 用降级国家
        second_payload = m.call_args_list[1].kwargs["payload"] if "payload" in m.call_args_list[1].kwargs else m.call_args_list[1].args[4]
        self.assertEqual(second_payload["billing_details"], {"country": "SG", "currency": "SGD"})

    def test_switch_country_lock_no_fallback_raises(self):
        ps = make_session()
        locked = FakeResp(False, 400, data={"detail": "Billing country must match request country"},
                          text="Billing country must match request country")
        with patch.object(pz, "_plus_request_with_retry", return_value=locked), \
             patch.object(pz, "detect_account_country", return_value=("SG", "SGD")), \
             patch("config.plus.ZERO_PLUS_COUNTRY_LOCK_FALLBACK", False):
            with self.assertRaises(RuntimeError):
                pz.switch_to_philippines(ps)

    def test_switch_failure_raises(self):
        ps = make_session()
        with patch.object(pz, "_plus_request_with_retry",
                          return_value=FakeResp(False, 500, data={"detail": "boom"})):
            with self.assertRaises(RuntimeError):
                pz.switch_to_philippines(ps)


class CreateSetupIntentTests(unittest.TestCase):
    def test_create_success(self):
        ps = make_session()
        with patch.object(pz, "_plus_request_with_retry",
                          return_value=FakeResp(200, data={"client_secret": "seti_1_secret_xyz"})) as m:
            secret = pz.create_setup_intent(ps)
        self.assertEqual(secret, "seti_1_secret_xyz")
        self.assertEqual(ps.client_secret, secret)
        headers = m.call_args.args[3]
        self.assertEqual(headers.get("chatgpt-account-id"), "acc_1")
        payload = m.call_args.kwargs["payload"] if "payload" in m.call_args.kwargs else m.call_args.args[4]
        self.assertEqual(payload, {"account_id": "acc_1"})

    def test_create_failure_raises(self):
        ps = make_session()
        with patch.object(pz, "_plus_request_with_retry",
                          return_value=FakeResp(False, 400, data={"detail": "no"})):
            with self.assertRaises(RuntimeError):
                pz.create_setup_intent(ps)


class ResolvePublishableKeyTests(unittest.TestCase):
    def test_uses_session_key_first(self):
        ps = make_session()
        ps.publishable_key = "pk_live_" + "k" * 120
        self.assertEqual(pz._resolve_publishable_key("whatever", ps), ps.publishable_key)

    def test_fragment_match(self):
        key = pz._resolve_publishable_key("seti_KslHRdbaPg_secret_x", None)
        self.assertEqual(key, pz.KNOWN_STRIPE_KEYS[0])

    def test_fallback_default(self):
        key = pz._resolve_publishable_key("seti_unknown_secret_x", None)
        self.assertEqual(key, pz.KNOWN_STRIPE_KEYS[0])


class BindCardViaStripeApiTests(unittest.TestCase):
    def test_bind_success(self):
        ps = make_session()
        ps.client_secret = "seti_1_secret_xyz"
        pm_resp = {"id": "pm_1"}
        si_resp = {"status": "succeeded"}
        with patch.object(pz, "_resolve_publishable_key", return_value="pk_test_x"), \
             patch.object(pz, "_stripe_api_request", side_effect=[pm_resp, si_resp]) as m:
            pm_id = pz.bind_card_via_stripe_api(ps, "4242424242424242", "12", "2029", "123")
        self.assertEqual(pm_id, "pm_1")
        self.assertEqual(ps.payment_method_id, "pm_1")
        # 第一次调用: payment_methods；第二次: setup_intents confirm
        self.assertEqual(m.call_args_list[0].args[1], "/payment_methods")
        self.assertEqual(m.call_args_list[1].args[1], "/setup_intents/seti_1/confirm")
        pm_data = m.call_args_list[0].args[3]
        self.assertEqual(pm_data["card[number]"], "4242424242424242")
        self.assertEqual(pm_data["billing_details[name]"], "CHATGPT USER")

    def test_bind_setupintent_failed_raises(self):
        ps = make_session()
        ps.client_secret = "seti_1_secret_xyz"
        with patch.object(pz, "_resolve_publishable_key", return_value="pk_test_x"), \
             patch.object(pz, "_stripe_api_request", side_effect=[
                 {"id": "pm_1"},
                 {"status": "requires_action", "last_setup_error": {"message": "3DS needed"}},
             ]):
            with self.assertRaises(RuntimeError) as ctx:
                pz.bind_card_via_stripe_api(ps, "4242424242424242", "12", "2029", "123")
        self.assertIn("3DS", str(ctx.exception))

    def test_bind_pm_creation_failed_raises(self):
        ps = make_session()
        ps.client_secret = "seti_1_secret_xyz"
        with patch.object(pz, "_resolve_publishable_key", return_value="pk_test_x"), \
             patch.object(pz, "_stripe_api_request", return_value={"error": "bad"}):
            with self.assertRaises(RuntimeError):
                pz.bind_card_via_stripe_api(ps, "4242424242424242", "12", "2029", "123")


class VerifyPaymentMethodsTests(unittest.TestCase):
    def test_verify_cards(self):
        ps = make_session()
        data = {
            "payment_methods": [
                {"id": "pm_1", "type": "card", "card": {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2029}},
                {"id": "pm_2", "type": "card", "card": {"brand": "mastercard", "last4": "4444", "exp_month": 1, "exp_year": 2030}},
            ],
            "default_payment_method_id": "pm_2",
        }
        fake = FakeBrowserSession()
        fake.get = lambda url, headers=None, **kw: FakeResp(200, data=data)
        with patch("core.session.BrowserSession", return_value=fake):
            cards = pz.verify_payment_methods(ps)
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]["brand"], "visa")
        self.assertFalse(cards[0]["default"])
        self.assertTrue(cards[1]["default"])
        self.assertEqual(ps.cards_bound, cards)


class VerifySubscriptionTests(unittest.TestCase):
    def test_plus_detected(self):
        ps = make_session()
        fake = FakeBrowserSession()
        fake.get = lambda url, headers=None, **kw: FakeResp(
            200, data={"user": {"plan": {"title": "ChatGPT Plus"}}})
        with patch("core.session.BrowserSession", return_value=fake):
            result = pz.verify_subscription(ps)
        self.assertTrue(result["is_plus"])
        self.assertTrue(ps.subscription_active)

    def test_free_plan(self):
        ps = make_session()
        fake = FakeBrowserSession()
        fake.get = lambda url, headers=None, **kw: FakeResp(
            200, data={"user": {"plan": {"title": "ChatGPT Free"}}})
        with patch("core.session.BrowserSession", return_value=fake):
            result = pz.verify_subscription(ps)
        self.assertFalse(result["is_plus"])
        self.assertFalse(ps.subscription_active)


if __name__ == "__main__":
    unittest.main()
