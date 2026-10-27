from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "agent-efficiency"
SCRIPTS = PLUGIN / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_efficiency as ae
import mcp_server


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class ReleaseHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        os.environ["AGENT_EFFICIENCY_DATA"] = str(self.state)
        os.environ["AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH"] = "1"

    def tearDown(self) -> None:
        os.environ.pop("AGENT_EFFICIENCY_DATA", None)
        os.environ.pop("AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH", None)
        self.temp.cleanup()

    def test_v050_release_surfaces_are_consistent(self) -> None:
        manifest = json.loads(
            (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        html = (PLUGIN / "assets" / "control_center.html").read_text(encoding="utf-8")
        readme = (ROOT / "codex" / "README.md").read_text(encoding="utf-8")

        self.assertEqual(ae.VERSION, "0.5.0")
        self.assertEqual(manifest["version"].split("+", 1)[0], ae.VERSION)
        self.assertIn("v0.5", manifest["interface"]["displayName"])
        self.assertTrue(mcp_server.UI_URI.endswith("v0.5.html"))
        self.assertIn('id="version">v0.5.0</span>', html)
        self.assertIn("Status: v0.5.0", readme)

    def test_dependency_free_install_verification_includes_manifest_schema(self) -> None:
        result = ae.verify_install(PLUGIN)

        self.assertEqual(result["status"], "pass", result["errors"])
        self.assertTrue(result["checks"]["manifest_schema"]["ok"])
        self.assertTrue(result["checks"]["version"]["ok"])

    def test_runtime_operations_do_not_modify_the_active_workspace(self) -> None:
        workspace = self.base / "workspace"
        workspace.mkdir()
        canary = workspace / "DO_NOT_TOUCH.txt"
        canary.write_bytes(b"private workspace canary\n")
        sessions = self.base / "empty-sessions"
        sessions.mkdir()
        before = _tree_snapshot(workspace)
        previous = Path.cwd()
        try:
            os.chdir(workspace)
            ae.get_settings()
            ae.get_security_status()
            task = ae.start_task(task_id="release-canary", mode="balanced", complexity="simple")
            ae.complete_task(task["task_id"], tokens={"input": 2, "output": 1})
            ae.list_history()
            ae.forecast_diagnostics()
            ae.scan_rollouts(sessions)
            ae.show_control_center("workspace canary", selected_mode="balanced")
        finally:
            os.chdir(previous)

        self.assertEqual(_tree_snapshot(workspace), before)
        self.assertEqual(canary.read_bytes(), b"private workspace canary\n")
        self.assertTrue(self.state.is_dir())

    def test_oversized_hook_payload_is_ignored_without_creating_state(self) -> None:
        hook_path = PLUGIN / "hooks" / "agent_efficiency_hook.py"
        spec = importlib.util.spec_from_file_location("ae_release_hook", hook_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        stdin = SimpleNamespace(buffer=io.BytesIO(b"x" * (hook.MAX_HOOK_INPUT_BYTES + 1)))

        with mock.patch.object(hook.sys, "stdin", stdin):
            self.assertEqual(hook.main(), 0)

        self.assertFalse(self.state.exists())

    def test_codex_ci_declares_least_privilege_release_checks(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "codex-tests.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertEqual(workflow.count("persist-credentials: false"), 2)
        self.assertEqual(workflow.count("timeout-minutes: 10"), 2)
        self.assertIn("Validate plugin structure and manifest", workflow)
        self.assertIn("--json verify", workflow)

    def test_public_mcp_contract_is_small_and_security_status_is_read_only(self) -> None:
        listing = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        tools = {tool["name"]: tool for tool in listing["result"]["tools"]}

        self.assertEqual(len(tools), 13)
        self.assertIn("get_security_status", tools)
        self.assertNotIn("refresh_rate_limits", tools)
        self.assertTrue(tools["get_security_status"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["get_security_status"]["annotations"]["destructiveHint"])


if __name__ == "__main__":
    unittest.main()
