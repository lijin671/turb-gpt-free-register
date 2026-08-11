# -*- coding: utf-8 -*-
"""L 取号服务（core.sms_provider，SMS_PROVIDER="l"）测试。

对照 L_API.md：take-phone / fetch-code / release 三个端点。
"""
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from core import sms_provider
from config import codex as codex_config
from config import env_loader
from webui import config_editor


class _Resp:
    status_code = 200
    text = "{}"

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def post(self, url, headers=None, data=None):
        self.calls.append({"url": url, "headers": headers or {}, "data": data})
        return _Resp(self.responses.pop(0))

    def close(self):
        self.closed = True


def _l_patches(**kw):
    overrides = {
        "SMS_PROVIDER": "l",
        "L_API_BASE": "http://localhost:8788",
        "L_ADMIN_AUTH_CODE": "adm",
        "SMS_SERVICE": "facebook",
        "SMS_COUNTRY": "10",
        "SMS_MAX_PRICE": "",
        "L_PHONE_PREFIX": "",
    }
    overrides.update(kw)
    return [patch.object(codex_config, k, v) for k, v in overrides.items()]


def _ctx(**kw):
    """返回 ExitStack 上下文，自动进入所有 L 配置 patch。"""
    stack = ExitStack()
    for patcher in _l_patches(**kw):
        stack.enter_context(patcher)
    return stack


class LSmsProviderTests(unittest.TestCase):
    def test_secret_registry_and_webui_fields_include_l(self):
        self.assertIn("L_ADMIN_AUTH_CODE", env_loader.SECRET_ENV_KEYS)
        fields = {f["key"]: f for f in config_editor.EDITABLE_FIELDS}
        self.assertIn("L_API_BASE", fields)
        self.assertIn("L_PHONE_PREFIX", fields)
        self.assertTrue(fields["L_ADMIN_AUTH_CODE"].get("secret"))

    def test_acquire_number_uses_l_take_phone(self):
        http = _Http([{"item": {"id": "lid-1", "phone": "9091234661",
                                "status": "active", "lastCode": ""}}])
        with _ctx():
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual(activation_id, "lid-1")
        self.assertEqual(phone, "9091234661")
        self.assertTrue(http.calls[0]["url"].endswith("/api/admin/l/take-phone"))
        self.assertIn('"service": "facebook"', http.calls[0]["data"])
        self.assertIn('"country": "10"', http.calls[0]["data"])
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Bearer adm")

    def test_acquire_number_applies_prefix_and_max_price(self):
        http = _Http([{"item": {"id": "lid-2", "phone": "91234661"}}])
        with _ctx(SMS_MAX_PRICE="0.05", L_PHONE_PREFIX="1"):
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual(activation_id, "lid-2")
        self.assertEqual(phone, "191234661")
        self.assertIn('"maxPrice": "0.05"', http.calls[0]["data"])

    def test_acquire_number_no_numbers_error(self):
        http = _Http([{"error": "取号失败：暂无号码", "raw": "NO_NUMBERS"}])
        with _ctx():
            with self.assertRaises(sms_provider.SmsNoNumbersError):
                sms_provider.acquire_number(http=http)

    def test_acquire_number_no_balance_error(self):
        http = _Http([{"error": "取号失败：余额不足", "raw": "NO_BALANCE"}])
        with _ctx():
            with self.assertRaises(sms_provider.SmsNoBalanceError):
                sms_provider.acquire_number(http=http)

    def test_acquire_number_missing_item_fields(self):
        http = _Http([{"item": {"phone": "9091234661"}}])
        with _ctx():
            with self.assertRaises(sms_provider.SmsProviderError):
                sms_provider.acquire_number(http=http)

    def test_wait_for_sms_code_uses_l_fetch_code(self):
        http = _Http([{"item": {"id": "lid-1", "status": "code_received"},
                       "code": "899201", "raw": "STATUS_OK:899201"}])
        with _ctx():
            code = sms_provider.wait_for_sms_code("lid-1", http=http, max_wait=1, poll_interval=0)

        self.assertEqual(code, "899201")
        self.assertTrue(http.calls[0]["url"].endswith("/api/admin/l/fetch-code"))
        self.assertIn('"id": "lid-1"', http.calls[0]["data"])

    def test_wait_for_sms_code_retries_until_code(self):
        http = _Http([
            {"item": {"id": "lid-1", "status": "active"}, "code": "",
             "raw": "STATUS_WAIT_CODE"},
            {"item": {"id": "lid-1", "status": "code_received"},
             "code": "899201", "raw": "STATUS_OK:899201"},
        ])
        # poll_interval=0 会被 `0 or config` 吃掉，需同时把配置轮询间隔置 0
        with _ctx(SMS_POLL_INTERVAL=0):
            code = sms_provider.wait_for_sms_code("lid-1", http=http, max_wait=2, poll_interval=0)

        self.assertEqual(code, "899201")
        self.assertEqual(len(http.calls), 2)

    def test_cancel_uses_l_release(self):
        http = _Http([{"released": 1, "failed": []}])
        with _ctx():
            sms_provider.cancel("lid-1", http=http)

        self.assertTrue(http.calls[0]["url"].endswith("/api/admin/l/release"))
        self.assertIn('"id": "lid-1"', http.calls[0]["data"])

    def test_release_l_numbers_batch(self):
        http = _Http([{"updated": 2, "released": 2, "failed": []}])
        with _ctx():
            data = sms_provider.release_l_numbers(["lid-1", "lid-2"], http=http)

        self.assertEqual(data["released"], 2)
        self.assertIn('"ids": ["lid-1", "lid-2"]', http.calls[0]["data"])

    def test_release_l_number_partial_failure_raises(self):
        http = _Http([{"updated": 0, "released": 0,
                       "failed": [{"id": "lid-1", "message": "订单不存在或已失效",
                                   "raw": "NO_ACTIVATION"}]}])
        with _ctx():
            # cancel() 设计为失败不抛（只告警），同步释放路径 _release_l_number 才会抛
            with self.assertRaises(sms_provider.SmsProviderError):
                sms_provider._release_l_number("lid-1", http=http)


if __name__ == "__main__":
    unittest.main()
