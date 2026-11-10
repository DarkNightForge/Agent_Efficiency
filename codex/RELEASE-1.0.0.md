# Codex v1.0.0 release record

Owner: @Eclipse1231317. Prepared November 9, 2026.

| Item | Current state |
| --- | --- |
| Release scope | Codex plugin and its companion documentation. Other providers retain their versions and ownership. |
| Branch | `robbie/codex-v1-release` |
| Upstream base | `7fdf0611cb684d5eb2bedbbfa608e9a4eab4d989`, merged PR #8 |
| Package and core version | `1.0.0` |
| GitHub release | [`v1.0.0`](https://github.com/DarkNightForge/Agent_Efficiency/releases/tag/v1.0.0) |
| Package inventory | [ACCEPTANCE-CANDIDATE.json](ACCEPTANCE-CANDIDATE.json) |
| Local validation | Three provider suites, 65 widget checks, 12 Codex policy fixtures and nine verifier checks passed. Seven platform skips. |
| Installed package | `1.0.0`: 17 payload hashes, isolated configured launcher, 13 tools, annotations, UI resource and nine installed verifier checks passed. |
| Final Desktop smoke test | Functional checks passed; tester confirmed Balanced save and Estimates updated feedback. Exact Throughput handoff, automatic completion, scoped tokens and subsequent Balanced passed. |
| Shared README/security wording | Proposed for Eashan in issue #6. The shared files remain at the upstream base. |

## Release overview

Agent Efficiency for Codex provides an interactive picker for Throughput,
Balanced and Stretch. A picker selection applies to one follow-up task.
A separate action saves the default for future turns.

Task records keep elapsed time, tool counts, per-model-call token measurements
and forecast outcomes when comparable forecast data is available. Verified child
measurements stay separate from their parent. The scanner handles partial and
oversized records, validates correlation, and invalidates measurements affected
by observed rollout truncation.

The plugin confines its managed writes to dedicated state using ownership
checks, an allowlist and fixed storage ceilings. Allowance snapshots come from
authenticated reads delegated to Codex. Hooks and the MCP worker may refresh
automatically. The privacy notice describes rollout metadata reads, retained
pseudonymous references, retention and telemetry controls.
[Codex privacy notice](PRIVACY.md)

## Installation and rollback

Use [INSTALL-AND-ROLLBACK.md](INSTALL-AND-ROLLBACK.md).
This lifecycle-hook build uses manual Codex Desktop installation through a local
marketplace. Follow the supported host controls when reviewing hooks.
[OpenAI plugin packaging](https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks)

## What changed after the verified developer build

The release preparation changes the core, manifest and widget version labels to
`1.0.0`, updates version-dependent fixtures, and regenerates the payload inventory.
The widget's resource URI includes a hash of its packaged HTML. The smoke
checks below tied the installed package to the reviewed source inventory.

The developer candidate `0.5.0+codex.20261103233201` passed the recorded Desktop
picker, one-time Throughput, default-save, Recalculate-feedback, parent/child
completion, independent token-accounting and idle-worker checks. All ten hooks
were reported trusted by the tester. PR #8's four Codex CI jobs passed after the
Windows short-path assertion fix. The prior local three-suite run had 659 passes
and seven platform skips. These are baseline results for the earlier package.
[PR #8](https://github.com/DarkNightForge/Agent_Efficiency/pull/8) ·
[CI run](https://github.com/DarkNightForge/Agent_Efficiency/actions/runs/41913463465)

## Release wording and remaining decisions

Issue #6 carries the narrow shared README/security wording proposal. Its
correction distinguishes provider boundaries, local state and authenticated
allowance requests. The user chose to keep the patch proposed for Eashan.
[codex proposal](SHARED-SECURITY.patch) ·
[Issue #6](https://github.com/DarkNightForge/Agent_Efficiency/issues/6)

For the release package, CLI configuration reported the expected installed MCP
command and working directory, and live module/server/hook fingerprints matched
the 17-file inventory. Development-chat Git verification closed the test chat's
access blocker. Independent process inspection then found installed `1.0.0` MCP
servers with the Desktop → Codex → Git → shell → Python ancestor chain. It did
not bind a process ID to the exact test chat connection. The release evidence
records this limit; it does not claim per-connection process attestation.

The historical Windows permission prompt has an unresolved cause. The user
previously chose to proceed after it was not reproduced and asked to keep a
support report as an optional reminder in future plans. No unexpected sensitive
prompt was encountered in this release smoke run. This does not establish the
historical prompt's cause. Preserve and report any recurrence through the
user-initiated support path.

Live compaction, SessionEnd and truncation were outside the developer Desktop
run. Their synthetic coverage remains separate. Recalculate feedback was
confirmed by the tester. A fresh allowance capture attributable specifically to
that click was not observed. Exact independent nested tool-call counting also
remains unobserved; counter snapshots were consistent. Earlier explicit refresh
and idle-worker checks retain their separate baseline evidence.

## Authorized release execution

The user authorized steps 4 and 5 on November 9, 2026. The exact runtime commit
`501d63f2f9ee60f89db244b90264b80e9bda7141` passed the local suites and installed
package checks. See [VALIDATION-1.0.0.md](VALIDATION-1.0.0.md). Later readiness-only
documentation updates do not change its 17-file payload.

The [release smoke test](RELEASE-SMOKE-TEST.md) completed against source commit
`b43806d9abd1305d90c105c2518ac9199ec96b43`. Its functional checks passed, with
the evidence limits above preserved. Private raw reports remain local; public
results contain no account usage or task/session identifiers.

The release workflow verifies CI on [PR #9](https://github.com/DarkNightForge/Agent_Efficiency/pull/9)
and checks the merged payload against the validated package before tagging.
