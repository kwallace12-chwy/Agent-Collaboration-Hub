"""Exercise the generated Cursor command against an isolated local MCP server.

This checks the generated stdio configuration; it does not launch Cursor or a
model provider. No personal client settings or existing board are accessed.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("cursor_setup_test", ROOT / "setup_clients.py")
setup_clients = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup_clients)


class CursorSetupTests(unittest.TestCase):
    def test_generated_cursor_command_runs_mcp_roundtrip_from_unrelated_directory(self):
        with tempfile.TemporaryDirectory(prefix="collab-cursor-test-") as folder:
            root = Path(folder).resolve()
            workspace = root / "private team's board"
            output = root / "generated settings"
            unrelated = root / "unrelated working folder"
            unrelated.mkdir()
            setup_clients.generate(workspace, output)
            configuration = json.loads((output / "cursor.mcp.json").read_text(encoding="utf-8"))
            server = configuration["mcpServers"]["agent-collab"]
            calls = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "synthetic-cursor-config-check", "version": "1"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                    "name": "collab_post", "arguments": {
                        "sender": "cursor-test", "content": "Synthetic Cursor configuration check [nowake]"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                    "name": "collab_read", "arguments": {"limit": 5}}},
            ]
            environment = {**os.environ, **server["env"], "COLLAB_ALERTS_AUTOSTART": "0",
                           "COLLAB_DISABLE_EXTERNAL_DELIVERY": "1", "COLLAB_ALLOW_CONSULTS": "0",
                           "PYTHONUTF8": "1"}
            def invoke(requests):
                result = subprocess.run([server["command"], *server["args"]], cwd=unrelated,
                                        env=environment, input="".join(json.dumps(call) + "\n" for call in requests),
                                        text=True, encoding="utf-8", capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                responses = {response["id"]: response
                             for response in map(json.loads, result.stdout.splitlines())}
                self.assertEqual(set(responses), {call["id"] for call in requests})
                for response in responses.values():
                    self.assertNotIn("error", response)
                    self.assertFalse(response.get("result", {}).get("isError", False), response)
                return responses

            # Tool handlers may run concurrently. Complete the write before a
            # second client process reads the same configured workspace.
            responses = invoke(calls[:3])
            tool_names = {tool["name"] for tool in responses[2]["result"]["tools"]}
            self.assertIn("collab_inbox", tool_names)
            self.assertIn("collab_post", tool_names)
            read = invoke([calls[0], calls[3]])
            self.assertIn("Synthetic Cursor configuration check", json.dumps(read[4]))
            self.assertTrue((workspace / "state" / "collab.db").is_file())
            self.assertEqual(list(unrelated.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
