# -*- coding: utf-8 -*-
"""hCaptcha 图片挑战 CLIP 求解器单元测试（本地服务可用时验证）。"""
import json
import os
import unittest
import urllib.request

CLIP_URL = os.environ.get("HCAPTCHA_CLIP_URL", "http://127.0.0.1:8766/solve")


def _probe():
    try:
        with urllib.request.urlopen(CLIP_URL.replace("/solve", "/"), timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


@unittest.skipUnless(_probe(), "CLIP 服务未运行")
class TestHcaptchaClip(unittest.TestCase):
    def test_solve_grid(self):
        import PIL.Image
        body = json.dumps({
            "task": "select all images containing a red car",
            "images": ["/tmp/cliptest/g%d.png" % i for i in range(9)],
        }).encode()
        req = urllib.request.Request(CLIP_URL, data=body, headers={"Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=60).read())
        self.assertTrue(r.get("ok"))
        self.assertTrue(r.get("indices"))


if __name__ == "__main__":
    unittest.main()
