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


class ForceSmsProviderContextTest(unittest.TestCase):
    def test_restores_provider(self):
        old = codex_cfg.SMS_PROVIDER
        codex_cfg.SMS_PROVIDER = "hero"
        try:
            with pvr._ForceSmsProvider("grizzly"):
                self.assertEqual(codex_cfg.SMS_PROVIDER, "grizzly")
            self.assertEqual(codex_cfg.SMS_PROVIDER, "hero")
        finally:
            codex_cfg.SMS_PROVIDER = old

    def test_hero_overrides_and_restores_plus_config(self):
        from config import plus as plus_cfg
        old_provider = codex_cfg.SMS_PROVIDER
        before = (plus_cfg.HERO_SMS_SERVICE, plus_cfg.HERO_SMS_COUNTRY, plus_cfg.HERO_SMS_MAX_PRICE)
        try:
            with pvr._ForceSmsProvider("hero", hero_country=4):
                self.assertEqual(codex_cfg.SMS_PROVIDER, "hero")
                self.assertEqual(plus_cfg.HERO_SMS_SERVICE, codex_cfg.REFRESH_DECODE_HERO_SERVICE)
                self.assertEqual(plus_cfg.HERO_SMS_COUNTRY, 4)
            self.assertEqual(
                (plus_cfg.HERO_SMS_SERVICE, plus_cfg.HERO_SMS_COUNTRY, plus_cfg.HERO_SMS_MAX_PRICE),
                before,
            )
        finally:
            codex_cfg.SMS_PROVIDER = old_provider


class DecodeSmsPlanTest(unittest.TestCase):
    def test_parses_provider_and_country(self):
        old = codex_cfg.REFRESH_DECODE_SMS_PROVIDERS
        codex_cfg.REFRESH_DECODE_SMS_PROVIDERS = "grizzly,hero:187,hero:4"
        try:
            self.assertEqual(
                pvr._decode_sms_plan(),
                [("grizzly", None), ("hero", 187), ("hero", 4)],
            )
        finally:
            codex_cfg.REFRESH_DECODE_SMS_PROVIDERS = old

    def test_empty_falls_back_to_grizzly_then_hero(self):
        old = codex_cfg.REFRESH_DECODE_SMS_PROVIDERS
        codex_cfg.REFRESH_DECODE_SMS_PROVIDERS = ""
        try:
            self.assertEqual(pvr._decode_sms_plan(), [("grizzly", None), ("hero", None)])
        finally:
            codex_cfg.REFRESH_DECODE_SMS_PROVIDERS = old


class VerifyPhoneFallbackTest(unittest.TestCase):
    def test_no_balance_falls_through_to_next_provider(self):
        calls = []

        def fake_verify(session, max_retries=None):
            provider = codex_cfg.SMS_PROVIDER
            calls.append(provider)
            if provider == "grizzly":
                raise pvr.sms_provider.SmsNoBalanceError("NO_BALANCE")
            return {"phone": "16195551234", "provider": provider, "attempts": 1}

        with patch.object(pvr, "sms_verify_phone", side_effect=fake_verify):
            out = pvr.verify_phone_with_fallback(
                MagicMock(), plan=[("grizzly", None), ("hero", 187)]
            )
        self.assertEqual(calls, ["grizzly", "hero"])
        self.assertEqual(out["provider"], "hero")

    def test_all_providers_fail_raises(self):
        with patch.object(pvr, "sms_verify_phone",
                          side_effect=pvr.sms_provider.SmsNoBalanceError("NO_BALANCE")):
            with self.assertRaises(pvr.PhoneVerifyRefreshError):
                pvr.verify_phone_with_fallback(
                    MagicMock(), plan=[("grizzly", None), ("hero", 187)]
                )


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
            patch.object(pvr, "verify_phone_with_fallback",
                         return_value={"activation_id": 1, "phone": "6391", "code": "1234",
                                       "attempts": 1, "provider": "grizzly"}),
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


class CheckAccountAliveRetryTest(unittest.TestCase):
    """出口脏（http_status=None）换 sid 重试；服务端明确答复则立即停。"""

    def test_network_error_retries_then_succeeds(self):
        calls = []

        def fake_check(token, proxy=None):
            calls.append(proxy)
            if len(calls) < 3:
                return {"ok": False, "http_status": None, "error": "CONNECT tunnel failed 504"}
            return {"ok": True, "plan_type": "free"}

        with patch.object(pvr, "check_account_plan", side_effect=fake_check), \
             patch("config.proxy.pick_proxy", side_effect=lambda: "http://p%d@127.0.0.1:2260" % len(calls)):
            out = pvr.check_account_alive("at-x", proxy="http://first@127.0.0.1:2260")
        self.assertTrue(out["ok"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], "http://first@127.0.0.1:2260")
        self.assertNotEqual(calls[1], calls[0])

    def test_definitive_http_status_stops_immediately(self):
        calls = []

        def fake_check(token, proxy=None):
            calls.append(proxy)
            return {"ok": False, "http_status": 401, "error": "token_invalidated"}

        with patch.object(pvr, "check_account_plan", side_effect=fake_check), \
             patch("config.proxy.pick_proxy", return_value="http://p@127.0.0.1:2260"):
            with self.assertRaises(pvr.PhoneVerifyRefreshError) as ctx:
                pvr.check_account_alive("at-x")
        self.assertEqual(len(calls), 1)
        self.assertIn("401", str(ctx.exception))

    def test_all_network_attempts_fail_raises(self):
        with patch.object(pvr, "check_account_plan",
                          return_value={"ok": False, "http_status": None, "error": "SSL EOF"}), \
             patch("config.proxy.pick_proxy", return_value="http://p@127.0.0.1:2260"):
            with self.assertRaises(pvr.PhoneVerifyRefreshError):
                pvr.check_account_alive("at-x")


if __name__ == "__main__":
    unittest.main()
