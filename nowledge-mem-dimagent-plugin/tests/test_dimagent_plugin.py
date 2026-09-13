"""Tests for the DimAgent Nowledge Mem plugin.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
CAPTURE_PATH = PLUGIN_ROOT / "scripts" / "nmem-capture-dimagent.py"
HOOKS_JSON_PATH = PLUGIN_ROOT / "hooks" / "hooks.json"
MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"

# DimAgent 0.5.2 `SUPPORTED_COMMAND_HOOK_EVENTS`; anything outside it discards the whole hooks file.
SUPPORTED_HOOK_EVENTS = {
    "SessionStart",
    "SubagentStart",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "UserPromptSubmit",
    "SubagentStop",
    "Stop",
}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def exported(name: str, texts: list[tuple[str, str]] | None = None) -> dict:
    texts = texts if texts is not None else [("user", "hello"), ("assistant", "hi")]
    return {
        "metadata": {"name": name},
        "messages": [
            {"role": role, "parts": [{"type": "text", "text": text}]}
            for role, text in texts
        ],
    }


class CaptureModuleTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("nmem_capture_dimagent", CAPTURE_PATH)
        # Keep the real ~/.cache/nmem-dimagent/capture.log free of test noise.
        log_patcher = mock.patch.object(self.mod, "_log")
        log_patcher.start()
        self.addCleanup(log_patcher.stop)

    def test_subagent_events_read_agent_id(self):
        for event in ("SubagentStart", "SubagentStop"):
            payload = {
                "hook_event_name": event,
                "session_id": "sess_1",
                "agent_id": "subagent_123",
            }
            self.assertEqual(self.mod._session_id_from(payload), "subagent_123", event)

    def test_other_events_read_session_id(self):
        payload = {"hook_event_name": "Stop", "session_id": "sess_1", "agent_id": "agent-identity"}
        self.assertEqual(self.mod._session_id_from(payload), "sess_1")

    def test_subagent_event_without_child_id_falls_back_to_session(self):
        payload = {"hook_event_name": "SubagentStop", "session_id": "sess_1", "agent_id": "agent-identity"}
        self.assertEqual(self.mod._session_id_from(payload), "sess_1")

    def test_fork_suffix_is_detected(self):
        self.assertTrue(self.mod._is_fork(exported("你好 (fork #1)")))

    def test_plain_title_is_not_a_fork(self):
        self.assertFalse(self.mod._is_fork(exported("你好")))

    def capture_with(self, payload: dict, export: dict) -> list[list[str]]:
        with mock.patch.object(self.mod, "_export_session", return_value=export), \
                mock.patch.object(
                    self.mod, "_locate_clis",
                    return_value=("/fake/bin/dimagent", "/fake/bin/nmem"),
                ), \
                mock.patch.object(self.mod.time, "sleep"), \
                mock.patch.object(self.mod, "_run") as run:
            self.mod.capture(payload)
        return [call.args[0] for call in run.call_args_list]

    def test_subagent_with_fork_suffix_is_still_captured(self):
        """Regression (0.5.2): child sessions carry a "(fork #N)" title too, so
        fork detection must key off the `subagent_` id prefix.
        """
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "sess_1",
            "agent_id": "subagent_123",
        }
        argvs = self.capture_with(payload, exported("Subagent tui_worker (fork #1)"))
        created = [a for a in argvs if "create" in a]
        self.assertTrue(created, "child session was skipped as a fork (regression)")
        self.assertIn("dimagent-subagent_123", created[0])

    def test_real_branch_of_main_session_is_skipped(self):
        payload = {"hook_event_name": "Stop", "session_id": "sess_1"}
        argvs = self.capture_with(payload, exported("你好 (fork #1)"))
        self.assertFalse([a for a in argvs if "create" in a], "branch was not skipped")
        self.assertTrue([a for a in argvs if "delete" in a], "stale branch thread was not cleaned")

    def test_child_thread_id_is_prefixed_and_scoped_to_child(self):
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "sess_1",
            "agent_id": "subagent_123",
        }
        argvs = self.capture_with(payload, exported("Subagent tui_worker"))
        created = [a for a in argvs if "create" in a]
        self.assertTrue(created)
        argv = created[0]
        self.assertEqual(argv[argv.index("--id") + 1], "dimagent-subagent_123")

    def test_title_fork_suffix_is_stripped_from_thread_title(self):
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "sess_1",
            "agent_id": "subagent_123",
        }
        argvs = self.capture_with(payload, exported("Subagent tui_worker (fork #3)"))
        created = [a for a in argvs if "create" in a]
        self.assertTrue(created)
        argv = created[0]
        self.assertEqual(argv[argv.index("-t") + 1], "Subagent tui_worker")

    def test_to_messages_keeps_text_parts_only(self):
        export = {
            "metadata": {"name": "x"},
            "messages": [
                {"role": "user", "parts": [{"type": "text", "text": "q"}]},
                {"role": "assistant", "parts": [
                    {"type": "thinking", "thinking": "..."},
                    {"type": "text", "text": "a"},
                    {"type": "tool_use", "name": "t"},
                ]},
                {"role": "tool_result", "parts": [{"type": "text", "text": "ignored"}]},
                {"role": "assistant", "parts": [{"type": "thinking", "thinking": "only"}]},
            ],
        }
        self.assertEqual(
            self.mod._to_messages(export),
            [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
        )

    def test_to_messages_empty_without_text(self):
        export = {
            "metadata": {},
            "messages": [{"role": "assistant", "parts": [{"type": "thinking", "thinking": "x"}]}],
        }
        self.assertEqual(self.mod._to_messages(export), [])

    def test_title_prefers_metadata_name(self):
        export = exported("我的会话")
        self.assertEqual(
            self.mod._title_for(export, self.mod._to_messages(export), "sess_1"), "我的会话"
        )

    def test_title_falls_back_to_first_user_line(self):
        export = exported("new session", [("user", "第一行\n第二行")])
        self.assertEqual(
            self.mod._title_for(export, self.mod._to_messages(export), "sess_1"), "第一行"
        )

    def test_title_falls_back_to_session_id(self):
        export = exported("Untitled", [("assistant", "说点什么")])
        self.assertEqual(
            self.mod._title_for(export, self.mod._to_messages(export), "sess_9"), "DimAgent sess_9"
        )

    # ---------- reliability ----------

    def test_write_thread_retries_after_a_failed_create(self):
        """`nmem t create` can take ~60s server-side; one retry recovers."""
        calls = [None, None, None, mock.Mock(returncode=0)]  # delete, create, delete, create
        with mock.patch.object(self.mod, "_nmem", side_effect=calls) as nmem_call, \
                mock.patch.object(self.mod.time, "sleep"), \
                mock.patch.object(self.mod, "_log"):
            self.assertTrue(self.mod._write_thread("/n", "sess_1", exported("A")))
        self.assertEqual(nmem_call.call_count, 4)

    def test_write_thread_gives_up_and_logs_the_reason(self):
        with mock.patch.object(self.mod, "_nmem", return_value=None), \
                mock.patch.object(self.mod.time, "sleep"), \
                mock.patch.object(self.mod, "_log") as log:
            self.assertFalse(self.mod._write_thread("/n", "sess_1", exported("A")))
        self.assertEqual(log.call_count, 2)
        self.assertIn("timed out", log.call_args_list[0].args[0])

    def test_write_thread_reports_create_error_text(self):
        failure = mock.Mock(returncode=1, stderr="boom", stdout="")
        with mock.patch.object(self.mod, "_nmem", return_value=failure), \
                mock.patch.object(self.mod.time, "sleep"), \
                mock.patch.object(self.mod, "_log") as log:
            self.mod._write_thread("/n", "sess_1", exported("A"))
        self.assertIn("rc=1 boom", log.call_args_list[0].args[0])

    def test_capture_logs_when_cli_is_missing(self):
        """A missing CLI used to be a silent no-op; it must leave a trace."""
        with mock.patch.object(self.mod, "_locate_clis", return_value=(None, None)), \
                mock.patch.object(self.mod, "_log") as log:
            self.mod.capture({"hook_event_name": "Stop", "session_id": "sess_1"})
        log.assert_called_once()
        self.assertIn("not found", log.call_args.args[0])

    def test_capture_logs_when_export_fails(self):
        with mock.patch.object(self.mod, "_locate_clis", return_value=("/d", "/n")), \
                mock.patch.object(self.mod, "_export_session", return_value=None), \
                mock.patch.object(self.mod, "_log") as log:
            self.mod.capture({"hook_event_name": "Stop", "session_id": "sess_1"})
        self.assertIn("export failed", log.call_args.args[0])

    def test_list_sessions_parses_json_rows(self):
        result = mock.Mock(
            returncode=0,
            stdout='[{"id":"a","name":"A"},{"id":"","name":"skip"},{},null]',
        )
        with mock.patch.object(self.mod, "_run", return_value=result):
            self.assertEqual(self.mod._list_sessions("/d"), [("a", "A")])

    def test_list_sessions_survives_bad_output(self):
        for result in (None, mock.Mock(returncode=1, stdout=""), mock.Mock(returncode=0, stdout="not json")):
            with mock.patch.object(self.mod, "_run", return_value=result):
                self.assertEqual(self.mod._list_sessions("/d"), [])

    def test_import_history_skips_forks_and_counts_writes(self):
        sessions = [("sess_1", "正常"), ("sess_2", "分支 (fork #1)")]
        exports = {sid: exported(name) for sid, name in sessions}
        with mock.patch.object(self.mod, "_locate_clis", return_value=("/d", "/n")), \
                mock.patch.object(self.mod, "_list_sessions", return_value=sessions), \
                mock.patch.object(self.mod, "_export_session", side_effect=lambda d, s: exports[s]), \
                mock.patch.object(self.mod, "_write_thread", return_value=True) as write, \
                mock.patch("builtins.print"):
            self.assertEqual(self.mod.import_history(), 0)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(write.call_args.args[1], "sess_1")

    def test_import_history_dry_run_writes_nothing(self):
        with mock.patch.object(self.mod, "_locate_clis", return_value=("/d", "/n")), \
                mock.patch.object(self.mod, "_list_sessions", return_value=[("sess_1", "A")]), \
                mock.patch.object(self.mod, "_export_session", return_value=exported("A")), \
                mock.patch.object(self.mod, "_write_thread") as write, \
                mock.patch("builtins.print"):
            self.assertEqual(self.mod.import_history(dry_run=True), 0)
        write.assert_not_called()

    def test_import_history_reports_missing_clis(self):
        with mock.patch.object(self.mod, "_locate_clis", return_value=(None, None)), \
                mock.patch("sys.stderr"):
            self.assertEqual(self.mod.import_history(), 1)

    def test_import_history_counts_empty_sessions_as_skipped(self):
        with mock.patch.object(self.mod, "_locate_clis", return_value=("/d", "/n")), \
                mock.patch.object(self.mod, "_list_sessions", return_value=[("sess_1", "empty")]), \
                mock.patch.object(self.mod, "_export_session", return_value={"metadata": {}, "messages": []}), \
                mock.patch.object(self.mod, "_write_thread") as write, \
                mock.patch("builtins.print") as printed:
            self.assertEqual(self.mod.import_history(), 0)
        write.assert_not_called()
        self.assertIn("skipped 1", " ".join(str(c.args[0]) for c in printed.call_args_list))

    def test_import_history_passes_cwd_through(self):
        """session list is cwd-scoped, so the requested directory must reach it."""
        with mock.patch.object(self.mod, "_locate_clis", return_value=("/d", "/n")), \
                mock.patch.object(self.mod, "_list_sessions", return_value=[]) as listed, \
                mock.patch("builtins.print"):
            self.mod.import_history(cwd="/some/project")
        listed.assert_called_once_with("/d", cwd="/some/project")


class RuntimeTests(unittest.TestCase):

    def setUp(self):
        self.rt = load_module("nmem_runtime", PLUGIN_ROOT / "scripts" / "nmem_runtime.py")

    def test_explicit_env_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "nmem"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            with mock.patch.dict(os.environ, {"NMEM_CLI_PATH": str(executable)}), \
                    mock.patch.object(self.rt.shutil, "which", return_value=None):
                self.assertEqual(self.rt.find_nmem_command(), str(executable))

    def test_returns_none_when_nothing_is_found(self):
        with mock.patch.object(self.rt, "_known_nmem_candidates", return_value=[]), \
                mock.patch.object(self.rt.shutil, "which", return_value=None):
            self.assertIsNone(self.rt.find_nmem_command())

    def test_plain_executable_is_not_wrapped(self):
        self.assertEqual(self.rt.build_command("/x/nmem", "t", "list"), ["/x/nmem", "t", "list"])

    def test_cmd_launcher_is_wrapped_for_posix_hosts(self):
        wrapped = self.rt.build_command("/x/nmem.cmd", "t", "list")
        if os.name == "nt":
            self.assertEqual(wrapped, ["/x/nmem.cmd", "t", "list"])
        else:
            self.assertIn("nmem.cmd", " ".join(wrapped))

    def test_no_window_kwargs_only_on_windows(self):
        kwargs = self.rt.windows_no_window_kwargs()
        self.assertEqual(bool(kwargs), os.name == "nt")


class PackagedConfigTests(unittest.TestCase):

    def setUp(self):
        self.hooks = json.loads(HOOKS_JSON_PATH.read_text(encoding="utf-8"))
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.hooks_raw = HOOKS_JSON_PATH.read_text(encoding="utf-8")

    def test_all_hook_events_are_supported(self):
        unsupported = set(self.hooks["hooks"]) - SUPPORTED_HOOK_EVENTS
        self.assertEqual(
            unsupported,
            set(),
            f"unsupported hook events discard the WHOLE hooks file: {unsupported}",
        )

    def test_session_end_is_absent(self):
        self.assertNotIn("SessionEnd", self.hooks["hooks"])

    def test_no_default_value_placeholder_syntax(self):
        """DimAgent only substitutes the exact ${CLAUDE_PLUGIN_ROOT} token."""
        self.assertNotIn("CLAUDE_PLUGIN_ROOT:-}", self.hooks_raw)

    def test_capture_events_point_to_the_dimagent_adapter(self):
        for event in ("Stop", "PreCompact", "SubagentStop"):
            command = self.hooks["hooks"][event][0]["hooks"][0]["command"]
            self.assertIn("nmem-capture-dimagent.py", command, event)

    def test_context_events_use_official_scripts(self):
        self.assertIn(
            "nmem-hook-read.sh",
            self.hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"],
        )
        self.assertIn(
            "nmem-hook-subagent.py",
            self.hooks["hooks"]["SubagentStart"][0]["hooks"][0]["command"],
        )

    def test_manifest_declares_component_paths(self):
        self.assertEqual(self.manifest["skills"], "./skills/")
        self.assertEqual(self.manifest["hooks"], "./hooks/hooks.json")

    def test_manifest_declares_the_bundled_mcp_server(self):
        """A bundled config must never carry a key, so the package stays publishable."""
        self.assertEqual(self.manifest["mcpServers"], "./.mcp.json")
        bundled = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
        server = bundled["mcpServers"]["nowledge-mem"]
        self.assertEqual(server["type"], "http")
        self.assertEqual(server["url"], "http://127.0.0.1:14242/mcp")
        headers = server.get("headers") or {}
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("X-NMEM-API-Key", headers)
        self.assertNotIn("apiKey", json.dumps(bundled))

    def test_required_files_exist(self):
        for rel in (
            ".codex-plugin/plugin.json",
            ".mcp.json",
            "hooks/hooks.json",
            "scripts/nmem-capture-dimagent.py",
            "scripts/nmem-hook-read.sh",
            "scripts/nmem-hook-subagent.py",
            "scripts/nmem-hook-save.py",
            "scripts/skill_outcome.py",
        ):
            self.assertTrue((PLUGIN_ROOT / rel).exists(), rel)

    def test_skills_directory_has_seven_skills(self):
        skills = sorted(p.parent.name for p in (PLUGIN_ROOT / "skills").glob("*/SKILL.md"))
        self.assertEqual(len(skills), 7, skills)
        self.assertIn("read-working-memory", skills)
        self.assertIn("distill-memory", skills)
        self.assertIn("search-memory", skills)


if __name__ == "__main__":
    unittest.main()
