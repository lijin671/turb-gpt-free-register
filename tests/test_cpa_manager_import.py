# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.cpa_manager_import import (
    build_auth_file,
    filter_co_risk_accounts,
    import_batch,
    import_single_account,
    sanitize_email,
    scan_account_dirs,
)


class CpaManagerImportTests(unittest.TestCase):
    def test_sanitize_email(self):
        self.assertEqual(sanitize_email("a@b.com"), "a_b.com")
        self.assertEqual(sanitize_email("foo+bar@x.y"), "foo_bar_x.y")
        self.assertEqual(sanitize_email(""), "unknown")

    def test_build_auth_file(self):
        content = build_auth_file([
            {"email": "a@b.com", "access_token": "tok1"},
            {"email": "c@d.com", "access_token": ""},  # 空 token 跳过
            {"email": "e@f.com", "access_token": "tok3"},
        ])
        lines = [l for l in content.splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first, {"type": "codex", "access_token": "tok1"})

    def test_import_single_account_empty(self):
        r = import_single_account("", "tok", "http://x", "key")
        self.assertFalse(r["ok"])
        r2 = import_single_account("a@b.com", "", "http://x", "key")
        self.assertFalse(r2["ok"])

    def test_import_single_account_missing_key(self):
        r = import_single_account("a@b.com", "tok", "http://x", "")
        self.assertFalse(r["ok"])
        self.assertIn("CPA_MANAGER_PLUS_KEY", r["message"])

    def test_scan_account_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "20260805-1个-27"
            d.mkdir()
            (d / "注册成功账号.json").write_text(json.dumps(
                {"email": "x@y.com", "access_token": "tokA"}
            ), encoding="utf-8")
            d2 = Path(tmp) / "20260805-1个-28"
            d2.mkdir()
            (d2 / "注册成功的token.txt").write_text("tokB\n", encoding="utf-8")
            (d2 / "注册成功的邮箱.txt").write_text("b@c.com\n", encoding="utf-8")

            accounts = scan_account_dirs(Path(tmp))
            self.assertEqual(len(accounts), 2)
            self.assertEqual({a["email"] for a in accounts}, {"x@y.com", "b@c.com"})

            filtered = scan_account_dirs(Path(tmp), "20260805-1个-27")
            self.assertEqual(len(filtered), 1)
            self.assertEqual(filtered[0]["email"], "x@y.com")



    def test_build_auth_file_skips_co_risk_flag(self):
        content = build_auth_file([
            {"email": "a@b.com", "access_token": "tok1"},
            {"email": "c@d.com", "access_token": "tok2", "ip_co_risk": True},
            {"email": "e@f.com", "access_token": "tok3"},
        ])
        lines = [l for l in content.splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["access_token"], "tok1")
        self.assertEqual(json.loads(lines[1])["access_token"], "tok3")

    def test_filter_co_risk_accounts(self):
        accounts = [
            {"email": "a@b.com", "access_token": "tok1"},
            {"email": "c@d.com", "access_token": "tok2", "ip_co_risk": True},
            {"email": "e@f.com", "access_token": "tok3"},
        ]
        with mock.patch("core.cpa_manager_import._co_risk_emails", return_value={"e@f.com"}):
            clean, skipped = filter_co_risk_accounts(accounts)
        self.assertEqual({a["email"] for a in clean}, {"a@b.com"})
        self.assertEqual({a["email"] for a in skipped}, {"c@d.com", "e@f.com"})

    def test_import_single_account_co_risk_rejected(self):
        with mock.patch("core.cpa_manager_import._co_risk_emails", return_value={"a@b.com"}):
            r = import_single_account("a@b.com", "tok", "http://x", "key")
        self.assertFalse(r["ok"])
        self.assertIn("连坐风险", r["message"])

    def test_import_single_account_no_co_risk_proceeds(self):
        uploaded = {}

        def fake_upload(name, content):
            uploaded["content"] = content
            return {"ok": True, "status": 200, "data": {}}

        fake_api = {
            "upload": fake_upload,
            "list": lambda: [],
            "models": lambda name: {"models": [{"id": "m1"}]},
            "delete": lambda name: {},
        }
        with mock.patch("core.cpa_manager_import._co_risk_emails", return_value=set()), \
                mock.patch("core.cpa_manager_import.api", return_value=fake_api):
            r = import_single_account("a@b.com", "tok", "http://x", "key")
        self.assertTrue(r["ok"])
        self.assertIn("a@b.com", r["message"])
        self.assertIn("tok", uploaded["content"])

    def test_import_batch_all_co_risk(self):
        accounts = [{"email": "a@b.com", "access_token": "tok", "ip_co_risk": True}]
        r = import_batch(accounts, "http://x", "key")
        self.assertFalse(r["ok"])
        self.assertEqual(r.get("skipped_count"), 1)
        self.assertIn("连坐风险", r["message"])

    def test_import_batch_skips_co_risk_uploads_clean(self):
        uploaded = {}

        def fake_upload(name, content):
            uploaded["name"] = name
            uploaded["content"] = content
            return {"ok": True, "status": 200, "data": {}}

        fake_api = {
            "upload": fake_upload,
            "list": lambda: [],
            "models": lambda name: {"models": [{"id": "m1"}]},
            "delete": lambda name: {},
        }
        accounts = [
            {"email": "a@b.com", "access_token": "tok1"},
            {"email": "c@d.com", "access_token": "tok2", "ip_co_risk": True},
        ]
        with mock.patch("core.cpa_manager_import._co_risk_emails", return_value=set()), \
                mock.patch("core.cpa_manager_import.api", return_value=fake_api):
            r = import_batch(accounts, "http://x", "key", name="all.json", verify=True)
        self.assertTrue(r["ok"])
        self.assertEqual(r.get("imported_count"), 1)
        self.assertEqual(r.get("skipped_count"), 1)
        self.assertIn("隔离 1 连坐风险", r["message"])
        lines = [l for l in uploaded["content"].splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["access_token"], "tok1")

    def test_import_batch_db_co_risk_skipped(self):
        uploaded = {}

        def fake_upload(name, content):
            uploaded["content"] = content
            return {"ok": True, "status": 200, "data": {}}

        fake_api = {
            "upload": fake_upload,
            "list": lambda: [],
            "models": lambda name: {"models": []},
            "delete": lambda name: {},
        }
        accounts = [
            {"email": "a@b.com", "access_token": "tok1"},
            {"email": "db-risk@x.com", "access_token": "tok2"},
        ]
        with mock.patch("core.cpa_manager_import._co_risk_emails", return_value={"db-risk@x.com"}), \
                mock.patch("core.cpa_manager_import.api", return_value=fake_api):
            r = import_batch(accounts, "http://x", "key", name="all.json", verify=False)
        self.assertTrue(r["ok"])
        self.assertEqual(r.get("imported_count"), 1)
        self.assertEqual(r.get("skipped_count"), 1)
        self.assertNotIn("tok2", uploaded["content"])


if __name__ == "__main__":
    unittest.main()

