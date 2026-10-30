"""Real local-process launcher checks, with all files and ports isolated."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

# Test subprocesses must never notify the OS or wake a real agent session.
os.environ["COLLAB_DISABLE_EXTERNAL_DELIVERY"] = "1"
os.environ["COLLAB_ALERTS_AUTOSTART"] = "0"

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("desktop_launcher", ROOT / "scripts" / "desktop.py")
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)

SERVER = '''import argparse,json,os
from http.server import BaseHTTPRequestHandler,HTTPServer
from socketserver import TCPServer
p=argparse.ArgumentParser();p.add_argument('action');p.add_argument('--host');p.add_argument('--port',type=int);a=p.parse_args()
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  body=json.dumps({'workspace':os.environ['COLLAB_WORKSPACE'],'pid':os.getpid()}).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass
class Server(HTTPServer):
 def server_bind(self):
  TCPServer.server_bind(self)
  self.server_name,self.server_port=self.server_address[:2]
Server((a.host,a.port),H).serve_forever()
'''


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="collab-desktop-test-")
        self.root = Path(self.temp.name).resolve()
        self.code = self.root / "app with spaces"
        self.code.mkdir()
        (self.code / "collab.py").write_text(SERVER, encoding="utf-8")
        self.workspace = self.root / "workspace with spaces"
        self.port = desktop.free_port(0)
        self.code_patch = patch.object(desktop, "CODE_DIR", self.code)
        self.code_patch.start()
        self.workspaces = []

    def tearDown(self):
        for workspace in self.workspaces:
            try:
                desktop.stop(workspace, self.port)
            except (OSError, RuntimeError):
                pass
        self.code_patch.stop()
        self.temp.cleanup()

    def launch(self, workspace=None):
        workspace = workspace or self.workspace
        self.workspaces.append(workspace)
        return desktop.start(workspace, self.port, open_browser=False, timeout=5)

    def test_start_reuses_matching_server_and_stop_checks_live_identity(self):
        first = self.launch()
        again = self.launch()
        self.assertTrue(first["running"])
        self.assertTrue(first["managed"])
        self.assertEqual(first["pid"], again["pid"])
        self.assertEqual(first["workspace"], str(self.workspace))
        stopped = desktop.stop(self.workspace, self.port)
        self.assertTrue(stopped["stopped"])
        self.assertFalse(desktop.status(self.workspace, self.port)["running"])

    def test_foreign_workspace_on_preferred_port_gets_separate_server(self):
        first = self.launch()
        other = self.launch(self.root / "other workspace")
        self.assertNotEqual(first["port"], other["port"])
        self.assertNotEqual(first["pid"], other["pid"])
        self.assertEqual(desktop.health(first["port"])["workspace"], str(self.workspace))

    def test_stale_or_forged_manifest_never_kills_unrelated_pid(self):
        first = self.launch()
        record = desktop.load_manifest(self.workspace)
        record["pid"] = os.getpid()
        desktop.write_manifest(self.workspace, record)
        with patch.object(desktop.os, "kill") as kill:
            with self.assertRaisesRegex(RuntimeError, "started elsewhere"):
                desktop.stop(self.workspace, self.port)
            kill.assert_not_called()
        record["pid"] = first["pid"]
        desktop.write_manifest(self.workspace, record)

    def test_status_is_read_only_for_missing_workspace(self):
        self.assertFalse(desktop.status(self.workspace, self.port)["running"])
        self.assertFalse(self.workspace.exists())

    def test_start_failure_includes_server_error_before_temporary_log_is_removed(self):
        (self.code / "collab.py").write_text(
            "import sys\nprint('STARTUP DIAGNOSTIC: cannot open test database', file=sys.stderr)\n"
            "raise SystemExit(3)\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "STARTUP DIAGNOSTIC: cannot open test database") as failure:
            self.launch()
        self.assertIn(str(self.workspace / "state" / "desktop-server.log"), str(failure.exception))

    def test_startup_log_diagnostic_is_bounded_and_handles_missing_file(self):
        logfile = self.root / "server.log"
        self.assertEqual(desktop.startup_log_tail(logfile), "")
        logfile.write_text("old output\n" * 2000 + "final error\n\n", encoding="utf-8")
        detail = desktop.startup_log_tail(logfile)
        self.assertLessEqual(len(detail.encode("utf-8")), 4096)
        self.assertTrue(detail.endswith("final error"))

    def test_browser_opens_matching_local_url_only(self):
        first = self.launch()
        with patch.object(desktop, "open_board", return_value=True) as browser:
            result = desktop.start(self.workspace, self.port)
            again = desktop.start(self.workspace, self.port)
        self.assertEqual(browser.call_count, 2)
        for call in browser.call_args_list:
            self.assertEqual(call.args, (first["url"],))
        self.assertEqual(result["pid"], again["pid"])
        self.assertTrue(result["browser_opened"])

    def test_browser_failure_keeps_running_board_available(self):
        first = self.launch()
        with patch.object(desktop, "open_board", side_effect=RuntimeError("Browser failed")):
            with self.assertRaisesRegex(RuntimeError, "Browser failed"):
                desktop.start(self.workspace, self.port)
        self.assertEqual(desktop.status(self.workspace, self.port)["pid"], first["pid"])

    def test_simultaneous_clicks_start_one_process(self):
        # Separate Python launchers exercise the OS file lock, not a thread mock.
        script = self.code / "desktop_test_driver.py"
        script.write_text(
            "import importlib.util,json\n"
            f"s=importlib.util.spec_from_file_location('launcher',{str(ROOT / 'scripts' / 'desktop.py')!r})\n"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
            f"m.CODE_DIR=m.Path({str(self.code)!r})\n"
            f"print(json.dumps(m.start(m.Path({str(self.workspace)!r}),{self.port},False,False,5)))\n", encoding="utf-8")
        self.workspaces.append(self.workspace)
        processes = [subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True) for _ in range(2)]
        results = []
        try:
            # Reap both launchers before asserting, including when one failed.
            outputs = [process.communicate(timeout=15) for process in processes]
            for process, (output, error) in zip(processes, outputs):
                self.assertEqual(process.returncode, 0, error)
                results.append(json.loads(output))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
        self.assertEqual(results[0]["pid"], results[1]["pid"])


class BrowserOpenTests(unittest.TestCase):
    def test_mac_opens_native_url_handler_without_applescript_backend(self):
        with patch.object(desktop.sys, "platform", "darwin"), \
             patch.object(desktop.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as native, \
             patch.object(desktop.webbrowser, "open") as browser:
            self.assertTrue(desktop.open_board("http://127.0.0.1:8765"))
        self.assertEqual(native.call_args.args[0], ["/usr/bin/open", "http://127.0.0.1:8765"])
        self.assertFalse(native.call_args.kwargs.get("shell", False))
        browser.assert_not_called()

    def test_mac_browser_failure_reports_running_board_address(self):
        with patch.object(desktop.sys, "platform", "darwin"), \
             patch.object(desktop.subprocess, "run", return_value=SimpleNamespace(
                 returncode=1, stdout="", stderr="No application can open this URL")):
            with self.assertRaisesRegex(RuntimeError, "http://127.0.0.1:8765.*macOS could not open"):
                desktop.open_board("http://127.0.0.1:8765")

    def test_other_platform_browser_failure_is_explicit(self):
        with patch.object(desktop.sys, "platform", "linux"), \
             patch.object(desktop.webbrowser, "open", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "Open that address manually"):
                desktop.open_board("http://127.0.0.1:8765")


class InstalledAppIntegrationTests(unittest.TestCase):
    def test_copied_real_app_keeps_data_separate_and_generates_valid_client_paths(self):
        """Install actual source, then launch and use only that installed copy."""
        spec = importlib.util.spec_from_file_location("real_app_installer", ROOT / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory(prefix="collab-real-install-") as directory:
            base = Path(directory).resolve()
            app, workspace = base / "installed app", base / "private workspace"
            installer.install(app, workspace, None, source=ROOT)
            environment = {**os.environ, "COLLAB_WORKSPACE": str(workspace)}
            port = desktop.free_port(0)

            def run(script, *args):
                result = subprocess.run([sys.executable, str(app / script), *args], cwd=app,
                                        env=environment, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                return result.stdout

            launched = False
            try:
                output = run("scripts/desktop.py", "--workspace", str(workspace), "--port", str(port), "--no-browser")
                result = json.loads(output)
                launched = True
                info = desktop.health(result["port"])
                self.assertEqual(info["workspace"], str(workspace))
                self.assertEqual(info["pid"], result["pid"])
                self.assertTrue(result["managed"])
                run("collab.py", "init")
                run("collab.py", "post", "--from", "install-test", "--msg", "Installed board roundtrip")
                self.assertIn("Installed board roundtrip", run("collab.py", "read"))
                self.assertTrue((workspace / "state" / "collab.db").is_file())
                self.assertFalse((app / "state").exists())

                run("setup_clients.py", "--workspace", str(workspace))
                configs = workspace / "client-configs"
                for filename in ("claude.mcp.json", "antigravity.mcp.json"):
                    config = json.loads((configs / filename).read_text())
                    server = config["mcpServers"]["agent-collab"]
                    self.assertEqual(server["env"]["COLLAB_WORKSPACE"], str(workspace))
                    self.assertEqual(Path(server["args"][0]), app / "mcp_server.py")
                    self.assertTrue(Path(server["args"][0]).is_file())
                for filename in ("claude.settings.json", "codex.hooks.json", "antigravity.hooks.json"):
                    config = json.loads((configs / filename).read_text())
                    def commands(value):
                        if isinstance(value, dict):
                            for key, item in value.items():
                                if key == "command":
                                    yield item
                                else:
                                    yield from commands(item)
                        elif isinstance(value, list):
                            for item in value:
                                yield from commands(item)
                    hook_commands = list(commands(config))
                    self.assertTrue(hook_commands)
                    for command in hook_commands:
                        self.assertIn(str(app / "scripts" / "board_hook.py"), command)
                        self.assertIn(str(workspace), command)
                    self.assertTrue((app / "scripts" / "board_hook.py").is_file())
                self.assertTrue((app / "assets" / "dashboard.html").is_file())
                self.assertTrue((app / "examples" / "README.md").is_file())
            finally:
                if launched:
                    stopped = json.loads(run("scripts/desktop.py", "stop", "--workspace", str(workspace), "--port", str(port)))
                    self.assertTrue(stopped["stopped"])


@unittest.skipUnless(os.name == "nt", "Requires native Windows command and shortcut support")
class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="collab-windows-launch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / "app with spaces"
        self.workspace = self.root / "workspace 100% & test!"
        self.desktop = self.root / "temporary desktop"
        (self.app / "scripts").mkdir(parents=True)
        self.workspace.mkdir()
        (self.app / "scripts" / "desktop.py").write_text(
            "import json,sys\nfrom pathlib import Path\n"
            "workspace=Path(sys.argv[sys.argv.index('--workspace')+1])\n"
            "(workspace/'launched.json').write_text(json.dumps(sys.argv[1:]),encoding='utf-8')\n",
            encoding="utf-8")
        spec = importlib.util.spec_from_file_location("native_windows_shortcuts", ROOT / "scripts" / "install_desktop.py")
        self.shortcuts = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.shortcuts)

    def test_command_shortcut_executes_with_spaces_and_shell_characters(self):
        with patch.object(self.shortcuts.shutil, "which", return_value=None):
            shortcut = self.shortcuts.create_shortcut(self.app, self.workspace, self.desktop,
                                                       sys.executable, "win32")
        self.assertEqual(Path(shortcut["path"]).suffix, ".cmd")
        result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", shortcut["path"]],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        args = json.loads((self.workspace / "launched.json").read_text(encoding="utf-8"))
        self.assertEqual(args, ["--workspace", str(self.workspace)])

    def test_native_shortcut_preserves_interpreter_workspace_and_working_directory(self):
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        self.assertIsNotNone(powershell, "Windows CI must verify the PowerShell/COM shortcut path")
        shortcut = self.shortcuts.create_shortcut(self.app, self.workspace, self.desktop,
                                                   sys.executable, "win32")
        self.assertEqual(Path(shortcut["path"]).suffix, ".lnk")
        quoted = "'" + shortcut["path"].replace("'", "''") + "'"
        script = ("$ErrorActionPreference='Stop';$w=New-Object -ComObject WScript.Shell;"
                  f"$s=$w.CreateShortcut({quoted});"
                  "[pscustomobject]@{TargetPath=$s.TargetPath;Arguments=$s.Arguments;"
                  "WorkingDirectory=$s.WorkingDirectory}|ConvertTo-Json -Compress")
        result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        details = json.loads(result.stdout)
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        self.assertEqual(Path(details["TargetPath"]).resolve(), pythonw if pythonw.exists() else Path(sys.executable))
        self.assertEqual(Path(details["WorkingDirectory"]).resolve(), self.app)
        self.assertEqual(details["Arguments"], subprocess.list2cmdline(
            [str(self.app / "scripts" / "desktop.py"), "--workspace", str(self.workspace)]))


if __name__ == "__main__":
    unittest.main()
