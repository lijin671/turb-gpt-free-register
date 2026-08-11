# -*- coding: utf-8 -*-
"""core.openai_auth.request_sentinel_header_with_retry：turnstile 求解失败自动重试。"""
import unittest
from unittest.mock import patch

from core import openai_auth as oa
from core.openai_auth import request_sentinel_header_with_retry


class FakeSession:
    device_id = "dev-1"
    browser_profile = {}
    sentinel_sid = "sid-1"
    auth_cookie_header = lambda self: "oai-did=dev-1"  # noqa: E731


TURNSTILE_ERR = RuntimeError("sentinel_turnstile_token_failed（纯Python降级）")


class SentinelRetryTests(unittest.TestCase):
    @patch.object(oa, "build_sentinel_header", return_value=("hdr-ok", "so-ok"))
    @patch.object(oa, "request_sentinel_token",
                  side_effect=[TURNSTILE_ERR, {"token": "c2"}])
    def test_turnstile_failure_retries_once(self, req, build):
        header, so = request_sentinel_header_with_retry(FakeSession(), "oauth_create_account")
        self.assertEqual((header, so), ("hdr-ok", "so-ok"))
        self.assertEqual(req.call_count, 2)
        build.assert_called_once()

    @patch.object(oa, "build_sentinel_header", return_value=("hdr-ok", None))
    @patch.object(oa, "request_sentinel_token", return_value={"token": "c1"})
    def test_success_first_try_no_retry(self, req, build):
        header, so = request_sentinel_header_with_retry(FakeSession(), "authorize_continue")
        self.assertEqual(header, "hdr-ok")
        req.assert_called_once()

    @patch.object(oa, "build_sentinel_header")
    @patch.object(oa, "request_sentinel_token", side_effect=RuntimeError("http 500"))
    def test_non_turnstile_error_raises_immediately(self, req, build):
        with self.assertRaisesRegex(RuntimeError, "http 500"):
            request_sentinel_header_with_retry(FakeSession(), "username_password_create")
        req.assert_called_once()
        build.assert_not_called()

    @patch.object(oa, "build_sentinel_header")
    @patch.object(oa, "request_sentinel_token",
                  side_effect=[TURNSTILE_ERR, TURNSTILE_ERR])
    def test_retries_exhausted_raises_last_error(self, req, build):
        with self.assertRaisesRegex(RuntimeError, "sentinel_turnstile_token_failed"):
            request_sentinel_header_with_retry(FakeSession(), "oauth_create_account")
        self.assertEqual(req.call_count, 2)

    @patch.object(oa, "build_sentinel_header")
    @patch.object(oa, "request_sentinel_token", side_effect=TURNSTILE_ERR)
    def test_attempts_one_means_no_retry(self, req, build):
        with self.assertRaisesRegex(RuntimeError, "sentinel_turnstile_token_failed"):
            request_sentinel_header_with_retry(FakeSession(), "oauth_create_account", attempts=1)
        req.assert_called_once()

    @patch.object(oa, "build_sentinel_header")
    @patch.object(oa, "request_sentinel_token", side_effect=TURNSTILE_ERR)
    def test_attempts_clamped_to_min_one_means_no_retry(self, req, build):
        # attempts=0 被钳制为 1：第一次失败即抛错，不重试
        with self.assertRaisesRegex(RuntimeError, "sentinel_turnstile_token_failed"):
            request_sentinel_header_with_retry(FakeSession(), "oauth_create_account", attempts=0)
        req.assert_called_once()


if __name__ == "__main__":
    unittest.main()
