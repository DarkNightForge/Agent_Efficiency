#!/usr/bin/env python3
"""
Tests for the FINDINGS 13 + 14 additions: weekly pacing, affordance, the pace
checkpoint, and unpriced-model detection.

Kept in its own file so this half can be built in parallel with the collector
half (AGENTS.md: split ownership by file).

The behaviour these lock in is asymmetric on purpose. Under-spending a
subscription is a real cost, so GO WIDE must actually fire when there is room;
over-spending must warn WITHOUT zeroing out reported capacity, because an agent
that reads "0 turns left" refuses work the user has already paid for.

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

DAY = 86400
WK = 1786975200.0  # a Monday 07:00 weekly anchor


def load_module(state_dir: Path):
    os.environ["USAGE_BUDGET_HOME"] = str(state_dir)
    import usage_budget
    return importlib.reload(usage_budget)


def weekly(cost, cap=1750.0, anchored=True):
    return {
        "cost_usd": cost, "capacity_usd": cap, "remaining_usd": cap - cost,
        "used_frac": cost / cap, "window_start": WK, "resets_at": WK + 7 * DAY,
        "anchored": anchored,
    }


def session(remaining=270.0, cap=300.0, conf="medium"):
    return {
        "cost_usd": cap - remaining, "capacity_usd": cap, "remaining_usd": remaining,
        "used_frac": (cap - remaining) / cap, "window_start": WK,
        "resets_at": WK + 5 * 3600, "confidence": conf, "samples": 2,
    }


def events(n=20, usd=0.09):
    return [[WK + i, usd, "claude-opus-5", f"m{i}"] for i in range(n)]


class PaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_day_one_grants_one_seventh_immediately(self):
        """Day-granular, not continuous.

        A continuous prorate would allow 1/168th of the week an hour into
        Monday, putting a normal morning instantly "over pace". Granting the
        whole of day N's share when day N opens is what a working day looks
        like.
        """
        p = self.ub._pace(weekly(0.0), WK + 3600)
        self.assertEqual(p["day_index"], 1)
        self.assertAlmostEqual(p["allowance_usd"], 1750.0 / 7, places=2)

    def test_day_index_advances_and_allowance_accumulates(self):
        for elapsed_days, expected in ((0.5, 1), (1.2, 2), (3.9, 4), (6.5, 7)):
            p = self.ub._pace(weekly(10.0), WK + elapsed_days * DAY)
            self.assertEqual(p["day_index"], expected, f"{elapsed_days}d")
            self.assertAlmostEqual(
                p["allowance_usd"], 1750.0 * expected / 7, places=2)

    def test_day_index_never_exceeds_the_week(self):
        """A late reset or a clock skew must not grant an 8/7 allowance."""
        p = self.ub._pace(weekly(10.0), WK + 12 * DAY)
        self.assertEqual(p["day_index"], 7)
        self.assertAlmostEqual(p["allowance_usd"], 1750.0, places=2)

    def test_over_pace_projects_the_day_the_week_runs_dry(self):
        # $900 over 2.5 ELAPSED days => $360/day => 1750/360 = 4.9 => dry day 5.
        # The ratio still divides by day_index (3), so 120% is unchanged: the
        # allowance stays day-granular while the rate does not.
        p = self.ub._pace(weekly(900.0), WK + 2.5 * DAY)
        self.assertTrue(p["over"])
        self.assertAlmostEqual(p["ratio"], 1.2, places=3)
        self.assertAlmostEqual(p["per_day_usd"], 360.0, places=2)
        self.assertEqual(p["exhaust_day"], 5)

    def test_rate_uses_elapsed_time_while_the_allowance_uses_the_day_index(self):
        """The two denominators are different on purpose.

        `used / day_index` divides by up to a whole unelapsed day, so it
        understated the burn and pushed the dry day later -- worst in the
        morning, i.e. worst when there was still a day left to act on it.
        """
        p = self.ub._pace(weekly(900.0), WK + 2.04 * DAY)   # 08:00 on day 3
        self.assertEqual(p["day_index"], 3)
        self.assertAlmostEqual(p["allowance_usd"], 1750.0 * 3 / 7, places=2)
        self.assertAlmostEqual(p["per_day_usd"], 900.0 / 2.04, places=2)
        self.assertNotAlmostEqual(p["per_day_usd"], 900.0 / 3, places=2)

    def test_the_rate_floor_leaves_day_one_untouched(self):
        """The floor is 1.0 BECAUSE that is day 1's day_index.

        Any smaller floor would let a fraction-of-a-day sample extrapolate a
        dry day, so a quiet Monday morning would render "under pace" and "runs
        dry on day 5" in the same block -- the self-contradiction this whole
        area exists to avoid.
        """
        for hours in (0.1, 0.5, 0.9):
            p = self.ub._pace(weekly(200.0), WK + hours * DAY)
            self.assertEqual(p["day_index"], 1, f"{hours}d")
            self.assertAlmostEqual(p["per_day_usd"], 200.0, places=4, msg=f"{hours}d")
            self.assertIsNone(p["exhaust_day"], f"{hours}d")

    def test_a_stale_anchor_cannot_dilute_the_rate(self):
        """Clamped at the top like day_index, for the same reason."""
        p = self.ub._pace(weekly(1400.0), WK + 12 * DAY)
        self.assertAlmostEqual(p["per_day_usd"], 1400.0 / 7, places=2)

    def test_on_pace_projects_no_exhaustion(self):
        # $200 against day 1's $250 share -- deliberately not $250, which is
        # exactly the share and therefore already "over" by the >= rule.
        p = self.ub._pace(weekly(200.0), WK + 0.5 * DAY)
        self.assertFalse(p["over"])
        self.assertIsNone(p["exhaust_day"])

    def test_unanchored_week_has_no_pace(self):
        """A rolling 7d figure spans the wrong days, so prorating it is a lie."""
        self.assertIsNone(self.ub._pace(weekly(100.0, anchored=False), WK + DAY))

    def test_uncalibrated_week_has_no_pace(self):
        w = weekly(100.0)
        w["capacity_usd"] = None
        self.assertIsNone(self.ub._pace(w, WK + DAY))


class AffordanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, wk_cost, elapsed_days, sess_remaining=270.0, ev=None):
        st = {"session": session(sess_remaining), "weekly": weekly(wk_cost)}
        st["pace"] = self.ub._pace(st["weekly"], WK + elapsed_days * DAY)
        st["affordance"] = self.ub._affordance(
            st, ev if ev is not None else events(), WK + elapsed_days * DAY)
        return st

    def test_median_not_mean_resists_the_skill_load_outlier(self):
        """A session's first turn carries skill loads at 3-4x a working turn.

        A mean would let that one record depress the turn count for the rest of
        the session; the median must ignore it.
        """
        ev = events(19) + [[WK + 99, 3.00, "claude-opus-5", "big"]]
        a = self.build(10.0, 0.5, ev=ev)["affordance"]
        self.assertAlmostEqual(a["per_turn_usd"], 0.09, places=4)

    def test_too_few_turns_yields_no_affordance(self):
        """Two samples cannot support a median, so report nothing."""
        st = {"session": session(), "weekly": weekly(10.0)}
        st["pace"] = self.ub._pace(st["weekly"], WK + DAY)
        self.assertIsNone(self.ub._affordance(st, events(2), WK + DAY))

    def test_go_wide_fires_when_room_is_ample_and_week_is_under_pace(self):
        a = self.build(10.0, 0.5)["affordance"]
        self.assertTrue(a["go_wide"])
        self.assertGreater(a["fanouts_left"], 3)

    def test_go_wide_suppressed_when_week_is_at_pace_even_with_a_full_window(self):
        """The 5h window being roomy is not permission to burn the week."""
        a = self.build(1750.0 * 0.9, 0.5)["affordance"]
        self.assertFalse(a["go_wide"])

    def test_go_wide_suppressed_without_calibration(self):
        """With no denominator there is no headroom to be confident about."""
        st = {"session": session(conf="none"), "weekly": weekly(10.0)}
        st["pace"] = self.ub._pace(st["weekly"], WK + 0.5 * DAY)
        a = self.ub._affordance(st, events(), WK + 0.5 * DAY)
        self.assertFalse(a["go_wide"])

    def test_over_pace_reports_real_capacity_not_zero(self):
        """The regression this whole feature could have shipped.

        Past today's prorated share the pace ceiling is spent, but the WEEK is
        not. Reporting 0 turns would make the agent refuse work the user has
        paid for -- the same failure that made the lockout forecast get
        rewritten after an agent cut scope with 53% unspent.
        """
        a = self.build(900.0, 2.5)["affordance"]
        self.assertTrue(a["borrowing"])
        self.assertGreater(a["turns_left"], 1000)
        self.assertGreater(a["fanouts_left"], 0)
        self.assertFalse(a["go_wide"])

    def test_pace_headroom_binds_before_the_session_window(self):
        """When today's share is the tighter ceiling, it is the one reported."""
        a = self.build(1750.0 / 7 - 20.0, 0.5, sess_remaining=290.0)["affordance"]
        self.assertEqual(a["binding_bucket"], "week pace")
        self.assertLess(a["headroom_usd"], 290.0)


class PaceAskGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def state(self, wk_cost, elapsed_days):
        st = {"session": session(), "weekly": weekly(wk_cost)}
        st["pace"] = self.ub._pace(st["weekly"], WK + elapsed_days * DAY)
        return st

    def test_no_ask_while_under_pace(self):
        st = self.state(10.0, 0.5)
        self.ub._pace_ask_gate(st)
        self.assertNotIn("pace_ask", st)

    def test_asks_once_then_stays_quiet_the_same_day(self):
        """A checkpoint that fires every turn gets approved reflexively."""
        st = self.state(900.0, 2.5)
        self.ub._pace_ask_gate(st)
        self.assertTrue(st.get("pace_ask"))
        for _ in range(5):
            st.pop("pace_ask", None)
            self.ub._pace_ask_gate(st)
            self.assertNotIn("pace_ask", st)

    def test_reasks_when_the_overshoot_materially_worsens(self):
        st = self.state(900.0, 2.5)          # ratio 1.20
        self.ub._pace_ask_gate(st)
        self.assertTrue(st.pop("pace_ask"))
        worse = self.state(1100.0, 2.5)      # ratio 1.47, +0.27 > 0.25 step
        self.ub._pace_ask_gate(worse)
        self.assertTrue(worse.get("pace_ask"))

    def test_does_not_reask_for_a_trivial_worsening(self):
        st = self.state(900.0, 2.5)
        self.ub._pace_ask_gate(st)
        nudge = self.state(950.0, 2.5)       # ratio 1.27, +0.07 < 0.25 step
        self.ub._pace_ask_gate(nudge)
        self.assertNotIn("pace_ask", nudge)

    def test_asks_again_on_a_new_day(self):
        st = self.state(900.0, 2.5)
        self.ub._pace_ask_gate(st)
        # Day 4's share is $1000, so the figure has to clear that to still be
        # over pace on the new day -- otherwise the gate correctly stays quiet.
        nxt = self.state(1200.0, 3.5)        # day 4, ratio 1.20
        self.ub._pace_ask_gate(nxt)
        self.assertTrue(nxt.get("pace_ask"))

    def test_asks_again_in_a_new_week(self):
        st = self.state(900.0, 2.5)
        self.ub._pace_ask_gate(st)
        nxt = self.state(900.0, 2.5)
        nxt["weekly"]["window_start"] = WK + 7 * DAY
        nxt["pace"] = self.ub._pace(nxt["weekly"], WK + 7 * DAY + 2.5 * DAY)
        self.ub._pace_ask_gate(nxt)
        self.assertTrue(nxt.get("pace_ask"))


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def render(self, wk_cost, elapsed_days, ask=False):
        st = {"session": session(), "weekly": weekly(wk_cost),
              "burn_usd_per_hour": 0.0}
        st["pace"] = self.ub._pace(st["weekly"], WK + elapsed_days * DAY)
        st["affordance"] = self.ub._affordance(
            st, events(), WK + elapsed_days * DAY)
        if ask:
            self.ub._pace_ask_gate(st)
        return self.ub.render_inject(st)

    def test_go_wide_line_tells_the_agent_not_to_economise(self):
        out = self.render(10.0, 0.5)
        self.assertIn("GO WIDE:", out)
        self.assertIn("expires at the reset", out)
        self.assertNotIn("ASK BEFORE PROCEEDING", out)

    def test_ask_line_names_the_tool_and_the_three_options(self):
        out = self.render(900.0, 2.5, ask=True)
        self.assertIn("ASK BEFORE PROCEEDING:", out)
        self.assertIn("AskUserQuestion", out)
        self.assertIn("continue at full scope", out)
        self.assertIn("stop and resume after the reset", out)

    def test_ask_line_is_framed_as_a_checkpoint_not_a_refusal(self):
        """Informing must never become refusing; the budget is the user's."""
        out = self.render(900.0, 2.5, ask=True)
        self.assertIn("NOT a refusal", out)
        self.assertNotIn("GO WIDE:", out)
        # ...and the affordance line must still show real capacity alongside it.
        self.assertIn("BORROWING from later days", out)

    def test_the_dry_day_and_the_days_left_sum_to_the_week(self):
        """REGRESSION: days-left was measured from today, not the dry day.

        `left` was `days - day_index`, which welded two different origins into
        one sentence: "runs dry on day 6/7 with 4 day(s) still to go" describes
        a 10-day week. It overstated the blocked stretch by the days already
        elapsed -- so the error grew as day_index grew, i.e. it was worst
        exactly when the checkpoint mattered most.
        """
        out = self.render(900.0, 2.5, ask=True)
        m = re.search(r"runs dry on day (\d+)/(\d+), "
                      r"blocking the last (\d+) day\(s\)", out)
        self.assertIsNotNone(m, f"projection clause missing or reworded:\n{out}")
        dry, days, left = (int(g) for g in m.groups())
        self.assertEqual(dry + left, days,
                         "dry day + days blocked must span exactly the week")
        self.assertEqual((dry, left), (5, 2))   # $360/day against a $1750 cap
        self.assertNotIn("still to go", out)    # the old, contradictory phrasing

    def test_pace_line_states_the_day_and_the_share(self):
        self.assertIn("Week pace: day 3/7", self.render(900.0, 2.5))
        self.assertIn("OVER PACE", self.render(900.0, 2.5))
        self.assertIn("under pace", self.render(10.0, 0.5))

    def test_affordance_line_is_in_turns_and_fanouts_not_only_dollars(self):
        out = self.render(10.0, 0.5)
        self.assertIn("usable turns at $", out)
        self.assertIn("held back to stop cleanly", out)
        self.assertIn("fan-out costs ~$", out)

    def test_block_is_silent_about_pacing_when_uncalibrated(self):
        st = {"session": session(conf="none"), "weekly": weekly(10.0, anchored=False),
              "burn_usd_per_hour": 0.0}
        st["pace"] = self.ub._pace(st["weekly"], WK + DAY)
        st["affordance"] = self.ub._affordance(st, events(), WK + DAY)
        out = self.ub.render_inject(st)
        self.assertNotIn("Week pace:", out)
        self.assertNotIn("GO WIDE:", out)


class PostureTest(unittest.TestCase):
    """FINDINGS 20. One baked-in policy fits nobody exactly.

    A user paying for maximum throughput does not want pacing; a user rationing
    a plan across a week wants more warning than the default. That is a value
    judgement, so it is a stored preference -- unlike the wall-vs-price question,
    which is a fact about the account, declared once rather than chosen.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def render(self, posture, wk_cost=10.0, elapsed=0.5):
        st = {"session": session(), "weekly": weekly(wk_cost),
              "burn_usd_per_hour": 0.0, "posture": posture}
        st["pace"] = self.ub._pace(st["weekly"], WK + elapsed * DAY)
        st["affordance"] = self.ub._affordance(st, events(), WK + elapsed * DAY)
        return st, self.ub.render_inject(st)

    def test_unknown_or_missing_posture_falls_back_to_balanced(self):
        for bad in ({}, {"posture": "aggressive"}, {"posture": None}):
            self.assertEqual(self.ub._posture(bad), "balanced")

    def test_throughput_suppresses_pacing_and_always_goes_wide(self):
        st, out = self.render("throughput")
        self.assertIn("Posture: THROUGHPUT", out)
        self.assertNotIn("Week pace:", out)
        self.assertTrue(st["affordance"]["go_wide"])

    def test_throughput_go_wide_states_the_real_reason(self):
        """It fires unconditionally here, so the usual justification is false."""
        _, out = self.render("throughput", wk_cost=1700.0, elapsed=6.0)
        self.assertIn("GO WIDE: the user has chosen speed over spend", out)
        self.assertNotIn("the week is under pace. Prefer", out)

    def test_stretch_never_goes_wide_however_much_room(self):
        st, out = self.render("stretch")
        self.assertFalse(st["affordance"]["go_wide"])
        self.assertNotIn("GO WIDE", out)
        self.assertIn("Posture: STRETCH", out)

    def test_balanced_is_silent_about_posture(self):
        """The default needs no announcement; saying so every turn is noise."""
        _, out = self.render("balanced")
        self.assertNotIn("Posture:", out)

    def test_checkpoint_thresholds_move_with_posture(self):
        self.assertIsNone(self.ub._ask_ratio("throughput"), "never interrupt")
        self.assertEqual(self.ub._ask_ratio("stretch"), 0.80)
        self.assertEqual(self.ub._ask_ratio("balanced"), self.ub.PACE_ASK_RATIO)

    def test_throughput_never_fires_the_checkpoint(self):
        st = {"session": session(), "weekly": weekly(1700.0), "posture": "throughput"}
        st["pace"] = self.ub._pace(st["weekly"], WK + 1.0 * DAY)
        self.assertTrue(st["pace"]["over"], "genuinely over pace")
        self.ub._pace_ask_gate(st)
        self.assertNotIn("pace_ask", st)


class OverageReframeTest(unittest.TestCase):
    """A cap is a wall or a price, and the account's declaration says which."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def state(self, overage_enabled):
        return {"session": session(), "weekly": weekly(1500.0),
                "burn_usd_per_hour": 90.0, "posture": "balanced",
                "overage": {"enabled": overage_enabled},
                "binding": {"bucket": "week", "hours_to_cap": 2.7,
                            "hours_to_reset": 132.0, "blocked_hours": 129.3}}

    def test_without_credits_the_lockout_language_stands(self):
        st = self.state(False)
        st["pace"] = self.ub._pace(st["weekly"], WK + 1.5 * DAY)
        out = self.ub.render_inject(st)
        self.assertIn("WOULD RUN OUT", out)
        self.assertNotIn("COST AHEAD", out)

    def test_with_credits_a_cap_is_reported_as_cost_not_blockage(self):
        """Telling someone with credits they'll be BLOCKED is factually wrong."""
        st = self.state(True)
        st["cost_ahead"] = {"bucket": "week", "hours_to_cap": 2.7}
        st["binding"] = None
        st["pace"] = self.ub._pace(st["weekly"], WK + 1.5 * DAY)
        out = self.ub.render_inject(st)
        self.assertIn("COST AHEAD (not a lockout)", out)
        self.assertNotIn("WOULD RUN OUT", out)
        self.assertNotIn("BLOCKED", out)


class BindingHysteresisTest(unittest.TestCase):
    """E12: the promoted directive must not lead every single turn.

    Observed live within an hour of shipping FINDINGS 17: four consecutive
    replies opened with the same sentence about a forecast that had barely
    moved. The ask checkpoint already had this gate; the louder line shipped
    without it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def state(self, blocked=10.0, bucket="week"):
        return {"session": session(), "weekly": weekly(1500.0),
                "burn_usd_per_hour": 90.0, "posture": "balanced",
                "binding": {"bucket": bucket, "hours_to_cap": 3.0,
                            "hours_to_reset": 13.0, "blocked_hours": blocked}}

    def test_speaks_the_first_time(self):
        st = self.state()
        self.ub._binding_ack_gate(st)
        self.assertNotIn("binding_quiet", st)

    def test_then_stays_quiet_on_an_unchanged_forecast(self):
        self.ub._binding_ack_gate(self.state())
        for _ in range(4):
            st = self.state()
            self.ub._binding_ack_gate(st)
            self.assertTrue(st.get("binding_quiet"))

    def test_speaks_again_when_materially_worse(self):
        self.ub._binding_ack_gate(self.state(blocked=10.0))
        st = self.state(blocked=20.0)          # 2x, past the 1.5x step
        self.ub._binding_ack_gate(st)
        self.assertNotIn("binding_quiet", st)

    def test_speaks_again_for_a_different_bucket(self):
        self.ub._binding_ack_gate(self.state(bucket="week"))
        st = self.state(bucket="premium week")
        self.ub._binding_ack_gate(st)
        self.assertNotIn("binding_quiet", st)

    def test_a_quiet_turn_renders_no_directive(self):
        self.ub._binding_ack_gate(self.state())
        st = self.state()
        st["pace"] = self.ub._pace(st["weekly"], WK + 1.5 * DAY)
        self.ub._binding_ack_gate(st)
        self.assertNotIn("WOULD RUN OUT", self.ub.render_inject(st))


class BindingPromotionTest(unittest.TestCase):
    """FINDINGS 17 / E10: the lockout warning is the FIRST line and imperative.

    Measured A/B, n=24 per arm, Sonnet: buried-and-advisory was obeyed 7/24
    (29%); first-and-imperative 22/24 (92%), Fisher one-sided p=0.00001. The
    shape is copied from the ASK checkpoint, which already passed every run.

    Position is load-bearing, so it is asserted, not just presence.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def render(self, binding=True, burn=90.0):
        st = {"session": session(), "weekly": weekly(1500.0),
              "burn_usd_per_hour": burn}
        st["pace"] = self.ub._pace(st["weekly"], WK + 1.5 * DAY)
        if binding:
            st["binding"] = {"bucket": "week", "hours_to_cap": 2.7,
                             "hours_to_reset": 132.0, "blocked_hours": 129.3}
        return self.ub.render_inject(st)

    def test_the_warning_is_the_first_line_after_the_tag(self):
        lines = self.render().split("\n")
        self.assertTrue(lines[1].startswith("WOULD RUN OUT"),
                        f"must lead the block, got: {lines[1][:60]!r}")

    def test_it_is_imperative_not_advisory(self):
        out = self.render()
        self.assertIn("TELL THE USER THIS FIRST", out)
        self.assertNotIn("Worth reshaping the work", out)

    def test_it_enumerates_options_rather_than_asking_for_invention(self):
        out = self.render()
        self.assertIn("continue at full scope", out)
        self.assertIn("cheaper model", out)
        self.assertIn("after the reset", out)

    def test_it_is_not_a_refusal(self):
        """Same guarantee as the ASK checkpoint: the budget is the user's."""
        self.assertIn("NOT a refusal", self.render())

    def test_it_appears_exactly_once(self):
        """It was moved, not duplicated -- the old site must be gone."""
        self.assertEqual(self.render().count("WOULD RUN OUT"), 1)

    def test_absent_when_nothing_binds(self):
        self.assertNotIn("WOULD RUN OUT", self.render(binding=False))

    def test_a_small_distant_lockout_stays_buried(self):
        """Leading every block with an imperative is how it earns its ignoring.

        Observed live: a 1h57m block forecast 151 hours out, off a rate measured
        minutes into a fresh window, took the top line.
        """
        st = {"session": session(), "weekly": weekly(1500.0),
              "burn_usd_per_hour": 50.0,
              "binding": {"bucket": "week", "hours_to_cap": 151.6,
                          "hours_to_reset": 153.6, "blocked_hours": 1.95}}
        st["pace"] = self.ub._pace(st["weekly"], WK + 1.5 * DAY)
        out = self.ub.render_inject(st)
        self.assertNotIn("TELL THE USER THIS FIRST", out)
        self.assertIn("Heads-up:", out)

    def test_a_large_lockout_is_promoted_however_far_out(self):
        """Read the lockout, not the cap.

        A first cut of the horizon gate demoted an 88-hour block because the cap
        was 56 hours away. Distance does not make three days of lockout small.
        """
        st = {"session": session(), "weekly": weekly(1500.0),
              "burn_usd_per_hour": 75.0,
              "binding": {"bucket": "premium week", "hours_to_cap": 56.0,
                          "hours_to_reset": 144.0, "blocked_hours": 88.0}}
        st["pace"] = self.ub._pace(st["weekly"], WK + 1.5 * DAY)
        out = self.ub.render_inject(st)
        self.assertIn("TELL THE USER THIS FIRST", out)
        self.assertNotIn("Heads-up:", out)

    def test_absent_without_a_burn_rate(self):
        """No rate means no forecast, so there is nothing to warn about."""
        self.assertNotIn("WOULD RUN OUT", self.render(burn=0.0))


class FanoutConstantTest(unittest.TestCase):
    """The fan-out price must stay on the expensive side of the measurement.

    Measured 2026-08-17: subagent turns run at 0.414x a main-loop turn (16,409
    main vs 10,551 subagent turns). The constant is rounded UP from that, because
    the error is asymmetric -- over-pricing a fan-out only under-promises how many
    fit, while under-pricing one walks into a lockout.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_multiplier_is_at_or_above_the_measured_ratio(self):
        self.assertGreaterEqual(self.ub.FANOUT_TURN_MULT, 0.414,
                                "must never under-price a subagent turn")

    def test_multiplier_is_not_wildly_conservative(self):
        """0.6 was the old guess; being 45% over the measurement is not caution."""
        self.assertLess(self.ub.FANOUT_TURN_MULT, 0.55)

    def test_a_fanout_still_costs_less_than_the_same_turns_on_the_main_loop(self):
        st = {"session": session(), "weekly": weekly(10.0)}
        st["pace"] = self.ub._pace(st["weekly"], WK + 0.5 * DAY)
        a = self.ub._affordance(st, events(), WK + 0.5 * DAY)
        naive = self.ub.FANOUT_AGENTS * self.ub.FANOUT_TURNS_EACH * a["per_turn_usd"]
        self.assertLess(a["fanout_usd"], naive)


class UnpricedModelTest(unittest.TestCase):
    """FINDINGS 7: a model this build cannot price must never be silent.

    Two problems share one signal — its cost is guessed at Opus-tier fallback,
    and it may be scored against a per-model weekly cap the tool cannot see.
    The status line does not carry those caps, so the only honest option is to
    say the model went unrecognised.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))
        # `_build_state` reads the real clock and drops every event outside the
        # current weekly window before it checks PRICES. A fixed fixture stamp
        # therefore ages out and starves this class silently; WK (a Monday in
        # August) had done exactly that. Anchor to now, an hour back so the
        # events cannot land in the future.
        self.t0 = time.time() - 3600

    def _state(self, events):
        """Build state, and prove the events were actually inside the window.

        Every assertion here reads `unknown_models`, and an empty list is also
        what a state that observed NO events reports. Without this guard the
        two negative tests pass vacuously -- which is how the fixture drift
        stayed invisible until the positive test finally went red.
        """
        st = self.ub._build_state(events)
        self.assertGreater(
            st["weekly"]["cost_usd"], 0.0,
            "fixture aged out of the weekly window: no event was counted, so "
            "the unknown-model assertions below would pass vacuously")
        return st

    def tearDown(self):
        self.tmp.cleanup()

    def test_known_models_produce_no_warning(self):
        st = self._state(
            [[self.t0 + i, 0.09, "claude-opus-5", f"m{i}"] for i in range(10)])
        self.assertEqual(st["unknown_models"], [])
        self.assertNotIn("UNPRICED", self.ub.render_inject(st))

    def test_an_unpriced_model_is_named(self):
        ev = [[self.t0 + i, 0.09, "claude-opus-5", f"m{i}"] for i in range(9)]
        ev.append([self.t0 + 9, 0.5, "claude-brandnew-6", "mX"])
        st = self._state(ev)
        self.assertIn("claude-brandnew-6", st["unknown_models"])

    def test_the_warning_names_the_model_and_the_remedy(self):
        st = {"session": session(), "weekly": weekly(10.0),
              "burn_usd_per_hour": 0.0, "unknown_models": ["claude-brandnew-6"]}
        out = self.ub.render_inject(st)
        self.assertIn("UNPRICED MODEL", out)
        self.assertIn("claude-brandnew-6", out)
        self.assertIn("premium_models", out)

    def test_fast_variants_of_known_models_are_not_flagged(self):
        """`:fast` keys are real PRICES entries, not unknown models."""
        st = self._state(
            [[self.t0 + i, 0.2, "claude-opus-5:fast", f"m{i}"] for i in range(5)])
        self.assertEqual(st["unknown_models"], [])

    def test_plural_agrees_with_count(self):
        one = self.ub.render_inject(
            {"session": session(), "weekly": weekly(10.0),
             "burn_usd_per_hour": 0.0, "unknown_models": ["a"]})
        two = self.ub.render_inject(
            {"session": session(), "weekly": weekly(10.0),
             "burn_usd_per_hour": 0.0, "unknown_models": ["a", "b"]})
        self.assertIn("UNPRICED MODEL seen", one)
        self.assertIn("UNPRICED MODELS seen", two)


if __name__ == "__main__":
    unittest.main()


class ThinBurnSampleTest(unittest.TestCase):
    """FINDINGS 35: a lockout forecast may not lead the block off two turns.

    Observed live 2026-08-22, replayed from the real cache: a resumed session's
    first turn cost $2.89 -- 68% of the whole window -- because a resume writes
    its cache prefix instead of reading it. Divided by a 14-second span that
    annualised to $173/h and promoted a 1h12m lockout. Turn by turn, the
    pre-fix arithmetic promoted on 8 of the window's first 8 turns.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def state(self, span_s, turns, bucket="5h window"):
        return {
            "session": session(), "weekly": weekly(1500.0),
            "burn_usd_per_hour": 90.0, "posture": "balanced",
            "burn": {
                "span_seconds": span_s, "turns": turns, "window_minutes": 30,
                "floored": span_s < self.ub.BURN_MIN_SPAN_MINUTES * 60,
                "measurable": (
                    span_s >= self.ub.PROMOTE_MIN_BURN_SPAN_MINUTES * 60
                    and turns >= self.ub.PROMOTE_MIN_BURN_TURNS),
            },
            "binding": {"bucket": bucket, "hours_to_cap": 1.2,
                        "hours_to_reset": 4.87, "blocked_hours": 3.67},
        }

    def test_a_fresh_window_does_not_open_the_reply(self):
        out = self.ub.render_inject(self.state(14, 1))
        self.assertNotIn("WOULD RUN OUT", out)

    def test_but_it_is_still_reported_quietly(self):
        """Demote, never suppress -- the lockout may yet be real."""
        out = self.ub.render_inject(self.state(14, 1))
        self.assertIn("Heads-up:", out)
        self.assertIn("would cap in", out)

    def test_the_label_admits_the_sample_is_extrapolated(self):
        out = self.ub.render_inject(self.state(14, 1))
        self.assertNotIn("(last 30m)", out)
        self.assertIn("extrapolated", out)

    def test_a_measured_sample_still_promotes(self):
        """The gate must not silence a lockout that IS measured."""
        out = self.ub.render_inject(self.state(22 * 60, 40))
        self.assertIn("WOULD RUN OUT", out)

    def test_turn_count_alone_is_not_enough(self):
        """40 turns in 90 seconds is a burst, not a five-hour trend."""
        out = self.ub.render_inject(self.state(90, 40))
        self.assertNotIn("WOULD RUN OUT", out)

    def test_span_alone_is_not_enough(self):
        out = self.ub.render_inject(self.state(20 * 60, 2))
        self.assertNotIn("WOULD RUN OUT", out)

    def test_the_weekly_bucket_is_never_gated_on_this(self):
        """The week runs on a week average; its span is never in doubt. Gating
        it on the trailing sample would suppress a real multi-day lockout."""
        st = self.state(14, 1, bucket="week")
        st["binding"]["blocked_hours"] = 129.3
        st["binding"]["hours_to_reset"] = 132.0
        st["pace"] = self.ub._pace(st["weekly"], WK + 1.5 * DAY)
        self.assertIn("WOULD RUN OUT", self.ub.render_inject(st))

    def test_state_without_a_burn_block_is_not_gated(self):
        """Forward compatibility: an older state.json has no evidence of
        thinness, so it must behave exactly as it did before."""
        st = self.state(14, 1)
        del st["burn"]
        self.assertIn("WOULD RUN OUT", self.ub.render_inject(st))
