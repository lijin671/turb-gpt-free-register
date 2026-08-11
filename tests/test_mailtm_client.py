# -*- coding: utf-8 -*-
"""core.mailtm_client 公共 Mail.tm 邮箱源单测（全 mock，无网络）。

覆盖：域名拉取缓存、邮箱创建/登录取 token、冲突重试、取码轮询、
超时报错、上下文释放，以及 email_provider 调度的接入。
"""
import unittest
from unittest.mock import Mock, patch

from core import mailtm_client, email_provider


class MailTmClientTests(unittest.TestCase):
    EMAIL = "fresh@mail.tm"

    def setUp(self):
        mailtm_client._CONTEXT_CACHE.clear()
        mailtm_client._DOMAIN_CACHE = None

    def _account(self, token="tok-1"):
        return mailtm_client.MailTmAccount(
            email=self.EMAIL, password="pw-1", token=token, domain="mail.tm"
        )

    @patch("core.mailtm_client.requests.request")
    def test_pick_account_creates_mailbox(self, request):
        domains_resp = Mock(status_code=200)
        domains_resp.json.return_value = {
            "hydra:member": [{"domain": "mail.tm", "isActive": True}]
        }
        create_resp = Mock(status_code=201)
        create_resp.json.return_value = {"address": self.EMAIL}
        token_resp = Mock(status_code=200)
        token_resp.json.return_value = {"token": "tok-abc"}
        request.side_effect = [domains_resp, create_resp, token_resp]

        account = mailtm_client.pick_account()

        self.assertTrue(account.email.endswith("@mail.tm"), account.email)
        self.assertEqual(account.token, "tok-abc")
        self.assertIs(mailtm_client.get_account_context(account.email), account)
        self.assertEqual(request.call_count, 3)
        urls = [c.args[1] for c in request.call_args_list]
        self.assertTrue(all(u.endswith(p) for u, p in zip(urls, ["/domains", "/accounts", "/token"])), urls)
        create_kwargs = request.call_args_list[1].kwargs
        self.assertEqual(create_kwargs["json"]["address"], account.email)
        token_kwargs = request.call_args_list[2].kwargs
        self.assertEqual(token_kwargs["json"]["password"], account.password)

    @patch("core.mailtm_client.requests.request")
    def test_pick_account_retries_on_address_conflict(self, request):
        domains_resp = Mock(status_code=200)
        domains_resp.json.return_value = {
            "hydra:member": [{"domain": "mail.tm", "isActive": True}]
        }
        conflict = Mock(status_code=422)
        conflict.text = "address already exists"
        create_ok = Mock(status_code=201)
        create_ok.json.return_value = {"address": "other@mail.tm"}
        token_resp = Mock(status_code=200)
        token_resp.json.return_value = {"token": "tok-2"}
        request.side_effect = [domains_resp, conflict, create_ok, token_resp]

        account = mailtm_client.pick_account()

        self.assertNotEqual(account.email, self.EMAIL)
        self.assertEqual(account.token, "tok-2")
        self.assertEqual(request.call_count, 4)

    @patch("core.mailtm_client.requests.request")
    def test_pick_account_raises_when_no_domains(self, request):
        domains_resp = Mock(status_code=200)
        domains_resp.json.return_value = {"hydra:member": []}
        request.return_value = domains_resp

        with self.assertRaisesRegex(mailtm_client.MailTmError, "可用域名"):
            mailtm_client.pick_account()

    @patch("core.mailtm_client.time.sleep")
    @patch("core.mailtm_client.requests.request")
    def test_fetch_latest_otp_reads_openai_code(self, request, sleep):
        messages_resp = Mock(status_code=200)
        messages_resp.json.return_value = {
            "hydra:member": [
                {
                    "id": "m1",
                    "from": {"address": "noreply@openai.com"},
                    "subject": "OpenAI code",
                    "createdAt": "2026-08-05T10:00:00Z",
                    "intro": "",
                }
            ]
        }
        detail_resp = Mock(status_code=200)
        detail_resp.json.return_value = {
            "id": "m1",
            "from": {"address": "noreply@openai.com"},
            "subject": "OpenAI code",
            "text": "Your verification code is 654321",
            "createdAt": "2026-08-05T10:00:00Z",
        }
        request.side_effect = [messages_resp, detail_resp]
        mailtm_client._CONTEXT_CACHE[mailtm_client._cache_key(self.EMAIL)] = self._account()

        code = mailtm_client.fetch_latest_otp(
            self.EMAIL, after_ts=0, max_wait=5, poll_interval=1, settle_seconds=0
        )

        self.assertEqual(code, "654321")
        detail_kwargs = request.call_args_list[1].kwargs
        self.assertEqual(detail_kwargs["headers"]["Authorization"], "Bearer tok-1")

    @patch("core.mailtm_client.time.sleep")
    @patch("core.mailtm_client.requests.request")
    def test_fetch_latest_otp_timeout_raises(self, request, sleep):
        empty_resp = Mock(status_code=200)
        empty_resp.json.return_value = {"hydra:member": []}
        request.return_value = empty_resp
        mailtm_client._CONTEXT_CACHE[mailtm_client._cache_key(self.EMAIL)] = self._account()

        with self.assertRaisesRegex(mailtm_client.MailTmError, "超时"):
            mailtm_client.fetch_latest_otp(
                self.EMAIL, after_ts=0, max_wait=0, poll_interval=1, settle_seconds=0
            )

    @patch("core.mailtm_client.requests.request")
    def test_fetch_latest_otp_requires_context(self, request):
        with self.assertRaisesRegex(mailtm_client.MailTmError, "无该邮箱上下文"):
            mailtm_client.fetch_latest_otp(self.EMAIL, max_wait=0)

    def test_release_account_clears_context(self):
        mailtm_client._CONTEXT_CACHE[mailtm_client._cache_key(self.EMAIL)] = self._account()
        mailtm_client.release_account(self.EMAIL, status="available", note="done")
        self.assertIsNone(mailtm_client.get_account_context(self.EMAIL))

    def test_get_account_context_unknown_returns_none(self):
        self.assertIsNone(mailtm_client.get_account_context("nobody@example.com"))

    def test_email_provider_registers_mailtm_source(self):
        self.assertIn("mailtm", email_provider._VALID_SOURCES)

    def test_email_provider_resolve_mailtm(self):
        mailtm_client._CONTEXT_CACHE[mailtm_client._cache_key(self.EMAIL)] = self._account()
        self.assertEqual(email_provider.resolve_email_source(self.EMAIL), "mailtm")

    def test_email_provider_dispatch_functions_cover_mailtm(self):
        import inspect
        for fn_name in ("_pick_from_source", "wait_for_otp", "release_email"):
            fn = getattr(email_provider, fn_name)
            source = inspect.getsource(fn)
            self.assertIn('"mailtm"', source, f"{fn_name} 未接入 mailtm 分支")


if __name__ == "__main__":
    unittest.main()
