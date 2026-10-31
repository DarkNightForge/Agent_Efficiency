# Codex public-repository migration

Owner: @Eclipse1231317. Prepared October 26, 2026.

The working branch starts from public main at
`4e7651538bcd3bd4e9ab8c9876c2bd9ba3473c5f`. It carries forward the Codex
security and reliability implementation from the preserved September checkout.
The local branch is `robbie/public-v1-hardening`. The source remains an
unreleased v0.5 candidate; this migration does not rename it to v1 or publish it.

The original checkout and its unpublished files remain intact. No Claude or
Hermes implementation is included in the port. This branch does not resolve
the outstanding Windows permission-prompt investigation or authorize release.

## Preservation and port scope

The previous checkout's HEAD is `7236d49a4a128247c7545beecf71db0cfacf7760`, with
additional uncommitted work. Its Codex-owned files were fingerprinted before
the port, and the original working files remain intact. The public Codex
baseline matches the prior v0.4 source, so no competing public Codex changes
were overwritten. The new public history is the branch's ancestor; private
commits were not merged wholesale or force-pushed into it.

The final preservation check on October 27 matched all 58 original-file
fingerprints. Final scope, local documentation links, whitespace and the
unapplied shared-doc patch were also checked before the local commit.

The transfer considered 44 owned implementation/test/documentation paths;
33 differed before the wording changes. It carries forward the v0.5 state
directory defenses, bounded inputs/processes, runtime validation, private
status inspection, fair rollout scanning, installed-layout checks, reset-aware
forecasting, scoped token accounting, identity eligibility, protocol recovery,
widget timeouts and one-time mode handoff. Source fixes for explicit timestamp
freshness and late token audit/provenance corrections are included.

Private acceptance reports, account data and investigation narratives were not
copied into the public branch. Some regression filenames retain earlier issue
or PR numbers as provenance; those numbers are not references to this public
repository's issue numbering. Claude/Hermes code, their workflows and existing
public findings remain unchanged. The only applied shared-file edit is the
small AGENTS.md test-count/fixture update permitted by its ownership rules;
mention that exception in the eventual PR body.

## Coverage of the earlier Codex findings

These mappings describe source coverage, not issue closure or live acceptance.

| Earlier finding group | Current implementation / tests |
| --- | --- |
| Telemetry opt-out loses mode policy | Prompt handling; `test_agent_efficiency.py`, `test_issue_6_handoff.py` |
| Missing allowance feed | App Server client and polling; `test_issue_5_polling.py` |
| Prompt hook discards headroom | Budget rendering and prompt policy; `test_agent_efficiency.py`, `test_v03.py` |
| Selected mode is not recorded | UI handoff and hook consumption; `test_issue_6_handoff.py`, widget tests |
| Windows import/locking failures | Guarded imports/locks and hook exception shield; Windows source tests. No dedicated injected import-failure test is claimed. |
| Token ingestion refreshes stale quota | Separate capture timestamps; `test_hardening.py` |
| Sparse updates discard windows | Sparse normalization and identity-safe inheritance; `test_hardening.py`, `test_issue_6_identity.py` |
| Anonymous/conflicting identities corrupt samples | Eligibility/provenance checks; `test_issue_6_identity.py` |
| Missing session matches the wrong task | Session-scoped matching; `test_hardening.py` |
| Malformed MCP calls crash/desynchronize | Bounded framing and validation; `test_issue_6_protocol.py` |
| Numeric IDs disable burn calculations | Numeric identity handling; hardening and identity tests |
| Persisted monotonic clock corrupts duration | Wall-clock completion and unavailable nonpositive duration; hardening tests |
| UI hangs or reports errors as success | Bounded requests, cleanup, result validation; `test_control_center.cjs` |
| Missing/fabricated measurements distort scores | Unclassified labels, unavailable measurements, audited residuals and token-only reconciliation; v0.4/calibration, reconciliation and truncation tests |
| Explicit snapshots bypass freshness | Supplied-time validation; `test_issue_6_freshness.py` |

The measurement-loop follow-up also includes per-call counting, parent/child
session separation and no revival of measurements invalidated by source shrink
(`test_issue_5_attribution.py`, `test_issue_5_reconciliation.py`,
`test_issue_5_truncation.py`). The false-lockout follow-up retains all 13
`test_issue_30.py` cases: calendar progress for long windows, current-reset
snapshot spans for short windows, maturity/coherence gates and scoped repeat
suppression. Synthetic tests do not prove actual five-minute polling or every
hook event on the current installed host.

## Wording and privacy review

The review used the official OpenAI plugin guidelines on
[user intent](https://developers.openai.com/plugins/plugin-guidelines#respect-user-intent),
[accurate tool descriptions](https://developers.openai.com/plugins/plugin-guidelines#descriptions-that-match-behavior),
[privacy](https://developers.openai.com/plugins/plugin-guidelines#privacy) and
[tool data boundaries](https://developers.openai.com/plugins/plugin-guidelines#tool-data-handling).
This source review does not constitute OpenAI review or approval of a submission.

Applied Codex-owned changes:

- The skill and injected block describe budget information as advisory. They
  cannot assign it developer priority, override platform/user instructions or
  authorize additional work, spending or access. Broad generic timing triggers
  were removed; headroom alone does not justify more work.
- Tool descriptions disclose authenticated refresh, local state initialization,
  preference overwrites, active-record completion, scanning and history updates.
  Read-only flags now match actual initialization effects; operations that can
  overwrite preferences/measurements carry destructive hints. Annotations do
  not grant permission. `test_tool_effects.py` checks observable initialization
  and preference writes rather than matching descriptive strings.
- UI text labels checks as local-state checks, keeps capacity estimated and
  explains the effects of recalculation, saved defaults and one-task requests.
- [PRIVACY.md](PRIVACY.md), the README and bundled telemetry reference distinguish
  local JSONL parsing from stored content, pseudonymous hashes from anonymity,
  plugin-managed writes from host writes, and history opt-out from offline
  operation. They describe automatic refresh/scanning and retention limits.
  Security-status output explicitly discloses possible delegated/authenticated
  requests and automatic refresh.
- Manifest descriptions state the capability without a blanket security claim
  or implication of OpenAI endorsement. Missing measurements remain unknown;
  current allowance is separate from estimated future task capacity.

### Shared documentation still needs coordination

The root security page still incorrectly says Codex makes no network calls and
describes the hook path as offline. Its ownership is not assigned to Codex;
the root README also requires a claim before editing. Under AGENTS.md those
files have therefore not been changed.

The exact proposed corrections are in [SHARED-SECURITY.patch](SHARED-SECURITY.patch).
They scope the provider claims, document delegated authenticated requests and
automatic triggers, and link the Codex privacy notice. The patch was checked
against the public base without applying it:

```text
git apply --check --unidiff-zero codex/SHARED-SECURITY.patch
```

Coordinate that limited scope with @DarkNightForge before applying the patch.
Recheck it if shared docs change. Coordination was subsequently requested in
[public issue 6](https://github.com/DarkNightForge/Agent_Efficiency/issues/6)
on October 28; see [PUBLIC-REVIEW.md](PUBLIC-REVIEW.md) for access and PR preparation.
The Codex-owned wording review is complete; applying the
shared security correction remains pending and must not be represented as done.

## Validation — October 26, 2026

Windows, Python 3.12.14, Node 24.22.1. Each check ran in a disposable child home,
provider/config/cache/state location with authenticated refresh disabled and a
nonexistent Codex executable. Protocol tests replace their worker; the real
launcher fixture isolates both allowance refresh and ordinary rollout access.
No live account polling, installed activation or real archive scan was used.

| Check | Run | Passed | Skipped | Failures/errors |
| --- | ---: | ---: | ---: | ---: |
| Public Claude unit suite | 365 | 365 | 0 | 0 |
| Migrated Codex unit suite | 253 | 246 | 7 | 0 |
| Public Hermes unit suite | 26 | 26 | 0 | 0 |
| **Python total** | **644** | **637** | **7** | **0** |
| Widget bridge tests | 54 | 54 | 0 | 0 |
| Codex policy fixtures | 12 | 12 | 0 | 0 |
| Repository verifier | 9 | 9 | 0 | 0 |

The seven skips are three POSIX FIFO, one POSIX permission and three Windows
symlink-privilege cases; they are not passes. The skill frontmatter validator
passed using PyYAML 6.0.3 in a temporary validation directory, without adding a
production dependency or modifying global Python packages. The generic scaffold
validator is unavailable in this host's current skill installation; the
repository verifier is not represented as that separate check. Diff, document
link and shared-patch checks supplement the executable suites. Actual CI,
Linux-specific cases and the configured Node 22 runtime remain to be observed.

## Remaining release gates

The migration originally preserved `0.5.0+codex.20260929224250`; acceptance
preparation on October 28 assigns `0.5.0+codex.20261028213407`. This fresh
developer identity does not itself install the candidate or prove parity.
Follow [INSTALLED-ACCEPTANCE.md](INSTALLED-ACCEPTANCE.md) and its payload inventory.

October 30 installed findings are addressed in repair candidate
`0.5.0+codex.20261030235900`; [INSTALLED-REPAIRS.md](INSTALLED-REPAIRS.md)
supersedes the older candidate's current validation/acceptance status.

Before v1: coordinate shared docs, obtain formal review, agree the Windows
permission-prompt disposition, test the exact installed candidate (real MCP/UI
handoff, applicable hooks, polling and scoped attribution), and inspect CI for
the commit proposed for merge. Then update release metadata, perform a clean
installation check and publish only after release approval. Historical research
records need an explicit disposition, not invented implementation or automatic
closure. No release hold is cleared by these source tests.

Current OpenAI guidance excludes lifecycle-hook plugins from the universal
public directory while permitting manual desktop installation. This candidate
continues along the local/repository distribution path; a directory-compatible
design would require a separate scope decision.
