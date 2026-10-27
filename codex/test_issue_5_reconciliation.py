"""Issue #5: late synthetic rollout measurements repair token audits only.

These tests use disposable plugin state and synthetic records. Live refresh,
account fetch, and archive scanning are forbidden; no installed state is read.
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


class RolloutReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.state = self.base / "state"
        env = mock.patch.dict(os.environ, {
            "AGENT_EFFICIENCY_DATA": str(self.state),
            "AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH": "1",
            "AGENT_EFFICIENCY_CODEX_BIN": str(self.base / "unavailable-codex.exe"),
            "CODEX_HOME": str(self.base / "synthetic-codex"),
        })
        env.start()
        self.addCleanup(env.stop)
        for name in ("refresh_usage_snapshot", "fetch_rate_limits", "scan_rollouts"):
            boundary = mock.patch.object(ae, name, side_effect=AssertionError(f"{name} forbidden"))
            boundary.start()
            self.addCleanup(boundary.stop)
        self.session_ref = ae.opaque_ref("synthetic-reconciliation-session", "session")
        self.turn_ref = ae.opaque_ref("synthetic-reconciliation-turn", "turn")
        self.key = ae._task_key_from_refs(self.session_ref, self.turn_ref)
        self.history = self.state / "history.jsonl"

    @staticmethod
    def tokens(total: int) -> dict:
        return ae._token_total({"input": total // 2, "output": total - total // 2,
                                "model_calls": 2 if total > 2 else 1})

    def record(self, total: int | None = 2) -> dict:
        record = {
            "task_id": "synthetic-task", "session_ref": self.session_ref,
            "turn_ref": self.turn_ref, "mode": "balanced", "complexity": "simple",
            "model": "synthetic-model", "status": "completed", "elapsed_seconds": 60,
            "tokens": self.tokens(total) if total is not None else {},
            "token_source": "unavailable", "tool_calls": 3, "subagents": 1,
            "forecast": {"available": True, "eta_minutes": {"low": 0.5, "high": 2},
                         "estimated_tokens": {"low": 10, "high": 30}},
            "usage_delta": [{"limit_id": "synthetic-window", "same_window": True,
                             "used_percent_delta": 2, "observation_seconds": 60}],
        }
        record["forecast_outcome"] = ae._forecast_outcome(record)
        record["efficiency_score"] = ae._efficiency_score(record)
        # Preserved evidence deliberately cannot be re-derived from this record's
        # forecast. A token correction must not overwrite unrelated audit history.
        record["forecast_outcome"]["capacity"] = [{
            "available": True, "relative_error": 0.1, "absolute_relative_error": 0.1,
            "actual_used_percent_delta": 2, "evidence": "original-capacity-audit",
        }]
        record["forecast_outcome"]["time"]["evidence"] = "original-time-audit"
        record["forecast_outcome"]["extension"] = {"preserve": True}
        return record

    def aggregate(self, total: int = 20) -> dict:
        return {
            "session_ref": self.session_ref, "turn_ref": self.turn_ref,
            "tokens": self.tokens(total), "updated_at": "2033-05-18T12:00:00+00:00",
            "model": "synthetic-measured-model", "subagent": True,
        }

    def seed(self, record: dict) -> None:
        ae._atomic_text(self.history, json.dumps(record) + "\n")

    def reconcile(self, aggregate: dict | None = None) -> tuple[bool, dict]:
        changed = ae._reconcile_history_rollouts(
            self.history, {self.key: aggregate if aggregate is not None else self.aggregate()})
        return changed, json.loads(self.history.read_text(encoding="utf-8"))

    def assert_repaired(self, record: dict) -> None:
        self.assertEqual(record["tokens"], self.tokens(20))
        self.assertEqual(record["token_source"], "codex-rollout")
        outcome = record["forecast_outcome"]
        self.assertEqual(outcome["tokens"]["actual_tokens"], 20)
        self.assertTrue(outcome["tokens"]["within_range"])
        self.assertEqual(outcome["tokens"]["absolute_relative_error"], 0)
        self.assertEqual((outcome["range_dimensions_audited"], outcome["range_dimensions_covered"]), (2, 2))

    def test_late_correction_repairs_token_audit_counters_score_and_source(self) -> None:
        before = self.record()
        self.assertEqual(before["forecast_outcome"]["tokens"]["actual_tokens"], 2)
        self.assertEqual(before["forecast_outcome"]["range_dimensions_covered"], 1)
        self.seed(before)
        changed, after = self.reconcile()
        self.assertTrue(changed)
        self.assert_repaired(after)
        self.assertEqual(after["tokens_updated_at"], self.aggregate()["updated_at"])
        self.assertEqual(after["efficiency_score"], ae._efficiency_score(after))
        self.assertNotEqual(after["efficiency_score"], before["efficiency_score"])
        self.assertEqual(after["model"], "synthetic-measured-model")
        self.assertTrue(after["is_subagent"])
        self.assertEqual((after["tool_calls"], after["subagents"]), (3, 1))

    def test_previously_unavailable_tokens_gain_a_measured_audit(self) -> None:
        before = self.record(None)
        self.assertFalse(before["forecast_outcome"]["tokens"]["available"])
        self.assertEqual(before["forecast_outcome"]["range_dimensions_audited"], 1)
        self.seed(before)
        _, after = self.reconcile()
        self.assert_repaired(after)

    def test_equal_totals_repair_stale_audit_without_claiming_new_tokens(self) -> None:
        before = self.record(20)
        before["token_source"] = "codex-rollout"
        stale = self.record()["forecast_outcome"]
        before["forecast_outcome"]["tokens"] = stale["tokens"]
        before["forecast_outcome"]["range_dimensions_covered"] = 1
        self.seed(before)
        changed, after = self.reconcile()
        self.assertTrue(changed)
        self.assert_repaired(after)
        self.assertNotIn("tokens_updated_at", after)

    def test_equal_totals_repair_unavailable_source_even_with_current_audit(self) -> None:
        self.seed(self.record(20))
        changed, after = self.reconcile()
        self.assertTrue(changed)
        self.assert_repaired(after)
        self.assertNotIn("tokens_updated_at", after)

    def test_repeated_reconciliation_is_byte_identical_and_reports_no_change(self) -> None:
        self.seed(self.record())
        self.reconcile()
        original = self.history.read_bytes()
        for _ in range(3):
            changed, after = self.reconcile()
            self.assertFalse(changed)
            self.assertEqual(self.history.read_bytes(), original)
            self.assert_repaired(after)

    def test_token_correction_preserves_time_capacity_and_other_evidence(self) -> None:
        before = self.record()
        self.seed(before)
        _, after = self.reconcile()
        for field in ("elapsed_seconds", "usage_delta", "forecast", "tool_calls", "subagents"):
            self.assertEqual(after[field], before[field], field)
        for dimension in ("time", "capacity", "extension"):
            self.assertEqual(after["forecast_outcome"][dimension], before["forecast_outcome"][dimension], dimension)
        self.assert_repaired(after)

    def test_residuals_and_diagnostics_consume_the_repaired_audit(self) -> None:
        self.seed(self.record())
        _, after = self.reconcile()
        self.assertEqual(ae._forecast_residuals([after], "tokens"), [0.0])
        diagnostics = ae.forecast_diagnostics()
        tokens = diagnostics["dimensions"]["tokens"]
        self.assertEqual((tokens["audited"], tokens["covered"], tokens["coverage_rate"]), (1, 1, 1.0))
        self.assertEqual(tokens["median_absolute_relative_error"], 0)
        self.assertEqual(diagnostics["dimensions"]["capacity"]["audited"], 1)
        self.assertEqual(diagnostics["calibration"]["jointly_audited"], 1)

    def test_completion_retains_scanner_measurement_source_when_tokens_omitted(self) -> None:
        forecast = copy.deepcopy(self.record()["forecast"])
        with mock.patch.object(ae, "_forecast_snapshot", return_value=forecast), \
                mock.patch.object(ae, "_active_windows", return_value=[]):
            ae.start_task(task_id="synthetic-task", mode="balanced", complexity="simple")
            active_path = self.state / "active.json"
            active = json.loads(active_path.read_text(encoding="utf-8"))
            active["synthetic-task"].update(tokens=self.tokens(20), token_source="unavailable",
                                            started_at="2033-05-18T12:00:00+00:00")
            ae._atomic_json(active_path, active)
            with mock.patch.object(ae.time, "time", return_value=ae._epoch("2033-05-18T12:01:00+00:00")):
                completed = ae.complete_task("synthetic-task")
        self.assert_repaired(completed)

    def test_completion_source_distinguishes_cached_measurements_from_caller_overrides(self) -> None:
        cases = (
            ("omitted", None, "codex-rollout", 20, 2),
            ("empty", {}, "codex-rollout", 20, 2),
            ("sparse-override", {"input": 30}, "unavailable", 40, 2),
            ("cli-alias-override", {"input_tokens": 30}, "unavailable", 40, 2),
            ("explicit-measurement", {"input": 30, "model_calls": 3}, "codex-rollout", 40, 3),
            ("normalized-measurement", {"input": 30, "model_calls": "3"}, "codex-rollout", 40, 3),
            ("explicit-unmeasured", {"input": 30, "model_calls": 0}, "unavailable", 40, 0),
            ("normalized-unmeasured", {"input": 30, "model_calls": "0"}, "unavailable", 40, 0),
        )
        forecast = copy.deepcopy(self.record()["forecast"])
        with mock.patch.object(ae, "_forecast_snapshot", return_value=forecast), \
                mock.patch.object(ae, "_active_windows", return_value=[]):
            for label, payload, source, total, calls in cases:
                with self.subTest(case=label):
                    task_id = f"synthetic-{label}"
                    ae.start_task(task_id=task_id, mode="balanced", complexity="simple")
                    active_path = self.state / "active.json"
                    active = json.loads(active_path.read_text(encoding="utf-8"))
                    active[task_id].update(tokens=self.tokens(20), token_source="codex-rollout",
                                           started_at="2033-05-18T12:00:00+00:00")
                    ae._atomic_json(active_path, active)
                    with mock.patch.object(ae.time, "time", return_value=ae._epoch("2033-05-18T12:01:00+00:00")):
                        completed = ae.complete_task(task_id, **({} if payload is None else {"tokens": payload}))
                    self.assertEqual(completed["tokens"]["total"], total)
                    self.assertEqual(completed["tokens"]["model_calls"], calls)
                    self.assertEqual(completed["token_source"], source)

    def test_malformed_token_forecast_is_unavailable_without_losing_other_audits(self) -> None:
        for malformed in (None, [], "bad", {"low": "private-invalid", "high": 30},
                          {"low": 30, "high": 10}, {"low": 10 ** 400, "high": 10 ** 400},
                          {"low": 1e308, "high": 1e308}, {"low": 1e-308, "high": 1e-308}):
            with self.subTest(predicted=malformed):
                before = self.record()
                before["forecast"]["estimated_tokens"] = malformed
                self.seed(before)
                _, after = self.reconcile()
                self.assertEqual(after["tokens"]["total"], 20)
                self.assertEqual(after["token_source"], "codex-rollout")
                outcome = after["forecast_outcome"]
                self.assertFalse(outcome["tokens"]["available"])
                self.assertNotIn("private-invalid", json.dumps(outcome))
                self.assertEqual((outcome["range_dimensions_audited"], outcome["range_dimensions_covered"]), (1, 1))
                for dimension in ("time", "capacity", "extension"):
                    self.assertEqual(outcome[dimension], before["forecast_outcome"][dimension])

    def test_absent_or_unavailable_forecast_cannot_leave_a_stale_token_audit(self) -> None:
        for forecast in (None, [], {"available": False}, {"available": False, "estimated_tokens": {"low": 10, "high": 30}}):
            with self.subTest(forecast=forecast):
                before = self.record()
                before["forecast"] = forecast
                self.seed(before)
                _, after = self.reconcile()
                self.assertEqual(after["tokens"]["total"], 20)
                self.assertEqual(after["token_source"], "codex-rollout")
                self.assertFalse(after["forecast_outcome"]["tokens"]["available"])
                self.assertEqual(ae._forecast_residuals([after], "tokens"), [])
                for dimension in ("time", "capacity"):
                    self.assertEqual(after["forecast_outcome"][dimension], before["forecast_outcome"][dimension])

    def test_missing_outcome_gets_token_only_audit_not_reconstructed_time_or_capacity(self) -> None:
        before = self.record()
        before.pop("forecast_outcome")
        self.seed(before)
        _, after = self.reconcile()
        outcome = after["forecast_outcome"]
        self.assertEqual(outcome["tokens"]["actual_tokens"], 20)
        self.assertFalse(outcome["time"]["available"])
        self.assertEqual(outcome["capacity"], [])
        self.assertEqual((outcome["range_dimensions_audited"], outcome["range_dimensions_covered"]), (1, 1))

    def test_failed_outcome_stays_unavailable_to_calibration_when_tokens_arrive(self) -> None:
        before = self.record()
        before["forecast_outcome"] = {"schema_version": ae.FORECAST_SCHEMA_VERSION,
                                      "available": False, "status": "outcome-generation-failed"}
        self.seed(before)
        _, after = self.reconcile()
        outcome = after["forecast_outcome"]
        self.assertEqual(outcome["status"], "outcome-generation-failed")
        self.assertFalse(outcome["available"])
        self.assertFalse(outcome["tokens"]["available"])
        self.assertFalse(outcome["time"]["available"])
        self.assertEqual(outcome["capacity"], [])
        self.assertEqual(ae._forecast_residuals([after], "tokens"), [])
        self.assertEqual(after["tokens"]["total"], 20)
        self.assertEqual(after["token_source"], "codex-rollout")

    def test_valid_aggregate_cannot_revive_quarantined_history(self) -> None:
        before = self.record()
        ae._invalidate_rollout_tokens(before)
        self.seed(before)
        _, after = self.reconcile()
        self.assertEqual(after["token_measurement_status"], ae.ROLLOUT_UNAVAILABLE_STATUS)
        self.assertEqual(after["tokens"], {})
        self.assertEqual(after["token_source"], "unavailable")
        self.assertIsNone(after["efficiency_score"])
        self.assertFalse(after["forecast_outcome"]["tokens"]["available"])
        for field in ("invalidated_tokens", "invalidated_token_forecast", "invalidated_efficiency_score"):
            self.assertEqual(after[field], before[field])
        for dimension in ("time", "capacity"):
            self.assertEqual(after["forecast_outcome"][dimension], before["forecast_outcome"][dimension])
        self.assertEqual(ae._forecast_residuals([after], "tokens"), [])

    def test_quarantined_aggregate_invalidates_existing_measured_history(self) -> None:
        before = self.record()
        aggregate = self.aggregate()
        ae._invalidate_rollout_tokens(aggregate)
        self.seed(before)
        _, after = self.reconcile(aggregate)
        self.assertEqual(after["tokens"], {})
        self.assertEqual(after["token_source"], "unavailable")
        self.assertEqual(after["invalidated_tokens"], before["tokens"])
        self.assertFalse(after["forecast_outcome"]["tokens"]["available"])
        self.assertEqual(after["forecast_outcome"]["time"], before["forecast_outcome"]["time"])


if __name__ == "__main__":
    unittest.main()
