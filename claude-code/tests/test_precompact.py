#!/usr/bin/env python3
"""
Tests for FINDINGS 29: steering what a compaction keeps.

FINDINGS 26 established that an agent cannot TRIGGER a compaction on any route.
It also found the one thing it can still do: a PreCompact hook's stdout is
passed to the summariser as additional instructions, on both the auto and manual
paths. That is not in the hooks documentation, so it is unverified on any given
Claude Code version.

That matters because compaction keeps a measured median 7.6% of prior context
(FINDINGS 25) and the summariser chooses which 7.6%. The cost of a bad choice is
rework, not lost tokens: a dropped measurement gets measured again, a dropped
dead end gets walked again, both at full price.

The contract these lock in is unusually unforgiving -- stdout IS the instruction,
so a traceback on stdout becomes guidance handed to the summariser. Silence is
the only safe failure.

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))
SCRIPT = REPO_DIR / "usage_budget.py"


def load_module(state_dir: Path):
    os.environ["USAGE_BUDGET_HOME"] = str(state_dir)
    import usage_budget
    return importlib.reload(usage_budget)


def run(payload, expect_rc=0):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "precompact"],
        input=payload, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == expect_rc, (proc.returncode, proc.stderr)
    return proc


class PreCompactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ub = load_module(Path(self.tmp.name))

    # --- content ----------------------------------------------------------

    def test_priorities_are_ranked_and_lead_with_measurements(self):
        text = self.ub.precompact_instructions("manual")
        self.assertIn("Rank what you keep by what it would cost to recover", text)
        # Order is the whole intervention: the summariser reads top-down.
        self.assertLess(text.index("MEASUREMENTS"), text.index("RULED-OUT"))
        self.assertLess(text.index("RULED-OUT"), text.index("DECISIONS"))
        self.assertLess(text.index("DECISIONS"), text.index("EXACT IDENTIFIERS"))

    def test_it_bounds_its_own_length_cost(self):
        # MEASURED 2026-08-18: without this clause the summariser ran to the
        # output cap in every uncapped A/B arm while the control stopped
        # naturally (pilot 4: control 5,625 tokens, treatment 8,000 = capped).
        # A longer summary is re-read on every turn after the compaction, so an
        # unbounded re-prioritisation buys retention with a recurring cost.
        text = self.ub.precompact_instructions()
        self.assertIn("not HOW MUCH you write", text)
        self.assertIn("same\nlength", text)

    def test_it_says_what_to_drop_not_only_what_to_keep(self):
        # An instruction that only adds priorities gives the summariser no way to
        # honour them under a budget. It has to be told what loses.
        text = self.ub.precompact_instructions()
        self.assertIn("drop the account of HOW the work proceeded", text)

    def test_auto_trigger_adds_the_in_flight_clause(self):
        auto = self.ub.precompact_instructions("auto")
        manual = self.ub.precompact_instructions("manual")
        self.assertIn("triggered automatically, mid-task", auto)
        self.assertNotIn("triggered automatically", manual)
        self.assertTrue(auto.startswith(manual))

    def test_trigger_matching_is_forgiving(self):
        for t in ("auto", "AUTO", " auto ", "Auto"):
            self.assertIn("mid-task", self.ub.precompact_instructions(t), t)
        for t in (None, "", "manual", "unknown-future-trigger"):
            self.assertNotIn("mid-task", self.ub.precompact_instructions(t), t)

    def test_no_prohibitions_that_fight_the_host_prompt(self):
        # FINDINGS 17: mood moves obedience more than content. The host's own
        # compaction prompt is already a wall of CRITICAL/do-NOT; adding more
        # competes with it instead of cooperating.
        text = self.ub.precompact_instructions("auto")
        self.assertNotIn("CRITICAL", text)
        self.assertNotIn("Do NOT", text)
        self.assertNotIn("You MUST", text)

    # --- the CLI contract -------------------------------------------------

    def test_stdout_is_only_the_instruction(self):
        proc = run('{"trigger":"auto"}')
        self.assertEqual(proc.stdout.strip(),
                         self.ub.precompact_instructions("auto"))

    def test_malformed_payload_is_silent_not_noisy(self):
        # Stdout is fed to the summariser. A traceback here would become
        # instructions. Empty output contributes nothing and is always safe.
        for bad in ("not json at all", "", "[]", "null", '{"trigger":{"a":1}}'):
            proc = run(bad)
            self.assertNotIn("Traceback", proc.stdout, bad)
            self.assertNotIn("Error", proc.stdout, bad)

    def test_missing_trigger_still_emits_the_base_instruction(self):
        proc = run("{}")
        self.assertIn("MEASUREMENTS", proc.stdout)
        self.assertNotIn("mid-task", proc.stdout)

    def test_exit_code_is_zero_even_on_garbage(self):
        # A non-zero exit marks the hook failed, and the host then drops its
        # output entirely -- so a crash costs the instruction silently.
        run("}{ garbage", expect_rc=0)

    def test_output_is_plain_text_not_json(self):
        # PreCompact takes raw stdout, unlike the JSON-shaped hook events.
        proc = run('{"trigger":"manual"}')
        with self.assertRaises(json.JSONDecodeError):
            json.loads(proc.stdout)

    def test_instruction_is_small_enough_to_be_worth_it(self):
        # It rides on every compaction. Keep it well under the ~24.7k a
        # compaction lands at (FINDINGS 25) or the cure competes with the disease.
        tokens = len(self.ub.precompact_instructions("auto")) / self.ub.BYTES_PER_TOKEN
        self.assertLess(tokens, 800)


if __name__ == "__main__":
    unittest.main()
