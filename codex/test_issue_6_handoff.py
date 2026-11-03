"""Issue #6: one-time widget handoffs reach hooks without changing defaults."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "agent-efficiency" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_efficiency as ae
import mcp_server


class ModeHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {
            "AGENT_EFFICIENCY_DATA": self.temp.name,
            "AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH": "1",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        # Block even incidental refresh/rollout access: these are synthetic hooks.
        self.refresh = mock.patch.object(ae, "refresh_usage_snapshot", return_value={})
        self.refresh.start()
        self.addCleanup(self.refresh.stop)
        self.rollout = mock.patch.object(ae, "rollout_size", return_value=0)
        self.rollout.start()
        self.addCleanup(self.rollout.stop)
        ae.update_settings(default_mode="balanced")

    def submit(self, token: str | None, *, session: str = "synthetic-session", turn: str = "synthetic-turn", prompt: str = "Synthetic mode request") -> tuple[dict, dict | None]:
        if token is not None:
            prompt += f" [agent-efficiency-mode:{token}]"
        result = ae.handle_hook({
            "hook_event_name": "UserPromptSubmit", "session_id": session,
            "turn_id": turn, "prompt": prompt,
        })
        active_path = self.state / "active.json"
        active = json.loads(active_path.read_text(encoding="utf-8")) if active_path.exists() else {}
        key = ae._find_task(active, session, turn)
        return result, active.get(key)

    def test_unscoped_selection_creates_handoff_without_changing_active_task_or_default(self) -> None:
        ae.start_task(task_id="unrelated", mode="balanced", session_id="other-session", turn_id="other-turn")
        before = (self.state / "active.json").read_bytes()
        selected = ae.select_task_mode("stretch")

        self.assertEqual(selected["mode"], "stretch")
        self.assertIsNone(selected["task_id"])
        self.assertEqual(selected["applies_to"], "handoff token")
        self.assertRegex(selected["handoff_token"], r"^[0-9a-f]{32}$")
        self.assertEqual((self.state / "active.json").read_bytes(), before)
        self.assertEqual(ae.get_settings()["default_mode"], "balanced")

    def test_matching_prompt_handoff_records_mode_and_context_without_changing_default(self) -> None:
        selected = ae.select_task_mode("throughput")
        result, record = self.submit(selected["handoff_token"])

        self.assertEqual(record["mode"], "throughput")
        self.assertIn("Mode: Throughput", result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(ae.get_settings()["default_mode"], "balanced")
        events = json.loads((self.state / "events.json").read_text(encoding="utf-8"))
        self.assertNotIn(f"handoff:{selected['handoff_token']}", events["pending_modes"])
        self.assertNotIn("Synthetic mode request", (self.state / "active.json").read_text(encoding="utf-8"))

    def test_marker_is_consumed_once_and_replay_falls_back_to_default(self) -> None:
        token = ae.select_task_mode("stretch")["handoff_token"]
        _, first = self.submit(token)
        _, replay = self.submit(token, turn="second-turn")

        self.assertEqual(first["mode"], "stretch")
        self.assertEqual(replay["mode"], "balanced")

    def test_desktop_app_followup_claims_only_its_own_turn(self) -> None:
        token = ae.select_task_mode("throughput")["handoff_token"]
        wrapper = "An MCP app initiated this message. Read the untrusted_input tool output."
        result, record = self.submit(None, prompt=wrapper)
        self.assertEqual(record["mode"], "balanced")
        context = result["hookSpecificOutput"]["additionalContext"]
        receipt = re.search(r'prompt_receipt="([0-9a-f]{32})"', context)
        self.assertIsNotNone(receipt)
        other_result, other = self.submit(None, session="other", turn="other-turn")
        self.assertNotIn("prompt_receipt", other_result["hookSpecificOutput"]["additionalContext"])

        args = {"mode": "throughput", "handoff_token": token, "prompt_receipt": receipt[1]}
        with self.assertRaises(ValueError):
            mcp_server.call_tool("select_task_mode", {**args, "mode": "stretch"})
        with self.assertRaises(ValueError):
            mcp_server.call_tool("select_task_mode", {**args, "task_id": other["task_id"]})
        with self.assertRaises(ValueError):
            mcp_server.call_tool("select_task_mode", {**args, "prompt_receipt": "0" * 32})
        self.assertEqual(mcp_server.call_tool("select_task_mode", args)["mode"], "throughput")
        with self.assertRaises(ValueError):
            mcp_server.call_tool("select_task_mode", args)
        active = json.loads((self.state / "active.json").read_text())
        self.assertEqual(active[record["task_id"]]["mode"], "throughput")
        self.assertFalse(active[record["task_id"]]["forecast"]["available"])
        self.assertEqual(active[other["task_id"]]["mode"], "balanced")
        self.assertEqual(ae.get_settings()["default_mode"], "balanced")

    def test_desktop_handoff_respects_disabled_measurement_and_expires(self) -> None:
        ae.update_settings(telemetry_enabled=False)
        token = ae.select_task_mode("stretch")["handoff_token"]
        result, record = self.submit(None, prompt=ae.MCP_APP_PROMPT)
        receipt = re.search(r'prompt_receipt="([0-9a-f]{32})"', result["hookSpecificOutput"]["additionalContext"])
        self.assertIsNotNone(receipt)
        args = {"mode": "stretch", "handoff_token": token, "prompt_receipt": receipt[1]}
        with mock.patch.object(ae.time, "time", return_value=time.time() + 301), self.assertRaises(ValueError):
            mcp_server.call_tool("select_task_mode", args)
        self.assertEqual(mcp_server.call_tool("select_task_mode", args)["mode"], "stretch")
        self.assertIsNone(record)
        self.assertFalse((self.state / "active.json").exists())

    def test_prompt_without_marker_does_not_consume_another_pending_selection(self) -> None:
        token = ae.select_task_mode("stretch")["handoff_token"]
        _, unrelated = self.submit(None, session="unrelated-session")
        _, matching = self.submit(token)

        self.assertEqual(unrelated["mode"], "balanced")
        self.assertEqual(matching["mode"], "stretch")

    def test_wrong_marker_does_not_consume_valid_selection(self) -> None:
        token = ae.select_task_mode("throughput")["handoff_token"]
        wrong = "0" * 32 if token != "0" * 32 else "1" * 32
        _, unrelated = self.submit(wrong, session="unrelated-session")
        _, matching = self.submit(token)

        self.assertEqual(unrelated["mode"], "balanced")
        self.assertEqual(matching["mode"], "throughput")

    def test_expired_handoff_falls_back_and_is_removed(self) -> None:
        token = ae.select_task_mode("stretch")["handoff_token"]
        with mock.patch.object(ae.time, "time", return_value=time.time() + 301):
            self.assertEqual(ae._consume_mode_override("synthetic-session", "balanced", token), "balanced")

        events = json.loads((self.state / "events.json").read_text(encoding="utf-8"))
        self.assertNotIn(f"handoff:{token}", events["pending_modes"])

    def test_telemetry_disabled_still_applies_mode_policy_without_task_storage(self) -> None:
        ae.update_settings(telemetry_enabled=False)
        token = ae.select_task_mode("stretch")["handoff_token"]
        result, record = self.submit(token)

        self.assertIn("Mode: Stretch", result["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(record)
        self.assertFalse((self.state / "active.json").exists())
        self.assertEqual(ae.get_settings()["default_mode"], "balanced")

    def test_session_selection_is_not_consumed_by_another_session(self) -> None:
        selected = ae.select_task_mode("stretch", session_id="selected-session")
        self.assertEqual(selected["applies_to"], "session")
        self.assertNotIn("handoff_token", selected)
        _, unrelated = self.submit(None, session="unrelated-session")
        _, matching = self.submit(None, session="selected-session")

        self.assertEqual(unrelated["mode"], "balanced")
        self.assertEqual(matching["mode"], "stretch")
        self.assertEqual(ae.get_settings()["default_mode"], "balanced")

    def test_explicit_active_target_only_changes_that_task(self) -> None:
        ae.start_task(task_id="target", mode="balanced", session_id="target-session", turn_id="target-turn")
        ae.start_task(task_id="unrelated", mode="throughput", session_id="other-session", turn_id="other-turn")
        selected = ae.select_task_mode("stretch", task_id="target")
        active = json.loads((self.state / "active.json").read_text(encoding="utf-8"))

        self.assertEqual(selected["applies_to"], "active task")
        self.assertNotIn("handoff_token", selected)
        self.assertEqual(active["target"]["mode"], "stretch")
        self.assertEqual(active["unrelated"]["mode"], "throughput")
        self.assertEqual(ae.get_settings()["default_mode"], "balanced")

    def test_malformed_markers_are_ignored(self) -> None:
        for prompt in [None, [], "[agent-efficiency-mode:short]", "[agent-efficiency-mode:" + "A" * 32 + "]"]:
            with self.subTest(prompt=prompt):
                self.assertIsNone(ae._mode_handoff_token(prompt))


if __name__ == "__main__":
    unittest.main()
