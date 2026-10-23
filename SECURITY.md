# Security

This tool reads your local coding-agent transcripts and the usage figures your
coding agent already hands its status line. That deserves a plain answer to
"what does it actually do with my data," so the sections below are written
against the code, and each claim names the file you can check it in.

## Reporting a vulnerability

Open a [security advisory](https://github.com/DarkNightForge/Agent_Efficiency/security/advisories/new)
rather than a public issue. If advisories are unavailable to you, open an issue
saying only that you have a security report and asking for a contact — no
details in the issue text.

Expect an acknowledgement within a week. This is a small project maintained by
volunteers; there is no bounty and no formal SLA, and pretending otherwise would
be its own kind of dishonesty.

## What it reads

| Path | Why | Mode |
|---|---|---|
| `~/.claude/projects/**/*.jsonl` | token counts and timestamps per turn | read-only |
| status line JSON, on stdin | `rate_limits`: the vendor's own utilization and reset times | read; the input is passed on unchanged to your own status line |
| the session transcript's mtime | to date a status line reading | `stat` only, never opened for this |
| `~/.claude/usage-budget/` | its own derived state | read/write |
| `~/.claude/settings.json` | to wire and verify hooks and the status line | read; written **only** by `install.sh` |

Transcripts are parsed for three things and no others: the per-turn `usage`
block, the timestamp, and the **names** of `tool_use` blocks (`Bash`, `Edit`, …)
so spend can be attributed per tool. Message text is never read, and neither are
tool *inputs* — the parser takes `b["name"]` from a `tool_use` block and nothing
else. See the transcript loop in
[`claude-code/usage_budget.py`](claude-code/usage_budget.py).

Tool names are still information about you: they reveal what kinds of operation
you run, though not on what. Nothing is transmitted either way — see below.

## What leaves your machine

**Nothing.** No part of this repository opens a socket:

- **`claude-code/`** makes **no network calls**. The vendor's usage meter
  arrives from Claude Code itself, as `rate_limits` in the JSON it pipes to
  your status line; see `record_statusline()` in
  [`claude-code/usage_budget.py`](claude-code/usage_budget.py).

- **`plugins/` (Codex)** makes **no network calls at all**. Its "telemetry" is a
  local store on your disk, despite the name.
- **`hermes/`** never fetches anything itself; it receives Hermes's own
  `fetch_account_usage` as an injected dependency and reads the result.

There is no analytics, no crash reporting, no version check, and no third-party
endpoint anywhere in the codebase.

## Credentials

**None.** The Claude Code implementation reads no token, no credentials file
and no environment secret. Your Claude login stays with Claude Code, which
is where Anthropic's terms keep it
([legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)).

Prompt injection runs offline against already-recorded state, so nothing on the
hook path can stall your turns.

## Data that must not be committed

The shipped `.gitignore` blocks `cache.json`, `config.json`, and `state.json` —
not as tidiness but as a privacy control. **`cache.json` holds one entry per
transcript file, and those paths encode the name of every project, client, and
job directory you have worked in.** `config.json` holds your calibration
samples, i.e. your real usage levels.

If you fork this repo, keep those rules. If you file a bug, redact paths before
pasting `status` or `--json` output.

## Threat model, honestly stated

**In scope:** unintended egress; credential mishandling; the tool writing
outside its own state directory; `install.sh` damaging `settings.json`; a
malformed transcript crashing a hook and breaking your session.

**Out of scope:** an attacker who already has read access to your home directory
— they can read the transcripts and credentials directly, without this tool.

**A property worth naming:** the injected block becomes model context, so it is
untrusted input to your agent in the same way any tool output is. It is derived
from token counts, timestamps, and tool names — never from message text or tool
inputs, which is what stops a hostile string in some file you once read from
reaching the block. The block is advisory in any case — it never blocks,
cancels, or executes anything.

## Hooks

`install.sh` is the only thing that writes `settings.json`. It backs the file up
first, appends only to matcher-less groups, refuses to overwrite hooks it does
not own, and validates the JSON before replacing it — an invalid `settings.json`
silently disables every setting in it. An existing status line is wrapped, not
replaced: your command still runs, with the same input, and its output and exit
code pass through. Verify what got wired with `usage-budget verify`.

## Supported versions

`main` only. This is a research-driven tool that tracks a moving host; there are
no maintained release branches and no backports.
