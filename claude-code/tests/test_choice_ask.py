#!/usr/bin/env python3
"""
Tests for the choice gate: offering the stop/shrink/continue fork ONCE.

Two directives enumerate mutually exclusive options -- AT LIMIT and WILL NOT
FINISH MUCH. Both used to say "state it, then offer ...", and an agent reading
that writes prose. Prose is a bad instrument for a fork: it asks the user to
compose a sentence mid-task, at the moment they are least willing to.

They now instruct AskUserQuestion instead. The whole risk of that change is
repetition -- MEASURED 2026-08-19, both directives re-render on EVERY turn while
their condition holds, so a naive change would have produced a modal per turn.
A question that fires every turn gets dismissed reflexively, which is worse than
prose because it looks like consent.

So the property under test is not "does it ask" but "does it ask exactly once,
and do the facts survive after it stops asking".

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))


def load_module(state_dir: Path):
    os.environ["USAGE_BUDGET_HOME"] = str(state_dir)
    import usage_budget
    return importlib.reload(usage_budget)


class ChoiceGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ub = load_module(Path(self.tmp.name))
        self.now = time.time()

    def state(self, *, stance=None, used=0.5, window=None):
        w = self.now - 3600 if window is None else window
        return {
            "session": {"window_start": w, "used_frac": used,
                        "capacity_usd": 100.0, "remaining_usd": 5.0,
                        "cost_usd": 95.0, "resets_at": self.now + 3600},
            "weekly": {"window_start": self.now - 86400, "used_frac": 0.4,
                       "capacity_usd": 700.0, "remaining_usd": 420.0,
                       "cost_usd": 280.0},
            # A full affordance dict: render_inject reads a dozen keys off it
            # and a partial one raises rather than degrading, so a thin fixture
            # would test the KeyError instead of the gate.
            "affordance": ({
                "stance": stance, "exact": True,
                "usable_turns": 12, "usable_lo": 12, "usable_hi": 12,
                "reserve_turns": 4, "per_turn_usd": 0.15,
                "fanouts_left": 0, "fanouts_lo": 0, "fanouts_hi": 0,
                "fanout_usd": 12.5, "binding_bucket": "5h window",
                "binding_stable": True, "basis": "median this window",
            } if stance else {}),
            "now": self.now,
        }

    # --- it asks when the fork is new ------------------------------------

    def test_asks_on_a_new_lockout(self):
        st = self.state(stance="landing")
        self.ub._choice_ask_gate(st)
        self.assertTrue(st.get("choice_ask"))

    def test_asks_on_a_hard_bucket(self):
        st = self.state(used=1.05)
        self.ub._choice_ask_gate(st)
        self.assertTrue(st.get("choice_ask"))

    def test_silent_when_nothing_is_forked(self):
        st = self.state(stance="ample", used=0.3)
        self.ub._choice_ask_gate(st)
        self.assertFalse(st.get("choice_ask"))

    # --- and only once ----------------------------------------------------

    def test_does_not_ask_twice_in_the_same_window(self):
        first = self.state(stance="landing")
        self.ub._choice_ask_gate(first)
        self.assertTrue(first.get("choice_ask"))
        for _ in range(4):
            again = self.state(stance="landing")
            self.ub._choice_ask_gate(again)
            self.assertFalse(again.get("choice_ask"),
                             "a modal every turn is worse than prose")

    def test_asks_again_in_a_new_window(self):
        first = self.state(stance="landing")
        self.ub._choice_ask_gate(first)
        later = self.state(stance="landing", window=self.now + 18_000)
        self.ub._choice_ask_gate(later)
        self.assertTrue(later.get("choice_ask"))

    def test_asks_again_only_when_materially_worse(self):
        self.ub._choice_ask_gate(self.state(used=1.05))
        barely = self.state(used=1.10)          # +5%, under the 25% step
        self.ub._choice_ask_gate(barely)
        self.assertFalse(barely.get("choice_ask"))
        much = self.state(used=1.40)            # +33%, over the step
        self.ub._choice_ask_gate(much)
        self.assertTrue(much.get("choice_ask"))

    # --- what the directives actually say ---------------------------------

    def test_directive_names_the_tool_when_asking(self):
        st = self.state(stance="landing", used=1.05)
        st["choice_ask"] = True
        out = self.ub.render_inject(st)
        self.assertIn("AskUserQuestion", out)

    def test_facts_survive_after_the_ask_stops(self):
        st = self.state(stance="landing", used=1.05)   # no choice_ask set
        out = self.ub.render_inject(st)
        self.assertNotIn("AskUserQuestion", out)
        self.assertIn("AT LIMIT", out)                 # the state still renders
        self.assertIn("do NOT ask again", out)

    def test_it_never_reads_as_a_refusal(self):
        st = self.state(used=1.05)
        st["choice_ask"] = True
        out = self.ub.render_inject(st)
        self.assertIn("NOT a refusal", out)
        self.assertIn("nothing is blocked", out)

    # --- the ack file is shared ------------------------------------------

    def test_pace_gate_no_longer_clobbers_other_acks(self):
        """REGRESSION: _pace_ask_gate replaced the ack file instead of merging.

        It shares pace_ack.json with the binding and choice gates, so replacing
        it wiped their acks and their directives spoke again next turn -- the
        every-turn repetition FINDINGS 20 exists to prevent.
        """
        st = self.state(stance="landing")
        st["binding"] = {"bucket": "session", "blocked_hours": 4.0}
        st["pace"] = {"ratio": 1.6, "day_index": 3, "days": 7,
                      "per_day_usd": 50.0, "exhaust_day": 6}
        st["posture"] = self.ub.DEFAULT_POSTURE

        self.ub._binding_ack_gate(st)
        self.ub._choice_ask_gate(st)
        self.ub._pace_ask_gate(st)

        ack = json.loads((Path(self.tmp.name) / "pace_ack.json").read_text())
        self.assertIn("binding", ack, "pace gate wiped the binding ack")
        self.assertIn("choice", ack, "pace gate wiped the choice ack")
        self.assertIn("week_start", ack, "pace gate lost its own state")

    def test_choice_ack_actually_persists(self):
        st = self.state(stance="landing")
        self.ub._choice_ask_gate(st)
        ack = json.loads((Path(self.tmp.name) / "pace_ack.json").read_text())
        self.assertEqual(ack["choice"]["kind"], "landing")

    def test_a_corrupt_ack_file_does_not_break_the_gate(self):
        (Path(self.tmp.name) / "pace_ack.json").write_text("{ not json")
        st = self.state(stance="landing")
        self.ub._choice_ask_gate(st)          # must not raise
        self.assertTrue(st.get("choice_ask"))


if __name__ == "__main__":
    unittest.main()
