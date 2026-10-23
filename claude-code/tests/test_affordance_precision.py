#!/usr/bin/env python3
"""
Tests for FINDINGS 28: the affordance line has to carry its own precision.

The defect these lock in was observed live rather than theorised. Two prompts in
one session, minutes apart, rendered `~454 usable turns · binding: week pace` and
then `~1,878 usable turns · binding: 5h window`. Cost per turn moved by $0.004.
Everything else was the week's utilisation being estimated on the first turn and
read from the vendor's meter on the second.

That is structural, not a glitch: the status line refreshes the meter when a
response arrives while the block renders on UserPromptSubmit, so the FIRST prompt
after any idle gap longer than METER_MAX_AGE always renders estimated -- which is precisely the turn
a user is deciding what to do next.

The fix is NOT to widen the estimate or to suppress it (the moment the meter is
least available is the moment the number is most needed). It is to stop a soft
number from being printed in a hard typeface.

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

DAY = 86400
WK = 1786975200.0


def load_module(state_dir: Path):
    os.environ["USAGE_BUDGET_HOME"] = str(state_dir)
    import usage_budget
    return importlib.reload(usage_budget)


def weekly(cost, cap=1750.0, exact=False, conf="medium"):
    return {
        "cost_usd": cost, "capacity_usd": cap, "remaining_usd": cap - cost,
        "used_frac": cost / cap, "window_start": WK, "resets_at": WK + 7 * DAY,
        "anchored": True, "exact": exact, "confidence": conf,
    }


def session(remaining=270.0, cap=300.0, exact=False, conf="medium"):
    return {
        "cost_usd": cap - remaining, "capacity_usd": cap, "remaining_usd": remaining,
        "used_frac": (cap - remaining) / cap, "window_start": WK,
        "resets_at": WK + 5 * 3600, "exact": exact, "confidence": conf, "samples": 2,
    }


def events(n=20, usd=0.09):
    return [[WK + i, usd, "claude-opus-5", f"m{i}"] for i in range(n)]


class AffordancePrecisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ub = load_module(Path(self.tmp.name))

    def _state(self, wcost, **kw):
        st = {"session": session(**kw.pop("s", {})),
              "weekly": weekly(wcost, **kw.pop("w", {}))}
        st["pace"] = self.ub._pace(st["weekly"], WK + 1.5 * DAY)
        return st

    def aff(self, st, now=None):
        return self.ub._affordance(st, events(), now or (WK + 1.5 * DAY))

    # --- the point figure survives when the meter backs it -----------------

    def test_probe_backed_affordance_reports_a_point_not_a_range(self):
        st = self._state(200.0, s={"exact": True, "conf": "exact"},
                         w={"exact": True, "conf": "exact"})
        a = self.aff(st)
        self.assertTrue(a["exact"])
        self.assertEqual(a["band"], 0.0)
        self.assertEqual(a["usable_lo"], a["usable_turns"])
        self.assertEqual(a["usable_hi"], a["usable_turns"])
        self.assertTrue(a["binding_stable"])

    def test_exact_render_has_no_estimated_marker(self):
        st = self._state(200.0, s={"exact": True, "conf": "exact"},
                         w={"exact": True, "conf": "exact"})
        st["affordance"] = self.aff(st)
        out = self.ub.render_inject(st)
        self.assertIn("usable turns", out)
        self.assertNotIn("ESTIMATED, not the meter", out)

    # --- and degrades when it does not -------------------------------------

    def test_estimated_affordance_widens_into_a_range(self):
        st = self._state(200.0)
        a = self.aff(st)
        self.assertFalse(a["exact"])
        self.assertGreater(a["band"], 0.0)
        self.assertLess(a["usable_lo"], a["usable_hi"])
        self.assertLessEqual(a["usable_lo"], a["usable_turns"])
        self.assertGreaterEqual(a["usable_hi"], a["usable_turns"])

    def test_one_estimated_bucket_is_enough_to_soften_the_line(self):
        # The ceiling is a MIN() across buckets, so an estimated week can decide
        # the answer even when the 5h window is exact.
        st = self._state(200.0, s={"exact": True, "conf": "exact"})
        self.assertFalse(self.aff(st)["exact"])

    def test_band_tracks_the_weaker_confidence(self):
        low = self.aff(self._state(200.0, s={"conf": "low"}, w={"conf": "low"}))
        high = self.aff(self._state(200.0, s={"conf": "high"}, w={"conf": "high"}))
        self.assertGreater(low["band"], high["band"])
        self.assertEqual(high["band"], self.ub.AFFORDANCE_BAND["high"])

    def test_estimated_render_marks_the_line_itself(self):
        st = self._state(200.0)
        st["affordance"] = self.aff(st)
        out = self.ub.render_inject(st)
        line = next(x for x in out.splitlines() if x.startswith("Affordance:"))
        # The caveat must travel WITH the number, not sit three lines away.
        self.assertIn("ESTIMATED, not the meter", line)
        self.assertIn("plan against the LOW end", line)
        self.assertIn("measured error", line)
        self.assertRegex(line, r"~\d+-\d+ usable turns")

    # --- the regime flip, which is the actual defect -----------------------

    def test_unstable_ceiling_is_called_out_separately(self):
        # Spend parked just under today's prorated share: pace headroom is a
        # difference of two similar numbers, so a few percent of capacity error
        # flips which ceiling binds. This is the 454-vs-1,878 case.
        # day 2/7 of 1750 => $500 allowed, $374 spent => $126 pace headroom,
        # against $150 left in the 5h window. At +20% capacity the pace ceiling
        # rises to $226 and the 5h window (now $210) takes over: same inputs,
        # different question being answered.
        st = self._state(374.0, s={"remaining": 150.0})
        a = self.aff(st)
        self.assertFalse(a["binding_stable"])
        st["affordance"] = a
        line = next(x for x in self.ub.render_inject(st).splitlines()
                    if x.startswith("Affordance:"))
        self.assertIn("binding ceiling itself changes", line)

    def test_stable_ceiling_does_not_cry_wolf(self):
        # Far from the pace boundary the 5h window binds at every scale.
        st = self._state(50.0, s={"remaining": 20.0})
        a = self.aff(st)
        self.assertTrue(a["binding_stable"])

    def test_flip_is_invisible_without_the_band(self):
        # Guards the regression this was written for: at scale 1.0 alone the two
        # regimes are indistinguishable, which is exactly why it shipped.
        st = self._state(374.0, s={"remaining": 150.0})
        a = self.aff(st)
        self.assertIsInstance(a["usable_turns"], int)
        self.assertLess(a["usable_lo"], a["usable_hi"])

    # --- invariants --------------------------------------------------------

    def test_point_estimate_is_unchanged_by_the_band(self):
        # No new over-braking (FINDINGS 13): the stance still keys off the point
        # figure, so widening the report must not move the decision.
        st_e = self._state(200.0, s={"exact": True, "conf": "exact"},
                           w={"exact": True, "conf": "exact"})
        st_c = self._state(200.0)
        self.assertEqual(self.aff(st_e)["usable_turns"],
                         self.aff(st_c)["usable_turns"])
        self.assertEqual(self.aff(st_e)["stance"], self.aff(st_c)["stance"])

    def test_range_never_inverts_or_goes_negative(self):
        for wcost in (10.0, 200.0, 374.0, 500.0, 900.0, 1700.0):
            a = self.aff(self._state(wcost))
            if a is None:
                continue
            self.assertLessEqual(a["usable_lo"], a["usable_hi"], wcost)
            self.assertGreaterEqual(a["usable_lo"], 0, wcost)
            self.assertLessEqual(a["fanouts_lo"], a["fanouts_hi"], wcost)

    def test_measured_drift_widens_the_band_past_calibration_spread(self):
        # Calibration spread says how much the samples disagree with EACH OTHER.
        # Drift says how wrong they were against the vendor's meter. Observed
        # live 2026-08-18: weekly spread was "medium" (<=20%) while the measured
        # residual was -31.6%, so the spread band alone did not contain the true
        # answer. The wider of the two has to win.
        self.ub._record_drift("weekly", -31.6, WK)
        st = self._state(200.0, s={"conf": "medium"}, w={"conf": "medium"})
        a = self.aff(st)
        self.assertGreater(a["band"], self.ub.AFFORDANCE_BAND["medium"])
        self.assertAlmostEqual(a["band"], 0.316, places=3)

    def test_drift_log_is_bounded_and_survives_a_corrupt_file(self):
        self.ub.DRIFT_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.ub.DRIFT_PATH.write_text("{ not json")
        self.ub._record_drift("weekly", 5.0, WK)          # must not raise
        for i in range(self.ub.DRIFT_KEEP + 8):
            self.ub._record_drift("weekly", float(i), WK + i)
        import json as _json
        rows = _json.loads(self.ub.DRIFT_PATH.read_text())["weekly"]
        self.assertEqual(len(rows), self.ub.DRIFT_KEEP)

    def test_no_drift_history_falls_back_to_the_spread_band(self):
        st = self._state(200.0, s={"conf": "high"}, w={"conf": "high"})
        self.assertEqual(self.aff(st)["band"], self.ub.AFFORDANCE_BAND["high"])

    def test_extremes_come_from_off_diagonal_corners(self):
        # The regression this refactor exists for. With a single shared scale the
        # reported ceiling stayed on "week pace" at both ends and the range came
        # out 314-1,422, missing a true answer of 1,527. The maximum actually
        # sits where the 5h capacity is HIGH and the weekly capacity is LOW:
        # a low weekly capacity pushes spend past today's prorated share, which
        # flips into borrow-ahead mode and hands the ceiling to the 5h window.
        self.ub._record_drift("session", -9.9, WK)
        self.ub._record_drift("weekly", -31.6, WK)
        st = self._state(453.0, s={"remaining": 240.0, "cap": 300.0})
        a = self.aff(st)
        self.assertNotAlmostEqual(a["band_session"], a["band_weekly"])
        shared = self._shared_scale_range(st, a["band"])
        self.assertGreater(a["usable_hi"], shared,
                           "per-bucket corners must reach further than a "
                           "single shared scale, or the refactor bought nothing")

    def _shared_scale_range(self, st, band):
        """Highest usable-turn count a single shared scale can reach."""
        a = self.aff(st)
        per_turn = a["per_turn_usd"]
        best = 0
        for sc in (1.0 - band, 1.0 + band):
            pace = st["pace"]
            sess = max(0.0, st["session"]["capacity_usd"] * sc
                       - st["session"]["cost_usd"])
            head = pace["allowance_usd"] * sc - pace["used_usd"]
            if head <= 0:
                head = max(0.0, st["weekly"]["capacity_usd"] * sc
                           - st["weekly"]["cost_usd"])
            h = min(sess, head)
            t = int(h / per_turn)
            r = max(self.ub.LANDING_RESERVE_MIN_TURNS,
                    int(t * self.ub.LANDING_RESERVE_FRAC))
            best = max(best, max(0, t - r))
        return best

    def test_weaker_confidence_helper(self):
        f = self.ub._weaker_confidence
        self.assertEqual(f({"confidence": "exact"}, {"confidence": "low"}), "low")
        self.assertEqual(f({"confidence": "high"}, {"confidence": "medium"}), "medium")
        self.assertEqual(f({}, {"confidence": "high"}), "none")
        self.assertEqual(f({"confidence": "bogus"}, {"confidence": "high"}), "none")


if __name__ == "__main__":
    unittest.main()
