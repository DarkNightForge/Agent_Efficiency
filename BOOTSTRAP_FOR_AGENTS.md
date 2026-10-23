# Bootstrap for agents

You are an AI coding agent installing **usage-budget** for the human you are
working with. This file is addressed to you, not to them. Follow it top to
bottom. It should take about five minutes.

**What you are installing:** a hook that reads the human's local Claude Code
transcripts, works out how much of their subscription window is left, and
injects that as a short block at the top of every prompt — so you can pace
long work instead of discovering the limit by hitting it. The usage
percentages come from the JSON Claude Code already passes to its status line.

---

## Hard rules

⛔ **NEVER INVENT CALIBRATION NUMBERS.** Phase 4 needs real figures from the
human's `/usage` screen. A fabricated number does not produce an error — it
produces a confidently wrong budget on every turn from then on, forever. If you
cannot get real numbers, stop and say so.

⛔ **NEVER HAND-EDIT `settings.json`.** Run `./install.sh`. It backs the file up
first, refuses to clobber hooks it does not own, and validates the JSON before
replacing it. Your hand-edit does none of that, and a malformed `settings.json`
silently disables **every** setting in it.

⛔ **ASK BEFORE YOU START.** This writes to `~/.claude/`, adds two hooks, wires
the status line (wrapping any existing one), and reads transcript history. Say
that in one sentence and get a yes first.

⛔ **DO NOT DECLARE SUCCESS FROM THE TRANSCRIPT.** The install is done when
`usage-budget verify` exits `0` — not when the output looks encouraging.

⛔ **NEVER WORK AROUND A REFUSAL.** If a step is blocked — no permission, a
file it cannot read — report it and stop. Do not find another route.

---

## Phase 1 — check the host

This implementation targets **Claude Code**. Confirm you are in it.

If the human is on **Codex** use `plugins/agent-efficiency/` instead, and on
**Hermes** use `hermes/` — both have their own READMEs. `codex/` is v0.1 and its
hooks do not fire; say so rather than installing it silently.

Also confirm `python3 --version` is 3.9 or newer. There are no other
dependencies — the tool is stdlib-only by design.

## Phase 2 — get the code

```bash
git clone https://github.com/DarkNightForge/Agent_Efficiency.git
cd Agent_Efficiency/claude-code
```

If the human already has a clone, use it and `git pull` instead.

## Phase 3 — install

```bash
./install.sh
```

Idempotent — safe to re-run. It creates the state directory, links a
`usage-budget` CLI shim into `~/.local/bin`, links the skill, wires two
hooks (`UserPromptSubmit → inject` and `Stop → collect`), and wires the status
line to `usage-budget statusline`. If the human already has a status line, it
is wrapped rather than replaced: their command still runs with the same input.
`settings.json` is backed up before any edit.

Read its output. If it prints `SKIPPED` or `ABORT`, relay that to the human
verbatim and stop — those mean it found something of theirs it refused to
overwrite.

## Phase 4 — teach it the capacity

The tool computes spend exactly from transcripts. What it cannot read is the
**size of the window** that spend counts against, because the vendor does not
publish it. It back-solves the size from a usage percentage, which arrives two
ways:

**4a. The status line.** Nothing to run. On Pro and Max plans, Claude Code
passes the 5-hour and weekly percentages to the status line after each reply,
and the status line wiring from Phase 3 records them. A reading older than 15
minutes is not used as exact.

**4b. A calibration, for when that reading is stale.** Ask the human. You
cannot do this part yourself — `/usage` is a user-only command and you cannot
run it. Say this, and wait:

> Please run `/usage` in this session and paste me the two percentages and both
> reset times.

Then feed the numbers back **exactly as given**:

```bash
usage-budget calibrate --pct 13 --resets "9:59pm" \
                       --weekly-pct 26 --weekly-resets "Aug 17 6:59am"
```

Add `--expires "<date>"` if they mention a promotional limit.

**4c. Extra-usage credits.** Ask whether their account has extra-usage credits
turned on. If yes, run `usage-budget extra-usage on`. Do not guess: left unset,
the tool assumes hitting a cap stops them, which is the safe reading.

## Phase 5 — verify

```bash
usage-budget verify
```

**Exit `0` is the finish line.** Anything else prints which check failed and how
to fix it. Fix it, re-run, and do not proceed until it exits `0`.

Use `usage-budget verify --json` if you want to branch on individual checks.

## Phase 6 — hand off

Hooks added mid-session do not take effect until the session restarts. Tell the
human, in plain language:

1. **Restart Claude Code** (or open `/hooks` once) to activate it.
2. From the next session, every prompt carries a `<usage-budget>` block.
3. It is advisory. It never blocks, never cancels, never sends anything
   anywhere. It makes no network calls and reads no credentials.
4. `usage-budget status` shows the current picture any time.

---

## Failure modes

| Symptom | Cause | Do this |
|---|---|---|
| `verify` says `UNCALIBRATED` | Phase 4 incomplete | Redo Phase 4. Do **not** invent numbers. |
| `verify` fails both hook checks | `install.sh` was skipped or run with `--no-hooks` | Re-run `./install.sh` |
| `verify` fails one hook check | Partial wiring | Re-run `./install.sh`; it fills the gap |
| `install.sh` prints `SKIPPED` | A real directory sits where the skill symlink goes | Relay to the human. **Their call**, not yours. |
| `install.sh` prints `ABORT` | `settings.json` is already invalid JSON | Relay it. Do not attempt a repair. |
| No transcripts found | Fresh machine | Expected, and non-blocking. It fills in after the first session. |
| Block never appears | Session not restarted | Phase 6, step 1 |

## What it reads, so you can answer honestly

- `~/.claude/projects/**/*.jsonl` — the human's own transcripts, for token counts
  and timestamps. Read-only.
- `~/.claude/usage-budget/` — its own state. Never commit these; the shipped
  `.gitignore` already blocks them, because transcript paths encode every
  project directory the human has worked in.
- The JSON Claude Code passes to the status line, for the 5-hour and weekly
  percentages and reset times. With an existing status line, the same JSON is
  passed through to it unchanged.
- Network and credentials: none. No telemetry, no analytics, no third party.
