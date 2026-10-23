#!/usr/bin/env python3
"""Provider-native subscription pacing for Hermes Agent.

This module is intentionally independent of Hermes at import time so its policy,
persistence, and rendering can be tested with the Python standard library. The
plugin adapter in ``__init__.py`` supplies Hermes account snapshots and hooks.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


PLUGIN_NAME = "agent-efficiency"
STATE_VERSION = 1
FRESH_SECONDS = 15 * 60
HISTORY_SECONDS = 14 * 24 * 3600
MIN_RATE_SAMPLE_SECONDS = 5 * 60
RATE_LOOKBACK_SECONDS = 6 * 3600
LOCKOUT_WARN_HOURS = 0.75
PROMOTE_MIN_BLOCKED_HOURS = 2.0
PROMOTE_MAX_HORIZON_HOURS = 24.0
PROMOTE_ALWAYS_BLOCKED_HOURS = 24.0
PACE_ASK_RATIO = 1.0
PACE_REASK_STEP = 0.25
# Rate-denominator floor; 1.0 is day 1's day_index, so day-1 output is unchanged.
PACE_MIN_RATE_DAYS = 1.0
GO_WIDE_PACE_MAX = 0.85
GO_WIDE_SESSION_USED_MAX = 30.0

STATIC_POLICY = """Agent Efficiency policy:
- Only the newest <usage-budget> block is current. It supersedes every earlier block replayed in conversation history; never act on an older directive.
- A STALE, UNAVAILABLE, or ROUTE CHANGED block retracts all older directives and authorizes no usage-driven action.
- Default to silence about usage. Usage is background planning context, not the topic.
- Never silently reduce scope to conserve a subscription allowance. Unspent allowance expires and buys nothing.
- If the dynamic block says GO WIDE, quietly prefer the thorough approach: useful parallelism, wider verification, and deeper search. Do not announce the budget.
- If it says WOULD RUN OUT, tell the user first, name the expected lockout, offer the listed choices, and do not refuse. Continue at full scope if they choose that.
- If it says ASK BEFORE PROCEEDING, ask once before substantial work using the listed options. Do not ask for trivial work and do not narrow scope on your own.
- Read the lockout, not merely whether a cap precedes a reset. A cap shortly before reset is explicitly not a reason to cut scope.
- Percentages and reset times marked EXACT come from the provider's live meter. Forecasts are estimates from changes between meter snapshots.
"""


def _parse_epoch(value: object) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _format_clock(epoch: Optional[float]) -> str:
    if not epoch:
        return "unknown"
    return datetime.fromtimestamp(epoch).astimezone().strftime("%a %H:%M")


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _window_kind(label: str) -> str:
    lower = str(label or "").strip().lower()
    if "week" in lower or "seven_day" in lower:
        return "weekly"
    if "session" in lower or "five" in lower or "5h" in lower:
        return "session"
    return "other"


def _is_shared_week(label: str) -> bool:
    lower = str(label or "").strip().lower()
    return lower in {"week", "weekly", "current week", "seven day", "seven_day"}


class SnapshotStore:
    """Concurrent, profile-local snapshot/history storage."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                provider TEXT PRIMARY KEY,
                fetched_at REAL NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history (
                provider TEXT NOT NULL,
                label TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                used_percent REAL NOT NULL,
                reset_at REAL,
                PRIMARY KEY (provider, label, fetched_at)
            );
            CREATE INDEX IF NOT EXISTS history_provider_label
                ON history(provider, label, fetched_at);
            CREATE TABLE IF NOT EXISTS pace_acks (
                provider TEXT NOT NULL,
                reset_at REAL NOT NULL,
                day_index INTEGER NOT NULL,
                ratio REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (provider, reset_at)
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS refresh_leases (
                provider TEXT PRIMARY KEY,
                last_started REAL NOT NULL
            );
            """
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
            (str(STATE_VERSION),),
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record_snapshot(self, snapshot: dict) -> None:
        provider = str(snapshot.get("provider") or "").strip().lower()
        scope = str(snapshot.get("scope") or provider).strip().lower()
        fetched_at = float(snapshot.get("fetched_at") or 0)
        windows = snapshot.get("windows")
        if not provider or not scope or fetched_at <= 0 or not isinstance(windows, list):
            raise ValueError("snapshot requires provider, fetched_at, and windows")
        payload = dict(snapshot)
        payload["provider"] = provider
        payload["scope"] = scope
        payload["fetched_at"] = fetched_at
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        cutoff = fetched_at - HISTORY_SECONDS
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO snapshots(provider, fetched_at, payload_json)
                   VALUES(?, ?, ?)
                   ON CONFLICT(provider) DO UPDATE SET
                       fetched_at=excluded.fetched_at,
                       payload_json=excluded.payload_json
                   WHERE excluded.fetched_at >= snapshots.fetched_at""",
                (scope, fetched_at, encoded),
            )
            for window in windows:
                if not isinstance(window, dict):
                    continue
                label = str(window.get("label") or "").strip()
                used = window.get("used_percent")
                if not label or isinstance(used, bool) or not isinstance(used, (int, float)):
                    continue
                used_value = float(used)
                if not math.isfinite(used_value):
                    continue
                self._conn.execute(
                    """INSERT OR REPLACE INTO history
                       (provider, label, fetched_at, used_percent, reset_at)
                       VALUES(?, ?, ?, ?, ?)""",
                    (scope, label, fetched_at, used_value, _parse_epoch(window.get("reset_at"))),
                )
            self._conn.execute("DELETE FROM history WHERE fetched_at < ?", (cutoff,))

    def latest_snapshot(self, provider: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM snapshots WHERE provider = ?",
                (str(provider or "").strip().lower(),),
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def window_history(
        self,
        provider: str,
        label: str,
        *,
        reset_at: Optional[float],
        since: float,
    ) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT fetched_at, used_percent, reset_at
                     FROM history
                    WHERE provider = ? AND label = ? AND fetched_at >= ?
                    ORDER BY fetched_at""",
                (str(provider or "").strip().lower(), label, since),
            ).fetchall()
        values = [dict(row) for row in rows]
        if reset_at is None:
            return [row for row in values if row.get("reset_at") is None]
        return [
            row
            for row in values
            if row.get("reset_at") is not None and abs(float(row["reset_at"]) - reset_at) < 1
        ]

    def claim_pace_ask(
        self,
        provider: str,
        *,
        reset_at: float,
        day_index: int,
        ratio: float,
        now: float,
    ) -> bool:
        provider = str(provider or "").strip().lower()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT day_index, ratio FROM pace_acks WHERE provider = ? AND reset_at = ?",
                (provider, reset_at),
            ).fetchone()
            should_ask = (
                row is None
                or int(row["day_index"]) != day_index
                or ratio >= float(row["ratio"]) + PACE_REASK_STEP
            )
            if should_ask:
                self._conn.execute(
                    """INSERT INTO pace_acks(provider, reset_at, day_index, ratio, updated_at)
                       VALUES(?, ?, ?, ?, ?)
                       ON CONFLICT(provider, reset_at) DO UPDATE SET
                           day_index=excluded.day_index,
                           ratio=excluded.ratio,
                           updated_at=excluded.updated_at""",
                    (provider, reset_at, day_index, ratio, now),
                )
            return should_ask

    def claim_refresh(self, provider: str, *, now: float, interval: float) -> bool:
        """Claim one profile-wide provider refresh interval atomically."""
        provider = str(provider or "").strip().lower()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT last_started FROM refresh_leases WHERE provider = ?",
                    (provider,),
                ).fetchone()
                if row and now - float(row["last_started"]) < interval:
                    self._conn.rollback()
                    return False
                self._conn.execute(
                    """INSERT INTO refresh_leases(provider, last_started) VALUES(?, ?)
                       ON CONFLICT(provider) DO UPDATE SET last_started=excluded.last_started""",
                    (provider, now),
                )
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise


class UsageBudget:
    """Turn exact provider snapshots into compact planning context."""

    def __init__(self, store: SnapshotStore, *, now: Callable[[], float] = time.time):
        self.store = store
        self._now = now

    @staticmethod
    def unavailable_context(provider: str, reason: str) -> str:
        provider = str(provider or "unknown").strip().lower() or "unknown"
        return "\n".join(
            [
                "<usage-budget>",
                f"{reason} — CURRENT BLOCK: IGNORE ALL EARLIER <usage-budget> blocks "
                "and take no usage-driven action from them this turn.",
                f"Current meter for {provider} is not usable; no exact headroom or pacing directive is active.",
                "</usage-budget>",
            ]
        )

    def _forecast(self, provider: str, window: dict, now: float) -> Optional[dict]:
        reset_at = _parse_epoch(window.get("reset_at"))
        used = window.get("used_percent")
        label = str(window.get("label") or "")
        if reset_at is None or not isinstance(used, (int, float)) or reset_at <= now:
            return None
        rows = self.store.window_history(
            provider,
            label,
            reset_at=reset_at,
            since=now - RATE_LOOKBACK_SECONDS,
        )
        if len(rows) < 2:
            return None
        first, last = rows[0], rows[-1]
        elapsed = float(last["fetched_at"]) - float(first["fetched_at"])
        delta = float(last["used_percent"]) - float(first["used_percent"])
        if elapsed < MIN_RATE_SAMPLE_SECONDS or delta <= 0:
            return None
        rate_per_hour = delta / (elapsed / 3600)
        if rate_per_hour <= 0:
            return None
        hours_to_cap = max(0.0, 100.0 - float(used)) / rate_per_hour
        hours_to_reset = max(0.0, reset_at - now) / 3600
        blocked_hours = max(0.0, hours_to_reset - hours_to_cap)
        return {
            "label": label,
            "rate_per_hour": rate_per_hour,
            "hours_to_cap": hours_to_cap,
            "hours_to_reset": hours_to_reset,
            "blocked_hours": blocked_hours,
        }

    @staticmethod
    def _pace(window: Optional[dict], now: float) -> Optional[dict]:
        if not window:
            return None
        reset_at = _parse_epoch(window.get("reset_at"))
        used = window.get("used_percent")
        if reset_at is None or not isinstance(used, (int, float)):
            return None
        start = reset_at - 7 * 86400
        day_index = min(7, max(1, int((now - start) // 86400) + 1))
        allowed_percent = 100.0 * day_index / 7
        ratio = float(used) / allowed_percent if allowed_percent else 0.0
        # Elapsed time, not day_index: the allowance is day-granular on purpose,
        # but reusing that integer as the rate denominator understated the burn
        # by the unelapsed part of today and pushed the dry day later.
        elapsed = min(7.0, max(PACE_MIN_RATE_DAYS, (now - start) / 86400.0))
        daily_rate = float(used) / elapsed
        exhaust_day = None
        if daily_rate > 0:
            projected = math.ceil(100.0 / daily_rate)
            if projected < 7:
                exhaust_day = max(day_index, projected)
        return {
            "day_index": day_index,
            "allowed_percent": allowed_percent,
            "ratio": ratio,
            "over": ratio >= PACE_ASK_RATIO,
            "exhaust_day": exhaust_day,
            "reset_at": reset_at,
        }

    def context_for(
        self,
        scope: str,
        *,
        meter_provider: str = "",
        claim_ask: bool = True,
    ) -> str:
        scope = str(scope or "").strip().lower()
        snapshot = self.store.latest_snapshot(scope)
        provider = str(
            (snapshot or {}).get("provider") or meter_provider or scope or "unknown"
        ).strip().lower()
        if not snapshot:
            return self.unavailable_context(provider, "UNAVAILABLE")
        now = self._now()
        age = now - float(snapshot.get("fetched_at") or 0)
        if age < -60 or age > FRESH_SECONDS:
            return self.unavailable_context(provider, "STALE")
        windows = [w for w in snapshot.get("windows", []) if isinstance(w, dict)]
        if not windows:
            return self.unavailable_context(provider, "UNAVAILABLE")

        session = next((w for w in windows if _window_kind(w.get("label", "")) == "session"), None)
        weekly_candidates = [w for w in windows if _window_kind(w.get("label", "")) == "weekly"]
        shared_week = next((w for w in weekly_candidates if _is_shared_week(w.get("label", ""))), None)
        if shared_week is None and len(weekly_candidates) == 1:
            shared_week = weekly_candidates[0]
        pace = self._pace(shared_week, now)

        forecasts = [self._forecast(scope, w, now) for w in windows]
        forecasts = [item for item in forecasts if item]
        binding = max(forecasts, key=lambda item: item["blocked_hours"], default=None)
        promote = bool(
            binding
            and binding["blocked_hours"] >= PROMOTE_MIN_BLOCKED_HOURS
            and (
                binding["blocked_hours"] >= PROMOTE_ALWAYS_BLOCKED_HOURS
                or binding["hours_to_cap"] <= PROMOTE_MAX_HORIZON_HOURS
            )
        )

        ask = False
        if pace and pace["over"] and pace["exhaust_day"] is not None:
            ask = (
                self.store.claim_pace_ask(
                    scope,
                    reset_at=pace["reset_at"],
                    day_index=pace["day_index"],
                    ratio=pace["ratio"],
                    now=now,
                )
                if claim_ask
                else True
            )

        session_used = session.get("used_percent") if session else None
        go_wide = bool(
            session
            and shared_week
            and isinstance(session_used, (int, float))
            and float(session_used) <= GO_WIDE_SESSION_USED_MAX
            and pace
            and pace["ratio"] < GO_WIDE_PACE_MAX
            and not ask
            and not (binding and binding["blocked_hours"] >= LOCKOUT_WARN_HOURS)
        )

        lines = ["<usage-budget>"]
        if promote and binding:
            lines.append(
                "WOULD RUN OUT — CURRENT BLOCK, supersedes all earlier usage-budget "
                "blocks. TELL THE USER THIS FIRST, BEFORE ANSWERING: the "
                f"{binding['label']} caps in ~{_format_duration(binding['hours_to_cap'] * 3600)} "
                "at the observed meter burn, and they are then BLOCKED "
                f"~{_format_duration(binding['blocked_hours'] * 3600)} until it resets. "
                "Offer: continue at full scope · reduce scope or use a cheaper model · "
                "do the critical part now and the rest after reset. NOT a refusal — if "
                "they choose continue, continue at full scope."
            )
        else:
            lines.append(
                "CURRENT BLOCK — supersedes all earlier usage-budget blocks. "
                "Ignore every earlier usage directive and use only this block."
            )

        for window in windows:
            label = str(window.get("label") or "Allowance")
            used = window.get("used_percent")
            reset_at = _parse_epoch(window.get("reset_at"))
            if isinstance(used, (int, float)):
                line = f"{label}: {float(used):.0f}% used (EXACT, provider meter) · {max(0.0, 100.0-float(used)):.0f}% remaining"
            else:
                line = f"{label}: usage unavailable"
            if reset_at:
                line += f" · resets {_format_clock(reset_at)} (in {_format_duration(reset_at-now)})"
            lines.append(line)

        if binding and not promote:
            if binding["blocked_hours"] >= LOCKOUT_WARN_HOURS:
                lines.append(
                    f"Heads-up: {binding['label']} would cap in "
                    f"~{_format_duration(binding['hours_to_cap'] * 3600)}, blocking "
                    f"~{_format_duration(binding['blocked_hours'] * 3600)}. It is not "
                    "yet consequential enough to lead the response."
                )
            elif binding["hours_to_cap"] < binding["hours_to_reset"]:
                lines.append(
                    f"Note: {binding['label']} may cap in "
                    f"~{_format_duration(binding['hours_to_cap'] * 3600)}, but resets "
                    f"~{_format_duration(binding['blocked_hours'] * 3600)} later — a short "
                    "wait, then a full allowance. NOT a reason to cut scope; carry on."
                )

        if pace and shared_week:
            verdict = "OVER PACE" if pace["over"] else "under pace" if pace["ratio"] < GO_WIDE_PACE_MAX else "on pace"
            line = (
                f"Week pace: day {pace['day_index']}/7 — {float(shared_week['used_percent']):.0f}% used of "
                f"{pace['allowed_percent']:.0f}% allowed by now "
                f"({pace['ratio'] * 100:.0f}% of today's share, {verdict})"
            )
            if pace["exhaust_day"] is not None:
                line += f" · projects dry on day {pace['exhaust_day']}/7"
            lines.append(line)

        if go_wide:
            lines.append(
                "GO WIDE: headroom is ample and the week is under pace. Prefer the "
                "more thorough approach — useful parallelism, wider verification, and "
                "deeper search. Do NOT cut scope to save usage; unspent allowance "
                "expires at reset and buys nothing."
            )

        if ask and pace:
            # Days left AFTER the dry day, not after today -- measured from
            # day_index this read as a 10-day week ("dry on day 6/7 with 4
            # day(s) left"). exhaust_day + days_left must sum to 7.
            days_left = 7 - pace["exhaust_day"]
            blocked = f", blocking the last {days_left} day(s)" if days_left else ""
            lines.append(
                f"ASK BEFORE PROCEEDING: the week is at {pace['ratio'] * 100:.0f}% of "
                f"day {pace['day_index']}/7's share and projects dry on day "
                f"{pace['exhaust_day']}/7{blocked}. Before substantial "
                "work, ask with these options: continue at full scope · reduce scope or "
                "use a cheaper model · stop and resume after reset. This is a checkpoint, "
                "NOT a refusal; if they continue, do not raise it again today."
            )

        lines.append(
            f"Meter: read live {_format_duration(max(0, age))} ago from {provider}. "
            "Percentages and resets are exact; cap forecasts are estimates from meter changes."
        )
        lines.append("</usage-budget>")
        return "\n".join(lines)
