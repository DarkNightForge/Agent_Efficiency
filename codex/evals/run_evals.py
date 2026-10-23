#!/usr/bin/env python3
"""Run offline policy fixtures; exits nonzero on any behavioral regression."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins" / "agent-efficiency" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_efficiency as ae
from budget_policy import render_usage_budget
from scenarios import scenarios


def run() -> dict:
    results = []
    for scenario in scenarios():
        decisions = ae._policy_decisions(scenario["capacity"], scenario["mode"])
        info = ae.MODE_INFO[scenario["mode"]]
        text = render_usage_budget(
            scenario["capacity"], mode_label=info["label"],
            mode_behavior=info["behavior"], decisions=decisions,
        )
        missing = [value for value in scenario["contains"] if value not in text]
        forbidden = [value for value in scenario["excludes"] if value in text]
        results.append({"name": scenario["name"], "passed": not missing and not forbidden,
                        "missing": missing, "forbidden": forbidden})
    return {"passed": all(item["passed"] for item in results), "scenarios": len(results), "results": results}


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
