#!/usr/bin/env python3
"""Open a workspace's private local board; also supports status and stop.

The server stays running after this launcher exits. No agent credentials or client
settings are changed. Alerts start only when --alerts is explicitly supplied.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
import file_lock

DEFAULT_PORT = 8765


def default_workspace() -> Path:
    sys.path.insert(0, str(CODE_DIR))
    from app_paths import workspace_root
    return Path(workspace_root()).expanduser().resolve()


def health(port: int, timeout: float = 0.4) -> dict | None:
    """Read loopback health without environment-configured HTTP proxies."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as response:
            data = response.read(65537)
            if len(data) > 65536:
                return None
            value = json.loads(data)
            return value if isinstance(value, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def matches(info: dict | None, workspace: Path) -> bool:
    if not info or not isinstance(info.get("workspace"), str):
        return False
    return Path(info["workspace"]).expanduser().resolve() == workspace.resolve()


def manifest_path(workspace: Path) -> Path:
    return workspace / "state" / "desktop-server.json"


def load_manifest(workspace: Path) -> dict:
    try:
        value = json.loads(manifest_path(workspace).read_text(encoding="utf-8"))
        if not isinstance(value, dict) or type(value.get("port")) is not int:
            return {}
        return value if 1 <= value["port"] <= 65535 else {}
    except (OSError, ValueError):
        return {}


def write_manifest(workspace: Path, value: dict) -> None:
    target = manifest_path(workspace)
    temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, target)


@contextmanager
def launch_lock(workspace: Path, timeout: float = 15):
    """A kernel lock makes two simultaneous desktop clicks reuse one server."""
    state = workspace / "state"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "desktop-launch.lock").open("a+b") as handle:
        # Do not read the lock byte before acquiring it: Windows byte locks
        # deny that read while a second launcher is still starting the board.
        deadline = time.monotonic() + timeout
        while True:
            try:
                file_lock.flock(handle.fileno(), file_lock.LOCK_EX | file_lock.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another launcher is still starting this workspace. Try again shortly.")
                time.sleep(0.1)
        try:
            yield
        finally:
            file_lock.flock(handle.fileno(), file_lock.LOCK_UN)


def free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", preferred))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def status(workspace: Path, port: int = DEFAULT_PORT) -> dict:
    record = load_manifest(workspace)
    candidates = list(dict.fromkeys([record.get("port", port), port]))
    for candidate in candidates:
        info = health(candidate)
        if matches(info, workspace):
            return {"running": True, "workspace": str(workspace), "port": candidate,
                    "url": f"http://127.0.0.1:{candidate}", "pid": info.get("pid"),
                    "managed": bool(record.get("pid") and record.get("pid") == info.get("pid")
                                    and record.get("port") == candidate)}
    return {"running": False, "workspace": str(workspace)}


def open_board(url: str) -> bool:
    """Use macOS's native URL handler without Python's AppleScript backend."""
    if sys.platform == "darwin":
        try:
            completed = subprocess.run(["/usr/bin/open", url], stdin=subprocess.DEVNULL,
                                       capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"The board is running at {url}, but the browser could not open: {exc}") from exc
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()[-500:]
            raise RuntimeError(f"The board is running at {url}, but macOS could not open the browser. "
                               + (detail or f"URL opener exited {completed.returncode}."))
        return True
    if not webbrowser.open(url):
        raise RuntimeError(f"The board is running at {url}, but the browser could not open. Open that address manually.")
    return True


def startup_log_tail(log_path: Path) -> str:
    """Keep a startup failure useful even if its temporary log is removed."""
    try:
        with log_path.open("rb") as logfile:
            logfile.seek(0, os.SEEK_END)
            logfile.seek(max(0, logfile.tell() - 4096))
            tail = logfile.read(4096).decode("utf-8", errors="replace")
        detail = "\n".join(line for line in tail.splitlines() if line.strip())
        return detail.encode("utf-8")[-4096:].decode("utf-8", errors="ignore")
    except OSError:
        return ""


def start(workspace: Path, port: int = DEFAULT_PORT, open_browser: bool = True,
          enable_alerts: bool = False, timeout: float = 15) -> dict:
    workspace = workspace.expanduser().resolve()
    with launch_lock(workspace):
        result = status(workspace, port)
        if not result["running"]:
            env = {**os.environ, "COLLAB_WORKSPACE": str(workspace), "PYTHONUNBUFFERED": "1"}
            log_path = workspace / "state" / "desktop-server.log"
            for attempt in range(3):
                chosen = free_port(port if attempt == 0 else 0)
                kwargs = {"start_new_session": True} if os.name != "nt" else {
                    "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                with log_path.open("ab") as logfile:
                    process = subprocess.Popen(
                        [sys.executable, str(CODE_DIR / "collab.py"), "serve", "--host", "127.0.0.1",
                         "--port", str(chosen)], cwd=str(CODE_DIR), env=env,
                        stdin=subprocess.DEVNULL, stdout=logfile, stderr=logfile, **kwargs)
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    info = health(chosen)
                    if matches(info, workspace) and info.get("pid") == process.pid:
                        write_manifest(workspace, {"pid": process.pid, "port": chosen,
                                       "workspace": str(workspace), "code_dir": str(CODE_DIR),
                                       "started_at": time.time()})
                        result = status(workspace, chosen)
                        # Keep/reap this intentionally persistent child without
                        # Popen's "still running" warning when start() returns.
                        threading.Thread(target=process.wait, daemon=True).start()
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                if result.get("running"):
                    break
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
            if not result.get("running"):
                detail = startup_log_tail(log_path) or "The server produced no startup output before it stopped."
                raise RuntimeError(f"The board did not start. Details: {log_path}\n{detail}")
        if enable_alerts:
            completed = subprocess.run([sys.executable, str(CODE_DIR / "alerts.py"), "start"],
                env={**os.environ, "COLLAB_WORKSPACE": str(workspace)}, cwd=str(CODE_DIR),
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
            result["alerts_started"] = completed.returncode == 0
            if completed.returncode:
                result["alerts_error"] = (completed.stderr or completed.stdout).strip()[-500:]
    if open_browser:
        result["browser_opened"] = open_board(result["url"])
    return result


def stop(workspace: Path, port: int = DEFAULT_PORT) -> dict:
    workspace = workspace.expanduser().resolve()
    with launch_lock(workspace):
        result = status(workspace, port)
        if not result["running"]:
            return {"stopped": False, "reason": "No server is running for this workspace."}
        if not result["managed"] or type(result.get("pid")) is not int:
            raise RuntimeError("This server was started elsewhere. Stop it in the terminal that started it.")
        os.kill(result["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            info = health(result["port"])
            if not matches(info, workspace) or info.get("pid") != result["pid"]:
                manifest_path(workspace).unlink(missing_ok=True)
                return {"stopped": True, "pid": result["pid"], "workspace": str(workspace)}
            time.sleep(0.1)
        raise RuntimeError("The server is still shutting down; try status again shortly.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=["start", "status", "stop"], default="start")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--alerts", action="store_true", help="Also start the optional alerts watcher")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    workspace = (args.workspace or default_workspace()).expanduser().resolve()
    try:
        result = (start(workspace, args.port, not args.no_browser, args.alerts) if args.action == "start"
                  else stop(workspace, args.port) if args.action == "stop" else status(workspace, args.port))
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"AI Agent Collaboration: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
