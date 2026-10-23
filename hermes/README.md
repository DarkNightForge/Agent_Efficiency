# Agent Efficiency for Hermes

This is a Hermes-native policy plugin. It reuses Hermes's live provider account meter and turns it into compact planning context before each user turn.

It does **not** duplicate Hermes's `/usage` implementation, add a model-facing tool, or reconstruct provider usage from transcripts.

## What it does

- Reads exact account utilization and reset times through Hermes for:
  - `openai-codex`
  - `openrouter`

  Hermes can also meter a Claude subscription login. That route is not supported
  here: Anthropic's terms reserve the login for Claude Code and Anthropic's own
  apps ([legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)).
- Caches snapshots in the active profile at `plugin-data/agent-efficiency/usage.db`.
- Injects a fresh `<usage-budget>` block through `pre_llm_call`.
- Keeps static behavioral policy in a cache-safe system-prompt section.
- Calculates:
  - remaining percentage and reset time;
  - weekly day-granular pace;
  - a cap/lockout forecast after at least two meter readings;
  - `GO WIDE`, `ASK BEFORE PROCEEDING`, and `WOULD RUN OUT` directives.
- Fails safe across Hermes replay: missing or stale meters inject a small current tombstone that explicitly retracts every older `<usage-budget>` directive. It never presents stale data as exact.

The provider meter refreshes at most once every five minutes per provider endpoint and never adds a model tool schema. Persistence is local-only and content-free: provider name, a short endpoint hash, window label, used percentage, reset time, and fetch timestamp. It stores no prompts, responses, tool arguments, credentials, account identifiers, raw endpoint URLs, plan names, or credit balances.

## Install

From the repository:

```bash
cd hermes
./install.sh
```

The installer symlinks `agent-efficiency/` into the active `$HERMES_HOME/plugins/` directory and enables it. Start a new Hermes session after installation so the plugin and frozen policy section are loaded.

To install without changing the enabled-plugin list:

```bash
./install.sh --no-enable
```

For a named profile, set that profile's home explicitly:

```bash
HERMES_HOME="$HOME/.hermes/profiles/<name>" ./install.sh
```

## Use

Inside Hermes:

```text
/agent-efficiency refresh
/agent-efficiency
/usage
```

The first standard-runtime turn in a new process may carry an `UNAVAILABLE` tombstone while the plugin refreshes in the background. Subsequent turns use the cached meter. `/agent-efficiency refresh` performs a synchronous refresh. The status command is read-only and does not consume the once-per-day pacing checkpoint.

## Architecture

```text
Hermes account meter ──> profile-local SQLite snapshot history
                                  │
                                  ├─> pre_llm_call dynamic block
                                  ├─> weekly pacing + lockout forecast
                                  └─> slash status/refresh

static policy ──> frozen system-prompt section (cache-safe)
full guidance ──> bundled plugin:usage-budget skill
```

Hermes supplies `provider`, `base_url`, and `api_mode` on `pre_llm_call`, so the plugin captures the actual current route before every injection. This closes the switch-turn gap even when two providers/endpoints use the same model name. The frozen prompt-section callback supplies a first-route fallback, and resumed sessions can also read Hermes's persisted dominant route.

The plugin requires a Hermes build with the additive route fields above and replayable multimodal `api_content` suffix sidecars. Those sidecars deliver the newest block on image/attachment turns without mutating clean user content, so a `STALE` tombstone can retract directives replayed from earlier turns.

Hermes's optional native `codex_app_server` runtime is explicitly excluded: Hermes v0.20.3 computes `pre_llm_call` context but bypasses the API-message composition that would deliver it to Codex. Standard OpenAI Codex (`codex_responses`) is supported.

## Validation

```bash
python3 -m unittest discover -s tests -v
hermes plugins doctor agent-efficiency --ci
```

The policy tests cover exact-vs-estimated language, stale snapshots, provider isolation, weekly pacing hysteresis, meaningful lockouts, cheap pre-reset caps, static policy, route capture, hook registration, and context injection.

## Current limits

- The forecast needs two readings from the same provider window, at least five minutes apart. Before that, exact headroom still appears without a cap forecast.
- OpenAI/Anthropic may expose different sets of windows by plan. Missing weekly data suppresses weekly pacing and `GO WIDE`; it is never invented.
- A provider/model/endpoint switch is captured from the current host route before injection. Unsupported/custom routes receive no exact meter.
- If the cache is older than 15 minutes, the newest standard-runtime turn—including a multimodal turn—receives a `STALE` tombstone that says to ignore all earlier blocks. Hermes persists prior API sidecars for prompt-cache replay, so silence would not retract an old directive.
- Endpoint scope hashes preserve the exact case, path, query, and trailing slash. When Hermes's meter resolves a different credential endpoint than the active route, the plugin rejects the reading instead of storing it under the requested route.
- Use one active account or credential pool per provider endpoint in a Hermes profile. Hermes still does not expose credential/account identity for two accounts behind the same endpoint; separate profiles are the hard boundary.
- Native `codex_app_server` mode is not supported until Hermes delivers `pre_llm_call` context to that transport. Use standard `codex_responses` for Agent Efficiency.
- This P0 does not yet attribute individual fan-outs or amortized tool-result context cost. Hermes stores aggregate session/model usage, but its native OpenAI Codex path does not currently emit the generic per-request observer hook needed for a uniform per-call plugin stream.
