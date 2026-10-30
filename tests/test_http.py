"""Exercise the real loopback API without reading or modifying a user's board."""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

# Test subprocesses must never notify the OS or wake a real agent session.
os.environ["COLLAB_DISABLE_EXTERNAL_DELIVERY"] = "1"
os.environ["COLLAB_ALERTS_AUTOSTART"] = "0"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class HttpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="collab-http-test-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve() / "private board with spaces"
        name = "collab_http_" + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(name, ROOT / "collab.py")
        self.hub = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"COLLAB_WORKSPACE": str(self.workspace)}):
            spec.loader.exec_module(self.hub)
        self.server = self.hub.ThreadingHTTPServer(
            ("127.0.0.1", 0), self.hub.CollabHTTPRequestHandler
        )
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.port = self.server.server_port
        self.origin = "http://127.0.0.1:" + str(self.port)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, payload=None, *, raw=None, headers=None):
        request_headers = dict(headers or {})
        if method == "POST":
            request_headers.setdefault("Content-Type", "application/json")
            if raw is None:
                raw = json.dumps(payload if payload is not None else {}).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=raw, headers=request_headers)
            response = connection.getresponse()
            body = response.read()
            response_headers = dict(response.getheaders())
            if response_headers.get("Content-Type", "").startswith("application/json"):
                body = json.loads(body)
            return response.status, response_headers, body
        finally:
            connection.close()

    def test_dashboard_aliases_cannot_be_framed(self):
        for path in ("/", "/index.html"):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Security-Policy"], "frame-ancestors 'none'")
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertIn(b"<title>AI Agent Collab Space</title>", body)

    def test_health_identifies_actual_process_and_private_workspace(self):
        status, headers, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "agent-collab-hub")
        self.assertEqual(body["workspace"], str(self.workspace.resolve()))
        self.assertEqual(body["pid"], os.getpid())
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_server_binds_without_reverse_dns_lookup(self):
        with patch("socket.getfqdn", side_effect=AssertionError("Reverse DNS must not delay startup")):
            server = self.hub.ThreadingHTTPServer(("127.0.0.1", 0), self.hub.CollabHTTPRequestHandler)
        try:
            self.assertEqual(server.server_name, "127.0.0.1")
            self.assertEqual(server.server_port, server.server_address[1])
            self.assertGreater(server.server_port, 0)
        finally:
            server.server_close()

    def test_message_roundtrip_and_snapshots_stay_in_selected_workspace(self):
        status, _, empty = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(empty["messages"], [])
        status, _, joined = self.request("POST", "/api/join", {
            "agent_id": "codex", "model": "test model", "surface": "test",
            "role": "Reviewer", "announce": False,
        }, headers={"Origin": self.origin})
        self.assertEqual(status, 200)
        self.assertTrue(joined["ok"])
        content = "A private idea: O'Brien’s review for @claude."
        status, _, posted = self.request("POST", "/api/post", {
            "sender": "codex", "content": content, "channel": "reviews", "pin": True,
        }, headers={"Origin": self.origin})
        self.assertEqual(status, 200)
        self.assertTrue(posted["ok"])
        status, _, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(len(state["messages"]), 1)
        message = state["messages"][0]
        self.assertEqual(message["content"], content)
        self.assertEqual(message["channel"], "reviews")
        self.assertEqual(message["model"], "test model")
        self.assertEqual(message["pinned"], 1)
        self.assertTrue((self.workspace / "state" / "collab.db").is_file())
        snapshot = json.loads((self.workspace / "state" / "board.json").read_text())
        self.assertEqual(snapshot["messages"][0]["content"], content)
        self.assertIn(content, (self.workspace / "BOARD.md").read_text())

    def test_another_imported_workspace_cannot_see_private_messages(self):
        status, _, _ = self.request("POST", "/api/post", {
            "sender": "codex", "content": "private to board one",
        })
        self.assertEqual(status, 200)
        other_workspace = self.workspace.parent / "second board"
        spec = importlib.util.spec_from_file_location(
            "collab_isolation_" + uuid.uuid4().hex, ROOT / "collab.py"
        )
        other = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"COLLAB_WORKSPACE": str(other_workspace)}):
            spec.loader.exec_module(other)
        other.init_db()
        with other.get_db() as database:
            state = other.export_state(database)
        self.assertEqual(state["messages"], [])
        self.assertNotEqual(other.DB_PATH, self.hub.DB_PATH)
        self.assertEqual(self.request("GET", "/api/state")[2]["messages"][0]["content"],
                         "private to board one")

    def test_dashboard_is_served_without_cross_origin_permission(self):
        for path in ("/", "/index.html"):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith("text/html"))
                self.assertIn(b"AI Agent Collab Space", body)
                self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_host_must_be_loopback_with_the_actual_port(self):
        for host in ("attacker.example:" + str(self.port), "127.0.0.1:1",
                     "127.0.0.1", "127.0.0.1:invalid", ""):
            with self.subTest(host=host):
                status, _, body = self.request("GET", "/api/state", headers={"Host": host})
                self.assertEqual(status, 403)
                self.assertFalse(body["ok"])
        status, _, _ = self.request("GET", "/api/health", headers={
            "Host": "localhost:" + str(self.port),
            "Origin": "http://localhost:" + str(self.port),
        })
        self.assertEqual(status, 200)

    def test_cross_origin_mutations_are_rejected_and_do_not_write(self):
        for origin in ("https://attacker.example", "null",
                       "http://127.0.0.1:1", "https://127.0.0.1:" + str(self.port),
                       "http://localhost:" + str(self.port)):
            with self.subTest(origin=origin):
                status, headers, body = self.request("POST", "/api/post", {
                    "sender": "attacker", "content": "must never be stored",
                }, headers={"Origin": origin})
                self.assertEqual(status, 403)
                self.assertFalse(body["ok"])
                self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.request("GET", "/api/state")[2]["messages"], [])

    def test_cross_origin_preflight_is_denied(self):
        status, headers, body = self.request("OPTIONS", "/api/post", headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "POST",
        })
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_post_requires_json_media_type(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", ""):
            with self.subTest(content_type=content_type):
                status, _, body = self.request("POST", "/api/post", {
                    "sender": "codex", "content": "not accepted",
                }, headers={"Content-Type": content_type})
                self.assertEqual(status, 415)
                self.assertFalse(body["ok"])
        status, _, body = self.request("POST", "/api/post", {
            "sender": "codex", "content": "valid charset parameter",
        }, headers={"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_invalid_json_utf8_and_non_object_bodies_are_bad_requests(self):
        for raw in (b"{", b'{"content": "\xff"}', b"[]", b"null", b'"string"', b"42"):
            with self.subTest(raw=raw):
                status, _, body = self.request("POST", "/api/post", raw=raw)
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])
        self.assertEqual(self.request("GET", "/api/state")[2]["messages"], [])

    def test_invalid_lengths_and_oversized_requests_fail_before_reading_body(self):
        for length, expected in (("not-a-number", 400), ("1048577", 413), ("-1", 413)):
            with self.subTest(length=length):
                status, _, body = self.request("POST", "/api/post", raw=b"", headers={
                    "Content-Length": length,
                })
                self.assertEqual(status, expected)
                self.assertFalse(body["ok"])

    def test_request_exactly_at_one_mebibyte_limit_is_accepted(self):
        payload = {"sender": "codex", "content": ""}
        overhead = len(json.dumps(payload).encode("utf-8"))
        payload["content"] = "x" * (1048576 - overhead)
        raw = json.dumps(payload).encode("utf-8")
        self.assertEqual(len(raw), 1048576)
        status, _, body = self.request("POST", "/api/post", raw=raw)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["state"]["messages"][0]["content"], payload["content"])

    def test_invalid_field_types_return_bad_request_without_crashing_server(self):
        cases = (
            ("/api/task/claim", {"task_id": "not-a-number"}),
            ("/api/task/update", {"task_id": None}),
            ("/api/vote", {"decision_id": []}),
            ("/api/lock", {"filepath": []}),
            ("/api/lock", {"filepath": "shared/app.py", "ttl": True}),
            ("/api/lock", {"filepath": "shared/app.py", "ttl": 0}),
            ("/api/lock", {"filepath": "shared/app.py", "ttl": "60"}),
            ("/api/post", {"content": {"bad": "type"}}),
            ("/api/post", {"channel": [], "content": "invalid channel"}),
            ("/api/post", {"reply_to": True, "content": "booleans are not message IDs"}),
            ("/api/post", {"pin": "false", "content": "booleans must be JSON booleans"}),
            ("/api/join", {"agent_id": []}),
        )
        for path, payload in cases:
            with self.subTest(path=path, payload=payload):
                status, _, body = self.request("POST", path, payload)
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])
        self.assertEqual(self.request("GET", "/api/health")[0], 200)

    def test_unknown_route_returns_json_not_found(self):
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                status, _, body = self.request(method, "/api/no-such-endpoint")
                self.assertEqual(status, 404)
                self.assertFalse(body["ok"])


if __name__ == "__main__":
    unittest.main()
