# -*- coding: utf-8 -*-
"""WebUI /api/turnstile-mint 端点测试（mock mint，无浏览器）。"""
import unittest
from unittest.mock import patch

from webui.app import create_app


class WebUiTurnstileMintTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("core.turnstile_browser_mint.mint_turnstile_token", return_value="tok-abc")
    def test_mint_success(self, mint):
        r = self.client.post("/api/turnstile-mint", json={
            "page_url": "https://example.test", "site_key": "0x4AAAAAAA", "timeout": 30,
        })
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["token"], "tok-abc")
        mint.assert_called_once_with(
            site_key="0x4AAAAAAA", page_url="https://example.test",
            headless=True, timeout=30, proxy="",
        )

    @patch("core.turnstile_browser_mint.mint_turnstile_token", return_value=None)
    def test_mint_failure_returns_422(self, mint):
        r = self.client.post("/api/turnstile-mint", json={"page_url": "https://example.test"})
        self.assertEqual(r.status_code, 422)
        self.assertFalse(r.get_json()["ok"])

    def test_mint_missing_page_url_returns_400(self):
        r = self.client.post("/api/turnstile-mint", json={})
        self.assertEqual(r.status_code, 400)

    @patch("core.turnstile_browser_mint.mint_turnstile_token", return_value="tok-x")
    def test_mint_headed_and_proxy_passed(self, mint):
        r = self.client.post("/api/turnstile-mint", json={
            "page_url": "https://example.test", "headed": True,
            "proxy": "http://127.0.0.1:7890", "timeout": 9999,
        })
        self.assertEqual(r.status_code, 200)
        mint.assert_called_once_with(
            site_key=None, page_url="https://example.test",
            headless=False, timeout=180, proxy="http://127.0.0.1:7890",
        )

    def test_mint_requires_auth(self):
        client = create_app(auth_code="secret").test_client()
        r = client.post("/api/turnstile-mint", json={"page_url": "https://example.test"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
