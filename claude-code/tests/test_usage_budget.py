#!/usr/bin/env python3
"""
Tests for usage_budget.

Every empirical claim in ../../docs/FINDINGS.md should have a test here. That is
the point of this file: the findings were expensive to discover (two of them were
shipped as bugs first and caught only by comparing against a real /usage reading),
so they get locked in as regressions rather than living only in prose.

Run:  python3 -m unittest discover -s tests -v
      python3 tests/test_usage_budget.py

Stdlib only -- the tool has zero dependencies and the tests must not add any.
"""

from __future__ import annotations

import importlib
import itertools
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

HOUR = 3600
DAY = 86400


def load_module(state_dir: Path, projects_dir: Path):
    """Import usage_budget with BASE/PROJECTS pointed at temp dirs.

    Both are resolved at module import time from the environment, so a reload is
    required after changing them.
    """
    os.environ["USAGE_BUDGET_HOME"] = str(state_dir)
    os.environ["CLAUDE_PROJECTS_DIR"] = str(projects_dir)
    import usage_budget
    return importlib.reload(usage_budget)


_MSG_SEQ = itertools.count(1)


def turn(epoch, *, model="claude-opus-5", inp=0, out=0, cread=0,
         cw5=0, cw1=0, speed="standard", sidechain=False, web=0,
         thinking=None, request_id=None, msg_id=None, tools=None):
    """One assistant transcript line, matching the real on-disk shape.

    `msg_id` defaults to a fresh unique id, mirroring one API response per
    line. Pass the SAME msg_id to several turns to reproduce what Claude Code
    actually writes for a multi-content-block reply: one record per block, each
    repeating the whole usage payload. Counting those as separate turns
    inflated the numerator ~2.4x against a real /usage reading (FINDINGS 11),
    and it went undetected because this helper used to omit the id entirely.

    `tools` attaches tool_use content blocks. Note that the duplication above
    is precisely a multi-block reply, so these two features interact: a real
    reply carrying N tool_use blocks is written as N records sharing one id.
    """
    usage = {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cread,
        "cache_creation_input_tokens": cw5 + cw1,
        "cache_creation": {
            "ephemeral_5m_input_tokens": cw5,
            "ephemeral_1h_input_tokens": cw1,
        },
        "server_tool_use": {"web_search_requests": web},
        "speed": speed,
    }
    if thinking is not None:
        usage["output_tokens_details"] = {"thinking_tokens": thinking}
    message = {"id": msg_id or f"msg_{next(_MSG_SEQ):08d}",
               "model": model, "usage": usage}
    if tools:
        message["content"] = [
            {"type": "tool_use", "id": f"tu_{epoch}_{i}", "name": name,
             "input": {}}
            for i, name in enumerate(tools)
        ]
    return json.dumps({
        "type": "assistant",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch)),
        "requestId": request_id or f"req_{epoch}",
        "isSidechain": sidechain,
        "message": message,
    })


def write_transcript(projects_dir: Path, name: str, lines, newline_at_end=True):
    d = projects_dir / "-home-user-proj"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.jsonl"
    body = "\n".join(lines)
    p.write_text(body + ("\n" if newline_at_end else ""))
    return p


class TempEnv(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.state = root / "state"
        self.projects = root / "projects"
        self.state.mkdir()
        self.projects.mkdir()
        self.ub = load_module(self.state, self.projects)

    def tearDown(self):
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# FINDINGS section 3 -- the cost formula
# ---------------------------------------------------------------------------

class TestCostModel(TempEnv):

    def test_matches_vendor_reported_cost_to_the_cent(self):
        """FINDINGS section 3: the load-bearing validation.

        /usage reported $11.90 for this exact turn mix on claude-opus-5. If this
        test fails, either a price changed or the formula regressed -- and every
        capacity figure derived from it is wrong.
        """
        cost = self.ub._cost_of(
            {"input_tokens": 658, "output_tokens": 43600,
             "cache_read_input_tokens": 10_800_000,
             "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                "ephemeral_1h_input_tokens": 539_500}},
            "claude-opus-5")
        self.assertAlmostEqual(cost, 11.888, places=2)
        self.assertLess(abs(cost - 11.90), 0.02,
                        "must stay within a cent or two of the vendor's own figure")

    def test_cache_write_ttl_split_is_not_collapsible(self):
        """FINDINGS section 2: 1h writes cost 2.0x input, 5m writes 1.25x.

        Collapsing them into one aggregate loses 60% on the 1h path, which on a
        cache-heavy session is the largest single cost component.
        """
        base = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
        c5 = self.ub._cost_of(
            {**base, "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000,
                                        "ephemeral_1h_input_tokens": 0}}, "claude-opus-5")
        c1 = self.ub._cost_of(
            {**base, "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                        "ephemeral_1h_input_tokens": 1_000_000}}, "claude-opus-5")
        self.assertAlmostEqual(c5, 5.00 * 1.25, places=6)
        self.assertAlmostEqual(c1, 5.00 * 2.00, places=6)
        self.assertAlmostEqual(c1 / c5, 1.6, places=6)

    def test_falls_back_to_aggregate_when_ttl_split_absent(self):
        """Older transcripts have no `cache_creation` breakdown."""
        cost = self.ub._cost_of(
            {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 1_000_000}, "claude-opus-5")
        self.assertAlmostEqual(cost, 5.00 * 1.25, places=6,
                               msg="aggregate should be treated as the 5m rate")

    def test_thinking_tokens_are_not_double_counted(self):
        """FINDINGS section 2: thinking_tokens is a SUBSET of output_tokens."""
        without = self.ub._cost_of(
            {"input_tokens": 0, "output_tokens": 1000, "cache_read_input_tokens": 0},
            "claude-opus-5")
        with_details = self.ub._cost_of(
            {"input_tokens": 0, "output_tokens": 1000, "cache_read_input_tokens": 0,
             "output_tokens_details": {"thinking_tokens": 900}}, "claude-opus-5")
        self.assertEqual(without, with_details)

    def test_cache_read_is_a_tenth_of_input(self):
        c = self.ub._cost_of(
            {"input_tokens": 0, "output_tokens": 0,
             "cache_read_input_tokens": 1_000_000}, "claude-opus-5")
        self.assertAlmostEqual(c, 0.50, places=6)

    def test_output_is_five_times_input(self):
        pin = self.ub._price("claude-opus-5")["in"]
        pout = self.ub._price("claude-opus-5")["out"]
        self.assertAlmostEqual(pout / pin, 5.0, places=6)

    def test_fast_mode_doubles_opus_pricing(self):
        u = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_input_tokens": 0}
        std = self.ub._cost_of(u, self.ub._norm_model("claude-opus-5", "standard"))
        fast = self.ub._cost_of(u, self.ub._norm_model("claude-opus-5", "fast"))
        self.assertAlmostEqual(fast / std, 2.0, places=6)

    def test_context_window_suffix_is_stripped(self):
        self.assertEqual(self.ub._norm_model("claude-opus-5[1m]", "standard"),
                         "claude-opus-5")

    def test_dated_model_id_resolves_to_its_real_price(self):
        """Transcripts carry dated ids; an unstripped date takes the Opus-tier
        fallback and prices Haiku at 5x. Wrong in the expensive direction."""
        self.assertEqual(self.ub._norm_model("claude-haiku-4-5-20251001", "standard"),
                         "claude-haiku-4-5")
        u = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_input_tokens": 0}
        dated = self.ub._cost_of(u, self.ub._norm_model("claude-haiku-4-5-20251001", "standard"))
        self.assertAlmostEqual(dated, self.ub.PRICES["claude-haiku-4-5"]["in"], places=6)
        self.assertLess(dated, self.ub.FALLBACK_PRICE["in"])

    def test_date_stripping_never_invents_a_match(self):
        """Only strip a date when it lands on a known key, so a genuinely
        unknown model still falls back instead of being mangled into a match."""
        self.assertEqual(self.ub._norm_model("claude-nonexistent-9-20251001", "standard"),
                         "claude-nonexistent-9-20251001")

    def test_unknown_model_falls_back_to_opus_tier(self):
        """An unrecognized model must not silently cost zero."""
        c = self.ub._cost_of(
            {"input_tokens": 1_000_000, "output_tokens": 0,
             "cache_read_input_tokens": 0}, "claude-does-not-exist-9")
        self.assertAlmostEqual(c, self.ub.FALLBACK_PRICE["in"], places=6)
        self.assertGreater(c, 0)

    def test_web_search_billed_per_request(self):
        c = self.ub._cost_of(
            {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "server_tool_use": {"web_search_requests": 7}}, "claude-opus-5")
        self.assertAlmostEqual(c, 7 * 0.01, places=6)


# ---------------------------------------------------------------------------
# FINDINGS sections 4 & 5 -- window boundaries
# ---------------------------------------------------------------------------

class TestWindows(TempEnv):

    def test_anchor_separates_turns_either_side_of_a_reset(self):
        """The anchor is a RESET time, so turns either side of it belong to
        different windows -- and the later window opens with the first message
        AFTER the reset, not at the reset itself (FINDINGS 4c). The window that
        starts exactly on the anchor is a grid artifact; the chain model says a
        window cannot open before someone sends something."""
        anchor = 1_000_000.0
        span = 5 * HOUR
        events = [[anchor - 1, 1.0, "m"], [anchor + 1, 2.0, "m"]]
        w = self.ub._session_windows(events, 5, anchor)
        self.assertEqual(len(w), 2, "turns either side of a boundary are separate windows")
        self.assertEqual(w[0]["end"], anchor, "the earlier window ends at the reset")
        self.assertEqual(w[0]["seeded"], "anchor", "and it is the calibrated one")
        self.assertEqual(w[1]["start"], anchor + 1, "the next opens at its first message")
        self.assertEqual(w[1]["end"], anchor + 1 + span)
        self.assertEqual(w[1]["seeded"], "chain")
        self.assertAlmostEqual(w[1]["lag"], 1.0, msg="1s after the reset")
        self.assertAlmostEqual(w[0]["cost"], 1.0)
        self.assertAlmostEqual(w[1]["cost"], 2.0)

    def test_idle_gap_reseeds_the_window_instead_of_tiling_a_grid(self):
        """FINDINGS 4c, and the bug this replaced: a rigid grid keeps reporting
        `anchor + k*span` forever, so it cannot notice you stopped working.
        Reproduced live on 2026-08-14 -- a 10.8h gap left a stale anchor still
        claiming an exact boundary while chaining from a reset 3 windows old."""
        anchor = 1_000_000.0
        span = 5 * HOUR
        events = [
            [anchor + 60, 1.0, "m"],          # window A, opens 60s after the reset
            [anchor + 2 * HOUR, 1.0, "m"],
            # ... then nothing for over 10 hours, spanning a whole window ...
            [anchor + 13 * HOUR, 1.0, "m"],   # this message opens a NEW window
        ]
        w = self.ub._session_windows(events, 5, anchor)
        cur = w[-1]
        self.assertEqual(cur["start"], anchor + 13 * HOUR,
                         "the window opens at the first message after expiry")
        self.assertEqual(cur["end"], anchor + 13 * HOUR + span)
        self.assertEqual(cur["seeded"], "chain")
        # A grid would have said this window started at anchor + 10h and reset
        # at anchor + 15h -- nearly 3 hours off, and silently.
        self.assertNotEqual(cur["start"], anchor + 10 * HOUR)

    def test_boundary_confidence_degrades_after_an_idle_gap(self):
        """The label must follow the evidence. A stale boundary decides which
        turns count as 'this window', so it corrupts the numerator that
        calibration divides -- not merely the displayed clock."""
        now = time.time()
        anchor = now - 13 * HOUR
        self.ub._save(self.ub.CONFIG_PATH,
                      {"calibrations": [], "session_anchor": anchor})
        # Worked right after the reset, went away for 10h, came back.
        st = self.ub._build_state([
            [anchor + 60, 1.0, "m"],
            [now - HOUR, 1.0, "m"],
        ])
        b = st["boundary"]
        self.assertEqual(b["confidence"], "estimated")
        self.assertTrue(b["reanchor"], "must ask for a re-anchor")
        self.assertGreater(b["lag_seconds"], 5 * HOUR)
        self.assertIn("ESTIMATED", self.ub.render_inject(st),
                      "the INJECTED block must carry it -- that is what the model reads")

    def test_boundary_stays_tight_when_work_is_continuous(self):
        """The caveat must not fire on ordinary continuous use, or it becomes
        noise the model learns to ignore."""
        now = time.time()
        anchor = now - 2 * HOUR
        self.ub._save(self.ub.CONFIG_PATH,
                      {"calibrations": [], "session_anchor": anchor})
        st = self.ub._build_state([[anchor + 30, 1.0, "m"], [now - 60, 1.0, "m"]])
        self.assertEqual(st["boundary"]["confidence"], "tight")
        self.assertFalse(st["boundary"]["reanchor"])
        self.assertNotIn("ESTIMATED", self.ub.render_inject(st))

    def test_chain_mode_is_biased_under_continuous_use(self):
        """FINDINGS section 4 -- this is the shipped bug, encoded as a test.

        With unbroken activity there is no gap to anchor on, so the chain
        heuristic reports a window that started when the OLDEST cached event
        happened rather than on the real boundary. Grid mode is immune.
        """
        anchor = 1_000_000.0
        # Continuous activity for an hour either side of a real boundary.
        events = [[anchor - HOUR + i * 60, 1.0, "m"] for i in range(120)]

        grid = self.ub._session_windows(events, 5, anchor)
        grid_cur = [x for x in grid if x["start"] <= anchor + 60 < x["end"]][0]
        self.assertEqual(grid_cur["start"], anchor,
                         "grid mode must put the boundary exactly on the anchor")

        chain = self.ub._session_windows(events, 5, None)
        chain_cur = [x for x in chain if x["start"] <= anchor + 60 < x["end"]][0]
        self.assertEqual(chain_cur["start"], anchor - HOUR,
                         "chain mode drifts to the oldest event -- the original bug")
        self.assertNotEqual(grid_cur["start"], chain_cur["start"])

    def test_grid_phase_cannot_drift_with_more_history(self):
        """Adding older history must not move a grid boundary (chain mode would)."""
        anchor = 1_000_000.0
        recent = [[anchor + 60, 1.0, "m"]]
        with_history = [[anchor - 3 * DAY, 1.0, "m"]] + recent

        def cur(evs):
            w = self.ub._session_windows(evs, 5, anchor)
            return [x for x in w if x["start"] <= anchor + 60 < x["end"]][0]["start"]

        self.assertEqual(cur(recent), cur(with_history))

    def test_weekly_fixed_period_excludes_days_outside_it(self):
        """FINDINGS section 5: a rolling 7 days is a DIFFERENT set of days.

        Measured 2x error on real data: the rolling sum was about twice the real week.
        """
        now = time.time()
        week_reset = now + 2 * DAY          # week runs (reset-7d) .. reset
        week_start = week_reset - 7 * DAY
        cfg = {"calibrations": [], "session_hours": 5, "weekly_days": 7,
               "weekly_anchor": week_reset}
        self.ub._save(self.ub.CONFIG_PATH, cfg)

        inside = [[week_start + HOUR, 10.0, "m"]]
        outside = [[week_start - HOUR, 99.0, "m"]]     # before the week opened
        st = self.ub._build_state(sorted(outside + inside))

        self.assertAlmostEqual(st["weekly"]["cost_usd"], 10.0,
                               msg="spend before the week opened must be excluded")
        self.assertTrue(st["weekly"]["anchored"])
        self.assertAlmostEqual(st["weekly"]["resets_at"], week_reset, places=3)

    def test_weekly_unanchored_is_rolling_and_labelled(self):
        now = time.time()
        self.ub._save(self.ub.CONFIG_PATH, {"calibrations": []})
        st = self.ub._build_state([[now - 3 * DAY, 5.0, "m"]])
        self.assertFalse(st["weekly"]["anchored"])
        self.assertIsNone(st["weekly"]["resets_at"])

    def _premium_cfg(self, week_reset):
        self.ub._save(self.ub.CONFIG_PATH, {
            "calibrations": [
                {"scope": "weekly", "pct": 50.0, "cost": 1000.0},
                {"scope": "weekly_premium", "pct": 10.0, "cost": 100.0},
            ],
            "session_hours": 5, "weekly_days": 7, "weekly_anchor": week_reset,
        })

    def test_premium_weekly_is_metered_separately(self):
        """FINDINGS 7: premium models draw on a smaller weekly cap AS WELL AS
        the shared one, so their spend appears in both meters, not instead."""
        week_reset = time.time() + 2 * DAY
        self._premium_cfg(week_reset)
        st = self.ub._build_state([
            [week_reset - 6 * DAY, 30.0, "claude-opus-5"],
            [week_reset - 5 * DAY, 20.0, "claude-fable-5"],
        ])
        p = st["weekly_premium"]
        self.assertAlmostEqual(p["cost_usd"], 20.0, msg="only premium turns")
        self.assertEqual(p["turns"], 1)
        self.assertAlmostEqual(st["weekly"]["cost_usd"], 50.0,
                               msg="premium spend ALSO counts toward the shared week")
        self.assertAlmostEqual(p["capacity_usd"], 1000.0)   # 100 / 0.10

    def test_premium_bucket_defaults_to_measured_models_only(self):
        """Mythos is priced like Fable and plausibly shares the bar, but only
        Fable has been measured. Measure before modelling."""
        cfg = {}
        self.assertIn("claude-fable-5", self.ub._premium_models(cfg))
        self.assertNotIn("claude-mythos-5", self.ub._premium_models(cfg))
        self.assertIn("claude-fable-5:fast", self.ub._premium_models(cfg),
                      "fast mode is the same model against the same allowance")
        widened = self.ub._premium_models({"premium_models": ["claude-mythos-5"]})
        self.assertIn("claude-mythos-5", widened)

    def test_binding_limit_names_the_bucket_nearest_its_cap(self):
        """Forecasting only the 5h window reads as all-clear while a different
        allowance is the one about to run out."""
        now = time.time()
        week_reset = now + 6 * DAY          # week opened 1 day ago
        self._premium_cfg(week_reset)
        # A SUSTAINED premium pace across the elapsed day — not a burst. Weekly
        # buckets are projected from the window average, so a few heavy minutes
        # must not imply a week-long trend (that was the over-warning bug).
        events = [[now - 60 * i, 300.0 / 240, "claude-fable-5"] for i in range(1, 241)]
        st = self.ub._build_state(sorted(events))
        self.assertIsNotNone(st["binding"])
        self.assertEqual(st["binding"]["bucket"], "premium week")
        out = self.ub.render_inject(st)
        self.assertIn("premium week caps in", out)
        # Running THIS bucket dry blocks you for days, which is the whole reason
        # it outranks the 5h window rather than merely differing from it.
        self.assertGreater(st["binding"]["blocked_hours"], 24)

    def test_a_burst_does_not_make_the_week_look_doomed(self):
        """Extrapolating a trailing 30-minute rate across a week assumes you work
        24/7. Half an hour of heavy use must not project into a weekly lockout —
        the window's own average already embeds the real duty cycle."""
        now = time.time()
        week_reset = now + 6 * DAY
        self._premium_cfg(week_reset)
        burst = [[now - 60 * i, 20.0, "claude-opus-5"] for i in range(1, 31)]
        st = self.ub._build_state(sorted(burst))
        trailing = self.ub._burn_rate(sorted(burst), now)
        self.assertGreater(trailing, 500, "$600 in 30 minutes is a huge trailing rate")
        # Projected on the trailing rate the week would cap in about an hour.
        # Projected on the week's own average it is dozens of hours away.
        naive = st["weekly"]["remaining_usd"] / trailing
        self.assertLess(naive, 5.0)
        self.assertGreater(st["weekly"]["hours_to_exhaustion"], 10 * naive,
                           "the weekly forecast must not extrapolate a burst")
        self.assertEqual(st["weekly"]["rate_basis"], "week average")

    def test_premium_meter_is_silent_when_unused(self):
        """No premium turns must mean no premium noise in the injected block."""
        week_reset = time.time() + 2 * DAY
        self._premium_cfg(week_reset)
        st = self.ub._build_state([[week_reset - 6 * DAY, 30.0, "claude-opus-5"]])
        self.assertAlmostEqual(st["weekly_premium"]["cost_usd"], 0.0)
        self.assertNotIn("Premium week", self.ub.render_inject(st))

    def test_session_window_reported_even_with_no_turns_yet(self):
        """A fresh window should still report correct boundaries, not 'unknown'."""
        now = time.time()
        anchor = now + HOUR
        self.ub._save(self.ub.CONFIG_PATH,
                      {"calibrations": [], "session_anchor": anchor})
        st = self.ub._build_state([])
        self.assertEqual(st["session"]["turns"], 0)
        self.assertAlmostEqual(st["session"]["resets_at"], anchor, places=3)
        self.assertTrue(st["session_anchored"])


# ---------------------------------------------------------------------------
# FINDINGS section 6 -- calibration
# ---------------------------------------------------------------------------

class TestCapacity(TempEnv):

    def _cfg(self, samples):
        return {"calibrations": samples}

    def test_capacity_is_cost_over_fraction(self):
        cap, conf, n = self.ub._capacity(
            self._cfg([{"scope": "session", "pct": 20.0, "cost": 100.0}]), "session")
        self.assertAlmostEqual(cap, 500.0)
        self.assertEqual((conf, n), ("low", 1))

    def test_one_sample_is_never_better_than_low_confidence(self):
        _, conf, _ = self.ub._capacity(
            self._cfg([{"scope": "session", "pct": 50.0, "cost": 10.0}]), "session")
        self.assertEqual(conf, "low",
                         "a single sample cannot distinguish capacity from a wrong weighting shape")

    def test_three_tight_samples_reach_high_confidence(self):
        s = [{"scope": "session", "pct": p, "cost": c} for p, c in
             [(10, 100.0), (20, 202.0), (30, 298.0)]]
        cap, conf, n = self.ub._capacity(self._cfg(s), "session")
        self.assertEqual((conf, n), ("high", 3))
        # capacities are 1000.0, 1010.0, 993.3 -> median is 1000.0
        self.assertAlmostEqual(cap, 1000.0, places=0)

    def test_wide_spread_stays_low_confidence(self):
        s = [{"scope": "session", "pct": 10, "cost": 100.0},
             {"scope": "session", "pct": 10, "cost": 300.0}]
        _, conf, _ = self.ub._capacity(self._cfg(s), "session")
        self.assertEqual(conf, "low", "disagreeing samples must not read as confident")

    def test_expired_sample_is_ignored_entirely(self):
        """FINDINGS section 6: promotional limits expire, so the sample must too.

        Degrade to 'I don't know', never to a silently over-generous number.
        """
        s = [{"scope": "weekly", "pct": 26.0, "cost": 1000.0,
              "expires_epoch": time.time() - 60}]
        cap, conf, n = self.ub._capacity(self._cfg(s), "weekly")
        self.assertIsNone(cap)
        self.assertEqual((conf, n), ("none", 0))

    def test_unexpired_sample_still_counts(self):
        s = [{"scope": "weekly", "pct": 26.0, "cost": 1000.0,
              "expires_epoch": time.time() + DAY}]
        cap, _, _ = self.ub._capacity(self._cfg(s), "weekly")
        self.assertAlmostEqual(cap, 1000.0 / 0.26, places=3)

    def test_retired_sample_is_excluded_but_not_erased(self):
        """FINDINGS 6d: a sample measured against a since-corrected window
        boundary was paired with the wrong cost, so it must stop counting — but
        it stays in the file, because an excluded sample the user cannot see is
        indistinguishable from one that was never taken."""
        s = [{"scope": "session", "pct": 13.0, "cost": 96.2832},
             {"scope": "session", "pct": 21.0, "cost": 196.83,
              "retired": "measured under the fixed-grid boundary model"},
             {"scope": "session", "pct": 47.0, "cost": 342.0275}]
        cap, conf, n = self.ub._capacity(self._cfg(s), "session")
        self.assertEqual(n, 2, "the retired sample must not count")
        # The two survivors agree to well under 2%, so confidence improves.
        self.assertEqual(conf, "medium")
        self.assertAlmostEqual(cap, (96.2832 / 0.13 + 342.0275 / 0.47) / 2, places=2)
        self.assertEqual(len(self._cfg(s)["calibrations"]), 3, "still on record")

    def test_scopes_do_not_leak_into_each_other(self):
        s = [{"scope": "session", "pct": 10, "cost": 50.0},
             {"scope": "weekly", "pct": 50, "cost": 500.0}]
        self.assertAlmostEqual(self.ub._capacity(self._cfg(s), "session")[0], 500.0)
        self.assertAlmostEqual(self.ub._capacity(self._cfg(s), "weekly")[0], 1000.0)


# ---------------------------------------------------------------------------
# FINDINGS section 8 -- scanning
# ---------------------------------------------------------------------------

class TestScanning(TempEnv):

    def test_counts_a_simple_transcript(self):
        now = time.time()
        write_transcript(self.projects, "s1",
                         [turn(now - 60, inp=1_000_000), turn(now - 30, out=1_000_000)])
        st = self.ub.collect(full=True)
        self.assertAlmostEqual(st["session"]["cost_usd"], 5.0 + 25.0, places=4)
        self.assertEqual(st["session"]["turns"], 2)

    def test_incremental_scan_resumes_at_offset(self):
        now = time.time()
        p = write_transcript(self.projects, "s1", [turn(now - 120, inp=1_000_000)])
        first = self.ub.collect(full=True)
        self.assertAlmostEqual(first["session"]["cost_usd"], 5.0, places=4)

        with p.open("a") as fh:
            fh.write(turn(now - 60, inp=1_000_000) + "\n")
        second = self.ub.collect()          # incremental, not full
        self.assertAlmostEqual(second["session"]["cost_usd"], 10.0, places=4,
                               msg="appended turns must be picked up exactly once")
        self.assertEqual(second["session"]["turns"], 2)

    def test_incremental_scan_does_not_double_count(self):
        now = time.time()
        write_transcript(self.projects, "s1", [turn(now - 60, inp=1_000_000)])
        self.ub.collect(full=True)
        again = self.ub.collect()           # nothing new
        self.assertAlmostEqual(again["session"]["cost_usd"], 5.0, places=4)
        self.assertEqual(again["session"]["turns"], 1)

    def test_partial_trailing_line_is_not_consumed(self):
        """A turn still being flushed to disk must survive to the next scan.

        The failure this guards against is NOT a parse error -- a truncated line
        fails json.loads either way. It is that consuming the partial advances the
        stored byte offset past it, so when the rest of the line lands it is never
        re-read and the turn is **permanently lost**.

        The truncated fixture must still contain `"usage"` and `"assistant"`, or the
        bytes prefilter skips it and the test passes for the wrong reason. (Mutation
        testing caught exactly that: an earlier version of this test could not tell
        correct code from code that consumed partial lines.)
        """
        now = time.time()
        complete = turn(now - 60, inp=1_000_000, request_id="req_complete")
        pending = turn(now - 30, inp=1_000_000, request_id="req_pending")
        head, tail = pending[:-20], pending[-20:]
        self.assertIn('"usage"', head, "fixture must reach the prefilter")
        self.assertIn('"assistant"', head)

        p = write_transcript(self.projects, "s1", [complete], newline_at_end=True)
        with p.open("a") as fh:
            fh.write(head)                      # mid-write: no trailing newline yet
        st = self.ub.collect(full=True)
        self.assertEqual(st["session"]["turns"], 1, "partial line must not be counted")

        with p.open("a") as fh:                 # the rest of that same line arrives
            fh.write(tail + "\n")
        st2 = self.ub.collect()                 # incremental
        self.assertEqual(st2["session"]["turns"], 2,
                         "the completed line must be re-read, not skipped past")
        self.assertAlmostEqual(st2["session"]["cost_usd"], 10.0, places=4)

    def test_subagent_turns_are_counted(self):
        """Sidechain turns consume the same quota."""
        now = time.time()
        write_transcript(self.projects, "s1", [
            turn(now - 60, inp=1_000_000, sidechain=False),
            turn(now - 30, inp=1_000_000, sidechain=True),
        ])
        st = self.ub.collect(full=True)
        self.assertEqual(st["session"]["turns"], 2)
        self.assertAlmostEqual(st["session"]["cost_usd"], 10.0, places=4)

    def test_non_assistant_lines_are_ignored(self):
        now = time.time()
        write_transcript(self.projects, "s1", [
            json.dumps({"type": "user", "timestamp": "2026-08-12T00:00:00.000Z",
                        "message": {"content": "hi"}}),
            json.dumps({"type": "attachment", "timestamp": "2026-08-12T00:00:00.000Z"}),
            turn(now - 60, inp=1_000_000),
        ])
        st = self.ub.collect(full=True)
        self.assertEqual(st["session"]["turns"], 1)

    def test_corrupt_lines_do_not_abort_the_scan(self):
        now = time.time()
        write_transcript(self.projects, "s1", [
            "{not json at all",
            turn(now - 60, inp=1_000_000),
            "}{",
        ])
        st = self.ub.collect(full=True)
        self.assertEqual(st["session"]["turns"], 1)

    def test_events_outside_retention_are_pruned(self):
        now = time.time()
        write_transcript(self.projects, "s1", [
            turn(now - 30 * DAY, inp=1_000_000),     # far outside RETAIN_DAYS
            turn(now - 60, inp=1_000_000),
        ])
        st = self.ub.collect(full=True)
        self.assertEqual(st["session"]["turns"], 1)

    def test_nested_project_directories_are_found(self):
        now = time.time()
        deep = self.projects / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "s.jsonl").write_text(turn(now - 60, inp=1_000_000) + "\n")
        st = self.ub.collect(full=True)
        self.assertEqual(st["session"]["turns"], 1, "rglob must reach nested dirs")


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------

class TestRendering(TempEnv):

    def _state(self, **over):
        now = time.time()
        s = {
            "generated_at": now,
            "session": {"cost_usd": 100.0, "turns": 10, "window_start": now - HOUR,
                        "resets_at": now + HOUR, "capacity_usd": 500.0,
                        "used_frac": 0.2, "remaining_usd": 400.0,
                        "confidence": "low", "samples": 1,
                        "hours_to_exhaustion": 4.0},
            "weekly": {"cost_usd": 200.0, "capacity_usd": 1000.0, "used_frac": 0.2,
                       "remaining_usd": 800.0, "confidence": "low", "samples": 1,
                       "anchored": True, "resets_at": now + 3 * DAY,
                       "window_start": now - 4 * DAY},
            "burn_usd_per_hour": 25.0,
            "session_anchored": True,
            "recent_windows": [],
        }
        s["session"].update(over.get("session", {}))
        s["weekly"].update(over.get("weekly", {}))
        for k, v in over.items():
            if k not in ("session", "weekly"):
                s[k] = v
        return s

    def test_calibrated_output_marks_figures_as_estimates(self):
        out = self.ub.render_inject(self._state())
        self.assertIn("~", out, "calibrated figures must read as estimates")
        self.assertIn("<usage-budget>", out)
        self.assertIn("</usage-budget>", out)

    def test_uncalibrated_output_says_so_loudly(self):
        st = self._state(session={"capacity_usd": None, "used_frac": None,
                                  "remaining_usd": None, "confidence": "none",
                                  "samples": 0, "hours_to_exhaustion": None})
        out = self.ub.render_inject(st)
        self.assertIn("UNCALIBRATED", out)
        self.assertNotIn("% used", out.split("Week")[0],
                         "must not invent a percentage without a denominator")

    def _burn_state(self, capacity, spent_early, spent_recent, resets_in_h):
        """Build real state: `spent_recent` lands in the trailing 30m, so the
        burn rate is roughly 2x it, and the window resets in `resets_in_h`."""
        now = time.time()
        self.ub._save(self.ub.CONFIG_PATH, {
            "calibrations": [{"scope": "session", "pct": 50.0, "cost": capacity / 2}],
            "session_hours": 5, "weekly_days": 7,
            "session_anchor": now + resets_in_h * HOUR,
        })
        events = [[now - 2 * HOUR, spent_early, "m"]]
        events += [[now - 60 * i, spent_recent / 25, "m"] for i in range(1, 26)]
        return self.ub._build_state(sorted(events))

    def test_a_cap_just_before_a_reset_is_not_worth_cutting_scope_over(self):
        """The bug that made a live agent shorten its own plan with 53% of the
        window unspent: it hit "cap arrives first" ~13 minutes before the window
        rolled over. Running out just before a reset costs you the wait, and the
        next window arrives with a full allowance."""
        st = self._burn_state(capacity=100.0, spent_early=13.0, spent_recent=34.0,
                              resets_in_h=1.0)
        self.assertGreater(st["session"]["remaining_usd"], 50.0, "53% still unspent")
        self.assertLess(st["session"]["hours_to_exhaustion"], 1.0, "cap does arrive first")
        self.assertIsNone(st["binding"], "but it must NOT demand a reshape")
        out = self.ub.render_inject(st)
        self.assertNotIn("WOULD RUN OUT", out)
        self.assertIn("NOT a reason to cut scope", out)

    def test_a_real_lockout_is_reported_with_how_long_it_lasts(self):
        """A cap only matters as long as it blocks you, so that is the number."""
        st = self._burn_state(capacity=100.0, spent_early=13.0, spent_recent=34.0,
                              resets_in_h=5.0)
        b = st["binding"]
        self.assertIsNotNone(b)
        self.assertEqual(b["bucket"], "5h window")
        self.assertGreater(b["blocked_hours"], self.ub.LOCKOUT_WARN_HOURS)
        out = self.ub.render_inject(st)
        self.assertIn("WOULD RUN OUT", out)
        self.assertIn("BLOCKED", out)

    def test_no_cap_in_sight_says_nothing_alarming(self):
        st = self._burn_state(capacity=10_000.0, spent_early=5.0, spent_recent=5.0,
                              resets_in_h=1.0)
        self.assertIsNone(st["binding"])
        out = self.ub.render_inject(st)
        self.assertNotIn("WOULD RUN OUT", out)
        self.assertNotIn("NOT a reason to cut scope", out)

    def test_output_tells_the_model_to_push_back(self):
        """The how-to-use line is what makes the data actionable rather than noise."""
        out = self.ub.render_inject(self._state())
        self.assertIn("How to use", out)
        self.assertRegex(out, r"exceeds the remaining|smaller scope")

    def test_unanchored_weekly_is_labelled_rolling(self):
        st = self._state(weekly={"anchored": False, "resets_at": None})
        self.assertIn("Rolling 7d", self.ub.render_inject(st))

    def test_render_status_runs_on_an_uncalibrated_state(self):
        st = self._state(session={"capacity_usd": None, "used_frac": None,
                                  "remaining_usd": None, "hours_to_exhaustion": None},
                         weekly={"capacity_usd": None, "used_frac": None,
                                 "remaining_usd": None})
        self.assertIn("5h window spend", self.ub.render_status(st))


class TestPredict(TempEnv):

    def test_predict_runs_with_no_window(self):
        self.assertIn("no active window", self.ub.predict())

    def test_predict_reports_both_hypotheses_and_separation(self):
        now = time.time()
        self.ub._save(self.ub.CONFIG_PATH, {
            "calibrations": [{"scope": "session", "pct": 20.0, "cost": 100.0}],
            "session_anchor": now + HOUR})
        write_transcript(self.projects, "s1",
                         [turn(now - 60, out=100_000, cread=50_000_000)])
        self.ub.collect(full=True)
        out = self.ub.predict()
        self.assertIn("price-weighted", out)
        self.assertIn("compute-weighted", out)
        self.assertIn("separation", out)

    def test_predict_explains_when_mix_cannot_discriminate(self):
        """Same cache-read share as calibration => identical predictions.

        Both hypotheses are refit from the same samples, so B's capacity scales
        with B's cost. Spending more never separates them; only a different mix
        does. The tool must say so rather than implying 'wait longer'.
        """
        now = time.time()
        self.ub._save(self.ub.CONFIG_PATH, {
            "calibrations": [{"scope": "session", "pct": 20.0, "cost": 100.0}],
            "session_anchor": now + HOUR})
        write_transcript(self.projects, "s1",
                         [turn(now - 60, out=10_000, cread=100_000_000)])
        self.ub.collect(full=True)
        out = self.ub.predict()
        self.assertIn("cannot discriminate", out)
        self.assertIn("will NOT create separation", out)

    def test_predict_prices_each_model_at_its_own_rates(self):
        """An all-Fable window costs 2x an all-Opus one. The original
        implementation priced the whole mix at Opus rates and measurably
        halved cost_a on a real all-Fable window (2026-08-13)."""
        now = time.time()
        self.ub._save(self.ub.CONFIG_PATH, {
            "calibrations": [{"scope": "session", "pct": 20.0, "cost": 100.0}],
            "session_anchor": now + HOUR})
        write_transcript(self.projects, "s1",
                         [turn(now - 60, model="claude-fable-5", out=1_000_000)])
        self.ub.collect(full=True)
        out = self.ub.predict()
        self.assertIn("cost $50.00", out,
                      "1M Fable output tokens are $50, not Opus's $25")
        self.assertIn("claude-fable-5 x1", out)

    def test_predict_prices_a_mixed_model_window_per_model(self):
        now = time.time()
        self.ub._save(self.ub.CONFIG_PATH, {
            "calibrations": [{"scope": "session", "pct": 20.0, "cost": 100.0}],
            "session_anchor": now + HOUR})
        write_transcript(self.projects, "s1", [
            turn(now - 120, model="claude-opus-5", out=1_000_000),
            turn(now - 60, model="claude-fable-5", out=1_000_000),
        ])
        self.ub.collect(full=True)
        out = self.ub.predict()
        self.assertIn("cost $75.00", out, "$25 Opus + $50 Fable")


class TestParseReset(TempEnv):

    def test_bare_time_12h(self):
        d = self.ub._parse_reset("9:59pm")
        self.assertEqual((d.hour, d.minute), (21, 59))

    def test_bare_time_24h(self):
        d = self.ub._parse_reset("21:59")
        self.assertEqual((d.hour, d.minute), (21, 59))

    def test_date_and_time(self):
        d = self.ub._parse_reset("Aug 17 6:59am")
        self.assertEqual((d.month, d.day, d.hour, d.minute), (8, 17, 6, 59))

    def test_date_with_comma(self):
        d = self.ub._parse_reset("Aug 17, 6:59am")
        self.assertEqual((d.month, d.day, d.hour, d.minute), (8, 17, 6, 59))

    def test_iso(self):
        d = self.ub._parse_reset("2026-08-17T06:59:00")
        self.assertEqual((d.month, d.day, d.hour), (8, 17, 6))

    def test_unparseable_returns_none_rather_than_guessing(self):
        self.assertIsNone(self.ub._parse_reset("sometime next Tuesday-ish"))


class TestRobustness(TempEnv):

    def test_burn_rate_is_zero_with_no_recent_events(self):
        self.assertEqual(self.ub._burn_rate([], time.time()), 0.0)

    def test_burn_rate_ignores_old_events(self):
        now = time.time()
        self.assertEqual(self.ub._burn_rate([[now - 10 * HOUR, 100.0, "m"]], now), 0.0)

    def test_load_returns_default_on_corrupt_json(self):
        self.ub.CONFIG_PATH.write_text("{ this is not json")
        self.assertEqual(self.ub._load(self.ub.CONFIG_PATH, {"ok": True}), {"ok": True})

    def test_save_is_atomic_and_leaves_no_tmp(self):
        self.ub._save(self.ub.CONFIG_PATH, {"a": 1})
        self.assertEqual(json.loads(self.ub.CONFIG_PATH.read_text()), {"a": 1})
        self.assertFalse(list(self.state.glob("*.tmp")), "temp file must be replaced")

    def test_calibrate_rejects_out_of_range_pct(self):
        for bad in (0, -5, 101):
            self.assertIn("error", self.ub.calibrate(bad, None, None).lower())

    def test_calibrate_refuses_when_window_has_no_spend(self):
        """Cannot pair a percentage with zero cost -- that yields capacity 0."""
        out = self.ub.calibrate(13.0, None, None)
        self.assertIn("error", out.lower())

    def test_calibrate_rejects_unparseable_reset(self):
        self.assertIn("error", self.ub.calibrate(None, None, "whenever").lower())

    def test_pinned_cost_overrides_the_live_figure(self):
        """A /usage reading and the calibrate call are separated by a human
        round-trip, and windows roll in between. Pairing a pre-roll percentage
        with the post-roll (near-zero) cost would store a garbage capacity."""
        now = time.time()
        write_transcript(self.projects, "s1", [turn(now - 60, inp=1_000_000)])
        self.ub.collect(full=True)
        live = self.ub._load(self.ub.STATE_PATH, {})["session"]["cost_usd"]
        self.assertAlmostEqual(live, 5.0, places=4)

        out = self.ub.calibrate(25.0, None, None, cost=200.0)
        self.assertNotIn("error", out.lower())
        cfg = self.ub._load(self.ub.CONFIG_PATH, {})
        sample = [c for c in cfg["calibrations"] if c["scope"] == "session"][-1]
        self.assertAlmostEqual(sample["cost"], 200.0,
                               msg="pinned cost must win over the live figure")
        cap, _, _ = self.ub._capacity(cfg, "session")
        self.assertAlmostEqual(cap, 800.0, places=3)

    def test_pinned_cost_still_rejects_nonsense(self):
        self.assertIn("error", self.ub.calibrate(25.0, None, None, cost=0.0).lower())

    def test_anchor_is_applied_before_cost_is_paired(self):
        """FINDINGS section 6: ordering is load-bearing.

        Anchors change which turns fall inside the window, so setting one must
        change the cost the percentage gets paired with. If it did not, the
        boundary error would be baked into the stored capacity.
        """
        now = time.time()
        # Two turns 3h apart. An anchor between them splits them into
        # different windows; without one, chaining puts both in the first.
        write_transcript(self.projects, "s1", [
            turn(now - 3 * HOUR, inp=1_000_000),
            turn(now - 60, inp=1_000_000),
        ])
        self.ub.collect(full=True)
        unanchored = self.ub._load(self.ub.STATE_PATH, {})["session"]["cost_usd"]

        # Anchor at now+3h => the current window is [now-2h, now+3h), which
        # contains the recent turn but not the one 3h ago. An anchor at now+1h
        # would span [now-4h, now+1h) and contain BOTH, testing nothing.
        anchor_iso = datetime.fromtimestamp(now + 3 * HOUR).isoformat()
        out = self.ub.calibrate(None, None, anchor_iso)
        self.assertNotIn("error", out.lower())
        anchored = self.ub._load(self.ub.STATE_PATH, {})["session"]["cost_usd"]
        self.assertNotEqual(unanchored, anchored,
                            "setting an anchor must change the in-window cost")
        self.assertAlmostEqual(anchored, 5.0, places=4,
                               msg="only the turn inside the anchored window should count")

    def test_59_anchor_is_kept_as_given(self):
        """FINDINGS section 15 REFUTED section 4b: ':59' is the real boundary.

        Direct API reads returned `21:09:59` and `06:59:59`, so a ':59' reading
        needs no correction. These tests previously asserted the opposite -- that
        calibrate should suggest rounding up to the hour -- and are inverted
        here rather than deleted, because the old advice is in circulation and
        the note now tells such a user to undo it.
        """
        out = self.ub.calibrate(None, None, "9:59pm")
        self.assertNotIn("one minute low", out,
                         "the rendering-artifact theory is refuted")
        self.assertIn("real boundary", out)
        self.assertIn('"21:59"', out, "must echo the value as given, not 22:00")
        cfg = self.ub._load(self.ub.CONFIG_PATH, {})
        anchored = datetime.fromtimestamp(cfg["session_anchor"])
        self.assertEqual(anchored.minute, 59,
                         "the anchor must still never be moved silently")

    def test_59_weekly_anchor_is_kept_as_given(self):
        out = self.ub.calibrate(None, None, None, "Aug 17 6:59am")
        self.assertNotIn("one minute low", out)
        self.assertIn('"Aug 17 06:59"', out)

    def test_on_the_hour_anchor_gets_no_note(self):
        out = self.ub.calibrate(None, None, "10:00pm", "Aug 17 7:00am")
        self.assertNotIn("real boundary", out)
        self.assertNotIn("one minute low", out)


# ---------------------------------------------------------------------------
# FINDINGS section 11 -- the numerator is NOT exact: content-block duplication
# ---------------------------------------------------------------------------

class TestContentBlockDeduplication(TempEnv):
    """Claude Code writes one record per content block of an assistant reply.

    Each record repeats the entire `message.usage` payload, so a reply with
    text plus two tool calls yields three identical usage rows. Counting rows
    rather than messages inflated the numerator ~2.4x, validated against a
    /usage reading that reported this session's own token totals.
    """

    def test_one_reply_split_across_blocks_is_counted_once(self):
        now = int(time.time())
        write_transcript(self.projects, "s", [
            turn(now - 60, out=1000, msg_id="msg_A"),
            turn(now - 59, out=1000, msg_id="msg_A"),
            turn(now - 58, out=1000, msg_id="msg_A"),
        ])
        st = self.ub.collect(full=True)
        one = self.ub._cost_of({"input_tokens": 0, "output_tokens": 1000,
                                "cache_read_input_tokens": 0}, "claude-opus-5")
        self.assertAlmostEqual(st["session"]["cost_usd"], one, places=6,
                               msg="three records of one reply must cost one reply")
        self.assertEqual(st["session"]["turns"], 1)

    def test_sidechain_replay_of_a_parent_turn_is_counted_once(self):
        """56 of today's duplicates were cross-file, so per-file dedup is not enough."""
        now = int(time.time())
        write_transcript(self.projects, "parent", [turn(now - 60, out=1000, msg_id="msg_B")])
        write_transcript(self.projects, "child",
                         [turn(now - 60, out=1000, msg_id="msg_B", sidechain=True)])
        st = self.ub.collect(full=True)
        self.assertEqual(st["session"]["turns"], 1,
                         "the same message id in two files is still one API call")

    def test_distinct_replies_are_both_counted(self):
        now = int(time.time())
        write_transcript(self.projects, "s", [
            turn(now - 60, out=1000, msg_id="msg_C"),
            turn(now - 59, out=1000, msg_id="msg_D"),
        ])
        st = self.ub.collect(full=True)
        self.assertEqual(st["session"]["turns"], 2)

    def test_records_without_a_message_id_are_never_dropped(self):
        """An id-less record cannot be shown to be a duplicate, so it is kept."""
        events = [[1, 1.0, "claude-opus-5", None], [2, 1.0, "claude-opus-5", None]]
        self.assertEqual(len(self.ub._dedupe_events(events)), 2)

    def test_legacy_three_element_events_survive_dedup(self):
        events = [[1, 1.0, "claude-opus-5"], [2, 1.0, "claude-opus-5"]]
        self.assertEqual(len(self.ub._dedupe_events(events)), 2)

    def test_token_mix_dedupes_too(self):
        """predict() reads the mix on its own path; it had the same defect."""
        now = int(time.time())
        write_transcript(self.projects, "s", [
            turn(now - 60, out=1000, cread=500, msg_id="msg_E"),
            turn(now - 59, out=1000, cread=500, msg_id="msg_E"),
        ])
        mix = self.ub._window_token_mix(now - 3600, now + 1)
        self.assertEqual(mix["out"], 1000)
        self.assertEqual(mix["cread"], 500)
        raw = self.ub._window_token_mix(now - 3600, now + 1, dedupe=False)
        self.assertEqual(raw["out"], 2000, "dedupe=False must reproduce the old rule")


class TestCalibrationRebase(TempEnv):
    """Samples taken before the dedup fix paired a /usage percentage with an
    inflated cost. Left alone they inflate capacity, which makes the tool
    UNDER-report usage -- the unsafe direction."""

    def _cfg(self, cost, at, window_start):
        return {"version": 1, "session_hours": 5, "weekly_days": 7,
                "calibrations": [{"at": at, "scope": "session", "pct": 20.0,
                                  "cost": cost, "window_start": window_start}]}

    def test_verified_sample_is_rebased_onto_deduped_cost(self):
        now = int(time.time())
        ws = now - 3600
        write_transcript(self.projects, "s", [
            turn(ws + 10, out=1_000_000, msg_id="msg_F"),
            turn(ws + 11, out=1_000_000, msg_id="msg_F"),
        ])
        at = datetime.fromtimestamp(now, timezone.utc).isoformat()
        raw = self.ub._window_cost(ws, now + 1, dedupe=False)
        cfg, notes = self.ub.migrate_calibrations(self._cfg(raw, at, ws))
        c = cfg["calibrations"][0]
        self.assertEqual(c.get("rebased"), "dedupe")
        self.assertAlmostEqual(c["cost"], raw / 2, places=4)
        self.assertAlmostEqual(c["cost_pre_dedupe"], raw, places=4)
        self.assertEqual(cfg["version"], 2)

    def test_sample_whose_anchor_does_not_reproduce_its_cost_is_retired(self):
        """calibrate stamps window_start from the window live when it runs, so a
        reading taken just after a rollover records an anchor that never held
        those turns. Rescaling such a sample would launder the error."""
        now = int(time.time())
        write_transcript(self.projects, "s", [turn(now - 3600, out=1000, msg_id="msg_G")])
        at = datetime.fromtimestamp(now, timezone.utc).isoformat()
        cfg, notes = self.ub.migrate_calibrations(self._cfg(500.0, at, now - 5))
        c = cfg["calibrations"][0]
        self.assertTrue(c.get("retired"))
        self.assertIn("does not reproduce", c["retired"])
        self.assertNotIn("rebased", c)

    def test_migration_is_idempotent(self):
        cfg = {"version": 2, "calibrations": [
            {"at": "2026-08-16T00:00:00+00:00", "scope": "session",
             "pct": 20.0, "cost": 100.0, "window_start": 1.0}]}
        out, notes = self.ub.migrate_calibrations(dict(cfg))
        self.assertEqual(notes, [])
        self.assertEqual(out["calibrations"][0]["cost"], 100.0)


# ---------------------------------------------------------------------------
# Context-spend analysis -- `analyze`
#
# The window meters answer "how much is left". These pin "where did it go":
# ~86% of weighted spend is context handling, and the cost of a tool result is
# deferred across every turn that re-reads it.
# ---------------------------------------------------------------------------

class TestAnalyze(TempEnv):

    def _profiles(self, lines, name="s1"):
        write_transcript(self.projects, name, lines)
        return self.ub._session_profiles(days=30)

    # --- FINDINGS 11 interaction: one reply is many records -----------------

    def test_blocks_of_one_reply_merge_into_one_turn(self):
        """Four records of one reply are one turn, not four.

        Turn count feeds amortisation directly -- cost is size x turns that
        follow -- so counting records inflated every amortised figure ~2.4x.
        """
        now = time.time()
        profiles = self._profiles([
            turn(now - 40, cread=1000, out=50, msg_id="m1"),
            turn(now - 39, cread=1000, out=50, msg_id="m1"),
            turn(now - 38, cread=1000, out=50, msg_id="m1", tools=["Bash"]),
            turn(now - 30, cread=2000, out=50, msg_id="m2"),
        ])
        self.assertEqual(len(profiles[0]["turns"]), 2)

    def test_merging_keeps_tools_that_arrive_in_later_blocks(self):
        """The trap: keep-first dedup would throw every tool identity away.

        A real reply is [thinking], [text], [tool_use], [tool_use] -- each
        record carries only its OWN block, so the first record has no tools at
        all. Dropping later records would leave analyze with nothing to
        attribute, silently.
        """
        now = time.time()
        profiles = self._profiles([
            turn(now - 40, cread=0, out=0, msg_id="m1"),
            turn(now - 39, cread=0, out=0, msg_id="m1", tools=["Read"]),
            turn(now - 38, cread=0, out=0, msg_id="m1", tools=["Grep"]),
            turn(now - 30, cread=100_000, out=0, msg_id="m2"),
        ])
        self.assertEqual(profiles[0]["turns"][0]["tools"], ["Read", "Grep"])

    def test_growth_splits_across_every_tool_the_reply_called(self):
        """Charging only the last record's tool over-attributed to it.

        Two tools in one reply, 100k of growth re-read by one later turn:
        each tool owns half.
        """
        now = time.time()
        profiles = self._profiles([
            turn(now - 40, cread=0, out=0, msg_id="m1", tools=["Read"]),
            turn(now - 39, cread=0, out=0, msg_id="m1", tools=["Grep"]),
            turn(now - 30, cread=100_000, out=0, msg_id="m2"),
        ])
        by_tool = self.ub._attribute_context_growth(profiles)["by_tool"]
        self.assertIn("Read", by_tool)
        self.assertIn("Grep", by_tool)
        self.assertAlmostEqual(by_tool["Read"]["usd"], by_tool["Grep"]["usd"],
                               places=9)

    def test_merged_turn_counts_its_usage_once(self):
        """Repeated records repeat the whole usage payload; cost is not 3x."""
        now = time.time()
        profiles = self._profiles([
            turn(now - 40, cread=1_000_000, out=0, msg_id="m1"),
            turn(now - 39, cread=1_000_000, out=0, msg_id="m1"),
            turn(now - 38, cread=1_000_000, out=0, msg_id="m1"),
            turn(now - 30, cread=1_000_000, out=0, msg_id="m2"),
        ])
        turns = profiles[0]["turns"]
        self.assertAlmostEqual(turns[0]["cost"], 0.50, places=6)
        self.assertEqual(turns[0]["ctx"], 1_000_000)
        # The aggregate is what actually regresses: three records of one reply
        # summed to 3x its cost before the merge.
        self.assertAlmostEqual(sum(t["cost"] for t in turns), 1.00, places=6)

    def test_a_result_costs_every_turn_that_follows_it(self):
        """The whole premise: cost is size x turns remaining, not size once.

        100k tokens land before turn 1 and three further turns re-read them.
        On opus-5 a cache read is $5/Mtok x 0.10, so each re-read costs $0.05
        and the growth is charged 3 x $0.05 = $0.15 -- not $0.05.
        """
        now = time.time()
        profiles = self._profiles([
            turn(now - 40, cread=0, out=0, tools=["Read"]),
            turn(now - 30, cread=100_000, out=0),
            turn(now - 20, cread=100_000, out=0),
            turn(now - 10, cread=100_000, out=0),
        ])
        attr = self.ub._attribute_context_growth(profiles)
        self.assertAlmostEqual(attr["by_tool"]["Read"]["usd"], 0.15, places=4)

    def test_previous_turns_output_is_not_charged_to_the_tool(self):
        """Growth includes the assistant's own output; that is not the tool's.

        Charging it would inflate whichever tool a verbose turn happened to
        call -- a fitted number dressed as a measured one. Here 50k of the 60k
        growth is the prior turn's output, so only 10k is attributable.
        """
        now = time.time()
        profiles = self._profiles([
            turn(now - 30, cread=0, out=50_000, tools=["Bash"]),
            turn(now - 20, cread=60_000, out=0),
            turn(now - 10, cread=60_000, out=0),
        ])
        attr = self.ub._attribute_context_growth(profiles)
        # 10k attributable, read by 2 turns: 10_000 * 5 * 0.1 * 2 / 1e6
        self.assertAlmostEqual(attr["by_tool"]["Bash"]["usd"], 0.01, places=5)

    def test_output_larger_than_growth_attributes_nothing(self):
        """Never charge a negative. A turn that mostly generated adds no result."""
        now = time.time()
        profiles = self._profiles([
            turn(now - 30, cread=0, out=90_000, tools=["Write"]),
            turn(now - 20, cread=10_000, out=0),
            turn(now - 10, cread=10_000, out=0),
        ])
        attr = self.ub._attribute_context_growth(profiles)
        self.assertNotIn("Write", attr["by_tool"])

    def test_shrinking_context_counts_as_compaction_not_attribution(self):
        """A prefix that shrank is compaction, not something a tool did."""
        now = time.time()
        profiles = self._profiles([
            turn(now - 30, cread=500_000, out=0, tools=["Read"]),
            turn(now - 20, cread=20_000, out=0),
            turn(now - 10, cread=20_000, out=0),
        ])
        attr = self.ub._attribute_context_growth(profiles)
        self.assertEqual(attr["compactions"], 1)
        self.assertNotIn("Read", attr["by_tool"])

    def test_a_turn_calling_several_tools_splits_the_growth(self):
        now = time.time()
        profiles = self._profiles([
            turn(now - 30, cread=0, out=0, tools=["Read", "Bash"]),
            turn(now - 20, cread=100_000, out=0),
            turn(now - 10, cread=100_000, out=0),
        ])
        attr = self.ub._attribute_context_growth(profiles)
        self.assertAlmostEqual(attr["by_tool"]["Read"]["usd"],
                               attr["by_tool"]["Bash"]["usd"], places=6)

    def test_growth_with_no_tool_call_is_charged_to_conversation(self):
        now = time.time()
        profiles = self._profiles([
            turn(now - 30, cread=0, out=0),
            turn(now - 20, cread=40_000, out=0),
            turn(now - 10, cread=40_000, out=0),
        ])
        attr = self.ub._attribute_context_growth(profiles)
        self.assertIn("(conversation)", attr["by_tool"])

    def test_breakeven_includes_the_cost_of_generating_the_summary(self):
        """Output is priced at 5x input, so a summary is not free for being small.

        Omitting the generation term roughly halves the break-even and makes
        compaction look better than it is. Pinned explicitly because it is the
        term most easily left out.
        """
        be = self.ub._breakeven_turns(500_000, "claude-opus-5",
                                      summary_tokens=30_000)
        # No floor measured, so the post-compaction context falls back to the
        # 30k assumption. Generation is charged on the MEASURED summary text
        # (4,650, FINDINGS 32), not on that fallback -- they are different
        # quantities and conflating them is what this now pins.
        # one-off = read 500k (500_000*5*0.1) + generate 4,650 (4,650*25)
        #           + rewrite 30k at 1h (30_000*5*2.0), all /1e6
        self.assertAlmostEqual(be["one_off"], 0.25 + 0.11625 + 0.30, places=6)
        # saving/turn = (500k - 30k) * 5 * 0.1 / 1e6
        self.assertAlmostEqual(be["per_turn_saving"], 0.235, places=6)
        self.assertEqual(be["turns"], 3)  # ceil(0.66625 / 0.235)

    def test_breakeven_declines_when_context_is_already_small(self):
        be = self.ub._breakeven_turns(10_000, "claude-opus-5",
                                      summary_tokens=30_000)
        self.assertFalse(be["worth_it"])
        self.assertIsNone(be["turns"])

    def test_breakeven_prices_each_model_at_its_own_rate(self):
        """Fable input is 2x Opus, so the same context implies a different figure."""
        opus = self.ub._breakeven_turns(500_000, "claude-opus-5")
        fable = self.ub._breakeven_turns(500_000, "claude-fable-5")
        self.assertGreater(fable["per_turn_saving"], opus["per_turn_saving"])

    def test_single_turn_sessions_are_ignored(self):
        """Nothing follows one turn, so there is no amortisation to measure."""
        now = time.time()
        profiles = self._profiles([turn(now - 10, cread=50_000)])
        self.assertEqual(profiles, [])

    def test_analyze_reports_cleanly_with_no_data(self):
        self.assertIn("nothing to analyse", self.ub.analyze(days=7))

    def test_analyze_renders_and_states_its_caveats(self):
        """The figures are upper bounds; the output must say so."""
        now = time.time()
        write_transcript(self.projects, "s1", [
            turn(now - 40, cread=0, out=0, tools=["Read"]),
            turn(now - 30, cread=200_000, out=0),
            turn(now - 20, cread=200_000, out=0),
        ])
        out = self.ub.analyze(days=30)
        self.assertIn("AMORTISED CONTEXT COST BY CALLER", out)
        self.assertIn("Read", out)
        self.assertIn("upper bounds", out)
        # The break-even rests on two separate quantities -- the summary that
        # gets generated and the context that survives -- so the output must
        # name both. Asserting the intent rather than the sentence: the wording
        # changed once FINDINGS 25 measured a figure, and again once FINDINGS 32
        # found that figure was a cache-write artifact AND that the summary is
        # not the whole post-compaction context. Prose-pinned assertions would
        # just have to be rewritten next time it is refined.
        self.assertIn("SUMMARY is measured", out)
        self.assertIn("post-compaction CONTEXT", out)
        self.assertIn("FINDINGS 32", out)


# ---------------------------------------------------------------------------
# FINDINGS section 35 -- a rate needs a span
# ---------------------------------------------------------------------------

class TestBurnSampleSpan(TempEnv):
    """The trailing rate divides by the observed span, which can be seconds.

    Real numbers, from the live cache for the 5h window opened 2026-08-22
    10:07:51: the resume turn cost $2.89 and the next landed 14 seconds later.
    """

    RESUME = 2.8924

    def test_one_turn_is_not_an_hourly_rate(self):
        now = time.time()
        rate, span, turns = self.ub._burn_sample([[now - 14, self.RESUME, "m"]], now)
        self.assertEqual(turns, 1)
        self.assertLess(span, 60)
        # Dividing by the true 14s span gives $744/h; the old 1-minute floor
        # gave $173/h. Both are a single turn wearing a per-hour costume.
        self.assertLess(rate, 40.0, "one turn must not annualise")
        self.assertAlmostEqual(
            rate, self.RESUME / (self.ub.BURN_MIN_SPAN_MINUTES / 60.0), places=4)

    def test_the_floor_is_reported_so_callers_can_see_it(self):
        now = time.time()
        _, span, _ = self.ub._burn_sample([[now - 14, self.RESUME, "m"]], now)
        st = self.ub._build_state([[now - 14, self.RESUME, "m", "id1"]])
        self.assertTrue(st["burn"]["floored"])
        self.assertFalse(st["burn"]["measurable"])
        self.assertEqual(st["burn"]["turns"], 1)

    def test_a_real_burst_is_still_measured_at_full_strength(self):
        """The floor must not dampen a rate that has a genuine span behind it --
        that would trade a false alarm for a missed wall."""
        now = time.time()
        burst = [[now - 60 * i, 20.0, "claude-opus-5"] for i in range(1, 31)]
        rate, span, turns = self.ub._burn_sample(sorted(burst), now)
        self.assertGreater(span, 25 * 60)
        self.assertGreater(rate, 500, "$600 over 30m is still a huge real rate")
        self.assertTrue(self.ub._build_state(
            [[e[0], e[1], e[2], f"m{i}"] for i, e in enumerate(burst)]
        )["burn"]["measurable"])

    def test_the_denominator_is_the_span_not_the_whole_window(self):
        """Idle 25 minutes then burn for 5: the rate must reflect the burn, or a
        real spike hides behind the quiet stretch that preceded it."""
        now = time.time()
        ev = [[now - 60 * i, 10.0, "m"] for i in range(1, 6)]
        rate, span, _ = self.ub._burn_sample(sorted(ev), now)
        self.assertLess(span, 6 * 60)
        self.assertGreater(rate, 500, "5 minutes of $50 is $600/h, not $100/h")

    def test_burn_rate_keeps_its_float_signature(self):
        now = time.time()
        self.assertIsInstance(self.ub._burn_rate([[now - 30, 1.0, "m"]], now), float)


if __name__ == "__main__":
    unittest.main(verbosity=2)
