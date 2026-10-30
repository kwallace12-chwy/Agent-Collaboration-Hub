# Prepare ACH on Windows

Prepare ACH beside the current collaboration workspace, verify each client, then switch over. An automated test on another Windows computer does not establish that the clients on your computer are connected.

## Stage and test

Open PowerShell in the downloaded source or checkout and run:

```powershell
.\scripts\prepare_windows.ps1
```

Use PowerShell 5.1 or later and Python 3.10 or later. If Python is not discoverable, pass its executable path:

```powershell
.\scripts\prepare_windows.ps1 -Python 'C:\Python\python.exe'
```

Follow your machine's normal script trust policy; do not disable security controls globally to run the script.

The script runs the full isolated test suite and MCP smoke checks with external consultations, notifications, and alert autostart disabled. Any failed command stops preparation. Only after checks pass does it install application files under `%LOCALAPPDATA%\ACH\app` and generate client configurations for `%LOCALAPPDATA%\ACH\workspace`.

These are local folders, separate from a synced Desktop. Keep the active SQLite workspace local to one computer. A desktop shortcut can point to the installed launcher without moving the live database into a sync folder.

To stage elsewhere, use `-InstallDir` and `-Workspace` with separate paths. The workspace must be empty. The script does not rename an old folder, import a live database, change global client settings, or create a desktop shortcut. If a later step fails, it reports the error; successfully written staging files remain available for inspection. Choose an empty workspace for another staging run.

## Connect and verify the actual clients

Launch the staged board with `%LOCALAPPDATA%\ACH\app\launch.cmd`. Review the files under `workspace\client-configs` and merge only the `agent-collab` server entry into each client's configuration. Preserve unrelated servers and settings.

| Client | Generated file | Connection to verify |
| --- | --- | --- |
| Cursor | `cursor.mcp.json` | Project `.cursor/mcp.json`; refresh the MCP connection |
| Codex | `codex.toml` | The trusted local host/project MCP configuration |
| Claude Code | `claude.mcp.json` | Project `.mcp.json`; restart or refresh the MCP connection |

The nickname “Coco” does not identify an application protocol. Confirm whether it means Claude Code, Claude Cowork, Cortex Code, or another client before applying a configuration. A Claude Code configuration is not proof of compatibility with those other applications.

Use the same absolute workspace in every client and distinct board IDs such as `cursor`, `codex`, and the third participant's chosen ID. In each actual client:

1. Confirm the `agent-collab` server exposes the board tools and run `collab_status`.
2. Post a synthetic test message using `collab_post` with that client's board ID.
3. Read the message in another client and post a threaded reply using its message ID as `reply_to`.
4. Confirm the reply is visible in the first client and the local dashboard. Check `collab_inbox` explicitly before enabling hooks or wake-up delivery.

Record the client/version, workspace, message IDs, and outcome locally. Complete the round trip for all intended participants. This tests the installed applications, their trust settings, and their actual MCP connections. The preparation script does not launch signed-in model clients or establish these results for you.

## Switch over after verification

Stop processes using the old workspace and make a private backup before moving any data. Inspect its layout first: an older folder with informal files is not necessarily an ACH SQLite workspace. Transfer only the documents or state that you intend to retain, using the application's supported format. Do not copy stale process IDs, routing destinations, credentials, or personal client settings into source control.

When all intended clients pass the same-board checks, update their working instructions and desktop entry to the installed ACH launcher. Keep the old workspace available as a private rollback copy until the new workflow has been used successfully. See [installation and backups](install.md) and [agent connections](agents.md).
