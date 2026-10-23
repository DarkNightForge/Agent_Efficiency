#!/usr/bin/env python3
"""Adapter tests for Hermes hook registration and provider routing."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "agent-efficiency"
_spec = importlib.util.spec_from_file_location(
    "agent_efficiency_plugin_test",
    PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(PLUGIN_DIR)],
)
assert _spec and _spec.loader
_plugin = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _plugin
_spec.loader.exec_module(_plugin)

PluginRuntime = _plugin.PluginRuntime
_default_route_resolver = _plugin._default_route_resolver
_route_scope = _plugin._route_scope


class _FakeContext:
    def __init__(self) -> None:
        self.hooks = {}
        self.sections = {}
        self.commands = {}
        self.skills = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_system_prompt_section(self, name, content, **kwargs):
        self.sections[name] = (content, kwargs)

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = (handler, description, args_hint)

    def register_skill(self, name, path, description=""):
        self.skills[name] = (Path(path), description)


class AdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 2_000_000_000.0

    def _fetcher(self, provider, **kwargs):
        del kwargs
        return SimpleNamespace(
            provider=provider,
            source="usage_api",
            fetched_at=SimpleNamespace(timestamp=lambda: self.now),
            plan="Test",
            windows=(
                SimpleNamespace(
                    label="Session",
                    used_percent=12.0,
                    reset_at=SimpleNamespace(isoformat=lambda: "2033-05-18T06:33:20+00:00"),
                    detail=None,
                ),
            ),
            details=(),
            unavailable_reason=None,
            available=True,
        )

    def test_captured_route_refreshes_and_injects_for_the_same_provider(self) -> None:
        runtime = PluginRuntime(
            Path(self.temp.name) / "usage.db",
            fetch_account_usage=self._fetcher,
            now=lambda: self.now,
            start_threads=False,
        )
        self.addCleanup(runtime.close)
        runtime.capture_route("session-1", "anthropic", "https://api.anthropic.com")
        self.assertTrue(runtime.refresh("anthropic"))

        result = runtime.pre_llm_call(session_id="session-1", model="claude-test")

        self.assertIsInstance(result, dict)
        self.assertIn("12% used", result["context"])
        self.assertIn("anthropic", result["context"])

        persisted = runtime.store.latest_snapshot(runtime.route_for("session-1")["scope"])
        self.assertNotIn("plan", persisted)
        self.assertNotIn("details", persisted)
        self.assertNotIn("credential_base_url", persisted)
        self.assertNotIn("detail", persisted["windows"][0])

    def test_unknown_session_fails_open_without_guessing_an_account(self) -> None:
        runtime = PluginRuntime(
            Path(self.temp.name) / "usage.db",
            fetch_account_usage=self._fetcher,
            now=lambda: self.now,
            start_threads=False,
            route_resolver=lambda session_id, model: None,
        )
        self.addCleanup(runtime.close)
        self.assertIsNone(runtime.pre_llm_call(session_id="unknown", model="mystery"))

    def test_register_exposes_only_hooks_policy_command_and_skill_not_a_model_tool(self) -> None:
        runtime = PluginRuntime(
            Path(self.temp.name) / "usage.db",
            fetch_account_usage=self._fetcher,
            now=lambda: self.now,
            start_threads=False,
        )
        self.addCleanup(runtime.close)
        ctx = _FakeContext()

        _plugin.register(ctx, runtime=runtime)

        self.assertEqual(
            set(ctx.hooks),
            {"pre_llm_call", "post_llm_call", "pre_api_request"},
        )
        self.assertIn("agent-efficiency.policy", ctx.sections)
        self.assertIn("agent-efficiency", ctx.commands)
        self.assertIn("usage-budget", ctx.skills)
        self.assertTrue(ctx.skills["usage-budget"][0].is_file())
        self.assertFalse(hasattr(ctx, "tools"))

    def test_prompt_section_captures_first_turn_provider(self) -> None:
        runtime = PluginRuntime(
            Path(self.temp.name) / "usage.db",
            fetch_account_usage=self._fetcher,
            now=lambda: self.now,
            start_threads=False,
        )
        self.addCleanup(runtime.close)
        ctx = _FakeContext()
        _plugin.register(ctx, runtime=runtime)
        section, _ = ctx.sections["agent-efficiency.policy"]

        text = section(
            {
                "session_id": "session-2",
                "provider": "openai-codex",
                "model": "gpt-test",
                "platform": "cli",
            }
        )

        self.assertIn("Default to silence", text)
        self.assertEqual(runtime.route_for("session-2")["provider"], "openai-codex")

    def test_background_refresh_is_throttled_per_provider(self) -> None:
        calls = []
        first_done = threading.Event()
        second_done = threading.Event()

        def fetch(provider, **kwargs):
            calls.append((provider, kwargs))
            result = self._fetcher(provider, **kwargs)
            (first_done if len(calls) == 1 else second_done).set()
            return result

        runtime = PluginRuntime(
            Path(self.temp.name) / "usage.db",
            fetch_account_usage=fetch,
            now=lambda: self.now,
            start_threads=True,
        )
        self.addCleanup(runtime.close)
        runtime.capture_route("session-3", "anthropic")
        self.assertTrue(first_done.wait(1), "initial refresh did not finish")

        runtime.pre_llm_call(session_id="session-3", model="claude-test")
        runtime.post_llm_call(session_id="session-3", model="claude-test")
        self.assertFalse(second_done.wait(0.1), "refresh repeated inside five-minute throttle")

        self.now += 301
        runtime.post_llm_call(session_id="session-3", model="claude-test")
        self.assertTrue(second_done.wait(1), "refresh did not resume after throttle")
        self.assertEqual(len(calls), 2)

    def test_refresh_command_can_resolve_the_configured_route_before_a_turn(self) -> None:
        runtime = PluginRuntime(
            Path(self.temp.name) / "usage.db",
            fetch_account_usage=self._fetcher,
            now=lambda: self.now,
            start_threads=False,
            route_resolver=lambda session_id, model: {
                "provider": "openai-codex",
                "base_url": "https://chatgpt.com/backend-api/codex",
            },
        )
        self.addCleanup(runtime.close)

        output = runtime.command("refresh")

        self.assertIn("openai-codex: refreshed", output)

    def test_status_command_does_not_acknowledge_the_next_turn_checkpoint(self) -> None:
        reset = "2033-05-23T03:33:20+00:00"

        def over_pace(provider, **kwargs):
            del kwargs
            return SimpleNamespace(
                provider=provider,
                source="usage_api",
                fetched_at=SimpleNamespace(timestamp=lambda: self.now),
                windows=(
                    SimpleNamespace(label="Session", used_percent=30.0, reset_at=SimpleNamespace(isoformat=lambda: "2033-05-18T03:33:20+00:00")),
                    SimpleNamespace(label="Weekly", used_percent=55.0, reset_at=SimpleNamespace(isoformat=lambda: reset)),
                ),
                available=True,
            )

        runtime = PluginRuntime(
            Path(self.temp.name) / "usage.db",
            fetch_account_usage=over_pace,
            now=lambda: self.now,
            start_threads=False,
        )
        self.addCleanup(runtime.close)
        runtime.capture_route("session-4", "openai-codex", model="gpt-test", api_mode="codex_responses")
        self.assertTrue(runtime.refresh("openai-codex"))

        self.assertIn("ASK BEFORE PROCEEDING", runtime.command(""))
        turn = runtime.pre_llm_call(session_id="session-4", model="gpt-test")
        self.assertIn("ASK BEFORE PROCEEDING", turn["context"])

    def test_native_codex_app_server_does_not_claim_or_pretend_to_inject(self) -> None:
        runtime = PluginRuntime(
            Path(self.temp.name) / "usage.db",
            fetch_account_usage=self._fetcher,
            now=lambda: self.now,
            start_threads=False,
        )
        self.addCleanup(runtime.close)
        runtime.capture_route(
            "native-codex",
            "openai-codex",
            model="gpt-test",
            api_mode="codex_app_server",
        )
        self.assertTrue(runtime.refresh("openai-codex"))
        self.assertIsNone(runtime.pre_llm_call(session_id="native-codex", model="gpt-test"))

    def test_model_switch_does_not_inject_the_previous_provider_meter(self) -> None:
        runtime = PluginRuntime(
            Path(self.temp.name) / "usage.db",
            fetch_account_usage=self._fetcher,
            now=lambda: self.now,
            start_threads=False,
            route_resolver=lambda session_id, model: {
                "provider": "openai-codex" if model.startswith("gpt") else "anthropic",
                "base_url": "",
                "model": model,
                "api_mode": "codex_responses" if model.startswith("gpt") else "anthropic_messages",
            },
        )
        self.addCleanup(runtime.close)
        runtime.capture_route("switch", "anthropic", model="claude-test", api_mode="anthropic_messages")
        runtime.store.record_snapshot(
            {
                "provider": "anthropic",
                "source": "test",
                "fetched_at": self.now,
                "windows": [{"label": "Session", "used_percent": 77.0, "reset_at": "2033-05-18T06:33:20+00:00"}],
            }
        )
        runtime.store.record_snapshot(
            {
                "provider": "openai-codex",
                "source": "test",
                "fetched_at": self.now,
                "windows": [{"label": "Session", "used_percent": 11.0, "reset_at": "2033-05-18T06:33:20+00:00"}],
            }
        )

        result = runtime.pre_llm_call(session_id="switch", model="gpt-test")
        self.assertIn("11% used", result["context"])
        self.assertNotIn("77% used", result["context"])

    def test_same_provider_different_base_urls_do_not_share_exact_snapshots(self) -> None:
        def fetch(provider, **kwargs):
            base_url = kwargs.get("base_url") or ""
            used = 11.0 if "one.example" in base_url else 77.0
            return SimpleNamespace(
                provider=provider,
                source="usage_api",
                fetched_at=SimpleNamespace(timestamp=lambda: self.now),
                windows=(
                    SimpleNamespace(
                        label="Session",
                        used_percent=used,
                        reset_at=SimpleNamespace(isoformat=lambda: "2033-05-18T06:33:20+00:00"),
                    ),
                ),
                available=True,
            )

        runtime = PluginRuntime(
            Path(self.temp.name) / "usage.db",
            fetch_account_usage=fetch,
            now=lambda: self.now,
            start_threads=False,
        )
        self.addCleanup(runtime.close)
        runtime.capture_route("one", "openrouter", "https://one.example/api", model="m")
        runtime.capture_route("two", "openrouter", "https://two.example/api", model="m")
        self.assertTrue(runtime.refresh("openrouter", "https://one.example/api"))
        self.assertTrue(runtime.refresh("openrouter", "https://two.example/api"))

        one = runtime.pre_llm_call(session_id="one", model="m")["context"]
        two = runtime.pre_llm_call(session_id="two", model="m")["context"]
        self.assertIn("11% used", one)
        self.assertNotIn("77% used", one)
        self.assertIn("77% used", two)
        self.assertNotIn("11% used", two)

    def test_route_scope_preserves_case_and_trailing_slash_distinctions(self) -> None:
        self.assertNotEqual(
            _route_scope("openrouter", "https://proxy.example/TenantA"),
            _route_scope("openrouter", "https://proxy.example/tenanta"),
        )
        self.assertNotEqual(
            _route_scope("openrouter", "https://proxy.example/TenantA"),
            _route_scope("openrouter", "https://proxy.example/TenantA/"),
        )

    def test_current_hook_route_wins_on_same_model_endpoint_switch(self) -> None:
        runtime = PluginRuntime(
            Path(self.temp.name) / "usage.db",
            fetch_account_usage=self._fetcher,
            now=lambda: self.now,
            start_threads=False,
        )
        self.addCleanup(runtime.close)
        runtime.capture_route(
            "same-model-switch",
            "openrouter",
            "https://old.example/api",
            model="same-model",
            api_mode="chat_completions",
        )
        for base_url, used in (
            ("https://old.example/api", 11.0),
            ("https://new.example/api", 77.0),
        ):
            runtime.store.record_snapshot(
                {
                    "provider": "openrouter",
                    "scope": _route_scope("openrouter", base_url),
                    "source": "test",
                    "fetched_at": self.now,
                    "windows": [
                        {
                            "label": "Session",
                            "used_percent": used,
                            "reset_at": "2033-05-18T06:33:20+00:00",
                        }
                    ],
                }
            )

        result = runtime.pre_llm_call(
            session_id="same-model-switch",
            model="same-model",
            provider="openrouter",
            base_url="https://new.example/api",
            api_mode="chat_completions",
        )
        self.assertIn("77% used", result["context"])
        self.assertNotIn("11% used", result["context"])

    def test_meter_resolved_for_another_endpoint_is_rejected(self) -> None:
        def wrong_endpoint(provider, **kwargs):
            del kwargs
            snapshot = self._fetcher(provider)
            snapshot.credential_base_url = "https://actual.example/api"
            return snapshot

        runtime = PluginRuntime(
            Path(self.temp.name) / "usage.db",
            fetch_account_usage=wrong_endpoint,
            now=lambda: self.now,
            start_threads=False,
        )
        self.addCleanup(runtime.close)
        runtime.capture_route(
            "mismatch",
            "openrouter",
            "https://requested.example/api",
            model="m",
        )

        self.assertFalse(
            runtime.refresh("openrouter", "https://requested.example/api")
        )
        scope = runtime.route_for("mismatch")["scope"]
        self.assertIsNone(runtime.store.latest_snapshot(scope))

    def test_anthropic_canonical_meter_is_rejected_for_proxy_route(self) -> None:
        def canonical_anthropic(provider, **kwargs):
            del kwargs
            snapshot = self._fetcher(provider)
            snapshot.credential_base_url = "https://api.anthropic.com"
            return snapshot

        runtime = PluginRuntime(
            Path(self.temp.name) / "usage.db",
            fetch_account_usage=canonical_anthropic,
            now=lambda: self.now,
            start_threads=False,
        )
        self.addCleanup(runtime.close)
        proxy = "https://tenant-proxy.example/anthropic"
        runtime.capture_route("anthropic-proxy", "anthropic", proxy, model="claude")

        self.assertFalse(runtime.refresh("anthropic", proxy))
        scope = runtime.route_for("anthropic-proxy")["scope"]
        self.assertIsNone(runtime.store.latest_snapshot(scope))

    def test_resume_resolver_uses_dominant_billing_field_names(self) -> None:
        fake_state = ModuleType("hermes_state")

        class FakeDB:
            def get_dominant_session_model_route(self, session_id):
                self.session_id = session_id
                return {
                    "model": "gpt-test",
                    "billing_provider": "openai-codex",
                    "billing_base_url": "https://chatgpt.com/backend-api/codex",
                    "billing_mode": "subscription_included",
                }

            def get_session(self, session_id):
                raise AssertionError("dominant route should be used")

            def close(self):
                pass

        setattr(fake_state, "SessionDB", FakeDB)
        fake_runtime_module = ModuleType("hermes_cli.runtime_provider")

        def resolve_runtime_provider(**kwargs):
            self.assertEqual(kwargs["requested"], "openai-codex")
            return {
                "provider": "openai-codex",
                "base_url": kwargs["explicit_base_url"],
                "api_mode": "codex_responses",
            }

        setattr(fake_runtime_module, "resolve_runtime_provider", resolve_runtime_provider)
        with patch.dict(
            sys.modules,
            {
                "hermes_state": fake_state,
                "hermes_cli.runtime_provider": fake_runtime_module,
            },
        ):
            route = _default_route_resolver("resumed", "gpt-test")

        self.assertEqual(route["provider"], "openai-codex")
        self.assertEqual(route["api_mode"], "codex_responses")


if __name__ == "__main__":
    unittest.main()
