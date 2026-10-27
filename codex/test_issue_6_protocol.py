"""Synthetic stdio sessions; never start the live telemetry worker."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "agent-efficiency" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import mcp_server as mcp


def frame(value: bytes | dict, style: str = "line") -> bytes:
    raw = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
    return (f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw) if style == "header" else raw + b"\n"


def unpack(raw: bytes, style: str) -> list[dict]:
    if style == "line":
        return [json.loads(line) for line in raw.splitlines()]
    stream, output = io.BytesIO(raw), []
    while first := stream.readline():
        assert first.startswith(b"Content-Length: "), first
        length = int(first.split(b":", 1)[1])
        assert stream.readline() == b"\r\n"
        output.append(json.loads(stream.read(length)))
    return output


PING = {"jsonrpc": "2.0", "id": 23, "method": "ping"}
INVALID_ARGUMENTS = (None, [], False, 0, 0.0, "", True, 1, "SECRET-input", [1])
# Keep handle/call_tool/schema validation real; replace only side-effect boundaries.
NO_ARGUMENT_TOOLS = {
    "get_settings": ("get_settings", {}),
    "get_security_status": ("get_security_status", {}),
    "update_settings": ("update_settings", {}),
    "refresh_usage": ("refresh_usage_snapshot", {"force": True, "raise_on_error": True}),
    "refresh_rate_limits": ("refresh_usage_snapshot", {"force": True, "raise_on_error": True}),
    "scan_rollouts": ("scan_rollouts", {}),
}
RUNNER = """
import sys
sys.path.insert(0, sys.argv[1])
import mcp_server as server
# The production worker refreshes usage AND scans rollouts. Disable the whole
# worker before main starts, not merely refresh via an environment variable.
server._telemetry_loop = lambda stop: None
def forbidden(*args, **kwargs):
    raise AssertionError('Live telemetry is forbidden in this synthetic test')
server.refresh_usage_snapshot = forbidden
server.scan_rollouts = forbidden
sys.exit(server.main())
"""


class ProtocolRecoveryTests(unittest.TestCase):
    def test_native_widget_access_is_explicit_and_limited_to_its_tools(self):
        allowed = {tool["name"] for tool in mcp.TOOLS if tool.get("_meta", {}).get("openai/widgetAccessible")}
        self.assertEqual(allowed, {"show_control_center", "update_settings", "select_task_mode"})
        for tool in mcp.TOOLS:
            if tool["name"] in allowed:
                self.assertEqual(tool["_meta"]["ui"]["visibility"], ["model", "app"])
            else:
                self.assertEqual(tool["_meta"]["ui"]["visibility"], ["model"])
                self.assertIs(tool["_meta"]["openai/widgetAccessible"], False)
        renderer = next(tool for tool in mcp.TOOLS if tool["name"] == "render_control_center")
        self.assertEqual(renderer["_meta"]["ui"]["resourceUri"], mcp.UI_URI)

    def run_session(self, data: bytes, style: str = "line", exit_code: int = 0) -> list[dict]:
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ)
            environment.update(
                AGENT_EFFICIENCY_DATA=str(Path(directory) / "state"),
                AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH="1",
                PYTHONDONTWRITEBYTECODE="1",
            )
            result = subprocess.run(
                [sys.executable, "-B", "-c", RUNNER, str(SCRIPTS)],
                input=data, capture_output=True, timeout=10, cwd=directory,
                env=environment, check=False,
            )
            self.assertEqual(result.returncode, exit_code, result.stderr.decode(errors="replace"))
            self.assertEqual(result.stderr, b"")
            self.assertNotIn(b"SECRET", result.stdout)
            self.assertEqual(list(Path(directory).iterdir()), [])
            return unpack(result.stdout, style)

    def test_malformed_first_body_recovers_in_both_framings(self):
        for style in ("line", "header"):
            with self.subTest(style=style):
                responses = self.run_session(frame(b'{"SECRET":', style) + frame(PING, style), style)
                self.assertEqual(responses[0]["error"]["code"], -32700)
                self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 23, "result": {}})

    def test_invalid_utf8_body_recovers_in_both_framings(self):
        for style in ("line", "header"):
            with self.subTest(style=style):
                responses = self.run_session(frame(b'"\xff"', style) + frame(PING, style), style)
                self.assertEqual(responses[0]["error"]["code"], -32700)
                self.assertEqual(responses[1]["id"], 23)

    def test_json_null_is_not_end_of_input(self):
        for style in ("line", "header"):
            with self.subTest(style=style):
                responses = self.run_session(frame(b"null", style) + frame(PING, style), style)
                self.assertEqual(responses[0]["error"]["code"], -32600)
                self.assertEqual(responses[1]["id"], 23)

    def test_invalid_envelope_does_not_crash_or_execute(self):
        for invalid in ({"method": "ping", "id": 1},
                        {"jsonrpc": "2.0", "method": [], "id": 1},
                        {"jsonrpc": "2.0", "method": "ping", "id": {}},
                        {"jsonrpc": "2.0", "method": "ping", "id": True}):
            with self.subTest(invalid=invalid):
                responses = self.run_session(frame(invalid) + frame(PING))
                self.assertEqual(responses[0]["error"]["code"], -32600)
                self.assertEqual(responses[1]["id"], 23)

    def test_notifications_do_not_reply_or_dispatch_request_tools(self):
        data = b"".join(frame(message) for message in (
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "get_settings"}},
            PING,
        ))
        self.assertEqual(self.run_session(data), [{"jsonrpc": "2.0", "id": 23, "result": {}}])

    def test_malformed_tool_params_followed_by_ping(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []}
        responses = self.run_session(frame(request) + frame(PING))
        self.assertEqual(responses[0]["error"]["code"], -32602)
        self.assertEqual(responses[1]["id"], 23)

    def test_non_object_arguments_rejected_before_tool_dispatch(self):
        for tool, (boundary, _) in NO_ARGUMENT_TOOLS.items():
            for arguments in INVALID_ARGUMENTS:
                with self.subTest(tool=tool, arguments=arguments), mock.patch.object(
                    mcp, boundary, return_value={"synthetic": True}
                ) as dispatch:
                    response = mcp.handle({
                        "jsonrpc": "2.0", "id": "invalid-args", "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments},
                    })
                    dispatch.assert_not_called()
                    self.assertEqual(response, {
                        "jsonrpc": "2.0", "id": "invalid-args", "result": {
                            "content": [{"type": "text", "text": "Agent Efficiency rejected an invalid request."}],
                            "isError": True,
                        },
                    })

    def test_omitted_or_empty_object_arguments_preserve_dispatch(self):
        for tool, (boundary, expected_kwargs) in NO_ARGUMENT_TOOLS.items():
            for supplied in ({}, {"arguments": {}}):
                with self.subTest(tool=tool, supplied=supplied), mock.patch.object(
                    mcp, boundary, return_value={"synthetic": True}
                ) as dispatch:
                    response = mcp.handle({
                        "jsonrpc": "2.0", "id": "valid-args", "method": "tools/call",
                        "params": {"name": tool, **supplied},
                    })
                    dispatch.assert_called_once_with(**expected_kwargs)
                    self.assertEqual(response["id"], "valid-args")
                    self.assertIs(response["result"]["isError"], False)
                    self.assertEqual(response["result"]["structuredContent"], {"result": {"synthetic": True}})

    def test_valid_object_arguments_reach_mutation_unchanged(self):
        arguments = {"default_mode": "stretch"}
        with mock.patch.object(mcp, "update_settings", return_value={"synthetic": True}) as dispatch:
            response = mcp.handle({
                "jsonrpc": "2.0", "id": 0, "method": "tools/call",
                "params": {"name": "update_settings", "arguments": arguments},
            })
            dispatch.assert_called_once_with(default_mode="stretch")
        self.assertEqual(arguments, {"default_mode": "stretch"})
        self.assertEqual(response["id"], 0)
        self.assertIs(response["result"]["isError"], False)

    def test_falsey_non_object_params_never_dispatch(self):
        for params in ([], False, 0, 0.0, ""):
            with self.subTest(params=params), mock.patch.object(mcp, "call_tool") as dispatch:
                response = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
                dispatch.assert_not_called()
                self.assertEqual(response["id"], 1)
                self.assertEqual(response["error"]["code"], -32602)

    def test_invalid_arguments_keep_both_framings_usable(self):
        for style in ("line", "header"):
            with self.subTest(style=style):
                requests = [
                    {"jsonrpc": "2.0", "id": index, "method": "tools/call",
                     "params": {"name": "refresh_usage", "arguments": arguments}}
                    for index, arguments in enumerate(INVALID_ARGUMENTS)
                ]
                responses = self.run_session(b"".join(frame(request, style) for request in [*requests, PING]), style)
                self.assertEqual(len(responses), len(requests) + 1)
                for request, response in zip(requests, responses):
                    self.assertEqual(response["id"], request["id"])
                    self.assertIs(response["result"]["isError"], True)
                    self.assertEqual(response["result"]["content"], [
                        {"type": "text", "text": "Agent Efficiency rejected an invalid request."},
                    ])
                self.assertEqual(responses[-1], {"jsonrpc": "2.0", "id": 23, "result": {}})

    def test_accepted_request_ids_always_receive_a_response(self):
        for method, request_id in (("unknown", None), ("notifications/initialized", 7),
                                   ("notifications/cancelled", 8)):
            with self.subTest(method=method, request_id=request_id):
                request = {"jsonrpc": "2.0", "id": request_id, "method": method}
                responses = self.run_session(frame(request) + frame(PING))
                self.assertEqual(responses[0]["id"], request_id)
                self.assertEqual(responses[0]["error"]["code"], -32601)
                self.assertEqual(responses[1]["id"], 23)

    def test_initialize_negotiates_supported_version_only(self):
        for version in (*mcp.SUPPORTED_PROTOCOL_VERSIONS, "unsupported-test", None):
            with self.subTest(version=version):
                request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": version}}
                responses = self.run_session(frame(request) + frame(PING))
                self.assertIn(responses[0]["result"]["protocolVersion"], mcp.SUPPORTED_PROTOCOL_VERSIONS)
                if version in mcp.SUPPORTED_PROTOCOL_VERSIONS:
                    self.assertEqual(responses[0]["result"]["protocolVersion"], version)
                self.assertEqual(responses[1]["id"], 23)

    def test_initialize_rejects_nonstrings_then_recovers(self):
        for value in ([], {}, 42, True):
            with self.subTest(value=value):
                request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": value}}
                responses = self.run_session(frame(request) + frame(PING))
                self.assertEqual(responses[0]["error"]["code"], -32602)
                self.assertEqual(responses[1]["id"], 23)

    def test_oversized_line_stops_instead_of_parsing_tail(self):
        responses = self.run_session(b"x" * (mcp.MAX_MCP_MESSAGE_BYTES + 1) + b"\n" + frame(PING), exit_code=1)
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["error"]["code"], -32700)

    def test_oversized_header_length_uses_header_error_and_stops(self):
        data = f"Content-Length: {mcp.MAX_MCP_MESSAGE_BYTES + 1}\r\n\r\n".encode() + frame(PING, "header")
        responses = self.run_session(data, "header", exit_code=1)
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["error"]["code"], -32700)

    def test_bad_lengths_and_duplicate_headers_stop_safely(self):
        for headers in (b"Content-Length: SECRET\r\n\r\n", b"Content-Length: -1\r\n\r\n",
                        b"Content-Length: 0\r\nContent-Length: 42\r\n\r\n"):
            with self.subTest(headers=headers):
                responses = self.run_session(headers + frame(PING, "header"), "header", exit_code=1)
                self.assertEqual(len(responses), 1)
                self.assertEqual(responses[0]["error"]["code"], -32700)

    def test_truncated_input_fails_boundedly(self):
        for data, style in ((b'{"SECRET":', "line"),
                            (b'Content-Length: 40\r\n\r\n{"SECRET":', "header"),
                            (b"Content-Length: 0\r\n", "header")):
            with self.subTest(style=style, data=data):
                self.assertEqual(len(self.run_session(data, style, exit_code=1)), 1)

    def test_oversized_header_block_is_bounded(self):
        data = b"Content-Length: 0\r\nX-Test: " + b"x" * mcp.MAX_MCP_HEADER_BYTES + b"\r\n\r\n"
        self.assertEqual(len(self.run_session(data, "header", exit_code=1)), 1)

    def test_recovery_uses_each_message_framing_not_last_good_message(self):
        first = frame(PING)
        second = frame(b'{"SECRET":', "header") + frame(PING, "header")
        # Decode the mixed response only after asserting the first line.
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, AGENT_EFFICIENCY_DATA=str(Path(directory) / "state"),
                       AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH="1", PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([sys.executable, "-B", "-c", RUNNER, str(SCRIPTS)],
                                    input=first + second, capture_output=True, timeout=10,
                                    env=env, cwd=directory, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, b"")
            self.assertEqual(list(Path(directory).iterdir()), [])
        line, rest = result.stdout.split(b"\n", 1)
        self.assertEqual(json.loads(line)["id"], 23)
        responses = unpack(rest, "header")
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["id"], 23)


if __name__ == "__main__":
    unittest.main()
