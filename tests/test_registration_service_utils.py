# -*- coding: utf-8 -*-
"""core/registration_service 失败判定与邮箱停用逻辑测试（纯逻辑 + mock release_email）。"""
import unittest
from unittest.mock import patch

import core.registration_service as rs


class RegistrationServiceUtilsTests(unittest.TestCase):
    def test_final_session_access_token_timeout_detected(self):
        err = "等待 /api/auth/session accessToken 超时 WARNING_BANNER {'_http_status': 200}"
        self.assertTrue(rs._is_final_session_access_token_timeout(err))
        self.assertFalse(rs._is_final_session_access_token_timeout("其他错误"))
        self.assertFalse(rs._is_final_session_access_token_timeout(""))
        self.assertFalse(rs._is_final_session_access_token_timeout(None))

    def test_should_disable_email_timeout(self):
        err = "等待 /api/auth/session accessToken 超时 WARNING_BANNER {'_http_status': 200}"
        self.assertTrue(rs._should_disable_failed_registration_email(err))

    def test_should_disable_email_login_password_page(self):
        self.assertTrue(rs._should_disable_failed_registration_email(
            "邮箱提交后进入登录密码页 auth.openai.com/log-in/password"))
        self.assertTrue(rs._should_disable_failed_registration_email(
            "unexpected /log-in/password redirect"))

    def test_should_not_disable_ordinary_failure(self):
        self.assertFalse(rs._should_disable_failed_registration_email(
            "代理 407 CONNECT tunnel failed"))
        self.assertFalse(rs._should_disable_failed_registration_email(""))

    def test_disable_job_email_calls_release(self):
        with patch("core.email_provider.release_email",
                   return_value="manymail") as release:
            ok = rs._disable_job_email("a@b.com", "final timeout")
        self.assertTrue(ok)
        release.assert_called_once()
        self.assertEqual(release.call_args.kwargs["status"], "disabled")
        self.assertIn("final timeout", release.call_args.kwargs["note"])

    def test_disable_job_email_empty_email(self):
        with patch("core.email_provider.release_email") as release:
            self.assertFalse(rs._disable_job_email("", "x"))
        release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
