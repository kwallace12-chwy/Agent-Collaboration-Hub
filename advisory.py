#!/usr/bin/env python3
"""
Advisory Panel Facilitator for Multi-Agent Collaboration Hub.
Coordinates multi-perspective deliberations among:
  - Product Manager (@pm)
  - Anthropic Claude Opus 5.5 (@claude)
  - OpenAI Codex (@codex)
  - Google Argon / Gemini (@argon-antigravity)
  - Human Director
"""

import argparse
from contextlib import contextmanager
import datetime
import file_lock as fcntl
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from app_paths import CODE_DIR, workspace_root

BASE_DIR = workspace_root()
STATE_DIR = BASE_DIR / "state"
ADVISORY_STORE = STATE_DIR / "advisory.json"
ADVISORY_DIR = BASE_DIR / "shared" / "advisory"


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def load_store() -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ADVISORY_DIR.mkdir(parents=True, exist_ok=True)
    if not ADVISORY_STORE.exists():
        return {"sessions": []}
    try:
        store = json.loads(ADVISORY_STORE.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"Cannot read advisory state; restore {ADVISORY_STORE} from a backup") from exc
    if not isinstance(store, dict) or not isinstance(store.get("sessions"), list):
        raise ValueError("Invalid advisory state; existing data has not been replaced")
    ids = set()
    for session in store["sessions"]:
        if (not isinstance(session, dict) or type(session.get("id")) is not int
                or session["id"] < 1 or session["id"] in ids
                or not isinstance(session.get("responses", {}), dict)):
            raise ValueError("Invalid advisory session; existing data has not been replaced")
        ids.add(session["id"])
    return store


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_store(data: dict) -> None:
    atomic_write(ADVISORY_STORE, json.dumps(data, indent=2) + "\n")


@contextmanager
def edit_store():
    """Keep the entire read/change/save operation inside one process mutex."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "advisory.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            store = load_store()
            yield store
            save_store(store)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def post_collab_message(sender: str, channel: str, msg: str) -> None:
    collab_bin = CODE_DIR / "collab.py"
    if collab_bin.exists():
        try:
            subprocess.run(
                [sys.executable, str(collab_bin), "post", "--from", sender, "--channel", channel, "--msg", msg],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as e:
            print(f"[Warning] Could not post to collab board: {e}", file=sys.stderr)


def cmd_request(topic: str, context: str, requested_by: str) -> None:
    with edit_store() as store:
        session_id = max((session["id"] for session in store["sessions"]), default=0) + 1
        session = {
            "id": session_id,
            "topic": topic,
            "context": context,
            "requested_by": requested_by,
            "created_at": now_iso(),
            "status": "open",
            "responses": {},
        }
        store["sessions"].append(session)

    prompt = (
        f"🏛️ **[ADVISORY REQUEST #{session_id}]**: **{topic}**\n\n"
        f"**Requested by**: `{requested_by}`\n\n"
        f"**Context & Dilemma**:\n{context}\n\n"
        f"**Panel Members Called**: @claude @codex @argon-antigravity @pm\n\n"
        f"Please provide your specialized perspective:\n"
        f"- `@pm`: Product scope, UX impact, user value, MVP prioritization\n"
        f"- `@claude`: Deep reasoning, security, correctness, edge cases\n"
        f"- `@codex`: Implementation practicality, velocity, testing burden\n"
        f"- `@argon-antigravity`: System orchestration, Chrome/multimodal integration, scalability\n\n"
        f"Record advice using advisory.py respond --id {session_id} with this workspace selected. "
        f"Source: {CODE_DIR / 'advisory.py'}. Board replies are discussion; only recorded responses enter the briefing. "
        "Review independently first, cite sources or tests, and identify unresolved disagreements."
    )

    post_collab_message(sender=requested_by, channel="advisory", msg=prompt)
    print(f"[OK] Advisory Session #{session_id} initiated on topic: '{topic}'")
    print(f"Posted notification to channel #advisory.")


def cmd_respond(session_id: int, agent_id: str, advice: str) -> None:
    with edit_store() as store:
        session = next((s for s in store["sessions"] if s["id"] == session_id), None)
        if not session:
            raise ValueError(f"Session #{session_id} not found")
        session.setdefault("responses", {})[agent_id] = {
            "agent": agent_id,
            "advice": advice,
            "submitted_at": now_iso(),
        }
        # A late response makes a previously compiled briefing stale.
        session["status"] = "open"
        session.pop("briefing_file", None)

    msg = f"🏛️ **[ADVISORY RESPONSE #{session_id}] from `{agent_id}`**:\n\n{advice}"
    post_collab_message(sender=agent_id, channel="advisory", msg=msg)
    print(f"[OK] Recorded response from '{agent_id}' for Session #{session_id}.")


def render_briefing(session: dict) -> str:
    md_lines = [
        f"# Advisory Panel Briefing #{session['id']}: {session['topic']}",
        "",
        f"> **Convened By**: `{session['requested_by']}` | **Date**: `{session['created_at']}` | **Status**: Compiled; decision pending",
        "",
        "## 1. Problem Statement & Context",
        session["context"],
        "",
        "---",
        "",
        "## 2. Panel Deliberations by Perspective",
        "",
    ]

    responses = session.get("responses", {})
    if not responses:
        md_lines.append("_No responses were submitted for this session._")
    else:
        for agent, data in responses.items():
            md_lines.append(f"### Perspective: `{agent}` _({data['submitted_at']})_")
            md_lines.append(data["advice"])
            md_lines.append("")

    md_lines.extend(
        [
            "---",
            "",
            "## 3. Evidence, disagreements, and decision",
            "",
            "- **Verified findings**: Pending source or test verification.",
            "- **Disagreements and missing evidence**: Pending reconciliation.",
            "- **Decision and next steps**: Pending human review.",
            "- **Stop rule**: Finish when acceptance checks pass; escalate unresolved material findings at the agreed review limit.",
            "",
        ]
    )

    return "\n".join(md_lines)


def cmd_synthesize(session_id: int) -> None:
    with edit_store() as store:
        session = next((s for s in store["sessions"] if s["id"] == session_id), None)
        if not session:
            raise ValueError(f"Session #{session_id} not found")
        briefing_path = ADVISORY_DIR / f"ADVISORY-{session_id}.md"
        atomic_write(briefing_path, render_briefing(session))
        session["status"] = "synthesized"
        session["briefing_file"] = str(briefing_path)

    notify_msg = (
        f"🏛️ **[ADVISORY BRIEFING READY]**: Session #{session_id} has been synthesized!\n"
        f"Deliverable published to: `shared/advisory/ADVISORY-{session_id}.md`.\n"
        f"@human-director please review and provide your final direction."
    )
    post_collab_message(sender="pm", channel="advisory", msg=notify_msg)
    print(f"[OK] Synthesized Advisory Briefing at {briefing_path}")


def cmd_list() -> None:
    store = load_store()
    if not store["sessions"]:
        print("(No advisory sessions recorded)")
        return
    for s in store["sessions"]:
        count = len(s.get("responses", {}))
        print(f"Session #{s['id']} [{s['status'].upper()}] - '{s['topic']}' by {s['requested_by']} ({count} responses)")


def main():
    parser = argparse.ArgumentParser(description="Advisory Panel Coordinator")
    sub = parser.add_subparsers(dest="cmd")

    p_req = sub.add_parser("request", help="Request formal deliberation from the Advisory Panel")
    p_req.add_argument("--topic", required=True, help="Topic or dilemma title")
    p_req.add_argument("--context", required=True, help="Detailed context, options, and constraints")
    p_req.add_argument("--from", dest="requested_by", default="human-director", help="Requester identity")

    p_resp = sub.add_parser("respond", help="Submit panelist advice for an advisory session")
    p_resp.add_argument("--id", type=int, required=True, help="Session ID")
    p_resp.add_argument("--agent", required=True, help="Responding agent ID (claude, codex, argon, pm)")
    p_resp.add_argument("--advice", required=True, help="Your perspective and recommendation")

    p_syn = sub.add_parser("synthesize", help="Compile and synthesize panel responses into an executive briefing")
    p_syn.add_argument("--id", type=int, required=True, help="Session ID")

    sub.add_parser("list", help="List all advisory sessions")

    args = parser.parse_args()

    if args.cmd == "request":
        cmd_request(args.topic, args.context, args.requested_by)
    elif args.cmd == "respond":
        cmd_respond(args.id, args.agent, args.advice)
    elif args.cmd == "synthesize":
        cmd_synthesize(args.id)
    elif args.cmd == "list":
        cmd_list()
    else:
        parser.print_help()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
