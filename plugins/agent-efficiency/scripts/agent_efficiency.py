#!/usr/bin/env python3
"""Agent Efficiency v0.4 core, CLI, estimator, and telemetry store.

The module is dependency-free so an installed Codex plugin can run it with the
system Python. It intentionally stores numeric metadata, never prompt or tool
content.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from budget_policy import render_usage_budget
from codex_telemetry import collect_turn_tokens, fetch_rate_limits, opaque_ref, rollout_size

try:  # POSIX file locking.
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on Windows.
    fcntl = None  # type: ignore[assignment]

try:  # Windows file locking.
    import msvcrt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on POSIX.
    msvcrt = None  # type: ignore[assignment]

VERSION = "0.4.0"
MODES = ("throughput", "balanced", "stretch")
MODE_ALIASES = {
    "big-baller": "throughput",
    "bigballer": "throughput",
    "big": "throughput",
    "efficiency": "balanced",
    "efficient": "balanced",
    "ultra-efficiency": "stretch",
    "ultra-efficient": "stretch",
    "ultra": "stretch",
}
USAGE_SNAPSHOT_MAX_AGE_SECONDS = 300
DISCLAIMER = (
    "Exact messages remaining cannot be calculated because messages do not have a fixed usage "
    "cost. Estimates depend on current allowance, model, context, task complexity, token "
    "composition, tools, execution settings, and expected future behavior."
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "schema_version": 1,
    "default_mode": "balanced",
    "estimate_scope": "selected",
    "capacity_scope": "inherit",
    "show_picker": "when_requested",
    "telemetry_enabled": True,
    "show_disclaimer": True,
    "capacity_unit": "tasks",
    "history_limit": 500,
}

MODE_INFO: dict[str, dict[str, Any]] = {
    "throughput": {
        "label": "Throughput",
        "objective": "Finish as soon as practical",
        "behavior": "Uses relevant tools readily, safe parallel work, resilient retries, and strong validation.",
        "tradeoff": "Highest expected token and allowance use.",
        "time_factor": 0.70,
        "token_factor": 1.60,
        "capacity_cost_factor": 1.60,
    },
    "balanced": {
        "label": "Balanced",
        "objective": "Balance speed, usage, and confidence",
        "behavior": "Uses focused context, useful parallel reads, targeted changes, and proportionate validation.",
        "tradeoff": "Balanced rather than absolutely fastest or cheapest.",
        "time_factor": 1.00,
        "token_factor": 1.00,
        "capacity_cost_factor": 1.00,
    },
    "stretch": {
        "label": "Stretch",
        "objective": "Minimize tokens and tool calls",
        "behavior": "Reads narrowly, avoids optional fan-out, stays concise, and runs the smallest sufficient validation.",
        "tradeoff": "Longer ETA and less optional exploration.",
        "time_factor": 1.30,
        "token_factor": 0.56,
        "capacity_cost_factor": 0.56,
    },
}

COMPLEXITY_BASE = {
    "trivial": (4.0, 3_000),
    "simple": (12.0, 9_000),
    "moderate": (30.0, 24_000),
    "complex": (75.0, 65_000),
    "very-complex": (160.0, 145_000),
}
UNCLASSIFIED_COMPLEXITY = "unclassified"
RECORD_SCHEMA_VERSION = 2
CONTEXT_FACTOR = {"small": 0.78, "medium": 1.0, "large": 1.38, "huge": 1.85}
REASONING_FACTOR = {"low": 0.75, "medium": 1.0, "high": 1.22, "xhigh": 1.48, "max": 1.75, "ultra": 2.05}
LOCATION_FACTOR = {"local": 1.0, "cloud": 1.18, "code-review": 1.12}

# How long you would have to sit blocked at a cap before it is worth reshaping the
# work. Hitting a cap costs you the gap between running out and the reset -- nothing
# else. Run out 10 minutes before a window rolls over and you have lost 10 minutes;
# the next window arrives with a full allowance. Below this threshold the right
# advice is to carry on, and saying so out loud matters: an agent left to interpret
# a bare "cap arrives first" cut its own plan with 53% of the window unspent.
# See FINDINGS section 9.
LOCKOUT_WARN_HOURS = 0.75
PROMOTE_MIN_BLOCKED_HOURS = 2.0
PROMOTE_MAX_HORIZON_HOURS = 24.0
PROMOTE_ALWAYS_BLOCKED_HOURS = 6.0
PACE_REASK_STEP = 0.25
CHOICE_REASK_WORSE = 1.25


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _epoch(value: Any) -> float | None:
    """Normalize App Server epoch or ISO timestamps to epoch seconds."""
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return _epoch(float(value))
        with contextlib.suppress(ValueError):
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.timestamp()
    return None


def _data_dir() -> Path:
    configured = os.environ.get("AGENT_EFFICIENCY_DATA") or os.environ.get("PLUGIN_DATA")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".agent-efficiency"


def _paths() -> dict[str, Path]:
    root = _data_dir()
    return {
        "root": root,
        "settings": root / "settings.json",
        "active": root / "active.json",
        "history": root / "history.jsonl",
        "usage": root / "usage.json",
        "events": root / "events.json",
        "rollout_cache": root / "rollout-cache.json",
        "policy_ack": root / "policy-ack.json",
        "lock": root / ".lock",
    }


@contextlib.contextmanager
def _locked() -> Iterator[dict[str, Path]]:
    paths = _paths()
    paths["root"].mkdir(parents=True, exist_ok=True)
    with paths["lock"].open("a+", encoding="utf-8") as lock:
        _lock_file(lock)
        try:
            yield paths
        finally:
            _unlock_file(lock)


def _lock_file(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is not None:  # pragma: no cover - exercised on Windows.
        handle.seek(0)
        if handle.read(1) == "":
            handle.seek(0)
            handle.write("\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_file(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - exercised on Windows.
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _load_json(path: Path, fallback: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def get_settings() -> dict[str, Any]:
    with _locked() as paths:
        stored = _load_json(paths["settings"], {})
    result = dict(DEFAULT_SETTINGS)
    if isinstance(stored, dict):
        result.update(stored)
    migrated = _normalize_mode_value(str(result.get("default_mode", "")))
    if migrated != result.get("default_mode"):
        result["default_mode"] = migrated
        with _locked() as paths:
            _atomic_json(paths["settings"], result)
    return result


def update_settings(**changes: Any) -> dict[str, Any]:
    allowed = set(DEFAULT_SETTINGS) - {"schema_version"}
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(unknown)}")
    if changes.get("default_mode") is not None:
        changes["default_mode"] = _normalize_mode_value(str(changes["default_mode"]))
    current = get_settings()
    current.update({key: value for key, value in changes.items() if value is not None})
    _validate_settings(current)
    with _locked() as paths:
        _atomic_json(paths["settings"], current)
    return current


def _validate_settings(settings: dict[str, Any]) -> None:
    if settings["default_mode"] not in MODES:
        raise ValueError(f"default_mode must be one of {', '.join(MODES)}")
    if settings["estimate_scope"] not in {"selected", "all"}:
        raise ValueError("estimate_scope must be selected or all")
    if settings["capacity_scope"] not in {"inherit", "selected", "all", "off"}:
        raise ValueError("capacity_scope must be inherit, selected, all, or off")
    if settings["show_picker"] not in {"always", "when_requested", "never"}:
        raise ValueError("show_picker must be always, when_requested, or never")
    if settings["capacity_unit"] not in {"tasks", "turns"}:
        raise ValueError("capacity_unit must be tasks or turns")
    if not isinstance(settings["telemetry_enabled"], bool):
        raise ValueError("telemetry_enabled must be true or false")
    if not isinstance(settings["show_disclaimer"], bool):
        raise ValueError("show_disclaimer must be true or false")
    if not isinstance(settings["history_limit"], int) or settings["history_limit"] < 10:
        raise ValueError("history_limit must be an integer of at least 10")


def _normalize_mode_value(mode: str) -> str:
    normalized = mode.strip().lower().replace("_", "-").replace(" ", "-")
    normalized = MODE_ALIASES.get(normalized, normalized)
    if normalized not in MODES:
        raise ValueError(f"Unknown mode {mode!r}; choose {', '.join(MODES)}")
    return normalized


def normalize_mode(mode: str | None) -> str:
    if not mode:
        return get_settings()["default_mode"]
    return _normalize_mode_value(mode)


def _range(value: float, spread: float = 0.24, minimum: float = 1.0) -> dict[str, float]:
    return {
        "low": round(max(minimum, value * (1.0 - spread)), 1),
        "high": round(max(minimum, value * (1.0 + spread)), 1),
    }


def _integer_range(value: float, spread: float = 0.28) -> dict[str, int]:
    return {
        "low": max(0, math.floor(value * (1.0 - spread))),
        "high": max(0, math.ceil(value * (1.0 + spread))),
    }


def _recent_history(limit: int = 100) -> list[dict[str, Any]]:
    path = _paths()["history"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
        except json.JSONDecodeError:
            continue
    return records


MODEL_CALIBRATION_MIN_SAMPLES = 3


def _history_complexity(item: dict[str, Any]) -> Any:
    """Read old and new automatic-task labels without rewriting history."""
    value = item.get("complexity")
    if value in ("unspecified", UNCLASSIFIED_COMPLEXITY):
        return UNCLASSIFIED_COMPLEXITY
    return value


def _forecast_residuals(items: list[dict[str, Any]], dimension: str) -> list[float]:
    values: list[float] = []
    for item in items:
        outcome = item.get("forecast_outcome")
        measured = outcome.get(dimension) if isinstance(outcome, dict) else None
        value = (
            measured.get("absolute_relative_error")
            if isinstance(measured, dict) and measured.get("available") is True
            else None
        )
        if (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and value >= 0
        ):
            values.append(float(value))
    return values


def _residual_interval_spread(values: list[float]) -> float | None:
    """Use the empirical 80th-percentile forecast miss as interval width."""
    if len(values) < MODEL_CALIBRATION_MIN_SAMPLES:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.8) - 1)
    return max(0.15, min(0.75, ordered[index]))


def _confidence_basis(
    interval_basis: str,
    samples: int,
    model_scope: str,
    time_samples: int,
    token_samples: int,
) -> str:
    if time_samples == 0 and token_samples == 0:
        return "first-use heuristic; no comparable history"
    if interval_basis == "forecast-residuals":
        noun = "forecast" if samples == 1 else "forecasts"
        prefix = f"{samples} paired audited {noun}"
    elif interval_basis == "mixed-residual-and-legacy-fallback":
        prefix = "partial residual audit; unaudited dimension widened"
    elif interval_basis == "legacy-variability-fallback":
        prefix = "legacy variability; residual audit pending"
    else:
        prefix = "limited history; residual audit pending"
    if model_scope == "cross-model-fallback":
        return f"{prefix}; cross-model confidence capped low"
    return prefix


def _history_calibration(
    mode: str,
    complexity: str,
    model: str | None = None,
) -> tuple[
    float, float, int, int, str, str, float | None, float | None,
    int, int, float | None, float | None,
]:
    """Calibrate from history, and SAY which history it used.

    A single blended number cannot distinguish "three runs on this exact model"
    from "three runs on a model four times slower", yet those imply very
    different estimates. So the scope is returned alongside the values and the
    caller reports it: `model-specific` when there is enough same-model history,
    `cross-model-fallback` when there is history but not for this model (which
    caps confidence at low), `heuristic-no-history` when there is none.

    Time and token sample counts are returned separately because a record can
    carry an elapsed time without a token total, and collapsing them to one
    count overstates whichever is scarcer.
    """
    completed = [
        item for item in _recent_history()
        if _history_mode(item) == mode and item.get("status") == "completed"
    ]
    exact = [item for item in completed if _history_complexity(item) == complexity]
    if exact:
        comparable = exact
        complexity_scope = "unclassified" if complexity == UNCLASSIFIED_COMPLEXITY else "exact"
    else:
        comparable = [
            item for item in completed
            if _history_complexity(item) == UNCLASSIFIED_COMPLEXITY
        ]
        complexity_scope = "unclassified-fallback" if comparable else "no-history"

    model_scope = "all-models"
    if model:
        model_specific = [item for item in comparable if item.get("model") == model]
        if len(model_specific) >= MODEL_CALIBRATION_MIN_SAMPLES:
            comparable, model_scope = model_specific, "model-specific"
        elif comparable:
            model_scope = "cross-model-fallback"
        else:
            model_scope = "heuristic-no-history"

    times = [
        float(value) / 60
        for item in comparable
        for value in (item.get("elapsed_seconds"),)
        if (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and value > 0
        )
    ]
    tokens = [
        float(value)
        for item in comparable
        for token_data in (item.get("tokens"),)
        for value in (token_data.get("total") if isinstance(token_data, dict) else None,)
        if (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and value > 0
        )
    ]
    time_value = statistics.median(times) if times else 0.0
    token_value = statistics.median(tokens) if tokens else 0.0
    time_residuals = _forecast_residuals(comparable, "time")
    token_residuals = _forecast_residuals(comparable, "tokens")
    return (
        time_value, token_value, len(times), len(tokens), model_scope, complexity_scope,
        _robust_relative_spread(times), _robust_relative_spread(tokens),
        len(time_residuals), len(token_residuals),
        _residual_interval_spread(time_residuals),
        _residual_interval_spread(token_residuals),
    )


def _history_mode(item: dict[str, Any]) -> str | None:
    try:
        return _normalize_mode_value(str(item.get("mode", "")))
    except ValueError:
        return None


def _robust_relative_spread(values: list[float]) -> float | None:
    """Return a bounded, outlier-resistant relative spread for 3+ samples."""
    if len(values) < MODEL_CALIBRATION_MIN_SAMPLES:
        return None
    center = statistics.median(values)
    if center <= 0:
        return None
    mad = statistics.median(abs(value - center) for value in values)
    return max(0.15, min(0.60, 1.4826 * mad / center))


def _rate_limit_poll_status() -> dict[str, Any]:
    usage = _load_json(_paths()["usage"], {})
    stored = usage.get("rate_limit_poll", {}) if isinstance(usage, dict) else {}
    stored = stored if isinstance(stored, dict) else {}
    return {
        "status": stored.get("status", "not-started"),
        "last_attempt_at": stored.get("last_attempt_at"),
        "last_success_at": stored.get("last_success_at"),
        "last_error": stored.get("last_error"),
    }


def _task_key_from_refs(session_ref: str | None, turn_ref: str | None) -> str | None:
    if not session_ref or not turn_ref:
        return None
    return opaque_ref(f"{session_ref}:{turn_ref}", "task")


def _record_ref(record: dict[str, Any], namespace: str) -> str | None:
    stored = record.get(f"{namespace}_ref")
    if stored:
        return str(stored)
    return opaque_ref(record.get(f"{namespace}_id"), namespace)


def _load_active(paths: dict[str, Path]) -> dict[str, Any]:
    """Load active timers and migrate legacy raw identifiers and transcript paths."""

    loaded = _load_json(paths["active"], {})
    if not isinstance(loaded, dict):
        loaded = {}
    sanitized: dict[str, Any] = {}
    changed = False
    for old_key, value in loaded.items():
        if not isinstance(value, dict):
            changed = True
            continue
        record = dict(value)
        session_ref = _record_ref(record, "session")
        turn_ref = _record_ref(record, "turn")
        if session_ref:
            record["session_ref"] = session_ref
        if turn_ref:
            record["turn_ref"] = turn_ref
        for field in ("session_id", "turn_id", "transcript_path"):
            if field in record:
                record.pop(field, None)
                changed = True
        new_key = str(old_key)
        if record.get("source") == "hook":
            new_key = _task_key_from_refs(session_ref, turn_ref) or new_key
            if record.get("task_id") == old_key:
                record["task_id"] = new_key
        if new_key != old_key:
            changed = True
        existing = sanitized.get(new_key)
        if not isinstance(existing, dict) or (
            (_epoch(record.get("started_at")) or 0)
            >= (_epoch(existing.get("started_at")) or 0)
        ):
            sanitized[new_key] = record
    if changed:
        _atomic_json(paths["active"], sanitized)
    return sanitized


def _record_rate_limit_poll(**changes: Any) -> dict[str, Any]:
    with _locked() as paths:
        usage = _load_json(paths["usage"], {})
        if not isinstance(usage, dict):
            usage = {}
        stored = usage.get("rate_limit_poll", {})
        status = {
            "status": stored.get("status", "not-started") if isinstance(stored, dict) else "not-started",
            "last_attempt_at": stored.get("last_attempt_at") if isinstance(stored, dict) else None,
            "last_success_at": stored.get("last_success_at") if isinstance(stored, dict) else None,
            "last_error": stored.get("last_error") if isinstance(stored, dict) else None,
        }
        status.update(changes)
        usage["rate_limit_poll"] = status
        _atomic_json(paths["usage"], usage)
    return status


def _sanitized_poll_error(error: Exception) -> dict[str, str]:
    if isinstance(error, TimeoutError):
        code, message = "timeout", "The Codex App Server rate-limit request timed out."
    elif isinstance(error, PermissionError):
        code, message = "permission-denied", "The host denied the rate-limit polling process."
    elif isinstance(error, FileNotFoundError):
        code, message = "codex-command-unavailable", "The Codex command was not available to the plugin."
    elif isinstance(error, RuntimeError):
        code, message = "app-server-error", "The Codex App Server rejected or returned an invalid rate-limit response."
    elif isinstance(error, OSError):
        code, message = "process-error", "The rate-limit polling process could not run."
    else:
        code, message = "unexpected-error", "Rate-limit polling failed unexpectedly."
    return {"code": code, "message": message, "at": _now()}


def refresh_usage_snapshot(
    *,
    force: bool = False,
    timeout: float = 5.0,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """Refresh Codex allowance state and persist sanitized polling health."""
    if os.environ.get("AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH") == "1":
        return _load_json(_paths()["usage"], {})
    usage = _load_json(_paths()["usage"], {})
    updated = _epoch(usage.get("rate_limits_updated_at")) if isinstance(usage, dict) else None
    age = max(0.0, time.time() - updated) if updated is not None else None
    if not force and age is not None and age <= USAGE_SNAPSHOT_MAX_AGE_SECONDS:
        return usage

    _record_rate_limit_poll(status="polling", last_attempt_at=_now())
    try:
        snapshot = fetch_rate_limits(timeout=timeout, version=VERSION)
    except Exception as error:
        _record_rate_limit_poll(status="degraded", last_error=_sanitized_poll_error(error))
        if raise_on_error:
            raise
        return _load_json(_paths()["usage"], {})
    ingest_usage(rate_limits=snapshot)
    _record_rate_limit_poll(status="ok", last_success_at=_now(), last_error=None)
    return _load_json(_paths()["usage"], {})


def _window_snapshot(
    explicit: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return the rate-limit windows, and whether they can be trusted.

    Freshness is tracked PER SOURCE and PER WINDOW, not per file. Two bugs live
    in the simpler design, and both fail toward false confidence:

      1. One `updated_at` for the whole usage blob means ingesting TOKENS
         restamps it, and the rate limits -- untouched, possibly hours old --
         start reading as fresh. Token counts say nothing about quota.
      2. A sparse update that mentions only `primary` would restamp the file
         and relabel an omitted, stale `secondary` as current.

    So rate limits carry their own `rate_limits_updated_at`, each window carries
    its own `captured_at`, and the snapshot is fresh only when EVERY stored
    window is. Anything less reports quota headroom that was never measured.
    Ported from Robbie's branch; docs/TRIAGE-PR11-2026-08-18.md block A.
    """
    if explicit is not None:
        captured = None
        if isinstance(explicit, dict):
            captured = _epoch(explicit.get("captured_at", explicit.get("updated_at")))
        captured = captured or time.time()
        age = max(0, int(time.time() - captured))
        fresh = age <= USAGE_SNAPSHOT_MAX_AGE_SECONDS
        normalized = _normalize_rate_limits(explicit) if fresh else []
        stamp = dt.datetime.fromtimestamp(captured, dt.timezone.utc).isoformat()
        for item in normalized:
            item["captured_at"] = stamp
        return normalized, {
            "source": "caller-provided rate-limit snapshot",
            "captured_at": stamp,
            "age_seconds": age,
            "fresh": fresh,
        }

    usage = _load_json(_paths()["usage"], {})
    windows = usage.get("rate_limits", []) if isinstance(usage, dict) else []
    updated = _epoch(usage.get("rate_limits_updated_at")) if isinstance(usage, dict) else None
    age = max(0, int(time.time() - updated)) if updated is not None else None
    fresh = age is not None and age <= USAGE_SNAPSHOT_MAX_AGE_SECONDS

    active_windows: list[dict[str, Any]] = []
    stored = 0
    if isinstance(windows, list):
        for item in windows:
            if not isinstance(item, dict):
                continue
            stored += 1
            item_updated = _epoch(item.get("captured_at")) or updated
            item_age = (max(0, int(time.time() - item_updated))
                        if item_updated is not None else None)
            if item_age is not None and item_age <= USAGE_SNAPSHOT_MAX_AGE_SECONDS:
                active_windows.append(item)
    # Every stored window must be fresh. A partially-fresh snapshot is a
    # snapshot of nothing: the constraining window may be the stale one.
    fresh = fresh and stored > 0 and len(active_windows) == stored

    metadata = {
        "source": "Codex App Server rate-limit snapshot",
        "captured_at": usage.get("rate_limits_updated_at") if isinstance(usage, dict) else None,
        "age_seconds": age,
        "fresh": fresh,
    }
    return (active_windows if fresh else []), metadata


def _active_windows(explicit: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return _window_snapshot(explicit)[0]


def _observed_percent_cost(mode: str, limit_id: str | None = None) -> tuple[float | None, int]:
    mode = normalize_mode(mode)
    values: list[float] = []
    for item in _recent_history(200):
        if _history_mode(item) != mode:
            continue
        deltas = item.get("usage_delta")
        for delta in deltas if isinstance(deltas, list) else []:
            if not isinstance(delta, dict) or delta.get("same_window") is not True:
                continue
            if limit_id is not None and str(delta.get("limit_id")) != limit_id:
                continue
            value = delta.get("used_percent_delta")
            if (
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)) and value > 0
            ):
                values.append(float(value))
    if not values:
        return None, 0
    return statistics.median(values), len(values)


def _observed_burn(mode: str, limit_id: str) -> tuple[float | None, int]:
    mode = normalize_mode(mode)
    rates: list[float] = []
    for item in _recent_history(200):
        if _history_mode(item) != mode or item.get("status") != "completed":
            continue
        elapsed = item.get("elapsed_seconds")
        if not isinstance(elapsed, (int, float)) or elapsed <= 0:
            continue
        deltas = item.get("usage_delta")
        for delta in deltas if isinstance(deltas, list) else []:
            if not isinstance(delta, dict) or delta.get("same_window") is not True:
                continue
            if str(delta.get("limit_id")) != str(limit_id):
                continue
            value = delta.get("used_percent_delta")
            if (
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)) and value > 0
            ):
                rates.append(float(value) * 3600 / float(elapsed))
    if not rates:
        return None, 0
    return statistics.median(rates), len(rates)


def estimate_capacity(mode: str, windows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    mode = normalize_mode(mode)
    active, snapshot = _window_snapshot(windows)
    fallback_cost = {"throughput": 8.0, "balanced": 5.0, "stretch": 2.8}[mode]
    estimates: list[dict[str, Any]] = []
    for index, window in enumerate(active):
        used = window.get("used_percent", window.get("usedPercent"))
        if not isinstance(used, (int, float)):
            continue
        limit_id = window.get("limit_id", window.get("limitId", f"window-{index + 1}"))
        observed, samples = _observed_percent_cost(mode, str(limit_id))
        expected_cost = observed if observed is not None else fallback_cost
        remaining = max(0.0, 100.0 - float(used))
        center = remaining / max(expected_cost, 0.01)
        burn, burn_samples = _observed_burn(mode, str(limit_id))
        reset_epoch = _epoch(window.get("resets_at", window.get("resetsAt")))
        seconds_to_reset = max(0, int(reset_epoch - time.time())) if reset_epoch is not None else None
        credits = window.get("credits")
        can_continue_after_cap = bool(
            isinstance(credits, dict)
            and (credits.get("unlimited") is True or credits.get("hasCredits") is True)
            and window.get("spend_control_reached", window.get("spendControlReached")) is not True
        )
        duration_mins = window.get("window_duration_mins", window.get("windowDurationMins"))
        pace_ratio = None
        if isinstance(duration_mins, (int, float)) and duration_mins >= 1440 and reset_epoch:
            window_start = reset_epoch - float(duration_mins) * 60
            elapsed_fraction = max(0.0, min(1.0, (time.time() - window_start) / (float(duration_mins) * 60)))
            if elapsed_fraction > 0.01:
                pace_ratio = float(used) / (elapsed_fraction * 100)
        hours_to_cap = remaining / burn if burn and burn > 0 else None
        # What running out actually COSTS is the time spent blocked waiting for the
        # reset, not the bare fact that the cap lands first. Anything at or below
        # zero means the window rolls over before the allowance runs out.
        if can_continue_after_cap:
            verdict = "cost-ahead"
            blocked_hours = 0.0
        elif hours_to_cap is None or seconds_to_reset is None:
            verdict = "insufficient-burn-history"
            blocked_hours = None
        else:
            blocked_hours = seconds_to_reset / 3600.0 - hours_to_cap
            if blocked_hours <= 0:
                verdict = "no-lockout"
                blocked_hours = 0.0
            elif blocked_hours >= LOCKOUT_WARN_HOURS:
                verdict = "lockout"
            else:
                verdict = "brief-lockout"
        estimates.append({
            "limit_id": limit_id,
            "used_percent": round(float(used), 2),
            "used_percent_label": "measured",
            "remaining_percent": round(remaining, 2),
            "remaining_percent_label": "derived from measured usage",
            "window_duration_mins": window.get("window_duration_mins", window.get("windowDurationMins")),
            "resets_at": window.get("resets_at", window.get("resetsAt")),
            "window_role": window.get("window_role", window.get("windowRole")),
            "plan_type": window.get("plan_type", window.get("planType")),
            "can_continue_after_cap": can_continue_after_cap,
            "pace_ratio": round(pace_ratio, 3) if pace_ratio is not None else None,
            "estimated_remaining": _integer_range(center, 0.34 if samples < 5 else 0.22),
            "estimated_remaining_label": "estimated",
            "expected_task_cost_percent": round(expected_cost, 3),
            "task_cost_basis": "observed comparable turns" if observed is not None else "first-use heuristic",
            "task_cost_sample_count": samples,
            "burn_percent_per_hour": round(burn, 3) if burn is not None else None,
            "burn_sample_count": burn_samples,
            "hours_to_cap": round(hours_to_cap, 2) if hours_to_cap is not None else None,
            "seconds_to_reset": seconds_to_reset,
            "blocked_hours": round(blocked_hours, 2) if blocked_hours is not None else None,
            "forecast_verdict": verdict,
        })
    if not estimates:
        reason = "No active usage-window percentage or credit balance is available."
        if snapshot["age_seconds"] is not None and not snapshot["fresh"]:
            reason = (
                f"The saved App Server usage snapshot is {snapshot['age_seconds']} seconds old; "
                f"refresh it before estimating capacity (maximum age {USAGE_SNAPSHOT_MAX_AGE_SECONDS} seconds)."
            )
        return {
            "available": False,
            "status": "UNCALIBRATED",
            "confidence": "low",
            "reason": reason,
            "measurement_source": snapshot["source"],
            "snapshot_age_seconds": snapshot["age_seconds"],
            "disclaimer": DISCLAIMER,
        }
    hard_walls = [item for item in estimates if not item["can_continue_after_cap"]]
    candidates = hard_walls or estimates
    constraining = min(candidates, key=lambda item: item["estimated_remaining"]["low"])
    samples = constraining["task_cost_sample_count"]
    observed_basis = constraining["task_cost_basis"] == "observed comparable turns"
    confidence = "high" if samples >= 15 else "medium" if samples >= 5 else "low"
    return {
        "available": True,
        "status": "estimated",
        "confidence": confidence,
        "basis": constraining["task_cost_basis"],
        "calibration_status": "observed" if observed_basis else "heuristic",
        "sample_count": samples,
        "measurement_source": snapshot["source"],
        "snapshot_age_seconds": snapshot["age_seconds"],
        "constraining_window": constraining["limit_id"],
        "estimated_remaining": constraining["estimated_remaining"],
        "windows": estimates,
        "disclaimer": DISCLAIMER,
    }


def estimate_mode(
    mode: str,
    *,
    complexity: str = "moderate",
    context: str = "medium",
    model: str | None = None,
    reasoning_effort: str = "medium",
    execution_location: str = "local",
    fast_mode: bool = False,
    expected_tools: int = 3,
    expected_subagents: int = 0,
    expected_media: int = 0,
    expected_output_tokens: int | None = None,
    cached_ratio: float = 0.25,
    rate_limits: list[dict[str, Any]] | None = None,
    include_capacity: bool = True,
) -> dict[str, Any]:
    mode = normalize_mode(mode)
    allowed_complexities = (*COMPLEXITY_BASE, UNCLASSIFIED_COMPLEXITY)
    if complexity not in allowed_complexities:
        raise ValueError(f"complexity must be one of {', '.join(allowed_complexities)}")
    if context not in CONTEXT_FACTOR:
        raise ValueError(f"context must be one of {', '.join(CONTEXT_FACTOR)}")
    if reasoning_effort not in REASONING_FACTOR:
        raise ValueError(f"reasoning_effort must be one of {', '.join(REASONING_FACTOR)}")
    if execution_location not in LOCATION_FACTOR:
        raise ValueError(f"execution_location must be one of {', '.join(LOCATION_FACTOR)}")
    cached_ratio = min(1.0, max(0.0, float(cached_ratio)))
    expected_tools = max(0, int(expected_tools))
    expected_subagents = max(0, int(expected_subagents))
    expected_media = max(0, int(expected_media))

    heuristic_complexity = "moderate" if complexity == UNCLASSIFIED_COMPLEXITY else complexity
    base_minutes, base_tokens = COMPLEXITY_BASE[heuristic_complexity]
    info = MODE_INFO[mode]
    context_factor = CONTEXT_FACTOR[context]
    reasoning_factor = REASONING_FACTOR[reasoning_effort]
    location_factor = LOCATION_FACTOR[execution_location]
    tool_factor = 1.0 + min(expected_tools, 20) * 0.035
    subagent_factor = 1.0 + min(expected_subagents, 8) * 0.20
    media_factor = 1.0 + min(expected_media, 5) * 0.28
    fast_time_factor = 0.68 if fast_mode else 1.0
    fast_cost_factor = 1.65 if fast_mode else 1.0

    minutes = (
        base_minutes * info["time_factor"] * context_factor * location_factor * tool_factor
        * (1.0 + expected_subagents * 0.04) * fast_time_factor
    )
    tokens = (
        base_tokens * info["token_factor"] * context_factor * reasoning_factor * tool_factor
        * subagent_factor * media_factor * fast_cost_factor
    )
    tokens *= 1.0 - 0.35 * cached_ratio
    if expected_output_tokens:
        tokens += max(0, expected_output_tokens) * reasoning_factor * info["token_factor"]

    (calibrated_minutes, calibrated_tokens, time_samples, token_samples,
     model_scope, complexity_scope, time_spread, token_spread,
     time_residual_samples, token_residual_samples,
     time_residual_spread, token_residual_spread) = _history_calibration(mode, complexity, model)
    if calibrated_minutes:
        minutes = 0.55 * minutes + 0.45 * calibrated_minutes
    if calibrated_tokens:
        tokens = 0.55 * tokens + 0.45 * calibrated_tokens
    # The public estimate always includes both time and tokens, so its overall
    # confidence may not outrun the less-audited dimension.
    samples = min(time_residual_samples, token_residual_samples)
    time_interval_spread = (
        time_residual_spread
        if time_residual_spread is not None
        else max(time_spread or 0.30, 0.30)
    )
    token_interval_spread = (
        token_residual_spread
        if token_residual_spread is not None
        else max(token_spread or 0.30, 0.30)
    )
    spread = max(time_interval_spread, token_interval_spread)
    if time_residual_spread is not None and token_residual_spread is not None:
        interval_basis = "forecast-residuals"
    elif time_residual_spread is not None or token_residual_spread is not None:
        interval_basis = "mixed-residual-and-legacy-fallback"
    else:
        interval_basis = (
            "legacy-variability-fallback"
            if time_spread is not None or token_spread is not None
            else "heuristic"
        )
    confidence = (
        "high" if samples >= 10 and spread <= 0.20
        else "medium" if samples >= 5 and spread <= 0.35
        else "low"
    )
    if model_scope in ("cross-model-fallback", "heuristic-no-history"):
        # History exists, but not for the model being asked about. Borrowing it
        # is better than nothing and worse than measuring, so the estimate still
        # uses it -- while refusing to call the result anything but low.
        confidence = "low"
        spread = max(spread, 0.30)

    result: dict[str, Any] = {
        "mode": mode,
        "label": info["label"],
        "objective": info["objective"],
        "behavior": info["behavior"],
        "tradeoff": info["tradeoff"],
        "eta_minutes": _range(minutes, spread),
        "estimated_tokens": {
            "low": int(max(1_000, tokens * (1.0 - spread))),
            "high": int(max(1_000, tokens * (1.0 + spread))),
        },
        "confidence": confidence,
        "confidence_basis": _confidence_basis(
            interval_basis, samples, model_scope, time_samples, token_samples
        ),
        "calibration_samples": samples,
        "calibration": {
            "model_scope": model_scope,
            "complexity_scope": complexity_scope,
            "time_samples": time_samples,
            "token_samples": token_samples,
            "time_residual_samples": time_residual_samples,
            "token_residual_samples": token_residual_samples,
            "time_interval_spread": round(time_interval_spread, 3),
            "token_interval_spread": round(token_interval_spread, 3),
            "interval_basis": interval_basis,
            "empirical_spread": round(spread, 3),
        },
        "assumptions": {
            "complexity": complexity,
            "heuristic_complexity": heuristic_complexity,
            "context": context,
            "model": model or "current",
            "reasoning_effort": reasoning_effort,
            "execution_location": execution_location,
            "fast_mode": fast_mode,
            "expected_tools": expected_tools,
            "expected_subagents": expected_subagents,
            "expected_media": expected_media,
            "cached_ratio": cached_ratio,
        },
    }
    if include_capacity:
        result["capacity"] = estimate_capacity(mode, rate_limits)
    return result


def show_control_center(
    task: str = "",
    selected_mode: str | None = None,
    estimate_scope: str | None = None,
    capacity_scope: str | None = None,
    picker_requested: bool = False,
    **inputs: Any,
) -> dict[str, Any]:
    refresh_usage_snapshot()
    settings = get_settings()
    if not isinstance(picker_requested, bool):
        raise ValueError("picker_requested must be true or false")
    picker_policy = settings["show_picker"]
    render_picker = (
        picker_policy == "always"
        or (picker_policy == "when_requested" and picker_requested)
    )
    selected = normalize_mode(selected_mode or settings["default_mode"])
    scope = estimate_scope or settings["estimate_scope"]
    if scope not in {"selected", "all"}:
        raise ValueError("estimate_scope must be selected or all")
    cap_scope = capacity_scope or settings["capacity_scope"]
    if cap_scope == "inherit":
        cap_scope = scope
    if cap_scope not in {"selected", "all", "off"}:
        raise ValueError("capacity_scope must be selected, all, off, or inherit")
    modes = [selected] if scope == "selected" else list(MODES)
    estimates = []
    for mode in modes:
        include_capacity = cap_scope == "all" or (cap_scope == "selected" and mode == selected)
        estimates.append(estimate_mode(mode, include_capacity=include_capacity, **inputs))
    return {
        "product": "Agent Efficiency",
        "version": VERSION,
        "task_summary": task.strip()[:160] if task else "Current task",
        "selected_mode": selected,
        "default_mode": settings["default_mode"],
        "estimate_scope": scope,
        "capacity_scope": cap_scope,
        "show_picker": picker_policy,
        "render_picker": render_picker,
        "telemetry_status": {"rate_limit_poll": _rate_limit_poll_status()},
        "modes": estimates,
        "descriptions": [
            {key: MODE_INFO[mode][key] for key in ("label", "objective", "behavior", "tradeoff")}
            | {"mode": mode}
            for mode in MODES
        ],
        "disclaimer": DISCLAIMER if settings["show_disclaimer"] else None,
    }


def _event_state(paths: dict[str, Path]) -> dict[str, Any]:
    value = _load_json(paths["events"], {"sessions": {}})
    return value if isinstance(value, dict) else {"sessions": {}}


FORECAST_SCHEMA_VERSION = 1


def _window_instance(window: Any) -> str | None:
    """Return an opaque identity for one allowance window/reset cycle."""
    if not isinstance(window, dict):
        return None
    limit_id = window.get("limit_id", window.get("limitId"))
    reset_epoch = _epoch(window.get("resets_at", window.get("resetsAt")))
    if limit_id is None or reset_epoch is None or not math.isfinite(reset_epoch):
        return None
    return opaque_ref(f"{limit_id}:{reset_epoch:.3f}", "window")


def _capacity_forecast_snapshot(capacity: Any) -> dict[str, Any]:
    """Keep only the numeric provenance needed to audit a capacity forecast.

    The full control-center result contains user-facing prose and several fields
    that do not help calibration. A history record needs the prediction and its
    basis, not a copy of the presentation layer.
    """
    if not isinstance(capacity, dict):
        return {"available": False, "status": "unavailable"}
    result: dict[str, Any] = {
        "available": bool(capacity.get("available")),
        "status": capacity.get("status"),
        "confidence": capacity.get("confidence"),
        "calibration_status": capacity.get("calibration_status"),
        "sample_count": capacity.get("sample_count", 0),
        "measurement_source": capacity.get("measurement_source"),
        "snapshot_age_seconds": capacity.get("snapshot_age_seconds"),
    }
    if not result["available"]:
        return result
    windows = capacity.get("windows")
    if not isinstance(windows, list):
        return {
            **result,
            "available": False,
            "status": "malformed-capacity-forecast",
        }
    result.update({
        "basis": capacity.get("basis"),
        "constraining_window": capacity.get("constraining_window"),
        "estimated_remaining": capacity.get("estimated_remaining"),
        "windows": [
            {
                "limit_id": window.get("limit_id"),
                "window_instance": _window_instance(window),
                "expected_task_cost_percent": window.get("expected_task_cost_percent"),
                "task_cost_basis": window.get("task_cost_basis"),
                "task_cost_sample_count": window.get("task_cost_sample_count", 0),
            }
            for window in windows
            if isinstance(window, dict)
        ],
    })
    return result


def _forecast_snapshot(mode: str, complexity: str, model: str | None) -> dict[str, Any]:
    """Create a privacy-safe forecast without making task startup depend on it."""
    try:
        estimate = estimate_mode(
            mode,
            complexity=complexity,
            model=model,
            include_capacity=True,
        )
        if not isinstance(estimate, dict):
            raise TypeError("estimator result must be an object")
        snapshot = {
            "schema_version": FORECAST_SCHEMA_VERSION,
            "estimator_version": VERSION,
            "created_at": _now(),
            "available": True,
            "mode": estimate["mode"],
            "complexity": complexity,
            "model": model,
            "eta_minutes": estimate["eta_minutes"],
            "estimated_tokens": estimate["estimated_tokens"],
            "confidence": estimate["confidence"],
            "calibration": estimate["calibration"],
            "assumptions": estimate["assumptions"],
            "capacity": _capacity_forecast_snapshot(estimate.get("capacity")),
        }
        if not snapshot["capacity"].get("available") and snapshot["capacity"].get("status") == "malformed-capacity-forecast":
            raise TypeError("malformed capacity forecast")
        return snapshot
    except Exception:
        # Forecasting is optional telemetry. Corrupt history or a malformed
        # allowance snapshot must not turn the timer -- or a prompt hook -- into
        # a failure. Do not persist exception text: it can contain local paths.
        return {
            "schema_version": FORECAST_SCHEMA_VERSION,
            "estimator_version": VERSION,
            "created_at": _now(),
            "available": False,
            "status": "forecast-generation-failed",
        }
def _range_forecast_outcome(actual: Any, predicted: Any, *, actual_key: str) -> dict[str, Any]:
    if (
        not isinstance(actual, (int, float)) or isinstance(actual, bool)
        or not math.isfinite(float(actual)) or actual <= 0
    ):
        return {"available": False, "reason": f"No measured {actual_key} was recorded."}
    if not isinstance(predicted, dict):
        return {"available": False, "reason": f"No {actual_key} forecast was recorded."}
    low, high = predicted.get("low"), predicted.get("high")
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(float(value))
        for value in (low, high)
    ):
        return {"available": False, "reason": f"The {actual_key} forecast range is invalid."}
    low_value, high_value = float(low), float(high)
    if low_value <= 0 or high_value < low_value:
        return {"available": False, "reason": f"The {actual_key} forecast range is invalid."}
    actual_value = float(actual)
    midpoint = (low_value + high_value) / 2
    signed_error = (actual_value - midpoint) / midpoint
    return {
        "available": True,
        actual_key: round(actual_value, 3),
        "predicted": {"low": low, "high": high},
        "predicted_midpoint": round(midpoint, 3),
        "within_range": low_value <= actual_value <= high_value,
        "relative_error": round(signed_error, 4),
        "absolute_relative_error": round(abs(signed_error), 4),
    }


def _capacity_forecast_outcomes(forecast: dict[str, Any], usage_delta: Any) -> list[dict[str, Any]]:
    capacity = forecast.get("capacity")
    if not isinstance(capacity, dict) or not capacity.get("available"):
        return []
    forecast_windows = capacity.get("windows")
    measured_items = usage_delta if isinstance(usage_delta, list) else []
    if not isinstance(forecast_windows, list):
        return []
    measured = {
        (str(item.get("limit_id")), item.get("window_instance")): item.get("used_percent_delta")
        for item in measured_items
        if (
            isinstance(item, dict) and item.get("limit_id") is not None
            and item.get("same_window") is True and item.get("window_instance") is not None
        )
    }
    outcomes = []
    for window in forecast_windows:
        if not isinstance(window, dict):
            continue
        limit_id = str(window.get("limit_id"))
        expected = window.get("expected_task_cost_percent")
        window_instance = window.get("window_instance")
        actual = measured.get((limit_id, window_instance))
        outcome: dict[str, Any] = {
            "limit_id": window.get("limit_id"),
            "window_instance": window_instance,
            "expected_task_cost_percent": expected,
            "task_cost_basis": window.get("task_cost_basis"),
            "task_cost_sample_count": window.get("task_cost_sample_count", 0),
        }
        if (
            isinstance(expected, (int, float)) and not isinstance(expected, bool)
            and math.isfinite(float(expected)) and expected > 0
            and isinstance(actual, (int, float)) and not isinstance(actual, bool)
            and math.isfinite(float(actual)) and actual > 0
        ):
            signed_error = (float(actual) - float(expected)) / float(expected)
            outcome.update({
                "available": True,
                "actual_used_percent_delta": round(float(actual), 3),
                "relative_error": round(signed_error, 4),
                "absolute_relative_error": round(abs(signed_error), 4),
            })
        else:
            outcome.update({
                "available": False,
                "reason": "No positive same-window allowance delta was measurable.",
            })
        outcomes.append(outcome)
    return outcomes


def _forecast_outcome(record: dict[str, Any]) -> dict[str, Any] | None:
    forecast = record.get("forecast")
    if not isinstance(forecast, dict) or not forecast.get("available"):
        return None
    elapsed = record.get("elapsed_seconds")
    elapsed_minutes = float(elapsed) / 60 if isinstance(elapsed, (int, float)) and elapsed > 0 else None
    token_total = record.get("tokens", {}).get("total")
    time_outcome = _range_forecast_outcome(
        elapsed_minutes,
        forecast.get("eta_minutes"),
        actual_key="actual_minutes",
    )
    token_outcome = _range_forecast_outcome(
        token_total,
        forecast.get("estimated_tokens"),
        actual_key="actual_tokens",
    )
    auditable = [item for item in (time_outcome, token_outcome) if item.get("available")]
    return {
        "schema_version": FORECAST_SCHEMA_VERSION,
        "time": time_outcome,
        "tokens": token_outcome,
        "capacity": _capacity_forecast_outcomes(forecast, record.get("usage_delta")),
        "range_dimensions_audited": len(auditable),
        "range_dimensions_covered": sum(bool(item.get("within_range")) for item in auditable),
    }


def start_task(
    *,
    task_id: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    mode: str | None = None,
    complexity: str = "moderate",
    model: str | None = None,
    source: str = "manual",
    transcript_path: str | None = None,
    rollout_offset: int = 0,
) -> dict[str, Any]:
    settings = get_settings()
    chosen = normalize_mode(mode or settings["default_mode"])
    allowed_complexities = (*COMPLEXITY_BASE, UNCLASSIFIED_COMPLEXITY)
    if complexity not in allowed_complexities:
        raise ValueError(f"complexity must be one of {', '.join(allowed_complexities)}")
    session_ref = opaque_ref(session_id, "session")
    turn_ref = opaque_ref(turn_id, "turn")
    key = task_id or _task_key_from_refs(session_ref, turn_ref) or str(uuid.uuid4())
    record = {
        "record_schema_version": RECORD_SCHEMA_VERSION,
        "task_id": key,
        "session_ref": session_ref,
        "turn_ref": turn_ref,
        "mode": chosen,
        "complexity": complexity,
        "model": model,
        "source": source,
        "rollout_offset": max(0, int(rollout_offset)),
        "started_at": _now(),
        "started_monotonic": time.monotonic(),
        "tool_calls": 0,
        "tool_failures": 0,
        "subagents": 0,
        "compactions": 0,
        "forecast": _forecast_snapshot(chosen, complexity, model),
    }
    with _locked() as paths:
        active = _load_active(paths)
        if key in active:
            return active[key]
        active[key] = record
        _atomic_json(paths["active"], active)
    return record


TOKEN_COMPONENT_FIELDS = ("input", "cached_input", "cache_write_input",
                          "reasoning", "output", "model_calls")


def _nonnegative_int(value: Any, field: str) -> int:
    """Coerce a token count, rejecting anything that is not a whole count.

    A bare `int(value)` accepts all three of the things a token count must never
    be: `-1` passes, `1.5` is silently truncated, and `True` is an int in Python
    so a boolean sails through as 1. Each of those turns a caller's bug into a
    plausible-looking number, which is the failure mode this project keeps
    rediscovering (FINDINGS 11, and again in FINDINGS 22's day bucket).

    Ported from Robbie's `robbie/codex-backlog`; see
    docs/TRIAGE-PR11-2026-08-18.md block A.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise ValueError(f"{field} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if number < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return number


def _token_total(tokens: dict[str, Any] | None, *,
                 authoritative_total: bool = False) -> dict[str, int]:
    """Normalise and VALIDATE a token payload.

    `authoritative_total` is for sources that report a total we do not get to
    recompute -- the App Server reports its own. Everywhere else an explicit
    total that disagrees with input+output is a caller bug, and saying so beats
    storing a number that cannot be reconciled later.
    """
    source = tokens or {}
    if not isinstance(source, dict):
        raise ValueError("tokens must be an object")
    result = {
        "input": _nonnegative_int(source.get("input", source.get("input_tokens")), "input tokens"),
        "cached_input": _nonnegative_int(source.get("cached_input", source.get("cached_input_tokens")), "cached input tokens"),
        "cache_write_input": _nonnegative_int(source.get("cache_write_input", source.get("cache_write_input_tokens")), "cache write input tokens"),
        "reasoning": _nonnegative_int(source.get("reasoning", source.get("reasoning_tokens")), "reasoning tokens"),
        "output": _nonnegative_int(source.get("output", source.get("output_tokens")), "output tokens"),
        "model_calls": _nonnegative_int(source.get("model_calls"), "model calls"),
    }
    explicit_total = source.get("total", source.get("total_tokens"))
    computed_total = result["input"] + result["output"]
    if explicit_total is None:
        result["total"] = computed_total
    else:
        result["total"] = _nonnegative_int(explicit_total, "total tokens")
        if not authoritative_total and result["total"] != computed_total:
            raise ValueError("total tokens must equal input plus output tokens")
    return result


def _merge_tokens(existing: Any, updates: dict[str, Any]) -> dict[str, int]:
    """Sparse update: fields absent from `updates` keep their existing value.

    Replacing wholesale is what dropped measured tokens on CLI completion --
    a partial payload overwrote a full one and the measurement was gone.
    """
    if not isinstance(updates, dict):
        raise ValueError("tokens must be an object")
    base = _token_total(existing if isinstance(existing, dict) else None)
    merged = {field: base[field] for field in TOKEN_COMPONENT_FIELDS}
    aliases = {
        "input": ("input", "input_tokens"),
        "cached_input": ("cached_input", "cached_input_tokens"),
        "cache_write_input": ("cache_write_input", "cache_write_input_tokens"),
        "reasoning": ("reasoning", "reasoning_tokens"),
        "output": ("output", "output_tokens"),
        "model_calls": ("model_calls",),
    }
    for field, names in aliases.items():
        for name in names:
            if name in updates:
                merged[field] = updates[name]
                break
    if "total" in updates:
        merged["total"] = updates["total"]
    elif "total_tokens" in updates:
        merged["total_tokens"] = updates["total_tokens"]
    return _token_total(merged)


def _cli_tokens(args: Any) -> dict[str, int]:
    """Collect only the token flags actually present on the command line."""
    supplied = {}
    for field, attr in (("input", "input_tokens"),
                        ("cached_input", "cached_input_tokens"),
                        ("cache_write_input", "cache_write_input_tokens"),
                        ("reasoning", "reasoning_tokens"),
                        ("output", "output_tokens")):
        value = getattr(args, attr, None)
        if value is not None:
            supplied[field] = value
    return supplied


def complete_task(
    task_id: str,
    *,
    status: str = "completed",
    validation_passed: bool | None = None,
    tokens: dict[str, Any] | None = None,
    usage_after: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    history_limit = get_settings()["history_limit"]
    with _locked() as paths:
        active = _load_active(paths)
        if task_id not in active:
            raise ValueError(f"No active task {task_id!r}")
        record = active.pop(task_id)
        record.pop("transcript_path", None)
        record.pop("session_id", None)
        record.pop("turn_id", None)
        record.pop("rollout_offset", None)
        token_source = "codex-rollout" if tokens and tokens.get("model_calls") else "unavailable"
        # Wall clock from started_at, and UNKNOWN stays unknown. Clamping a
        # nonpositive elapsed to 0.0 turns a clock problem into a measurement --
        # and 0.0 seconds then feeds a plausible-looking efficiency score.
        started = _epoch(record.get("started_at"))
        completed = time.time()
        elapsed = (completed - started
                   if started is not None and completed > started else None)
        record.pop("started_monotonic", None)
        # Sparse merge: a completion reporting only --input-tokens must not
        # discard the cached_input/reasoning totals already measured for it.
        measured_tokens = (_merge_tokens(record.get("tokens"), tokens)
                           if tokens is not None
                           else _token_total(record.get("tokens")))
        record.update({
            "completed_at": _now(),
            "elapsed_seconds": round(elapsed, 3) if elapsed is not None else None,
            "status": status,
            "validation_passed": validation_passed,
            "tokens": measured_tokens,
            "token_source": token_source,
        })
        before = record.get("usage_before", [])
        after = usage_after or _active_windows()
        record["usage_delta"] = _usage_deltas(before, after)
        try:
            record["forecast_outcome"] = _forecast_outcome(record)
        except Exception:
            # Outcome telemetry is optional. A malformed legacy or active
            # forecast must never prevent Stop from preserving the task record.
            record["forecast_outcome"] = {
                "schema_version": FORECAST_SCHEMA_VERSION,
                "available": False,
                "status": "outcome-generation-failed",
            }
        record["efficiency_score"] = _efficiency_score(record)
        _atomic_json(paths["active"], active)
        with paths["history"].open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        _trim_history(paths["history"], history_limit)
    return record


def _usage_deltas(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(before, list) or not isinstance(after, list):
        return []
    indexed = {
        item.get("limit_id", item.get("limitId")): item
        for item in before if isinstance(item, dict)
    }
    result = []
    for item in after:
        if not isinstance(item, dict):
            continue
        limit_id = item.get("limit_id", item.get("limitId"))
        previous = indexed.get(limit_id, {})
        old = previous.get("used_percent", previous.get("usedPercent"))
        new = item.get("used_percent", item.get("usedPercent"))
        if all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value))
            for value in (old, new)
        ):
            previous_instance = _window_instance(previous)
            current_instance = _window_instance(item)
            same_window = (
                previous_instance is not None
                and current_instance is not None
                and previous_instance == current_instance
            )
            delta: dict[str, Any] = {
                "limit_id": limit_id,
                "window_instance": current_instance,
                "same_window": same_window,
            }
            if same_window:
                delta["used_percent_delta"] = round(float(new) - float(old), 3)
            else:
                delta["status"] = "window-changed-or-unidentified"
            result.append(delta)
    return result


def _efficiency_score(record: dict[str, Any]) -> dict[str, Any] | None:
    """Score a completed task, or return None when it cannot be scored.

    Two changes from the bare-float version, both the same lesson: the old code
    floored `time_ratio` and `token_ratio` at 0.1 and defaulted a missing token
    total to 1.0, so a task with NO measured elapsed time and NO measured tokens
    still produced a confident-looking number. Unmeasurable now returns None.
    The ratios and the basis ride along, so a caller can see what the score was
    computed from instead of trusting a scalar.
    """
    mode = record.get("mode", "efficiency")
    complexity = record.get("complexity", "moderate")
    if complexity not in COMPLEXITY_BASE:
        return None
    elapsed = record.get("elapsed_seconds")
    token_total = record.get("tokens", {}).get("total", 0)
    if not isinstance(elapsed, (int, float)) or elapsed <= 0 or not token_total:
        return None
    expected_minutes = COMPLEXITY_BASE[complexity][0] * MODE_INFO[mode]["time_factor"]
    time_ratio = float(elapsed) / 60 / expected_minutes
    expected_tokens = COMPLEXITY_BASE[complexity][1] * MODE_INFO[mode]["token_factor"]
    token_ratio = token_total / expected_tokens
    quality = 1.0 if record.get("validation_passed") is not False and record.get("status") == "completed" else 0.65
    score = 100.0 * quality / max(0.01, 0.45 * time_ratio + 0.55 * token_ratio)
    return {
        "score": round(min(150.0, score), 1),
        "basis": "measured elapsed time and measured rollout or caller token totals",
        "time_ratio": round(time_ratio, 4),
        "token_ratio": round(token_ratio, 4),
        "quality_factor": quality,
    }


def _trim_history(path: Path, limit: int) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > limit:
            path.write_text("\n".join(lines[-limit:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def list_history(limit: int = 20) -> list[dict[str, Any]]:
    return list(reversed(_recent_history(max(1, min(int(limit), 500)))))


def _finite_metric(value: Any, *, nonnegative: bool = False) -> float | None:
    if (
        not isinstance(value, (int, float)) or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        return None
    number = float(value)
    return None if nonnegative and number < 0 else number


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


def _audit_summary(entries: list[dict[str, Any]], *, range_dimension: bool) -> dict[str, Any]:
    absolute_errors: list[float] = []
    signed_errors: list[float] = []
    covered = 0
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("available") is not True:
            continue
        absolute = _finite_metric(entry.get("absolute_relative_error"), nonnegative=True)
        signed = _finite_metric(entry.get("relative_error"))
        if absolute is None or signed is None:
            continue
        absolute_errors.append(absolute)
        signed_errors.append(signed)
        if range_dimension and entry.get("within_range") is True:
            covered += 1
    audited = len(absolute_errors)
    result: dict[str, Any] = {
        "audited": audited,
        "unavailable": len(entries) - audited,
        "median_absolute_relative_error": (
            round(statistics.median(absolute_errors), 4) if absolute_errors else None
        ),
        "p80_absolute_relative_error": (
            round(_nearest_rank(absolute_errors, 0.8), 4) if absolute_errors else None
        ),
        "median_signed_relative_error": (
            round(statistics.median(signed_errors), 4) if signed_errors else None
        ),
    }
    if range_dimension:
        result.update({
            "covered": covered,
            "coverage_rate": round(covered / audited, 4) if audited else None,
        })
    return result


def forecast_diagnostics(limit: int = 100) -> dict[str, Any]:
    """Return aggregate, privacy-safe health for forecast auditing and calibration."""
    bounded_limit = max(1, min(int(limit), 500))
    records = list_history(bounded_limit)
    completed = [item for item in records if item.get("status") == "completed"]
    time_entries: list[dict[str, Any]] = []
    token_entries: list[dict[str, Any]] = []
    capacity_entries: list[dict[str, Any]] = []
    available_forecast_records = 0
    legacy_records = 0
    forecast_generation_failures = 0
    outcome_generation_failures = 0
    missing_outcomes = 0
    jointly_audited = 0
    paired_cohorts: dict[tuple[str, str, str], int] = {}
    by_mode = {
        mode: {"completed": 0, "time_audited": 0, "token_audited": 0, "jointly_audited": 0}
        for mode in MODES
    }

    for record in completed:
        mode = _history_mode(record)
        if mode in by_mode:
            by_mode[mode]["completed"] += 1
        forecast = record.get("forecast")
        if not isinstance(forecast, dict):
            legacy_records += 1
            continue
        if forecast.get("available") is not True:
            forecast_generation_failures += 1
            continue
        available_forecast_records += 1
        outcome = record.get("forecast_outcome")
        if not isinstance(outcome, dict):
            missing_outcomes += 1
            continue
        if outcome.get("status") == "outcome-generation-failed":
            outcome_generation_failures += 1
            continue
        time_entry = outcome.get("time")
        token_entry = outcome.get("tokens")
        time_entries.append(time_entry if isinstance(time_entry, dict) else {})
        token_entries.append(token_entry if isinstance(token_entry, dict) else {})
        capacity = outcome.get("capacity")
        if isinstance(capacity, list):
            capacity_entries.extend(item for item in capacity if isinstance(item, dict))

        time_ok = (
            isinstance(time_entry, dict) and time_entry.get("available") is True
            and _finite_metric(time_entry.get("absolute_relative_error"), nonnegative=True) is not None
            and _finite_metric(time_entry.get("relative_error")) is not None
        )
        token_ok = (
            isinstance(token_entry, dict) and token_entry.get("available") is True
            and _finite_metric(token_entry.get("absolute_relative_error"), nonnegative=True) is not None
            and _finite_metric(token_entry.get("relative_error")) is not None
        )
        if mode in by_mode:
            by_mode[mode]["time_audited"] += int(time_ok)
            by_mode[mode]["token_audited"] += int(token_ok)
            by_mode[mode]["jointly_audited"] += int(time_ok and token_ok)
        jointly_audited += int(time_ok and token_ok)
        if time_ok and token_ok:
            cohort = (
                mode or "unknown",
                str(_history_complexity(record)),
                str(record.get("model")),
            )
            paired_cohorts[cohort] = paired_cohorts.get(cohort, 0) + 1

    largest_cohort = max(paired_cohorts.values(), default=0)
    if outcome_generation_failures:
        status = "attention"
        next_action = "Inspect outcome-generation failures before trusting calibration growth."
    elif forecast_generation_failures:
        status = "attention"
        next_action = "Inspect forecast-generation failures; task execution stayed fail-open."
    elif missing_outcomes:
        status = "attention"
        next_action = "Inspect completed forecast records that are missing outcome audits."
    elif largest_cohort >= 10:
        status = "well-audited"
        next_action = "Continue collecting audited forecasts and monitor residual drift."
    elif largest_cohort >= 5:
        status = "confidence-ready"
        next_action = (
            f"Collect {10 - largest_cohort} more paired audits in one comparable cohort "
            "for the high-sample threshold."
        )
    elif largest_cohort >= MODEL_CALIBRATION_MIN_SAMPLES:
        status = "interval-ready"
        next_action = (
            f"Collect {5 - largest_cohort} more paired audits in one comparable cohort "
            "for the medium-sample threshold."
        )
    else:
        status = "collecting" if completed else "no-data"
        next_action = (
            f"Collect {3 - largest_cohort} more paired audits in one comparable cohort "
            "to calibrate residual intervals."
        )

    return {
        "schema_version": 1,
        "status": status,
        "records_considered": len(records),
        "completed_records": len(completed),
        "available_forecast_records": available_forecast_records,
        "legacy_records": legacy_records,
        "forecast_generation_failures": forecast_generation_failures,
        "outcome_generation_failures": outcome_generation_failures,
        "missing_outcomes": missing_outcomes,
        "dimensions": {
            "time": _audit_summary(time_entries, range_dimension=True),
            "tokens": _audit_summary(token_entries, range_dimension=True),
            "capacity": _audit_summary(capacity_entries, range_dimension=False),
        },
        "calibration": {
            "jointly_audited": jointly_audited,
            "largest_comparable_cohort": largest_cohort,
            "interval_sample_threshold": MODEL_CALIBRATION_MIN_SAMPLES,
            "medium_sample_threshold": 5,
            "high_sample_threshold": 10,
        },
        "by_mode": by_mode,
        "next_action": next_action,
        "privacy": "Aggregate numeric diagnostics only; no task, session, turn, prompt, or path data.",
    }


def ingest_usage(
    *,
    rate_limits: Any = None,
    tokens: dict[str, Any] | None = None,
    task_id: str | None = None,
    plan_type: str | None = None,
) -> dict[str, Any]:
    normalized = _normalize_rate_limits(rate_limits)
    with _locked() as paths:
        usage = _load_json(paths["usage"], {})
        if not isinstance(usage, dict):
            usage = {}
        if normalized:
            stamp = _now()
            # Merge by limit_id rather than replacing. A caller reporting only
            # `primary` is not asserting that `secondary` ceased to exist, and
            # dropping it would silently discard a window that may be the
            # constraining one. Only the windows actually present in THIS
            # update get a new captured_at.
            existing = usage.get("rate_limits")
            merged: dict[str, dict[str, Any]] = {}
            order: list[str] = []
            if isinstance(existing, list):
                for item in existing:
                    if isinstance(item, dict) and item.get("limit_id") is not None:
                        merged[item["limit_id"]] = dict(item)
                        order.append(item["limit_id"])
            for item in normalized:
                key = item.get("limit_id")
                if key is None:
                    continue
                fresh_item = dict(merged.get(key) or {})
                fresh_item.update({k: v for k, v in item.items() if v is not None})
                fresh_item["captured_at"] = stamp
                if key not in merged:
                    order.append(key)
                merged[key] = fresh_item
            usage["rate_limits"] = [merged[k] for k in order]
            # Rate limits get their OWN timestamp; see _window_snapshot.
            usage["rate_limits_updated_at"] = stamp
        if plan_type:
            usage["plan_type"] = plan_type
        if tokens:
            usage["latest_tokens"] = _token_total(tokens)
        usage["updated_at"] = _now()
        _atomic_json(paths["usage"], usage)
        if task_id and tokens:
            active = _load_active(paths)
            if task_id in active:
                active[task_id]["tokens"] = _token_total(tokens)
                _atomic_json(paths["active"], active)
    return usage


def _normalize_rate_limits(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, dict) and "rateLimitsByLimitId" in value:
        mapped = value.get("rateLimitsByLimitId") or {}
        source = [
            {**snapshot, "limitId": snapshot.get("limitId") or key}
            for key, snapshot in mapped.items() if isinstance(snapshot, dict)
        ]
        if not source and value.get("rateLimits"):
            return _normalize_rate_limits(value["rateLimits"])
    elif isinstance(value, dict) and "rateLimits" in value:
        return _normalize_rate_limits(value["rateLimits"])
    elif isinstance(value, dict) and ("primary" in value or "secondary" in value):
        source = []
        for suffix in ("primary", "secondary"):
            bucket = value.get(suffix)
            if bucket:
                # Measured against the real App Server schema (issue #4, Codex
                # 0.148.0-alpha.9): `limitId` sits on the CONTAINING snapshot,
                # not on primary/secondary, so `<limitId>:<role>` is already a
                # stable key -- primary/secondary are semantic roles, not
                # positions, and cannot be reordered. This branch is therefore
                # defensive only: it honours a bucket-level id if some future
                # shape supplies one, and otherwise behaves exactly as before.
                identity = bucket.get("limitId", bucket.get("limit_id"))
                if not identity:
                    identity = f"{value.get('limitId', 'codex')}:{suffix}"
                source.append({
                    **bucket,
                    "limitId": identity,
                    "limitName": value.get("limitName"),
                    "planType": value.get("planType"),
                    "credits": value.get("credits"),
                    "individualLimit": value.get("individualLimit"),
                    "rateLimitReachedType": value.get("rateLimitReachedType"),
                    "spendControlReached": value.get("spendControlReached"),
                    "windowRole": suffix,
                })
    elif isinstance(value, dict):
        source = [value]
    elif isinstance(value, list):
        source = value
    else:
        raise ValueError("rate_limits must be an App Server rate-limit object or a list")
    result = []
    for index, item in enumerate(source):
        if not isinstance(item, dict):
            continue
        if "primary" in item or "secondary" in item:
            result.extend(_normalize_rate_limits(item))
            continue
        result.append({
            "limit_id": item.get("limit_id", item.get("limitId", f"window-{index + 1}")),
            "limit_name": item.get("limit_name", item.get("limitName")),
            "used_percent": item.get("used_percent", item.get("usedPercent")),
            "window_duration_mins": item.get("window_duration_mins", item.get("windowDurationMins")),
            "resets_at": item.get("resets_at", item.get("resetsAt")),
            "plan_type": item.get("plan_type", item.get("planType")),
            "credits": item.get("credits"),
            "individual_limit": item.get("individual_limit", item.get("individualLimit")),
            "rate_limit_reached_type": item.get(
                "rate_limit_reached_type", item.get("rateLimitReachedType")
            ),
            "spend_control_reached": item.get("spend_control_reached", item.get("spendControlReached")),
            "window_role": item.get("window_role", item.get("windowRole")),
        })
    return result


def _find_task(active: dict[str, Any], session_id: str | None, turn_id: str | None) -> str | None:
    """Locate a task, refusing to guess without a session.

    Without the session guard a call carrying no session_id matched any record
    whose own session_id was also absent -- attributing a turn to whatever task
    happened to be first in the dict. Iteration order is not a tiebreak, so when
    several tasks do match, take the NEWEST by start time.
    """
    if not session_id:
        return None
    session_ref = opaque_ref(session_id, "session")
    turn_ref = opaque_ref(turn_id, "turn")
    direct = _task_key_from_refs(session_ref, turn_ref)
    if direct and direct in active:
        return direct
    matches = [
        (key, record) for key, record in active.items()
        if _record_ref(record, "session") == session_ref
        and (turn_id is None or _record_ref(record, "turn") == turn_ref)
    ]
    if not matches:
        return None
    return max(matches, key=lambda pair: _epoch(pair[1].get("started_at")) or 0)[0]


def _find_task_by_refs(
    active: dict[str, Any], session_ref: str | None, turn_ref: str | None
) -> str | None:
    if not session_ref:
        return None
    direct = _task_key_from_refs(session_ref, turn_ref)
    if direct and direct in active:
        return direct
    matches = [
        (key, record) for key, record in active.items()
        if _record_ref(record, "session") == session_ref
        and (turn_ref is None or _record_ref(record, "turn") == turn_ref)
    ]
    if not matches:
        return None
    return max(matches, key=lambda pair: _epoch(pair[1].get("started_at")) or 0)[0]


def _policy_decisions(
    capacity: dict[str, Any],
    mode: str,
    *,
    session_id: str | None = None,
    consume: bool = False,
) -> dict[str, Any]:
    """Choose actionable policy signals and suppress repeats within a session."""
    mode = normalize_mode(mode)
    windows = list(capacity.get("windows") or []) if capacity.get("available") else []
    promoted: list[dict[str, Any]] = []
    for window in windows:
        blocked = window.get("blocked_hours")
        horizon = window.get("hours_to_cap")
        if not isinstance(blocked, (int, float)) or blocked < PROMOTE_MIN_BLOCKED_HOURS:
            continue
        if blocked >= PROMOTE_ALWAYS_BLOCKED_HOURS or (
            isinstance(horizon, (int, float)) and horizon <= PROMOTE_MAX_HORIZON_HOURS
        ):
            promoted.append(window)

    ask_ratio = {"throughput": None, "balanced": 1.0, "stretch": 0.80}[mode]
    pace = [
        window for window in windows
        if ask_ratio is not None
        and isinstance(window.get("pace_ratio"), (int, float))
        and float(window["pace_ratio"]) > ask_ratio
    ]

    emitted_lockouts = _gate_policy_signals("lockout", promoted, session_id, consume)
    emitted_pace = _gate_policy_signals("pace", pace, session_id, consume)
    worst_pace = max(
        (float(window["pace_ratio"]) for window in windows if isinstance(window.get("pace_ratio"), (int, float))),
        default=None,
    )
    balanced_ample = any(
        isinstance(window.get("pace_ratio"), (int, float))
        and float(window["pace_ratio"]) <= 0.85
        and float(window.get("remaining_percent") or 0) >= 50
        for window in windows
    )
    go_wide = bool(
        windows and not promoted and (
            mode == "throughput"
            or (mode == "balanced" and balanced_ample and worst_pace is not None and worst_pace <= 0.85)
        )
    )
    estimate = capacity.get("estimated_remaining") or {}
    fit_check = capacity.get("calibration_status") == "observed" and int(estimate.get("low") or 0) <= 1
    return {
        "lockout_limit_ids": emitted_lockouts,
        "pace_limit_ids": emitted_pace,
        "go_wide": go_wide,
        "fit_check": fit_check,
    }


def _gate_policy_signals(
    kind: str,
    windows: list[dict[str, Any]],
    session_id: str | None,
    consume: bool,
) -> list[str]:
    """Emit a signal once per window, then only when its severity is 25% worse."""
    if not windows:
        return []
    if not session_id or not consume:
        return [str(window.get("limit_id")) for window in windows]
    with _locked() as paths:
        state = _load_json(paths["policy_ack"], {})
        state = state if isinstance(state, dict) else {}
        raw_sessions = state.get("sessions")
        raw_sessions = raw_sessions if isinstance(raw_sessions, dict) else {}
        sessions = {
            (key if str(key).startswith("ae-session-") else opaque_ref(key, "session")): value
            for key, value in raw_sessions.items()
            if isinstance(value, dict)
        }
        session_key = opaque_ref(session_id, "session")
        session = sessions.setdefault(session_key, {})
        emitted: list[str] = []
        for window in windows:
            limit_id = str(window.get("limit_id"))
            reset = str(window.get("resets_at") or "unknown")
            key = f"{kind}:{limit_id}:{reset}"
            metric_field = "blocked_hours" if kind == "lockout" else "pace_ratio"
            metric = float(window.get(metric_field) or 0)
            previous = session.get(key)
            previous_metric = float(previous.get("metric") or 0) if isinstance(previous, dict) else None
            reask_ratio = CHOICE_REASK_WORSE if kind == "lockout" else 1.0 + PACE_REASK_STEP
            if previous_metric is None or metric >= previous_metric * reask_ratio:
                emitted.append(limit_id)
                session[key] = {"metric": metric, "acknowledged_at": _now()}
        sessions[session_key] = session
        state["sessions"] = sessions
        # State is tiny in normal use, but bound abandoned session records.
        if len(sessions) > 50:
            for key in list(sessions)[:-50]:
                sessions.pop(key, None)
        if emitted or sessions != raw_sessions:
            _atomic_json(paths["policy_ack"], state)
    return emitted


def handle_hook(payload: dict[str, Any]) -> dict[str, Any] | None:
    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id")
    turn_id = payload.get("turn_id")
    settings = get_settings()
    telemetry_enabled = settings["telemetry_enabled"]

    if event == "SessionStart":
        refresh_usage_snapshot()
        info = MODE_INFO[settings["default_mode"]]
        context = (
            f"Agent Efficiency v{VERSION} is active. Default mode: {info['label']}. "
            f"Estimate scope: {settings['estimate_scope']}. Use the Agent Efficiency control center "
            "when the user asks to compare or choose modes."
        )
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}

    if event == "UserPromptSubmit":
        refresh_usage_snapshot()
        # A per-session mode override, consumed once. The handoff token lets the
        # control center hand a chosen mode to the next turn without the user
        # retyping it, and without one session's choice leaking into another's.
        handoff_token = _mode_handoff_token(payload.get("prompt"))
        selected_mode = normalize_mode(
            payload.get("agent_efficiency_mode")
            or _consume_mode_override(session_id, settings["default_mode"], handoff_token)
        )
        if telemetry_enabled:
            transcript = payload.get("transcript_path")
            record = start_task(
                session_id=session_id,
                turn_id=turn_id,
                mode=selected_mode,
                complexity=UNCLASSIFIED_COMPLEXITY,
                model=payload.get("model"),
                source="hook",
                transcript_path=transcript,
                rollout_offset=rollout_size(transcript),
            )
            with _locked() as paths:
                active = _load_active(paths)
                if record["task_id"] in active:
                    active[record["task_id"]]["usage_before"] = _active_windows()
                    _atomic_json(paths["active"], active)
        else:
            record = {"mode": selected_mode}
        info = MODE_INFO[record["mode"]]
        capacity = estimate_capacity(record["mode"])
        context = render_usage_budget(
            capacity,
            mode_label=info["label"],
            mode_behavior=info["behavior"],
            decisions=_policy_decisions(
                capacity,
                record["mode"],
                session_id=session_id,
                consume=telemetry_enabled,
            ),
        )
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}

    if not telemetry_enabled:
        return {} if event in {"Stop", "SubagentStop"} else None

    with _locked() as paths:
        active = _load_active(paths)
        task_id = _find_task(active, session_id, turn_id)
        if task_id:
            record = active[task_id]
            if event == "PreToolUse":
                record["tool_calls"] = int(record.get("tool_calls", 0)) + 1
            elif event == "PostToolUse" and _tool_failed(payload.get("tool_response")):
                record["tool_failures"] = int(record.get("tool_failures", 0)) + 1
            elif event == "SubagentStart":
                record["subagents"] = int(record.get("subagents", 0)) + 1
            elif event in {"PreCompact", "PostCompact"}:
                record["compactions"] = int(record.get("compactions", 0)) + (1 if event == "PostCompact" else 0)
            _atomic_json(paths["active"], active)

    if event == "Stop":
        if not telemetry_enabled:
            return {}
        task_id = None
        task_record = None
        with _locked() as paths:
            active = _load_active(paths)
            task_id = _find_task(active, session_id, turn_id)
            task_record = active.get(task_id) if task_id else None
        if task_id and isinstance(task_record, dict):
            tokens = collect_turn_tokens(
                payload.get("transcript_path"),
                turn_id=turn_id,
                turn_ref=task_record.get("turn_ref") if turn_id is None else None,
                start_offset=int(task_record.get("rollout_offset", 0) or 0),
            )
            refresh_usage_snapshot(force=True)
            usage_after = _active_windows()
            with contextlib.suppress(ValueError):
                complete_task(
                    task_id,
                    tokens=tokens,
                    usage_after=usage_after,
                )
        return {}
    if event == "SessionEnd":
        with _locked() as paths:
            active = _load_active(paths)
            tasks = [
                (key, dict(record))
                for key, record in active.items()
                if _record_ref(record, "session") == opaque_ref(session_id, "session")
            ]
        for task_id, task_record in tasks:
            tokens = collect_turn_tokens(
                payload.get("transcript_path"),
                turn_ref=_record_ref(task_record, "turn"),
                start_offset=int(task_record.get("rollout_offset", 0) or 0),
            )
            with contextlib.suppress(ValueError):
                complete_task(task_id, status="session-ended", tokens=tokens)
        return None
    if event == "SubagentStop":
        return {}
    return None


def _tool_failed(response: Any) -> bool:
    if isinstance(response, dict):
        if response.get("isError") is True or response.get("success") is False:
            return True
        status = str(response.get("status", "")).lower()
        return status in {"failed", "error", "declined"}
    return False


def _duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def render_control_center(center: dict[str, Any]) -> str:
    lines = [f"Agent Efficiency v{center['version']}", f"Task: {center['task_summary']}", ""]
    poll = center.get("telemetry_status", {}).get("rate_limit_poll", {})
    poll_status = poll.get("status", "not-started")
    poll_line = f"Rate-limit telemetry: {poll_status}"
    if poll_status == "degraded" and poll.get("last_error"):
        poll_line += f" ({poll['last_error'].get('message', 'Polling failed.')})"
    lines.extend([poll_line, ""])
    for item in center["modes"]:
        eta = item["eta_minutes"]
        tokens = item["estimated_tokens"]
        marker = " (selected)" if item["mode"] == center["selected_mode"] else ""
        lines.extend([
            f"{item['label']}{marker}",
            f"  Performs: {item['behavior']}",
            f"  Tradeoff: {item['tradeoff']}",
            f"  ETA: {eta['low']:g}-{eta['high']:g} minutes",
            f"  Estimated tokens: {tokens['low']:,}-{tokens['high']:,}",
            f"  Estimate confidence: {item.get('confidence', 'low')} "
            f"({item.get('confidence_basis', 'basis unavailable')})",
        ])
        capacity = item.get("capacity")
        if capacity and capacity.get("available"):
            remaining = capacity["estimated_remaining"]
            lines.append(
                f"  Estimated tasks remaining: {remaining['low']}-{remaining['high']} "
                f"({capacity['confidence']} confidence; {capacity['basis']})"
            )
            window = next(
                (entry for entry in capacity["windows"] if entry["limit_id"] == capacity["constraining_window"]),
                capacity["windows"][0],
            )
            lines.append(
                f"  Measured usage: {window['used_percent']:g}% used; "
                f"{window['remaining_percent']:g}% remaining (derived)"
            )
            verdict = window["forecast_verdict"]
            if verdict == "lockout":
                lines.append(
                    f"  Forecast: caps in ~{_duration(window['hours_to_cap'] * 3600)}, "
                    f"then BLOCKED ~{_duration(window['blocked_hours'] * 3600)} until it "
                    f"resets — worth reshaping the work"
                )
            elif verdict == "brief-lockout":
                lines.append(
                    f"  Forecast: caps in ~{_duration(window['hours_to_cap'] * 3600)}, "
                    f"~{_duration(window['blocked_hours'] * 3600)} before it resets — a short "
                    f"wait, then a full allowance. NOT a reason to cut scope"
                )
            elif verdict == "no-lockout":
                lines.append(
                    f"  Forecast: resets in {_duration(window['seconds_to_reset'])} with "
                    f"allowance to spare — not a constraint"
                )
            else:
                lines.append("  Forecast: unavailable until completed-task burn history is recorded")
        elif capacity:
            lines.append(f"  Estimated tasks remaining: UNCALIBRATED ({capacity['reason']})")
        lines.append("")
    if center.get("disclaimer"):
        lines.append(center["disclaimer"])
    return "\n".join(lines).rstrip()


def _parse_bool(value: str) -> bool:
    normalized = value.lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-efficiency", description="Agent Efficiency v0.4")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    control = sub.add_parser("control-center", help="compare or estimate execution modes")
    control.add_argument("task", nargs="?", default="Current task")
    control.add_argument("--mode", type=normalize_mode, choices=MODES)
    control.add_argument("--scope", choices=("selected", "all"))
    control.add_argument("--capacity-scope", choices=("selected", "all", "off", "inherit"))
    control.add_argument("--complexity", choices=tuple(COMPLEXITY_BASE), default="moderate")
    control.add_argument("--context", choices=tuple(CONTEXT_FACTOR), default="medium")
    control.add_argument("--model")
    control.add_argument("--reasoning", choices=tuple(REASONING_FACTOR), default="medium")
    control.add_argument("--location", choices=tuple(LOCATION_FACTOR), default="local")
    control.add_argument("--fast", action="store_true")
    control.add_argument("--tools", type=int, default=3)
    control.add_argument("--subagents", type=int, default=0)
    control.add_argument("--media", type=int, default=0)
    control.add_argument("--cached-ratio", type=float, default=0.25)

    sub.add_parser("settings", help="show persistent settings")
    setting = sub.add_parser("set", help="change a persistent setting")
    setting.add_argument("key", choices=sorted(set(DEFAULT_SETTINGS) - {"schema_version"}))
    setting.add_argument("value")

    start = sub.add_parser("start", help="start a task timer")
    start.add_argument("--id")
    start.add_argument("--mode", type=normalize_mode, choices=MODES)
    start.add_argument("--complexity", choices=tuple(COMPLEXITY_BASE), default="moderate")
    start.add_argument("--model")

    complete = sub.add_parser("complete", help="complete a task timer")
    complete.add_argument("id")
    complete.add_argument("--status", default="completed")
    complete.add_argument("--validation-passed", type=_parse_bool)
    # default=None, not 0: with a zero default "not supplied" is indistinguishable
    # from "explicitly zero", and a sparse merge then reads every omitted flag as
    # an instruction to zero a field that was already measured.
    complete.add_argument("--input-tokens", type=int, default=None)
    complete.add_argument("--cached-input-tokens", type=int, default=None)
    complete.add_argument("--cache-write-input-tokens", type=int, default=None)
    complete.add_argument("--reasoning-tokens", type=int, default=None)
    complete.add_argument("--output-tokens", type=int, default=None)

    history = sub.add_parser("history", help="show recent task records")
    history.add_argument("--limit", type=int, default=20)
    diagnostics = sub.add_parser(
        "forecast-diagnostics",
        help="show aggregate forecast accuracy and calibration readiness",
    )
    diagnostics.add_argument("--limit", type=int, default=100)

    ingest = sub.add_parser("ingest-usage", help="save App Server or manual usage telemetry")
    ingest.add_argument("--used-percent", type=float)
    ingest.add_argument("--window-minutes", type=int)
    ingest.add_argument("--resets-at", type=int)
    ingest.add_argument("--limit-id", default="codex")
    ingest.add_argument("--plan-type")
    ingest.add_argument("--file", help="JSON file containing account/rateLimits/read result")
    sub.add_parser("refresh-usage", help="refresh the authoritative Codex allowance snapshot")
    scan = sub.add_parser("scan-rollouts", help="incrementally ingest local rollout token events")
    scan.add_argument("--sessions-root")
    sub.add_parser("verify", help="verify the local plugin structure and writable state")
    return parser


def _coerce_setting(key: str, value: str) -> Any:
    default = DEFAULT_SETTINGS[key]
    if isinstance(default, bool):
        return _parse_bool(value)
    if isinstance(default, int):
        return int(value)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "control-center":
            value = show_control_center(
                task=args.task,
                selected_mode=args.mode,
                estimate_scope=args.scope,
                capacity_scope=args.capacity_scope,
                complexity=args.complexity,
                context=args.context,
                model=args.model,
                reasoning_effort=args.reasoning,
                execution_location=args.location,
                fast_mode=args.fast,
                expected_tools=args.tools,
                expected_subagents=args.subagents,
                expected_media=args.media,
                cached_ratio=args.cached_ratio,
            )
        elif args.command == "settings":
            value = get_settings()
        elif args.command == "set":
            value = update_settings(**{args.key: _coerce_setting(args.key, args.value)})
        elif args.command == "start":
            value = start_task(task_id=args.id, mode=args.mode, complexity=args.complexity, model=args.model)
        elif args.command == "complete":
            value = complete_task(
                args.id,
                status=args.status,
                validation_passed=args.validation_passed,
                # Only pass the fields the caller actually supplied. Sending
                # every flag as None makes a sparse merge look like an explicit
                # "set these to zero", which wipes tokens already measured for
                # the task by ingest-usage. None here means "not stated".
                tokens=_cli_tokens(args) or None,
            )
        elif args.command == "history":
            value = list_history(args.limit)
        elif args.command == "forecast-diagnostics":
            value = forecast_diagnostics(args.limit)
        elif args.command == "ingest-usage":
            if args.file:
                source = json.loads(Path(args.file).read_text(encoding="utf-8"))
            else:
                source = {
                    "limit_id": args.limit_id,
                    "used_percent": args.used_percent,
                    "window_duration_mins": args.window_minutes,
                    "resets_at": args.resets_at,
                }
            value = ingest_usage(rate_limits=source, plan_type=args.plan_type)
        elif args.command == "refresh-usage":
            value = refresh_usage_snapshot(force=True, raise_on_error=True)
        elif args.command == "scan-rollouts":
            value = scan_rollouts(args.sessions_root)
        elif args.command == "verify":
            value = verify_install()
            if value["status"] != "pass":
                print(json.dumps(value, indent=2, sort_keys=True))
                return 1
        else:
            parser.error("unknown command")
            return 2
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json or args.command != "control-center":
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print(render_control_center(value))
    return 0


# --- Ported from robbie/codex-backlog (PR #11 block B) ----------------------
# Capabilities this tree never had, kept when the App Server work superseded the
# polling architecture around them. The App Server stays authoritative for
# current allowance state; these are the fallback and reliability tiers it
# always assumed existed:
#   * rollout JSONL scanning -- the documented fallback for completed token
#     usage. Without it there is no fallback, only a gap.
# NOT ported: his rate-limit polling tier (`refresh_rate_limits`,
# `poll_rate_limits`, and their status/sanitiser helpers). It shells out to
# `codex app-server` by subprocess to do the job `app_server_client.py` already
# does in-process. Keeping both would give this plugin two competing quota
# paths that can disagree, which is worse than either alone.
#   * per-session task-mode override with a one-time handoff token.
#   * `_atomic_text`, so concurrent writers cannot interleave a partial file.
# See docs/TRIAGE-PR11-2026-08-18.md block B.
MODE_HANDOFF_PATTERN = re.compile(r"\[agent-efficiency-mode:([0-9a-f]{32})\]")
_PROCESS_LOCK = threading.RLock()
ROLLOUT_CACHE_SCHEMA_VERSION = 3


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _rollout_tokens(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    aliases = {
        "input": "input_tokens",
        "cached_input": "cached_input_tokens",
        "cache_write_input": "cache_write_input_tokens",
        "output": "output_tokens",
        "reasoning": "reasoning_output_tokens",
    }
    if not any(field in value for field in aliases.values()):
        return None
    measured = {target: value.get(source, 0) for target, source in aliases.items()}
    if value.get("total_tokens") is not None:
        measured["total"] = value["total_tokens"]
    return _token_total(measured, authoritative_total=True)


def scan_rollouts(sessions_root: str | Path | None = None) -> dict[str, Any]:
    """Incrementally measure complete token_count lines from Codex rollout JSONL."""
    root = Path(sessions_root).expanduser() if sessions_root else Path.home() / ".codex" / "sessions"
    if not get_settings()["telemetry_enabled"]:
        return {
            "sessions_root": str(root), "files_seen": 0, "lines_processed": 0,
            "token_events_processed": 0, "turns_updated": 0, "status": "telemetry-disabled",
        }
    with _locked() as paths:
        cache = _load_json(paths["rollout_cache"], {})
        if not isinstance(cache, dict) or cache.get("schema_version") != ROLLOUT_CACHE_SCHEMA_VERSION:
            cache = {"schema_version": ROLLOUT_CACHE_SCHEMA_VERSION, "files": {}, "turns": {}}
        files = cache.setdefault("files", {})
        turns = cache.setdefault("turns", {})
        processed_lines = 0
        token_events = 0
        touched_turns: set[str] = set()
        candidates = sorted(root.glob("*/*/*/rollout-*.jsonl")) if root.exists() else []
        for path in candidates:
            key = opaque_ref(str(path.resolve()), "rollout")
            state = files.setdefault(key, {
                "offset": 0, "model": None, "turn_ref": None,
                "session_ref": None, "subagent": False,
            })
            size = path.stat().st_size
            offset = int(state.get("offset", 0))
            if size < offset:
                offset = 0
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
            newline = chunk.rfind(b"\n")
            if newline < 0:
                continue
            complete = chunk[: newline + 1]
            state["offset"] = offset + len(complete)
            for raw in complete.splitlines():
                try:
                    item = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                processed_lines += 1
                kind = item.get("type")
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                if kind == "session_meta":
                    observed = payload.get("id")
                    state["session_ref"] = (
                        opaque_ref(observed, "session") if observed is not None
                        else state.get("session_ref")
                    )
                    source = payload.get("source")
                    state["subagent"] = isinstance(source, dict) and bool(source.get("subagent"))
                elif kind == "turn_context":
                    state["turn_ref"] = opaque_ref(payload.get("turn_id"), "turn")
                    state["model"] = payload.get("model")
                event = payload if payload.get("type") == "token_count" else None
                info = event.get("info") if isinstance(event, dict) else None
                measured = _rollout_tokens(info.get("last_token_usage") if isinstance(info, dict) else None)
                if not measured or not state.get("session_ref") or not state.get("turn_ref"):
                    continue
                token_events += 1
                turn_key = _task_key_from_refs(state["session_ref"], state["turn_ref"])
                aggregate = turns.setdefault(turn_key, {
                    "session_ref": state["session_ref"], "turn_ref": state["turn_ref"],
                    "model": state.get("model"), "subagent": bool(state.get("subagent")), "tokens": {},
                })
                aggregate["model"] = state.get("model") or aggregate.get("model")
                aggregate["subagent"] = bool(state.get("subagent"))
                current = _token_total(aggregate.get("tokens"), authoritative_total=True)
                for field in TOKEN_COMPONENT_FIELDS:
                    current[field] += measured[field]
                current["total"] += measured["total"]
                aggregate["tokens"] = current
                aggregate["updated_at"] = _now()
                touched_turns.add(turn_key)
        active = _load_active(paths)
        active_changed = False
        if isinstance(active, dict):
            for aggregate in turns.values():
                task_id = _find_task_by_refs(
                    active, aggregate.get("session_ref"), aggregate.get("turn_ref")
                )
                if task_id:
                    active[task_id]["tokens"] = aggregate["tokens"]
                    active[task_id]["model"] = aggregate.get("model") or active[task_id].get("model")
                    active[task_id]["is_subagent"] = bool(aggregate.get("subagent"))
                    active_changed = True
        if active_changed:
            _atomic_json(paths["active"], active)
        _reconcile_history_rollouts(
            paths["history"],
            {turn_key: turns[turn_key] for turn_key in touched_turns if turn_key in turns},
        )
        cache["updated_at"] = _now()
        _atomic_json(paths["rollout_cache"], cache)
    return {
        "sessions_root": str(root), "files_seen": len(candidates), "lines_processed": processed_lines,
        "token_events_processed": token_events, "turns_updated": len(touched_turns),
    }


def _reconcile_history_rollouts(path: Path, aggregates: dict[str, dict[str, Any]]) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    changed = False
    output: list[str] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            output.append(line)
            continue
        if not isinstance(record, dict):
            output.append(line)
            continue
        session_ref = _record_ref(record, "session")
        turn_ref = _record_ref(record, "turn")
        if session_ref:
            record["session_ref"] = session_ref
        if turn_ref:
            record["turn_ref"] = turn_ref
        for field in ("session_id", "turn_id", "transcript_path"):
            if field in record:
                record.pop(field, None)
                changed = True
        turn_key = _task_key_from_refs(session_ref, turn_ref)
        aggregate = aggregates.get(turn_key) if turn_key else None
        if aggregate:
            tokens = aggregate.get("tokens")
            if isinstance(tokens, dict) and record.get("tokens") != tokens:
                record["tokens"] = tokens
                record["model"] = aggregate.get("model") or record.get("model")
                record["is_subagent"] = bool(aggregate.get("subagent"))
                record["tokens_updated_at"] = aggregate.get("updated_at") or _now()
                record["efficiency_score"] = _efficiency_score(record)
                changed = True
        output.append(json.dumps(record, separators=(",", ":")))
    if changed:
        _atomic_text(path, "\n".join(output) + ("\n" if output else ""))
    return changed


def _mode_handoff_token(prompt: Any) -> str | None:
    if not isinstance(prompt, str):
        return None
    match = MODE_HANDOFF_PATTERN.search(prompt)
    return match.group(1) if match else None


def _consume_mode_override(
    session_id: str | None,
    fallback: str,
    handoff_token: str | None = None,
) -> str:
    with _locked() as paths:
        events = _event_state(paths)
        pending_modes = events.get("pending_modes")
        if not isinstance(pending_modes, dict):
            pending_modes = {}
        legacy = events.pop("pending_mode", None)
        if isinstance(legacy, dict) and legacy.get("session_id"):
            session_ref = opaque_ref(legacy.pop("session_id"), "session")
            pending_modes.setdefault(f"session:{session_ref}", legacy)

        sanitized_pending = {}
        for pending_key, pending_value in pending_modes.items():
            if not isinstance(pending_value, dict):
                continue
            value = dict(pending_value)
            raw_session = value.pop("session_id", None)
            key = str(pending_key)
            if key.startswith("session:") and not key.startswith("session:ae-session-"):
                key = f"session:{opaque_ref(key.removeprefix('session:'), 'session')}"
            elif raw_session and not key.startswith("handoff:"):
                key = f"session:{opaque_ref(raw_session, 'session')}"
            sanitized_pending[key] = value
        pending_modes = sanitized_pending

        now = time.time()
        pending_modes = {
            key: value for key, value in pending_modes.items()
            if isinstance(value, dict)
            and (selected := _epoch(value.get("selected_at"))) is not None
            and now - selected <= 300
        }
        handoff_key = f"handoff:{handoff_token}" if handoff_token else None
        session_key = f"session:{opaque_ref(session_id, 'session')}" if session_id else None
        key = handoff_key if handoff_key and handoff_key in pending_modes else session_key
        pending = pending_modes.pop(key, None)
        events["pending_modes"] = pending_modes
        _atomic_json(paths["events"], events)
        if not isinstance(pending, dict):
            return fallback
    return normalize_mode(pending.get("mode"))


def select_task_mode(
    mode: str,
    *,
    task_id: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> dict[str, Any]:
    """Apply a non-persistent mode override to an active or immediately following task."""
    chosen = normalize_mode(mode)
    if turn_id and not session_id:
        raise ValueError("turn_id requires session_id")
    with _locked() as paths:
        active = _load_active(paths)
        if task_id is not None and task_id not in active:
            raise ValueError(f"No active task {task_id!r}")
        target = task_id if task_id is not None else _find_task(active, session_id, turn_id)
        if target:
            active[target]["mode"] = chosen
            active[target]["mode_selected_at"] = _now()
            _atomic_json(paths["active"], active)
        events = _event_state(paths)
        pending_modes = events.get("pending_modes")
        if not isinstance(pending_modes, dict):
            pending_modes = {}
        handoff_token = None
        if target is None:
            if session_id:
                pending_key = f"session:{opaque_ref(session_id, 'session')}"
            else:
                handoff_token = uuid.uuid4().hex
                pending_key = f"handoff:{handoff_token}"
            pending_modes[pending_key] = {
                "mode": chosen, "selected_at": _now(),
            }
        events["pending_modes"] = pending_modes
        events.pop("pending_mode", None)
        _atomic_json(paths["events"], events)
    result = {
        "mode": chosen,
        "task_id": target,
        "applies_to": "active task" if target else ("session" if session_id else "handoff token"),
    }
    if handoff_token:
        result["handoff_token"] = handoff_token
    return result


def verify_install(plugin_root: str | Path | None = None) -> dict[str, Any]:
    """Run dependency-free structural and local-state checks without network access."""
    root = Path(plugin_root).resolve() if plugin_root else Path(__file__).resolve().parents[1]
    required = {
        "manifest": root / ".codex-plugin" / "plugin.json",
        "hooks": root / "hooks" / "hooks.json",
        "hook_adapter": root / "hooks" / "agent_efficiency_hook.py",
        "skill": root / "skills" / "manage-agent-efficiency" / "SKILL.md",
        "control_center": root / "assets" / "control_center.html",
        "mcp_config": root / ".mcp.json",
    }
    checks: dict[str, Any] = {}
    errors: list[str] = []
    for name, path in required.items():
        present = path.is_file()
        checks[name] = {"ok": present, "path": str(path)}
        if not present:
            errors.append(f"missing {name}: {path}")
    for name in ("manifest", "hooks", "mcp_config"):
        path = required[name]
        if not path.is_file():
            continue
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            checks[name]["valid_json"] = isinstance(parsed, dict)
        except (OSError, json.JSONDecodeError) as exc:
            checks[name]["valid_json"] = False
            errors.append(f"invalid {name} JSON: {exc}")
    if checks["manifest"].get("valid_json"):
        manifest = json.loads(required["manifest"].read_text(encoding="utf-8"))
        version_ok = str(manifest.get("version", "")).split("+", 1)[0] == VERSION
        checks["version"] = {"ok": version_ok, "core": VERSION, "manifest": manifest.get("version")}
        if not version_ok:
            errors.append("core and manifest base versions differ")
    try:
        with _locked() as paths:
            probe = paths["root"] / ".verify-write.json"
            _atomic_json(probe, {"version": VERSION})
            probe.unlink()
        checks["local_state"] = {"ok": True, "path": str(_data_dir())}
    except OSError as exc:
        checks["local_state"] = {"ok": False, "path": str(_data_dir())}
        errors.append(f"local state is not writable: {exc}")
    return {"status": "pass" if not errors else "fail", "version": VERSION, "checks": checks, "errors": errors}


if __name__ == "__main__":
    raise SystemExit(main())
