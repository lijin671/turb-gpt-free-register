# -*- coding: utf-8 -*-
"""core/chatgpt_bootstrap 预热链路测试（mock 请求，无网络）。"""
import unittest
from unittest.mock import patch

import core.chatgpt_bootstrap as cb


class _FakeResp:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text

    def json(self):
        return self._body


class _FakeSession:
    sentinel_sid = "sid-x"
    device_id = "dev-x"
    browser_profile = {"p": 1}

    def js_timezone_offset_min(self):
        return -480

    def get_chatgpt_headers(self, referer=""):
        return {"referer": referer}

    def get(self, url, headers=None):
        return _FakeResp(200)

    def post(self, url, headers=None, data=None):
        return _FakeResp(200)


class ChatgptBootstrapTests(unittest.TestCase):
    def test_system_hint_paths(self):
        paths = cb._system_hint_paths(("custom_agents", "connectors"), "https://x")
        self.assertEqual(paths, [
            "https://x/system_hints?mode=custom_agents",
            "https://x/system_hints?mode=connectors",
        ])

    def test_safe_request_ok_and_http_error(self):
        ok = cb._safe_request("x", lambda: _FakeResp(200))
        self.assertIsNotNone(ok)
        self.assertIsNone(cb._safe_request("x", lambda: _FakeResp(400, text="bad"), strict=False))
        with self.assertRaises(RuntimeError):
            cb._safe_request("x", lambda: _FakeResp(400, text="bad"), strict=True)

    def test_safe_request_exception(self):
        self.assertIsNone(cb._safe_request("x", lambda: (_ for _ in ()).throw(ConnectionError("no")), strict=False))
        with self.assertRaises(ConnectionError):
            cb._safe_request("x", lambda: (_ for _ in ()).throw(ConnectionError("no")), strict=True)

    def test_chat_requirements_prepare_posts_payload(self):
        sess = _FakeSession()
        with patch.object(cb, "generate_requirements_token", return_value="TOK-P"), \
             patch.object(cb, "_json_post", return_value=_FakeResp(200)) as post:
            resp = cb._chat_requirements_prepare(sess, "https://chatgpt.com/backend-anon",
                                                 "https://chatgpt.com/", strict=False)
        self.assertEqual(resp.status_code, 200)
        url = post.call_args[0][1]
        payload = post.call_args[0][2]
        self.assertIn("chat-requirements/prepare", url)
        self.assertEqual(payload["p"], "TOK-P")

    def test_finalize_skips_when_no_prepare_token(self):
        sess = _FakeSession()
        self.assertIsNone(cb._maybe_chat_requirements_finalize(sess, "b", "r", None))
        with patch.object(cb, "_json_post") as post:
            self.assertIsNone(cb._maybe_chat_requirements_finalize(
                sess, "b", "r", _FakeResp(200, {"c": ""})))
        post.assert_not_called()

    def test_finalize_posts_payload_with_challenges(self):
        sess = _FakeSession()
        prepare = _FakeResp(200, {"prepare_token": "PT", "proofofwork": {"x": 1}, "turnstile": "T"})
        with patch.object(cb, "_json_post", return_value=_FakeResp(200)) as post:
            cb._maybe_chat_requirements_finalize(sess, "https://b", "https://r", prepare)
        self.assertEqual(post.call_count, 1)
        payload = post.call_args[0][2]
        self.assertEqual(payload["prepare_token"], "PT")
        self.assertEqual(payload["turnstile"], "T")
        self.assertIn("proofofwork", payload)

    def test_anonymous_bootstrap_orchestration(self):
        sess = _FakeSession()
        prep = _FakeResp(200, {"prepare_token": "PT"})
        with patch.object(cb, "_chat_requirements_prepare", return_value=prep) as prepare, \
             patch.object(cb, "_maybe_chat_requirements_finalize") as finalize, \
             patch.object(cb, "_safe_request") as safe:
            cb.anonymous_bootstrap(sess)
        prepare.assert_called_once()
        finalize.assert_called_once()
        labels = [c.args[0] for c in safe.call_args_list]
        self.assertIn("anon accounts/check", labels)
        self.assertIn("anon me", labels)
        self.assertIn("anon conversation/init", labels)
        self.assertTrue(any("system_hints" in str(c.args[0]) for c in safe.call_args_list))


if __name__ == "__main__":
    unittest.main()
