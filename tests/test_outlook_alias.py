# -*- coding: utf-8 -*-
"""Outlook +tag 别名（参考 sleep-reg email_providers/outlook.py use_alias）测试。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from config import OUTLOOK_ALIAS, OUTLOOK_ALIAS_REUSE_ON_SUCCESS
from core import email_provider, outlook_client


class OutlookAliasUnitTests(unittest.TestCase):
    def test_config_defaults(self):
        self.assertTrue(OUTLOOK_ALIAS)
        self.assertFalse(OUTLOOK_ALIAS_REUSE_ON_SUCCESS)

    def test_make_outlook_alias_basic(self):
        alias = outlook_client.make_outlook_alias("Base.User@Outlook.COM")
        self.assertRegex(alias, r"^base\.user\+[a-z0-9]{10}@outlook\.com$")

    def test_make_outlook_alias_idempotent(self):
        alias = outlook_client.make_outlook_alias("base+oldtag@outlook.com", tag="newtag")
        self.assertEqual(alias, "base+newtag@outlook.com")

    def test_make_outlook_alias_sanitizes_tag(self):
        alias = outlook_client.make_outlook_alias("base@outlook.com", tag="a b!c#d@e_f-g")
        self.assertEqual(alias, "base+abcde_f-g@outlook.com")

    def test_make_outlook_alias_invalid_input_passthrough(self):
        self.assertEqual(outlook_client.make_outlook_alias(""), "")
        self.assertEqual(outlook_client.make_outlook_alias("not-an-email"), "not-an-email")

    def test_resolve_base_email_strips_tag(self):
        self.assertEqual(
            outlook_client.resolve_base_email("base+abc123@outlook.com"),
            "base@outlook.com",
        )

    def test_resolve_base_email_passthrough(self):
        self.assertEqual(outlook_client.resolve_base_email("base@outlook.com"), "base@outlook.com")
        self.assertEqual(outlook_client.resolve_base_email(""), "")


class OutlookAliasAcquireTests(unittest.TestCase):
    def tearDown(self):
        outlook_client._CONTEXT_CACHE.clear()

    @patch("core.outlook_client.pick_account")
    def test_acquire_email_returns_alias_and_registers_context(self, pick_account):
        account = outlook_client.OutlookAccount(
            email="base@outlook.com", password="pw", client_id="cid", refresh_token="rt"
        )
        pick_account.return_value = account
        with patch.object(outlook_client, "_email_cfg", SimpleNamespace(OUTLOOK_ALIAS=True)):
            email = outlook_client.acquire_email()
        self.assertRegex(email, r"^base\+[a-z0-9]{10}@outlook\.com$")
        self.assertIs(outlook_client._CONTEXT_CACHE[email], account)

    @patch("core.outlook_client.pick_account")
    def test_acquire_email_returns_base_when_alias_disabled(self, pick_account):
        account = outlook_client.OutlookAccount(
            email="base@outlook.com", password="pw", client_id="cid", refresh_token="rt"
        )
        pick_account.return_value = account
        with patch.object(outlook_client, "_email_cfg", SimpleNamespace(OUTLOOK_ALIAS=False)):
            self.assertEqual(outlook_client.acquire_email(), "base@outlook.com")

    def test_get_account_context_resolves_alias_via_cache(self):
        account = outlook_client.OutlookAccount(
            email="base@outlook.com", password="pw", client_id="cid", refresh_token="rt"
        )
        outlook_client._CONTEXT_CACHE["base@outlook.com"] = account
        self.assertIs(
            outlook_client.get_account_context("base+tag1@outlook.com"),
            account,
        )

    @patch("core.db.get_outlook_by_email")
    def test_get_account_context_resolves_alias_via_db(self, get_outlook_by_email):
        get_outlook_by_email.return_value = {
            "email": "base@outlook.com", "password": "pw",
            "client_id": "cid", "refresh_token": "rt",
        }
        context = outlook_client.get_account_context("base+tag2@outlook.com")
        self.assertIsNotNone(context)
        get_outlook_by_email.assert_called_once_with("base@outlook.com")

    @patch("core.db.release_outlook")
    def test_release_account_resolves_base(self, release_outlook):
        outlook_client.release_account("base+tag3@outlook.com", status="failed", note="测试")
        release_outlook.assert_called_once_with(
            "base@outlook.com", status="failed", note="测试"
        )


class OutlookAliasProviderTests(unittest.TestCase):
    @patch("core.db.get_outlook_by_email", return_value={"email": "base@outlook.com"})
    def test_resolve_email_source_recognizes_alias(self, get_outlook_by_email):
        self.assertEqual(
            email_provider.resolve_email_source("base+tag4@outlook.com"),
            "outlook",
        )
        self.assertIn(
            ("base@outlook.com",),
            [c.args for c in get_outlook_by_email.call_args_list],
        )

    @patch("core.db.release_unconsumed_outlook", return_value=True)
    def test_release_email_if_unconsumed_resolves_base(self, release_unconsumed):
        self.assertTrue(
            email_provider.release_email_if_unconsumed("base+tag5@outlook.com", note="回收")
        )
        release_unconsumed.assert_called_once_with("base@outlook.com", note="回收")


class OutlookAliasReuseTests(unittest.TestCase):
    @patch("core.email_provider.release_email")
    @patch("core.email_provider.resolve_email_source", return_value="outlook")
    def test_maybe_release_for_alias_reuse_enabled(self, resolve, release):
        with patch.object(outlook_client, "_email_cfg", SimpleNamespace(
            OUTLOOK_ALIAS=True, OUTLOOK_ALIAS_REUSE_ON_SUCCESS=True,
        )):
            outlook_client.maybe_release_outlook_for_alias_reuse("base+tag6@outlook.com")
        release.assert_called_once_with(
            "base+tag6@outlook.com", status="available", note="注册成功，别名复用"
        )

    @patch("core.email_provider.release_email")
    def test_maybe_release_for_alias_reuse_disabled(self, release):
        with patch.object(outlook_client, "_email_cfg", SimpleNamespace(
            OUTLOOK_ALIAS=True, OUTLOOK_ALIAS_REUSE_ON_SUCCESS=False,
        )):
            outlook_client.maybe_release_outlook_for_alias_reuse("base+tag7@outlook.com")
        release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
