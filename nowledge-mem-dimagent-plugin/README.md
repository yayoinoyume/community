# Nowledge Mem for DimAgent

Cross-tool memory for [DimAgent](https://dimagent.com/docs) — the agent reads your
working context at startup, searches past decisions on demand, and captures
conversations (including subagent sessions) into Nowledge Mem.

> **Status**: community-built. DimAgent is **not** in the official Nowledge Mem
> integration list, and there is no official DimAgent package. This plugin is
> assembled from official Nowledge Mem components (Claude Code plugin scripts,
> shared skills) plus a DimAgent-specific capture adapter. Everything here has
> been verified against DimAgent **0.5.2**; the deviations from the official
> components are documented under "How this plugin differs" below.

---

## What you get

| Capability | How |
|---|---|
| Agent can read/write memory | bundled `nmem` MCP server — `.mcp.json`, no key needed locally |
| Working Memory + Context Bundle injected at session start | `SessionStart` hook → `nmem-hook-read.sh` |
| Proactive recall / save reminders each turn | `UserPromptSubmit` hook |
| Subagent gets its own bounded context | `SubagentStart` hook → `nmem-hook-subagent.py` |
| Conversations captured into Mem | `Stop` / `PreCompact` → `nmem-capture-dimagent.py` |
| Subagent conversations captured too | `SubagentStop` → same adapter |
| Conversations from before install | `--all-sessions` import (see [Install](#install)) |
| 7 shared skills | `skills/` (from `nowledge-mem-npx-skills`) |

---

## Prerequisites

| Requirement | Check |
|---|---|
| Nowledge Mem running and reachable | `nmem status` → `status ok` |
| DimAgent installed | `dimagent --version` |
| A working model provider | `dimagent provider list` |
| `nmem` reachable by the hooks | `python3 scripts/nmem_runtime.py` prints both paths |

> **Hooks run with a restricted `PATH`.** They are launched by the DimAgent
> process and inherit *its* environment, not your interactive shell's. The
> capture adapter resolves both CLIs itself — `PATH` first, then the standard
> install locations (`/usr/local/bin`, `/usr/bin`, `/opt/homebrew/bin`,
> `~/.local/bin`, `~/.local/share/nowledge-mem/bin/nmem-wrapper`, and the Windows
> `%LOCALAPPDATA%` / `%APPDATA%` layouts) — and records the reason in
> `~/.cache/nmem-dimagent/capture.log` instead of failing silently.
>
> **If either CLI lives elsewhere** (a conda env, an nvm bin directory, a custom
> prefix), set `NMEM_CLI_PATH` / `DIMAGENT_CLI_PATH` to its absolute path. That is
> the supported escape hatch, and it wins over everything else.
>
> The two context-injection hooks (`SessionStart` and `SubagentStart`) are the
> official scripts and still look up `nmem` on `PATH` alone. On a machine where
> `nmem` lives only inside a conda env or an nvm bin directory, start DimAgent
> from a shell where `nmem` is already on `PATH` — otherwise those two inject
> nothing while capture keeps working.

---

## Install

### 1. Install the plugin

```bash
dimagent plugin install /path/to/nowledge-mem-dimagent-plugin
```

Installs to `~/.dimcode/v2/plugins/nowledge-mem/` — `DIMCODE_HOME`, or
`XDG_CONFIG_HOME/.dimcode/v2`, overrides that default, so check
`dimagent plugin install`'s own output if you set either. The install copies the
source tree verbatim and adds one `dim-install.json` provenance file; it does
**not** rewrite `.codex-plugin/plugin.json`.

Restart any running DimAgent session — hooks, skills, and the bundled MCP server
load at startup. **No further configuration is needed**: the plugin ships
`.mcp.json`, so the memory tools are there as soon as the session restarts.

### 2. Import existing sessions (optional)

Hooks only capture conversations that happen *after* they are installed. To pull
in the sessions DimAgent already has on disk:

```bash
python3 scripts/nmem-capture-dimagent.py --all-sessions --dry-run   # preview
python3 scripts/nmem-capture-dimagent.py --all-sessions             # import all
python3 scripts/nmem-capture-dimagent.py --all-sessions --limit 20  # newest 20
```

Each session becomes one thread (`dimagent-<session_id>`); forked sessions are
skipped, and re-running is safe because every write rebuilds the thread.

### Overriding the MCP server (optional)

The bundled `.mcp.json` points at the local Mem (`http://127.0.0.1:14242/mcp`) and
carries **no API key** — the local server answers without one, and a published
file must never hold a secret. You only need your own config for:

- **Remote Mem / Nowledge Cloud / Access Anywhere** — those endpoints do require
auth, so keep the key in your private client config rather than in this package.
- **A non-default local port.**

```bash
python3 scripts/nmem-install-mcp.py            # reads endpoint + key from nmem
python3 scripts/nmem-install-mcp.py --dry-run  # preview, key masked
python3 scripts/nmem-install-mcp.py --url https://<workspace>/mcp   # remote Mem
python3 scripts/nmem-install-mcp.py --remove   # undo
```

The script reads `~/.nowledge-mem/config.json` and removes any previous entry
first (`dimagent mcp add-json` has no `--force` flag), so it is safe to re-run.

Note that DimAgent registers plugin servers as `plugin:<plugin>/<id>` and never
merges them with `mcp.json`, so your own entry **adds a second server** instead
of replacing the bundled one. Both work; duplicate tool sets are just noisy —
disable whichever you do not want.

### Upgrading from a hand-configured setup

Earlier versions did not ship `.mcp.json`, so MCP was configured by hand. If you
did that, remove the manual entry after upgrading, otherwise you get two copies
of the same tools:

```bash
dimagent mcp remove nowledge-mem             # the manual one
dimagent mcp list                            # expect only plugin:nowledge-mem/nowledge-mem
```

---

## Verify

```bash
# 1. Files are in place (the tree is copied verbatim; stray __pycache__ shows up
#    here if you ran the tests without PYTHONDONTWRITEBYTECODE=1)
find ~/.dimcode/v2/plugins/nowledge-mem -type f | sort

# 2. Startup context injection
dimagent exec "Answer only: does your context contain 'Nowledge Mem Context Bundle'? yes/no. Do not call tools."

# 3. Per-turn guidance
dimagent exec "Answer only: does your context contain a '[Nowledge Mem]' line mentioning 'Search proactively'? yes/no. Do not call tools."

# 4. Skills loaded
dimagent exec "List only the names of skills containing 'memory' or 'working'."

# 5. Conversation capture (main + subagent)
dimagent exec "Delegate a small task to a subagent: compute 1+1, then relay the result."
sleep 15
nmem --json t list --limit 300 | python3 -c "
import sys, json
rows = [t['id'] for t in json.load(sys.stdin)['threads'] if 'dimagent' in str(t.get('source',''))]
print('dimagent threads:', len(rows))
print('subagent threads:', [r for r in rows if 'subagent_' in r])
"
```

## If capture seems not to work

```bash
# Did the adapter run, and why did it stop?
cat ~/.cache/nmem-dimagent/capture.log

# Can the hooks find both CLIs?
python3 scripts/nmem_runtime.py

# Is a thread actually there?
nmem --json t list --limit 50 | python3 -c "
import sys, json
print([t['id'] for t in json.load(sys.stdin)['threads'] if t['id'].startswith('dimagent-')])
"
```

The log is only written when a capture could not complete, so an absent file
means nothing has gone wrong yet.

---

### Commands that will mislead you

Do **not** judge success by these — all three are known-broken or meaningless:

| Command | What you see | Reality |
|---|---|---|
| `dimagent mcp test` | `tools: []` | Display bug (reads `result.tools`; internal field is `toolCount`). Use `dimagent exec` instead. |
| `dimagent hooks list --json` | `{"hooks":[]}` | CLI omits `pluginHookSources`; plugin hooks *are* registered at runtime. |
| `dimagent plugin list` | usage error | The subcommand does not exist — only `marketplace` and `install`. |

`dimagent mcp list` showing `disconnected` / `not_authenticated` is also normal:
each CLI call is a fresh process, and auth uses static headers rather than the
OAuth store it inspects.

---

## Update / uninstall

```bash
# update: edit the source tree, then
rm -rf ~/.dimcode/v2/plugins/nowledge-mem
dimagent plugin install /path/to/nowledge-mem-dimagent-plugin

# uninstall
rm -rf ~/.dimcode/v2/plugins/nowledge-mem   # takes skills, hooks and the bundled MCP server with it
dimagent mcp remove nowledge-mem            # only if you also added a manual entry yourself
```

---

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

`PYTHONDONTWRITEBYTECODE=1` keeps `__pycache__` out of the tree: `dimagent plugin
install` copies the source directory verbatim, so stray bytecode ends up inside
the installed plugin.

40 tests covering the capture adapter's session-id routing, fork handling
(including the child-session regression), export→thread conversion, failure
logging, CLI discovery under a restricted `PATH`, history import, and the
packaged manifest/hooks config.

---

## How this plugin differs from the official ones

DimAgent's plugin system is a hybrid: it reads **Codex-style manifest fields**
(`skills`, `hooks`, `mcpServers` — no directory-convention fallback) but
substitutes the **Claude-style `${CLAUDE_PLUGIN_ROOT}`** variable and uses
Claude-style hook event names.

Consequences:

- `.codex-plugin/plugin.json` is **required** — the Claude manifest has no
  component-path fields and cannot be used.
- Claude Code's `hooks.json` works after two fixes: drop the `SessionEnd` event
  (not in DimAgent's allowlist — it silently discards the **entire** hooks file)
  and strip `:-` default syntax from `${CLAUDE_PLUGIN_ROOT:-}` (the substitution
  regex is literal).
- Claude Code's `nmem-hook-save.py` **cannot** work here: it needs a JSONL
  `transcript_path`, which DimAgent always reports as `null`. That is why
  `scripts/nmem-capture-dimagent.py` exists.
- The manifest **ships** `mcpServers`, pointing at a bundled `.mcp.json` with no
  API key (verified: the local Mem server answers without one, and the official
  Codex plugin does the same). DimAgent namespaces plugin servers as
  `plugin:<plugin>/<id>`, so this **coexists with** rather than replaces any
  hand-written `mcp.json` entry — if you already had one, remove it after
  upgrading. A plugin server can also only be toggled, not `mcp remove`d.

### Platform support

**Supported: WSL2 / Linux. Untested: macOS. Unsupported: Windows.**

Only WSL2 / Linux has been run for real. The adapter and the CLI resolver carry
the same cross-platform code paths the official Codex plugin uses (`.cmd`
launching, `CREATE_NO_WINDOW`, WSL↔Windows path conversion) and already probe
`/opt/homebrew/bin`, but none of that has been exercised on real macOS hardware —
treat macOS as untested rather than broken.

Windows is a different case: the hook commands in `hooks.json` are POSIX shell,
which Windows does not provide, so the hooks cannot run there at all.

---

## Links

- [Nowledge Mem docs](https://mem.nowledge.co/zh/docs)
- [DimAgent docs](https://dimagent.com/docs)
- [community repo](https://github.com/nowledge-co/community)
