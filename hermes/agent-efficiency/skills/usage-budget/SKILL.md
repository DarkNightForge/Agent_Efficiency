---
name: usage-budget
version: 0.1.0
description: Interpret the <usage-budget> block injected by the Agent Efficiency Hermes plugin. Use when the block says WOULD RUN OUT, ASK BEFORE PROCEEDING, or GO WIDE; when the user asks about subscription headroom; or when troubleshooting the meter. Default to silence otherwise.
---

# Agent Efficiency for Hermes

A `<usage-budget>` block may be injected into the current user turn. Its percentages and reset times come from Hermes's provider-native account meter; forecasts come from changes between cached meter snapshots.

Hermes replays prior API-sidecar context to preserve prompt caching. Therefore **only the newest `<usage-budget>` block is current**. Every current block says it supersedes older ones. A `STALE`, `UNAVAILABLE`, or `ROUTE CHANGED` tombstone retracts all older directives; take no usage-driven action from them.

## Rules

1. Default to silence. Do not narrate ample headroom or open a response with usage status.
2. Never silently reduce scope to conserve a subscription allowance. Unspent allowance expires.
3. `GO WIDE` changes execution, not narration: use worthwhile parallelism, broader verification, and deeper search without announcing the budget.
4. `WOULD RUN OUT` must be stated before substantial work. Name the projected lockout and offer the block's choices. It is not a refusal; continue at full scope if the user chooses that.
5. `ASK BEFORE PROCEEDING` is a one-time checkpoint for substantial work. Ask with the listed options; do not ask for trivial work and do not narrow scope yourself.
6. A cap shortly before reset is not a meaningful constraint. If the block explicitly says it is not a reason to cut scope, carry on.
7. Treat `EXACT` percentages and reset times as provider facts. Treat burn and cap timing as forecasts, not facts.

## Commands

- `/agent-efficiency` — show the currently cached pacing block.
- `/agent-efficiency refresh` — synchronously refresh the active provider meter.
- `/usage` — Hermes's authoritative raw account and session usage display.

## Troubleshooting

- An `UNAVAILABLE` tombstone on the first standard-runtime turn can be normal: the plugin learns the active route and refreshes asynchronously, then injects fresh data from the next turn.
- If the cache is older than 15 minutes, the plugin injects a `STALE` tombstone rather than presenting stale data as exact. This explicitly retracts earlier replayed directives.
- Supported quota sources are `openai-codex` and `openrouter`. Hermes can also meter a Claude subscription login, but this project does not support that route, because Anthropic's terms reserve the login for Claude Code and Anthropic's own apps. Other providers fail open without injection.
- The plugin requires Hermes host support for current `provider`/`base_url`/`api_mode` fields on `pre_llm_call` and tagged multimodal `api_content` suffixes; without those, same-model endpoint switches and multimodal tombstones are not safe.
- `/agent-efficiency` is read-only. Showing status does not consume the once-per-day `ASK BEFORE PROCEEDING` checkpoint.
- Hermes v0.20.3 native `codex_app_server` mode does not deliver `pre_llm_call` context to Codex, so this plugin deliberately does not claim or pretend to inject there. Standard OpenAI Codex `codex_responses` mode is supported.
