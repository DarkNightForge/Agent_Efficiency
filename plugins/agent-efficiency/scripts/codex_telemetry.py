#!/usr/bin/env python3
"""Codex-native telemetry adapters for Agent Efficiency.

The App Server is authoritative for current allowance state. Rollout JSONL is a
fallback source for completed model-call token usage; it is not used to infer
quota percentages or reset boundaries.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from app_server_client import read_rate_limits
from pathlib import Path
from typing import Any


MAX_ROLLOUT_LINE_BYTES = 1 * 1024 * 1024
MAX_TURN_SCAN_BYTES = 64 * 1024 * 1024
MAX_CONTEXT_LOOKBACK_BYTES = 16 * 1024 * 1024


def opaque_ref(value: Any, namespace: str) -> str | None:
    """Return a deterministic hashed reference; it remains private metadata."""

    if value is None:
        return None
    digest = hashlib.sha256(
        f"agent-efficiency:{namespace}:{value}".encode("utf-8", errors="replace")
    ).hexdigest()
    return f"ae-{namespace}-{digest[:24]}"


def fetch_rate_limits(*, timeout: float = 5.0, version: str = "0") -> dict[str, Any]:
    """Read ``account/rateLimits/read`` from a short-lived App Server client.

    The App Server speaks newline-delimited JSON on stdio. Closing stdin after
    the request lets the short-lived process exit, which keeps this adapter
    independent from the Codex process hosting the plugin.
    """

    executable = os.environ.get("AGENT_EFFICIENCY_CODEX_BIN") or shutil.which("codex")
    if not executable:
        raise RuntimeError("codex executable is not available on PATH")
    return read_rate_limits(executable, timeout=timeout, version=version)



def rollout_size(path: str | os.PathLike[str] | None) -> int:
    """Return the current rollout size, or zero when the hook has no transcript."""

    if not path:
        return 0
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def collect_turn_tokens(
    path: str | os.PathLike[str] | None,
    *,
    turn_id: str | None = None,
    turn_ref: str | None = None,
    start_offset: int = 0,
) -> dict[str, Any]:
    """Sum model-call ``last_token_usage`` records for one Codex turn.

    A user turn may emit several ``token_count`` events during tool loops. The
    cumulative ``total_token_usage`` field must never be summed. Only complete
    JSONL records are consumed so a partially-written final line is ignored.
    """

    totals = {
        "input": 0,
        "cached_input": 0,
        "cache_write_input": 0,
        "reasoning": 0,
        "output": 0,
        "total": 0,
        "model_calls": 0,
    }
    if not path:
        return totals
    rollout = Path(path)
    offset = max(0, int(start_offset))
    try:
        with rollout.open("rb") as handle:
            active_turn = (
                _turn_before_offset(handle, offset)
                if turn_id is not None or turn_ref is not None
                else None
            )
            for raw in _bounded_complete_lines(
                handle,
                start_offset=offset,
                byte_limit=MAX_TURN_SCAN_BYTES,
            ):
                if raw is None:
                    continue
                try:
                    record = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "turn_context":
                    value = payload.get("turn_id")
                    active_turn = str(value) if value is not None else None
                    continue
                if record.get("type") != "event_msg" or payload.get("type") != "token_count":
                    continue
                if turn_id is not None and active_turn != str(turn_id):
                    continue
                if turn_ref is not None and opaque_ref(active_turn, "turn") != turn_ref:
                    continue
                info = payload.get("info")
                usage = info.get("last_token_usage") if isinstance(info, dict) else None
                if not isinstance(usage, dict):
                    continue
                totals["input"] += _integer(usage, "input_tokens", "inputTokens")
                totals["cached_input"] += _integer(usage, "cached_input_tokens", "cachedInputTokens")
                totals["cache_write_input"] += _integer(
                    usage, "cache_write_input_tokens", "cacheWriteInputTokens"
                )
                totals["reasoning"] += _integer(
                    usage, "reasoning_output_tokens", "reasoningOutputTokens"
                )
                totals["output"] += _integer(usage, "output_tokens", "outputTokens")
                reported_total = _integer(usage, "total_tokens", "totalTokens")
                totals["total"] += reported_total or (
                    _integer(usage, "input_tokens", "inputTokens")
                    + _integer(usage, "output_tokens", "outputTokens")
                )
                totals["model_calls"] += 1
    except OSError:
        return totals
    return totals


def _turn_before_offset(handle: Any, offset: int) -> str | None:
    """Return the latest complete turn context before a token-read boundary.

    Codex may write ``turn_context`` before ``UserPromptSubmit`` records the
    rollout offset. Token events written after that offset still belong to the
    active turn, so initialize the parser from preceding metadata without
    counting any preceding token records.
    """

    if offset <= 0:
        return None
    start = max(0, offset - MAX_CONTEXT_LOOKBACK_BYTES)
    active_turn: str | None = None
    for raw in _bounded_complete_lines(handle, start_offset=start, byte_limit=offset - start):
        if raw is None:
            continue
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        payload = record.get("payload")
        if record.get("type") != "turn_context" or not isinstance(payload, dict):
            continue
        value = payload.get("turn_id")
        active_turn = str(value) if value is not None else None
    return active_turn


def _bounded_complete_lines(
    handle: Any,
    *,
    start_offset: int,
    byte_limit: int,
) -> Any:
    """Stream complete JSONL records with fixed memory and per-call limits."""
    handle.seek(max(0, int(start_offset)))
    remaining = max(0, int(byte_limit))
    while remaining > 0:
        line_start = handle.tell()
        raw = handle.readline(min(MAX_ROLLOUT_LINE_BYTES + 1, remaining))
        if not raw:
            return
        remaining -= len(raw)
        if raw.endswith(b"\n"):
            yield raw.rstrip(b"\r\n") if len(raw) <= MAX_ROLLOUT_LINE_BYTES else None
            continue
        if len(raw) <= MAX_ROLLOUT_LINE_BYTES:
            handle.seek(line_start)
            return

        complete = False
        while remaining > 0:
            piece = handle.readline(min(64 * 1024, remaining))
            if not piece:
                break
            remaining -= len(piece)
            if piece.endswith(b"\n"):
                complete = True
                break
        yield None
        if not complete:
            return


def _integer(value: dict[str, Any], *keys: str) -> int:
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, (int, float)) and raw >= 0:
            return int(raw)
    return 0
