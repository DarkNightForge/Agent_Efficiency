"""Tests for Component 1's three gaps: day bucket, threshold log, read API.

These lock in the two things that were actually wrong while building it: a day
bucket sourced from a session-scoped event list (reports the session's spend as
the day's), and a crossing log that would have written the same crossing on
every turn because the block renders every turn.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import usage_budget as ub

DAY = 86400


def weekly(start, cap=700.0, cost=0.0, anchored=True):
    return {"cost_usd": cost, "window_start": start, "resets_at": start + 7 * DAY,
            "anchored": anchored, "capacity_usd": cap,
            "used_frac": cost / cap if cap else None,
            "remaining_usd": cap - cost, "confidence": "exact", "samples": 2}


def session(cost=0.0, cap=100.0, start=None):
    return {"cost_usd": cost, "turns": 3, "window_start": start or time.time(),
            "resets_at": (start or time.time()) + 5 * 3600, "capacity_usd": cap,
            "used_frac": cost / cap if cap else None,
            "remaining_usd": cap - cost, "confidence": "exact", "samples": 2}


class Isolated(unittest.TestCase):
    """Redirect the log files so tests never touch the live install."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._log, self._seen = ub.THRESHOLD_LOG, ub.THRESHOLD_SEEN
        ub.THRESHOLD_LOG = Path(self.dir) / "thresholds.jsonl"
        ub.THRESHOLD_SEEN = Path(self.dir) / "seen.json"

    def tearDown(self):
        ub.THRESHOLD_LOG, ub.THRESHOLD_SEEN = self._log, self._seen


class ApiContractTest(Isolated):
    BUCKETS = {"session", "day", "weekly", "weekly_premium"}
    KEYS = {"cost_usd", "turns", "capacity_usd", "used_frac", "remaining_usd",
            "window_start", "resets_at", "confidence", "level"}

    def test_version_is_published(self):
        self.assertEqual(ub.budget_read({})["api_version"], ub.BUDGET_API_VERSION)

    def test_all_four_buckets_always_present_with_identical_keys(self):
        """Callers must never have to branch on a key being absent."""
        for st in ({}, {"session": session()}, None):
            snap = ub.budget_read(st if st is not None else {})
            self.assertEqual(set(snap["buckets"]), self.BUCKETS)
            for name, b in snap["buckets"].items():
                self.assertEqual(set(b), self.KEYS, f"bucket {name}")

    def test_never_raises_on_garbage(self):
        """It sits behind a prompt hook; a throw there costs the user a turn."""
        for junk in ({"session": "not-a-dict"}, {"weekly": None}, {"day": 17}):
            snap = ub.budget_read(junk)
            self.assertEqual(snap["api_version"], ub.BUDGET_API_VERSION)

    def test_uncalibrated_yields_nones_not_zeros(self):
        """A missing capacity is unknown, not free."""
        snap = ub.budget_read({})
        self.assertIsNone(snap["buckets"]["weekly"]["capacity_usd"])
        self.assertFalse(snap["calibrated"])

    def test_thresholds_are_published_in_the_snapshot(self):
        t = ub.budget_read({})["thresholds"]
        self.assertEqual(t["soft"], ub.SOFT_THRESHOLD)
        self.assertEqual(t["hard"], ub.HARD_THRESHOLD)


class LevelTest(Isolated):
    def test_level_boundaries(self):
        self.assertEqual(ub._level_for(0.0), "ok")
        self.assertEqual(ub._level_for(0.7999), "ok")
        self.assertEqual(ub._level_for(0.80), "soft")
        self.assertEqual(ub._level_for(0.999), "soft")
        self.assertEqual(ub._level_for(1.0), "hard")
        self.assertEqual(ub._level_for(1.4), "hard")
        self.assertEqual(ub._level_for(None), "unknown")

    def test_level_is_advisory_and_blocks_nothing(self):
        """FINDINGS 17: informing beats refusing. Nothing here gates on level."""
        st = {"session": session(cost=250.0, cap=100.0)}   # 250% used
        snap = ub.budget_read(st)
        self.assertEqual(snap["buckets"]["session"]["level"], "hard")
        # The API still answers, and downstream consumers still work.
        self.assertIsNotNone(snap["generated_at"])


class DayBucketTest(Isolated):
    def test_day_is_phase_aligned_to_the_weekly_anchor(self):
        """Not local midnight: _pace slices the week from the weekly start, so a
        midnight-aligned counter would measure a different day than the share."""
        wk_start = time.time() - 2.5 * DAY
        st = {"weekly": weekly(wk_start)}
        day = ub._day_bucket(st, [], time.time())
        self.assertEqual(day["basis"], "weekly-anchored")
        self.assertAlmostEqual((day["window_start"] - wk_start) % DAY, 0, places=3)
        self.assertEqual(day["resets_at"] - day["window_start"], DAY)

    def test_unanchored_falls_back_to_midnight_and_says_so(self):
        st = {"weekly": weekly(time.time(), anchored=False)}
        day = ub._day_bucket(st, [], time.time())
        self.assertEqual(day["basis"], "local-midnight")
        lt = datetime.fromtimestamp(day["window_start"])
        self.assertEqual((lt.hour, lt.minute, lt.second), (0, 0, 0))

    def test_capacity_is_the_weekly_share_and_is_flagged_derived(self):
        """There is no vendor daily cap. The denominator is week/7 and must not
        be presented as a published limit."""
        st = {"weekly": weekly(time.time() - DAY, cap=700.0)}
        day = ub._day_bucket(st, [], time.time())
        self.assertAlmostEqual(day["capacity_usd"], 100.0, places=2)
        self.assertTrue(day["capacity_is_derived"])

    def test_only_todays_events_count(self):
        now = time.time()
        wk_start = now - 3 * DAY
        st = {"weekly": weekly(wk_start)}
        day_start = wk_start + 3 * DAY
        events = [[day_start - 60, 5.0, "claude-opus-5"],      # yesterday
                  [day_start + 60, 2.0, "claude-opus-5"],      # today
                  [day_start + 120, 3.0, "claude-opus-5"]]     # today
        day = ub._day_bucket(st, events, now)
        self.assertAlmostEqual(day["cost_usd"], 5.0, places=4)
        self.assertEqual(day["turns"], 2)

    def test_day_is_not_sourced_from_the_session_window(self):
        """The bug this file exists for: a 24h bucket read through a 5h filter."""
        now = time.time()
        wk_start = now - 0.5 * DAY
        st = {"weekly": weekly(wk_start), "session": session(start=now - 3600)}
        events = [[now - 6 * 3600, 40.0, "claude-opus-5"],   # in day, before session
                  [now - 600, 10.0, "claude-opus-5"]]        # in both
        day = ub._day_bucket(st, events, now)
        self.assertAlmostEqual(day["cost_usd"], 50.0, places=4)


class ThresholdLogTest(Isolated):
    def _snap(self, used_frac, window_start=1000.0, bucket="session"):
        cap = 100.0
        return {"generated_at": time.time(), "burn_usd_per_hour": 1.0,
                "last_turn": {"epoch": 1, "cost_usd": 0.5, "model": "claude-opus-5"},
                "buckets": {bucket: {
                    "used_frac": used_frac, "cost_usd": used_frac * cap,
                    "capacity_usd": cap, "window_start": window_start,
                    "resets_at": window_start + 3600,
                    "level": ub._level_for(used_frac)}}}

    def test_crossing_is_logged_once_not_every_turn(self):
        """The block renders every turn; logging on level alone would write the
        same crossing hundreds of times and destroy the log's value."""
        first = ub._record_crossings(self._snap(0.85))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["to"], "soft")
        for _ in range(5):
            self.assertEqual(ub._record_crossings(self._snap(0.85)), [])
        self.assertEqual(len(ub.read_threshold_log()), 1)

    def test_escalation_to_hard_is_a_new_crossing(self):
        ub._record_crossings(self._snap(0.85))
        got = ub._record_crossings(self._snap(1.05))
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["from"], got[0]["to"]), ("soft", "hard"))

    def test_new_window_relogs_the_same_level(self):
        """Crossing 80% in a fresh window is a genuinely new event."""
        ub._record_crossings(self._snap(0.85, window_start=1000.0))
        got = ub._record_crossings(self._snap(0.85, window_start=99999.0))
        self.assertEqual(len(got), 1)

    def test_downgrade_is_recorded(self):
        """A rollover or a capacity correction moves a bucket back down; a log
        that only ratchets up would misrepresent the history."""
        ub._record_crossings(self._snap(1.05))
        got = ub._record_crossings(self._snap(0.10, window_start=99999.0))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["to"], "ok")

    def test_ok_is_not_logged_from_a_cold_start(self):
        self.assertEqual(ub._record_crossings(self._snap(0.10)), [])
        self.assertEqual(ub.read_threshold_log(), [])

    def test_record_captures_the_required_fields(self):
        """Spec: timestamp, counter values, and what triggered it."""
        rec = ub._record_crossings(self._snap(0.85))[0]
        for k in ("ts", "iso", "bucket", "to", "used_frac", "cost_usd",
                  "capacity_usd", "window_start", "trigger", "burn_usd_per_hour"):
            self.assertIn(k, rec)
        self.assertEqual(rec["trigger"]["model"], "claude-opus-5")

    def test_log_is_trimmed(self):
        ub.THRESHOLD_LOG.write_text("\n".join(
            json.dumps({"n": i}) for i in range(ub.THRESHOLD_LOG_MAX + 200)) + "\n")
        ub._trim_threshold_log()
        self.assertEqual(len(ub.THRESHOLD_LOG.read_text().splitlines()),
                         ub.THRESHOLD_LOG_MAX)

    def test_reading_is_side_effect_free_without_record(self):
        ub.budget_read({"session": session(cost=99.0, cap=100.0)})
        self.assertFalse(ub.THRESHOLD_LOG.exists())

    def test_record_true_writes(self):
        ub.budget_read({"session": session(cost=99.0, cap=100.0)}, record=True)
        self.assertTrue(ub.THRESHOLD_LOG.exists())


class SoftWarningRenderTest(Isolated):
    """The proposal's soft warning has to REACH the model, not just be computed.

    Rendered on a crossing only. FINDINGS 17 measured what position and density
    cost this block, so a standing row for a bucket sitting at 12% would dilute
    the lines that actually change behaviour; silence is the signal that nothing
    has crossed.
    """

    def _render(self, **buckets):
        st = {"session": session(), "weekly": weekly(time.time() - DAY),
              "burn_usd_per_hour": 1.0}
        st.update(buckets)
        st["pace"] = ub._pace(st["weekly"], time.time())
        return ub.render_inject(st)

    def test_nothing_is_said_while_every_bucket_is_under_the_threshold(self):
        out = self._render(session=session(cost=10.0, cap=100.0))
        self.assertNotIn("SOFT LIMIT", out)
        self.assertNotIn("AT LIMIT", out)

    def test_soft_crossing_is_announced_with_the_bucket_named(self):
        out = self._render(session=session(cost=85.0, cap=100.0))
        self.assertIn("SOFT LIMIT", out)
        self.assertIn("5h window", out)
        self.assertIn("85%", out)

    def test_hard_level_states_plainly_that_it_is_not_a_refusal(self):
        """FINDINGS 17: informing beats refusing, and nothing here blocks."""
        out = self._render(session=session(cost=120.0, cap=100.0))
        self.assertIn("AT LIMIT", out)
        self.assertIn("NOT a refusal", out)
        self.assertIn("let the user decide", out)

    def test_day_crossing_says_its_denominator_is_derived(self):
        """There is no vendor daily cap; a percentage must not imply one."""
        day = {"cost_usd": 90.0, "turns": 5, "capacity_usd": 100.0,
               "used_frac": 0.90, "window_start": time.time() - 3600,
               "resets_at": time.time() + 3600, "confidence": "exact"}
        out = self._render(day=day)
        self.assertIn("SOFT LIMIT", out)
        self.assertIn("today", out)
        self.assertIn("not a published daily cap", out)

    def test_uncalibrated_buckets_say_nothing(self):
        out = self._render(session={"cost_usd": 5.0, "used_frac": None})
        self.assertNotIn("SOFT LIMIT", out)


class ConsumerTest(Isolated):
    def test_quoter_reads_through_the_api(self):
        """Component 2 must not reach into state.json's internal shape."""
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.py")
        with open(p, "w") as fh:
            fh.write("x = 1\n" * 300)
        st = {"affordance": {"headroom_usd": 50.0, "binding_bucket": "5h window",
                             "per_turn_usd": 0.05}}
        q = ub.quote_paths([p], state=st)
        self.assertEqual(q["binding_bucket"], "5h window")
        self.assertIn("share_pct", q)


if __name__ == "__main__":
    unittest.main()
