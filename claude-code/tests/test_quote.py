"""Tests for the deterministic quoting system (Component 2).

The constants under test are not arbitrary: the gate constants replicate the
limits Read itself reports (measured live against Read), and the cost ratio is
measured against transcript ground truth. A test that
merely asserts "the function returns a number" would not have caught either of
the two bugs found while building this -- quoting a 27 MB file at 12M tokens,
and treating the token cap as an error when the host paginates.
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import usage_budget as ub


class TempTree(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def write(self, name, data):
        p = os.path.join(self.dir, name)
        mode = "wb" if isinstance(data, bytes) else "w"
        with open(p, mode) as fh:
            fh.write(data)
        return p


class FormulaTest(TempTree):
    def test_formula_is_deterministic(self):
        p = self.write("a.py", "x = 1\n" * 100)
        a = ub.quote_paths([p], state={})
        b = ub.quote_paths([p], state={})
        self.assertEqual(a["tokens"], b["tokens"])
        self.assertGreater(a["tokens"], 0)

    def test_formula_string_is_published_in_the_quote(self):
        """No opaque estimates: the quote carries the formula that produced it."""
        p = self.write("a.py", "x = 1\n" * 10)
        q = ub.quote_paths([p], state={})
        self.assertIn(str(ub.BYTES_PER_TOKEN), q["formula"])
        self.assertIn(str(ub.LINE_PREFIX_TOKENS), q["formula"])

    def test_multiplier_is_configurable_and_scales_the_estimate(self):
        p = self.write("a.py", "x = 1\n" * 200)
        low = ub.quote_paths([p], multiplier=1.0, state={})["tokens"]
        high = ub.quote_paths([p], multiplier=2.0, state={})["tokens"]
        self.assertAlmostEqual(high / low, 2.0, places=1)

    def test_line_prefix_overhead_is_counted(self):
        """Same bytes, more lines => more tokens, because Read renders `N\\t`."""
        one = self.write("one.txt", b"a" * 1000)
        many = self.write("many.txt", b"a\n" * 500)
        self.assertEqual(os.path.getsize(one), 1000)
        self.assertEqual(os.path.getsize(many), 1000)
        t_one = ub.quote_paths([one], state={})["tokens"]
        t_many = ub.quote_paths([many], state={})["tokens"]
        self.assertGreater(t_many, t_one)

    def test_estimate_is_conservative_against_the_hosts_own_divisor(self):
        """Must not under-quote. bytes/4 is what the host gates on; we measured
        2.6, so our number has to come out above the naive one."""
        body = "def f(x):\n    return x + 1\n" * 200
        p = self.write("code.py", body)
        naive = len(body.encode()) / 4
        self.assertGreater(ub.quote_paths([p], state={})["tokens"], naive)


class BinaryTest(TempTree):
    def test_binary_by_extension_is_flagged_not_estimated(self):
        p = self.write("img.png", b"\x89PNG\r\n" + b"a" * 5000)
        q = ub.quote_paths([p], state={})
        self.assertEqual(q["files_quoted"], 0)
        self.assertEqual(q["skipped"][0]["status"], "binary")
        self.assertEqual(q["tokens"], 0)

    def test_binary_by_nul_sniff_is_flagged(self):
        """No telltale extension, but NULs in the first 8 KB."""
        p = self.write("mystery.dat", b"text-ish header\x00\x00\x00" + b"\x01" * 4000)
        q = ub.quote_paths([p], state={})
        self.assertEqual(q["files_quoted"], 0)
        self.assertEqual(q["skipped"][0]["reason"], "NUL byte")

    def test_text_file_is_not_mistaken_for_binary(self):
        p = self.write("fine.md", "# hello\n\nsome prose\n")
        self.assertEqual(ub.quote_paths([p], state={})["files_quoted"], 1)


class HostGateTest(TempTree):
    def test_over_byte_cap_is_too_large_and_returns_nothing(self):
        """256 KB. Read errors and hands back NO content, so it must not be
        priced as if it were readable."""
        p = self.write("huge.txt", "x" * (ub.READ_MAX_BYTES + 1))
        q = ub.quote_paths([p], state={})
        self.assertEqual(q["files_quoted"], 0)
        self.assertEqual(q["skipped"][0]["status"], "too_large")
        self.assertEqual(q["tokens"], 0)

    def test_over_token_cap_paginates_rather_than_failing(self):
        """25,000 tokens. Confirmed live: Read returns page 1 plus an offset hint,
        so the file is quoted at one page, not rejected and not quoted whole."""
        p = self.write("big.txt", ("y" * 79 + "\n") * 2500)   # 200,000 B < 256 KB
        m = ub._measure_file(p)
        self.assertEqual(m["status"], "ok")
        self.assertTrue(m["paginated"])
        self.assertLess(m["effective_bytes"], m["bytes"])
        q = ub.quote_paths([p], state={})
        self.assertEqual(q["paginated"], [p])

    def test_json_uses_the_hosts_denser_divisor(self):
        """json/jsonl/jsonc estimate at /2, everything else at /4, so a
        .json paginates at half the size an identically-sized .txt does."""
        body = '{"a":1}\n' * 4000
        j = self.write("d.json", body)
        t = self.write("d.txt", body)
        self.assertGreater(ub._measure_file(j)["host_estimate"],
                           ub._measure_file(t)["host_estimate"])
        self.assertTrue(ub._measure_file(j)["paginated"])
        self.assertFalse(ub._measure_file(t)["paginated"])


class SkipTest(TempTree):
    def test_missing_directory_and_empty_are_each_reported(self):
        empty = self.write("empty.txt", "")
        q = ub.quote_paths([os.path.join(self.dir, "nope.txt"), self.dir, empty],
                           state={})
        self.assertEqual(q["files_quoted"], 0)
        self.assertEqual({m["status"] for m in q["skipped"]},
                         {"missing", "not_a_file", "empty"})

    def test_skipped_files_never_contribute_tokens(self):
        good = self.write("g.py", "x = 1\n" * 50)
        bad = self.write("b.png", b"\x89PNG" + b"\x00" * 100)
        q = ub.quote_paths([good, bad], state={})
        self.assertEqual(q["tokens"], ub.quote_paths([good], state={})["tokens"])


class BudgetTest(TempTree):
    def _state(self, headroom):
        return {"affordance": {"headroom_usd": headroom, "binding_bucket": "5h window",
                               "per_turn_usd": 0.05}}

    def test_share_of_remaining_budget_is_reported(self):
        p = self.write("a.py", "x = 1\n" * 500)
        q = ub.quote_paths([p], state=self._state(100.0))
        self.assertIn("share_pct", q)
        self.assertFalse(q["over_budget"])
        self.assertIn("% of the", ub.render_quote(q))

    def test_exceeding_budget_demands_a_reduced_scope(self):
        p = self.write("a.py", "x = 1\n" * 5000)
        q = ub.quote_paths([p], state=self._state(0.01))
        self.assertTrue(q["over_budget"])
        self.assertIn("EXCEEDS REMAINING BUDGET", ub.render_quote(q))
        self.assertIn("reduced scope", ub.render_quote(q))

    def test_quote_still_renders_with_no_calibration(self):
        """No affordance => no share line, but the token/cost estimate stands."""
        p = self.write("a.py", "x = 1\n" * 20)
        q = ub.quote_paths([p], state={})
        self.assertNotIn("share_pct", q)
        self.assertIn("estimated", ub.render_quote(q))


class CostTest(TempTree):
    def test_carry_is_reported_alongside_landing(self):
        """analyze established a tool result is paid for every subsequent turn.
        A quote that showed only the landing cost would repeat that error."""
        p = self.write("a.py", "x = 1\n" * 400)
        q = ub.quote_paths([p], carry_turns=20, state={})
        self.assertGreater(q["carry_usd_per_turn"], 0)
        self.assertGreater(q["total_usd"], q["landing_usd"])

    def test_cheaper_model_quotes_cheaper(self):
        p = self.write("a.py", "x = 1\n" * 400)
        opus = ub.quote_paths([p], model="claude-opus-5", state={})["total_usd"]
        haiku = ub.quote_paths([p], model="claude-haiku-4-5", state={})["total_usd"]
        self.assertLess(haiku, opus)

    def test_unknown_model_falls_back_without_crashing(self):
        p = self.write("a.py", "x = 1\n")
        self.assertEqual(ub.quote_paths([p], model="not-a-model", state={})["model"],
                         "claude-opus-5")


class PerformanceTest(TempTree):
    def test_500_files_quote_in_under_one_second(self):
        paths = [self.write(f"f{i}.py", "x = 1\n" * 60) for i in range(500)]
        for n in (10, 100, 500):
            start = time.perf_counter()
            ub.quote_paths(paths[:n], state={})
            elapsed = time.perf_counter() - start
            self.assertLess(elapsed, 1.0, f"{n} files took {elapsed:.3f}s")


if __name__ == "__main__":
    unittest.main()
