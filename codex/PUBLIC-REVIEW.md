# Codex public PR preparation

Owner: @Eclipse1231317. Updated November 3, 2026.

Review branch: `robbie/public-v1-hardening`. Tested runtime commit: `0e500fc`.
Installed candidate: `0.5.0+codex.20261103233201`. The
[acceptance inventory](ACCEPTANCE-CANDIDATE.json) identifies its 17 payload files.

GitHub confirmed that @Eclipse1231317 can read the upstream repository and
cannot push to it. No fork at `Eclipse1231317/Agent_Efficiency` was visible.
The upstream repository permits forks. Contribution access and shared
security wording are tracked in
[issue #6](https://github.com/DarkNightForge/Agent_Efficiency/issues/6).

## Draft PR title

Harden Codex plugin state, picker handoffs, and task measurements

## Draft PR body

The Codex plugin now confines writes to dedicated plugin state, validates and
bounds inputs, and describes the data it retains and the allowance requests it
delegates to Codex. The control center applies a one-time mode selection to the
exact follow-up. For example, selecting Throughput applies it to that task
while Balanced stays the saved default. The Stop hook verifies the child
identity and completes its token record separately from the parent's.

The branch also hardens rollout scanning and per-call token accounting,
preserves measurement integrity when rollouts are truncated, and makes the
allowance and forecast limits explicit. Changes are scoped to the Codex
implementation, its tests and CI, and a test-count update in AGENTS.md.
The root README and SECURITY.md proposal remains in
[issue #6](https://github.com/DarkNightForge/Agent_Efficiency/issues/6).

**Validation.** On Windows, the three provider suites ran 666 tests with 659
passing and seven platform skips. The 65 widget checks, 12 Codex policy
fixtures, and nine repository verifier checks passed. The unchanged Claude
timing test exceeded its threshold while suites ran together and passed in
the serial rerun. Its existing tracking issue stays open.
[Issue #7](https://github.com/DarkNightForge/Agent_Efficiency/issues/7)

The installed Desktop candidate matched all 17 payload hashes. Desktop checks
confirmed the Recalculate success message, saving the existing Balanced
default, and a one-time Throughput handoff. The tester confirmed all ten
hooks were trusted. Parent
and child records completed automatically, with independent per-call token
checks and the child counted once. The background worker refreshed during the
idle interval, and a later ordinary prompt used Balanced. The installed MCP
server advertised the expected 13 tools. The candidate worktree was clean
when verified separately from the chat's starting checkout.

**Open review items.** The CLI reported the expected configured MCP launcher
and working directory. Independent inspection of the live Desktop process
remains unavailable. Recalculate rendered estimates, while a fresh allowance
capture was independently verified on an explicit refresh and on the idle
worker path. No fresh capture was attributed to the Recalculate click.
Live compaction, SessionEnd, and truncation were outside the Desktop run.
GitHub CI and formal review are pending. The historical Windows permission
prompt remains unresolved. A support report is optional.

@DarkNightForge, could you review the Codex changes and the remaining Desktop
provenance and shared-document items?

## Publish the draft

Use upstream write access if granted, or an approved fork. Fetch and rebase
on upstream main immediately before pushing. Verify the proposed branch and
test evidence after rebase, then push the review branch and open a draft PR
against upstream main. The draft body asks @DarkNightForge to review it. A
formal GitHub review request requires upstream write access.

Check the Codex workflow on the published commit. The Claude and Hermes
workflows filter by provider paths, so this branch may require a maintainer
to arrange runs if those remote checks are required. Report their actual
status and the separate local suite results.
