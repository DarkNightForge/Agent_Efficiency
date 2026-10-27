"""Issue #5: detected rollout shrink invalidates measurements, never replays them.

All archives and state below are synthetic temporary files. No installed plugin,
real rollout directory, Codex process, or account refresh is used.
"""
from __future__ import annotations

import copy
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


class RolloutTruncationTests(unittest.TestCase):
    PARENT = "private-synthetic-parent"
    CHILD = "private-synthetic-child"
    UNRELATED = "private-synthetic-unrelated"
    TURN = "private-synthetic-shared-turn"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.sessions = self.base / "synthetic-sessions"
        self.directory = self.sessions / "2033" / "05" / "18"
        self.directory.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {
            "AGENT_EFFICIENCY_DATA": str(self.state),
            "AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        refresh = mock.patch.object(ae, "refresh_usage_snapshot", side_effect=AssertionError("Live refresh forbidden"))
        refresh.start()
        self.addCleanup(refresh.stop)

    @staticmethod
    def event() -> dict:
        return {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 7, "cached_input_tokens": 2,
                                 "output_tokens": 3, "reasoning_output_tokens": 1, "total_tokens": 10},
        }}}

    def metadata(self, session: str, turn: str | None = None, *, child: bool = False) -> list[dict]:
        source = {"subagent": {"thread_spawn": {"parent_thread_id": self.PARENT}}} if child else "cli"
        return [
            {"type": "session_meta", "payload": {"id": session, "source": source}},
            {"type": "turn_context", "payload": {"turn_id": turn or self.TURN, "model": "synthetic-model"}},
        ]

    def rollout(self, name: str, records: list[dict]) -> Path:
        path = self.directory / f"rollout-{name}.jsonl"
        self.replace(path, records)
        return path

    @staticmethod
    def replace(path: Path, records: list[dict]) -> None:
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    def read_state(self, name: str) -> dict:
        return json.loads((self.state / name).read_text(encoding="utf-8"))

    def cache(self) -> dict:
        return self.read_state("rollout-cache.json")

    @staticmethod
    def turn_key(session: str, turn: str) -> str:
        return ae._task_key_from_refs(ae.opaque_ref(session, "session"), ae.opaque_ref(turn, "turn"))

    def aggregate(self, session: str, turn: str | None = None) -> dict:
        return self.cache()["turns"][self.turn_key(session, turn or self.TURN)]

    def task(self, task_id: str, session: str, turn: str | None = None) -> dict:
        return ae.start_task(task_id=task_id, session_id=session, turn_id=turn or self.TURN,
                             mode="balanced", complexity="simple", model="synthetic-model")

    def seeded(self, *, task: bool = False) -> Path:
        if task:
            self.task("parent-task", self.PARENT)
        path = self.rollout("private-parent-path", self.metadata(self.PARENT) + [self.event(), self.event()])
        self.assertEqual(ae.scan_rollouts(self.sessions)["token_events_processed"], 2)
        return path

    def shrink(self, path: Path) -> dict:
        self.replace(path, [self.event()])  # A token record without new identity/context.
        return ae.scan_rollouts(self.sessions)

    def assert_unavailable(self, record: dict, total: int = 20) -> None:
        self.assertEqual(record["token_measurement_status"], ae.ROLLOUT_UNAVAILABLE_STATUS)
        self.assertEqual(record["token_source"], "unavailable")
        self.assertEqual(record["tokens"], {})
        self.assertEqual(record["invalidated_tokens"]["total"], total)
        self.assertFalse(ae._token_measurement_available(record))

    @staticmethod
    def audited_record() -> dict:
        return {
            "mode": "balanced", "complexity": "simple", "model": "synthetic-model",
            "status": "completed", "elapsed_seconds": 60,
            "tokens": {"total": 20, "model_calls": 2},
            "efficiency_score": {"score": 80},
            "forecast": {"available": True, "eta_minutes": {"low": 0.5, "high": 2},
                         "estimated_tokens": {"low": 10, "high": 30}},
            "usage_delta": [{"limit_id": "five-hour", "same_window": True,
                             "used_percent_delta": 2, "observation_seconds": 60}],
            "forecast_outcome": {
                "time": {"available": True, "within_range": True,
                         "relative_error": 0.2, "absolute_relative_error": 0.2},
                "tokens": {"available": True, "within_range": True,
                           "relative_error": -0.3, "absolute_relative_error": 0.3},
                "capacity": [{"available": True, "relative_error": 0.1, "absolute_relative_error": 0.1}],
                "range_dimensions_audited": 2, "range_dimensions_covered": 2,
            },
        }

    def test_detected_shrink_does_not_replay_events_or_reuse_previous_context(self) -> None:
        path = self.seeded()
        result = self.shrink(path)
        self.assertEqual(result["status"], "partial-unavailable")
        self.assertEqual(result["token_events_processed"], 0)
        self.assertEqual(result["lines_processed"], 0)
        self.assertEqual((result["quarantined_files"], result["unavailable_turns"]), (1, 1))
        self.assert_unavailable(self.aggregate(self.PARENT))
        state = next(iter(self.cache()["files"].values()))
        self.assertTrue(state["quarantined"])
        self.assertEqual(state["quarantine_reason"], "file-shrank")
        self.assertEqual(state["offset"], 0)
        for field in ("session_ref", "turn_ref", "model"):
            self.assertIsNone(state[field])
        self.assertFalse(state["subagent"])

    def test_regrowth_and_repeated_scans_remain_quarantined(self) -> None:
        path = self.seeded()
        self.shrink(path)
        original = copy.deepcopy(self.aggregate(self.PARENT))
        self.replace(path, self.metadata(self.CHILD, child=True) + [self.event()] * 20)
        for _ in range(3):
            result = ae.scan_rollouts(self.sessions)
            self.assertEqual(result["token_events_processed"], 0)
            self.assertEqual(result["status"], "partial-unavailable")
            self.assertEqual(self.aggregate(self.PARENT), original)
            self.assertEqual(len(self.cache()["turns"]), 1)

    def test_child_and_unrelated_sessions_keep_measured_tokens_when_parent_shrinks(self) -> None:
        self.task("parent-task", self.PARENT)
        self.task("child-task", self.CHILD)
        self.task("other-task", self.UNRELATED)
        parent = self.rollout("parent", self.metadata(self.PARENT) + [self.event(), self.event()])
        self.rollout("child", self.metadata(self.CHILD, child=True) + [self.event()])
        self.rollout("unrelated", self.metadata(self.UNRELATED) + [self.event()] * 3)
        ae.scan_rollouts(self.sessions)
        before = self.read_state("active.json")
        self.shrink(parent)
        active = self.read_state("active.json")
        self.assert_unavailable(active["parent-task"])
        for task in ("child-task", "other-task"):
            self.assertEqual(active[task], before[task])
            self.assertTrue(ae._token_measurement_available(active[task]))
        self.assertTrue(active["child-task"]["is_subagent"])
        self.assertEqual(active["other-task"]["tokens"]["total"], 30)

    def test_complete_file_tracking_limits_invalidation_to_its_turns(self) -> None:
        first = self.seeded()
        other_turn = "private-other-turn"
        self.rollout("same-session-other-turn", self.metadata(self.PARENT, other_turn) + [self.event()])
        ae.scan_rollouts(self.sessions)
        before = self.aggregate(self.PARENT, other_turn)
        self.shrink(first)
        self.assert_unavailable(self.aggregate(self.PARENT))
        self.assertEqual(self.aggregate(self.PARENT, other_turn), before)

    def test_legacy_schema_three_uses_session_fallback_without_resetting_archive(self) -> None:
        first = self.seeded()
        other_turn = "private-other-turn"
        self.rollout("same-session-other-turn", self.metadata(self.PARENT, other_turn) + [self.event()])
        self.rollout("unrelated", self.metadata(self.UNRELATED) + [self.event()])
        ae.scan_rollouts(self.sessions)
        cache = self.cache()
        self.assertEqual(cache["schema_version"], 3)
        key = ae.opaque_ref(str(first.resolve()), "rollout")
        cache["files"][key].pop("turn_keys")
        cache["files"][key].pop("turn_tracking_complete")
        (self.state / "rollout-cache.json").write_text(json.dumps(cache), encoding="utf-8")
        unrelated = self.aggregate(self.UNRELATED)
        self.shrink(first)
        self.assert_unavailable(self.aggregate(self.PARENT))
        self.assert_unavailable(self.aggregate(self.PARENT, other_turn), 10)
        self.assertEqual(self.aggregate(self.UNRELATED), unrelated)
        self.assertEqual(len(self.cache()["turns"]), 3)
        self.assertFalse(self.cache()["files"][key]["turn_tracking_complete"])

    def test_active_tokens_become_unavailable_with_original_evidence_retained(self) -> None:
        path = self.seeded(task=True)
        before = self.read_state("active.json")["parent-task"]
        self.shrink(path)
        after = self.read_state("active.json")["parent-task"]
        self.assert_unavailable(after)
        self.assertEqual(after["invalidated_tokens"], before["tokens"])
        for field in ("started_at", "forecast", "session_ref", "turn_ref", "mode", "model"):
            self.assertEqual(after[field], before[field])

    def test_history_preserves_time_usage_and_invalidated_audit_evidence(self) -> None:
        path = self.seeded()
        record = self.audited_record()
        record.update(session_ref=ae.opaque_ref(self.PARENT, "session"),
                      turn_ref=ae.opaque_ref(self.TURN, "turn"))
        (self.state / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        self.shrink(path)
        after = ae.list_history()[0]
        self.assert_unavailable(after)
        self.assertEqual(after["elapsed_seconds"], record["elapsed_seconds"])
        self.assertEqual(after["usage_delta"], record["usage_delta"])
        self.assertIsNone(after["efficiency_score"])
        self.assertEqual(after["invalidated_efficiency_score"], record["efficiency_score"])
        self.assertEqual(after["invalidated_token_forecast"], record["forecast_outcome"]["tokens"])
        outcome = after["forecast_outcome"]
        self.assertFalse(outcome["tokens"]["available"])
        self.assertEqual(outcome["time"], record["forecast_outcome"]["time"])
        self.assertEqual(outcome["capacity"], record["forecast_outcome"]["capacity"])
        self.assertEqual((outcome["range_dimensions_audited"], outcome["range_dimensions_covered"]), (1, 1))
        first_bytes = (self.state / "history.jsonl").read_bytes()
        ae.scan_rollouts(self.sessions)
        self.assertEqual((self.state / "history.jsonl").read_bytes(), first_bytes)

    def test_completion_cannot_revive_invalidated_tokens_from_late_hook_totals(self) -> None:
        path = self.seeded(task=True)
        self.shrink(path)
        record = ae.complete_task("parent-task", tokens={"input": 700, "output": 300, "model_calls": 1})
        self.assert_unavailable(record)
        self.assertIsNone(record["efficiency_score"])
        self.assertIsNotNone(record["elapsed_seconds"])
        self.assertFalse(record["forecast_outcome"]["tokens"]["available"])

    def test_alias_file_cannot_revive_invalidated_aggregate_or_history(self) -> None:
        path = self.seeded(task=True)
        self.shrink(path)
        ae.complete_task("parent-task")
        original = self.aggregate(self.PARENT)
        history = ae.list_history()[0]
        self.rollout("alias-copy", self.metadata(self.PARENT) + [self.event()] * 4)
        result = ae.scan_rollouts(self.sessions)
        self.assertEqual(result["token_events_processed"], 0)
        self.assertEqual(self.aggregate(self.PARENT), original)
        after = ae.list_history()[0]
        self.assert_unavailable(after)
        for field in ("tokens", "invalidated_tokens", "elapsed_seconds", "usage_delta", "efficiency_score"):
            self.assertEqual(after[field], history[field])
        self.assertFalse(after["forecast_outcome"]["tokens"]["available"])
        self.assertEqual(after, history)

    def test_calibration_rejects_flagged_tokens_even_if_legacy_totals_are_present(self) -> None:
        record = self.audited_record()
        record["token_measurement_status"] = ae.ROLLOUT_UNAVAILABLE_STATUS
        with mock.patch.object(ae, "_recent_history", return_value=[record] * 3):
            calibrated = ae._history_calibration("balanced", "simple", "synthetic-model")
        self.assertEqual(calibrated[:4], (1.0, 0.0, 3, 0))
        self.assertEqual(calibrated[8:10], (3, 0))
        self.assertEqual(ae._forecast_residuals([record], "tokens"), [])
        self.assertEqual(ae._forecast_residuals([record], "time"), [0.2])
        self.assertIsNone(ae._efficiency_score(record))
        outcome = ae._forecast_outcome(record)
        self.assertTrue(outcome["time"]["available"])
        self.assertFalse(outcome["tokens"]["available"])

    def test_diagnostics_exclude_flagged_token_audits_but_keep_time_and_capacity(self) -> None:
        record = self.audited_record()
        record["token_measurement_status"] = ae.ROLLOUT_UNAVAILABLE_STATUS
        with mock.patch.object(ae, "list_history", return_value=[record]):
            diagnostics = ae.forecast_diagnostics()
        self.assertEqual(diagnostics["dimensions"]["time"]["audited"], 1)
        self.assertEqual(diagnostics["dimensions"]["capacity"]["audited"], 1)
        self.assertEqual(diagnostics["dimensions"]["tokens"]["audited"], 0)
        self.assertEqual(diagnostics["dimensions"]["tokens"]["unavailable"], 1)
        self.assertEqual(diagnostics["calibration"]["jointly_audited"], 0)
        with mock.patch.object(ae, "_recent_history", return_value=[record]):
            self.assertEqual(ae._observed_percent_cost("balanced", "five-hour"), (2.0, 1))

    def test_quarantine_result_and_persisted_state_do_not_expose_source_identifiers(self) -> None:
        path = self.seeded(task=True)
        result = self.shrink(path)
        self.assertEqual(set(result), {"files_seen", "lines_processed", "token_events_processed",
                                      "turns_updated", "status", "quarantined_files", "unavailable_turns"})
        texts = [json.dumps(result)] + [
            (self.state / filename).read_text(encoding="utf-8")
            for filename in ("rollout-cache.json", "active.json")
        ]
        for text in texts:
            for private in (self.PARENT, self.TURN, str(self.sessions), "private-parent-path", "parent_thread_id"):
                self.assertNotIn(private, text)

    def test_quarantine_only_marks_plugin_cache_and_leaves_source_and_workspace_untouched(self) -> None:
        path = self.seeded()
        other = self.rollout("untouched", self.metadata(self.UNRELATED) + [self.event()])
        canary = self.base / "unrelated-workspace-file.txt"
        canary.write_text("synthetic user-owned workspace canary\n", encoding="utf-8")
        self.replace(path, [self.event()])
        originals = {
            item: (item.read_bytes(), item.stat().st_size, item.stat().st_mtime_ns)
            for item in (path, other, canary)
        }
        paths_before = set(self.sessions.rglob("*"))
        for _ in range(2):
            result = ae.scan_rollouts(self.sessions)
            self.assertEqual(result["quarantined_files"], 1)
            self.assertEqual(set(self.sessions.rglob("*")), paths_before)
            for item, snapshot in originals.items():
                self.assertTrue(item.is_file())
                self.assertEqual((item.read_bytes(), item.stat().st_size, item.stat().st_mtime_ns), snapshot)


if __name__ == "__main__":
    unittest.main()
