# -*- coding: utf-8 -*-
"""core.hero_sms 单元测试（mock 掉网络层）。"""
import unittest
from unittest.mock import MagicMock

from core import hero_sms


class FakeResp:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


class HeroSMSClientTest(unittest.TestCase):
    def _client(self, responses: list[str]) -> hero_sms.HeroSMSClient:
        session = MagicMock()
        session.proxies = {}
        session.get.side_effect = [FakeResp(t) for t in responses]
        return hero_sms.HeroSMSClient(api_key="test-key", session=session)

    def test_get_balance_legacy(self):
        c = self._client(["ACCESS_BALANCE:12.34"])
        self.assertAlmostEqual(c.get_balance(), 12.34)

    def test_get_balance_json(self):
        c = self._client(['{"amount": 5.5}'])
        self.assertAlmostEqual(c.get_balance(), 5.5)

    def test_get_number_legacy(self):
        c = self._client(["ACCESS_NUMBER:12345678:6391712345678"])
        act = c.get_number(service="gcash", country=6)
        self.assertEqual(act.id, 12345678)
        self.assertEqual(act.phone, "6391712345678")
        self.assertEqual(act.service, "gcash")

    def test_get_number_v2_fallback(self):
        c = self._client(["NO_NUMBERS", "ACCESS_NUMBER:99:639123"])
        act = c.get_number(service="gcash", country=6)
        self.assertEqual(act.id, 99)

    def test_no_numbers_raises(self):
        c = self._client(["NO_NUMBERS", "NO_NUMBERS"])
        with self.assertRaises(hero_sms.NoNumbersAvailableError):
            c.get_number(service="gcash", country=6)

    def test_insufficient_funds_raises(self):
        c = self._client(["NO_BALANCE"])
        with self.assertRaises(hero_sms.InsufficientFundsError):
            c.get_balance()

    def test_bad_key_raises(self):
        c = self._client(["BAD_KEY"])
        with self.assertRaises(hero_sms.InvalidApiKeyError):
            c.get_balance()

    def test_wait_for_code_status_ok(self):
        c = self._client(["STATUS_WAIT_CODE", "STATUS_WAIT_CODE", "STATUS_OK:481516"])
        code = c.wait_for_code(1, timeout=30, poll_interval=0, prefer_all_sms=False)
        self.assertEqual(code, "481516")

    def test_wait_for_code_all_sms(self):
        c = self._client([
            '{"data": []}',
            '{"data": [{"id":"1","phoneFrom":"+639","code":"1234","text":"Your code","service":"gcash","date":"2026-08-07","type":"sms"}]}',
        ])
        code = c.wait_for_code(1, timeout=30, poll_interval=0, prefer_all_sms=True)
        self.assertEqual(code, "1234")

    def test_wait_for_code_timeout(self):
        session = MagicMock()
        session.proxies = {}
        session.get.side_effect = lambda *a, **k: FakeResp("STATUS_WAIT_CODE")
        c = hero_sms.HeroSMSClient(api_key="test-key", session=session)
        with self.assertRaises(hero_sms.HeroSMSError):
            c.wait_for_code(1, timeout=0.01, poll_interval=0, prefer_all_sms=False)

    def test_get_all_sms_parses(self):
        c = self._client(['{"data": [{"id":"7","phoneFrom":"+639","code":"8888","text":"x","service":"gcash","date":"d","type":"sms"}]}'])
        items = c.get_all_sms(7)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].code, "8888")

    def test_complete_cancel(self):
        c = self._client(["ACCESS_ACTIVATION", "ACCESS_CANCEL"])
        self.assertTrue(c.complete(1))
        self.assertTrue(c.cancel(1))

    def test_extract_code_from_text(self):
        self.assertEqual(hero_sms.extract_code_from_text("Your OTP is 482913"), "482913")
        self.assertIsNone(hero_sms.extract_code_from_text("no code here"))

    def test_find_country_id(self):
        c = self._client(['{"countries": [{"id": 6, "eng": "Philippines", "chn": "菲律宾"}]}',
                          '{"countries": [{"id": 6, "eng": "Philippines", "chn": "菲律宾"}]}'])
        self.assertEqual(c.find_country_id("philippines"), 6)
        self.assertEqual(c.find_country_id("菲律宾"), 6)

    def test_find_service_code(self):
        c = self._client(['{"services": [{"code": "gcash", "name": "GCash"}]}'])
        self.assertEqual(c.find_service_code("gcash"), "gcash")


if __name__ == "__main__":
    unittest.main()
