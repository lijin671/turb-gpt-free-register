# -*- coding: utf-8 -*-
"""hCaptcha 图片挑战 frame 定位器单元测试。

复现 2026-08-06 实跑 bug: Stripe 集成里 b.stripecdn.com/.../hcaptcha.html
引导页("Close One more step before you're done Select the checkbox below")
被误判为图片挑战 frame，导致任务文本错误、CLIP 选错格子、120s 超时。
"""
import unittest

from core.plus_browser_bind import (
    _is_hcaptcha_interstitial,
    _pick_hcaptcha_challenge_frame,
)


class _Frame:
    def __init__(self, url, text):
        self.url = url
        self._text = text
        self.reads = 0

    def evaluate(self, js):
        self.reads += 1
        if "innerText" in js:
            return self._text
        return ""

    def set_text(self, text):
        self._text = text


REAL_CHAL_URL = ("https://newassets.hcaptcha.com/captcha/v1/9175be290bd54c5fd0571736bb8a0df6ba243a74"
                 "/static/hcaptcha.html#frame=challenge&parent=https%3a%2f%2fchatgpt.com")
STRIPE_WRAP_URL = ("https://b.stripecdn.com/stripethirdparty-srv/assets/v33.6/hcaptcha.html"
                   "?id=5e49df6c-e7fb-4d29-ab6c-4001e66a1c5a&origin=h")
CHECKBOX_URL = ("https://newassets.hcaptcha.com/captcha/v1/9175be290bd54c5fd0571736bb8a0df6ba243a74"
                "/static/hcaptcha.html#frame=checkbox&parent=https%3a%2f%2fchatgpt.com")

INTERSTITIAL_FULL = "Close One more step before you're done Select the checkbox below."
REAL_TASK = "Click on all items you might find near a sink. Please try again. \u26a0\ufe0f Verify EN"


class TestIsInterstitial(unittest.TestCase):
    def test_full_interstitial(self):
        self.assertTrue(_is_hcaptcha_interstitial(INTERSTITIAL_FULL.lower()))

    def test_partial_interstitial(self):
        self.assertTrue(_is_hcaptcha_interstitial(
            "close one more step before you're done"))

    def test_real_task_not_interstitial(self):
        self.assertFalse(_is_hcaptcha_interstitial(REAL_TASK.lower()))

    def test_empty(self):
        self.assertFalse(_is_hcaptcha_interstitial(""))


class TestPickChallengeFrame(unittest.TestCase):
    def test_prefers_real_challenge_frame_over_stripe_wrapper(self):
        """真实挑战 frame 存在时，即使 Stripe 引导页在前也要选中它。"""
        stripe = _Frame(STRIPE_WRAP_URL, INTERSTITIAL_FULL)
        chal = _Frame(REAL_CHAL_URL, REAL_TASK)
        checkbox = _Frame(CHECKBOX_URL, "")
        picked, tl = _pick_hcaptcha_challenge_frame([stripe, chal, checkbox])
        self.assertIs(picked, chal)
        self.assertIn("click on all items you might find near a sink", tl)

    def test_async_interstitial_loaded_after_scan_is_rejected(self):
        """扫描时引导页只读到 "Close One more step" 前缀 → 直接识别为引导页跳过。"""
        stripe = _Frame(STRIPE_WRAP_URL, "Close One more step before you're done")
        waits = []

        def wait():
            waits.append(1)
            stripe.set_text(INTERSTITIAL_FULL)

        picked, tl = _pick_hcaptcha_challenge_frame([stripe], wait=wait)
        self.assertIsNone(picked)
        self.assertEqual(tl, "")
        self.assertFalse(waits, "含 close 的引导页前缀应直接排除，无需等待复核")

    def test_async_interstitial_without_close_is_rejected(self):
        """引导页无 "close" 前缀、也无任务关键词时同样被跳过。"""
        stripe = _Frame(STRIPE_WRAP_URL, "One more step before you're done")
        picked, tl = _pick_hcaptcha_challenge_frame([stripe], wait=lambda: None)
        self.assertIsNone(picked)

    def test_no_challenge_returns_none(self):
        picked, tl = _pick_hcaptcha_challenge_frame(
            [_Frame(CHECKBOX_URL, ""), _Frame(STRIPE_WRAP_URL, INTERSTITIAL_FULL)])
        self.assertIsNone(picked)
        self.assertEqual(tl, "")

    def test_non_captcha_frames_skipped(self):
        main = _Frame("https://chatgpt.com/", "click on all images containing a bus")
        picked, tl = _pick_hcaptcha_challenge_frame([main])
        self.assertIsNone(picked)

    def test_stripe_wrapper_with_full_text_never_selected(self):
        picked, tl = _pick_hcaptcha_challenge_frame(
            [_Frame(STRIPE_WRAP_URL, INTERSTITIAL_FULL)])
        self.assertIsNone(picked)


if __name__ == "__main__":
    unittest.main()
