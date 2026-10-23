"""PR #11 triage block A: hardening this tree dropped.

Ported verbatim from Robbie's `robbie/codex-backlog` (rebased as `robbie-rebase`).
Every one of these failed against the App Server tree in the SAME direction --
a guess where there should be an unknown, a stale reading presented as fresh, or
validation that stopped firing. That is FINDINGS 11's defect recurring in the
Codex half, which is why they are locked in here rather than rewritten to match
the implementation. See docs/TRIAGE-PR11-2026-08-18.md.
"""
from __future__ import annotations

import importlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "agent-efficiency" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_efficiency as ae
import codex_telemetry as ct
import mcp_server


class HardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        os.environ["AGENT_EFFICIENCY_DATA"] = self.temp.name
        os.environ["AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH"] = "1"

    def tearDown(self) -> None:
        os.environ.pop("AGENT_EFFICIENCY_DATA", None)
        os.environ.pop("AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH", None)
        self.temp.cleanup()

    def test_stale_usage_snapshot_is_uncalibrated(self) -> None:
        ae.ingest_usage(rate_limits=[{"limit_id": "five-hour", "used_percent": 40}])
        usage_path = Path(self.temp.name) / "usage.json"
        usage = json.loads(usage_path.read_text(encoding="utf-8"))
        usage["rate_limits_updated_at"] = "2000-01-01T00:00:00+00:00"
        usage_path.write_text(json.dumps(usage), encoding="utf-8")
        capacity = ae.estimate_capacity("efficiency")
        self.assertFalse(capacity["available"])
        self.assertEqual(capacity["status"], "UNCALIBRATED")
        self.assertIn("refresh it", capacity["reason"])

    def test_token_ingest_does_not_refresh_rate_limit_freshness(self) -> None:
        ae.ingest_usage(rate_limits=[{"limit_id": "five-hour", "used_percent": 40}])
        usage_path = Path(self.temp.name) / "usage.json"
        usage = json.loads(usage_path.read_text(encoding="utf-8"))
        usage["rate_limits_updated_at"] = "2000-01-01T00:00:00+00:00"
        usage_path.write_text(json.dumps(usage), encoding="utf-8")
        ae.ingest_usage(tokens={"input": 3, "output": 2})
        self.assertFalse(ae.estimate_capacity("efficiency")["available"])

    def test_sparse_update_does_not_relabel_omitted_stale_window_as_fresh(self) -> None:
        ae.ingest_usage(rate_limits={"limitId": "codex", "primary": {"usedPercent": 10}, "secondary": {"usedPercent": 90}})
        path = Path(self.temp.name) / "usage.json"
        usage = json.loads(path.read_text(encoding="utf-8"))
        for window in usage["rate_limits"]:
            if window["window_role"] == "secondary":
                window["captured_at"] = "2000-01-01T00:00:00Z"
        path.write_text(json.dumps(usage), encoding="utf-8")
        ae.ingest_usage(rate_limits={"limitId": "codex", "primary": {"usedPercent": 15}})
        self.assertFalse(ae.estimate_capacity("efficiency")["available"])

    def test_nonpositive_wall_clock_elapsed_is_unknown(self) -> None:
        ae.start_task(task_id="clock")
        active_path = Path(self.temp.name) / "active.json"
        active = json.loads(active_path.read_text(encoding="utf-8"))
        active["clock"]["started_at"] = "2999-01-01T00:00:00Z"
        active_path.write_text(json.dumps(active), encoding="utf-8")
        done = ae.complete_task("clock", tokens={"input": 10})
        self.assertIsNone(done["elapsed_seconds"])
        self.assertIsNone(done["efficiency_score"])

    def test_negative_and_fractional_tokens_are_rejected(self) -> None:
        ae.start_task(task_id="s:t", session_id="s", turn_id="t")
        for tokens in ({"input": -1}, {"output": 1.5}, {"total_tokens": -2}, {"input": True}):
            with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                ae.ingest_usage(tokens=tokens)
        with self.assertRaises(ValueError):
            ae.complete_task("s:t", tokens={"total": -100})

    def test_total_tokens_alias_is_supported_and_validated(self) -> None:
        usage = ae.ingest_usage(tokens={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})
        self.assertEqual(usage["latest_tokens"]["total"], 5)
        with self.assertRaisesRegex(ValueError, "total tokens must equal input plus output"):
            ae.ingest_usage(tokens={"input_tokens": 100, "output_tokens": 50, "total_tokens": 1})

    def test_explicit_snapshot_is_normalized_and_stale_timestamp_rejected(self) -> None:
        fresh = ae.estimate_capacity("efficiency", {"rateLimits": {
            "limitId": "codex", "primary": {"usedPercent": 25}
        }})
        self.assertTrue(fresh["available"])
        stale = ae.estimate_capacity("efficiency", {
            "captured_at": "2000-01-01T00:00:00Z",
            "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 25}},
        })
        self.assertFalse(stale["available"])

    def test_timer_history_and_score(self) -> None:
        started = ae.start_task(task_id="task-1", mode="efficiency", complexity="simple")
        self.assertEqual(started["task_id"], "task-1")
        time.sleep(0.01)
        done = ae.complete_task("task-1", validation_passed=True, tokens={"input": 100, "output": 50})
        self.assertGreater(done["elapsed_seconds"], 0)
        self.assertEqual(done["tokens"]["total"], 150)
        self.assertEqual(done["efficiency_score"]["basis"], "measured elapsed time and measured rollout or caller token totals")
        self.assertIn("time_ratio", done["efficiency_score"])
        self.assertIn("token_ratio", done["efficiency_score"])
        self.assertEqual(ae.list_history(1)[0]["task_id"], "task-1")

    def test_map_key_is_preserved_and_sparse_update_merges(self) -> None:
        initial = ae.ingest_usage(rate_limits={"rateLimitsByLimitId": {"stable-key": {
            "primary": {"usedPercent": 10}, "secondary": {"usedPercent": 20}
        }}})
        self.assertEqual({w["limit_id"] for w in initial["rate_limits"]}, {"stable-key:primary", "stable-key:secondary"})
        updated = ae.ingest_usage(rate_limits={"rateLimits": {
            "limitId": "stable-key", "primary": {"usedPercent": 15}
        }})
        by_id = {w["limit_id"]: w for w in updated["rate_limits"]}
        self.assertEqual(by_id["stable-key:primary"]["used_percent"], 15)
        self.assertEqual(by_id["stable-key:secondary"]["used_percent"], 20)

    def test_cli_completion_preserves_measured_tokens_and_merges_partial_input(self) -> None:
        ae.start_task(task_id="measured")
        ae.ingest_usage(task_id="measured", tokens={"input": 100, "cached_input": 40, "output": 20, "reasoning": 10})
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(ae.main(["--json", "complete", "measured"]), 0)
        self.assertEqual(ae.list_history(1)[0]["tokens"]["total"], 120)

        ae.start_task(task_id="partial")
        ae.ingest_usage(task_id="partial", tokens={"input": 100, "cached_input": 40, "output": 20, "reasoning": 10})
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(ae.main(["--json", "complete", "partial", "--input-tokens", "120"]), 0)
        tokens = ae.list_history(1)[0]["tokens"]
        # Adapted from the original exact-dict assertion: this tree's token
        # schema carries an extra `model_calls` field (it is what distinguishes
        # a measured rollout total from a caller-supplied one), so comparing the
        # whole dict would fail on a field that is a superset, not a defect.
        # Every value the original asserted is still asserted here.
        expected = {"input": 120, "cached_input": 40, "cache_write_input": 0,
                    "reasoning": 10, "output": 20, "total": 140}
        self.assertEqual({k: tokens[k] for k in expected}, expected)
        self.assertIn("model_calls", tokens)

    def test_find_task_requires_session_and_uses_newest_match(self) -> None:
        active = {
            "old": {"session_id": "s", "turn_id": "a", "started_at": "2026-01-01T00:00:00Z"},
            "new": {"session_id": "s", "turn_id": "b", "started_at": "2026-01-02T00:00:00Z"},
            "none": {"session_id": None, "started_at": "2027-01-01T00:00:00Z"},
        }
        self.assertIsNone(ae._find_task(active, None, None))
        self.assertEqual(ae._find_task(active, "s", None), "new")

    def test_numeric_limit_ids_match_observed_burn(self) -> None:
        history = {"mode": "efficiency", "status": "completed", "elapsed_seconds": 3600,
                   "usage_delta": [{"limit_id": 5, "same_window": True,
                                    "used_percent_delta": 10}]}
        (Path(self.temp.name) / "history.jsonl").write_text(json.dumps(history) + "\n", encoding="utf-8")
        burn, samples = ae._observed_burn("efficiency", "5")
        self.assertEqual((burn, samples), (10, 1))

    def test_model_specific_history_changes_estimates_and_unknown_model_is_labeled_fallback(self) -> None:
        records = []
        for model, elapsed, total in (("model-fast", 60, 4_000), ("model-slow", 600, 40_000)):
            for _ in range(3):
                records.append({
                    "mode": "efficiency", "complexity": "simple", "status": "completed", "model": model,
                    "elapsed_seconds": elapsed, "tokens": {"total": total},
                })
        (Path(self.temp.name) / "history.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8",
        )
        fast = ae.estimate_mode("efficiency", complexity="simple", model="model-fast", include_capacity=False)
        slow = ae.estimate_mode("efficiency", complexity="simple", model="model-slow", include_capacity=False)
        unknown = ae.estimate_mode("efficiency", complexity="simple", model="unknown", include_capacity=False)
        self.assertLess(fast["eta_minutes"]["low"], slow["eta_minutes"]["low"])
        self.assertLess(fast["estimated_tokens"]["low"], slow["estimated_tokens"]["low"])
        self.assertEqual(fast["calibration"]["model_scope"], "model-specific")
        self.assertEqual(unknown["calibration"]["model_scope"], "cross-model-fallback")
        self.assertEqual(unknown["confidence"], "low")


if __name__ == "__main__":
    unittest.main()
