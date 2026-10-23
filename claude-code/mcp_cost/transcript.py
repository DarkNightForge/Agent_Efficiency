#!/usr/bin/env python3
"""
What an MCP server actually costs a live Claude Code session, read off disk.

    python3 transcript.py                     # newest session
    python3 transcript.py --server rea
    python3 transcript.py --session <uuid> --json

Claude Code records the real request payload in the session transcript:
`api-request-shape` events carry the exact `system` and `tools` arrays that were
sent, and `api-request` events name the shape each request used. So an MCP
server's share of the prompt is a fact on the local filesystem, not something a
measurement run has to rediscover. No model is invoked and nothing is billed.

This replaced a harness that ran `claude -p` with a server registered and again
without it, and subtracted. That cost $0.86 a run and, worse, measured the wrong
thing: it loaded every tool schema eagerly, while a real session DEFERS them and
pulls schemas in only as they are used. Measured 2026-10-21, eager registration
put 343,969 tokens in a request; the live session that produced this file paid
2,040, for the six schemas it actually used.

`--server` takes any MCP server name. REA is the worked example because it has
207 tools and made the deferral gap obvious, but nothing here is specific to it.

Bytes are exact — they come from the payload. Tokens are exact only when
`tiktoken` is importable; otherwise they are apportioned from the request's own
measured token count by byte share, and labelled as an estimate.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, OrderedDict
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
MCP_NAME = re.compile(r"^mcp__([^_]+(?:_[^_]+)*?)__")

try:  # optional: exact token counts, the same proxy REA's own rig uses
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001 - absence is normal and must not be fatal
    _ENC = None


def tokens_of(value) -> int | None:
    if _ENC is None:
        return None
    return len(_ENC.encode(json.dumps(value, ensure_ascii=False,
                                      separators=(",", ":")), disallowed_special=()))


def newest_session() -> Path:
    files = sorted(PROJECTS.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise SystemExit(f"no transcripts under {PROJECTS}")
    return files[0]


def find_session(session_id: str) -> Path:
    matches = list(PROJECTS.rglob(f"{session_id}.jsonl"))
    if not matches:
        raise SystemExit(f"no transcript for session {session_id}")
    return matches[0]


def server_of(tool: dict) -> str:
    """Group a tool by its MCP server, or 'built-in' for Claude Code's own."""
    match = MCP_NAME.match(str(tool.get("name", "")))
    return match.group(1) if match else "built-in"


def read(path: Path) -> dict:
    shapes: "OrderedDict[str, dict]" = OrderedDict()
    requests = Counter()
    totals = {"cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
              "input_tokens": 0, "output_tokens": 0, "assistant_turns": 0}
    first_prefix_tokens = None

    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")

        if kind == "api-request-shape":
            shape = event.get("shape") or {}
            tools = shape.get("tools") or []
            by_server: dict[str, dict] = {}
            for tool in tools:
                bucket = by_server.setdefault(
                    server_of(tool), {"count": 0, "bytes": 0, "tokens": 0 if _ENC else None, "tools": []})
                bucket["count"] += 1
                bucket["bytes"] += len(json.dumps(tool, ensure_ascii=False, separators=(",", ":")))
                if _ENC:
                    bucket["tokens"] += tokens_of(tool) or 0
                bucket["tools"].append(tool.get("name"))
            shapes[event.get("shapeHash")] = {
                "index": len(shapes) + 1,
                "tool_count": len(tools),
                "tools_bytes": len(json.dumps(tools, ensure_ascii=False, separators=(",", ":"))),
                "system_bytes": len(json.dumps(shape.get("system") or [],
                                               ensure_ascii=False, separators=(",", ":"))),
                "tools_tokens": tokens_of(tools),
                "by_server": by_server,
            }

        elif kind == "api-request":
            requests[event.get("shapeHash")] += 1

        elif kind == "assistant":
            usage = (event.get("message") or {}).get("usage") or {}
            if not usage:
                continue
            totals["assistant_turns"] += 1
            for key in ("cache_creation_input_tokens", "cache_read_input_tokens",
                        "input_tokens", "output_tokens"):
                totals[key] += usage.get(key) or 0
            if first_prefix_tokens is None:
                # The first request pays for the whole prefix as a cache write:
                # system, tools and the opening message. An exact figure for a
                # known payload, and the anchor the estimate leans on.
                first_prefix_tokens = usage.get("cache_creation_input_tokens") or None

    for digest, shape in shapes.items():
        shape["requests"] = requests.get(digest, 0)
    return {"path": str(path), "shapes": shapes, "totals": totals,
            "first_prefix_tokens": first_prefix_tokens}


def report(data: dict, server: str) -> str:
    shapes = data["shapes"]
    if not shapes:
        return (f"{data['path']}\nNo api-request-shape events. Either the session predates "
                "shape recording or it made no API calls.")

    lines = [f"Session {Path(data['path']).stem}", f"  {data['totals']['assistant_turns']} "
             f"assistant turns, {sum(s['requests'] for s in shapes.values())} requests, "
             f"{len(shapes)} distinct request shapes", ""]

    unit = "tokens" if _ENC else "bytes"
    lines += [f"| shape | reqs | tools | {server} tools | {server} {unit} | {server} share of tools |",
              "| --- | --- | --- | --- | --- | --- |"]
    last = None
    for shape in shapes.values():
        bucket = shape["by_server"].get(server, {"count": 0, "bytes": 0, "tokens": 0, "tools": []})
        share = bucket["bytes"] / shape["tools_bytes"] if shape["tools_bytes"] else 0
        size = bucket["tokens"] if _ENC else bucket["bytes"]
        lines.append(f"| {shape['index']} | {shape['requests']} | {shape['tool_count']} "
                     f"| {bucket['count']} | {(size or 0):,} | {share:.1%} |")
        last = (shape, bucket, share)

    lines.append("")
    if last and last[1]["count"]:
        shape, bucket, share = last
        lines.append(f"At the newest shape, {server} contributes {bucket['bytes']:,} bytes of the "
                     f"{shape['tools_bytes']:,}-byte tool payload ({share:.1%}), across "
                     f"{bucket['count']} tools:")
        lines.append("  " + ", ".join(sorted(n or "?" for n in bucket["tools"])))
        if _ENC and bucket.get("tokens"):
            per_request = bucket["tokens"]
            lines.append(f"That is {per_request:,} tokens riding EVERY request at this shape, "
                         f"whether or not a {server} tool is called.")
    else:
        lines.append(f"{server} contributed no tool schemas to any request in this session. "
                     "Either it is not registered, or Claude Code deferred every schema and none "
                     "was used — deferral is the normal case and is what keeps the cost near zero.")

    lines += [
        "",
        f"Prefix cost paid once, measured: {data['first_prefix_tokens']:,} tokens of cache write "
        f"on the first request (system + tools + opening message)."
        if data["first_prefix_tokens"] else "",
        "Bytes are exact, from the recorded payload. "
        + ("Token counts are exact (tiktoken)." if _ENC
           else "Install `tiktoken` for exact token counts; without it only bytes are reported, "
                "because apportioning by byte share would be an unlabelled guess."),
        "Free: this reads local transcripts only. No model runs and nothing is billed.",
    ]
    return "\n".join(line for line in lines if line != "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", help="session uuid (default: newest transcript)")
    parser.add_argument("--server", default="rea", help="MCP server name (default: rea)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    path = find_session(args.session) if args.session else newest_session()
    data = read(path)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(report(data, args.server))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
