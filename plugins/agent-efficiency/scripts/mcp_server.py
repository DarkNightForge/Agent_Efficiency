#!/usr/bin/env python3
"""Dependency-free stdio MCP server for Agent Efficiency."""

from __future__ import annotations

import json
import hashlib
import math
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
    get_security_status,
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

UI_VERSION = ".".join(VERSION.split(".")[:2])
UI_URI = f"ui://widget/agent-efficiency-control-center-v{UI_VERSION}.html"
UI_PATH = Path(__file__).resolve().parents[1] / "assets" / "control_center.html"
RUNTIME_SERVER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
MAX_MCP_MESSAGE_BYTES = 1 * 1024 * 1024
MAX_MCP_HEADER_BYTES = 8 * 1024
MAX_MCP_HEADERS = 32
MAX_MCP_COLLECTION_ITEMS = 512
MAX_MCP_NESTING = 12
MAX_MCP_STRING_CHARS = 16 * 1024
# Negotiate only the protocol this server implements, not an arbitrary client
# string. A client that cannot use this version can reject initialization.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-03-26",)
_END_OF_INPUT = object()


class MCPReadError(ValueError):
    """A sanitized read failure carrying the framing already observed."""

    def __init__(self, message: str, *, framing: str = "line", fatal: bool = False):
        super().__init__(message)
        self.framing = framing
        self.fatal = fatal


class MCPMessageLimitError(MCPReadError):
    """Raised when continuing would leave the stdio protocol desynchronized."""

    def __init__(self, message: str, *, framing: str = "line"):
        super().__init__(message, framing=framing, fatal=True)

TOKEN_PROPERTIES = {
    "input": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
    "input_tokens": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
    "cached_input": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
    "cached_input_tokens": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
    "cache_write_input": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
    "cache_write_input_tokens": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
    "reasoning": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
    "reasoning_tokens": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
    "output": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
    "output_tokens": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
    "total": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
    "total_tokens": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
}
TOKEN_SCHEMA = {"type": "object", "properties": TOKEN_PROPERTIES, "additionalProperties": False}


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
    *,
    read_only: bool = False,
    idempotent: bool = False,
    destructive: bool = False,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return {
        "name": name,
        "description": description,
        "inputSchema": schema,
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": idempotent,
            "openWorldHint": False,
        },
    }


TOOLS = [
    _tool(
        "show_control_center",
        "Show task modes and estimated time, tokens and capacity. Refreshes account usage through Codex and saves local state; estimates are not guarantees.",
        {
            "task": {"type": "string", "maxLength": 4096, "description": "Brief non-sensitive task summary for estimation and display; not stored in measurement history. Do not include credentials or a conversation transcript."},
            "selected_mode": {"type": "string", "enum": list(MODES)},
            "estimate_scope": {"type": "string", "enum": ["selected", "all"]},
            "capacity_scope": {"type": "string", "enum": ["inherit", "selected", "all", "off"]},
            "picker_requested": {"type": "boolean"},
            "complexity": {"type": "string", "enum": list(COMPLEXITY_BASE)},
            "context": {"type": "string", "enum": list(CONTEXT_FACTOR)},
            "model": {"type": "string", "maxLength": 128},
            "reasoning_effort": {"type": "string", "enum": list(REASONING_FACTOR)},
            "execution_location": {"type": "string", "enum": list(LOCATION_FACTOR)},
            "fast_mode": {"type": "boolean"},
            "expected_tools": {"type": "integer", "minimum": 0},
            "expected_subagents": {"type": "integer", "minimum": 0},
            "expected_media": {"type": "integer", "minimum": 0},
            "expected_output_tokens": {"type": "integer", "minimum": 0},
            "cached_ratio": {"type": "number", "minimum": 0, "maximum": 1},
            "rate_limits": {"type": "array", "maxItems": 16, "items": {"type": "object"}},
        },
        idempotent=True,
    ),
    _tool(
        "get_settings",
        "Read saved mode and calculation preferences. May initialize dedicated local state and normalize a legacy mode preference.",
        {},
        idempotent=True,
        destructive=True,
    ),
    _tool(
        "refresh_usage",
        "Delegate an authenticated allowance read to Codex App Server and save the snapshot and polling health locally. Failed refreshes preserve the previous capture time.",
        {},
        idempotent=True,
    ),
    _tool(
        "update_settings",
        "Overwrite selected saved Agent Efficiency preferences. Use only for a user-requested lasting setting change; telemetry opt-out does not disable allowance refresh.",
        {
            "default_mode": {"type": "string", "enum": list(MODES)},
            "estimate_scope": {"type": "string", "enum": ["selected", "all"]},
            "capacity_scope": {"type": "string", "enum": ["inherit", "selected", "all", "off"]},
            "show_picker": {"type": "string", "enum": ["always", "when_requested", "never"]},
            "telemetry_enabled": {"type": "boolean"},
            "show_disclaimer": {"type": "boolean"},
            "capacity_unit": {"type": "string", "enum": ["tasks", "turns"]},
            "history_limit": {"type": "integer", "minimum": 10, "maximum": 500},
        },
        idempotent=True,
        destructive=True,
    ),
    _tool(
        "start_task",
        "Create a local timer and numeric measurement record for requested task measurement. Hooks normally handle this; use non-sensitive identifiers.",
        {
            "task_id": {"type": "string", "maxLength": 256},
            "session_id": {"type": "string", "maxLength": 256},
            "turn_id": {"type": "string", "maxLength": 256},
            "mode": {"type": "string", "enum": list(MODES)},
            "complexity": {"type": "string", "enum": list(COMPLEXITY_BASE)},
            "model": {"type": "string", "maxLength": 128},
            "source": {"type": "string", "maxLength": 64},
        },
    ),
    _tool(
        "complete_task",
        "Complete the specified local timer, remove its active record and save measured results and an optional score/audit in bounded history. Missing measurements remain unavailable.",
        {
            "task_id": {"type": "string", "maxLength": 256},
            "status": {"type": "string", "maxLength": 64},
            "validation_passed": {"type": "boolean"},
            "tokens": TOKEN_SCHEMA,
            "usage_after": {"type": "array", "maxItems": 16, "items": {"type": "object"}},
        },
        ["task_id"],
        destructive=True,
    ),
    _tool(
        "ingest_usage",
        "Persist explicitly supplied allowance/token measurements and update matching local measurement state. Inputs must not contain conversation text or credentials.",
        {
            "rate_limits": {},
            "tokens": TOKEN_SCHEMA,
            "task_id": {"type": "string", "maxLength": 256},
            "plan_type": {"type": "string", "maxLength": 64},
        },
        destructive=True,
    ),
    _tool(
        "get_history",
        "Return recent local measurement records for an explicit report, including timestamps and scoped identifiers. Excludes conversation bodies/tool arguments; may initialize dedicated state.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 500}},
        idempotent=True,
    ),
    _tool(
        "get_forecast_diagnostics",
        "Summarize forecast audits and calibration depth without individual task identifiers or content. May initialize dedicated state; sample depth does not guarantee accuracy.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 500}},
        idempotent=True,
    ),
    _tool(
        "select_task_mode",
        "Store or replace a one-time mode handoff, or update a specified active task's mode. Does not change the persistent default; the next prompt hook must consume an unscoped handoff.",
        {
            "mode": {"type": "string", "enum": list(MODES)},
            "task_id": {"type": "string", "maxLength": 256},
            "session_id": {"type": "string", "maxLength": 256},
            "turn_id": {"type": "string", "maxLength": 256},
        },
        ["mode"],
        destructive=True,
    ),
    _tool(
        "scan_rollouts",
        "Read bounded local rollout JSONL and save token/correlation caches. May correct or invalidate associated measurement history; does not modify the original rollouts.",
        {},
        idempotent=True,
        destructive=True,
    ),
    _tool(
        "get_security_status",
        "Inspect aggregate storage, retention and safeguards without changing state or exposing local paths or identifiers. An unused state directory is not initialized.",
        {},
        read_only=True,
        idempotent=True,
    ),
    _tool(
        "render_control_center",
        "Render the interactive Agent Efficiency picker from a show_control_center result. Call show_control_center first, then pass its complete result here.",
        {"control_center": {"type": "object", "description": "Complete output returned by show_control_center."}},
        ["control_center"],
        read_only=True,
        idempotent=True,
    ),
]
TOOLS[-1]["title"] = "Agent Efficiency Control Center"
TOOLS[-1]["_meta"] = {
    "ui": {"resourceUri": UI_URI},
    "openai/outputTemplate": UI_URI,
    "openai/toolInvocation/invoking": "Opening Agent Efficiency…",
    "openai/toolInvocation/invoked": "Agent Efficiency is ready.",
}
# The existing widget uses these three tools only. Keep the legacy native
# bridge usable alongside the standard MCP Apps visibility metadata.
WIDGET_TOOLS = frozenset({"show_control_center", "update_settings", "select_task_mode"})
for tool in TOOLS:
    widget_accessible = tool["name"] in WIDGET_TOOLS
    metadata = tool.setdefault("_meta", {})
    # MCP Apps defaults omitted visibility to both model and app. Explicitly
    # restrict every other tool, preserving resource metadata on the renderer.
    metadata.setdefault("ui", {})["visibility"] = (
        ["model", "app"] if widget_accessible else ["model"]
    )
    metadata["openai/widgetAccessible"] = widget_accessible

TOOL_SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
COMPATIBILITY_TOOL_SCHEMAS = {
    "refresh_rate_limits": TOOL_SCHEMAS["refresh_usage"],
}


def _validate_value(value: Any, schema: dict[str, Any], path: str = "arguments", depth: int = 0) -> None:
    if depth > MAX_MCP_NESTING:
        raise ValueError(f"{path} exceeds the nesting limit")
    expected = schema.get("type")
    if expected == "object" or (expected is None and isinstance(value, dict)):
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        if len(value) > MAX_MCP_COLLECTION_ITEMS:
            raise ValueError(f"{path} has too many properties")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise ValueError(f"{path} is missing required properties")
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise ValueError(f"{path} contains unsupported properties")
        for key, item in value.items():
            _validate_value(item, properties.get(key, {}), f"{path}.{key}", depth + 1)
        return
    if expected == "array" or (expected is None and isinstance(value, list)):
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        maximum = min(int(schema.get("maxItems", MAX_MCP_COLLECTION_ITEMS)), MAX_MCP_COLLECTION_ITEMS)
        if len(value) > maximum:
            raise ValueError(f"{path} has too many items")
        for index, item in enumerate(value):
            _validate_value(item, schema.get("items", {}), f"{path}[{index}]", depth + 1)
        return
    if expected == "string" or (expected is None and isinstance(value, str)):
        if not isinstance(value, str):
            raise ValueError(f"{path} must be a string")
        maximum = min(int(schema.get("maxLength", MAX_MCP_STRING_CHARS)), MAX_MCP_STRING_CHARS)
        if len(value) > maximum:
            raise ValueError(f"{path} exceeds the string limit")
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{path} must be a boolean")
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{path} must be an integer")
    elif expected == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError(f"{path} must be a finite number")
    elif expected is None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{path} must contain finite numbers")
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise ValueError(f"{path} contains an unsupported value")
    elif expected is not None:
        raise ValueError(f"{path} has an unsupported schema")

    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not an allowed value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} is below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} exceeds the maximum")


def _validate_tool_arguments(name: str, args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ValueError("tool arguments must be an object")
    schema = TOOL_SCHEMAS.get(name) or COMPATIBILITY_TOOL_SCHEMAS.get(name)
    if schema is None:
        raise ValueError("unknown Agent Efficiency tool")
    _validate_value(args, schema)
    return dict(args)


def _safe_error_message(error: Exception) -> str:
    if isinstance(error, (ValueError, TypeError, json.JSONDecodeError, UnicodeError)):
        return "Agent Efficiency rejected an invalid request."
    if isinstance(error, TimeoutError):
        return "Agent Efficiency timed out while reading local Codex telemetry."
    if isinstance(error, PermissionError):
        return "Agent Efficiency was denied access to its dedicated local state."
    if isinstance(error, OSError):
        return "Agent Efficiency refused or could not access its dedicated local state."
    return "Agent Efficiency encountered an internal error."


def call_tool(name: str, args: dict[str, Any]) -> Any:
    args = _validate_tool_arguments(name, args)
    if name == "show_control_center":
        return show_control_center(**args)
    if name == "get_settings":
        return get_settings()
    if name == "get_security_status":
        result = get_security_status()
        result["mcp_contract"] = {
            "server_sha256": RUNTIME_SERVER_SHA256,
            "ui_resource": UI_URI,
            "tools": {tool["name"]: dict(tool["annotations"]) for tool in TOOLS},
            "scope": "Live server declarations; not independent host configuration or hook trust evidence.",
        }
        return result
    if name == "refresh_usage":
        return refresh_usage_snapshot(force=True, raise_on_error=True, caller="tool")
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
        return refresh_usage_snapshot(force=True, raise_on_error=True, caller="tool")
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
        if not message or len(message) > MAX_MCP_COLLECTION_ITEMS:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        responses = [handle(item) for item in message]
        return [item for item in responses if item is not None] or None
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method = message.get("method")
    request_id = message.get("id")
    if (
        message.get("jsonrpc") != "2.0" or not isinstance(method, str) or not method
        or isinstance(request_id, bool)
        or not isinstance(request_id, (str, int, float, type(None)))
        or (isinstance(request_id, float) and not math.isfinite(request_id))
    ):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    # Notifications must not get responses or invoke request-only tool actions.
    if "id" not in message:
        return None
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "params must be an object or null"}}
    if method == "initialize":
        requested = params.get("protocolVersion")
        if requested is not None and not isinstance(requested, str):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "protocolVersion must be a string"}}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False},
                                 "extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}},
                "serverInfo": {"name": "agent-efficiency", "version": VERSION},
            },
        }
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
        except OSError:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "UI resource unavailable"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": {"contents": [{
            "uri": UI_URI, "mimeType": "text/html;profile=mcp-app", "text": text,
            "_meta": {"ui": {"prefersBorder": True}},
        }]}}
    if method == "tools/call":
        try:
            tool_name = params.get("name", "")
            # Only omission means no arguments; falsey non-objects must still
            # reach the validator instead of being silently coerced to {}.
            value = call_tool(tool_name, params.get("arguments", {}))
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
                "result": {"content": [{"type": "text", "text": _safe_error_message(exc)}], "isError": True},
            }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _read_message() -> tuple[Any, str]:
    first = sys.stdin.buffer.readline(MAX_MCP_MESSAGE_BYTES + 1)
    if not first:
        return _END_OF_INPUT, "line"
    framing = "header" if first.lower().startswith(b"content-length:") else "line"
    if len(first) > MAX_MCP_MESSAGE_BYTES:
        raise MCPMessageLimitError("MCP message exceeds the size limit", framing=framing)
    if framing == "header":
        if len(first) > MAX_MCP_HEADER_BYTES:
            raise MCPMessageLimitError("MCP header exceeds the size limit", framing=framing)
        raw_length = first.split(b":", 1)[1].strip()
        if not raw_length.isdigit() or len(raw_length) > 10:
            raise MCPReadError("Invalid Content-Length header", framing=framing, fatal=True)
        length = int(raw_length)
        if length < 0 or length > MAX_MCP_MESSAGE_BYTES:
            raise MCPMessageLimitError("Content-Length is out of range", framing=framing)
        header_bytes = len(first)
        for _ in range(MAX_MCP_HEADERS):
            header = sys.stdin.buffer.readline(MAX_MCP_HEADER_BYTES + 1)
            header_bytes += len(header)
            if header_bytes > MAX_MCP_HEADER_BYTES:
                raise MCPMessageLimitError("MCP headers exceed the size limit", framing=framing)
            if not header:
                raise MCPReadError("Unexpected EOF in headers", framing=framing, fatal=True)
            if header in {b"\r\n", b"\n"}:
                break
            if not header.endswith(b"\n") or b":" not in header or header.lower().startswith(b"content-length:"):
                raise MCPReadError("Invalid or duplicate header", framing=framing, fatal=True)
        else:
            raise MCPMessageLimitError("MCP message has too many headers", framing=framing)
        raw = sys.stdin.buffer.read(length)
        if len(raw) != length:
            raise MCPReadError("Unexpected EOF in framed message", framing=framing, fatal=True)
    else:
        if not first.endswith(b"\n"):
            raise MCPReadError("MCP line message is incomplete", fatal=True)
        raw = first
    try:
        return json.loads(raw), framing
    except (ValueError, UnicodeError, RecursionError) as exc:
        # The entire body/line was consumed, so the next message is recoverable.
        raise MCPReadError("Invalid JSON", framing=framing) from exc


def _write_message(message: Any, framing: str) -> None:
    raw = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_MCP_MESSAGE_BYTES:
        request_id = message.get("id") if isinstance(message, dict) else None
        raw = json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32603, "message": "Agent Efficiency response exceeded the size limit"},
        }, separators=(",", ":")).encode("utf-8")
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
            refresh_usage_snapshot(force=True, raise_on_error=True, caller="worker")
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
                if message is _END_OF_INPUT:
                    return 0
                response = handle(message)
                if response is not None:
                    _write_message(response, framing)
            except MCPReadError as exc:
                _write_message({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid or oversized JSON-RPC message"}}, exc.framing)
                if exc.fatal:
                    return 1
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                _write_message({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid or oversized JSON-RPC message"}}, framing)
            except Exception:
                _write_message({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "Agent Efficiency internal error"}}, framing)
    finally:
        stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
