# -*- coding: utf-8 -*-
"""core.token_revival 复活链路单测（全 mock，无网络）。

覆盖：成功（reauth→OTP→validate→exchange→写回）、账号缺失/无 token、
OTP 超时、异常失败、批量复活、外部 session 复用不关闭、自带 session 关闭。
"""
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from core import token_revival as tr


class _FakeSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class ReviveAccountTests(unittest.TestCase):
    def _acc(self, token="tok-1", proxy="socks5://1.2.3.4:1080", device_id="dev-1"):
        return {
            "email": "a@x.com",
            "access_token": token,
            "proxy_used": proxy,
            "device_id": device_id,
        }

    def _patch_chain(self, acc=None, acc_missing=False, new_token="fresh-token"):
        acc_value = None if acc_missing else (acc if acc is not None else self._acc())
        return [
            patch("core.db.get_account_by_email", return_value=acc_value),
            patch("core.session.BrowserSession", return_value=_FakeSession()),
            patch("core.account_export._trigger_reauth", return_value="https://auth.openai.com/reauth"),
            patch("core.account_export._follow_reauth", return_value=None),
            patch("core.email_provider.wait_for_otp", return_value="123456"),
            patch("core.account_export._validate_reauth_otp", return_value="https://chatgpt.com/cb?code=x"),
            patch("core.account_export._exchange_new_token", return_value=new_token),
            patch("core.db.update_account_access_token", return_value=True),
            patch("core.humanize.delay", return_value=None),
        ]

    def _revive(self, email="a@x.com", *, acc=None, acc_missing=False, new_token="fresh-token",
                session=None, otp_code=None):
        stack = ExitStack()
        self._patches = [stack.enter_context(p) for p in self._patch_chain(
            acc=acc, acc_missing=acc_missing, new_token=new_token)]
        try:
            return tr.revive_account(email, otp_code=otp_code, session=session)
        finally:
            stack.close()

    def test_success_writes_new_token(self):
        r = self._revive()
        self.assertTrue(r["ok"])
        self.assertEqual(r["access_token"], "fresh-token")
        upd = self._patches[7]
        upd.assert_called_once()
        self.assertEqual(upd.call_args.args[0], "a@x.com")
        self.assertEqual(upd.call_args.args[1], "fresh-token")

    def test_success_uses_account_proxy_and_device(self):
        r = self._revive()
        self.assertTrue(r["ok"])
        bs = self._patches[1]
        bs.assert_called_once()
        self.assertEqual(bs.call_args.kwargs["proxy"], "socks5://1.2.3.4:1080")
        self.assertEqual(bs.call_args.kwargs["device_id"], "dev-1")

    def test_missing_account(self):
        r = self._revive(acc_missing=True)
        self.assertFalse(r["ok"])
        self.assertIn("不存在", r["message"])

    def test_no_access_token(self):
        r = self._revive(acc={"email": "a@x.com", "access_token": ""})
        self.assertFalse(r["ok"])
        self.assertIn("无 access_token", r["message"])

    def test_otp_timeout_fails(self):
        stack = ExitStack()
        patches = [stack.enter_context(p) for p in self._patch_chain()]
        try:
            patches[4].return_value = None  # wait_for_otp → None
            r = tr.revive_account("a@x.com")
        finally:
            stack.close()
        self.assertFalse(r["ok"])
        self.assertIn("超时", r["message"])
        patches[7].assert_not_called()

    def test_exception_in_reauth_fails(self):
        stack = ExitStack()
        patches = [stack.enter_context(p) for p in self._patch_chain()]
        try:
            patches[2].side_effect = RuntimeError("session dead")
            r = tr.revive_account("a@x.com")
        finally:
            stack.close()
        self.assertFalse(r["ok"])
        self.assertIn("RuntimeError", r["message"])

    def test_external_session_not_closed(self):
        fake = _FakeSession()
        r = self._revive(session=fake)
        self.assertTrue(r["ok"])
        self.assertFalse(fake.closed)

    def test_own_session_closed(self):
        fake = _FakeSession()
        stack = ExitStack()
        patches = [stack.enter_context(p) for p in self._patch_chain()]
        try:
            patches[1].return_value = fake
            r = tr.revive_account("a@x.com")
        finally:
            stack.close()
        self.assertTrue(r["ok"])
        self.assertTrue(fake.closed)

    def test_batch_revive_with_codes(self):
        with patch.object(tr, "revive_account", return_value={"ok": True, "email": "x"}) as ra:
            results = tr.revive_accounts(["a@x.com", "  b@x.com  ", ""],
                                         otp_codes={"a@x.com": "111111"})
        self.assertEqual(len(results), 2)
        self.assertEqual(ra.call_args_list[0].kwargs["otp_code"], "111111")
        self.assertEqual(ra.call_args_list[1].kwargs["otp_code"], None)


if __name__ == "__main__":
    unittest.main()


class ReexportTests(unittest.TestCase):
    def _base_patches(self):
        return [
            patch("core.db.get_account_by_email",
                  return_value={"email": "a@x.com", "access_token": "tok-1",
                                "proxy_used": "socks5://1.2.3.4:1080", "device_id": "dev-1"}),
            patch("core.session.BrowserSession", return_value=_FakeSession()),
            patch("core.account_export._trigger_reauth", return_value="https://auth.openai.com/reauth"),
            patch("core.account_export._follow_reauth", return_value=None),
            patch("core.email_provider.wait_for_otp", return_value="123456"),
            patch("core.account_export._validate_reauth_otp", return_value="https://chatgpt.com/cb?code=x"),
            patch("core.account_export._exchange_new_token", return_value="fresh-token"),
            patch("core.db.update_account_access_token", return_value=True),
            patch("core.humanize.delay", return_value=None),
        ]

    def test_reexport_after_revive(self):
        stack = ExitStack()
        patches = [stack.enter_context(p) for p in self._base_patches()]
        patches.append(stack.enter_context(
            patch("core.chatgpt2api_export.export_account_to_chatgpt2api",
                  return_value={"ok": True, "message": "ok", "added": 0, "total": 5})))
        patches.append(stack.enter_context(
            patch("config.export.AUTO_REEXPORT_AFTER_REVIVE", True)))
        try:
            r = tr.revive_account("a@x.com")
        finally:
            stack.close()
        self.assertTrue(r["ok"])
        self.assertTrue(r["reexport"]["ok"])
        export_mock = patches[-2]
        export_mock.assert_called_once()
        self.assertEqual(export_mock.call_args.args[0], "a@x.com")
        self.assertEqual(export_mock.call_args.args[1], "fresh-token")

    def test_reexport_failure_does_not_fail_revive(self):
        stack = ExitStack()
        patches = [stack.enter_context(p) for p in self._base_patches()]
        patches.append(stack.enter_context(
            patch("core.chatgpt2api_export.export_account_to_chatgpt2api",
                  side_effect=RuntimeError("tokens file missing"))))
        patches.append(stack.enter_context(
            patch("config.export.AUTO_REEXPORT_AFTER_REVIVE", True)))
        try:
            r = tr.revive_account("a@x.com")
        finally:
            stack.close()
        self.assertTrue(r["ok"])
        self.assertFalse(r["reexport"]["ok"])

    def test_reexport_disabled_by_config(self):
        stack = ExitStack()
        patches = [stack.enter_context(p) for p in self._base_patches()]
        patches.append(stack.enter_context(
            patch("core.chatgpt2api_export.export_account_to_chatgpt2api")))
        patches.append(stack.enter_context(
            patch("config.export.AUTO_REEXPORT_AFTER_REVIVE", False)))
        try:
            r = tr.revive_account("a@x.com")
        finally:
            stack.close()
        self.assertTrue(r["ok"])
        self.assertIsNone(r["reexport"])
        patches[-2].assert_not_called()


class ReimportCpaTests(unittest.TestCase):
    def _base_patches(self):
        return [
            patch("core.db.get_account_by_email",
                  return_value={"email": "a@x.com", "access_token": "tok-1",
                                "proxy_used": "socks5://1.2.3.4:1080", "device_id": "dev-1"}),
            patch("core.session.BrowserSession", return_value=_FakeSession()),
            patch("core.account_export._trigger_reauth", return_value="https://auth.openai.com/reauth"),
            patch("core.account_export._follow_reauth", return_value=None),
            patch("core.email_provider.wait_for_otp", return_value="123456"),
            patch("core.account_export._validate_reauth_otp", return_value="https://chatgpt.com/cb?code=x"),
            patch("core.account_export._exchange_new_token", return_value="fresh-token"),
            patch("core.db.update_account_access_token", return_value=True),
            patch("core.humanize.delay", return_value=None),
        ]

    def test_reimport_cpa_after_revive(self):
        stack = ExitStack()
        patches = [stack.enter_context(p) for p in self._base_patches()]
        patches.append(stack.enter_context(
            patch("core.chatgpt2api_export.export_account_to_chatgpt2api",
                  return_value={"ok": True, "message": "ok"})))
        patches.append(stack.enter_context(
            patch("core.cpa_manager_import.import_single_account",
                  return_value={"ok": True, "name": "chatgpt-1.json", "message": "已导入"})))
        patches.append(stack.enter_context(
            patch("config.export.AUTO_REEXPORT_AFTER_REVIVE", True)))
        patches.append(stack.enter_context(
            patch("config.export.AUTO_REIMPORT_AFTER_REVIVE", True)))
        patches.append(stack.enter_context(
            patch("config.export.CPA_MANAGER_PLUS_BASE", "http://127.0.0.1:18317")))
        patches.append(stack.enter_context(
            patch("config.export.CPA_MANAGER_PLUS_KEY", "cpamp_test")))
        try:
            r = tr.revive_account("a@x.com")
        finally:
            stack.close()
        self.assertTrue(r["ok"])
        self.assertTrue(r["reimport"]["ok"])
        imp = patches[-6]
        imp.assert_called_once()
        self.assertEqual(imp.call_args.args[0], "a@x.com")
        self.assertEqual(imp.call_args.args[1], "fresh-token")

    def test_reimport_failure_does_not_fail_revive(self):
        stack = ExitStack()
        patches = [stack.enter_context(p) for p in self._base_patches()]
        patches.append(stack.enter_context(
            patch("core.chatgpt2api_export.export_account_to_chatgpt2api",
                  return_value={"ok": True, "message": "ok"})))
        patches.append(stack.enter_context(
            patch("core.cpa_manager_import.import_single_account",
                  side_effect=RuntimeError("cpa down"))))
        patches.append(stack.enter_context(
            patch("config.export.AUTO_REEXPORT_AFTER_REVIVE", True)))
        patches.append(stack.enter_context(
            patch("config.export.AUTO_REIMPORT_AFTER_REVIVE", True)))
        patches.append(stack.enter_context(
            patch("config.export.CPA_MANAGER_PLUS_BASE", "http://127.0.0.1:18317")))
        patches.append(stack.enter_context(
            patch("config.export.CPA_MANAGER_PLUS_KEY", "cpamp_test")))
        try:
            r = tr.revive_account("a@x.com")
        finally:
            stack.close()
        self.assertTrue(r["ok"])
        self.assertFalse(r["reimport"]["ok"])

    def test_reimport_disabled_by_config(self):
        stack = ExitStack()
        patches = [stack.enter_context(p) for p in self._base_patches()]
        patches.append(stack.enter_context(
            patch("core.chatgpt2api_export.export_account_to_chatgpt2api",
                  return_value={"ok": True, "message": "ok"})))
        imp_mock = stack.enter_context(
            patch("core.cpa_manager_import.import_single_account"))
        patches.append(imp_mock)
        patches.append(stack.enter_context(
            patch("config.export.AUTO_REEXPORT_AFTER_REVIVE", True)))
        patches.append(stack.enter_context(
            patch("config.export.AUTO_REIMPORT_AFTER_REVIVE", False)))
        try:
            r = tr.revive_account("a@x.com")
        finally:
            stack.close()
        self.assertTrue(r["ok"])
        self.assertIsNone(r["reimport"])
        imp_mock.assert_not_called()
