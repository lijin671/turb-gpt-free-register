# -*- coding: utf-8 -*-
"""tools/export_chatgpt2api 导出子流程测试（纯逻辑，无网络）。"""
import json
import tempfile
import unittest
from pathlib import Path

from tools.export_chatgpt2api import load_accounts


def _write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class ExportChatgpt2ApiTests(unittest.TestCase):
    def test_load_accounts_list_and_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "batch1" / "注册成功账号.json", [
                {"email": "a@x.com", "access_token": "tok-a"},
                {"email": "b@x.com"},  # 无 token 应跳过
            ])
            _write(root / "batch2" / "注册成功账号.json", {
                "email": "c@x.com", "access_token": "tok-c",
            })
            (root / "batch3").mkdir()  # 无账号文件，应跳过

            out = load_accounts(str(root))

        self.assertEqual(len(out), 2)
        emails = {a["email"] for _, a in out}
        self.assertEqual(emails, {"a@x.com", "c@x.com"})
        batches = {b for b, _ in out}
        self.assertEqual(batches, {"batch1", "batch2"})

    def test_load_accounts_skips_corrupted_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "bad" / "注册成功账号.json", {"email": "x@x.com", "access_token": "t"})
            bad2 = root / "bad2"
            bad2.mkdir(parents=True, exist_ok=True)
            (bad2 / "注册成功账号.json").write_text("{not json", encoding="utf-8")
            out = load_accounts(str(root))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1]["email"], "x@x.com")


if __name__ == "__main__":
    unittest.main()
