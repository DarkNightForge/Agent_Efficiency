"""Tool annotations must reflect observable local effects, not just names."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "agent-efficiency" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_efficiency as ae
import mcp_server


class ToolEffectTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("fetch_rate_limits", "refresh_usage_snapshot", "scan_rollouts"):
            guard = mock.patch.object(ae, name, side_effect=AssertionError("Live activity forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.tools = {tool["name"]: tool for tool in mcp_server.TOOLS}

    def test_state_initializing_reads_are_not_declared_read_only(self) -> None:
        for name, operation in (("get_settings", ae.get_settings),
                                ("get_history", ae.list_history),
                                ("get_forecast_diagnostics", ae.forecast_diagnostics)):
            with self.subTest(tool=name), mock.patch.dict(os.environ, {
                "AGENT_EFFICIENCY_DATA": str(self.root / name),
                "AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH": "1",
            }):
                state = self.root / name
                self.assertFalse(state.exists())
                operation()
                self.assertTrue((state / ae.STATE_DIRECTORY_MARKER).is_file())
                self.assertFalse(self.tools[name]["annotations"]["readOnlyHint"])

    def test_settings_operations_declare_preference_overwrites(self) -> None:
        state = self.root / "settings"
        with mock.patch.dict(os.environ, {"AGENT_EFFICIENCY_DATA": str(state)}):
            ae.update_settings(default_mode="stretch")
            self.assertEqual(ae.get_settings()["default_mode"], "stretch")
            ae.update_settings(default_mode="balanced")
            path = state / "settings.json"
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["default_mode"], "balanced")
            saved["default_mode"] = "efficiency"
            ae._atomic_json(path, saved)
            ae.get_settings()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["default_mode"], "balanced")
        for name in ("update_settings", "get_settings"):
            self.assertTrue(self.tools[name]["annotations"]["destructiveHint"])


if __name__ == "__main__":
    unittest.main()
