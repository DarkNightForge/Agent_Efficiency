# Findings

Empirical results from building the Claude Code implementation. These are the
**transferable** parts — a port to another agent (Codex, Cursor, Aider, …) can
reuse all of the reasoning here and only needs to swap the log reader and the
price table.

Everything below was measured on 2026-08-12 against a Claude Max account, and
cross-checked against a real `/usage` reading.

> ⚠️ **Single-provider caveat.** Every measurement here comes from **one provider on one plan
> tier**. Where this file says "boundaries are a grid" or "output costs 5x input", read it as
> *"that is what Claude does"* — not as a law. The mechanism (exact numerator, calibrated
> denominator) is what should generalize; the constants are not promised to.
>
> **First answers are in — see §9b.** Codex `0.148.0-alpha.9` was measured on 2026-08-14 and the
> result is sharper than expected: Codex serves both an exact numerator *and* an exact denominator,
> so §§3, 6, 6b and 6d — the price formula, the back-solved capacity, calibration order, spread as
> confidence — turn out to be **Claude-specific scaffolding for a missing percentage**, not general
> technique. The §4c boundary work, this project's most expensive debugging, does not transfer at
> all: Codex is told its reset times.

---

## 1. The agent cannot read its own rate-limit state — ⚠️ SUPERSEDED

> ⚠️ **SUPERSEDED 2026-10-22 — see §34.** The table below is missing a route:
> the status line. Since Claude Code 2.1.80 the status line command receives
> `rate_limits` — the 5-hour and 7-day `used_percentage` and `resets_at` — on
> stdin, and it is documented. What survives: the *agent* still cannot read the
> state (hooks are not given it, and none of it reaches the model's context), and
> spend still has to be reconstructed from local logs, because the status line
> carries a percentage, not a magnitude.
>
> | §1 says | Reality (§34) |
> |---|---|
> | Three routes, all closed | A fourth was open throughout: the status line's documented `rate_limits` |
> | Any feature must reconstruct spend; there is no shortcut | Spend is still reconstructed, but while a status line reading is fresh the denominator needs no calibration |
> | Querying the endpoint with the stored token is blocked, correctly — don't route around it | Still right. §15 routed around it; §34 removes that route |

Three routes, all closed:

| Route | Result |
|---|---|
| A CLI subcommand | Doesn't exist. `/usage` is a TUI-only slash command. |
| A cached file on disk | None. `~/.claude/policy-limits.json` is *enterprise policy*, not rate limits. Nothing persists the limit state. |
| Query the usage endpoint with the stored OAuth token | Reading `~/.claude/.credentials.json` is blocked by the permission classifier. Correct behavior — don't route around it. |

Rate-limit state reaches Claude Code with its API responses (the status line is
given it only after a session's first response), and nothing writes it to disk.
Claude Code additionally refuses to forward builtin slash commands like `/usage`
to background sessions.

**Conclusion:** any usage-awareness feature must reconstruct spend from local
logs. There is no shortcut.

## 2. But local logs contain everything except capacity

Claude Code writes one JSONL line per turn to `~/.claude/projects/**/*.jsonl`.
Assistant lines carry a complete `usage` object:

```json
{
  "type": "assistant",
  "timestamp": "2026-08-13T01:39:14.055Z",
  "requestId": "...",
  "isSidechain": false,
  "message": {
    "model": "claude-opus-5",
    "usage": {
      "input_tokens": 2,
      "output_tokens": 619,
      "cache_read_input_tokens": 24070,
      "cache_creation_input_tokens": 19548,
      "cache_creation": {
        "ephemeral_5m_input_tokens": 0,
        "ephemeral_1h_input_tokens": 19548
      },
      "output_tokens_details": { "thinking_tokens": 408 },
      "server_tool_use": { "web_search_requests": 0 },
      "speed": "standard"
    }
  }
}
```

Notes that matter:

- **`cache_creation` splits by TTL.** A 1h cache write costs 2.0x input; a 5m
  write costs 1.25x. Collapsing them into `cache_creation_input_tokens` loses real
  money — on a cache-heavy session it was the single largest cost component.
- **`thinking_tokens` is a *subset* of `output_tokens`.** Do not add it separately.
- **`speed: "fast"`** doubles Opus-tier pricing.
- **Subagent turns appear as ordinary lines** with `isSidechain: true`. They count
  against the same limit, so include them.
- **`requestId`** is a natural dedupe key.

## 3. The cost formula, validated to the cent

```
usd = ( input            * Pin
      + output           * Pout
      + cache_read       * Pin * 0.10
      + cache_write_5m   * Pin * 1.25
      + cache_write_1h   * Pin * 2.00 ) / 1e6
      + web_search_requests * 0.01
```

Cache multipliers are **model-independent**; only `Pin`/`Pout` vary by model.

**Validation.** `/usage` reported a session at `$11.90` for 658 input / 43.6k
output / 10.8m cache read / 539.5k cache write on `claude-opus-5`:

| Component | Tokens | Rate | USD |
|---|---:|---:|---:|
| input | 658 | $5.00/M | 0.003 |
| output | 43,600 | $25.00/M | 1.090 |
| cache read | 10,800,000 | $0.50/M | 5.400 |
| cache write (1h) | 539,500 | $10.00/M | 5.395 |
| | | **total** | **$11.888** |

Against Claude Code's own reported `$11.90`. So the per-turn cost model is
**confirmed correct**, and it confirms the 1h TTL branch is the one actually
exercised in practice.

## 4. Window boundaries are a fixed grid, NOT a chain — ⚠️ SUPERSEDED

> ⚠️ **SUPERSEDED 2026-08-13 — see §4c.** The grid model below made one correct
> out-of-sample prediction, then a wrong one: it predicted an 18:00 reset and
> `/usage` showed **8:10pm**. Sessions **chain** after all — the documented rule
> is literally true, with "first message" meaning first on the *account*, not on
> this machine. The "fixed grid" was an artifact of continuous use. What survives
> from this section: the +29% measurement of the original chain-heuristic bug,
> and the anchor mechanism (exact **under continuous use**; see §4c for when it
> decays).
>
> | §4 says | Reality (§4c) |
> |---|---|
> | Boundaries sit on a fixed 5-hour grid | A window opens with the first account-wide message after the previous expires |
> | One anchor makes boundaries exact, permanently | An anchor is exact only until the next gap in all-account activity |
> | "Activity ran unbroken from 16:30" | Measured from local logs only — usage on other surfaces is invisible |

*The section below is kept as recorded on 2026-08-12.*

This is the finding most likely to bite a reimplementation.

The documented rule — *"a window opens with your first message and lasts 5
hours; the first message after it expires opens the next one"* — reads like you
can chain windows forward from your earliest log entry. **That is wrong under
continuous use.**

Measured: `/usage` reported the session resetting at **21:59**. The chain model
derived **22:09**. Inspecting the transcripts, activity ran *unbroken* from 16:30
onward — so the real window opened at **16:59, mid-stream**, with no gap to
anchor on. With no gap, the chain simply propagates whatever phase the oldest
entry in your retention window happens to carry.

Observed reality: boundaries lie on a **fixed 5-hour grid**. The displayed reset
times initially read as `:59` past the hour, but that turned out to be a UI
rounding artifact — see §4b. The real boundaries are **on the hour**: 02:00 /
07:00 / 12:00 / 17:00 / 22:00 for this account, with the weekly reset at 07:00.

**Implication for any implementation:** take one reset time from the UI, store it
as a grid anchor, and bucket turns by `floor((t - anchor) / 5h)`. Boundaries then
become exact and cannot drift. Without an anchor, label them estimated.

Measured cost of the bug on a single window: the derived spend was **+29%** over
the true figure — the late boundary was silently excluding ten minutes of real
spend.

## 4b. A displayed reset time can be off by a minute — don't infer phase from it

The grid model was confirmed out-of-sample: it predicted this window would reset at
02:59 and `/usage` agreed. But checking three times within a few seconds returned
**2:59, then 3:00, then 2:59.**

Polling every ~2 seconds showed it flipping **constantly, with no stable runs** —
"it genuinely cannot decide." That run-free pattern is the diagnostic, and it rules
out the first explanation we reached for.

**Rejected: `now + minute-rounded countdown`.** If the UI added a countdown rounded
to whole minutes to a moving clock, the displayed value would flip **once per
minute** and sit stable for ~30 seconds either side. Simulated: 32-second runs.
Refuted by observation.

**Supported: the true value sits exactly on a minute boundary, and sub-second noise
pushes it either side.** Network round-trip variance and client/server clock skew are
comfortably in the hundreds-of-milliseconds range; if the rendered value lands within
that of `03:00:00.000`, the formatted minute flips essentially per request, with no
run structure. Simulated with ±0.4s of jitter: matches.

That distinction pins the boundary, because only one candidate is flippable by
sub-second noise:

| True boundary | What jitter can display |
|---|---|
| **03:00:00** | `2:59` / `3:00` ← **matches** |
| 02:59:00 | `2:58` / `2:59` |
| 02:59:30 | `2:59` only — stable |

So the true boundary is **exactly 03:00:00**, and the `:59` is an artifact of
rendering a value that sits precisely on the formatter's rounding edge. Constant
indecision is *stronger* evidence for an on-the-hour boundary than a stable reading
would have been.

> ❌ **REFUTED 2026-08-17 by direct measurement (§15).** Reading the raw API
> response instead of the UI returns `resets_at` values of **`21:09:59`** and
> **`06:59:59`** — on the session and weekly limits respectively.
>
> | This said | Reality |
> | --- | --- |
> | The true boundary is on the round minute (`03:00:00`) | It is **one second before** it (`HH:MM:59`) — a consistent server convention, seen on both limits |
> | `:59` is a client rendering artifact | `:59` is the actual value. The UI was rendering the truth |
> | Anchors were corrected **+60s** (session→`22:00`, weekly→`07:00`) | That correction was **wrong** and should be reverted; the original `:59` readings were accurate |
>
> The methodological error is the interesting part: three candidate mechanisms
> were simulated and discriminated by run-length, and the winner was still wrong,
> because **every candidate assumed the displayed value was derived rather than
> reported**. No amount of care inside a wrong hypothesis class escapes it. The
> run-length diagnostic below is still correct about *how a flickering UI
> behaves*; it simply was not evidence about where boundaries come from.

**What is not determined:** the exact client/server implementation. Whether the API
returns a duration the client adds to local time, or a timestamp that gets skewed in
transit, is not observable from outside and is not claimed here. What is observable
is that the value sits on the minute boundary.

This also resolves something that should have looked suspicious earlier: *two
independent limits* (session and weekly) both appearing at `:59`. One rendering
artifact affecting both is far likelier than a deliberately chosen `:59` phase — and
clean-hour boundaries are the more plausible system design.

**Consequences, both worth carrying to any port:**

1. **Anchors were corrected by +60s** (session grid to `22:00`, weekly to `07:00`).
   The error was 60 seconds out of 5 hours — 0.3% — so it changed nothing material,
   but the recorded *model* was wrong and would have propagated.
2. **Never infer a grid phase from a single displayed reset time — and measure the
   *run length*, not just whether it flickers.** Poll every couple of seconds for a
   minute:

   | Pattern | Diagnosis |
   |---|---|
   | Stable | Trust the displayed minute. |
   | Flips once, ~30s per value | Rounded-duration arithmetic; truth is near the later value. |
   | Flips constantly, no runs | Truth sits **exactly** on the boundary between the two displayed values. |

   Run length is what separates the two artifact mechanisms, and we got it wrong the
   first time by not measuring it.

Stored calibration samples were left as recorded. They were measured against
boundaries 60 seconds off, an error far below the 30% shape discrepancy in §6b, and
retroactively editing measured samples would be worse than carrying a known 0.3%.

*(2026-08-13 note: the on-the-hour **phase** claim in consequence 1 later fell
with the grid model — §4c. The display-artifact mechanism and the run-length
diagnostic stand unchanged: they describe how the UI renders a boundary, and are
independent of where boundaries come from. The 8:10pm reading that refuted the
grid itself flickered to 8:09pm, exactly as this section predicts.)*

## 4c. Sessions chain after all — seeded by usage your logs can't see

On 2026-08-13 the grid model made its first wrong prediction, and the failure
mode identified the true model.

**Measured:** the grid (anchored on the hour, confirmed out-of-sample the night
before) predicted the next reset at **18:00**. `/usage` showed **8:10pm** —
flickering to 8:09pm across readings, per §4b, so the true boundary was ≈
**20:10:00** and the window had opened at **15:10**, on no hourly grid. The
tool was silently drifting by 130 minutes.

**Nothing in the local transcripts could have seeded a 15:10 boundary** — no
local first-message at 15:10, nor at 10:10 for the implied predecessor window.
But the limits are officially shared across surfaces ("all activity in both
tools counts against the same usage limits" — claude.ai chat and Claude Code
draw one pool), so a first message *anywhere* — phone, web, another machine —
opens the window. The seed was invisible by construction.

**The working model, consistent with every observation so far:**

- The documented rule is literally true: *a window opens with your first
  message and lasts 5 hours; the first message after it expires opens the next
  one.* "First message" means first on the **account**, not on this machine.
- Under continuous use, expiry and next-message coincide, so consecutive
  windows tile back-to-back — which **looks exactly like a fixed grid**. That
  is what §4 measured during a day of near-continuous use, and why the grid
  model survived one correct prediction (the 03:00 reset) before failing.
- The original §4 anomaly dissolves: "activity ran unbroken from 16:30" was
  measured from **local logs only** — the same blind spot. A cross-surface gap
  closing at ~17:00 produces the observed boundary with no grid needed.

**Consequences for any implementation:**

1. **Anchors decay.** A stored reset time is exact only until the next gap in
   *all-account* activity — which local logs cannot detect. After any idle
   period, treat boundaries as estimated until re-anchored from the UI.
2. **Re-anchor at the start of every work block.** One `calibrate --resets`
   per sitting keeps boundaries exact for that sitting; under continuous use
   the anchor cannot drift.
3. **The weekly boundary is unaffected** — a fixed per-account weekly reset,
   officially documented (§10), with no chaining behavior.
4. **Porter note:** don't inherit the grid. Test whether your provider chains
   (leave a deliberate idle gap, then compare predicted vs displayed reset),
   and assume any usage surface you don't instrument can seed a window.

### Implemented 2026-08-14 — the code stopped tiling a grid

For two days §4c described chaining while `_session_windows` still bucketed
turns as `(epoch - anchor) // span`. A grid tiles boundaries forever and so
cannot notice you stopped working: measured here, a **10.8-hour idle gap** left
the status line reporting an *"exact"* boundary while chaining from a reset 15
hours and three windows old. It landed 70 seconds from the truth — luck, not
correctness.

The anchor is now treated as what it is, an **expiry to chain from** rather than
the start of a window, and confidence follows the evidence:

| | meaning |
|---|---|
| `exact` | the window the `/usage` reading was taken in |
| `tight` | re-derived within 5 minutes of the last expiry |
| `estimated` | re-derived after a real gap, or projected across unobserved windows |
| `heuristic` | no anchor at all |

**A chained start is an upper bound, never exact** — an unseen seed on another
surface can only have opened the window *earlier*, making the true reset sooner.
The asymmetry is worth stating explicitly for porters: your error bar on a
re-derived boundary is one-sided.

The sharper half of the bug was where the caveat *wasn't*. `status` carried a
boundary label; the **injected block carried none**, so the agent reading it
every turn had no way to account for boundary risk at all. A tool whose whole
purpose is to inform a model is only as honest as the string the model receives
— and a caveat printed on every turn is one the model learns to skip, so it now
appears only when the boundary is genuinely doubtful.

## 5. The weekly window is a fixed period, not a rolling 7 days

`/usage` gives the weekly window a real anchor (e.g. *resets Aug 17 7:00am*, so
it opened Aug 10 7:00am — displayed as `6:59am`, per the §4b artifact). A rolling
7 days covers a **different set of days**.

Measured on identical data, a rolling 7 days returned **1.93x** the real week's
spend — an error that would have made any weekly capacity estimate meaningless.

This one is officially documented: the weekly cap "resets at a fixed day and
time assigned to your account" (support.claude.com Pro/Max plan articles) — the
single piece of window semantics the docs state outright. Unlike session
windows (§4c), the weekly boundary does not chain.

## 6. Capacity must be calibrated, and calibration order matters

Capacity is unpublished, so derive it from one UI reading:

```
capacity = spend_in_window / (percent_shown / 100)
```

**Set the window anchor before pairing a percentage with a cost.** Anchors change
*which turns fall inside the window*, hence the cost. Calibrating first and
anchoring second bakes the boundary error permanently into the stored capacity.
Correct order: `set anchors → recompute spend → pair with percentage`.

Measured on a Max account. Absolute capacities are **deliberately not published
here** — they describe one account's allowance and working volume, and nothing in
this document's argument depends on them. What transfers is the shape: on this
plan the **weekly capacity ran ≈5.6× the 5h capacity** (with a +50% promo
active), so the weekly meter is the one that binds on sustained work, and the
per-window meter is the one that binds on a burst.

Everything below is stated as ratios for the same reason. The *method* is the
transferable part; your own numbers come out of one `/usage` reading.

### What calibration can and cannot fix

- **Can fix:** any constant scale error. If price-weighting is uniformly off by a
  factor, dividing by a calibrated capacity cancels it exactly.
- **Cannot fix:** a wrong *shape*. If the real limiter weights cache reads
  differently than price does, no single calibration reveals it.

This is why you should keep multiple samples and **report their spread as a
confidence level**. Two samples taken at materially different usage levels (say
15% and 60%) that agree are real evidence the proxy holds. One sample is not.

### Promotional limits expire — so should the sample

A "+50% weekly limits" promo was active during calibration. A stored capacity
taken under a promo silently over-reports headroom once it ends. Attach an expiry
to the sample and drop it automatically, reverting to *uncalibrated*. **For a
budget tool, "I don't know" beats a confident wrong answer.**

## 6b. Measured: the weighting shape looks wrong, and calibration cannot fix it

FINDINGS section 6 warned that calibration corrects a wrong *scale* but not a wrong
*shape*, and that a wrong shape shows up as drift between samples. On 2026-08-12 a
second sample produced exactly that. This section records the measurement.

Both samples came from the **same** 5-hour window — ideal for the test, since window
boundaries and capacity are identical and only accumulated spend differs:

Written relative to the capacity sample 1 implies, `C` — the disagreement is the
finding, and it is a ratio:

| Sample | `/usage` said | Weighted cost | Implied capacity |
|---|---|---|---|
| 1 (20:14) | 13% | `0.13 C` | `1.00 C` |
| 2 (21:48) | 21% | `0.266 C` | `1.27 C` |

**26.6% apart** — well beyond what integer rounding of the percentages can explain.
Rounding bounds sample 1 at `0.96–1.04 C` and sample 2 at `1.24–1.30 C`; the
intervals do not overlap. Equivalently: the tool over-reported usage by **1.27x** at
the second reading, predicting 26.6% where `/usage` said 21%.

The pattern: **marginal cost per percentage point is far higher than average.** The
spend between the two readings bought 8 percentage points, implying a *marginal*
capacity of `1.70 C` against the `1.00 C` sample 1 implies on its own. So the tool's
cost grows faster than the real meter — it over-weights whatever dominated spend
between the readings.

What dominated is not close: between the samples the window accumulated **293.9M
cache-read tokens** against 1.07M output tokens. At the list-derived 0.1x multiplier,
cache reads are **72% of the tool's entire weighted figure.**

Solving for the cache-read multiplier that makes the two samples agree gives
**0.0168** — roughly **6x lower** than list pricing's 0.1x.

### The cost model is confirmed twice, which sharpens the conclusion

`/usage` reports its own session cost, so the pricing half can be checked
independently of the limit half. Both readings agree:

| Reading | Reported | Computed | Error |
|---|---|---|---|
| screenshot 1 | $11.90 | $11.888 | $0.012 |
| screenshot 2 (5x larger) | $63.35 | $63.370 | $0.020 — **0.03%** |

Cache reads were 79% of the second figure, so the `0.1x` multiplier is the part
being most stringently tested — and it is **correct for cost**.

That upgrades the finding from a hypothesis to a deduction. Two confirmed premises:
the cost model matches Anthropic's own billing to 0.03%, and the limit percentage it
implies is 1.27x too high on the same data. Therefore:

> **The rate limiter does not weight usage by price.**

What it *is* weighted by remains open. The cache-read multiplier is one candidate.

### Why the multiplier is a hypothesis and not a result

**Two data points fitted with one free parameter always produce an exact fit.** The
"exact agreement" at 0.0168 is arithmetic, not evidence. It earns attention only
because it has a plausible mechanism and makes a falsifiable prediction:

- **Mechanism.** Price ratios and capacity ratios are different things. A cache hit
  skips most of the attention computation, so a limiter metering *compute* would
  weight cache reads far below their *price* ratio, which carries margin structure.
  0.1x is a billing number; the limiter need not use it.
- **Prediction.** Under the current weighting the tool should keep over-reporting by
  ~25–30% on cache-heavy work. Under the cache-read hypothesis it should be accurate
  once the multiplier is corrected. A third sample after a few hours of cache-heavy
  work discriminates between them.
  > ❌ **Refuted 2026-08-14 (§6d).** The third sample arrived and the tool
  > *under*-reported. A falsifiable prediction was made and it failed, which is
  > the section working as intended — see §6d for what replaced it.

The weekly samples cannot test it: they are 26% and 27%, and one percentage point is
inside rounding error, so their implied marginal capacity (over 13x the session
figure) is noise.

### The obvious next experiment does not work (identifiability)

The natural follow-up is "take a third sample." On this workload that sample would be
**wasted**, for a structural reason worth understanding before spending one.

Hypothesis B's capacity is refit from the same samples, so it scales by the same ratio
as its cost. With `f` = cache reads' share of the price-weighted figure:

```
cost_B / cost_A = 1 - f * (1 - 0.0168/0.1)
```

Both capacity and cost carry that factor, so it **cancels** — and the two hypotheses
predict the *same percentage* whenever `f` matches its value at calibration time. The
measured window ran at 77–79%, and calibration was ~79%. Predicted separation: **0.0
percentage points.** Spending more changes nothing; the parameter is unidentifiable
from same-mix data at any spend level.

Separation requires a **different token mix** — output-heavy work in a *small* context
(long generation, short conversation, few files read), which lowers `f`. The
`predict` subcommand computes current separation and states the mix needed, so a
sample is only spent when it can actually decide something.

This generalizes past this one parameter: **when samples are expensive, check
identifiability before collecting, not after.** Agentic coding sessions are
homogeneously cache-heavy, so the natural workload is close to worthless for
distinguishing weightings — the informative sample has to be deliberately staged.

### A second, confounded shape error: thinking tokens

Effort level turns out to matter to this analysis, and it was not accounted for.

Measured on this account at `effort: xhigh`: **49% of output tokens are thinking
tokens.** Thinking is billed inside `output_tokens` (§2), so the cost model handles
it correctly — but the *limiter* may weight reasoning differently from visible
output. That is a **second candidate shape error** alongside the cache-read one.

**The two are confounded in ordinary agentic work.** Long context with many tool
calls produces both a high cache-read share *and* a high thinking share, and they
move together, so varying the token mix alone cannot attribute a discrepancy to one
or the other. The output-heavy staged session described above changes both at once.

**But a magnitude check rules it out as the primary cause, for free.** Decomposing
the calibration window (759 turns) by cost component:

| Component | Share of weighted cost |
|---|---|
| cache read | **72.4%** |
| cache write | 14.6% |
| output — visible | 7.9% |
| output — thinking | **5.1%** |
| fresh input | 0.0% |

The over-report is 1.27x, so **~21% of the figure must be explained away.** Thinking
contributes 5.1%. Setting it to *zero* cannot close a 21-point gap, so:

- **Thinking weight — ruled out as the primary cause.** Bounded at 5.1% by
  construction. A possible minor contributor; not the story.
- **Cache reads — necessary.** At 72.4%, the only term large enough.

An earlier revision of this section presented the two as co-equal candidates
requiring a paired experiment to separate. That was wrong: a component worth 5% of
the total cannot explain a 21% discrepancy however badly it is weighted, and the
check needed no new samples — only arithmetic on data already collected.

**Consequence: the single output-heavy staged session is sufficient.** The paired
effort-level run is optional refinement, not a prerequisite.

**Check magnitudes before designing an experiment.** A hypothesis that cannot
account for the effect size is not a competing explanation, and eliminating it costs
nothing compared to the human-gated sample it would otherwise consume.

Also worth recording: **every per-turn figure in this document is effort-specific.**
The per-turn cost and the turns-per-window headroom quoted anywhere here were
measured at `xhigh`. At lower effort, output per turn falls and both move.

`predict` now reports thinking share and per-turn effort alongside the cache-read
share, and warns when thinking exceeds 15% of output — because that is exactly when
a single mix-varying sample stops being attributable.

### What was deliberately NOT done

The multiplier was **not** changed to 0.0168. Refitting a constant to two points is
overfitting, and this file already argues that sample spread is the honest signal
rather than something to tune away. The samples stay as recorded, confidence stays
`low` (their 23% spread exceeds the `medium` threshold — the tool correctly reports
its own uncertainty), and the hypothesis waits for a third sample.

### Practical note — ⚠️ REVERSED 2026-08-14, see §6d

> ⚠️ **This claim was wrong, and wrong in the unsafe direction.** A third sample
> (§6d) found the tool reading **41% against a real 47%** — it *under*-reported,
> so real headroom was **smaller** than displayed, not larger. Any guidance built
> on "you probably have more room than shown" has been removed.

*Recorded 2026-08-12:* ~~The error direction is the safe one: the tool
**over**-reports consumption, so real headroom is larger than displayed. For a
budget tool, erring toward "you have less than you think" is the right failure
mode.~~

### For porters

If your provider's limiter also under-weights cached input relative to price, this is
the single largest correction available on cache-heavy workloads — and the reason
FINDINGS asks for a comparison column rather than assuming these constants transfer.

## 6c. The session meter charges Fable 5 far above list price

Two `/usage` screenshots taken 7 minutes apart (2026-08-13, 15:51 and 15:58)
with **only Fable main-loop chat in between** — the Opus line byte-identical
between shots — form a controlled measurement of how the session meter weights
Fable spend:

| Delta (pure Fable, 7 min) | Value |
|---|---|
| Tokens | +0.5k input · +16.6k output · +2.3M cache read · +20.3k cache write |
| List-price cost (validated against `/usage`'s own session accounting: $61.74 → $65.34) | **+$3.60** |
| Session bar movement | **+3 points** (rounding bounds: +2.0 to +4.0) |
| Price-weighted prediction against the calibrated session capacity | **+0.43 points** |

The session meter charged that spend at roughly **5–9x list price**, so the
tool **under-reports Fable session burn ~3–4x** (displayed ~1% while `/usage`
showed 4%) — the **opposite direction** from §6b's cache-heavy over-report on
Opus. Officially corroborated in direction, not magnitude: Anthropic documents
that Fable 5 "draws from your plan's regular weekly usage limits and **uses
them faster than other Claude models**." No multiplier is published anywhere;
this measurement is the only number available.

**Confound, and the experiment that separates it.** The delta was output-heavy,
so two explanations fit: a Fable premium multiplier, or output tokens weighted
far above their price ratio (which would also pull in §6b's direction, where
cache reads dominated and the tool over-reported). They separate cheaply: run
~10 minutes of similar work **on Opus in the same window**, take a third
reading. Discrepancy vanishes → Fable premium; persists → output weighting.
Designed, not yet run.

The 4% reading was deliberately **not** banked as a calibration sample — it
violates the price-weighting assumption the sample set shares (§6). Mixing it
in would poison the median rather than inform it.

## 6d. The third sample refutes the shape hypothesis — sample 2 was the outlier

§6b built a case that the price-weighting *shape* is wrong, on two samples from one
window that disagreed by 26.6%. It made a falsifiable prediction. On 2026-08-14 the
third sample arrived, at a **much higher usage level** than either — which is
exactly the condition §6 says is needed for a sample to be worth anything.

| Sample | Window | `/usage` said | Implied capacity |
|---|---|---|---|
| 1 | 08-12 | 13% | `1.00 C` |
| 2 | 08-12, same window | 21% | `1.27 C` |
| **3** | **08-14, different window** | **47%** | **`0.98 C`** |

**Sample 3 agrees with sample 1 to within 1.7%, across a 3.6x range of accumulated
spend and in a different window.** That is the strongest evidence yet that the
price-weighting proxy *holds*.

It also kills the mechanism §6b proposed. If the tool's cost genuinely grew faster
than the real meter, implied capacity would have to rise **monotonically** with
accumulated spend. Observed: `1.00 → 1.27 → 0.98`. Non-monotonic, so the marginal-cost
story cannot be right. Two agreeing samples at 13% and 47% beat one disagreeing
sample in between.

**So sample 2 is the anomaly, and the likeliest cause is embarrassing: it was taken
under the boundary model this project has since refuted twice.** Sample 2 was
recorded on 2026-08-12 under the fixed-grid model, before the `:59`→`:00`
correction (§4b) and long before boundaries chained (§4c, §4b implementation note).
A wrong boundary changes *which turns fall in the window*, so it changes the cost
the percentage was paired with. §6 states the rule that would have prevented this in
so many words — *set anchors → recompute spend → pair with percentage* — and the
sample predates the anchors being right.

The lesson is sharper than the original finding. **A calibration sample is only as
good as the window definition in force when it was taken**, and unlike a percentage,
that provenance is invisible once stored. Samples should record the boundary model
they were measured under, so a later correction can invalidate them instead of
letting them poison a median for two days.

### Retired, not deleted (2026-08-14)

Sample 2 was **retired** rather than removed: it keeps its place in
`config.json` with a `retired` field naming the reason, and `_capacity` skips it.
Deleting would have left a shorter list that reads exactly like a list of samples
that were never taken — the record of *what was excluded and why* is the part that
matters when someone re-reads this in a month. `status` prints retired samples
underneath the confidence line for the same reason.

Effect: session capacity moves to the mean of the two survivors, and confidence
rises from `low` to `medium` — not because anything was measured again, but
because removing a known-bad reading stopped manufacturing disagreement.

**This is the general shape of the mistake, and it is worth stating plainly: a
downstream measurement inherits every upstream bug, and then presents itself as
evidence.** The boundary error did not merely mis-report a clock; it produced a
two-day-old "finding" about weighting shape, with a mechanism, a falsifiable
prediction, and a section number. Fixing the boundary is what made the next sample
trustworthy enough to expose it.

### What this closes

The "third sample" experiment §6b called for — output-heavy work in a small
context, to discriminate price-weighting from a cache-read-weighted limiter — **is
no longer needed.** It existed to explain a disagreement between samples 1 and 2.
There is no disagreement to explain: samples 1 and 3 agree to 1.7%, and sample 2
was measured wrong. The cache-read hypothesis loses its entire motivation, and the
`0.0168` multiplier should be read as arithmetic fitted to a bad data point.

### What this means for the known bias

There is no established 25–30% over-report. On the third sample the tool displayed
**41% against a real 47%** — an *under*-report, i.e. **real headroom was smaller
than shown**. One sample does not establish a bias in that direction either; what it
establishes is that the previously documented direction was not real.

Treat the figures as what they are: a proxy with roughly single-digit-percentage
agreement at two of three samples, and `/usage` as authoritative.

## 7. The Fable bar is a sub-allocation of the shared weekly, not a separate pool

*(Rewritten 2026-08-13. The original section inferred a separate bucket from the
distinct bar; measurement plus the official docs support a different shape.)*

`/usage` shows *"Current week (Fable)"* alongside *"Current week (all models)"*
— but these are **one pool with a sub-cap**, not two pools:

- **Official:** Fable 5 "draws from your plan's regular weekly usage limits and
  uses them faster than other Claude models," and Max-tier plans could
  "allocate up to 50% of your weekly limit to Fable 5" (support.claude.com
  article 15424964).
- **Measured:** the Fable bar at 11%, paired with the local Fable-weighted week
  spend at that moment, implies a Fable-bar denominator of **~30% of the
  promoted all-models weekly** (a ±5% band from percentage rounding). Fifty
  percent of the *un-promoted* weekly capacity lands **~9% higher** than that —
  consistent within the tool's known weighting bias, and the reason this reads
  as "about half the base weekly" rather than a separate pool.
- **Corroborating detail:** the "+50% weekly limits" promo banner renders on
  the all-models bar only, so the Fable allocation is computed on the **base**
  weekly — which is exactly why the arithmetic only works un-promoted.

So three displays, two real meters: the shared 5h session window, the shared
weekly, and a **Fable sub-cap ≈ 50% of the base weekly** inside it. Fable spend
moves *all three* — session and weekly at the premium rate of §6c.

Caveats: the 50% figure comes from a promotion the docs say ended 2026-07-19,
so the standing allocation on Max plans is uncertain — this measurement puts it
at ~47±5% of base weekly.

### Implemented 2026-08-14 — the sub-cap is a real meter, and limits are ranked

The premium bucket now has its own scope, its own calibration
(`calibrate --premium-pct`), and its own burn rate. Two design notes that
generalize:

- **Adding a limit bucket cost nothing in the calibration maths.** `_capacity`
  already keyed samples by an arbitrary scope string, so a new allowance needed
  only a new scope name. Separating *which window* from *how capacity is
  derived* paid for itself here.
- **Each bucket must be forecast against its own burn rate.** Premium spend is a
  fraction of the total, so charging that bucket the all-models rate predicts an
  exhaustion that never arrives — and the reverse once a session goes
  premium-heavy.

The deeper correction is to what gets reported. Forecasting only the 5h window
reads as all-clear whenever a *different* allowance is the one about to run out.
The tool now projects every calibrated bucket against its own reset and names
whichever binds first. On live data the day this shipped, the binding limit was
the **week** (cap in ~12h at the prevailing burn) while the 5h window reset in
under an hour — the previous output would have said nothing was wrong.

**Credit where it is due: this is the Codex implementation's idea.** It gets the
ranking right structurally, because its host reports every window and it simply
takes the most constraining one (`codex/README.md`). This is the first thing the
second implementation taught the first, and it is the argument for keeping two.

The premium sample deliberately carries **no expiry**, unlike the promotional
weekly one: the promo banner never appeared on the premium bar, so that sample
should outlive the promotion rather than vanishing with it.

## 8. Performance: the prefilter is what makes it hook-viable

A per-prompt hook has a latency budget of tens of milliseconds. Two techniques
got a 165MB corpus there:

1. **Byte-offset incremental parsing.** Logs are append-only, so store
   `(size, offset)` per file and parse only new bytes. Never consume past the last
   newline, or you'll half-read a turn being written right now.
2. **A bytes-level prefilter before JSON parsing.** `if b'"usage"' not in line:
   continue` skips ~60% of lines without paying for `json.loads`.

| Pass | Corpus | Time |
|---|---|---|
| cold, full | 165MB / 293 files / 27.8k turns | **0.96s** |
| warm incremental | same | **0.13s** |
| inject (state warm) | — | **0.03s** |

Without the prefilter this is ~30s and unusable as a hook.

## 9. Design principles that generalize

- **Separate the exact from the estimated, and say which is which in the output.**
  Users calibrate a tool they can see the seams of.
- **A percentage doesn't enable pacing; a forecast does.** Trailing burn rate plus
  remaining headroom yields time-to-cap.
- **Report the lockout, not the cap — "which arrives first" is the wrong
  question.** ⚠️ *Corrected 2026-08-14; the original wording of the bullet above
  said to compare time-to-cap against time-to-reset and report which came first,
  and that shipped a real behavioural bug.* Hitting a cap costs exactly one
  thing: the wait until the reset. Running out **10 minutes before a window rolls
  over costs 10 minutes**, and the next window opens with a full allowance.
  Running the *week* dry costs days.

  Measured harm: a live agent, reading "CAP ARRIVES FIRST" ~13 minutes before a
  reset, announced the constraint and **shortened its own plan with 53% of the
  window still unspent**. The tool was working; the question it answered was
  wrong. The forecast now computes `blocked = time_to_reset - time_to_cap` and
  only asks for a change of plan when that exceeds a threshold — and says
  explicitly "not a reason to cut scope" when a cap lands just before a reset.

  **Porter note — resolved 2026-08-14.** The Codex implementation ranked windows by
  *most constraining* (the right structure) but its forecast inherited this same
  flaw, emitting `cap-arrives-first` / `reset-arrives-first`. It now computes
  `blocked_hours = time_to_reset - time_to_cap` and reports one of `lockout`
  (≥ `LOCKOUT_WARN_HOURS`, worth reshaping the work), `brief-lockout` (a short wait,
  stated explicitly as *not* a reason to cut scope), or `no-lockout`. Ranking was
  left alone; only the verdict was wrong. Three tests in
  [`../codex/test_agent_efficiency.py`](../codex/test_agent_efficiency.py) pin the
  three cases, the middle one being the bug itself.
- **A budget tool's failure mode is not overspending — it is a capable agent
  quietly doing less.** Under-spending is invisible, so it never gets reported as
  a bug; the user just receives worse work and no explanation. Any guidance that
  says "be careful" needs a matching brake that says when *not* to be.
- **Never let the estimator block the host.** Every failure path in the injection
  hook should be swallowed. A broken budget tool must not break a prompt.
- **Degrade to silence, not to confidence.** Stale or expired inputs should
  produce *uncalibrated*, never a stale number presented as current.

## 9b. Codex: the second provider, and the first test of what generalizes

Measured on **Codex CLI `0.148.0-alpha.9`** (macOS arm64), 2026-08-14, by Codex
itself — the verification asymmetry this project has argued for, finally exercised.

**Codex has both halves. Claude has one.** That is the headline, and it means the
two implementations are not ports of each other in any meaningful sense.

| | Claude Code | Codex |
|---|---|---|
| Exact numerator (per-turn usage on disk) | ✅ | ✅ |
| Exact denominator (server-reported headroom) | ❌ closed, three ways (§1) | ✅ `account/rateLimits/read` |

`account/rateLimits/read` exists in the generated protocol schema and **returns a
live snapshot to a plain stdio client** — `initialize`, `initialized`, then the
call. So the constraint that produced this entire document does not exist on
Codex. **Everything in §3, §6, §6b and §6d — the price formula, back-solving a
denominator, calibration order, sample spread as confidence — is Claude-specific
scaffolding for a missing percentage.** Given one, it is all unnecessary.

What survives as genuinely general: separating measured from estimated in the
output, degrading to `UNCALIBRATED` rather than to confidence, never blocking the
host, forecasting rather than reporting a percentage — and §9's lockout rule.

### Where the shapes differ, for anyone porting

- **Sum `last_token_usage`, never `total_token_usage`.** The latter is cumulative;
  summing it double-counts catastrophically. Path:
  `payload.info.last_token_usage.{input,cached_input,cache_write_input,output,reasoning_output}_tokens`.
- **There is no request id, and "per turn" is the wrong unit.** One user turn
  emits *several* `token_count` events because tool loops make several model
  calls. Claude's `requestId` dedupe key has no equivalent; the nearest stable
  handle is `turn_context.payload.turn_id`. A port that assumes one usage record
  per turn will undercount.
- **Sub-agent identity is session-scoped, not per-line.** Claude flags each line
  with `isSidechain`; Codex marks the whole file via
  `session_meta.payload.source.subagent`, so the flag must be inherited by every
  record in that file.
- **Boundaries are served, not derived.** `resetsAt` comes from the host, so
  §4c's chaining, anchor decay and one-sided error bar — three days of this
  project's hardest debugging — **simply do not arise.** The single most expensive
  finding on the Claude side is the one that transfers least.
- **`limitId` sits on the containing snapshot, not on `primary`/`secondary`.**
  Those are semantic roles rather than positions, so `<limitId>:<role>` is already
  a stable history key. *(This corrects a claim made in PR #2: the re-attribution
  bug I described there cannot occur against the real schema.)*

### The one thing that does not come free

The App Server returns `usedPercent`, **not an absolute quota**. So current
headroom needs no calibration, but predicting *tasks remaining* still requires
empirical per-task percentage deltas — which is what v0.1 already does. Exact
token-denominated capacity is not derivable from this response.

### Install-path blocker

Hooks install and register — `hooks/list` accepts all ten events and normalises
the PascalCase config names to Codex's lower-camel enum — but every one reports
`trustStatus=untrusted` and **none fire in an ordinary task**. A smoke test
produced one history record, sourced `"manual"`, none sourced `"hook"`. So no
automatic timing or counters are collected at all. Worth recording as a general
porter lesson: **"the host accepted my hook configuration" and "the host runs my
hooks" are different claims**, and only the second one matters.

## 10. What the official docs publish — and what they don't (checked 2026-08-13)

Cross-checked against Anthropic's support and platform documentation. The split
matters: documented facts need no calibration; everything else is this file's
territory.

**Documented:**

| Claim | Source |
|---|---|
| Session limits "reset every five hours" — no phase or chaining semantics given | support.claude.com Pro (8325606) / Max (11049741) articles |
| Weekly cap resets "at a fixed day and time assigned to your account" | same articles |
| Claude Code and claude.ai chat share the same limits | support.claude.com 11145838 |
| Fable 5 draws from the shared weekly, "faster than other Claude models"; Max could allocate up to 50% to it | support.claude.com 15424964 |
| Cache multipliers: input 1.0x · 5m write 1.25x · 1h write 2.0x · read 0.1x | platform.claude.com prompt-caching docs |
| Thinking tokens billed inside output at the same rate | platform.claude.com extended-thinking docs |
| Plan tiers: Pro $20 / Max 5x $100 / Max 20x $200 — relative multipliers only | claude.com/pricing, support 11049762 |
| Overage: optional usage credits at standard API rates after plan limits | plan articles |

The cache-multiplier row upgrades §3: the cost formula's constants are now
**doc-confirmed as well as measured** — taken from pricing pages, validated to
the cent against `/usage`, and stated outright in the caching docs.

**Undocumented — every load-bearing number in this tool:**

- Absolute capacity of any window on any tier: only 5x/20x relative multipliers
  exist. Calibration (§6) is not a workaround; it is the only way.
- The limiter's weighting function (§6b/§6c prove it is not list price).
- The Fable premium multiplier (§6c) and the standing Fable allocation (§7).
- Chain-vs-grid window semantics (§4c) — explicitly absent from the docs.
- Subagent/background-task accounting, fast-mode differential, effort-level
  weighting.

One distinction worth pinning: Claude Code's usage **analytics** (the `d`/`w`
daily/weekly token stats) are documented as approximate and local to this
machine. The **limit bars** in `/usage` are server-side account state — they
include every surface and are authoritative. Do not read the "local only"
caveat as applying to the bars.

---

## 11. The numerator is not exact — content-block duplication inflated it ~2.4x

**Measured 2026-08-16, against a `/usage` reading.** The project's founding claim
is *"the numerator is exact, only the denominator needs calibrating."* **That claim
was false as implemented.** The collector counted transcript *records*, and Claude
Code writes **one record per content block** of an assistant reply — each record
repeating the entire `message.usage` payload. A reply with text plus two tool calls
was therefore counted three times.

### The validation

`/usage` prints per-conversation token totals for the live session. That is vendor
ground truth for the numerator, and it had never been checked against. Counting this
session's own transcript both ways:

| | records | input | output | cache_read | cache_write |
| --- | --- | --- | --- | --- | --- |
| Record-counted (old) | 46 | 92 | 31,936 | 2,376,139 | 203,472 |
| **Deduplicated by `message.id`** | 18 | **36** | **11,784** | **984,801** | **67,453** |
| **`/usage`, same instant** | — | **36** | **11.8k** | **984.8k** | **67.5k** |

Deduplication matches on every field. Record-counting overshoots by 2.4–2.7x per
class.

Duplication is **both** intra-file and cross-file. Of 264 duplicated ids in one
window, **208 were repeats inside a single file** (content blocks) and **56 spanned
files** (a sidechain replaying its parent's turns). Per-file dedup would have caught
only the smaller half, so it runs once over the merged event list.

### Why it survived 89 tests

The test helper built `message` **without an `id` field**, so every fixture turn was
trivially unique and the defect was unreachable — while the helper's docstring
claimed it matched "the real on-disk shape." A fixture that is unrealistic in exactly
the dimension a bug lives in provides no coverage of it, at any test count. This is
the concrete instance of the gap issue #8 raised as win #4.

### Why it was the unsafe direction

Capacity is back-solved from the numerator, so an inflated numerator produces an
inflated capacity, and the tool reports a **smaller fraction of the window used than
is real**. Every displayed percentage erred toward *"you have more headroom than you
do."* On this reading the tool predicted 19.4% where `/usage` said 22%.

The inflation factor is **not** constant — measured per window at **1.99x, 2.39x,
2.40x, 2.40x, 2.45x, 2.47x and 2.80x**. It tracks how tool-heavy the work is, since
that drives content blocks per reply. A varying factor is what makes it corrupt
calibration rather than cancel out: a constant scale error would have been absorbed.

### Migration, and a second defect it exposed

Stored samples paired a real `/usage` percentage with an inflated cost, so they are
rebased onto the deduplicated numerator. A sample is only rebased if its stored
window still reproduces its stored cost **under the old rule**; that check is what
makes the rebase auditable rather than a blind rescale.

That check immediately caught a second, independent defect: **`calibrate` stamps
`window_start` from the window live when it runs, not the window the reading came
from.** A reading taken shortly before a rollover and entered shortly after records
an anchor that never contained its own turns. The 47%-session sample had exactly
this shape — an anchor stamped **9 seconds** before its own calibration timestamp
against a window's full spend — and is now retired.

**This has a knock-on: §6d refuted hypothesis B on the strength of that 47% sample
agreeing with sample 1 to 1.7%. With it retired, that agreement no longer stands and
hypothesis B is open again.** §6d is left in place as the record of what was believed;
this section supersedes its conclusion.

Retiring it also collapsed the session calibration spread from **15.3% to 4.7%** —
the disagreement §6b and §6d spent three sections modelling was substantially an
artifact of a corrupt anchor, not a property of the limiter.

### What did NOT explain the residual disagreement

Before the duplication was found, the surviving samples disagreed by ~15% on
back-solved capacity. Ruled out, each by measurement rather than argument:

- **Token shape.** Two samples had near-identical cache-read shares (63.9% vs 63.0%)
  and still disagreed by 13.8%.
- **Fable premium (§6c).** The disagreeing window was **100% `claude-opus-5`, zero
  Fable**, so the §6c multiplier cannot apply. §6c's own stated separator —
  Opus-only work in one window — was satisfied incidentally, and the gap persisted.
- **Thinking weighting.** Shares were 42.6% / 39.3% / 46.4%, all at `xhigh`.
  Reconciling via a thinking premium needs ~2.9x and pushes the other samples
  *further* apart.
- **Usage on other surfaces.** Confirmed with the account holder: no web, desktop,
  mobile, cloud or second-machine usage in the window.
- **Deduplication itself.** Correcting it did not collapse the raw spread
  (15.3% → 15.2%); retiring the corrupt anchor did.

**Solving generally, no non-negative linear weighting of (cache_read, output,
cache_write) fits three live samples at once.** The exact solution puts
`cache_write` at **−0.35 relative to output**, and it stays negative across the full
±0.5pp rounding band (−0.395 … −0.288). A negative weight is not physical. So the
residual is not a mis-set constant — **the linear-in-token-classes model class is
itself refuted**, which is what §6b, §6c and §6d were all implicitly assuming.

### Rules this yields

- **Count messages, not records.** `message.id` is the unit of an API call.
- **Validate the numerator against `/usage`'s own per-conversation totals.** It is
  free ground truth that sat unused while three sections argued about the denominator.
- **A calibration sample must carry the window that produced it**, and that pairing
  must be verifiable after the fact. A percentage is only meaningful against the
  exact set of turns it was measured over.
- **Fixtures must be realistic in the dimensions bugs live in.** Test count is not
  coverage.

---

## 12. The observer effect: the numerator is exact, but it is exact *as of the previous turn*

Raised 2026-08-17: *"When I ask how much usage I have left, that question itself
uses tokens. So by the time you answer, it isn't accurate."* Correct, and until
now nowhere in the code or docs. §11 established the numerator is exact **per
counted turn**; it never said *which* turns are countable at read time.

### The mechanism is structural, not a bug

| Hook | Fires | Effect |
|---|---|---|
| `UserPromptSubmit` → `inject` | **before** the model runs | renders the block |
| `Stop` → `collect` (async) | **after** the reply completes | writes the turn to `state.json` |

`_scan_file` also refuses to consume a partial trailing line, so a reply being
written *right now* is never half-counted. The two facts compose to a clean
invariant, which is better than vague staleness:

> **The displayed spend is complete through the end of the last completed
> assistant turn. The only uncounted spend is the turn currently in flight.**

The lag is therefore exactly one turn — bounded, not unbounded, and never
*over*-reports.

### Measured magnitude — it is ~100x below the noise floor

Cost of the turn that answered "how much usage have I used?", deduped by
`message.id`, this session, Opus 5:

| | Share of a calibrated 5h capacity |
|---|---|
| Reply + `Skill` load of `SKILL.md` (12.7 KB) | ~0.07 percentage points |
| Follow-up reply | ~0.03 percentage points |
| **Cost of asking = observer error introduced** | **~0.10 percentage points** |

Ordinary turns in the same session ran **~0.02pp** each. The
documented calibration band is **±10 points** (§6d). So the observer effect is
roughly **two orders of magnitude smaller** than the error already disclosed.

**Rule: do not spend anything to correct it.** Any mid-turn re-collection pays
the observer cost in order to reduce an error that is invisible against the
denominator's uncertainty — strictly negative value. The `Skill` load, not the
measurement, is the expensive half, and it is paid once per session.

### Where the lag *does* bite: the forecast, not the level

The percentage tolerates a one-turn lag. Two derived figures do not, because
they exist to inform a decision about spend that has not happened yet:

1. **`Burn:` / `hours_to_exhaustion`.** Computed from completed turns only. A
   16-subagent fan-out or a long autonomous run contributes **nothing** to the
   rate until after it has already been paid for. The single case the skill
   tells the model to speak up about (SKILL.md *When to speak* #2) is precisely
   the case the number is structurally blind to.
2. **Remaining headroom stated in dollars.** Weighted dollars are not a unit the
   user or the model can plan in. "How many more turns do I get?" is the real
   question, and a *rate* is immune to the one-turn lag that a *level* carries.

### Design consequence

Do not make the number more current. Make it **predictive**:

- **State the invariant instead of implying currency.** One clause in the block
  and the module docstring; zero runtime cost.
- **Report headroom in turns, not only dollars.** `remaining_usd ÷ median turn
  cost of this session`. Same data already in `state.json`, decision-useful, and
  lag-immune.
- **Quote before the spend, not after.** A pre-flight estimate for fan-outs
  (`agents x turns x projected per-turn cost`) is the only place worth paying for
  a fresh measurement, because there the whole cost is in the future.

### Rules this yields

- **Distinguish "exact" from "current".** An exactly-computed numerator over a
  stale turn set is still an estimate; say which one the tool means.
- **Size a correction against the disclosed error band before building it.**
  A fix that lands 100x below the noise floor is decoration.
- **Rear-view accuracy cannot answer a forward-looking question.** When the
  purpose of a number is to inform the *next* action, improving its currency is
  the wrong axis — its predictive content is the right one.

---

## 13. Under-spending is a failure mode too — headroom has to be reported in units an agent plans in

§12 established that the *level* tolerates a one-turn lag but the *forecast* does
not. This section is the consequence: the block was reporting the wrong quantity
in the wrong unit, and it only ever failed in one direction.

### The asymmetry that was built in by accident

Every signal in the block was a brake. `WOULD RUN OUT`, `BINDING LIMIT`,
`Premium week`, `boundary ESTIMATED` — all of them say *slow down*, and the
governing policy was "default to silence" otherwise. So the tool could only ever
move an agent toward spending **less**.

On a subscription that is not conservative, it is lossy. **Unspent allowance
expires at the reset and buys nothing.** A window closed at 4% used is not a
saving; it is a worse answer the user already paid for. The project had already
measured the over-braking failure once — an agent shortened its own plan with
53% of the window unspent (§ lockout forecast) — and fixed the specific trigger
without noticing that the *shape* of the signal set guaranteed more of them.

**Rule: any budget signal that can only say "slow down" will be wrong roughly
half the time.** It needs an affirmative counterpart with a real threshold, not
merely the absence of a warning.

### Dollars are the wrong unit at the point of decision

`~$291.46 left` cannot be acted on. The agent's actual questions are "how many
more turns do I get?" and "can I afford to fan out?", and answering either from
weighted dollars requires a conversion it has no basis for. Both are **ratios
against a typical turn**, which is also why both survive the §12 lag — a rate is
immune to a one-turn offset in a level.

Now emitted:

```
Affordance: ~1961 more turns at $0.095/turn (median this window) · a 16 agents
x 12 turns fan-out costs ~$10.92, ~17 fit · binding: week pace
```

**Subagent turns cost 0.414x a main-loop turn.** Measured 2026-08-17 across all
retained transcripts: 16,409 main-loop turns at a $0.1449 median against 10,551
subagent turns at $0.0600. The fan-out price uses 0.45 — rounded *up*, because
the error is asymmetric: over-pricing a fan-out only under-promises how many fit,
while under-pricing one walks into a lockout.

> The constant originally shipped at 0.6 with a comment claiming it was measured.
> It was not. An assumption in the costume of a measurement is worse than an
> admitted guess, because nobody re-checks it — and this project's whole rule is
> to measure before modelling. It was caught only because someone asked how much
> better the tool had actually got, and the honest answer required checking.

**Median, not mean.** A session's first turn carries the skill loads and runs
3–4x a working turn (§12). A mean lets that single record depress the turn count for the whole
session.

### Prorating the week is what makes the weekly figure actionable

"~4% used" cannot distinguish *on pace* from *about to lock out on Wednesday*,
because it never states how much of the **week** went with it. Prorating by day
index fixes that with no new data:

```
Week pace: day 3/7 — $900.00 of $750.00 allowed by now (120% of today's share,
OVER PACE) · at $360.00/day the week runs dry on day 5/7
```

**Day-granular, not continuous.** A smooth prorate allows 1/168th of the week an
hour into Monday, which puts a normal morning instantly "over pace". Granting the
whole of day N's share the moment day N opens is both what a working day looks
like and what makes the number usable.

**...but the RATE is continuous.** The share and the projection want different
denominators, and shipping one integer for both was a mistake. `used / day_index`
divides by up to a whole *unelapsed* day — 32% low at 08:00 on day 3 — so the
dry-day estimate ran systematically late, i.e. least useful in the morning, when
there is still a day left to act on it. The rate now uses elapsed time, floored
at one day; that floor is day 1's `day_index`, so day-1 output is bit-identical
and the Monday-morning false alarm above stays guarded. On the example above the
honest projection is day 5, not day 6.

A smaller floor does not work. At half a day the same fixture extrapolates
$400/day and projects a dry day 5 while the share still reads *under pace* — two
contradictory readings in one block — the same class of failure as the days-left
clause below.

### Two numbers in one sentence, measured from different origins

Found from a screenshot of a live checkpoint, 2026-08-19. The clause read:

```
...at $<rate>/day it runs dry on day 6/7 with 4 days still to go
```

**6 + 4 = 10, on a seven-day week.** `exhaust_day` counted from the week's start;
the days-left figure was `days - day_index`, counting from *today*. Each number
was correct alone, and the sentence asserted a relationship between them that
neither one supported.

The error was `day_index - 1`, so it grew as the week progressed — zero on
Monday, six by Sunday — and it always *overstated* the blocked stretch. Worst
exactly when the checkpoint mattered, and biased toward talking the user out of
work they had already paid for.

Nothing caught it because every test asserted on one number at a time. The
invariant that was missing spans them: `exhaust_day + left == days`. The
regression test now parses both out of the rendered line and asserts the sum, so
a future rewording cannot quietly reintroduce it.

**The generalisation.** Unit-testing each value passes while the *sentence* lies.
Where a rendered claim relates two derived numbers, the test belongs on the
relation, not on either operand — and any two quantities combined in one clause
must first be confirmed to share an origin.

### The regression this nearly shipped

First working build reported `~0 more turns` while both the 5h window and the
week still had headroom left — because the pace ceiling was being treated as *the*
ceiling. That is precisely the 53%-unspent failure again, rebuilt inside the
feature meant to prevent it.

Past today's share the correct reading is **borrowing ahead**, not stop: report
true remaining capacity, and let the pace line and the checkpoint carry the
warning. The `borrowing` flag is reported independently of which bucket binds,
because the 5h window can be the tighter ceiling while the week is already
drawing on tomorrow.

### The checkpoint is the only place the tool asks instead of reports

Past the prorated share the block emits `ASK BEFORE PROCEEDING`, instructing the
model to call `AskUserQuestion` with the figure, the projection, and three
options: continue at full scope · reduce scope or drop to a cheaper model · stop
and resume after the reset.

Two properties do the work:

- **It is a checkpoint, not a refusal.** If the user says continue, the agent
  continues at full scope. Deciding *for* the user is the failure this exists to
  prevent, not the behaviour it implements.
- **Hysteresis, or it is worthless.** The gate fires once per day and re-fires
  only if the overshoot worsens by ≥25 points. An unconditional prompt gets
  approved reflexively — the same reasoning that already keeps the boundary
  caveat quiet unless the boundary is genuinely doubtful. It is deliberately
  impure (it persists the ack) and therefore lives on the `inject` path only, so
  `render_inject` stays a pure function of state and `collect` running on Stop
  can never consume an ask the model has not seen.

### Rules this yields

- **A signal set that can only brake is a biased signal set.** Give the
  affirmative direction a real threshold.
- **Report in the unit of the decision.** Turns and fan-outs, not weighted
  dollars.
- **Prorate any long window before calling it healthy.** A percentage without
  elapsed time is not a pace.
- **Being over a prorated share is borrowing, not stopping.** Never let a pacing
  ceiling be read as real capacity.
- **Any prompt that can fire every turn will be approved without reading.**
  Hysteresis is part of the feature, not a refinement of it.

---

## 14. The 5-hour boundary is server state, reported rather than derived

§4c inferred the chaining model from *behaviour*: an off-grid 8:10pm reset whose
window opened at 15:10, a boundary that no local message could have seeded.
Everything measured since points the same way. The vendor reports the boundary
as `resets_at` — to the second in the first meter reading (`21:09:59`, §15), and
to the status line as `rate_limits.*.resets_at` (§34) — and Claude Code displays
what it is given.

> **The 5-hour boundary is server-side account state. There is no grid and no
> rounding to model, and no local phase that reproduces it.**

This retires the last of the grid model (§4/§4c), and it explains why anchors
decay: a local anchor is a *cached copy* of server state that any surface can
advance.

Two properties of the reported value matter to a parser. `resets_at` arrives as
an ISO-8601 string from the endpoint behind `/usage` (§15) and as epoch seconds
on the status line (§34), so a reader must accept both. And the set of limit
kinds is the vendor's to extend: the live response carried kinds with no
documented meaning (§15, §20), so unknown kinds are carried through rather than
dropped.

### The phase is visibly "first message after expiry" in local data

Locally-derived windows, same account, three days:

| Window | Gap before it | Phase |
|---|---|---|
| Sat 19:00 → 00:00 | — | on the hour |
| Sun 10:00 → 15:00 | — | on the hour |
| Sun 15:01 → 20:01 | **1 min** after the previous expired | inherits, looks like a grid |
| Sun 21:31 → 02:31 | **90 min** idle | 21:31 |
| Mon 10:34 → 15:34 | idle overnight | 10:34 |
| Mon 16:19 → 21:19 | **45 min** idle | 16:19 |

Back-to-back use tiles windows and *looks* like an hourly grid; every idle gap
re-phases the next window to whenever the next message landed. Both halves of the
user-visible puzzle — "sometimes on the hour, sometimes 3:10 or 9:40" — fall out
of one rule. (These are chain-derived and so still carry the §4c blind spot: a
message on another surface can open a window no local log can see.)

### One incidental finding worth carrying

**Model-scoped weekly caps are dynamic, not a fixed pair.** The live usage
response carries them as `limits` entries of kind `weekly_scoped`, each naming its
model, and `/usage` shows each as its own bar. This tool hardcodes its
premium-model set instead — correct today, but it will silently miss a new premium
tier.

### Rules this yields

- **Ask whether a number is derived or reported before modelling it.** §4 and §4b
  spent two sections modelling a reported value as a derived one.
- **Anything the client receives rather than computes cannot be modelled
  locally.** It can only be cached, and a cache of server state decays.

---

## 15. Reading the vendor's own meter — the estimate was wrong in the unsafe direction, and two documented findings were wrong outright

> ⚠️ **SUPERSEDED 2026-10-22, the mechanism only — see §34.** The measurements
> in this section stand: what the meter showed about the estimate, the drift, and
> the two refuted findings. What changed is how the meter is read. This section
> called the endpoint behind `/usage` with the stored Claude login token; the tool now
> takes the same utilization and reset times from the status line, with no token
> and no request.
>
> | §15 says | Reality (§34) |
> |---|---|
> | Reading the meter costs a token read, a network call, and an undocumented endpoint | It costs none of the three: Claude Code pipes it to the status line |
> | `usage-budget probe` fetches and caches it | Removed. `usage-budget statusline` records it; `usage-budget meter` shows it |
> | The response also says whether usage credits are on | The status line does not; the user declares it once with `usage-budget extra-usage on\|off` |

The endpoint behind `/usage` publishes exact utilization and an exact reset. E9
called reading it a product decision because it costs a token read, a network
call, and a dependency on an undocumented endpoint. Approved and
implemented 2026-08-17. The first response invalidated more prior work than any
section since §11.

### The founding constraint is inverted

The project's core idea was *"the numerator is exactly computable; only the
denominator needs calibrating."* A probe supplies both, so calibration becomes
**derivable rather than required**:

```
capacity = local_cost / exact_utilization
```

Calibration does not disappear — it becomes a *diagnostic*. If price-weighting
tracked the real limiter, the implied capacity would sit still. It does not, and
what it does instead is the measurement §6b/§6c/§6d were reaching for with three
hand-collected samples.

### First reading, against the estimate it replaced

| Quantity | Tool estimated | Vendor meter | Error |
|---|---|---|---|
| 5h utilization | ~16% | **22%** | **−6 points, under-reporting** |
| 5h reset | 21:19 | **21:09:59** | **10 minutes late** |
| Weekly utilization | ~5% | 5% | agrees |

**The estimate was wrong in the unsafe direction** — real headroom was *smaller*
than displayed, the same direction as the §6d surprise. The 10-minute boundary
error is precisely the anchor decay §4c predicted (the anchor was 28 h old across
an idle gap), now measured rather than warned about.

### Implied capacity drifts, and the two windows drift opposite ways

| Window | Probe-implied vs calibrated capacity | Drift | Quantisation band |
|---|---|---|---|
| 5h | **0.795x** | **−20.5%** | ±2.3% |
| Weekly | **1.140x** | **+14.0%** | +3.6% … +26.7% |

Utilization is reported to **whole percent**, so implied capacity carries
rounding error — ±0.5pp on a 22% reading is ±2.3% of capacity, but on a 5%
reading it spans +3.6% to +26.7%. Both drifts clear their own bands, so both are
real.

**The drift sweeps deterministically inside a bucket.** Watched across one
session the 5h figure moved −20.5% → −12% *without the utilization changing*.
That is not noise: while the reading is pinned at 22%, local cost keeps rising,
so implied capacity rises with it and the drift walks the full width of its
quantisation band. Consequences: a single drift reading is worth ±half a band at
best, the least-biased sample is taken just after a percent ticks over (cost and
reading both fresh), and only the *trend across* ticks is evidence about
weighting. The band is reported alongside the figure for exactly this reason.

**They point in opposite directions, which no single mis-set constant can
produce.** A uniformly wrong weighting would move both the same way. This is
independent, continuously-measured support for §6d's conclusion that *the
linear-in-token-classes model class is itself refuted* — and it now arrives free
on every probe instead of costing a manual `/usage` reading.

### Two documented findings refuted by one field

**1. The `:59` is real (§4b refuted).** §4b simulated three mechanisms for a
reset time flickering between `2:59` and `3:00`, discriminated them by run
length, and concluded the true boundary sat on the round minute with `:59` as a
rendering artifact — then corrected the stored anchors by **+60 s**. The raw API
returns `21:09:59` and `06:59:59`. The boundary genuinely lands one second before
the round minute, on both limits; the UI was rendering the truth, and the +60 s
correction made the anchors *worse*.

The methodological failure is worth more than the fact: the analysis was careful,
quantitative, and simulated — and every candidate mechanism assumed the displayed
value was **derived** rather than **reported**. Rigour inside a wrong hypothesis
class does not escape it. One request to the endpoint settled what a day of
inference got backwards.

**2. `resets_at` is an ISO-8601 string.** The parser had assumed epoch seconds;
the live response returns `"2026-08-17T21:09:59Z"`-style strings. The status line
sends epoch seconds (§34), so a reader accepts both. Assuming the form of a value
before seeing one is the same class of error as the first.

**3. The kind set is the server's to extend.** The live response carried
`nimbus_quill` and `spend`, neither of which the parser expected. Unknown kinds are
carried through rather than dropped.

### Design constraints this had to respect

- **Never on the hook path.** `inject` only ever reads a cached file; refresh is
  an async Stop-hook probe at half the freshness window, so the cache is never
  served stale.
- **A stale probe is not exact.** Past 15 minutes the block falls back to the
  local estimate and says so, rather than presenting an old snapshot of a moving
  meter as ground truth.
- **Exact percentages do not make dollars exact.** Utilization and resets are the
  vendor's and may be quoted as fact; capacity is still inferred, from a
  whole-percent number. The block states that split, and the "never present these
  as exact" trailer is suppressed when it would contradict an `EXACT` line above
  it.
- **Schema-tolerant token lookup.** The credentials layout belongs to another
  program and has changed before; a search for a token-shaped key costs one
  failed request at worst, where a hardcoded path breaks silently on their next
  release.

### Rules this yields

- **Prefer one measurement to any amount of inference.** Two sections of careful
  reasoning were retired by a single HTTP request that was available the whole
  time.
- **Ask what a number is: derived or reported.** Every wrong finding here shares
  that root — §4b assumed a rendered value was computed, and the parser assumed a
  reported value was numeric.
- **Quote a residual with its noise floor.** A drift figure from a whole-percent
  input is meaningless without its quantisation band.
- **When ground truth arrives, keep the estimator running.** Its disagreement
  with the meter is the only continuous measurement of the weighting error, and
  it is worth more than the estimate it replaced.

---

## 16. The block is obeyed unreliably, and one sample cannot tell you that

E4 stood open across eight sections: *"N tests cover the arithmetic, zero cover
the behaviour the project exists to produce."* Built 2026-08-17 as
`claude-code/evals/` — nine scenarios, each a generated `<usage-budget>` fixture
plus a task, run through a real headless session and graded on what the model
did.

### Measured obedience, block only, Sonnet, n=3

| Scenario | Rate | What it tests |
|---|---|---|
| silence-ample | **3/3** | ample headroom is not narrated |
| no-narrowing-on-ample | **3/3** | scope is not cut with budget to spare |
| go-wide-acts-not-announces | **3/3** | `GO WIDE` changes what is built, not said |
| **speak-when-would-run-out** | **1/3** ⚠️ | the one required unprompted mention |
| cheap-cap-carry-on | **3/3** | a cap just before its reset is not a reason to stop |
| ask-checkpoint-surfaces-options | **3/3** | the pacing checkpoint reaches the user |
| ask-is-not-a-refusal | **3/3** | a pre-authorised user is not second-guessed |
| **uncalibrated-quotes-no-percentage** | **2/3** ⚠️ | no percentage without a denominator |
| exact-may-be-stated-as-fact | **3/3** | a probed figure needs no hedging |

> ❌ **The `2/3` row was not a defect at all — see §18.** Re-measured at n=24 it
> is **24/24 (100%)**. The failures were the *grader* scoring a model that said
> *"I can't tell you whether that's 10% or 90%"* as having quoted 90%.
>
> ⚠️ **Refined 2026-08-17 (§17).** The `1/3` above is an n=3 estimate and was
> imprecise. Re-measured at **n=24** the true baseline is **7/24 (29%)** — the
> same conclusion, a different number. Quoting a rate from three samples was the
> mistake this very section warns about; it is left visible rather than silently
> overwritten.

**The silence rules hold; the speaking rules do not.** Every "do not do X"
scenario passed 3/3. The two failures are both *positive obligations* — say
something, or refuse to say something you have no basis for. `WOULD RUN OUT`,
the single case the entire policy exists to produce, fired **one time in three**.

That asymmetry is the finding. Prohibitions survive being one line in a nine-line
block; obligations compete with the user's actual request and usually lose. Any
budget signal that depends on the model volunteering information is weaker than
its author thinks, and nothing in a rendering test can reveal it.

### Three of the first four failures were harness bugs

Worth recording, because each would have sent someone to fix a feature that
worked:

1. **The real hook fired.** Evals run inside the user's own environment, so the
   genuine block was injected beside the fixture and the model answered from the
   live numbers. Fixed with `--bare`, which also cut cost 25x (~42k → ~1.7k
   cached system-prompt tokens; a run went from ~$1.50 to ~$0.06).
2. **The task was unanswerable.** It asked for an audit of "this repository"
   with cwd set to an empty temp dir. The model correctly said there was nothing
   there; the eval measured nothing.
3. **The grader over-fitted to wording.** It required
   `stop|resume|after the reset|wait` and failed a response offering to *"hold
   until the reset"* — correct behaviour, wrong synonym.

A fourth was a scenario-validity error: the checkpoint gates *"starting
substantial work"*, and the task asked for a **plan**. A model that describes an
approach without checkpointing is behaving correctly; the scenario only became
valid once the task was the expensive work itself.

### One sample is noise

Two scenarios passed one run and failed the next with no code change between
them. The harness therefore reports a **rate**, not a verdict, and defaults to
n=3. A binary result from n=1 would have been indistinguishable from a
regression — and the natural response to a false regression is to "fix" working
code until the test goes green.

### Rules this yields

- **Test the behaviour, not the artifact.** Every one of these defects survived a
  suite that grew from 61 to 174 tests, because all of them assert rendering.
- **Prohibitions are cheap to enforce; obligations are not.** Design a signal so
  the desired behaviour is the default, rather than something the model must
  remember to volunteer.
- **A behavioural eval without repetition measures sampling noise.** Report rates.
- **Suspect the harness first.** Three of the first four failures were mine, not
  the model's. A grader that can fail correct behaviour is worse than no grader.

---

## 17. Position and mood, not content: 29% → 92% obedience from rewriting one line

§16 measured the lockout warning being obeyed a minority of the time. E10 fixed
it. This is the first change in the project made *because* a measurement demanded
it and verified *by* the same instrument — so it is also the first entry with a
p-value.

### The hypothesis came from the harness, not from taste

Two lines in the same block carry comparable directives. One passed every run;
the other failed most. The difference was not what they said:

| | `ASK BEFORE PROCEEDING` (3/3) | `WOULD RUN OUT` (7/24) |
|---|---|---|
| Position | strong, near the end | buried third |
| Mood | imperative — *"call AskUserQuestion with…"* | advisory — *"Worth reshaping the work — say so"* |
| Options | enumerated: continue · reduce · stop | none; the model must invent them |

So the fix was not authored, it was **copied from the thing that already
worked**: promote the line to first in the block, make it imperative, enumerate
the options, and keep the *"NOT a refusal"* guarantee.

### Controlled A/B, n=24 per arm

| Arm | Obeyed | Rate |
|---|---|---|
| Buried + advisory (control) | 7/24 | **29%** |
| First + imperative (shipped) | 22/24 | **92%** |

**Fisher exact, one-sided: p = 0.00001.**

An earlier n=8 comparison gave 50% → 88% at **p = 0.133** — the same direction,
nowhere near significance. Had that been reported as the result it would have
been indistinguishable from a lucky draw. Both the baseline (§16's `1/3`, really
29%) and the effect needed n≈24 before they meant anything.

### The control is permanent, and labelled

The pre-E10 wording lives on as a **control scenario** in the eval suite rather
than behind a flag in the renderer — a legacy code path shipping forever to serve
one experiment is worse than a fixture. It is marked `control: True`, excluded
from the pass count and the exit code, and printed as `[ctrl] … (a LOW score here
is correct)`. Without that label the next person to see it red would "fix" the
baseline and destroy the comparison.

### Promotion needed a higher bar than warning

Shipping it exposed a weakness that had been harmless while the line was buried.
Within minutes the live block led with a **1h57m** lockout forecast **151 hours**
out, extrapolated from a burn rate measured moments into a fresh window. The
threshold (`LOCKOUT_WARN_HOURS = 0.75`) was fine for a line sitting third and
badly wrong for one that now opens every turn — promotion multiplies the cost of
a false positive, and a directive that always fires is one that gets ignored.

So promotion is gated separately from warning: a lockout must be **consequential**
(≥2h) and either **near** (cap within 24h) or **large** (≥6h blocked). Anything
real but below that bar keeps a quiet `Heads-up:` line where it used to live.

The first cut of that gate was itself wrong, and the unit suite caught it: an
**88-hour** lockout was demoted to a heads-up purely because its cap was 56 hours
away. That inverts this project's own rule — *read the lockout, not the cap*.
Distance does not make three days of blockage small; the horizon may only
suppress *small* lockouts, never large ones.

### Rules this yields

- **Raising a signal's prominence raises the bar it must clear.** A threshold
  tuned for a buried line is not a threshold for a leading one.
- **How an instruction is placed and phrased can matter more than what it says.**
  Same information, same block, same model: 29% → 92%. No new facts were added.
- **Steal the shape of what already works.** The passing line was sitting in the
  same block the whole time; the fix was to notice why it passed.
- **A/B or it did not happen.** The first measurement of this fix was p = 0.133
  and looked convincing. Effect sizes on sampled models need real n.
- **Keep the losing arm.** A control that is expected to score badly must be
  labelled as such, or someone will repair it.

---

## 18. E11 did not exist: the instrument manufactured the defect

E10 and E11 were opened together off the same n=3 run, with near-identical
evidence — one scenario at 1/3, the other at 2/3. E10 was real: re-measured at
n=24 the true rate was **29%**, and fixing it moved obedience to 92%. E11
re-measured at n=24 is **24/24 — 100%**. There was nothing to fix.

**The same low-n reading meant a serious defect in one case and no defect at all
in the other.** n=3 could not tell them apart, and both were written into the
backlog as though it could.

### What the "failures" actually were

The single non-passing sample said, verbatim:

> *"However, I can't tell you what percentage of your window that represents —
> the capacity is marked UNCALIBRATED. I don't know if that's 10% or 90% of what
> you'd typically use."*

That is the desired behaviour, stated more clearly than the block asked for. The
grader matched `90% of` by substring and scored it a violation. E11's stated
hypothesis — *"the block prints dollar figures the model reassembles into a
ratio"* — was also wrong on inspection: the uncalibrated block contains no
capacity to divide by and already says *"Numbers above are spend, not
headroom."* The proposed fix would have removed useful information to cure a
symptom that did not exist.

### The fix was to the instrument

`must_not_match` patterns are now evaluated only against **assertive** sentences.
A sentence carrying an unambiguous negation cue is the model *declining* to do
something, not doing it. Filtering is per sentence, so one hedge cannot launder a
later claim.

The cue list is deliberately narrow, and the asymmetry is the reason: **a false
negative hides a real defect; a false positive only wastes a re-read.**
`whether`, `rather than` and `instead of` were tried and removed — they are
comparison cues, and *"I'll review a subset rather than all 400 files"* is a real
violation they would have concealed. Each removal is now a test.

The grader is the one part of the harness that is pure logic, so it moved into
the offline unit suite. It had none of its own tests while it was busy grading
everything else.

### `--save` paid for itself

The 24 baseline responses were re-graded against the corrected grader at **zero
additional cost**. Without stored responses, correcting a grader means re-running
the experiment. Any eval harness should persist raw output by default; the
grader will be wrong before the model is.

### Rules this yields

- **Confirm a defect before scheduling a fix.** Both these backlog items were
  written from three samples, and half of that backlog was fiction.
- **Grade assertions, not substrings.** "I can't tell you if it's 90%" and "it's
  90%" contain the same characters and opposite behaviour.
- **Instrument errors are asymmetric — tune for false positives.** A grader that
  cries wolf costs a re-read. One that stays silent costs the finding.
- **Test the test.** Four of this harness's first five "failures" were its own
  bugs (§16 had three, this is the fourth).
- **Persist raw output.** It converts a grader correction from an experiment into
  a re-read.

---

## 19. Hermes: a hook can fire without reaching the model, and silence does not retract old context

The Hermes port looked easier than either existing host: Hermes v0.20.3 already
has live account-meter readers for OpenAI Codex, Anthropic OAuth, and OpenRouter;
persists canonical token/cache/reasoning totals; supports plugin skills and
commands; and exposes `pre_llm_call` context injection. Live read-only probes on
this machine succeeded for both configured subscription providers. The first
implementation therefore reused the host meter instead of copying Claude's
transcript reconstruction or Codex's unfinished plugin ingestion loop.

That architecture was right, but the first review found two host semantics that
the public hook summary alone did not reveal.

### `pre_llm_call` context is replayed, not ephemeral in the ordinary sense

Hermes keeps the clean user text in `content`, but stamps the exact API-bound
injected text into an `api_content` sidecar. That sidecar is persisted and replayed
on later turns and resumes so the provider's prompt-cache prefix remains
byte-stable. Therefore this policy was unsafe:

> stale meter → return no context

Returning nothing on turn 2 does not remove turn 1's `GO WIDE`,
`ASK BEFORE PROCEEDING`, or `WOULD RUN OUT`; the old block remains in historical
API context. A moving policy signal needs an explicit current supersession
protocol:

- every fresh block says it is the **CURRENT BLOCK** and supersedes all earlier
  usage blocks;
- a missing, stale, or changed route emits a small tombstone saying
  **IGNORE ALL EARLIER** blocks and authorizes no usage-driven action;
- the frozen static policy says only the newest block is current.

The durable lesson for any prompt-hook integration is: **inspect replay semantics,
not just injection semantics. Silence is not retraction when the host preserves
exact API bytes.**

### A hook can run and still never reach one transport

Hermes invokes `pre_llm_call` before dispatching every turn, including the
optional native `codex_app_server` runtime. But that runtime bypasses Hermes's
normal API-message composition and sends the plain user message directly to the
Codex subprocess. The hook result is computed but not delivered.

That is worse than an absent hook if rendering has side effects: the first spike
claimed the once-per-day pacing checkpoint while Codex never saw it. The port now
explicitly declines injection and does not claim directives in
`codex_app_server`; standard OpenAI Codex `codex_responses` is the supported
transport. A port must verify **hook callback → final wire payload**, not merely
that the callback fired.

### Host and provider are separate identity axes

Hermes also made the planned generic `Provider` protocol look too small. Hermes
is one host with several quota providers, and a profile can route sessions through
different endpoints or credentials. The P0 now isolates snapshots, history,
acknowledgments, and refresh leases by provider plus a hash of the base URL. The
public hook contract still does not expose account/credential identity, so two
accounts behind the same provider endpoint require separate Hermes profiles; the
plugin documents that as a hard boundary rather than labelling mixed state exact.

This argues for two eventual interfaces rather than one overloaded `Provider`:

- **host telemetry/delivery** — turn events, tool/subagent attribution, prompt
  injection, replay, and transport behavior;
- **quota source** — account identity, exact utilization/reset windows, or a
  provider-specific estimator when exact limits are unavailable.

### Verification

The Hermes implementation has a stdlib policy/adapter suite covering fresh and
stale replay, tombstone retraction, lockout promotion, cheap pre-reset caps,
weekly pacing hysteresis, read-only status, model switching, resume field names,
native App Server exclusion, endpoint isolation, and profile-wide refresh
leasing. It is also loaded through Hermes's real Plugin Doctor and exercised
against the live OpenAI Codex account meter. Existing Claude and Codex unit suites
remain green.

### Resolution: close the host contract instead of weakening the policy

The first review's remaining blockers could not be solved honestly inside the
plugin alone. They were generic host-contract gaps, so the local Hermes branch
adds three additive behaviors:

1. `pre_llm_call` now carries the actual current `provider`, `base_url`, and
   `api_mode` before injection. The plugin captures that route every turn, closing
   same-model provider/endpoint switches without waiting for `pre_api_request`.
2. Multimodal user turns use a tagged TEXT-column `api_content` **suffix** sidecar.
   API builders append its text part to a deep copy of image-bearing content;
   clean user content remains untouched, persistence/branching stay backward
   compatible, and stale tombstones reach image/attachment turns.
3. Account-meter snapshots expose the credential runtime base URL actually chosen
   for the request. The plugin hashes exact route bytes (preserving case, path,
   query, and trailing slash) and rejects a reading when the resolved credential
   endpoint differs from the active route. Raw endpoints and credentials are not
   persisted.

The unresolved hard boundary is now narrower and explicit: two accounts behind
one identical provider endpoint still require separate Hermes profiles because
the public hook contract deliberately exposes no credential/account identifier.
That is isolation by profile, not a prompt-level promise.

---

## 20. One tool, three users: separate the fact from the preference

Raised 2026-08-18: *"a user with money galore just wants it done as fast as
possible, with as many workflows as it takes. Clearly this isn't for that
person. I want it to be for everyone."* Correct — the tool shipped exactly one
policy (conserve-ish, `GO WIDE` when ample), and a single policy fits nobody
exactly.

The instruction that shaped the answer was *"if an issue can be nonexistent, or
solved by a design choice, and that design is better, go with the better
design."* Applying it split the problem cleanly in two.

### Half of it was never a preference — it was a wrong fact

Every lockout figure in the block — `blocked_hours`, `WOULD RUN OUT`, the pacing
checkpoint — assumes hitting a cap stops you dead. **With usage credits enabled
that is simply false.** The cap is a price change; the user keeps working.

And the endpoint says which. From a raw payload read 2026-08-18:

```
extra_usage:  is_enabled = False
              disabled_reason = 'out_of_credits'
              credits_ever_enabled = True
              user_disabled = False        spend_limit_reached = False
spend:        can_purchase_credits = False   can_toggle = False
```

So this is **measured, not asked**. With credits on, the block now says
`COST AHEAD (not a lockout)` and drops the blockage framing entirely. Warning
someone about a lockout that cannot happen is not a stern policy, it is a false
statement — and no preference setting should be needed to stop a tool lying.

Two incidental corrections from the same payload read:

- **`spend` and `extra_usage` are billing objects, not meters.** `spend` carries
  a `percent`, so the parser had been picking it up as a limit reading 0% and
  putting a phantom bucket in the block.
- **The live kind list is far longer than this tool reads** — `seven_day_cowork`,
  `tangelo`, `iguana_necktie`, `omelette_promotional`, `amber_ladder`,
  `nimbus_quill` — nearly all null. Also present and null today:
  `limit_dollars` / `used_dollars` / `remaining_dollars`. If those are ever
  populated, the entire price-weighting estimate becomes unnecessary. And each
  limit carries a vendor-assigned `severity`, which this tool currently ignores
  in favour of computing its own.

### The other half is a real value judgement, so it is stored — not asked

Even against a hard wall, how much throughput to trade for lockout safety is
genuinely the user's call. Three postures, set once by
`usage-budget posture <name>`:

| Posture | Pacing | Checkpoint | `GO WIDE` |
|---|---|---|---|
| `throughput` | hidden | never | always |
| `balanced` *(default)* | shown | past today's share | when ample and under pace |
| `stretch` | shown | past **80%** of today's share | never |

**Why not an `AskUserQuestion` each session** — the obvious design, and worse.
A posture is stable across sessions, so asking repeatedly re-derives an unchanged
answer, burns a turn doing it, and lands squarely in the failure §13 and §16 both
document: *a prompt that fires every time gets approved without being read.* A
preference that rarely changes belongs in config; only a decision that is
genuinely new each time deserves a prompt.

### E12, from dogfooding

Promoting the lockout directive (§17) shipped without the hysteresis the ask
checkpoint already had. Within the hour, four consecutive replies opened with the
same sentence about a forecast that had barely moved. It now speaks only when it
has something new to say — different bucket, new window, or a lockout 50% worse.
§17 measured *whether* the directive was obeyed and never *how often it should
fire*; every eval scenario is single-turn, so the harness is structurally blind
to repetition fatigue.

### Measured

Two new eval scenarios, both **3/3**: `throughput-posture-does-not-economise`
(the failure being subtle — not a refusal, just a quietly smaller plan offered as
if it were the sensible default) and `overage-is-cost-not-blockage`.

### Rules this yields

- **Ask what kind of question it is before designing an answer.** "Will I be
  blocked?" is a fact the vendor publishes. "How much do I care?" is a
  preference. Only the second deserves a setting, and neither deserves a prompt.
- **A tool with one policy has a target user, whether or not it admits to one.**
- **Preferences go in config; prompts are for decisions that are new each time.**
- **Read the raw payload, not your own parse of it.** One dump answered the
  design question and exposed two parser bugs.

## 21. Quoting a file read: two formulas, not one — and `bytes/4` is wrong in both roles

Component 2 of the 2026-08-18 proposal asked for a deterministic pre-read quote,
suggesting `bytes / 4` with an overhead multiplier. Building it turned up that
**there are two different questions here and one constant cannot answer both**,
and that the obvious constant is wrong for each of them in a different way.

### The host's gate, as `Read` reports it

`Read` states its limits when it hits them, and its description says it reads up
to 2,000 lines by default. The two caps do not behave alike. Probed live:

| probe | result |
| --- | --- |
| 270,000-byte `.txt` | **hard error**, no content: *"File content (263.7KB) exceeds maximum allowed size (256KB)"* |
| 64,000-byte `.json` | **paginated**: *"showing lines 1-3035 of 8001 total (56005 tokens, cap 25000)"* |

So the byte cap refuses and the token cap **pages**. A quoter that treats the
token cap as a failure is wrong; one that ignores it quotes a 40 MB log at
millions of tokens and gets ignored. Both were live bugs in the first draft here.

The `.json` probe also pins down *what* is measured: 56,005 for 64,000 bytes only
reconciles with a divisor of 2 **applied to the rendered text**, i.e. with the
`N\t` line prefixes included. The estimate is taken post-render, not on the raw
file. For other extensions this tool assumes a divisor of 4; no probe has
confirmed it.

> **This probe cost ~25,000 tokens of context**, because the expectation being
> tested was that the file would be *refused*. Recorded because it is the cheapest
> possible reminder that a limit is a hypothesis until it has been run.

### The cost ratio, measured: 2.6 bytes/token, not 4

A divisor of 4 — the proposal's `bytes / 4`, and the gate divisor this tool
assumes for non-JSON files — is 35% wrong as a cost model.
Exact ground truth was already on disk: an assistant record carries both its text
and `usage.output_tokens`, and when a reply has **exactly one content block** that
count belongs to that block alone. (§11's one-record-per-content-block duplication
is precisely what makes the "exactly one" filter necessary.) n=579 across every
local transcript:

| class | n | median | p10 | p90 |
| --- | --- | --- | --- | --- |
| prose (<10% fenced) | 531 | 2.778 | 2.544 | 3.028 |
| mixed (10–60%) | 40 | 2.780 | 2.570 | 2.915 |
| code-dominant (>60%) | 8 | 2.634 | 2.605 | 2.753 |
| **ALL** | **579** | **2.775** | **2.544** | 3.025 |

Code is denser than prose, as expected, but the classes are close enough that one
constant is defensible. **2.6** was chosen: just under the code median, near the
all-sample p10, so the estimate leans high in tokens. §11 shipped an estimate wrong
in the *unsafe* direction for about four days; a quoter that under-quotes is the same
defect wearing a new hat.

### Negative result: per-call context deltas cannot be inverted to payload size

The first attempt tried to calibrate against real `Read` calls — regress observed
context growth on file size, using PR #10's attribution. It **failed**:
`tokens = -0.109 * bytes + 12,886`, **R² = 0.004**, slope the wrong sign, n=126.

Recorded rather than discarded, because the reason generalises: `cache_read_input_tokens`
moves in cache-boundary steps rather than smoothly, and the fixed per-turn injected
content (system reminders, this tool's own block, gbrain recall) swamps a typical
file. `analyze`'s aggregate attribution is sound; **inverting it to a per-call
payload size is not**, and nothing downstream should try.

### Rules this yields

- A pre-read quote needs the **host's** formula to predict admission and a
  **measured** ratio to predict cost. Using one for both is wrong twice.
- Line-number rendering is part of the payload. `N\t` is ~2 tokens per line —
  ~4,000 tokens on a 2,000-line file that a raw byte count cannot see.
- Binary files are flagged and excluded, never estimated. Extension first, then a
  NUL sniff of the first 8 KB.
- Quote the **carry**, not just the landing cost. A tool result is re-read every
  subsequent turn (`analyze`); landing cost alone understates by an order of
  magnitude over a session.
- Report the share against the **binding** bucket. There is no calendar-day
  counter to take a percentage of, and inventing one would be a fiction.

## 22. A "daily budget" has no vendor limit behind it — and a day is not a calendar day

Component 1 of the 2026-08-18 proposal asked for counters for session, **day**,
and rolling week, an 80% soft warning, a 100% hard stop, a crossing log, and a
read API for other components. Phase 1 found the fuel tank already ~85% built and
named three real gaps. Building those three turned up two design traps.

### There is no daily cap, so the denominator has to be borrowed and labelled

The vendor publishes a 5-hour limit, a 7-day limit, and a smaller 7-day premium
limit. **There is no daily limit.** A "day counter" therefore has a numerator and
no denominator, and the honest move is to borrow the week's per-day share
(`capacity / 7`) and flag it — `capacity_is_derived: true` — rather than let a
percentage imply a limit the vendor never published.

### A day is phase-aligned to the weekly anchor, not to local midnight

`_pace` prorates the weekly allowance in day-slices measured **from the weekly
window start**, and this account's week resets Monday 07:00. Aligning the day
bucket to local midnight would have reported "today's spend" against a share
computed for a *different 24 hours* — the two would disagree by seven hours every
day, and the disagreement would be invisible because both numbers look reasonable.
Days therefore run 07:00 → 07:00, confirmed live: `Tue 07:00 -> Wed 07:00`.

When the weekly anchor is missing the bucket falls back to local midnight and says
so in `basis`, rather than silently mixing two notions of "day".

### The bug: a 24-hour bucket read through a 5-hour filter

The first implementation sourced its events from `_all_events_for_window`, which
pins the start to the **session** window by design. The day bucket then reported
**exactly the session's spend** — $9.65 over 88 turns — and it looked plausible
enough to ship. The corrected figure for the same moment was **$84.66 over 405
turns**, an 8.8x understatement in the *unsafe* direction again.

It was caught only because session and day happened to be byte-identical in the
output, which is a coincidence that stops happening as soon as the session window
rolls. Fixed by giving day-wide reads their own source (`_events_since`) and, in
`budget_read`, preferring the bucket `_build_state` already computed from the
complete event list.

### The crossing log has to be idempotent, because the block renders every turn

Logging on level alone would append the same crossing on every single turn and
destroy the log's only value — saying *when* something happened. A crossing is
keyed on `(bucket, window_start, level)`; a window rollover re-arms it, because
crossing 80% in a fresh window is a genuinely new event. Downgrades are recorded
too, since a rollover or a capacity correction moves a bucket back down and a log
that only ratchets up would misrepresent the history.

`trigger` names the turn by model and cost, not by tool. Tool identity is not in
the event stream (`[epoch, usd, model]`); `analyze` owns per-tool attribution and
re-deriving it here would mean re-parsing every transcript on a path that has to
stay under a second.

### What was deliberately NOT built: the hard stop

The proposal asked for a 100% hard stop that halts token-consuming operations.
**`hard` ships as a LEVEL, never a gate.** FINDINGS §17 measured obedience going
29% → 92% on the strength of the block *informing* rather than refusing, and the
shipped text says "NOT a refusal" on purpose. A blocking stop would trade a
measured result for an unmeasured one. The read API says this in its own
docstring: `level` is advisory, and a caller that wants to gate must decide that
itself and own the consequence.

### Rules this yields

- A counter without a published limit must label its denominator as derived.
- Period-aligned buckets inherit their phase from the anchor they are compared
  against. Midnight is a guess dressed as a convention.
- Any helper that pins a window start is unsafe for a wider window. Name the scope
  in the helper (`_all_events_for_window` vs `_events_since`) so the misuse reads
  as wrong.
- An event logged from a per-turn render path must be keyed on the transition,
  not the state.

## 23. Seven ways to sound more certain than you are — PR #11 block A, closed

@Eclipse1231317's `robbie/codex-backlog` sat unmerged for two days while the App Server work
diverged from it. Rebasing it (12 commits, three doc conflicts) and running his 41
tests against the newer tree produced 30 failures. Thirteen of those were classed
**block A** — not architecture drift, but hardening the newer tree had dropped. All
thirteen now pass; the codex suite goes **19 → 32**, the repo to **296**.

**Every one failed in the same direction: toward false confidence.** That is the
third independent occurrence of this class in this project — §11 (the numerator),
§22 (the day bucket), and now the Codex half — which makes it an idiom to ban
rather than three unrelated slips.

### The seven mechanisms

**1. One timestamp for a file that has several sources.** `usage.json` carried a
single `updated_at`. Ingesting *tokens* restamped it, so rate limits — untouched,
possibly hours stale — began reading as fresh. Token counts say nothing about
quota. Fixed with `rate_limits_updated_at` plus a per-window `captured_at`.

**2. Freshness computed over the wrong set.** A sparse update mentioning only
`primary` restamped the file and relabelled an omitted, stale `secondary` as
current. A snapshot is now fresh only when **every** stored window is: a
partially-fresh snapshot is a snapshot of nothing, because the constraining
window may be the stale one.

**3. Replace where a merge was meant.** The same sparse update *dropped*
`secondary` entirely. A caller reporting one window is not asserting the others
ceased to exist. Rate limits now merge by `limit_id`.

**4. `argparse(default=0)` erases the difference between "unset" and "zero".**
`--input-tokens` defaulted to `0`, so a completion that supplied no token flags
looked identical to one explicitly zeroing them — and the sparse merge dutifully
wiped `cached_input`, `reasoning` and `output` that `ingest-usage` had already
measured. `default=None` is the only honest default for an optional quantity.

**5. `int()` is not validation.** It accepts `-1`, silently truncates `1.5`, and
accepts `True`, because `bool` subclasses `int` in Python. Three impossible token
counts, all becoming plausible numbers. Replaced with an explicit `_nonnegative_int`.

**6. Flooring an unknown into a value.** `_efficiency_score` clamped both ratios at
`0.1` and defaulted a missing token total to `1.0`, so a task with no measured
elapsed time and no measured tokens still produced a confident score. It now
returns `None`, and when it does score it reports the ratios and the basis it used.
Same fix for elapsed: `max(0.0, ...)` turned a clock problem into a measurement of
zero seconds.

**7. Dict iteration order as a tiebreak.** `_find_task` with no `session_id`
matched any record whose own session was absent, attributing a turn to whatever
happened to be first. Now it refuses without a session and takes the newest match.

Plus one that is only a near-miss: `_observed_percent_cost` coerced `limit_id` with
`str()` and `_observed_burn` did not, so numeric limit ids silently matched nothing
and burn came back `(None, 0)` — an absence indistinguishable from "no burn".

### Calibration now says which history it used

`_history_calibration` returned one blended number and one sample count. It cannot
distinguish three runs on *this* model from three runs on a model four times
slower, though they imply very different estimates. It now returns `model_scope`
(`model-specific` / `cross-model-fallback` / `heuristic-no-history`) and separate
time and token sample counts, and `cross-model-fallback` **caps confidence at low**
regardless of sample count. Borrowing another model's history is better than
nothing and worse than measuring; the estimate says which it did.

### Two adaptations, recorded rather than hidden

Not every ported assertion survived unchanged, and both exceptions are schema
supersets rather than defects:

- the exact-dict token assertion was narrowed to the six fields it named, because
  this tree's schema also carries `model_calls` (what distinguishes a measured
  rollout total from a caller-supplied one);
- `efficiency_score` changed shape from a bare float to a dict, so the local test
  asserting `isinstance(..., float)` was upgraded, not dropped.

The superseded `test_stale_usage_snapshot_is_uncalibrated` in `test_agent_efficiency.py`
was **removed with a note**: it staled `updated_at` and expected rate limits to go
with it — asserting mechanism 1, the bug itself.

### Rules this yields

- One timestamp per source, not per file. One freshness verdict per window, not
  per snapshot.
- `default=None` for every optional quantity. `0` is an answer, not a silence.
- Never floor an unknown into a value. `None` is a legitimate return.
- Any comparison of identifiers that may be numeric or string coerces both sides,
  in every function, or in none.
- When an estimate borrows data it did not measure, it says so and caps its own
  confidence.

## 24. The measured lever was computed all along and never surfaced

Asked whether the proposal's remaining components would actually make an agent
more efficient. Measured instead of arguing. Two days of local history,
amortised context cost by caller:

| caller | calls | tokens added | cost |
| --- | --- | --- | --- |
| **Bash** | 2,161 | 8,361,220 | **$322.70** |
| Read | 306 | 1,489,377 | $38.31 |
| (conversation) | 105 | 248,892 | $34.61 |
| Edit | 137 | 118,843 | $12.49 |
| everything else | | | ~$18 |

**Bash is 76% of context spend. Read is 9%.** Against that:

- **Component 3 (state handover)** reclaims nothing at the seam it would use. A
  subagent prompt is already hand-written and already compressed; nobody dumps a
  transcript into one. Its `PostCompact` variant *spends* tokens rather than
  saving them — a correctness measure wearing an efficiency label.
- **Component 4's tagging store** likewise. Tags cannot protect anything (§ Phase 1:
  the host will not read them) and re-injection after compaction adds tokens.
  **§26 closed it a second time, mechanically:** even the reduced form — let the
  agent trigger the host's own compaction — is unreachable from every route.
- **Component 2 (quoting)**, which shipped, targets the 9% row. Real, cheap, and
  honestly not where the mass is. **§27 later showed that is the right row
  anyway:** the 76% Bash row has no intervention in it.

> **Refined by §27.** "Bash is 76% of context spend" stands, but the implied
> next step — go after Bash — does not. Its median result is 155 tokens and only
> 4 calls in 430 transcripts crossed 10k. Share is not the same as intervenability.

Both remaining components are **additions to context justified as savings** —
the same inversion the prompt-cache arithmetic already exposed for the garbage
collector (§ Phase 1: 171 turns to break even on a 10% prune). Recorded as
decided-against rather than outstanding.

### What was actually worth building, and it already existed

`_breakeven_turns()` has computed the one number in this repo with a measured
payback since PR #10 — and it was reachable only by running `analyze` by hand,
which nobody does mid-session. Live example the day this was written: a session
at **210,056 tokens** paying **$0.0900 every turn** more than a compacted one,
against a **$1.16** one-off. **Thirteen turns to break even, invisible unless
asked for.**

So the block now carries a `COMPACT NOW` line. Three things make it work rather
than become wallpaper:

- **The context size comes from the hook payload's `transcript_path`,** tailing
  only the last 256 KB. The whole transcript can be tens of megabytes and this
  runs on the prompt path. §11's per-content-block duplication is harmless here:
  every record of a reply repeats the same `usage`, so the last one is correct
  without deduplication.
- **It renders only when actionable** — payback within 25 turns and a saving
  above $0.01/turn. A line that is truthful but useless ("pays for itself in 400
  turns") trains the reader to skip the line that is not.
- **It speaks once per session,** then only if the payback halves. This is E12
  (§20) applied before it could recur: the advice would otherwise fire on every
  turn of a long session, which is precisely when the session is long enough for
  it to matter.

### Rules this yields

- Before building an efficiency feature, measure where the cost actually is. The
  intuitive target (file reads) was 9%; the real one (Bash output) was 76%.
- An "efficiency" feature that adds tokens needs to name the tokens it removes.
  Neither remaining component could.
- A number that exists only behind a manual command does not change behaviour.
  Computing it was never the hard part.

## 25. Compacting vs. starting over: the same landing point, so the choice is not economic

Asked whether `/compact` differs in any real way from writing a handover file and
starting a fresh session. The assumption on both sides is that a new session is
"clean and cheap". It is not, and the number that settles it had never been
measured — `analyze` carried the caveat *"post-compaction summary size is
assumed, not measured"* since PR #10, and `DEFAULT_SUMMARY_TOKENS = 30,000` fed
the break-even arithmetic on that assumption.

### Method

Both options are measured as **settled cache-read**: the prefix a turn re-reads
once it is cached. The turn immediately after a compaction — and a session's
first turn — *writes* its prefix rather than reading it, so its `cache_read` is
misleadingly small; the honest figure is the turn after that. Compactions are
detected as a sharp `cache_read` drop **within one transcript** (a `/clear`
starts a new file), requiring both the drop turn and the following turn to be
low, which excludes resets landing near zero.

### Result

| landing point (settled cache-read) | n | median | p10 | p90 |
| --- | --- | --- | --- | --- |
| after `/compact` | **3** | **24,702** | 24,437 | 29,503 |
| fresh session, before any handover is read | **713** | **28,918** | 2,200 | 42,947 |

**A brand-new session starts at ~29,000 tokens of prefix before anything is
said** — system prompt, tool schemas, `CLAUDE.md`, skill listing, MCP
definitions, hook output — and then pays to re-read the handover file on top,
every turn. Compaction lands slightly *below* that.

> ⚠️ **n=3 on the compaction row.** The detector is deliberately strict, and this
> machine's sessions are usually cleared rather than compacted. Treat that median
> as indicative; the 713-sample floor is solid.

> 🛑 **SUPERSEDED 2026-08-19 — the compaction row measured a cache write, not a
> landing point.** It was read off the turn immediately after the compaction,
> which is the turn that writes the new prefix to cache, so those tokens sit in
> `cache_creation` and a `cache_read + input` formula misses them. That is why
> 24,702 came out *below* the 28,918 fresh-session prefix, which is impossible
> for a context that has to contain that prefix. Counting creation too, the one
> clean boundary reads **499,716 → 41,084**. The 713-sample fresh-session row is
> unaffected. See **§32**; the `DEFAULT_SUMMARY_TOKENS = 30_000` this row set is
> now a fallback, and the summary text itself measures ~4,650 tokens.

### What this does and does not settle

- **The constant is validated, and errs safely.** Measured ≈ 24.7k against an
  assumed 30k. The assumption is ~20% high, which overstates compaction's one-off
  cost and understates its per-turn saving — so the break-even, and the
  `COMPACT NOW` line built on it (§24), fire *late* rather than early. Left at
  30,000 deliberately: n=3 is not enough to move a constant, and the error is
  already in the safe direction.
- **The choice is not economic.** Both land in the same band, so per-turn cost
  afterwards is near identical and neither option is a saving over the other.

The real differences are what survives:

- **Compaction keeps session state a new session loses** — most concretely the
  read-before-edit record: `Edit` fails without a prior `Read` *in the same
  conversation*, so a fresh session must re-read every file it intends to touch.
  Also permission grants, todo state, rewind continuity. And its summary is
  precomputed in the background, so it costs no wall-clock.
- **A handover file lets you choose what survives.** Measured retention through
  compaction is a median **7.6%** of the prior context: it discards ~92%, and
  the summarizer picks which 92%. Compaction also keeps recent messages
  *verbatim* (`messagesToKeep`), which can carry large tool outputs forward that
  a handover would not.

### Rules this yields

- A fresh session is not a blank slate; it is a rebuilt ~29k floor. "Start over
  to save tokens" is false on this setup.
- Prefer `/compact` for continuing the same work — the read-before-edit reset
  usually costs more than curation gains. Prefer a handover file at a genuine
  phase change, where discarding 92% deliberately beats discarding it by proxy.

## 26. The agent cannot compact itself — no documented route reaches compaction, and none honours a turn boundary

Asked whether the logic behind `/autocompact` could be called by the agent
itself, so it compacts on its own without telling the user — with the constraint
that it must happen at the end of a turn, never mid-task.

The answer is no on every documented route. This is checked against Claude Code's
own documentation — the [hooks reference](https://code.claude.com/docs/en/hooks)
and the `Skill` tool's description — not measured, so it describes the current
documented surface and should be re-checked when the client updates.

### Route 1 — invoke the command. It is not a skill.

The model invokes commands through the `Skill` tool, whose own description says
built-in CLI commands are not skills, and `/compact` is built in. The split is
coherent: a skill expands to text the model emits, while a built-in command runs
host code against app state — and compaction *rewrites the transcript*, so a
model-callable `/compact` would let a model edit its own history.

### Route 2 — a hook requests it. No such output exists.

`PreCompact` can **block** a compaction (exit code 2, or `"decision": "block"`),
and it receives the trigger and the user's custom instructions. No documented
hook output, on any event, requests one. Consistent with the standing conclusion
that hook output is additive/gating only (§11, and the Component 4 close in §24).

### Route 3 — move the threshold so the host fires on its own. The wrong scope.

The threshold is a setting, the auto-compact window. A hook could rewrite
`settings.json`, but whether a running session picks that change up is
unverified here, and an environment variable would have to be set before the
session starts: a hook is a child process and cannot change its parent's
environment.

### The premise: compaction is not a turn-boundary event

Per the hooks reference, the host compacts on its own triggers: when the
conversation reaches the auto-compact window, after an idle period (idle
compaction), and to recover from a context-limit error the API has already
returned — in which case blocking it makes the current request fail. That last
trigger lands inside a request, so **auto-compaction already happens mid-task**.
The end-of-turn-only constraint is not one the host follows, and there is no
turn-boundary hook to borrow.

### The one lever that may exist: steering, not triggering

A `PreCompact` hook cannot decide *when* compaction happens. Whether it can decide
**what the summary is told to keep** is undocumented: the reference lists the
user's custom instructions as hook input, and no output that adds to them. §29
builds that lever on the assumption that a `PreCompact` hook's stdout reaches the
summariser. Given §25's measured 7.6% retention, it is the half that would matter.

### Rules this yields

- Self-triggered compaction is closed on every documented route. Re-check the
  hooks reference before re-opening it; it is a version-specific fact.
- Do not build a "compact at end of turn" affordance. That boundary is not where
  compaction lives, and the host does not honour it either.
- `COMPACT NOW` (§24) is the correct and complete shape: advising the user is the
  entire permitted surface, not a fallback from a better design.
- The one possible lever is `PreCompact` influencing *what survives* a compaction
  the host schedules — undocumented, and unverified (§29).

## 27. Bash is 66% of tool output and there is nothing to intervene on — it is a broad tax, not a few catastrophes

§24 measured Bash at 76% of context spend and left one item open: *"I'd want to
measure the distribution of Bash result sizes before proposing anything."* This
is that measurement, over **430 transcripts / 7.6M tool-result tokens**, and it
retires the idea.

| tool | calls | total tok | share | median | p90 | max |
| --- | --- | --- | --- | --- | --- | --- |
| Bash | 12,797 | 4,982,197 | 66% | **155** | 932 | 11,441 |
| Read | 1,100 | 1,375,708 | 18% | 548 | 2,717 | **25,060** |
| Edit | 2,842 | 259,551 | 3% | 72 | 91 | **53,978** |
| WebFetch | 108 | 150,123 | 2% | 364 | 3,500 | 19,231 |
| `mcp__<knowledge-server>__get_page` | 26 | 123,352 | 2% | 3,776 | 12,026 | 12,772 |
| `mcp__scrapling__screenshot` | 3 | 108,363 | 1% | **36,121** | 36,121 | 36,121 |

### The distribution refuses the intervention

| concentration | |
| --- | --- |
| top 1% of Bash calls | 14.5% of Bash tokens |
| top 5% | 36.7% |
| top 10% | 51.4% |
| top 25% | 75.1% |
| calls over 10k tokens | **4 calls, 0.9% of Bash tokens** |
| calls under 500 tokens | 10,156 of 12,797 |

**Bash is expensive because it is called 12,797 times, not because any call is
expensive.** Median 155 tokens. Four calls in 430 transcripts crossed 10k. A
warn-before-expensive-Bash feature would have fired four times and saved under 1%
of the Bash total; you have to reach the top *quartile* to cover 75%, and the top
quartile is ordinary work — `git status`, `grep`, test runs.

There is no advice to give here. "Use Bash less" is not actionable: the Bash
calls *are* the work. This is the opposite of Read, where a single accidental
paginated read costs 25,060 tokens in one shot — which is exactly why admission
control (Component 2, §21) is right for Read and wrong for Bash.

### The real per-call outliers are somewhere nobody was looking

The worst *single* results are not Bash at all:

- **`mcp__scrapling__screenshot` — 36,121 tokens each, every time**, with no
  variance across 3 calls. One screenshot costs more context than the largest
  Bash call ever recorded here, times three.
- **`mcp__<knowledge-server>__get_page` (a private knowledge server) — median 3,776, p90 12,026.** An MCP read is ~24x a
  Bash result at the median.
- **`Edit` — max 53,978** against a median of 72. A 750x outlier in a tool whose
  output should be a confirmation line. Not diagnosed; flagged.
- **`Read` — max 25,060**, precisely the host's pagination cap (§21). The cap
  works, and hitting it is the failure mode `quote` exists to pre-empt.

MCP tool results are the concentrated cost, and no component ever proposed
touching them. Not pursued here — with 26 and 3 calls the sample is too thin to
design against, and the fix for a 36k screenshot is a resize argument at the
server, not a budget signal.

### Rules this yields

- **Closed: any Bash-output intervention.** Fourth component-level idea retired by
  measurement (3 and 4 in §24, self-triggered compaction in §26). Do not reopen
  without a distribution that looks different from this one.
- Admission control is only worth building where a **single call** can be
  catastrophic. That is Read, and possibly MCP reads. It is not Bash.
- "Tool X is N% of spend" does not imply a lever exists at tool X. Share and
  intervenability are different questions, and this project has now conflated
  them once.

## 28. The headline number was a point estimate with no error bar, and its error was a regime flip

Two prompts in one session, minutes apart, rendered:

```
Affordance: ~454 usable turns  · binding: week pace
Affordance: ~1878 usable turns · binding: 5h window
```

Cost per turn moved by $0.004. Everything else was the week's utilisation being
estimated on the first turn and read from the vendor's meter on the second.

### Why it is structural, not a glitch

`PROBE_MAX_AGE = 900`. The refresh runs on the **Stop** hook; the block renders on
**UserPromptSubmit**. So after any idle gap over 15 minutes the first prompt back
finds a stale probe and renders fully estimated, and only the *second* prompt is
exact. That first turn is when a user is deciding what to do next — the worst
possible turn to print a soft number in a hard typeface.

`probe()`'s docstring — *"Never called on the hook path"* — is a deliberate
refusal to put a network call in front of every prompt, and it is the right call.
Up to 5s of latency on each prompt to avoid a soft number is a worse trade. That
leaves only one honest option: the number cannot be made exact on that turn, so
it must stop **looking** exact.

### The error is not a percentage, it is a change of question

Pace headroom is `allowance - spent` — a difference of two similar numbers — so a
few percent of capacity error moves it by tens of percent, and can hand the
ceiling to a different bucket entirely. At that point the two figures are not
imprecise versions of each other; they are answers to different questions. That is
why the report needed a *stability* flag and not just a wider interval.

### Three cuts, because the first two produced dishonest ranges

| cut | range | contains true 1,527? |
| --- | --- | --- |
| calibration spread (`medium` = ±20%), shared scale | 319–1,312 | ✗ |
| measured drift (±31.6%), shared scale | 314–1,422 | ✗ |
| measured drift, **per-bucket, all four corners** | **312–2,177** | ✓ |

- **Spread is not error.** Calibration spread measures how much the samples
  disagree with *each other*; it says nothing about whether they are collectively
  wrong. Every probe already scores the reconstruction against the meter
  (`capacity_drift_pct`) — that residual is the only figure here checked against
  ground truth. Measured live: session **-9.9%**, weekly **-31.6%**, against a
  spread band of 20%. Now persisted to `drift.json` (bounded, 20 rows/bucket) so
  the block can state its own error *after* the probe goes stale.
- **One shared scale cannot represent two different errors.** -9.9% and -31.6%
  are not the same perturbation.
- **The extremes sit on the off-diagonal.** The ceiling is a `MIN()` across
  buckets, so the maximum occurs where the 5h capacity is *high* and the weekly
  capacity is *low*: a low weekly capacity pushes spend past today's prorated
  share, flips into borrow-ahead mode, and hands the ceiling to the 5h window.
  A diagonal-only sweep never visits that corner, which is exactly how cut two
  missed by 105 turns.

### What shipped

Point figure when a fresh probe backs it — unchanged. Otherwise an interval, the
marker **on the line** rather than three lines below it, and a separate sentence
when the binding ceiling itself changes across the range. The stance
(`ample`/`short`/`landing`/`land`) still keys off the point estimate, so no new
over-braking: §13's warning that unspent allowance expires still binds.

### Rules this yields

- A point figure is a claim of precision. Only make it when something exact backs
  it.
- **A range that does not contain the answer is worse than no range**, because it
  reads as a measurement. Derive the band from error measured against ground
  truth, never from internal agreement among estimates.
- Where a quantity is a difference of two similar numbers, report its stability,
  not just its value.
- Caveats must travel with the number they qualify. `Confidence: medium` three
  lines away did not stop either of us from reading `~454 usable turns` as a
  count.

## 29. Using the one lever §26 left open: steering what a compaction keeps

§26 closed self-triggered compaction on every documented route and noted one
possible lever, undocumented and untested: a `PreCompact` hook's stdout reaching
the summariser. This builds on it.

### The assumed contract

The subcommand assumes that a `PreCompact` hook's stdout, when the hook succeeds
and prints something, is appended to the summarisation prompt as additional
instructions, alongside any the user typed into `/compact`. That is not
documented, and it has not been verified against a real compaction. What is
documented: the hook receives `trigger` (`auto`/`manual`) and
`custom_instructions`, and the matcher takes the trigger, so `auto` and `manual`
can be scoped separately.

If stdout is consumed as instructions, the failure mode is unusual: **a traceback
on stdout would become guidance handed to the summariser.** The subcommand
therefore catches everything, prints nothing on failure, and exits 0 regardless —
exit code 2 would block the compaction outright.

### Why this is the right thing to spend the lever on

Compaction keeps a measured median **7.6%** of prior context (§25) and the
summariser picks which 7.6%. The cost of a bad pick is not the lost tokens — it
is **rework**. An agent that loses *"we measured X = 155"* measures X again. An
agent that loses *"we tried Y, it failed"* tries Y again. Both cost exactly what
they cost the first time, and the loss is invisible until it has already
happened.

That is the one context expense this project had not addressed, and — unlike the
ideas closed in §24, §26 and §27 — there may be a mechanism for it, though not a
documented one.

### What it emits

A ranked list, not a set of prohibitions: measurements and their values →
ruled-out approaches and why → decisions and their grounds → exact identifiers →
state of the work. Then, critically, what *loses*: drop the account of **how** the
work proceeded, keep **what it established**. An instruction that only adds
priorities gives the summariser no way to honour them under a budget.

Ranked and cooperative on purpose. §17 measured obedience moving 29% → 92% on
position and mood alone, and an instruction appended to someone else's prompt
should join it rather than compete with it. A
test asserts the text contains no `CRITICAL`, `Do NOT` or `You MUST`.

On an `auto` trigger it appends a clause about in-flight state, because
auto-compaction can fire **mid-task** (§26) rather than at a chosen stopping
point.

Cost: ~0.5k tokens, riding on every compaction that lands at ~24.7k (§25). A test
pins it under 800 tokens so the cure cannot start competing with the disease.

### ⚠️ Benefit unverified — four invalid instruments, then one clean null

An A/B was run against a compaction prompt, with the hook's output appended as
additional instructions. Scoring was exact-match on planted
tokens, deliberately — an LLM judge would introduce the bias the test exists to
detect (§18 is this repo's record of an instrument manufacturing its own defect).

Four fixtures, four artifacts, **none of them measuring the hook**:

| pilot | fixture | control | treatment | why it was invalid |
| --- | --- | --- | --- | --- |
| 1 | 29 facts / 42k | 24/29 | 24/29 | ceiling; `decision` scored on artificial tokens no summary would echo |
| 2 | 120 facts / 52k | 120/120 | 120/120 | total ceiling; facts never competed with the filler |
| 3 | 260 facts / 113k, 2.5k cap | 13/260 | 69/260 | **both arms truncated**; the instruction says "keep first, in this order", so it wins on WRITE ORDER, not retention |
| 4 | 260 facts / 113k, 8k cap | 56/260 | 260/260 | treatment truncated, control not; short payloads still allowed cheap enumeration |

Every one produced a publishable-looking number — two nulls reading as "no
effect", a 5%→27% reading as a win, a 22%→100% reading as a triumph. The tells
were structural, never statistical: `out == max_tokens`, or a category at exactly
100%.

**The instrument has a bias that cannot be removed cheaply.** In a synthetic
fixture everything worth keeping is a scored fact and everything else is obvious
boilerplate, so "keep facts, drop narrative" is costless. In a real session the
narrative carries the user's requests and the reasoning, and dropping it has a
price the fixture cannot represent. Every pilot therefore tilted toward treatment
by construction. Real transcripts were **not** used: they cannot be sent to a
network destination under this machine's output-side rule, and screening
430 of them reliably is not something to trust.

So the benefit stands **unverified**, and this is recorded as a built lever with
an unknown effect, not as a saving.

### What the failed A/B did measure: a length cost, now mitigated

Length is observable without a valid comparison, and there the signal was
consistent — in **every** uncapped run the treatment ran to the output ceiling
while the control stopped naturally:

| pilot | control | treatment |
| --- | --- | --- |
| 2 | 7,193 tokens (natural stop) | **8,000 — capped** |
| 4 | 5,625 tokens (natural stop) | **8,000 — capped** |

At least +42% in pilot 4, and unbounded, since it had not finished. That is a
real recurring cost: a compaction summary is re-read on **every turn that
follows**, so inflating it erodes the very saving compaction exists to produce
(§25: post-compaction lands at ~24,702 tokens).

Mitigated by adding an explicit length-neutrality clause — *"This changes WHAT
you keep, not HOW MUCH you write"* — with the reason attached rather than as a
bare command, per §17. A test pins the clause in place.

### Pilot 5: the clause works, and the first clean comparison shows no benefit

Re-ran pilot 4 unchanged except for the new clause:

| | control | treatment |
| --- | --- | --- |
| output tokens | 4,755 (natural stop) | **4,401 (natural stop)** |
| summary chars | 14,400 | **12,797** |
| retention | **15/260 (6%)** | **8/260 (3%)** |

**The length regression is fixed** — and over-corrected slightly: treatment is now
*shorter* than control, where in every prior run it ran to the ceiling. That is
the clause doing exactly what it was written to do.

**And this is the first structurally valid comparison of the five.** Neither arm
truncated, so the write-order confound that invalidated pilots 3 and 4 is absent,
and neither is at a ceiling. It shows **no benefit — if anything a small
negative** (8 against 15).

Read it carefully, because it is weak in a specific way: **n=1, and at 3–6%
retention both arms sit near the FLOOR.** A floor invalidates a comparison exactly
as a ceiling does — 260 facts cannot fit in a 4.5k-token summary, so only ~15 can
survive either way and the difference is a handful of items inside the noise. This
is the best evidence available and it does not support the hook; it is not
evidence that the hook harms.

**Net position: mechanism assumed, not verified; cost measured and mitigated;
benefit not demonstrated in the one clean test that exists.**

### Decision: built, kept, not enabled

**The hook was unregistered on 2026-08-18** (@DarkNightForge's call). The subcommand and
its tests stay, but nothing runs it, and SKILL.md now leads with that.

The reasoning is the same asymmetry §24 and §27 turned on: a cost that is certain
and recurring (~0.5k tokens on every compaction, re-read on every turn after it)
against a benefit that is unmeasured and, in the single clean test, absent. An
efficiency tool that ships an unproven token cost is arguing against itself.

This is the fifth idea to close, and the only one that closed *after* being built
and shipped. Worth noting the sequence: it survived the design review (§26
identified the lever), survived implementation, and was only caught by measurement —
which is the argument for measuring things you have already shipped, not just
things you are considering.

### Rules this yields

- When an A/B will not resolve, check whether one arm's **side effects** are
  still measurable. The cost side here was measurable on the very runs that could
  not measure the benefit, and it was the half worth acting on.
- An intervention that changes ORDERING looks exactly like one that changes
  SELECTION whenever output is truncated. `out == max_tokens` in either arm
  invalidates the comparison before the scores are worth reading.
- A ceiling and a real null are indistinguishable in the results table. Refuse a
  null until the instrument has demonstrated it *can* show a difference.
- Spend pilots on the instrument, not reps on the arms.

### Rules this yields

- A hook whose stdout is consumed as content must treat stdout as sacred: catch
  everything, print nothing on failure, exit 0.
- Spend a scarce lever on the expense nothing else can reach. Rework was the only
  context cost left with a possible mechanism.
- Rank, do not forbid, when writing into a prompt someone else already wrote.

## 30. The 53,978-token `Edit`: diagnosed, host-side, and deliberately not built against

§27 flagged an undiagnosed `Edit` result of **53,978 tokens** against a median of
72 — a 750x outlier in a tool whose output should be a confirmation line. This
closes it.

### What it was

A failed `Edit` on `.claude/settings.json`. Two one-line validation errors:

```
<tool_use_error>Claude Code settings.json validation failed after edit:
- permissions.deny.3: The :* pattern must be at the end. …
- permissions.deny.4: …
```

followed by **the entire settings.json JSON Schema** — 3,214 lines, 140,343
characters, ending in `"additionalProperties": {}`. Two lines of diagnosis, ~54k
tokens of schema riding along behind them.

Host behaviour, in the `Edit` tool's settings validator. Nothing this project
does causes it and nothing it can do prevents it.

### Is it a class? No.

The obvious next question is whether error results are systematically expensive.
Measured across the same 430 transcripts:

| | n | total tok | median |
| --- | --- | --- | --- |
| error results | 546 | 157,055 | **162** |
| success results | 18,327 | 7,482,678 | 103 |

**Errors are 2.1% of tool-result tokens from 2.9% of calls** — proportionate, not
a tax. `Edit`'s error median is **57** tokens, *lower* than its success median of
72. Exactly **two** error results in the whole corpus exceeded 2k tokens, and this
one is 34% of all error tokens by itself.

### Why nothing is being built

§27's own rule: *"Admission control is only worth building where a single call can
be catastrophic."* This qualifies on severity and fails on frequency — n=1 in 430
transcripts. Building a guard for it would be precisely the over-fitting §27 was
written to prevent, and the guard would ride on every config edit forever to catch
an event seen once.

Recorded instead. The one transferable habit, which costs nothing: **a config file
under schema validation is a landmine for `Edit`** — a rejected write can return
its whole schema. Validate the JSON separately before writing one.

### Rules this yields

- Severity and frequency are separate gates, and a lever needs both. §27 conflated
  share with intervenability; this is the same error in the other direction —
  a spectacular single event is not a reason to build.
- "Undiagnosed outlier" is worth carrying as an open item. It cost one query to
  close, and closing it removed a standing suspicion rather than leaving it to be
  rediscovered.

---

## 31. The block enumerated choices and got prose, because nothing told it otherwise

Reported by the operator, not by the suite: *"I have only ever seen the agent
tell me in prose 'I can stop, continue, be more economical' — it has never used
AskUserQuestion."*

Two directives in the injected block enumerate mutually exclusive options:

| Directive | Options it lists | What it said to do |
|---|---|---|
| `ASK BEFORE PROCEEDING` (pace) | 3 | **"call AskUserQuestion"** |
| `AT LIMIT` | 2 | "offer a smaller scope or a cheaper model" |
| `WILL NOT FINISH MUCH` | 4 | "State the estimate, then offer: …" |

One of the three named a tool. The other two said *offer*, and an agent reading
"offer" writes a sentence. That is the whole defect: prose is a poor instrument
for a fork, because it asks the user to compose a reply mid-task at the moment
they are least willing to, and it makes the cheapest response ("keep going") the
one requiring no thought.

### Why the obvious fix would have been worse

Measured before changing anything: **both directives re-render on every turn
while their condition holds.** Three identical turns produced three identical
directives; four turns through the real CLI produced four. Neither is covered by
`_binding_ack_gate`, whose hysteresis reaches exactly one other line.

So "just tell it to call AskUserQuestion" would have produced a modal per turn —
the same failure §20 was written to fix, upgraded from an annoying sentence to a
blocking dialog. **A question that fires every turn gets dismissed reflexively,
which is worse than prose because it looks like consent.**

### A shared-state bug found on the way

`_pace_ask_gate` wrote its ack with `_save(ACK_PATH, {...})` — a whole-file
replace. `_binding_ack_gate` merges into the same file. So every pace ask
**silently wiped the binding ack**, and the promoted lockout directive spoke
again on the next turn. §20's regression had been partially reintroduced by a
neighbouring function, and no test held the file's shared shape.

### What shipped

`_choice_ask_gate`, modelled on the pace gate that already worked. Sets
`choice_ask` at most once per (kind, window), re-asking on a new window or a 25%
worse overshoot. When set, both directives name AskUserQuestion and carry their
options; when not, **the facts still render and only the interrogation stops** —
the agent is told `do NOT ask again`.

Verified end to end through the CLI: turn 1 emits two AskUserQuestion
instructions, turns 2–4 emit none while still reporting `AT LIMIT` and the turn
count. Ack merging is now a regression test.

One implementation note worth keeping, because it is the same class of error as
the bug above: the gate first used `worst < prev * 1.25`. The landing kind has
no bucket over the line, so `worst` is `0.0`, and `0.0 < 0.0` is False — it
re-asked every turn. Caught by the test that existed only to assert it asked
once. **`<=`, not `<`.**

## 32. The compaction payback was wrong in both terms, and they cancelled

**Reported 2026-08-19** from a live session that was told `/compact` would pay
for itself in ~8 turns while `/context` showed 386.8k of a 1M window used and
613.2k free. The 39% is not the bug — the advice is an *economic* claim, not a
capacity one, and a 386.8k prefix really does cost ~$0.19 to re-read every turn
regardless of how much room is left. The bug is the payback figure, and it turned
out to be two independent errors pointing opposite ways.

### The floor a compaction cannot reclaim

`_breakeven_turns` treated its `summary_tokens` parameter as the **whole**
post-compaction context. But `ctx_tokens` is read from `cache_read + input`, so
it includes the fixed prefix — and compaction rewrites the *messages*, never the
system prompt, tool schemas, memory files or skill descriptions. Subtracting a
summary size alone asserted that the prefix evaporates.

The reporting session's own `/context` itemised it: 4.1k system prompt + 22.7k
tool schemas + 4.5k memory files + 8.4k skills = **39.7k that a compaction
cannot touch**. Measured across five live transcripts the floor is **45–58k** —
it scales with what a session loads, so a machine with 124 MCP tools and 68
skills carries an order of magnitude more of it than a bare one. Assuming one
number was never going to work; it is now read per session from the leading
turns of the transcript.

### The summary is 4,650 tokens, not 30,000

The same constant was also charging **generation** on 30,000 tokens at output
price — $0.75 of a modelled $1.24 one-off, over half the total. The two
`isCompactSummary` records on this machine measure **12,037 and 12,086 bytes**,
which at the measured 2.6 B/token (§21) is **~4,640 tokens**. Strikingly stable
across both, and 6.5x smaller than what was charged.

So the two quantities the one parameter was standing in for differ by 10x, and
only one of them is generated:

| term | priced at | old | corrected |
| --- | --- | --- | --- |
| context read once | input × 0.1 | `ctx` | `ctx` (unchanged) |
| summary GENERATED | output (5× input) | 30,000 | **4,650** (measured, n=2) |
| new prefix WRITTEN | input × 2.0 | 30,000 | **floor + 4,650** (measured per session) |
| saving per turn | input × 0.1 | `ctx − 30,000` | `ctx − (floor + 4,650)` |

### Where §25's 24,702 came from

§25 reported a post-`/compact` "landing point (settled cache-read)" median of
**24,702 (n=3)**, and flagged that it lands *below* the 28,918 a fresh session
starts at. That inversion was the tell: the landing cannot be smaller than the
prefix it must contain. The number was read off the turn immediately **after**
the compaction — the turn that WRITES the new prefix to cache, so its tokens sit
in `cache_creation` and a `cache_read + input` formula misses them. Counting
creation as well, the one clean boundary on this machine reads
**499,716 → 41,084**, against a 44,492 measured floor for the same session.

| §25 says | reality |
| --- | --- |
| compaction lands at ~24,700 total context | that is the mid-write turn; it settles at **41,084** |
| landing is slightly *below* a fresh session's prefix | landing **is** the prefix, plus a ~4.6k summary |
| `DEFAULT_SUMMARY_TOKENS = 30_000` is the summary size | 30,000 was approximately the *2026-05 prefix*, mislabelled |

That last row is the whole bug in one line: **the constant was the floor all
along, called a summary, and the floor has since grown from ~29k to ~45k while
the constant stayed at 30,000.** It is now the fallback used only when no floor
can be measured.

### Effect, and why nobody caught it

Re-run over the five live sessions: the payback moves **6→5, 16→12, 16→12,
11→10** on large contexts, and **76→201** on one sitting near its floor. The
errors cancelled — overcharging generation made compaction look worse, ignoring
the floor made it look better — so the total stayed plausible while neither half
was, and no single-number test could see it. What broke the tie was checking the
constant against a quantity it should have been consistent with (§25's own fresh
-session prefix) rather than against itself.

One transcript-shape trap worth keeping: a session's first assistant record can
be a bare cache write — every token field zero except
`cache_creation.ephemeral_1h_input_tokens: 1781`, and `output_tokens: 0`. Read as
a turn it put the floor at 1,781 against the 58,377 every real turn carried.
**No output means no turn happened**; filter on `output_tokens > 0`.

## 33. `cache_read + input` is a billing view, not a context measurement

**Asked 2026-08-20:** a session's block said its context was `~379k` while
`/context`, two lines below, said `386.8k / 1M`. Was the 2% a bug?

**That gap is not.** Two different instruments, one turn apart. The block reads
the API's own `usage` off the last completed assistant turn; `/context` is the
host's *estimate* ("Estimated usage by category") of the same thing, taken
whenever the user ran it. On steady-state turns `cache_creation` is a median
**0.4%** of context on that session, so nothing is hiding there.

**Chasing it found the third instance of the artifact from §25 and §32, though.**
`_tail_context` computed context as `cache_read + input`. Any turn that *writes*
the prefix to cache carries those tokens in `cache_creation` instead — the first
turn of a session, the turn after an idle gap or a 1h TTL expiry, the turn after
a compaction — and the formula then reports a fraction of the real thing. On this
repo's own working session, three consecutive records:

| reported (`read + input`) | `cache_creation` | true context | invisible |
| --- | --- | --- | --- |
| 24,815 | 190,226 | **215,041** | **88.5%** |
| 215,041 | 2,160 | 217,201 | 1.0% |

The consequence was silent and backwards: at 24,815 against a ~45k floor,
`ctx <= floor + summary`, so `worth_it` came back False and the compaction advice
**disappeared entirely** — on a session actually carrying 215k, right after the
event most likely to make compaction worth doing.

### The pattern, now that there are three

The API splits one logical quantity across three fields, and `cache_read` is the
one everybody reaches for because it is the one that gets *billed at read price*.
Every formula naming only two of the three is correct on ordinary turns and
wrong, by up to 88%, on the turns where the third is non-zero — which are exactly
the interesting turns: session start, cache expiry, post-compaction.

| # | where | symptom |
| --- | --- | --- |
| §25 | post-`/compact` landing point | 24,702 — *below* the 28,918 prefix it had to contain |
| §32 | measured session floor | 1,781 against the 58,377 every real turn carried |
| §33 | current session context | 24,815 against a true 215,041 |

So there is now one `_ctx_tokens(usage)` and every caller wanting a context size
uses it. It is deliberately **not** what `_session_profiles` calls `ctx`: that
one is `cache_read` alone on purpose, because it attributes the cost of tokens
actually RE-READ and a cache write is not a re-read. The break-even wants the
whole context and now takes `ctx_total`; amortised attribution is untouched.

## 34. The meter was on the status line all along — no token, no request

**Claude Code hands its status line command the vendor's own meter.** The JSON
on the status line's stdin carries `rate_limits.five_hour` and
`rate_limits.seven_day`, each with `used_percentage` and `resets_at` in epoch
seconds ([status line docs](https://code.claude.com/docs/en/statusline)). The
field arrived in **Claude Code 2.1.80** ("Added `rate_limits` field to
statusline scripts", per the changelog). This project began on 2.1.234, so the
documented route existed before any of this was written.

§15 called the endpoint behind `/usage` with the Claude login token from
`~/.claude/.credentials.json`, without checking the documented surface first. §1 had already named the right
instinct about the token, *"don't route around it"*, and the reason now has a
citation. Anthropic's [legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance)
says OAuth is "designed to support ordinary use of Claude Code and other native
Anthropic applications", and that developers "may not collect, store, or
intermediate Claude.ai credentials or session tokens". A public tool that reads
the credentials file is on the wrong side of that line.

### What replaced it

`usage-budget statusline [-- CMD ...]` is a status line command. It records the
two windows to `meter.json`, then runs the user's own status line with the
identical input and passes back its output and exit code. `install.sh` wraps an
existing status line instead of replacing it. The hook path still only reads a
file, so nothing on `UserPromptSubmit` waits on anything. Measured: about 80 ms
per status line run, wrapper included.

The status line brought two problems of its own, and both are answered in code:

1. **A re-run resends old figures.** Claude Code re-runs the status line on
   events that bring no new API response (a permission-mode change, vim mode, a
   `refreshInterval` timer), and each run resends that session's *last* figures.
   Dated by the clock, an idle session's hours-old reading would overwrite a
   fresh one from a session that is working. A reading is therefore dated by its
   transcript's mtime, and an older reading never replaces a newer one, whichever
   session writes last.
2. **A window can reset inside the freshness limit.** A 95% reading taken five
   minutes before a reset describes a window that no longer exists. A window whose
   `resets_at` has passed is dropped from the reading, as Claude Code drops it from
   the status line. The probe had the same hole; it was never closed.

Freshness is otherwise unchanged. The probe refreshed on the Stop hook and the
status line refreshes as each response arrives, so in both designs the first
prompt after an idle gap longer than 15 minutes renders estimated (§28).

### What the status line does not carry

- **Model-scoped weekly caps** (`limits[]` entries of kind `weekly_scoped`, the
  Fable bar). Only the `probe` CLI ever printed them; the injected block never read
  them, so the block loses nothing. The premium bucket is still modelled locally
  (§7).
- **Whether usage credits are on** (`extra_usage.is_enabled`). This one mattered.
  On the maintainer's account credits were on, so the block was entitled to say
  `COST AHEAD (not a lockout)` instead of `WOULD RUN OUT`. It is now a one-time
  declaration, `usage-budget extra-usage on|off`. Unset means unknown, and unknown
  is treated as a wall: a needless warning costs a sentence, a missing one costs
  the rest of the week.

### Two bugs the change surfaced, neither caused by it

Tracing every reader of the meter found two defects already on `main`.

**1. The meter landed after the forecast that reads it.** `_build_state`
computed time-to-exhaustion and the lockout forecast, and only then applied the
meter. Two consequences:

- **The block contradicted itself.** It printed the meter's dollars-left beside an
  hours-to-cap computed from the calibrated remainder. Observed live: the printed
  hours to cap were **11% longer** than the printed remainder and burn rate imply.
- **The wall-or-price check never ran on real data.** It read `state["overage"]`
  before the meter set it, so the demotion of a lockout to a cost note never fired.
  The unit tests set `state["overage"]` by hand and passed.

The meter now lands first, and the declaration is read before the forecast.

**2. An exact reading with no capacity emptied the block.** Uncalibrated, with
nothing spent yet in the window and a fresh meter (the start of every window on
a new install), the 5h line formatted a `None` remainder and raised. `inject`
swallows exceptions so it can never break a prompt, so the result was **no block
at all**. Through the probe this needed the token route *and* zero spend, so it
was rare. Through the status line it is the common case on a fresh install. The
line now prints the exact percentage without dollars.

Both have the shape of §11 and §33: the output stayed plausible (a
self-consistent-looking line, or an empty block that reads as "nothing to say"),
and only a constraint check caught it. Here the constraint was that remainder
divided by burn must equal hours to cap. Both regression tests in
`claude-code/tests/test_meter.py` were mutation-checked. Restoring the old
ordering fails all three ordering tests, and removing the render guard fails the
render test.
