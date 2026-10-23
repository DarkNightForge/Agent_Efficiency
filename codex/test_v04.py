from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "agent-efficiency" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_efficiency as ae


class V04ForecastAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        os.environ["AGENT_EFFICIENCY_DATA"] = self.temp.name
        os.environ["AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH"] = "1"

    def tearDown(self) -> None:
        os.environ.pop("AGENT_EFFICIENCY_DATA", None)
        os.environ.pop("AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH", None)
        self.temp.cleanup()

    @staticmethod
    def estimate_fixture() -> dict:
        return {
            "mode": "balanced",
            "eta_minutes": {"low": 1.0, "high": 3.0},
            "estimated_tokens": {"low": 10_000, "high": 20_000},
            "confidence": "medium",
            "calibration": {
                "model_scope": "model-specific",
                "complexity_scope": "exact",
                "time_samples": 5,
                "token_samples": 5,
                "empirical_spread": 0.25,
            },
            "assumptions": {
                "complexity": "simple",
                "context": "medium",
                "model": "gpt-test",
                "reasoning_effort": "medium",
                "execution_location": "local",
                "fast_mode": False,
                "expected_tools": 3,
                "expected_subagents": 0,
                "expected_media": 0,
                "cached_ratio": 0.25,
            },
            "capacity": {
                "available": True,
                "status": "estimated",
                "confidence": "medium",
                "calibration_status": "observed",
                "sample_count": 5,
                "measurement_source": "codex-app-server",
                "snapshot_age_seconds": 2,
                "basis": "observed comparable turns",
                "constraining_window": "five-hour",
                "estimated_remaining": {"low": 4, "high": 6},
                "windows": [{
                    "limit_id": "five-hour",
                    "expected_task_cost_percent": 2.0,
                    "task_cost_basis": "observed comparable turns",
                    "task_cost_sample_count": 5,
                    "used_percent": 10,
                    "resets_at": 9999999999,
                }],
            },
        }

    def test_start_task_records_a_minimal_privacy_safe_forecast(self) -> None:
        with mock.patch.object(ae, "estimate_mode", return_value=self.estimate_fixture()):
            started = ae.start_task(
                task_id="audit",
                session_id="raw-session-secret",
                turn_id="raw-turn-secret",
                mode="balanced",
                complexity="simple",
                model="gpt-test",
                transcript_path="C:/private/transcript.jsonl",
            )

        forecast = started["forecast"]
        self.assertEqual(forecast["schema_version"], 1)
        self.assertEqual(forecast["eta_minutes"], {"low": 1.0, "high": 3.0})
        self.assertEqual(forecast["capacity"]["windows"], [{
            "limit_id": "five-hour",
            "window_instance": ae._window_instance({
                "limit_id": "five-hour", "resets_at": 9999999999,
            }),
            "expected_task_cost_percent": 2.0,
            "task_cost_basis": "observed comparable turns",
            "task_cost_sample_count": 5,
        }])
        serialized = json.dumps(started)
        self.assertNotIn("raw-session-secret", serialized)
        self.assertNotIn("raw-turn-secret", serialized)
        self.assertNotIn("private/transcript", serialized)
        self.assertNotIn("used_percent", json.dumps(forecast["capacity"]))
        self.assertNotIn("resets_at", json.dumps(forecast["capacity"]))

    def test_forecast_failure_never_blocks_task_start(self) -> None:
        with mock.patch.object(ae, "estimate_mode", side_effect=RuntimeError("C:/private/path")):
            started = ae.start_task(task_id="safe")
        self.assertEqual(started["forecast"]["status"], "forecast-generation-failed")
        self.assertNotIn("private", json.dumps(started["forecast"]))

    def test_malformed_projection_never_blocks_start_or_prompt_hook(self) -> None:
        malformed = self.estimate_fixture()
        malformed["capacity"]["windows"] = None
        with mock.patch.object(ae, "estimate_mode", return_value=malformed):
            started = ae.start_task(task_id="safe-shape")
            result = ae.handle_hook({
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session",
                "turn_id": "turn",
            })
        self.assertEqual(started["forecast"]["status"], "forecast-generation-failed")
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")

    def test_hook_tasks_are_versioned_and_unclassified(self) -> None:
        with mock.patch.object(ae, "refresh_usage_snapshot"), mock.patch.object(
            ae, "estimate_capacity", return_value={"available": False, "status": "unavailable"}
        ):
            ae.handle_hook({
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session",
                "turn_id": "turn",
            })
        active = json.loads((Path(self.temp.name) / "active.json").read_text(encoding="utf-8"))
        record = next(iter(active.values()))
        self.assertEqual(record["record_schema_version"], 2)
        self.assertEqual(record["complexity"], "unclassified")
        self.assertEqual(record["forecast"]["assumptions"]["heuristic_complexity"], "moderate")

    def test_completion_audits_time_tokens_and_capacity(self) -> None:
        with mock.patch.object(ae, "estimate_mode", return_value=self.estimate_fixture()):
            ae.start_task(task_id="audit", mode="balanced", complexity="simple", model="gpt-test")
        active_path = Path(self.temp.name) / "active.json"
        active = json.loads(active_path.read_text(encoding="utf-8"))
        active["audit"]["started_at"] = datetime.fromtimestamp(
            time.time() - 120, tz=timezone.utc
        ).isoformat()
        active["audit"]["usage_before"] = [{
            "limit_id": "five-hour", "used_percent": 10.0, "resets_at": 9999999999,
        }]
        active_path.write_text(json.dumps(active), encoding="utf-8")

        done = ae.complete_task(
            "audit",
            tokens={"input": 12_000, "output": 3_000},
            usage_after=[{
                "limit_id": "five-hour", "used_percent": 12.5, "resets_at": 9999999999,
            }],
        )

        outcome = done["forecast_outcome"]
        self.assertTrue(outcome["time"]["within_range"])
        self.assertTrue(outcome["tokens"]["within_range"])
        self.assertEqual(outcome["tokens"]["actual_tokens"], 15_000)
        self.assertEqual(outcome["capacity"][0]["actual_used_percent_delta"], 2.5)
        self.assertEqual(outcome["capacity"][0]["relative_error"], 0.25)
        self.assertEqual(outcome["range_dimensions_audited"], 2)
        self.assertEqual(outcome["range_dimensions_covered"], 2)

    def test_reset_cycle_change_cannot_create_a_capacity_measurement(self) -> None:
        changed = ae._usage_deltas(
            [{"limit_id": "five-hour", "used_percent": 10, "resets_at": 100}],
            [{"limit_id": "five-hour", "used_percent": 20, "resets_at": 200}],
        )
        self.assertFalse(changed[0]["same_window"])
        self.assertNotIn("used_percent_delta", changed[0])

        same = ae._usage_deltas(
            [{"limit_id": "five-hour", "used_percent": 10, "resets_at": 200}],
            [{"limit_id": "five-hour", "used_percent": 12, "resets_at": 200}],
        )
        self.assertTrue(same[0]["same_window"])
        self.assertEqual(same[0]["used_percent_delta"], 2)

        forecast = self.estimate_fixture()
        with mock.patch.object(ae, "estimate_mode", return_value=forecast):
            snapshot = ae._forecast_snapshot("balanced", "simple", "gpt-test")
        outcomes = ae._capacity_forecast_outcomes(snapshot, changed)
        self.assertFalse(outcomes[0]["available"])

    def test_completion_is_fail_open_for_malformed_or_failing_outcome(self) -> None:
        ae.start_task(task_id="malformed")
        active_path = Path(self.temp.name) / "active.json"
        active = json.loads(active_path.read_text(encoding="utf-8"))
        active["malformed"]["forecast"] = {
            "available": True,
            "eta_minutes": {"low": 1, "high": 2},
            "estimated_tokens": {"low": 1_000, "high": 2_000},
            "capacity": {"available": True, "windows": None},
        }
        active_path.write_text(json.dumps(active), encoding="utf-8")
        done = ae.complete_task("malformed")
        self.assertEqual(done["forecast_outcome"]["capacity"], [])

        ae.start_task(task_id="failing")
        with mock.patch.object(ae, "_forecast_outcome", side_effect=TypeError("bad shape")):
            done = ae.complete_task("failing")
        self.assertEqual(done["forecast_outcome"]["status"], "outcome-generation-failed")
        self.assertEqual(len(ae.list_history(10)), 2)

    def test_legacy_active_record_completes_without_destructive_migration(self) -> None:
        active_path = Path(self.temp.name) / "active.json"
        active_path.write_text(json.dumps({
            "legacy": {
                "task_id": "legacy",
                "mode": "balanced",
                "complexity": "unspecified",
                "started_at": datetime.fromtimestamp(time.time() - 1, tz=timezone.utc).isoformat(),
            },
        }), encoding="utf-8")
        done = ae.complete_task("legacy", usage_after=[])
        self.assertNotIn("record_schema_version", done)
        self.assertEqual(done["complexity"], "unspecified")
        self.assertIsNone(done["forecast_outcome"])

    def test_legacy_unspecified_history_is_an_unclassified_fallback(self) -> None:
        history = Path(self.temp.name) / "history.jsonl"
        records = [{
            "mode": "balanced",
            "complexity": "unspecified",
            "status": "completed",
            "model": "m",
            "elapsed_seconds": 60,
            "tokens": {"total": 10_000},
        } for _ in range(3)]
        history.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
        estimate = ae.estimate_mode("balanced", complexity="simple", model="m", include_capacity=False)
        self.assertEqual(estimate["calibration"]["complexity_scope"], "unclassified-fallback")
        self.assertEqual(estimate["confidence"], "low")

    def test_missing_measurements_are_not_scored(self) -> None:
        record = {
            "forecast": ae._forecast_snapshot("balanced", "moderate", None),
            "elapsed_seconds": None,
            "tokens": {"total": 0},
            "usage_delta": [],
        }
        outcome = ae._forecast_outcome(record)
        self.assertIsNotNone(outcome)
        self.assertFalse(outcome["time"]["available"])
        self.assertFalse(outcome["tokens"]["available"])
        self.assertEqual(outcome["range_dimensions_audited"], 0)
        self.assertEqual(outcome["range_dimensions_covered"], 0)

    def test_nonfinite_forecast_values_are_not_scored(self) -> None:
        outcome = ae._range_forecast_outcome(
            10,
            {"low": float("nan"), "high": 20},
            actual_key="actual_tokens",
        )
        self.assertFalse(outcome["available"])

    def test_range_boundaries_are_inclusive_and_invalid_capacity_is_unavailable(self) -> None:
        for actual in (10, 20):
            self.assertTrue(ae._range_forecast_outcome(
                actual, {"low": 10, "high": 20}, actual_key="actual_tokens"
            )["within_range"])
        for actual in (9.99, 20.01):
            self.assertFalse(ae._range_forecast_outcome(
                actual, {"low": 10, "high": 20}, actual_key="actual_tokens"
            )["within_range"])

        instance = "window-instance"
        delta = [{
            "limit_id": "five-hour", "window_instance": instance,
            "same_window": True, "used_percent_delta": 2,
        }]
        for expected in (0, -1, float("nan"), "2"):
            forecast = {"capacity": {"available": True, "windows": [{
                "limit_id": "five-hour", "window_instance": instance,
                "expected_task_cost_percent": expected,
            }]}}
            self.assertFalse(ae._capacity_forecast_outcomes(forecast, delta)[0]["available"])

        valid_forecast = {"capacity": {"available": True, "windows": [{
            "limit_id": "five-hour", "window_instance": instance,
            "expected_task_cost_percent": 2,
        }]}}
        for actual in (0, -1, float("inf"), "2"):
            invalid_delta = [{
                "limit_id": "five-hour", "window_instance": instance,
                "same_window": True, "used_percent_delta": actual,
            }]
            self.assertFalse(
                ae._capacity_forecast_outcomes(valid_forecast, invalid_delta)[0]["available"]
            )


if __name__ == "__main__":
    unittest.main()
