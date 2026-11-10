# Codex v1.0.0 validation

Owner: @Eclipse1231317. November 9, 2026 (America/Los_Angeles).

Validated runtime source: `501d63f2f9ee60f89db244b90264b80e9bda7141`.
Desktop smoke source: `b43806d9abd1305d90c105c2518ac9199ec96b43`.
Base: `7fdf0611cb684d5eb2bedbbfa608e9a4eab4d989`.
The sources were clean at their checks. Subsequent readiness documentation
updates leave the 17-file plugin payload unchanged.

| Check | Result |
| --- | --- |
| Codex unit suite | PASS: 268 passed, seven platform skips, 275 total |
| Claude Code unit suite | PASS: 365 passed |
| Hermes unit suite | PASS: 26 passed |
| Widget checks | PASS: 65 passed |
| Codex offline policy fixtures | PASS: 12 passed |
| Source package verifier | PASS: nine checks |
| Installer result | PASS: full version `1.0.0` |
| Source, inventory and installed bytes | PASS: all 17 payload files, including recheck after the smoke |
| Installed extras | Only four generated script bytecode files after Desktop activation |
| Installed configured Git launcher | PASS in isolated offline state; CLI configuration also matches |
| Installed MCP initialize and discovery | PASS: version `1.0.0`, 13 tools |
| Installed tool annotations | PASS: only security status and renderer are read-only |
| Installed UI resource | PASS: content-qualified `v1.0` URI matches installed HTML |
| Installed package verifier | PASS: nine checks in disposable state |
| Fresh Desktop activation and loaded skill/module/server/hook fingerprints | PASS |
| Balanced default save and Recalculate feedback | PASS: tester confirmed both visible confirmations; saved Balanced independently verified |
| Exact one-time Throughput and consumed selection | PASS on the automatic parent follow-up |
| Exact parent automatic completion | PASS: completion after final response, without manual repair/reconciliation |
| Scoped per-call token accounting | PASS: all scoped token fields match independent sums |
| Ordinary next-turn default | PASS: Balanced, no leftover selection marker |
| Source HEAD and cleanliness | PASS in development chat; access blocked in the test chat |
| Independent process inspection | Installed `1.0.0` MCP processes observed with Desktop → Codex → Git → shell → Python ancestry; exact chat-to-PID binding not established |
| Recalculate-specific fresh capture | NOT OBSERVED; successful UI feedback does not prove new account data |
| Exact independent tool-call total | NOT OBSERVED for nested calls; pre-final and completed counters agree |
| Unexpected sensitive prompt | None encountered in this run; historical cause unresolved |
| Release PR CI | Eight jobs passed at `b43806d`; final documentation head must also pass before publication |

The unit suites ran serially. Tests and the separate installed launcher probe used
disposable home, state and rollout locations with live allowance refresh disabled.
The real Desktop test used the installed package normally. The developer chat
independently checked Git and process evidence without changing permissions.
The original private report retains the test chat's access blockers; this record
adds the separately scoped development-chat results and tester UI confirmation.

Seven skips cover POSIX-only FIFO/permission cases and unavailable Windows
symlink privileges. Skips are not counted as passes. Private raw logs, numerical
usage, task/session identifiers and installation/rollback paths remain outside
the repository.

The functional release smoke passed. The evidence limits above remain open
observations rather than inferred product failures. The earlier developer
build's child, idle-worker and other Desktop checks remain [historical baseline
evidence](RELEASE-1.0.0.md). Live compaction, SessionEnd and truncation were not
rerun for this version-only packaging change; their synthetic coverage is separate.

[PR #9](https://github.com/DarkNightForge/Agent_Efficiency/pull/9) ·
[Prior PR CI](https://github.com/DarkNightForge/Agent_Efficiency/actions/runs/42725297254)

The shared README/SECURITY correction stays proposed for Eashan in issue #6,
per the user's choice. Use the [Codex privacy notice](PRIVACY.md) for this
provider's data boundaries. Publication uses the validated merged payload after
the final PR head passes CI.
