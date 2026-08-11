# -*- coding: utf-8 -*-
"""core/codex_oauth 纯工具函数测试（PKCE/URL/code/JWT/错误分类，无网络）。"""
import base64
import hashlib
import unittest
from unittest.mock import patch

import config.codex as codex_cfg
import core.codex_oauth as co


class CodexOauthUtilsTests(unittest.TestCase):
    def test_generate_pkce(self):
        verifier, challenge = co._generate_pkce()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertLessEqual(len(verifier), 128)
        expect = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        self.assertEqual(challenge, expect)

    def test_generate_state_nonempty(self):
        self.assertTrue(co._generate_state())

    def test_build_authorize_url(self):
        with patch.object(codex_cfg, "CODEX_CLIENT_ID", "cli-1"), \
             patch.object(codex_cfg, "CODEX_REDIRECT_URI", "http://localhost:1455/auth/callback"), \
             patch.object(codex_cfg, "CODEX_SCOPE", "openid email"), \
             patch.object(codex_cfg, "CODEX_AUTH_URL", "https://auth.openai.com/oauth/authorize"):
            url = co._build_authorize_url("st", "ch", prompt="login")
        self.assertIn("client_id=cli-1", url)
        self.assertIn("state=st", url)
        self.assertIn("code_challenge=ch", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("prompt=login", url)

    def test_first_non_empty(self):
        self.assertEqual(co._first_non_empty("", None, "  x  ", "y"), "x")
        self.assertEqual(co._first_non_empty("", None), "")

    def test_extract_state_from_auth_url(self):
        self.assertEqual(
            co._extract_state_from_auth_url("https://a.b/c?state=abc123&x=1"), "abc123")
        self.assertEqual(co._extract_state_from_auth_url("https://a.b/c"), "")

    def test_is_redirect_uri(self):
        self.assertTrue(co._is_redirect_uri("http://localhost:1455/auth/callback?code=x"))
        self.assertTrue(co._is_redirect_uri("https://127.0.0.1:1455/auth/callback"))
        self.assertFalse(co._is_redirect_uri("http://localhost:9999/auth/callback"))
        self.assertFalse(co._is_redirect_uri("https://evil.com/auth/callback"))
        self.assertFalse(co._is_redirect_uri("not a url"))

    def test_extract_code(self):
        self.assertEqual(
            co._extract_code("http://localhost:1455/auth/callback?code=CODE1&state=st", "st"),
            "CODE1")
        with self.assertRaisesRegex(RuntimeError, "error"):
            co._extract_code("http://localhost:1455/auth/callback?error=access_denied", "st")
        with self.assertRaisesRegex(RuntimeError, "缺少 code"):
            co._extract_code("http://localhost:1455/auth/callback", "st")
        with self.assertRaisesRegex(RuntimeError, "state 不匹配"):
            co._extract_code("http://localhost:1455/auth/callback?code=X&state=other", "st")

    def test_decode_jwt_segment(self):
        payload = base64.urlsafe_b64encode(b'{"a": 1}').rstrip(b"=").decode()
        self.assertEqual(co._decode_jwt_segment(payload), {"a": 1})
        self.assertEqual(co._decode_jwt_segment("!!!not-base64!!!"), {})

    def test_cpa_callback_retryable(self):
        self.assertTrue(co._is_cpa_callback_retryable(RuntimeError("timeout waiting for oauth callback")))
        self.assertTrue(co._is_cpa_callback_retryable(RuntimeError("status=503")))
        self.assertFalse(co._is_cpa_callback_retryable(RuntimeError("status=400")))

    def test_cpa_callback_reauth_error(self):
        self.assertTrue(co._is_cpa_callback_reauth_error("timeout waiting for oauth callback"))
        self.assertTrue(co._is_cpa_callback_reauth_error(
            "oauth-callback status=409 timeout waiting for oauth callback"))
        self.assertFalse(co._is_cpa_callback_reauth_error("status=409"))

    def test_phone_failure_reason(self):
        self.assertEqual(co._phone_failure_reason("please use whatsapp"), "whatsapp_channel")
        self.assertEqual(co._phone_failure_reason("invalid phone number"), "invalid_phone")
        self.assertEqual(co._phone_failure_reason("无法向该号码发送验证码"), "delivery_refused")
        self.assertEqual(co._phone_failure_reason("too many requests"), "send_limited")
        self.assertEqual(co._phone_failure_reason("already used"), "phone_used_or_max")
        self.assertEqual(co._phone_failure_reason("boom", status_code=502), "server_error")
        self.assertEqual(co._phone_failure_reason("boom", status_code=422), "send_rejected")
        self.assertEqual(co._phone_failure_reason("ok", status_code=200), "")


if __name__ == "__main__":
    unittest.main()
