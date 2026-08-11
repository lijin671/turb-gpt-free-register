# -*- coding: utf-8 -*-
"""WebUI 零元 Plus 绑卡端点测试（异步 202 + bulk + tasks，mock enqueue_bind）。"""
import unittest
from unittest.mock import patch

from webui.app import create_app


def _acc(**kw):
    base = {"id": 1, "email": "a@b.com", "access_token": "tok-1",
            "user_id": "acc-1", "device_id": "dev-1", "proxy_used": ""}
    base.update(kw)
    return base


def _queued(**kw):
    out = {"accepted": True, "task_id": "task-abc123", "status": "pending"}
    out.update(kw)
    return out


class WebUiPlusZeroTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        self.card_patches = [
            patch("config.plus.PLUS_CARD_NUMBER", "4513110000000000"),
            patch("config.plus.PLUS_CARD_EXP_MONTH", "08"),
            patch("config.plus.PLUS_CARD_EXP_YEAR", "2027"),
            patch("config.plus.PLUS_CARD_CVV", "123"),
        ]
        for p in self.card_patches:
            p.start()

    def tearDown(self):
        for p in self.card_patches:
            p.stop()

    @patch("core.plus_bind_service.enqueue_bind", return_value=_queued())
    @patch("webui.app.db.get_account", return_value=None)
    def test_missing_account_404(self, get_account, enqueue):
        r = self.client.post("/api/accounts/999/plus-zero", json={})
        self.assertEqual(r.status_code, 404)
        enqueue.assert_not_called()

    @patch("webui.app.db.get_account", return_value=_acc(access_token=""))
    def test_no_token_400(self, get_account):
        r = self.client.post("/api/accounts/1/plus-zero", json={})
        self.assertEqual(r.status_code, 400)

    @patch("core.plus_bind_service.enqueue_bind", return_value=_queued())
    @patch("webui.app.db.get_account", return_value=_acc())
    def test_bind_success_202_uses_configured_card(self, get_account, enqueue):
        r = self.client.post("/api/accounts/1/plus-zero", json={})
        self.assertEqual(r.status_code, 202)
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["task_id"], "task-abc123")
        self.assertEqual(data["status"], "pending")
        enqueue.assert_called_once()
        args, kwargs = enqueue.call_args
        self.assertEqual(args[0], 1)
        self.assertEqual(kwargs["access_token"], "tok-1")
        self.assertEqual(kwargs["email"], "a@b.com")
        self.assertEqual(kwargs["card_number"], "4513110000000000")
        self.assertEqual(kwargs["cvc"], "123")
        self.assertEqual(kwargs["device_id"], "dev-1")

    @patch("config.plus.PLUS_CARD_CVV", "")
    @patch("config.plus.PLUS_CARD_EXP_YEAR", "")
    @patch("config.plus.PLUS_CARD_EXP_MONTH", "")
    @patch("config.plus.PLUS_CARD_NUMBER", "")
    @patch("core.plus_bind_service.enqueue_bind", return_value=_queued(task_id="task-body1"))
    @patch("webui.app.db.get_account", return_value=_acc())
    def test_bind_success_with_body_card_override(self, get_account, enqueue):
        r = self.client.post("/api/accounts/1/plus-zero", json={
            "card_number": "5236860000000000",
            "exp_month": "12", "exp_year": "2028", "cvc": "999",
        })
        self.assertEqual(r.status_code, 202)
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["card_number"], "5236860000000000")
        self.assertEqual(kwargs["cvc"], "999")

    @patch("core.plus_bind_service.enqueue_bind",
           return_value={"accepted": False, "busy": True, "task_id": "task-busy1"})
    @patch("webui.app.db.get_account", return_value=_acc())
    def test_bind_busy_409(self, get_account, enqueue):
        r = self.client.post("/api/accounts/1/plus-zero", json={})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["busy"])

    @patch("config.plus.PLUS_CARD_CVV", "")
    @patch("config.plus.PLUS_CARD_EXP_YEAR", "")
    @patch("config.plus.PLUS_CARD_EXP_MONTH", "")
    @patch("config.plus.PLUS_CARD_NUMBER", "")
    @patch("webui.app.db.get_account", return_value=_acc())
    def test_missing_card_config_400(self, get_account):
        r = self.client.post("/api/accounts/1/plus-zero", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("卡信息不完整", r.get_json()["error"])

    # ---------- bulk ----------

    @patch("core.plus_bind_service.enqueue_bind")
    @patch("webui.app.db.get_account", side_effect=[
        _acc(id=1, access_token="tok-1"),
        _acc(id=2, access_token="tok-2"),
    ])
    def test_bulk_mixed_accepted_busy(self, get_account, enqueue):
        def fake_enqueue(acc_id, **kw):
            if acc_id == 1:
                return _queued(task_id="task-1")
            return {"accepted": False, "busy": True, "task_id": "task-2"}
        enqueue.side_effect = fake_enqueue
        r = self.client.post("/api/accounts/plus-zero-bulk", json={"account_ids": [1, 2]})
        self.assertEqual(r.status_code, 202)
        data = r.get_json()
        self.assertEqual(data["accepted_count"], 1)
        self.assertEqual(data["busy_count"], 1)
        self.assertEqual(data["skipped_count"], 0)
        self.assertEqual(data["accepted"][0]["account_id"], 1)
        self.assertEqual(data["busy"][0]["account_id"], 2)

    @patch("webui.app.db.get_account", side_effect=[None, _acc(id=2, access_token=""),
                                                    _acc(id=3, access_token="tok-3")])
    @patch("core.plus_bind_service.enqueue_bind", return_value=_queued(task_id="task-3"))
    def test_bulk_skips_missing_and_no_token(self, enqueue, get_account):
        r = self.client.post("/api/accounts/plus-zero-bulk", json={"account_ids": [1, 2, 3]})
        self.assertEqual(r.status_code, 202)
        data = r.get_json()
        self.assertEqual(data["accepted_count"], 1)
        self.assertEqual(data["skipped_count"], 2)
        reasons = {s["account_id"]: s["reason"] for s in data["skipped"]}
        self.assertEqual(reasons[1], "账号不存在")
        self.assertEqual(reasons[2], "无 access_token")

    def test_bulk_empty_ids_400(self):
        r = self.client.post("/api/accounts/plus-zero-bulk", json={"account_ids": []})
        self.assertEqual(r.status_code, 400)

    def test_bulk_too_many_400(self):
        r = self.client.post("/api/accounts/plus-zero-bulk",
                             json={"account_ids": list(range(201))})
        self.assertEqual(r.status_code, 400)

    @patch("config.plus.PLUS_CARD_CVV", "")
    @patch("config.plus.PLUS_CARD_EXP_YEAR", "")
    @patch("config.plus.PLUS_CARD_EXP_MONTH", "")
    @patch("config.plus.PLUS_CARD_NUMBER", "")
    def test_bulk_missing_card_config_400(self):
        r = self.client.post("/api/accounts/plus-zero-bulk", json={"account_ids": [1]})
        self.assertEqual(r.status_code, 400)

    @patch("core.plus_bind_service.list_tasks", return_value=[
        {"task_id": "task-1", "account_id": 1, "email": "a@b.com",
         "status": "done", "started_at": 1, "finished_at": 2,
         "result": {"ok": True}, "error": ""},
    ])
    def test_tasks_endpoint(self, list_tasks):
        r = self.client.get("/api/plus-zero/tasks?limit=10")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["tasks"][0]["task_id"], "task-1")
        list_tasks.assert_called_once_with(10)

    def test_requires_auth(self):
        client = create_app(auth_code="secret").test_client()
        r = client.post("/api/accounts/1/plus-zero", json={})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
