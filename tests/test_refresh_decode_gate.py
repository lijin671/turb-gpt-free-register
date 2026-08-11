# -*- coding: utf-8 -*-
"""解码保号开关（REFRESH_DECODE_ENABLED）测试：默认关，用户指示才开。"""
import unittest
from unittest.mock import patch

from main import _maybe_refresh_decode


class RefreshDecodeGateTest(unittest.TestCase):
    def _run(self, enabled: bool, result: dict, decode_out=None, decode_error=None):
        with patch("config.codex.REFRESH_DECODE_ENABLED", enabled), \
             patch("core.phone_verify_refresh.run_phone_verify_refresh",
                   side_effect=decode_error if decode_error else (lambda **kw: decode_out)) as m:
            out = _maybe_refresh_decode("u@example.com", dict(result))
            return out, m

    def test_disabled_skips_without_calling(self):
        out, m = self._run(False, {"success": True, "access_token": "at-1"})
        self.assertEqual(out["refresh_decode"], "skipped_disabled")
        m.assert_not_called()

    def test_enabled_with_token_calls_and_merges(self):
        decode_out = {
            "ok": True,
            "email": "u@example.com",
            "refresh_token": "rt-xyz",
            "file_path": "/root/codex_accounts/codex-u@example.com.json",
        }
        out, m = self._run(True, {"success": True, "access_token": "at-1"}, decode_out=decode_out)
        m.assert_called_once()
        self.assertEqual(m.call_args.kwargs["access_token"], "at-1")
        self.assertEqual(m.call_args.kwargs["email"], "u@example.com")
        self.assertEqual(out["refresh_decode"], "ok")
        self.assertEqual(out["refresh_token"], "rt-xyz")
        self.assertEqual(out["cpa_credential_file"], "/root/codex_accounts/codex-u@example.com.json")

    def test_enabled_without_token_skips(self):
        out, m = self._run(True, {"success": True})
        self.assertEqual(out["refresh_decode"], "skipped_no_token")
        m.assert_not_called()

    def test_enabled_failure_non_blocking(self):
        decode_out = {"ok": False, "email": "u@example.com", "error": "账号已废"}
        out, m = self._run(True, {"success": True, "access_token": "at-1"}, decode_out=decode_out)
        self.assertTrue(out["success"])
        self.assertEqual(out["refresh_decode"], "error: 账号已废")

    def test_enabled_exception_non_blocking(self):
        out, m = self._run(True, {"success": True, "access_token": "at-1"},
                           decode_error=RuntimeError("NO_BALANCE"))
        self.assertTrue(out["success"])
        self.assertIn("NO_BALANCE", out["refresh_decode"])


class ManyMailDomainParseTest(unittest.TestCase):
    def test_comma_separated_env_splits(self):
        from core import manymail_client
        with patch("config.email.MANYMAIL_DOMAINS", ["lijin.ug.cx,mail.lijin.ug.cx"]):
            self.assertEqual(
                manymail_client.list_domains(force=True),
                ["lijin.ug.cx", "mail.lijin.ug.cx"],
            )

    def test_multiline_env_splits(self):
        from core import manymail_client
        with patch("config.email.MANYMAIL_DOMAINS", ["a.example", "b.example"]):
            self.assertEqual(
                manymail_client.list_domains(force=True),
                ["a.example", "b.example"],
            )


if __name__ == "__main__":
    unittest.main()
