# Codex data access and privacy

This notice describes `plugins/agent-efficiency/`. Claude Code and Hermes have
separate implementations and data boundaries.

## Local reads and retained information

The plugin parses local Codex rollout JSONL records for per-model-call token
counts, timestamps, model labels and session/subagent correlation. Parsing
reads the record bytes locally. The plugin does not reconstruct the conversation
for another model. Measurement history excludes prompt/response bodies, tool
arguments and tool output. Do not provide full conversations, credentials or
sensitive text as task labels or tool inputs.

Retained metadata includes preferences, elapsed time, token/tool counts,
model/complexity labels, validation results, forecast audits, allowance
snapshots, polling health and scanner progress. Automatic session/turn/file
correlation uses truncated SHA-256 references. These are pseudonymous, not
guaranteed anonymous. Manually supplied task labels may be stored verbatim.

`get_history` returns individual measurement records for explicit history
requests. Security status and forecast diagnostics return aggregate information
without local paths or task/session/turn identifiers. The control center sends
displayed estimates and mode choices through the supported host UI bridge;
its one-task control sends a follow-up message with a one-time mode token.

## State writes and retention

State defaults to `~/.agent-efficiency/`. `AGENT_EFFICIENCY_DATA` or host-provided
`PLUGIN_DATA` may select a dedicated absolute directory. Before claiming a
location, the plugin checks ownership and existing entries. It rejects broad
locations, unrelated files, redirected links and special files. Writes use a
fixed filename allowlist and bounded atomic replacement. POSIX permissions
are restricted; Windows uses the inherited directory ACL. These defenses are
scoped and do not guarantee protection against every operating-system race.

History retains at most `history_limit` (10–500 records). There is a fixed
16 MiB total ceiling and per-file limits. Settings, snapshots, correlation
caches and remaining history have no promised age-based expiry. Detected
rollout shrink invalidates associated plugin measurements without repairing,
moving or deleting the original rollout. Normal reconciliation may correct
history metadata and token audits; installation does not initiate a bulk repair.

These limits describe plugin-managed state operations, not every filesystem
action of Codex, its installer, another executable or the operating system.

## Delegated account requests and automatic activity

The plugin launches the configured Codex executable (or `codex` on PATH) with
`app-server --stdio` and without a command shell. It sends `initialize`,
`initialized` and `account/rateLimits/read`. Codex handles authentication and
the account request. The usage-read payload built by this client contains no
rollout or conversation contents. Codex's own traffic and credential handling
remain the host's responsibility. The plugin does not implement sign-in,
request authentication secrets or explicitly request passkey enumeration.

Refresh may run from session-start/prompt/completion hooks, control-center
requests, the explicit refresh tool and the running MCP worker. Healthy worker
iterations normally wait 240 seconds; failures use bounded retry delays. This
is delegated network activity even without a separate plugin HTTP client.
Failed refreshes do not relabel old captures as fresh.

With telemetry enabled, the worker also performs bounded incremental rollout
reads. A new chat does not isolate that background scan. Measured percentages
and reset times do not guarantee future task capacity.

## User controls

- `telemetry_enabled: false` disables task measurement and rollout scanning.
  Existing records remain, and allowance refresh and modes remain enabled.
- A smaller history limit reduces task records on subsequent history writes;
  it does not erase other state files or the host's original transcripts.
- Disable the plugin through supported host controls to stop future activation.
  Already running host processes may require a restart to unload.
- Review hook commands and use supported trust controls. Plugin text and tool
  annotations do not grant permissions or override platform instructions.

The earlier Windows permission-prompt investigation remains a release gate.
Functional tests do not identify its historical cause. Keep sensitive access
denied and stop on an unexpected credential/passkey prompt; do not retry it to
reproduce a result. A support report remains optional and user-initiated.

Report vulnerabilities through the private security-advisory channel described
in the root security page. Do not post raw transcripts, credentials, local
identifiers or private usage history in public issues.
