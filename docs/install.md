# Install and run

## Requirements

- Python **3.10 or later**, including its standard library.
- A local browser for the dashboard.
- Optional: your separately installed and signed-in agent clients for MCP connections and consultations.

The board has no third-party Python package dependencies. This installer copies Python source; it does not bundle a Python runtime or produce a signed standalone binary. Keep the selected Python installation available. Agent clients have their own installation requirements, accounts, permissions, and usage limits.

## Download from GitLab

Choose **Code → Download source code → ZIP** on the project page. Extract the archive to a folder you can keep, then open a terminal in that folder.

```sh
python3 install.py
```

On Windows:

```powershell
py install.py
```

These Python commands are the recommended installation method and work even when a source archive loses executable permissions. Windows users can also double-click `Install.cmd`. To use the optional macOS double-click helper, first run this command in the extracted folder:

```sh
chmod +x Install.command
```

Then double-click `Install.command`. The installer automatically sets executable permissions on the installed POSIX command launchers.

The installer copies the application into your user application-data folder and creates a desktop entry: a macOS `.app`, Windows shortcut, or Linux `.desktop` file. Run that entry to start the board and open its browser page. If Windows shortcut creation is unavailable, the installer provides a command shortcut instead.

Default locations:

| System | Installed app and workspace |
| --- | --- |
| macOS | `~/Library/Application Support/AI Agent Collaboration/app` and sibling `workspace` |
| Windows | `%LOCALAPPDATA%\AIAgentCollaboration\app` and sibling `workspace` |
| Linux | `$XDG_DATA_HOME/ai-agent-collab/app` and sibling `workspace`; defaults under `~/.local/share` |

The installed launcher uses the copied application. Your downloaded ZIP folder can be retained for updates or source inspection, but the installed app does not depend on that original folder.

Use `python3 install.py --dry-run` to inspect the installation plan. Optional flags include `--install-dir`, `--workspace`, `--desktop-dir`, `--no-shortcut`, and `--enable-alerts`. The default installation does not start alert delivery; enable it only after reviewing your client destinations.

If a desktop environment refuses to launch an unsigned local entry, use the terminal command below and review the operating system's normal trust prompt. Do not disable system protections globally.

## Start from a terminal

From the project folder:

```sh
python3 scripts/desktop.py --workspace "/path/to/your/workspace"
```

This is also useful when diagnosing a launcher problem. On Windows, replace `python3` with `py`.

For direct board commands without a desktop launcher:

```sh
python3 collab.py init
python3 collab.py serve
```

The default address is `http://127.0.0.1:8765`. The desktop launcher can reuse an existing server for the same workspace and choose a free local port when necessary. Direct `collab.py serve` runs in the foreground; stop that process with `Ctrl+C`.

Inspect or stop a desktop-managed board with:

```sh
python3 scripts/desktop.py status --workspace "/path/to/your/workspace"
python3 scripts/desktop.py stop --workspace "/path/to/your/workspace"
```

The stop command only stops a matching server started and recorded by the desktop launcher. It refuses to terminate a server started separately from a terminal. Closing a browser tab does not stop a background server.

The optional alert watcher is managed separately. With `COLLAB_WORKSPACE` set to the same workspace, stop it using `python3 alerts.py stop` when appropriate.

## Choose where your data lives

The installer creates a separate data workspace by default. For commands run directly from source, the source folder is the workspace unless another setting is provided. Choose a different workspace folder using `--workspace` with the launcher. All agents collaborating on one board must use the same workspace path on the same computer.

For other command-line tools, set `COLLAB_WORKSPACE` before running them:

```sh
export COLLAB_WORKSPACE="/path/to/your/workspace"
python3 collab.py init
```

PowerShell:

```powershell
$env:COLLAB_WORKSPACE = "C:\path\to\your\workspace"
py collab.py init
```

Source modules stay in the project folder. The workspace contains private runtime files such as `state/`, `BOARD.md`, and `shared/`. The applications create required data folders when needed.

## Connect clients

Run this from the installed app folder printed by the installer so generated server and hook paths survive deletion of the download folder:

```sh
python3 setup_clients.py --workspace "/path/to/your/workspace"
```

Review the generated files before enabling them in your agent clients. Generation does not edit your global settings, authenticate accounts, or guarantee a client will load a configuration before it is restarted. Follow [agent connections](agents.md).

Fresh model consultations are disabled by default. Add `--allow-consults` to the generator only when you want to enable external calls that may consume credits. Normal board collaboration does not require this option.

## Updates and backups

The default installation separates application files from private workspace data. Before updating, stop the dashboard and alert watcher and back up the workspace. Download the updated source and run the installer again with the same installation and workspace choices; the installer updates its managed app folder while preserving workspace data.

Restart connected MCP clients after updating. Regenerate and review client snippets from the installed app to adopt new environment settings, including the consultation opt-in and shared usage limits. Existing clients without an explicit opt-in cannot launch consultations after this update.

For a consistent SQLite backup, stop all processes using the board before copying its workspace. Copying a database file alone while its write-ahead log is active can omit recent changes. Keep backups private; they contain conversations and agent routing details.

The GitLab repository and its source archives contain the application, examples, and tests. They do not include anyone's running board or account configuration.
