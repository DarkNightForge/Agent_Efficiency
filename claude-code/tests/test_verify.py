#!/usr/bin/env python3
"""
Tests for `usage-budget verify` -- the install gate.

The agent-driven install path (BOOTSTRAP_FOR_AGENTS.md) tells an installing
agent it is not finished until this exits 0. That makes the EXIT CODE the
contract, not the printed text: an agent that reads a reassuring transcript and
declares victory is the exact failure this is built to prevent.

So these tests care about two things above all:

  1. Exit 1 when the install cannot actually work. A gate that only ever passes
     is not a gate. Half of these deliberately break something.
  2. The required/optional split. A fresh machine has no transcripts yet and
     that must NOT block the install, while unwired hooks must.

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))
SCRIPT = REPO_DIR / "usage_budget.py"

WIRED = {
    "hooks": {
        "UserPromptSubmit": [{"hooks": [
            {"type": "command", "command": f"python3 {SCRIPT} inject"}]}],
        "Stop": [{"hooks": [
            {"type": "command", "command": f"python3 {SCRIPT} collect >/dev/null 2>&1"}]}],
    }
}


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.state = root / "state"
        self.projects = root / "projects"
        for d in (self.home / ".claude", self.state, self.projects):
            d.mkdir(parents=True, exist_ok=True)
        os.environ["USAGE_BUDGET_HOME"] = str(self.state)
        os.environ["CLAUDE_PROJECTS_DIR"] = str(self.projects)

    # --- helpers ----------------------------------------------------------

    def write_settings(self, cfg):
        (self.home / ".claude" / "settings.json").write_text(json.dumps(cfg))

    def calibrate(self):
        (self.state / "config.json").write_text(json.dumps(
            {"calibrations": [{"pct": 13, "resets": "9:59pm"}]}))

    def run_verify(self, extra=()):
        env = dict(os.environ, HOME=str(self.home),
                   USERPROFILE=str(self.home),
                   USAGE_BUDGET_HOME=str(self.state),
                   CLAUDE_PROJECTS_DIR=str(self.projects))
        return subprocess.run(
            [sys.executable, str(SCRIPT), "verify", *extra],
            capture_output=True, text=True, timeout=60, env=env)

    def checks(self):
        proc = self.run_verify(["--json"])
        return json.loads(proc.stdout), proc.returncode

    def named(self, data, prefix):
        return next(c for c in data["checks"] if c["check"].startswith(prefix))

    # --- the gate must actually fail --------------------------------------

    def test_bare_machine_is_not_ready(self):
        self.write_settings({})
        proc = self.run_verify()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("NOT READY", proc.stdout)

    def test_unwired_hooks_block_the_install(self):
        self.write_settings({})
        self.calibrate()
        data, rc = self.checks()
        self.assertEqual(rc, 1)
        self.assertFalse(self.named(data, "hook: UserPromptSubmit")["ok"])
        self.assertFalse(self.named(data, "hook: Stop")["ok"])

    def test_uncalibrated_blocks_the_install(self):
        # The one step a human left alone reliably skips, so it is required.
        self.write_settings(WIRED)
        data, rc = self.checks()
        self.assertEqual(rc, 1)
        cap = self.named(data, "capacity known")
        self.assertFalse(cap["ok"])
        self.assertIn("UNCALIBRATED", cap["detail"])

    def test_half_wired_still_fails(self):
        # Only UserPromptSubmit wired: spend would never refresh.
        half = {"hooks": {"UserPromptSubmit": WIRED["hooks"]["UserPromptSubmit"]}}
        self.write_settings(half)
        self.calibrate()
        data, rc = self.checks()
        self.assertEqual(rc, 1)
        self.assertTrue(self.named(data, "hook: UserPromptSubmit")["ok"])
        self.assertFalse(self.named(data, "hook: Stop")["ok"])

    def test_someone_elses_hooks_do_not_count_as_ours(self):
        self.write_settings({"hooks": {
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": "echo inject"}]}],
            "Stop": [{"hooks": [
                {"type": "command", "command": "some_other_tool collect"}]}],
        }})
        self.calibrate()
        data, rc = self.checks()
        self.assertEqual(rc, 1)
        self.assertFalse(self.named(data, "hook: UserPromptSubmit")["ok"])

    # --- and it must pass when the install is real ------------------------

    def test_wired_and_calibrated_is_ready(self):
        self.write_settings(WIRED)
        self.calibrate()
        proc = self.run_verify()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("READY", proc.stdout)

    def test_missing_transcripts_do_not_block(self):
        # A fresh machine has none. Blocking here would make the gate unpassable
        # before the user's first session, which is precisely when they install.
        self.write_settings(WIRED)
        self.calibrate()
        data, rc = self.checks()
        self.assertEqual(rc, 0)
        tr = self.named(data, "transcripts")
        self.assertFalse(tr["ok"])
        self.assertFalse(tr["required"])

    def test_missing_skill_does_not_block(self):
        self.write_settings(WIRED)
        self.calibrate()
        data, _ = self.checks()
        self.assertFalse(self.named(data, "skill")["required"])

    # --- contract shape ---------------------------------------------------

    def test_json_is_parseable_and_carries_the_verdict(self):
        self.write_settings(WIRED)
        self.calibrate()
        data, rc = self.checks()
        self.assertIs(data["ok"], True)
        self.assertEqual(rc, 0)
        for c in data["checks"]:
            self.assertEqual({"check", "ok", "detail", "required", "fix"}, set(c))

    def test_every_failing_check_tells_you_how_to_fix_it(self):
        self.write_settings({})
        data, _ = self.checks()
        for c in data["checks"]:
            if not c["ok"]:
                self.assertTrue(c["fix"], f"{c['check']} fails with no fix")

    def test_malformed_settings_json_does_not_crash_the_gate(self):
        (self.home / ".claude" / "settings.json").write_text("{ not json")
        proc = self.run_verify()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("NOT READY", proc.stdout)

    def test_exit_code_is_boolean_not_a_count(self):
        # Agents branch on `if verify; then`. Anything but 0/1 breaks that.
        self.write_settings({})
        self.assertEqual(self.run_verify().returncode, 1)
        self.write_settings(WIRED)
        self.calibrate()
        self.assertEqual(self.run_verify().returncode, 0)


if __name__ == "__main__":
    unittest.main()
