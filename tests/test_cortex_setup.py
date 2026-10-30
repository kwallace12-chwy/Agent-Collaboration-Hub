"""Exercise generated Cortex Code settings against an isolated local MCP server."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("cortex_setup_test", ROOT / "setup_clients.py")
setup_clients = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup_clients)


class McpClient:
    """Small line-oriented client that verifies the generated process command."""
    def __init__(self, server, cwd, environment):
        self.proc = subprocess.Popen([server["command"], *server["args"]], cwd=cwd, env=environment,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     bufsize=0)
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self._read_lines, daemon=True)
        self.reader.start()
        self.next_id = 1

    def _read_lines(self):
        while line := self.proc.stdout.readline():
            self.lines.put(line)
        self.lines.put(None)

    def send(self, request):
        self.proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def call(self, method, params=None):
        request_id = self.next_id
        self.next_id += 1
        request = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        self.send(request)
        try:
            line = self.lines.get(timeout=10)
        except queue.Empty as error:
            raise AssertionError("Timed out waiting for MCP response") from error
        if line is None:
            raise AssertionError("MCP server closed stdout before responding")
        response = json.loads(line.decode("utf-8"))
        if response.get("id") != request_id:
            raise AssertionError(f"Unexpected MCP response: {response}")
        if "error" in response:
            raise AssertionError(f"MCP error: {response}")
        return response["result"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=5)
        self.reader.join(timeout=5)
        self.proc.stdout.close()
        self.proc.stderr.close()


class CortexSetupTests(unittest.TestCase):
    def test_generated_cortex_command_exchanges_synthetic_same_board_messages(self):
        with tempfile.TemporaryDirectory(prefix="collab-cortex-test-") as folder:
            root = Path(folder).resolve()
            workspace = root / "synthetic private board"
            output = root / "generated settings"
            unrelated = root / "unrelated working folder"
            unrelated.mkdir()
            setup_clients.generate(workspace, output)
            configuration = json.loads((output / "cortex.mcp.json").read_text(encoding="utf-8"))
            server = configuration["mcpServers"]["agent-collab"]
            self.assertEqual(server["type"], "stdio")
            environment = {**os.environ, **server["env"], "COLLAB_ALERTS_AUTOSTART": "0",
                           "COLLAB_DISABLE_EXTERNAL_DELIVERY": "1", "COLLAB_ALLOW_CONSULTS": "0",
                           "PYTHONUTF8": "1"}

            initialize = {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "synthetic-cortex-config-check", "version": "1"}}
            coco = McpClient(server, unrelated, environment)
            try:
                started = coco.call("initialize", initialize)
                self.assertEqual(started["protocolVersion"], "2025-11-25")
                coco.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                self.assertEqual(coco.call("ping"), {})
                tools = coco.call("tools/list")["tools"]
                tool_names = {tool["name"] for tool in tools}
                self.assertTrue({"collab_status", "collab_post", "collab_read"}.issubset(tool_names))
                posted = coco.call("tools/call", {"name": "collab_post", "arguments": {
                    "sender": "coco", "content": "Synthetic Cortex Code message [nowake]"}})
                message_id = json.loads(posted["content"][0]["text"])["message_id"]
            finally:
                coco.close()

            peer = McpClient(server, unrelated, environment)
            try:
                started = peer.call("initialize", initialize)
                self.assertEqual(started["protocolVersion"], "2025-11-25")
                peer.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                self.assertEqual(peer.call("ping"), {})
                replied = peer.call("tools/call", {"name": "collab_post", "arguments": {
                    "sender": "synthetic-peer", "content": "Synthetic same-board reply [nowake]",
                    "reply_to": message_id}})
                self.assertFalse(replied["isError"])
                read = peer.call("tools/call", {"name": "collab_read", "arguments": {"limit": 5}})
                messages = json.loads(read["content"][0]["text"])["messages"]
                self.assertEqual([message["content"] for message in messages], [
                    "Synthetic Cortex Code message [nowake]", "Synthetic same-board reply [nowake]"])
                self.assertEqual(messages[1]["reply_to"], message_id)
            finally:
                peer.close()
            self.assertTrue((workspace / "state" / "collab.db").is_file())
            self.assertEqual(list(unrelated.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
