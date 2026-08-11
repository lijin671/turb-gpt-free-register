# -*- coding: utf-8 -*-
"""OTP 重新发送携带 Sentinel 头（SEND_SENTINEL_ON_EMAIL_OTP_SEND）A/B 开关测试。"""
import unittest
from unittest.mock import Mock, patch

import main as main_mod
from core.openai_auth import send_email_otp


class _FakeSession:
    def __init__(self):
        self.headers_seen = None

    def get_auth_navigate_headers(self, referer=""):
        return {"referer": referer}

    def get(self, url, headers=None, allow_redirects=True):
        self.headers_seen = headers
        resp = Mock(status_code=200, text="{}")
        return resp


class SendOtpSentinelTests(unittest.TestCase):
    def test_send_otp_no_sentinel_by_default(self):
        session = _FakeSession()
        send_email_otp(session)
        self.assertNotIn("openai-sentinel-token", session.headers_seen)
        self.assertNotIn("openai-sentinel-so-token", session.headers_seen)

    def test_send_otp_attaches_sentinel_when_provided(self):
        session = _FakeSession()
        send_email_otp(session, sentinel_header="st-1", so_header="so-1")
        self.assertEqual(session.headers_seen["openai-sentinel-token"], "st-1")
        self.assertEqual(session.headers_seen["openai-sentinel-so-token"], "so-1")

    def test_resend_flag_off_no_mint(self):
        session = _FakeSession()
        with patch.object(main_mod, "request_sentinel_header_with_retry") as mint, \
             patch.object(main_mod, "send_email_otp") as send, \
             patch("config.openai_protocol.SEND_SENTINEL_ON_EMAIL_OTP_SEND", False):
            main_mod._resend_otp(session)
            mint.assert_not_called()
            send.assert_called_once_with(session, sentinel_header=None, so_header=None)

    def test_resend_flag_on_mints_and_attaches(self):
        session = _FakeSession()
        with patch.object(main_mod, "request_sentinel_header_with_retry",
                          return_value=("st-1", "so-1")) as mint, \
             patch.object(main_mod, "send_email_otp") as send, \
             patch("config.openai_protocol.SEND_SENTINEL_ON_EMAIL_OTP_SEND", True), \
             patch.object(main_mod, "human_delay", return_value=0.0):
            main_mod._resend_otp(session)
            mint.assert_called_once_with(session, "authorize_continue")
            send.assert_called_once_with(session, sentinel_header="st-1", so_header="so-1")

    def test_resend_flag_on_mint_failure_falls_back(self):
        session = _FakeSession()
        with patch.object(main_mod, "request_sentinel_header_with_retry",
                          side_effect=RuntimeError("sentinel down")) as mint, \
             patch.object(main_mod, "send_email_otp") as send, \
             patch("config.openai_protocol.SEND_SENTINEL_ON_EMAIL_OTP_SEND", True), \
             patch.object(main_mod, "human_delay", return_value=0.0):
            main_mod._resend_otp(session)
            mint.assert_called_once()
            send.assert_called_once_with(session, sentinel_header=None, so_header=None)


if __name__ == "__main__":
    unittest.main()
