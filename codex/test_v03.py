from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "agent-efficiency" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "codex" / "evals"))

import agent_efficiency as ae
from budget_policy import render_usage_budget
from run_evals import run as run_behavioral_evals


class V03Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        os.environ["AGENT_EFFICIENCY_DATA"] = self.temp.name
        os.environ["AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH"] = "1"

    def tearDown(self) -> None:
        os.environ.pop("AGENT_EFFICIENCY_DATA", None)
        os.environ.pop("AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH", None)
        self.temp.cleanup()

    def test_plugin_registers_bundled_mcp_control_center(self) -> None:
        plugin = ROOT / "plugins" / "agent-efficiency"
        manifest = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        config = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))
        server = config["mcpServers"]["agent-efficiency"]
        self.assertEqual(server["command"], "git")
        self.assertEqual(server["cwd"], ".")
        self.assertEqual(server["args"], ["--exec-path=./scripts", "agent-efficiency"])
        self.assertNotIn("PLUGIN_ROOT", " ".join(server["args"]))

    def test_mcp_manifest_command_completes_handshake_cross_platform(self) -> None:
        plugin = ROOT / "plugins" / "agent-efficiency"
        config = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))
        server = config["mcpServers"]["agent-efficiency"]
        env = os.environ.copy()
        env["AGENT_EFFICIENCY_DATA"] = self.temp.name
        env["AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH"] = "1"
        request = "\n".join((
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            "",
        ))
        completed = subprocess.run(
            [server["command"], *server["args"]],
            input=request,
            text=True,
            capture_output=True,
            timeout=20,
            env=env,
            cwd=plugin / server["cwd"],
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual(responses[0]["id"], 1)
        tools = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertEqual(len(tools), 13)
        self.assertIn("show_control_center", tools)
        self.assertIn("get_forecast_diagnostics", tools)

    def test_legacy_mode_names_migrate_without_being_displayed(self) -> None:
        settings = Path(self.temp.name) / "settings.json"
        settings.write_text(json.dumps({"default_mode": "ultra-efficiency"}), encoding="utf-8")
        self.assertEqual(ae.get_settings()["default_mode"], "stretch")
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["default_mode"], "stretch")
        self.assertEqual(ae.normalize_mode("big-baller"), "throughput")
        args = ae.build_parser().parse_args(["control-center", "x", "--mode", "efficiency"])
        self.assertEqual(args.mode, "balanced")
        self.assertNotIn("big-baller", ae.build_parser().format_help())

    def test_posture_semantics_match_proven_thresholds(self) -> None:
        capacity = {
            "available": True,
            "calibration_status": "heuristic",
            "estimated_remaining": {"low": 4, "high": 7},
            "windows": [{"limit_id": "week", "remaining_percent": 70, "pace_ratio": 0.82}],
        }
        self.assertTrue(ae._policy_decisions(capacity, "throughput")["go_wide"])
        self.assertTrue(ae._policy_decisions(capacity, "balanced")["go_wide"])
        self.assertFalse(ae._policy_decisions(capacity, "stretch")["go_wide"])
        self.assertEqual(ae._policy_decisions(capacity, "throughput")["pace_limit_ids"], [])
        self.assertEqual(ae._policy_decisions(capacity, "stretch")["pace_limit_ids"], ["week"])

    def test_lockout_promotion_uses_duration_and_horizon(self) -> None:
        def decisions(blocked: float, horizon: float) -> list[str]:
            capacity = {
                "available": True,
                "estimated_remaining": {"low": 2, "high": 3},
                "windows": [{
                    "limit_id": "week", "blocked_hours": blocked, "hours_to_cap": horizon,
                    "remaining_percent": 20,
                }],
            }
            return ae._policy_decisions(capacity, "balanced")["lockout_limit_ids"]

        self.assertEqual(decisions(1.99, 1), [])
        self.assertEqual(decisions(3, 24), ["week"])
        self.assertEqual(decisions(3, 25), [])
        self.assertEqual(decisions(6, 25), ["week"])

    def test_acknowledgement_reasks_only_when_25_percent_worse(self) -> None:
        def capacity(blocked: float) -> dict:
            return {
                "available": True,
                "estimated_remaining": {"low": 2, "high": 3},
                "windows": [{
                    "limit_id": "week", "resets_at": 99, "blocked_hours": blocked,
                    "hours_to_cap": 3, "remaining_percent": 20,
                }],
            }

        first = ae._policy_decisions(capacity(4), "balanced", session_id="s", consume=True)
        same = ae._policy_decisions(capacity(4), "balanced", session_id="s", consume=True)
        slightly_worse = ae._policy_decisions(capacity(4.9), "balanced", session_id="s", consume=True)
        materially_worse = ae._policy_decisions(capacity(5), "balanced", session_id="s", consume=True)
        self.assertEqual(first["lockout_limit_ids"], ["week"])
        self.assertEqual(same["lockout_limit_ids"], [])
        self.assertEqual(slightly_worse["lockout_limit_ids"], [])
        self.assertEqual(materially_worse["lockout_limit_ids"], ["week"])

    def test_estimator_uses_bounded_empirical_spread(self) -> None:
        stable = [
            {"mode": "balanced", "complexity": "simple", "status": "completed", "model": "m",
             "elapsed_seconds": 60, "tokens": {"total": 10_000},
             "forecast_outcome": {
                 "time": {"available": True, "absolute_relative_error": 0.1},
                 "tokens": {"available": True, "absolute_relative_error": 0.1},
             }}
            for _ in range(10)
        ]
        history = Path(self.temp.name) / "history.jsonl"
        history.write_text("\n".join(json.dumps(item) for item in stable) + "\n", encoding="utf-8")
        estimate = ae.estimate_mode("balanced", complexity="simple", model="m", include_capacity=False)
        self.assertEqual(estimate["calibration"]["empirical_spread"], 0.15)
        self.assertEqual(estimate["confidence"], "high")

        noisy = []
        for value in range(1, 11):
            noisy.append({
                "mode": "balanced", "complexity": "simple", "status": "completed", "model": "m",
                "elapsed_seconds": value * 60, "tokens": {"total": value * 10_000},
                "forecast_outcome": {
                    "time": {"available": True, "absolute_relative_error": value / 10},
                    "tokens": {"available": True, "absolute_relative_error": value / 10},
                },
            })
        history.write_text("\n".join(json.dumps(item) for item in noisy) + "\n", encoding="utf-8")
        estimate = ae.estimate_mode("balanced", complexity="simple", model="m", include_capacity=False)
        self.assertEqual(estimate["calibration"]["empirical_spread"], 0.75)
        self.assertEqual(estimate["confidence"], "low")

        legacy = [
            {"mode": "balanced", "complexity": "simple", "status": "completed", "model": "m",
             "elapsed_seconds": 60, "tokens": {"total": 10_000}}
            for _ in range(10)
        ]
        history.write_text("\n".join(json.dumps(item) for item in legacy) + "\n", encoding="utf-8")
        estimate = ae.estimate_mode("balanced", complexity="simple", model="m", include_capacity=False)
        self.assertEqual(estimate["calibration"]["interval_basis"], "legacy-variability-fallback")
        self.assertEqual(estimate["confidence"], "low")

    def test_policy_render_respects_ack_gate(self) -> None:
        capacity = {
            "available": True, "estimated_remaining": {"low": 2, "high": 4},
            "windows": [{"limit_id": "week", "blocked_hours": 4, "hours_to_cap": 2,
                         "used_percent": 80, "remaining_percent": 20}],
        }
        quiet = render_usage_budget(
            capacity, mode_label="Balanced", mode_behavior="Focused",
            decisions={"lockout_limit_ids": [], "pace_limit_ids": [], "go_wide": False, "fit_check": False},
        )
        loud = render_usage_budget(
            capacity, mode_label="Balanced", mode_behavior="Focused",
            decisions={"lockout_limit_ids": ["week"], "pace_limit_ids": [], "go_wide": False, "fit_check": False},
        )
        self.assertNotIn("WOULD RUN OUT", quiet)
        self.assertIn("Small, low-cost work", loud)

    def test_telemetry_opt_out_does_not_persist_policy_severity(self) -> None:
        ae.update_settings(telemetry_enabled=False)
        capacity = {
            "available": True, "estimated_remaining": {"low": 2, "high": 4},
            "windows": [{"limit_id": "week", "blocked_hours": 4,
                         "hours_to_cap": 2, "used_percent": 80, "remaining_percent": 20}],
        }
        with mock.patch.object(ae, "estimate_capacity", return_value=capacity):
            result = ae.handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "private", "turn_id": "1"})
        self.assertIn("WOULD RUN OUT", result["hookSpecificOutput"]["additionalContext"])
        self.assertFalse((Path(self.temp.name) / "policy-ack.json").exists())

    def test_cli_exposes_rollout_refresh_and_verify(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()) as output:
            self.assertEqual(ae.main(["--json", "scan-rollouts", "--sessions-root", self.temp.name]), 0)
            self.assertEqual(json.loads(output.getvalue())["files_seen"], 0)
        with mock.patch("sys.stdout", new=io.StringIO()) as output:
            self.assertEqual(ae.main(["--json", "verify"]), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "pass")

    def test_behavioral_policy_fixtures(self) -> None:
        result = run_behavioral_evals()
        self.assertTrue(result["passed"], result["results"])
        self.assertEqual(result["scenarios"], 12)


if __name__ == "__main__":
    unittest.main()
