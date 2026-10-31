"""External-delivery guards use temporary boards and mocked OS boundaries."""
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

# Each test entrypoint must protect child processes even when run on its own.
os.environ["COLLAB_DISABLE_EXTERNAL_DELIVERY"] = "1"
os.environ["COLLAB_ALERTS_AUTOSTART"] = "0"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_source(name, path):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    return module


class AlertDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="collab-alert-delivery-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        environment = patch.dict(os.environ, {
            "COLLAB_WORKSPACE": str(self.workspace),
            "COLLAB_DISABLE_EXTERNAL_DELIVERY": "1",
            "COLLAB_ALERTS_AUTOSTART": "0",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.alerts = load_source("isolated_alert_delivery", ROOT / "alerts.py")
        self.hub = load_source("isolated_alert_board", ROOT / "collab.py")
        self.hub.init_db()
        self.alerts.scan()  # Establish the new-board cursor before test messages.
        # A missed guard must fail a test instead of sending a notification.
        blocked = patch.object(self.alerts.subprocess, "run",
                               side_effect=AssertionError("Unexpected external delivery"))
        self.external_run = blocked.start()
        self.addCleanup(blocked.stop)

    def test_disabled_delivery_guards_each_subprocess_boundary(self):
        operations = (
            lambda: self.alerts.push_macos("test", "synthetic message"),
            lambda: self.alerts.push_codex("synthetic-thread", "synthetic message"),
            lambda: self.alerts.push_command("synthetic-command", "test", "title", "message"),
        )
        with patch.object(self.alerts, "find_executable",
                          side_effect=AssertionError("Executable discovery should be skipped")):
            for operation in operations:
                with self.subTest(operation=operation):
                    ok, detail = operation()
                    self.assertFalse(ok)
                    self.assertIn("COLLAB_DISABLE_EXTERNAL_DELIVERY", detail)
        self.external_run.assert_not_called()

    def test_disabled_forced_delivery_does_not_read_targets_or_send_reminders(self):
        self.assertEqual(self.alerts.deliver(None, force=True), {})
        self.assertIsNone(self.alerts.remind_human(None, "test", [], 0, "test", force=True))
        self.external_run.assert_not_called()

    def test_disabled_antigravity_relay_never_starts_external_process(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.alerts.run_relay("test", "synthetic-conversation", 1), 0)
        self.external_run.assert_not_called()

    def test_forced_urgent_none_recipient_is_inbox_only(self):
        self.alerts.register("audit-b", via="none")
        with patch.dict(os.environ, {"COLLAB_DISABLE_EXTERNAL_DELIVERY": "0"}), \
                patch.dict(sys.modules, {"collab": self.hub}):
            result = self.alerts.notify("audit-a", "audit-b", "Synthetic test message", urgent=True)
        self.assertEqual(result["delivery"]["via"], "inbox")
        inbox = self.alerts.inbox("audit-b", mark_read=False)
        self.assertTrue(inbox["alerts"])
        self.external_run.assert_not_called()

    def test_none_human_target_disables_unreachable_agent_reminder(self):
        self.alerts.register("audit-b", via="hook")
        self.alerts.register("human", via="none")
        with patch.dict(os.environ, {"COLLAB_DISABLE_EXTERNAL_DELIVERY": "0"}), \
                patch.dict(sys.modules, {"collab": self.hub}):
            result = self.alerts.notify("audit-a", "audit-b", "Synthetic test message", urgent=True)
        self.assertEqual(result["delivery"]["via"], "inbox")
        self.external_run.assert_not_called()

    def test_configured_human_macos_reminder_remains_available(self):
        self.alerts.register("audit-b", via="hook")
        with patch.dict(os.environ, {"COLLAB_DISABLE_EXTERNAL_DELIVERY": "0"}), \
                patch.dict(sys.modules, {"collab": self.hub}), \
                patch.object(self.alerts, "push_macos", return_value=(True, "mock notification")) as sender:
            result = self.alerts.notify("audit-a", "audit-b", "Synthetic test message", urgent=True)
        self.assertEqual(result["delivery"]["via"], "macos-reminder")
        sender.assert_called_once()
        self.external_run.assert_not_called()

    def test_enabled_macos_boundary_retains_real_command_with_mocked_execution(self):
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.dict(os.environ, {"COLLAB_DISABLE_EXTERNAL_DELIVERY": "0"}), \
                patch.object(self.alerts.subprocess, "run", return_value=completed) as sender:
            self.assertTrue(self.alerts.push_macos("title", "message")[0])
        self.assertEqual(sender.call_args.args[0][0], "/usr/bin/osascript")
        self.external_run.assert_not_called()

    def test_smoke_child_forces_delivery_off_even_if_parent_enables_it(self):
        smoke = load_source("isolated_smoke_client", ROOT / "tests" / "smoke_mcp.py")
        server = self.workspace / "synthetic_server.py"
        server.write_text(
            "import json,os,pathlib\n"
            "pathlib.Path('child-env.json').write_text(json.dumps({"
            "key:os.environ.get(key) for key in "
            "('COLLAB_DISABLE_EXTERNAL_DELIVERY','COLLAB_ALERTS_AUTOSTART','COLLAB_WORKSPACE','COLLAB_ALLOW_CONSULTS')}))\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"COLLAB_DISABLE_EXTERNAL_DELIVERY": "0", "COLLAB_ALLOW_CONSULTS": "1"}):
            client = smoke.Client(server, self.workspace)
            self.assertEqual(client.close(), "")
        environment = json.loads((self.workspace / "child-env.json").read_text())
        self.assertEqual(environment["COLLAB_DISABLE_EXTERNAL_DELIVERY"], "1")
        self.assertEqual(environment["COLLAB_ALERTS_AUTOSTART"], "0")
        self.assertEqual(environment["COLLAB_ALLOW_CONSULTS"], "0")
        self.assertEqual(environment["COLLAB_WORKSPACE"], str(self.workspace))


if __name__ == "__main__":
    unittest.main()
