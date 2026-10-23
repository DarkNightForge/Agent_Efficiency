# mcp_cost — which MCP server is eating your context

Agent_Efficiency tells you how much of your usage window is left. This tells you **what is
consuming it**: the exact token cost each registered MCP server adds to every request.

That is the more actionable half. "You are at 20%" is harder to act on than "rea is costing you
2,040 tokens on every turn, and you have used it twice."

```bash
python3 transcript.py                                  # newest session
python3 transcript.py --session <uuid> --server rea
python3 transcript.py --server gbrain --json
```

## It reads, it does not measure

Claude Code records the **real request payload** in the session transcript. `api-request-shape`
events carry the exact `system` and `tools` arrays that were sent, and `api-request` events name the
shape each request used. So a server's share of the prompt is a fact on the local filesystem.

No model runs. Nothing is billed. Same principle the rest of this project is built on: the
numerator is computable from local logs.

Measured on a real session, 2026-10-21:

| shape | requests | tools | rea tools | rea tokens | share of tool payload |
| --- | --- | --- | --- | --- | --- |
| 1 | 22 | 17 | 0 | 0 | 0.0% |
| 5 | 2 | 22 | 2 | 448 | 2.7% |
| 7 | 55 | 26 | 6 | 2,040 | 11.0% |
| 8 | 51 | 27 | 6 | 2,040 | 10.7% |

The shape grows as schemas load, so the table is a history of cost accruing — not a single number.

## Deferral is why the numbers are small

Claude Code **defers** MCP tool schemas: it ships the names and pulls a schema in only when the tool
is used. A headless run with `--strict-mcp-config` loads them **eagerly** instead.

Measured on the same server the same day: eager registration put **343,969** tokens in a request;
the live deferred session paid **2,040**, for the six schemas it actually used. **168x.**

This matters beyond trivia. An earlier version of this tool answered the same question by running
`claude -p` with a server registered and again without, then subtracting — $0.86 a cold run, to
measure a configuration nobody runs. Reading the record the system already keeps was free, exact,
and about the real thing.

## Exact token counts

Bytes are always exact; they come from the payload. Tokens are exact when `tiktoken` is importable.
It is deliberately an optional import so this adds no dependency:

```bash
uv venv ~/.local/share/rea-cost-venv
uv pip install --python ~/.local/share/rea-cost-venv/bin/python tiktoken
~/.local/share/rea-cost-venv/bin/python transcript.py --server rea
```

Without it the tool reports bytes and says so, rather than apportioning by byte share and
presenting a guess as a measurement.

## Scope

`--server` takes any MCP server name, not just `rea`.

What this does **not** attribute: tokens spent *calling* a tool — the arguments and the returned
result. Those are conversation, not prompt. This measures the standing cost of a server being
available, which is paid on every request whether or not you call it.

Resource cost — elapsed, CPU, peak memory of the server's own process — is a different question
with a different answer, and does not live here.
