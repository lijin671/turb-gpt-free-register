# -*- coding: utf-8 -*-
"""core.plus_zero.main() CLI 参数测试（--bind-proxy-retries 透传）。"""
import sys
import unittest
from unittest.mock import patch

import core.plus_zero as pz


class PlusZeroCliTests(unittest.TestCase):
    def setUp(self):
        from config import plus as _plus_cfg
        self._saved = getattr(_plus_cfg, "ZERO_PLUS_BIND_PROXY_RETRIES", 2)

    def tearDown(self):
        from config import plus as _plus_cfg
        _plus_cfg.ZERO_PLUS_BIND_PROXY_RETRIES = self._saved

    def _argv(self, extra):
        return ["zero_plus", "--token", "t", "--account-id", "a",
                "--card", "1", "--exp-month", "1", "--exp-year", "1",
                "--cvc", "1"] + extra

    @patch.object(pz, "run_zero_plus", return_value={"ok": True, "status": "success"})
    def test_bind_proxy_retries_zero(self, run):
        with patch.object(sys, "argv", self._argv(["--bind-proxy-retries", "0"])):
            rc = pz.main()
        self.assertEqual(rc, 0)
        from config import plus as _plus_cfg
        self.assertEqual(_plus_cfg.ZERO_PLUS_BIND_PROXY_RETRIES, 0)
        run.assert_called_once()

    @patch.object(pz, "run_zero_plus", return_value={"ok": True, "status": "success"})
    def test_default_keeps_config_value(self, run):
        from config import plus as _plus_cfg
        _plus_cfg.ZERO_PLUS_BIND_PROXY_RETRIES = 5
        with patch.object(sys, "argv", self._argv([])):
            rc = pz.main()
        self.assertEqual(rc, 0)
        self.assertEqual(_plus_cfg.ZERO_PLUS_BIND_PROXY_RETRIES, 5)

    @patch.object(pz, "run_zero_plus", return_value={"ok": False, "status": "error", "message": "x"})
    def test_failure_returns_1(self, run):
        with patch.object(sys, "argv", self._argv([])):
            rc = pz.main()
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
