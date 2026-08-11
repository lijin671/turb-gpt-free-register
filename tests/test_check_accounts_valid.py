# -*- coding: utf-8 -*-
"""tools/check_accounts_valid 校验子流程测试（mock BrowserSession，无网络）。"""
import unittest
from unittest.mock import patch

import tools.check_accounts_valid as cav


class _FakeResp:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


class _FakeSession:
    def __init__(self, resp_sequence):
        self.resp_sequence = list(resp_sequence)
        self.get_calls = []

    def get(self, url, headers=None, timeout=25):
        self.get_calls.append((url, headers, timeout))
        return self.resp_sequence.pop(0)


class CheckAccountsValidTests(unittest.TestCase):
    def test_check_token_ok(self):
        sess = _FakeSession([
            _FakeResp(200, {"email": "a@b.com", "plan_type": "plus", "id": "user-123"}),
        ])
        with patch.object(cav, "BrowserSession", return_value=sess), \
             patch.object(cav.time, "sleep"):
            status, email, plan, note = cav.check_token("tok", "http://p")
        self.assertEqual(status, "ok")
        self.assertEqual(email, "a@b.com")
        self.assertEqual(plan, "plus")
        self.assertEqual(sess.get_calls[0][0], "https://chatgpt.com/backend-api/me")

    def test_check_token_revoked_401(self):
        sess = _FakeSession([
            _FakeResp(401, {"error": {"code": "token_invalidated"}}),
        ])
        with patch.object(cav, "BrowserSession", return_value=sess), \
             patch.object(cav.time, "sleep"):
            status, _, _, note = cav.check_token("tok", "http://p")
        self.assertEqual(status, "revoked")
        self.assertIn("token_invalidated", note)

    def test_check_token_403_retry_then_ok(self):
        sess = _FakeSession([
            _FakeResp(403, text="cf block"),
            _FakeResp(200, {"email": "a@b.com"}),
        ])
        with patch.object(cav, "BrowserSession", return_value=sess), \
             patch.object(cav.time, "sleep") as sleeper:
            status, email, _, _ = cav.check_token("tok", "http://p")
        self.assertEqual(status, "ok")
        self.assertEqual(email, "a@b.com")
        self.assertEqual(len(sess.get_calls), 2)
        sleeper.assert_called_once()

    def test_check_token_exception_retry_then_error(self):
        class Boom:
            def get(self, *a, **k):
                raise RuntimeError("proxy dead")

        with patch.object(cav, "BrowserSession", return_value=Boom()), \
             patch.object(cav.time, "sleep"):
            status, _, _, note = cav.check_token("tok", "http://p")
        self.assertEqual(status, "error")
        self.assertIn("RuntimeError", note)


if __name__ == "__main__":
    unittest.main()
