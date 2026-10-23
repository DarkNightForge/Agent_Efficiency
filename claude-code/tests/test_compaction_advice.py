"""Tests for the compaction-advice line.

`analyze` has always computed the one number in this repo with a MEASURED
payback -- turns until compacting pays for itself -- and it was reachable only
by running `analyze` by hand. These lock in that it now reaches the model, that
it stays quiet when it is not actionable, and that it does not repeat (E12,
FINDINGS 20: a directive that fires every turn stops being read).
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import usage_budget as ub


def turn(ctx, model="claude-opus-5", out=200):
    return json.dumps({
        "type": "assistant",
        "message": {"id": f"msg_{ctx}", "role": "assistant", "model": model,
                    "usage": {"cache_read_input_tokens": ctx, "input_tokens": 0,
                              "output_tokens": out}},
    })


class Isolated(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._ack = ub.ACK_PATH
        ub.ACK_PATH = Path(self.dir) / "ack.json"

    def tearDown(self):
        ub.ACK_PATH = self._ack

    def transcript(self, *lines):
        p = os.path.join(self.dir, "t.jsonl")
        with open(p, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        return p

    def session(self, current, floor=40_000, name="t.jsonl"):
        """A realistic transcript: a first turn carrying only the fixed prefix,
        then the current turn. One-turn transcripts cannot be used here -- the
        floor is read from the head, so a lone turn reads as all-prefix, which
        correctly means there is nothing to reclaim.
        """
        p = os.path.join(self.dir, name)
        with open(p, "w") as fh:
            fh.write(turn(floor) + "\n" + turn(current) + "\n")
        return p


class TailContextTest(Isolated):
    def test_reads_the_most_recent_assistant_turn(self):
        t = self.transcript(turn(10_000), turn(500_000))
        self.assertEqual(ub._tail_context(t)["ctx"], 500_000)

    def test_reads_only_the_tail_of_a_large_file(self):
        """The inject path is a prompt hook; whole transcripts run to megabytes."""
        pad = [turn(1_000 + i) for i in range(6_000)]
        t = self.transcript(*pad, turn(400_000))
        self.assertGreater(os.path.getsize(t), ub.COMPACT_TAIL_BYTES)
        self.assertEqual(ub._tail_context(t)["ctx"], 400_000)

    def test_missing_or_unreadable_transcript_is_silent(self):
        self.assertIsNone(ub._tail_context(None))
        self.assertIsNone(ub._tail_context("/nope/missing.jsonl"))

    def test_a_transcript_with_no_usage_is_silent(self):
        t = self.transcript(json.dumps({"type": "user", "message": {}}))
        self.assertIsNone(ub._tail_context(t))


class AdviceTest(Isolated):
    def test_a_large_context_is_worth_compacting(self):
        adv = ub._compaction_advice(self.session(600_000))
        self.assertIsNotNone(adv)
        self.assertGreater(adv["per_turn_saving"], 0)
        self.assertLessEqual(adv["turns"], ub.COMPACT_ADVISE_MAX_TURNS)

    def test_a_small_context_says_nothing(self):
        """Below the summary size there is nothing to reclaim."""
        self.assertIsNone(ub._compaction_advice(self.session(5_000, floor=1_000)))

    def test_payback_beyond_the_horizon_says_nothing(self):
        """Truthful but useless: a session will not run that long."""
        adv = ub._compaction_advice(self.session(75_000, floor=45_000))
        self.assertIsNone(adv)

    def test_generation_cost_is_included_in_the_one_off(self):
        """Output is priced ~5x input; omitting it halves the break-even and
        makes compaction look better than it is."""
        adv = ub._compaction_advice(self.session(600_000))
        read_only = 600_000 * 5.00 * ub.CACHE_READ_MULT / 1e6
        self.assertGreater(adv["one_off"], read_only)


class CtxTokensTest(Isolated):
    """`cache_read + input` misses any turn that WRITES the prefix to cache.
    Measured on this repo's own working session: 24,815 reported against a true
    215,041 (FINDINGS 33)."""

    def cache_write_turn(self, read, created, out=200):
        return json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "usage": {"input_tokens": 0, "output_tokens": out,
                                  "cache_read_input_tokens": read,
                                  "cache_creation": {
                                      "ephemeral_1h_input_tokens": created,
                                      "ephemeral_5m_input_tokens": 0}}},
        })

    def test_cache_creation_counts_toward_the_context(self):
        u = {"input_tokens": 2, "cache_read_input_tokens": 24_813,
             "cache_creation": {"ephemeral_1h_input_tokens": 190_226,
                                "ephemeral_5m_input_tokens": 0}}
        self.assertEqual(ub._ctx_tokens(u), 215_041)

    def test_the_flat_cache_creation_field_is_read_too(self):
        """Older records carry the un-split field."""
        u = {"input_tokens": 0, "cache_read_input_tokens": 1_000,
             "cache_creation_input_tokens": 9_000}
        self.assertEqual(ub._ctx_tokens(u), 10_000)

    def test_a_cache_write_turn_reports_its_real_context(self):
        t = self.transcript(turn(40_000),
                            self.cache_write_turn(24_815, 190_226))
        self.assertEqual(ub._tail_context(t)["ctx"], 215_041)

    def test_advice_survives_a_cache_write_turn(self):
        """The regression this fixes: after an idle gap or a TTL expiry the
        context read as a fraction of itself, so the advice silently vanished
        on exactly the sessions that most needed it."""
        t = self.transcript(turn(45_000),
                            self.cache_write_turn(24_815, 190_226))
        self.assertIsNotNone(ub._compaction_advice(t))


class FloorTest(Isolated):
    """The prefix a compaction cannot touch. Reported 2026-08-19 from a live
    1M-context session: 39.7k of a 386.8k context was system prompt, tool
    schemas, memory files and skills, and treating the 30k summary as the whole
    post-compaction context understated the payback by ~40% (FINDINGS 30)."""

    def test_the_floor_is_read_from_the_leading_turns(self):
        self.assertEqual(ub._floor_context(self.session(400_000, floor=45_000)),
                         45_000)

    def test_a_bare_cache_write_does_not_set_the_floor(self):
        """A live session's first record was a cache write, not a turn: every
        token field zero except `cache_creation.ephemeral_1h: 1781` and no
        output. Read as a turn it put the floor at 1,781 against the 58,377
        every real turn carried."""
        prewarm = json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "usage": {"input_tokens": 0, "cache_read_input_tokens": 0,
                                  "output_tokens": 0,
                                  "cache_creation": {"ephemeral_1h_input_tokens": 1_781,
                                                     "ephemeral_5m_input_tokens": 0}}},
        })
        t = self.transcript(prewarm, turn(58_377), turn(59_567))
        self.assertEqual(ub._floor_context(t), 58_377)

    def test_a_missing_transcript_has_no_floor(self):
        self.assertEqual(ub._floor_context(None), 0)
        self.assertEqual(ub._floor_context("/nope/missing.jsonl"), 0)

    def test_the_floor_is_subtracted_from_the_per_turn_saving(self):
        with_floor = ub._breakeven_turns(400_000, "claude-opus-5", 30_000, 45_000)
        without = ub._breakeven_turns(400_000, "claude-opus-5", 30_000, 0)
        self.assertEqual(with_floor["post_ctx"], 45_000 + ub.SUMMARY_TEXT_TOKENS)
        self.assertLess(with_floor["per_turn_saving"], without["per_turn_saving"])
        self.assertGreater(with_floor["turns"], without["turns"])

    def test_only_the_summary_is_generated_but_the_whole_prefix_is_written(self):
        """Output is 5x input, so charging generation on floor+summary would
        overstate the one-off as badly as omitting the floor understated it."""
        be = ub._breakeven_turns(400_000, "claude-opus-5", 30_000, 45_000)
        p = ub._price("claude-opus-5")
        expected = (400_000 * p["in"] * ub.CACHE_READ_MULT
                    + ub.SUMMARY_TEXT_TOKENS * p["out"]
                    + be["post_ctx"] * p["in"] * ub.CACHE_WRITE_1H_MULT) / 1e6
        self.assertAlmostEqual(be["one_off"], expected, places=6)

    def test_a_context_at_its_floor_has_nothing_to_reclaim(self):
        be = ub._breakeven_turns(50_000, "claude-opus-5", 30_000, 48_000)
        self.assertFalse(be["worth_it"])
        self.assertIsNone(be["turns"])

    def test_the_floor_can_push_the_payback_past_the_horizon(self):
        """The floor is not just a nicer number: on a mid-size session it moves
        the payback out past the horizon, so the advice should not fire at all.
        Live floors on this machine measured 45-58k."""
        unfloored = ub._breakeven_turns(100_000, "claude-opus-5", 30_000, 0)
        self.assertLessEqual(unfloored["turns"], ub.COMPACT_ADVISE_MAX_TURNS)
        self.assertIsNone(ub._compaction_advice(self.session(100_000, floor=45_639)))


class GateTest(Isolated):
    def test_it_speaks_once_then_stays_quiet(self):
        t = self.session(600_000)
        adv = ub._compaction_advice(t)
        self.assertTrue(ub._compact_ack_gate(adv, t))
        for _ in range(4):
            self.assertFalse(ub._compact_ack_gate(adv, t))

    def test_it_speaks_again_when_the_payback_halves(self):
        t = self.session(600_000)
        ub._compact_ack_gate(ub._compaction_advice(t), t)
        better = dict(ub._compaction_advice(t))
        better["turns"] = 1
        self.assertTrue(ub._compact_ack_gate(better, t))

    def test_separate_sessions_are_gated_separately(self):
        a = self.session(600_000)
        b = self.session(600_000, name="other.jsonl")
        adv = ub._compaction_advice(a)
        self.assertTrue(ub._compact_ack_gate(adv, a))
        self.assertTrue(ub._compact_ack_gate(ub._compaction_advice(b), b))

    def test_no_advice_never_speaks(self):
        self.assertFalse(ub._compact_ack_gate(None, "x"))


class RenderTest(Isolated):
    def test_the_line_is_imperative_and_carries_the_numbers(self):
        st = {"compaction_advice": {"ctx": 600_000, "turns": 4,
                                    "per_turn_saving": 0.2850, "one_off": 1.42,
                                    "summary_tokens": 30_000,
                                    "floor_tokens": 40_000, "post_ctx": 70_000}}
        out = ub.render_inject(st)
        self.assertIn("COMPACT NOW", out)
        self.assertIn("600,000", out)
        self.assertIn("$0.2850", out)
        self.assertIn("~4 more turns", out)

    def test_the_line_states_what_compaction_cannot_reclaim(self):
        """The payback is only auditable if the post-compaction size it is
        measured against is on the line with it."""
        st = {"compaction_advice": {"ctx": 600_000, "turns": 4,
                                    "per_turn_saving": 0.2850, "one_off": 1.42,
                                    "summary_tokens": 30_000,
                                    "floor_tokens": 40_000, "post_ctx": 70_000}}
        out = ub.render_inject(st)
        self.assertIn("70,000", out)
        self.assertIn("40,000", out)

    def test_no_advice_renders_no_line(self):
        self.assertNotIn("COMPACT NOW", ub.render_inject({}))


if __name__ == "__main__":
    unittest.main()
