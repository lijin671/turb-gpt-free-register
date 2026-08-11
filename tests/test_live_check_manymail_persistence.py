# -*- coding: utf-8 -*-
"""查活功能 + manymail 凭据持久化/恢复链路测试。"""
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import manymail_client
from core import email_provider
from core import db


class ManyMailPersistenceTest(unittest.TestCase):

    def tearDown(self):
        # 清掉进程内缓存，避免污染其他测试
        manymail_client._CONTEXT_CACHE.clear()

    def test_restore_context(self):
        acc = manymail_client.restore_context(
            "abc@mail.lijin671.com", "pwd123", token="tok", domain="mail.lijin671.com"
        )
        self.assertEqual(acc.email, "abc@mail.lijin671.com")
        self.assertEqual(acc.password, "pwd123")
        self.assertEqual(manymail_client.get_account_context("abc@mail.lijin671.com").token, "tok")

    def test_save_account_data_persists_manymail_credentials(self):
        # 模拟注册进程内存中有 manymail 上下文
        manymail_client.restore_context(
            "persist-test@mail.lijin671.com", "secret-pw", token="tok-x", domain="mail.lijin671.com"
        )
        with mock.patch("core.db.insert_account", return_value=1) as m_insert:
            from core.account_export import save_account_data
            with mock.patch("core.account_export._append_batch_archive", return_value=Path(".")):
                with mock.patch("core.plan_check_service.enqueue_account_plan_check", return_value={"accepted": True}):
                    save_account_data(
                        email="persist-test@mail.lijin671.com",
                        access_token="jwt-token",
                        email_source="manymail",
                    )
            kwargs = m_insert.call_args.kwargs
            extra = kwargs.get("extra") or {}
            self.assertEqual(extra["manymail"]["password"], "secret-pw")
            self.assertEqual(extra["manymail"]["token"], "tok-x")

    def test_resolve_email_source_restores_from_db(self):
        # 新进程：内存无上下文，但 db 账号记录 extra 里有凭据 → resolve 应为 manymail
        fake_acc = {
            "email": "restore-me@mail.lijin671.com",
            "extra": {"manymail": {"password": "pw", "token": "t", "domain": "mail.lijin671.com"}},
        }
        with mock.patch.object(db, "get_account_by_email", return_value=fake_acc):
            src = email_provider.resolve_email_source("restore-me@mail.lijin671.com")
        self.assertEqual(src, "manymail")
        # 上下文应已重建，可被取码器使用
        self.assertIsNotNone(manymail_client.get_account_context("restore-me@mail.lijin671.com"))



    def test_resolve_email_source_restores_from_db_extra_json(self):
        # 真实 db.get_account_by_email 返回的是 decorated 行：只有 extra_json 原始字符串，没有 extra 键
        import json as _json
        fake_acc = {
            "email": "restore-json@mail.lijin671.com",
            "extra_json": _json.dumps({"manymail": {"password": "pw2", "token": "t2", "domain": "mail.lijin671.com"}}),
        }
        with mock.patch.object(db, "get_account_by_email", return_value=fake_acc):
            src = email_provider.resolve_email_source("restore-json@mail.lijin671.com")
        self.assertEqual(src, "manymail")
        self.assertIsNotNone(manymail_client.get_account_context("restore-json@mail.lijin671.com"))


if __name__ == "__main__":
    unittest.main()
