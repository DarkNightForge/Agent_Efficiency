# Installed-host repair cycle

Historical repair evidence. Current release preparation is in
[RELEASE-1.0.0.md](RELEASE-1.0.0.md).

Owner: @Eclipse1231317. Prepared October 30, 2026.
Candidate: `0.5.0+codex.20261030235900`. Source baseline: `874043d`.

Historical repair record. The subsequent Desktop test passed state visibility
and worker refresh but still failed picker and child registration. Current
work and validation are in [INTEGRATION-FOLLOWUP.md](INTEGRATION-FOLLOWUP.md).

The prior exact installation passed payload identity and parent token accounting
but failed visible-picker persistence. It also exposed separate state visibility,
zero parent subagent counts, missing worker attribution and an undersized idle
interval. Those results do not pass this repair candidate's Desktop acceptance.
Private reports and account measurements are not copied into this repository.

## Changes and their limits

- State selection honors the explicit override, then host PLUGIN_DATA. The legacy
  MCP config forwards those named variables. Without either, only a verified
  `plugins/cache/<marketplace>/agent-efficiency/<full-version>` package may derive
  the host's corresponding `plugins/data/` directory. Manifest/layout mismatch
  fails closed. Standalone behavior remains unchanged. No directory is copied,
  merged, repaired or removed; existing marker/link/size checks remain in force.
- The widget performs the MCP Apps initialization/initialized handshake and
  advertises its inline presentation capability. It sends size notifications,
  accepts finalized native tool-output updates, and can restore only an ephemeral
  selected mode after a native remount. It retains request timeouts and cleanup.
  This repairs protocol/lifecycle gaps; actual Desktop persistence must still
  be observed, and host behavior is not claimed fixed from a fake-DOM test.
- The current official runtime emits SubagentStart in the child context. The
  old handler instead tried to find a parent task using that child identity.
  A bounded read of the designated child header now verifies the explicit parent
  link. Exactly one active parent is required; duplicate start/stop observations
  do not increment its count twice. Child lifecycle records and per-call tokens
  remain separate, and parent-context compatibility events cannot relabel a parent
  as a child. Missing/conflicting metadata remains unmatched, not guessed.
- Fixed refresh-caller labels preserve separate worker/hook/UI/tool timestamps.
  Later prompt refreshes no longer erase the new caller-specific worker evidence.
  Security status adds bounded aggregate hook observations, public module-code
  fingerprints, build version and state-source category. Its live MCP response
  reports the configured tool annotations and resource URI. No new tool, runtime
  dependency, process ID, path, prompt or raw session identifier is added.
  These are plugin observations, not independent host/trust attestation.

## Source validation

Windows, Python 3.12.14, Node 24.22.1. Disposable homes/config/cache/state;
authenticated refresh disabled and a nonexistent Codex executable supplied for
synthetic suites. New regression coverage is in `test_installed_repairs.py` and
the actual widget-script harness. Existing privacy/state canaries remain active.

| Check | Result |
| --- | --- |
| Codex unit suite | 266 run; 259 passed, 7 platform skips |
| Widget script tests | 59 passed |
| Codex policy fixtures | 12 passed |
| Repository verifier | 9 checks passed |
| Hermes unit suite | 26 passed |
| Claude unit suite | 365 run; 364 passed, 1 performance failure |

The unchanged Claude `test_500_files_quote_in_under_one_second` failed at 1.028s
and 1.021s against its 1.0s threshold across two isolated full-suite runs. No
Claude source/test, threshold or CI workflow was edited. This remains an owner
validation issue; the required three-suite gate is **not green**. Do not keep
retrying the benchmark until it passes or silently convert it into a skip.
Owner follow-up is [issue 7](https://github.com/DarkNightForge/Agent_Efficiency/issues/7).
The Codex skips remain POSIX FIFO/permission and Windows symlink-privilege cases.

The permitted small AGENTS.md edit updates only test/fixture counts and must be
mentioned in a future PR body. Shared README/SECURITY and provider implementations
remain unchanged; issue 6 coordination still applies.

## Installed acceptance

Follow [INSTALLED-ACCEPTANCE.md](INSTALLED-ACCEPTANCE.md) with the new
[historical payload inventory](https://github.com/DarkNightForge/Agent_Efficiency/blob/7fdf0611cb684d5eb2bedbbfa608e9a4eab4d989/codex/ACCEPTANCE-CANDIDATE.json). A base version label or old cache
entry is insufficient. Use live module fingerprints and caller diagnostics as
additional scoped evidence, while preserving any independent host-metadata gap.

An explicitly launched installed MCP process can validate real state visibility,
annotations/resources and five-minute worker activity. Such a process is not
the Desktop's own loaded connection, and cannot establish that Desktop's picker
persists, trusted hooks use the new package, or the actual UI handoff works.
Those results must be recorded separately rather than substituted for Prompt A/B.

The repair package was installed with the supported CLI; all 17 installed
payload hashes match the inventory, and the prior candidate remains present.
A real process launched by its installed Git/Python MCP entrypoint passed:
13-tool discovery, core/server fingerprint checks, live annotation comparison,
actual UI-resource content, host-directory history/audit visibility and genuine
delegated worker refreshes. The observation lasted 305.062 seconds and the fixed
worker success timestamp advanced. No explicit refresh tool was used during
that interval. The process was then closed normally. Detailed results remain
in a private artifact outside the repository; account working-volume figures
are intentionally omitted here.

The Desktop connection in the active development chat still returned the prior
response format after installation (no new runtime/contract fields). That is
not acceptance of the new connection. A restart/fresh session and ordinary hook
review remain necessary before Desktop UI and automatic child lifecycle tests
can be completed. No trust bypass or desktop permission automation was used.

The Desktop phase requires a fresh loaded candidate after ordinary hook review,
real UI interactions, parent/child hook evidence and at least 300 seconds of
measured idle time. The guide recommends a six-minute timer to avoid the earlier
shortfall. No merge/release is authorized by source tests or package installation.
The historical permission-prompt cause is unresolved; no reproduction or support
report is part of this work.

## Primary implementation references

- [OpenAI UI integration and state](https://developers.openai.com/plugins/build/chatgpt-ui)
- [MCP Apps handshake specification](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx)
- [Codex legacy MCP config normalization](https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/plugin_config.rs)
- [Codex plugin data layout](https://github.com/openai/codex/blob/main/codex-rs/core-plugins/src/store.rs)
- [Codex subagent start dispatch](https://github.com/openai/codex/blob/main/codex-rs/core/src/hook_runtime.rs)

Source references explain the implementation basis; current-host behavior still
requires empirical acceptance. No formal OpenAI approval is claimed.
