#!/usr/bin/env python3
"""Exercise a local stdio MCP adapter against a temporary copy of its hub.

Usage: python3 smoke_mcp.py /path/to/collab_mcp.py
Optional: --cases cases.json, a list of {"name": ..., "arguments": ...} calls.
All sibling .py modules are copied, but the real database/board are not copied.
The tested adapter must resolve its hub from its own directory, not a hardcoded
production path. Inspect an adapter before executing this script against it.
External notifications, agent wake-ups, and paid consultations are disabled for the child.
"""

import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading


class Client:
    def __init__(self, server, cwd):
        self.stderr = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, str(server)], cwd=str(cwd), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.stderr, bufsize=0,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
                 "COLLAB_WORKSPACE": str(cwd), "COLLAB_ALERTS_AUTOSTART": "0",
                 "COLLAB_DISABLE_EXTERNAL_DELIVERY": "1", "COLLAB_ALLOW_CONSULTS": "0"},
        )
        # Windows selectors support sockets, not subprocess pipes. A reader
        # thread preserves the response timeout on every supported platform.
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self._read_lines, daemon=True)
        self.reader.start()
        self.next_id = 1

    def _read_lines(self):
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    break
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def send(self, obj):
        self.raw(json.dumps(obj, ensure_ascii=False))

    def raw(self, line):
        self.proc.stdin.write((line + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def receive(self):
        try:
            line = self.lines.get(timeout=10)
        except queue.Empty as exc:
            raise AssertionError("Timed out waiting for JSON-RPC response") from exc
        if line is None:
            raise AssertionError("Server stdout closed before a response")
        try:
            result = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise AssertionError(f"Non-protocol stdout: {line[:300]!r}") from exc
        assert isinstance(result, dict), result
        assert result.get("jsonrpc") == "2.0", result
        return result

    def call(self, method, params=None):
        request_id = self.next_id
        self.next_id += 1
        request = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        self.send(request)
        response = self.receive()
        assert response.get("id") == request_id, response
        assert ("result" in response) != ("error" in response), response
        return response

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        self.reader.join(timeout=3)
        self.proc.stdout.close()
        self.stderr.seek(0)
        logs = self.stderr.read()
        self.stderr.close()
        return logs


def rejected(response):
    return "error" in response or response.get("result", {}).get("isError") is True


def run(server, cases, hub_source=None):
    with tempfile.TemporaryDirectory(prefix="collab-mcp-test-") as folder:
        sandbox = Path(folder)
        for module in server.parent.glob("*.py"):
            shutil.copy2(module, sandbox / module.name)
        if (server.parent / "assets").is_dir():
            shutil.copytree(server.parent / "assets", sandbox / "assets")
        if hub_source:
            shutil.copy2(hub_source, sandbox / "collab.py")
        client = Client(sandbox / server.name, sandbox)
        checks = []
        try:
            initialized = client.call("initialize", {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "collab-isolated-smoke", "version": "1.0"},
            })
            init = initialized.get("result", {})
            assert init.get("protocolVersion") in {
                "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"
            }, initialized
            assert "tools" in init.get("capabilities", {}), initialized
            assert init.get("serverInfo", {}).get("name"), initialized
            checks.append("initialize negotiates protocol and tools")

            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            assert client.call("ping").get("result") == {}, "Ping failed or notification produced a response"
            checks.append("initialized notification is silent; ping responds")

            listing = client.call("tools/list")
            tools = listing.get("result", {}).get("tools")
            assert isinstance(tools, list) and tools, listing
            names = [tool.get("name") for tool in tools]
            assert len(names) == len(set(names)), "Duplicate tool names"
            for tool in tools:
                assert isinstance(tool.get("name"), str) and tool["name"], tool
                schema = tool.get("inputSchema")
                assert isinstance(schema, dict) and schema.get("type") == "object", tool
            checks.append(f"tools/list advertises {len(tools)} object schemas")

            unknown = client.call("tools/call", {"name": "does_not_exist_7f3f", "arguments": {}})
            assert rejected(unknown), unknown
            checks.append("unknown tool rejected")

            required_tool = next((tool for tool in tools if tool["inputSchema"].get("required")), None)
            assert required_tool, "Expected at least one tool with required arguments"
            missing = client.call("tools/call", {"name": required_tool["name"], "arguments": {}})
            assert rejected(missing), missing
            checks.append("required arguments validated")

            primitive = client.call("tools/call", {"name": required_tool["name"], "arguments": "invalid"})
            assert rejected(primitive), primitive
            checks.append("non-object arguments rejected")

            bad_method = client.call("method/does-not-exist")
            assert bad_method.get("error", {}).get("code") == -32601, bad_method
            checks.append("unknown method returns JSON-RPC method-not-found")

            client.raw("{ malformed JSON")
            malformed = client.receive()
            assert malformed.get("error", {}).get("code") == -32700, malformed
            assert malformed.get("id") is None, malformed
            checks.append("malformed JSON returns parse error")

            for case in cases:
                payload = {k: v for k, v in case.items() if k != "expect_error"}
                result = client.call("tools/call", payload)
                if case.get("expect_error"):
                    assert rejected(result), {"case": case, "response": result}
                    checks.append(f"tool error handled: {case['name']}")
                    continue
                assert not rejected(result), {"case": case, "response": result}
                content = result.get("result", {}).get("content")
                assert isinstance(content, list), result
                checks.append(f"tool call succeeds: {case['name']}")

            # Ensure an earlier rejected call did not terminate the server.
            assert client.call("ping").get("result") == {}
            checks.append("server remains responsive after errors")
            return {"ok": True, "checks": checks, "tools": names,
                    "protocolVersion": init["protocolVersion"], "isolated": True}
        finally:
            logs = client.close()
            if logs:
                print(logs, file=sys.stderr, end="")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("server", type=Path)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--hub-source", type=Path, help="Overlay this collab.py inside the temporary copy")
    args = parser.parse_args()
    server = args.server.resolve(strict=True)
    cases = json.loads(args.cases.read_text(encoding="utf-8")) if args.cases else []
    try:
        result = run(server, cases, args.hub_source)
    except (AssertionError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
