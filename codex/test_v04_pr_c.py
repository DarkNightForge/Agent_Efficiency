from __future__ import annotations

import io
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
import mcp_server


class V04ForecastDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        os.environ["AGENT_EFFICIENCY_DATA"] = self.temp.name
        os.environ["AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH"] = "1"

    def tearDown(self) -> None:
        os.environ.pop("AGENT_EFFICIENCY_DATA", None)
        os.environ.pop("AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH", None)
        self.temp.cleanup()

    def write_history(self, records: list[dict]) -> None:
        path = Path(self.temp.name) / "history.jsonl"
        path.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")

    @staticmethod
    def audited_record(
        *,
        time_error: float = 0.2,
        token_error: float = 0.4,
        time_covered: bool = True,
        token_covered: bool = False,
    ) -> dict:
        return {
            "mode": "balanced",
            "complexity": "simple",
            "status": "completed",
            "model": "m",
            "elapsed_seconds": 60,
            "tokens": {"total": 10_000},
            "forecast": {"available": True},
            "forecast_outcome": {
                "time": {
                    "available": True,
                    "within_range": time_covered,
                    "relative_error": time_error,
                    "absolute_relative_error": abs(time_error),
                },
                "tokens": {
                    "available": True,
                    "within_range": token_covered,
                    "relative_error": -token_error,
                    "absolute_relative_error": abs(token_error),
                },
                "capacity": [{
                    "available": True,
                    "relative_error": 0.25,
                    "absolute_relative_error": 0.25,
                }],
            },
        }

    def test_diagnostics_aggregate_coverage_failures_and_next_action(self) -> None:
        second = self.audited_record(time_error=-0.1, token_error=0.2, time_covered=False)
        second["forecast_outcome"]["tokens"] = {
            "available": False,
            "reason": "No measured tokens were recorded.",
        }
        second["forecast_outcome"]["capacity"] = [{
            "available": False,
            "reason": "No same-window delta was measurable.",
        }]
        self.write_history([
            self.audited_record(),
            second,
            {"mode": "stretch", "status": "completed", "complexity": "unspecified"},
            {
                "mode": "balanced", "status": "completed",
                "forecast": {"available": False, "status": "forecast-generation-failed"},
            },
            {
                "mode": "balanced", "status": "completed", "forecast": {"available": True},
                "forecast_outcome": {"status": "outcome-generation-failed"},
            },
            {"mode": "balanced", "status": "failed", "forecast": {"available": True}},
        ])

        report = ae.forecast_diagnostics(100)

        self.assertEqual(report["records_considered"], 6)
        self.assertEqual(report["completed_records"], 5)
        self.assertEqual(report["available_forecast_records"], 3)
        self.assertEqual(report["legacy_records"], 1)
        self.assertEqual(report["forecast_generation_failures"], 1)
        self.assertEqual(report["outcome_generation_failures"], 1)
        self.assertEqual(report["dimensions"]["time"]["audited"], 2)
        self.assertEqual(report["dimensions"]["time"]["covered"], 1)
        self.assertEqual(report["dimensions"]["time"]["coverage_rate"], 0.5)
        self.assertEqual(report["dimensions"]["time"]["median_absolute_relative_error"], 0.15)
        self.assertEqual(report["dimensions"]["time"]["p80_absolute_relative_error"], 0.2)
        self.assertEqual(report["dimensions"]["tokens"]["audited"], 1)
        self.assertEqual(report["dimensions"]["tokens"]["unavailable"], 1)
        self.assertEqual(report["dimensions"]["capacity"]["audited"], 1)
        self.assertEqual(report["calibration"]["jointly_audited"], 1)
        self.assertEqual(report["status"], "attention")
        self.assertIn("outcome-generation", report["next_action"])
        self.assertEqual(report["by_mode"]["balanced"]["completed"], 4)
        self.assertEqual(report["by_mode"]["stretch"]["completed"], 1)

    def test_diagnostics_ignore_corrupt_metrics_and_return_no_identifiers(self) -> None:
        record = self.audited_record()
        record.update({
            "task_id": "SECRET-TASK",
            "session_ref": "SECRET-SESSION",
            "turn_ref": "SECRET-TURN",
        })
        record["forecast_outcome"]["time"]["absolute_relative_error"] = float("nan")
        record["forecast_outcome"]["tokens"]["relative_error"] = True
        self.write_history([record])

        report = ae.forecast_diagnostics()
        serialized = json.dumps(report)

        self.assertEqual(report["dimensions"]["time"]["audited"], 0)
        self.assertEqual(report["dimensions"]["tokens"]["audited"], 0)
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("task_id", serialized)
        self.assertNotIn("session_ref", serialized)
        self.assertNotIn("turn_ref", serialized)

    def test_diagnostics_empty_state_and_cli_json(self) -> None:
        report = ae.forecast_diagnostics()
        self.assertEqual(report["status"], "no-data")
        self.assertEqual(report["calibration"]["jointly_audited"], 0)
        self.assertIn("3 more", report["next_action"])

        with mock.patch("sys.stdout", new=io.StringIO()) as output:
            self.assertEqual(ae.main(["--json", "forecast-diagnostics", "--limit", "7"]), 0)
        self.assertEqual(json.loads(output.getvalue())["records_considered"], 0)

    def test_readiness_uses_one_comparable_cohort_not_global_total(self) -> None:
        records = [self.audited_record(time_error=0.1, token_error=0.1) for _ in range(3)]
        for record in [self.audited_record(time_error=0.1, token_error=0.1) for _ in range(2)]:
            record["mode"] = "stretch"
            records.append(record)
        self.write_history(records)

        report = ae.forecast_diagnostics()

        self.assertEqual(report["calibration"]["jointly_audited"], 5)
        self.assertEqual(report["calibration"]["largest_comparable_cohort"], 3)
        self.assertEqual(report["status"], "interval-ready")
        self.assertIn("2 more", report["next_action"])

    def test_mcp_exposes_aggregate_diagnostics(self) -> None:
        listing = mcp_server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        })
        names = {tool["name"] for tool in listing["result"]["tools"]}
        self.assertIn("get_forecast_diagnostics", names)

        response = mcp_server.handle({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_forecast_diagnostics", "arguments": {"limit": 10}},
        })
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["result"]["status"], "no-data")

    def test_control_center_presents_confidence_provenance(self) -> None:
        center = ae.show_control_center("Review PR C", selected_mode="balanced")
        estimate = center["modes"][0]
        self.assertEqual(estimate["confidence_basis"], "first-use heuristic; no comparable history")
        rendered = ae.render_control_center(center)
        self.assertIn("Estimate confidence: low (first-use heuristic; no comparable history)", rendered)

        html = (ROOT / "plugins" / "agent-efficiency" / "assets" / "control_center.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('metric("Estimate confidence",estimate.confidence', html)
        self.assertIn('metric("Calibration",estimate.confidence_basis', html)
        self.assertIn('metric("Capacity confidence",cap.confidence)', html)

    def test_picker_policy_is_consumed_and_reported(self) -> None:
        default = ae.show_control_center("x", picker_requested=False)
        requested = ae.show_control_center("x", picker_requested=True)
        self.assertEqual(default["show_picker"], "when_requested")
        self.assertFalse(default["render_picker"])
        self.assertTrue(requested["render_picker"])

        ae.update_settings(show_picker="always")
        self.assertTrue(ae.show_control_center("x")["render_picker"])
        ae.update_settings(show_picker="never")
        self.assertFalse(ae.show_control_center("x", picker_requested=True)["render_picker"])

        with self.assertRaisesRegex(ValueError, "picker_requested"):
            ae.show_control_center("x", picker_requested="yes")

    def test_mcp_picker_round_trip_renders_requested_control_center(self) -> None:
        shown = mcp_server.handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "show_control_center",
                "arguments": {"task": "Review PR C", "picker_requested": True},
            },
        })
        self.assertFalse(shown["result"]["isError"])
        center = shown["result"]["structuredContent"]["result"]
        self.assertTrue(center["render_picker"])

        rendered = mcp_server.handle({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "render_control_center",
                "arguments": {"control_center": center},
            },
        })
        self.assertFalse(rendered["result"]["isError"])
        self.assertEqual(rendered["result"]["structuredContent"], center)

    def test_paired_residuals_are_named_as_confidence_basis(self) -> None:
        self.write_history([self.audited_record(time_error=0.1, token_error=0.1) for _ in range(5)])
        estimate = ae.estimate_mode(
            "balanced", complexity="simple", model="m", include_capacity=False
        )
        self.assertEqual(estimate["confidence"], "medium")
        self.assertEqual(estimate["confidence_basis"], "5 paired audited forecasts")


if __name__ == "__main__":
    unittest.main()
