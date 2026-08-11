# -*- coding: utf-8 -*-
import base64
import json
import unittest
from unittest.mock import patch

from core.turnstile_solver import _xor_string, solve_turnstile_token


def _make_dx(program_json: str, p: str) -> str:
    return base64.b64encode(_xor_string(program_json, p).encode()).decode()


class TurnstileSolverTests(unittest.TestCase):
    def test_synthetic_turnstile_solve(self):
        # 指令 [[3, "hello"]] → func_3 -> result = base64("hello")
        p = "gAAAAAC" + base64.b64encode(json.dumps([0] * 25, separators=(',', ':')).encode()).decode()
        dx = _make_dx(json.dumps([[3, "hello"]], separators=(',', ':')), p)
        out = solve_turnstile_token(dx, p)
        self.assertEqual(out, base64.b64encode(b"hello").decode())

    def test_solve_turnstile_bad_input_returns_none(self):
        self.assertIsNone(solve_turnstile_token("!!!not-base64!!!", "p"))
        self.assertIsNone(solve_turnstile_token("", "p"))

    def test_python_fallback_builds_header_with_turnstile(self):
        import core.openai_auth as oa

        class FakeSession:
            device_id = "dev-123"
            sentinel_sid = "sid-abc"
            browser_profile = {}
            sentinel_req_p = ""

        p = "gAAAAAC" + base64.b64encode(json.dumps([0] * 25, separators=(',', ':')).encode()).decode()
        dx = _make_dx(json.dumps([[3, "hello"]], separators=(',', ':')), p)
        sess = FakeSession()
        sess.sentinel_req_p = p
        resp = {
            "token": "fake_c_token",
            "proofofwork": {"required": False},
            "turnstile": {"required": True, "dx": dx},
        }
        header, so = oa._build_sentinel_header_python(sess, resp, "authorize_continue")
        parsed = json.loads(header)
        self.assertEqual(parsed["t"], base64.b64encode(b"hello").decode())
        self.assertEqual(parsed["c"], "fake_c_token")
        self.assertEqual(parsed["id"], "dev-123")
        self.assertEqual(parsed["flow"], "authorize_continue")
        self.assertIsNone(so)

    def test_python_fallback_pow_path(self):
        import core.openai_auth as oa

        class FakeSession:
            device_id = "dev-123"
            sentinel_sid = "sid-abc"
            browser_profile = {}
            sentinel_req_p = ""

        sess = FakeSession()
        resp = {
            "token": "tok2",
            "proofofwork": {"required": True, "seed": "s1", "difficulty": "0"},
            "turnstile": {"required": False},
        }
        header, _ = oa._build_sentinel_header_python(sess, resp, "oauth_create_account")
        parsed = json.loads(header)
        self.assertTrue(parsed["p"].startswith("gAAAAAB"))
        self.assertEqual(parsed["t"], "")

    def test_runner_failure_falls_back_to_python(self):
        import core.openai_auth as oa

        class FakeSession:
            device_id = "dev-123"
            sentinel_sid = "sid-abc"
            browser_profile = {}
            sentinel_req_p = ""

            def auth_cookie_header(self):
                return f"oai-did={self.device_id}"

        p = "gAAAAAC" + base64.b64encode(json.dumps([0] * 25, separators=(',', ':')).encode()).decode()
        dx = _make_dx(json.dumps([[3, "hello"]], separators=(',', ':')), p)
        sess = FakeSession()
        sess.sentinel_req_p = p
        resp = {
            "token": "fake_c_token",
            "proofofwork": {"required": False},
            "turnstile": {"required": True, "dx": dx},
        }
        with patch.object(oa, "generate_sentinel_token", side_effect=RuntimeError("node missing")):
            header, so = oa.build_sentinel_header(sess, resp, "authorize_continue")
        parsed = json.loads(header)
        self.assertEqual(parsed["t"], base64.b64encode(b"hello").decode())
        self.assertIsNone(so)


if __name__ == "__main__":
    unittest.main()
