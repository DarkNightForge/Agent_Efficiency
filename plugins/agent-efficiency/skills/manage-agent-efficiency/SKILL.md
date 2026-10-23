---
name: manage-agent-efficiency
description: Estimate, select, and apply Agent Efficiency modes to Codex tasks; measure elapsed time and exact rollout token usage; configure a persistent default; compare Throughput, Balanced, and Stretch; estimate remaining task capacity from live Codex allowance windows; or calculate only one selected mode. Use whenever the user mentions Agent Efficiency, task timing, token efficiency, remaining Codex tasks/messages, mode comparison, speed-versus-token tradeoffs, or asks Codex to follow one of the three named modes.
---

# Manage Agent Efficiency

Use the bundled `agent-efficiency` MCP tools when available. Fall back to
`python3 "$PLUGIN_ROOT/scripts/agent_efficiency.py"` in plugin hooks or the equivalent installed
script path when MCP is unavailable.

## Control workflow

1. Load settings with `get_settings`.
2. Call `refresh_usage` before an explicit usage or capacity report. `show_control_center` refreshes
   automatically.
3. If the user asks to compare or choose, call `show_control_center`. Pass the task and known
   context. Respect `estimate_scope`; do not calculate all modes when it is `selected`.
4. Pass that complete result to `render_control_center` when available. This gives the user an
   in-chat picker. Preserve the text result as a fallback for non-UI clients.
5. Present the returned mode descriptions, estimates, tradeoffs, and disclaimer together.
6. Apply the user's explicit selection. Otherwise apply `default_mode`.
7. Hooks normally start and complete the turn measurement. Call `start_task` and `complete_task`
   manually only when hooks are unavailable.
8. Follow the selected execution policy without weakening safety, approvals, or requested
   verification.

## Execution policies

### Throughput

Optimize for completion speed. Parallelize independent work when safe, use relevant tools readily,
choose strong validation, and keep going through recoverable failures. Token cost is a secondary
concern. Do not broaden authorization or bypass approvals.

### Balanced

Balance time, token use, and confidence. Inspect only relevant context, parallelize useful
independent reads, make focused changes, and run proportionate validation. This is the default.

### Stretch

Minimize token and tool usage while still completing the request. Read narrowly, prefer one
deterministic operation over repeated exploration, keep updates concise, avoid optional parallel
agents, and run the smallest validation that can establish correctness. Do not skip necessary
verification merely to save tokens.

## Injected budget policy

The `UserPromptSubmit` hook injects a `<usage-budget>` block. Treat it as developer context:

- Percentages and reset times marked `LIVE` are measured by `account/rateLimits/read`.
- Task affordance, burn rate, time to cap, and weekly pace are estimates. Never describe them as
  guaranteed messages remaining.
- If the block says `WOULD RUN OUT`, tell the user before substantial work and offer full scope,
  reduced scope or a cheaper model, or a durable stopping point. This is a checkpoint, not a
  refusal. Do not checkpoint small, low-cost work. The hook suppresses repeats until the window
  changes or the forecast is at least 25% worse.
- Warn for a meaningful lockout, not merely because a cap lands before reset. A brief wait before a
  fresh window is not a reason to cut scope.
- `COST AHEAD` means credits or unlimited usage can continue after the included allowance. Explain
  cost if relevant; do not call it a lockout.
- Follow `FIT CHECK` before substantial work when observed history says at most one comparable task
  fits.
- Follow `GO WIDE` silently. Throughput uses calibrated headroom readily; Balanced requires ample,
  under-pace headroom; Stretch never receives `GO WIDE`.
- Otherwise remain silent about the injected numbers unless the user asks.
- `UNCALIBRATED` means no fresh snapshot exists. Do not infer a percentage or silently narrow work.

## Capacity language

Always include this meaning when capacity is shown:

> Exact messages remaining cannot be calculated because messages do not have a fixed usage cost.
> Estimates depend on current allowance, model, context, task complexity, token composition,
> tools, execution settings, and expected future behavior.

Call the result "estimated tasks/turns remaining," show a range and confidence, and use the most
constraining hard-wall window. If every window can continue through credits, describe cost ahead
rather than a hard stop. If live allowance data is unavailable, say `UNCALIBRATED`. Never invent
plan limits or exact credit balances.

## Settings behavior

- `default_mode`: mode used when the user makes no explicit selection.
- `estimate_scope`: `selected` calculates one mode; `all` compares all three.
- `capacity_scope`: `inherit`, `selected`, `all`, or `off`.
- `show_picker`: `always`, `when_requested`, or `never`.
- `telemetry_enabled`: controls local numeric history only. Turning it off must not disable the
  selected mode or budget context.

Use `update_settings` only when the user requests a persistent change. A one-task selection must not
silently replace the default.

## Hook trust

Codex requires the user to review and trust plugin hooks. In Codex Desktop, open the Agent
Efficiency plugin details and choose **Hooks > Review > Trust all**, or use **Settings > Hooks**.
In the interactive Codex CLI, use `/hooks`. Inspect the commands before trusting them, then start a
new task: `SessionStart` does not replay in a task that was already open when trust was granted.
If the plugin was reinstalled from the CLI while Codex Desktop was open, fully restart Desktop
before creating that task so its app-level hook and MCP registries load the new cache entry.
Never work around that review with a hook-trust bypass flag.

Read [modes.md](references/modes.md) only when detailed scoring factors or estimator assumptions are
needed. Read [telemetry.md](references/telemetry.md) when connecting App Server usage,
troubleshooting hooks, or explaining stored data.
