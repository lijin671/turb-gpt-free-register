# -*- coding: utf-8 -*-
"""core/profile_utils 生日/密码生成测试（纯逻辑）。"""
import unittest
from datetime import date
from unittest.mock import patch

import core.profile_utils as pu


class ProfileUtilsTests(unittest.TestCase):
    def test_generate_random_birthday_age_bounds(self):
        today = date(2026, 8, 5)
        for _ in range(50):
            b = pu.generate_random_birthday(min_age=18, max_age=65)
            y, m, d = (int(x) for x in b.split("-"))
            age = today.year - y - ((today.month, today.day) < (m, d))
            self.assertGreaterEqual(age, 18)
            self.assertLessEqual(age, 65)
            self.assertGreaterEqual(m, 1)
            self.assertLessEqual(m, 12)

    def test_shift_year_safe_leap_day(self):
        self.assertEqual(pu._shift_year_safe(date(2024, 2, 29), 1), date(2025, 2, 28))
        self.assertEqual(pu._shift_year_safe(date(2024, 2, 29), 4), date(2028, 2, 29))

    def test_generate_random_password_length_and_complexity(self):
        pw = pu.generate_random_password(length=20)
        self.assertEqual(len(pw), 20)
        self.assertTrue(any(c.isupper() for c in pw))
        self.assertTrue(any(c.islower() for c in pw))
        self.assertTrue(any(c.isdigit() for c in pw))
        self.assertTrue(any(c in "!@#$%^&*" for c in pw))

    def test_generate_random_password_short_length(self):
        pw = pu.generate_random_password(length=4)
        self.assertEqual(len(pw), 4)  # 恰好四类各一个
        self.assertTrue(any(c.isupper() for c in pw))
        self.assertTrue(any(c.islower() for c in pw))
        self.assertTrue(any(c.isdigit() for c in pw))
        self.assertTrue(any(c in "!@#$%^&*" for c in pw))


if __name__ == "__main__":
    unittest.main()
