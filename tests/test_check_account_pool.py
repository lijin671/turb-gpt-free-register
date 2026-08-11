# -*- coding: utf-8 -*-
"""tools.check_account_pool 账号库存水位统计单测（mock db，无网络）。"""
import json
import unittest
from unittest.mock import patch

import tools.check_account_pool as cap


def _acc(email, token="tok", co_risk=False, codex_status="", expires_at="", plus_status="", proxy=""):
    return {
        "email": email,
        "access_token": token,
        "ip_co_risk": co_risk,
        "codex_status": codex_status,
        "expires_at": expires_at,
        "plus_status": plus_status,
        "proxy_used": proxy,
        "ip_key": proxy,
    }


class PoolStatsTests(unittest.TestCase):
    def test_empty(self):
        s = cap.pool_stats([])
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["potential_usable"], 0)
        self.assertEqual(s["potential_usable_rate"], 0.0)

    def test_mixed_counts(self):
        accounts = [
            _acc("a@x.com"),                                      # 可用
            _acc("b@x.com", co_risk=True),                        # 连坐风险
            _acc("c@x.com", codex_status="failed"),               # codex 失败
            _acc("d@x.com", codex_status="missing"),              # codex 缺失
            _acc("e@x.com", token=""),                            # 无 token
            _acc("f@x.com", plus_status="success"),               # 可用 + plus
        ]
        s = cap.pool_stats(accounts)
        self.assertEqual(s["total"], 6)
        self.assertEqual(s["has_token"], 5)
        self.assertEqual(s["co_risk"], 1)
        self.assertEqual(s["codex_failed"], 1)
        self.assertEqual(s["codex_missing"], 1)
        self.assertEqual(s["plus_success"], 1)
        self.assertEqual(s["potential_usable"], 2)  # a + f

    def test_expired_detection(self):
        future = "2099-01-01T00:00:00+00:00"
        past = "2020-01-01T00:00:00+00:00"
        s = cap.pool_stats([_acc("ok@x.com", expires_at=future),
                            _acc("old@x.com", expires_at=past),
                            _acc("none@x.com"),
                            _acc("bad@x.com", expires_at="not-a-date")])
        self.assertEqual(s["expired"], 1)


class LiveCheckTests(unittest.TestCase):
    def test_live_check_counts_statuses(self):
        accounts = [
            _acc("a@x.com", token="t1"),
            _acc("b@x.com", token="t2"),
            _acc("c@x.com", token="t3", co_risk=True),   # 不参与抽样
            _acc("d@x.com", token=""),
        ]
        with patch("tools.check_accounts_valid.check_token") as ct:
            ct.side_effect = [("ok", "a@x.com", "free", "user=1"),
                              ("revoked", "", "", "HTTP401 code=token_invalidated")]
            r = cap.live_check(accounts, limit=10)
        self.assertEqual(r["live_checked"], 2)
        self.assertEqual(r["live_ok"], 1)
        self.assertEqual(r["live_revoked"], 1)
        self.assertEqual(r["live_error"], 0)

    def test_live_check_limit(self):
        accounts = [_acc(f"a{i}@x.com", token="t") for i in range(5)]
        with patch("tools.check_accounts_valid.check_token",
                   return_value=("ok", "", "free", "")):
            r = cap.live_check(accounts, limit=2)
        self.assertEqual(r["live_checked"], 2)


class MainTests(unittest.TestCase):
    def test_shortfall_returns_nonzero(self):
        accounts = [_acc("a@x.com")]
        with patch("core.db.list_accounts", return_value=accounts), \
             patch.object(cap.sys, "argv", ["check_account_pool.py", "--min-usable", "3"]):
            rc = cap.main()
        self.assertEqual(rc, 1)

    def test_enough_returns_zero(self):
        accounts = [_acc("a@x.com"), _acc("b@x.com")]
        with patch("core.db.list_accounts", return_value=accounts), \
             patch.object(cap.sys, "argv", ["check_account_pool.py", "--min-usable", "2"]):
            rc = cap.main()
        self.assertEqual(rc, 0)

    def test_json_shape(self):
        with patch("core.db.list_accounts", return_value=[_acc("a@x.com")]), \
             patch.object(cap.sys, "argv", ["check_account_pool.py", "--json"]), \
             patch.object(cap, "print") as pr:
            rc = cap.main()
        self.assertEqual(rc, 0)
        payload = json.loads(pr.call_args_list[0].args[0])
        self.assertIn("potential_usable", payload)
        self.assertIn("shortfall", payload)
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()


class RevokedMarkingTests(unittest.TestCase):
    def test_live_check_marks_revoked_by_ip(self):
        accounts = [
            _acc("a@x.com", token="t1", proxy="socks5://1.1.1.1:1080"),
            _acc("b@x.com", token="t2", proxy="socks5://1.1.1.1:1080"),
            _acc("c@x.com", token="t3", proxy="socks5://2.2.2.2:1080"),
        ]
        with patch("tools.check_accounts_valid.check_token") as ct, \
             patch("core.ip_discipline.mark_ip_co_risk", return_value=1) as mark:
            ct.side_effect = [("ok", "a@x.com", "free", ""),
                              ("revoked", "", "", "HTTP401"),
                              ("revoked", "", "", "HTTP401")]
            r = cap.live_check(accounts, limit=10)
        self.assertEqual(r["live_revoked"], 2)
        # 同 IP 的 b 聚合一次；c 一次 → 共 2 次
        self.assertEqual(mark.call_count, 2)
        b_call = mark.call_args_list[0]
        self.assertEqual(b_call.args[0], "socks5://1.1.1.1:1080")
        self.assertIn("b@x.com", b_call.kwargs["emails"])
        self.assertEqual(r["live_marked_co_risk"], 2)

    def test_live_check_no_mark_flag(self):
        accounts = [_acc("a@x.com", token="t1", proxy="socks5://1.1.1.1:1080")]
        with patch("tools.check_accounts_valid.check_token",
                   return_value=("revoked", "", "", "HTTP401")), \
             patch("core.ip_discipline.mark_ip_co_risk") as mark:
            r = cap.live_check(accounts, limit=10, mark_revoked=False)
        self.assertEqual(r["live_revoked"], 1)
        mark.assert_not_called()

    def test_main_no_mark_flag_passed_through(self):
        with patch("core.db.list_accounts",
                   return_value=[_acc("a@x.com", token="t1", proxy="socks5://1.1.1.1:1080")]), \
             patch("tools.check_accounts_valid.check_token",
                   return_value=("revoked", "", "", "HTTP401")), \
             patch("core.ip_discipline.mark_ip_co_risk") as mark, \
             patch.object(cap.sys, "argv",
                          ["check_account_pool.py", "--live-check-limit", "5", "--no-mark"]):
            rc = cap.main()
        self.assertEqual(rc, 0)
        mark.assert_not_called()


class ReviveRevokedTests(unittest.TestCase):
    def _revoked_details(self, emails):
        return [{"email": e, "status": "revoked", "note": "HTTP401"} for e in emails]

    def test_revive_success_no_mark(self):
        accounts = [_acc("a@x.com", token="t1", proxy="socks5://1.1.1.1:1080")]
        with patch("core.token_revival.revive_account",
                   return_value={"ok": True, "email": "a@x.com", "message": "成功"}), \
             patch("core.ip_discipline.mark_ip_co_risk") as mark:
            r = cap.revive_revoked_accounts(accounts, self._revoked_details(["a@x.com"]))
        self.assertEqual(r["revived_ok"], 1)
        self.assertEqual(r["revived_failed"], 0)
        mark.assert_not_called()

    def test_revive_failure_marks_co_risk(self):
        accounts = [_acc("a@x.com", token="t1", proxy="socks5://1.1.1.1:1080")]
        with patch("core.token_revival.revive_account",
                   return_value={"ok": False, "email": "a@x.com", "message": "会话已死"}), \
             patch("core.ip_discipline.mark_ip_co_risk", return_value=1) as mark:
            r = cap.revive_revoked_accounts(accounts, self._revoked_details(["a@x.com"]))
        self.assertEqual(r["revived_failed"], 1)
        self.assertEqual(r["revived_marked"], 1)
        mark.assert_called_once()
        self.assertEqual(mark.call_args.args[0], "socks5://1.1.1.1:1080")
        self.assertIn("a@x.com", mark.call_args.kwargs["emails"])

    def test_ignores_non_revoked_details(self):
        accounts = [_acc("a@x.com", token="t1"), _acc("b@x.com", token="t2")]
        details = [{"email": "a@x.com", "status": "ok", "note": ""},
                   {"email": "b@x.com", "status": "error", "note": "Timeout"}]
        with patch("core.token_revival.revive_account") as ra:
            r = cap.revive_revoked_accounts(accounts, details)
        self.assertEqual(r["revived_ok"], 0)
        ra.assert_not_called()

    def test_main_revive_success_updates_stats(self):
        accounts = [_acc("a@x.com", token="t1", proxy="socks5://1.1.1.1:1080")]
        fresh = [_acc("a@x.com", token="fresh", proxy="socks5://1.1.1.1:1080")]
        with patch("core.db.list_accounts", side_effect=[accounts, fresh]), \
             patch("tools.check_accounts_valid.check_token",
                   return_value=("revoked", "", "", "HTTP401")), \
             patch("core.token_revival.revive_account",
                   return_value={"ok": True, "email": "a@x.com", "message": "成功"}), \
             patch.object(cap.sys, "argv",
                          ["check_account_pool.py", "--live-check-limit", "5", "--revive-revoked", "--json"]), \
             patch.object(cap, "print") as pr:
            rc = cap.main()
        self.assertEqual(rc, 0)
        payload = json.loads(pr.call_args_list[0].args[0])
        self.assertEqual(payload["revived_ok"], 1)

    def test_main_revive_requires_live_check_limit(self):
        with patch.object(cap.sys, "argv",
                          ["check_account_pool.py", "--revive-revoked"]):
            rc = cap.main()
        self.assertEqual(rc, 2)
