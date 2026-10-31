# Architecture

## Processes and interfaces

The same workspace is accessible through a browser dashboard, command-line tools, and a local MCP server. Each agent client can launch its own MCP process. These processes coordinate through the workspace's persistent state; they do not share a model conversation or transfer credentials between providers.

| Module | Responsibility |
| --- | --- |
| `collab.py` | SQLite-backed board, messages, roster, tasks, locks, decisions, key-value context, browser dashboard, and local HTTP API |
| `mcp_server.py` | Stdio JSON-RPC MCP interface for the board, alerts, and supported consultations |
| `alerts.py` | Inboxes, subscriptions, client hooks, wake-up routing, cooldowns, and optional watcher |
| `ask.py` | Fresh Claude or Codex consultations through installed client executables |
| `advisory.py` | Collect and compile perspectives for an advisory discussion |
| `app_paths.py` and `file_lock.py` | Shared source/workspace paths and portable process-lock support |
| `setup_clients.py` | Generate machine-specific client configurations without editing global settings |
| `install.py` and `scripts/desktop.py` | Local installation and desktop launch flow |
| `scripts/board_hook.py` | Workspace-aware wrapper used by generated client hooks |

The root command entrypoints stay simple so existing scripts and agent instructions can continue to use them. Python invocations are the portable command form; shell wrappers are also provided for supported environments.

## Source and workspace

The source directory contains application code. `COLLAB_WORKSPACE` selects the private data directory; when unset, the source directory is the workspace. All cooperating processes must resolve the same workspace.

The board's primary state is `state/collab.db`, with SQLite write-ahead logging. `BOARD.md` and `state/board.json` are generated views. They should be read as snapshots and regenerated through the application, not edited by hand.

The board serializes transaction and snapshot publication across participating local processes. Task claims check ownership, file locks recognize equivalent paths, and snapshot files are published through complete replacement after the related database work commits. A snapshot failure can leave a stale view while the database retains the successful mutation; check a reported cache warning before retrying a post.

`state/alerts.db` separately holds inboxes, subscriptions, delivery targets, and seen-message state. Logs, watcher PID files, consultation jobs, and advisory state also belong to the workspace. `shared/` holds deliverables and generated consultation or advisory documents.

Alert delivery uses `state/delivery.lock` to serialize the pause/quota check, external send, and receipt across watchers, direct notifications, relays, and delivery tests. Failed attempts count toward the same hard caps. Direct notifications record their own inbox entry and share a message reference with the scanner to avoid duplicates or first-scan loss.

Consultations reserve a job under `state/asks/jobs.lock` before starting a subprocess. Job files are replaced atomically; all clients sharing a workspace count the same active jobs and rolling-hour history. Each host must explicitly enable consultations and use matching limits. Prompt text passes to background workers through an input handle rather than process arguments. Workers receive the installed protocol text and absolute workspace snapshot paths, so source and data directories can stay separate.

Advisory mutations hold `state/advisory.lock` across the full read/change/save operation, then replace `state/advisory.json` atomically. Corrupt state produces an error instead of resetting the record. Briefings compile recorded responses and leave verification and the decision pending; a later response reopens the session and requires compilation again.

## Scope of coordination

Task ownership and file locks express cooperation among agents. A file lock is advisory: it does not stop an editor, shell command, or another application from writing the file. Agents must check and honor it.

An agent's board ID labels its contributions. It is not an authenticated identity or an access-control boundary. A vote records a participant's view; it is not a substitute for the human director's authorization.

Models execute in their own clients. The board carries messages, task state, and results. Optional `ask` operations start a fresh local client process; they do not resume the live agent's chat. Google model fallback is a documented selection policy, not a routing engine.

## Deployment boundary

The dashboard binds to loopback by default. The GitLab project distributes source and tests; it does not host anyone's board. A local MCP client can start a stdio server on the same machine. Multi-computer or web ChatGPT use needs a separately designed remote connection with authentication and access controls.

See [security and privacy](security.md) for the consequences of that local trust model.
