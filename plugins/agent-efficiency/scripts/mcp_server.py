#!/usr/bin/env python3
"""Dependency-free stdio MCP server for Agent Efficiency."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

from agent_efficiency import (
    COMPLEXITY_BASE,
    CONTEXT_FACTOR,
    LOCATION_FACTOR,
    MODES,
    REASONING_FACTOR,
    VERSION,
    complete_task,
    forecast_diagnostics,
    get_settings,
    ingest_usage,
    list_history,
    refresh_usage_snapshot,
    scan_rollouts,
    select_task_mode,
    show_control_center,
    start_task,
    update_settings,
)

UI_URI = "ui://widget/agent-efficiency-control-center-v0.4.html"
UI_PATH = Path(__file__).resolve().parents[1] / "assets" / "control_center.html"

TOKEN_PROPERTIES = {
    "input": {"type": "integer", "minimum": 0},
    "input_tokens": {"type": "integer", "minimum": 0},
    "cached_input": {"type": "integer", "minimum": 0},
    "cached_input_tokens": {"type": "integer", "minimum": 0},
    "cache_write_input": {"type": "integer", "minimum": 0},
    "cache_write_input_tokens": {"type": "integer", "minimum": 0},
    "reasoning": {"type": "integer", "minimum": 0},
    "reasoning_tokens": {"type": "integer", "minimum": 0},
    "output": {"type": "integer", "minimum": 0},
    "output_tokens": {"type": "integer", "minimum": 0},
    "total": {"type": "integer", "minimum": 0},
    "total_tokens": {"type": "integer", "minimum": 0},
}
TOKEN_SCHEMA = {"type": "object", "properties": TOKEN_PROPERTIES, "additionalProperties": False}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "inputSchema": schema}


TOOLS = [
    _tool(
        "show_control_center",
        "Show Agent Efficiency modes together with what each performs, ETA, token range, and estimated remaining task capacity.",
        {
            "task": {"type": "string", "description": "Short task description; prompt text is not persisted."},
            "selected_mode": {"type": "string", "enum": list(MODES)},
            "estimate_scope": {"type": "string", "enum": ["selected", "all"]},
            "capacity_scope": {"type": "string", "enum": ["inherit", "selected", "all", "off"]},
            "picker_requested": {"type": "boolean"},
            "complexity": {"type": "string", "enum": list(COMPLEXITY_BASE)},
            "context": {"type": "string", "enum": list(CONTEXT_FACTOR)},
            "model": {"type": "string"},
            "reasoning_effort": {"type": "string", "enum": list(REASONING_FACTOR)},
            "execution_location": {"type": "string", "enum": list(LOCATION_FACTOR)},
            "fast_mode": {"type": "boolean"},
            "expected_tools": {"type": "integer", "minimum": 0},
            "expected_subagents": {"type": "integer", "minimum": 0},
            "expected_media": {"type": "integer", "minimum": 0},
            "expected_output_tokens": {"type": "integer", "minimum": 0},
            "cached_ratio": {"type": "number", "minimum": 0, "maximum": 1},
            "rate_limits": {"type": "array", "items": {"type": "object"}},
        },
    ),
    _tool("get_settings", "Read the persistent Agent Efficiency default mode and calculation preferences.", {}),
    _tool(
        "refresh_usage",
        "Refresh measured allowance windows from Codex App Server and return the saved snapshot.",
        {},
    ),
    _tool(
        "update_settings",
        "Persist one or more Agent Efficiency preferences. Use only when the user asks for a lasting setting change.",
        {
            "default_mode": {"type": "string", "enum": list(MODES)},
            "estimate_scope": {"type": "string", "enum": ["selected", "all"]},
            "capacity_scope": {"type": "string", "enum": ["inherit", "selected", "all", "off"]},
            "show_picker": {"type": "string", "enum": ["always", "when_requested", "never"]},
            "telemetry_enabled": {"type": "boolean"},
            "show_disclaimer": {"type": "boolean"},
            "capacity_unit": {"type": "string", "enum": ["tasks", "turns"]},
            "history_limit": {"type": "integer", "minimum": 10},
        },
    ),
    _tool(
        "start_task",
        "Start an Agent Efficiency timer and numeric measurement record before substantive task work.",
        {
            "task_id": {"type": "string"},
            "session_id": {"type": "string"},
            "turn_id": {"type": "string"},
            "mode": {"type": "string", "enum": list(MODES)},
            "complexity": {"type": "string", "enum": list(COMPLEXITY_BASE)},
            "model": {"type": "string"},
            "source": {"type": "string"},
        },
    ),
    _tool(
        "complete_task",
        "Stop a task timer and save elapsed time, validation result, tokens, usage deltas, and an efficiency score.",
        {
            "task_id": {"type": "string"},
            "status": {"type": "string"},
            "validation_passed": {"type": "boolean"},
            "tokens": TOKEN_SCHEMA,
            "usage_after": {"type": "array", "items": {"type": "object"}},
        },
        ["task_id"],
    ),
    _tool(
        "ingest_usage",
        "Ingest Codex App Server rate-limit and token telemetry for capacity estimation without storing message content.",
        {
            "rate_limits": {},
            "tokens": TOKEN_SCHEMA,
            "task_id": {"type": "string"},
            "plan_type": {"type": "string"},
        },
    ),
    _tool(
        "get_history",
        "Read recent local Agent Efficiency measurements for reports and estimator calibration.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 500}},
    ),
    _tool(
        "get_forecast_diagnostics",
        "Read aggregate forecast accuracy, audit coverage, failures, and calibration readiness without returning task identifiers or content.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 500}},
    ),
    _tool(
        "select_task_mode",
        "Apply a mode to the current or next task without changing the persistent default.",
        {
            "mode": {"type": "string", "enum": list(MODES)},
            "task_id": {"type": "string"},
            "session_id": {"type": "string"},
            "turn_id": {"type": "string"},
        },
        ["mode"],
    ),
    _tool("scan_rollouts", "Incrementally ingest measured token_count events from local Codex rollout JSONL.", {}),
    _tool("refresh_rate_limits", "Fetch a fresh authenticated account/rateLimits/read snapshot via the App Server client.", {}),
    _tool(
        "render_control_center",
        "Render the interactive Agent Efficiency picker from a show_control_center result. Call show_control_center first, then pass its complete result here.",
        {"control_center": {"type": "object", "description": "Complete output returned by show_control_center."}},
        ["control_center"],
    ),
]
TOOLS[-1]["title"] = "Agent Efficiency Control Center"
TOOLS[-1]["_meta"] = {
    "ui": {"resourceUri": UI_URI},
    "openai/outputTemplate": UI_URI,
    "openai/toolInvocation/invoking": "Opening Agent Efficiency…",
    "openai/toolInvocation/invoked": "Agent Efficiency is ready.",
}


def call_tool(name: str, args: dict[str, Any]) -> Any:
    args = dict(args)
    if name == "show_control_center":
        return show_control_center(**args)
    if name == "get_settings":
        return get_settings()
    if name == "refresh_usage":
        return refresh_usage_snapshot(force=True, raise_on_error=True)
    if name == "update_settings":
        return update_settings(**args)
    if name == "start_task":
        return start_task(**args)
    if name == "select_task_mode":
        return select_task_mode(**args)
    if name == "complete_task":
        task_id = args.pop("task_id")
        return complete_task(task_id, **args)
    if name == "ingest_usage":
        return ingest_usage(**args)
    if name == "get_history":
        return list_history(args.get("limit", 20))
    if name == "get_forecast_diagnostics":
        return forecast_diagnostics(args.get("limit", 100))
    if name == "scan_rollouts":
        return scan_rollouts()
    if name == "refresh_rate_limits":
        return refresh_usage_snapshot(force=True, raise_on_error=True)
    if name == "render_control_center":
        center = args.get("control_center")
        if not isinstance(center, dict) or "modes" not in center or "descriptions" not in center:
            raise ValueError("control_center must be a complete show_control_center result")
        if not center.get("render_picker"):
            raise ValueError("picker is disabled by the effective show_picker policy")
        return center
    raise ValueError(f"Unknown tool {name!r}")


def handle(message: Any) -> Any:
    if isinstance(message, list):
        if not message:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        responses = [handle(item) for item in message]
        return [item for item in responses if item is not None] or None
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "params must be an object or null"}}
    if method == "initialize":
        requested = params.get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": requested or "2025-03-26",
                "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
                "serverInfo": {"name": "agent-efficiency", "version": VERSION},
            },
        }
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "resources/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"resources": [{
                "uri": UI_URI,
                "name": "agent-efficiency-control-center",
                "title": "Agent Efficiency Control Center",
                "description": "Interactive picker for task execution modes, ETA, usage, and defaults.",
                "mimeType": "text/html;profile=mcp-app",
            }]},
        }
    if method == "resources/read":
        uri = params.get("uri")
        if uri != UI_URI:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32002, "message": "Resource not found"}}
        try:
            text = UI_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": f"UI resource unavailable: {exc}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": {"contents": [{
            "uri": UI_URI, "mimeType": "text/html;profile=mcp-app", "text": text,
            "_meta": {"ui": {"prefersBorder": True}},
        }]}}
    if method == "tools/call":
        try:
            tool_name = params.get("name", "")
            value = call_tool(tool_name, dict(params.get("arguments") or {}))
            text = json.dumps(value, indent=2, sort_keys=True)
            structured = value if tool_name == "render_control_center" else {"result": value}
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": structured,
                    "isError": False,
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": f"Agent Efficiency error: {exc}"}], "isError": True},
            }
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _read_message() -> tuple[Any, str]:
    first = sys.stdin.buffer.readline()
    if not first:
        return None, "line"
    if first.lower().startswith(b"content-length:"):
        raw_length = first.split(b":", 1)[1].strip()
        if not raw_length.isdigit():
            raise ValueError("Invalid Content-Length header")
        length = int(raw_length)
        if length < 0 or length > 16 * 1024 * 1024:
            raise ValueError("Content-Length is out of range")
        while True:
            header = sys.stdin.buffer.readline()
            if header in {b"\r\n", b"\n", b""}:
                break
        raw = sys.stdin.buffer.read(length)
        if len(raw) != length:
            raise ValueError("Unexpected EOF in framed message")
        return json.loads(raw), "header"
    return json.loads(first), "line"


def _write_message(message: Any, framing: str) -> None:
    raw = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if framing == "header":
        sys.stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw)
    else:
        sys.stdout.buffer.write(raw + b"\n")
    sys.stdout.buffer.flush()


def _telemetry_loop(stop: threading.Event, interval: int = 240) -> None:
    """Keep authenticated headroom and local token measurements fresh without touching stdout."""
    failures = 0
    while not stop.is_set():
        try:
            refresh_usage_snapshot(force=True, raise_on_error=True)
            failures = 0
        except Exception:
            failures += 1
        try:
            scan_rollouts()
        except Exception:
            pass
        delay = interval if failures == 0 else min(interval, 15 * (2 ** min(failures - 1, 3)))
        if stop.wait(delay):
            break


def main() -> int:
    framing = "line"
    stop = threading.Event()
    threading.Thread(target=_telemetry_loop, args=(stop,), daemon=True).start()
    try:
        while True:
            try:
                message, framing = _read_message()
                if message is None:
                    return 0
                response = handle(message)
                if response is not None:
                    _write_message(response, framing)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                _write_message({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}, framing)
            except Exception as exc:
                _write_message({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": f"Internal error: {exc}"}}, framing)
    finally:
        stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
