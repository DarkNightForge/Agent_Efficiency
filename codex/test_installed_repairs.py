"""Regression coverage for the October installed-host findings; no live account."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "agent-efficiency" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import agent_efficiency as ae
import mcp_server as mcp


class InstalledRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.state = self.base / "state"
        patch = mock.patch.dict(os.environ, {"AGENT_EFFICIENCY_DATA": str(self.state),
                                           "AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH": "1"})
        patch.start()
        self.addCleanup(patch.stop)

    def package(self, version="0.5.0+codex.synthetic"):
        root = self.base / "plugins" / "cache" / "synthetic-market" / "agent-efficiency" / version
        (root / ".codex-plugin").mkdir(parents=True)
        (root / "scripts").mkdir()
        (root / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "agent-efficiency", "version": version}), encoding="utf-8")
        return root

    def child_event(self, parent="parent", header_id="child", **extra):
        path = self.base / "child.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": header_id, "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}, **extra,
        }}) + "\n", encoding="utf-8")
        return {"hook_event_name": "SubagentStart", "session_id": "child", "turn_id": "child-turn", "agent_id": "child", "transcript_path": str(path)}

    def test_installed_server_and_host_hook_resolve_same_state_without_migration(self):
        root = self.package()
        wanted = self.base / "plugins" / "data" / "agent-efficiency-synthetic-market"
        fallback = self.base / "home" / ".agent-efficiency"
        fallback.mkdir(parents=True)
        old = fallback / "history.jsonl"
        old.write_bytes(b"preserve old records\n")
        with mock.patch.object(ae, "__file__", str(root / "scripts" / "agent_efficiency.py")), mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(Path, "home", return_value=self.base / "home"):
                self.assertEqual(ae._data_dir(), wanted)
                self.assertEqual(ae.get_security_status()["runtime"]["state_source"], "installed-layout")
                self.assertFalse(wanted.exists())
                with mock.patch.dict(os.environ, {"PLUGIN_DATA": str(wanted)}):
                    ae.start_task(task_id="shared-record")
                    ae.complete_task("shared-record", tokens={"input": 2, "output": 1})
                self.assertEqual(ae.list_history()[0]["task_id"], "shared-record")
                self.assertEqual(ae.forecast_diagnostics()["completed_records"], 1)
        self.assertEqual(old.read_bytes(), b"preserve old records\n")

    def test_explicit_override_precedes_host_data_and_layout(self):
        root = self.package()
        with mock.patch.object(ae, "__file__", str(root / "scripts" / "agent_efficiency.py")), mock.patch.dict(os.environ, {"PLUGIN_DATA": str(self.base / "host")}):
            self.assertEqual(ae._data_dir(), self.state)
            os.environ["AGENT_EFFICIENCY_DATA"] = ""
            self.assertEqual(ae._data_dir(), self.base / "host")

    def test_invalid_installed_identity_fails_closed_without_fallback_writes(self):
        root = self.package()
        (root / ".codex-plugin" / "plugin.json").write_text('{"name":"agent-efficiency","version":"different"}')
        with mock.patch.object(ae, "__file__", str(root / "scripts" / "agent_efficiency.py")), mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ae.StateSecurityError):
                ae.get_settings()
        self.assertFalse((self.base / "plugins" / "data").exists())

    def test_standalone_fallback_still_works(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(Path, "home", return_value=self.base):
            self.assertEqual(ae._data_dir(), self.base / ".agent-efficiency")

    def test_child_context_counts_parent_once_and_completes_separately(self):
        parent = ae.start_task(session_id="parent", turn_id="parent-turn", mode="stretch")
        event = self.child_event()
        ae.handle_hook(event)
        ae.handle_hook(event)
        active = json.loads((self.state / "active.json").read_text())
        self.assertEqual(active[parent["task_id"]]["subagents"], 1)
        child_key = ae._task_key_from_refs(ae.opaque_ref("child", "session"), ae.opaque_ref("child-turn", "turn"))
        self.assertEqual(active[child_key]["mode"], "stretch")
        self.assertTrue(active[child_key]["is_subagent"])
        with Path(event["transcript_path"]).open("a", encoding="utf-8") as handle:
            for record in ({"type": "turn_context", "payload": {"turn_id": "child-turn"}},
                           {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}}}}):
                handle.write(json.dumps(record) + "\n")
        ae.handle_hook({**event, "hook_event_name": "SubagentStop"})
        ae.handle_hook({**event, "hook_event_name": "SubagentStop"})
        history = ae.list_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["tokens"]["total"], 5)
        self.assertTrue(history[0]["is_subagent"])
        self.assertEqual(history[0]["usage_delta"], [])
        active = json.loads((self.state / "active.json").read_text())
        self.assertEqual(active[parent["task_id"]]["subagents"], 1)
        self.assertNotIn("tokens", active[parent["task_id"]])

    def test_ambiguous_parent_turns_are_not_guessed(self):
        ae.start_task(session_id="parent", turn_id="one")
        ae.start_task(session_id="parent", turn_id="two")
        self.assertEqual(ae._register_subagent(self.child_event()), (None, False))
        active = json.loads((self.state / "active.json").read_text())
        self.assertTrue(all(record["subagents"] == 0 for record in active.values()))

    def test_conflicting_header_or_wrong_child_is_not_attributed(self):
        ae.start_task(session_id="parent", turn_id="one")
        for fields in ({"parent_thread_id": "conflicting"}, {"header_id": "other-child"}):
            self.assertEqual(ae._register_subagent(self.child_event(**fields)), (None, False))

    def test_missing_child_header_is_safe_and_retry_at_stop_can_match(self):
        parent = ae.start_task(session_id="parent", turn_id="one")
        event = self.child_event()
        Path(event["transcript_path"]).unlink()
        ae.handle_hook(event)
        event = self.child_event()
        ae.handle_hook({**event, "hook_event_name": "SubagentStop"})
        active = json.loads((self.state / "active.json").read_text())
        self.assertEqual(active[parent["task_id"]]["subagents"], 1)

    def test_parent_context_event_never_relabels_parent_as_child(self):
        parent = ae.start_task(session_id="parent", turn_id="one")
        ae.handle_hook({"hook_event_name": "SubagentStart", "session_id": "parent", "turn_id": "one", "agent_id": "different-child"})
        active = json.loads((self.state / "active.json").read_text())
        self.assertNotIn("is_subagent", active[parent["task_id"]])
        self.assertEqual(len(active), 1)

    def test_worker_evidence_survives_a_later_prompt_refresh(self):
        snapshot = {"primary": {"limitId": "synthetic", "usedPercent": 20, "windowDurationMins": 300}}
        with mock.patch.dict(os.environ, {"AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH": "0"}), mock.patch.object(ae, "fetch_rate_limits", return_value=snapshot):
            ae.refresh_usage_snapshot(force=True, caller="worker")
            first = ae.get_security_status()["runtime"]["poll_callers"]["worker"]["last_success_at"]
            ae.refresh_usage_snapshot(force=True, caller="prompt")
            callers = ae.get_security_status()["runtime"]["poll_callers"]
            self.assertEqual(callers["worker"]["last_success_at"], first)
            self.assertEqual(callers["prompt"]["status"], "ok")

    def test_unknown_poll_caller_cannot_persist_sensitive_label(self):
        ae._record_rate_limit_poll(caller="private-path-or-identifier", status="polling", last_attempt_at=ae._now())
        serialized = (self.state / "usage.json").read_text()
        self.assertNotIn("private-path-or-identifier", serialized)
        self.assertIn("unspecified", ae.get_security_status()["runtime"]["poll_callers"])

    def test_live_contract_is_read_only_and_does_not_return_paths(self):
        result = mcp.call_tool("get_security_status", {})
        self.assertFalse(self.state.exists())
        self.assertEqual(result["mcp_contract"]["server_sha256"], hashlib.sha256((SCRIPTS / "mcp_server.py").read_bytes()).hexdigest())
        self.assertEqual(len(result["mcp_contract"]["tools"]), 13)
        self.assertEqual(result["mcp_contract"]["ui_resource"], mcp.UI_URI)
        self.assertNotIn(str(self.state), json.dumps(result))

    def test_hook_health_is_bounded_and_contains_no_identity(self):
        ae.handle_hook({"hook_event_name": "PreToolUse", "session_id": "private-session", "turn_id": "private-turn"})
        status = ae.get_security_status()
        self.assertEqual(status["runtime"]["hook_events"]["PreToolUse"]["seen"], 1)
        self.assertEqual(status["runtime"]["hook_events"]["PreToolUse"]["matched"], 0)
        self.assertNotIn("private-session", json.dumps(status))
        self.assertEqual(status["runtime"]["last_hook_core_sha256"], ae.RUNTIME_CORE_SHA256)


if __name__ == "__main__":
    unittest.main()
