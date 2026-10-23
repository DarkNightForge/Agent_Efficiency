#!/usr/bin/env python3
"""Tests for the Hermes-native Agent Efficiency policy plugin."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "agent-efficiency"
MODULE_PATH = PLUGIN_DIR / "agent_efficiency.py"
_spec = importlib.util.spec_from_file_location("hermes_agent_efficiency", MODULE_PATH)
assert _spec and _spec.loader
_agent_efficiency = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_agent_efficiency)

SnapshotStore = _agent_efficiency.SnapshotStore
UsageBudget = _agent_efficiency.UsageBudget
STATIC_POLICY = _agent_efficiency.STATIC_POLICY


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _snapshot(now: float, windows: list[dict], provider: str = "anthropic") -> dict:
    return {
        "provider": provider,
        "source": "usage_api",
        "fetched_at": now,
        "plan": "Test",
        "windows": windows,
        "details": [],
    }


class PolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SnapshotStore(Path(self.temp.name) / "usage.db")
        self.addCleanup(self.store.close)
        self.now = 2_000_000_000.0
        self.budget = UsageBudget(self.store, now=lambda: self.now)

    def test_exact_provider_windows_are_rendered_without_estimate_markers(self) -> None:
        snap = _snapshot(
            self.now,
            [
                {
                    "label": "Current session",
                    "used_percent": 22.0,
                    "reset_at": _iso(self.now + 2 * 3600),
                },
                {
                    "label": "Current week",
                    "used_percent": 31.0,
                    "reset_at": _iso(self.now + 4 * 86400),
                },
            ],
        )
        self.store.record_snapshot(snap)

        block = self.budget.context_for("anthropic")

        self.assertIn("CURRENT BLOCK", block.splitlines()[1])
        self.assertIn("supersedes all earlier", block.splitlines()[1])
        self.assertIn("Current session: 22% used (EXACT", block)
        self.assertIn("Current week: 31% used (EXACT", block)
        self.assertNotIn("UNCALIBRATED", block)
        self.assertNotIn("~22%", block)

    def test_go_wide_requires_both_session_room_and_week_under_pace(self) -> None:
        weekly_reset = self.now + 6 * 86400
        roomy = _snapshot(
            self.now,
            [
                {"label": "Session", "used_percent": 10.0, "reset_at": _iso(self.now + 4 * 3600)},
                {"label": "Weekly", "used_percent": 5.0, "reset_at": _iso(weekly_reset)},
            ],
            provider="openai-codex",
        )
        self.store.record_snapshot(roomy)
        self.assertIn("GO WIDE", self.budget.context_for("openai-codex"))

        no_week = _snapshot(
            self.now,
            [{"label": "Session", "used_percent": 10.0, "reset_at": _iso(self.now + 4 * 3600)}],
            provider="anthropic",
        )
        self.store.record_snapshot(no_week)
        self.assertNotIn("GO WIDE", self.budget.context_for("anthropic"))

    def test_weekly_checkpoint_fires_once_per_day_then_reasks_after_material_worsening(self) -> None:
        # Day 3 allows 3/7 ~= 42.9%; 55% used is over pace and projects dry before day 7.
        reset = self.now + 5 * 86400
        first = _snapshot(
            self.now,
            [
                {"label": "Session", "used_percent": 30.0, "reset_at": _iso(self.now + 3 * 3600)},
                {"label": "Weekly", "used_percent": 55.0, "reset_at": _iso(reset)},
            ],
            provider="openai-codex",
        )
        self.store.record_snapshot(first)
        self.assertIn("ASK BEFORE PROCEEDING", self.budget.context_for("openai-codex"))
        self.assertNotIn("ASK BEFORE PROCEEDING", self.budget.context_for("openai-codex"))

        # A >=25-point worsening in pace ratio is material enough to ask again.
        worse = _snapshot(
            self.now + 600,
            [
                {"label": "Session", "used_percent": 35.0, "reset_at": _iso(self.now + 3 * 3600)},
                {"label": "Weekly", "used_percent": 70.0, "reset_at": _iso(reset)},
            ],
            provider="openai-codex",
        )
        self.store.record_snapshot(worse)
        self.now += 600
        self.assertIn("ASK BEFORE PROCEEDING", self.budget.context_for("openai-codex"))

    def test_meaningful_lockout_warning_is_promoted_to_first_line(self) -> None:
        reset = self.now + 10 * 3600
        self.store.record_snapshot(
            _snapshot(
                self.now - 3600,
                [{"label": "Weekly", "used_percent": 40.0, "reset_at": _iso(reset)}],
            )
        )
        self.store.record_snapshot(
            _snapshot(
                self.now,
                [{"label": "Weekly", "used_percent": 60.0, "reset_at": _iso(reset)}],
            )
        )

        lines = self.budget.context_for("anthropic").splitlines()

        self.assertEqual(lines[0], "<usage-budget>")
        self.assertTrue(lines[1].startswith("WOULD RUN OUT"), lines)
        self.assertIn("BLOCKED", lines[1])
        self.assertIn("NOT a refusal", lines[1])

    def test_cap_close_to_reset_is_explicitly_not_a_reason_to_cut_scope(self) -> None:
        reset = self.now + 1.5 * 3600
        self.store.record_snapshot(
            _snapshot(
                self.now - 3600,
                [{"label": "Session", "used_percent": 80.0, "reset_at": _iso(reset)}],
            )
        )
        self.store.record_snapshot(
            _snapshot(
                self.now,
                [{"label": "Session", "used_percent": 90.0, "reset_at": _iso(reset)}],
            )
        )

        block = self.budget.context_for("anthropic")

        self.assertNotIn("WOULD RUN OUT", block)
        self.assertIn("NOT a reason to cut scope", block)

    def test_stale_snapshot_injects_a_tombstone_that_retracts_earlier_directives(self) -> None:
        self.store.record_snapshot(
            _snapshot(
                self.now - 901,
                [{"label": "Session", "used_percent": 10.0, "reset_at": _iso(self.now + 3600)}],
            )
        )
        block = self.budget.context_for("anthropic")
        self.assertIn("STALE", block)
        self.assertIn("IGNORE ALL EARLIER", block)
        self.assertNotIn("10% used (EXACT", block)

    def test_missing_snapshot_also_retracts_replayed_history(self) -> None:
        block = self.budget.context_for("anthropic")
        self.assertIn("UNAVAILABLE", block)
        self.assertIn("IGNORE ALL EARLIER", block)

    def test_fresh_directive_is_explicitly_retracted_after_cache_goes_stale(self) -> None:
        self.store.record_snapshot(
            _snapshot(
                self.now,
                [
                    {"label": "Session", "used_percent": 10.0, "reset_at": _iso(self.now + 4 * 3600)},
                    {"label": "Weekly", "used_percent": 5.0, "reset_at": _iso(self.now + 6 * 86400)},
                ],
            )
        )
        historical = self.budget.context_for("anthropic")
        self.assertIn("GO WIDE", historical)

        self.now += 901
        current = self.budget.context_for("anthropic")
        self.assertIn("STALE", current)
        self.assertIn("IGNORE ALL EARLIER", current)
        self.assertNotIn("GO WIDE", current)

    def test_provider_routes_are_isolated(self) -> None:
        self.store.record_snapshot(
            _snapshot(
                self.now,
                [{"label": "Session", "used_percent": 11.0, "reset_at": _iso(self.now + 3600)}],
                provider="openai-codex",
            )
        )
        self.store.record_snapshot(
            _snapshot(
                self.now,
                [{"label": "Current session", "used_percent": 77.0, "reset_at": _iso(self.now + 3600)}],
                provider="anthropic",
            )
        )
        self.assertIn("11% used", self.budget.context_for("openai-codex"))
        self.assertNotIn("77% used", self.budget.context_for("openai-codex"))

    def test_static_policy_preserves_silence_and_user_control(self) -> None:
        self.assertIn("Default to silence", STATIC_POLICY)
        self.assertIn("Never silently reduce scope", STATIC_POLICY)
        self.assertIn("GO WIDE", STATIC_POLICY)
        self.assertIn("ASK BEFORE PROCEEDING", STATIC_POLICY)

    def test_refresh_lease_is_shared_across_store_connections(self) -> None:
        other = SnapshotStore(Path(self.temp.name) / "usage.db")
        self.addCleanup(other.close)
        self.assertTrue(self.store.claim_refresh("anthropic", now=self.now, interval=300))
        self.assertFalse(other.claim_refresh("anthropic", now=self.now, interval=300))
        self.assertTrue(other.claim_refresh("anthropic", now=self.now + 301, interval=300))


if __name__ == "__main__":
    unittest.main()
