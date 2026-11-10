# Codex v1.0.0 release preparation

Owner: @Eclipse1231317. Prepared November 9, 2026.

| Item | Current state |
| --- | --- |
| Release scope | Codex plugin and its companion documentation. Other providers retain their versions and ownership. |
| Branch | `robbie/codex-v1-release` |
| Upstream base | `7fdf0611cb684d5eb2bedbbfa608e9a4eab4d989`, merged PR #8 |
| Package and core version | `1.0.0` |
| Proposed GitHub tag | `v1.0.0`; publication authorized after validation |
| Package inventory | [ACCEPTANCE-CANDIDATE.json](ACCEPTANCE-CANDIDATE.json) |
| Local validation | Three provider suites, 65 widget checks, 12 Codex policy fixtures and nine verifier checks passed. Seven platform skips. |
| Installed package | `1.0.0`: 17 payload hashes, isolated configured launcher, 13 tools, annotations, UI resource and nine installed verifier checks passed. |
| Final Desktop smoke test | Pending restart, hook review and real picker/completion checks. |
| Publishing | Authorized after validation; no release or tag created yet. |
| Shared README/security wording | Proposed for Eashan in issue #6. The shared files remain at the upstream base. |

## Release notes draft

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
The widget's content-qualified resource URI will change with its packaged HTML.
The new installation must be tied to this release branch when validation begins.

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

CLI configuration reported the expected MCP command and working directory.
The live module and hook fingerprints matched the developer payload.
Independent inspection of the running Desktop process was unavailable.
Keep that scope distinction in the final release evidence and obtain a
disposition before publishing.

The historical Windows permission prompt has an unresolved cause. A support
report is optional and user-initiated. Final validation should preserve any
unexpected prompt as evidence and stop the affected action. The release
decision must record the owner's disposition of this historical issue.

Live compaction, SessionEnd and truncation were outside the developer Desktop
run. Their synthetic coverage remains separate. Recalculate showed successful
rendering, while the earlier explicit refresh and idle worker provided their
own attributable capture evidence.

## Authorized release execution

The user authorized steps 4 and 5 on November 9, 2026. The exact runtime commit
`501d63f2f9ee60f89db244b90264b80e9bda7141` passed the local suites and installed
package checks. See [VALIDATION-1.0.0.md](VALIDATION-1.0.0.md). Later readiness-only
documentation updates do not change its 17-file payload.

Complete the [release smoke test](RELEASE-SMOKE-TEST.md) and the affected
installed checks in [INSTALLED-ACCEPTANCE.md](INSTALLED-ACCEPTANCE.md).
Record failures and missing evidence with their actual scope.

After validation and release decisions pass, prepare the release PR against
upstream main, re-fetch and rebase before pushing, verify the final commit's CI,
and use the authorized release publication step. Re-check the merged
payload identity if merge changes its contents.
