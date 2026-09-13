# Changelog

Versioning note: this plugin tracks the version of the upstream Nowledge Mem
components it bundles (the Claude Code plugin's hooks and scripts). `0.7.24`
was the upstream component version at first assembly. Adaptations specific to
this plugin are listed under **Changed** / **Fixed**.

## [Unreleased]

### Added

- Bundled MCP server: `.mcp.json` (no API key) plus `"mcpServers"` in the
  manifest, so a local install works right after `plugin install` with no manual
  configuration. Verified end-to-end in a clean-user scenario — tools enumerate,
  search, and write, with the write confirmed through the `nmem` CLI.
- `scripts/nmem-install-mcp.py` — registers the MCP server in one command by
  reading `~/.nowledge-mem/config.json` (no hand-typed key, no hand-assembled
  JSON), with `--dry-run`, `--url` for remote Mem, and `--remove`.
- `scripts/nmem_runtime.py` — resolves `nmem` and `dimagent` when the host gives
  hooks a restricted `PATH`: standard install locations, Windows launchers, and
  `NMEM_CLI_PATH` / `DIMAGENT_CLI_PATH` overrides.
- `--all-sessions` history import in the capture adapter, with `--limit` and
  `--dry-run`, for conversations that predate the install.

### Fixed

- `nmem t create` is slow server-side (10-57s measured for a two-message thread),
  so the previous 30s timeout silently dropped reconstructions — five attempts
  in a row succeeded only twice. Timeout raised to 180s with one retry, and the
  real reason (timeout / rc / stderr) is logged instead of a misleading
  "no text messages".
- Capture no longer fails silently when either CLI is missing or session export
  fails: the reason is appended to `~/.cache/nmem-dimagent/capture.log`.
- nmem invocations go through `build_command()` so `.cmd` launchers work from
  Git Bash / WSL, and the detached worker uses `CREATE_NO_WINDOW` on Windows.

### Changed

- **Reversed the earlier decision to omit `mcpServers`.** Both reasons given for
  omitting it were wrong: duplicate registration only happens for users who
  already configured MCP by hand, and the `unsupported` column in
  `dimagent mcp list` is the *auth status* (`formatAuthStatus`), not an
  availability verdict — the plugin server answered real tool calls. The local
  Mem endpoint also does not check an API key, so the bundled file carries no
  secret.
- README install flow reduced to "install the plugin, restart"; manual MCP
  configuration now only applies to remote/Cloud endpoints or a non-default port.
- `sys.dont_write_bytecode = True` keeps `__pycache__` out of the installed
  copy when the adapter imports `nmem_runtime` at runtime.
- Comments trimmed in `scripts/nmem-capture-dimagent.py` and the tests to match
  the repo's plugin style (one-line module docstring, comments only for host
  quirks). No logic changes.
- `PreCompact` verified against 0.5.2: it fires when compaction completes,
  carries `session_id` plus `trigger`, and an absent `matcher` matches
  unconditionally. The capture path was exercised with a real payload and is
  idempotent (repeated triggers converge on one thread).
- Tests run with `PYTHONDONTWRITEBYTECODE=1` so `__pycache__` cannot ride along
  into the installed plugin.

## [0.7.24] - 2026-09-13

First assembled, verified, and repaired against DimAgent **0.5.2**.

### Added

- `.codex-plugin/plugin.json` — manifest declaring `skills` / `hooks` paths.
  Required because DimAgent reads component paths from manifest fields and has
  no directory-convention fallback; the official Claude manifest has no such
  fields.
- `scripts/nmem-capture-dimagent.py` — session capture adapter. The official
  `nmem-hook-save.py` requires a JSONL `transcript_path`, which DimAgent always
  reports as `null` (sessions live in SQLite). The adapter instead drives
  `dimagent session export` and rebuilds one Mem thread per session.
- `tests/test_dimagent_plugin.py` — 40 tests over session-id routing, fork
  handling, export→thread conversion, the CLI resolver, and the packaged
  manifest/hooks config.

### Changed

- `hooks/hooks.json` (from the official Claude Code plugin) with three edits:
  - removed the `SessionEnd` event — not in DimAgent's
    `SUPPORTED_COMMAND_HOOK_EVENTS`; a single unsupported event makes DimAgent
    discard the **entire** hooks file, silently.
  - `${CLAUDE_PLUGIN_ROOT:-}` → `${CLAUDE_PLUGIN_ROOT}` (6 occurrences) — the
    substitution regex is literal and does not match default-value syntax.
  - `Stop` / `PreCompact` / `SubagentStop` now invoke
    `nmem-capture-dimagent.py` instead of the official save hook.

### Fixed

- **Child sessions were never captured.** DimAgent 0.5.2 titles subagent
  sessions `Subagent <type> (fork #1)`, which is indistinguishable by title
  from a genuine session branch (`<name> (fork #N)`). The `SKIP_FORKS` guard
  therefore skipped every child session — silently, with exit code 0. The
  decision now keys off the `subagent_` session-id prefix, never the title.
- Verified that DimAgent 0.5.2 no longer truncates `dimagent session export`
  output to 64 KiB when stdout is a pipe (the upstream bug this adapter was
  originally written around). The temp-file redirection is retained as a
  no-op defence for older builds.

### Verified

Against DimAgent 0.5.2, end to end: plugin install, manifest preservation,
`SessionStart` context-bundle injection, `UserPromptSubmit` guidance,
`SubagentStart` routing context, skill loading, main-session capture, and
child-session capture.
