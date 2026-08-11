# -*- coding: utf-8 -*-
"""hCaptcha 尺寸挑战决策逻辑单测（纯函数，无需服务）。"""
import sys
import unittest

sys.path.insert(0, "tools")
import hcaptcha_clip_server as H


class TestSizeDecision(unittest.TestCase):
    def test_smaller_hits(self):
        # run25 实测：参考=donkey(size 4)，墨镜 top3 全 size 1 → 命中
        self.assertTrue(H.decide_size_hit(
            [("sunglasses", 0.82), ("car", 0.085), ("glasses", 0.071)], 4, True))
        # 低置信但 top3 全小 → 命中（cell4: sunglasses 0.159）
        self.assertTrue(H.decide_size_hit(
            [("sunglasses", 0.159), ("glasses", 0.147), ("cap", 0.066)], 4, True))

    def test_smaller_rejects(self):
        # 同尺寸（donkey=4）→ 不命中
        self.assertFalse(H.decide_size_hit(
            [("donkey", 0.964), ("camel", 0.012), ("horse", 0.008)], 4, True))
        # 更大（cow=4? 同等级 4 不算更小；horse=4）→ 不命中
        self.assertFalse(H.decide_size_hit(
            [("cow", 0.589), ("sheep", 0.052), ("ant", 0.037)], 4, True))

    def test_larger_hits(self):
        self.assertTrue(H.decide_size_hit(
            [("elephant", 0.9), ("hippo", 0.05), ("whale", 0.02)], 4, False))
        self.assertFalse(H.decide_size_hit(
            [("sunglasses", 0.82), ("car", 0.085), ("glasses", 0.071)], 4, False))


if __name__ == "__main__":
    unittest.main()
