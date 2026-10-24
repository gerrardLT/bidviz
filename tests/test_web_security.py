from __future__ import annotations

import sys
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))
sys.path.insert(0, str(BASE))

import web.server as server  # noqa: E402


class WebPostAuthorizationTests(unittest.TestCase):
    def handler(self, headers: dict[str, str]):
        handler = server.Handler.__new__(server.Handler)
        message = Message()
        for key, value in headers.items():
            message[key] = value
        handler.headers = message
        handler.responses = []
        handler._json = lambda body, code=200: handler.responses.append((body, code))
        return handler

    def test_rejects_simple_cross_origin_post(self):
        handler = self.handler({
            "Host": "127.0.0.1:8770",
            "Origin": "https://evil.example",
            "Content-Type": "text/plain",
        })
        self.assertFalse(handler._authorized_post())
        self.assertIn(handler.responses[-1][1], {403, 415})

    def test_rejects_missing_explicit_header(self):
        handler = self.handler({
            "Host": "127.0.0.1:8770",
            "Content-Type": "application/json",
        })
        self.assertFalse(handler._authorized_post())
        self.assertEqual(handler.responses[-1][1], 403)

    def test_rejects_other_loopback_port(self):
        handler = self.handler({
            "Host": "127.0.0.1:8770",
            "Origin": "http://localhost:9999",
            "Content-Type": "application/json",
            "X-BidViz-Request": "1",
        })
        self.assertFalse(handler._authorized_post())
        self.assertEqual(handler.responses[-1][1], 403)

    def test_accepts_local_json_request(self):
        handler = self.handler({
            "Host": "localhost:8770",
            "Origin": "http://127.0.0.1:8770",
            "Content-Type": "application/json; charset=utf-8",
            "X-BidViz-Request": "1",
        })
        self.assertTrue(handler._authorized_post())
        self.assertEqual(handler.responses, [])


class BestEffortTelemetryTests(unittest.TestCase):
    def test_web_recorder_failure_returns_noop(self):
        with patch.object(server.memory, "RunRecorder", side_effect=OSError("read-only")):
            run = server._new_run("test")
        self.assertIsInstance(run, server._NullRun)
        server._record(run, "event", secret="value")
        server._close_run(run, "success")


if __name__ == "__main__":
    unittest.main()
