# Picker and child-linkage follow-up

Historical repair evidence. Current release preparation is in
[RELEASE-1.0.0.md](RELEASE-1.0.0.md).

## November 3, 2026 candidate

The prior installed Desktop retest confirmed a usable picker, save and
recalculate controls, automatic app follow-ups, child registration, and parent
completion. Throughput reached the app follow-up but the prompt hook recorded
Balanced and left the one-time token pending. The child Stop hook reported
`identity-mismatch` and the child remained active.

The scoped rollout shows the app-message placeholder in the prompt and the
token in the later app input. For that host path the prompt hook provides a
short-lived receipt. The follow-up presents the app token and that receipt to
`select_task_mode`, which validates both, consumes them once and applies the
selection to that exact turn. Hosts that pass the token in the prompt retain
direct hook consumption. No conversation scan is used to recover the token.

Codex documents a parent-scoped `SubagentStop` with a separate
`agent_transcript_path`. The new handler verifies the parent and child headers
and the recorded parent or child turn before completing the unique child
measurement. It reads child tokens from the child transcript and preserves
legacy child-scoped Stop support. Regressions for both failures fail against the
previous runtime and pass with the repairs. Desktop retests of
`0.5.0+codex.20261103233201` confirmed the different-mode handoff, separate
child completion, and worker refresh. See [PUBLIC-REVIEW.md](PUBLIC-REVIEW.md)
for the consolidated evidence and remaining Desktop process inspection.

[Codex hook input fields](https://learn.chatgpt.com/docs/hooks#subagentstop)

## November 2, 2026 candidate history

Owner: @Eclipse1231317. Prepared November 2, 2026 (local date).
Candidate: `0.5.0+codex.20261103023954`. Source baseline: `88c2627`.

The preceding Desktop test verified state visibility, worker-attributed refresh,
automatic completion, parent per-call accounting, preferences and privacy. It
still failed a usable picker and child registration. Private test reports and
working-volume figures remain outside the repository.

## Confirmed causes addressed

The UI's shared handshake sent core MCP's `clientInfo`, but the normative MCP
Apps request requires `appInfo`. The previous simulated host replied without
checking required fields. Its new validation rejected the old implementation
before the field correction, and accepts the corrected initialization. Shared
negotiation is now attempted even when native compatibility aliases exist;
supported shared methods take priority, with bounded legacy fallback. Final
output updates preserve an existing presentation selection without saving a
default or issuing another action. The UI resource URI now includes an HTML
content digest, preventing ambiguous template identity across candidate changes.
This does not prove caching caused the prior failure or that Desktop hosting
is fixed; actual visible interaction still requires observation.

Read-only inspection of this test's own child header established that its session
and thread IDs differ; the parent's IDs happened to match. The earlier equality
guards and fixtures therefore excluded a valid child. Header identity handling
now accepts only explicitly bound session/thread identities and verifies any
supplied agent/thread identifier against that same header. Parent linkage uses
the canonical hashed thread reference and rejects missing, conflicting or
ambiguous relationships. No latest-parent guess or unrelated-session search is
introduced. Child records keep their hook session reference and a verified
thread alias; active/history reconciliation uses that alias without mixing
parent/child tokens. Parent-context events cannot complete or relabel a root
record. Legacy records without a verified alias retain their prior identity.

Subagent diagnostics expose a fixed rejection label and bounded per-label
counts. No raw input, identifiers, paths or conversation content is added to
those outputs. Existing state locations are not merged, moved or deleted.

## Validation

Synthetic tests use disposable homes/config/cache/state, disabled live refresh
and a nonexistent Codex executable. They cover the observed distinct-ID header
shape using invented identifiers, strict handshake rejection, mixed/shared and
legacy bridges, separate child completion, scanner/history aliases, conflicting
bindings, ambiguous records, root-stop protection, and malformed diagnostics.
Final validation: Windows, Python 3.12.14, Node 24.22.1.

| Check | Result |
| --- | --- |
| Claude unit suite | 365 passed |
| Codex unit suite | 273 run: 266 passed, 7 platform skips |
| Hermes unit suite | 26 passed |
| **Python total** | **664 run: 657 passed, 7 skipped, no failures** |
| Widget script tests | 65 passed |
| Codex policy fixtures | 12 passed |
| Repository verifier | 9 checks passed |

The seven skips remain POSIX FIFO/permission and Windows symlink-privilege
cases. No provider-owned source/tests/workflow or timing threshold changed.
The prior Claude timing failure remains historical evidence in open issue 7;
this successful full run does not claim a provider-code correction. The small
permitted AGENTS.md count update must be mentioned in the eventual PR body.

The first strict-handshake regression rejected the old field before its fix.
An old fixed-URI expectation was updated to verify the content digest; the
subsequent full Codex suite passed. Final whitespace/local-link checks and all
17 source payload fingerprints were checked before the local commit.

## Installed verification and retest

The [historical developer inventory](https://github.com/DarkNightForge/Agent_Efficiency/blob/7fdf0611cb684d5eb2bedbbfa608e9a4eab4d989/codex/ACCEPTANCE-CANDIDATE.json) identifies all 17 payload files.
The package was installed through the supported local marketplace CLI and all
17 installed hashes match. Its actual installed launcher also passed an isolated
MCP check for core/server fingerprints, 13-tool contract, content-qualified URI
and returned HTML, using disposable preferences with telemetry/live refresh
disabled. Real preferences and histories were not changed by that isolated
check. The development chat still exposed the prior UI declaration afterward;
installation is not proof of a fresh loaded Desktop connection.

Follow [INSTALLED-ACCEPTANCE.md](INSTALLED-ACCEPTANCE.md) in a fresh, normally
trusted Desktop session after the package update. Verify the live build and
core/server digests, and the content-qualified resource URI. Use the actual
picker buttons; a renderer response alone is not a pass.

For this test's child, use its supported activity metadata and header-verified
thread_ref/parent_thread_ref. Do not assume its session_ref equals its thread
reference. If a check fails, record the fixed linkage reason rather than raw
payloads. Preserve independent host/trust evidence limitations. This source
correction is not a claim that the new installed Desktop acceptance has passed.

Required review/CI/shared-doc gates remain separate. The historical Windows
permission-prompt cause is unresolved; an optional user-initiated support report
remains available and was not reproduced or submitted by this work.

## Primary references

- [OpenAI shared UI bridge guidance](https://developers.openai.com/plugins/build/chatgpt-ui)
- [Normative MCP Apps initialization type](https://github.com/modelcontextprotocol/ext-apps/blob/main/src/spec.types.ts)
- [MCP Apps generated request validator](https://github.com/modelcontextprotocol/ext-apps/blob/main/src/generated/schema.ts)
- [Codex hook dispatch identities](https://github.com/openai/codex/blob/main/codex-rs/core/src/hook_runtime.rs)

These references support field/identity semantics, not independent certification
or approval of this plugin or of the candidate's current-host behavior.
