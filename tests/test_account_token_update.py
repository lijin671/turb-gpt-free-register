# -*- coding: utf-8 -*-
"""core.db.update_account_access_token 单测（temp 文件）。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class AccountTokenUpdateTests(unittest.TestCase):
    def _paths(self, root):
        return dict(
            _ACCOUNTS_JSON=root / "accounts.json",
            _LEGACY_ACCOUNTS_JSON=root / "legacy_accounts.json",
            _ACCOUNTS_TXT=root / "accounts.txt",
            _TOKENS_TXT=root / "tokens.txt",
            _VIEWER_HTML=root / "viewer.html",
        )

    def test_update_access_token_and_expires(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text(
                '[{"id":1,"email":"a@test.com","access_token":"old"}]', encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"), \
                 patch.object(db, "_ACCOUNTS_TXT", root / "a.txt"), \
                 patch.object(db, "_TOKENS_TXT", root / "t.txt"), \
                 patch.object(db, "_VIEWER_HTML", root / "v.html"):
                self.assertTrue(db.update_account_access_token(
                    "a@test.com", "new-token", expires_at="2099-01-01T00:00:00+00:00"))
                row = db.get_account_by_email("a@test.com")
                self.assertEqual(row["access_token"], "new-token")
                self.assertEqual(row["expires_at"], "2099-01-01T00:00:00+00:00")

    def test_missing_account_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text("[]", encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"), \
                 patch.object(db, "_ACCOUNTS_TXT", root / "a.txt"), \
                 patch.object(db, "_TOKENS_TXT", root / "t.txt"), \
                 patch.object(db, "_VIEWER_HTML", root / "v.html"):
                self.assertFalse(db.update_account_access_token("nope@x.com", "tok"))


if __name__ == "__main__":
    unittest.main()
