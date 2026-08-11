# -*- coding: utf-8 -*-
"""WebUI 重启后注册任务恢复（recover_interrupted_jobs）测试。

线程池在进程内，重启后 pending/running/stopping 任务没有线程执行，
必须恢复为 failed 才能让 UI 重试；终态任务不受影响。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


class RecoverInterruptedJobsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patchers = [
            patch.object(db, "_JOBS_JSON", root / "jobs.json"),
            patch.object(db, "_LOG_DIR", root / "logs"),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()

    def _make_job(self, status: str) -> dict:
        job = db.create_job(email_source="outlook")
        if status != "pending":
            db.update_job(int(job["id"]), status=status)
        return job

    def test_recover_marks_pending_and_running_failed(self):
        pending = self._make_job("pending")
        running = self._make_job("running")
        done = self._make_job("success")

        recovered = db.recover_interrupted_jobs()

        self.assertEqual(recovered, 2)
        self.assertEqual(db.get_job(int(pending["id"]))["status"], "failed")
        self.assertIn("重启", db.get_job(int(pending["id"]))["error_message"] or "")
        self.assertIsNotNone(db.get_job(int(pending["id"]))["completed_at"])
        self.assertEqual(db.get_job(int(running["id"]))["status"], "failed")
        self.assertEqual(db.get_job(int(done["id"]))["status"], "success")
        self.assertIsNone(db.get_job(int(done["id"]))["error_message"])

    def test_recover_also_handles_stopping(self):
        job = self._make_job("stopping")
        db.recover_interrupted_jobs()
        self.assertEqual(db.get_job(int(job["id"]))["status"], "failed")

    def test_create_app_recovers_jobs_on_startup(self):
        pending = self._make_job("running")
        done = self._make_job("success")
        create_app(auth_code="test-auth")
        self.assertEqual(db.get_job(int(pending["id"]))["status"], "failed")
        self.assertEqual(db.get_job(int(done["id"]))["status"], "success")

    def test_no_active_jobs_returns_zero(self):
        done = self._make_job("success")
        self.assertEqual(db.recover_interrupted_jobs(), 0)
        self.assertEqual(db.get_job(int(done["id"]))["status"], "success")

    def test_recovered_job_is_retryable(self):
        job = self._make_job("running")
        db.recover_interrupted_jobs()
        from core.registration_service import get_retry_info
        info = get_retry_info(db.get_job(int(job["id"])))
        self.assertTrue(info["retryable"])
        self.assertEqual(info["retry_action"], "registration")


if __name__ == "__main__":
    unittest.main()
