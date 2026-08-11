# -*- coding: utf-8 -*-
"""core.phone_verify_refresh（Clqx fork 移植）冒烟测试：mock 网络层。"""
import base64
import json
import unittest
from unittest.mock import MagicMock, patch

from core import phone_verify_refresh as pvr
from config import codex as codex_cfg


def _make_jwt(email: str, plan: str = "free", exp_offset: int = 3600) -> str:
    def b64(d: dict) -> str:
        raw = json.dumps(d).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    import time
    header = b64({"alg": "none", "typ": "JWT"})
    payload = {
        "https://api.openai.com/profile": {"email": email, "name": email.split("@")[0]},
        "https://api.openai.com/auth": {"chatgpt_plan_type": plan},
        "exp": int(time.time()) + exp_offset,
    }
    return f"{header}.{b64(payload)}.sig"


class ResolveAccountTest(unittest.TestCase):
    def test_resolve_from_token(self):
        claims = pvr.resolve_account_from_token(_make_jwt("a@example.com", "free"))
        self.assertEqual(claims.get("email"), "a@example.com")
        self.assertFalse(claims.get("token_expired"))

    def test_empty_token_raises(self):
        with self.assertRaises(pvr.PhoneVerifyRefreshError):
            pvr.resolve_account_from_token("")

    def test_expired_token_raises(self):
        with self.assertRaises(pvr.PhoneVerifyRefreshError):
            pvr.resolve_account_from_token(_make_jwt("a@example.com", exp_offset=-10))


class ForceGrizzlyContextTest(unittest.TestCase):
    def test_restores_provider(self):
        old = codex_cfg.SMS_PROVIDER
        codex_cfg.SMS_PROVIDER = "hero"
        try:
            with pvr._ForceGrizzlyProvider():
                self.assertEqual(codex_cfg.SMS_PROVIDER, "grizzly")
            self.assertEqual(codex_cfg.SMS_PROVIDER, "hero")
        finally:
            codex_cfg.SMS_PROVIDER = old


class RunPhoneVerifyRefreshTest(unittest.TestCase):
    def _patch_flow(self, refresh_token="rt-abc123"):
        patches = [
            patch.object(pvr, "resolve_account_from_token",
                         return_value={"email": "u@example.com", "token_expired": False}),
            patch.object(pvr, "check_account_alive", return_value={"ok": True}),
            patch("core.phone_verify_refresh.BrowserSession"),
            patch.object(pvr, "network_preflight"),
            patch.object(pvr, "human_delay"),
            patch.object(pvr, "_bootstrap_authorize"),
            patch.object(pvr, "_submit_email"),
            patch.object(pvr, "_submit_email_otp"),
            patch.object(pvr, "grizzly_verify_phone",
                         return_value={"activation_id": 1, "phone": "6391", "code": "1234", "attempts": 1}),
            patch.object(pvr, "_select_workspace_and_get_callback", return_value="cb://?code=xyz&state=s"),
            patch.object(pvr, "_extract_code", return_value="auth-code-1"),
            patch.object(pvr, "exchange_codex_token",
                         return_value={
                             "refresh_token": refresh_token,
                             "access_token": "at-new",
                             "id_token": _make_jwt("u@example.com", "free"),
                             "expires_in": 3600,
                         }),
            patch.object(pvr, "build_codex_storage", return_value={"storage": 1}),
            patch.object(pvr, "save_codex_credential", return_value="/tmp/codex-u@example.com.json"),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def test_full_flow_ok(self):
        self._patch_flow()
        out = pvr.run_phone_verify_refresh(
            access_token=_make_jwt("u@example.com"),
            otp_provider=lambda email, after_ts: "654321",
            skip_plan_check=False,
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["refresh_token"], "rt-abc123")
        self.assertEqual(out["email"], "u@example.com")
        self.assertEqual(out["phone"]["code"], "1234")
        self.assertEqual(out["file_path"], "/tmp/codex-u@example.com.json")

    def test_failure_returns_ok_false(self):
        self._patch_flow()
        with patch.object(pvr, "exchange_codex_token",
                          side_effect=RuntimeError("network down")):
            out = pvr.run_phone_verify_refresh(
                access_token=_make_jwt("u@example.com"),
                otp_provider=lambda email, after_ts: "654321",
                skip_plan_check=True,
            )
        self.assertFalse(out["ok"])
        self.assertIn("network down", out["error"])


if __name__ == "__main__":
    unittest.main()
