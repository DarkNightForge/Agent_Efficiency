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
read time and are never rewritten. The plugin uses non-reversible local
references instead of raw session, turn, rollout-path, or automatic task
identifiers. It does not persist:

- prompt or assistant-response text;
- file contents or paths;
- tool arguments or tool output; or
- API keys, authentication tokens, or account email.

Set `AGENT_EFFICIENCY_DATA` to override the directory for development or testing.
Installed hooks use `PLUGIN_DATA` when Codex provides it.

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
They measure metadata only, never block a tool call, and swallow errors so Agent
Efficiency cannot break a task.

`UserPromptSubmit` records the current rollout byte offset. The
`Stop` hook reads only records written after that point and sums
`payload.info.last_token_usage` for every model call in the matching
`turn_context.payload.turn_id`. It never sums cumulative `total_token_usage`.

## App Server telemetry

The plugin opens a short-lived sequential stdio client, waits for the
`initialize` response, sends `initialized`, and calls
`account/rateLimits/read`. The result is authoritative for current allowance
state. Supported fields include `limitId`, `primary`, `secondary`,
`usedPercent`, `windowDurationMins`, `resetsAt`, `planType`, and credit metadata.

`refresh_usage` performs an explicit refresh; the control center and prompt hook
refresh automatically. A snapshot expires after five minutes. Failure produces
`UNCALIBRATED`, not a stale percentage presented as current.

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
