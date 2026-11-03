# Telemetry and privacy reference

## Local data

Agent Efficiency stores JSON settings, active timers, numeric hook counters,
rate-limit snapshots, and JSONL task history in the plugin data directory. New
task records also keep the numeric forecast available at task start and an
outcome audit showing whether measured elapsed time and tokens landed inside
their predicted ranges. Capacity auditing keeps only the expected percentage
cost and the later percentage delta when the limit and opaque reset-cycle
identity both match. Automatic prompt records use `unclassified` rather than
inventing a complexity; legacy `unspecified` records map to that label only at
read time rather than bulk-migrated. Normal token reconciliation can update
measurement records and their token audits. The plugin uses opaque hashed local
references instead of raw session, turn, rollout-path, or automatic task
identifiers for automatic correlation; these are private, pseudonymous data,
not guaranteed anonymous. Manually supplied task labels may be stored verbatim.
Measurement history excludes:

- prompt or assistant-response text;
- original file contents and automatic rollout paths;
- tool arguments or tool output; or
- API keys, authentication tokens, or account email.

Set `AGENT_EFFICIENCY_DATA` to override the directory for development or testing.
Installed hooks use `PLUGIN_DATA` when Codex provides it. Overrides must be
absolute paths to dedicated directories.

The MCP config forwards only the named state overrides, not arbitrary environment
variables. If no override is supplied, a package with a verified legacy Codex
cache identity resolves the corresponding host `plugins/data/` directory.
Standalone use keeps the home-directory fallback. Existing locations are never
automatically copied, merged or removed to reconcile their contents.

Subagent registration uses the child's designated rollout header. A bounded read
verifies an explicit parent relationship. Only one unambiguous
active parent is counted, once per hashed child reference. Child token records
remain separate. Missing/conflicting metadata is not replaced with a guess.
Hook session IDs may differ from thread IDs. Only the designated header can bind
their hashed aliases; reconciliation uses the verified thread reference without
rewriting the original session reference. A parent-scoped `SubagentStop` names
the child separately through `agent_id` and `agent_transcript_path`. Completion
validates both headers and the recorded turn relationship, then reads tokens
from the child's own turn. Linkage health adds only fixed reason labels and
bounded counts.

Security status includes fixed refresh-caller timestamps, aggregate hook-event
health, the package build version and public module fingerprints. Its MCP form
also reports the live server's tool annotations and UI-resource declaration.
These help inspect the plugin's own runtime; they do not attest the host's trust
policy or process configuration. No local paths or raw session identifiers are
added. Optional host widget state stores only the selected mode.

If the host gives the prompt hook only an app-message placeholder, the hook
offers a short-lived receipt for that turn. The follow-up can confirm the mode
by presenting the receipt and the picker's one-time token to `select_task_mode`.
Confirmation consumes both values and applies to that follow-up. The plugin
does not search rollout messages for the picker token.

The state directory is claimed with `.agent-efficiency-state.json`. Empty
directories and directories containing only recognized state from v0.4 are
adopted without rewriting existing records. Agent Efficiency refuses broad or
executable locations, unclaimed directories with unrelated files, and state
roots or files redirected by symbolic links, Windows junctions, or hard links.
Every write is restricted to a fixed state-filename allowlist. JSON and history
updates use flushed atomic replacement; POSIX permissions are tightened to
`0700` for the directory and `0600` for its files. These guarantees cover
plugin-managed state, not all writes of Codex, an installer or another process.
Original rollout files are not modified. See [the full Codex privacy notice](../../../../../codex/PRIVACY.md)
in a source checkout; installed users can use the repository's Codex documentation.

State has a fixed 16 MiB total ceiling and smaller per-file ceilings. External
JSON, MCP messages, and hook payloads are capped at 1 MiB. Rollout telemetry is
read as a bounded stream: an individual file contributes at most 16 MiB per
scan, all files together contribute at most 64 MiB, and oversized 1 MiB lines
are skipped. The local App Server client also limits message, line, queue, and
total output sizes and has a fixed request timeout. These are safety boundaries,
not user-adjustable settings.

MCP tool arguments are validated against the advertised schemas at runtime,
including nesting, collection, string, numeric-range, and enum limits. Returned
errors are sanitized so host exception details and filesystem paths are not
reflected to clients. `get_security_status` reports ownership-marker health,
unexpected or linked entries, permissions, fixed limits, aggregate disk use,
retention, and stored/excluded data categories. It never returns local paths or
identifiers, and inspecting an unused location does not create or claim it.

## Codex hooks

The hooks observe session start, user prompt submission, supported tool calls,
subagent activity, compaction, turn stop, and session end. Codex requires the user
to inspect and trust non-managed plugin hooks before they run. Codex Desktop exposes
review under the plugin's **Hooks** details and under **Settings > Hooks**; the
interactive CLI uses `/hooks`. Trust is tied to the exact command hash, so changed
hooks must be reviewed again. Start a new task after trusting because `SessionStart`
does not replay retroactively. If a CLI reinstall occurred while Desktop was open,
restart Desktop before creating the task so hook and MCP registries reload; Agent
Efficiency does not bypass this protection.
Handlers record metadata and aim to fail open within bounded input/time limits.
This does not guarantee that every host/runtime failure is harmless.

`UserPromptSubmit` records the current rollout byte offset. The
`Stop` hook reads only records written after that point and sums
`payload.info.last_token_usage` for every model call in the matching
`turn_context.payload.turn_id`. It never sums cumulative `total_token_usage`.

## App Server telemetry

The plugin delegates authenticated reads to the configured Codex executable,
using a short-lived sequential stdio client. It waits for the
`initialize` response, sends `initialized`, and calls
`account/rateLimits/read`. The result is authoritative for current allowance
state. Supported fields include `limitId`, `primary`, `secondary`,
`usedPercent`, `windowDurationMins`, `resetsAt`, `planType`, and credit metadata.

`refresh_usage` performs an explicit refresh. Session-start/prompt/completion
hooks and control-center requests can refresh automatically; the running MCP
worker normally refreshes every 240 seconds, with bounded retries on failure.
This is delegated network activity. The client does not send rollout contents
in its usage-read request; that is not a claim about all traffic of the host.
A snapshot expires after five minutes. Failed refresh does not advance its
capture time, and stale capacity is `UNCALIBRATED`.

With telemetry enabled, the worker also scans local rollout metadata. Turning
`telemetry_enabled` off disables measurement/scanning while retaining existing
state; allowance refresh and mode behavior remain active. Disabling refresh in
an isolated test alone does not disable the scanner. A fresh chat does not
isolate ordinary background scanning.

History is bounded by `history_limit` (10–500 records) on history writes. Other
state has size ceilings but no promised age-based expiry. Use supported plugin
disable/trust controls; never ask for credentials or weaken host permissions.

The App Server exposes percentages and reset boundaries, not a guaranteed count
of future messages. Agent Efficiency learns comparable-task percentage deltas
from completed turns and reports estimated capacity as a range with confidence.
Credits or unlimited continuation are preserved so an included-allowance cap is
reported as cost ahead rather than a hard lockout.

Forecast generation is optional telemetry and cannot block task startup. A
missing or invalid measurement stays unavailable in the outcome audit; the
plugin does not convert it to zero or use it as evidence of accuracy. Outcome
generation is also fail-open, so a malformed stored forecast cannot block task
completion. Historical elapsed time and token totals may calibrate the point
estimate, while confidence and interval width use audited forecast residuals;
legacy records without an audit therefore cannot create high confidence.

`get_forecast_diagnostics` and the `forecast-diagnostics` CLI command summarize
forecast health without returning individual history records. The report covers
range coverage, median and 80th-percentile residual error, generation failures,
paired time/token audit depth by mode, and the next sample threshold. It contains
aggregate numeric values and fixed explanatory text only—never task, session,
turn, prompt, or path data. A report marked `attention` means outcome generation
failed for at least one completed task; `collecting`, `interval-ready`,
`confidence-ready`, and `well-audited` describe sample depth, not a guarantee that
forecast error is low.
