"""Regression coverage for delivery authority, concurrency, and consult controls.

All state is disposable. Provider calls and notification boundaries are mocked;
the only real subprocesses are local Python workers exercising file locks.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

os.environ["COLLAB_DISABLE_EXTERNAL_DELIVERY"] = "1"
os.environ["COLLAB_ALERTS_AUTOSTART"] = "0"
os.environ["COLLAB_ALLOW_CONSULTS"] = "0"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_source(name, path):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


class IsolatedReviewTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="collab-regression-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.keep_patch(patch.dict(os.environ, {
            "COLLAB_WORKSPACE": str(self.workspace), "COLLAB_DISABLE_EXTERNAL_DELIVERY": "1",
            "COLLAB_ALERTS_AUTOSTART": "0", "COLLAB_ALLOW_CONSULTS": "0", "COLLAB_HEADLESS": "0",
            "COLLAB_MAX_CONCURRENT_CONSULTS": "2", "COLLAB_MAX_CONSULTS_PER_HOUR": "10",
        }))
        self.hub = load_source("review_hub", ROOT / "collab.py")
        self.alerts = load_source("review_alerts", ROOT / "alerts.py")
        self.keep_patch(patch.dict(sys.modules, {"collab": self.hub, "alerts": self.alerts}))
        self.ask = load_source("review_ask", ROOT / "ask.py")
        self.keep_patch(patch.dict(sys.modules, {"ask": self.ask}))
        with patch.object(sys, "stdout", sys.stdout):
            self.mcp = load_source("review_mcp", ROOT / "mcp_server.py")
        self.advisory = load_source("review_advisory", ROOT / "advisory.py")
        self.keep_patch(patch.object(self.advisory, "post_collab_message"))
        self.external = self.keep_patch(patch.object(
            self.alerts.subprocess, "run", side_effect=AssertionError("Unexpected external command")))
        self.keep_patch(patch.object(self.ask, "run_claude", side_effect=AssertionError("Unexpected model call")))
        self.keep_patch(patch.object(self.ask, "run_codex", side_effect=AssertionError("Unexpected model call")))
        self.hub.init_db()

    def keep_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def set_paused(self, paused):
        conn = self.alerts.open_db()
        try:
            self.alerts.meta_set(conn, "paused", "1" if paused else "0")
        finally:
            conn.close()

    def worker_group(self, script, count=6, environment=None):
        """Start workers together without invoking model or notification clients."""
        processes = [subprocess.Popen(
            [sys.executable, "-c", script, str(ROOT), str(index)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, **(environment or {})},
        ) for index in range(count)]
        try:
            for process in processes:
                process.stdin.write("go\n")
                process.stdin.flush()
            outputs = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stderr)
                outputs.append(stdout)
            return outputs
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)


class DeliveryAuthorityTests(IsolatedReviewTest):
    def test_mcp_cannot_rewrite_command_target_via_alias_or_omitted_transport(self):
        self.alerts.register("review-target", via="command", target="synthetic-approved-command",
                             aliases=["target-alias"])
        for extra in ({}, {"via": None}, {"via": "none"}, {"via": "command"}):
            with self.subTest(extra=extra):
                result = self.mcp.call_tool({"name": "collab_alert_settings", "arguments": {
                    "agent_id": "target-alias", "target": "synthetic-replacement", **extra}})
                self.assertTrue(result["isError"])
                conn = self.alerts.open_db()
                try:
                    target = self.alerts.load_targets(conn)["review-target"]
                    self.assertEqual(target["target"], "synthetic-approved-command")
                    self.assertEqual(target["via"], "command")
                finally:
                    conn.close()
        # Muting remains possible without giving MCP command-editing authority.
        self.assertFalse(self.mcp.call_tool({"name": "collab_alert_settings", "arguments": {
            "agent_id": "target-alias", "muted": True}})["isError"])

    def test_first_mention_is_present_when_agent_opens_its_first_inbox(self):
        self.hub.op_post("review-sender", "@codex Please review the synthetic draft")
        inbox = self.alerts.inbox("codex", mark_read=False)
        self.assertEqual(len(inbox["alerts"]), 1)

    def test_first_direct_notification_creates_unknown_recipient_inbox(self):
        result = self.alerts.notify("review-sender", "new-reviewer", "Synthetic first message")
        self.assertTrue(result["delivery"]["ok"])
        inbox = self.alerts.inbox("new-reviewer", mark_read=False)
        self.assertEqual([a["message_id"] for a in inbox["alerts"]], [result["message_id"]])
        self.external.assert_not_called()

    def test_direct_notification_does_not_wait_for_large_scanner_backlog(self):
        with self.hub.get_db() as conn:
            conn.executemany("INSERT INTO messages (sender, content, created_at) VALUES (?, ?, ?)",
                             [("review-sender", "Synthetic history", self.hub.now_iso())] * 501)
        result = self.alerts.notify("review-sender", "new-reviewer", "Immediate synthetic message")
        conn = self.alerts.open_db()
        try:
            count = lambda: conn.execute("SELECT COUNT(*) FROM alerts WHERE agent = ? AND message_id = ?",
                                          ("new-reviewer", result["message_id"])).fetchone()[0]
            self.assertEqual(count(), 1)
            self.alerts.scan(conn)
            self.assertEqual(count(), 1, "Later scans must not duplicate the direct alert")
        finally:
            conn.close()

    def test_muted_or_self_notification_does_not_claim_inbox_success(self):
        self.alerts.register("review-target", muted=True)
        self.assertFalse(self.alerts.notify("review-sender", "review-target", "Muted")["delivery"]["ok"])
        self.assertFalse(self.alerts.notify("review-sender", "review-sender", "Self")["delivery"]["ok"])

    def test_global_pause_blocks_forced_urgent_delivery_and_reminders(self):
        self.alerts.register("review-target", via="command", target="synthetic-command")
        self.set_paused(True)
        with patch.dict(os.environ, {"COLLAB_DISABLE_EXTERNAL_DELIVERY": "0"}):
            result = self.alerts.notify("review-sender", "review-target", "Paused", urgent=True)
            self.assertEqual(result["delivery"]["via"], "inbox")
            conn = self.alerts.open_db()
            try:
                self.assertIsNone(self.alerts.remind_human(conn, "review-target", [], time.time(), "test", force=True))
            finally:
                conn.close()
        self.external.assert_not_called()

    def test_paused_hook_keeps_alerts_unread_until_resumed(self):
        self.alerts.notify("review-sender", "codex", "Synthetic pending review")
        self.set_paused(True)
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO("{}")), redirect_stdout(output):
            self.assertEqual(self.alerts.run_hook("codex", "codex", "SessionStart"), 0)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(len(self.alerts.inbox("codex", mark_read=False)["alerts"]), 1)
        self.set_paused(False)
        with patch.object(sys, "stdin", io.StringIO("{}")), redirect_stdout(output):
            self.alerts.run_hook("codex", "codex", "SessionStart")
        self.assertIn("Synthetic pending review", output.getvalue())

    def test_relay_obeys_pause_and_inbox_only_reconfiguration(self):
        self.alerts.register("review-target", via="antigravity", target="synthetic-conversation")
        self.alerts.notify("review-sender", "review-target", "Synthetic relay request")
        conn = self.alerts.open_db()
        try:
            with patch.dict(os.environ, {"COLLAB_DISABLE_EXTERNAL_DELIVERY": "0"}):
                self.set_paused(True)
                self.alerts.relay_delivery(conn, "review-target", "synthetic-conversation", "synthetic-agentapi")
                self.set_paused(False)
                self.alerts.register("review-target", via="none")
                self.alerts.relay_delivery(conn, "review-target", "synthetic-conversation", "synthetic-agentapi")
        finally:
            conn.close()
        self.external.assert_not_called()

    def test_relay_does_not_send_to_a_replaced_conversation(self):
        self.alerts.register("review-target", via="antigravity", target="synthetic-conversation")
        self.alerts.notify("review-sender", "review-target", "Synthetic relay request")
        conn = self.alerts.open_db()
        try:
            with patch.dict(os.environ, {"COLLAB_DISABLE_EXTERNAL_DELIVERY": "0"}), \
                    patch.object(self.alerts.subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 0, stdout="{}", stderr="")) as sender:
                self.alerts.relay_delivery(conn, "review-target", "stale-conversation", "synthetic-agentapi")
                sender.assert_not_called()
                self.alerts.relay_delivery(conn, "review-target", "synthetic-conversation", "synthetic-agentapi")
                sender.assert_called_once()
        finally:
            conn.close()

    def test_urgent_and_forced_sends_share_hard_attempt_cap_including_failures(self):
        self.alerts.register("review-target", via="command", target="synthetic-command")
        with patch.dict(os.environ, {"COLLAB_DISABLE_EXTERNAL_DELIVERY": "0"}), \
                patch.object(self.alerts, "MAX_WAKES_PER_HOUR", 2), \
                patch.object(self.alerts, "push", return_value=(False, "synthetic failure")) as sender:
            for index in range(4):
                self.alerts.notify("review-sender", "review-target", f"Synthetic {index}", urgent=True)
        self.assertEqual(sender.call_count, 2)

    def test_parallel_senders_cannot_exceed_workspace_wake_cap(self):
        for index in range(6):
            self.alerts.register(f"review-target-{index}", via="command", target="synthetic-command")
        with patch.dict(os.environ, {"COLLAB_DISABLE_EXTERNAL_DELIVERY": "0"}), \
                patch.object(self.alerts, "MAX_TOTAL_WAKES_PER_HOUR", 2), \
                patch.object(self.alerts, "push", return_value=(True, "mocked")) as sender:
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda index: self.alerts.notify(
                    "review-sender", f"review-target-{index}", "Synthetic", urgent=True), range(6)))
        self.assertEqual(sender.call_count, 2)

    def test_consult_and_notification_annotations_disclose_external_effects(self):
        for name in ("collab_ask", "collab_notify"):
            annotations = self.mcp.TOOLS[name]["annotations"]
            self.assertFalse(annotations["readOnlyHint"])
            self.assertTrue(annotations["openWorldHint"])


class AdvisoryConcurrencyTests(IsolatedReviewTest):
    WORKER = """
import sys,time
sys.path.insert(0,sys.argv[1])
import advisory
advisory.post_collab_message=lambda *a,**k: None
original=advisory.load_store
def slow_read():
    value=original()
    time.sleep(0.04)
    return value
advisory.load_store=slow_read
sys.stdin.readline()
"""

    def test_simultaneous_process_responses_are_all_preserved(self):
        with redirect_stdout(io.StringIO()):
            self.advisory.cmd_request("Synthetic review", "Synthetic context", "human")
        self.worker_group(self.WORKER + "advisory.cmd_respond(1,'reviewer-'+sys.argv[2],'Synthetic advice')\n")
        store = self.advisory.load_store()
        self.assertEqual(set(store["sessions"][0]["responses"]), {f"reviewer-{i}" for i in range(6)})

    def test_simultaneous_session_requests_get_distinct_ids(self):
        self.worker_group(self.WORKER + "advisory.cmd_request('Synthetic '+sys.argv[2],'Context','human')\n")
        self.assertEqual(sorted(s["id"] for s in self.advisory.load_store()["sessions"]), list(range(1, 7)))

    def test_corrupt_store_is_preserved_instead_of_silently_reset(self):
        self.advisory.STATE_DIR.mkdir(exist_ok=True)
        original = '{"sessions": ['
        self.advisory.ADVISORY_STORE.write_text(original)
        with self.assertRaises(ValueError):
            self.advisory.cmd_request("New", "Context", "human")
        self.assertEqual(self.advisory.ADVISORY_STORE.read_text(), original)

    def test_interrupted_replacement_keeps_previous_valid_store(self):
        original = {"sessions": []}
        self.advisory.save_store(original)
        with patch.object(self.advisory.os, "replace", side_effect=OSError("synthetic disk error")):
            with self.assertRaises(OSError):
                self.advisory.cmd_request("New", "Context", "human")
        self.assertEqual(self.advisory.load_store(), original)

    def test_briefing_preserves_responses_and_leaves_decision_pending(self):
        with redirect_stdout(io.StringIO()):
            self.advisory.cmd_request("Review", "Context", "human")
            self.advisory.cmd_respond(1, "reviewer-a", "Evidence A")
            self.advisory.cmd_respond(1, "reviewer-b", "Disagree based on evidence B")
            self.advisory.cmd_synthesize(1)
        text = (self.advisory.ADVISORY_DIR / "ADVISORY-1.md").read_text()
        self.assertIn("Evidence A", text)
        self.assertIn("Disagree based on evidence B", text)
        self.assertIn("decision pending", text)
        with redirect_stdout(io.StringIO()):
            self.advisory.cmd_respond(1, "reviewer-c", "New evidence")
        self.assertEqual(self.advisory.load_store()["sessions"][0]["status"], "open")


class ConsultationControlsTests(IsolatedReviewTest):
    def test_disabled_consult_does_not_spawn_or_create_a_job(self):
        with patch.object(self.ask.subprocess, "Popen") as spawn:
            result = self.mcp.call_tool({"name": "collab_ask", "arguments": {
                "requester": "codex", "agent": "claude", "prompt": "Synthetic review"}})
        self.assertTrue(result["isError"])
        self.assertIn("COLLAB_ALLOW_CONSULTS", result["content"][0]["text"])
        spawn.assert_not_called()
        self.assertFalse(self.ask.JOBS_DIR.exists())

    def test_separate_processes_share_one_concurrency_limit(self):
        script = """
import sys,json
sys.path.insert(0,sys.argv[1])
import ask
sys.stdin.readline()
try:
    job=ask.reserve_job('codex')
    print(json.dumps({'accepted':True,'job':job['job']}))
except ValueError as error:
    print(json.dumps({'accepted':False,'error':str(error)}))
"""
        outputs = self.worker_group(script, environment={"COLLAB_ALLOW_CONSULTS": "1"})
        results = [json.loads(output) for output in outputs]
        self.assertEqual(sum(result["accepted"] for result in results), 2)
        self.assertTrue(all(result["accepted"] or "concurrency limit" in result["error"] for result in results))

    def test_completed_and_failed_jobs_still_count_toward_hourly_limit(self):
        with patch.dict(os.environ, {"COLLAB_ALLOW_CONSULTS": "1", "COLLAB_MAX_CONSULTS_PER_HOUR": "2"}):
            for status in ("done", "failed"):
                job = self.ask.reserve_job("codex")
                self.ask.update_job(job["job"], status=status)
            with self.assertRaisesRegex(ValueError, "hourly limit"):
                self.ask.reserve_job("claude")

    def test_expired_reservation_releases_concurrency_but_keeps_history(self):
        with patch.dict(os.environ, {"COLLAB_ALLOW_CONSULTS": "1", "COLLAB_MAX_CONCURRENT_CONSULTS": "1"}):
            job = self.ask.reserve_job("codex")
            self.ask.update_job(job["job"], expires_at=time.time() - 1)
            next_job = self.ask.reserve_job("codex")
        self.assertEqual(self.ask.read_job(job["job"])["status"], "expired")
        self.assertEqual(next_job["status"], "queued")

    def test_recursive_consult_and_invalid_limits_are_rejected(self):
        with patch.dict(os.environ, {"COLLAB_ALLOW_CONSULTS": "1", "COLLAB_HEADLESS": "1"}):
            with self.assertRaisesRegex(ValueError, "cannot start another"):
                self.ask.reserve_job("claude")
        with patch.dict(os.environ, {"COLLAB_ALLOW_CONSULTS": "1", "COLLAB_MAX_CONCURRENT_CONSULTS": "0"}):
            with self.assertRaisesRegex(ValueError, "positive integer"):
                self.ask.reserve_job("claude")

    def test_job_cannot_be_claimed_twice_or_by_another_model(self):
        with patch.dict(os.environ, {"COLLAB_ALLOW_CONSULTS": "1"}):
            job = self.ask.reserve_job("codex")
            with self.assertRaises(ValueError):
                self.ask.claim_job(job["job"], "claude")
            self.ask.claim_job(job["job"], "codex")
            with self.assertRaises(ValueError):
                self.ask.claim_job(job["job"], "codex")

    def test_background_prompt_is_not_in_argv_and_fast_completion_is_preserved(self):
        prompt = "Synthetic private review prompt"
        def fast_worker(command, **kwargs):
            self.assertNotIn(prompt, command)
            self.assertEqual(kwargs["stdin"].read(), prompt)
            self.assertEqual(command[command.index("--model") + 1], "synthetic-model")
            self.assertEqual(command[command.index("--effort") + 1], "high")
            job_id = command[command.index("--job") + 1]
            self.assertEqual(self.ask.read_job(job_id)["timeout"], 25)
            self.ask.claim_job(job_id, "codex")
            self.ask.update_job(job_id, status="done", finished_at=self.ask.now_iso(), message_id=7)
            return types.SimpleNamespace(pid=12345)
        with patch.dict(os.environ, {"COLLAB_ALLOW_CONSULTS": "1"}), \
                patch.object(self.ask.subprocess, "Popen", side_effect=fast_worker):
            job = self.ask.start_job("codex", prompt, timeout=25, model="synthetic-model", effort="high")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["message_id"], 7)
        self.assertIn("started_epoch", job)

    def test_async_cli_preserves_explicit_model_effort_and_timeout(self):
        with patch.object(self.ask, "start_job", return_value={"job": "synthetic"}) as start, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(self.ask.main(["codex", "Synthetic review", "--async", "--model", "synthetic-model",
                                            "--effort", "high", "--timeout", "25"]), 0)
        self.assertEqual(start.call_args.kwargs, {"timeout": 25, "model": "synthetic-model", "effort": "high"})

    def test_failed_codex_process_does_not_publish_partial_output_as_success(self):
        # Load an unmocked helper but keep its executable and process boundary synthetic.
        helper = load_source("review_codex_failure", ROOT / "ask.py")
        def failed_process(command, **kwargs):
            Path(command[command.index("-o") + 1]).write_text("Synthetic partial output", encoding="utf-8")
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="Synthetic provider failure")
        with patch.object(helper.alerts, "find_executable", return_value="synthetic-codex"), \
                patch.object(helper.subprocess, "run", side_effect=failed_process):
            ok, answer = helper.run_codex("Synthetic prompt", "reviewer", False, 25)
        self.assertFalse(ok)
        self.assertEqual(answer, "Synthetic provider failure")

    def test_spawn_failure_releases_slot_and_retains_failure_record(self):
        with patch.dict(os.environ, {"COLLAB_ALLOW_CONSULTS": "1", "COLLAB_MAX_CONCURRENT_CONSULTS": "1"}), \
                patch.object(self.ask.subprocess, "Popen", side_effect=OSError("synthetic spawn error")):
            with self.assertRaises(OSError):
                self.ask.start_job("claude", "Synthetic review")
            records = [json.loads(p.read_text()) for p in self.ask.JOBS_DIR.glob("*.json")]
            self.assertEqual(records[0]["status"], "failed")
            self.ask.reserve_job("codex")

    def test_synchronous_cli_uses_quota_and_records_completion(self):
        with patch.dict(os.environ, {"COLLAB_ALLOW_CONSULTS": "1"}), \
                patch.object(self.ask, "run_codex", return_value=(True, "Synthetic answer")) as model, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(self.ask.main(["codex", "Synthetic review"]), 0)
        model.assert_called_once()
        records = [json.loads(p.read_text()) for p in self.ask.JOBS_DIR.glob("*.json")]
        self.assertEqual(records[0]["status"], "done")

    def test_installed_consult_receives_protocol_and_workspace_snapshot_paths(self):
        app = self.workspace / "separate installed app"
        data = self.workspace / "separate private workspace"
        app.mkdir()
        data.mkdir()
        (app / "AGENTS.md").write_text("Synthetic installed collaboration protocol", encoding="utf-8")
        with patch.object(self.ask, "CODE_DIR", app), patch.object(self.ask, "BASE_DIR", data):
            text = self.ask.framing("reviewer")
        self.assertIn("Synthetic installed collaboration protocol", text)
        self.assertIn(str(data / "BOARD.md"), text)
        self.assertNotIn("./collab read", text)


if __name__ == "__main__":
    unittest.main()
