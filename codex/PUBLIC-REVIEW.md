# Codex public PR preparation

Owner: @Eclipse1231317. Updated October 28, 2026.

October 30 follow-up: the first installed acceptance did not pass. The repair
candidate is now `0.5.0+codex.20261030235900`; see
[INSTALLED-REPAIRS.md](INSTALLED-REPAIRS.md) for the actual changes, validation
and remaining Desktop checks. This supersedes the earlier build identity below.
Current Codex validation passes; the unchanged Claude suite has a reproducible
local performance-threshold failure. Do not use the earlier all-green paragraph
as evidence that all current suites pass, or submit/merge with that blocker hidden.

Public main remains `4e7651538bcd3bd4e9ab8c9876c2bd9ba3473c5f` after a fresh
fetch. `robbie/public-v1-hardening` rebased cleanly; it was already current.
The migration commit is `a1807d38e37218994a1cad3670b6e4f95e14edf7`.
The acceptance preparation gives the manifest a distinct developer version:
`0.5.0+codex.20261028213407`. Runtime code is unchanged by that identity update.

The authenticated contributor has read access but no push access, and no pending
invitation for this repository. No branch or fork was published. Contribution
access and shared-document coordination were requested from @DarkNightForge in
[public issue 6](https://github.com/DarkNightForge/Agent_Efficiency/issues/6),
with the complete proposed patch. The root README and SECURITY.md remain
unchanged pending coordination. No reply or approval is assumed.

The installed acceptance procedure is in
[INSTALLED-ACCEPTANCE.md](INSTALLED-ACCEPTANCE.md). Its inventory records the
exact local payload; these instructions are preparation, not an acceptance pass.

## Ready-to-use PR title

Port Codex hardening and reliability fixes; clarify data access and privacy

## Ready-to-use PR body

The public Codex implementation was still at v0.4 while reviewed and unpublished
Codex fixes remained in the earlier checkout. This ports that implementation
onto public main without importing private history or acceptance reports. It
adds dedicated-state ownership checks, bounded I/O/processes, validated MCP
inputs, fair scanning, reset-aware forecasts, identity-safe measurements,
protocol/widget recovery, one-time mode handoff, and audited per-call accounting
with truncation quarantine and late-correction handling.

The wording review removes self-assigned instruction priority and concealed
spending prompts, narrows skill triggering, and corrects tool effect annotations.
The Codex privacy notice describes retained pseudonymous metadata, delegated
authenticated allowance requests, automatic refresh/scanning, retention and
telemetry opt-out. The developer candidate has a distinct installation version;
this is not a v1 release or a claim of OpenAI approval.

Ownership: Claude/Hermes implementations and workflows, findings history and
shared root security documents are unchanged. The small permitted AGENTS.md edit
updates the test-count/fixture inventory. Shared security corrections are
proposed separately in issue 6 and remain pending owner coordination.

Validation on Windows: 644 Python unit tests run, 637 passed and 7 platform
skips; 54 widget checks, 12 Codex policy fixtures and 9 repository checks passed.
The skips cover POSIX FIFO/permissions and Windows symlink privileges. The
candidate identity/packaging checks are recorded separately after preparation.
Linux/Node 22 CI and exact installed acceptance remain pending; update this
paragraph with their actual results before opening the PR.

Candidate preparation on October 28: all 16 release/layout tests and all 9
repository verifier checks passed with live refresh disabled in disposable
homes/state. The initial deep Windows temporary path exceeded the path limit;
the verifier suite passed in a shorter temporary location. The standalone CLI
check used normal script-directory imports after `-I` excluded sibling modules.
Neither harness correction changed runtime code. Payload inventory parity and
local-document checks were also verified. These are not installed-host results.

Release gates: formal review, shared-doc disposition, the Windows permission-
prompt disposition, installed source/payload parity and host behavior, then CI
for the proposed merge commit and final release metadata. Source tests do not
close private issues or clear those gates.

Distribution: this hook-containing build is for manual desktop installation and
repository distribution. Current OpenAI guidance excludes lifecycle-hook plugins
from the universal public directory. A directory-compatible build would require
a separately approved design decision; it is not part of this port.

## Before publication

Resolve contribution access or an authorized fork workflow. Re-fetch and rebase
on public main immediately before any push. Recheck the shared patch against
that base and obtain coordination before applying it. Inspect all three suites
and relevant CI for the final proposed commit. Update the draft validation
paragraph with actual acceptance/CI results; do not mark pending checks passed.

Official packaging basis:
[Package your plugin](https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks).
