"""Hermes adapter for Agent Efficiency's provider-native pacing policy."""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from .agent_efficiency import PLUGIN_NAME, STATIC_POLICY, SnapshotStore, UsageBudget


logger = logging.getLogger(__name__)
_DEFAULT_RUNTIME = None
_DEFAULT_RUNTIME_LOCK = threading.Lock()
REFRESH_INTERVAL_SECONDS = 5 * 60


def _route_scope(provider: str, base_url: str = "") -> str:
    """Key snapshots by provider plus a non-secret digest of its endpoint."""
    provider = str(provider or "").strip().lower()
    endpoint = str(base_url or "").strip()
    if not endpoint:
        return provider
    # Do not lowercase or trim the path/query/trailing slash: HTTP paths can be
    # case-sensitive, and safety here means over-isolating aliases rather than
    # ever merging distinguishable credential routes.
    digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:12]
    return f"{provider}@{digest}"


def _as_epoch(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    timestamp = getattr(value, "timestamp", None)
    if callable(timestamp):
        try:
            return float(timestamp())
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _as_iso(value: object) -> Optional[str]:
    if value in (None, ""):
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return str(isoformat())
        except (TypeError, ValueError):
            return None
    return str(value)


def _serialize_snapshot(snapshot: object) -> Optional[dict]:
    if snapshot is None or getattr(snapshot, "available", True) is False:
        return None
    provider = str(getattr(snapshot, "provider", "") or "").strip().lower()
    fetched_at = _as_epoch(getattr(snapshot, "fetched_at", None))
    windows = []
    for window in getattr(snapshot, "windows", ()) or ():
        used = getattr(window, "used_percent", None)
        windows.append(
            {
                "label": str(getattr(window, "label", "") or ""),
                "used_percent": used,
                "reset_at": _as_iso(getattr(window, "reset_at", None)),
            }
        )
    if not provider or fetched_at <= 0 or not windows:
        return None
    return {
        "provider": provider,
        "source": str(getattr(snapshot, "source", "") or ""),
        "fetched_at": fetched_at,
        "windows": windows,
        "credential_base_url": str(
            getattr(snapshot, "credential_base_url", "") or ""
        ).strip(),
    }


def _default_route_resolver(session_id: str, model: str) -> Optional[dict]:
    """Resolve a persisted route, then the configured runtime, without credentials."""
    persisted = None
    if session_id:
        try:
            from hermes_state import SessionDB

            db = SessionDB()
            try:
                route = db.get_dominant_session_model_route(session_id)
                if not route:
                    row = db.get_session(session_id) or {}
                    route = {
                        "provider": row.get("billing_provider"),
                        "base_url": row.get("billing_base_url"),
                        "model": row.get("model"),
                    }
            finally:
                db.close()
            provider = str(
                (route or {}).get("billing_provider")
                or (route or {}).get("provider")
                or ""
            ).strip().lower()
            if provider:
                persisted = {
                    "provider": provider,
                    "base_url": str(
                        (route or {}).get("billing_base_url")
                        or (route or {}).get("base_url")
                        or ""
                    ).strip(),
                    "model": str((route or {}).get("model") or model or ""),
                }
        except Exception:
            logger.debug("agent-efficiency: persisted route lookup failed", exc_info=True)

    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        route = resolve_runtime_provider(
            requested=(persisted or {}).get("provider"),
            explicit_base_url=(persisted or {}).get("base_url") or None,
            target_model=model or (persisted or {}).get("model") or None,
        )
        provider = str(route.get("provider") or "").strip().lower()
        if provider:
            return {
                "provider": provider,
                "base_url": str(route.get("base_url") or "").strip(),
                "model": str(model or route.get("model") or (persisted or {}).get("model") or ""),
                "api_mode": str(route.get("api_mode") or "").strip(),
            }
    except Exception:
        logger.debug("agent-efficiency: configured route lookup failed", exc_info=True)
    return persisted


class PluginRuntime:
    """Hermes hook lifecycle, route discovery, refresh, and context injection."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        fetch_account_usage: Callable[..., object],
        now: Callable[[], float],
        start_threads: bool = True,
        route_resolver: Callable[[str, str], Optional[dict]] = _default_route_resolver,
    ) -> None:
        self.store = SnapshotStore(db_path)
        self.budget = UsageBudget(self.store, now=now)
        self._fetch = fetch_account_usage
        self._now = now
        self._start_threads = start_threads
        self._route_resolver = route_resolver
        self._routes: dict[str, dict] = {}
        self._routes_lock = threading.RLock()
        self._refreshing: set[str] = set()
        self._last_refresh_started: dict[str, float] = {}

    def close(self) -> None:
        self.store.close()

    def capture_route(
        self,
        session_id: str,
        provider: str,
        base_url: str = "",
        *,
        model: str = "",
        api_mode: str = "",
    ) -> None:
        session_id = str(session_id or "").strip()
        provider = str(provider or "").strip().lower()
        if not session_id or not provider or provider in {"auto", "custom", "moa"}:
            return
        with self._routes_lock:
            self._routes[session_id] = {
                "provider": provider,
                "base_url": str(base_url or "").strip(),
                "scope": _route_scope(provider, base_url),
                "model": str(model or "").strip(),
                "api_mode": str(api_mode or "").strip(),
            }
        if self._start_threads:
            self._refresh_async(provider, base_url)

    def route_for(self, session_id: str) -> Optional[dict]:
        with self._routes_lock:
            route = self._routes.get(str(session_id or "").strip())
            return dict(route) if route else None

    def _resolve_route(self, session_id: str, model: str) -> Optional[dict]:
        route = self.route_for(session_id)
        if route and route.get("model") and model and route["model"] != model:
            # A /model switch happens before pre_llm_call, while pre_api_request
            # cannot publish the new provider until later in the turn. Never
            # inject the previous model/provider's meter into the switch turn.
            route = self._route_resolver("", str(model or ""))
            if not route:
                return {"provider": "unknown", "route_changed": True, "model": model}
            self.capture_route(
                session_id,
                route.get("provider", ""),
                route.get("base_url", ""),
                model=route.get("model", "") or model,
                api_mode=route.get("api_mode", ""),
            )
            return self.route_for(session_id)
        if route:
            if route.get("api_mode"):
                return route
            resolved = self._route_resolver(str(session_id or ""), str(model or ""))
            if resolved and resolved.get("provider") == route.get("provider"):
                self.capture_route(
                    session_id,
                    resolved.get("provider", ""),
                    resolved.get("base_url", "") or route.get("base_url", ""),
                    model=resolved.get("model", "") or route.get("model", "") or model,
                    api_mode=resolved.get("api_mode", ""),
                )
                return self.route_for(session_id)
            return route
        route = self._route_resolver(str(session_id or ""), str(model or ""))
        if not route:
            return None
        self.capture_route(
            session_id,
            route.get("provider", ""),
            route.get("base_url", ""),
            model=route.get("model", "") or model,
            api_mode=route.get("api_mode", ""),
        )
        return self.route_for(session_id) or route

    def refresh(self, provider: str, base_url: str = "") -> bool:
        provider = str(provider or "").strip().lower()
        if provider not in {"openai-codex", "anthropic", "openrouter"}:
            return False
        if not base_url:
            with self._routes_lock:
                matches = [
                    route.get("base_url", "")
                    for route in self._routes.values()
                    if route.get("provider") == provider and route.get("base_url")
                ]
            if len(set(matches)) == 1:
                base_url = matches[0]
        try:
            snapshot = self._fetch(provider, base_url=base_url or None)
            payload = _serialize_snapshot(snapshot)
            if payload is None:
                return False
            credential_base_url = str(
                payload.pop("credential_base_url", "") or ""
            ).strip()
            requested_scope = _route_scope(provider, base_url)
            actual_scope = _route_scope(provider, credential_base_url)
            if base_url and credential_base_url and requested_scope != actual_scope:
                logger.warning(
                    "agent-efficiency: refusing %s meter resolved for a different "
                    "credential endpoint than the active route",
                    provider,
                )
                return False
            payload["scope"] = actual_scope if credential_base_url else requested_scope
            self.store.record_snapshot(payload)
            return True
        except Exception:
            logger.debug("agent-efficiency: account refresh failed for %s", provider, exc_info=True)
            return False

    def _refresh_async(self, provider: str, base_url: str = "") -> None:
        provider = str(provider or "").strip().lower()
        if provider not in {"openai-codex", "anthropic", "openrouter"}:
            return
        scope = _route_scope(provider, base_url)
        with self._routes_lock:
            now = self._now()
            if (
                scope in self._refreshing
                or now - self._last_refresh_started.get(scope, 0)
                < REFRESH_INTERVAL_SECONDS
            ):
                return
            if not self.store.claim_refresh(
                scope,
                now=now,
                interval=REFRESH_INTERVAL_SECONDS,
            ):
                return
            self._refreshing.add(scope)
            self._last_refresh_started[scope] = now

        def run() -> None:
            try:
                self.refresh(provider, base_url)
            finally:
                with self._routes_lock:
                    self._refreshing.discard(scope)

        threading.Thread(
            target=run,
            name=f"agent-efficiency-{provider}-refresh",
            daemon=True,
        ).start()

    def pre_llm_call(
        self,
        *,
        session_id: str = "",
        model: str = "",
        provider: str = "",
        base_url: str = "",
        api_mode: str = "",
        **kwargs: Any,
    ):
        del kwargs
        # Hermes v0.20.3+ supplies the actual current route before injection.
        # Capture it on every turn so same-model provider/endpoint switches do
        # not receive one turn of the previous route's exact meter.
        if provider:
            self.capture_route(
                session_id,
                provider,
                base_url,
                model=model,
                api_mode=api_mode,
            )
        route = self._resolve_route(session_id, model)
        if not route:
            return None
        if route.get("route_changed"):
            return {
                "context": self.budget.unavailable_context(
                    "unknown", "ROUTE CHANGED"
                )
            }
        if route.get("api_mode") == "codex_app_server":
            # Hermes v0.20.3's native Codex transport bypasses API-message
            # composition, so pre_llm_call context is not delivered there.
            return None
        provider = route.get("provider", "")
        context = self.budget.context_for(
            route.get("scope") or _route_scope(provider, route.get("base_url", "")),
            meter_provider=provider,
        )
        if self._start_threads:
            self._refresh_async(provider, route.get("base_url", ""))
        return {"context": context} if context else None

    def post_llm_call(self, *, session_id: str = "", model: str = "", **kwargs: Any) -> None:
        del kwargs
        route = self._resolve_route(session_id, model)
        if route and self._start_threads:
            self._refresh_async(route.get("provider", ""), route.get("base_url", ""))

    def pre_api_request(
        self,
        *,
        session_id: str = "",
        provider: str = "",
        base_url: str = "",
        model: str = "",
        api_mode: str = "",
        **kwargs: Any,
    ) -> None:
        del kwargs
        self.capture_route(
            session_id,
            provider,
            base_url,
            model=model,
            api_mode=api_mode,
        )

    def prompt_section(self, session_info: dict) -> str:
        self.capture_route(
            str(session_info.get("session_id") or ""),
            str(session_info.get("provider") or ""),
            "",
            model=str(session_info.get("model") or ""),
        )
        return STATIC_POLICY

    def command(self, raw_args: str) -> str:
        args = str(raw_args or "").strip().lower()
        with self._routes_lock:
            routes = list({route["scope"]: route for route in self._routes.values()}.values())
        if args in {"refresh", "update"}:
            if not routes:
                resolved = self._route_resolver("", "")
                if resolved:
                    routes = [resolved]
            if not routes:
                return "Agent Efficiency: no active provider route has been observed yet."
            results = [
                f"{route['provider']}: {'refreshed' if self.refresh(route['provider'], route.get('base_url', '')) else 'unavailable'}"
                for route in routes
            ]
            return "Agent Efficiency refresh\n" + "\n".join(results)

        if not routes:
            return "Agent Efficiency: no active provider route has been observed yet. Send a message, then retry."
        blocks = [
            self.budget.context_for(
                route.get("scope") or _route_scope(route["provider"], route.get("base_url", "")),
                meter_provider=route["provider"],
                claim_ask=False,
            )
            for route in routes
        ]
        blocks = [block for block in blocks if block]
        if not blocks:
            return "Agent Efficiency: no fresh provider meter is cached. Run /agent-efficiency refresh."
        return "\n\n".join(blocks)


def _make_default_runtime() -> PluginRuntime:
    from agent.account_usage import fetch_account_usage
    from plugins.plugin_storage import plugin_data_dir
    import time

    return PluginRuntime(
        plugin_data_dir(PLUGIN_NAME) / "usage.db",
        fetch_account_usage=fetch_account_usage,
        now=time.time,
    )


def _get_default_runtime() -> PluginRuntime:
    global _DEFAULT_RUNTIME
    with _DEFAULT_RUNTIME_LOCK:
        if _DEFAULT_RUNTIME is None:
            _DEFAULT_RUNTIME = _make_default_runtime()
        return _DEFAULT_RUNTIME


def register(ctx, *, runtime: Optional[PluginRuntime] = None) -> None:
    runtime = runtime or _get_default_runtime()
    ctx.register_hook("pre_llm_call", runtime.pre_llm_call)
    ctx.register_hook("post_llm_call", runtime.post_llm_call)
    ctx.register_hook("pre_api_request", runtime.pre_api_request)
    ctx.register_system_prompt_section(
        "agent-efficiency.policy",
        runtime.prompt_section,
        position="after_memory",
        max_chars=2000,
    )
    ctx.register_command(
        "agent-efficiency",
        runtime.command,
        description="Show or refresh subscription usage pacing",
        args_hint="[refresh]",
    )
    skill_path = Path(__file__).with_name("skills") / "usage-budget" / "SKILL.md"
    ctx.register_skill(
        "usage-budget",
        skill_path,
        "Interpret Agent Efficiency usage blocks and pacing directives",
    )
