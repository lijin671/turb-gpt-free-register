# -*- coding: utf-8 -*-
"""1ip1号 IP 关联控制测试（core.ip_discipline + config.proxy + db 标记）。"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from config.proxy import IP_SUCCESS_COOLDOWN_SECONDS, proxy_ip_key
from core import ip_discipline as ipd
from core.ip_discipline import (
    accounts_sharing_ip,
    claim_proxy,
    clear_state,
    is_ip_free,
    mark_ip_co_risk,
    record_ip_use,
    release_proxy,
    status_summary,
)


class IpDisciplineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_file = os.path.join(self._tmp.name, "ip_discipline.json")
        patcher = patch.dict(os.environ, {"IP_DISCIPLINE_FILE": self._state_file})
        patcher.start()
        self.addCleanup(patcher.stop)
        clear_state()

    def tearDown(self):
        clear_state()

    # ---------- proxy_ip_key ----------
    def test_proxy_ip_key_static_normalizes(self):
        self.assertEqual(
            proxy_ip_key("socks5://user:pass@1.2.3.4:1080"),
            "socks5://1.2.3.4:1080",
        )
        self.assertEqual(
            proxy_ip_key("http://a:b@1.2.3.4:3128"),
            "http://1.2.3.4:3128",
        )
        self.assertEqual(proxy_ip_key(""), "")

    def test_proxy_ip_key_resin_keeps_sid_masks_password(self):
        key = proxy_ip_key("socks5://Pokemon.cli-session-abc12345:token@127.0.0.1:2260")
        self.assertIn("abc12345", key)
        self.assertNotIn("token", key)
        key2 = proxy_ip_key("socks5://Pokemon.cli-session-xyz67890:token@127.0.0.1:2260")
        self.assertNotEqual(key, key2)

    # ---------- claim / release / cooldown ----------
    def test_claim_then_not_free_then_release(self):
        proxy = "socks5://1.2.3.4:1080"
        self.assertTrue(is_ip_free(proxy)[0])
        self.assertTrue(claim_proxy(proxy, owner="job-1"))
        free, reason = is_ip_free(proxy)
        self.assertFalse(free)
        self.assertEqual(reason, "leased")
        # 第二个 owner 拿不到
        self.assertFalse(claim_proxy(proxy, owner="job-2"))
        release_proxy(proxy, owner="job-1")
        self.assertTrue(is_ip_free(proxy)[0])

    def test_cooldown_after_use(self):
        proxy = "socks5://5.6.7.8:1080"
        record_ip_use(proxy, email="a@x.com")
        free, reason = is_ip_free(proxy)
        self.assertFalse(free)
        self.assertEqual(reason, "max_accounts")
        # 时间推进超过冷却后恢复可用
        future = datetime.now() + timedelta(seconds=3600)
        with patch.object(ipd, "_now", return_value=future):
            free, reason = is_ip_free(proxy)
            self.assertTrue(free, reason)

    def test_max_accounts_per_ip_blocks_second_static(self):
        p1 = "socks5://9.9.9.9:1080"
        record_ip_use(p1, email="one@x.com")
        free, reason = is_ip_free(p1, max_per_ip=1)
        self.assertFalse(free)
        self.assertEqual(reason, "max_accounts")

    def test_resin_sids_are_independent(self):
        p1 = "socks5://Pokemon.cli-session-aaaa1111:tok@127.0.0.1:2260"
        p2 = "socks5://Pokemon.cli-session-bbbb2222:tok@127.0.0.1:2260"
        self.assertTrue(claim_proxy(p1, owner="job-1"))
        # 不同 sid = 不同 IP，不受影响
        self.assertTrue(is_ip_free(p2)[0])
        self.assertTrue(claim_proxy(p2, owner="job-2"))

    # ---------- co-risk 标记 ----------
    def test_mark_ip_co_risk_updates_db_accounts(self):
        import core.db as db
        with tempfile.TemporaryDirectory() as d:
            from pathlib import Path as _P
            for name in ("_ACCOUNTS_JSON", "_ACCOUNTS_TXT", "_TOKENS_TXT",
                         "_OUTLOOK_JSON", "_OUTLOOK_TXT", "_LEGACY_ACCOUNTS_JSON",
                         "_LEGACY_OUTLOOK_JSON"):
                setattr(db, name, _P(d) / (name.lstrip("_") + ".json"))
            with patch.object(db, "_render_static_viewer", return_value=None):
                db.insert_account(
                    email="ip1@x.com", access_token="tok1",
                    proxy_used="socks5://3.3.3.3:1080",
                    ip_key="socks5://3.3.3.3:1080",
                    exit_ip="3.3.3.3",
                )
                db.insert_account(
                    email="ip2@x.com", access_token="tok2",
                    proxy_used="socks5://8.8.8.8:1080",
                    ip_key="socks5://8.8.8.8:1080",
                    exit_ip="8.8.8.8",
                )
            # 标记 3.3.3.3 出口连坐风险
            with patch("core.ip_discipline.accounts_sharing_ip") as mock_share:
                mock_share.return_value = [{"email": "ip1@x.com"}]
                marked = mark_ip_co_risk("socks5://3.3.3.3:1080", "account_revoked")
                self.assertEqual(marked, 1)
            row1 = db.get_account_by_email("ip1@x.com")
            row2 = db.get_account_by_email("ip2@x.com")
            self.assertTrue(row1.get("ip_co_risk"))
            self.assertEqual(row1.get("ip_co_risk_reason"), "account_revoked")
            self.assertFalse(row2.get("ip_co_risk"))

    def test_acquire_proxy_waits_until_available(self):
        from core.ip_discipline import acquire_proxy
        calls = {"n": 0}

        def fake_pick(owner=""):
            calls["n"] += 1
            if calls["n"] < 2:
                return None
            return "socks5://4.4.4.4:1080"

        with patch("config.proxy.pick_disciplined_proxy", side_effect=fake_pick), \
             patch("core.ip_discipline.time.sleep", return_value=None):
            proxy = acquire_proxy(owner="job-x", timeout=30, poll_interval=0.01)
        self.assertEqual(proxy, "socks5://4.4.4.4:1080")
        self.assertEqual(calls["n"], 2)

    def test_acquire_proxy_timeout_returns_none(self):
        from core.ip_discipline import acquire_proxy
        with patch("config.proxy.pick_disciplined_proxy", return_value=None), \
             patch("core.ip_discipline.time.sleep", return_value=None):
            proxy = acquire_proxy(owner="job-x", timeout=0.01, poll_interval=0.005)
        self.assertIsNone(proxy)

    def test_status_summary_shape(self):
        s = status_summary()
        for key in ("active_leases", "ips_in_cooldown", "co_risk_ips", "cooldown_seconds", "max_accounts_per_ip"):
            self.assertIn(key, s)


if __name__ == "__main__":
    unittest.main()


class SuccessCooldownTests(unittest.TestCase):
    """成功注册的 IP 冷却更长（1ip1号：成功号 token 常 ~30min 内被吊销，同 IP 短时间再注册连坐）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_file = os.path.join(self._tmp.name, "ip_discipline.json")
        patcher = patch.dict(os.environ, {"IP_DISCIPLINE_FILE": self._state_file})
        patcher.start()
        self.addCleanup(patcher.stop)
        clear_state()

    def tearDown(self):
        clear_state()

    def test_success_outcome_blocks_beyond_base_cooldown(self):
        proxy = "socks5://7.7.7.7:1080"
        record_ip_use(proxy, email="ok@x.com", outcome="success")
        # 基础冷却(1800s)已过，但成功冷却(86400s)没过 → 仍不可复用
        future = datetime.now() + timedelta(seconds=3600)
        with patch.object(ipd, "_now", return_value=future):
            free, reason = is_ip_free(proxy)
            self.assertFalse(free, reason)
        # 超过成功冷却后恢复可用
        future2 = datetime.now() + timedelta(seconds=IP_SUCCESS_COOLDOWN_SECONDS + 60)
        with patch.object(ipd, "_now", return_value=future2):
            self.assertTrue(is_ip_free(proxy)[0])

    def test_failure_outcome_uses_base_cooldown(self):
        proxy = "socks5://7.7.7.8:1080"
        record_ip_use(proxy, email="fail@x.com", outcome="failure")
        future = datetime.now() + timedelta(seconds=3600)
        with patch.object(ipd, "_now", return_value=future):
            self.assertTrue(is_ip_free(proxy)[0])

    def test_record_default_outcome_is_failure(self):
        proxy = "socks5://7.7.7.9:1080"
        record_ip_use(proxy, email="d@x.com")  # 不传 outcome → failure
        future = datetime.now() + timedelta(seconds=3600)
        with patch.object(ipd, "_now", return_value=future):
            self.assertTrue(is_ip_free(proxy)[0])
        state = ipd._load()
        self.assertEqual(state["usage"][proxy_ip_key(proxy)][0].get("outcome"), "failure")

    def test_status_summary_includes_success_cooldown(self):
        s = status_summary()
        self.assertIn("success_cooldown_seconds", s)
        self.assertEqual(s["success_cooldown_seconds"], IP_SUCCESS_COOLDOWN_SECONDS)
