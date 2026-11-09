# Codex implementation

> **Status: v0.5.0 Codex candidate (unreleased), being prepared for v1.0.0.** The installable plugin lives in
> [`../plugins/agent-efficiency/`](../plugins/agent-efficiency/).

Agent Efficiency gives Codex three execution modes, a control-center picker,
automatic per-turn measurement, and a live allowance-aware planning policy. It
keeps the behavior and decision rules aligned with the Claude Code version while
using telemetry Codex actually exposes. v0.5.0 adds an enforced state sandbox,
bounded local inputs and processes, and a read-only privacy status without adding
cloud telemetry or third-party runtime dependencies.

An earlier implementation was exercised against Codex CLI `0.147.0`: a local App Server
client returned a live rate-limit snapshot, the snapshot normalized into active
windows, and the capacity estimator consumed it without a manual usage reading.
Current source validation and remaining host acceptance are tracked in
[PUBLIC-MIGRATION.md](PUBLIC-MIGRATION.md). This candidate is not a published release.

The latest focused corrections are tracked in
[INTEGRATION-FOLLOWUP.md](INTEGRATION-FOLLOWUP.md): schema-valid widget negotiation,
content-specific resource identity and header-verified session/thread linkage.
Their Desktop picker/child acceptance remains separate from source tests.

## Install

You need Git, Python 3, and a Codex build with plugin support. The plugin has no
third-party Python dependencies.

For the current unreleased candidate, follow
[the exact installed-candidate procedure](INSTALLED-ACCEPTANCE.md) instead of
installing public main. This hook-containing build is manually installed in
Desktop; current [OpenAI packaging guidance](https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks)
excludes lifecycle-hook plugins from the universal public directory.

```bash
git clone git@github.com:DarkNightForge/Agent_Efficiency.git
cd Agent_Efficiency
codex plugin marketplace add "$PWD"
codex plugin add agent-efficiency@agent-efficiency
```

Start a new Codex task. Open `/hooks`, inspect the Agent Efficiency commands, and
trust them. Codex intentionally does not execute untrusted plugin hooks; do not
use a hook-trust bypass flag. Start another new task after installing an updated
build because Codex caches plugin versions and loaded skills per task.

## Use

Ordinary language is enough:

```text
Show the Agent Efficiency control center for this task.
Compare all three Agent Efficiency modes.
Calculate only Stretch for this task.
Use Throughput for this task.
Set Balanced as my default mode.
Show my Agent Efficiency settings and capacity.
Show my Agent Efficiency security and privacy status.
```

The control center describes every mode, its tradeoff, ETA, estimated token
range, estimate confidence, and the calibration evidence behind that confidence.
When the App Server snapshot is fresh, it also shows estimated comparable tasks
that fit. A one-task choice never changes the saved default unless you explicitly
ask to save it.

## Modes

### Throughput

Optimize for completion speed. Codex uses relevant tools readily, parallelizes
safe independent work, retries recoverable failures, and performs strong
validation. Token and allowance use are secondary.

### Balanced

Balance speed, usage, and confidence. Codex reads focused context, makes targeted
changes, parallelizes useful independent checks, and validates in proportion to
risk. This is the default.

### Stretch

Minimize tokens and tool calls while still completing the request. Codex reads
narrowly, avoids optional fan-out, stays concise, and runs the smallest
sufficient validation.

No mode weakens safety checks, authorization boundaries, or necessary
verification.

## What is automatic

Once trusted, hooks do four things:

1. `SessionStart` refreshes the Codex allowance snapshot and loads the default
   mode.
2. `UserPromptSubmit` starts numeric turn measurement and injects a complete
   `<usage-budget>` policy block.
3. Tool, subagent, and compaction hooks increment numeric counters without
   storing their content.
4. The `Stop` hook reads exact per-model-call token usage from the
   current rollout, refreshes allowance again, records percentage deltas, and
   completes the task history entry.

The plugin registers its bundled MCP server through `.mcp.json`, so
`show_control_center` can open the interactive picker and `refresh_usage` can
request an explicit live snapshot. `get_security_status` reports storage,
retention, and privacy safeguards without returning local paths or identifiers.
The older duplicate `refresh_rate_limits` name remains a callable compatibility
alias but is no longer advertised as a separate tool. The control-center CLI
remains a text fallback for hosts without MCP Apps. Snapshots older than five minutes are
rejected as stale and shown as `UNCALIBRATED`; sanitized telemetry health
explains whether App Server polling is `ok`, `polling`, or `degraded`.

Hook handlers are synchronous for compatibility: an earlier Codex 0.147 test
accepted `async` in the config but skipped those handlers at discovery time.
Counter hooks only perform a small locked JSON update; the heavier Stop handler
is capped at ten seconds. Recheck supported hook behavior on the host used for acceptance.

All MCP requests, hook payloads, state files, rollout records, and App Server
responses have fixed size or runtime ceilings. Rollout JSONL is processed as a
bounded stream instead of being loaded as an unbounded file. Tool arguments are
validated against their advertised schemas at runtime, and errors returned over
MCP are sanitized so filesystem paths and host error details are not reflected.

## Budget policy

The injected policy separates observation from forecast:

- **LIVE / measured:** App Server used percentage, window duration, and reset.
- **Derived:** remaining percentage (`100 - used`).
- **Estimated:** comparable tasks remaining, burn rate, time to cap, and weekly
  pace.
- **UNCALIBRATED:** no fresh host snapshot; the plugin does not invent one.

Forecasting uses lockout duration, not the simplistic fact that a cap arrives
before a reset. If a window would cap only minutes before renewal, ordinary work
continues. A checkpoint is promoted only for at least two blocked hours when the
cap is within 24 hours, or for at least six blocked hours at any horizon. The
same session is asked again only for a different window or a 25% worse forecast.
Small work continues without an unnecessary checkpoint.

Long-window forecasts use measured progress over elapsed calendar time, including
zero usage. A newly opened window needs at least ten minutes of elapsed time
before its burn can trigger a lockout forecast. Shorter windows require allowance
snapshots from the current reset cycle separated by at least five minutes.
Samples with missing or different reset identities cannot drive that forecast.
These minimum spans reduce sensitivity to rounded percentage movement; they do
not make forecasts certain. Active-turn burn remains available only as an
explicitly labeled continuous-work scenario and never promotes a lockout on its own.

Credit-backed or unlimited continuation is labeled **COST AHEAD**, not a hard
lockout. Throughput uses calibrated headroom aggressively, Balanced says **GO
WIDE** only when long-window usage is comfortably under pace with ample
headroom, and Stretch never says **GO WIDE**. This respects the selected posture
without degrading ordinary work merely to preserve allowance that expires at
reset.

Exact messages remaining cannot be calculated because message cost varies with
the model, context, reasoning, tools, media, output length, execution settings,
and future behavior. Estimated task capacity is therefore a range with a stated
confidence and becomes personalized only after completed-task percentage deltas
exist.

## Telemetry and privacy

Local state honors `AGENT_EFFICIENCY_DATA`, then `PLUGIN_DATA`. A verified legacy
Codex installation falls back to the matching host `plugins/data/` directory;
standalone use defaults to `~/.agent-efficiency/`. Existing state locations are
not automatically moved, merged or deleted. An override must be an absolute, dedicated
directory. On first use, Agent Efficiency claims an empty directory—or a
directory containing only recognized v0.4 state files—with an ownership marker.
It refuses filesystem roots, the home directory itself, the current working
directory, the installed plugin directory, unclaimed directories containing
unrelated files, and state roots or files redirected through symbolic links,
junctions, or hard links.

Existing directories are validated before their permissions are changed; a
rejected location retains its original permissions and contents. State readers
reject special files before opening them and validate the opened descriptor.
On POSIX they also use nonblocking opens so a FIFO substituted during validation
cannot stall the security-status check.

All plugin writes use a fixed filename allowlist inside that claimed directory.
JSON and history updates are flushed and atomically replaced so a failed write
preserves the previous file. POSIX state directories are restricted to the
current user (`0700`) and state files to that user (`0600`); Windows relies on
the directory's inherited user ACL. Plugin-managed state writes remain inside
the dedicated state directory; this does not describe all writes made by Codex,
its installer, or the operating system. Original rollout files are read rather
than modified. Stored measurement metadata includes:

- elapsed time, mode, model, and complexity provenance (`unclassified` for
  automatically observed prompts whose complexity was not selected);
- input, cached-input, cache-write, reasoning, output, and total tokens;
- model calls, tool calls and failures, subagents, and compactions;
- measured rate-limit snapshots and reset-cycle-matched per-task percentage deltas;
- validation status and efficiency score; and
- the numeric forecast made when a task started, plus whether measured elapsed
  time, tokens, and allowance delta matched it.

Active correlation and rollout scan caches use truncated SHA-256 references for
session IDs, turn IDs, automatic task IDs, and rollout paths. Raw identifiers and
filesystem paths used for automatic correlation are not persisted. These hashes
are private, pseudonymous references, not proof of anonymization. Explicitly
supplied task labels may be retained; use short, non-sensitive labels.

Rollout scanning rotates through opaque file references using a persistent
cursor, so collections larger than the 1,000-file scan limit do not permanently
exclude newer files. Each scan retains the existing per-file and total byte
limits; incomplete records resume safely without counting tokens twice.

The plugin never writes prompt text, assistant responses, file contents, tool
arguments, tool output, API keys, account email, or authentication tokens to its
history. Set `telemetry_enabled` to `false` to disable task measurements and the
rollout scan; existing records are retained. Mode/budget behavior and allowance
refresh remain active, so this is not an offline switch.

The plugin delegates authenticated allowance reads to `codex app-server --stdio`.
Hooks, control-center requests and a running MCP worker can refresh automatically;
the healthy worker interval is 240 seconds. JSONL rollout records are parsed
locally for counts and scoped metadata, without retaining their conversation
bodies in measurement history. See [the Codex privacy notice](PRIVACY.md) for
data categories, retention, user controls and the external-process boundary.

Local plugin state has a fixed 16 MiB ceiling, with smaller per-file ceilings.
The control center and `get_security_status` show current disk use, the configured
history-record limit, ownership-marker health, unexpected-entry detection, and
platform permission status. This status inspection is read-only: checking an
uninitialized location does not create or claim it.

Forecast auditing is failure-safe: if history or allowance state cannot produce
a forecast, task measurement still starts and records the forecast as
unavailable. Missing elapsed time or token totals remain unmeasurable rather
than being scored as zero. Completion also remains fail-open if an older or
malformed stored forecast cannot be audited.

New task records carry a schema version. History readers do not bulk-rewrite old
records; normal reconciliation can update token measurements, scoped metadata
and token audits. The legacy `unspecified` complexity label is treated as
`unclassified` only during comparison. Point estimates may still use compatible
legacy measurements, but confidence and interval width are promoted only by
audited forecast residuals. Capacity calibration accepts a percentage delta only
when its opaque reset-cycle identity matches from forecast through completion.
The `get_forecast_diagnostics` MCP tool and `forecast-diagnostics` CLI
command aggregate audit coverage, residual error, failure counts, per-mode sample
depth, and the next calibration milestone. Their output contains no task,
session, turn, prompt, or path values. These reports may initialize the dedicated
state directory; they are not declared read-only tools.

## How Codex differs from Claude Code

| Concern | Claude Code | Codex |
|---|---|---|
| Current allowance | Host status-line readings, with `/usage` calibration as fallback | `account/rateLimits/read` supplies measured percentages and resets |
| Per-call tokens | Transcript records with provider request IDs | Rollout `last_token_usage`, grouped by `turn_context.payload.turn_id` |
| One user turn | Often deduplicated by request ID | May contain several model calls; every `last_token_usage` is summed |
| Window boundaries | Must be reconstructed and anchored | `resetsAt` is served directly |
| Pricing ratios | Needed to reconstruct weighted spend | Not needed for percentage-space headroom or pacing |
| Hook activation | Provider-specific hook install | Codex user reviews and trusts commands through `/hooks` |

The shared part is the policy: distinguish measured and estimated values, reject
stale state, warn based on real lockout cost, treat credits as cost rather than a
block, use under-pace headroom, preserve safety and validation, and fail silently
if the budget helper itself breaks.

## Command-line fallback

Hooks are the automatic path in Codex 0.147, and the dependency-free CLI is
the explicit fallback (and useful for development):

```bash
python3 plugins/agent-efficiency/scripts/agent_efficiency.py control-center \
  "Fix the login bug" --scope all --complexity moderate

python3 plugins/agent-efficiency/scripts/agent_efficiency.py set \
  default_mode stretch

python3 plugins/agent-efficiency/scripts/agent_efficiency.py settings

python3 plugins/agent-efficiency/scripts/agent_efficiency.py --json scan-rollouts

python3 plugins/agent-efficiency/scripts/agent_efficiency.py --json refresh-usage

python3 plugins/agent-efficiency/scripts/agent_efficiency.py \
  --json forecast-diagnostics --limit 100

python3 plugins/agent-efficiency/scripts/agent_efficiency.py --json security-status

python3 plugins/agent-efficiency/scripts/agent_efficiency.py --json verify
```

Use `--json` before the subcommand for machine-readable output.

## Validation

Run the Codex suite from the repository root:

```bash
python3 -m unittest discover -s codex -p "test_*.py" -v
python3 codex/evals/run_evals.py
node --test codex/test_control_center.cjs
```

The widget tests require Node 22 or newer and use only built-in modules: no npm
installation is needed. They execute the actual UI script with synthetic host
bridges, a fake DOM, and fake timers. Protocol recovery tests replace the entire
background telemetry worker before starting their synthetic stdio server; no
live refresh or real rollout scan is needed.

The public migration carries forward bounded state/process handling, trustworthy
quota identities, protocol validation/recovery, bounded widget requests,
one-time mode handoff, per-call/session-scoped accounting and truncation guards.
Explicit zero/invalid snapshot times cannot become fresh readings. Late token
corrections update token audits/provenance while preserving time/allowance evidence
and excluding invalidated measurements. [PUBLIC-MIGRATION.md](PUBLIC-MIGRATION.md)
records the source baseline, results and remaining release gates.

A matching version string alone does not establish source/installed parity.
Synthetic tests do not prove current-host hook execution, real UI handoff,
authenticated polling cadence or parent/subagent attribution. These need an
installed candidate tied to the reviewed source and a separate acceptance report.

The suite covers legacy-name migration, posture thresholds, warning hysteresis,
empirical estimator spread, mode ordering, selected-only calculation, persistence, live
snapshot normalization, credit-backed windows, staleness, lockout duration,
rollout token accounting, hook privacy and opt-out behavior, prompt policy
injection, forecast diagnostics, bounded input/process behavior, state-directory
canaries, privacy reporting, manifest validation, MCP tools, and the control-center
resource.

Installation verification accepts both a source folder named `agent-efficiency`
and an installed `agent-efficiency/<full-manifest-version>` folder, including
the exact cachebuster when present. These are structural checks, not proof of
package authenticity or hook trust; manifest, core-version, and companion-file
checks still apply.

For live Codex Desktop acceptance, trust Agent Efficiency's hooks from the plugin
details or **Settings > Hooks**, then create a new task. Do not reuse the task that
was open while trust was granted: `SessionStart` only runs when a task starts and
does not replay retroactively. Verify both activation context and the 13 bundled MCP
tools in that post-trust task. When reinstalling from the CLI while Desktop is open,
fully restart Desktop first; its long-lived hook and MCP registries may predate the
new plugin cache entry even when a later task can already see the updated skill.

For the cross-provider experiments and rationale, see
[`../docs/FINDINGS.md`](../docs/FINDINGS.md).
