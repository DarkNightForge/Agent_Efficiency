from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "agent-efficiency" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_efficiency as ae
import codex_telemetry as ct
import mcp_server


class AgentEfficiencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        os.environ["AGENT_EFFICIENCY_DATA"] = self.temp.name
        os.environ["AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH"] = "1"

    def tearDown(self) -> None:
        os.environ.pop("AGENT_EFFICIENCY_DATA", None)
        os.environ.pop("AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH", None)
        self.temp.cleanup()

    def test_defaults_and_persistent_setting(self) -> None:
        self.assertEqual(ae.get_settings()["default_mode"], "balanced")
        value = ae.update_settings(default_mode="ultra-efficiency", estimate_scope="all")
        self.assertEqual(value["default_mode"], "stretch")
        self.assertEqual(ae.get_settings()["estimate_scope"], "all")

    def test_selected_scope_only_calculates_one_mode(self) -> None:
        center = ae.show_control_center("Fix a bug", selected_mode="big-baller", estimate_scope="selected")
        self.assertEqual([item["mode"] for item in center["modes"]], ["throughput"])
        self.assertEqual(len(center["descriptions"]), 3)
        self.assertIn("Exact messages remaining", center["disclaimer"])

    def test_all_modes_have_ordered_time_and_token_tradeoffs(self) -> None:
        center = ae.show_control_center("Build a feature", estimate_scope="all", complexity="complex")
        by_mode = {item["mode"]: item for item in center["modes"]}
        self.assertLess(by_mode["throughput"]["eta_minutes"]["low"], by_mode["balanced"]["eta_minutes"]["low"])
        self.assertLess(by_mode["balanced"]["eta_minutes"]["low"], by_mode["stretch"]["eta_minutes"]["low"])
        self.assertGreater(by_mode["throughput"]["estimated_tokens"]["low"], by_mode["balanced"]["estimated_tokens"]["low"])
        self.assertGreater(by_mode["balanced"]["estimated_tokens"]["low"], by_mode["stretch"]["estimated_tokens"]["low"])

    def test_capacity_uses_most_constraining_window(self) -> None:
        windows = [
            {"limit_id": "five-hour", "used_percent": 50, "window_duration_mins": 300},
            {"limit_id": "weekly", "used_percent": 90, "window_duration_mins": 10080},
        ]
        capacity = ae.estimate_capacity("efficiency", windows)
        self.assertTrue(capacity["available"])
        self.assertEqual(capacity["constraining_window"], "weekly")
        self.assertEqual(capacity["confidence"], "low")
        weekly = next(item for item in capacity["windows"] if item["limit_id"] == "weekly")
        self.assertEqual(weekly["used_percent_label"], "measured")
        self.assertEqual(weekly["estimated_remaining_label"], "estimated")
        self.assertEqual(capacity["calibration_status"], "heuristic")

    # test_stale_usage_snapshot_is_uncalibrated moved to test_hardening.py.
    # The version that lived here asserted the OLD single-timestamp model -- it
    # staled `updated_at` (the whole usage blob) and expected rate limits to go
    # uncalibrated with it. That model is the defect: token ingestion restamped
    # the blob and laundered stale quota data as fresh. The ported version
    # asserts the same outcome against `rate_limits_updated_at`, which is the
    # field that now carries rate-limit freshness.

    def _burn_10_percent_per_hour(self) -> None:
        """Seed one completed task worth 10% of the window in one hour."""
        history = {
            "mode": "efficiency",
            "status": "completed",
            "elapsed_seconds": 3600,
            "usage_delta": [{
                "limit_id": "five-hour",
                "same_window": True,
                "used_percent_delta": 10,
            }],
        }
        (Path(self.temp.name) / "history.jsonl").write_text(json.dumps(history) + "\n", encoding="utf-8")

    def _forecast(self, hours_to_reset: float) -> dict:
        self._burn_10_percent_per_hour()
        capacity = ae.estimate_capacity("efficiency", [{
            "limit_id": "five-hour",
            "used_percent": 50,
            "resets_at": time.time() + hours_to_reset * 3600,
        }])
        return capacity["windows"][0]

    def test_burn_forecast_reports_lockout_duration(self) -> None:
        # 50% left at 10%/h caps in 5h; the window resets in 10h, so running out
        # costs 5 hours sat blocked. That is worth reshaping the work over.
        window = self._forecast(10)
        self.assertEqual(window["burn_percent_per_hour"], 10)
        self.assertEqual(window["hours_to_cap"], 5)
        self.assertEqual(window["hours_to_cap"], 5)
        self.assertEqual(window["blocked_hours"], 5)
        self.assertEqual(window["forecast_verdict"], "lockout")

    def test_cap_landing_just_before_reset_is_not_a_constraint(self) -> None:
        # FINDINGS 9: the bug this encodes. Caps in 5h, resets in 5.5h -- the old
        # cap-versus-reset verdict called this "cap arrives first" and a live agent
        # cut its plan with half the window unspent. The cost is 30 minutes.
        window = self._forecast(5.5)
        self.assertEqual(window["blocked_hours"], 0.5)
        self.assertEqual(window["forecast_verdict"], "brief-lockout")
        self.assertLess(window["blocked_hours"], ae.LOCKOUT_WARN_HOURS)

    def test_reset_before_cap_reports_no_lockout(self) -> None:
        # Caps in 5h but resets in 2h, so the allowance is never exhausted.
        window = self._forecast(2)
        self.assertEqual(window["blocked_hours"], 0)
        self.assertEqual(window["forecast_verdict"], "no-lockout")

    def test_timer_history_and_score(self) -> None:
        started = ae.start_task(task_id="task-1", mode="efficiency", complexity="simple")
        self.assertEqual(started["task_id"], "task-1")
        time.sleep(0.01)
        done = ae.complete_task("task-1", validation_passed=True, tokens={"input": 100, "output": 50})
        self.assertGreater(done["elapsed_seconds"], 0)
        self.assertEqual(done["tokens"]["total"], 150)
        # Shape upgraded from a bare float: the score now carries the ratios and
        # the basis it was computed from, and is None when unmeasurable.
        self.assertIsInstance(done["efficiency_score"], dict)
        self.assertIn("basis", done["efficiency_score"])
        self.assertEqual(ae.list_history(1)[0]["task_id"], "task-1")

    def test_app_server_rate_limit_shape(self) -> None:
        value = ae.ingest_usage(rate_limits={
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 123},
                    "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 456},
                }
            }
        })
        self.assertEqual(len(value["rate_limits"]), 2)
        self.assertEqual(value["rate_limits"][0]["used_percent"], 25)
        # No per-bucket limitId in this shape, so a positional name is the correct
        # fallback -- but see the next test: it must not override a real one.
        self.assertEqual(value["rate_limits"][0]["limit_id"], "codex:primary")

    def test_bucket_keeps_its_own_limit_id(self) -> None:
        # Learned burn rates and observed percentage deltas are keyed by limit_id.
        # Overwriting the host's identity with a positional name re-attributes one
        # window's history to another as soon as the host reorders its windows.
        value = ae.ingest_usage(rate_limits={
            "primary": {"limitId": "5h", "usedPercent": 18, "windowDurationMins": 300, "resetsAt": 1},
            "secondary": {"limitId": "weekly", "usedPercent": 41, "windowDurationMins": 10080, "resetsAt": 2},
        })
        self.assertEqual([w["limit_id"] for w in value["rate_limits"]], ["5h", "weekly"])

    def test_app_server_metadata_survives_window_normalization(self) -> None:
        value = ae.ingest_usage(rate_limits={
            "rateLimitsByLimitId": {
                "codex": {
                    "limitName": "Codex",
                    "planType": "plus",
                    "credits": {"hasCredits": True, "unlimited": False},
                    "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 123},
                }
            }
        })
        window = value["rate_limits"][0]
        self.assertEqual(window["limit_id"], "codex:primary")
        self.assertEqual(window["window_role"], "primary")
        self.assertEqual(window["plan_type"], "plus")
        self.assertTrue(window["credits"]["hasCredits"])

    def test_credit_backed_limit_is_cost_ahead_not_lockout(self) -> None:
        self._burn_10_percent_per_hour()
        capacity = ae.estimate_capacity("efficiency", [{
            "limit_id": "five-hour",
            "used_percent": 90,
            "resets_at": time.time() + 10 * 3600,
            "credits": {"hasCredits": True},
        }])
        window = capacity["windows"][0]
        self.assertEqual(window["forecast_verdict"], "cost-ahead")
        self.assertEqual(window["blocked_hours"], 0)
        hard_wall = ae.estimate_capacity("efficiency", [{
            "limit_id": "five-hour",
            "used_percent": 90,
            "resets_at": time.time() + 10 * 3600,
            "credits": {"hasCredits": True},
            "spend_control_reached": True,
        }])
        self.assertFalse(hard_wall["windows"][0]["can_continue_after_cap"])
        self.assertEqual(hard_wall["windows"][0]["forecast_verdict"], "lockout")

    def test_rollout_tokens_sum_last_usage_per_model_call(self) -> None:
        rollout = Path(self.temp.name) / "rollout.jsonl"
        records = [
            {"type": "turn_context", "payload": {"turn_id": "turn-a"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "reasoning_output_tokens": 10, "output_tokens": 30, "total_tokens": 130}, "total_token_usage": {"total_tokens": 9999}}}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 50, "output_tokens": 15, "total_tokens": 65}}}},
            {"type": "turn_context", "payload": {"turn_id": "turn-b"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 700, "output_tokens": 300}}}},
        ]
        rollout.write_text(
            "\n".join(json.dumps(record) for record in records) + '\n{"partial":',
            encoding="utf-8",
        )
        tokens = ct.collect_turn_tokens(rollout, turn_id="turn-a")
        self.assertEqual(tokens["input"], 150)
        self.assertEqual(tokens["cached_input"], 20)
        self.assertEqual(tokens["reasoning"], 10)
        self.assertEqual(tokens["output"], 45)
        self.assertEqual(tokens["total"], 195)
        self.assertEqual(tokens["model_calls"], 2)

    def test_session_end_recovers_tokens_if_stop_did_not_finish(self) -> None:
        rollout = Path(self.temp.name) / "session-end.jsonl"
        records = [{"type": "turn_context", "payload": {"turn_id": "turn"}}, {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}}}}]
        rollout.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        ae.start_task(session_id="session", turn_id="turn", transcript_path=str(rollout), rollout_offset=0)
        ae.handle_hook({"hook_event_name": "SessionEnd", "session_id": "session", "transcript_path": str(rollout)})
        done = ae.list_history(1)[0]
        self.assertEqual(done["tokens"]["total"], 15)
        self.assertEqual(done["token_source"], "codex-rollout")

    def test_turn_context_before_rollout_offset_is_preserved(self) -> None:
        rollout = Path(self.temp.name) / "context-before-offset.jsonl"
        context = {"type": "turn_context", "payload": {"turn_id": "turn-a"}}
        rollout.write_text(json.dumps(context) + "\n", encoding="utf-8")
        offset = rollout.stat().st_size
        token_event = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 11,
                        "cached_input_tokens": 2,
                        "reasoning_output_tokens": 3,
                        "output_tokens": 7,
                        "total_tokens": 18,
                    }
                },
            },
        }
        with rollout.open("a", encoding="utf-8", newline="") as handle:
            handle.write(json.dumps(token_event) + "\n")

        tokens = ct.collect_turn_tokens(rollout, turn_id="turn-a", start_offset=offset)
        self.assertEqual(tokens["input"], 11)
        self.assertEqual(tokens["cached_input"], 2)
        self.assertEqual(tokens["reasoning"], 3)
        self.assertEqual(tokens["output"], 7)
        self.assertEqual(tokens["total"], 18)
        self.assertEqual(tokens["model_calls"], 1)

    def test_rollout_cache_persists_only_opaque_file_and_turn_references(self) -> None:
        sessions_root = Path(self.temp.name) / "sessions-private-user"
        rollout_dir = sessions_root / "2026" / "08" / "21"
        rollout_dir.mkdir(parents=True)
        rollout = rollout_dir / "rollout-private.jsonl"
        session_id = "private-rollout-session"
        turn_id = "private-rollout-turn"
        records = [
            {"type": "session_meta", "payload": {"id": session_id}},
            {"type": "turn_context", "payload": {"turn_id": turn_id, "model": "test"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 3, "output_tokens": 1}
            }}},
        ]
        rollout.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )

        result = ae.scan_rollouts(sessions_root)
        self.assertEqual(result["token_events_processed"], 1)
        cache_text = (Path(self.temp.name) / "rollout-cache.json").read_text(encoding="utf-8")
        self.assertNotIn(str(rollout.resolve()), cache_text)
        self.assertNotIn(session_id, cache_text)
        self.assertNotIn(turn_id, cache_text)
        cache = json.loads(cache_text)
        self.assertEqual(cache["schema_version"], 3)
        self.assertTrue(all(key.startswith("ae-rollout-") for key in cache["files"]))

    def test_prompt_hook_injects_full_live_budget_policy(self) -> None:
        ae.ingest_usage(rate_limits=[{
            "limit_id": "five-hour",
            "used_percent": 25,
            "window_duration_mins": 300,
            "resets_at": time.time() + 3600,
        }])
        result = ae.handle_hook({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session",
            "turn_id": "turn",
        })
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("<usage-budget>", context)
        self.assertIn("Codex allowance: LIVE", context)
        self.assertIn("5h window: 25% used", context)

    def test_disabling_telemetry_keeps_mode_and_budget_context(self) -> None:
        ae.update_settings(telemetry_enabled=False)
        result = ae.handle_hook({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session",
            "turn_id": "turn",
        })
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("<usage-budget>", context)
        self.assertIn("Mode: Balanced", context)
        self.assertFalse((Path(self.temp.name) / "active.json").exists())

    def test_hooks_do_not_store_prompt_or_tool_content(self) -> None:
        ae.handle_hook({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session",
            "turn_id": "turn",
            "model": "test-model",
            "prompt": "SECRET PROMPT",
        })
        ae.handle_hook({
            "hook_event_name": "PreToolUse",
            "session_id": "session",
            "turn_id": "turn",
            "tool_name": "Bash",
            "tool_input": {"command": "SECRET COMMAND"},
        })
        ae.handle_hook({
            "hook_event_name": "Stop",
            "session_id": "session",
            "turn_id": "turn",
            "last_assistant_message": "SECRET RESPONSE",
        })
        stored = "\n".join(path.read_text(encoding="utf-8") for path in Path(self.temp.name).glob("*.*"))
        self.assertNotIn("SECRET", stored)

    def test_hook_state_uses_opaque_references_and_never_persists_rollout_paths(self) -> None:
        private_root = Path(self.temp.name) / "private-user-name"
        private_root.mkdir()
        rollout = private_root / "rollout-private-session.jsonl"
        session_id = "private-session-identifier"
        turn_id = "private-turn-identifier"
        rollout.write_text(
            json.dumps({"type": "turn_context", "payload": {"turn_id": turn_id}}) + "\n",
            encoding="utf-8",
        )
        ae.handle_hook({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session_id,
            "turn_id": turn_id,
            "transcript_path": str(rollout),
        })

        active_text = (Path(self.temp.name) / "active.json").read_text(encoding="utf-8")
        self.assertNotIn(str(rollout), active_text)
        self.assertNotIn(session_id, active_text)
        self.assertNotIn(turn_id, active_text)
        active = json.loads(active_text)
        record = next(iter(active.values()))
        self.assertIn("session_ref", record)
        self.assertIn("turn_ref", record)
        self.assertNotIn("transcript_path", record)
        self.assertNotIn("session_id", record)
        self.assertNotIn("turn_id", record)

        with rollout.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": 4, "output_tokens": 2}},
                },
            }) + "\n")
        ae.handle_hook({
            "hook_event_name": "Stop",
            "session_id": session_id,
            "turn_id": turn_id,
            "transcript_path": str(rollout),
        })
        history_text = (Path(self.temp.name) / "history.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(str(rollout), history_text)
        self.assertNotIn(session_id, history_text)
        self.assertNotIn(turn_id, history_text)
        self.assertEqual(ae.list_history(1)[0]["tokens"]["total"], 6)

    def test_start_task_migrates_legacy_raw_hook_state(self) -> None:
        active_path = Path(self.temp.name) / "active.json"
        session_id = "legacy-private-session"
        turn_id = "legacy-private-turn"
        rollout_path = str(Path(self.temp.name) / "private-user" / "rollout.jsonl")
        legacy_key = f"{session_id}:{turn_id}"
        active_path.write_text(json.dumps({legacy_key: {
            "task_id": legacy_key,
            "session_id": session_id,
            "turn_id": turn_id,
            "transcript_path": rollout_path,
            "source": "hook",
            "started_at": "2026-08-21T00:00:00Z",
        }}), encoding="utf-8")

        record = ae.start_task(session_id=session_id, turn_id=turn_id, source="hook")
        migrated_text = active_path.read_text(encoding="utf-8")
        self.assertNotIn(session_id, migrated_text)
        self.assertNotIn(turn_id, migrated_text)
        self.assertNotIn(rollout_path, migrated_text)
        self.assertTrue(record["task_id"].startswith("ae-task-"))

    def test_empty_rollout_scan_sanitizes_legacy_history(self) -> None:
        history_path = Path(self.temp.name) / "history.jsonl"
        sessions_root = Path(self.temp.name) / "sessions"
        sessions_root.mkdir()
        session_id = "legacy-private-session"
        turn_id = "legacy-private-turn"
        transcript_path = str(Path(self.temp.name) / "private-user" / "rollout.jsonl")
        history_path.write_text(json.dumps({
            "task_id": "legacy-task",
            "session_id": session_id,
            "turn_id": turn_id,
            "transcript_path": transcript_path,
            "status": "completed",
        }) + "\n", encoding="utf-8")

        result = ae.scan_rollouts(sessions_root)

        self.assertEqual(result["files_seen"], 0)
        migrated_text = history_path.read_text(encoding="utf-8")
        self.assertNotIn(session_id, migrated_text)
        self.assertNotIn(turn_id, migrated_text)
        self.assertNotIn(transcript_path, migrated_text)
        migrated = json.loads(migrated_text)
        self.assertTrue(migrated["session_ref"].startswith("ae-session-"))
        self.assertTrue(migrated["turn_ref"].startswith("ae-turn-"))

    def test_mcp_tools_list_and_call(self) -> None:
        listing = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        names = {tool["name"] for tool in listing["result"]["tools"]}
        self.assertIn("show_control_center", names)
        self.assertIn("render_control_center", names)
        self.assertIn("refresh_usage", names)
        render_tool = next(tool for tool in listing["result"]["tools"] if tool["name"] == "render_control_center")
        self.assertEqual(render_tool["_meta"]["ui"]["resourceUri"], mcp_server.UI_URI)
        response = mcp_server.handle({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_settings", "arguments": {}},
        })
        self.assertFalse(response["result"]["isError"])

    def test_rate_limit_refresh_failure_is_sanitized_and_recovery_clears_it(self) -> None:
        os.environ.pop("AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH", None)
        with mock.patch.object(ae, "fetch_rate_limits", side_effect=TimeoutError("SECRET details")):
            with self.assertRaises(TimeoutError):
                ae.refresh_usage_snapshot(force=True, raise_on_error=True)
        failed = ae._rate_limit_poll_status()
        self.assertEqual(failed["status"], "degraded")
        self.assertEqual(failed["last_error"]["code"], "timeout")
        self.assertNotIn("SECRET", (Path(self.temp.name) / "usage.json").read_text(encoding="utf-8"))

        snapshot = {"primary": {"limitId": "five-hour", "usedPercent": 25}}
        with mock.patch.object(ae, "fetch_rate_limits", return_value=snapshot):
            refreshed = ae.refresh_usage_snapshot(force=True, raise_on_error=True)
        self.assertEqual(refreshed["rate_limit_poll"]["status"], "ok")
        self.assertIsNone(refreshed["rate_limit_poll"]["last_error"])
        self.assertIsNotNone(refreshed["rate_limit_poll"]["last_success_at"])

    def test_mcp_refresh_failure_uses_bounded_backoff(self) -> None:
        class OneCycleStop:
            def __init__(self) -> None:
                self.done = False
                self.delays: list[int] = []

            def is_set(self) -> bool:
                return self.done

            def wait(self, delay: int) -> bool:
                self.delays.append(delay)
                self.done = True
                return True

        stop = OneCycleStop()
        with mock.patch.object(
            mcp_server,
            "refresh_usage_snapshot",
            side_effect=PermissionError("denied"),
        ), mock.patch.object(mcp_server, "scan_rollouts") as scan:
            mcp_server._telemetry_loop(stop, interval=240)
        self.assertEqual(stop.delays, [15])
        scan.assert_called_once_with()

    def test_mcp_ui_resource(self) -> None:
        major_minor = ".".join(ae.VERSION.split(".")[:2])
        self.assertIn(f"Agent Efficiency v{major_minor}", ae.__doc__)
        self.assertEqual(ae.build_parser().description, f"Agent Efficiency v{major_minor}")
        self.assertEqual(
            mcp_server.UI_URI,
            f"ui://widget/agent-efficiency-control-center-v{major_minor}.html",
        )
        listing = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {}})
        self.assertEqual(listing["result"]["resources"][0]["uri"], mcp_server.UI_URI)
        resource = mcp_server.handle({
            "jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": mcp_server.UI_URI}
        })
        content = resource["result"]["contents"][0]
        self.assertEqual(content["mimeType"], "text/html;profile=mcp-app")
        self.assertIn(f'<span id="version">v{ae.VERSION}</span>', content["text"])
        self.assertIn("Use selected mode", content["text"])


if __name__ == "__main__":
    unittest.main()
