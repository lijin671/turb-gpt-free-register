# -*- coding: utf-8 -*-
"""core/otp_utils 邮箱识别与 OTP 抽取测试（纯逻辑）。"""
import unittest

import core.otp_utils as otp_utils
from core.otp_utils import _get_field, extract_otp, looks_like_openai_email


class OtpUtilsTests(unittest.TestCase):
    def test_get_field_flat_and_nested(self):
        item = {"subject": "hi", "from": {"emailAddress": {"address": "a@b.com"}}}
        self.assertEqual(_get_field(item, "subject"), "hi")
        self.assertEqual(_get_field(item, "from.emailAddress.address"), "a@b.com")
        self.assertEqual(_get_field(item, "missing", "subject"), "hi")
        self.assertEqual(_get_field(item, "missing", "from.emailAddress.missing"), "")

    def test_looks_like_openai_sender_hint(self):
        self.assertTrue(looks_like_openai_email({"sendEmail": "no-reply@openai.com"}))
        self.assertTrue(looks_like_openai_email({"fromName": "OpenAI"}))

    def test_looks_like_openai_multilang_keywords(self):
        self.assertTrue(looks_like_openai_email({"subject": "ChatGPT 验证码"}))
        self.assertTrue(looks_like_openai_email({"content": "OpenAI 認証コード"}))
        self.assertTrue(looks_like_openai_email({"text": "인증 코드 안내"}))
        self.assertTrue(looks_like_openai_email({"bodyPreview": "Your verification code"}))

    def test_looks_like_openai_false_for_unrelated(self):
        self.assertFalse(looks_like_openai_email({"subject": "Weekly digest", "from": "noreply@bank.com"}))

    def test_extract_otp_from_subject(self):
        item = {"subject": "Your OpenAI code is 525210", "text": "code 525210"}
        self.assertEqual(extract_otp(item), "525210")

    def test_extract_otp_from_text_body(self):
        # 123456 属于 at-maker 噪声码（连号），真实 OTP 应为非常规组合
        self.assertEqual(extract_otp({"text": "验证码：837465，5 分钟内有效"}), "837465")
        self.assertIsNone(extract_otp({"text": "验证码：123456，5 分钟内有效"}))

    def test_extract_otp_html_stripped(self):
        html = "<div style='color:red'><b>code</b> 654321 <a href='x'>link</a></div>"
        self.assertEqual(extract_otp({"content": html}), "654321")

    def test_extract_otp_picks_context_nearest(self):
        # 111111 与"验证码"距离超过 ±60 窗口，应被跳过；222222 紧邻关键字被选中
        text = "订单号 111111 已支付。" + "很" * 70 + "验证码：222222"
        self.assertEqual(extract_otp({"text": text}), "222222")

    def test_extract_otp_none_when_no_codes(self):
        self.assertIsNone(extract_otp({"text": "no code here"}))


    def test_decode_quoted_printable_chinese(self):
        # UTF-8 中文 "你的验证码" 的 QP 编码
        qp = "=E4=BD=A0=E7=9A=84=E9=AA=8C=E8=AF=81=E7=A0=81 654321"
        self.assertEqual(otp_utils.decode_quoted_printable(qp), "你的验证码 654321")

    def test_decode_quoted_printable_soft_linebreak(self):
        qp = "Your code is =0D=0A654321=0D=0A"
        self.assertEqual(otp_utils.decode_quoted_printable(qp), "Your code is \r\n654321\r\n")

    def test_decode_quoted_printable_plain_passthrough(self):
        self.assertEqual(otp_utils.decode_quoted_printable("plain text 123456"), "plain text 123456")

    def test_base_mailbox_strips_alias_and_angle(self):
        self.assertEqual(otp_utils.base_mailbox("User+chatgpt@Example.COM"), "user@example.com")
        self.assertEqual(otp_utils.base_mailbox("<u+tag@x.com>"), "u@x.com")
        self.assertEqual(otp_utils.normalize_mailbox("<User@X.com>"), "user@x.com")

    def test_looks_like_openai_qp_chinese_subject(self):
        item = {"subject": "=E4=BD=A0=E7=9A=84=E9=AA=8C=E8=AF=81=E7=A0=81", "text": "654321"}
        self.assertTrue(otp_utils.looks_like_openai_email(item))

    def test_extract_otp_from_qp_body(self):
        item = {
            "subject": "OpenAI",
            "text": "Your verification code is =0D=0A=E2=9C=85 654321",
        }
        self.assertEqual(otp_utils.extract_otp(item), "654321")


    # ---- at-maker 移植：junk 过滤 + 拒绝码记忆 ----

    def test_looks_like_junk_code_dates_and_noise(self):
        self.assertTrue(otp_utils.looks_like_junk_code("202608"))   # YYYYMM
        self.assertTrue(otp_utils.looks_like_junk_code("203912"))   # YYYYMM 上限
        self.assertTrue(otp_utils.looks_like_junk_code("203999"))   # 年份前缀
        self.assertTrue(otp_utils.looks_like_junk_code("120007"))   # 跟踪号
        self.assertTrue(otp_utils.looks_like_junk_code("000000"))
        self.assertTrue(otp_utils.looks_like_junk_code("111111"))
        self.assertTrue(otp_utils.looks_like_junk_code("123456"))
        self.assertTrue(otp_utils.looks_like_junk_code("12345"))    # 非 6 位
        self.assertTrue(otp_utils.looks_like_junk_code(None))

    def test_looks_like_junk_code_false_for_real_otp(self):
        for code in ("525210", "654321", "937482", "384756"):
            self.assertFalse(otp_utils.looks_like_junk_code(code))

    def test_extract_otp_skips_date_noise_in_subject(self):
        # 主题里的 YYYYMM 是噪声，应回退到正文取真实码
        item = {"subject": "ChatGPT 到期时间 202608", "text": "你的验证码：937482"}
        self.assertEqual(extract_otp(item), "937482")

    def test_extract_otp_skips_junk_in_body_fallback(self):
        item = {"subject": "OpenAI", "text": "日期 202608 已更新，请查收"}
        self.assertIsNone(extract_otp(item))

    def test_extract_otp_exclude_codes_skips_rejected(self):
        item = {"subject": "OpenAI", "text": "验证码：937482，5 分钟内有效"}
        self.assertEqual(extract_otp(item), "937482")
        self.assertIsNone(extract_otp(item, exclude_codes={"937482"}))

    def test_mark_and_query_rejected_codes(self):
        otp_utils.mark_otp_rejected("user+tag@x.com", "937482")
        self.assertEqual(otp_utils.rejected_otp_codes("user+tag@x.com"), {"937482"})
        # 非数字混入归一化为 6 位
        otp_utils.mark_otp_rejected("user+tag@x.com", "abc 654321 xyz")
        self.assertEqual(otp_utils.rejected_otp_codes("user+tag@x.com"), {"937482", "654321"})
        otp_utils.clear_otp_rejected("user+tag@x.com")
        self.assertEqual(otp_utils.rejected_otp_codes("user+tag@x.com"), set())

    def test_provider_pattern_skips_rejected_code(self):
        # 模拟各 provider 的调用形态：extract_otp(item, exclude_codes=rejected_otp_codes(email))
        email = "user+chatgpt@x.com"
        otp_utils.mark_otp_rejected(email, "937482")
        item = {"subject": "OpenAI", "text": "验证码：937482"}
        self.assertIsNone(extract_otp(item, exclude_codes=otp_utils.rejected_otp_codes(email)))
        item2 = {"subject": "OpenAI", "text": "验证码：384756"}
        self.assertEqual(extract_otp(item2, exclude_codes=otp_utils.rejected_otp_codes(email)), "384756")
        otp_utils.clear_otp_rejected(email)

    def test_extract_otp_still_returns_real_code_when_only_subject_code(self):
        self.assertEqual(extract_otp({"subject": "Your OpenAI code is 525210"}), "525210")


if __name__ == "__main__":
    unittest.main()

