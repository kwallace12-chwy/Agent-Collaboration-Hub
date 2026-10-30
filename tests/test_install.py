import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Test subprocesses must never notify the OS or wake a real agent session.
os.environ["COLLAB_DISABLE_EXTERNAL_DELIVERY"] = "1"
os.environ["COLLAB_ALERTS_AUTOSTART"] = "0"

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("board_install", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
spec2 = importlib.util.spec_from_file_location("desktop_shortcut", ROOT / "scripts" / "install_desktop.py")
shortcuts = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(shortcuts)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="collab-install-test-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source checkout"
        for name in installer.REQUIRED:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if name.startswith("scripts/"):
                target.write_bytes((ROOT / name).read_bytes())
            else:
                target.write_text("# fixture module\n", encoding="utf-8")
        (self.source / "state").mkdir()
        (self.source / "state" / "collab.db").write_text("private database", encoding="utf-8")
        (self.source / "credentials.json").write_text("private", encoding="utf-8")
        self.app = self.root / "installed app"
        self.data = self.root / "preserved workspace"
        self.desktop = self.root / "desktop with spaces"

    def tearDown(self):
        self.temp.cleanup()

    def install(self, **kwargs):
        return installer.install(self.app, self.data, self.desktop, source=self.source,
                                 platform=kwargs.pop("platform", "linux"), **kwargs)

    def test_dry_run_creates_nothing(self):
        plan = self.install(dry_run=True)
        self.assertTrue(plan["dry_run"])
        self.assertFalse(self.app.exists())
        self.assertFalse(self.data.exists())
        self.assertFalse(self.desktop.exists())

    def test_python_version_rejected_before_any_files_are_created(self):
        with patch.object(installer.sys, "version_info", (3, 9, 9)):
            with self.assertRaisesRegex(ValueError, "Python 3.10"):
                self.install()
        self.assertFalse(self.app.exists())
        self.assertFalse(self.data.exists())

    def test_installed_hook_and_optional_examples_are_copied(self):
        examples = self.source / "examples"
        examples.mkdir()
        (examples / "README.md").write_text("Public examples", encoding="utf-8")
        (examples / "client.json").write_text("{}", encoding="utf-8")
        (examples / "private.db").write_text("never install this", encoding="utf-8")
        self.install()
        self.assertTrue((self.app / "scripts" / "board_hook.py").is_file())
        self.assertTrue((self.app / "examples" / "client.json").is_file())
        self.assertFalse((self.app / "examples" / "private.db").exists())

    @unittest.skipIf(os.name == "nt", "POSIX executable modes do not apply on Windows")
    def test_installed_shell_entrypoints_execute_when_source_modes_are_lost(self):
        for name in ("collab", "alerts", "ask", "advisory"):
            source = self.source / name
            source.write_text("#!/bin/sh\nprintf '%s\\n' entrypoint-ready\n", encoding="utf-8")
            source.chmod(0o644)
        self.install()
        for name in ("collab", "alerts", "ask", "advisory"):
            with self.subTest(entrypoint=name):
                installed = self.app / name
                self.assertEqual(installed.stat().st_mode & 0o777, 0o755)
                self.assertEqual((self.source / name).stat().st_mode & 0o777, 0o644)
                result = subprocess.run([str(installed)], capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), "entrypoint-ready")

    def test_allowlist_excludes_database_and_credentials_and_preserves_workspace(self):
        self.data.mkdir()
        sentinel = self.data / "credentials.json"
        sentinel.write_text("keep my settings", encoding="utf-8")
        result = self.install()
        self.assertEqual(sentinel.read_text(), "keep my settings")
        self.assertFalse((self.app / "state").exists())
        self.assertFalse((self.app / "credentials.json").exists())
        self.assertTrue(Path(result["shortcut"]["path"]).exists())
        self.assertTrue((self.app / installer.MARKER).is_file())

    def test_update_preserves_data_and_replaces_only_app_code(self):
        self.install()
        (self.data / "notes.txt").write_text("keep", encoding="utf-8")
        (self.source / "collab.py").write_text("# new version\n", encoding="utf-8")
        self.install()
        self.assertEqual((self.app / "collab.py").read_text(), "# new version\n")
        self.assertEqual((self.data / "notes.txt").read_text(), "keep")

    def test_refuses_unrelated_nonempty_install_folder_and_nested_data(self):
        self.app.mkdir()
        (self.app / "mine.txt").write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unrelated"):
            self.install()
        with self.assertRaisesRegex(ValueError, "non-nested"):
            installer.install(self.root / "different", self.root / "different" / "data", None,
                              source=self.source)

    def test_no_shortcut_mode_only_creates_installed_launcher(self):
        result = installer.install(self.app, self.data, None, source=self.source)
        self.assertNotIn("shortcut", result)
        self.assertTrue(Path(result["launcher"]).exists())
        self.assertFalse(self.desktop.exists())

    def test_real_install_generates_stable_client_paths_and_workspace_consult_context(self):
        installer.install(self.app, self.data, None, source=ROOT)
        environment = {**os.environ, "COLLAB_WORKSPACE": str(self.data), "COLLAB_ALLOW_CONSULTS": "0",
                       "COLLAB_DISABLE_EXTERNAL_DELIVERY": "1", "COLLAB_ALERTS_AUTOSTART": "0"}
        generated = subprocess.run([sys.executable, str(self.app / "setup_clients.py"),
                                    "--workspace", str(self.data)], cwd=self.data, env=environment,
                                   text=True, capture_output=True, timeout=20)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        client = json.loads((self.data / "client-configs" / "claude.mcp.json").read_text())
        self.assertEqual(client["mcpServers"]["agent-collab"]["args"], [str(self.app / "mcp_server.py")])
        self.assertEqual(client["mcpServers"]["agent-collab"]["env"]["COLLAB_ALLOW_CONSULTS"], "0")
        context = subprocess.run([sys.executable, "-c", "import sys;sys.path.insert(0,sys.argv[1]);"
                                  "import ask;print(ask.framing('synthetic-reviewer'))", str(self.app)],
                                 cwd=self.data, env=environment, text=True, capture_output=True, timeout=20)
        self.assertEqual(context.returncode, 0, context.stderr)
        self.assertIn(str(self.app / "AGENTS.md"), context.stdout)
        self.assertIn(str(self.data / "BOARD.md"), context.stdout)
        self.assertIn("Authority and privacy", context.stdout)
        self.assertNotIn("./collab read", context.stdout)
        disabled = subprocess.run([sys.executable, str(self.app / "ask.py"), "codex", "Synthetic prompt"],
                                  cwd=self.data, env=environment, text=True, capture_output=True, timeout=20)
        self.assertEqual(disabled.returncode, 1)
        self.assertIn("Consultations are disabled", disabled.stderr)
        self.assertFalse((self.data / "state" / "asks").exists())

    def test_mac_app_contains_executable_and_valid_plist(self):
        result = self.install(platform="darwin")
        bundle = Path(result["shortcut"]["path"])
        with (bundle / "Contents" / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
        launcher = bundle / "Contents" / "MacOS" / info["CFBundleExecutable"]
        for key in ("NSDesktopFolderUsageDescription", "NSDocumentsFolderUsageDescription",
                    "NSDownloadsFolderUsageDescription"):
            self.assertIn("workspace you chose", info[key])
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertIn("--workspace", launcher.read_text())
        self.assertIn(str(self.data), launcher.read_text())

    @unittest.skipIf(os.name == "nt", "macOS app entrypoint is a POSIX shell script")
    def test_mac_bundle_entrypoint_runs_with_sparse_finder_path_and_logs(self):
        self.app.mkdir()
        (self.app / "scripts").mkdir()
        stub = self.app / "scripts" / "desktop.py"
        stub.write_text(
            "import json,os,sys\nfrom pathlib import Path\n"
            "workspace=Path(sys.argv[sys.argv.index('--workspace')+1])\n"
            "(workspace/'started.json').write_text(json.dumps({'argv':sys.argv[1:],'path':os.environ.get('PATH')}))\n"
            "print('launcher stdout')\nprint('launcher stderr',file=sys.stderr)\n", encoding="utf-8")
        result = shortcuts.create_shortcut(self.app, self.data, self.desktop, sys.executable, "darwin")
        bundle = Path(result["path"])
        with (bundle / "Contents" / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
        entrypoint = bundle / "Contents" / "MacOS" / info["CFBundleExecutable"]
        completed = subprocess.run([str(entrypoint)], cwd="/", env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        started = json.loads((self.data / "started.json").read_text())
        self.assertEqual(started["argv"], ["--workspace", str(self.data)])
        self.assertEqual(started["path"], "/usr/bin:/bin:/usr/sbin:/sbin")
        log = (self.data / "state" / "desktop-launch.log").read_text()
        self.assertIn("launcher stdout", log)
        self.assertIn("launcher stderr", log)

    def test_windows_fallback_is_actual_command_shortcut(self):
        with patch.object(shortcuts.shutil, "which", return_value=None):
            result = shortcuts.create_shortcut(self.app, self.data, self.desktop,
                                               "C:\\Python\\python.exe", "win32")
        path = Path(result["path"])
        self.assertEqual(path.suffix, ".cmd")
        self.assertIn("DisableDelayedExpansion", path.read_text())
        self.assertIn("--workspace", path.read_text())

    def test_linux_exec_quotes_spaces_and_escapes_percent_fields(self):
        result = shortcuts.create_shortcut(self.app, self.data / "100% done", self.desktop,
                                           "/some python/python3", "linux")
        text = Path(result["path"]).read_text()
        self.assertIn('Exec="/some python/python3"', text)
        self.assertIn("100%% done", text)
        self.assertIn("Terminal=false", text)


if __name__ == "__main__":
    unittest.main()
