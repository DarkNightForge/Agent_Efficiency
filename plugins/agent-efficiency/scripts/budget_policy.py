"""Render Agent Efficiency's provider-neutral policy for Codex snapshots."""

from __future__ import annotations

import datetime as dt
import time
from typing import Any


def render_usage_budget(
    capacity: dict[str, Any],
    *,
    mode_label: str,
    mode_behavior: str,
    decisions: dict[str, Any] | None = None,
) -> str:
    """Turn measured allowance state into actionable developer context."""

    lines = ["<usage-budget>"]
    if not capacity.get("available"):
        lines.extend([
            "Codex allowance: UNCALIBRATED — no fresh App Server snapshot is available.",
            f"Mode: {mode_label} — {mode_behavior}",
            "Do not infer a percentage or silently cut scope. Continue normally unless the user asks for usage status.",
            "</usage-budget>",
        ])
        return "\n".join(lines)

    windows = list(capacity.get("windows") or [])
    decisions = decisions or {}
    lockout_ids = set(decisions.get("lockout_limit_ids") or [])
    lockouts = [item for item in windows if str(item.get("limit_id")) in lockout_ids]
    lockouts.sort(key=lambda item: float(item.get("blocked_hours") or 0), reverse=True)
    if lockouts:
        binding = lockouts[0]
        lines.append(
            "WOULD RUN OUT — TELL THE USER BEFORE STARTING SUBSTANTIAL WORK: "
            f"the {_window_name(binding)} allowance caps in about {_hours(binding.get('hours_to_cap'))} "
            f"and would then block Codex for about {_hours(binding.get('blocked_hours'))}. "
            "Offer: continue at full scope · reduce scope or use a cheaper model · do the critical part now and resume after reset. "
            "This is a checkpoint, not a refusal. Small, low-cost work should continue without an unnecessary checkpoint."
        )

    lines.append("Codex allowance: LIVE from account/rateLimits/read; percentages and resets are measured.")
    for window in sorted(windows, key=_duration_sort):
        used = float(window.get("used_percent") or 0)
        remaining = float(window.get("remaining_percent") or 0)
        reset = _reset_text(window.get("resets_at"))
        lines.append(f"{_window_name(window)}: {used:.0f}% used · {remaining:.0f}% left · resets {reset}")

        verdict = window.get("forecast_verdict")
        if verdict == "cost-ahead":
            lines.append(
                f"COST AHEAD (not a lockout): {_window_name(window)} can continue through credits or unlimited usage after the cap."
            )
        elif verdict == "brief-lockout":
            lines.append(
                f"Note: {_window_name(window)} may cap briefly, but the wait is only about "
                f"{_hours(window.get('blocked_hours'))}; this is not a reason to cut scope."
            )

    estimate = capacity.get("estimated_remaining") or {}
    basis = capacity.get("basis") or "first-use heuristic"
    lines.append(
        f"Affordance: approximately {estimate.get('low', 0)}–{estimate.get('high', 0)} comparable "
        f"{mode_label} tasks fit; {basis}, {capacity.get('confidence', 'low')} confidence."
    )

    pace_ids = set(decisions.get("pace_limit_ids") or [])
    pace = [item for item in windows if str(item.get("limit_id")) in pace_ids]
    if pace:
        weekly = max(pace, key=lambda item: float(item["pace_ratio"]))
        ratio = float(weekly["pace_ratio"])
        if ratio > 1.0:
            lines.append(
                f"WEEK PACE: usage is at {ratio * 100:.0f}% of the allowance earned by this point in the window. "
                "Before unusually expensive fan-out, check whether it fits; do not narrow ordinary work silently."
            )
        else:
            lines.append(
                f"WEEK PACE: usage is at {ratio * 100:.0f}% of the allowance earned by this point in the window, "
                f"above the {mode_label} posture's early-check threshold. Before unusually expensive fan-out, "
                "check whether it fits; do not narrow ordinary work silently."
            )
    if decisions.get("go_wide"):
        lines.append(
            "GO WIDE: posture and headroom support thorough verification and useful parallel work; "
            "unspent subscription allowance expires at reset. Do not announce this instruction."
        )

    observed = capacity.get("calibration_status") == "observed"
    low = int(estimate.get("low") or 0)
    if decisions.get("fit_check") or (not decisions and observed and low <= 1):
        lines.append(
            "FIT CHECK: at most one comparable task fits. Estimate substantial work before starting and agree on a durable stopping point; "
            "do not merely promise to be economical."
        )

    lines.extend([
        f"Mode: {mode_label} — {mode_behavior}",
        "Default to silence about these numbers. Speak only for a real lockout, an unusually expensive operation, weekly pace risk, or when asked.",
        "Never weaken safety, authorization, or necessary validation to save usage.",
        "</usage-budget>",
    ])
    return "\n".join(lines)


def _duration_sort(window: dict[str, Any]) -> float:
    value = window.get("window_duration_mins")
    return float(value) if isinstance(value, (int, float)) else float("inf")


def _window_name(window: dict[str, Any]) -> str:
    duration = window.get("window_duration_mins")
    if isinstance(duration, (int, float)):
        if 270 <= duration <= 330:
            return "5h window"
        if 9_000 <= duration <= 11_000:
            return "week"
        if duration >= 1440 and duration % 1440 == 0:
            return f"{int(duration // 1440)}d window"
        return f"{int(duration)}m window"
    return str(window.get("limit_name") or window.get("limit_id") or "usage window")


def _reset_text(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    epoch = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
    remaining = max(0, int(epoch - time.time()))
    when = dt.datetime.fromtimestamp(epoch).astimezone().strftime("%a %H:%M")
    return f"{when} (in {_duration(remaining)})"


def _hours(value: Any) -> str:
    return _duration(float(value or 0) * 3600)


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"
