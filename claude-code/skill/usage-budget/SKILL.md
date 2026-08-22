---
name: usage-budget
version: 0.1.0
description: How to read and act on the <usage-budget> block injected into your context each turn — remaining subscription window headroom, burn rate, and time-to-cap. Read this when the block says WOULD RUN OUT or UNCALIBRATED, before starting an expensive fan-out or a long autonomous run, when the user asks how much usage is left, or when they want to calibrate the estimate against /usage. Default posture is silence: the numbers are for pacing, not for narrating.
triggers:
  - "how much usage do I have left"
  - "how much of my window is left"
  - "usage budget"
  - "calibrate usage"
  - "am I going to hit the limit"
  - "WOULD RUN OUT"
  - "UNCALIBRATED"
  - "can we afford this task"
mutating: true
---

# usage-budget

A `<usage-budget>` block is injected into your context on every turn by a
`UserPromptSubmit` hook. It estimates how much of the user's subscription usage
window remains. This skill is the policy for what to do about it.

## The two most important rules

**1. Default to silence about the numbers.** Ample headroom is the normal case
and narrating it is noise. A 5-hour window holds *thousands* of turns of ordinary
work — you will almost never approach it. A tool that warns constantly gets
ignored precisely when it matters. Speak only in the situations listed under
*When to speak*.

**2. Never be silently thrifty.** Silence about the numbers is not the same as
economising. Unspent allowance **expires at the reset and buys nothing** — a
subscription is not a metered bill, so headroom you decline to use is a worse
answer the user already paid for. When the block says `GO WIDE`, take the more
thorough path *without discussing it*: more parallel subagents, wider
verification, deeper search.

These are not in tension. Rule 1 governs what you **say**; rule 2 governs what you
**build**. The failure modes are symmetric and both are real: running the week dry
on Tuesday, and handing back a shallow answer on Monday with 96% of the allowance
unspent.

## Reading the block

```
5h window: ~25% used ($150.00 of ~$600.00 weighted) · ~$450.00 left · resets 22:00 (in 20m)
Burn: $56.00/h (last 26m) → 5h window caps in ~8h 02m, resets in 20m
Week: ~28% used ($840.00 of ~$3000.00) · ~$2160.00 left · resets Mon 07:00 (in 105h 20m)
Confidence: low (1 calibration).
```

| Field | Meaning |
|---|---|
| `~N% used` | Estimated fraction of the window consumed. The `~` is not decoration — see *Honesty* below. |
| `$X of ~$Y weighted` | List-price-**equivalent** units, not money owed. A unit of account on a subscription. |
| `resets HH:MM` | When the window rolls over and headroom returns to full. |
| `Burn: $N/h (last …)` | Trailing rate over the span the parenthesis names — spend since the sample's first turn, over at most 30 minutes. The span is printed because it varies: early in a window there may be only minutes of it. |
| `— extrapolated` / `— thin` | The sample is under 5 minutes (floored) or under 10 minutes / 3 turns. Treat `caps in` as an order of magnitude, not a forecast; a lockout on a sample this thin is demoted to `Heads-up:` and will not lead the block. Common on the first turns after a resume, whose cache-write cost is a one-off (§35). |
| `caps in … , resets in …` | Informational. A cap only matters as far as it precedes the reset. |
| `Confidence` | `none` / `low` / `medium` / `high`, from agreement between calibration samples. |
| `boundary ESTIMATED` | Appears only when the window boundary was re-derived after an idle gap. It may have opened earlier than shown, so the reset may be sooner. Suggest a re-anchor. |
| `WOULD RUN OUT: <bucket> … BLOCKED ~Nh` | The only line that asks you to change plan. `N` is how long you would sit blocked. |
| `Note: … NOT a reason to cut scope` | A cap is coming but the reset lands right behind it. Carry on. |
| `Week pace: day N/7` | The week's allowance prorated across its days. A flat weekly percentage cannot tell "on pace" from "locks out Wednesday", because it never says how much of the week went with it. |
| `Affordance: ~N more turns … ~M fit` | Headroom converted into the units you actually plan in. `M` is how many nominal 16-agent fan-outs fit. This is the line to read before choosing an approach. |
| `BORROWING from later days` | Past today's share, drawing on the rest of the week. Not a stop — real capacity is on the same line. |
| `GO WIDE` | Affirmative permission. Room is ample *and* the week is under pace. Build the thorough version. |
| `ASK BEFORE PROCEEDING` | The one line that wants a decision from the user, not from you. See *When to speak* #5. |
| `EXACT` / `Meter: read from the status line …` | A fresh reading of the vendor's own meter is in play. See *When the meter is live* below. |
| `COST AHEAD (not a lockout)` | Credits cover this cap. Nobody gets blocked; it starts costing money. |
| `Posture: …` | The user's stored preference for how much the budget should shape the work. |
| `Premium week …` | Premium-model turns draw on a second, smaller weekly cap as well as the shared one. Appears only when it is being used. |

(Figures in the example are illustrative, not measured.)

**Read the lockout, not the cap.** Running out of an allowance costs you exactly
one thing: the wait until it resets. Running out ten minutes before a window rolls
over costs ten minutes, and the next window arrives with a **full** allowance —
that is not worth reshaping work over, and cutting scope to avoid it is strictly
worse than just continuing. Running the *week* dry is a different matter: that
blocks you for days.

So the block reports **how long you would be blocked**, and only asks you to act
when that number is large enough to matter. If it is not telling you to act, do
not act.

## When the meter is live

If the block carries `EXACT` and a `Meter: read from the status line …` line, the
tool has read the vendor's own meter, which Claude Code hands its status line,
rather than reconstructing it (FINDINGS §15, §34). That changes what you are
allowed to claim:

| | Without a fresh reading | With a fresh reading |
|---|---|---|
| Percentages | hedge: "roughly 22%" | **state as fact: "22%"** |
| Reset times | may be stale; suggest re-anchoring | **exact — never suggest re-anchoring** |
| Dollar figures | estimates | **still derived** — capacity is inferred from utilization |
| `/usage` | authoritative, defer to it | the same vendor figure, so no need to send the user there |

Two things do **not** change. Dollars stay approximate — say "about $190 left",
never "$191.98". And the `Price-weighting is off by …%` figure is a diagnostic
about the *estimator*, not headroom; do not report it to the user unless they ask
about accuracy.

If the `Meter:` line is absent, the reading is stale (it expires after 15
minutes, so the first prompt after a break is always without one) or there is
none, and every earlier rule applies unchanged — including hedging and the
calibration caveat.

## Posture, and when a cap is not a wall

Two things decide how much the budget should shape your work, and they are
different in kind.

**Whether a cap blocks you at all is a FACT, and the user declares it once.**
With usage credits enabled, hitting a limit is a price change, not a lockout —
so once the user has run `usage-budget extra-usage on`, the block says
`COST AHEAD (not a lockout)` instead of `WOULD RUN OUT`. Never tell someone they
will be blocked when that line is present; they will not be. Mention the cost
once if the task is large, then carry on. If the user says they have usage
credits and the block still says `WOULD RUN OUT`, the declaration is missing:
tell them that one command fixes it, rather than arguing with the block.

**How hard to economise is the USER'S CHOICE, and it is stored, not asked.**

| Block says | What it means for you |
|---|---|
| `Posture: THROUGHPUT` | They are buying speed. Do not pace, do not economise, do not raise usage unless a hard wall is imminent. Take the most thorough approach available every time. |
| *(no posture line)* | Balanced — the default. Everything else in this document applies as written. |
| `Posture: STRETCH` | They are rationing across the week. Prefer the cheaper path where quality allows, and check in earlier than usual before expensive work. |

Never ask which posture they want in the middle of a task — it is set with
`usage-budget posture <name>` and persists. If they say something like "stop
worrying about cost" or "make it last the week", tell them that command exists
rather than adopting the posture for one turn and forgetting it.

## Planning with the affordance line

Before choosing *how* to build something — not after — read `Affordance:` and
size the approach to it. It is stated in turns and fan-outs precisely so no unit
conversion is needed.

| Block says | Build like this |
|---|---|
| `GO WIDE` | The thorough version. Parallel subagents, multiple independent verifiers, broad search. Do not ask permission and do not mention the budget. |
| No `GO WIDE`, several fan-outs fit | Normal judgement. One fan-out where it genuinely helps. |
| `~0-1 fit` | Sequential work. Reach for a fan-out only if the task truly needs it. |
| `BORROWING from later days` | Proceed, but prefer the cheaper path where it costs nothing in quality. The capacity is real; it is tomorrow's. |

The `Burn:` rate is computed from **completed** turns, so it cannot see a fan-out
you are about to launch (FINDINGS 12). `Affordance:` is the forward-looking
number — use it for the decision, not the burn rate.

## Quoting a read before you take it

The block tells you what is left. `quote` tells you what a specific plan will
*cost*, before you propose it — so the user approves a number instead of a vibe.

**When a plan involves reading a set of files, quote it first:**

```bash
python3 ~/.claude/usage-budget/usage_budget.py quote FILE...      # or --stdin
python3 ~/.claude/usage-budget/usage_budget.py quote --stdin --json
```

Put the result in the plan proposal. It exits non-zero when the quote does not
fit the remaining headroom, so you can branch on it; when it does not fit, say so
and propose a reduced scope **before** reading anything.

Two things it reports that a raw file size cannot:

- **Landing cost and carry, separately.** A tool result is not paid for once. It
  is re-read as part of the cached prefix on every later turn of the session, so
  a read that looks cheap on arrival is not. Quote the total over the turns you
  expect to still be working, not the landing cost.
- **Whether `Read` will even return the file whole.** Over 256 KB it errors and
  returns nothing; over 25,000 estimated tokens it paginates and hands back page
  one. The quote flags both, so "I will read these twelve files" does not turn
  into six silent partial reads.

Do not quote every read. A quote is for a *plan* — a batch of files, a sweep, a
fan-out — not for opening one file you already know you need. Quoting a single
`Read` costs more attention than it saves.

The estimate is deliberately conservative: it prices at 2.6 bytes/token, measured
against real tokenizer output rather than the familiar 4.0, which under-counts by
about 35%. It should come in slightly high. If it comes in *low*, that is a bug
worth reporting, not a pleasant surprise.

## COMPACT NOW

The only line in the block with a **measured** payback. It appears when this
session's context has grown large enough that compacting pays for itself inside a
working stretch, and it carries the arithmetic:

```
COMPACT NOW: this session's context is 210,056 tokens and is costing $0.0900
EVERY turn more than a compacted one would. `/compact` costs ~$1.16 once and
pays for itself after ~13 more turns.
```

**Act on it, don't narrate it.** If the session plausibly has that many turns
left, run `/compact` (or suggest it, if you are mid-task and compaction would
lose something the user still needs). If the work is nearly done, say nothing and
carry on — paying $0.09/turn for three more turns is cheaper than a $1.16
compaction.

It speaks **once per session**, then only again if the payback materially
improves. If you have seen it and decided not to act, you will not be nagged; do
not treat its absence on later turns as the situation having resolved.

The one-off figure includes **generating** the summary at output price, not just
re-reading the context. That is the term that is easy to omit, and omitting it
roughly halves the break-even and makes compaction look better than it is.

## When to speak

Five cases. Outside them, say nothing about usage.

**0. It appears at most once per situation.** The lockout directive is gated: it
speaks when the forecast is new or materially worse, and stays silent otherwise.
So if you see it, say it — it is not repeating itself, and a turn without it is
not an all-clear.

**1. The block says `WOULD RUN OUT`.** Say so before starting substantial work,
in one sentence, naming the wait and offering a concrete alternative:

> At this burn rate the weekly allowance runs out in about two hours and doesn't
> reset until Monday — so that would block us for two days. This refactor is a few
> hours of work; want me to do the auth module now and the rest after the reset, or
> run it on Sonnet instead?

**Never do this on your own initiative when the block has not flagged it.** A live
session shortened its own plan with **53% of the window still unspent**, because
the old wording said "cap arrives first" ~13 minutes before a reset that would have
handed it a full allowance. Pre-emptive thrift that costs the user capability is a
failure, not caution. The allowance exists to be spent.

**2. Before an unusually expensive operation.** Read the per-turn cost off the block itself — spend divided by turns. (It is effort-specific: at lower effort, output per turn falls and so does the figure.)
The expensive things are fan-outs and long autonomous runs — a multi-agent
workflow spawning 16 subagents, a sweep over hundreds of files, an overnight run.
A single turn has been measured at **~14x the median turn**. If remaining headroom is
under roughly 3x your estimate for the operation, flag it *before* starting rather
than stopping halfway.

**3. The weekly window is the one actually at risk.** The 5-hour window is hard to
exhaust; the weekly one binds sooner. Compare weekly percent used against the
fraction of the week elapsed. At 28% used with 60% of the week left, pace is fine.
At 80% used with half the week left, mention it.


**5b. The block says `AT LIMIT` or `WILL NOT FINISH MUCH`.** These enumerate
options too, and they get the same treatment: **call AskUserQuestion**, do not
write the options out as prose. A list of choices in a paragraph is not
something the user can act on in one step — it asks them to compose a reply
mid-task, at the moment they are least willing to.

The options are already in the directive. Use them verbatim:

- `AT LIMIT` — continue at full scope · reduce scope or a cheaper model · stop
  and resume after the reset.
- `WILL NOT FINISH MUCH` — defer until the reset · a smaller sub-goal landing on
  a durable checkpoint · a cheaper model · proceed with a stopping point agreed
  now.

**Ask once.** Both directives re-render on every turn while the condition holds,
so the tool gates the *question* even though the *facts* keep printing. When the
directive says `do NOT ask again`, it means the user has already chosen this
window — state the number if it moved and carry on. A modal on every turn gets
dismissed reflexively, which is worse than prose because it looks like consent.

**Neither is a refusal.** Nothing is blocked. If they say continue, continue at
full scope.

**4. The user asks.** Then give the numbers plainly. If a `Meter:` line is
present, say the percentages and resets are the vendor's own and drop the
confidence hedging; otherwise include the confidence level and note that `/usage`
is authoritative.

**5. The block says `ASK BEFORE PROCEEDING`.** One of three cases where you
stop and hand the decision back (see also `AT LIMIT` and `WILL NOT FINISH MUCH`
below). Before starting substantial work, call
**AskUserQuestion** with the pace figure, the projected dry-out day, and three
options:

- **Continue at full scope** — spend ahead of pace deliberately.
- **Reduce scope or drop to a cheaper model** — stay near pace.
- **Stop and resume after the reset** — bank the rest of the week.

Rules for this checkpoint:

- **It is a checkpoint, not a refusal.** If they choose continue, continue at
  **full scope** and do not raise it again that day. The allowance is theirs.
- **Ask once.** The gate already suppresses repeats within a day unless the
  overshoot materially worsens — do not re-litigate a decision it let through.
- **Do not ask for small work.** A one-file edit does not warrant a prompt; the
  line exists to gate *substantial* work.
- **Never narrow scope on your own** because you saw the line. Ask, or proceed.

## When NOT to speak

- **Never** when the block has not flagged `WOULD RUN OUT` and the user didn't ask.
- **Never** repeat a warning you already gave this session unless the situation
  materially changed. One mention, then drop it.
- **Never** open a response with usage status. It is context, not the topic.
- **Never** treat it as a reason to refuse work or to silently narrow scope. Inform,
  offer an option, and then **do what the user asks.** The budget is theirs to spend.
- **Never** announce `GO WIDE`. It changes what you build, not what you say. "I
  have plenty of budget so I will use lots of agents" is exactly the narration
  rule 1 forbids.

## Known bias (measured 2026-08-12)

**Do not assume an error direction.** An earlier version of this file said the tool
over-reports, so real headroom was probably larger than displayed. A third
calibration sample on 2026-08-14 **reversed that**: the block read 41% against a
real 47%, meaning real headroom was *smaller* than shown. See
[`docs/FINDINGS.md` §6d](../../../docs/FINDINGS.md).

Current state: two of three samples agree within a couple of percent, one is an
outlier and probably measured under a boundary model since corrected. So treat the
percentage as good to roughly **±10 points**, in *either* direction, and never as a
reason to assume spare room. `/usage` is authoritative — when the answer matters,
ask the user to run it.

## Honesty

These figures are reconstructed from local transcripts and weighted by list price
as a proxy for however the limiter actually scores usage. Therefore:

- Always hedge: "roughly 25% used", never "you have used 25%".
- Never quote the dollar figures as money owed. They are weights.
- Say `/usage` is authoritative when precision matters.
- If `Confidence: none` or the block says **`UNCALIBRATED`**, do not reason about
  percentages *at all* — there is no denominator. Report raw spend if asked, mention
  calibration once, and move on.
- Usage from claude.ai or another machine is invisible to this tool, so estimates
  read **low**. Never claim more headroom than the block shows.

## Calibrating

Needed once, and again after any plan change or when a promotional limit expires.
Only the user can do step 1 — `/usage` is a TUI-only command you cannot run.

1. Ask the user to run `/usage` and report the session percent, the weekly percent,
   and **both reset times**. **A reset ending in `:59` is correct — record it as
   shown.** The raw API returns `HH:MM:59` (FINDINGS §15); the older instruction
   to round up to the hour was based on a refuted rendering-artifact theory.
2. Then:

```bash
usage-budget calibrate --pct 13 --resets "10:00pm" \
                       --weekly-pct 26 --weekly-resets "Aug 17 7:00am"
```

**Always pass the reset times.** They seed the window chain, and they must be
applied before the percentage is paired with a cost — `calibrate` handles that
ordering for you. Anchors decay: sessions chain from the first message on the
*account* (any surface) after the previous window expires, so a stored anchor is
exact only for the window it was read in (FINDINGS §4c). After that the tool
re-derives boundaries and downgrades its own confidence — `tight` while work is
continuous, `estimated` once a real gap intervenes. **When you see `estimated`,
say so and suggest a re-anchor** (`calibrate --resets` needs no percentage): the
boundary decides which turns count as this window, so a stale one corrupts the
spend figure itself, not just the clock.

If premium-model turns are accruing, also read the separate premium bar and pass
`--premium-pct`. It is a smaller cap that can bind while the shared meters still
look fine.

Add `--expires "<date>"` if a promotional limit boost is active, so the sample
self-expires instead of silently over-reporting headroom later.

Confidence only reaches `medium`/`high` with multiple samples taken at **materially
different** usage levels (say 15% and 60%). Two agreeing samples is the first real
evidence the price-weighting proxy holds; one sample cannot distinguish "capacity is
X" from "the weighting shape is wrong."

## Other commands

```bash
usage-budget status              # human-readable, includes recent windows
usage-budget status --json       # machine-readable
usage-budget collect             # rescan transcripts (the Stop hook does this)
usage-budget predict             # token mix for this window + implied /usage %
usage-budget analyze             # where spend went: amortised context cost + compaction break-even
usage-budget reset               # clear calibration history
```

Reach for `analyze` when the user asks *where* the budget went, or wants to
spend less rather than merely know the remaining figure. Roughly **86% of
weighted spend is context handling, not output**, so this is where the money
actually is. It reports two things the other commands cannot:

- **Amortised context cost by caller.** A tool result is not paid for once — it
  is re-read on every subsequent turn of the session as part of the cached
  prefix. So a large result landing early in a long session is far more
  expensive than it looks at the moment it lands, and `analyze` ranks callers by
  that deferred total rather than by call count.
- **Compaction break-even.** How many more turns until clearing the context pays
  for itself, given what a turn currently costs to re-read. Useful when a
  session has grown large and the user is deciding whether to continue in it.

Two things to be careful about when reporting its output. It counts **whole
sessions** that were active in the window, so its total is *not* comparable to
spend inside that window and must never be quoted as one. And every figure is an
**upper bound** — the command prints its own caveats, so pass them on rather than
quoting the number bare.

Reach for `predict` when the user asks *why* a window is burning fast: it splits
the weighted cost into cache-read / output / cache-write, reports thinking share
and per-turn effort, and names the model mix. (Its A-vs-B hypothesis columns are
a closed experiment — see FINDINGS 6d — but the breakdown above them is live.)

## Steering what a compaction keeps

> **NOT REGISTERED BY DEFAULT — removed 2026-08-18.** The subcommand ships and
> works, but the only structurally clean A/B showed no benefit (FINDINGS §29), so
> it is not wired into any hook. It costs ~0.5k tokens on every compaction; do not
> turn it on without evidence that it earns them.

To enable it, add a `PreCompact` hook running:

    python3 ~/.claude/usage-budget/usage_budget.py precompact

An agent **cannot trigger** a compaction — every route is closed, and the host
already compacts on its own (FINDINGS §26). This hook assumes a `PreCompact`
hook's **stdout reaches the summariser** as additional instructions, on both the
auto and manual paths. That is not in the hooks documentation, so it is
unverified, but it is the one lever left, and this uses it.

It matters because compaction keeps a measured median **7.6%** of prior context
(§25) and the summariser picks which 7.6%. The cost of a bad pick is not the lost
tokens, it is **rework**: a dropped measurement gets measured again, a dropped
dead end gets walked again, both at exactly what they cost the first time.

So the hook emits a ranked priority list — measurements and their values, ruled-out
approaches and why, decisions and their grounds, exact identifiers, state of the
work — and tells the summariser what to drop instead: the account of *how* the
work proceeded, keeping *what it established*. On an `auto` trigger it adds a
clause about in-flight state, because an automatic compaction fires mid-task
rather than at a chosen stopping point.

**If you are reading a summary that came through this hook**, that ranking is why
it leads with numbers and dead ends rather than narrative. Trust the figures it
carries as things this session actually measured, and treat a listed dead end as
closed rather than as something to re-check.

**Mechanism undocumented; benefit unverified.** The hook fires (PreCompact is a
documented event), but that its text reaches the summariser is not in the hooks
documentation, so treat it as unverified on any given version. Whether it improves what
survives is *not* established: four A/B instruments were built and all four
measured their own fixtures rather than the hook, and a fifth — the only
structurally clean one — showed **no benefit** at n=1 (FINDINGS §29). Do not cite
this as a saving.

One thing the failed A/B did measure: without a length bound the instruction made
summaries run to the output ceiling every time, which is a recurring cost because
a summary is re-read on every following turn. The instruction now carries an
explicit length-neutrality clause for that reason.

## Troubleshooting

| Symptom | Cause |
|---|---|
| No `<usage-budget>` block at all | Hook not firing. Check `UserPromptSubmit` in `~/.claude/settings.json`; if just added, the user must open `/hooks` once or restart. |
| `UNCALIBRATED` | No calibration sample yet, or the only one expired. Calibrate. |
| Weekly says `Rolling 7d` | No weekly anchor set — the figure spans the wrong days. Pass `--weekly-resets`. |
| `boundary ESTIMATED, may be earlier` in the block | The window was re-derived after an idle gap, so it may have opened earlier than shown and reset sooner. Suggest re-anchoring (`calibrate --resets`) before trusting the window figures — and before calibrating against them. |
| Boundaries `heuristic` in `status` | No session anchor at all. Pass `--resets`; without it, boundary detection is a biased local chain. |
| `BINDING LIMIT is the <bucket>` | A different allowance runs out before the 5h window. Pace against *that* one; the 5h figure is not the constraint. |
| `Premium week … UNCALIBRATED` | Premium-model turns are accruing against a separate smaller cap with no sample for it. Ask for the Fable bar from `/usage`, then `calibrate --premium-pct <N>`. |
| Numbers look far too low | Usage from another machine or claude.ai is invisible. `/usage` wins. |

Project: <https://github.com/DarkNightForge/Agent_Efficiency>
