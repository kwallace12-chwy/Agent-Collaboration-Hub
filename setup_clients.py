#!/usr/bin/env python3
"""Generate machine-local client configuration without editing global settings."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from app_paths import CODE_DIR, workspace_root


def shell_command(arguments: list[str]) -> str:
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


def generate(workspace: Path, output: Path, python: str = sys.executable, *,
             allow_consults: bool = False, max_concurrent_consults: int = 2,
             max_consults_per_hour: int = 10) -> list[Path]:
    if max_concurrent_consults < 1 or max_consults_per_hour < 1:
        raise ValueError("Consultation limits must be positive integers")
    workspace = workspace.expanduser().resolve()
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    environment = {"COLLAB_WORKSPACE": str(workspace),
                   "COLLAB_ALLOW_CONSULTS": "1" if allow_consults else "0",
                   "COLLAB_MAX_CONCURRENT_CONSULTS": str(max_concurrent_consults),
                   "COLLAB_MAX_CONSULTS_PER_HOUR": str(max_consults_per_hour)}
    server = {"command": python, "args": [str(CODE_DIR / "mcp_server.py")], "env": environment}
    configs = {
        "claude.mcp.json": {"mcpServers": {"agent-collab": {"type": "stdio", **server}}},
        "antigravity.mcp.json": {"mcpServers": {"agent-collab": server}},
    }

    def hook(client: str, agent: str, event: str) -> dict:
        command = shell_command([python, str(CODE_DIR / "scripts" / "board_hook.py"),
                                 "--workspace", str(workspace), "--client", client,
                                 "--agent", agent, "--event", event])
        return {"hooks": [{"type": "command", "command": command, "timeout": 15}]}

    configs["claude.settings.json"] = {
        "model": "claude-opus-5-5", "effortLevel": "high",
        "env": {"CLAUDE_CODE_EFFORT_LEVEL": "high"},
        "hooks": {event: [hook("claude", "claude", event)]
                  for event in ("SessionStart", "UserPromptSubmit", "Stop")},
    }
    configs["codex.hooks.json"] = {"hooks": {
        event: [hook("codex", "codex", event)]
        for event in ("SessionStart", "UserPromptSubmit", "Stop")}}
    configs["antigravity.hooks.json"] = {"agent-board-alerts": {
        event: [hook("antigravity", "argon-antigravity", event)]
        for event in ("SessionStart", "PreInvocation", "Stop")}}
    written = []
    for name, data in configs.items():
        target = output / name
        target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        written.append(target)
    toml = output / "codex.toml"
    toml.write_text(
        "[mcp_servers.agent-collab]\n"
        f"command = {json.dumps(python)}\n"
        f"args = [{json.dumps(str(CODE_DIR / 'mcp_server.py'))}]\n"
        "\n[mcp_servers.agent-collab.env]\n" +
        "".join(f"{key} = {json.dumps(value)}\n" for key, value in environment.items()), encoding="utf-8")
    written.append(toml)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=workspace_root())
    parser.add_argument("--output", type=Path, help="Default: WORKSPACE/client-configs")
    parser.add_argument("--allow-consults", action="store_true", help="Opt in to external, potentially paid model consultations")
    parser.add_argument("--max-concurrent-consults", type=int, default=2)
    parser.add_argument("--max-consults-per-hour", type=int, default=10)
    args = parser.parse_args()
    workspace = args.workspace.expanduser().resolve()
    if args.max_concurrent_consults < 1 or args.max_consults_per_hour < 1:
        parser.error("Consultation limits must be positive integers")
    written = generate(workspace, args.output or workspace / "client-configs", allow_consults=args.allow_consults,
                       max_concurrent_consults=args.max_concurrent_consults,
                       max_consults_per_hour=args.max_consults_per_hour)
    print("Generated configuration for workspace: " + str(workspace))
    for path in written:
        print("  " + str(path))
    print("Review and merge the relevant file into your client settings. No global settings were changed.")
    print("Setup instructions: " + str(CODE_DIR / "docs" / "agents.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
