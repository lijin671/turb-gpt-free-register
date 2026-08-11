# -*- coding: utf-8 -*-
"""hCaptcha 音频挑战求解器单元测试。"""
import unittest

from core.hcaptcha_audio import normalize_hcaptcha_answer


class TestNormalizeHcaptchaAnswer(unittest.TestCase):
    def test_number_words(self):
        self.assertEqual(normalize_hcaptcha_answer("three five eight two"), "3582")

    def test_mixed_words_and_digits(self):
        self.assertEqual(normalize_hcaptcha_answer("four 2 seven"), "427")

    def test_letters_kept(self):
        # hCaptcha 音频也可能读字母序列
        self.assertEqual(normalize_hcaptcha_answer("a b c"), "abc")

    def test_punctuation_stripped(self):
        self.assertEqual(normalize_hcaptcha_answer("3, 5, 8, 2."), "3582")

    def test_oh_as_zero(self):
        self.assertEqual(normalize_hcaptcha_answer("one oh two"), "102")

    def test_empty(self):
        self.assertEqual(normalize_hcaptcha_answer(""), "")
        self.assertEqual(normalize_hcaptcha_answer(None), "")

    def test_uppercase(self):
        self.assertEqual(normalize_hcaptcha_answer("FIVE THREE"), "53")


if __name__ == "__main__":
    unittest.main()
