"""Deterministic policy scenarios for Agent Efficiency's behavioral contract."""

from __future__ import annotations

import time


def scenarios() -> list[dict]:
    reset = time.time() + 12 * 3600
    base = {"available": True, "calibration_status": "heuristic", "estimated_remaining": {"low": 4, "high": 8}}

    def case(name: str, mode: str, windows: list[dict], contains: list[str], excludes: list[str]) -> dict:
        return {"name": name, "mode": mode, "capacity": base | {"windows": windows}, "contains": contains, "excludes": excludes}

    return [
        {"name": "uncalibrated stays honest", "mode": "balanced",
         "capacity": {"available": False}, "contains": ["UNCALIBRATED"], "excludes": ["GO WIDE", "WOULD RUN OUT"]},
        case("throughput spends headroom", "throughput", [{"limit_id": "week", "remaining_percent": 40}], ["GO WIDE"], []),
        case("balanced goes wide under pace", "balanced", [{"limit_id": "week", "remaining_percent": 70, "pace_ratio": 0.80}], ["GO WIDE"], []),
        case("balanced stays neutral without pace", "balanced", [{"limit_id": "week", "remaining_percent": 70}], [], ["GO WIDE"]),
        case("stretch never goes wide", "stretch", [{"limit_id": "week", "remaining_percent": 90, "pace_ratio": 0.40}], [], ["GO WIDE"]),
        case("throughput suppresses pace asks", "throughput", [{"limit_id": "week", "remaining_percent": 20, "pace_ratio": 1.50}], [], ["WEEK PACE"]),
        case("balanced warns above pace", "balanced", [{"limit_id": "week", "remaining_percent": 20, "pace_ratio": 1.10}], ["WEEK PACE"], []),
        case("stretch warns earlier", "stretch", [{"limit_id": "week", "remaining_percent": 40, "pace_ratio": 0.90}], ["WEEK PACE"], []),
        case("brief lockout is informational", "balanced", [{"limit_id": "week", "remaining_percent": 20, "blocked_hours": 0.5, "hours_to_cap": 1, "forecast_verdict": "brief-lockout"}], ["not a reason to cut scope"], ["WOULD RUN OUT"]),
        case("meaningful near lockout checkpoints", "balanced", [{"limit_id": "week", "remaining_percent": 20, "blocked_hours": 3, "hours_to_cap": 12, "resets_at": reset}], ["WOULD RUN OUT", "checkpoint, not a refusal"], []),
        case("far three-hour lockout stays quiet", "balanced", [{"limit_id": "week", "remaining_percent": 20, "blocked_hours": 3, "hours_to_cap": 25, "resets_at": reset}], [], ["WOULD RUN OUT"]),
        (case("observed final task gets fit check", "balanced", [{"limit_id": "week", "remaining_percent": 5}], ["FIT CHECK"], [])
         | {"capacity": base | {"calibration_status": "observed", "estimated_remaining": {"low": 1, "high": 2}, "windows": [{"limit_id": "week", "remaining_percent": 5}]}}),
    ]
