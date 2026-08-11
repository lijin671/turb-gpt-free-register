# -*- coding: utf-8 -*-
"""core/session BrowserSession 熔断器测试（构造最小实例，无网络）。"""
import unittest
from unittest.mock import patch

from core.session import BrowserSession


class _FakeResp:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _minimal_session():
    sess = BrowserSession.__new__(BrowserSession)
    sess.blocked_until = 0.0
    sess.blocked_reason = ""
    sess.cf_challenge_count = 0
    sess.cf_cookie_snapshot = lambda: {}
    return sess


class SessionCircuitBreakerTests(unittest.TestCase):
    def test_is_cf_challenge(self):
        self.assertTrue(BrowserSession._is_cf_challenge(
            _FakeResp(403, "<title>Just a moment...</title>")))
        self.assertTrue(BrowserSession._is_cf_challenge(
            _FakeResp(503, "cf-chl-abc")))
        self.assertFalse(BrowserSession._is_cf_challenge(_FakeResp(403, "plain")))
        self.assertFalse(BrowserSession._is_cf_challenge(_FakeResp(429, "rate")))
        self.assertFalse(BrowserSession._is_cf_challenge(None))

    def test_parse_retry_after(self):
        self.assertEqual(BrowserSession._parse_retry_after("120"), 120)
        self.assertEqual(BrowserSession._parse_retry_after(""), 0)
        self.assertEqual(BrowserSession._parse_retry_after("abc"), 0)
        self.assertEqual(BrowserSession._parse_retry_after(None), 0)

    def test_403_opens_circuit_and_blocks_next(self):
        sess = _minimal_session()
        with patch("core.session.time.time", return_value=1000.0):
            sess._observe_response_for_circuit_breaker(
                _FakeResp(403, "blocked"), "https://chatgpt.com/backend-api/me")
        self.assertGreaterEqual(sess.blocked_until, 1000.0 + 899)
        self.assertLessEqual(sess.blocked_until, 1000.0 + 901)
        self.assertIn("HTTP 403", sess.blocked_reason)
        with patch("core.session.time.time", return_value=1000.0):
            with self.assertRaisesRegex(RuntimeError, "熔断冷却"):
                sess._raise_if_circuit_open()

    def test_429_uses_retry_after(self):
        sess = _minimal_session()
        with patch("core.session.time.time", return_value=1000.0):
            sess._observe_response_for_circuit_breaker(
                _FakeResp(429, "rate", headers={"retry-after": "60"}),
                "https://chatgpt.com/backend-api/me")
        self.assertAlmostEqual(sess.blocked_until, 1060.0)

    def test_cf_challenge_short_cooldown_and_counter(self):
        sess = _minimal_session()
        with patch("core.session.time.time", return_value=1000.0):
            resp = sess._observe_response_for_circuit_breaker(
                _FakeResp(403, "<title>Just a moment</title>"),
                "https://chatgpt.com/backend-api/sentinel/req")
        self.assertIs(resp, resp)
        self.assertEqual(sess.cf_challenge_count, 1)
        self.assertLessEqual(sess.blocked_until, 1000.0 + 120)
        self.assertIn("CF challenge", sess.blocked_reason)


if __name__ == "__main__":
    unittest.main()
