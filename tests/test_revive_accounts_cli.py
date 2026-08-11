# -*- coding: utf-8 -*-
"""tools.revive_accounts CLI 单测（mock db/check_token/revive_accounts，无网络）。"""
import json
import unittest
from unittest.mock import patch

import tools.revive_accounts as ra


def _acc(email, token="tok", proxy="socks5://1.1.1.1:1080", device_id="d1"):
    return {"email": email, "access_token": token, "proxy_used": proxy, "device_id": device_id}


class PickTargetsTests(unittest.TestCase):
    def test_explicit_emails_win(self):
        with patch("core.db.list_accounts") as la:
            targets = ra._pick_targets(["a@x.com"], 1, False)
        self.assertEqual(targets, ["a@x.com"])
        la.assert_not_called()

    def test_limit_takes_newest_with_token_and_proxy(self):
        accounts = [
            _acc("skip-no-token@x.com", token=""),
            _acc("skip-no-proxy@x.com", proxy=""),
            _acc("b@x.com"),  # db.list_accounts 按 id 降序（最新在前）
            _acc("a@x.com"),
        ]
        with patch("core.db.list_accounts", return_value=accounts):
            targets = ra._pick_targets([], 2, False)
        self.assertEqual(targets, ["b@x.com", "a@x.com"])

    def test_live_check_first_filters_revoked_only(self):
        accounts = [_acc("ok@x.com"), _acc("dead@x.com"), _acc("err@x.com")]
        def _fake_check(token, proxy, timeout=25, device_id=""):
            return {"ok": ("ok", "", "free", ""),
                    "dead": ("revoked", "", "", "HTTP401"),
                    "err": ("error", "", "", "Timeout")}[
                {"ok@x.com": "ok", "dead@x.com": "dead", "err@x.com": "err"}[
                    "ok@x.com" if token == "tok" and proxy == "socks5://1.1.1.1:1080" else "err"]]
        with patch("core.db.list_accounts", return_value=accounts), \
             patch("tools.check_accounts_valid.check_token") as ct:
            ct.side_effect = [("ok", "", "free", ""),
                              ("revoked", "", "", "HTTP401"),
                              ("error", "", "", "Timeout")]
            targets = ra._pick_targets([], 3, True)
        self.assertEqual(targets, ["dead@x.com"])


class MainTests(unittest.TestCase):
    def test_email_mode_calls_revive_and_exit_zero(self):
        with patch.object(ra, "_pick_targets", return_value=["a@x.com"]), \
             patch("core.token_revival.revive_accounts",
                   return_value=[{"ok": True, "email": "a@x.com", "message": "成功"}]), \
             patch.object(ra.sys, "argv", ["revive_accounts.py", "--email", "a@x.com"]):
            rc = ra.main()
        self.assertEqual(rc, 0)

    def test_partial_failure_exit_nonzero(self):
        with patch.object(ra, "_pick_targets", return_value=["a@x.com", "b@x.com"]), \
             patch("core.token_revival.revive_accounts",
                   return_value=[{"ok": True, "email": "a@x.com", "message": "成功"},
                                 {"ok": False, "email": "b@x.com", "message": "失败"}]), \
             patch.object(ra.sys, "argv", ["revive_accounts.py", "--limit", "2"]):
            rc = ra.main()
        self.assertEqual(rc, 1)

    def test_otp_code_requires_single_email(self):
        with patch.object(ra.sys, "argv",
                          ["revive_accounts.py", "--email", "a@x.com", "--email", "b@x.com",
                           "--otp-code", "123456"]):
            rc = ra.main()
        self.assertEqual(rc, 2)

    def test_json_output_shape(self):
        with patch.object(ra, "_pick_targets", return_value=["a@x.com"]), \
             patch("core.token_revival.revive_accounts",
                   return_value=[{"ok": True, "email": "a@x.com", "message": "成功"}]), \
             patch.object(ra.sys, "argv", ["revive_accounts.py", "--email", "a@x.com", "--json"]), \
             patch.object(ra, "print") as pr:
            rc = ra.main()
        self.assertEqual(rc, 0)
        payload = json.loads(pr.call_args_list[0].args[0])
        self.assertEqual(payload["ok"], 1)
        self.assertEqual(payload["total"], 1)


if __name__ == "__main__":
    unittest.main()
