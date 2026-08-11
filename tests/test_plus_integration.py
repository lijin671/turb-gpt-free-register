# -*- coding: utf-8 -*-
"""core/plus_integration 注册后零元 Plus 触发包装测试（mock run_zero_plus/fetch_session）。"""
import unittest
from unittest.mock import patch

import config.plus as plus_cfg
import core.plus_integration as pi


class PlusIntegrationTests(unittest.TestCase):
    def _call(self, **kw):
        base = dict(email="a@b.com", access_token="tok", account_id="acc-1",
                    card_number="4242424242424242")
        base.update(kw)
        return pi.try_zero_plus_after_registration(**base)

    def test_disabled_skips(self):
        with patch.object(plus_cfg, "ENABLE_ZERO_PLUS", False), \
             patch.object(pi, "run_zero_plus") as run:
            r = self._call()
        self.assertEqual(r["status"], "skipped")
        run.assert_not_called()

    def test_no_card_skips(self):
        with patch.object(plus_cfg, "ENABLE_ZERO_PLUS", True), \
             patch.object(pi, "run_zero_plus") as run:
            r = self._call(card_number="")
        self.assertEqual(r["status"], "skipped")
        self.assertIn("卡号", r["message"])
        run.assert_not_called()

    def test_no_token_skips(self):
        with patch.object(plus_cfg, "ENABLE_ZERO_PLUS", True), \
             patch.object(pi, "run_zero_plus") as run:
            r = self._call(access_token="")
        self.assertEqual(r["status"], "skipped")
        self.assertIn("access_token", r["message"])
        run.assert_not_called()

    def test_missing_account_id_fetches_session(self):
        with patch.object(plus_cfg, "ENABLE_ZERO_PLUS", True), \
             patch.object(pi, "fetch_session",
                          return_value={"account": {"id": "acc-9"}}) as fs, \
             patch.object(pi, "run_zero_plus",
                          return_value={"ok": True, "status": "success"}) as run:
            r = self._call(account_id="")
        self.assertTrue(r["ok"])
        fs.assert_called_once()
        self.assertEqual(run.call_args.kwargs["account_id"], "acc-9")

    def test_session_fetch_failure_failed(self):
        with patch.object(plus_cfg, "ENABLE_ZERO_PLUS", True), \
             patch.object(pi, "fetch_session", side_effect=RuntimeError("revoked")), \
             patch.object(pi, "run_zero_plus") as run:
            r = self._call(account_id="")
        self.assertEqual(r["status"], "failed")
        self.assertIn("session 提取失败", r["message"])
        run.assert_not_called()

    def test_session_without_account_id_failed(self):
        with patch.object(plus_cfg, "ENABLE_ZERO_PLUS", True), \
             patch.object(pi, "fetch_session", return_value={"account": {}}), \
             patch.object(pi, "run_zero_plus") as run:
            r = self._call(account_id="")
        self.assertEqual(r["status"], "failed")
        self.assertIn("account_id", r["message"])
        run.assert_not_called()

    def test_retry_on_bind_failed(self):
        results = [{"ok": False, "status": "bind_failed"}, {"ok": True, "status": "success"}]

        def _run(**kw):
            return results.pop(0)

        with patch.object(plus_cfg, "ENABLE_ZERO_PLUS", True), \
             patch.object(pi, "run_zero_plus", side_effect=_run) as run, \
             patch.object(pi.time, "sleep"):
            r = self._call(retry_on_fail=True, max_retries=2)
        self.assertTrue(r["ok"])
        self.assertEqual(run.call_count, 2)

    def test_no_retry_when_success(self):
        with patch.object(plus_cfg, "ENABLE_ZERO_PLUS", True), \
             patch.object(pi, "run_zero_plus",
                          return_value={"ok": True, "status": "success"}) as run, \
             patch.object(pi.time, "sleep"):
            r = self._call(retry_on_fail=True, max_retries=3)
        self.assertTrue(r["ok"])
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
