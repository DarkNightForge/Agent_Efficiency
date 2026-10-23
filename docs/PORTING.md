# Porting this to another agent

Want subscription-usage awareness on an agent that isn't Claude Code or Codex —
Cursor, Aider, Cline, a home-grown harness? This is the shape.

Read [`FINDINGS.md`](FINDINGS.md) first for the empirical basis, and
[`../codex/README.md`](../codex/README.md) as a worked example with a verification
playbook you can copy.

## The architecture, in one line

**Compute the numerator exactly from local logs; calibrate the denominator once
from whatever the vendor's UI reports; inject the result into every prompt; teach
the model when to act on it.**

> ⚠️ **Porters: the first half is the one that bit us.** "Compute the numerator
> exactly" hid a **~2.4x** over-count for the project's first four days, because Claude Code logs one
> record per *content block* and every record repeats the whole usage payload
> (FINDINGS §11). Before trusting your host's numerator, find the field that
> identifies one API response and deduplicate on it, then check the result against
> whatever per-conversation token totals the vendor's own UI shows. On Claude that
> check was free and never run.

That decomposition is the entire idea, and it ports to any agent that satisfies two
preconditions.

## Preconditions — check these before writing code

| Requirement | Why it's non-negotiable |
|---|---|
| The agent writes **per-turn token usage** to disk (timestamp + input + output at minimum) | This is the numerator. Without it there is nothing to reconstruct and the project does not port. Check this **first** — it takes minutes. |
| The agent has a **pre-prompt hook** that can inject text into context | This is the delivery mechanism. Without it the model never sees the estimate. |

Two more that degrade rather than block:

| Nice to have | If missing |
|---|---|
| A UI reporting percent-used + reset time | No denominator — the tool can only ever report raw spend, honestly labelled `UNCALIBRATED`. Still useful, much less so. |
| A skill / always-loaded instruction surface | The model gets data with no policy. See *The skill is not optional* below. |

## The five pieces

**1. A log reader** → `[(epoch, cost, model), …]`. The only function that must be
written from scratch. Include sub-agent turns; they consume the same quota. Read
append-only with a stored byte offset and never consume past the last newline, or a
turn still being written is lost permanently.

**2. A price table.** Per-model input/output rates plus cache/reasoning multipliers.
These are **weights, not billing** — calibration divides out the scale, so what must
be right is the *ratio between token classes*, not the magnitudes.

**3. Window semantics.** How many windows, how long, and — the one that bit us
*twice* — how boundaries are seeded. On Claude they **chain from the first
message on the account** (any surface, including ones your logs can't see), so
under continuous use they masquerade as a fixed grid, and an anchor taken from
the UI is exact only until the next idle gap. **Verify against a real UI
reading, and re-verify after an idle gap.** See FINDINGS §4c.

**4. A prompt hook** that emits the block on stdout, exits 0, and **swallows every
error**. A budget tool must never block a prompt.

**5. A skill or equivalent policy surface.** See below.

## The shared core — copy, don't rewrite

All of this is provider-independent and lives in
[`../claude-code/usage_budget.py`](../claude-code/usage_budget.py):

anchored window bucketing (with the anchor-decay caveat of FINDINGS §4c) ·
`capacity = cost / (pct/100)` · median across
samples · confidence from sample spread · auto-expiring samples for promotional
limits · burn rate → time-to-cap → **lockout duration**
(`blocked = time_to_reset - time_to_cap`, buckets ranked by it) · a duty-cycle-aware
rate for long windows (week-scale buckets project on the window average, only the
5h window uses the trailing rate) · incremental byte-offset
scanning with a bytes prefilter · anchors-before-pairing calibration ordering.

⚠️ Do **not** port a "which arrives first, the cap or the reset?" comparison. That
was the original design here and it shipped a real behavioural bug — see
[FINDINGS §9](FINDINGS.md) for the measured harm. Hitting a cap costs exactly the
wait until the reset, so a cap landing shortly before a reset is nearly free and
must not be reported as a constraint.

The [test suite](../claude-code/tests/) is worth porting too — it encodes each
finding as an assertion, including two bugs that shipped before being caught.

## The skill is not optional

This is the piece easiest to skip and most likely to make the difference.

Injecting numbers into context does **not** change model behavior on its own. In the
session that built the Claude Code implementation, the block was present on every
turn and the model never once acted on it — because headroom was ample and a single
"how to use" line is weak instruction competing with a large context.

So each implementation needs a policy surface telling the model **when to speak and
when to stay quiet**. The Claude Code version is
[`../claude-code/skill/usage-budget/SKILL.md`](../claude-code/skill/usage-budget/SKILL.md);
port its policy, not just its prose. The load-bearing parts:

- **Default to silence.** Ample headroom is the normal case; narrating it is noise.
  A 5-hour window on Max holds ~2,700 ordinary turns — warn constantly and you get
  ignored exactly when it matters.
- **Speak for the few real cases:** a lockout long enough to be worth reshaping the
  work around (not merely a cap that lands before a reset), an unusually
  expensive fan-out (single turns have been measured at ~14x the median turn),
  weekly pace drifting off, or the user asking.
- **Inform, never gate.** Offer an option, then do what the user asked. The budget
  is theirs to spend.
- **Hedge every figure**, and when uncalibrated, don't reason about percentages at
  all — there is no denominator.

## Keep the output format aligned

Emit the same block shape so a user running two agents reads one format, and so the
numbers are comparable:

```
<usage-budget>
5h window: ~25% used ($150.00 of ~$600.00 weighted) · ~$450.00 left · resets 22:00 (in 20m)
Burn: $56.00/h (last 30m) → 5h window caps in ~8h 02m, resets in 20m
Week: ~28% used ($1156.21 of ~$4126.95) · ~$2970.74 left · resets Mon 07:00 (in 105h 20m)
Confidence: low (1 calibration). …
How to use: …
</usage-budget>
```

Non-negotiables: `~` on every calibrated figure, a loud `UNCALIBRATED` when capacity
is unknown, a lockout forecast rather than bare percentages, and a how-to-use
line — otherwise the data sits in context unused. When a cap lands close enough to
its reset that the wait is trivial, say so in as many words; silence leaves the
agent to conclude "constrained" and quietly cut scope.

## Suggested CLI parity

```
<tool> status [--json] | collect [--full] | inject | reset
<tool> calibrate --pct N [--resets "…"] [--weekly-pct M] [--weekly-resets "…"] [--expires "…"]
```

## Contributing a port

Open a PR adding `<agent-name>/` alongside `claude-code/` and `codex/`, and add a
column to the comparison table in FINDINGS. The two existing ports do not share a
layout: `claude-code/` holds its own implementation, while the Codex port keeps its
docs and tests in `codex/` and ships the installable code from
`plugins/agent-efficiency/` because its host expects a plugin directory. Follow
whichever your host requires and say which you chose — the shared artifact is
FINDINGS, not the directory shape. **Negative results are wanted** — *"this
agent logs no per-turn usage"* is a real finding that saves the next person the same
investigation.
