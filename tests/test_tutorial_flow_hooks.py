# -*- coding: utf-8 -*-
"""教程 8.8 流程改造测试：日区代理选择 / 同步提链 / 注册后提链钩子 / iCloud 邮箱池。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class PickRegionProxyTest(unittest.TestCase):
    def test_empty_region_falls_back_to_disciplined(self):
        from config.proxy import pick_region_proxy
        with patch("config.proxy.pick_disciplined_proxy", return_value="http://p:1") as fb:
            out = pick_region_proxy("", owner="u")
        self.assertEqual(out, "http://p:1")
        fb.assert_called_once_with(owner="u")

    def test_probes_until_region_match_then_claims(self):
        from config.proxy import pick_region_proxy
        proxy = "http://Pokemon.cli-session-aaaaaaaa:tok@127.0.0.1:2260"
        with patch("config.proxy.PROXY_POOL", [proxy]), \
             patch("config.proxy.IP_DISCIPLINE_ENABLED", True), \
             patch("config.proxy._ensure_fresh_session", side_effect=lambda p: p), \
             patch("config.proxy.probe_exit_region",
                   side_effect=[("SG", "1.1.1.1"), ("JP", "2.2.2.2")]), \
             patch("core.ip_discipline.is_ip_free", return_value=(True, "")), \
             patch("core.ip_discipline.claim_proxy", return_value=True) as claim:
            out = pick_region_proxy("jp", owner="u", max_attempts=3)
        self.assertEqual(out, proxy)
        self.assertEqual(claim.call_count, 1)
        self.assertEqual(claim.call_args.args[0], proxy)

    def test_no_match_returns_none_without_claim(self):
        from config.proxy import pick_region_proxy
        with patch("config.proxy.PROXY_POOL", ["http://p:1"]), \
             patch("config.proxy.IP_DISCIPLINE_ENABLED", True), \
             patch("config.proxy._ensure_fresh_session", side_effect=lambda p: p), \
             patch("config.proxy.probe_exit_region", return_value=("SG", "1.1.1.1")), \
             patch("core.ip_discipline.is_ip_free", return_value=(True, "")), \
             patch("core.ip_discipline.claim_proxy") as claim:
            out = pick_region_proxy("jp", max_attempts=3)
        self.assertIsNone(out)
        claim.assert_not_called()


class ExtractLinkNowTest(unittest.TestCase):
    def test_runs_sync_without_slot_release_and_claims_first(self):
        from core import extract_link_service as svc
        expected = {"ok": True, "status": "success", "result": {"long_url": "x"}}
        with patch("core.extract_link_service._link_type", return_value="pix"), \
             patch("core.extract_link_service._cdk", return_value="CDK"), \
             patch("core.extract_link_service.db.claim_account_extract", return_value=True) as claim, \
             patch("core.extract_link_service._run_extract", return_value=expected) as run:
            out = svc.extract_link_now(account_id=9, email="u@x.com",
                                       access_token="AT", trigger="post_register")
        self.assertEqual(out, expected)
        claim.assert_called_once_with(9, trigger="post_register", link_type="pix")
        self.assertEqual(run.call_args.kwargs["_release_slot"], False)
        self.assertEqual(run.call_args.kwargs["account_id"], 9)

    def test_account_id_none_skips_db_claim(self):
        from core import extract_link_service as svc
        with patch("core.extract_link_service._link_type", return_value="pix"), \
             patch("core.extract_link_service._cdk", return_value="CDK"), \
             patch("core.extract_link_service.db.claim_account_extract") as claim, \
             patch("core.extract_link_service._run_extract", return_value={"ok": False, "error": "e"}):
            out = svc.extract_link_now(account_id=None, email="u@x.com", access_token="AT")
        self.assertFalse(out["ok"])
        claim.assert_not_called()


class MaybeExtractLinkHookTest(unittest.TestCase):
    def _run(self, enabled=True, token="AT", out=None, error=None):
        from main import _maybe_extract_link
        result = {"success": True, "email": "u@example.com", "access_token": token}
        with patch("config.plus.ENABLE_EXTRACT_AFTER_REGISTER", enabled), \
             patch("core.db.get_account_by_email", return_value={"id": 9, "email": "u@example.com"}), \
             patch("core.extract_link_service.extract_link_now",
                   side_effect=error if error else (lambda **kw: out)):
            return _maybe_extract_link("u@example.com", result)

    def test_disabled_marks_skipped(self):
        out = self._run(enabled=False)
        self.assertEqual(out["extract_link_status"], "skipped_disabled")

    def test_no_token_skips(self):
        out = self._run(token="")
        self.assertEqual(out["extract_link_status"], "skipped_no_token")

    def test_success_copies_result(self):
        out = self._run(out={"ok": True, "status": "success", "link_type": "pix",
                             "result": {"long_url": "https://pay/x"}})
        self.assertEqual(out["extract_link_status"], "ok")
        self.assertEqual(out["extract_link_result"]["long_url"], "https://pay/x")

    def test_failure_does_not_block(self):
        out = self._run(out={"ok": False, "error": "CDK 余额不足"})
        self.assertEqual(out["extract_link_status"], "failed")
        self.assertEqual(out["extract_link_error"], "CDK 余额不足")
        self.assertTrue(out["success"])


class IcloudPoolTest(unittest.TestCase):
    def test_pick_marks_used_and_available_release_returns(self):
        import core.icloud_client as icloud
        with tempfile.TemporaryDirectory() as td:
            pool = Path(td) / "icloud.txt"
            pool.write_text("a@icloud.com====pwd1\nb@icloud.com====pwd2\n", encoding="utf-8")
            with patch("config.email.ICLOUD_ACCOUNTS_FILE", str(pool)):
                icloud._CONTEXT_CACHE.clear() if hasattr(icloud, "_CONTEXT_CACHE") else None
                a1 = icloud.pick_account()
                a2 = icloud.pick_account()
                self.assertNotEqual(a1.email, a2.email)
                # 池子用完后再领应报错
                with self.assertRaises(icloud.IcloudMailError):
                    icloud.pick_account()
                # available 释放后可以再领
                icloud.release_account(a1.email, status="available")
                a3 = icloud.pick_account()
                self.assertEqual(a3.email, a1.email)


if __name__ == "__main__":
    unittest.main()
