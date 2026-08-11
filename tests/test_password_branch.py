# -*- coding: utf-8 -*-
"""协议注册密码分支（create-account/password fallback）测试。"""
import json
import unittest
from unittest.mock import patch

from core.openai_auth import (
    AccountUnusableError,
    follow_authorize,
    is_password_branch_url,
    register_user,
)
from core.profile_utils import generate_random_password, registration_password


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if isinstance(body, dict) else "")

    def json(self):
        if isinstance(self._body, dict):
            return self._body
        raise ValueError("no json")


class FakeSession:
    def __init__(self):
        self.calls = []

    def get_auth_headers(self, referer=""):
        return {"referer": referer, "content-type": "application/json"}

    def post(self, url, headers=None, data=None):
        self.calls.append((url, headers, data))
        return FakeResponse(status_code=200, body={"ok": True})


class PasswordBranchTests(unittest.TestCase):
    def test_is_password_branch_url(self):
        self.assertTrue(is_password_branch_url("https://auth.openai.com/create-account/password"))
        self.assertTrue(is_password_branch_url("https://auth.openai.com/api/accounts/user/register"))
        self.assertFalse(is_password_branch_url("https://auth.openai.com/email-verification"))
        self.assertFalse(is_password_branch_url(""))

    def test_register_user_posts_expected_body(self):
        sess = FakeSession()
        result = register_user(sess, "user@x.com", "Passw0rd!abc", "sentinel-tok", "so-tok")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(sess.calls), 1)
        url, headers, data = sess.calls[0]
        self.assertEqual(url, "https://auth.openai.com/api/accounts/user/register")
        self.assertEqual(headers["openai-sentinel-token"], "sentinel-tok")
        self.assertEqual(headers["openai-sentinel-so-token"], "so-tok")
        self.assertEqual(json.loads(data), {"username": "user@x.com", "password": "Passw0rd!abc"})

    def test_register_user_dead_account_raises(self):
        class DeadResp(FakeResponse):
            def __init__(self):
                super().__init__(status_code=400, body={"error": {"code": "account_deactivated"}})

        class DeadSession(FakeSession):
            def post(self, url, headers=None, data=None):
                return DeadResp()

        with self.assertRaises(AccountUnusableError):
            register_user(DeadSession(), "user@x.com", "pw", "tok")

    def test_register_user_http_error_raises(self):
        class ErrResp(FakeResponse):
            def __init__(self):
                super().__init__(status_code=422, text="bad request")
            def raise_for_status(self):
                raise RuntimeError("HTTP 422")

        class ErrSession(FakeSession):
            def post(self, url, headers=None, data=None):
                return ErrResp()

        with self.assertRaises(RuntimeError):
            register_user(ErrSession(), "user@x.com", "pw", "tok")

    def test_follow_authorize_returns_password_url_without_raising(self):
        class NavResp(FakeResponse):
            def __init__(self):
                super().__init__(status_code=200)
                self.url = "https://auth.openai.com/create-account/password"
            def raise_for_status(self):
                pass

        class NavSession(FakeSession):
            def __init__(self):
                super().__init__()
                self.resp = NavResp()
            def get_auth_navigate_headers(self, referer=""):
                return {}
            def get(self, url, headers=None, allow_redirects=True):
                self.calls.append(url)
                return self.resp

        sess = NavSession()
        final_url = follow_authorize(sess, "https://auth.openai.com/api/accounts/authorize?x=1")
        self.assertEqual(final_url, "https://auth.openai.com/create-account/password")

    def test_generate_random_password_complexity(self):
        for _ in range(5):
            pw = generate_random_password()
            self.assertGreaterEqual(len(pw), 14)
            self.assertTrue(any(c.isupper() for c in pw))
            self.assertTrue(any(c.islower() for c in pw))
            self.assertTrue(any(c.isdigit() for c in pw))
            self.assertTrue(any(c in "!@#$%^&*" for c in pw))

    def test_registration_password_uses_configured_value(self):
        fake_cfg = type("RegisterCfg", (), {"REGISTER_PASSWORD": "ConfiguredPw1!"})
        with patch.dict("sys.modules", {"config.register": fake_cfg}):
            # registration_password 内部 from config import register
            import core.profile_utils as pu
            with patch("config.register", fake_cfg):
                self.assertEqual(pu.registration_password(), "ConfiguredPw1!")


if __name__ == "__main__":
    unittest.main()
