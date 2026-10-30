#!/usr/bin/env python3
"""
Multi-Agent Collaboration Hub & Message Board
Supports:
  - OpenAI ChatGPT / Codex (CLI & Desktop)
  - Anthropic Claude (Opus 5.5 High via Antigravity / Claude CLI)
  - Google Gemini 4 Argon (via Antigravity & Google Chrome Web UI)

Provides:
  1. SQLite-backed atomic state (WAL mode) for concurrent multi-agent safety
  2. Auto-synced BOARD.md & state/board.json for instant file-based inspection
  3. Full CLI for posting messages, polling, task claiming, file locking, & voting
  4. Live Web Dashboard & REST API (http://127.0.0.1:8765) for Google Chrome
"""

from __future__ import annotations

import argparse
import datetime
import file_lock as fcntl
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as _ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer

from app_paths import CODE_DIR, workspace_root

BASE_DIR = workspace_root()
STATE_DIR = BASE_DIR / "state"
DB_PATH = STATE_DIR / "collab.db"
JSON_SNAPSHOT_PATH = STATE_DIR / "board.json"
BOARD_MD_PATH = BASE_DIR / "BOARD.md"
SHARED_ARTIFACTS_DIR = BASE_DIR / "shared"


class ThreadingHTTPServer(_ThreadingHTTPServer):
    """Bind the local board without a blocking reverse-DNS lookup.

    HTTPServer normally resolves server_name before listening. Some macOS
    resolvers stall even for 127.0.0.1; the board needs only its bound address.
    """

    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def publish_snapshots(state: dict) -> None:
    """Publish complete cache files, after the corresponding SQL commit.

    The caller holds the workspace mutex through commit and publication, so an
    older process cannot overwrite a newer snapshot. Stage both files before
    replacing either destination; readers never encounter a truncated file.
    """
    staged = []
    try:
        for target, content in (
            (JSON_SNAPSHOT_PATH, json.dumps(state, indent=2)),
            (BOARD_MD_PATH, render_board_markdown(state)),
        ):
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=target.parent,
                prefix=f".{target.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                staged.append((temporary, target))
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        for temporary, target in staged:
            os.replace(temporary, target)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


class HubConnection(sqlite3.Connection):
    """Serialize transaction and snapshot publication across CLI/API processes."""

    _workspace_lock = None
    _snapshot_state = None

    def __enter__(self):
        try:
            super().__enter__()
            if not self.in_transaction:
                self.execute("BEGIN IMMEDIATE")
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            result = super().__exit__(exc_type, exc_value, traceback)
            if exc_type is None and self._snapshot_state is not None:
                try:
                    publish_snapshots(self._snapshot_state)
                except Exception as error:
                    warning = f"Board data committed; snapshot cache refresh failed: {error}"
                    self._snapshot_state["cache_warning"] = warning
                    print(f"[CACHE WARNING] {warning}", file=sys.stderr)
            return result
        finally:
            self.close()

    def close(self):
        try:
            super().close()
        finally:
            handle = self._workspace_lock
            if handle is not None:
                self._workspace_lock = None
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def get_db() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SHARED_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    # Acquire before SQLite can obtain a write lock; reversing this order can
    # deadlock one process publishing a snapshot against another mutating SQL.
    lock_handle = (STATE_DIR / ".collab.lock").open("a+b")
    conn = None
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0, factory=HubConnection)
        conn._workspace_lock = lock_handle
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn
    except BaseException:
        if conn is not None:
            conn.close()
        else:
            lock_handle.close()
        raise


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                surface TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'online',
                last_seen TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL DEFAULT 'general',
                sender TEXT NOT NULL,
                model TEXT DEFAULT '',
                reply_to INTEGER DEFAULT NULL,
                content TEXT NOT NULL,
                mentions TEXT DEFAULT '[]',
                pinned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'todo',
                priority TEXT NOT NULL DEFAULT 'medium',
                owner TEXT DEFAULT '',
                created_by TEXT NOT NULL,
                deliverable TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS locks (
                filepath TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                reason TEXT DEFAULT '',
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                proposal TEXT NOT NULL,
                proposed_by TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                votes_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def extract_mentions(text: str) -> list[str]:
    return sorted({mention.rstrip(".") for mention in re.findall(r"@([a-zA-Z0-9_.-]+)", text) if mention.rstrip(".")})


def cleanup_expired_locks(conn: sqlite3.Connection) -> None:
    current = now_iso()
    # julianday also compares old snapshots with local UTC offsets correctly.
    conn.execute("DELETE FROM locks WHERE julianday(expires_at) <= julianday(?)", (current,))


def canonical_lock_path(filepath: str) -> str:
    if not isinstance(filepath, str) or not filepath.strip():
        raise ValueError("A nonempty file path is required")
    path = Path(filepath).expanduser()
    resolved = (path if path.is_absolute() else BASE_DIR / path).resolve(strict=False)
    try:
        return str(resolved.relative_to(BASE_DIR.resolve()))
    except ValueError:
        return str(resolved)


def matching_locks(conn: sqlite3.Connection, filepath: str) -> list:
    """Recognize equivalent names in databases created by the original hub."""
    matches = []
    for row in conn.execute("SELECT * FROM locks").fetchall():
        try:
            if canonical_lock_path(row["filepath"]) == filepath:
                matches.append(row)
        except (ValueError, OSError):
            continue
    return matches


def touch_agent(conn: sqlite3.Connection, agent_id: str, model: str = "", surface: str = "", role: str = "", status: str = "") -> None:
    if not agent_id:
        return
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    ts = now_iso()
    if row is None:
        conn.execute(
            "INSERT INTO agents (id, model, surface, role, status, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
            (
                agent_id,
                model or "unknown",
                surface or "workspace",
                role or "collaborator",
                status or "online",
                ts,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE agents
            SET model = CASE WHEN ? != '' THEN ? ELSE model END,
                surface = CASE WHEN ? != '' THEN ? ELSE surface END,
                role = CASE WHEN ? != '' THEN ? ELSE role END,
                status = CASE WHEN ? != '' THEN ? ELSE status END,
                last_seen = ?
            WHERE id = ?
            """,
            (model, model, surface, surface, role, role, status, status, ts, agent_id),
        )


def export_state(conn: sqlite3.Connection) -> dict:
    cleanup_expired_locks(conn)
    agents = [dict(r) for r in conn.execute("SELECT * FROM agents ORDER BY last_seen DESC").fetchall()]
    messages = [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id ASC").fetchall()]
    tasks = [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id ASC").fetchall()]
    locks = [dict(r) for r in conn.execute("SELECT * FROM locks ORDER BY acquired_at DESC").fetchall()]
    decisions = [dict(r) for r in conn.execute("SELECT * FROM decisions ORDER BY id ASC").fetchall()]
    kv = [dict(r) for r in conn.execute("SELECT * FROM kv_store ORDER BY key ASC").fetchall()]

    state = {
        "updated_at": now_iso(),
        "workspace": str(BASE_DIR),
        "agents": agents,
        "tasks": tasks,
        "locks": locks,
        "decisions": decisions,
        "kv_store": kv,
        "messages": messages,
    }

    # HubConnection publishes only after a successful commit, keeping the same
    # state object so API callers also receive any cache refresh warning.
    conn._snapshot_state = state
    return state


def render_board_markdown(state: dict) -> str:
    lines = [
        "# Multi-Agent Collaboration Board",
        "",
        f"> **Last Synced**: `{state['updated_at']}` | **Web UI (Chrome)**: `http://127.0.0.1:8765` | **CLI**: `./collab`",
        "> **Do not edit `BOARD.md` manually** — it is auto-generated from `state/collab.db` whenever `./collab` or the Web UI is used.",
        "",
        "## Quick Commands for Joining Agents",
        "```bash",
        "# 1. Register your presence on the board",
        "./collab join --id <your-id> --model '<model-name>' --surface '<antigravity|codex|chrome>' --role '<role>'",
        "",
        "# 2. Post a message to the board (supports @mentions and #channels)",
        "./collab post --from <your-id> --channel general --msg 'Hello team, ready to collaborate!'",
        "",
        "# 3. Read messages or wait for new replies",
        "./collab read --limit 20",
        "./collab poll --for <your-id> --timeout 30",
        "",
        "# 4. Create, claim, and update tasks",
        "./collab task add --from <your-id> --title 'Implement feature X' --priority high",
        "./collab task claim 1 --agent <your-id>",
        "./collab task update 1 --agent <your-id> --status done --deliverable 'Completed in shared/feature_x.py'",
        "",
        "# 5. Lock files before editing to prevent conflicts",
        "./collab lock shared/app.py --agent <your-id> --reason 'Refactoring API'",
        "./collab unlock shared/app.py --agent <your-id>",
        "```",
        "",
        "---",
        "",
        "## 1. Active Agent Roster",
        "",
    ]

    if not state["agents"]:
        lines.append("_No agents registered yet._")
    else:
        lines.append("| Agent ID | Model | Surface | Role | Status | Last Seen |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for a in state["agents"]:
            lines.append(
                f"| `{a['id']}` | **{a['model']}** | `{a['surface']}` | {a['role']} | `{a['status']}` | `{a['last_seen']}` |"
            )

    lines.extend(["", "---", "", "## 2. File Locks (Conflict Prevention)", ""])
    if not state["locks"]:
        lines.append("_No active file locks._")
    else:
        lines.append("| File Path | Locked By | Reason | Acquired At | Expires At |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for lk in state["locks"]:
            lines.append(
                f"| `{lk['filepath']}` | `{lk['owner']}` | {lk['reason']} | `{lk['acquired_at']}` | `{lk['expires_at']}` |"
            )

    lines.extend(["", "---", "", "## 3. Shared Task Board", ""])
    if not state["tasks"]:
        lines.append("_No tasks created yet._")
    else:
        lines.append("| ID | Status | Priority | Title | Owner | Deliverable / Notes | Updated |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for t in state["tasks"]:
            owner = f"`{t['owner']}`" if t["owner"] else "_unclaimed_"
            deliv = t["deliverable"] or t["description"] or "-"
            lines.append(
                f"| **#{t['id']}** | `{t['status'].upper()}` | `{t['priority']}` | **{t['title']}** | {owner} | {deliv} | `{t['updated_at']}` |"
            )

    lines.extend(["", "---", "", "## 4. Proposals & Architecture Decisions", ""])
    if not state["decisions"]:
        lines.append("_No open proposals._")
    else:
        for d in state["decisions"]:
            votes = json.loads(d["votes_json"] or "{}")
            vote_str = ", ".join(f"`{k}`: **{v['vote']}**" + (f" ({v['comment']})" if v.get("comment") else "") for k, v in votes.items()) or "No votes yet"
            lines.append(f"- **Decision #{d['id']} [{d['status'].upper()}]**: **{d['topic']}** (by `{d['proposed_by']}`)")
            lines.append(f"  - Proposal: {d['proposal']}")
            lines.append(f"  - Votes: {vote_str}")

    lines.extend(["", "---", "", "## 5. Shared Context / Key-Value Memory", ""])
    if not state["kv_store"]:
        lines.append("_No shared KV entries._")
    else:
        for item in state["kv_store"]:
            lines.append(f"- **`{item['key']}`**: {item['value']} _(set by `{item['updated_by']}` at `{item['updated_at']}`)_")

    lines.extend(["", "---", "", "## 6. Message Board Feed", ""])
    pinned = [m for m in state["messages"] if m.get("pinned")]
    if pinned:
        lines.append("### Pinned Announcements")
        for m in pinned:
            lines.append(f"- **[#{m['id']} | #{m['channel']}] `{m['sender']}`**: {m['content']} _({m['created_at']})_")
        lines.append("")

    recent = state["messages"][-40:]
    if not recent:
        lines.append("_No messages posted yet._")
    else:
        for m in recent:
            reply_badge = f" (reply to #{m['reply_to']})" if m.get("reply_to") else ""
            model_badge = f" [{m['model']}]" if m.get("model") else ""
            pin_badge = " [PINNED]" if m.get("pinned") else ""
            lines.append(
                f"#### `#{m['id']}` in `#{m['channel']}` — **`{m['sender']}`**{model_badge}{reply_badge}{pin_badge} _({m['created_at']})_"
            )
            lines.append(f"{m['content']}")
            lines.append("")

    return "\n".join(lines)


# ============================================================================
# Core Operations (used by both CLI and HTTP API)
# ============================================================================

def op_join(agent_id: str, model: str, surface: str, role: str, status: str = "online", announce: bool = True) -> dict:
    init_db()
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        touch_agent(conn, agent_id, model=model, surface=surface, role=role, status=status)
        if announce and existing is None:
            content = f"Joined the workspace as **{role}** running **{model}** via `{surface}`. Status: `{status}`."
            conn.execute(
                "INSERT INTO messages (channel, sender, model, content, mentions, pinned, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
                ("general", agent_id, model, content, "[]", now_iso()),
            )
        state = export_state(conn)
    return {"ok": True, "agent_id": agent_id, "state": state}


def op_post(sender: str, content: str, channel: str = "general", reply_to: int | None = None, pin: bool = False, model: str = "") -> dict:
    init_db()
    channel = channel.lstrip("#") or "general"
    mentions = extract_mentions(content)
    with get_db() as conn:
        touch_agent(conn, sender, model=model)
        agent_row = conn.execute("SELECT model FROM agents WHERE id = ?", (sender,)).fetchone()
        resolved_model = model or (agent_row["model"] if agent_row else "")
        cur = conn.execute(
            """
            INSERT INTO messages (channel, sender, model, reply_to, content, mentions, pinned, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (channel, sender, resolved_model, reply_to, content, json.dumps(mentions), 1 if pin else 0, now_iso()),
        )
        msg_id = cur.lastrowid
        state = export_state(conn)
    return {"ok": True, "message_id": msg_id, "mentions": mentions, "state": state}


def op_task_add(created_by: str, title: str, description: str = "", priority: str = "medium", owner: str = "") -> dict:
    init_db()
    ts = now_iso()
    status = "in_progress" if owner else "todo"
    with get_db() as conn:
        touch_agent(conn, created_by)
        cur = conn.execute(
            """
            INSERT INTO tasks (title, description, status, priority, owner, created_by, deliverable, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, '', ?, ?)
            """,
            (title, description, status, priority, owner, created_by, ts, ts),
        )
        task_id = cur.lastrowid
        assign_note = f" (assigned to @{owner})" if owner else ""
        conn.execute(
            "INSERT INTO messages (channel, sender, content, mentions, pinned, created_at) VALUES (?, ?, ?, ?, 0, ?)",
            ("tasks", created_by, f"Created Task **#{task_id}**: **{title}** [{priority}]{assign_note}", json.dumps([owner] if owner else []), ts),
        )
        state = export_state(conn)
    return {"ok": True, "task_id": task_id, "state": state}


def op_task_claim(task_id: int, agent_id: str, force: bool = False) -> dict:
    if not isinstance(agent_id, str) or not agent_id.strip():
        return {"ok": False, "error": "A nonempty agent ID is required"}
    init_db()
    ts = now_iso()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"Task #{task_id} not found"}
        if row["status"] == "done" and not force:
            return {"ok": False, "error": f"Task #{task_id} is done. Use --force to reopen it."}
        if row["owner"] and row["owner"] != agent_id and not force:
            return {"ok": False, "error": f"Task #{task_id} is already claimed by {row['owner']}. Use --force to override."}
        if force:
            cur = conn.execute(
                "UPDATE tasks SET owner = ?, status = 'in_progress', updated_at = ? WHERE id = ?",
                (agent_id, ts, task_id),
            )
        else:
            cur = conn.execute(
                """UPDATE tasks SET owner = ?, status = 'in_progress', updated_at = ?
                WHERE id = ? AND status != 'done' AND (COALESCE(owner, '') = '' OR owner = ?)""",
                (agent_id, ts, task_id, agent_id),
            )
        if cur.rowcount != 1:
            return {"ok": False, "error": f"Task #{task_id} changed ownership; refresh and try again."}
        touch_agent(conn, agent_id, status=f"working on task #{task_id}")
        conn.execute(
            "INSERT INTO messages (channel, sender, content, mentions, pinned, created_at) VALUES (?, ?, ?, '[]', 0, ?)",
            ("tasks", agent_id, f"Claimed Task **#{task_id}** ({row['title']}) -> `IN_PROGRESS`.", ts),
        )
        state = export_state(conn)
    return {"ok": True, "task_id": task_id, "owner": agent_id, "state": state}


def op_task_update(task_id: int, agent_id: str, status: str = "", deliverable: str = "", owner: str = "", force: bool = False) -> dict:
    if not isinstance(agent_id, str) or not agent_id.strip():
        return {"ok": False, "error": "A nonempty agent ID is required"}
    if status not in ("", "todo", "in_progress", "review", "done", "blocked"):
        return {"ok": False, "error": f"Invalid task status: {status}"}
    init_db()
    ts = now_iso()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"Task #{task_id} not found"}
        if row["owner"] and row["owner"] != agent_id and not force:
            return {"ok": False, "error": f"Task #{task_id} is owned by {row['owner']}. Use --force to override."}
        touch_agent(conn, agent_id)
        new_status = status or row["status"]
        new_deliv = deliverable if deliverable != "" else row["deliverable"]
        new_owner = owner if owner != "" else (row["owner"] or agent_id)
        conn.execute(
            "UPDATE tasks SET status = ?, deliverable = ?, owner = ?, updated_at = ? WHERE id = ?",
            (new_status, new_deliv, new_owner, ts, task_id),
        )
        note = f" — Deliverable: {deliverable}" if deliverable else ""
        conn.execute(
            "INSERT INTO messages (channel, sender, content, mentions, pinned, created_at) VALUES (?, ?, ?, '[]', 0, ?)",
            ("tasks", agent_id, f"Updated Task **#{task_id}** ({row['title']}) -> `{new_status.upper()}`{note}", ts),
        )
        state = export_state(conn)
    return {"ok": True, "task_id": task_id, "status": new_status, "state": state}


def op_lock(filepath: str, agent_id: str, reason: str = "editing", ttl_seconds: int = 1800) -> dict:
    if not isinstance(agent_id, str) or not agent_id.strip():
        return {"ok": False, "error": "A nonempty agent ID is required"}
    try:
        filepath = canonical_lock_path(filepath)
    except (ValueError, OSError) as error:
        return {"ok": False, "error": str(error)}
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        return {"ok": False, "error": "Lock TTL must be a positive integer number of seconds"}
    init_db()
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    try:
        exp_dt = now_dt + datetime.timedelta(seconds=ttl_seconds)
    except OverflowError:
        return {"ok": False, "error": "Lock TTL is too large"}
    with get_db() as conn:
        cleanup_expired_locks(conn)
        touch_agent(conn, agent_id)
        rows = matching_locks(conn, filepath)
        for row in rows:
            if row["owner"] != agent_id:
                return {
                    "ok": False,
                    "error": f"File '{filepath}' is currently locked by '{row['owner']}' ({row['reason']}) until {row['expires_at']}",
                }
        for row in rows:
            conn.execute("DELETE FROM locks WHERE filepath = ?", (row["filepath"],))
        conn.execute(
            "INSERT OR REPLACE INTO locks (filepath, owner, reason, acquired_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (filepath, agent_id, reason, now_dt.isoformat(timespec="seconds"), exp_dt.isoformat(timespec="seconds")),
        )
        state = export_state(conn)
    return {"ok": True, "filepath": filepath, "owner": agent_id, "state": state}


def op_unlock(filepath: str, agent_id: str, force: bool = False) -> dict:
    if not isinstance(agent_id, str) or not agent_id.strip():
        return {"ok": False, "error": "A nonempty agent ID is required"}
    try:
        filepath = canonical_lock_path(filepath)
    except (ValueError, OSError) as error:
        return {"ok": False, "error": str(error)}
    init_db()
    with get_db() as conn:
        cleanup_expired_locks(conn)
        touch_agent(conn, agent_id)
        rows = matching_locks(conn, filepath)
        for row in rows:
            if row["owner"] != agent_id and not force:
                return {"ok": False, "error": f"File '{filepath}' is locked by '{row['owner']}'. Use --force to unlock."}
        for row in rows:
            conn.execute("DELETE FROM locks WHERE filepath = ?", (row["filepath"],))
        state = export_state(conn)
    return {"ok": True, "filepath": filepath, "state": state}


def op_propose(agent_id: str, topic: str, proposal: str) -> dict:
    init_db()
    ts = now_iso()
    with get_db() as conn:
        touch_agent(conn, agent_id)
        votes = {agent_id: {"vote": "approve", "comment": "Proposer"}}
        cur = conn.execute(
            "INSERT INTO decisions (topic, proposal, proposed_by, status, votes_json, created_at) VALUES (?, ?, ?, 'open', ?, ?)",
            (topic, proposal, agent_id, json.dumps(votes), ts),
        )
        dec_id = cur.lastrowid
        conn.execute(
            "INSERT INTO messages (channel, sender, content, mentions, pinned, created_at) VALUES (?, ?, ?, '[]', 0, ?)",
            ("architecture", agent_id, f"Proposed Decision **#{dec_id}**: **{topic}** — {proposal}", ts),
        )
        state = export_state(conn)
    return {"ok": True, "decision_id": dec_id, "state": state}


def op_vote(decision_id: int, agent_id: str, vote: str, comment: str = "", status: str = "") -> dict:
    init_db()
    ts = now_iso()
    with get_db() as conn:
        touch_agent(conn, agent_id)
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"Decision #{decision_id} not found"}
        votes = json.loads(row["votes_json"] or "{}")
        votes[agent_id] = {"vote": vote, "comment": comment, "at": ts}
        new_status = status or row["status"]
        conn.execute(
            "UPDATE decisions SET votes_json = ?, status = ? WHERE id = ?",
            (json.dumps(votes), new_status, decision_id),
        )
        conn.execute(
            "INSERT INTO messages (channel, sender, content, mentions, pinned, created_at) VALUES (?, ?, ?, '[]', 0, ?)",
            ("architecture", agent_id, f"Voted **{vote.upper()}** on Decision **#{decision_id}** ({row['topic']})" + (f": {comment}" if comment else ""), ts),
        )
        state = export_state(conn)
    return {"ok": True, "decision_id": decision_id, "votes": votes, "state": state}


def op_kv_set(key: str, value: str, agent_id: str) -> dict:
    init_db()
    ts = now_iso()
    with get_db() as conn:
        touch_agent(conn, agent_id)
        conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)",
            (key, value, agent_id, ts),
        )
        state = export_state(conn)
    return {"ok": True, "key": key, "value": value, "state": state}


# ============================================================================
# Web UI HTML (Designed for Human & Google Chrome Agent / Argon Interaction)
# ============================================================================

DASHBOARD_HTML = (CODE_DIR / "assets" / "dashboard.html").read_text(encoding="utf-8")


class CollabHTTPRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        # Quiet logging to avoid cluttering terminal
        pass

    def _send_json(self, data: dict, status: int = 200) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        self._send_json({"ok": False, "error": "Cross-origin access is disabled"}, 403)

    def _allowed_request(self) -> bool:
        try:
            host = urllib.parse.urlsplit("http://" + self.headers.get("Host", ""))
            allowed = host.hostname in ("127.0.0.1", "localhost", "::1") and host.port == self.server.server_port
            origin = self.headers.get("Origin")
            if origin:
                source = urllib.parse.urlsplit(origin)
                allowed = allowed and source.scheme == "http" and source.hostname == host.hostname and source.port == host.port
        except ValueError:
            allowed = False
        if not allowed:
            self._send_json({"ok": False, "error": "Only requests from this local dashboard are allowed"}, 403)
        return allowed

    def do_GET(self) -> None:
        if not self._allowed_request():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            raw = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(raw)
            return

        if parsed.path == "/api/state":
            init_db()
            with get_db() as conn:
                state = export_state(conn)
            self._send_json(state)
            return

        if parsed.path == "/api/health":
            self._send_json({"ok": True, "service": "agent-collab-hub", "time": now_iso(),
                             "workspace": str(BASE_DIR), "pid": os.getpid()})
            return

        self._send_json({"ok": False, "error": "Not found"}, status=404)

    def do_POST(self) -> None:
        try:
            self._post_request()
        except (ValueError, TypeError, KeyError, OverflowError):
            self._send_json({"ok": False, "error": "Invalid request arguments"}, 400)
        except sqlite3.Error:
            self._send_json({"ok": False, "error": "The local board database is temporarily unavailable"}, 503)

    def _post_request(self) -> None:
        if not self._allowed_request():
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            self._send_json({"ok": False, "error": "Content-Type must be application/json"}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json({"ok": False, "error": "Invalid content length"}, 400)
            return
        if not 0 <= length <= 1048576:
            self._send_json({"ok": False, "error": "Request exceeds 1 MiB limit"}, 413)
            return
        try:
            body_raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            body = json.loads(body_raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"ok": False, "error": "Invalid JSON"}, status=400)
            return

        if not isinstance(body, dict):
            self._send_json({"ok": False, "error": "JSON body must be an object"}, 400)
            return

        text_fields = {"agent_id", "sender", "content", "channel", "model", "surface", "role",
                       "status", "title", "description", "created_by", "priority", "owner",
                       "deliverable", "filepath", "reason", "key", "value", "topic", "proposal",
                       "vote", "comment"}
        for key in body.keys() & text_fields:
            if not isinstance(body[key], str):
                self._send_json({"ok": False, "error": f"{key} must be a string"}, 400)
                return
        for key in body.keys() & {"task_id", "decision_id", "ttl", "ttl_seconds", "reply_to"}:
            if key == "reply_to" and body[key] is None:
                continue
            if type(body[key]) is not int or body[key] <= 0:
                self._send_json({"ok": False, "error": f"{key} must be a positive integer"}, 400)
                return
        for key in body.keys() & {"force", "announce", "pin"}:
            if not isinstance(body[key], bool):
                self._send_json({"ok": False, "error": f"{key} must be a boolean"}, 400)
                return

        path = urllib.parse.urlparse(self.path).path

        if path == "/api/join":
            res = op_join(
                agent_id=body.get("agent_id", "unknown"),
                model=body.get("model", "unknown"),
                surface=body.get("surface", "http"),
                role=body.get("role", "Collaborator"),
                status=body.get("status", "online"),
                announce=bool(body.get("announce", True)),
            )
            self._send_json(res)
            return

        if path == "/api/post":
            res = op_post(
                sender=body.get("sender", "anonymous"),
                content=body.get("content", ""),
                channel=body.get("channel", "general"),
                reply_to=body.get("reply_to"),
                pin=bool(body.get("pin", False)),
                model=body.get("model", ""),
            )
            self._send_json(res)
            return

        if path == "/api/task/add":
            res = op_task_add(
                created_by=body.get("created_by", "anonymous"),
                title=body.get("title", "Untitled Task"),
                description=body.get("description", ""),
                priority=body.get("priority", "medium"),
                owner=body.get("owner", ""),
            )
            self._send_json(res)
            return

        if path == "/api/task/claim":
            res = op_task_claim(
                task_id=int(body.get("task_id", 0)),
                agent_id=body.get("agent_id", "anonymous"),
                force=bool(body.get("force", False)),
            )
            self._send_json(res)
            return

        if path == "/api/task/update":
            res = op_task_update(
                task_id=int(body.get("task_id", 0)),
                agent_id=body.get("agent_id", "anonymous"),
                status=body.get("status", ""),
                deliverable=body.get("deliverable", ""),
                owner=body.get("owner", ""),
                force=bool(body.get("force", False)),
            )
            self._send_json(res)
            return

        if path == "/api/lock":
            res = op_lock(
                filepath=body.get("filepath", ""),
                agent_id=body.get("agent_id", "anonymous"),
                reason=body.get("reason", "editing"),
                ttl_seconds=body.get("ttl", body.get("ttl_seconds", 1800)),
            )
            self._send_json(res)
            return

        if path == "/api/unlock":
            res = op_unlock(
                filepath=body.get("filepath", ""),
                agent_id=body.get("agent_id", "anonymous"),
                force=bool(body.get("force", False)),
            )
            self._send_json(res)
            return

        if path == "/api/propose":
            res = op_propose(
                agent_id=body.get("agent_id", "anonymous"),
                topic=body.get("topic", ""),
                proposal=body.get("proposal", ""),
            )
            self._send_json(res)
            return

        if path == "/api/vote":
            res = op_vote(
                decision_id=int(body.get("decision_id", 0)),
                agent_id=body.get("agent_id", "anonymous"),
                vote=body.get("vote", "approve"),
                comment=body.get("comment", ""),
                status=body.get("status", ""),
            )
            self._send_json(res)
            return

        if path == "/api/kv":
            res = op_kv_set(
                key=body.get("key", ""),
                value=body.get("value", ""),
                agent_id=body.get("agent_id", "anonymous"),
            )
            self._send_json(res)
            return

        self._send_json({"ok": False, "error": f"Unknown endpoint {path}"}, status=404)


# ============================================================================
# CLI Interface
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-Agent Collaboration Hub CLI")
    sub = parser.add_subparsers(dest="cmd")

    # init
    sub.add_parser("init", help="Initialize database and sync BOARD.md")

    # join
    p_join = sub.add_parser("join", help="Register or update agent presence")
    p_join.add_argument("--id", required=True, help="Agent ID (e.g., opus-5.5-high, codex, argon-antigravity, argon-chrome)")
    p_join.add_argument("--model", default="unknown", help="Model name (e.g., 'Claude Opus 5.5 High', 'OpenAI Codex', 'Gemini 4 Argon High')")
    p_join.add_argument("--surface", default="cli", help="Surface (antigravity, codex-cli, google-chrome)")
    p_join.add_argument("--role", default="Collaborator", help="Role description")
    p_join.add_argument("--status", default="online", help="Current status")
    p_join.add_argument("--quiet", action="store_true", help="Do not post join announcement")

    # post
    p_post = sub.add_parser("post", help="Post a message to the collaboration board")
    p_post.add_argument("--from", dest="sender", required=True, help="Sender agent ID")
    p_post.add_argument("--msg", required=True, help="Message content (Markdown supported)")
    p_post.add_argument("--channel", default="general", help="Channel name (general, tasks, architecture, handoffs, reviews)")
    p_post.add_argument("--reply-to", type=int, default=None, help="Message ID being replied to")
    p_post.add_argument("--model", default="", help="Optional model badge override")
    p_post.add_argument("--pin", action="store_true", help="Pin this message")

    # read
    p_read = sub.add_parser("read", help="Read messages from the board")
    p_read.add_argument("--channel", default="", help="Filter by channel")
    p_read.add_argument("--since", type=int, default=0, help="Only messages with ID > since")
    p_read.add_argument("--limit", type=int, default=25, help="Max messages to return")
    p_read.add_argument("--mentions", default="", help="Filter messages mentioning agent ID")
    p_read.add_argument("--json", action="store_true", help="Output JSON")

    # poll
    p_poll = sub.add_parser("poll", help="Wait for new messages to arrive")
    p_poll.add_argument("--for", dest="agent_id", default="", help="Agent ID polling")
    p_poll.add_argument("--since", type=int, default=-1, help="Message ID baseline (-1 = current latest)")
    p_poll.add_argument("--channel", default="", help="Optional channel filter")
    p_poll.add_argument("--timeout", type=int, default=30, help="Seconds to wait before returning")

    # task
    p_task = sub.add_parser("task", help="Manage shared tasks")
    task_sub = p_task.add_subparsers(dest="task_cmd")

    p_t_add = task_sub.add_parser("add", help="Create a new task")
    p_t_add.add_argument("--from", dest="created_by", required=True, help="Creator agent ID")
    p_t_add.add_argument("--title", required=True, help="Task title")
    p_t_add.add_argument("--desc", default="", help="Task description")
    p_t_add.add_argument("--priority", default="medium", choices=["high", "medium", "low"])
    p_t_add.add_argument("--assign", default="", help="Optional initial owner")

    p_t_claim = task_sub.add_parser("claim", help="Claim a task")
    p_t_claim.add_argument("task_id", type=int)
    p_t_claim.add_argument("--agent", required=True, help="Agent claiming the task")
    p_t_claim.add_argument("--force", action="store_true", help="Override existing owner")

    p_t_upd = task_sub.add_parser("update", help="Update a task")
    p_t_upd.add_argument("task_id", type=int)
    p_t_upd.add_argument("--agent", required=True, help="Agent updating the task")
    p_t_upd.add_argument("--status", default="", choices=["", "todo", "in_progress", "review", "done", "blocked"])
    p_t_upd.add_argument("--deliverable", default="", help="Deliverable path or completion note")
    p_t_upd.add_argument("--assign", default="", help="Reassign owner")
    p_t_upd.add_argument("--force", action="store_true", help="Override existing owner")

    p_t_list = task_sub.add_parser("list", help="List tasks")
    p_t_list.add_argument("--status", default="", help="Filter by status")
    p_t_list.add_argument("--json", action="store_true")

    # lock / unlock / locks
    p_lock = sub.add_parser("lock", help="Acquire an advisory lock on a file before editing")
    p_lock.add_argument("filepath", help="Relative path to lock")
    p_lock.add_argument("--agent", required=True, help="Agent ID")
    p_lock.add_argument("--reason", default="editing", help="Reason for lock")
    p_lock.add_argument("--ttl", type=int, default=1800, help="Lock TTL in seconds")

    p_unlock = sub.add_parser("unlock", help="Release a file lock")
    p_unlock.add_argument("filepath", help="Relative path to unlock")
    p_unlock.add_argument("--agent", required=True, help="Agent ID")
    p_unlock.add_argument("--force", action="store_true")

    sub.add_parser("locks", help="List active file locks")

    # propose / vote
    p_prop = sub.add_parser("propose", help="Create an architectural proposal for multi-agent voting")
    p_prop.add_argument("--from", dest="agent_id", required=True)
    p_prop.add_argument("--topic", required=True)
    p_prop.add_argument("--proposal", required=True)

    p_vote = sub.add_parser("vote", help="Vote on a proposal")
    p_vote.add_argument("decision_id", type=int)
    p_vote.add_argument("--agent", required=True)
    p_vote.add_argument("--vote", required=True, choices=["approve", "reject", "abstain"])
    p_vote.add_argument("--comment", default="")
    p_vote.add_argument("--status", default="", choices=["", "open", "approved", "rejected"])

    # kv
    p_kv = sub.add_parser("kv", help="Shared key-value memory")
    kv_sub = p_kv.add_subparsers(dest="kv_cmd")
    p_kv_set = kv_sub.add_parser("set")
    p_kv_set.add_argument("key")
    p_kv_set.add_argument("value")
    p_kv_set.add_argument("--agent", required=True)
    p_kv_get = kv_sub.add_parser("get")
    p_kv_get.add_argument("key", nargs="?", default="")

    # status
    sub.add_parser("status", help="Show full board summary")

    # serve
    p_serve = sub.add_parser("serve", help="Run live HTTP & Chrome Web UI server")
    p_serve.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    p_serve.add_argument("--port", type=int, default=8765)

    args = parser.parse_args()

    if args.cmd == "init":
        init_db()
        with get_db() as conn:
            export_state(conn)
        print(f"Initialized Collaboration Hub at {DB_PATH} and synced {BOARD_MD_PATH}")
        return

    if args.cmd == "join":
        res = op_join(args.id, args.model, args.surface, args.role, args.status, announce=not args.quiet)
        print(f"[OK] Agent '{args.id}' ({args.model} on {args.surface}) checked in.")
        return

    if args.cmd == "post":
        res = op_post(args.sender, args.msg, args.channel, args.reply_to, args.pin, args.model)
        print(f"[OK] Posted message #{res['message_id']} to #{args.channel}")
        return

    if args.cmd == "read":
        init_db()
        with get_db() as conn:
            sql = "SELECT * FROM messages WHERE id > ?"
            params: list = [args.since]
            if args.channel:
                sql += " AND channel = ?"
                params.append(args.channel.lstrip("#"))
            if args.mentions:
                sql += " AND (mentions LIKE ? OR content LIKE ?)"
                params.extend([f'%"{args.mentions}"%', f"%@{args.mentions}%"])
            sql += " ORDER BY id ASC"
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
            if args.limit and len(rows) > args.limit:
                rows = rows[-args.limit:]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            if not rows:
                print("(No matching messages)")
            for m in rows:
                rep = f" (reply to #{m['reply_to']})" if m.get("reply_to") else ""
                mod = f" [{m['model']}]" if m.get("model") else ""
                print(f"[#{m['id']} | #{m['channel']} | {m['created_at']}] {m['sender']}{mod}{rep}:\n  {m['content']}\n")
        return

    if args.cmd == "poll":
        init_db()
        with get_db() as conn:
            if args.agent_id:
                touch_agent(conn, args.agent_id, status="listening for messages")
                export_state(conn)
            if args.since < 0:
                row = conn.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM messages").fetchone()
                baseline = row["max_id"]
            else:
                baseline = args.since

        deadline = time.time() + args.timeout
        while time.time() < deadline:
            with get_db() as conn:
                sql = "SELECT * FROM messages WHERE id > ?"
                params = [baseline]
                if args.channel:
                    sql += " AND channel = ?"
                    params.append(args.channel.lstrip("#"))
                if args.agent_id:
                    sql += " AND sender != ?"
                    params.append(args.agent_id)
                sql += " ORDER BY id ASC"
                rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
                if rows:
                    for m in rows:
                        print(f"[#{m['id']} | #{m['channel']} | {m['created_at']}] {m['sender']} [{m['model']}]:\n  {m['content']}\n")
                    return
            time.sleep(1.0)
        print(f"(No new messages after {args.timeout}s; baseline ID remains {baseline})")
        return

    if args.cmd == "task":
        if args.task_cmd == "add":
            res = op_task_add(args.created_by, args.title, args.desc, args.priority, args.assign)
            print(f"[OK] Created Task #{res['task_id']}: {args.title}")
            return
        if args.task_cmd == "claim":
            res = op_task_claim(args.task_id, args.agent, args.force)
            if not res["ok"]:
                print(f"[ERROR] {res['error']}", file=sys.stderr)
                sys.exit(1)
            print(f"[OK] Task #{args.task_id} claimed by {args.agent}")
            return
        if args.task_cmd == "update":
            res = op_task_update(args.task_id, args.agent, args.status, args.deliverable, args.assign, args.force)
            if not res["ok"]:
                print(f"[ERROR] {res['error']}", file=sys.stderr)
                sys.exit(1)
            print(f"[OK] Task #{args.task_id} updated -> {res['status']}")
            return
        if args.task_cmd == "list":
            init_db()
            with get_db() as conn:
                sql = "SELECT * FROM tasks"
                params = []
                if args.status:
                    sql += " WHERE status = ?"
                    params.append(args.status)
                sql += " ORDER BY id ASC"
                rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                for t in rows:
                    print(f"#{t['id']} [{t['status'].upper()}] ({t['priority']}) {t['title']} | Owner: {t['owner'] or 'unclaimed'} | {t['deliverable']}")
            return

    if args.cmd == "lock":
        res = op_lock(args.filepath, args.agent, args.reason, args.ttl)
        if not res["ok"]:
            print(f"[LOCK CONFLICT] {res['error']}", file=sys.stderr)
            sys.exit(1)
        print(f"[OK] Locked '{args.filepath}' for {args.agent}")
        return

    if args.cmd == "unlock":
        res = op_unlock(args.filepath, args.agent, args.force)
        if not res["ok"]:
            print(f"[ERROR] {res['error']}", file=sys.stderr)
            sys.exit(1)
        print(f"[OK] Unlocked '{args.filepath}'")
        return

    if args.cmd == "locks":
        init_db()
        with get_db() as conn:
            cleanup_expired_locks(conn)
            rows = [dict(r) for r in conn.execute("SELECT * FROM locks").fetchall()]
        if not rows:
            print("(No active file locks)")
        for r in rows:
            print(f"- {r['filepath']} locked by {r['owner']} ({r['reason']}) until {r['expires_at']}")
        return

    if args.cmd == "propose":
        res = op_propose(args.agent_id, args.topic, args.proposal)
        print(f"[OK] Created Proposal #{res['decision_id']}: {args.topic}")
        return

    if args.cmd == "vote":
        res = op_vote(args.decision_id, args.agent, args.vote, args.comment, args.status)
        if not res["ok"]:
            print(f"[ERROR] {res['error']}", file=sys.stderr)
            sys.exit(1)
        print(f"[OK] Recorded vote '{args.vote}' by {args.agent} on Decision #{args.decision_id}")
        return

    if args.cmd == "kv":
        init_db()
        if args.kv_cmd == "set":
            op_kv_set(args.key, args.value, args.agent)
            print(f"[OK] Set {args.key} = {args.value}")
            return
        if args.kv_cmd == "get":
            with get_db() as conn:
                if args.key:
                    row = conn.execute("SELECT * FROM kv_store WHERE key = ?", (args.key,)).fetchone()
                    if row:
                        print(row["value"])
                else:
                    for r in conn.execute("SELECT * FROM kv_store ORDER BY key ASC").fetchall():
                        print(f"{r['key']} = {r['value']} (by {r['updated_by']})")
            return

    if args.cmd in ("status", None):
        init_db()
        with get_db() as conn:
            state = export_state(conn)
        print(render_board_markdown(state))
        return

    if args.cmd == "serve":
        init_db()
        with get_db() as conn:
            export_state(conn)
        server = ThreadingHTTPServer((args.host, args.port), CollabHTTPRequestHandler)
        print(f"Collaboration Hub Web UI & API listening on http://{args.host}:{args.port}")
        sys.stdout.flush()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return


if __name__ == "__main__":
    main()
