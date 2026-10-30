"""Client configuration and hook integration, isolated from global settings."""
from __future__ import annotations

import configparser
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

# Test subprocesses must never notify the OS or wake a real agent session.
os.environ["COLLAB_DISABLE_EXTERNAL_DELIVERY"] = "1"
os.environ["COLLAB_ALERTS_AUTOSTART"] = "0"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def command_hooks(value):
    """Find executable hook entries independently of client nesting conventions."""
    if isinstance(value, dict):
        if value.get("type") == "command" and isinstance(value.get("command"), str):
            yield value
        for child in value.values():
            yield from command_hooks(child)
    elif isinstance(value, list):
        for child in value:
            yield from command_hooks(child)


class ClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="collab-client-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "private team's board"
        self.output = self.root / "generated configuration"
        self.home = self.root / "unrelated home"
        self.home.mkdir()
        self.global_settings = {
            self.home / ".cursor" / "mcp.json": '{"keep": "cursor"}\n',
            self.home / ".claude" / "settings.json": '{"keep": "claude"}\n',
            self.home / ".codex" / "config.toml": 'keep = "codex"\n',
            self.home / ".gemini" / "antigravity" / "mcp_config.json": '{"keep": "google"}\n',
        }
        for path, content in self.global_settings.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(
            "setup_clients_test_" + uuid.uuid4().hex, ROOT / "setup_clients.py"
        )
        self.setup_clients = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.setup_clients)

    def generate(self):
        with patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}):
            return self.setup_clients.generate(self.workspace, self.output)

    def read_json(self, name):
        return json.loads((self.output / name).read_text(encoding="utf-8"))

    def assert_global_settings_unchanged(self):
        actual_files = {path for path in self.home.rglob("*") if path.is_file()}
        self.assertEqual(actual_files, set(self.global_settings))
        for path, expected in self.global_settings.items():
            self.assertEqual(path.read_text(encoding="utf-8"), expected)

    def test_generates_seven_local_files_without_touching_global_settings(self):
        written = self.generate()
        expected = {
            "cursor.mcp.json",
            "claude.mcp.json", "antigravity.mcp.json", "claude.settings.json",
            "codex.hooks.json", "antigravity.hooks.json", "codex.toml",
        }
        self.assertEqual({path.name for path in written}, expected)
        self.assertEqual(set(self.output.iterdir()), set(written))
        self.assertTrue(all(path.parent == self.output.resolve() for path in written))
        self.assertFalse(self.workspace.exists(), "Generating snippets must not initialize a private board")
        self.assert_global_settings_unchanged()

    def test_every_mcp_client_targets_the_same_workspace_and_existing_server(self):
        self.generate()
        clients = []
        for name in ("cursor.mcp.json", "claude.mcp.json", "antigravity.mcp.json"):
            clients.append(self.read_json(name)["mcpServers"]["agent-collab"])
        # The generated TOML contains JSON-compatible string and array literals.
        # ConfigParser keeps these sections readable on supported Python 3.9/3.10,
        # where tomllib is not yet part of the standard library.
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(self.output / "codex.toml", encoding="utf-8")
        server = parser["mcp_servers.agent-collab"]
        clients.append({
            "command": json.loads(server["command"]),
            "args": json.loads(server["args"]),
            "env": {key: json.loads(value)
                    for key, value in parser["mcp_servers.agent-collab.env"].items()},
        })
        for client in clients:
            with self.subTest(client=client):
                self.assertEqual(client["command"], sys.executable)
                self.assertTrue(Path(client["command"]).is_file())
                self.assertEqual(client["env"]["COLLAB_WORKSPACE"], str(self.workspace.resolve()))
                self.assertEqual(client["env"]["PYTHONUTF8"], "1")
                self.assertEqual(client["env"]["COLLAB_ALLOW_CONSULTS"], "0")
                self.assertEqual(client["env"]["COLLAB_MAX_CONCURRENT_CONSULTS"], "2")
                self.assertEqual(client["env"]["COLLAB_MAX_CONSULTS_PER_HOUR"], "10")
                self.assertEqual(len(client["args"]), 1)
                server_path = Path(client["args"][0])
                self.assertTrue(server_path.is_file())
                self.assertEqual(server_path, ROOT / "mcp_server.py")

    def test_explicit_consult_opt_in_and_limits_are_written_for_every_client(self):
        self.setup_clients.generate(self.workspace, self.output, allow_consults=True,
                                    max_concurrent_consults=1, max_consults_per_hour=3)
        environments = [self.read_json(name)["mcpServers"]["agent-collab"]["env"]
                        for name in ("cursor.mcp.json", "claude.mcp.json", "antigravity.mcp.json")]
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(self.output / "codex.toml", encoding="utf-8")
        environments.append({key: json.loads(value)
                             for key, value in parser["mcp_servers.agent-collab.env"].items()})
        for environment in environments:
            self.assertEqual(environment["COLLAB_ALLOW_CONSULTS"], "1")
            self.assertEqual(environment["COLLAB_MAX_CONCURRENT_CONSULTS"], "1")
            self.assertEqual(environment["COLLAB_MAX_CONSULTS_PER_HOUR"], "3")
        self.assert_global_settings_unchanged()

    def test_invalid_consult_limits_do_not_write_configuration(self):
        for settings in ({"max_concurrent_consults": 0}, {"max_consults_per_hour": -1}):
            with self.assertRaisesRegex(ValueError, "positive"):
                self.setup_clients.generate(self.workspace, self.output, **settings)
        self.assertFalse(self.output.exists())
        self.assert_global_settings_unchanged()

    def test_claude_uses_exact_opus_model_and_high_effort(self):
        self.generate()
        settings = self.read_json("claude.settings.json")
        self.assertEqual(settings["model"], "claude-opus-5-5")
        self.assertEqual(settings["effortLevel"], "high")
        self.assertEqual(settings["env"]["CLAUDE_CODE_EFFORT_LEVEL"], "high")

    def test_hook_commands_keep_workspace_with_spaces_and_apostrophe_intact(self):
        self.generate()
        for filename, client in (("claude.settings.json", "claude"),
                                 ("codex.hooks.json", "codex"),
                                 ("antigravity.hooks.json", "antigravity")):
            hooks = list(command_hooks(self.read_json(filename)))
            self.assertEqual(len(hooks), 3)
            for hook in hooks:
                with self.subTest(filename=filename, command=hook["command"]):
                    arguments = shlex.split(hook["command"], posix=os.name != "nt")
                    if os.name == "nt":
                        arguments = [arg.strip('"') for arg in arguments]
                    self.assertEqual(arguments[0], sys.executable)
                    self.assertEqual(Path(arguments[1]), ROOT / "scripts" / "board_hook.py")
                    self.assertTrue(Path(arguments[1]).is_file())
                    self.assertEqual(arguments[arguments.index("--workspace") + 1], str(self.workspace.resolve()))
                    self.assertEqual(arguments[arguments.index("--client") + 1], client)
                    self.assertIn("--agent", arguments)
                    self.assertIn("--event", arguments)
                    self.assertGreater(hook["timeout"], 0)

    def test_hook_reads_stdin_and_writes_only_the_explicit_workspace(self):
        unrelated = self.root / "unrelated working directory"
        unrelated.mkdir()
        sentinel = unrelated / "keep.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        environment_workspace = self.root / "wrong inherited workspace"
        environment = {
            **os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home),
            "COLLAB_WORKSPACE": str(environment_workspace), "COLLAB_HEADLESS": "0",
            "COLLAB_ALERTS_AUTOSTART": "0",
        }
        result = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "board_hook.py"),
            "--workspace", str(self.workspace), "--client", "codex",
            "--agent", "codex", "--event", "SessionStart",
        ], input=json.dumps({"session_id": "test-session-from-stdin", "model": "test-model"}),
            text=True, capture_output=True, cwd=unrelated, env=environment, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        output = response["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "SessionStart")
        self.assertIn(str(self.workspace.resolve()), output["additionalContext"])
        database = self.workspace / "state" / "alerts.db"
        self.assertTrue(database.is_file())
        with closing(sqlite3.connect(str(database))) as connection:
            target, model = connection.execute(
                "SELECT target, model FROM targets WHERE agent = 'codex'"
            ).fetchone()
        self.assertEqual(target, "test-session-from-stdin")
        self.assertEqual(model, "test-model")
        self.assertFalse(environment_workspace.exists())
        self.assertEqual(list(unrelated.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assert_global_settings_unchanged()


if __name__ == "__main__":
    unittest.main()
