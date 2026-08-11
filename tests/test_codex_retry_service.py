# -*- coding: utf-8 -*-
"""core.codex_retry_service Codex 补跑服务单测（mock run_codex_oauth/db，无网络）。

覆盖：reserve 幂等与脏占位清理、release、stop 请求、run_worker 各结果分支
（成功/账号已废→连坐隔离/异常/用户停止）。
"""
import tempfile
import unittest
from unittest.mock import patch

import core.codex_retry_service as crs


class ReserveReleaseTests(unittest.TestCase):
    def setUp(self):
        with crs._RETRYING_LOCK:
            crs._RETRYING.clear()
            crs._RUNNING_THREADS.clear()
            crs._RESERVED_AT.clear()
            crs._STOP_REQUESTED.clear()

    def test_reserve_rejects_empty(self):
        self.assertFalse(crs.reserve(""))
        self.assertFalse(crs.reserve("  "))

    def test_reserve_first_ok_duplicate_rejected(self):
        self.assertTrue(crs.reserve("a@b.com"))
        self.assertFalse(crs.reserve("A@B.COM"))  # 大小写归一，同 key 拒绝
        crs.release("a@b.com")
        self.assertTrue(crs.reserve("a@b.com"))

    def test_reserve_cleans_dirty_placeholder(self):
        # 线程已死 + DB 状态不是 retrying → 脏占位清理后可重新 reserve
        self.assertTrue(crs.reserve("a@b.com"))
        with crs._RETRYING_LOCK:
            crs._RUNNING_THREADS["a@b.com"] = 99999999  # 不存在的线程 id
            crs._RESERVED_AT["a@b.com"] = 0.0
        with patch.object(crs.db, "get_account_by_email", return_value={"codex_status": "failed"}):
            self.assertTrue(crs.reserve("a@b.com"))

    def test_is_retrying_and_stop_requested(self):
        crs.reserve("a@b.com")
        self.assertTrue(crs.is_retrying("a@b.com"))
        self.assertFalse(crs.is_stop_requested("a@b.com"))
        with crs._RETRYING_LOCK:
            crs._STOP_REQUESTED.add("a@b.com")
        self.assertTrue(crs.is_stop_requested("a@b.com"))
        with self.assertRaises(crs.CodexRetryStopped):
            crs.check_stop_requested("a@b.com")


class RequestStopTests(unittest.TestCase):
    def setUp(self):
        with crs._RETRYING_LOCK:
            crs._RETRYING.clear()
            crs._RUNNING_THREADS.clear()
            crs._RESERVED_AT.clear()
            crs._STOP_REQUESTED.clear()

    def test_request_stop_not_retrying_marks_stopped(self):
        with patch.object(crs.db, "update_account_codex_status") as upd:
            r = crs.request_stop("a@b.com")
        self.assertTrue(r["ok"])
        upd.assert_called_once()
        self.assertEqual(upd.call_args.args[2], "用户手动停止（未发现运行中的补跑）")

    def test_request_stop_empty_email(self):
        r = crs.request_stop("")
        self.assertFalse(r["ok"])


class RunWorkerTests(unittest.TestCase):
    def setUp(self):
        with crs._RETRYING_LOCK:
            crs._RETRYING.clear()
            crs._RUNNING_THREADS.clear()
            crs._RESERVED_AT.clear()
            crs._STOP_REQUESTED.clear()
        self.tmpdir = tempfile.mkdtemp(prefix="codex-retry-test-")

    def _patch_db_update(self):
        return patch.object(crs.db, "update_account_codex_status", return_value=True)

    def test_run_worker_success(self):
        with (
            self._patch_db_update() as upd,
            patch("core.codex_oauth.run_codex_oauth",
                  return_value={"status": "success", "ok": True, "file_path": "/tmp/x.json",
                                "callback_url": "https://x/cb"}),
            patch("config.reload_all", return_value=None),
        ):
            result = crs.run_worker("a@b.com", target_log_path=self.tmpdir + "/r.log",
                                    clear_log=False)
        self.assertTrue(result["ok"])
        upd.assert_called_once()
        self.assertEqual(upd.call_args.args[1], "success")

    def test_run_worker_deactivated_quarantines_ip(self):
        deactivated = {"status": "deactivated", "ok": False, "message": "账号已废（account_deactivated）"}
        with (
            self._patch_db_update(),
            patch("core.codex_oauth.run_codex_oauth", return_value=deactivated),
            patch("config.reload_all", return_value=None),
            patch.object(crs.db, "get_account_by_email",
                         return_value={"email": "a@b.com", "ip_key": "1.2.3.4",
                                       "proxy_used": "http://u:p@1.2.3.4:8080"}),
            patch("core.ip_discipline.mark_ip_co_risk") as mark,
        ):
            result = crs.run_worker("a@b.com", target_log_path=self.tmpdir + "/r.log",
                                    clear_log=False)
        self.assertEqual(result["status"], "deactivated")
        mark.assert_called_once()
        args = mark.call_args.args
        kwargs = mark.call_args.kwargs
        self.assertEqual(args[0], "1.2.3.4")
        self.assertIn("死亡", args[1])
        self.assertEqual(kwargs["emails"], ["a@b.com"])

    def test_run_worker_exception_marks_failed(self):
        with (
            self._patch_db_update() as upd,
            patch("core.codex_oauth.run_codex_oauth", side_effect=RuntimeError("boom")),
            patch("config.reload_all", return_value=None),
        ):
            result = crs.run_worker("a@b.com", target_log_path=self.tmpdir + "/r.log",
                                    clear_log=False)
        self.assertFalse(result["ok"])
        self.assertIn("boom", result["message"])
        self.assertEqual(upd.call_args.args[1], "failed")

    def test_run_worker_stopped(self):
        with (
            self._patch_db_update() as upd,
            patch("core.codex_oauth.run_codex_oauth",
                  side_effect=crs.CodexRetryStopped("用户手动停止 Codex 补跑")),
            patch("config.reload_all", return_value=None),
        ):
            result = crs.run_worker("a@b.com", target_log_path=self.tmpdir + "/r.log",
                                    clear_log=False)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(upd.call_args.args[1], "stopped")


if __name__ == "__main__":
    unittest.main()
