"""Small sequential client for the Codex App Server stdio protocol."""

from __future__ import annotations

import contextlib
import json
import queue
import subprocess
import threading
import time
from typing import Any


def read_rate_limits(executable: str, *, timeout: float, version: str) -> dict[str, Any]:
    """Initialize App Server, then read its authoritative allowance snapshot."""

    deadline = time.monotonic() + max(0.2, float(timeout))
    try:
        process = subprocess.Popen(
            [executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise RuntimeError(f"Codex App Server rate-limit read failed: {exc}") from exc

    inbox: queue.Queue[str | None] = queue.Queue()

    def consume_stdout() -> None:
        assert process.stdout is not None
        try:
            for raw in process.stdout:
                inbox.put(raw)
        finally:
            inbox.put(None)

    reader = threading.Thread(target=consume_stdout, name="agent-efficiency-app-server", daemon=True)
    reader.start()

    def send(message: dict[str, Any]) -> None:
        if process.stdin is None:
            raise RuntimeError("Codex App Server stdin is unavailable")
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    try:
        send({
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "agent-efficiency", "version": version},
                "capabilities": {"experimentalApi": True},
            },
        })
        _await_response(inbox, request_id=1, deadline=deadline)

        # App Server does not process a pipelined request reliably before the
        # initialize response has been read. Keep this exchange sequential.
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        response = _await_response(inbox, request_id=2, deadline=deadline)
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Codex App Server returned no rate-limit result")
        return result
    except (OSError, BrokenPipeError) as exc:
        raise RuntimeError(f"Codex App Server rate-limit read failed: {exc}") from exc
    finally:
        if process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.close()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.5)


def _await_response(
    inbox: queue.Queue[str | None],
    *,
    request_id: int,
    deadline: float,
) -> dict[str, Any]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"Codex App Server timed out waiting for response {request_id}")
        try:
            raw = inbox.get(timeout=remaining)
        except queue.Empty as exc:
            raise RuntimeError(f"Codex App Server timed out waiting for response {request_id}") from exc
        if raw is None:
            raise RuntimeError(f"Codex App Server exited before response {request_id}")
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if message.get("id") != request_id:
            continue
        error = message.get("error")
        if isinstance(error, dict):
            detail = error.get("message") or "unknown App Server error"
            raise RuntimeError(f"Codex App Server rate-limit read failed: {detail}")
        return message
