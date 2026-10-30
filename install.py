#!/usr/bin/env python3
"""Install this board for the current user, with a desktop icon and separate data.

No dependencies are downloaded and no agent credentials/configurations are edited.
Requires Python 3.10+. Run again to update installed code while preserving data.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

SOURCE_DIR = Path(__file__).resolve().parent
MARKER = ".ai-agent-collab-install.json"
REQUIRED = ("collab.py", "alerts.py", "ask.py", "mcp_server.py", "advisory.py", "app_paths.py",
            "file_lock.py", "setup_clients.py", "assets/dashboard.html",
            "scripts/desktop.py", "scripts/install_desktop.py", "scripts/board_hook.py")
OPTIONAL = ("README.md", "LICENSE", "AGENTS.md", "CLAUDE.md", "ADVISORY.md", "PM.md", "install.py",
            "SECURITY.md", "CHANGELOG.md", "CONTRIBUTING.md",
            "collab", "alerts", "ask", "advisory", "scripts/demo.py",
            "assets/app-icon.svg", "assets/app-icon-256.png", "assets/app-icon-512.png",
            "assets/app-icon.icns", "assets/app-icon.ico")
POSIX_ENTRYPOINTS = {"collab", "alerts", "ask", "advisory"}


def user_paths(platform: str | None = None, home: Path | None = None) -> tuple[Path, Path]:
    platform, home = platform or sys.platform, home or Path.home()
    if platform == "darwin":
        base = home / "Library" / "Application Support" / "AI Agent Collaboration"
    elif platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "AIAgentCollaboration"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "ai-agent-collab"
    return base / "app", base / "workspace"


def default_desktop_dir() -> Path:
    if sys.platform == "win32":
        try:
            import ctypes
            buffer = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) == 0:
                return Path(buffer.value)
        except (AttributeError, OSError):
            pass
    if sys.platform.startswith("linux"):
        try:
            config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "user-dirs.dirs"
            for line in config.read_text(encoding="utf-8").splitlines():
                if line.startswith('XDG_DESKTOP_DIR="') and line.endswith('"'):
                    value = line[len('XDG_DESKTOP_DIR="'):-1].replace("$HOME", str(Path.home()))
                    if "$" not in value and Path(value).is_absolute():
                        return Path(value)
        except OSError:
            pass
    return Path.home() / "Desktop"


def install(install_dir: Path, workspace: Path, desktop_dir: Path | None, *, source: Path = SOURCE_DIR,
            dry_run: bool = False, alerts: bool = False, platform: str | None = None,
            python: str = sys.executable) -> dict:
    if sys.version_info < (3, 10):
        raise ValueError("Python 3.10 or newer is required. No installation files were changed.")
    platform = platform or sys.platform
    install_dir, workspace, source = (p.expanduser().resolve() for p in (install_dir, workspace, source))
    desktop_dir = desktop_dir.expanduser().resolve() if desktop_dir else None
    if any("\n" in str(p) or "\r" in str(p) for p in (install_dir, workspace, desktop_dir or "", python)):
        raise ValueError("Installation paths cannot contain line breaks")
    if install_dir == source or install_dir in source.parents:
        raise ValueError("Choose an installation folder outside the source checkout")
    if workspace == install_dir or install_dir in workspace.parents or workspace in install_dir.parents:
        raise ValueError("The app folder and workspace must be separate, non-nested folders")
    if install_dir.exists() and any(install_dir.iterdir()):
        try:
            marker = json.loads((install_dir / MARKER).read_text(encoding="utf-8"))
            owned = isinstance(marker, dict) and marker.get("install_dir") == str(install_dir)
        except (OSError, ValueError):
            owned = False
        if not owned:
            raise ValueError("The installation folder contains unrelated files. Choose an empty folder.")
    missing = [name for name in REQUIRED if not (source / name).is_file()]
    if missing:
        raise ValueError("Incomplete source checkout; missing: " + ", ".join(missing))
    files = list(REQUIRED) + [name for name in OPTIONAL if (source / name).is_file()]
    files += [str(path.relative_to(source)) for path in sorted((source / "docs").glob("*.md"))]
    files += [str(path.relative_to(source)) for path in sorted((source / "docs" / "images").glob("*"))
              if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg"}]
    files += [str(path.relative_to(source)) for path in sorted((source / "examples").rglob("*"))
              if path.is_file() and path.suffix in {".md", ".json", ".toml", ".txt"}]
    if any(not (source / name).resolve().is_relative_to(source) for name in files):
        raise ValueError("An installation resource points outside the source checkout")
    plan = {"install_dir": str(install_dir), "workspace": str(workspace), "python": python,
            "desktop_dir": str(desktop_dir) if desktop_dir else None, "alerts_enabled": alerts,
            "files": files, "dry_run": dry_run}
    if dry_run:
        return plan
    install_dir.mkdir(parents=True, exist_ok=True)
    # Claim a newly created app folder before copying.  If a copy is interrupted,
    # the pending marker lets this installer safely resume only that folder.
    marker_path = install_dir / MARKER
    pending_plan = {**plan, "status": "pending"}
    with tempfile.NamedTemporaryFile(dir=install_dir, mode="w", encoding="utf-8", delete=False) as handle:
        handle.write(json.dumps(pending_plan, indent=2) + "\n")
        marker_temp = Path(handle.name)
    os.replace(marker_temp, marker_path)
    for name in files:
        destination = install_dir / name
        # Do not follow an existing directory symlink out of this installation.
        if any(parent.is_symlink() for parent in destination.parents if parent != install_dir.parent):
            raise ValueError(f"Refusing a symlinked installation path: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
            temp = Path(handle.name)
        try:
            shutil.copyfile(source / name, temp)
            shutil.copymode(source / name, temp)
            if os.name != "nt" and name in POSIX_ENTRYPOINTS:
                # Browser uploads and source ZIPs may lose executable modes.
                temp.chmod(0o755)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(install_dir / "scripts"))
    from install_desktop import cmd_launcher, create_shortcut, launch_args, shell_launcher
    args = launch_args(install_dir, workspace, python, alerts)
    launcher = install_dir / ("launch.cmd" if platform == "win32" else "launch.command" if platform == "darwin" else "launch.sh")
    launcher.write_text(cmd_launcher(args) if platform == "win32" else shell_launcher(args), encoding="utf-8", newline="")
    launcher.chmod(0o755)
    plan["launcher"] = str(launcher)
    plan["installed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    # Record a usable installation before optional shortcut creation. If the OS
    # rejects a shortcut, rerunning the installer can safely finish the install.
    plan["status"] = "complete"
    with tempfile.NamedTemporaryFile(dir=install_dir, mode="w", encoding="utf-8", delete=False) as handle:
        handle.write(json.dumps(plan, indent=2) + "\n")
        marker_temp = Path(handle.name)
    os.replace(marker_temp, marker_path)
    if desktop_dir:
        plan["shortcut"] = create_shortcut(install_dir, workspace, desktop_dir, python, platform, alerts)
    return plan


def main(argv=None) -> int:
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is required. No installation files were changed.", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    default_install, default_data = user_paths()
    parser.add_argument("--install-dir", type=Path, default=default_install)
    parser.add_argument("--workspace", type=Path, default=default_data)
    parser.add_argument("--desktop-dir", type=Path, default=default_desktop_dir())
    parser.add_argument("--no-shortcut", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--enable-alerts", action="store_true", help="Desktop launcher also starts the optional alerts watcher")
    args = parser.parse_args(argv)
    try:
        result = install(args.install_dir, args.workspace, None if args.no_shortcut else args.desktop_dir,
                         dry_run=args.dry_run, alerts=args.enable_alerts)
    except (OSError, ValueError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    if not args.dry_run:
        print("\nInstalled. Open the desktop icon or the launcher shown above to open your board.")
        print("Connect your AI clients separately; your existing settings and credentials were preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
