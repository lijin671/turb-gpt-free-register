# -*- coding: utf-8 -*-
"""main 批量失败原因分类统计单测（纯逻辑）。

覆盖：账号已废（中/英）、IP 纪律、OTP 超时、Codex 未完成、其他/未知的归类，
以及 _summarize_failures 的成功/失败分流与计数。
"""
import unittest

import main as main_mod


class FailureBucketTests(unittest.TestCase):
    def test_account_dead_english(self):
        self.assertEqual(
            main_mod._failure_reason_bucket("Browser driver: your account has been deactivated"),
            "account_dead",
        )

    def test_account_dead_chinese(self):
        for text in ("账号已废弃", "账号已废（account_deactivated）", "账号已停用", "账户已删除"):
            self.assertEqual(main_mod._failure_reason_bucket(text), "account_dead", text)

    def test_ip_discipline(self):
        self.assertEqual(
            main_mod._failure_reason_bucket("[IP纪律] 300s 内无可用出口 IP（代理池在冷却/占满）"),
            "ip_discipline",
        )

    def test_otp_timeout(self):
        for text in ("等待 ManyMail 验证码超时: inbox empty", "等待验证码超时: inbox empty",
                     "OTP 验证码错误/过期"):
            self.assertEqual(main_mod._failure_reason_bucket(text), "otp_timeout", text)

    def test_codex(self):
        self.assertEqual(
            main_mod._failure_reason_bucket("Codex 未完成: 手机号接码失败"),
            "codex",
        )

    def test_other_and_unknown(self):
        self.assertEqual(main_mod._failure_reason_bucket("curl error 28 timeout"), "other")
        self.assertEqual(main_mod._failure_reason_bucket(""), "unknown")
        self.assertEqual(main_mod._failure_reason_bucket(None), "unknown")


class SummarizeFailuresTests(unittest.TestCase):
    def test_counts_mixed_failures_and_skips_success(self):
        results = [
            {"success": True, "email": "a@x.com"},
            {"success": False, "error": "等待验证码超时: inbox empty"},
            {"success": False, "error": "等待验证码超时: inbox empty"},
            {"success": False, "error": "账号已废弃（account_deactivated）"},
            {"success": False, "error": "curl error 28"},
            {"success": False, "error": ""},
        ]
        buckets = main_mod._summarize_failures(results)
        self.assertEqual(buckets.get("otp_timeout"), 2)
        self.assertEqual(buckets.get("account_dead"), 1)
        self.assertEqual(buckets.get("other"), 1)
        self.assertEqual(buckets.get("unknown"), 1)
        self.assertEqual(sum(buckets.values()), 5)

    def test_all_success_returns_empty(self):
        self.assertEqual(
            main_mod._summarize_failures([{"success": True}, {"success": True}]),
            {},
        )

    def test_labels_cover_all_buckets(self):
        for bucket in ("account_dead", "ip_discipline", "otp_timeout", "codex", "other", "unknown"):
            self.assertIn(bucket, main_mod._FAILURE_BUCKET_LABELS)


if __name__ == "__main__":
    unittest.main()
