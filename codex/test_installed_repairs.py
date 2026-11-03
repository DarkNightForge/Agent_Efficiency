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

    def child_event(self, parent="parent", header_id="child", hook_session="child", hook_agent="child", **extra):
        path = self.base / "sessions" / "2030" / "01" / "01" / "rollout-child.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": header_id, "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}, **extra,
        }}) + "\n", encoding="utf-8")
        return {"hook_event_name": "SubagentStart", "session_id": hook_session, "turn_id": "child-turn", "agent_id": hook_agent, "transcript_path": str(path)}

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
        self.assertEqual(ae._register_subagent(self.child_event())[:2], (None, False))
        active = json.loads((self.state / "active.json").read_text())
        self.assertTrue(all(record["subagents"] == 0 for record in active.values()))

    def test_conflicting_header_or_wrong_child_is_not_attributed(self):
        ae.start_task(session_id="parent", turn_id="one")
        for fields in ({"parent_thread_id": "conflicting"}, {"header_id": "other-child"}):
            self.assertEqual(ae._register_subagent(self.child_event(**fields))[:2], (None, False))

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

    def test_distinct_session_and_thread_ids_link_complete_and_reconcile(self):
        parent_path = self.base / "parent.jsonl"
        parent_path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "parent-thread", "session_id": "parent-session", "source": "vscode",
        }}) + "\n", encoding="utf-8")
        ae.handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "parent-session",
                        "turn_id": "parent-turn", "transcript_path": str(parent_path)})
        parent_key = ae._task_key_from_refs(ae.opaque_ref("parent-session", "session"), ae.opaque_ref("parent-turn", "turn"))
        event = self.child_event(parent="parent-thread", header_id="child-thread", session_id="child-session",
                                 hook_session="child-session", hook_agent="child-thread")
        ae.handle_hook(event)
        ae.handle_hook(event)
        child_key = ae._task_key_from_refs(ae.opaque_ref("child-session", "session"), ae.opaque_ref("child-turn", "turn"))
        with Path(event["transcript_path"]).open("a", encoding="utf-8") as handle:
            for record in ({"type": "turn_context", "payload": {"turn_id": "child-turn"}},
                           {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}}}}):
                handle.write(json.dumps(record) + "\n")
        ae.scan_rollouts(self.base / "sessions")
        active = json.loads((self.state / "active.json").read_text())
        self.assertEqual(active[child_key]["tokens"]["total"], 5)
        self.assertEqual(active[parent_key]["subagents"], 1)
        self.assertNotIn("tokens", active[parent_key])
        stop = {**event, "hook_event_name": "SubagentStop", "session_id": "parent-session",
                "turn_id": "parent-turn", "transcript_path": str(parent_path),
                "agent_transcript_path": event["transcript_path"]}
        ae.handle_hook({**stop, "session_id": "unrelated"})
        ae.handle_hook({**stop, "agent_id": "unrelated"})
        ae.handle_hook({**stop, "turn_id": "stale-turn"})
        self.assertEqual(ae.list_history(), [])
        ae.handle_hook(stop)
        history = ae.list_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["tokens"]["total"], 5)
        self.assertEqual(history[0]["session_ref"], ae.opaque_ref("child-session", "session"))
        self.assertEqual(history[0]["thread_ref"], ae.opaque_ref("child-thread", "session"))
        self.assertEqual(history[0]["parent_thread_ref"], ae.opaque_ref("parent-thread", "session"))
        self.assertEqual(history[0]["usage_delta"], [])
        aggregate = {"tokens": {**history[0]["tokens"], "input": 8, "total": 10}, "subagent": True}
        key = ae._task_key_from_refs(history[0]["thread_ref"], history[0]["turn_ref"])
        self.assertTrue(ae._reconcile_history_rollouts(self.state / "history.jsonl", {key: aggregate}))
        self.assertEqual(ae.list_history()[0]["tokens"]["total"], 10)
        runtime = ae.get_security_status()["runtime"]
        self.assertEqual(runtime["hook_events"]["SubagentStart"]["link_status_counts"]["matched"], 2)
        self.assertEqual(runtime["hook_events"]["SubagentStop"]["last_link_status"], "matched")
        serialized = "".join(p.read_text() for p in self.state.iterdir() if p.suffix in {".json", ".jsonl"})
        for raw in ("parent-session", "parent-thread", "child-session", "child-thread"):
            self.assertNotIn(raw, serialized)

    def test_thread_id_alone_cannot_override_conflicting_session_binding(self):
        event = self.child_event(header_id="child-thread", session_id="real-session",
                                 hook_session="other-session", hook_agent="child-thread")
        ae.handle_hook(event)
        status = ae.get_security_status()["runtime"]["hook_events"]["SubagentStart"]
        self.assertEqual(status["last_link_status"], "identity-mismatch")
        self.assertFalse((self.state / "active.json").exists())

    def test_subagent_stop_cannot_complete_a_root_parent_record(self):
        path = self.base / "root.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "parent", "source": "vscode"}}) + "\n")
        parent = ae.start_task(session_id="parent", turn_id="one")
        ae.handle_hook({"hook_event_name": "SubagentStop", "session_id": "parent", "turn_id": "one",
                        "agent_id": "parent", "transcript_path": str(path)})
        self.assertIn(parent["task_id"], json.loads((self.state / "active.json").read_text()))
        self.assertEqual(ae.list_history(), [])
        self.assertEqual(ae.get_security_status()["runtime"]["hook_events"]["SubagentStop"]["last_link_status"], "not-subagent")

    def test_alias_reconciliation_rejects_ambiguous_active_records(self):
        reference = ae.opaque_ref("one-thread", "session")
        turn = ae.opaque_ref("one-turn", "turn")
        active = {name: {"session_ref": ae.opaque_ref(name, "session"), "thread_ref": reference, "turn_ref": turn}
                  for name in ("first-session", "second-session")}
        self.assertIsNone(ae._find_task_by_refs(active, reference, turn))
        direct = ae._task_key_from_refs(reference, turn)
        active[direct] = active.pop("first-session")
        self.assertIsNone(ae._find_task_by_refs(active, reference, turn))

    def test_malformed_saved_link_diagnostic_is_not_returned_or_allowed_to_crash(self):
        ae.get_settings()
        ae._atomic_json(self.state / "events.json", {"hook_runtime": {"SubagentStart": {
            "last_link_status": {"private-path": "not-a-label"},
            "link_status_counts": {"matched": True, "not-a-label": 1},
        }}})
        event = ae.get_security_status()["runtime"]["hook_events"]["SubagentStart"]
        self.assertIsNone(event["last_link_status"])
        self.assertEqual(event["link_status_counts"], {})

    def test_widget_resource_identity_changes_with_content_not_local_path(self):
        first = self.base / "first.html"
        second = self.base / "second.html"
        first.write_bytes(b"<html>first</html>")
        second.write_bytes(first.read_bytes())
        original = mcp._ui_resource_uri(first)
        self.assertEqual(original, mcp._ui_resource_uri(second))
        first.write_bytes(b"<html>second</html>")
        self.assertNotEqual(original, mcp._ui_resource_uri(first))
        self.assertNotIn(str(self.base), original)

    def test_diagnostic_reasons_are_fixed_and_do_not_expose_input(self):
        ae.handle_hook({"hook_event_name": "SubagentStart", "session_id": "private-id", "turn_id": "private-turn"})
        runtime = ae.get_security_status()["runtime"]
        event = runtime["hook_events"]["SubagentStart"]
        self.assertEqual(event["link_status_counts"], {"missing-context": 1})
        self.assertNotIn("private-id", json.dumps(runtime))
        self.assertNotIn("private-turn", json.dumps(runtime))

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
