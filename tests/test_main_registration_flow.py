# -*- coding: utf-8 -*-
"""main._run_registration_impl 协议注册全流程编排集成测试（全 mock，无网络）。

覆盖 main.py 阶段1→9.5 的协议 driver 编排：
  - 密码分支（create-account/password → user/register → 显式 send_email_otp）
  - 直连邮箱验证分支（跳过 user/register）
  - OTP 错误自动重发重试
  - OTP 后 external_url 直连 OAuth 回调（跳过 create_account）
  - dead-account 邮箱回收（release_email status=failed）
"""
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import main as main_mod
import config.email as cemail_cfg
import config.export as cexport
import config.proxy as cproxy
import core.chatgpt_bootstrap as cgb
import core.chatgpt2api_export as cc2api
import core.codex_oauth as cco
import core.cpa_manager_import as ccmi
import core.email_provider as cep
import core.openai_auth as coa
import core.flow_trigger as cft
import core.plus_integration as cpi
import core.profile_utils as cpu
from core.openai_auth import AccountUnusableError, EmailOtpInvalidError


class FakeSession:
    proxy = "http://user-region-KR-sid-AAAAAAAA-t-5:pass@127.0.0.1:2260"
    device_id = "dev-1"
    auth_session_logging_id = "log-1"
    exit_geo = {"ip": "1.2.3.4", "country": "KR"}
    sentinel_sid = "sid-AAAAAAAA"
    browser_profile = "fake"


ABOUT_YOU_RESP = {
    "page": {"type": "about_you"},
    "continue_url": "https://auth.openai.com/about-you",
}


def _base_patches(**kw):
    follow_authorize = kw.pop("follow_authorize", "https://auth.openai.com/email-verification?x=1")
    validate = kw.pop("validate_email_otp", ABOUT_YOU_RESP)
    return [
        patch.object(main_mod, "_create_session_with_preflight", return_value=FakeSession()),
        patch.object(main_mod, "human_delay", return_value=0.0),
        patch.object(main_mod, "get_providers", return_value=["openai"]),
        patch.object(main_mod, "get_csrf_token", return_value="csrf-1"),
        patch.object(main_mod, "signin_openai",
                     return_value="https://auth.openai.com/api/accounts/authorize?x=1"),
        patch.object(main_mod, "follow_authorize", return_value=follow_authorize),
        patch.object(main_mod, "request_sentinel_header_with_retry", return_value=("sent-hdr", "so-hdr")),
        patch.object(main_mod, "send_email_otp", return_value=None),
        patch.object(main_mod, "wait_for_otp", return_value="654321"),
        patch.object(main_mod, "validate_email_otp", return_value=validate),
        patch.object(main_mod, "navigate_about_you", return_value=None),
        patch.object(main_mod, "create_account", return_value={
            "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=abc",
        }),
        patch.object(main_mod, "follow_oauth_callback", return_value=None),
        patch.object(main_mod, "fetch_session", return_value={
            "accessToken": "tok-123", "user": {"email": "a@b.com"},
            "account": {"id": "acc-1"}, "expires": 999,
        }),
        patch.object(main_mod, "setup_2fa", return_value=None),
        patch.object(main_mod, "save_account_data", return_value="acc-1"),
        patch.object(cgb, "anonymous_bootstrap", return_value=None),
        patch.object(cgb, "authenticated_bootstrap", return_value=None),
        patch.object(cco, "run_codex_oauth",
                     return_value={"status": "skipped", "ok": False, "message": "未配置"}),
        patch.object(cft, "trigger_flow",
                     return_value={"status": "skipped", "ok": False, "message": "未触发"}),
        patch.object(cpi, "try_zero_plus_after_registration",
                     return_value={"status": "skipped", "ok": False, "message": "未触发"}),
        patch.object(ccmi, "import_single_account", return_value={"ok": False, "message": "跳过"}),
        patch.object(cc2api, "export_account_to_chatgpt2api", return_value={"ok": False, "message": "跳过"}),
        patch.object(cep, "resolve_email_source", return_value="manymail"),
        patch.object(cproxy, "proxy_ip_key", return_value="ip-key-1"),
        patch.object(cexport, "AUTO_EXPORT_TO_CHATGPT2API", False),
        patch.object(cexport, "AUTO_IMPORT_TO_CPA_MANAGER", False),
        patch.object(cemail_cfg, "USE_EMAIL_SERVICE", True),
    ]


def _stack(patchers):
    """把 _base_patches 返回的 patch 列表压入 ExitStack，作为 with 上下文。"""
    st = ExitStack()
    for p in patchers:
        st.enter_context(p)
    return st


class MainRegistrationFlowTests(unittest.TestCase):
    EMAIL = "flow.test@example.com"

    def _call(self, otp_code="123456", **kw):
        return main_mod._run_registration_impl(
            email=self.EMAIL, name="Flow Tester",
            proxy="http://user:pass@127.0.0.1:2260",
            otp_code=otp_code, batch_dir=None, **kw,
        )

    def test_password_branch_full_flow(self):
        """A/B 分流到 create-account/password：先 user/register 设密码，再显式发 OTP，密码随账号落盘。"""
        with _stack(_base_patches(
            follow_authorize="https://auth.openai.com/create-account/password?x=1",
        )), \
             patch.object(main_mod, "is_password_branch_url", return_value=True), \
             patch.object(coa, "register_user", return_value={"ok": True}) as reg, \
             patch.object(main_mod, "send_email_otp") as send_otp, \
             patch.object(cpu, "registration_password", return_value="GenPw123!") as gen_pw, \
             patch.object(main_mod, "save_account_data") as save:
            r = self._call()

        self.assertTrue(r["success"], r)
        reg.assert_called_once()
        args = reg.call_args
        self.assertEqual(args.args[1], self.EMAIL)
        self.assertEqual(args.args[2], "GenPw123!")
        self.assertEqual(args.args[3], "sent-hdr")
        self.assertEqual(args.args[4], "so-hdr")
        # 密码分支下 OTP 不会自动发，必须显式调用
        send_otp.assert_called_once()
        gen_pw.assert_called_once()
        save_kwargs = save.call_args.kwargs
        self.assertEqual(save_kwargs["email"], self.EMAIL)
        self.assertEqual(save_kwargs["ip_key"], "ip-key-1")
        self.assertEqual(save_kwargs["exit_ip"], "1.2.3.4")
        self.assertEqual(save_kwargs["extra"]["password"], "GenPw123!")

    def test_direct_email_verification_skips_register_user(self):
        """正常 OTP-only 路径：不调用 user/register，走 about-you → create_account。"""
        with _stack(_base_patches()), \
             patch.object(coa, "register_user") as reg, \
             patch.object(main_mod, "send_email_otp") as send_otp, \
             patch.object(main_mod, "navigate_about_you") as nav, \
             patch.object(main_mod, "create_account") as create, \
             patch.object(main_mod, "save_account_data") as save:
            r = self._call()

        self.assertTrue(r["success"], r)
        reg.assert_not_called()
        send_otp.assert_not_called()
        nav.assert_called_once()
        create.assert_called_once()
        save_kwargs = save.call_args.kwargs
        self.assertIsNone(save_kwargs["extra"]["password"])
        self.assertEqual(save_kwargs["extra"]["account"]["id"], "acc-1")

    def test_otp_wait_timeout_resends_once(self):
        """等码超时：显式补发一次再等，第二次取到码后继续完成注册。"""
        calls = {"n": 0}

        def _wait(email, after_ts=None, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("等待验证码超时: inbox empty")
            return "654321"

        with _stack(_base_patches()), \
             patch.object(main_mod, "wait_for_otp", side_effect=_wait) as wait, \
             patch.object(main_mod, "send_email_otp") as send_otp, \
             patch.object(main_mod, "save_account_data") as save:
            r = self._call(otp_code=None)

        self.assertTrue(r["success"], r)
        self.assertEqual(wait.call_count, 2)
        send_otp.assert_called_once()
        save.assert_called_once()

    def test_otp_wait_timeout_twice_fails_after_resend(self):
        """等码持续超时：补发后仍无码，任务失败并回收邮箱。"""
        def _wait(email, after_ts=None, **kw):
            raise RuntimeError("等待验证码超时: inbox empty")

        with _stack(_base_patches()), \
             patch.object(main_mod, "wait_for_otp", side_effect=_wait) as wait, \
             patch.object(main_mod, "send_email_otp") as send_otp, \
             patch.object(cep, "release_email", return_value="manymail") as release:
            r = self._call(otp_code=None)

        self.assertFalse(r["success"])
        self.assertIn("等待验证码超时", r["error"])
        self.assertEqual(wait.call_count, 3)
        self.assertEqual(send_otp.call_count, 2)
        release.assert_called_once()
        self.assertEqual(release.call_args.kwargs["status"], "available")

    def test_otp_wait_timeout_resend_disabled_raises(self):
        """OTP_RESEND_ON_TIMEOUT=False 时等码超时直接失败，不补发。"""
        def _wait(email, after_ts=None, **kw):
            raise RuntimeError("等待验证码超时: inbox empty")

        with _stack(_base_patches()), \
             patch.object(main_mod, "wait_for_otp", side_effect=_wait) as wait, \
             patch.object(main_mod, "send_email_otp") as send_otp, \
             patch.object(main_mod._protocol_cfg, "OTP_RESEND_ON_TIMEOUT", False, create=True), \
             patch.object(cep, "release_email", return_value="manymail"):
            r = self._call(otp_code=None)

        self.assertFalse(r["success"])
        wait.assert_called_once()
        send_otp.assert_not_called()

    def test_otp_invalid_retry_resends(self):
        """OTP 错误：自动重发并重取验证码，validate 调用两次后成功。"""
        calls = {"n": 0}

        def _validate(session, otp, sentinel=None, so=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise EmailOtpInvalidError("invalid code")
            return ABOUT_YOU_RESP

        with _stack(_base_patches()), \
             patch.object(main_mod, "validate_email_otp", side_effect=_validate) as v, \
             patch.object(main_mod, "send_email_otp") as send_otp, \
             patch.object(main_mod, "wait_for_otp", return_value="654321") as wait:
            r = self._call(otp_code="000000")

        self.assertTrue(r["success"], r)
        self.assertEqual(v.call_count, 2)
        send_otp.assert_called_once()
        wait.assert_called_once()

    def test_otp_external_url_direct_oauth(self):
        """OTP 后返回 external_url：跳过 create_account，直接 OAuth 回调拿 token。"""
        external = {
            "page": {"type": "external_url"},
            "external_url": "https://chatgpt.com/api/auth/callback/openai?code=ext",
        }
        with _stack(_base_patches(validate_email_otp=external)), \
             patch.object(main_mod, "navigate_about_you") as nav, \
             patch.object(main_mod, "create_account") as create, \
             patch.object(main_mod, "follow_oauth_callback") as cb, \
             patch.object(main_mod, "save_account_data") as save:
            r = self._call()

        self.assertTrue(r["success"], r)
        nav.assert_not_called()
        create.assert_not_called()
        cb.assert_called_once()
        self.assertEqual(save.call_args.kwargs["extra"]["account"]["id"], "acc-1")

    def _call_wrapped(self, otp_code="123456", **kw):
        """走 run_registration 包装（连坐收口点），显式传代理避免触发 IP 纪律等待。"""
        return main_mod.run_registration(
            email=self.EMAIL, name="Flow Tester",
            proxy="http://user:pass@127.0.0.1:2260",
            otp_code=otp_code, batch_dir=None, **kw,
        )

    def test_dead_account_marks_ip_co_risk(self):
        """注册阶段确认账号死亡：除邮箱标 failed 外，出口 IP 标记连坐风险（收口点）。"""
        def _reg(*a, **k):
            raise AccountUnusableError("账号已废弃", error_code="account_deactivated")

        with _stack(_base_patches(
            follow_authorize="https://auth.openai.com/create-account/password?x=1",
        )), \
             patch.object(main_mod, "is_password_branch_url", return_value=True), \
             patch.object(coa, "register_user", side_effect=_reg), \
             patch.object(cep, "release_email", return_value="manymail") as release, \
             patch("core.ip_discipline.mark_ip_co_risk") as mark:
            r = self._call_wrapped(otp_code=None)

        self.assertFalse(r["success"])
        self.assertIn("废弃", r["error"])
        release.assert_called_once()
        mark.assert_called_once()
        mark_args = mark.call_args.args
        mark_kwargs = mark.call_args.kwargs
        self.assertEqual(mark_kwargs["emails"], [self.EMAIL])
        self.assertEqual(mark_args[0], "http://user:pass@127.0.0.1:2260")
        self.assertIn("死亡", mark_args[1])

    def test_codex_deactivated_marks_ip_co_risk(self):
        """Codex 确认账号死亡（deactivated）：任务标失败并隔离出口 IP（收口点）。"""
        deactivated = {"status": "deactivated", "ok": False, "email": self.EMAIL,
                       "message": "账号已废（account_deactivated）"}
        with _stack(_base_patches()), \
             patch.object(cco, "run_codex_oauth", return_value=deactivated), \
             patch("core.ip_discipline.mark_ip_co_risk") as mark:
            r = self._call_wrapped()

        self.assertFalse(r["success"])
        self.assertIn("Codex 未完成", r["error"])
        mark.assert_called_once()
        mark_args = mark.call_args.args
        mark_kwargs = mark.call_args.kwargs
        self.assertEqual(mark_kwargs["emails"], [self.EMAIL])
        self.assertIn("确认账号死亡", mark_args[1])

    def test_driver_dead_text_marks_ip_co_risk(self):
        """跨驱动兜底：任意驱动（roxy/cloak/browser_use/skyvern）返回死号文本即隔离 IP。"""
        with patch.object(main_mod, "_run_registration_impl",
                          return_value={"success": False, "email": self.EMAIL,
                                        "error": "Browser driver: your account has been deactivated"}), \
             patch("core.ip_discipline.mark_ip_co_risk") as mark:
            r = main_mod.run_registration(
                email=self.EMAIL, name="Flow Tester",
                proxy="http://user:pass@127.0.0.1:2260",
                otp_code=None, batch_dir=None,
            )

        self.assertFalse(r["success"])
        mark.assert_called_once()
        mark_args = mark.call_args.args
        mark_kwargs = mark.call_args.kwargs
        self.assertEqual(mark_kwargs["emails"], [self.EMAIL])
        self.assertEqual(mark_args[0], "http://user:pass@127.0.0.1:2260")
        self.assertIn("确认账号死亡", mark_args[1])

    def test_driver_failure_without_dead_text_no_quarantine(self):
        """普通失败（网络/风控）不触发连坐隔离，避免误伤 IP。"""
        with patch.object(main_mod, "_run_registration_impl",
                          return_value={"success": False, "email": self.EMAIL,
                                        "error": "curl error 28 timeout"}), \
             patch("core.ip_discipline.mark_ip_co_risk") as mark:
            r = main_mod.run_registration(
                email=self.EMAIL, name="Flow Tester",
                proxy="http://user:pass@127.0.0.1:2260",
                otp_code=None, batch_dir=None,
            )

        self.assertFalse(r["success"])
        mark.assert_not_called()

    def test_dead_account_releases_email_failed(self):
        """user/register 报 account_deactivated：邮箱标记 failed 剔除，不再复用。"""
        def _reg(*a, **k):
            raise AccountUnusableError("账号已废弃", error_code="account_deactivated")

        with _stack(_base_patches(
            follow_authorize="https://auth.openai.com/create-account/password?x=1",
        )), \
             patch.object(main_mod, "is_password_branch_url", return_value=True), \
             patch.object(coa, "register_user", side_effect=_reg), \
             patch.object(cep, "release_email",
                          return_value="manymail") as release:
            r = self._call()

        self.assertFalse(r["success"])
        self.assertIn("废弃", r["error"])
        release.assert_called_once()
        self.assertEqual(release.call_args.kwargs["status"], "failed")
        self.assertIn("废弃", release.call_args.kwargs["note"])


if __name__ == "__main__":
    unittest.main()
