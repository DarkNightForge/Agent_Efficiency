from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "agent-efficiency" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_efficiency as ae
import mcp_server


class PrivacyTransparencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        os.environ["AGENT_EFFICIENCY_DATA"] = str(self.state)
        os.environ["AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH"] = "1"

    def tearDown(self) -> None:
        os.environ.pop("AGENT_EFFICIENCY_DATA", None)
        os.environ.pop("AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH", None)
        self.temp.cleanup()

    def test_security_status_is_read_only_before_initialization(self) -> None:
        self.assertFalse(self.state.exists())

        status = ae.get_security_status()

        self.assertEqual(status["status"], "ready")
        self.assertTrue(status["safety_checks_passed"])
        self.assertFalse(status["state"]["initialized"])
        self.assertFalse(self.state.exists())
        serialized = json.dumps(status)
        self.assertNotIn(str(self.state), serialized)
        self.assertFalse(status["privacy"]["third_party_network"])
        self.assertTrue(status["privacy"]["may_delegate_authenticated_requests"])
        self.assertTrue(status["privacy"]["may_refresh_automatically"])

    def test_security_status_reports_storage_retention_and_privacy(self) -> None:
        ae.get_settings()
        task = ae.start_task(task_id="privacy-task", mode="balanced", complexity="simple")
        ae.complete_task(task["task_id"], tokens={"input": 4, "output": 2})

        status = ae.get_security_status()

        self.assertEqual(status["status"], "pass")
        self.assertTrue(status["state"]["ownership_marker_valid"])
        self.assertTrue(status["storage"]["within_limit"])
        self.assertGreater(status["storage"]["used_bytes"], 0)
        self.assertEqual(status["retention"]["history_records"], 1)
        self.assertEqual(status["retention"]["history_record_limit"], 500)
        self.assertIn("prompts and responses", status["privacy"]["excluded"])
        self.assertNotIn(str(self.state), json.dumps(status))

    def test_unexpected_entry_is_reported_and_future_writes_fail_closed(self) -> None:
        ae.get_settings()
        canary = self.state / "DO_NOT_TOUCH.txt"
        canary.write_text("private canary", encoding="utf-8")

        status = ae.get_security_status()

        self.assertEqual(status["status"], "attention")
        self.assertIn("unexpected-state-entries", status["issues"])
        with self.assertRaisesRegex(ae.StateSecurityError, "not owned"):
            ae.update_settings(default_mode="stretch")
        self.assertEqual(canary.read_text(encoding="utf-8"), "private canary")

    def test_mcp_replaces_duplicate_refresh_alias_with_security_status(self) -> None:
        listing = mcp_server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        })
        tools = {tool["name"]: tool for tool in listing["result"]["tools"]}
        self.assertIn("get_security_status", tools)
        self.assertNotIn("refresh_rate_limits", tools)
        self.assertTrue(tools["get_security_status"]["annotations"]["readOnlyHint"])

        response = mcp_server.handle({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_security_status", "arguments": {}},
        })
        self.assertFalse(response["result"]["isError"])
        result = response["result"]["structuredContent"]["result"]
        self.assertNotIn(str(self.state), json.dumps(result))

        compatibility = mcp_server.handle({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "refresh_rate_limits", "arguments": {}},
        })
        self.assertFalse(compatibility["result"]["isError"])

    def test_control_center_and_cli_surface_the_same_security_summary(self) -> None:
        center = ae.show_control_center("privacy review", selected_mode="balanced")
        self.assertIn("security", center)
        rendered = ae.render_control_center(center)
        self.assertIn("Security & Privacy: pass", rendered)
        self.assertIn("workspace writes are refused", rendered)

        with mock.patch("sys.stdout", new=io.StringIO()) as output:
            self.assertEqual(ae.main(["--json", "security-status"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "pass")

        html = (ROOT / "plugins" / "agent-efficiency" / "assets" / "control_center.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("Security &amp; Privacy", html)
        self.assertIn('id="storage-status"', html)
        self.assertIn('id="retention-status"', html)


if __name__ == "__main__":
    unittest.main()
