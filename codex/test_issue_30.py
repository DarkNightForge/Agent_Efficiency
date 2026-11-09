"""Regression coverage for GitHub issue #30's false lockout warnings."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "agent-efficiency" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_efficiency as ae
import budget_policy


class ActiveTurnBurnForecastTests(unittest.TestCase):
    NOW = 2_000_000_000.0

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        os.environ["AGENT_EFFICIENCY_DATA"] = self.temp.name
        os.environ["AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH"] = "1"

    def tearDown(self) -> None:
        os.environ.pop("AGENT_EFFICIENCY_DATA", None)
        os.environ.pop("AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH", None)
        self.temp.cleanup()

    def _write_history(self, *records: dict) -> None:
        path = Path(self.temp.name) / "history.jsonl"
        path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )

    def _turn(self, *, delta: float, elapsed: float, observation_seconds: float | None = None) -> dict:
        usage_delta = {
            "limit_id": "codex",
            "same_window": True,
            "window_instance": ae._window_instance({
                "limit_id": "codex", "resets_at": self.NOW + 4 * 3600,
            }),
            "used_percent_delta": delta,
        }
        if observation_seconds is not None:
            usage_delta["observation_seconds"] = observation_seconds
        return {
            "mode": "balanced",
            "status": "completed",
            "elapsed_seconds": elapsed,
            "usage_delta": [usage_delta],
        }

    def _weekly_window(self, used: float, elapsed_fraction: float = 0.5) -> dict:
        duration_seconds = 10_080 * 60
        return {
            "limit_id": "codex",
            "used_percent": used,
            "window_duration_mins": 10_080,
            "resets_at": self.NOW + duration_seconds * (1 - elapsed_fraction),
        }

    def test_under_pace_weekly_window_ignores_short_active_turn_spike(self) -> None:
        self._write_history(self._turn(delta=1, elapsed=60))
        with mock.patch.object(ae.time, "time", return_value=self.NOW):
            capacity = ae.estimate_capacity("balanced", [self._weekly_window(30)])
            decisions = ae._policy_decisions(capacity, "balanced")

        window = capacity["windows"][0]
        self.assertEqual(window["pace_ratio"], 0.6)
        self.assertEqual(window["forecast_verdict"], "no-lockout")
        self.assertEqual(window["active_turn_burn_percent_per_hour"], 60)
        self.assertEqual(window["active_turn_burn_basis"], "continuous-work scenario only")
        self.assertEqual(decisions["lockout_limit_ids"], [])
        self.assertTrue(decisions["go_wide"])
        rendered = budget_policy.render_usage_budget(
            capacity,
            mode_label="Balanced",
            mode_behavior="Balance speed and confidence",
            decisions=decisions,
        )
        self.assertNotIn("WOULD RUN OUT", rendered)
        self.assertIn("continuous-work scenario", rendered)

    def test_above_pace_weekly_window_can_promote_from_calendar_progress(self) -> None:
        with mock.patch.object(ae.time, "time", return_value=self.NOW):
            capacity = ae.estimate_capacity("balanced", [self._weekly_window(70)])
            decisions = ae._policy_decisions(capacity, "balanced")

        window = capacity["windows"][0]
        self.assertEqual(window["pace_ratio"], 1.4)
        self.assertEqual(window["burn_basis"], "measured window progress over calendar time")
        self.assertEqual(window["forecast_verdict"], "lockout")
        self.assertEqual(decisions["lockout_limit_ids"], ["codex"])

    def test_well_spanned_five_hour_burn_keeps_meaningful_warning(self) -> None:
        self._write_history(self._turn(delta=10, elapsed=60, observation_seconds=3_600))
        window = {
            "limit_id": "codex",
            "used_percent": 80,
            "window_duration_mins": 300,
            "resets_at": self.NOW + 4 * 3_600,
        }
        with mock.patch.object(ae.time, "time", return_value=self.NOW):
            capacity = ae.estimate_capacity("balanced", [window])
            decisions = ae._policy_decisions(capacity, "balanced")

        forecast = capacity["windows"][0]
        self.assertEqual(forecast["burn_percent_per_hour"], 10)
        self.assertEqual(forecast["burn_basis"], "same-window snapshots over wall-clock time")
        self.assertEqual(forecast["hours_to_cap"], 2)
        self.assertEqual(forecast["blocked_hours"], 2)
        self.assertEqual(decisions["lockout_limit_ids"], ["codex"])

    def test_thin_rounded_delta_does_not_create_confident_hourly_forecast(self) -> None:
        self._write_history(self._turn(delta=1, elapsed=5, observation_seconds=30))
        window = {
            "limit_id": "codex",
            "used_percent": 50,
            "window_duration_mins": 300,
            "resets_at": self.NOW + 4 * 3_600,
        }
        with mock.patch.object(ae.time, "time", return_value=self.NOW):
            capacity = ae.estimate_capacity("balanced", [window])

        forecast = capacity["windows"][0]
        self.assertIsNone(forecast["burn_percent_per_hour"])
        self.assertEqual(forecast["burn_sample_count"], 0)
        self.assertEqual(forecast["forecast_verdict"], "insufficient-burn-history")
        self.assertEqual(forecast["active_turn_burn_percent_per_hour"], 720)

    def test_usage_delta_records_snapshot_observation_span(self) -> None:
        before = [{
            "limit_id": "codex", "used_percent": 10, "resets_at": self.NOW + 10_000,
            "captured_at": self.NOW,
        }]
        after = [{
            "limit_id": "codex", "used_percent": 12, "resets_at": self.NOW + 10_000,
            "captured_at": self.NOW + 600,
        }]
        delta = ae._usage_deltas(before, after)[0]
        self.assertEqual(delta["used_percent_delta"], 2)
        self.assertEqual(delta["observation_seconds"], 600)

    def test_policy_coherence_gate_rejects_contradictory_long_window(self) -> None:
        capacity = {
            "available": True,
            "calibration_status": "heuristic",
            "estimated_remaining": {"low": 4},
            "windows": [{
                "limit_id": "weekly",
                "window_duration_mins": 10_080,
                "pace_ratio": 0.8,
                "remaining_percent": 60,
                "hours_to_cap": 1,
                "blocked_hours": 10,
            }],
        }
        decisions = ae._policy_decisions(capacity, "balanced")
        self.assertEqual(decisions["lockout_limit_ids"], [])
        self.assertTrue(decisions["go_wide"])

    def test_fresh_windows_cannot_inherit_previous_cycle_burn(self) -> None:
        self._write_history(self._turn(delta=10, elapsed=60, observation_seconds=3600))
        for duration in (300, 10080):
            for used in (0, 2):
                with self.subTest(duration=duration, used=used):
                    window = {"limit_id": "codex", "used_percent": used,
                              "window_duration_mins": duration,
                              "resets_at": self.NOW + duration * 60 - 120}
                    with mock.patch.object(ae.time, "time", return_value=self.NOW):
                        capacity = ae.estimate_capacity("balanced", [window])
                    forecast = capacity["windows"][0]
                    self.assertIsNone(forecast["hours_to_cap"])
                    self.assertFalse(forecast["burn_measurable"])
                    self.assertEqual(ae._policy_decisions(capacity, "balanced")["lockout_limit_ids"], [])

    def test_short_window_rejects_old_or_unidentified_history(self) -> None:
        window = {"limit_id": "codex", "used_percent": 80,
                  "window_duration_mins": 300, "resets_at": self.NOW + 4 * 3600}
        for identity in (None, "previous-cycle"):
            with self.subTest(identity=identity):
                record = self._turn(delta=10, elapsed=60, observation_seconds=3600)
                record["usage_delta"][0]["window_instance"] = identity
                with mock.patch.object(ae, "_recent_history", return_value=[record]), mock.patch.object(
                    ae.time, "time", return_value=self.NOW
                ):
                    forecast = ae.estimate_capacity("balanced", [window])["windows"][0]
                self.assertIsNone(forecast["burn_percent_per_hour"])
                self.assertIsNone(forecast["hours_to_cap"])

    def test_zero_usage_mature_week_does_not_fall_back_to_history(self) -> None:
        self._write_history(self._turn(delta=10, elapsed=60, observation_seconds=3600))
        with mock.patch.object(ae.time, "time", return_value=self.NOW):
            forecast = ae.estimate_capacity("balanced", [self._weekly_window(0)])["windows"][0]
        self.assertEqual(forecast["burn_percent_per_hour"], 0)
        self.assertEqual(forecast["forecast_verdict"], "no-lockout")
        self.assertIsNone(forecast["hours_to_cap"])

    def test_weekly_minimum_span_boundary(self) -> None:
        for span in (599, 600):
            with self.subTest(span=span), mock.patch.object(ae.time, "time", return_value=self.NOW):
                window = self._weekly_window(2, span / (10080 * 60))
                capacity = ae.estimate_capacity("balanced", [window])
                forecast = capacity["windows"][0]
                self.assertEqual(forecast["burn_measurable"], span == 600)
                self.assertEqual(bool(ae._policy_decisions(capacity, "balanced")["lockout_limit_ids"]), span == 600)

    def test_unmeasurable_precomputed_forecast_cannot_promote(self) -> None:
        capacity = {"available": True, "windows": [{"limit_id": "codex",
                    "burn_measurable": False, "hours_to_cap": 1, "blocked_hours": 100}]}
        self.assertEqual(ae._policy_decisions(capacity, "balanced")["lockout_limit_ids"], [])

    def test_missing_reset_cannot_reuse_history(self) -> None:
        self._write_history(self._turn(delta=10, elapsed=60, observation_seconds=3600))
        forecast = ae.estimate_capacity("balanced", [{
            "limit_id": "codex", "used_percent": 50, "window_duration_mins": 300,
        }])["windows"][0]
        self.assertIsNone(forecast["burn_percent_per_hour"])

    def test_forecast_acknowledgement_survives_and_resets_with_window(self) -> None:
        with mock.patch.object(ae.time, "time", return_value=self.NOW):
            capacity = ae.estimate_capacity("balanced", [self._weekly_window(70)])
        first = ae._policy_decisions(capacity, "balanced", session_id="reset-test", consume=True)
        repeat = ae._policy_decisions(capacity, "balanced", session_id="reset-test", consume=True)
        capacity["windows"][0]["resets_at"] += 10080 * 60
        next_cycle = ae._policy_decisions(capacity, "balanced", session_id="reset-test", consume=True)
        self.assertEqual(first["lockout_limit_ids"], ["codex"])
        self.assertEqual(repeat["lockout_limit_ids"], [])
        self.assertEqual(next_cycle["lockout_limit_ids"], ["codex"])


if __name__ == "__main__":
    unittest.main()
