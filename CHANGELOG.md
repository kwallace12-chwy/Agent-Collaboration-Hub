# Changelog

## Unreleased

### Fixed

- Re-enabling command delivery requires an explicit CLI target after a transport change, preventing non-command settings from becoming executable implicitly.
- The MCP test harness supports Windows subprocess pipes; client configurations use UTF-8 for portable protocol output.
- MCP cannot replace a CLI-configured command alert destination through aliases or omitted transport fields.
- Pause applies to forced notifications, urgent sends, hooks, relay pushes, desktop reminders, and explicit delivery tests. External attempts share hard per-recipient and workspace limits, including failures.
- First notifications reach the inbox, direct messages do not wait behind a scanner backlog, and muted or self notifications no longer claim successful queueing.
- Concurrent advisory updates preserve every participant's response, publish complete state atomically, and reject corrupt state without resetting it.
- Installed consultations and hooks use the installed protocol and workspace-aware paths instead of assuming app code lives in the data folder.
- Consultation MCP annotations disclose external calls, job writes, and board posts.
- Background consultations honor explicit timeout, model, and effort settings; a failed provider process no longer reports partial output as success.

### Added

- Native Windows CI on Python 3.10 and 3.14, including real shortcut and launcher checks.
- Cursor MCP configuration and an isolated connection test that exchanges a message through separate server processes.
- Host opt-in for external consultations, shared concurrency and hourly limits, atomic job records, and a guard against nested consultations.
- Evidence-based adversarial review guidance with independent first passes, disagreement tracking, and a stopping rule.
- Regression coverage for delivery authority, concurrent writers, consultation controls, and installed workspace separation; test subprocesses explicitly disable paid consultations.
- Public source distribution with a local board, MCP tools, alerts, task ownership, file locks, decision voting, consultations, and advisory workflows.
- Installation and desktop-launch support, plus generated examples for connecting agent clients.
- Documentation for separate workspaces, agent model preferences, contributor workflows, original design, and private runtime data.
- GitLab issue and merge-request templates with reproduction and validation guidance.
- Isolated regression and MCP checks for core coordination behavior.

### Clarified

- The preferred Claude model is Opus 5.5 at high effort.
- Argon High to Gemini 3.8 Flash High fallback is a manual agent-surface policy.
- GitLab hosts source code; it does not host a public board or provide browser ChatGPT access to a local server.
- Windows compatibility requires verification on Windows.
