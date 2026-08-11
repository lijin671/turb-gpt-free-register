# -*- coding: utf-8 -*-
"""Sentinel SDK 版本自动发现 / 缓存 / runner 与 openai_auth 接线测试。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import sentinel_sdk
from core.sentinel_sdk import (
    clear_cache,
    current_sentinel_sv,
    ensure_sentinel_sdk,
    script_src_for_version,
)
from config import SENTINEL_SV


class FakeResponse:
    def __init__(self, text="", content=b"", status_code=200):
        self.text = text
        self.content = content
        self.status_code = status_code


class SentinelSdkTests(unittest.TestCase):
    def setUp(self):
        clear_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(clear_cache)

    def _patch_cache_dir(self):
        return patch.dict(os.environ, {"SENTINEL_SDK_CACHE_DIR": self._tmp.name})

    def test_script_src_format(self):
        self.assertEqual(
            script_src_for_version("20260101abcd"),
            "https://chatgpt.com/sentinel/20260101abcd/sdk.js",
        )

    def test_discovery_extracts_version_and_caches(self):
        with patch("core.sentinel_sdk.curl_requests") as fake_mod, \
             self._patch_cache_dir():
            fake_mod.get.return_value = FakeResponse(
                text='src="https://chatgpt.com/sentinel/20990101new1/sdk.js"'
            )
            self.assertEqual(current_sentinel_sv(), "20990101new1")
            # TTL 缓存命中：不再发起网络请求
            fake_mod.get.reset_mock()
            self.assertEqual(current_sentinel_sv(), "20990101new1")
            fake_mod.get.assert_not_called()

    def test_discovery_failure_falls_back_to_pinned(self):
        with patch("core.sentinel_sdk.curl_requests.get", side_effect=RuntimeError("network down")) as fake_get:
            self.assertEqual(current_sentinel_sv(), SENTINEL_SV)
        self.assertTrue(fake_get.called)

    def test_auto_update_disabled_uses_pinned(self):
        with patch.object(sentinel_sdk, "SENTINEL_SDK_AUTO_UPDATE", False), \
             patch("core.sentinel_sdk.curl_requests") as fake_mod:
            self.assertEqual(current_sentinel_sv(), SENTINEL_SV)
            fake_mod.get.assert_not_called()

    def test_ensure_sdk_downloads_and_caches(self):
        version = "20990101new2"
        with patch("core.sentinel_sdk.curl_requests") as fake_mod, \
             self._patch_cache_dir():
            def fake_get(url, **kwargs):
                if "backend-api/sentinel/sdk.js" in url:
                    return FakeResponse(text=f'src="https://chatgpt.com/sentinel/{version}/sdk.js"')
                return FakeResponse(content=b"var SentinelSDK=1;")
            fake_mod.get.side_effect = fake_get
            sdk_path, got_version, script_src = ensure_sentinel_sdk()
            self.assertEqual(got_version, version)
            self.assertEqual(script_src, script_src_for_version(version))
            self.assertTrue(sdk_path.is_file())
            self.assertEqual(sdk_path.read_bytes(), b"var SentinelSDK=1;")
            # 第二次命中内存缓存，不再下载
            fake_mod.get.reset_mock()
            sdk_path2, got_version2, _ = ensure_sentinel_sdk()
            self.assertEqual(sdk_path2, sdk_path)
            self.assertEqual(got_version2, version)
            fake_mod.get.assert_not_called()

    def test_ensure_sdk_download_failure_falls_back_vendored(self):
        # 发现到新版本但下载失败 -> 回退项目自带 sdk.js + SENTINEL_SV
        version = "20990101new2"
        with patch("core.sentinel_sdk.curl_requests") as fake_mod, \
             self._patch_cache_dir():
            def fake_get(url, **kwargs):
                if "backend-api/sentinel/sdk.js" in url:
                    return FakeResponse(text=f'src="https://chatgpt.com/sentinel/{version}/sdk.js"')
                return FakeResponse(content=b"", status_code=403)
            fake_mod.get.side_effect = fake_get
            sdk_path, got_version, script_src = ensure_sentinel_sdk()
            self.assertEqual(got_version, SENTINEL_SV)
            self.assertTrue(sdk_path.is_file())
            self.assertEqual(script_src, script_src_for_version(SENTINEL_SV))

    def test_ensure_sdk_reuses_vendored_when_version_matches(self):
        # 未发现新版本（回退 SENTINEL_SV）时直接复用项目自带 sdk.js，不发起下载
        with patch("core.sentinel_sdk.curl_requests.get", side_effect=RuntimeError("network down")) as fake_get, \
             self._patch_cache_dir():
            sdk_path, got_version, script_src = ensure_sentinel_sdk()
            self.assertEqual(got_version, SENTINEL_SV)
            self.assertEqual(sdk_path.name, "sdk.js")
            self.assertTrue(sdk_path.is_file())
            self.assertEqual(script_src, script_src_for_version(SENTINEL_SV))
            # 只有一次版本探测请求，没有下载请求
            fake_get.assert_called_once()

    def test_runner_uses_discovered_sdk(self):
        from core import sentinel_runner
        version = "20990101new3"
        fake_sdk = Path(self._tmp.name) / "sdk.js"
        fake_sdk.write_text("var SentinelSDK=1;")

        def fake_ensure(session=None, timeout=15.0):
            return fake_sdk, version, script_src_for_version(version)

        fake_proc = type("Proc", (), {"returncode": 0, "stdout": json.dumps({
            "p": "gAAAAABp", "c": "c123", "id": "dev-1", "flow": "authorize_continue",
            "t": "", "so": None,
        }), "stderr": ""})()

        with patch.object(sentinel_runner, "ensure_sentinel_sdk", side_effect=fake_ensure), \
             patch("core.sentinel_runner.subprocess.run", return_value=fake_proc) as mock_run:
            out = sentinel_runner.generate_sentinel_token(
                challenge={"token": "c123"}, flow="authorize_continue", device_id="dev-1",
            )
            self.assertIn("gAAAAABp", out)
            cmd = mock_run.call_args[0][0]
            self.assertIn("--sdk", cmd)
            self.assertEqual(cmd[cmd.index("--sdk") + 1], str(fake_sdk))
            self.assertIn("--script-src", cmd)
            self.assertEqual(
                cmd[cmd.index("--script-src") + 1],
                f"https://chatgpt.com/sentinel/{version}/sdk.js",
            )

    def test_request_sentinel_token_uses_configured_url_and_sets_version(self):
        import core.openai_auth as oa
        version = "20990101new4"
        fake_sdk = Path(self._tmp.name) / "sdk.js"
        fake_sdk.write_text("var SentinelSDK=1;")

        def fake_ensure(session=None, timeout=15.0):
            return fake_sdk, version, script_src_for_version(version)

        class FakeResp:
            def raise_for_status(self):
                pass
            def json(self):
                return {"token": "tok", "proofofwork": {"required": False}, "turnstile": {"required": False}}

        class FakeSession:
            device_id = "dev-1"
            sentinel_sid = "sid-1"
            browser_profile = {}
            sentinel_req_p = ""
            sentinel_sv = ""
            sentinel_script_src = ""
            def get_sentinel_headers(self):
                return {"accept": "*/*"}
            def post(self, url, headers=None, data=None):
                self.posted_url = url
                return FakeResp()

        sess = FakeSession()
        with patch.object(oa, "ensure_sentinel_sdk", side_effect=fake_ensure), \
             patch.object(oa, "SENTINEL_REQ_URL", "https://sentinel.openai.com/backend-api/sentinel/req"):
            data = oa.request_sentinel_token(sess, "authorize_continue")
        self.assertEqual(data["token"], "tok")
        self.assertEqual(sess.sentinel_sv, version)
        self.assertEqual(sess.sentinel_script_src, script_src_for_version(version))
        self.assertEqual(sess.posted_url, "https://sentinel.openai.com/backend-api/sentinel/req")


if __name__ == "__main__":
    unittest.main()
