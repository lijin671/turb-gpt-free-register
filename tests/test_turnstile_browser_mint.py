# -*- coding: utf-8 -*-
"""core.turnstile_browser_mint 测试（fake playwright，不依赖真实浏览器）。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import turnstile_browser_mint as mint_mod
from core.turnstile_browser_mint import mint_turnstile_token


class FakePage:
    def __init__(self, sitekey="0x4AAAAAAAtest", token="tok-abc123"):
        self._sitekey = sitekey
        self._token = token
        self.goto_called = None
        self.wait_selectors = []
        self.no_widget = False
        self.no_sitekey = False
        self.timeout_on_token = False
        self.goto_error = None

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_called = (url, wait_until, timeout)
        if self.goto_error:
            raise self.goto_error

    def wait_for_selector(self, sel, timeout=None):
        self.wait_selectors.append(sel)
        if self.no_widget:
            raise Exception("timeout: not found")
        return True

    def evaluate(self, expr):
        if "data-sitekey" in expr:
            return "" if self.no_sitekey else self._sitekey
        return "" if self.timeout_on_token else self._token


class FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.launch_kwargs = None

    def new_page(self):
        return self.page


class FakePlaywright:
    def __init__(self, browser):
        self.browser = browser
        self.chromium = SimpleNamespace(launch=self._launch)

    def _launch(self, **kwargs):
        self.browser.launch_kwargs = kwargs
        return self.browser

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _fake_playwright_factory(page):
    browser = FakeBrowser(page)
    return FakePlaywright(browser), browser


class TurnstileBrowserMintTests(unittest.TestCase):
    def test_playwright_missing_returns_none(self):
        def _raise():
            raise ImportError("no playwright")
        with patch.object(mint_mod, "_playwright_sync", _raise):
            self.assertIsNone(mint_turnstile_token(page_url="https://example.test"))

    def test_mint_success_with_discovered_sitekey(self):
        page = FakePage()
        pw, browser = _fake_playwright_factory(page)
        logs = []
        with patch.object(mint_mod, "_playwright_sync", lambda: lambda: pw):
            token = mint_turnstile_token(
                page_url="https://example.test", timeout=10,
                proxy="http://127.0.0.1:7890", on_log=logs.append,
            )
        self.assertEqual(token, "tok-abc123")
        self.assertEqual(page.goto_called[0], "https://example.test")
        self.assertEqual(browser.launch_kwargs["proxy"], {"server": "http://127.0.0.1:7890"})
        self.assertIn("--disable-blink-features=AutomationControlled", browser.launch_kwargs["args"])
        self.assertTrue(any("获取到 Turnstile token" in line for line in logs))

    def test_mint_success_with_explicit_sitekey(self):
        page = FakePage(sitekey="")
        pw, _ = _fake_playwright_factory(page)
        with patch.object(mint_mod, "_playwright_sync", lambda: lambda: pw):
            token = mint_turnstile_token(site_key="0x4AAAAAAAexplicit", page_url="https://example.test")
        self.assertEqual(token, "tok-abc123")

    def test_mint_returns_none_when_no_widget(self):
        page = FakePage()
        page.no_widget = True
        pw, _ = _fake_playwright_factory(page)
        with patch.object(mint_mod, "_playwright_sync", lambda: lambda: pw):
            self.assertIsNone(mint_turnstile_token(page_url="https://example.test"))

    def test_mint_returns_none_when_no_sitekey(self):
        page = FakePage()
        page.no_sitekey = True
        pw, _ = _fake_playwright_factory(page)
        with patch.object(mint_mod, "_playwright_sync", lambda: lambda: pw):
            self.assertIsNone(mint_turnstile_token(page_url="https://example.test"))

    def test_mint_timeout_returns_none(self):
        page = FakePage()
        page.timeout_on_token = True
        pw, _ = _fake_playwright_factory(page)
        with patch.object(mint_mod, "time", FakeTime()):
            with patch.object(mint_mod, "_playwright_sync", lambda: lambda: pw):
                self.assertIsNone(mint_turnstile_token(page_url="https://example.test", timeout=1))

    def test_mint_goto_failure_returns_none(self):
        page = FakePage()
        page.goto_error = RuntimeError("net err")
        pw, _ = _fake_playwright_factory(page)
        with patch.object(mint_mod, "_playwright_sync", lambda: lambda: pw):
            self.assertIsNone(mint_turnstile_token(page_url="https://example.test"))

    def test_mint_missing_page_url_returns_none(self):
        self.assertIsNone(mint_turnstile_token())


if __name__ == "__main__":
    unittest.main()
