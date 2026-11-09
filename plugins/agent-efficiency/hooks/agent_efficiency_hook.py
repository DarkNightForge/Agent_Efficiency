#!/usr/bin/env python3
"""Thin Codex hook adapter for Agent Efficiency."""

from __future__ import annotations

import json
import sys
from pathlib import Path

MAX_HOOK_INPUT_BYTES = 1 * 1024 * 1024


def main() -> int:
    try:
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        sys.path.insert(0, str(scripts))
        from agent_efficiency import handle_hook

        raw = sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES + 1)
        if len(raw) > MAX_HOOK_INPUT_BYTES:
            return 0
        payload = json.loads(raw)
        result = handle_hook(payload)
        if result is not None:
            json.dump(result, sys.stdout, separators=(",", ":"))
            sys.stdout.write("\n")
        return 0
    except Exception:  # Hooks must never break or distract from a Codex task.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
