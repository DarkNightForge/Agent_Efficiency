# usage-budget

Estimates how much Claude subscription usage is left in the current 5-hour window,
and injects that estimate into every prompt so the model can pace itself and push
back on work that won't fit.

Built 2026-08-12.

## The problem it solves

Claude Code knows how much of your plan is used: `/usage` shows it, and the status
line command is handed it as `rate_limits` on stdin. None of it reaches the
model's context, and hooks are not given it, so the agent plans blind to the one
number that decides whether a big task fits.

Two local sources close that gap. Every assistant turn is logged to
`~/.claude/projects/**/*.jsonl` with an exact `usage` block, so spend is
computable exactly. And `usage-budget statusline`, installed as your status line
or wrapped around the one you have, records the vendor's own utilization and
reset times each time Claude replies. While that reading is fresh, capacity is
implied by it rather than guessed.

## What is exact vs estimated

| Quantity | Source | Exactness |
|---|---|---|
| Tokens per turn (in/out/cache-read/cache-write) | transcript `message.usage` | **exact** |
| Cache write TTL split (5m vs 1h — 1.25x vs 2x input) | `usage.cache_creation` | **exact** |
| Subagent / sidechain turns | same transcripts, `isSidechain` | **exact** (counted) |
| Model and fast-mode flag | `message.model`, `usage.speed` | **exact** |
| Timestamps | `timestamp` | **exact** |
| 5-hour and weekly utilization | status line `rate_limits` | **exact while the reading is fresh** (15 min) |
| 5-hour window boundaries | status line `resets_at`; otherwise cached from one `/usage` reading | **exact while the meter is fresh; otherwise exact when anchored, decays after any idle gap** (see below) |
| Window capacity | implied by the meter; otherwise one `/usage` reading | **derived** from exact utilization, else **calibrated** |
| Whether a cap is a wall or a price (usage credits) | your declaration, `usage-budget extra-usage on\|off` | **declared** |
| Model-scoped weekly caps beyond Fable | not in the status line | **not tracked** (see below) |
| That the real limit is proportional to price-weighted spend | assumption | **partly validated — see below** |

### What Claude Code exposes, and what it does not (FINDINGS §14, §34)

`/usage` shows the vendor's own meter, and the status line command receives the
5-hour and 7-day `used_percentage` and `resets_at` as `rate_limits`
([status line docs](https://code.claude.com/docs/en/statusline)). None of it
reaches a hook's input or the model's context. Checked on disk against
**v2.1.234 (2026-08-17)**: transcripts carry no rate-limit fields,
`policy-limits.json` is enterprise policy (not limits), and `stats-cache.json`
is local activity counts.

This tool never reads your Claude login. Anthropic's terms describe it as for
ordinary use of Claude Code and its own apps
([legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)),
and the status line already carries the two windows that matter (see *The
vendor meter* below).

**A window boundary is server state, not something reconstructable.** A window
opens with the first message on the *account*, on any surface, so local logs
cannot place it; an off-grid 8:10pm reset that no local message could have
seeded is the evidence (FINDINGS §4c). The meter reports `resets_at` directly.
Without a fresh reading, what this tool keeps is a **cache** of the boundary,
which is exactly why anchors decay.

**Two gaps this leaves.** `/usage` shows a *"Current week (Sonnet only)"* bar on
Max/Team plans, and per-model weekly bars such as Fable's; the status line
carries none of them. This tool models one premium weekly bucket from a
**static** set, so it will go stale when a new premium tier ships. It cannot
predict that, so it detects it instead: any model absent from `PRICES` is
reported as `UNPRICED MODEL` rather than folded silently into the shared bucket.

### Window boundaries chain — and your anchor decays (corrected 2026-08-12, again 2026-08-13)

The model here has been corrected twice; the current understanding is
[`FINDINGS` §4c](../docs/FINDINGS.md):

- **Sessions chain**: a window opens with the first message on the **account**
  (any surface — claude.ai chat shares the same limits) after the previous
  window expires. Under continuous use, windows tile back-to-back and *look*
  like a fixed grid — which is what the first correction (2026-08-12) recorded,
  until an off-grid `8:10pm` reset refuted it out-of-sample.
- **Local logs can't see the seed.** Usage from other surfaces is invisible
  here, so neither the chain nor any local heuristic can reliably derive the
  phase. Only a `/usage` reading can.
- **So `--resets` seeds the chain, and the tool chains forward from there.** The
  anchor is a *reset* time, i.e. an expiry: the window it opens begins with the
  first message after it, not at the boundary itself. Windows before the anchor
  are tiled backwards for display only.

The tool no longer claims more than it knows. Each window records how its start
was derived, and `status` reports which of these you have:

| | meaning |
|---|---|
| `exact` | the window your `/usage` reading was taken in |
| `tight` | re-derived within 5 minutes of the last expiry — good to about that much |
| `estimated` | re-derived after a real gap, or projected across windows with no local activity |
| `heuristic` | no anchor at all — chained from the oldest local event |

**A chained start is an upper bound, never exact.** Activity on a surface these
logs cannot see may have opened the window earlier, which would make the true
reset *sooner* than displayed. A short re-seed lag leaves little room for that;
a long one means the boundary is a guess. When it degrades to `estimated`, the
injected block says so and asks for a re-anchor — because the boundary decides
which turns count as "this window", so a stale one corrupts the numerator that
calibration divides by, not merely the displayed clock.

Re-anchor at the start of each work block; a stale anchor was measured drifting
by 130 minutes, and a rigid grid was measured claiming an "exact" boundary
across a 10.8-hour idle gap.

(The displayed reset really does end in `:59` — it is not an artifact; see
[`FINDINGS` §4b](../docs/FINDINGS.md). Don't infer anything from one reading.)

Measured impact of the original unanchored-chain bug on one window:
**+29%** on the derived spend — the late boundary silently excluded 10 minutes of
real spend.

### The weekly window is a fixed period, not a rolling 7 days

`/usage` reports a weekly window with a real anchor (e.g. resets Aug 17 07:00, so
it opened Aug 10 07:00 — displayed as `06:59`, per the §4b artifact). A rolling
7 days covers a *different set of days*.
Measured on the same data, a rolling 7 days returned **1.93x** the real week's
spend — an error that would have made any weekly capacity estimate meaningless. Set it with `--weekly-resets`.

### Premium models draw on a second, smaller weekly cap

`/usage` shows a distinct *"Current week (Fable)"* bar alongside the all-models
one. It is a **sub-allocation, not a separate pool** ([`FINDINGS` §7](../docs/FINDINGS.md)):
premium turns count against *both*. Measured 2026-08-13 at roughly **a quarter of
the promoted all-models weekly** — and with no promo banner of its own, so it does
not expire alongside a promotional weekly sample.

That makes it possible for the smaller cap to bind while every shared meter still
looks healthy. Calibrate it from the premium bar:

```bash
usage-budget calibrate --premium-pct 11
```

The bucket defaults to Fable alone, because Fable alone has been measured.
`claude-mythos-5` carries identical list pricing and plausibly shares the bar,
but plausible is not measured — widen it via `premium_models` in `config.json`
once you have a reading.

Once more than one allowance is calibrated, the tool forecasts each against its
*own* burn rate and reset, then names whichever binds first:

```
BINDING LIMIT is the week, not the 5h window: cap in ~12h 02m vs its reset in 63h 41m.
```

(Charging the premium bucket the all-models burn rate would predict exhaustion
that never arrives, so each bucket gets its own rate.)

### Pricing formula validated against Claude Code's own accounting

`/usage` reported a session at `$11.90` for 658 input / 43.6k output / 10.8m cache
read / 539.5k cache write on `claude-opus-5`. This tool's formula, taking the 1h
cache-write multiplier:

```
0.003 + 1.090 + 5.400 + 5.395 = $11.888
```

Matching to the cent. So the *per-turn cost model* is confirmed correct; what
remains assumed is only whether the rate limiter weights those classes the same
way pricing does.

### The weighting proxy

Spend is expressed as list-price-equivalent USD:

```
usd = (input*Pin + output*Pout + cache_read*Pin*0.10
       + cache_write_5m*Pin*1.25 + cache_write_1h*Pin*2.00) / 1e6
      + web_search_requests * 0.01
```

Cache multipliers are model-independent, so only the per-model input/output prices
vary (Opus 5 `$5/$25`, fast mode `$10/$50`, Sonnet 5 `$3/$15`, Haiku 4.5 `$1/$5`).
Standard list price is used, not promotional pricing — a billing promo shouldn't
move a rate-limit weighting, and calibration absorbs any constant scale error.

**What calibration cannot fix:** if the real limit weights token classes
differently than price does, that shows up as *drift between calibrations*. Which
is why calibration spread is tracked and reported as a confidence level rather
than hidden.

## Usage

```bash
usage-budget status                     # human-readable summary
usage-budget status --json              # machine-readable
usage-budget collect                    # rescan transcripts (incremental)
usage-budget collect --full             # ignore cache, full rescan
usage-budget inject                     # the <usage-budget> block hooks emit
usage-budget predict                    # token mix + what each weighting implies
usage-budget reset                      # clear calibration history
usage-budget quote FILE...              # what will reading these cost?
usage-budget quote --stdin --json       # same, from a file list on stdin
usage-budget api                        # stable read surface for other components (JSON)
usage-budget thresholds                 # when a bucket crossed 80% / 100%
```

The injected block also carries a **COMPACT NOW** line when the current session's
context has grown enough that compacting pays for itself within ~25 turns. This
is the one lever here with a measured payback: `analyze` has always computed it,
but only on demand, so it was never seen at the moment it mattered. The session
transcript comes from the hook payload, only its tail is read, and the line is
gated to speak once per session (E12's lesson -- a directive that fires every
turn stops being read).

`quote` prices a set of files BEFORE you read them, so a plan can carry a
number instead of a vibe. It exits non-zero when the quote does not fit the
remaining headroom, so a caller can branch on it.

It uses **two** formulas on purpose ([§21](../docs/FINDINGS.md)). To predict
whether Read will return a file whole it replicates the host's own gate --
`round(rendered_chars / 4)`, or `/ 2` for `.json`/`.jsonl`/`.jsonc`, against a
256 KB hard cap (errors) and a 25,000-token cap (paginates). To predict *cost*
it uses **2.6 bytes/token**, measured against transcript ground truth (n=579,
median 2.775) rather than the familiar 4.0, which under-counts real text by
about 35%. Binary files are flagged and excluded, never estimated. The quote
reports the one-off landing cost *and* the per-turn carry, because a tool
result is re-read on every subsequent turn (`analyze`).

`predict` breaks the current window down by token mix (cache read vs output vs
cache write), thinking share, and per-model turn counts, then states what the
price-weighted model implies `/usage` should read. It was built to discriminate
between weighting hypotheses; that experiment is closed ([§6d](../docs/FINDINGS.md)),
but the breakdown remains the quickest way to see *why* a window is costing what
it costs.

### Calibrating (do this once, then occasionally)

1. Run `/usage` in an interactive Claude Code session.
2. Read the 5-hour percentage (and weekly, if you want that too).
3. Feed it back:

```bash
usage-budget calibrate --pct 13 --resets "10:00pm" \
                       --weekly-pct 26 --weekly-resets "Aug 17 7:00am"
```

**Pass the reset times.** They set the window anchor, which makes boundaries
exact for the current sitting — and boundaries change *which turns fall inside
the window*, so they must be set before the percentage is paired with a cost.
`calibrate` handles that ordering for you (anchors first, then recompute, then
pair). Anchors decay at the next idle gap (§4c), so re-anchor at the start of
each work block — anchors alone are cheap (no percentage needed).

> ⚠️ **Corrected 2026-08-17 (FINDINGS §15):** the `:59` is REAL, not a rendering artifact — the raw API returns `HH:MM:59` (measured `21:09:59` and `06:59:59`). Anchor to the value shown; do NOT round up to the hour.

~~If the displayed reset ends in `:59`, read it a few times: a value flickering
between `:59` and `:00` is an on-the-hour boundary rendered one minute low (§4b).
Anchor to the round hour; `calibrate` warns when it sees a `:59` anchor.~~

You can set anchors alone, without any percentage:

```bash
usage-budget calibrate --resets "10:00pm" --weekly-resets "Aug 17 7:00am"
```

Anchors persist, so later calibrations only need `--pct`.

Each calibration is stored as a sample. Capacity is the median across samples;
confidence rises as samples agree:

| Samples | Spread | Confidence |
|---|---|---|
| 1 | — | low |
| 2+ | ≤ 20% | medium |
| 3+ | ≤ 10% | high |

Recalibrate after any plan change — capacity changes, the samples don't.

### Retiring a bad sample

A sample can be measured correctly and still be wrong — most often because the
window boundary it was paired with was later corrected, which changes *which
turns* the percentage was measured against ([§6d](../docs/FINDINGS.md)). `reset`
is the blunt instrument: it clears every sample. To drop just one, hand-edit
`$HOME/.claude/usage-budget/config.json` and add a `retired` key to that entry,
with the reason as its value:

```json
{
  "scope": "session",
  "pct": 21.0,
  "at": "2026-08-13T04:49:03+00:00",
  "retired": "paired with the wrong turns under the pre-correction boundary model (FINDINGS 6d)"
}
```

Retired samples are excluded from the capacity median and from the sample count
that drives confidence, but they stay in the file and `status` prints them under
`retired sample`. That is deliberate: a silently shorter sample list is
indistinguishable from one that was never taken. Samples carrying an
`expires_epoch` (set by `calibrate --expires`, for promotional limits) drop out
the same way once that date passes.

## Install

```bash
./install.sh              # symlink CLI, create state dir, wire hooks
./install.sh --no-hooks   # everything except touching settings.json
```

Idempotent, and it backs up `settings.json` before any edit. Then calibrate (see
above) — until you do, it reports raw spend and says `UNCALIBRATED`.

## Files

Code lives in the repo; **state lives outside it**, because it contains your
project paths and usage figures (see the repo `.gitignore` for why).

| Path | Contents | In repo? |
|---|---|---|
| `usage_budget.py` | the whole tool | ✅ |
| `install.sh` | installer | ✅ |
| `$HOME/.claude/usage-budget/config.json` | calibration samples + window anchors (the only hand-tuned state) | ❌ |
| `$HOME/.claude/usage-budget/state.json` | last computed windows, burn rate, headroom | ❌ |
| `$HOME/.claude/usage-budget/cache.json` | per-file byte offsets + parsed events — **contains transcript paths**; regenerable, safe to delete | ❌ |
| `$HOME/.local/bin/usage-budget` | CLI shim | ❌ |

Override the state directory with `USAGE_BUDGET_HOME`, and the transcript source
with `CLAUDE_PROJECTS_DIR` (useful for testing against a fixture).

## Hooks

Wired into `~/.claude/settings.json`:

- **UserPromptSubmit** → `inject` (sync, 10s). stdout from this event is injected
  into context, which is the whole delivery mechanism.
- **Stop** → `collect` (async, 8s, output discarded). Keeps `state.json` warm so
  the inject path is a file read rather than a scan.

`inject` recollects itself if `state.json` is older than 90s, so it stays correct
even if the Stop hook is skipped. Every failure path in `inject` is swallowed — a
broken budget tool must never block a prompt.

## Performance

Transcripts are append-only, so `collect` stores a byte offset per file and parses
only new bytes. A bytes-level prefilter (`b'"usage"' in line`) runs before
`json.loads`, so only assistant lines are ever parsed.

| Pass | Time |
|---|---|
| cold, full (165MB / 293 files / 27.8k turns) | ~0.96s |
| warm incremental | ~0.13s |
| `inject` (state warm) | ~0.03s |

## The skill

`skill/usage-budget/SKILL.md` is installed (symlinked) to
`~/.claude/skills/usage-budget` by `install.sh`. Its description sits in context
permanently; the body loads when relevant.

It exists because **the injected block alone does not change behavior.** Through the
entire session that built this tool the block was present every turn and was never
acted on — headroom was ample, and one "how to use" line is weak instruction against
a large context. The skill carries the policy that line cannot: default to silence,
the four cases worth speaking up for, inform-never-gate, and how to hedge figures
that are estimates.

## The vendor meter, from the status line (FINDINGS §15, §34)

`usage-budget statusline` is a status line command. Claude Code pipes it the
[status line JSON](https://code.claude.com/docs/en/statusline); it records
`rate_limits.five_hour` and `rate_limits.seven_day` (`used_percentage`,
`resets_at`), then runs your own status line with the same input, or prints
`5h 11% · week 36%` if you have none. `install.sh` wires it, wrapping an
existing status line rather than replacing it:

```json
"statusLine": {
  "type": "command",
  "command": "python3 /path/to/usage_budget.py statusline -- sh -c '~/.claude/statusline.sh'"
}
```

While a reading is fresh the block reports **exact** utilization and **exact**
reset times, and derives capacity as `local_cost / utilization` — so
calibration stops being required and becomes a *diagnostic* instead.

```bash
usage-budget meter              # the reading on record, its age, and whether it is fresh
usage-budget meter --json
usage-budget extra-usage on     # usage credits are enabled: a cap is a price, not a wall
```

**What it touches.** No network call and no credentials: the figures arrive on
stdin from Claude Code itself. The command takes about 80 ms, and it never
breaks your status line — a bookkeeping failure is swallowed, and the wrapped
command gets the exact bytes Claude Code sent, and its exit code is passed back.

**How fresh is fresh.** `rate_limits` appears only for Pro and Max subscribers,
after a session's first API response. Claude Code also re-runs the status line
on events that bring no new response (a mode change, a `refreshInterval`), each
time resending that session's last figures, so a reading is dated by its
transcript's mtime rather than the clock: an idle session cannot overwrite a
newer reading from a session that is working. A reading older than 15 minutes
is not served as exact, nor is a window whose reset has passed; the block
reverts to the local estimate. The first prompt after an idle gap therefore
always renders estimated, which is why a calibration is still worth having. And
exact *percentages* do not make the *dollar* figures exact: capacity is still
inferred from utilization, which the block says outright.

**What the status line does not carry.** Per-model weekly caps (the Fable bar
`/usage` shows), and whether usage credits are enabled. Without the second,
every cap is forecast as a hard stop, so declare it once with
`usage-budget extra-usage on|off`; left unset, it stays a hard stop.

**Keep the estimator running.** Its disagreement with the meter is the only
continuous measurement of how far price-weighting is from the real limiter — on
the first reading, −20.5% on the 5h window and +14.0% on the weekly, in opposite
directions.

## Tests

```bash
python3 -m unittest discover -s tests -v     # 337 tests, ~2s, stdlib only
```

Every empirical claim in [`../docs/FINDINGS.md`](../docs/FINDINGS.md) has a test
here — including the `$11.888` vs `$11.90` validation against the vendor's own
reported cost, and the two bugs that shipped before being caught (the chain-vs-grid
boundary drift and the rolling-vs-fixed week). Those two are encoded as tests that
*assert the broken behavior is broken*, so a regression is loud.

Fixtures write synthetic transcripts to a temp dir and point `CLAUDE_PROJECTS_DIR`
and `USAGE_BUDGET_HOME` at it, so tests never touch real data.

**The suite is mutation-tested.** Nine deliberate breakages were introduced on a
copy of the implementation and the suite caught all nine. That check found a real
weakness: `test_partial_trailing_line_is_not_consumed` originally passed for the
wrong reason (its truncated fixture didn't contain `"usage"`, so the bytes prefilter
skipped it regardless of the newline logic) and could not distinguish correct code
from code that consumed partial lines. Passing tests are not evidence; tests that
fail when you break the thing are. Scope note: that run covered the suite as it
stood at 61 tests. Of the tests added since, only the meter-ordering and
exact-render tests (FINDINGS §34) have been checked the same way: restoring the
old ordering fails all three ordering tests, and removing the render guard fails
the render test.

Verified on Python 3.11 and 3.12 locally, 3.11–3.13 in CI. No dependencies — if the
tests ever need `pip install`, something has gone wrong.

## Limitations

- **Local transcripts only.** Usage from claude.ai, another machine, or a session
  whose transcript was pruned is invisible, so estimates read *low*.
- **The vendor meter needs the status line.** On an API-key plan, or with a
  status line this tool does not wrap, every figure is the local estimate.
- **Promotional limit changes invalidate a weekly calibration.** A sample taken
  during a promo over-states capacity once it ends. Record one with
  `--expires "<date>"` and the weekly figure reverts to `UNCALIBRATED` on its own
  rather than quietly over-reporting. Session capacity is unaffected.
- **A capacity estimate is only as good as its samples.** One sample cannot
  distinguish "capacity is X" from "the weighting shape is wrong". Calibrate again
  at a materially different usage level (e.g. ~60% rather than ~13%) — agreement
  between the two is the real evidence the price-weighting proxy holds.
- **`cleanupPeriodDays`** (default 30) prunes transcripts. Fine at a 9-day
  retention window, but a much lower setting would starve this.
- **Not billing.** These are list-price-equivalent weights on a subscription plan.
  The dollar figures are a *unit of account*, not money owed.
- **`/usage` is always authoritative.** This tool is a planning aid between
  readings.
