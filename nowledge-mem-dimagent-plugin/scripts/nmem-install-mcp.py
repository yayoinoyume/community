#!/usr/bin/env python3
"""Register the Nowledge Mem MCP server with DimAgent in one command.

Reads the endpoint and API key from the local nmem config, then drives
`dimagent mcp add-json`. Replaces the hand-assembled JSON in README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nmem_runtime import (  # noqa: E402
    build_command,
    find_dimagent_command,
    find_nmem_command,
    windows_no_window_kwargs,
)

SERVER_NAME = "nowledge-mem"
DEFAULT_ENDPOINT = "http://127.0.0.1:14242/mcp"
CONFIG_PATH = Path.home() / ".nowledge-mem" / "config.json"
TIMEOUT = 30


def _load_credentials() -> tuple[str, str]:
    """Return (endpoint, api_key) from ~/.nowledge-mem/config.json."""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"{CONFIG_PATH} not found; run `nmem connect` first.")
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read {CONFIG_PATH}: {exc}")
    if not isinstance(raw, dict):
        raise SystemExit(f"{CONFIG_PATH} is not a JSON object.")
    api_key = str(raw.get("apiKey") or "").strip()
    if not api_key:
        raise SystemExit(f"no apiKey in {CONFIG_PATH}; run `nmem connect` first.")
    base = str(raw.get("apiUrl") or "").strip().rstrip("/")
    return (base + "/mcp") if base else DEFAULT_ENDPOINT, api_key


def _server_config(endpoint: str, api_key: str) -> dict:
    return {
        "type": "http",
        "url": endpoint,
        "headers": {
            "APP": "DimAgent",
            "X-Nmem-Tool-Set": "external-agent",
            "X-Nowledge-Tool-Schema-Profile": "slim",
            "Authorization": f"Bearer {api_key}",
            "X-NMEM-API-Key": api_key,
        },
    }


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT,
        check=False,
        **windows_no_window_kwargs(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Override the MCP endpoint (remote Mem or Cloud workspace)")
    parser.add_argument("--dry-run", action="store_true", help="Print the commands without running them")
    parser.add_argument("--remove", action="store_true", help=f"Remove the {SERVER_NAME} entry")
    args = parser.parse_args(argv)

    dimagent = find_dimagent_command()
    if not dimagent:
        raise SystemExit("dimagent executable not found; install it or add it to PATH.")

    if args.remove:
        result = _run(build_command(dimagent, "mcp", "remove", SERVER_NAME))
        sys.stdout.write(result.stdout or result.stderr)
        return result.returncode

    endpoint, api_key = _load_credentials()
    if args.url:
        endpoint = args.url
    if find_nmem_command() is None:
        sys.stderr.write("warning: nmem CLI not found; capture and retrieval will not work.\n")

    payload = json.dumps(_server_config(endpoint, api_key), ensure_ascii=False)
    masked = json.dumps(_server_config(endpoint, api_key[:8] + "..."), ensure_ascii=False)

    if args.dry_run:
        print(f"# endpoint: {endpoint}")
        print(f"{dimagent} mcp remove {SERVER_NAME}")
        print(f"{dimagent} mcp add-json {SERVER_NAME} '{masked}'")
        return 0

    # `add-json` has no --force, so drop the old entry to keep this re-runnable.
    _run(build_command(dimagent, "mcp", "remove", SERVER_NAME))
    added = _run(build_command(dimagent, "mcp", "add-json", SERVER_NAME, payload))
    if added.returncode != 0:
        sys.stderr.write(added.stderr or added.stdout)
        return added.returncode

    listed = _run(build_command(dimagent, "mcp", "list"))
    sys.stdout.write(listed.stdout or listed.stderr)
    if SERVER_NAME not in (listed.stdout or ""):
        sys.stderr.write(f"config not applied: {SERVER_NAME} missing from `dimagent mcp list`.\n")
        return 1
    print(f"\nDone. Restart the DimAgent session, then check with `dimagent mcp test {SERVER_NAME}`.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
