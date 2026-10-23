#!/usr/bin/env python3
"""
Tests for per-model pricing: the Claude 5.5 rows, per-model cache-read
multipliers, and Haiku 5.5's prompt-length tier.

Every expected figure is Anthropic's published table
(platform.claude.com/docs/en/about-claude/pricing), worked by hand here so a
typo in PRICES cannot also be a typo in the test.

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(REPO_DIR / "tests"))

from test_usage_budget import turn, write_transcript  # noqa: E402

M = 1_000_000


def usage(inp=0, out=0, cread=0, cw5=0, cw1=0):
    return {"input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": cread,
            "cache_creation_input_tokens": cw5 + cw1,
            "cache_creation": {"ephemeral_5m_input_tokens": cw5,
                               "ephemeral_1h_input_tokens": cw1}}


class PricesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "state").mkdir()
        (root / "projects").mkdir()
        os.environ["USAGE_BUDGET_HOME"] = str(root / "state")
        os.environ["CLAUDE_PROJECTS_DIR"] = str(root / "projects")
        import usage_budget
        self.ub = importlib.reload(usage_budget)
        self.projects = root / "projects"

    def tearDown(self):
        self.tmp.cleanup()

    def cost(self, model, speed=None, **u):
        u = usage(**u)
        return self.ub._cost_of(u, self.ub._norm_model(model, speed, u))

    # --- the 5.5 rows, at their published rates -------------------------------

    def test_opus_5_5_is_4_and_20_with_cache_hits_at_0_05x(self):
        # 1M in at $4 + 1M out at $20 + 1M cache read at $0.20 + 1M 5m write at $5
        got = self.cost("claude-opus-5-5", inp=M, out=M, cread=M, cw5=M)
        self.assertAlmostEqual(got, 4 + 20 + 0.20 + 5, places=6)

    def test_opus_5_5_fast_is_8_and_40_and_keeps_its_cache_multiplier(self):
        """Fast mode stacks with the caching multipliers, per the pricing page."""
        self.assertEqual(self.ub._norm_model("claude-opus-5-5", "fast"), "claude-opus-5-5:fast")
        got = self.cost("claude-opus-5-5", speed="fast", inp=M, out=M, cread=M)
        self.assertAlmostEqual(got, 8 + 40 + 8 * 0.05, places=6)

    def test_sonnet_5_5_is_2_and_10_with_cache_hits_at_0_05x(self):
        got = self.cost("claude-sonnet-5-5", inp=M, out=M, cread=M, cw1=M)
        self.assertAlmostEqual(got, 2 + 10 + 0.10 + 4, places=6)

    def test_fable_5_1_cache_hits_are_0_025x(self):
        got = self.cost("claude-fable-5-1", cread=M)
        self.assertAlmostEqual(got, 0.25, places=6)

    def test_models_without_an_override_keep_0_1x(self):
        self.assertAlmostEqual(self.cost("claude-opus-5", cread=M), 0.50, places=6)
        self.assertAlmostEqual(self.cost("claude-fable-5", cread=M), 1.00, places=6)

    def test_none_of_the_5_5_models_is_unpriced_any_more(self):
        for m in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
            self.assertIn(self.ub._norm_model(m, None), self.ub.PRICES, m)

    def test_an_unknown_model_still_takes_the_fallback(self):
        self.assertEqual(self.ub._price("claude-nonexistent-9"), self.ub.FALLBACK_PRICE)
        self.assertEqual(self.ub._cread_mult("claude-nonexistent-9"), self.ub.CACHE_READ_MULT)

    # --- Haiku 5.5's prompt-length tier ---------------------------------------

    def test_haiku_5_5_at_exactly_100k_pays_the_base_price(self):
        """The higher price applies to a prompt OVER 100,000 tokens."""
        u = usage(inp=100_000)
        self.assertEqual(self.ub._norm_model("claude-haiku-5-5", None, u), "claude-haiku-5-5")
        self.assertAlmostEqual(self.cost("claude-haiku-5-5", inp=100_000), 0.10 * 0.1, places=9)

    def test_haiku_5_5_one_token_over_pays_the_long_price_for_the_whole_request(self):
        u = usage(inp=100_001, out=M)
        self.assertEqual(self.ub._norm_model("claude-haiku-5-5", None, u), "claude-haiku-5-5:long")
        self.assertAlmostEqual(self.cost("claude-haiku-5-5", inp=100_001, out=M),
                               100_001 * 0.50 / M + 2.50, places=9)

    def test_prompt_length_counts_cache_reads_and_writes(self):
        """A mostly-cached 120k prompt is still a long prompt."""
        u = usage(inp=1_000, cread=110_000, cw5=9_000)
        self.assertEqual(self.ub._norm_model("claude-haiku-5-5", None, u), "claude-haiku-5-5:long")
        # cache hits on the long row are still 0.1x of ITS input price: $0.05/MTok
        got = self.cost("claude-haiku-5-5", inp=1_000, cread=110_000, cw5=9_000)
        self.assertAlmostEqual(got, (1_000 * 0.50 + 110_000 * 0.05 + 9_000 * 0.625) / M, places=9)

    def test_a_dated_haiku_id_still_gets_its_tier(self):
        u = usage(inp=150_000)
        self.assertEqual(self.ub._norm_model("claude-haiku-5-5-20261001", None, u),
                         "claude-haiku-5-5:long")

    def test_without_a_usage_payload_the_base_row_applies(self):
        """The quoter prices a hypothetical read; there is no request to measure."""
        self.assertEqual(self.ub._norm_model("claude-haiku-5-5", None), "claude-haiku-5-5")

    def test_a_window_total_prices_each_tier_at_its_own_rate(self):
        """Aggregates sum tokens per model KEY, so the tier must be in the key.

        If the long tier were applied only per turn, _window_cost (which prices
        summed tokens) would charge a long request at the base rate and disagree
        with the per-turn total the block is built from.
        """
        now = time.time()
        short = dict(model="claude-haiku-5-5", inp=20_000, out=2_000)
        long_ = dict(model="claude-haiku-5-5", inp=1_000, cread=130_000, out=4_000)
        write_transcript(self.projects, "s", [turn(now - 120, **short), turn(now - 60, **long_)])
        per_turn = self.cost(**short) + self.cost(**long_)
        self.assertAlmostEqual(self.ub._window_cost(now - 600, now + 1), per_turn, places=9)
        mix = self.ub._window_token_mix(now - 600, now + 1)
        self.assertEqual(set(mix["models"]), {"claude-haiku-5-5", "claude-haiku-5-5:long"})

    # --- a price change reprices history -----------------------------------------

    def cached_costs(self):
        import json
        files = json.loads(self.ub.CACHE_PATH.read_text())["files"]
        return sorted(round(e[1], 6) for f in files.values() for e in f["events"])

    def test_a_changed_price_table_reprices_the_cached_history(self):
        """Events are priced once, when scanned. Without a fingerprint check, a
        corrected price would only apply to turns scanned after the change, and
        the last RETAIN_DAYS of history would keep the old rate."""
        now = time.time()
        write_transcript(self.projects, "s", [turn(now - 60, model="claude-opus-5-5", inp=M)])
        self.ub.collect()
        self.assertEqual(self.cached_costs(), [4.0])
        self.ub.PRICES["claude-opus-5-5"] = dict(self.ub.PRICES["claude-opus-5-5"], **{"in": 6.0})
        self.ub.collect()
        self.assertEqual(self.cached_costs(), [6.0])

    def test_an_unchanged_price_table_keeps_the_incremental_cache(self):
        now = time.time()
        write_transcript(self.projects, "s", [turn(now - 60, model="claude-opus-5-5", inp=M)])
        self.ub.collect()
        sig = __import__("json").loads(self.ub.CACHE_PATH.read_text())["prices"]
        self.ub.collect()
        self.assertEqual(__import__("json").loads(self.ub.CACHE_PATH.read_text())["prices"], sig)
        self.assertEqual(self.cached_costs(), [4.0])


if __name__ == "__main__":
    unittest.main()
