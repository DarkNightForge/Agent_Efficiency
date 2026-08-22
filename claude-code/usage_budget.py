#!/usr/bin/env python3
"""
usage-budget — estimate remaining Claude subscription usage from local transcripts.

WHY THIS EXISTS
    Claude Code knows how much of your plan is used -- `/usage` shows it and the
    status line command is handed it -- but none of it reaches the model's
    context, and hooks are not given it. So the agent works blind to the one
    number that decides whether a big task fits.

    Two local sources close that gap. Every assistant turn is logged to
    ~/.claude/projects/**/*.jsonl with an exact `usage` block and timestamp, and
    the status line receives the vendor's own utilization and reset times as
    `rate_limits` on stdin, which `usage-budget statusline` records. Verified
    on disk against Claude Code v2.1.234: the transcript `usage` block
    carries no rate-limit fields, and a window boundary is server state that
    local logs cannot place (FINDINGS 4c): the meter reports it as `resets_at`.

    So: the NUMERATOR (what you spent) is computable exactly. The DENOMINATOR
    (window capacity) is unpublished -> while a status line reading is fresh it
    is implied by the exact utilization; otherwise you calibrate it from `/usage`.

WHAT IS EXACT vs ESTIMATED
    exact      tokens, timestamps, model, speed, cache TTL split, subagent turns
    exact      per-turn cost -- validated to the cent against /usage's own
               session total ($11.90) on 2026-08-12
    exact      window boundaries, BUT ONLY once anchored (see below)
    estimated  window capacity (calibrated), and the assumption that the limit is
               proportional to list-price-weighted spend

    EXACT IS NOT THE SAME AS CURRENT. `inject` runs on UserPromptSubmit (before
    the model replies) and `collect` on Stop (after), so the numerator is
    complete through the END OF THE LAST COMPLETED TURN -- the turn in flight is
    never counted. The lag is exactly one turn, and it only ever under-reports.
    Measured at ~0.02-0.10 percentage points, i.e. ~100x below the +/-10pt
    calibration band, so it is not worth paying to correct; but it does mean the
    BURN RATE cannot see an expensive operation until after it is paid for.
    FINDINGS 12.

WINDOW BOUNDARIES -- corrected twice (2026-08-12 grid, 2026-08-13 chain)
    The documented rule ("a window opens with your first message and lasts 5h")
    is literally true -- but "first message" means first on the ACCOUNT, across
    every surface (claude.ai chat included; limits are officially shared). Local
    transcripts cannot see other surfaces, so a locally-derived chain propagates
    the wrong phase; and under continuous use, back-to-back windows LOOK like a
    fixed grid. That grid was this tool's model for a day -- one confirmed
    prediction -- until a 8:10pm reset refuted it (FINDINGS 4c).

    Practical model: `--resets` SETS the anchor from a /usage reading, and grid
    bucketing from that anchor is exact UNTIL THE NEXT GAP in all-account
    activity -- a gap local logs cannot detect. Re-anchor at the start of each
    work block. Unanchored => chain heuristic over local logs, biased, labelled
    `estimated`. /usage may DISPLAY a boundary a minute low (":59"-style
    rendering artifact, FINDINGS 4b). The weekly boundary is a fixed per-account
    reset (documented) and does not chain.

    Likewise the weekly window is a FIXED period, not a rolling 7 days: over the
    Aug 10->17 week, the rolling sum measured about twice the real week, a 2x error.

    We use list-price-weighted dollars as the weighting proxy. The absolute scale
    is wrong until you calibrate; calibration cancels it out entirely. What
    calibration can NOT fix is if the real limit weights token classes
    differently than price does -- that shows up as drift between calibrations,
    which is why we track and report calibration spread as a confidence signal.

MODES
    collect      rescan transcripts (incremental), write state.json
    inject       emit <usage-budget> block for a UserPromptSubmit hook
    status       human-readable summary
    calibrate    set window anchors and/or teach capacity from a /usage reading
    reset        clear calibration history

Part of https://github.com/DarkNightForge/Agent_Efficiency — see
docs/FINDINGS.md for the empirical basis of the cost formula and the
window model. MIT licensed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
# Runtime state lives outside the code so this file can be checked into a repo
# and run from anywhere. Override either path for testing or a non-standard
# Claude Code install.
BASE = Path(os.environ.get("USAGE_BUDGET_HOME") or HOME / ".claude" / "usage-budget")
PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS_DIR") or HOME / ".claude" / "projects")
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
CACHE_PATH = BASE / "cache.json"

# Keep 9 days of events: 7 for the weekly window + 2 days of slack.
RETAIN_DAYS = 9
SESSION_HOURS = 5
WEEKLY_DAYS = 7

# How soon after a window expires a message must land for the re-derived
# boundary to still count as tight. Longer than this and an unseen seed on
# another surface could have opened the window materially earlier.
RESEED_TIGHT_SECONDS = 300

# How long you would have to sit blocked at a cap before it is worth reshaping
# the work. Hitting a cap costs you the gap between running out and the reset --
# nothing else. Run out 10 minutes before a window rolls over and you have lost
# 10 minutes; the next window arrives with a full allowance. Below this
# threshold, the right advice is to carry on.
LOCKOUT_WARN_HOURS = 0.75

# Promotion has a HIGHER bar than warning (FINDINGS 17). Leading every block with
# a maximally imperative directive is exactly what earns a directive its
# ignoring -- the same reasoning that keeps the boundary caveat quiet unless the
# boundary is genuinely doubtful. Observed live: a 1h57m lockout forecast 151
# hours out, off a burn rate measured minutes into a fresh window, took the top
# line of the block. Credible and consequential, or it stays buried.
PROMOTE_MIN_BLOCKED_HOURS = 2.0
# Beyond a day, a burn-rate extrapolation is not solid enough to lead with -- the
# code already concedes this by switching the weekly buckets to a duty-cycle rate.
# But the horizon may only suppress SMALL lockouts. This project's own rule is
# "read the lockout, not the cap": an 88-hour block is worth leading with whenever
# it is coming, and a first cut of this gate demoted exactly that case because the
# cap was 56 hours out. The test suite caught it.
PROMOTE_MAX_HORIZON_HOURS = 24.0
PROMOTE_ALWAYS_BLOCKED_HOURS = 6.0
# The gate above fixed WHEN a lockout forecast may lead the block. It did not
# fix the forecast. The 5h window is still projected off the trailing rate (by
# design -- a 5-hour horizon makes "you will keep going at this rate" fair), and
# that rate divides trailing spend by the time from its FIRST event to now,
# floored at one minute. So two turns 38 seconds apart annualised to $197/h and
# promoted a 1h05m lockout, live, on 2026-08-22. Measured from the cache: the
# window's first turn was a session RESUME costing $2.89 -- 68% of the window's
# entire spend -- because a resumed turn writes its whole cache prefix instead of
# reading it (§25). That turn is the most expensive of the session and is
# guaranteed not to recur, and it lands when the sample is one turn wide. The
# promoted directive was therefore MOST likely to fire in the first two minutes
# of a resumed session -- precisely when the user can see the forecast is absurd,
# which is the credibility §17 spent an A/B on.
# A rate is only a rate over a span. Floor the denominator: short enough to stay
# responsive to a genuine burst, long enough that one turn is not an hourly rate.
BURN_WINDOW_MINUTES = 30
BURN_MIN_SPAN_MINUTES = 5
# And leading the block needs the forecast to be MEASURED, not merely computable.
# Below this the trailing rate is a couple of turns wide, so the lockout falls
# back to the quiet Heads-up line rather than opening the reply. Demote, never
# suppress -- same treatment §17 gave a real-but-small lockout.
PROMOTE_MIN_BURN_SPAN_MINUTES = 10
PROMOTE_MIN_BURN_TURNS = 3
# Re-fire the promoted directive only when it says something new. Without this
# it leads EVERY turn with the same sentence -- which is the failure the ask gate
# already avoids, and which was observed live across four consecutive replies.
BINDING_REASK_WORSE = 1.5   # blocked hours must grow 50% to speak again

# --- Posture (FINDINGS 20) -------------------------------------------------
# The tool shipped one policy: conserve-ish, with GO WIDE when ample. That fits
# nobody exactly. A user paying for maximum throughput does not want pacing, and
# a user rationing a plan across a week wants more of it than the default gives.
# This is a genuine value judgement -- unlike the wall-vs-price question, which
# is a fact about the account (see _overage) -- so it is a stored preference,
# not a recurring prompt.
POSTURES = ("throughput", "balanced", "stretch")
DEFAULT_POSTURE = "balanced"


def _posture(config: dict) -> str:
    p = (config or {}).get("posture")
    return p if p in POSTURES else DEFAULT_POSTURE


def _overage(config: dict) -> dict | None:
    """Is a cap a WALL or a PRICE? The user's declaration, or None if unknown.

    The whole lockout apparatus -- `blocked_hours`, WOULD RUN OUT, the pacing
    checkpoint -- assumes hitting a cap stops you dead. For an account with
    usage credits enabled that is simply FALSE: the cap is a price change, and
    warning about a lockout that cannot happen is not a policy mismatch, it is a
    wrong statement of fact.

    The status line does not carry this, so it is declared once with
    `usage-budget extra-usage on|off`. Unknown is treated as a wall: a needless
    warning costs a sentence, a missing one costs the rest of the week.
    """
    v = (config or {}).get("extra_usage")
    if not isinstance(v, bool):
        return None
    return {"enabled": v, "source": "declared"}


def _ask_ratio(posture: str) -> float | None:
    """Share of today's allowance at which the checkpoint fires. None = never."""
    if posture == "throughput":
        return None          # this user is buying speed; do not interrupt them
    if posture == "stretch":
        return 0.80          # rationing across the week wants earlier notice
    return PACE_ASK_RATIO

# Precision of the affordance line (FINDINGS 28).
#
# Percentages are the vendor's own only while a status line reading is fresh.
# After an idle gap longer than METER_MAX_AGE the block falls back to
# calibration -- and the FIRST prompt back ALWAYS does, because the status line
# refreshes when a response arrives while the block renders on UserPromptSubmit,
# before one has. That is the turn a user is deciding what to do next, so it is
# the worst possible turn to print a soft number in a hard typeface.
#
# The band is not invented: _calibrated() already classifies capacity error by
# calibration spread -- high is <=10%, medium <=20%, low is anything worse. These
# reuse those exact thresholds, so the range reported is the error already
# measured rather than a new guess.
AFFORDANCE_BAND = {"exact": 0.0, "high": 0.10, "medium": 0.20,
                   "low": 0.35, "none": 0.35}
_CONF_ORDER = ("exact", "high", "medium", "low", "none")


def _weaker_confidence(*buckets) -> str:
    """Worst confidence among the buckets that could bind. The affordance is only
    as sound as the weakest input to the MIN() that picks the ceiling."""
    worst = 0
    for b in buckets:
        c = (b or {}).get("confidence", "none")
        worst = max(worst, _CONF_ORDER.index(c) if c in _CONF_ORDER
                    else len(_CONF_ORDER) - 1)
    return _CONF_ORDER[worst]


# --- Steering what a compaction keeps (FINDINGS 26, 29) --------------------
#
# The ONE lever the host leaves open. A hook cannot schedule a compaction
# (FINDINGS 26: every route is closed), but PreCompact's stdout is passed to the
# summariser as additional instructions, on both the auto and manual paths. That
# is not in the hooks documentation, so treat it as unverified on any given
# Claude Code version; its benefit is unverified too (FINDINGS 29).
#
# Worth using because compaction retains a measured median 7.6% of prior context
# (FINDINGS 25) and the summariser picks which 7.6%. The cost of a bad pick is
# not the lost tokens -- it is REWORK: an agent that loses "we measured X" will
# measure X again, and an agent that loses "we tried Y and it failed" will try Y
# again. Both cost exactly what they cost the first time. That is the one context
# expense this project had not addressed, and unlike the ideas closed in 24/26/27
# there is a supported mechanism for it.
#
# Written as priorities rather than prohibitions. FINDINGS 17 measured obedience
# going from 29% to 92% on position and mood alone; a list of "do not drop"
# clauses reads as scolding and competes with the host's own prompt, while a
# ranked "keep this first" cooperates with it.
PRECOMPACT_INSTRUCTIONS = """\
Rank what you keep by what it would cost to recover. Anything the next turns can
re-derive cheaply is a candidate to drop; anything that would take re-running
work is not.

Keep first, in this order:

1. MEASUREMENTS AND THEIR VALUES. Every figure this session established by
   actually running something -- counts, sizes, timings, test results, benchmark
   numbers -- with enough context to know what was measured and under what
   conditions. A dropped number gets measured again at full price.
2. RULED-OUT APPROACHES, and why they failed. These are the most expensive thing
   to lose: without them the same dead end is walked a second time, at the same
   cost, and the loss is invisible until it has already happened.
3. DECISIONS AND THEIR GROUNDS -- what was chosen, what was rejected, on what
   evidence -- so a settled question is not reopened as an open one.
4. EXACT IDENTIFIERS: file paths, function and symbol names, commit SHAs, line
   numbers, command invocations, error strings, config keys. Cheap to carry,
   expensive to find again.
5. STATE OF THE WORK: what is committed versus uncommitted, what has actually
   been verified versus assumed, and what was in flight at this moment.

When something has to go, drop the account of HOW the work proceeded and keep
WHAT IT ESTABLISHED. A narrative of steps taken is the cheapest thing here to
reconstruct and the least useful to carry.

This changes WHAT you keep, not HOW MUCH you write. Produce a summary the same
length you otherwise would: each item kept under these priorities should displace
something less costly to recover, not add to the total. A longer summary is not a
better one -- it is re-read on every turn that follows, so length spent here is
paid for repeatedly."""

# An auto-compaction fires mid-task, between tool calls, without anyone asking
# for it (FINDINGS 26) -- so the in-flight state is the part most likely to be
# lost and least likely to be reconstructable. A manual one is usually run at a
# boundary the user chose, where that risk is lower.
PRECOMPACT_AUTO_SUFFIX = """

This compaction was triggered automatically, mid-task, rather than at a chosen
stopping point. Be especially concrete about what was in progress at the moment
it fired: the immediate next action, and any step that was started but not
finished."""


def precompact_instructions(trigger: str | None = None) -> str:
    """Instruction text for a PreCompact hook to emit on stdout."""
    text = PRECOMPACT_INSTRUCTIONS
    if (trigger or "").strip().lower() == "auto":
        text += PRECOMPACT_AUTO_SUFFIX
    return text


# --- Affordance and pacing (FINDINGS 13) ----------------------------------
# Reporting headroom in weighted dollars asks the agent to do a unit conversion
# it has no basis for. It plans in TURNS and in FAN-OUTS, so convert for it.
#
# A nominal fan-out, used only to price "can I go wide?" in units the agent
# recognises. Deliberately a fixed yardstick rather than a prediction: the point
# is a comparable ruler across turns, not a forecast of one specific workflow.
FANOUT_AGENTS = 16
FANOUT_TURNS_EACH = 12
# Subagent turns carry less context than main-loop turns -- no accumulated
# conversation, no skill loads.
#
# MEASURED 2026-08-17 over all retained transcripts: 16,409 main-loop turns at a
# $0.1449 median against 10,551 subagent turns at $0.0600 -- a ratio of **0.414**.
# Rounded UP to 0.45 so the fan-out estimate errs expensive: over-pricing a
# fan-out under-promises how many fit, which costs a little thoroughness, while
# under-pricing one walks into a lockout. Asymmetric error, asymmetric rounding.
#
# This constant previously read 0.6 and its comment claimed to be measured when
# it was not -- an assumption in the costume of a measurement, in a project whose
# rule is to measure before modelling. Caught by checking it.
FANOUT_TURN_MULT = 0.45
# Go wide only with room for several such fan-outs, so a GO-WIDE call is never
# immediately followed by a WOULD RUN OUT.
GO_WIDE_MARGIN = 3.0
# ...and only while the week is comfortably under its prorated share.
GO_WIDE_PACE_MAX = 0.85
# Ask the user once the week is over the share of it that has elapsed.
PACE_ASK_RATIO = 1.0
# Re-ask only after the overshoot materially worsens. Without hysteresis the
# prompt fires every turn, and a prompt that always fires gets click-through
# approved -- the same failure the boundary caveat avoids by staying silent.
PACE_REASK_STEP = 0.25
# Floor on the RATE denominator. A weekly projection extrapolated from a
# fraction of a day is noise, and 1.0 is also day 1's day_index -- so the floor
# leaves day-1 output bit-identical while letting days 2-7 use real elapsed time.
PACE_MIN_RATE_DAYS = 1.0
# A window has to hold enough turns for a median to mean anything.
MIN_TURNS_FOR_MEDIAN = 3

# --- Fit, not rate (FINDINGS 20) -------------------------------------------
# Every signal before this one was about RATE -- how fast you are burning. None
# was about FIT -- whether the job in front of you will finish. So the only move
# available to an agent near the wall was "go slower", which is an adverb, not a
# plan: it moves the wall out slightly and makes the stopping point LESS
# predictable. Measured harm, 2026-08-17: an agent was told the budget was tight,
# said it would "proceed economically", did exactly that, and the user's work was
# still cut off mid-task.
#
# Budget held back so the agent can stop CLEANLY -- commit, write state, say
# where it got to. Running out mid-edit is far worse than running out after a
# commit, and the difference is a few turns of reserve.
LANDING_RESERVE_FRAC = 0.10
LANDING_RESERVE_MIN_TURNS = 8
# Usable-turn thresholds at which the affordance line changes its tone. A line
# that reads identically at 1900 turns and at 10 is not a warning system.
FIT_SHORT_TURNS = 100
FIT_LANDING_TURNS = 25

# --- The vendor's own meter, read from the status line (FINDINGS 15, 34) ----
# Claude Code hands its status line command `rate_limits` on stdin: the EXACT
# utilization and EXACT reset time of the 5-hour and 7-day windows
# (code.claude.com/docs/en/statusline; present since Claude Code 2.1.80, for
# Pro and Max subscribers, after a session's first API response). Everything
# this tool calibrates for is published there. `usage-budget statusline`
# records each reading; the hook path only reads the file. A fresh reading
# replaces the estimate with ground truth -- and, more valuably, lets the
# price-weighted reconstruction be scored against the real meter continuously
# instead of once per manual /usage reading.
METER_PATH = BASE / "meter.json"
# Past this the reading is a stale snapshot of a moving meter, so the block falls
# back to the local estimate rather than presenting an old number as exact.
METER_MAX_AGE = 900
# Every reading scores the price-weighted reconstruction against the real meter
# (see _apply_meter). Keeping those residuals is what lets the block state its
# own error AFTER the reading goes stale -- which is exactly when it needs to.
DRIFT_PATH = BASE / "drift.json"
DRIFT_KEEP = 20
# The subscription windows the status line documents. Any other kind it sends is
# carried through and named rather than dropped: the set is the vendor's to
# extend, and dropping what we do not recognise is how a tool goes quietly stale.
METER_KINDS = ("five_hour", "seven_day")
# A Claude apps gateway's spend cap rides the same object, but it is a different
# meter with its own period; folding it into the 5h/weekly buckets would be wrong.
METER_SKIP_KINDS = ("spend_limit",)

# ---------------------------------------------------------------------------
# Pricing, per million tokens (USD list price).
#
# Cache multipliers are relative to that model's input price and are
# model-independent: 5m write = 1.25x, 1h write = 2.0x, read = 0.1x.
#
# We deliberately use STANDARD list price, not promotional/intro pricing --
# a billing promo shouldn't move a rate-limit weighting, and calibration
# absorbs any constant scale error anyway.
# ---------------------------------------------------------------------------
PRICES = {
    "claude-opus-5":       {"in": 5.00,  "out": 25.00},
    "claude-opus-5:fast":  {"in": 10.00, "out": 50.00},
    "claude-opus-4-8":     {"in": 5.00,  "out": 25.00},
    "claude-opus-4-8:fast": {"in": 10.00, "out": 50.00},
    "claude-opus-4-7":     {"in": 5.00,  "out": 25.00},
    "claude-opus-4-6":     {"in": 5.00,  "out": 25.00},
    "claude-opus-4-5":     {"in": 5.00,  "out": 25.00},
    "claude-fable-5":      {"in": 10.00, "out": 50.00},
    "claude-mythos-5":     {"in": 10.00, "out": 50.00},
    "claude-sonnet-5":     {"in": 3.00,  "out": 15.00},
    "claude-sonnet-4-6":   {"in": 3.00,  "out": 15.00},
    "claude-sonnet-4-5":   {"in": 3.00,  "out": 15.00},
    "claude-haiku-4-5":    {"in": 1.00,  "out": 5.00},
}
FALLBACK_PRICE = {"in": 5.00, "out": 25.00}  # assume Opus-tier if unknown

CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.00
CACHE_READ_MULT = 0.10
WEB_SEARCH_USD = 0.01  # $10 / 1000 requests


def _norm_model(model: str | None, speed: str | None) -> str:
    """Normalize a transcript model id to a PRICES key.

    Strips context-window suffixes like `[1m]` and appends `:fast` for fast mode,
    which is priced at 2x on Opus-tier models.

    Also strips a trailing `-YYYYMMDD` release date, but ONLY when doing so lands
    on a known key. Transcripts carry both forms -- `claude-opus-5` alongside
    `claude-haiku-4-5-20251001` -- and an unstripped dated id misses PRICES and
    silently takes FALLBACK_PRICE, which is Opus-tier. That prices Haiku at 5x
    its real rate, in the *expensive* direction, which is the direction a budget
    tool must never be wrong in. Guarding on membership means a genuinely
    unknown model still falls back rather than being mangled into a wrong match.
    """
    if not model:
        return "unknown"
    m = re.sub(r"\[.*?\]", "", model).strip()
    if m not in PRICES:
        undated = re.sub(r"-\d{8}$", "", m)
        if undated in PRICES:
            m = undated
    if speed == "fast" and f"{m}:fast" in PRICES:
        return f"{m}:fast"
    return m


def _price(model_key: str) -> dict:
    return PRICES.get(model_key, FALLBACK_PRICE)


def _ctx_tokens(usage: dict) -> int:
    """Total context an assistant turn saw, from its `usage` payload.

    `cache_read + input` is NOT enough, and the gap is not small. Any turn that
    (re)writes the prefix to cache carries those tokens in `cache_creation`
    instead -- the first turn of a session, the turn after an idle gap or a 1h
    TTL expiry, the turn after a compaction -- and a read+input formula then
    reports a fraction of the real context. Measured on this repo's own working
    session: **24,815 reported against a true 215,041, 88.5% invisible.**

    Third instance of this artifact. It is what made FINDINGS 25 read a
    compaction landing point (24,702) BELOW the prefix that landing had to
    contain (28,918), and what put a measured session floor at 1,781 against
    the 58,377 every real turn carried (FINDINGS 32). Hence one formula, used
    by every caller that wants a context size.

    Distinct from the `ctx` in `_session_profiles`, which is deliberately
    cache_read alone: that one attributes the cost of tokens actually RE-READ,
    and a cache write is not a re-read.
    """
    cc = usage.get("cache_creation") or {}
    created = ((cc.get("ephemeral_5m_input_tokens") or 0)
               + (cc.get("ephemeral_1h_input_tokens") or 0)
               or (usage.get("cache_creation_input_tokens") or 0))
    return ((usage.get("input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0) + created)


def _cost_of(usage: dict, model_key: str) -> float:
    """List-price-equivalent USD for one assistant turn.

    Every field here is read straight from the transcript -- no estimation.
    """
    p = _price(model_key)
    pin, pout = p["in"], p["out"]

    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    cread = usage.get("cache_read_input_tokens") or 0

    # Prefer the TTL-split breakdown when present: a 1h cache write costs 2x
    # input while a 5m write costs 1.25x, so collapsing them loses real money.
    cc = usage.get("cache_creation") or {}
    cw5 = cc.get("ephemeral_5m_input_tokens")
    cw1 = cc.get("ephemeral_1h_input_tokens")
    if cw5 is None and cw1 is None:
        cw5 = usage.get("cache_creation_input_tokens") or 0
        cw1 = 0
    cw5 = cw5 or 0
    cw1 = cw1 or 0

    usd = (
        inp * pin
        + out * pout
        + cread * pin * CACHE_READ_MULT
        + cw5 * pin * CACHE_WRITE_5M_MULT
        + cw1 * pin * CACHE_WRITE_1H_MULT
    ) / 1_000_000.0

    st = usage.get("server_tool_use") or {}
    usd += (st.get("web_search_requests") or 0) * WEB_SEARCH_USD
    return usd


# ---------------------------------------------------------------------------
# JSON helpers (atomic writes; a half-written state file would break the hook)
# ---------------------------------------------------------------------------

def _load(path: Path, default):
    try:
        with path.open() as fh:
            return json.load(fh)
    except Exception:
        return default


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    os.replace(tmp, path)


def default_config() -> dict:
    return {
        "version": 2,
        "session_hours": SESSION_HOURS,
        "weekly_days": WEEKLY_DAYS,
        "calibrations": [],
        "notes": "capacity_usd values are list-price-equivalent proxies, not billing.",
    }


def load_config() -> dict:
    """Load config, applying the FINDINGS 11 rebase once and persisting it."""
    cfg = _load(CONFIG_PATH, default_config())
    if cfg.get("version", 1) < 2:
        cfg, notes = migrate_calibrations(cfg)
        _save(CONFIG_PATH, cfg)
        if notes:
            print("calibration samples rebased onto the deduplicated "
                  "numerator (FINDINGS 11):", file=sys.stderr)
            for n in notes:
                print(f"  {n}", file=sys.stderr)
    return cfg


def _calibration_cache_read_share(config: dict) -> float | None:
    """Median cache-read share of weighted cost across live session samples.

    predict() used to state this as a hardcoded "~79%" while computing every
    other figure it printed. No sample ever supported that number -- the real
    shares are nearer 57-64% -- so it is measured here instead (FINDINGS 11).
    """
    shares = []
    for c in config.get("calibrations", []):
        if c.get("retired") or c.get("scope") != "session":
            continue
        ws = c.get("window_start")
        if not ws:
            continue
        try:
            at = datetime.fromisoformat(c["at"]).timestamp()
        except Exception:
            continue
        mix = _window_token_mix(ws, at + 1)
        total = _window_cost(ws, at + 1)
        if total <= 0:
            continue
        cread = sum(m["cread"] * _price(mk)["in"] * CACHE_READ_MULT / 1e6
                    for mk, m in mix["models"].items())
        shares.append(cread / total)
    if not shares:
        return None
    shares.sort()
    n = len(shares)
    return shares[n // 2] if n % 2 else (shares[n // 2 - 1] + shares[n // 2]) / 2


def _weekly_period_start(config: dict, at: float) -> float | None:
    """Start of the 7-day weekly period containing `at`, from the anchor."""
    anchor = config.get("weekly_anchor")
    if not anchor:
        return None
    span = WEEKLY_DAYS * 86400
    start = anchor - span
    while start > at:
        start -= span
    while start + span <= at:
        start += span
    return start


def migrate_calibrations(config: dict) -> tuple[dict, list]:
    """Rebase pre-dedup calibration costs onto the deduplicated numerator.

    Every sample taken before FINDINGS 11 paired a /usage percentage with a
    cost that counted each assistant message once per content block. Those
    costs are ~2-2.5x too high, and because capacity is back-solved from them,
    leaving them in place makes the tool UNDER-report usage -- the unsafe
    direction. So each sample is rebased.

    A sample is only rebased if its stored window still reproduces its stored
    cost under the OLD counting rule. That check is what catches a second
    defect: `calibrate` stamps `window_start` from the window live at the time
    it runs, so a reading taken just after a rollover records an anchor that
    does not contain the turns its cost came from. Such a sample cannot be
    rebased and is retired rather than silently rescaled.

    Windows older than RETAIN_DAYS have no transcripts left to recount, so they
    are retired too. Returns (config, notes).
    """
    notes: list = []
    if config.get("version", 1) >= 2:
        return config, notes

    for c in config.get("calibrations", []):
        if c.get("retired"):
            continue
        scope, ws, old = c.get("scope"), c.get("window_start"), c.get("cost")
        if not old:
            continue
        try:
            at = datetime.fromisoformat(c["at"]).timestamp()
        except Exception:
            continue
        models = None
        if scope in ("weekly", "weekly_premium"):
            # Weekly samples carry no window_start, but the period is derivable
            # from the anchor by stepping back whole weeks. Retention is 9 days
            # against a 7-day window, so a sample from this week IS recountable
            # -- which matters, because weekly is usually the binding meter.
            ws = _weekly_period_start(config, at)
            if scope == "weekly_premium":
                models = _premium_models(config)
        if not ws or at - ws > RETAIN_DAYS * 86400:
            c["retired"] = ("cost measured under the pre-dedup counter "
                            "(FINDINGS 11); window predates retained "
                            "transcripts, so it cannot be recounted")
            c["retired_at"] = datetime.now(timezone.utc).isoformat()
            notes.append(f"{scope} {c.get('pct')}% — retired, outside retention")
            continue
        raw = _window_cost(ws, at + 1, dedupe=False, models=models)
        new = _window_cost(ws, at + 1, dedupe=True, models=models)
        if raw <= 0 or abs(raw - old) / old > 0.10:
            c["retired"] = (
                f"stored window_start does not reproduce the stored cost "
                f"(recounted ${raw:.2f} vs ${old:.2f}) — the anchor was "
                f"stamped from the window live at calibrate time, not the "
                f"window the reading came from (FINDINGS 11)")
            c["retired_at"] = datetime.now(timezone.utc).isoformat()
            notes.append(f"{scope} {c.get('pct')}% — retired, anchor mismatch")
            continue
        c["cost_pre_dedupe"] = old
        c["cost"] = round(new, 4)
        c["rebased"] = "dedupe"
        notes.append(f"{scope} {c.get('pct')}% — ${old:.2f} → ${new:.2f} "
                     f"({old / new:.2f}x)")

    config["version"] = 2
    return config, notes


# ---------------------------------------------------------------------------
# Transcript scanning -- incremental, byte-offset based.
#
# Transcripts are append-only, so we remember (size, offset) per file and only
# parse the bytes that are new. A full cold scan of ~165MB takes ~30s; a warm
# incremental pass is milliseconds. That difference is what makes this viable
# as a per-prompt hook.
# ---------------------------------------------------------------------------

def _scan_file(path: Path, start_offset: int) -> tuple[list, int]:
    """Parse assistant-turn usage events from `path`, starting at byte offset.

    Returns (events, new_offset) where each event is [epoch_int, usd, model_key].
    Only consumes up to the last complete newline so a turn being written right
    now isn't half-parsed.
    """
    events: list = []
    try:
        size = path.stat().st_size
    except OSError:
        return events, start_offset
    if start_offset >= size:
        return events, start_offset

    with path.open("rb") as fh:
        fh.seek(start_offset)
        chunk = fh.read()

    # Never consume a partial trailing line.
    cut = chunk.rfind(b"\n")
    if cut == -1:
        return events, start_offset
    consumed = start_offset + cut + 1
    chunk = chunk[: cut + 1]

    for line in chunk.split(b"\n"):
        # Cheap bytes-level prefilter: skips user turns, tool results, meta
        # lines. Roughly 60% of lines never reach json.loads.
        if b'"usage"' not in line or b'"assistant"' not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        ts = rec.get("timestamp")
        if not ts:
            continue
        try:
            epoch = int(
                datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            )
        except Exception:
            continue
        mk = _norm_model(msg.get("model"), usage.get("speed"))
        usd = _cost_of(usage, mk)
        if usd <= 0:
            continue
        # The message id is carried so aggregation can deduplicate. Claude Code
        # writes ONE record per content block of an assistant turn, and every
        # record repeats the whole `message.usage` payload -- so a turn with
        # text plus two tool calls lands three identical usage rows. Counting
        # rows instead of messages inflated the numerator ~2.4x (FINDINGS 11).
        events.append([epoch, round(usd, 8), mk, msg.get("id")])

    return events, consumed


def _dedupe_events(events: list) -> list:
    """Drop repeated records of the same assistant message.

    Duplication is both intra-file (one record per content block) and
    cross-file (a sidechain replays its parent's turns), so this has to run
    once over the merged event list rather than per file. Events with no
    message id are kept as-is -- they cannot be shown to be duplicates.
    """
    seen: set = set()
    out: list = []
    for e in events:
        mid = e[3] if len(e) > 3 else None
        if mid is not None:
            if mid in seen:
                continue
            seen.add(mid)
        out.append(e)
    return out


def collect(full: bool = False, verbose: bool = False) -> dict:
    """Rescan transcripts and rebuild state.json."""
    cache = {} if full else _load(CACHE_PATH, {})
    # v2 added the message id to each event. v1 events cannot be deduplicated,
    # so a stale cache is discarded rather than merged.
    if cache.get("version") != 2:
        cache = {"version": 2, "files": {}}
    files = cache.setdefault("files", {})

    cutoff = time.time() - RETAIN_DAYS * 86400
    seen_paths = set()
    n_new = 0

    if PROJECTS.is_dir():
        for path in PROJECTS.rglob("*.jsonl"):
            sp = str(path)
            seen_paths.add(sp)
            try:
                st = path.stat()
            except OSError:
                continue
            # Skip files untouched for longer than the retention window.
            if st.st_mtime < cutoff and sp not in files:
                continue

            entry = files.get(sp)
            if entry and st.st_size >= entry.get("size", 0):
                offset = entry.get("offset", 0)
                events = entry.get("events", [])
            else:
                # New file, or truncated/rotated -> start clean.
                offset, events = 0, []

            new_events, new_offset = _scan_file(path, offset)
            n_new += len(new_events)
            events = events + new_events
            events = [e for e in events if e[0] >= cutoff]
            files[sp] = {
                "size": st.st_size,
                "offset": new_offset,
                "events": events,
            }

    for gone in set(files) - seen_paths:
        del files[gone]

    _save(CACHE_PATH, cache)

    all_events: list = []
    for entry in files.values():
        all_events.extend(entry["events"])
    all_events.sort(key=lambda e: e[0])
    all_events = _dedupe_events(all_events)

    state = _build_state(all_events)
    _save(STATE_PATH, state)

    if verbose:
        print(
            f"scanned {len(files)} files, {n_new} new turns, "
            f"{len(all_events)} turns in window"
        )
    return state


# ---------------------------------------------------------------------------
# Window reconstruction
# ---------------------------------------------------------------------------

def _session_windows(events: list, hours: int, anchor: float | None = None) -> list:
    """Split events into N-hour windows.

    ANCHORED (anchor set) -- seed the chain at `anchor`, a reset time read from
    `/usage`, then chain forward: a window runs `span` from its start, and the
    FIRST message at or after that expiry opens the next one (FINDINGS 4c).

    This is deliberately not a rigid grid. A grid tiles `anchor + k*span`
    forever and cannot notice that you stopped working, so it keeps reporting a
    boundary it has no evidence for. Measured on this machine: a 10.8-hour idle
    gap left a stale anchor claiming an "exact" boundary while chaining from a
    reset three windows old. Seeding the chain at the anchor keeps the one thing
    the anchor genuinely establishes -- the phase at the moment it was read --
    and re-derives everything after it from evidence.

    Each window records how its start was obtained, because the caller needs to
    know how much to trust it:

      "anchor"  the start IS the calibrated reset -- exact
      "chain"   re-derived from the first message after the previous expiry;
                `lag` is how long after that expiry the message landed
      "grid"    history from BEFORE the anchor, tiled backwards for display only

    A chained start is an upper bound, never exact: the window may have been
    opened earlier by account activity on a surface these logs cannot see, in
    which case the true start is earlier and the true reset sooner. A small
    `lag` means little room for that; a large one means the boundary is a guess.

    UNANCHORED (no anchor) -- the same chain seeded at the oldest local event.
    A biased fallback: it propagates an arbitrary phase whenever the true seed
    was another surface or predates the retention window. Measured error before
    anchoring existed: 10 minutes late on a 5h window. Set an anchor via
    `calibrate --resets` and this path stops being used.
    """
    span = hours * 3600
    windows: list = []

    if anchor is not None and events:
        # History from before the anchor is tiled backwards. It is display-only
        # -- the current window never comes from here -- so a grid is fine, and
        # it keeps old windows stable as more history is scanned.
        buckets: dict[int, dict] = {}
        for epoch, usd, _mk, *_ in events:
            if epoch >= anchor:
                continue
            idx = int((epoch - anchor) // span)
            b = buckets.setdefault(
                idx,
                {
                    "start": anchor + idx * span,
                    "end": anchor + (idx + 1) * span,
                    "cost": 0.0,
                    "turns": 0,
                    # idx == -1 is the window ENDING at the anchor: the exact
                    # one the /usage reading described. Older cells are tiled.
                    "seeded": "anchor" if idx == -1 else "grid",
                    "lag": None,
                },
            )
            b["cost"] += usd
            b["turns"] += 1
        windows = [buckets[i] for i in sorted(buckets)]

        # The anchor is a RESET time, so it is the first expiry to chain from,
        # not the start of a window. The window it opens begins with the first
        # message after it -- which is the whole point of the chain model.
        cur_start, cur_end = None, anchor
        cur_cost, cur_n, cur_lag = 0.0, 0, None
        for epoch, usd, _mk, *_ in events:
            if epoch < anchor:
                continue
            if cur_start is None or epoch >= cur_end:
                if cur_start is not None:
                    windows.append({
                        "start": cur_start, "end": cur_end, "cost": cur_cost,
                        "turns": cur_n, "seeded": "chain", "lag": cur_lag,
                    })
                # Re-seed HERE, at the first message after expiry -- not at the
                # expiry itself. Nothing opened a window in between.
                cur_lag = epoch - cur_end
                cur_start, cur_end = epoch, epoch + span
                cur_cost, cur_n = 0.0, 0
            cur_cost += usd
            cur_n += 1
        if cur_start is not None:
            windows.append({
                "start": cur_start, "end": cur_end, "cost": cur_cost,
                "turns": cur_n, "seeded": "chain", "lag": cur_lag,
            })
        return windows

    cur_start = None
    cur_end = None
    cur_cost = 0.0
    cur_n = 0
    for epoch, usd, _mk, *_ in events:
        if cur_start is None or epoch >= cur_end:
            if cur_start is not None:
                windows.append(
                    {"start": cur_start, "end": cur_end, "cost": cur_cost,
                     "turns": cur_n, "seeded": "chain", "lag": None}
                )
            cur_start = epoch
            cur_end = epoch + span
            cur_cost = 0.0
            cur_n = 0
        cur_cost += usd
        cur_n += 1
    if cur_start is not None:
        windows.append(
            {"start": cur_start, "end": cur_end, "cost": cur_cost,
             "turns": cur_n, "seeded": "chain", "lag": None}
        )
    return windows


def _burn_sample(events: list, now: float, minutes: int = BURN_WINDOW_MINUTES,
                 models: set | None = None) -> tuple[float, float, int]:
    """(usd_per_hour, span_seconds, turns) over the trailing `minutes`.

    `models` restricts the rate to one limit bucket. A bucket's headroom must be
    forecast against its OWN burn: premium-model spend is a fraction of the
    total, so charging it the all-models rate would predict exhaustion that
    never arrives, and the reverse when a session goes premium-heavy.

    The denominator is the OBSERVED span -- first event to now -- so a burst that
    starts 25 minutes into the sample is not diluted by the quiet 25. But it is
    floored at BURN_MIN_SPAN_MINUTES, because dividing by the true span makes the
    rate diverge as the span goes to zero: one $2.89 resume turn 14 seconds ago
    is $744/h on the arithmetic and tells you nothing about the next five hours.
    The span is returned alongside so callers can tell a measured rate from an
    extrapolated one -- the number alone cannot carry that.
    """
    since = now - minutes * 60
    recent = [e for e in events if e[0] >= since and (models is None or e[2] in models)]
    if not recent:
        return 0.0, 0.0, 0
    total = sum(e[1] for e in recent)
    span = max(0.0, now - min(e[0] for e in recent))
    elapsed_h = max(span, BURN_MIN_SPAN_MINUTES * 60) / 3600.0
    return total / elapsed_h, span, len(recent)


def _burn_rate(events: list, now: float, minutes: int = BURN_WINDOW_MINUTES,
               models: set | None = None) -> float:
    """USD/hour over the trailing `minutes`. Drives the exhaustion forecast."""
    return _burn_sample(events, now, minutes, models)[0]


def _burn_measurable(state: dict) -> bool:
    """Is the trailing rate wide enough to lead the block with?

    Only the 5h window is forecast off it; the weekly buckets use a week average
    whose span is never in doubt, so callers must not gate those on this.
    """
    b = state.get("burn") or {}
    if not b:
        return True  # pre-`burn` state dicts: no evidence of thinness, do not gate
    return bool(b.get("measurable"))


def _premium_models(config: dict) -> set:
    """Model keys that draw on the separate premium weekly allowance.

    Claude's usage UI shows a distinct "Current week (Fable)" bar alongside the
    all-models one, and FINDINGS 7 records it as a sub-allocation rather than a
    separate pool: premium turns count against BOTH. Measured 2026-08-13 at
    roughly a quarter of the all-models weekly capacity.

    The per-model bars `/usage` shows are not in the status line, so the set
    here is static and WILL go stale when a new premium tier ships. The mitigation is detection, not
    prediction: any model absent from PRICES is reported as unpriced so it cannot
    be folded silently into the shared bucket.

    Only Fable is listed by default because only Fable has been measured.
    `claude-mythos-5` carries identical list pricing and plausibly shares the
    bucket, but plausible is not measured, and this project's own rule is to
    measure before modelling. Override via `premium_models` in config.json once
    there is a reading to justify it.
    """
    configured = config.get("premium_models")
    base = set(configured) if configured else {"claude-fable-5"}
    # Fast-mode variants are the same model against the same allowance.
    return base | {f"{m}:fast" for m in base}


def _capacity(config: dict, scope: str) -> tuple[float | None, str, int]:
    """Derive window capacity from calibration samples.

    Each sample says "at cost C the UI showed P%", so capacity = C / (P/100).
    We take the median across samples and report confidence from their spread --
    wide spread means price-weighting is a poor proxy for the real limit.
    """
    now = time.time()
    samples = [
        c for c in config.get("calibrations", [])
        if c.get("scope") == scope and c.get("pct") and c.get("cost") is not None
        # A sample taken while a promotional limit was active stops describing
        # reality when the promo ends. Better to report no capacity than a
        # capacity that is silently 50% too generous.
        and not (c.get("expires_epoch") and now >= c["expires_epoch"])
        # Retired: measured correctly but against something later found wrong --
        # most often a window boundary since corrected, which changes WHICH turns
        # the percentage was paired with (FINDINGS 6d). Kept in the file rather
        # than deleted, so the record shows what was excluded and why; a silently
        # shorter list is indistinguishable from one that was never taken.
        and not c.get("retired")
    ]
    if not samples:
        return None, "none", 0
    caps = sorted(s["cost"] / (s["pct"] / 100.0) for s in samples if s["pct"] > 0)
    if not caps:
        return None, "none", 0
    n = len(caps)
    med = caps[n // 2] if n % 2 else (caps[n // 2 - 1] + caps[n // 2]) / 2

    if n == 1:
        conf = "low"
    else:
        spread = (caps[-1] - caps[0]) / med if med else 1.0
        if n >= 3 and spread <= 0.10:
            conf = "high"
        elif spread <= 0.20:
            conf = "medium"
        else:
            conf = "low"
    return med, conf, n


def _meter_epoch(val) -> float | None:
    """Normalise a `resets_at` to an epoch float.

    The status line sends epoch seconds. An ISO-8601 string is accepted too,
    because that is how the same server value is serialised by the endpoint
    behind `/usage` (FINDINGS 15), and a reader that trusts one form of a value
    breaks the day the other one arrives.
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _reading_time(payload: dict, now: float) -> float:
    """When the rate-limit figures in a status line payload were last current.

    Claude Code re-runs the status line on events that bring no new API
    response -- a permission-mode change, vim mode, a `refreshInterval` timer --
    and every run resends the figures from that session's LAST response. Stamped
    with the wall clock, an idle session would present an hours-old reading as
    fresh, and overwrite a newer one from a session that is working. The
    transcript is written as the session works, so its mtime approximates when
    the figures were last refreshed; the wall clock is only the fallback.
    """
    path = (payload or {}).get("transcript_path")
    if isinstance(path, str) and path:
        try:
            return min(now, os.path.getmtime(path))
        except OSError:
            pass
    return now


def _parse_statusline(payload: dict, read_at: float) -> dict | None:
    """Pull the rate-limit windows out of one status line payload.

    None means there is nothing to record. `rate_limits` is absent until a
    session's first API response, and absent entirely for API-key users, so its
    absence says nothing about the meter and must never clobber a good reading.
    """
    rl = (payload or {}).get("rate_limits")
    if not isinstance(rl, dict):
        return None
    out = {"fetched_at": read_at, "limits": {}, "unknown_kinds": []}
    for kind, win in rl.items():
        if kind in METER_SKIP_KINDS or not isinstance(win, dict):
            continue
        util = win.get("used_percentage")
        if isinstance(util, bool) or not isinstance(util, (int, float)):
            continue
        if kind not in METER_KINDS:
            out["unknown_kinds"].append(kind)
        out["limits"][kind] = {
            "utilization": float(util),
            "resets_at": _meter_epoch(win.get("resets_at")),
        }
    return out if out["limits"] else None


def record_statusline(payload: dict, now: float | None = None) -> bool:
    """Store the meter from one status line payload. True if it was written.

    Every session's status line writes the same file, so a reading older than
    the one already held is dropped (see _reading_time). The file therefore
    holds the freshest figures any session has seen, whichever wrote last.
    """
    now = time.time() if now is None else now
    reading = _parse_statusline(payload, _reading_time(payload, now))
    if not reading:
        return False
    held = _load(METER_PATH, None)
    if isinstance(held, dict):
        try:
            if reading["fetched_at"] < float(held.get("fetched_at") or 0):
                return False
        except (TypeError, ValueError):
            pass
    _save(METER_PATH, reading)
    return True


def _statusline_text(payload: dict) -> str:
    """The default status line: the meter this session can see, or nothing."""
    rl = (payload or {}).get("rate_limits")
    if not isinstance(rl, dict):
        return ""
    parts = []
    for kind, label in (("five_hour", "5h"), ("seven_day", "week")):
        util = (rl.get(kind) or {}).get("used_percentage") if isinstance(rl.get(kind), dict) else None
        if isinstance(util, (int, float)) and not isinstance(util, bool):
            parts.append(f"{label} {util:.0f}%")
    return " · ".join(parts)


def _load_meter(now: float | None = None) -> dict | None:
    """Return the recorded meter if it is fresh enough to present as exact.

    A window whose `resets_at` has passed is dropped even inside the freshness
    limit: a reading of 95% taken five minutes before a reset is a statement
    about a window that no longer exists. Claude Code drops such a window from
    the status line for the same reason.
    """
    now = time.time() if now is None else now
    mr = _load(METER_PATH, None)
    if not isinstance(mr, dict) or not isinstance(mr.get("limits"), dict):
        return None
    try:
        age = now - float(mr.get("fetched_at") or 0)
    except (TypeError, ValueError):
        return None
    if age > METER_MAX_AGE:
        return None
    def _live(lim) -> bool:
        if not isinstance(lim, dict):
            return False
        r = lim.get("resets_at")
        return not (isinstance(r, (int, float)) and r <= now)

    mr["limits"] = {kind: lim for kind, lim in mr["limits"].items() if _live(lim)}
    if not mr["limits"]:
        return None
    mr["age_seconds"] = round(max(0.0, age))
    return mr


# Below this the implied capacity is division by near-zero: a 1% reading turns a
# few dollars of local spend into a four-figure capacity. Use exact utilization
# for the percentage regardless, but keep the calibrated capacity for dollars.
METER_MIN_UTIL_FOR_CAPACITY = 0.02


def _record_drift(bucket: str, pct: float, when: float) -> None:
    """Append one implied-vs-calibrated residual, newest last, bounded."""
    log = _load(DRIFT_PATH, None)
    if not isinstance(log, dict):
        log = {}
    row = log.get(bucket)
    if not isinstance(row, list):
        row = []
    row.append({"at": round(when, 3), "pct": pct})
    log[bucket] = row[-DRIFT_KEEP:]
    try:
        _save(DRIFT_PATH, log)
    except Exception:
        pass  # never break a hook over telemetry


def _observed_drift(*buckets: str) -> float | None:
    """Worst recent |drift| across the named buckets, as a fraction.

    Calibration SPREAD measures how much the samples disagree with each other;
    it says nothing about whether they are collectively wrong. Drift measures
    them against the vendor's own meter, so it is the only figure here that has
    been checked against ground truth -- and it has run larger than the spread
    bands in practice. Take the worst, not the mean: a range that fails to
    contain the answer is worse than no range at all, because it reads as a
    measurement.
    """
    log = _load(DRIFT_PATH, None)
    if not isinstance(log, dict):
        return None
    worst = None
    for b in buckets:
        for row in (log.get(b) or [])[-DRIFT_KEEP:]:
            try:
                v = abs(float(row["pct"])) / 100.0
            except (KeyError, TypeError, ValueError):
                continue
            worst = v if worst is None else max(worst, v)
    return worst


def _apply_meter(state: dict, mr: dict) -> None:
    """Overlay the vendor's own meter onto locally-reconstructed state.

    This inverts the project's founding constraint. The core idea said the
    numerator is computable and only the denominator needs calibrating; a fresh
    reading supplies BOTH exactly, so calibration stops being required at all:

        capacity = local_cost / exact_utilization

    That identity is also the long-missing measurement. FINDINGS 6b/6c/6d spent
    three sections arguing about whether the limiter weights tokens like list
    price, on three manual samples. The implied capacity is that same comparison
    run on every reading -- if price-weighting tracked the real meter, implied
    capacity would sit still. Drift against the calibrated figure IS the
    weighting error, reported rather than assumed.
    """
    limits = mr.get("limits") or {}
    state["meter"] = {
        "fetched_at": mr.get("fetched_at"),
        "age_seconds": mr.get("age_seconds"),
        "unknown_kinds": mr.get("unknown_kinds") or [],
    }

    for kind, key in (("five_hour", "session"), ("seven_day", "weekly")):
        lim, bucket = limits.get(kind), state.get(key)
        if not lim or not bucket:
            continue
        util = lim.get("utilization")
        if util is None:
            continue
        frac = max(0.0, float(util) / 100.0)
        bucket["used_frac"] = frac
        bucket["exact"] = True
        bucket["confidence"] = "exact"
        reset = _meter_epoch(lim.get("resets_at"))
        if reset:
            bucket["resets_at"] = reset
            bucket["boundary_exact"] = True

        cost = bucket.get("cost_usd") or 0.0
        calibrated = bucket.get("capacity_usd")
        if frac >= METER_MIN_UTIL_FOR_CAPACITY and cost > 0:
            implied = cost / frac
            bucket["capacity_usd"] = round(implied, 4)
            bucket["remaining_usd"] = round(implied - cost, 4)
            bucket["capacity_basis"] = "meter-implied"
            if calibrated:
                # The residual IS the weighting error (see docstring).
                drift = round((implied - calibrated) / calibrated * 100, 1)
                bucket["capacity_drift_pct"] = drift
                _record_drift(key, drift, mr.get("fetched_at") or time.time())
        elif calibrated:
            bucket["remaining_usd"] = round(calibrated * (1 - frac), 4)
            bucket["capacity_basis"] = "calibrated (utilization too low to imply)"

    # A boundary read off the meter is server state, so none of the decay
    # machinery applies to it (FINDINGS 14).
    if _meter_epoch((limits.get("five_hour") or {}).get("resets_at")):
        state["boundary"] = {"confidence": "exact", "lag_seconds": None,
                             "anchor_age_hours": None, "reanchor": False}


def _pace(weekly: dict, now: float) -> dict | None:
    """Prorate the weekly allowance across the days of the week.

    A weekly cap spent evenly is a cap you never hit. Spent front-loaded, it
    locks you out for days -- and the flat "~4% used" figure cannot distinguish
    the two, because it never says how much of the WEEK has gone with it.

    Day-granular, not continuous: a smooth prorate would put you over pace after
    a single turn on Monday morning (one hour in => 1/168th allowed). Granting
    the whole of day N's share the moment day N opens is both what a working day
    actually looks like and what makes the number usable.

    The ALLOWANCE is day-granular; the RATE deliberately is not. Extrapolating
    the dry day from `used / day_index` divides by up to a whole unelapsed day
    -- 32% low at 08:00 on day 3 -- so it pushed the projection later exactly
    when the warning mattered most. The rate uses elapsed time instead, floored
    at one day. That floor is day 1's day_index, so day-1 output is unchanged
    and the Monday-morning false alarm guarded against above stays guarded.
    """
    cap, start = weekly.get("capacity_usd"), weekly.get("window_start")
    if not cap or not start or not weekly.get("anchored"):
        return None
    day_index = min(WEEKLY_DAYS, max(1, int((now - start) // 86400) + 1))
    allowance = cap * day_index / WEEKLY_DAYS
    used = weekly.get("cost_usd", 0.0)
    ratio = used / allowance if allowance else 0.0
    # Daily rate so far, projected forward to name the day the week runs dry.
    # Elapsed time, not day_index -- see the docstring. Clamped at the top like
    # day_index so a stale anchor cannot dilute the rate, floored at the bottom
    # so a minutes-old week cannot project a dry day from nothing.
    elapsed = min(WEEKLY_DAYS, max(PACE_MIN_RATE_DAYS, (now - start) / 86400.0))
    per_day = used / elapsed
    exhaust_day = None
    if per_day > 0:
        d = cap / per_day
        if d < WEEKLY_DAYS:
            exhaust_day = max(day_index, int(d) + 1)
    return {
        "day_index": day_index,
        "days": WEEKLY_DAYS,
        "allowance_usd": round(allowance, 4),
        "used_usd": round(used, 4),
        "headroom_usd": round(allowance - used, 4),
        "ratio": round(ratio, 4),
        "over": ratio >= PACE_ASK_RATIO,
        "per_day_usd": round(per_day, 4),
        "exhaust_day": exhaust_day,
    }


def _affordance(state: dict, events: list, now: float) -> dict | None:
    """Convert remaining headroom into units an agent plans in.

    Dollars are the wrong unit at the point of decision. The two questions a
    coding agent actually has are "how many more turns do I get?" and "can I
    afford to fan out?", and both are ratios against a typical turn -- so both
    survive the one-turn measurement lag that a level does not (FINDINGS 12).

    Median, not mean: a session's first turn carries the skill loads and is
    routinely 3-4x a working turn, and a mean lets that one outlier depress the
    turn count for the rest of the session.
    """
    s, w = state.get("session") or {}, state.get("weekly") or {}
    start = s.get("window_start")
    if not start:
        return None
    costs = sorted(e[1] for e in events if e[0] >= start)
    if len(costs) < MIN_TURNS_FOR_MEDIAN:
        return None
    mid = len(costs) // 2
    per_turn = (costs[mid] if len(costs) % 2 else (costs[mid - 1] + costs[mid]) / 2)
    if per_turn <= 0:
        return None

    fanout_usd = FANOUT_AGENTS * FANOUT_TURNS_EACH * FANOUT_TURN_MULT * per_turn

    # The binding headroom is the smallest of: this window's remaining, and the
    # week's remaining -- but the week is measured against TODAY'S prorated
    # share, not the whole cap. Spending the week's entire remainder today is
    # exactly the failure pacing exists to prevent.
    pace = state.get("pace")

    def _scaled_remaining(bucket: dict, scale: float) -> float | None:
        """Remaining under a capacity perturbed by `scale`.

        Perturb CAPACITY, not the remainder. Spend is reconstructed locally and
        is the solid half; the uncertainty lives entirely in the denominator, so
        applying it anywhere else understates how far the answer can move.
        """
        if bucket.get("remaining_usd") is None:
            return None
        cap = bucket.get("capacity_usd")
        if not cap:
            return max(0.0, bucket["remaining_usd"])
        return max(0.0, cap * scale - bucket.get("cost_usd", 0.0))

    def _binding(s_scale: float = 1.0, w_scale: float | None = None):
        """Pick the binding ceiling with each capacity scaled independently.

        Independently, because the two buckets' errors are not the same error.
        Measured 2026-08-18: the 5h reconstruction was off by -9.9% against the
        meter while the week was off by -31.6%. A single shared scale cannot
        represent that, and a range built from one lands outside the answer --
        which is how the first cut of this function missed 1,527 with a
        314-1,422 interval.
        """
        if w_scale is None:
            w_scale = s_scale
        limits = []
        rem_s = _scaled_remaining(s, s_scale)
        if rem_s is not None:
            limits.append(("5h window", rem_s))
        borrowing = False
        if pace:
            headroom = pace["allowance_usd"] * w_scale - pace["used_usd"]
            if headroom > 0:
                limits.append(("week pace", headroom))
            else:
                # Past today's share the pace ceiling is spent -- but the WEEK is
                # not, and reporting "0 turns left" against a week with real
                # headroom would make the agent refuse work it can afford. That
                # is the failure the lockout forecast was rewritten to avoid: an
                # agent cut its own plan with 53% of the allowance unspent.
                # Report true capacity; the pace line and the checkpoint carry
                # the warning.
                borrowing = True
                rem_w = _scaled_remaining(w, w_scale)
                if rem_w is not None:
                    limits.append(("week (borrowing ahead)", rem_w))
        else:
            rem_w = _scaled_remaining(w, w_scale)
            if rem_w is not None:
                limits.append(("week", rem_w))
        if not limits:
            return None
        bucket, headroom = min(limits, key=lambda kv: kv[1])
        return bucket, headroom, borrowing

    point = _binding()
    if point is None:
        return None
    bucket, headroom, borrowing = point

    fanouts = int(headroom / fanout_usd) if fanout_usd > 0 else 0

    turns_left = int(headroom / per_turn)
    reserve = max(LANDING_RESERVE_MIN_TURNS,
                  int(turns_left * LANDING_RESERVE_FRAC))
    usable = max(0, turns_left - reserve)
    if usable <= 0:
        stance = "land"          # spend what is left stopping cleanly
    elif usable < FIT_LANDING_TURNS:
        stance = "landing"
    elif usable < FIT_SHORT_TURNS:
        stance = "short"
    else:
        stance = "ample"
    posture = state.get("posture", DEFAULT_POSTURE)
    calibrated = s.get("confidence", "none") != "none"
    if posture == "throughput":
        # Bought for speed. Unspent allowance still expires, so the affirmative
        # signal is the DEFAULT here rather than something to qualify for.
        go_wide = calibrated
    elif posture == "stretch":
        go_wide = False
    else:
        go_wide = (
            headroom >= GO_WIDE_MARGIN * fanout_usd
            and (pace is None or pace["ratio"] < GO_WIDE_PACE_MAX)
            and calibrated
        )
    # How precise is any of this? Exact only while BOTH buckets carry a fresh
    # meter reading: the ceiling is a MIN() across them, so an estimated week can decide
    # the answer even when the 5h window is exact.
    exact = bool(s.get("exact")) and bool(w.get("exact"))
    spread = AFFORDANCE_BAND.get(_weaker_confidence(s, w), AFFORDANCE_BAND["none"])
    s_band = 0.0 if exact else max(spread, _observed_drift("session") or 0.0)
    w_band = 0.0 if exact else max(spread, _observed_drift("weekly") or 0.0)
    band = max(s_band, w_band)

    usable_lo = usable_hi = usable
    fanouts_lo = fanouts_hi = fanouts
    binding_stable = True
    if band:
        # All four corners, not the diagonal: the ceiling is a MIN() across the
        # buckets, so the extremes of the ANSWER sit where one bucket is high
        # and the other low -- precisely the combinations a shared scale skips.
        corners = {(1.0 + si * s_band, 1.0 + wi * w_band)
                   for si in (-1, 1) for wi in (-1, 1)}
        for s_scale, w_scale in sorted(corners):
            alt = _binding(s_scale, w_scale)
            if alt is None:
                continue
            alt_bucket, alt_head, _ = alt
            if alt_bucket != bucket:
                # The ceiling itself changed. This is the real instability, not
                # a few percent on a count: pace headroom is `allowance - spent`,
                # a difference of two similar numbers, so a small capacity error
                # moves it enormously and can hand the decision to a different
                # bucket. Measured live 2026-08-18 -- the same session reported
                # 454 turns (binding: week pace) and then 1,878 (binding: 5h
                # window) one turn apart, on a $0.004 change in cost per turn.
                binding_stable = False
            alt_turns = int(alt_head / per_turn)
            alt_reserve = max(LANDING_RESERVE_MIN_TURNS,
                              int(alt_turns * LANDING_RESERVE_FRAC))
            usable_lo = min(usable_lo, max(0, alt_turns - alt_reserve))
            usable_hi = max(usable_hi, max(0, alt_turns - alt_reserve))
            if fanout_usd > 0:
                fanouts_lo = min(fanouts_lo, int(alt_head / fanout_usd))
                fanouts_hi = max(fanouts_hi, int(alt_head / fanout_usd))

    return {
        "per_turn_usd": round(per_turn, 4),
        "turns_left": turns_left,
        "usable_turns": usable,
        "reserve_turns": reserve,
        "stance": stance,
        "binding_bucket": bucket,
        "headroom_usd": round(headroom, 4),
        "fanout_usd": round(fanout_usd, 4),
        "fanouts_left": fanouts,
        "go_wide": go_wide,
        "borrowing": borrowing,
        "basis": f"{FANOUT_AGENTS} agents x {FANOUT_TURNS_EACH} turns",
        # Precision, carried alongside the figure rather than three lines away.
        "exact": exact,
        "band": band,
        "band_session": s_band,
        "band_weekly": w_band,
        "usable_lo": usable_lo,
        "usable_hi": usable_hi,
        "fanouts_lo": fanouts_lo,
        "fanouts_hi": fanouts_hi,
        "binding_stable": binding_stable,
    }


def _build_state(events: list) -> dict:
    config = load_config()
    now = time.time()
    hours = config.get("session_hours", SESSION_HOURS)
    s_anchor = config.get("session_anchor")
    w_anchor = config.get("weekly_anchor")

    windows = _session_windows(events, hours, s_anchor)
    cur = None
    for w in windows:
        if w["start"] <= now < w["end"]:
            cur = w
    if cur is None and s_anchor is not None:
        # No turns yet in the current window: still report boundaries rather
        # than "no active window". With no messages to chain from there is no
        # evidence to beat the anchor's own phase, so project it forward -- but
        # mark it, because every window between then and now went unobserved.
        span = hours * 3600
        idx = int((now - s_anchor) // span)
        cur = {
            "start": s_anchor + idx * span,
            "end": s_anchor + (idx + 1) * span,
            "cost": 0.0,
            "turns": 0,
            # idx == -1 is the window ENDING at the anchor -- i.e. the very
            # window the /usage reading described. Anything else is projection.
            "seeded": "anchor" if idx == -1 else "projected",
            "lag": None,
        }
    session_cost = cur["cost"] if cur else 0.0
    session_turns = cur["turns"] if cur else 0
    resets_at = cur["end"] if cur else None

    # How much the current boundary can be trusted. An anchor is exact only for
    # the window it was read in; after that the chain re-derives boundaries from
    # local messages, and a chained start is an UPPER bound -- account activity
    # on another surface may have opened the window earlier, making the real
    # reset sooner than shown. A short re-seed lag leaves little room for that;
    # a long one means the boundary is a guess. This is why the operational rule
    # is to re-anchor from /usage at the start of each work block.
    boundary = {
        "confidence": "heuristic",
        "lag_seconds": None,
        "anchor_age_hours": None,
        "reanchor": False,
    }
    if s_anchor is not None and cur is not None:
        seeded, lag = cur.get("seeded"), cur.get("lag")
        boundary["lag_seconds"] = round(lag) if lag is not None else None
        boundary["anchor_age_hours"] = round((now - s_anchor) / 3600.0, 2)
        if seeded == "anchor":
            boundary["confidence"] = "exact"
        elif seeded == "chain" and lag is not None and lag <= RESEED_TIGHT_SECONDS:
            # Re-seeded within minutes of expiry: the chain is tight enough that
            # an unseen earlier seed could only have moved it by that much.
            boundary["confidence"] = "tight"
        else:
            boundary["confidence"] = "estimated"
    boundary["reanchor"] = boundary["confidence"] == "estimated"

    wk_days = config.get("weekly_days", WEEKLY_DAYS)
    wk_span = wk_days * 86400
    if w_anchor is not None:
        # Fixed weekly period ending at the known reset. A rolling 7 days is a
        # *different set of days* than the real billing week -- calibrating a
        # percentage against the wrong period yields a meaningless capacity.
        idx = int((now - w_anchor) // wk_span)
        wk_start = w_anchor + idx * wk_span
        wk_end = wk_start + wk_span
    else:
        wk_start, wk_end = now - wk_span, None
    weekly_cost = sum(
        e[1] for e in events if e[0] >= wk_start and (wk_end is None or e[0] < wk_end)
    )

    premium = _premium_models(config)
    premium_cost = sum(
        e[1] for e in events
        if e[0] >= wk_start and (wk_end is None or e[0] < wk_end) and e[2] in premium
    )
    premium_turns = sum(
        1 for e in events
        if e[0] >= wk_start and (wk_end is None or e[0] < wk_end) and e[2] in premium
    )

    burn, burn_span, burn_turns = _burn_sample(events, now)
    scap, sconf, sn = _capacity(config, "session")
    wcap, wconf, wn = _capacity(config, "weekly")
    pcap, pconf, pn = _capacity(config, "weekly_premium")

    def frac(cost, cap):
        if not cap:
            return None
        return max(0.0, min(1.5, cost / cap))

    state = {
        "generated_at": now,
        "session": {
            "cost_usd": round(session_cost, 4),
            "turns": session_turns,
            "window_start": cur["start"] if cur else None,
            "resets_at": resets_at,
            "capacity_usd": round(scap, 4) if scap else None,
            "used_frac": frac(session_cost, scap),
            "remaining_usd": round(scap - session_cost, 4) if scap else None,
            "confidence": sconf,
            "samples": sn,
        },
        "weekly": {
            "cost_usd": round(weekly_cost, 4),
            "window_start": wk_start,
            "resets_at": wk_end,
            "anchored": w_anchor is not None,
            "capacity_usd": round(wcap, 4) if wcap else None,
            "used_frac": frac(weekly_cost, wcap),
            "remaining_usd": round(wcap - weekly_cost, 4) if wcap else None,
            "confidence": wconf,
            "samples": wn,
        },
        "weekly_premium": {
            "cost_usd": round(premium_cost, 4),
            "turns": premium_turns,
            "models": sorted(premium),
            "window_start": wk_start,
            "resets_at": wk_end,
            "capacity_usd": round(pcap, 4) if pcap else None,
            "used_frac": frac(premium_cost, pcap),
            "remaining_usd": round(pcap - premium_cost, 4) if pcap else None,
            "confidence": pconf,
            "samples": pn,
        },
        "session_anchored": s_anchor is not None,
        "boundary": boundary,
        "burn_usd_per_hour": round(burn, 4),
        "burn": {
            "span_seconds": int(burn_span),
            "turns": burn_turns,
            "window_minutes": BURN_WINDOW_MINUTES,
            "floored": burn_span < BURN_MIN_SPAN_MINUTES * 60,
            "measurable": bool(
                burn_span >= PROMOTE_MIN_BURN_SPAN_MINUTES * 60
                and burn_turns >= PROMOTE_MIN_BURN_TURNS
            ),
        },
        "recent_windows": [
            {
                "start": w["start"],
                "end": w["end"],
                "cost_usd": round(w["cost"], 4),
                "turns": w["turns"],
            }
            for w in windows[-6:]
        ],
    }

    # Day bucket. Built after the dict because it reads the weekly window it is
    # phase-aligned to (see _day_bucket for why not local midnight).
    try:
        state["day"] = _day_bucket(state, events, now)
    except Exception:
        pass

    # The meter, if fresh, replaces estimates with the vendor's own figures. It
    # has to land BEFORE time-to-exhaustion, the lockout forecast, pacing and
    # affordance, so all of them run on the figures the block prints. Applied
    # after them, the hours to cap come from the calibrated remainder while the
    # block prints the exact one: "$251 left" beside "$11.27/h -> caps in ~24h
    # 42m", which implies $278.
    fresh = _load_meter(now)
    if fresh:
        _apply_meter(state, fresh)
    # Wall or price, declared once. Read before the lockout forecast below,
    # which is the only thing it changes.
    state["overage"] = _overage(config)

    # Time-to-exhaustion at the current burn rate. This is the number that lets
    # the model actually push back on a task rather than just report a percentage.
    s = state["session"]
    if s["remaining_usd"] is not None and burn > 0.01:
        s["hours_to_exhaustion"] = round(max(0.0, s["remaining_usd"]) / burn, 2)
    else:
        s["hours_to_exhaustion"] = None

    # Which allowance actually binds. Reporting only the 5h window is wrong
    # whenever a different bucket is nearer its cap -- a premium-heavy stretch
    # can exhaust the premium weekly while the 5h window still looks roomy.
    # Each bucket is forecast against its own burn rate and its own reset.
    # (Borrowed from the Codex implementation, which gets this right by having
    # the host report every window and taking the most constraining one.)
    # Long windows need a DUTY-CYCLE-AWARE rate. Extrapolating a trailing
    # 30-minute rate across a week assumes you work 24/7 -- project $65/h over 35
    # hours and the weekly looks doomed, when in truth nobody burns continuously
    # for a day and a half. The window's own average so far already embeds how
    # much of the week you actually spend working, so use that for the weekly
    # buckets and keep the trailing rate for the 5h window, where a short horizon
    # makes "you will keep going at this rate" a fair assumption.
    wk_elapsed_h = max((now - wk_start) / 3600.0, 1 / 60.0)

    p = state["weekly_premium"]
    premium_rate = premium_cost / wk_elapsed_h
    if p["remaining_usd"] is not None and premium_rate > 0.01:
        p["hours_to_exhaustion"] = round(max(0.0, p["remaining_usd"]) / premium_rate, 2)
    else:
        p["hours_to_exhaustion"] = None
    p["burn_usd_per_hour"] = round(premium_rate, 4)
    p["rate_basis"] = "week average"

    wkly = state["weekly"]
    weekly_rate = weekly_cost / wk_elapsed_h
    if wkly["remaining_usd"] is not None and weekly_rate > 0.01:
        wkly["hours_to_exhaustion"] = round(max(0.0, wkly["remaining_usd"]) / weekly_rate, 2)
    else:
        wkly["hours_to_exhaustion"] = None
    wkly["burn_usd_per_hour"] = round(weekly_rate, 4)
    wkly["rate_basis"] = "week average"

    # What running out actually COSTS is the time you sit blocked waiting for the
    # reset -- not the fact that it happens. "Which arrives first" gets this
    # exactly backwards near a boundary: running out 13 minutes before a window
    # rolls over is a 13-minute inconvenience, and the fresh window then arrives
    # with a full allowance. Measured harm from the old logic: an agent shortened
    # its own plan with 53% of the window still unspent, to avoid a cap that
    # would have cost it minutes.
    binding, imminent = None, None
    for label, bucket in (("5h window", s), ("week", wkly), ("premium week", p)):
        h2e, resets = bucket.get("hours_to_exhaustion"), bucket.get("resets_at")
        if h2e is None or not resets:
            continue
        hrs_to_reset = (resets - now) / 3600.0
        blocked = hrs_to_reset - h2e
        bucket["blocked_hours"] = round(blocked, 2) if blocked > 0 else 0.0
        if blocked <= 0:
            continue
        cand = {
            "bucket": label,
            "hours_to_cap": h2e,
            "hours_to_reset": round(hrs_to_reset, 2),
            "blocked_hours": round(blocked, 2),
        }
        if blocked >= LOCKOUT_WARN_HOURS:
            if binding is None or blocked > binding["blocked_hours"]:
                binding = cand
        elif imminent is None or blocked > imminent["blocked_hours"]:
            # A cap is coming but the reset lands right behind it. Worth stating
            # so the agent does NOT quietly cut scope over a trivial wait.
            imminent = cand
    # Usage credits mean a cap is a price change, not a wall. Every lockout
    # figure above assumes you stop dead; with overage enabled that is factually
    # wrong, so the lockout is demoted to a cost note rather than merely softened.
    ov = state.get("overage") or {}
    if ov.get("enabled") and binding:
        state["cost_ahead"] = {
            "bucket": binding["bucket"],
            "hours_to_cap": binding["hours_to_cap"],
        }
        binding, imminent = None, None
    state["binding"] = binding
    state["cheap_cap"] = imminent if binding is None else None

    # Models this build has never priced. Two independent problems, one signal:
    # the cost is being guessed at FALLBACK_PRICE (Opus-tier), AND per FINDINGS 7
    # the limiter may be scoring it against its own per-model weekly cap, like the
    # Fable bar `/usage` shows, that this tool does not track. The status line does
    # not carry those caps, so the honest move is to say a model went unrecognised
    # rather than fold it silently into the shared bucket.
    unknown = sorted({
        e[2] for e in events
        if e[0] >= wk_start and (wk_end is None or e[0] < wk_end)
        and e[2] not in PRICES
    })
    state["unknown_models"] = unknown

    # Pacing first: affordance treats the week's PRORATED share as the ceiling,
    # not its whole remainder, so the order matters.
    # Posture is resolved at RENDER time, not here -- state.json is cached for
    # up to 90s, so a posture set from the CLI would not take effect until the
    # next collect. A preference the user just changed must apply to the next
    # prompt, not the next scan.
    state["posture"] = _posture(load_config())
    state["pace"] = _pace(state["weekly"], now)
    state["affordance"] = _affordance(state, events, now)
    return state


# ---------------------------------------------------------------------------
# Hypothesis discrimination.
#
# The cost model is confirmed (twice, against /usage's own session totals, to
# 0.03%). Yet the limit percentage it implies is ~1.27x too high on cache-heavy
# work -- so the limiter is demonstrably NOT price-weighted. What it IS weighted
# by is open, and samples are expensive (each needs a human /usage reading), so
# every sample must be spent as a test rather than another curve fit.
#
# `predict` states, BEFORE the reading is taken, what each competing weighting
# implies /usage will say. One reading then discriminates.
# ---------------------------------------------------------------------------

# Candidate cache-read multiplier that reconciled the first two samples exactly.
# NOT applied to the live figures -- two points fitted with one free parameter
# always agree, so this is a hypothesis on trial, not a correction.
HYP_CACHE_READ_MULT = 0.0168


def _window_cost(start: float, end: float, dedupe: bool = True,
                 models: set | None = None) -> float:
    """Price a window straight from transcripts, at each model's own rates.

    `dedupe=False` reproduces the pre-FINDINGS-11 counting rule, which is what
    migrate_calibrations needs in order to verify a stored anchor. `models`
    restricts the total to one set of model keys, for the premium sub-cap.
    """
    mix = _window_token_mix(start, end, dedupe=dedupe)
    total = 0.0 if models else mix["web"] * WEB_SEARCH_USD
    for mk, m in mix["models"].items():
        if models is not None and mk not in models:
            continue
        p = _price(mk)
        total += (m["inp"] * p["in"]
                  + m["out"] * p["out"]
                  + m["cread"] * p["in"] * CACHE_READ_MULT
                  + m["cw5"] * p["in"] * CACHE_WRITE_5M_MULT
                  + m["cw1"] * p["in"] * CACHE_WRITE_1H_MULT) / 1e6
    return total


def _window_token_mix(start: float, end: float, dedupe: bool = True) -> dict:
    """Per-class token totals for turns in [start, end). Own scan, for analysis.

    Totals are also split per model under `models`, because pricing them needs
    each model's own rates -- an all-Fable window costs 2x an all-Opus one, and
    a hardcoded Opus assumption measurably halved predict's cost on 2026-08-13.
    """
    acc = dict(inp=0, out=0, cread=0, cw5=0, cw1=0, web=0, think=0, turns=0,
               effort={}, models={})
    if not PROJECTS.is_dir():
        return acc
    # Same content-block duplication as the main collector -- see
    # _dedupe_events. Without this the mix is inflated ~2.4x (FINDINGS 11).
    seen_ids: set = set()
    cutoff = start - 1
    for path in PROJECTS.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        try:
            fh = path.open("rb")
        except OSError:
            continue
        with fh:
            for line in fh:
                if b'"usage"' not in line or b'"assistant"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("timestamp")
                if rec.get("type") != "assistant" or not ts:
                    continue
                try:
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if not (start <= t < end):
                    continue
                msg = rec.get("message") or {}
                u = msg.get("usage") or {}
                mid = msg.get("id")
                if dedupe and mid is not None:
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                cc = u.get("cache_creation") or {}
                mk = _norm_model(msg.get("model"), u.get("speed"))
                m = acc["models"].setdefault(
                    mk, dict(inp=0, out=0, cread=0, cw5=0, cw1=0, turns=0))
                for key, val in (
                    ("inp", u.get("input_tokens") or 0),
                    ("out", u.get("output_tokens") or 0),
                    ("cread", u.get("cache_read_input_tokens") or 0),
                    ("cw5", cc.get("ephemeral_5m_input_tokens") or 0),
                    ("cw1", cc.get("ephemeral_1h_input_tokens") or 0),
                ):
                    acc[key] += val
                    m[key] += val
                m["turns"] += 1
                acc["web"] += (u.get("server_tool_use") or {}).get("web_search_requests") or 0
                # Thinking is a SUBSET of output_tokens (never added to cost) but its
                # share is tracked, because the limiter may weight it differently from
                # visible output -- a second candidate shape error, confounded with the
                # cache-read one in ordinary agentic work.
                acc["think"] += ((u.get("output_tokens_details") or {})
                                 .get("thinking_tokens") or 0)
                acc["effort"][rec.get("effort") or "?"] = \
                    acc["effort"].get(rec.get("effort") or "?", 0) + 1
                acc["turns"] += 1
    return acc


# ---------------------------------------------------------------------------
# Context-spend analysis
#
# The window meters answer "how much is LEFT". This answers "where did it GO",
# which is a different and, until now, unexamined question. Measured across
# local history: ~86% of weighted spend is context handling (cache read +
# cache write) against ~14% output. Every earlier finding in this file -- 6b,
# 6c, 6d -- investigated the output weighting, i.e. the small slice.
#
# This scan is deliberately NOT the one `collect` uses. The hot path stores a
# minimal [epoch, usd, model] triple behind a bytes-level prefilter, and that
# prefilter is what makes the hook viable at all (FINDINGS 8). Analysis needs
# per-turn context sizes and tool names, so it pays for its own slower pass.
# Affordable precisely because `analyze` is on demand and never in a hook.
# ---------------------------------------------------------------------------

# Assumed post-compaction context, used ONLY when the incompressible prefix
# cannot be measured off the transcript. Overridable via --summary-tokens.
# FINDINGS 25 read this off the turn immediately after a compaction, which is
# the turn that WRITES the new prefix to cache -- so its 24,702 median measured
# a cache write, not a settled context, and came out below the 28,918 prefix a
# fresh session starts with. FINDINGS 32 has the corrected measurement.
DEFAULT_SUMMARY_TOKENS = 30_000
# The summary TEXT a compaction generates, measured from the two
# `isCompactSummary` records on this machine: 12,037 and 12,086 bytes at the
# measured 2.6 B/token (FINDINGS 21) => ~4,640. Strikingly stable, and 6.5x
# SMALLER than the 30,000 this used to charge -- generation is priced at output,
# so that one term was over half the modelled one-off cost.
SUMMARY_TEXT_TOKENS = 4_650
# How many of a transcript's leading assistant turns to scan for the fixed
# prefix. Turn 1 carries it as a cache WRITE, later turns as a cache read, so
# a minimum over a few turns finds the floor without depending on which. The
# window has to be wider than it looks: FINDINGS 11's per-content-block
# duplication repeats a turn's usage payload, so five records can be three turns.
FLOOR_SCAN_TURNS = 8


def _session_profiles(days: int = 7) -> list:
    """Ordered per-session turn profiles, for context attribution.

    Reads assistant lines ONLY. That is not a shortcut: an assistant turn
    carries both its `usage` block and the `tool_use` blocks for whatever it
    calls, so context size and tool identity arrive on the same line and the
    existing prefilter still applies unchanged. Tool *results* -- which are the
    bulky records -- never have to be parsed at all.

    One reply is MANY records, and they must be merged rather than dropped.
    Claude Code writes one record per content block, each repeating the whole
    `usage` payload but carrying only its OWN block (FINDINGS 11). A reply that
    thinks, speaks and calls two tools is four records: [thinking], [text],
    [tool_use], [tool_use]. So the collector's keep-first dedup is WRONG here --
    it would keep the `thinking` record and discard every tool identity, which
    is the one thing this function exists to read. Records sharing a
    `message.id` are therefore merged: usage counted once, tool blocks unioned.

    Counting records as turns distorted attribution three ways at once. Turn
    counts inflated ~2.4x, and amortised cost is size x turns-that-follow, so
    it inflated with them. Growth was charged to the LAST record's tools rather
    than split across every tool the reply called. And `(conversation)` was
    never charged when a reply happened to end on a tool_use.

    Each profile is {path, turns: [{epoch, model, ctx, cost, tools}]} where
    `ctx` is cache_read_input_tokens: the measured size of the prefix that turn
    re-read. Using it means growth is a real token count rather than a
    chars/4 estimate off the result payload.
    """
    out: list = []
    if not PROJECTS.is_dir():
        return out
    cutoff = time.time() - days * 86400
    for path in PROJECTS.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            fh = path.open("rb")
        except OSError:
            continue
        turns: list = []
        by_id: dict = {}
        with fh:
            for line in fh:
                if b'"usage"' not in line or b'"assistant"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                u = msg.get("usage")
                ts = rec.get("timestamp")
                if not isinstance(u, dict) or not ts:
                    continue
                try:
                    epoch = datetime.fromisoformat(
                        ts.replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                tools = []
                content = msg.get("content")
                if isinstance(content, list):
                    tools = [b.get("name") for b in content
                             if isinstance(b, dict) and b.get("type") == "tool_use"
                             and b.get("name")]
                mk = _norm_model(msg.get("model"), u.get("speed"))
                mid = msg.get("id")
                prev = by_id.get(mid) if mid is not None else None
                if prev is not None:
                    # Another block of a reply already seen. Its usage is the
                    # same payload repeated, so only the tool identities are
                    # new. Keep the earliest timestamp as the turn's epoch.
                    prev["tools"].extend(tools)
                    prev["epoch"] = min(prev["epoch"], epoch)
                    continue
                turn = {
                    "epoch": epoch,
                    "model": mk,
                    "ctx": u.get("cache_read_input_tokens") or 0,
                    # Attribution uses `ctx` (tokens RE-READ). The break-even
                    # needs the whole context, which on a cache-write turn is
                    # mostly not a re-read at all -- see _ctx_tokens.
                    "ctx_total": _ctx_tokens(u),
                    "out": u.get("output_tokens") or 0,
                    "cost": _cost_of(u, mk),
                    "tools": tools,
                }
                turns.append(turn)
                if mid is not None:
                    by_id[mid] = turn
        if len(turns) >= 2:
            turns.sort(key=lambda t: t["epoch"])
            out.append({"path": str(path), "turns": turns})
    return out


def _attribute_context_growth(profiles: list) -> dict:
    """Charge each turn's context growth to whatever tool call caused it.

    A tool result is not paid for once. It is paid for on EVERY subsequent turn
    of the session, as part of the cached prefix that turn re-reads. So the
    real cost of a call is its size times the number of turns that follow it --
    a cost that is invisible at the moment it is incurred and deferred across
    the rest of the session.

    Growth observed at turn i is charged to the tools called at turn i-1,
    because that is the call whose result landed in between. When a turn called
    several tools the growth is split evenly among them; when it called none,
    it is charged to `(conversation)` -- the user's message plus the assistant's
    own output.

    The raw delta is NOT all tool result. Growth from turn i-1 to turn i also
    contains the assistant's own output from i-1 -- its reasoning and tool-call
    text join the prefix too -- so that is subtracted first. Without it, verbose
    turns inflate whichever tool they happened to call, which is a fitted number
    dressed as a measured one. What remains is the tool result plus any user
    message, and in agentic work the result dominates.

    A NEGATIVE delta means the prefix shrank, which is compaction or a context
    reset rather than anything a tool did. Those are counted, not attributed.
    """
    by_tool: dict = {}
    compactions = 0
    total_amortised = 0.0
    for prof in profiles:
        turns = prof["turns"]
        n = len(turns)
        for i in range(1, n):
            delta = turns[i]["ctx"] - turns[i - 1]["ctx"]
            if delta < 0:
                compactions += 1
                continue
            # The previous turn's own output is part of this growth, and was
            # not caused by the tool.
            delta -= turns[i - 1]["out"]
            if delta <= 0:
                continue
            # Turns that pay cache-read on this growth. Turn i is INCLUDED:
            # the growth first appears in its own cache_read, so it is the
            # first turn charged, not the one before the charging starts.
            # Cross-check: sum(ctx[i]) must equal sum(growth * reads), which
            # only holds with `n - i`.
            reads = n - i
            if reads <= 0:
                continue
            pin = _price(turns[i]["model"])["in"]
            usd = delta * pin * CACHE_READ_MULT * reads / 1_000_000.0
            callers = turns[i - 1]["tools"] or ["(conversation)"]
            share = usd / len(callers)
            for name in callers:
                e = by_tool.setdefault(name, {"usd": 0.0, "tokens": 0, "calls": 0})
                e["usd"] += share
                e["tokens"] += delta // len(callers)
                e["calls"] += 1
            total_amortised += usd
    return {"by_tool": by_tool, "compactions": compactions,
            "total": total_amortised}


def _breakeven_turns(ctx_tokens: int, model_key: str,
                     summary_tokens: int = DEFAULT_SUMMARY_TOKENS,
                     floor_tokens: int = 0) -> dict:
    """How many more turns until compacting now pays for itself.

    Continuing costs `ctx * read_mult` per turn, forever. Compacting costs three
    one-off things -- reading the context once, GENERATING the summary at output
    price, and writing the smaller prefix back to cache -- after which each turn
    costs the POST-COMPACTION context at `read_mult` instead.

    Generation is the term that is easy to leave out and matters most: output is
    priced at 5x input, so a 30k-token summary is not free just because it is
    small. Omitting it roughly halves the break-even and makes compaction look
    better than it is.

    Both one-off terms were wrong until 2026-08-19, in OPPOSITE directions, so
    the total looked plausible while neither half was (FINDINGS 32).

    `floor_tokens` is what a compaction cannot reclaim: the system prompt, tool
    schemas, memory files and skill descriptions are re-sent verbatim. Since
    `ctx_tokens` is read from `cache_read + input` it INCLUDES that prefix, so
    subtracting a summary size alone pretended the prefix evaporated. Measured
    on a live 1M-context session: 39.7k fixed of a 386.8k context (`/context`
    reported 4.1k system + 22.7k tools + 4.5k memory + 8.4k skills), and 45-58k
    across five live transcripts. Read per session rather than assumed, because
    a session with 124 MCP tools and 68 skills loaded carries an order of
    magnitude more of it than a bare one.

    In the other direction, generation was charged on 30,000 tokens when the
    measured summary is ~4,650 (SUMMARY_TEXT_TOKENS). Output is priced at 5x
    input, so that single term was over half the modelled one-off.

    Note the asymmetry the old single parameter could not express: only the
    summary is GENERATED at output price, while the WHOLE post-compaction
    prefix -- floor plus summary -- is written back to cache.
    """
    p = _price(model_key)
    pin, pout = p["in"], p["out"]
    # Falls back to the flat assumption only when the floor is unmeasurable.
    # A transcript that BEGINS after a compaction reads its landing point as the
    # floor, so this adds one summary too many there -- conservative, and the
    # error is 4.65k against a 45k floor.
    post_ctx = (max(0, floor_tokens) + SUMMARY_TEXT_TOKENS
                if floor_tokens > 0 else summary_tokens)
    if ctx_tokens <= post_ctx:
        return {"worth_it": False, "turns": None, "per_turn_saving": 0.0,
                "one_off": 0.0, "post_ctx": post_ctx}
    one_off = (ctx_tokens * pin * CACHE_READ_MULT
               + SUMMARY_TEXT_TOKENS * pout
               + post_ctx * pin * CACHE_WRITE_1H_MULT) / 1_000_000.0
    per_turn_saving = ((ctx_tokens - post_ctx)
                       * pin * CACHE_READ_MULT) / 1_000_000.0
    if per_turn_saving <= 0:
        return {"worth_it": False, "turns": None, "per_turn_saving": 0.0,
                "one_off": one_off, "post_ctx": post_ctx}
    return {
        "worth_it": True,
        "post_ctx": post_ctx,
        "turns": math.ceil(one_off / per_turn_saving),
        "per_turn_saving": per_turn_saving,
        "one_off": one_off,
    }


def analyze(days: int = 7, summary_tokens: int = DEFAULT_SUMMARY_TOKENS) -> str:
    """Where the spend went, and what to do about it."""
    profiles = _session_profiles(days)
    if not profiles:
        return (f"no sessions with 2+ turns in the last {days}d — "
                f"nothing to analyse")

    attr = _attribute_context_growth(profiles)
    by_tool = attr["by_tool"]

    # Sessions ACTIVE in the window, counted whole. Amortisation is a
    # session-level property: truncating at the time boundary would discard the
    # early growth that later turns are still re-reading, which undercounts the
    # very thing being measured. So this is deliberately not a "spend in the
    # last N days" total, and must not be labelled as one.
    lines = [f"Context spend across {len(profiles)} sessions active in the "
             f"last {days}d",
             f"(whole sessions, {sum(len(p['turns']) for p in profiles):,} "
             f"turns — not a {days}d spend total)", ""]

    if by_tool:
        lines.append("AMORTISED CONTEXT COST BY CALLER")
        lines.append("  What each call cost across every turn that re-read it,")
        lines.append("  not just the turn it landed on.")
        lines.append("")
        lines.append(f"  {'caller':<24}{'calls':>7}{'ctx added':>13}{'amortised':>13}")
        ranked = sorted(by_tool.items(), key=lambda kv: -kv[1]["usd"])
        for name, e in ranked[:12]:
            lines.append(f"  {name[:23]:<24}{e['calls']:>7}"
                         f"{e['tokens']:>12,}{'':>1}${e['usd']:>11,.2f}")
        lines.append("")
        lines.append(f"  total amortised context cost: ${attr['total']:,.2f}")
        if attr["compactions"]:
            lines.append(f"  context shrank {attr['compactions']}x "
                         f"(compaction or reset)")
        lines.append("")

    # Break-even, computed on the largest live session -- the one where the
    # question is actually being asked.
    biggest = max(profiles, key=lambda p: p["turns"][-1]["ctx"])
    last = biggest["turns"][-1]
    floor = _floor_context(biggest["path"])
    ctx_total = last.get("ctx_total") or last["ctx"]
    be = _breakeven_turns(ctx_total, last["model"], summary_tokens, floor)
    lines.append("COMPACTION BREAK-EVEN (largest current context)")
    lines.append(f"  session   {Path(biggest['path']).name[:36]}")
    lines.append(f"  context   {ctx_total:,} tokens over "
                 f"{len(biggest['turns'])} turns")
    if floor:
        lines.append(f"  incompressible {floor:,} tokens — system prompt, tools, "
                     f"memory, skills; a compaction")
        lines.append(f"                 cannot touch them, so it lands at "
                     f"{be['post_ctx']:,}, not {summary_tokens:,}")
    if be["worth_it"]:
        lines.append(f"  each further turn re-reads that prefix at "
                     f"${be['per_turn_saving']:,.4f} more than a "
                     f"{be['post_ctx']:,}-token one would cost")
        lines.append(f"  compacting costs ${be['one_off']:,.2f} one-off "
                     f"(read + generate summary + rewrite cache)")
        lines.append(f"  => pays for itself after ~{be['turns']} more turns")
    else:
        lines.append(f"  already at or below the {be['post_ctx']:,}-token "
                     f"floor — nothing to reclaim")
    lines.append("")
    lines.append("Caveats, so these are read as upper bounds:")
    lines.append("  - amortisation assumes the prefix survives to end of session;")
    lines.append("    a mid-session compaction retires it early.")
    lines.append("  - growth net of the previous turn's output still contains")
    lines.append("    per-turn injected content — system reminders, and this")
    lines.append("    tool's own hook block — charged to whichever tool ran.")
    lines.append("  - the initial prefix (system prompt, first context) is never")
    lines.append("    attributed, so the fixed floor is excluded.")
    lines.append(f"  - the SUMMARY is measured at {SUMMARY_TEXT_TOKENS:,} tokens "
                 "(n=2, FINDINGS 32); the 24,700 in FINDINGS 25 was")
    lines.append("    a cache-write artifact, not a settled context.")
    lines.append("  - the post-compaction CONTEXT adds the measured "
                 "incompressible")
    lines.append("    prefix on top of that. Reading the summary size as the "
                 "whole")
    lines.append("    post-compaction context — as this did before "
                 "2026-08-19 —")
    lines.append("    got both one-off terms wrong (FINDINGS 32).")
    lines.append("  - sessions are counted whole, so this total is NOT")
    lines.append("    comparable to cache-read spend inside the same window;")
    lines.append("    it will exceed it whenever a long session spans the edge.")
    return "\n".join(lines)


def predict() -> str:
    """Pre-register what each weighting hypothesis implies /usage will report."""
    state = _fresh_state()
    s = state.get("session") or {}
    start, end = s.get("window_start"), s.get("resets_at")
    if not start:
        return "no active window yet — do some work first, then re-run"

    mix = _window_token_mix(start, min(end or time.time(), time.time()) + 1)
    # Price each model's tokens at its own rates, mirroring _cost_of. The
    # hypotheses only disagree on the cache-read multiplier, so cread is kept
    # as a unit cost (per 1.0 of multiplier) and applied per hypothesis below.
    base = mix["web"] * WEB_SEARCH_USD
    cread_unit = 0.0    # cost per 1.0 of cache-read multiplier
    for mk, m in mix["models"].items():
        p = _price(mk)
        base += (m["inp"] * p["in"]
                 + m["out"] * p["out"]
                 + m["cw5"] * p["in"] * CACHE_WRITE_5M_MULT
                 + m["cw1"] * p["in"] * CACHE_WRITE_1H_MULT) / 1e6
        cread_unit += m["cread"] * p["in"] / 1e6

    cost_a = base + CACHE_READ_MULT * cread_unit
    cost_b = base + HYP_CACHE_READ_MULT * cread_unit
    cap_a = s.get("capacity_usd")

    # Hypothesis B's capacity is refitted from the same samples under its own
    # multiplier, so the two are compared on equal footing.
    cfg = load_config()
    caps_b = [c["cost"] / (c["pct"] / 100.0)
              for c in cfg.get("calibrations", []) if c.get("scope") == "session"]
    ratio = (cost_b / cost_a) if cost_a else 1.0
    cap_b = (sorted(caps_b)[len(caps_b) // 2] * ratio) if caps_b else None

    out = [
        "Hypothesis discrimination — run /usage and compare.",
        "",
        "  NOTE (2026-08-16, supersedes the 2026-08-14 note): hypothesis B was",
        "  marked REFUTED on the strength of a third sample at 47% agreeing with",
        "  sample 1 to 1.7% (FINDINGS 6d). That sample has since been RETIRED --",
        "  its stored anchor does not reproduce its own cost -- so the agreement",
        "  that closed the question no longer stands. B is OPEN again.",
        "  Separately, FINDINGS 11 showed no non-negative linear weighting of",
        "  (cache_read, output, cache_write) fits the live samples at all, so the",
        "  A-vs-B framing may be the wrong shape rather than the wrong constant.",
        "",
    ]
    out.append(f"  window          {_fmt_clock(start)} → {_fmt_clock(end)}"
               f"   ({mix['turns']} turns so far)")
    out.append(f"  token mix       cache_read {mix['cread']/1e6:.1f}M · output "
               f"{mix['out']/1e6:.2f}M · cache_write {(mix['cw5']+mix['cw1'])/1e6:.2f}M")
    if mix["models"]:
        out.append("  models          " + " · ".join(
            f"{mk} x{m['turns']}" for mk, m in
            sorted(mix["models"].items(), key=lambda kv: -kv[1]["turns"])))
    if mix["out"]:
        out.append(f"  thinking        {mix['think']/1e6:.2f}M "
                   f"({mix['think']/mix['out']*100:.0f}% of output)   "
                   f"effort: {', '.join(f'{k}x{v}' for k, v in sorted(mix['effort'].items()))}")
    if cost_a:
        out.append(f"  cache reads are {CACHE_READ_MULT*cread_unit/cost_a*100:.0f}% of the "
                   f"price-weighted figure")
    out.append("")
    out.append(f"  A  price-weighted (cache_read {CACHE_READ_MULT})   "
               f"cost ${cost_a:.2f}  cap ${cap_a or 0:.0f}  "
               f"→ /usage should say {(cost_a/cap_a*100) if cap_a else 0:.1f}%")
    if cap_b:
        out.append(f"  B  compute-weighted (cache_read {HYP_CACHE_READ_MULT})  "
                   f"cost ${cost_b:.2f}  cap ${cap_b:.0f}  "
                   f"→ /usage should say {cost_b/cap_b*100:.1f}%")
    out.append("")
    # Why separation can be zero even with plenty of spend:
    #
    # Hypothesis B's capacity is refitted from the same samples, so it scales by
    # the same ratio as its cost. The two therefore predict the SAME percentage
    # whenever the current cache-read share matches the share at calibration
    # time. Waiting longer does not help -- only a DIFFERENT token mix does.
    # With f = cache-read share of the price-weighted cost, cost_B/cost_A =
    # 1 - f*(1 - HYP/CACHE_READ_MULT), so separation is driven by how far f has
    # moved from its calibration value, not by how much has been spent.
    f_now = (CACHE_READ_MULT * cread_unit / cost_a) if cost_a else 0.0
    if cap_a and cap_b:
        pa, pb = cost_a / cap_a * 100, cost_b / cap_b * 100
        gap = abs(pa - pb)
        out.append(f"  separation: {gap:.1f} percentage points"
                   + ("  — enough to discriminate" if gap >= 3
                      else "  — cannot discriminate on this token mix"))
        if gap < 3:
            k = 1 - HYP_CACHE_READ_MULT / CACHE_READ_MULT
            r_now = 1 - k * f_now
            # f that would move the prediction by 3 points at the current level
            target_ratio = (pa + 3) / pa if pa > 0.5 else None
            out.append("")
            f_cal = _calibration_cache_read_share(cfg)
            was = (f"~{f_cal*100:.0f}% at calibration"
                   if f_cal is not None else "unmeasured at calibration")
            out.append(f"  WHY: cache reads are {f_now*100:.0f}% of the weighted cost now, and were")
            out.append(f"       {was}. Same mix -> same prediction, no matter how")
            out.append(f"       much is spent. Spending more will NOT create separation.")
            if target_ratio:
                f_needed = (1 - r_now * target_ratio) / k
                if 0 <= f_needed <= 1:
                    out.append(f"  NEED: a window where cache reads are ~{f_needed*100:.0f}% of the")
                    out.append(f"        weighted cost (vs {f_now*100:.0f}% now) — i.e. output-heavy work in a")
                    out.append(f"        SMALL context: long generation, short conversation, few files read.")
                else:
                    out.append("  NEED: a materially different token mix — output-heavy work in a small")
                    out.append("        context (long generation, short conversation, few files read).")
    out.append("")
    out.append("  The cost model itself is confirmed (0.03% against /usage's own session")
    out.append("  totals), so a miss here is the LIMITER's weighting, not the pricing.")
    if mix["out"] and mix["think"] / mix["out"] > 0.15:
        out.append("")
        out.append(f"  ⚠ CONFOUND: thinking is {mix['think']/mix['out']*100:.0f}% of output here. If the")
        out.append("    limiter weights thinking differently from visible output, that is a")
        out.append("    SECOND shape error, and it moves together with cache-read share in")
        out.append("    ordinary agentic work — so a mix change alone cannot separate them.")
        out.append("    To isolate it: run the SAME task at two effort levels. Effort moves")
        out.append("    thinking share while leaving cache-read share roughly fixed.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, m = seconds // 3600, (seconds % 3600) // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _fmt_clock(epoch: float | None) -> str:
    if not epoch:
        return "?"
    return datetime.fromtimestamp(epoch).strftime("%H:%M")


def _events_since(start: float) -> list:
    """Every cached event at or after `start`, deduped.

    Separate from `_all_events_for_window`, which pins the start to the SESSION
    window. A day bucket asking that helper for its events gets the session's
    spend labelled as the day's -- they only coincide when the session window
    happens to be as old as the day, which is almost never.
    """
    cache = _load(CACHE_PATH, {}) or {}
    out = []
    for rec in (cache.get("files") or {}).values():
        for e in rec.get("events") or []:
            if e[0] >= start:
                out.append(e)
    return _dedupe_events(out)


def _all_events_for_window(state: dict) -> list:
    """Events in the current session window, from the collector's own cache."""
    start = (state.get("session") or {}).get("window_start") or 0
    cache = _load(CACHE_PATH, {}) or {}
    out = []
    for rec in (cache.get("files") or {}).values():
        for e in rec.get("events") or []:
            if e[0] >= start:
                out.append(e)
    return _dedupe_events(out)


def _fresh_state(max_age: int = 90) -> dict:
    """Return state.json, recollecting if it's stale.

    The Stop hook keeps this warm, so the inject path normally just reads a file.
    """
    state = _load(STATE_PATH, None)
    if not state or (time.time() - state.get("generated_at", 0)) > max_age:
        try:
            state = collect()
        except Exception:
            state = state or {}
    if state:
        # Re-resolve the live preference over whatever the cache holds, and
        # recompute the one derived field that depends on it.
        state["posture"] = _posture(load_config())
        try:
            state["affordance"] = _affordance(
                state, _all_events_for_window(state), time.time())
        except Exception:
            pass
    return state


ACK_PATH = BASE / "pace_ack.json"


def _binding_ack_gate(state: dict) -> None:
    """Let the promoted lockout directive speak once, not every turn.

    FINDINGS 17 promoted this line to the top of the block and made it
    imperative, which took obedience from 29% to 92%. It shipped without the
    hysteresis the ask checkpoint already had, and the result was visible within
    the hour: four consecutive replies opened with the same sentence about a
    forecast that had barely moved. A directive that fires every turn is the
    thing this codebase keeps warning about.

    Speaks again only when it has something new to say -- a different bucket, a
    new window, or a materially worse lockout.
    """
    bind = state.get("binding")
    if not bind:
        return
    ack = _load(ACK_PATH, None) or {}
    prev = ack.get("binding") or {}
    same = (prev.get("bucket") == bind["bucket"]
            and prev.get("window_start") == (state.get("session") or {}).get("window_start"))
    if same and bind["blocked_hours"] < prev.get("blocked_hours", 0) * BINDING_REASK_WORSE:
        state["binding_quiet"] = True
        return
    ack["binding"] = {
        "bucket": bind["bucket"],
        "blocked_hours": bind["blocked_hours"],
        "window_start": (state.get("session") or {}).get("window_start"),
        "at": time.time(),
    }
    _save(ACK_PATH, ack)


def _pace_ask_gate(state: dict) -> None:
    """Decide whether this turn should stop and ask the user about pacing.

    Sets `state["pace_ask"]`. Impure by design (it persists the ack), so it is
    called from the inject path only -- `render_inject` stays a pure function of
    state, and `collect` running on Stop never silently consumes an ask the
    model has not seen yet.

    The gate exists because an unconditional prompt is worse than none: a
    checkpoint that fires every turn gets approved reflexively, which is the
    same failure mode the boundary caveat avoids by staying quiet unless the
    boundary is actually doubtful.
    """
    pace = state.get("pace")
    ratio = _ask_ratio(state.get("posture", DEFAULT_POSTURE))
    if not pace or ratio is None or pace["ratio"] < ratio:
        return
    ack = _load(ACK_PATH, None) or {}
    same_week = ack.get("week_start") == state.get("weekly", {}).get("window_start")
    if same_week and ack.get("day_index") == pace["day_index"]:
        # Already asked today. Re-ask only if the overshoot has materially
        # worsened since -- not merely persisted.
        if pace["ratio"] < ack.get("ratio", 0) + PACE_REASK_STEP:
            return
    state["pace_ask"] = True
    # MERGE, never replace. This file is shared with the binding and choice
    # gates; replacing it wiped their acks, so their directives spoke again on
    # the next turn -- silently reintroducing the every-turn repetition that
    # FINDINGS 20 exists to prevent. Measured 2026-08-19.
    ack.update({
        "week_start": state.get("weekly", {}).get("window_start"),
        "day_index": pace["day_index"],
        "ratio": pace["ratio"],
        "asked_at": time.time(),
    })
    _save(ACK_PATH, ack)


CHOICE_REASK_WORSE = 1.25   # the overshoot must grow 25% to ask a second time


def _choice_ask_gate(state: dict) -> None:
    """Offer the stop/shrink/continue fork as a real question -- once.

    Two directives in this block enumerate mutually exclusive options: AT LIMIT
    and WILL NOT FINISH MUCH. Both were phrased "state it, then offer ...", and
    an agent reading that writes prose. A list of options in prose is not a
    choice the user can make in one action; it is a paragraph they have to
    answer in another sentence, mid-task, exactly when they are least willing.

    So when the fork is NEW, the directive tells the agent to call
    AskUserQuestion instead. What stops that becoming a modal on every turn is
    this gate: MEASURED 2026-08-19, both directives re-render on every single
    turn while their condition holds -- three identical turns produced three
    identical directives. A question that fires every turn gets dismissed
    reflexively, which is worse than prose because it looks like consent.

    After the ask, the facts still render; only the interrogation stops. Speaks
    again on a new window, or when the overshoot grows materially worse.
    """
    a = state.get("affordance") or {}
    hard = [n for n in ("session", "day", "weekly", "weekly_premium")
            if isinstance(state.get(n), dict)
            and isinstance(state[n].get("used_frac"), (int, float))
            and _level_for(state[n]["used_frac"]) == "hard"]
    stance = a.get("stance")
    if stance != "landing" and not hard:
        return

    kind = "landing" if stance == "landing" else "hard"
    worst = max((state[n]["used_frac"] for n in hard), default=0.0)
    window = (state.get("session") or {}).get("window_start")

    ack = _load(ACK_PATH, None) or {}
    prev = ack.get("choice") or {}
    # `<=`, not `<`. The landing kind has no bucket over the line, so `worst`
    # is 0.0 -- and `0.0 < 0.0` is False, which re-asked on every single turn.
    if (prev.get("kind") == kind and prev.get("window_start") == window
            and worst <= prev.get("worst", 0.0) * CHOICE_REASK_WORSE):
        return

    state["choice_ask"] = True
    ack["choice"] = {"kind": kind, "worst": worst,
                     "window_start": window, "at": time.time()}
    _save(ACK_PATH, ack)


# --- Compaction advice ------------------------------------------------------
# `analyze` has always computed the one number in this repo with a measured
# payback -- how many more turns until compacting pays for itself -- and it was
# only ever visible by running `analyze` by hand, which nobody does mid-session.
# The block already says how much is LEFT; this says what is making the current
# session expensive, which is the more actionable half.
#
# Measured 2026-08-18 over 2 days of local history: context re-reads cost
# $426 amortised, and the largest live session was paying $0.3563 EVERY TURN
# more than a compacted one would, against a $1.42 one-off. Four turns to break
# even, invisible unless asked for.
COMPACT_ADVISE_MAX_TURNS = 25      # only when payback lands within a working stretch
COMPACT_ADVISE_MIN_SAVING = 0.01   # $/turn -- below this it is noise
COMPACT_TAIL_BYTES = 262_144       # enough to hold several assistant records
COMPACT_REASK_BETTER = 0.5         # re-speak when payback HALVES


def _tail_context(transcript: str | None) -> dict | None:
    """Context size and model of the most recent assistant turn in `transcript`.

    Reads only the tail of the file. The inject path is a prompt hook -- it has
    to stay fast, and the whole transcript can be tens of megabytes.

    FINDINGS 11's per-content-block duplication is harmless here: every record
    of a reply repeats the same `usage` payload, so taking the last one that has
    a `cache_read_input_tokens` is correct without deduplication.
    """
    if not transcript:
        return None
    try:
        path = Path(transcript)
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > COMPACT_TAIL_BYTES:
                fh.seek(size - COMPACT_TAIL_BYTES)
                fh.readline()          # discard the partial line
            chunk = fh.read()
    except OSError:
        return None

    for raw in reversed(chunk.decode("utf-8", "replace").splitlines()):
        if '"assistant"' not in raw:
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        u = msg.get("usage")
        if not isinstance(u, dict):
            continue
        ctx = _ctx_tokens(u)
        if ctx <= 0:
            continue
        return {"ctx": ctx, "model": _norm_model(msg.get("model"), u.get("speed"))}
    return None


def _floor_context(transcript: str | None) -> int:
    """Tokens of `transcript`'s context that a compaction cannot reclaim.

    The system prompt, tool schemas, memory files and skill descriptions are
    re-sent verbatim after every compaction, so the post-compaction context is
    this floor PLUS the summary, not the summary alone (see `_breakeven_turns`).

    Read from the leading turns rather than assumed. Turn 1 carries the prefix
    as a cache WRITE and later turns carry it as a cache READ, so this counts
    creation, read and plain input together and takes the minimum over the first
    few turns -- the smallest total any turn saw is the prefix with the least
    conversation stacked on top of it.

    Records with no output are skipped, and that filter is load-bearing. A live
    session's first record was a bare cache write -- every token field zero
    except `cache_creation.ephemeral_1h_input_tokens: 1781`, `output_tokens: 0`
    -- and reading it as a turn put the floor at 1,781 against the 58,377 that
    every real turn carried. No output means no turn happened, so there is no
    prefix to learn from. Erring high is the safe direction for what remains: a
    bigger floor makes compaction look worse, and over-recommending a lossy
    operation is the direction this tool must not be wrong in.
    """
    if not transcript:
        return 0
    try:
        with open(transcript, "rb") as fh:
            chunk = fh.read(COMPACT_TAIL_BYTES)
    except OSError:
        return 0

    seen = []
    for raw in chunk.decode("utf-8", "replace").splitlines():
        if len(seen) >= FLOOR_SCAN_TURNS:
            break
        if '"assistant"' not in raw:
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            continue          # a truncated final line, or not JSON at all
        if rec.get("type") != "assistant":
            continue
        u = (rec.get("message") or {}).get("usage")
        if not isinstance(u, dict):
            continue
        if not (u.get("output_tokens") or 0):
            continue          # a cache write, not a turn -- see the docstring
        total = _ctx_tokens(u)
        if total > 0:
            seen.append(total)
    return min(seen) if seen else 0


def _compaction_advice(transcript: str | None,
                       summary_tokens: int = DEFAULT_SUMMARY_TOKENS) -> dict | None:
    """Should this session compact now? Returns None when there is nothing to say."""
    cur = _tail_context(transcript)
    if not cur:
        return None
    floor = _floor_context(transcript)
    be = _breakeven_turns(cur["ctx"], cur["model"], summary_tokens, floor)
    if not be["worth_it"]:
        return None
    if be["turns"] is None or be["turns"] > COMPACT_ADVISE_MAX_TURNS:
        return None
    if be["per_turn_saving"] < COMPACT_ADVISE_MIN_SAVING:
        return None
    return {
        "ctx": cur["ctx"],
        "turns": be["turns"],
        "per_turn_saving": be["per_turn_saving"],
        "one_off": be["one_off"],
        "summary_tokens": summary_tokens,
        "floor_tokens": floor,
        "post_ctx": be["post_ctx"],
    }


def _compact_ack_gate(advice: dict | None, transcript: str | None) -> bool:
    """True when the advice is new enough to be worth a line.

    Same lesson as E12 (FINDINGS 20): a directive that fires every turn stops
    being read. This one would fire on EVERY turn of a long session, which is
    exactly when the session is long enough for it to matter -- so it speaks
    once, then only again when the payback has materially improved.
    """
    if not advice:
        return False
    ack = _load(ACK_PATH, None) or {}
    prev = (ack.get("compaction") or {}).get(transcript or "-")
    if prev and advice["turns"] >= prev.get("turns", 0) * COMPACT_REASK_BETTER:
        return False
    ack.setdefault("compaction", {})[transcript or "-"] = {
        "turns": advice["turns"], "ctx": advice["ctx"], "at": time.time(),
    }
    _save(ACK_PATH, ack)
    return True

def _burn_basis(state: dict) -> str:
    """How the burn figure was arrived at, in the words the number deserves.

    "(last 30m)" was printed unconditionally, including over a 38-second sample.
    A label that overstates its own evidence is the same defect as a stale doc:
    the reader calibrates on it. So say what was actually measured.
    """
    b = state.get("burn") or {}
    span, turns = b.get("span_seconds"), b.get("turns")
    if not span or not turns:
        return f"last {b.get('window_minutes', BURN_WINDOW_MINUTES)}m"
    if b.get("floored"):
        return f"only {_fmt_dur(span)}/{turns} turns — extrapolated"
    if not b.get("measurable"):
        return f"last {_fmt_dur(span)}, {turns} turns — thin"
    return f"last {_fmt_dur(span)}"


def render_inject(state: dict) -> str:
    s = state.get("session") or {}
    w = state.get("weekly") or {}
    burn = state.get("burn_usd_per_hour") or 0.0
    now = time.time()
    lines = ["<usage-budget>"]

    # E10 / FINDINGS 17. This line used to sit third, phrased as "Worth
    # reshaping the work — say so before starting", and was obeyed 4 times in 8.
    # The ASK checkpoint, which passes every run, differs in three ways: it is
    # FIRST in the model's attention (an unmissable directive), it is imperative
    # rather than advisory, and it enumerates concrete options instead of asking
    # the model to invent them. This mirrors that shape deliberately -- it is a
    # copy of the thing that already works, not a fresh guess.
    bind = state.get("binding")
    promote = bool(
        bind
        and not state.get("binding_quiet")
        and (state.get("burn_usd_per_hour") or 0) > 0.01
        and bind["blocked_hours"] >= PROMOTE_MIN_BLOCKED_HOURS
        and (bind["blocked_hours"] >= PROMOTE_ALWAYS_BLOCKED_HOURS
             or bind["hours_to_cap"] <= PROMOTE_MAX_HORIZON_HOURS)
        # The 5h window is the only bucket forecast off the trailing rate, so it
        # is the only one whose promotion depends on that rate being measured
        # rather than extrapolated from the first turn or two of a window. The
        # weekly buckets run on a week average and must NOT be gated here.
        and (bind["bucket"] != "5h window" or _burn_measurable(state))
    )
    if promote:
        lines.append(
            f"WOULD RUN OUT — TELL THE USER THIS FIRST, BEFORE ANSWERING: the "
            f"{bind['bucket']} caps in ~{_fmt_dur(bind['hours_to_cap'] * 3600)} at "
            f"the current burn, and they are then BLOCKED "
            f"~{_fmt_dur(bind['blocked_hours'] * 3600)} until it resets. Open your "
            "reply by stating that in one sentence, then offer: continue at full "
            "scope · reduce scope or drop to a cheaper model · do the critical part "
            "now and the rest after the reset. NOT a refusal — if they say continue, "
            "continue at full scope. Pace against this allowance, not the others."
        )

    # With credits enabled a cap is a price change, not a wall. Saying "BLOCKED"
    # to someone who cannot be blocked is a false statement, not a stern one.
    cost = state.get("cost_ahead")
    if cost:
        lines.append(
            f"COST AHEAD (not a lockout): the {cost['bucket']} caps in "
            f"~{_fmt_dur(cost['hours_to_cap'] * 3600)}, after which usage credits "
            "cover you — you keep working and it starts costing money. Worth "
            "mentioning once if the task is large; it is a budget question for "
            "the user, not a reason to reshape the work."
        )

    posture = state.get("posture", DEFAULT_POSTURE)
    if posture == "throughput":
        lines.append(
            "Posture: THROUGHPUT — the user has said speed matters more than "
            "spend. Do not pace, do not economise, do not raise usage unless a "
            "hard wall is imminent. Take the most thorough approach available."
        )
    elif posture == "stretch":
        lines.append(
            "Posture: STRETCH — the user is rationing this allowance across the "
            "week. Prefer the cheaper path where quality allows, and check in "
            "earlier than usual before expensive work."
        )

    resets = s.get("resets_at")
    reset_txt = (
        f"resets {_fmt_clock(resets)} (in {_fmt_dur(resets - now)})" if resets else "no active window"
    )
    # Only say anything when the boundary is actually doubtful. Injecting a
    # caveat every turn trains the model to ignore it; injecting one only when
    # it matters keeps it load-bearing.
    b = state.get("boundary") or {}
    if resets and b.get("confidence") == "estimated":
        reset_txt += " — boundary ESTIMATED, may be earlier"

    if s.get("used_frac") is not None:
        pct = s["used_frac"] * 100
        if s.get("exact"):
            # Measured, not reconstructed: no "~", no hedge. The dollars need a
            # capacity, and exact utilization implies one only once there is
            # spend to divide (METER_MIN_UTIL_FOR_CAPACITY). Without one -- an
            # uncalibrated install at the start of a window -- the percentage
            # stands alone instead of raising and taking the whole block with it.
            dollars = (
                f"~${max(0.0, s['remaining_usd']):.2f} left of ~"
                f"${s['capacity_usd']:.2f} implied · "
                if s.get("capacity_usd") and s.get("remaining_usd") is not None else ""
            )
            lines.append(
                f"5h window: {pct:.0f}% used (EXACT, from the vendor meter) · "
                f"{dollars}{reset_txt}"
            )
        else:
            lines.append(
                f"5h window: ~{pct:.0f}% used "
                f"(${s['cost_usd']:.2f} of ~${s['capacity_usd']:.2f} weighted) · "
                f"~${max(0.0, s['remaining_usd']):.2f} left · {reset_txt}"
            )
    else:
        lines.append(
            f"5h window: ${s.get('cost_usd', 0):.2f} spent "
            f"({s.get('turns', 0)} turns) · capacity UNCALIBRATED · {reset_txt}"
        )

    if burn > 0.01:
        line = f"Burn: ${burn:.2f}/h ({_burn_basis(state)})"
        h2e = s.get("hours_to_exhaustion")
        if h2e is not None and resets:
            line += f" → 5h window caps in ~{_fmt_dur(h2e * 3600)}, resets in {_fmt_dur(resets - now)}"
        lines.append(line)

        # The cost of hitting a cap is the wait it forces, so that is what gets
        # reported. Naming the bucket matters too: running the WEEK dry blocks
        # you for days, running a 5h window dry blocks you for minutes.
        cheap = state.get("cheap_cap")
        if bind and not promote:
            # Real, but too small or too far out to lead with. Still worth
            # saying -- quietly, where it was before E10.
            lines.append(
                f"Heads-up: the {bind['bucket']} would cap in "
                f"~{_fmt_dur(bind['hours_to_cap'] * 3600)} at this burn, blocking "
                f"~{_fmt_dur(bind['blocked_hours'] * 3600)}. Distant or small "
                "enough that it is not worth reshaping the work yet."
            )
        elif bind:
            pass  # promoted to the top of the block
        elif cheap:
            lines.append(
                f"Note: the {cheap['bucket']} caps in "
                f"~{_fmt_dur(cheap['hours_to_cap'] * 3600)}, but resets "
                f"~{_fmt_dur(cheap['blocked_hours'] * 3600)} later — a short wait, then a "
                "full allowance. NOT a reason to cut scope; carry on."
            )

    wk_label = "Week" if w.get("anchored") else "Rolling 7d"
    wk_reset = (
        f" · resets {datetime.fromtimestamp(w['resets_at']):%a %H:%M}"
        f" (in {_fmt_dur(w['resets_at'] - now)})"
        if w.get("resets_at") else ""
    )
    if w.get("used_frac") is not None:
        exact_tag = " (EXACT)" if w.get("exact") else ""
        # Same guard as the 5h line: an exact percentage can arrive before any
        # capacity is known, and it is worth printing on its own.
        dollars = (
            f"(${w['cost_usd']:.2f} of ~${w['capacity_usd']:.2f}) · "
            f"~${max(0.0, w['remaining_usd']):.2f} left"
            if w.get("capacity_usd") and w.get("remaining_usd") is not None
            else f"(${w.get('cost_usd', 0):.2f} spent)"
        )
        lines.append(
            f"{wk_label}: {'' if w.get('exact') else '~'}"
            f"{w['used_frac'] * 100:.0f}% used{exact_tag} {dollars}{wk_reset}"
        )
    else:
        lines.append(
            f"{wk_label}: ${w.get('cost_usd', 0):.2f} spent · capacity UNCALIBRATED{wk_reset}"
        )

    # A flat weekly percentage cannot tell "on pace" from "about to lock out on
    # Wednesday", because it never says how much of the week went with it.
    pace = state.get("pace")
    if pace and posture == "throughput":
        pace = None          # pacing is not this user's concern
    if pace:
        verdict = (
            "OVER PACE" if pace["over"]
            else "under pace" if pace["ratio"] < GO_WIDE_PACE_MAX
            else "on pace"
        )
        line = (
            f"Week pace: day {pace['day_index']}/{pace['days']} — "
            f"${pace['used_usd']:.2f} of ${pace['allowance_usd']:.2f} allowed by now "
            f"({pace['ratio'] * 100:.0f}% of today's share, {verdict})"
        )
        if pace["exhaust_day"]:
            line += (
                f" · at ${pace['per_day_usd']:.2f}/day the week runs dry on day "
                f"{pace['exhaust_day']}/{pace['days']}"
            )
        lines.append(line)

    # The proposal's soft warning. Rendered ONLY on a crossing, never as a
    # standing line: FINDINGS 17 measured how much position and density cost
    # this block, and a permanent row for a bucket sitting at 12% would dilute
    # the lines that do change behaviour. Silence here is the signal that
    # nothing has crossed.
    for name, label in (("session", "5h window"), ("day", "today"),
                        ("weekly", "the week"), ("weekly_premium", "the premium week")):
        bucket = state.get(name)
        if not isinstance(bucket, dict):
            continue
        uf = bucket.get("used_frac")
        if not isinstance(uf, (int, float)):
            continue
        level = _level_for(uf)
        if level == "soft":
            lines.append(
                f"SOFT LIMIT: {label} is {uf * 100:.0f}% spent"
                + (f" of ${bucket['capacity_usd']:.2f}" if bucket.get("capacity_usd") else "")
                + (" (derived from the week, not a published daily cap)"
                   if name == "day" else "")
                + ". Say so before starting anything unusually expensive."
            )
        elif level == "hard":
            lines.append(
                f"AT LIMIT: {label} is {uf * 100:.0f}% spent. This is NOT a "
                "refusal and nothing is blocked — "
                + ("state it, then call AskUserQuestion with these options: "
                   "continue at full scope · reduce scope or drop to a cheaper "
                   "model · stop and resume after the reset. A list of options "
                   "in prose is not a choice the user can act on in one step."
                   if state.get("choice_ask") else
                   "state it and let the user decide. You have already asked "
                   "this window — do NOT ask again.")
            )

    # The one lever in this repo with a measured payback. Rendered only when it
    # is actionable and only when it is new (see _compact_ack_gate).
    ca = state.get("compaction_advice")
    if ca:
        lines.append(
            f"COMPACT NOW: this session's context is {ca['ctx']:,} tokens and is "
            f"costing ${ca['per_turn_saving']:.4f} EVERY turn more than a compacted "
            f"one would. `/compact` costs ~${ca['one_off']:.2f} once and pays for "
            f"itself after ~{ca['turns']} more turns — that is against a "
            f"post-compaction context of ~{ca['post_ctx']:,} tokens, since "
            f"{ca['floor_tokens']:,} of the current one is system prompt, tools, "
            f"memory and skills that a compaction cannot reclaim. If this session "
            f"has that many turns left in it, compact before continuing."
        )

    # Dollars are not a unit an agent plans in. Turns and fan-outs are.
    a = state.get("affordance")
    if a:
        # A point figure is a claim of precision. Make it only when a fresh meter
        # reading backs it; otherwise print the interval the calibration spread allows,
        # ON the line, because a caveat three lines down does not travel with the
        # number an agent acts on (FINDINGS 28).
        if a.get("exact", True) or a["usable_hi"] == a["usable_lo"]:
            turns = f"~{a['usable_turns']} usable turns"
        else:
            turns = f"~{a['usable_lo']}-{a['usable_hi']} usable turns"
        if a.get("exact", True) or a["fanouts_hi"] == a["fanouts_lo"]:
            fits = f"~{a['fanouts_left']} fit"
        else:
            fits = f"~{a['fanouts_lo']}-{a['fanouts_hi']} fit"

        mark = ""
        if not a.get("exact", True):
            mark = (" · ESTIMATED, not the meter: this is the range this "
                    "reconstruction's own measured error allows, so plan "
                    "against the LOW end")
            if not a.get("binding_stable", True):
                # Worth its own sentence. A wide range says "roughly this
                # many"; an unstable ceiling says the arithmetic is answering a
                # different question at each end, and no single number is right.
                mark += (" — and the binding ceiling itself changes across that "
                         "range, so treat the count as a bound, not a figure")
        lines.append(
            f"Affordance: {turns} at "
            f"${a['per_turn_usd']:.3f}/turn (median this window), plus "
            f"{a['reserve_turns']} held back to stop cleanly · a {a['basis']} "
            f"fan-out costs ~${a['fanout_usd']:.2f}, {fits} · "
            f"binding: {a['binding_bucket']}"
            # The flag is independent of which bucket binds: the 5h window can
            # be the tighter ceiling while the week is already borrowing from
            # tomorrow. Surfacing only the bucket label would hide that.
            + (" · BORROWING from later days of the week" if a.get("borrowing") else "")
            + mark
        )

        # RATE is not FIT. Before this, an agent near the wall could only "go
        # slower" -- which does not decide whether the job in front of it will
        # finish, and produces an unpredictable stopping point instead of a
        # chosen one. These lines ask for a number, because a number can be
        # checked against the budget and an adverb cannot.
        stance = a.get("stance")
        if stance == "ample":
            pass
        elif stance == "short":
            lines.append(
                f"FIT CHECK: {turns} left. Before "
                "starting substantial work, estimate what it needs IN TURNS and "
                "say the number. If the estimate exceeds what is usable, say so "
                "plainly — do NOT promise to 'be economical', which changes the "
                "stopping point without changing whether the work fits."
            )
        elif stance == "landing":
            resets = (state.get("session") or {}).get("resets_at")
            soon = (f" The window resets in {_fmt_dur(resets - now)} with a full "
                    "allowance, so deferring often finishes SOONER than starting "
                    "now and being cut off." if resets else "")
            lines.append(
                f"WILL NOT FINISH MUCH: only {turns} "
                "remain. Do not begin anything you cannot finish in that. State "
                "the estimate, then "
                + ("call AskUserQuestion with these options: defer until the "
                   "reset · a smaller sub-goal that lands on a durable "
                   "checkpoint (committed, tested, written down) · a cheaper "
                   "model · proceed with a stopping point agreed NOW rather "
                   "than discovered at the wall."
                   if state.get("choice_ask") else
                   "stop. You have already put this choice to the user this "
                   "window — restate the number if it has moved, but do NOT "
                   "ask again.")
                + soon
            )
        else:
            lines.append(
                "LAND NOW: the usable budget is spent and only the landing "
                f"reserve (~{a['reserve_turns']} turns) is left. Do not start "
                "anything new. Commit what exists, write down where you got to "
                "and what comes next, and tell the user you are stopping — an "
                "interruption you choose is recoverable, one you hit is not."
            )

    # Under-spending a subscription allowance is a real cost, not a saving: the
    # user paid for headroom that expires unused, and got a worse answer for it.
    # So the affirmative case gets stated as explicitly as the warning does.
    if a and a.get("go_wide"):
        # The justification has to match why it actually fired. Under
        # `throughput` it fires unconditionally, so claiming the week is under
        # pace would be a false statement in the same breath as a directive.
        why = ("the user has chosen speed over spend"
               if posture == "throughput"
               else "headroom is ample and the week is under pace")
        lines.append(
            f"GO WIDE: {why}. Prefer the "
            "more thorough approach — parallel subagents, wider verification, "
            "deeper search. Do NOT cut scope to save usage; unspent allowance "
            "expires at the reset and buys nothing."
        )

    # The one place the tool asks for a decision instead of reporting. Fires
    # only past the prorated share, and only once per day unless it worsens.
    if state.get("pace_ask") and pace:
        # Days left AFTER the projected dry day -- not after today. Measured
        # from day_index this attached today's remainder to the exhaustion day,
        # so the clause described a 10-day week: "runs dry on day 6/7 with 4
        # day(s) still to go". exhaust_day + left must always sum to days.
        projection = ""
        if pace["exhaust_day"]:
            left = pace["days"] - pace["exhaust_day"]
            projection = (
                f", and at ${pace['per_day_usd']:.2f}/day it runs dry on day "
                f"{pace['exhaust_day']}/{pace['days']}"
            )
            if left:
                projection += f", blocking the last {left} day(s) of the week"
        lines.append(
            f"ASK BEFORE PROCEEDING: the week is at {pace['ratio'] * 100:.0f}% of "
            f"day {pace['day_index']}/{pace['days']}'s share"
            + projection
            + ". Before starting substantial work, call AskUserQuestion with the "
            "figure, the projection, and these options: continue at full scope · "
            "reduce scope or drop to a cheaper model · stop and resume after the "
            "reset. This is a checkpoint, NOT a refusal — if they say continue, "
            "continue at full scope and do not raise it again today."
        )

    # Premium models draw on a second, smaller weekly allowance as well as the
    # shared one (FINDINGS 7). Silent while it is unused; a session that goes
    # premium-heavy can exhaust it with the shared meters still looking fine.
    p = state.get("weekly_premium") or {}
    if p.get("cost_usd", 0) > 0:
        if p.get("used_frac") is not None:
            lines.append(
                f"Premium week ({p['turns']} turns): ~{p['used_frac'] * 100:.0f}% used "
                f"(${p['cost_usd']:.2f} of ~${p['capacity_usd']:.2f}) · "
                f"~${max(0.0, p['remaining_usd']):.2f} left — a SEPARATE, smaller cap"
            )
        else:
            lines.append(
                f"Premium week ({p['turns']} turns): ${p['cost_usd']:.2f} spent on a "
                "separate smaller weekly cap · UNCALIBRATED "
                "(calibrate --premium-pct <N> from the Fable bar in /usage)"
            )

    unknown = state.get("unknown_models") or []
    if unknown:
        lines.append(
            f"UNPRICED MODEL{'S' if len(unknown) > 1 else ''} seen this week: "
            f"{', '.join(unknown)} — costed at Opus-tier fallback, and possibly "
            "drawing on a model-scoped weekly cap this tool does not track "
            "(FINDINGS 7). Check /usage for a bar this block does not show, then "
            "set `premium_models` in config.json and recalibrate."
        )

    pr = state.get("meter")
    if pr:
        drift = s.get("capacity_drift_pct")
        line = (
            f"Meter: read from the status line {_fmt_dur(pr['age_seconds'])} ago — "
            "percentages and reset times above are the vendor's own, not estimates."
        )
        if drift is not None:
            # Utilization is reported to whole percent, so the implied capacity
            # carries quantisation error -- large at low utilization. Stating
            # the drift without its noise floor would invite over-reading it.
            band = 0.5 / (s["used_frac"] * 100) * 100 if s.get("used_frac") else None
            line += (
                f" Price-weighting is off by {drift:+.0f}% against it"
                + (f" (±{band:.0f}% from whole-percent rounding)" if band else "")
                + "; dollar figures are re-derived from the meter, not calibration."
            )
        lines.append(line)

    conf = s.get("confidence", "none")
    if pr:
        pass
    elif conf == "none":
        lines.append(
            "Confidence: NONE. Numbers above are spend, not headroom. "
            "Run /usage, then: usage-budget calibrate --pct <N> [--weekly-pct <M>]"
        )
    else:
        line = (
            f"Confidence: {conf} ({s.get('samples', 0)} calibration"
            f"{'s' if s.get('samples', 0) != 1 else ''})."
        )
        if b.get("reanchor"):
            # The boundary decides which turns count as "this window", so a
            # stale one corrupts the numerator, not just the displayed clock.
            line += (
                " Boundary is estimated after an idle gap — re-anchor from /usage"
                " before trusting the window figures or calibrating."
            )
        else:
            line += " Recalibrate from /usage if this looks off."
        lines.append(line)

    if pr:
        lines.append(
            "How to use: percentages and reset times are the vendor's own meter and "
            "may be quoted as fact; the DOLLAR figures remain derived (capacity is "
            "inferred from spend ÷ utilization, and utilization is whole-percent). "
            "If a task's likely spend exceeds the remaining headroom, say so before "
            "starting and offer a smaller scope or a cheaper model."
        )
    else:
        lines.append(
            "How to use: these are estimates from local transcripts weighted by list "
            "price, not the real meter. If a task's likely spend exceeds the remaining "
            "headroom, say so before starting and offer a smaller scope or a cheaper "
            "model — don't silently run the window dry. Never present these as exact."
        )
    lines.append("</usage-budget>")
    return "\n".join(lines)


def render_status(state: dict) -> str:
    s, w = state.get("session") or {}, state.get("weekly") or {}
    out = ["Claude usage budget (list-price-weighted estimate)", ""]
    out.append(f"  5h window spend    ${s.get('cost_usd', 0):.3f}  ({s.get('turns', 0)} turns)")
    if s.get("capacity_usd"):
        out.append(f"  5h capacity (est)  ${s['capacity_usd']:.3f}")
        out.append(f"  5h used            {s['used_frac'] * 100:.1f}%")
        out.append(f"  5h remaining       ${max(0.0, s['remaining_usd']):.3f}")
    ws = s.get("window_start")
    if ws:
        out.append(f"  window opened      {datetime.fromtimestamp(ws):%Y-%m-%d %H:%M}")
        out.append(
            f"  window resets      {datetime.fromtimestamp(s['resets_at']):%Y-%m-%d %H:%M}"
            f"  (in {_fmt_dur(s['resets_at'] - time.time())})"
        )
    out.append(f"  burn rate          ${state.get('burn_usd_per_hour', 0):.3f}/h "
               f"({_burn_basis(state)})")
    if s.get("hours_to_exhaustion") is not None:
        out.append(f"  time to cap        {_fmt_dur(s['hours_to_exhaustion'] * 3600)} at current burn")
    out.append("")
    out.append(
        f"  week spend         ${w.get('cost_usd', 0):.3f}"
        + ("" if w.get("anchored") else "   (rolling 7d — NOT anchored to your real week)")
    )
    if w.get("window_start"):
        out.append(f"  week opened        {datetime.fromtimestamp(w['window_start']):%Y-%m-%d %H:%M}")
    if w.get("resets_at"):
        out.append(
            f"  week resets        {datetime.fromtimestamp(w['resets_at']):%Y-%m-%d %H:%M}"
            f"  (in {_fmt_dur(w['resets_at'] - time.time())})"
        )
    if w.get("capacity_usd"):
        out.append(f"  week capacity(est) ${w['capacity_usd']:.3f}")
        out.append(f"  week used          {w['used_frac'] * 100:.1f}%")
        out.append(f"  week remaining     ${max(0.0, w['remaining_usd']):.3f}")

    pace = state.get("pace")
    day = state.get("day")
    if isinstance(day, dict) and day.get("capacity_usd"):
        out.append("")
        out.append(f"  today spend        ${day['cost_usd']:.3f}  ({day['turns']} turns)")
        out.append(f"  today share (est)  ${day['capacity_usd']:.3f}   "
                   f"(week/{WEEKLY_DAYS}, not a published daily cap)")
        out.append(f"  today used         {(day['used_frac'] or 0) * 100:.1f}%")
        out.append(f"  today window       {datetime.fromtimestamp(day['window_start']):%a %H:%M}"
                   f" -> {datetime.fromtimestamp(day['resets_at']):%a %H:%M}  ({day['basis']})")
    if pace:
        out.append("")
        out.append(f"  week day           {pace['day_index']}/{pace['days']}")
        out.append(f"  allowed by now     ${pace['allowance_usd']:.3f}"
                   f"   ({pace['ratio'] * 100:.0f}% consumed"
                   f"{', OVER PACE' if pace['over'] else ''})")
        out.append(f"  daily rate         ${pace['per_day_usd']:.3f}/day")
        if pace.get("exhaust_day"):
            out.append(f"  week runs dry      day {pace['exhaust_day']}/{pace['days']}")

    a = state.get("affordance")
    if a:
        out.append("")
        out.append(f"  median turn        ${a['per_turn_usd']:.4f}")
        out.append(f"  turns left         ~{a['turns_left']}"
                   f"   (binding: {a['binding_bucket']})")
        out.append(f"  fan-out cost       ${a['fanout_usd']:.3f}   ({a['basis']})")
        out.append(f"  fan-outs left      ~{a['fanouts_left']}")
        if a.get("go_wide"):
            out.append("  verdict            GO WIDE — headroom is ample, week under pace")
        elif a.get("borrowing"):
            out.append("  verdict            BORROWING from later days of the week")

    unknown = state.get("unknown_models") or []
    if unknown:
        out.append("")
        out.append(f"  UNPRICED MODELS    {', '.join(unknown)}")
        out.append("                     costed at Opus-tier fallback; may have an")
        out.append("                     untracked model-scoped weekly cap (FINDINGS 7)")

    p = state.get("weekly_premium") or {}
    if p.get("cost_usd", 0) > 0:
        out.append("")
        out.append(f"  premium week       ${p['cost_usd']:.3f}  ({p['turns']} turns)"
                   f"   [{', '.join(p.get('models') or [])}]")
        if p.get("capacity_usd"):
            out.append(f"  premium capacity   ${p['capacity_usd']:.3f}  (est)")
            out.append(f"  premium used       {p['used_frac'] * 100:.1f}%")
            out.append(f"  premium remaining  ${max(0.0, p['remaining_usd']):.3f}")
            if p.get("hours_to_exhaustion") is not None:
                out.append(
                    f"  premium time to cap {_fmt_dur(p['hours_to_exhaustion'] * 3600)}"
                    f" at ${p.get('burn_usd_per_hour', 0):.2f}/h premium burn"
                )
        else:
            out.append("  premium capacity   UNCALIBRATED — calibrate --premium-pct <N>")

    bind = state.get("binding") or state.get("cheap_cap")
    if bind:
        out.append("")
        tag = "WOULD RUN OUT   " if state.get("binding") else "cap, but cheap  "
        out.append(f"  {tag}   {bind['bucket']} — caps in "
                   f"{_fmt_dur(bind['hours_to_cap'] * 3600)}, resets "
                   f"{_fmt_dur(bind['blocked_hours'] * 3600)} later "
                   f"(= how long you'd be blocked)")

    out.append("")
    b = state.get("boundary") or {}
    conf = b.get("confidence", "heuristic")
    if conf == "exact":
        btxt = "exact — this is the window /usage was read in"
    elif conf == "tight":
        btxt = (
            f"re-derived, tight (first message {_fmt_dur(b['lag_seconds'])} after the "
            f"last expiry) — good to ±that much"
        )
    elif conf == "estimated":
        lag = b.get("lag_seconds")
        btxt = "ESTIMATED — "
        btxt += (
            f"a {_fmt_dur(lag)} gap preceded this window, so it may have opened earlier "
            "than shown (reset sooner). Re-anchor: /usage, then calibrate --resets"
            if lag else
            "projected from the anchor across windows with no local activity. "
            "Re-anchor: /usage, then calibrate --resets"
        )
    else:
        btxt = "heuristic (no anchor — chained from the oldest local event; set --resets)"
    out.append(f"  boundaries         {btxt}")
    if b.get("anchor_age_hours") is not None:
        out.append(f"  anchor age         {_fmt_dur(b['anchor_age_hours'] * 3600)}")
    out.append(f"  confidence         {s.get('confidence', 'none')} ({s.get('samples', 0)} samples)")
    retired = [c for c in _load(CONFIG_PATH, {}).get("calibrations", []) if c.get("retired")]
    for c in retired:
        # Shown, not hidden. An excluded sample the user cannot see is
        # indistinguishable from one that was never taken.
        out.append(f"  retired sample     {c.get('scope')} {c.get('pct')}% "
                   f"({c.get('at', '?')[:10]}) — {c.get('retired')}")
    rw = state.get("recent_windows") or []
    if rw:
        out.append("")
        out.append("  recent 5h windows:")
        for win in rw:
            out.append(
                f"    {datetime.fromtimestamp(win['start']):%m-%d %H:%M}"
                f" → {datetime.fromtimestamp(win['end']):%H:%M}"
                f"   ${win['cost_usd']:>7.3f}   {win['turns']:>4} turns"
            )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def _parse_reset(text: str, future: bool = True) -> datetime | None:
    """Parse a reset time as shown by /usage into local-time datetime.

    Accepts ISO, a bare time ("9:59pm", "21:59"), or a date+time
    ("Aug 17 6:59am", "Aug 17, 6:59am"). A bare time resolves to the next
    occurrence, which is what /usage displays.
    """
    text = " ".join(text.strip().replace(",", " ").split())
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        pass

    now = datetime.now()
    compact = text.upper().replace(" ", "")

    # Date + time, e.g. "Aug 17 6:59am"
    for fmt in ("%b %d %I:%M%p", "%b %d %H:%M", "%B %d %I:%M%p", "%B %d %H:%M",
                "%Y-%m-%d %I:%M%p", "%Y-%m-%d %H:%M"):
        for candidate in (text, text.upper(), text.title()):
            try:
                t = datetime.strptime(candidate, fmt)
            except Exception:
                continue
            year = now.year if t.year == 1900 else t.year
            out = t.replace(year=year)
            if t.year == 1900 and out < now - timedelta(days=180):
                out = out.replace(year=year + 1)
            return out

    # Bare time
    for fmt in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            t = datetime.strptime(compact if "%p" in fmt else text, fmt)
        except Exception:
            continue
        cand = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if future and cand < now:
            cand += timedelta(days=1)
        return cand
    return None


def _artifact_note(parsed: datetime, flag: str, fmt: str) -> str | None:
    """A ':59' reset is CORRECT and needs no correction (FINDINGS 15).

    This function used to advise adding a minute, on the FINDINGS 4b theory that
    ':59' was an on-the-hour boundary rendered low. Direct reads of the API
    refuted that: boundaries really do land on `HH:MM:59` (measured `21:09:59`
    and `06:59:59`). The note is kept, inverted, because the old advice is in
    circulation and a user who followed it has an anchor 60 seconds late.

    Never auto-corrected either way -- silently moving user input would blur the
    exact/estimated line, which is the same reason the old version only warned.
    """
    if parsed.minute != 59:
        return None
    return (
        "note: ':59' is the real boundary, not a rendering artifact "
        "(FINDINGS 15) — recorded as-is. If you previously rounded a ':59' "
        f"anchor up to the hour, re-anchor with {flag} "
        f'"{parsed:{fmt}}" to drop the stale +60s correction.'
    )


def calibrate(
    pct: float | None,
    weekly_pct: float | None,
    resets: str | None,
    weekly_resets: str | None = None,
    expires: str | None = None,
    cost: float | None = None,
    weekly_cost: float | None = None,
    premium_pct: float | None = None,
    premium_cost: float | None = None,
) -> str:
    config = load_config()
    now = time.time()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    notes: list[str] = []

    # Anchors must be set BEFORE recomputing spend -- they change which turns
    # fall inside each window, and therefore the cost the percentage is paired
    # with. Calibrating against the old boundaries would bake in the error.
    if resets:
        parsed = _parse_reset(resets)
        if not parsed:
            return f"error: could not parse --resets {resets!r}"
        prev = config.get("session_anchor")
        config["session_anchor"] = parsed.timestamp()
        old = _load(STATE_PATH, {}).get("session", {}).get("resets_at")
        drift = abs(parsed.timestamp() - old) / 60 if old else None
        notes.append(
            f"session grid anchored to {parsed:%Y-%m-%d %H:%M} local"
            + (f" (was drifting {drift:.0f}m)" if drift and drift >= 1 else "")
            + (" [replaced previous anchor]" if prev else "")
        )
        note = _artifact_note(parsed, "--resets", "%H:%M")
        if note:
            notes.append(note)
    if weekly_resets:
        parsed = _parse_reset(weekly_resets)
        if not parsed:
            return f"error: could not parse --weekly-resets {weekly_resets!r}"
        config["weekly_anchor"] = parsed.timestamp()
        notes.append(
            f"weekly period anchored to {parsed:%Y-%m-%d %H:%M} local "
            f"(week runs {(parsed - timedelta(days=7)):%b %d %H:%M} → {parsed:%b %d %H:%M})"
        )
        note = _artifact_note(parsed, "--weekly-resets", "%b %d %H:%M")
        if note:
            notes.append(note)
    if resets or weekly_resets:
        _save(CONFIG_PATH, config)

    state = collect()
    added = []

    if pct is not None:
        if not 0 < pct <= 100:
            return "error: --pct must be between 0 and 100 (exclusive of 0)"
        # A /usage reading and this call are separated by a human round-trip, and
        # windows roll. Pairing a pre-roll percentage with the post-roll (near
        # zero) cost would store a garbage capacity, so allow the caller to pin
        # the cost the percentage was actually observed against.
        if cost is None:
            cost = state["session"]["cost_usd"]
        if cost <= 0:
            return (
                "error: this 5h window has no recorded spend yet, so there is "
                "nothing to calibrate against. Use Claude for a few turns first."
            )
        config["calibrations"].append({
            "at": stamp,
            "scope": "session",
            "pct": pct,
            "cost": cost,
            "window_start": state["session"]["window_start"],
        })
        added.append(f"session: {pct}% at ${cost:.3f} → capacity ≈ ${cost / (pct / 100):.3f}")

    if weekly_pct is not None:
        if not 0 < weekly_pct <= 100:
            return "error: --weekly-pct must be between 0 and 100"
        cost = weekly_cost if weekly_cost is not None else state["weekly"]["cost_usd"]
        if cost <= 0:
            return "error: no recorded spend in the last 7 days"
        sample = {
            "at": stamp,
            "scope": "weekly",
            "pct": weekly_pct,
            "cost": cost,
        }
        if expires:
            exp = _parse_reset(expires)
            if not exp:
                return f"error: could not parse --expires {expires!r}"
            sample["expires_epoch"] = exp.timestamp()
            sample["expires_note"] = expires
        config["calibrations"].append(sample)
        added.append(f"weekly: {weekly_pct}% at ${cost:.3f} → capacity ≈ ${cost / (weekly_pct / 100):.3f}")
        if expires:
            added.append(
                f"        ^ expires {_parse_reset(expires):%Y-%m-%d %H:%M} — after that this "
                "sample is ignored and weekly reverts to UNCALIBRATED until you recalibrate"
            )

    if premium_pct is not None:
        if not 0 < premium_pct <= 100:
            return "error: --premium-pct must be between 0 and 100"
        cost = (premium_cost if premium_cost is not None
                else state["weekly_premium"]["cost_usd"])
        if cost <= 0:
            return (
                "error: no premium-model spend recorded in this week, so there "
                "is nothing to pair the percentage with. Note the premium bucket "
                "defaults to Fable only; set `premium_models` in config.json if "
                "your premium bar covers more."
            )
        # Deliberately NO --expires default here. The promo that inflates the
        # all-models weekly did not appear on the premium bar (measured
        # 2026-08-13: no promo banner), so this sample should outlive it.
        config["calibrations"].append({
            "at": stamp,
            "scope": "weekly_premium",
            "pct": premium_pct,
            "cost": cost,
        })
        added.append(
            f"premium weekly: {premium_pct}% at ${cost:.3f} → capacity ≈ "
            f"${cost / (premium_pct / 100):.3f}"
        )

    if not added and not notes:
        return ("nothing to do: pass --pct / --weekly-pct / --premium-pct / "
                "--resets / --weekly-resets")

    _save(CONFIG_PATH, config)
    st = collect()
    lines: list[str] = []
    if notes:
        lines.append("Anchors:")
        lines += [f"  {n}" for n in notes]
        lines.append("")
    if added:
        lines.append("Calibrated:")
        lines += [f"  {a}" for a in added]
        lines.append("")
        lines.append(
            f"  confidence now: {st['session']['confidence']} "
            f"({st['session']['samples']} samples)"
        )
        lines.append("")
    lines.append(render_status(st))
    return "\n".join(lines)


# ---------------------------------------------------------------------------


# --- Component 1 gaps: day bucket, threshold log, read API -------------------
# Phase 1 of the 2026-08-18 proposal found the fuel tank ~85% built and named
# three genuine gaps. These are those three, and NOT the proposal's blocking
# hard stop -- that would regress FINDINGS 17, where obedience went 29% -> 92%
# precisely because the block informs rather than refuses, and the shipped text
# says "NOT a refusal" on purpose. `hard` here is a LEVEL, never a gate.
SOFT_THRESHOLD = 0.80
HARD_THRESHOLD = 1.00
THRESHOLD_LOG = BASE / "thresholds.jsonl"
THRESHOLD_SEEN = BASE / "threshold_seen.json"
THRESHOLD_LOG_MAX = 2000

BUDGET_API_VERSION = 1


def _level_for(used_frac: float | None) -> str:
    """Bucket a utilisation into ok / soft / hard."""
    if used_frac is None:
        return "unknown"
    if used_frac >= HARD_THRESHOLD:
        return "hard"
    if used_frac >= SOFT_THRESHOLD:
        return "soft"
    return "ok"


def _day_bucket(state: dict, events: list, now: float) -> dict:
    """Spend inside the current DAY, phase-aligned to the weekly anchor.

    The proposal asked for a calendar-day counter. Local midnight would be the
    obvious choice and is the wrong one: `_pace` prorates the weekly allowance
    in day-slices measured from the weekly window start, so a midnight-aligned
    counter would report "today's spend" against a share computed for a
    different day. The weekly reset is 07:00 on this account, so days run
    07:00 -> 07:00 and the two numbers line up.

    There is no vendor daily cap, so the denominator is the WEEK's per-day
    share, not a published limit. Said plainly wherever it is rendered.
    """
    wk = state.get("weekly")
    wk = wk if isinstance(wk, dict) else {}
    wk_start, wk_cap = wk.get("window_start"), wk.get("capacity_usd")
    if not isinstance(wk_start, (int, float)):
        wk_start = None
    if not isinstance(wk_cap, (int, float)):
        wk_cap = None
    anchored = bool(wk.get("anchored"))

    if wk_start is not None and anchored:
        day_index = max(0, int((now - wk_start) // 86400))
        day_start = wk_start + day_index * 86400
        basis = "weekly-anchored"
    else:
        # No anchor: fall back to local midnight and say the alignment is a
        # guess, rather than silently mixing two different notions of "day".
        lt = datetime.fromtimestamp(now)
        day_start = datetime(lt.year, lt.month, lt.day).timestamp()
        day_index = 0
        basis = "local-midnight"

    day_end = day_start + 86400
    cost = sum(e[1] for e in events if day_start <= e[0] < day_end)
    turns = sum(1 for e in events if day_start <= e[0] < day_end)
    cap = (wk_cap / WEEKLY_DAYS) if wk_cap else None
    used_frac = (cost / cap) if cap else None

    return {
        "cost_usd": round(cost, 4),
        "turns": turns,
        "window_start": day_start,
        "resets_at": day_end,
        "day_index": day_index + 1,
        "basis": basis,
        "capacity_usd": round(cap, 4) if cap else None,
        "capacity_is_derived": True,   # week/7, not a published daily limit
        "used_frac": used_frac,
        "remaining_usd": round(cap - cost, 4) if cap else None,
        "confidence": wk.get("confidence") if cap else "none",
    }


def _record_crossings(snapshot: dict) -> list:
    """Append one line per NEW threshold crossing. Returns what was logged.

    Idempotent by (bucket, window_start, level): the block is rendered on every
    turn, so logging on level alone would write the same crossing hundreds of
    times and make the log useless as a record of when something happened. A
    crossing is only new if that bucket has not already reached that level in
    THIS window -- and a window rolling over resets it, which is correct,
    because crossing 80% in a fresh window is a genuinely new event.

    Downgrades are recorded too (`from` > `to`): a window rolling over or a
    calibration correcting capacity both move a bucket back down, and a log that
    only ever ratchets up would misrepresent the history.
    """
    try:
        seen = _load(THRESHOLD_SEEN, {}) or {}
    except Exception:
        seen = {}

    logged = []
    for name, b in (snapshot.get("buckets") or {}).items():
        level, ws = b.get("level"), b.get("window_start")
        if level in (None, "unknown", "ok") and not seen.get(name):
            continue
        prev = seen.get(name) or {}
        if prev.get("window_start") == ws and prev.get("level") == level:
            continue
        if level == "ok" and prev.get("level") in (None, "ok"):
            seen[name] = {"window_start": ws, "level": level}
            continue

        rec = {
            "ts": snapshot.get("generated_at"),
            "iso": datetime.fromtimestamp(
                snapshot.get("generated_at") or time.time()).isoformat(timespec="seconds"),
            "bucket": name,
            "from": prev.get("level"),
            "to": level,
            "used_frac": b.get("used_frac"),
            "cost_usd": b.get("cost_usd"),
            "capacity_usd": b.get("capacity_usd"),
            "window_start": ws,
            "resets_at": b.get("resets_at"),
            # The turn that pushed it over. Tool identity is NOT in the event
            # stream -- events are [epoch, usd, model] -- so the trigger is
            # named by model and cost. `analyze` is where per-tool attribution
            # lives; duplicating it here would mean re-parsing every transcript
            # on a hook path that has to stay under a second.
            "trigger": snapshot.get("last_turn"),
            "burn_usd_per_hour": snapshot.get("burn_usd_per_hour"),
        }
        logged.append(rec)
        seen[name] = {"window_start": ws, "level": level}

    if logged:
        try:
            THRESHOLD_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(THRESHOLD_LOG, "a") as fh:
                for rec in logged:
                    fh.write(json.dumps(rec) + "\n")
            _trim_threshold_log()
        except Exception:
            pass
        try:
            _save(THRESHOLD_SEEN, seen)
        except Exception:
            pass
    else:
        try:
            _save(THRESHOLD_SEEN, seen)
        except Exception:
            pass
    return logged


def _trim_threshold_log() -> None:
    """Keep the log bounded. It is a record, not a time series."""
    try:
        lines = THRESHOLD_LOG.read_text().splitlines()
        if len(lines) > THRESHOLD_LOG_MAX:
            THRESHOLD_LOG.write_text("\n".join(lines[-THRESHOLD_LOG_MAX:]) + "\n")
    except Exception:
        pass


def read_threshold_log(limit: int = 50) -> list:
    """Most recent crossings, newest last."""
    try:
        lines = THRESHOLD_LOG.read_text().splitlines()
    except Exception:
        return []
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def budget_read(state: dict | None = None, record: bool = False) -> dict:
    """THE READ API. Stable surface for other components; see BUDGET_API_VERSION.

    Everything downstream -- the quoter, a garbage collector, a router, any
    port to another host -- should read budget state through here rather than reaching
    into `state.json`'s internal shape. The internal shape has changed twice
    already (the premium bucket, then the landing reserve); this contract is
    what absorbs the next change.

    GUARANTEES
      - `api_version` is present and is bumped on any breaking change.
      - `buckets` always contains session / day / weekly / weekly_premium, each
        with the same keys, `None` where uncalibrated. Callers never branch on
        a key being absent.
      - `used_frac` is a fraction, not a percent, and may exceed 1.0.
      - `level` is one of ok / soft / hard / unknown. It is ADVISORY. Nothing in
        this project blocks on it (FINDINGS 17); a caller that wants to gate
        must decide that for itself and own the consequence.
      - Never raises. An uncalibrated or missing state yields a well-formed
        snapshot full of `None`, because this sits behind a prompt hook and a
        throw there costs the user their turn.

    `record=True` also appends any NEW threshold crossings to the log. Only the
    collect path should pass it, so that reading state stays free of side
    effects.
    """
    try:
        st = state if state is not None else _fresh_state()
    except Exception:
        st = {}
    st = st or {}

    try:
        events = _all_events_for_window(st)
    except Exception:
        events = []
    now = st.get("generated_at") or time.time()

    try:
        # _build_state computes this from the complete event list. Recomputing it
        # here from a session-scoped list would silently under-report the day.
        day = st.get("day")
        if not day:
            wk = st.get("weekly")
            wk = wk if isinstance(wk, dict) else {}
            probe_start = (wk.get("window_start") or now) if wk.get("anchored") \
                else now - 86400
            day = _day_bucket(st, _events_since(min(probe_start, now - 86400)), now)
    except Exception:
        day = {"cost_usd": 0.0, "turns": 0, "window_start": None, "resets_at": None,
               "capacity_usd": None, "used_frac": None, "remaining_usd": None,
               "confidence": "none", "basis": "unavailable",
               "capacity_is_derived": True, "day_index": None}

    buckets = {}
    for name, src in (("session", st.get("session")),
                      ("day", day),
                      ("weekly", st.get("weekly")),
                      ("weekly_premium", st.get("weekly_premium"))):
        # `or {}` is not enough: a truthy non-dict (a str from a corrupted
        # state.json) survives it and then throws on .get, which breaks the
        # never-raises guarantee on a prompt-hook path.
        if not isinstance(src, dict):
            src = {}
        uf = src.get("used_frac")
        if not isinstance(uf, (int, float)):
            uf = None
        buckets[name] = {
            "cost_usd": src.get("cost_usd"),
            "turns": src.get("turns"),
            "capacity_usd": src.get("capacity_usd"),
            "used_frac": uf,
            "remaining_usd": src.get("remaining_usd"),
            "window_start": src.get("window_start"),
            "resets_at": src.get("resets_at"),
            "confidence": src.get("confidence"),
            "level": _level_for(uf),
        }

    aff = st.get("affordance") or {}
    last = None
    if events:
        e = max(events, key=lambda x: x[0])
        last = {"epoch": e[0], "cost_usd": round(e[1], 6), "model": e[2]}

    snapshot = {
        "api_version": BUDGET_API_VERSION,
        "generated_at": now,
        "burn_usd_per_hour": st.get("burn_usd_per_hour"),
        "per_turn_usd": aff.get("per_turn_usd"),
        "usable_turns": aff.get("usable_turns"),
        "posture": st.get("posture"),
        "overage": st.get("overage"),
        "thresholds": {"soft": SOFT_THRESHOLD, "hard": HARD_THRESHOLD},
        "buckets": buckets,
        "binding": {
            "bucket": aff.get("binding_bucket"),
            "headroom_usd": aff.get("headroom_usd"),
        },
        "calibrated": any(b["capacity_usd"] for b in buckets.values()),
        "last_turn": last,
    }
    if record:
        snapshot["crossings"] = _record_crossings(snapshot)
    return snapshot


# --- Quoting (Component 2) -------------------------------------------------
# A hard estimate of what reading a set of files will cost, produced BEFORE the
# plan is proposed, so the user approves a number instead of a vibe.
#
# TWO DIFFERENT QUESTIONS, TWO DIFFERENT FORMULAS. Conflating them is the trap.
#
#   (a) "Will Read even return this file whole?" -- answered by REPLICATING the
#       host's own gate, because only the host's arithmetic decides that.
#   (b) "What will it cost me?" -- answered by a ratio MEASURED against real
#       tokenizer output, because the host's gate formula is deliberately crude.
#
# (a) THE HOST'S GATE, as Read itself reports it:
#
#       256 KB (262,144 bytes)  -> HARD ERROR, nothing is returned
#       25,000 tokens           -> PAGINATES, returns page 1 + an offset hint
#       2,000 lines             -> Read's default line limit (its tool description)
#       token estimate          =  round(rendered_chars / 2) for json/jsonl/jsonc,
#                                  / 4 for everything else
#
#     Measured against Read itself: a 270,000-byte file returned
#     "File content (263.7KB) exceeds maximum allowed size (256KB)" and no
#     content. A 64,000-byte .json returned page 1 with
#     "showing lines 1-3035 of 8001 total (56005 tokens, cap 25000)" -- so the
#     token cap PAGINATES rather than failing, and 56005 for 64,000 bytes only
#     reconciles if the estimate is taken on the RENDERED text (line-number
#     prefixes included) at a divisor of 2. The divisor of 4 for other
#     extensions was not separately measured. Both facts matter: a quoter
#     that reports a 40 MB log as 15M tokens is useless, and one that reports it
#     as an error is wrong.
#
# (b) BYTES_PER_TOKEN = 2.6, NOT the 4.0 the host uses for its gate and not the
#     4.0 everyone reaches for. Measured against exact ground truth: an
#     assistant transcript record carries both its text and
#     `usage.output_tokens`, and when a reply has exactly one content block that
#     count belongs to that block alone -- the one-record-per-block duplication
#     of FINDINGS 11 is what makes the "exactly one" filter necessary. n=579
#     samples across every local transcript:
#
#         prose (<10% fenced)     n=531   median 2.778   p10 2.544   p90 3.028
#         mixed (10-60% fenced)   n=40    median 2.780   p10 2.570   p90 2.915
#         code-dominant (>60%)    n=8     median 2.634   p10 2.605   p90 2.753
#         ALL                     n=579   median 2.775   p10 2.544   min 2.150
#
#     2.6 sits just under the code median and near the all-sample p10, so the
#     estimate leans high in tokens. Deliberate: FINDINGS 11 shipped an estimate
#     wrong in the UNSAFE direction -- it under-reported usage -- for its first
#     four days, and a quoter that under-quotes is that defect wearing a new hat.
#     The host's own 4.0 would under-count real text by about 35%.
#
# LINE_PREFIX_TOKENS = 2. Read renders `cat -n` style and the prefix is the bare
# line number plus a tab (`236\t`), NOT space-padded -- confirmed against real
# tool_result records. Digits tokenize badly, so a number plus a tab is about two
# tokens whatever the file is. On a 2,000-line file that is ~4,000 tokens the raw
# byte count cannot see.
BYTES_PER_TOKEN = 2.6
LINE_PREFIX_TOKENS = 2
QUOTE_OVERHEAD_MULT = 1.15

READ_MAX_BYTES = 262_144      # hard error above this ("maximum allowed size (256KB)")
READ_MAX_TOKENS = 25_000      # paginates above this ("cap 25000")
READ_JSON_DIVISOR = 2         # estimate divisor for json/jsonl/jsonc (measured)
READ_DEFAULT_DIVISOR = 4      # estimate divisor otherwise (not separately measured)
JSON_EXTS = {".json", ".jsonl", ".jsonc"}

BINARY_SNIFF_BYTES = 8192
QUOTE_CHUNK = 1 << 20

# Extensions that are binary regardless of what a NUL sniff finds in the first
# 8 KB -- some are text-ish at the head and unreadable after it.
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".avif",
    ".pdf", ".zip", ".gz", ".bz2", ".xz", ".7z", ".tar", ".rar", ".jar",
    ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".flac", ".ogg", ".webm",
    ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a", ".class", ".wasm",
    ".pyc", ".pyo", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".sqlite", ".db", ".parquet", ".npy", ".npz", ".pkl", ".h5",
}

# How many turns a freshly-read file is assumed to sit in context. Not a guess
# about the user: `analyze` established that a tool result is paid for on every
# subsequent turn of the session, not once, so a quote that prices only the
# landing cost repeats the exact error `analyze` exists to correct.
QUOTE_CARRY_TURNS = 20


def _host_estimate(rendered_chars: int, ext: str) -> int:
    """Replicate the token estimate Read uses for its size gate.

    Used ONLY to predict the host's gating behaviour -- never to price anything.
    round(), not ceil(): the measured JSON case cannot tell them apart, and they
    differ by at most one token.
    """
    div = READ_JSON_DIVISOR if ext in JSON_EXTS else READ_DEFAULT_DIVISOR
    return round(rendered_chars / div)


def _measure_file(path: str) -> dict:
    """Size one file for quoting, reading no more of it than necessary.

    Always returns `path` and `status`; status is one of `ok`, `binary`,
    `empty`, `missing`, `unreadable`, `not_a_file`, or `too_large`.

    Binary files are FLAGGED AND EXCLUDED, never estimated as text. Extension
    first (cheap, and catches formats that look like text at the head), then a
    NUL sniff of the first 8 KB. Estimating a PNG at bytes/2.6 would be
    confidently wrong in the expensive direction and would drown the real files.

    `too_large` means Read returns NOTHING -- the 256 KB byte cap. Files over the
    token cap are still `ok`, because Read paginates those rather than failing;
    they carry `paginated` and are quoted at one page.
    """
    out = {"path": path, "bytes": 0, "lines": 0, "paginated": False}
    try:
        if not os.path.exists(path):
            return {**out, "status": "missing"}
        if not os.path.isfile(path):
            return {**out, "status": "not_a_file"}
        size = os.path.getsize(path)
    except OSError:
        return {**out, "status": "unreadable"}

    out["bytes"] = size
    ext = os.path.splitext(path)[1].lower()
    if size == 0:
        return {**out, "status": "empty"}
    if ext in BINARY_EXTS:
        return {**out, "status": "binary", "reason": "extension"}
    if size > READ_MAX_BYTES:
        return {**out, "status": "too_large", "reason": f"over {READ_MAX_BYTES:,}B cap"}

    lines = 0
    try:
        with open(path, "rb") as fh:
            first = fh.read(min(BINARY_SNIFF_BYTES, size))
            if b"\x00" in first:
                return {**out, "status": "binary", "reason": "NUL byte"}
            buf = first
            while buf:
                lines += buf.count(b"\n")
                buf = fh.read(QUOTE_CHUNK)
    except OSError:
        return {**out, "status": "unreadable"}

    lines = lines + 1 if size else 0        # a trailing line without \n still renders
    out["lines"] = lines

    # Rendered size is what Read actually hands over: content plus `N\t` per line.
    prefix_chars = sum(len(str(i)) + 1 for i in range(1, lines + 1)) if lines else 0
    rendered = size + prefix_chars
    out["rendered_chars"] = rendered
    out["host_estimate"] = _host_estimate(rendered, ext)

    if out["host_estimate"] > READ_MAX_TOKENS:
        # Read will hand back one page. Quote the page, and say so.
        out["paginated"] = True
        share = READ_MAX_TOKENS / out["host_estimate"]
        out["effective_bytes"] = int(size * share)
        out["effective_lines"] = int(lines * share)
    else:
        out["effective_bytes"] = size
        out["effective_lines"] = lines

    return {**out, "status": "ok"}


def _tokens_for(measured: dict, multiplier: float = QUOTE_OVERHEAD_MULT) -> int:
    """Apply the measured cost formula to one measured file.

        tokens = ceil(effective_bytes / BYTES_PER_TOKEN
                      + effective_lines * LINE_PREFIX_TOKENS) * multiplier
    """
    if measured.get("status") != "ok":
        return 0
    body = measured["effective_bytes"] / BYTES_PER_TOKEN
    prefixes = measured["effective_lines"] * LINE_PREFIX_TOKENS
    return int(math.ceil((body + prefixes) * multiplier))


def quote_paths(paths: list, model: str | None = None,
                multiplier: float = QUOTE_OVERHEAD_MULT,
                carry_turns: int = QUOTE_CARRY_TURNS,
                state: dict | None = None) -> dict:
    """Price reading `paths`, and say what that is as a share of what is left.

    Two costs, because there are two. Landing cost is the one-off cache WRITE
    when the content first enters context. Carry is what it costs on every turn
    afterwards, re-read as part of the cached prefix at CACHE_READ_MULT. For
    anything but a very short session the carry is the larger number, which is
    the whole reason `analyze` exists.

    The share is quoted against the BINDING bucket's headroom, not against an
    invented daily budget. This tool tracks a rolling 5-hour window and a 7-day
    week because those are the limits that exist; there is no calendar-day
    counter to take a percentage of.
    """
    model_key = _norm_model(model, None) if model else None
    if not model_key or model_key not in PRICES:
        model_key = "claude-opus-5"
    rate = _price(model_key)["in"] / 1_000_000

    measured = [_measure_file(p) for p in paths]
    ok = [m for m in measured if m["status"] == "ok"]
    skipped = [m for m in measured if m["status"] != "ok"]

    for m in ok:
        m["tokens"] = _tokens_for(m, multiplier)
    tokens = sum(m["tokens"] for m in ok)

    landing = tokens * CACHE_WRITE_5M_MULT * rate
    carry_per_turn = tokens * CACHE_READ_MULT * rate
    total = landing + carry_per_turn * carry_turns

    q = {
        "model": model_key,
        "multiplier": multiplier,
        "files_quoted": len(ok),
        "files_skipped": len(skipped),
        "bytes": sum(m["effective_bytes"] for m in ok),
        "lines": sum(m["effective_lines"] for m in ok),
        "paginated": [m["path"] for m in ok if m["paginated"]],
        "tokens": tokens,
        "landing_usd": round(landing, 4),
        "carry_usd_per_turn": round(carry_per_turn, 4),
        "carry_turns": carry_turns,
        "total_usd": round(total, 4),
        "files": ok,
        "skipped": skipped,
        "formula": (f"ceil(effective_bytes/{BYTES_PER_TOKEN} + "
                    f"lines*{LINE_PREFIX_TOKENS}) * {multiplier}"),
    }

    snap = budget_read(state)
    headroom = (snap.get("binding") or {}).get("headroom_usd")
    if headroom:
        q["headroom_usd"] = headroom
        q["binding_bucket"] = snap["binding"].get("bucket")
        q["share_pct"] = round(100.0 * total / headroom, 1)
        q["over_budget"] = total > headroom
        per_turn = snap.get("per_turn_usd") or 0
        if per_turn:
            q["equivalent_turns"] = int(total / per_turn)
    return q


def render_quote(q: dict) -> str:
    """Human-readable quote. The agent pastes this into the plan proposal."""
    L = [f"QUOTE - {q['files_quoted']} file(s), {q['model']}",
         f"  {q['bytes']:,} bytes over {q['lines']:,} rendered lines",
         f"  estimated {q['tokens']:,} tokens   [{q['formula']}]",
         f"  landing ${q['landing_usd']:.4f} (cache write) "
         f"+ ${q['carry_usd_per_turn']:.4f}/turn carried",
         f"  ~${q['total_usd']:.4f} over {q['carry_turns']} turns"]
    if q.get("headroom_usd"):
        L.append(f"  = {q['share_pct']:.1f}% of the ${q['headroom_usd']:.2f} left in the "
                 f"{q.get('binding_bucket') or 'binding'} window"
                 + (f", ~{q['equivalent_turns']} turns' worth"
                    if q.get("equivalent_turns") else ""))
        if q.get("over_budget"):
            L.append("  ** EXCEEDS REMAINING BUDGET - propose a reduced scope "
                     "before proceeding. **")
    if q["paginated"]:
        L.append(f"  {len(q['paginated'])} file(s) exceed Read's "
                 f"{READ_MAX_TOKENS:,}-token cap and will paginate; "
                 f"quoted at one page each")
    if q["skipped"]:
        by = {}
        for m in q["skipped"]:
            by.setdefault(m["status"], []).append(m["path"])
        for status, ps in sorted(by.items()):
            L.append(f"  skipped ({status}): {len(ps)} - " + ", ".join(
                os.path.basename(p) for p in ps[:4]) + (" ..." if len(ps) > 4 else ""))
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(prog="usage-budget", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("collect", help="rescan transcripts, rebuild state")
    p.add_argument("--full", action="store_true", help="ignore cache, full rescan")
    p.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("inject", help="emit <usage-budget> block for a hook")
    sub.add_parser("precompact",
                   help="emit summarisation priorities for a PreCompact hook")
    po = sub.add_parser("posture", help="show or set how hard to economise")
    po.add_argument("value", nargs="?", choices=POSTURES)
    sl = sub.add_parser(
        "statusline",
        help="status line command: record the meter from Claude Code's JSON on "
             "stdin, then run CMD (your own status line) with the same input")
    sl.add_argument("wrapped", nargs=argparse.REMAINDER, metavar="-- CMD ...",
                    help="your existing status line command; omit for a short default line")
    me = sub.add_parser("meter", help="show the vendor meter last recorded from the status line")
    me.add_argument("--json", action="store_true", help="machine-readable")
    xu = sub.add_parser("extra-usage",
                        help="declare whether usage credits cover you past a plan limit")
    xu.add_argument("value", nargs="?", choices=("on", "off", "unset"))

    p = sub.add_parser("status", help="human-readable summary")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("calibrate", help="teach capacity from a /usage reading")
    p.add_argument("--pct", type=float, help="5h window percent used, from /usage")
    p.add_argument("--weekly-pct", type=float, help="weekly percent used, from /usage")
    p.add_argument("--resets", help='session reset time from /usage, e.g. "10:00pm" — sets the 5h grid anchor. A displayed ":59" is usually an on-the-hour boundary rendered one minute low (FINDINGS 4b)')
    p.add_argument("--weekly-resets", help='weekly reset from /usage, e.g. "Aug 17 7:00am" — sets the weekly period')
    p.add_argument("--expires", help='date a promotional weekly limit ends, e.g. "Aug 19 7:00am" — sample is ignored after it')
    p.add_argument("--cost", type=float, help="pin the session cost the percentage was observed against, instead of the live figure (use when the window rolled between reading /usage and running this)")
    p.add_argument("--weekly-cost", type=float, help="same, for the weekly percentage")
    p.add_argument("--premium-pct", type=float, help='percent used from the premium-model weekly bar in /usage (the separate "Current week (Fable)" meter) — a smaller cap that premium turns draw on as well as the shared one')
    p.add_argument("--premium-cost", type=float, help="pin the premium-weighted cost the premium percentage was observed against")

    sub.add_parser("predict", help="pre-register what each weighting hypothesis implies /usage will say")

    p = sub.add_parser("analyze", help="where the spend went: amortised context cost per tool call, and the compaction break-even")
    p.add_argument("--days", type=int, default=7, help="how far back to scan (default 7)")
    p.add_argument("--summary-tokens", type=int, default=DEFAULT_SUMMARY_TOKENS,
                   help=f"assumed post-compaction context size (default {DEFAULT_SUMMARY_TOKENS:,})")

    p = sub.add_parser("api", help="the stable read surface other components consume (JSON)")
    p.add_argument("--pretty", action="store_true")

    p = sub.add_parser("thresholds", help="threshold crossings that have been logged")
    p.add_argument("-n", type=int, default=20, help="how many to show (default 20)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("quote", help="hard token/cost estimate for reading a file list, BEFORE proposing a plan")
    p.add_argument("paths", nargs="*", help="files to quote; omit and use --stdin for a long list")
    p.add_argument("--stdin", action="store_true", help="read newline-separated paths from stdin")
    p.add_argument("--json", action="store_true", help="machine-readable")
    p.add_argument("--model", help=f"price against this model (default claude-opus-5)")
    p.add_argument("--multiplier", type=float, default=QUOTE_OVERHEAD_MULT,
                   help=f"envelope overhead multiplier (default {QUOTE_OVERHEAD_MULT})")
    p.add_argument("--turns", type=int, default=QUOTE_CARRY_TURNS,
                   help=f"turns the content is assumed to stay in context (default {QUOTE_CARRY_TURNS})")

    sub.add_parser("reset", help="clear calibration history")
    v = sub.add_parser("verify",
        help="check the install end to end; exit 0 only when it is usable")
    v.add_argument("--json", action="store_true", help="machine-readable")

    args = ap.parse_args()
    BASE.mkdir(parents=True, exist_ok=True)

    if args.cmd == "collect":
        st = collect(full=args.full, verbose=args.verbose)
        # Only the collect path records crossings: it is the one place with
        # freshly-built state, and it keeps `budget_read` free of side effects
        # everywhere else.
        try:
            budget_read(st, record=True)
        except Exception:
            pass
        if not args.verbose:
            print(render_status(st))
        return 0

    if args.cmd == "precompact":
        # Stdout IS the instruction, on the assumption this hook rests on (FINDINGS
        # 29): whatever a PreCompact hook prints reaches the summariser as
        # additional instructions. So anything else reaching stdout -- a traceback,
        # a warning -- would be fed to the summariser as guidance. Fail silent and empty, always: an
        # empty hook output contributes nothing and the compaction proceeds
        # exactly as it would have.
        try:
            trigger = None
            if not sys.stdin.isatty():
                payload = json.loads(sys.stdin.read() or "{}")
                if isinstance(payload, dict):
                    trigger = payload.get("trigger")
            print(precompact_instructions(trigger))
        except Exception:
            pass
        return 0

    if args.cmd == "inject":
        # A hook must never break the prompt. Any failure = emit nothing.
        try:
            st = _fresh_state()
            _binding_ack_gate(st)
            _pace_ask_gate(st)
            _choice_ask_gate(st)
            # The hook payload carries this session's transcript path, which is
            # the only cheap way to know the CURRENT context size. Guarded on
            # isatty so a manual run does not block waiting on a pipe.
            transcript = None
            try:
                if not sys.stdin.isatty():
                    payload = json.loads(sys.stdin.read() or "{}")
                    if isinstance(payload, dict):
                        transcript = payload.get("transcript_path")
            except Exception:
                transcript = None
            try:
                advice = _compaction_advice(transcript)
                if _compact_ack_gate(advice, transcript):
                    st["compaction_advice"] = advice
            except Exception:
                pass
            print(render_inject(st))
        except Exception:
            pass
        return 0

    if args.cmd == "api":
        print(json.dumps(budget_read(), indent=2 if args.pretty else None))
        return 0

    if args.cmd == "verify":
        checks, ok = verify_install()
        if args.json:
            print(json.dumps({"ok": ok, "checks": checks}, indent=2))
        else:
            print(render_verify(checks, ok))
        # The exit code IS the contract an installing agent checks. Keep it
        # boolean: 0 usable, 1 not. Anything richer belongs in --json.
        return 0 if ok else 1

    if args.cmd == "thresholds":
        rows = read_threshold_log(args.n)
        if args.json:
            print(json.dumps(rows, indent=2))
        elif not rows:
            print("no threshold crossings logged yet")
        else:
            for r in rows:
                frm = r.get("from") or "-"
                pct = r.get("used_frac")
                pct = f"{pct*100:.0f}%" if isinstance(pct, (int, float)) else "?"
                trig = r.get("trigger") or {}
                print(f"{r.get('iso','?'):20} {r.get('bucket','?'):16} "
                      f"{frm:>7} -> {r.get('to','?'):<6} {pct:>5}  "
                      f"${r.get('cost_usd') or 0:.2f}/"
                      f"${r.get('capacity_usd') or 0:.2f}"
                      + (f"  [{trig.get('model')}]" if trig.get("model") else ""))
        return 0

    if args.cmd == "quote":
        paths = list(args.paths)
        if args.stdin or not paths:
            paths += [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
        if not paths:
            print("quote: no paths given (pass paths, or --stdin)", file=sys.stderr)
            return 2
        q = quote_paths(paths, model=args.model, multiplier=args.multiplier,
                        carry_turns=args.turns)
        print(json.dumps(q, indent=2) if args.json else render_quote(q))
        # Non-zero exit when the quote does not fit, so a caller can branch.
        return 1 if q.get("over_budget") else 0

    if args.cmd == "posture":
        cfg = load_config()
        if args.value:
            cfg["posture"] = args.value
            _save(CONFIG_PATH, cfg)
        cur = _posture(cfg)
        print(f"posture: {cur}")
        if not args.value:
            for name, blurb in (
                ("throughput", "speed over spend — no pacing, no checkpoints, always go wide"),
                ("balanced",   "default — pace the week, check in past today's share"),
                ("stretch",    "ration across the week — cheaper paths, earlier check-ins"),
            ):
                print(f"  {'*' if name == cur else ' '} {name:11} {blurb}")
        return 0

    if args.cmd == "statusline":
        # Runs on every status line refresh, so it stays cheap: no config load,
        # no collect. And it must never take the user's status line down with
        # it -- a bookkeeping failure is swallowed, and the wrapped command
        # still gets the exact bytes Claude Code sent.
        import subprocess
        raw = b""
        try:
            if not sys.stdin.isatty():
                raw = sys.stdin.buffer.read()
        except Exception:
            raw = b""
        payload = {}
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
            if isinstance(payload, dict):
                record_statusline(payload)
            else:
                payload = {}
        except Exception:
            payload = payload if isinstance(payload, dict) else {}
        cmd = list(args.wrapped or [])
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if cmd:
            try:
                return subprocess.run(cmd, input=raw).returncode
            except OSError as e:
                print(f"usage-budget: cannot run {cmd[0]}: {e}", file=sys.stderr)
                return 127
        # Bytes, not print(): on Windows a pipe gets the locale encoding, which
        # would garble the "·" for a host that reads the status line as UTF-8.
        sys.stdout.buffer.write((_statusline_text(payload) + "\n").encode("utf-8"))
        return 0

    if args.cmd == "meter":
        mr = _load(METER_PATH, None)
        if not isinstance(mr, dict) or not mr.get("limits"):
            print("no meter recorded yet -- wire `usage-budget statusline` as your "
                  "status line (./install.sh does it), then let Claude reply once",
                  file=sys.stderr)
            return 1
        age = time.time() - float(mr.get("fetched_at") or 0)
        if args.json:
            print(json.dumps(dict(mr, age_seconds=round(age),
                                  fresh=_load_meter() is not None), indent=2))
            return 0
        state = "fresh" if age <= METER_MAX_AGE else "STALE -- not served as exact"
        print(f"vendor meter, read from the status line {_fmt_dur(age)} ago ({state}):")
        for kind, lim in sorted(mr["limits"].items()):
            r = lim.get("resets_at")
            when = f"  resets {datetime.fromtimestamp(r):%Y-%m-%d %H:%M}" if r else ""
            print(f"  {kind:22} {lim.get('utilization')}%{when}")
        if mr.get("unknown_kinds"):
            print(f"  NOTE: unrecognised kinds carried through: "
                  f"{', '.join(mr['unknown_kinds'])}")
        return 0

    if args.cmd == "extra-usage":
        cfg = load_config()
        if args.value:
            if args.value == "unset":
                cfg.pop("extra_usage", None)
            else:
                cfg["extra_usage"] = args.value == "on"
            _save(CONFIG_PATH, cfg)
            # The lockout forecast is computed at collect time, so rebuild now:
            # a declaration must change the very next block, not the one after
            # the next Stop hook.
            try:
                collect()
            except Exception:
                pass
        ov = _overage(cfg)
        if ov is None:
            print("extra usage: unknown -- a cap is treated as a hard stop. "
                  "Declare it with `usage-budget extra-usage on|off`.")
        elif ov["enabled"]:
            print("extra usage: on -- a cap is a price change, so lockout warnings "
                  "become cost notes.")
        else:
            print("extra usage: off -- a cap is a hard stop.")
        return 0

    if args.cmd == "status":
        st = _fresh_state()
        print(json.dumps(st, indent=2) if args.json else render_status(st))
        return 0

    if args.cmd == "calibrate":
        print(calibrate(args.pct, args.weekly_pct, args.resets, args.weekly_resets,
                        args.expires, args.cost, args.weekly_cost,
                        args.premium_pct, args.premium_cost))
        return 0

    if args.cmd == "predict":
        print(predict())
        return 0

    if args.cmd == "analyze":
        print(analyze(days=args.days, summary_tokens=args.summary_tokens))
        return 0

    if args.cmd == "reset":
        cfg = load_config()
        cfg["calibrations"] = []
        _save(CONFIG_PATH, cfg)
        print("calibration history cleared")
        return 0

    ap.print_help()
    return 1


# ---------------------------------------------------------------------------
# verify -- the install gate.
#
# An agent-driven install needs a machine-checkable finish line, because agents
# declare success optimistically and a transcript that "looks right" is not
# evidence. Exit 0 means the tool will actually produce a usable budget block on
# the next turn. Anything else means it will not, and says which check failed.
#
# Calibration counts as REQUIRED on purpose. Uncalibrated, the tool still runs
# and still reports raw spend, but every capacity figure it derives is absent --
# which is the one step a human left alone reliably skips.
# ---------------------------------------------------------------------------

SETTINGS_PATH = HOME / ".claude" / "settings.json"
SKILL_PATH = HOME / ".claude" / "skills" / "usage-budget"


def _hooks_wired(settings_path: Path = SETTINGS_PATH) -> tuple[bool, bool]:
    """(inject_on_UserPromptSubmit, collect_on_Stop) as found in settings.json."""
    cfg = _load(settings_path, {}) or {}
    hooks = cfg.get("hooks") or {}

    def _has(event: str, needle: str) -> bool:
        for group in hooks.get(event) or []:
            for h in group.get("hooks") or []:
                cmd = h.get("command") or ""
                if "usage_budget" in cmd and needle in cmd:
                    return True
        return False

    return _has("UserPromptSubmit", "inject"), _has("Stop", "collect")


def _statusline_wired(settings_path: Path = SETTINGS_PATH) -> bool:
    """Whether the configured status line runs `usage_budget.py statusline`."""
    sl = (_load(settings_path, {}) or {}).get("statusLine") or {}
    cmd = sl.get("command") if isinstance(sl, dict) else None
    return isinstance(cmd, str) and "usage_budget" in cmd and " statusline" in cmd


def verify_install() -> tuple[list[dict], bool]:
    """Run every install check. Returns (results, ok) where ok gates exit 0."""
    checks: list[dict] = []

    def add(name, ok, detail, required=True, fix=None):
        checks.append({"check": name, "ok": bool(ok), "detail": detail,
                       "required": required, "fix": fix})

    add("python", sys.version_info >= (3, 9),
        f"{sys.version_info.major}.{sys.version_info.minor}",
        fix="install Python 3.9 or newer")

    add("state dir", BASE.is_dir() and os.access(BASE, os.W_OK), str(BASE),
        fix=f"mkdir -p {BASE}")

    n_jsonl = 0
    if PROJECTS.is_dir():
        for _ in PROJECTS.rglob("*.jsonl"):
            n_jsonl += 1
            if n_jsonl >= 1:
                break
    add("transcripts", n_jsonl > 0,
        f"{PROJECTS}" + ("" if n_jsonl else " (none yet)"),
        required=False,
        fix="expected on a fresh machine -- they appear after your first session")

    inject_ok, collect_ok = _hooks_wired()
    add("hook: UserPromptSubmit -> inject", inject_ok,
        "injects the budget block into every prompt",
        fix="./install.sh   (or add it by hand to ~/.claude/settings.json)")
    add("hook: Stop -> collect", collect_ok,
        "refreshes spend after each turn",
        fix="./install.sh")

    add("skill", SKILL_PATH.exists(), str(SKILL_PATH), required=False,
        fix="./install.sh  -- teaches the agent when to act on the block")

    add("status line -> statusline", _statusline_wired(),
        "records the vendor's own meter (Pro/Max only)", required=False,
        fix="./install.sh  -- wraps an existing status line rather than replacing it")

    meter = None
    try:
        meter = _load_meter()
    except Exception:
        meter = None
    calibrations = (_load(CONFIG_PATH, {}) or {}).get("calibrations") or []
    have_capacity = bool(meter) or bool(calibrations)
    add("capacity known", have_capacity,
        "vendor meter, from the status line" if meter else
        (f"{len(calibrations)} calibration(s)" if calibrations else "UNCALIBRATED"),
        fix="run /usage in Claude Code, then: usage-budget calibrate "
            "--pct N --resets 'H:MMpm' --weekly-pct N --weekly-resets 'Mon H:MMam'")

    emitted = ""
    try:
        emitted = render_inject(_fresh_state())
    except Exception as e:
        emitted = f"__error__ {type(e).__name__}: {e}"
    add("emits a budget block", emitted.strip().startswith("<usage-budget>"),
        f"{len(emitted)} chars",
        fix="re-run ./install.sh; if it persists this is a bug worth reporting")

    ok = all(c["ok"] for c in checks if c["required"])
    return checks, ok


def render_verify(checks: list[dict], ok: bool) -> str:
    lines = []
    for c in checks:
        if c["ok"]:
            mark = "PASS"
        elif c["required"]:
            mark = "FAIL"
        else:
            mark = "WARN"
        lines.append(f"  [{mark}] {c['check']}: {c['detail']}")
        if not c["ok"] and c.get("fix"):
            lines.append(f"         fix: {c['fix']}")
    lines.append("")
    lines.append("READY -- the next turn will carry a usable budget block."
                 if ok else
                 "NOT READY -- fix the FAIL lines above, then re-run `usage-budget verify`.")
    return "\n".join(lines)



if __name__ == "__main__":
    sys.exit(main())
