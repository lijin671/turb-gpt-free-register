# -*- coding: utf-8 -*-
"""authorize 短路径（参考 sleep-reg _chatgpt_web_authorize）测试。"""
import base64
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from config import openai_protocol as protocol_cfg
from core.chatgpt_auth import build_direct_authorize_url
from core.session import BrowserSession


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _session() -> BrowserSession:
    return BrowserSession(proxy="", detect_exit_geo=False)


class BuildDirectAuthorizeUrlTests(unittest.TestCase):
    def test_returns_authorize_url_with_full_params(self):
        session = _session()
        url = build_direct_authorize_url(session, "test@example.com")
        parsed = urlparse(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "auth.openai.com")
        self.assertEqual(parsed.path, "/api/accounts/authorize")
        q = parse_qs(parsed.query)

        self.assertEqual(q["client_id"][0], protocol_cfg.OPENAI_CLIENT_ID)
        self.assertIn("offline_access", q["scope"][0])
        self.assertEqual(q["redirect_uri"][0], protocol_cfg.OPENAI_REDIRECT_URI)
        self.assertEqual(q["audience"][0], protocol_cfg.OPENAI_AUDIENCE)
        self.assertEqual(q["device_id"][0], session.device_id)
        self.assertEqual(q["ext-oai-did"][0], session.device_id)
        self.assertEqual(q["auth_session_logging_id"][0], session.auth_session_logging_id)
        self.assertEqual(q["login_hint"][0], "test@example.com")
        self.assertEqual(q["screen_hint"][0], "login_or_signup")
        self.assertEqual(q["response_type"][0], "code")
        self.assertEqual(q["response_mode"][0], "query")
        self.assertEqual(q["code_challenge_method"][0], "S256")
        self.assertEqual(q["ccaps"][0], "login_methods")
        self.assertTrue(q["state"][0])
        self.assertTrue(q["nonce"][0])

    def test_pkce_verifier_stored_on_session_and_challenge_matches(self):
        session = _session()
        url = build_direct_authorize_url(session, "test@example.com")
        verifier = session.pkce_code_verifier
        self.assertTrue(verifier)
        challenge = parse_qs(urlparse(url).query)["code_challenge"][0]
        self.assertEqual(challenge, _b64url(hashlib.sha256(verifier.encode("ascii")).digest()))

    def test_config_default_fallback_disabled(self):
        self.assertFalse(protocol_cfg.AUTHORIZE_SHORT_PATH_FALLBACK)


class Stage1FallbackTests(unittest.TestCase):
    def test_nextauth_failure_with_fallback_enabled_returns_short_path(self):
        import main
        session = _session()
        with patch.object(main, "_protocol_cfg", SimpleNamespace(AUTHORIZE_SHORT_PATH_FALLBACK=True)), \
             patch.object(main, "get_providers", side_effect=RuntimeError("csrf blocked")), \
             patch.object(main, "human_delay", lambda *a, **k: None), \
             patch("core.chatgpt_auth.build_direct_authorize_url", return_value="https://auth.openai.com/api/accounts/authorize?x=1") as build:
            url, used = main._stage1_authorize_url(session, "test@example.com")
        self.assertEqual(url, "https://auth.openai.com/api/accounts/authorize?x=1")
        self.assertTrue(used)
        build.assert_called_once_with(session, "test@example.com")

    def test_nextauth_ok_returns_signin_url(self):
        import main
        session = _session()
        with patch.object(main, "_protocol_cfg", SimpleNamespace(AUTHORIZE_SHORT_PATH_FALLBACK=True)), \
             patch.object(main, "get_providers", return_value={}), \
             patch.object(main, "get_csrf_token", return_value="csrf-123"), \
             patch.object(main, "signin_openai", return_value="https://auth.openai.com/authorize?from=signin") as signin, \
             patch.object(main, "human_delay", lambda *a, **k: None):
            url, used = main._stage1_authorize_url(session, "test@example.com")
        self.assertEqual(url, "https://auth.openai.com/authorize?from=signin")
        self.assertFalse(used)
        signin.assert_called_once_with(session, "csrf-123", "test@example.com")

    def test_nextauth_failure_with_fallback_disabled_raises(self):
        import main
        session = _session()
        with patch.object(main, "_protocol_cfg", SimpleNamespace(AUTHORIZE_SHORT_PATH_FALLBACK=False)), \
             patch.object(main, "get_providers", side_effect=RuntimeError("csrf blocked")):
            with self.assertRaises(RuntimeError):
                main._stage1_authorize_url(session, "test@example.com")


if __name__ == "__main__":
    unittest.main()
