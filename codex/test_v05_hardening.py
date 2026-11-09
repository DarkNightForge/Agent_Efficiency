from __future__ import annotations

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
SCRIPTS = ROOT / "plugins" / "agent-efficiency" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_efficiency as ae
import app_server_client as app_client
import codex_telemetry as telemetry
import mcp_server


class InputAndProcessHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.sessions = self.base / "sessions"
        os.environ["AGENT_EFFICIENCY_DATA"] = str(self.state)
        os.environ["AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH"] = "1"

    def tearDown(self) -> None:
        os.environ.pop("AGENT_EFFICIENCY_DATA", None)
        os.environ.pop("AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH", None)
        self.temp.cleanup()

    def test_state_and_external_json_reads_are_bounded(self) -> None:
        ae.get_settings()
        settings = self.state / "settings.json"
        settings.write_bytes(b" " * (ae.STATE_FILE_LIMITS["settings.json"] + 1))
        with self.assertRaisesRegex(ae.StateSecurityError, "oversized"):
            ae.get_settings()

        external = self.base / "external.json"
        external.write_bytes(b" " * (ae.MAX_EXTERNAL_JSON_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "1 MiB"):
            ae._load_external_json(external)

    def test_oversized_state_write_preserves_previous_file(self) -> None:
        with ae._locked() as paths:
            ae._atomic_text(paths["history"], "sentinel\n")
            before = paths["history"].read_bytes()
            with self.assertRaisesRegex(ae.StateSecurityError, "size limit"):
                ae._atomic_text(
                    paths["history"],
                    "x" * (ae.STATE_FILE_LIMITS["history.jsonl"] + 1),
                )
            self.assertEqual(paths["history"].read_bytes(), before)

    def _rollout(self, *records: bytes) -> Path:
        directory = self.sessions / "2026" / "09" / "02"
        directory.mkdir(parents=True)
        path = directory / "rollout-security.jsonl"
        with path.open("wb") as handle:
            for record in records:
                handle.write(record.rstrip(b"\n") + b"\n")
        return path

    @staticmethod
    def _record(value: dict[str, object]) -> bytes:
        return json.dumps(value, separators=(",", ":")).encode("utf-8")

    def test_rollout_scan_skips_an_oversized_record_and_resumes(self) -> None:
        token = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 3, "output_tokens": 2}},
            },
        }
        path = self._rollout(
            self._record({"type": "session_meta", "payload": {"id": "session"}}),
            self._record({"type": "turn_context", "payload": {"turn_id": "turn"}}),
            b"x" * (ae.MAX_ROLLOUT_LINE_BYTES + 32),
            self._record(token),
        )

        result = ae.scan_rollouts(self.sessions)

        self.assertEqual(result["token_events_processed"], 1)
        cache = json.loads((self.state / "rollout-cache.json").read_text(encoding="utf-8"))
        file_state = next(iter(cache["files"].values()))
        self.assertEqual(file_state["offset"], path.stat().st_size)

    def test_turn_token_reader_skips_an_oversized_record(self) -> None:
        token = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 7, "output_tokens": 5}},
            },
        }
        path = self._rollout(
            self._record({"type": "turn_context", "payload": {"turn_id": "turn"}}),
            b"x" * (telemetry.MAX_ROLLOUT_LINE_BYTES + 32),
            self._record(token),
        )

        measured = telemetry.collect_turn_tokens(path, turn_id="turn")

        self.assertEqual(measured["model_calls"], 1)
        self.assertEqual(measured["total"], 12)

    def test_mcp_runtime_schema_rejects_unknown_types_and_nonfinite_values(self) -> None:
        cases = [
            ("update_settings", {"history_limit": "SECRET-value"}),
            ("get_settings", {"unexpected": "SECRET-value"}),
            ("show_control_center", {"rate_limits": [{"usedPercent": float("nan")}]}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                response = mcp_server.handle({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                })
                self.assertTrue(response["result"]["isError"])
                text = response["result"]["content"][0]["text"]
                self.assertEqual(text, "Agent Efficiency rejected an invalid request.")
                self.assertNotIn("SECRET", text)

    def test_every_mcp_tool_has_closed_world_security_annotations(self) -> None:
        listing = mcp_server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        })
        overwriters = {"get_settings", "update_settings", "complete_task",
                       "ingest_usage", "select_task_mode", "scan_rollouts"}
        read_only = {"get_security_status", "render_control_center"}
        for tool in listing["result"]["tools"]:
            with self.subTest(tool=tool["name"]):
                annotations = tool["annotations"]
                self.assertEqual(annotations["destructiveHint"], tool["name"] in overwriters)
                self.assertFalse(annotations["openWorldHint"])
                self.assertEqual(annotations["readOnlyHint"], tool["name"] in read_only)
                self.assertIsInstance(annotations["idempotentHint"], bool)

    def test_mcp_line_input_is_bounded(self) -> None:
        raw = b"x" * (mcp_server.MAX_MCP_MESSAGE_BYTES + 32) + b"\n"
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(raw))
        with mock.patch.object(mcp_server.sys, "stdin", fake_stdin):
            with self.assertRaises(mcp_server.MCPMessageLimitError):
                mcp_server._read_message()

    def test_app_server_output_is_bounded(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(
                    b"x" * (app_client.MAX_APP_SERVER_LINE_BYTES + 1) + b"\n"
                )

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

        with mock.patch.object(app_client.subprocess, "Popen", return_value=FakeProcess()):
            with self.assertRaises(app_client.AppServerOutputLimitError):
                app_client.read_rate_limits("codex", timeout=0.5, version="test")


if __name__ == "__main__":
    unittest.main()
