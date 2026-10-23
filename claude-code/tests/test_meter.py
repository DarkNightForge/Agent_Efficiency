#!/usr/bin/env python3
"""
Tests for the vendor meter, read from Claude Code's status line (FINDINGS 15, 34).

No network, and no credentials anywhere in the path. Every test drives the
parse/record/overlay/render path from a payload in the documented status line
shape (code.claude.com/docs/en/statusline), because the whole point of the
meter is that its OUTPUT is exact -- so the code that consumes it must be
exercised against the shape Claude Code actually sends.

Three properties are locked in here beyond the parse itself:
  - an idle session's status line cannot overwrite a fresher reading;
  - a window whose reset has passed is not served as exact;
  - the meter lands before the lockout forecast, so the block's hours-to-cap
    and its dollars-left come from the same figures.

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))
SCRIPT = REPO_DIR / "usage_budget.py"

WK = 1786975200.0
HOUR = 3600
DAY = 86400


def load_module(state_dir: Path):
    os.environ["USAGE_BUDGET_HOME"] = str(state_dir)
    import usage_budget
    return importlib.reload(usage_budget)


def payload(five=22.0, seven=5.0, base=WK, **extra):
    """A status line payload in the documented shape, trimmed to what we read."""
    rl = {}
    if five is not None:
        rl["five_hour"] = {"used_percentage": five, "resets_at": int(base + 5 * HOUR)}
    if seven is not None:
        rl["seven_day"] = {"used_percentage": seven, "resets_at": int(base + 7 * DAY)}
    rl.update(extra)
    return {"session_id": "s1", "model": {"display_name": "Opus"}, "rate_limits": rl}


class MeterEpochTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_epoch_seconds_is_the_status_line_shape(self):
        self.assertEqual(self.ub._meter_epoch(1787026199), 1787026199.0)

    def test_iso_string_is_still_accepted(self):
        got = self.ub._meter_epoch("2026-08-17T21:09:59Z")
        self.assertIsInstance(got, float)
        self.assertEqual(int(got) % 60, 59)

    def test_garbage_and_none_degrade_to_none(self):
        for bad in (None, "", "not a date", {}, [], True):
            self.assertIsNone(self.ub._meter_epoch(bad), repr(bad))


class ParseStatuslineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_both_windows_are_extracted_exactly(self):
        got = self.ub._parse_statusline(payload(23.5, 41.2), WK)
        self.assertEqual(got["fetched_at"], WK)
        self.assertEqual(got["limits"]["five_hour"],
                         {"utilization": 23.5, "resets_at": WK + 5 * HOUR})
        self.assertEqual(got["limits"]["seven_day"]["utilization"], 41.2)
        self.assertEqual(got["unknown_kinds"], [])

    def test_absent_rate_limits_records_nothing(self):
        """Absent until the first API response, and always for API-key users.

        Absence says nothing about the meter, so it must not become a reading.
        """
        self.assertIsNone(self.ub._parse_statusline({"session_id": "s1"}, WK))
        self.assertIsNone(self.ub._parse_statusline({"rate_limits": None}, WK))
        self.assertIsNone(self.ub._parse_statusline({"rate_limits": {}}, WK))

    def test_a_gateway_spend_limit_is_not_a_subscription_window(self):
        p = payload(spend_limit={"used_percentage": 62.8, "resets_at": int(WK + 30 * DAY)})
        got = self.ub._parse_statusline(p, WK)
        self.assertNotIn("spend_limit", got["limits"])
        self.assertNotIn("spend_limit", got["unknown_kinds"])

    def test_an_unknown_window_kind_is_carried_and_named(self):
        p = payload(seven_day_fable={"used_percentage": 27, "resets_at": int(WK + 7 * DAY)})
        got = self.ub._parse_statusline(p, WK)
        self.assertIn("seven_day_fable", got["limits"])
        self.assertEqual(got["unknown_kinds"], ["seven_day_fable"])

    def test_malformed_windows_are_skipped_not_fatal(self):
        p = {"rate_limits": {"five_hour": {"used_percentage": "23"},
                             "seven_day": {"used_percentage": True},
                             "odd": "nope"}}
        self.assertIsNone(self.ub._parse_statusline(p, WK))


class RecordStatuslineTest(unittest.TestCase):
    """Every session's status line writes one file. The freshest reading wins."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def transcript(self, name, mtime):
        p = Path(self.tmp.name) / f"{name}.jsonl"
        p.write_text("{}\n")
        os.utime(p, (mtime, mtime))
        return str(p)

    def held(self):
        return json.loads(self.ub.METER_PATH.read_text())

    def test_a_reading_is_dated_by_its_transcript_not_the_clock(self):
        p = dict(payload(base=self.now), transcript_path=self.transcript("a", self.now - 120))
        self.assertTrue(self.ub.record_statusline(p, self.now))
        self.assertAlmostEqual(self.held()["fetched_at"], self.now - 120, places=0)

    def test_an_idle_session_cannot_overwrite_a_fresher_reading(self):
        """The failure this guards: a refreshInterval or mode change re-runs an
        idle session's status line with figures from its LAST response, hours
        old. Dated by the clock, that stale reading would replace a live one."""
        busy = dict(payload(40.0, base=self.now),
                    transcript_path=self.transcript("busy", self.now - 5))
        idle = dict(payload(12.0, base=self.now),
                    transcript_path=self.transcript("idle", self.now - 3 * HOUR))
        self.assertTrue(self.ub.record_statusline(busy, self.now))
        self.assertFalse(self.ub.record_statusline(idle, self.now))
        self.assertEqual(self.held()["limits"]["five_hour"]["utilization"], 40.0)

    def test_a_newer_reading_replaces_an_older_one(self):
        old = dict(payload(12.0, base=self.now),
                   transcript_path=self.transcript("old", self.now - HOUR))
        new = dict(payload(40.0, base=self.now),
                   transcript_path=self.transcript("new", self.now - 5))
        self.ub.record_statusline(old, self.now)
        self.assertTrue(self.ub.record_statusline(new, self.now))
        self.assertEqual(self.held()["limits"]["five_hour"]["utilization"], 40.0)

    def test_without_a_transcript_the_clock_is_the_fallback(self):
        self.assertTrue(self.ub.record_statusline(payload(base=self.now), self.now))
        self.assertEqual(self.held()["fetched_at"], self.now)

    def test_a_future_mtime_is_capped_at_now(self):
        p = dict(payload(base=self.now), transcript_path=self.transcript("f", self.now + DAY))
        self.ub.record_statusline(p, self.now)
        self.assertLessEqual(self.held()["fetched_at"], self.now)

    def test_a_payload_without_rate_limits_leaves_the_held_reading_alone(self):
        self.ub.record_statusline(payload(40.0, base=self.now), self.now)
        self.assertFalse(self.ub.record_statusline({"session_id": "x"}, self.now + 1))
        self.assertEqual(self.held()["limits"]["five_hour"]["utilization"], 40.0)


class MeterFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, fetched_at, limits):
        self.ub._save(self.ub.METER_PATH, {"fetched_at": fetched_at, "limits": limits})

    def test_a_stale_reading_is_not_served_as_exact(self):
        """A snapshot of a moving meter expires; silence beats a stale 'EXACT'."""
        now = time.time()
        self.save(now - self.ub.METER_MAX_AGE - 1, {"five_hour": {"utilization": 22.0}})
        self.assertIsNone(self.ub._load_meter(now))

    def test_a_fresh_reading_carries_its_age(self):
        now = time.time()
        self.save(now - 30, {"five_hour": {"utilization": 22.0}})
        self.assertEqual(self.ub._load_meter(now)["age_seconds"], 30)

    def test_an_empty_reading_is_ignored(self):
        self.save(time.time(), {})
        self.assertIsNone(self.ub._load_meter())

    def test_a_window_past_its_reset_is_dropped(self):
        """95% read five minutes before a reset describes a window that is gone."""
        now = time.time()
        self.save(now - 60, {
            "five_hour": {"utilization": 95.0, "resets_at": now - 30},
            "seven_day": {"utilization": 40.0, "resets_at": now + 3 * DAY},
        })
        got = self.ub._load_meter(now)
        self.assertNotIn("five_hour", got["limits"])
        self.assertIn("seven_day", got["limits"])

    def test_nothing_left_after_resets_means_no_meter(self):
        now = time.time()
        self.save(now - 60, {"five_hour": {"utilization": 95.0, "resets_at": now - 30}})
        self.assertIsNone(self.ub._load_meter(now))


class ApplyMeterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def state(self, sess_cost=60.0, wk_cost=90.0):
        return {
            "session": {"cost_usd": sess_cost, "capacity_usd": 300.0,
                        "used_frac": sess_cost / 300.0,
                        "remaining_usd": 300.0 - sess_cost,
                        "window_start": WK, "resets_at": WK + 18000,
                        "confidence": "medium", "samples": 2},
            "weekly": {"cost_usd": wk_cost, "capacity_usd": 1800.0,
                       "used_frac": wk_cost / 1800.0,
                       "remaining_usd": 1800.0 - wk_cost,
                       "window_start": WK, "resets_at": WK + 7 * 86400,
                       "anchored": True},
        }

    def applied(self, p=None, **kw):
        st = self.state(**kw)
        self.ub._apply_meter(st, self.ub._parse_statusline(p or payload(), WK))
        return st

    def test_utilization_replaces_the_estimate_exactly(self):
        st = self.applied()
        self.assertAlmostEqual(st["session"]["used_frac"], 0.22)
        self.assertTrue(st["session"]["exact"])
        self.assertEqual(st["session"]["confidence"], "exact")

    def test_capacity_is_derived_from_the_meter_not_calibration(self):
        """capacity = local_cost / exact_utilization.

        This is the identity that makes calibration unnecessary.
        """
        st = self.applied(sess_cost=60.0)
        self.assertAlmostEqual(st["session"]["capacity_usd"], 60.0 / 0.22, places=2)
        self.assertEqual(st["session"]["capacity_basis"], "meter-implied")

    def test_drift_against_calibration_is_reported(self):
        """The residual IS the price-weighting error (FINDINGS 6b/6c/6d)."""
        st = self.applied(sess_cost=60.0)
        self.assertLess(st["session"]["capacity_drift_pct"], 0)

    def test_low_utilization_does_not_imply_a_capacity(self):
        """At 1% a few dollars of spend implies a four-figure capacity.

        The percentage must still become exact; only the dollars stay calibrated.
        """
        st = self.applied(payload(1.0, None))
        self.assertAlmostEqual(st["session"]["used_frac"], 0.01)
        self.assertEqual(st["session"]["capacity_usd"], 300.0, "unchanged")
        self.assertIn("calibrated", st["session"]["capacity_basis"])

    def test_boundary_becomes_exact_and_stops_asking_for_a_reanchor(self):
        st = self.applied()
        self.assertEqual(st["boundary"]["confidence"], "exact")
        self.assertFalse(st["boundary"]["reanchor"])
        self.assertTrue(st["session"]["boundary_exact"])

    def test_a_reading_without_five_hour_leaves_the_boundary_alone(self):
        st = self.state()
        st["boundary"] = {"confidence": "estimated", "reanchor": True}
        self.ub._apply_meter(st, self.ub._parse_statusline(payload(None, 5.0), WK))
        self.assertEqual(st["boundary"]["confidence"], "estimated")

    def test_the_meter_no_longer_decides_wall_or_price(self):
        """Overage is the account's declaration (_overage), not a meter field."""
        st = self.applied()
        self.assertNotIn("overage", st)


class BuildStateOrderingTest(unittest.TestCase):
    """The meter has to land before the forecast that reads it.

    Applied after, the block printed exact dollars-left beside an hours-to-cap
    computed from the calibrated remainder, and the declared wall-or-price never
    reached the lockout forecast at all.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))
        self.now = time.time()
        # $1 a minute for ten minutes: a $60/h burn on $10 of window spend.
        self.events = [[self.now - m * 60, 1.0, "claude-opus-5"] for m in range(10, 0, -1)]

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, five, config=None):
        self.ub._save(self.ub.CONFIG_PATH, dict({"version": 2, "calibrations": [],
                                                 "session_anchor": self.now - HOUR},
                                                **(config or {})))
        self.ub._save(self.ub.METER_PATH, {
            "fetched_at": self.now - 10,
            "limits": {"five_hour": {"utilization": five, "resets_at": self.now + 4 * HOUR},
                       "seven_day": {"utilization": 10.0, "resets_at": self.now + 3 * DAY}},
        })
        return self.ub._build_state(self.events)

    def test_hours_to_cap_come_from_the_exact_remainder(self):
        st = self.build(40.0)
        s = st["session"]
        self.assertEqual(s["capacity_basis"], "meter-implied")
        self.assertIsNotNone(s["hours_to_exhaustion"],
                             "uncalibrated, so only the meter can supply a remainder")
        self.assertAlmostEqual(
            s["hours_to_exhaustion"],
            max(0.0, s["remaining_usd"]) / st["burn_usd_per_hour"], places=1)

    def test_a_cap_with_no_declaration_is_a_lockout(self):
        st = self.build(90.0)
        self.assertIsNotNone(st["binding"])
        self.assertNotIn("cost_ahead", st)

    def test_declared_extra_usage_turns_the_lockout_into_a_cost(self):
        st = self.build(90.0, {"extra_usage": True})
        self.assertIsNone(st["binding"])
        self.assertEqual(st["cost_ahead"]["bucket"], "5h window")
        self.assertEqual(st["overage"], {"enabled": True, "source": "declared"})

    def test_an_exact_reading_with_no_capacity_still_renders(self):
        """Uncalibrated, nothing spent yet this window, meter fresh: the start of
        every window on a new install. The render used to format a None
        remainder, raise, and inject printed NOTHING -- the whole block gone."""
        self.events = []
        out = self.ub.render_inject(self.build(11.0))
        self.assertIn("11% used (EXACT", out)
        self.assertIn("10% used (EXACT)", out)
        self.assertNotIn("None", out)


class OverageDeclarationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_unset_is_unknown_and_unknown_is_not_a_declaration(self):
        for cfg in ({}, {"extra_usage": None}, {"extra_usage": "yes"}, None):
            self.assertIsNone(self.ub._overage(cfg), repr(cfg))

    def test_on_and_off_are_read_as_declared(self):
        self.assertEqual(self.ub._overage({"extra_usage": True}),
                         {"enabled": True, "source": "declared"})
        self.assertFalse(self.ub._overage({"extra_usage": False})["enabled"])


class StatuslineCommandTest(unittest.TestCase):
    """The command runs as the user's status line, so test it as a process."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, USAGE_BUDGET_HOME=self.tmp.name)
        self.meter = Path(self.tmp.name) / "meter.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cmd(self, stdin: bytes, *wrapped):
        args = [sys.executable, str(SCRIPT), "statusline"]
        if wrapped:
            args += ["--", *wrapped]
        return subprocess.run(args, input=stdin, capture_output=True, env=self.env,
                              timeout=30)

    def test_it_records_and_prints_a_default_line(self):
        r = self.run_cmd(json.dumps(payload(23.5, 41.2, base=time.time())).encode())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.decode().strip(), "5h 24% · week 41%")
        self.assertTrue(self.meter.exists())

    def test_a_wrapped_command_gets_the_exact_bytes_and_its_exit_code(self):
        raw = json.dumps(payload(base=time.time())).encode()
        echo = "import sys; d = sys.stdin.buffer.read(); sys.stdout.write(str(len(d))); sys.exit(3)"
        r = self.run_cmd(raw, sys.executable, "-c", echo)
        self.assertEqual(r.stdout.decode(), str(len(raw)))
        self.assertEqual(r.returncode, 3)
        self.assertTrue(self.meter.exists())

    def test_garbage_on_stdin_never_breaks_the_wrapped_status_line(self):
        r = self.run_cmd(b"not json", sys.executable, "-c", "print('mine')")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.decode().strip(), "mine")
        self.assertFalse(self.meter.exists())


class StatuslineWiredTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))
        self.settings = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def wired(self, cfg):
        self.settings.write_text(json.dumps(cfg))
        return self.ub._statusline_wired(self.settings)

    def test_wrapped_and_bare_forms_count(self):
        for cmd in ("python3 /x/usage_budget.py statusline",
                    "python3 /x/usage_budget.py statusline -- sh -c '~/.claude/statusline.sh'"):
            self.assertTrue(self.wired({"statusLine": {"type": "command", "command": cmd}}), cmd)

    def test_someone_elses_status_line_does_not(self):
        self.assertFalse(self.wired({"statusLine": {"command": "~/.claude/statusline.sh"}}))
        self.assertFalse(self.wired({}))


class MeterRenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ub = load_module(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def render(self):
        st = {
            "session": {"cost_usd": 60.0, "capacity_usd": 300.0,
                        "used_frac": 0.17, "remaining_usd": 240.0,
                        "window_start": WK, "resets_at": WK + 18000,
                        "confidence": "medium", "samples": 2},
            "weekly": {"cost_usd": 90.0, "capacity_usd": 1800.0,
                       "used_frac": 0.05, "remaining_usd": 1710.0,
                       "window_start": WK, "resets_at": WK + 7 * 86400,
                       "anchored": True},
            "burn_usd_per_hour": 0.0,
        }
        self.ub._apply_meter(st, self.ub._parse_statusline(payload(), WK))
        st["meter"]["age_seconds"] = 30
        return self.ub.render_inject(st)

    def test_exact_figures_are_labelled_exact(self):
        out = self.render()
        self.assertIn("22% used (EXACT", out)
        self.assertNotIn("~22%", out)

    def test_the_trailer_stops_saying_never_present_these_as_exact(self):
        """The block contradicted itself: EXACT above, 'never exact' below."""
        out = self.render()
        self.assertNotIn("Never present these as exact", out)
        self.assertIn("may be quoted as fact", out)

    def test_dollars_are_still_flagged_as_derived(self):
        """Exact percentages do not make the dollar figures exact."""
        self.assertIn("DOLLAR figures remain derived", self.render())

    def test_the_calibration_caveat_is_replaced_by_the_meter_line(self):
        out = self.render()
        self.assertIn("Meter: read from the status line", out)
        self.assertNotIn("Recalibrate from /usage", out)
        self.assertNotIn("re-anchor from /usage", out)

    def test_drift_is_reported_with_its_quantisation_band(self):
        """Utilization is whole-percent, so the implied capacity is coarse.

        Stating drift without its noise floor invites over-reading it.
        """
        self.assertIn("Price-weighting is off by", self.render())


if __name__ == "__main__":
    unittest.main()
