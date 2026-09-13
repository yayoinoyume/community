#!/usr/bin/env python3
"""Capture a DimAgent session into Nowledge Mem.

DimAgent exposes no JSONL transcript (the hook payload always reports
`transcript_path: null`), so the official `nmem-hook-save.py` cannot work here.
This adapter drives `dimagent session export` and rebuilds one thread per
session id, so repeated Stop/PreCompact captures converge on one thread.
Never raises into the host session.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the installed copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nmem_runtime import (  # noqa: E402
    build_command,
    find_dimagent_command,
    find_nmem_command,
    windows_no_window_kwargs,
)

THREAD_PREFIX = "dimagent-"
WORKER_FLAG = "--worker"
CHILD_ENV = "NMEM_DIMAGENT_CAPTURE_CHILD"
EXPORT_TIMEOUT = 30
# Measured on 0.5.2: one `nmem t create` of a two-message thread took 10-57s
# server-side, so a 30s budget silently dropped captures.
NMEM_TIMEOUT = 180
CREATE_ATTEMPTS = 2
CREATE_RETRY_DELAY_SECONDS = 2.0
# Keep one argv payload small enough for every platform's limit: macOS caps the
# whole argument list at 1 MB, Linux at 2 MB. Larger sessions go through append.
MAX_ARG_BYTES = 300_000
APPEND_BATCH = 20

# A fork replicates the source transcript under a new session id, so skipping
# forks keeps one thread per mainline (cost: post-fork turns are lost).
SKIP_FORKS = True
FORK_SUFFIX_RE = re.compile(r"\s*\(fork #\d+\)\s*$")

# Child sessions are titled "(fork #N)" too in 0.5.2, so fork detection must
# exclude them by id prefix — never by title.
SUBAGENT_PREFIX = "subagent_"

LOG_PATH = (
    Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    / "nmem-dimagent"
    / "capture.log"
)
LOG_MAX_BYTES = 64 * 1024


def _log(message: str) -> None:
    """Record why a capture did nothing; silent failures are otherwise invisible."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            LOG_PATH.unlink()
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
    except OSError:
        pass


def _locate_clis() -> tuple[str | None, str | None]:
    """Hosts often run hooks with a restricted PATH, so resolve explicitly."""
    return find_dimagent_command(), find_nmem_command()


def _is_fork(exported: dict) -> bool:
    name = str((exported.get("metadata") or {}).get("name") or "")
    return bool(FORK_SUFFIX_RE.search(name))


def _export_session(dim: str, sid: str):
    """Export a session as a dict, or None on failure."""
    # stdout is truncated to 64 KiB when it is a pipe, so always redirect to a file.
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json",
            prefix="nmem-export-", delete=False,
        ) as handle:
            tmp = handle.name
        with open(tmp, "w", encoding="utf-8") as out:
            proc = subprocess.run(
                build_command(dim, "session", "export", sid),
                stdout=out,
                stderr=subprocess.DEVNULL,
                timeout=EXPORT_TIMEOUT,
                check=False,
                **windows_no_window_kwargs(),
            )
        if proc.returncode != 0:
            return None
        with open(tmp, "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read().strip()
        if not raw:
            return None
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _run(
    argv: list[str],
    stdin: str | None = None,
    timeout: int = NMEM_TIMEOUT,
    cwd: str | None = None,
):
    try:
        return subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            cwd=cwd,
            **windows_no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _nmem(nmem: str, *args: str):
    return _run(build_command(nmem, *args), timeout=NMEM_TIMEOUT)


def _to_messages(exported: dict) -> list[dict]:
    """DimAgent parts[] -> Mem's [{role, content}] shape (text parts only)."""
    out: list[dict] = []
    for msg in exported.get("messages") or []:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        texts = [
            (part.get("text") or "").strip()
            for part in (msg.get("parts") or [])
            if part.get("type") == "text" and (part.get("text") or "").strip()
        ]
        content = "\n\n".join(texts).strip()
        if content:
            out.append({"role": role, "content": content})
    return out


def _title_for(exported: dict, messages: list[dict], sid: str) -> str:
    meta = exported.get("metadata") or {}
    name = FORK_SUFFIX_RE.sub("", str(meta.get("name") or "").strip()).strip()
    if name and name.lower() not in ("new session", "untitled"):
        return name[:120]
    first_user = next((m["content"] for m in messages if m["role"] == "user"), "")
    line = first_user.strip().splitlines()[0].strip() if first_user else ""
    return line[:60] if line else f"DimAgent {sid}"


def _session_id_from(payload: dict) -> str:
    """Return the session this event is about."""
    # SubagentStop carries the main session in session_id and the CHILD session
    # in agent_id (subagent_<ts>_<rand>) — the child is the one worth capturing.
    event = str(payload.get("hook_event_name") or "").strip()
    if event in ("SubagentStart", "SubagentStop"):
        sub = str(payload.get("agent_id") or "").strip()
        if sub.startswith(SUBAGENT_PREFIX):
            return sub
    return str(payload.get("session_id") or "").strip()


def _why(result) -> str:
    if result is None:
        return "timed out or could not be executed"
    detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")[:160]
    return f"rc={result.returncode} {detail}"


def _write_thread(nmem: str, sid: str, exported: dict) -> bool:
    messages = _to_messages(exported)
    if not messages:
        _log(f"skip {sid}: no text messages")
        return False

    thread_id = THREAD_PREFIX + sid
    title = _title_for(exported, messages, sid)
    blob = json.dumps(messages, ensure_ascii=False)
    oversized = len(blob.encode("utf-8")) > MAX_ARG_BYTES

    for attempt in range(1, CREATE_ATTEMPTS + 1):
        # Rebuild from the full snapshot so repeat triggers stay idempotent.
        # No --cascade: memories already extracted from this thread are preserved.
        _nmem(nmem, "t", "delete", thread_id, "-f", "-j")

        if oversized:
            # Too large for a single argv: seed with one message, then append in batches.
            head, rest = messages[0], messages[1:]
            seeded = _nmem(
                nmem, "t", "create", "--id", thread_id, "-s", "dimagent", "-t", title,
                "-m", json.dumps([head], ensure_ascii=False),
            )
            if seeded is None or seeded.returncode != 0:
                _log(f"create failed for {thread_id} (attempt {attempt}): {_why(seeded)}")
            else:
                for i in range(0, len(rest), APPEND_BATCH):
                    chunk = json.dumps(rest[i:i + APPEND_BATCH], ensure_ascii=False)
                    _nmem(nmem, "t", "append", thread_id, "-m", chunk)
                return True
        else:
            created = _nmem(
                nmem, "t", "create", "--id", thread_id, "-s", "dimagent", "-t", title, "-m", blob
            )
            if created is not None and created.returncode == 0:
                return True
            _log(f"create failed for {thread_id} (attempt {attempt}): {_why(created)}")

        if attempt < CREATE_ATTEMPTS:
            time.sleep(CREATE_RETRY_DELAY_SECONDS)
    return False


def capture(payload: dict) -> None:
    sid = _session_id_from(payload)
    if not sid:
        return

    dim, nmem = _locate_clis()
    if not dim or not nmem:
        _log(f"skip {sid}: dimagent={dim or 'not found'} nmem={nmem or 'not found'}")
        return

    exported = _export_session(dim, sid)
    if not exported:
        _log(f"skip {sid}: session export failed")
        return

    is_subagent = sid.startswith(SUBAGENT_PREFIX)
    if SKIP_FORKS and not is_subagent and _is_fork(exported):
        # Drop a stale fork thread without creating a new one.
        _nmem(nmem, "t", "delete", THREAD_PREFIX + sid, "-f", "-j")
        return

    if not _write_thread(nmem, sid, exported):
        return  # _write_thread already logged why


def _list_sessions(dim: str, cwd: str | None = None) -> list[tuple[str, str]]:
    # `session list` only reports sessions for its own working directory.
    result = _run(
        build_command(dim, "session", "list", "--json"),
        timeout=EXPORT_TIMEOUT,
        cwd=cwd,
    )
    if result is None or result.returncode != 0:
        return []
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    sessions: list[tuple[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("id") or "").strip()
        if sid:
            sessions.append((sid, str(row.get("name") or "").strip()))
    return sessions


def import_history(
    limit: int = 0,
    dry_run: bool = False,
    cwd: str | None = None,
) -> int:
    """Rebuild a thread for every session DimAgent has for one directory."""
    dim, nmem = _locate_clis()
    if not dim or not nmem:
        print("Cannot import history: dimagent or nmem not found.", file=sys.stderr)
        return 1

    sessions = _list_sessions(dim, cwd=cwd)
    if limit > 0:
        sessions = sessions[:limit]
    if not sessions:
        print(f"No sessions to import for {cwd or os.getcwd()}.")
        print("Note: DimAgent groups sessions by cwd, so run from the project directory or pass --cwd.")
        return 0

    print(f"Found {len(sessions)} session(s)" + (" (dry run, nothing written)" if dry_run else ""))
    imported = skipped = failed = 0
    for index, (sid, name) in enumerate(sessions, 1):
        exported = _export_session(dim, sid)
        if not exported or _is_fork(exported) or not _to_messages(exported):
            skipped += 1  # empty, unreadable, or forked sessions have nothing to store
            continue
        if dry_run:
            print(f"  {index}/{len(sessions)}  {sid}  {name[:60]}")
            continue
        if _write_thread(nmem, sid, exported):
            imported += 1
        else:
            failed += 1
        if index % 10 == 0:
            print(f"  ... {index}/{len(sessions)}")
    print(f"Done: imported {imported}, skipped {skipped}, failed {failed}")
    return 0


def main(argv: list[str]) -> int:
    if "--all-sessions" in argv:
        limit = 0
        if "--limit" in argv:
            position = argv.index("--limit")
            if position + 1 < len(argv) and argv[position + 1].isdigit():
                limit = int(argv[position + 1])
        cwd = None
        if "--cwd" in argv:
            position = argv.index("--cwd")
            if position + 1 < len(argv):
                cwd = argv[position + 1]
        return import_history(limit=limit, dry_run="--dry-run" in argv, cwd=cwd)

    raw = sys.stdin.read()

    if WORKER_FLAG in argv:
        # Detached worker: payload arrives via the file path in argv.
        path = argv[argv.index(WORKER_FLAG) + 1] if len(argv) > argv.index(WORKER_FLAG) + 1 else ""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return 0
        try:
            os.unlink(path)
        except OSError:
            pass
        try:
            capture(json.loads(text))
        except Exception:
            pass
        return 0

    # Detach the capture so a slow export never blocks the host session.
    if os.environ.get(CHILD_ENV) == "1":
        try:
            capture(json.loads(raw) if raw.strip() else {})
        except Exception:
            pass
        return 0

    if not raw.strip():
        return 0
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", prefix="nmem-dimagent-", delete=False
        )
        with handle:
            handle.write(raw)
        env = dict(os.environ, **{CHILD_ENV: "1"})
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), WORKER_FLAG, handle.name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            **windows_no_window_kwargs(),
        )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        sys.exit(0)
