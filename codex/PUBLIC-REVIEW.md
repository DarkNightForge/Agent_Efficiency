# Codex v1.0.0 release review

Owner: @Eclipse1231317. Updated November 9, 2026.

[PR #8](https://github.com/DarkNightForge/Agent_Efficiency/pull/8) merged at
`7fdf061`. [PR #9](https://github.com/DarkNightForge/Agent_Efficiency/pull/9)
prepares Codex `1.0.0`. Runtime changes after PR #8 are version surfaces and
the resulting content-qualified UI identity. The final updates record validation;
they do not change the 17-file plugin payload.

## Validation

All three local provider suites passed: 659 passes and seven platform skips
out of 666 tests. The 65 widget checks, 12 Codex policy fixtures and nine verifier
checks passed. Installed `1.0.0` matched all source/inventory payload files and
passed its isolated configured-launcher, tool/annotation, UI-resource and verifier
checks.

The functional Desktop smoke passed: live fingerprints, tester-confirmed save
and Recalculate feedback, exact Throughput follow-up, one-time consumption,
automatic completion, scoped per-call tokens and subsequent Balanced default.
Development-chat checks closed the test chat's Git access blocker and observed
installed MCP process ancestry. Exact chat-to-PID binding, nested independent
tool-call totals and a new capture attributable to Recalculate remain unobserved.

[Full validation and evidence limits](VALIDATION-1.0.0.md) ·
[Release record](RELEASE-1.0.0.md) ·
[Install and rollback](INSTALL-AND-ROLLBACK.md)

## Publication

The user authorized merging and publication after validation. Eight Codex CI
jobs passed at `b43806d`. Verify CI again on the final documentation head, use a
linear merge, verify the merged payload and publish tag `v1.0.0` for that commit.
The Claude/Hermes remote workflows filter by provider paths; distinguish absent
remote jobs from the successful local three-suite run.

The root README/SECURITY correction remains proposed for Eashan in
[issue #6](https://github.com/DarkNightForge/Agent_Efficiency/issues/6), following
the user's explicit choice. Those shared files and provider-owned implementations
are unchanged. Use the [Codex privacy notice](PRIVACY.md) for Codex-specific
local data, delegated authenticated allowance reads and controls.

The historical Windows permission-prompt cause remains unresolved. The user
previously chose to proceed after non-reproduction and keep support reporting
optional. No recurrence was observed in this release smoke.
