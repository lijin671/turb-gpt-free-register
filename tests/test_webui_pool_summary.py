# -*- coding: utf-8 -*-
"""WebUI /api/summary 库存水位字段测试（mock db.list_accounts，无网络）。"""
import unittest
from unittest.mock import patch

from webui.app import create_app


def _acc(email, token="tok", co_risk=False, codex_status=""):
    return {"email": email, "access_token": token,
            "ip_co_risk": co_risk, "codex_status": codex_status}


class WebUiPoolSummaryTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.db.outlook_pool_summary",
           return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    @patch("webui.app.db.domain_email_pool_summary",
           return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    @patch("webui.app.db.count_accounts", return_value=2)
    @patch("webui.app.db.list_accounts")
    def test_summary_includes_pool_water_level(self, list_accounts, count_accounts, domain, outlook):
        list_accounts.return_value = [
            _acc("a@x.com", token="t1"),
            _acc("b@x.com", token="t2", co_risk=True),
            _acc("c@x.com", token=""),
        ]
        r = self.client.get("/api/summary")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["pool_usable"], 1)
        self.assertEqual(data["pool_has_token"], 2)
        self.assertEqual(data["pool_co_risk"], 1)
        self.assertEqual(data["pool_total"], 3)
        self.assertEqual(data["pool_usable_rate"], round(1 / 3, 4))

    @patch("webui.app.db.outlook_pool_summary",
           return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    @patch("webui.app.db.domain_email_pool_summary",
           return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    @patch("webui.app.db.list_accounts", side_effect=RuntimeError("db broken"))
    def test_summary_survives_pool_stats_failure(self, list_accounts, domain, outlook):
        r = self.client.get("/api/summary")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["pool_usable"], 0)
        self.assertEqual(data["pool_total"], 0)


if __name__ == "__main__":
    unittest.main()
