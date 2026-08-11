# -*- coding: utf-8 -*-
"""core/humanize 随机延迟逻辑测试（mock time.sleep / random.uniform）。"""
import unittest
from unittest.mock import patch

import config.humanize as humanize_cfg
import core.humanize as humanize_mod


class HumanizeTests(unittest.TestCase):
    def test_disabled_returns_zero_no_sleep(self):
        with patch.object(humanize_cfg, "ENABLE_HUMANIZE_DELAY", False), \
             patch.object(humanize_mod.time, "sleep") as sleep:
            self.assertEqual(humanize_mod.delay("api"), 0.0)
        sleep.assert_not_called()

    def test_returns_sleep_value_within_bounds(self):
        with patch.object(humanize_cfg, "ENABLE_HUMANIZE_DELAY", True), \
             patch.object(humanize_cfg, "HUMANIZE_DELAYS", {"api": (0.5, 1.0)}), \
             patch.object(humanize_cfg, "HUMANIZE_DELAY_FACTOR", 1.0), \
             patch.object(humanize_mod.random, "uniform", return_value=0.75) as uni, \
             patch.object(humanize_mod.time, "sleep") as sleep:
            got = humanize_mod.delay("api")
        self.assertEqual(got, 0.75)
        sleep.assert_called_once_with(0.75)
        uni.assert_called_once_with(0.5, 1.0)

    def test_factor_scales_bounds(self):
        with patch.object(humanize_cfg, "ENABLE_HUMANIZE_DELAY", True), \
             patch.object(humanize_cfg, "HUMANIZE_DELAYS", {"api": (1.0, 2.0)}), \
             patch.object(humanize_cfg, "HUMANIZE_DELAY_FACTOR", 0.5), \
             patch.object(humanize_mod.random, "uniform", return_value=0.6) as uni, \
             patch.object(humanize_mod.time, "sleep") as sleep:
            got = humanize_mod.delay("api")
        self.assertEqual(got, 0.6)
        uni.assert_called_once_with(0.5, 1.0)

    def test_override_minimum_maximum(self):
        with patch.object(humanize_cfg, "ENABLE_HUMANIZE_DELAY", True), \
             patch.object(humanize_cfg, "HUMANIZE_DELAY_FACTOR", 1.0), \
             patch.object(humanize_mod.random, "uniform", return_value=3.0) as uni, \
             patch.object(humanize_mod.time, "sleep") as sleep:
            got = humanize_mod.delay("api", minimum=2.0, maximum=4.0)
        self.assertEqual(got, 3.0)
        uni.assert_called_once_with(2.0, 4.0)


if __name__ == "__main__":
    unittest.main()
