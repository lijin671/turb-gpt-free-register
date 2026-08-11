# -*- coding: utf-8 -*-
"""hCaptcha 十字标记检测单元测试（本地服务可用时）。"""
import json
import os
import unittest
import urllib.request

CROSS_URL = os.environ.get("HCAPTCHA_CROSS_URL", "http://127.0.0.1:8767/detect")


def _probe():
    try:
        with urllib.request.urlopen(CROSS_URL.replace("/detect", "/"), timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


@unittest.skipUnless(_probe(), "cross 检测服务未运行")
class TestHcaptchaCross(unittest.TestCase):
    def test_detect_two_crosses(self):
        import cv2
        import numpy as np
        img = np.full((300, 400, 3), 255, dtype=np.uint8)
        for (cx, cy) in [(100, 100), (280, 200)]:
            cv2.line(img, (cx - 20, cy - 20), (cx + 20, cy + 20), (0, 0, 0), 6)
            cv2.line(img, (cx + 20, cy - 20), (cx - 20, cy + 20), (0, 0, 0), 6)
        path = "/tmp/cross_test_ut.png"
        cv2.imwrite(path, img)
        body = json.dumps({"image": path, "num": 2}).encode()
        req = urllib.request.Request(CROSS_URL, data=body, headers={"Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=20).read())
        self.assertTrue(r.get("ok"))
        pts = [(p["x"], p["y"]) for p in r.get("points", [])]
        self.assertGreaterEqual(len(pts), 2)
        # 第一个点应接近 (0.25, 0.333)
        self.assertLess(abs(pts[0][0] - 0.25), 0.05)
        self.assertLess(abs(pts[0][1] - 0.333), 0.05)


if __name__ == "__main__":
    unittest.main()
