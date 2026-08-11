# -*- coding: utf-8 -*-
"""core.plus_bind_service 后台任务注册表测试（去重/执行/异常/清理）。"""
import threading
import time
import unittest
from unittest.mock import patch

from core import plus_bind_service


def _wait_task(task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = plus_bind_service.get_task(task_id)
        if task and task["status"] in ("done", "error"):
            return task
        time.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内结束: {task}")


class PlusBindServiceTests(unittest.TestCase):
    def setUp(self):
        with plus_bind_service._lock:
            plus_bind_service._TASKS.clear()

    def tearDown(self):
        with plus_bind_service._lock:
            plus_bind_service._TASKS.clear()

    @patch("core.plus_zero.run_zero_plus",
           return_value={"ok": True, "status": "success", "message": "🎉 成功"})
    def test_enqueue_executes_in_background(self, run):
        queued = plus_bind_service.enqueue_bind(
            1, access_token="tok-1", email="a@b.com",
            card_number="4513110000000000", exp_month="08", exp_year="2027", cvc="123",
        )
        self.assertTrue(queued["accepted"])
        self.assertEqual(queued["status"], "pending")
        task = _wait_task(queued["task_id"])
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["result"]["status"], "success")
        run.assert_called_once()
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["access_token"], "tok-1")
        self.assertEqual(kwargs["account_id"], "1")
        self.assertEqual(kwargs["card_number"], "4513110000000000")
        self.assertEqual(kwargs["cvc"], "123")
        self.assertEqual(kwargs["card_name"], "CHATGPT USER")

    @patch("core.plus_zero.run_zero_plus", side_effect=RuntimeError("boom"))
    def test_execution_error_records_error(self, run):
        queued = plus_bind_service.enqueue_bind(
            2, access_token="tok-2", card_number="1", exp_month="1", exp_year="1", cvc="1",
        )
        task = _wait_task(queued["task_id"])
        self.assertEqual(task["status"], "error")
        self.assertIn("boom", task["error"])

    def test_same_account_dedup_while_running(self):
        release = threading.Event()

        def slow_run(**kwargs):
            release.wait(timeout=5)
            return {"ok": True, "status": "success"}

        with patch("core.plus_zero.run_zero_plus", side_effect=slow_run):
            first = plus_bind_service.enqueue_bind(
                3, access_token="tok-3", card_number="1", exp_month="1", exp_year="1", cvc="1",
            )
            self.assertTrue(first["accepted"])
            second = plus_bind_service.enqueue_bind(
                3, access_token="tok-3", card_number="1", exp_month="1", exp_year="1", cvc="1",
            )
            self.assertFalse(second["accepted"])
            self.assertTrue(second["busy"])
            self.assertEqual(second["task_id"], first["task_id"])
            release.set()
            _wait_task(first["task_id"])

    def test_done_task_allows_requeue(self):
        with patch("core.plus_zero.run_zero_plus",
                   return_value={"ok": True, "status": "success"}):
            first = plus_bind_service.enqueue_bind(
                4, access_token="tok-4", card_number="1", exp_month="1", exp_year="1", cvc="1",
            )
            _wait_task(first["task_id"])
        with patch("core.plus_zero.run_zero_plus",
                   return_value={"ok": True, "status": "success"}):
            second = plus_bind_service.enqueue_bind(
                4, access_token="tok-4", card_number="1", exp_month="1", exp_year="1", cvc="1",
            )
            self.assertTrue(second["accepted"])
            _wait_task(second["task_id"])

    def test_list_tasks_and_clear_finished(self):
        with patch("core.plus_zero.run_zero_plus",
                   return_value={"ok": True, "status": "success"}):
            t1 = plus_bind_service.enqueue_bind(
                5, access_token="tok-5", card_number="1", exp_month="1", exp_year="1", cvc="1",
            )
            _wait_task(t1["task_id"])
        rows = plus_bind_service.list_tasks(10)
        self.assertTrue(any(t["task_id"] == t1["task_id"] for t in rows))
        task = plus_bind_service.get_task(t1["task_id"])
        self.assertIsNotNone(task)
        self.assertNotIn("params", task)  # 敏感参数不回传
        cleared = plus_bind_service.clear_finished()
        self.assertGreaterEqual(cleared, 1)
        self.assertIsNone(plus_bind_service.get_task(t1["task_id"]))


if __name__ == "__main__":
    unittest.main()
