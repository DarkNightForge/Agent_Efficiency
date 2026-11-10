# Codex v1.0.0 validation

Owner: @Eclipse1231317. November 9, 2026.

Validated runtime source: `501d63f2f9ee60f89db244b90264b80e9bda7141`.
Base: `7fdf0611cb684d5eb2bedbbfa608e9a4eab4d989`.
The source was clean at validation. Subsequent readiness documentation changes
leave the 17-file plugin payload unchanged.

| Check | Result |
| --- | --- |
| Codex unit suite | PASS: 268 passed, seven platform skips, 275 total |
| Claude Code unit suite | PASS: 365 passed |
| Hermes unit suite | PASS: 26 passed |
| Widget checks | PASS: 65 passed |
| Codex offline policy fixtures | PASS: 12 passed |
| Source package verifier | PASS: nine checks |
| Installer result | PASS: full version `1.0.0` |
| Source, inventory and installed bytes | PASS: all 17 payload files |
| Installed extras immediately after installation | PASS: none |
| Installed configured Git launcher | PASS in isolated offline state |
| Installed MCP initialize and discovery | PASS: version `1.0.0`, 13 tools |
| Installed tool annotations | PASS: only security status and renderer are read-only |
| Installed UI resource | PASS: content-qualified `v1.0` URI matches installed HTML |
| Installed package verifier | PASS: nine checks in disposable state |
| Fresh Desktop activation, hook review and picker | PENDING |
| Exact one-time mode, saved default and automatic completion | PENDING for `1.0.0` |
| Release PR CI | PENDING |

The unit suites ran serially. Tests and the separate installed launcher probe used
disposable home, state and rollout locations with live allowance refresh disabled.
The probe used the installed `.mcp.json` command and working directory; it does
not establish which process a running Desktop connection loaded.

Seven skips cover POSIX-only FIFO/permission cases and unavailable Windows
symlink privileges. Skips are not counted as passes. Private raw logs and
installation/rollback details remain outside the repository.

The earlier developer build's real Desktop and idle-worker results are
[historical baseline evidence](RELEASE-1.0.0.md), not a substitute for the
new version's remaining [Desktop smoke test](RELEASE-SMOKE-TEST.md).
The shared security wording proposal and historical prompt/provenance limits
remain explicitly recorded in the release record. No public tag or release has
been created.
