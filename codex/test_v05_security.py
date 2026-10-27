from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "agent-efficiency" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_efficiency as ae


def _snapshot(root: Path) -> dict[str, tuple[str, str | None]]:
    result: dict[str, tuple[str, str | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            result[relative] = ("directory", None)
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            result[relative] = ("file", digest)
    return result


class StateSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.previous_cwd = Path.cwd()
        os.environ["AGENT_EFFICIENCY_DATA"] = str(self.state)
        os.environ["AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH"] = "1"

    def tearDown(self) -> None:
        os.chdir(self.previous_cwd)
        os.environ.pop("AGENT_EFFICIENCY_DATA", None)
        os.environ.pop("AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH", None)
        self.temp.cleanup()

    def test_first_run_claims_a_dedicated_state_directory(self) -> None:
        settings = ae.get_settings()

        self.assertEqual(settings["default_mode"], "balanced")
        marker = json.loads((self.state / ae.STATE_DIRECTORY_MARKER).read_text(encoding="utf-8"))
        self.assertEqual(marker, {
            "owner": "agent-efficiency",
            "schema_version": ae.STATE_DIRECTORY_SCHEMA_VERSION,
        })
        self.assertLessEqual(
            {path.name for path in self.state.iterdir()},
            ae.STATE_FILENAMES,
        )

    def test_v04_state_is_adopted_without_rewriting_it(self) -> None:
        self.state.mkdir()
        settings_path = self.state / "settings.json"
        original = '{"default_mode":"stretch"}\n'
        settings_path.write_text(original, encoding="utf-8")

        self.assertEqual(ae.get_settings()["default_mode"], "stretch")
        self.assertEqual(settings_path.read_text(encoding="utf-8"), original)
        self.assertTrue((self.state / ae.STATE_DIRECTORY_MARKER).is_file())

    def test_unrelated_existing_directory_is_never_adopted(self) -> None:
        self.state.mkdir()
        canary = self.state / "notes.txt"
        canary.write_text("belongs to the user", encoding="utf-8")

        with self.assertRaises(ae.StateSecurityError):
            ae.get_settings()

        self.assertEqual(canary.read_text(encoding="utf-8"), "belongs to the user")
        self.assertEqual({path.name for path in self.state.iterdir()}, {"notes.txt"})

    def test_relative_and_dangerous_state_roots_are_rejected(self) -> None:
        os.chdir(self.workspace)
        for configured in (
            "relative-state",
            str(self.workspace),
            str(self.workspace / ".agent-efficiency"),
            str(Path.home()),
        ):
            with self.subTest(configured=configured):
                os.environ["AGENT_EFFICIENCY_DATA"] = configured
                with self.assertRaises(ae.StateSecurityError):
                    ae.get_settings()
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_state_symlinks_cannot_redirect_reads_or_writes(self) -> None:
        self.state.mkdir()
        outside = self.base / "outside.json"
        outside.write_text('{"default_mode":"throughput"}\n', encoding="utf-8")
        try:
            (self.state / "settings.json").symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        with self.assertRaises(ae.StateSecurityError):
            ae.get_settings()
        self.assertEqual(outside.read_text(encoding="utf-8"), '{"default_mode":"throughput"}\n')

    def test_state_hard_links_cannot_alias_user_files(self) -> None:
        self.state.mkdir()
        outside = self.base / "outside-hardlink.json"
        outside.write_text('{"default_mode":"throughput"}\n', encoding="utf-8")
        try:
            os.link(outside, self.state / "settings.json")
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")

        with self.assertRaises(ae.StateSecurityError):
            ae.get_settings()
        self.assertEqual(outside.read_text(encoding="utf-8"), '{"default_mode":"throughput"}\n')

    def test_state_root_symlink_is_rejected(self) -> None:
        target = self.base / "target"
        target.mkdir()
        try:
            self.state.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        with self.assertRaises(ae.StateSecurityError):
            ae.get_settings()
        self.assertEqual(list(target.iterdir()), [])

    def test_symlinked_parent_cannot_redirect_state_creation(self) -> None:
        target = self.base / "parent-target"
        target.mkdir()
        linked_parent = self.base / "linked-parent"
        try:
            linked_parent.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        os.environ["AGENT_EFFICIENCY_DATA"] = str(linked_parent / "state")

        with self.assertRaises(ae.StateSecurityError):
            ae.get_settings()
        self.assertEqual(list(target.iterdir()), [])

    def test_windows_junction_detection_fails_closed(self) -> None:
        self.state.mkdir()
        with mock.patch.object(ae.os.path, "isjunction", return_value=True, create=True):
            with self.assertRaises(ae.StateSecurityError):
                ae.get_settings()
        self.assertEqual(list(self.state.iterdir()), [])

    def test_invalid_ownership_marker_is_not_replaced(self) -> None:
        self.state.mkdir()
        marker = self.state / ae.STATE_DIRECTORY_MARKER
        marker.write_text('{"owner":"some-other-program"}\n', encoding="utf-8")

        with self.assertRaises(ae.StateSecurityError):
            ae.get_settings()
        self.assertEqual(marker.read_text(encoding="utf-8"), '{"owner":"some-other-program"}\n')

    def test_internal_writes_enforce_the_fixed_filename_allowlist(self) -> None:
        ae.get_settings()
        with self.assertRaises(ae.StateSecurityError):
            ae._atomic_json(self.state / "user-file.json", {"overwritten": True})
        self.assertFalse((self.state / "user-file.json").exists())

    def test_failed_atomic_replace_preserves_the_previous_file(self) -> None:
        ae.get_settings()
        history = self.state / "history.jsonl"
        history.write_text('{"original":true}\n', encoding="utf-8")

        with mock.patch.object(ae.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                ae._append_state_line(history, '{"replacement":true}')

        self.assertEqual(history.read_text(encoding="utf-8"), '{"original":true}\n')
        self.assertFalse(any(path.name.startswith(".history.jsonl.") for path in self.state.iterdir()))

    def test_every_public_state_operation_leaves_workspace_unchanged(self) -> None:
        (self.workspace / "keep.txt").write_text("keep me", encoding="utf-8")
        nested = self.workspace / "nested"
        nested.mkdir()
        (nested / "data.bin").write_bytes(b"\x00\x01\x02")
        before = _snapshot(self.workspace)
        sessions = self.base / "sessions"
        sessions.mkdir()
        os.chdir(self.workspace)

        ae.get_settings()
        ae.update_settings(default_mode="stretch", history_limit=10)
        ae.start_task(task_id="canary", mode="balanced", complexity="simple")
        ae.ingest_usage(task_id="canary", tokens={"input": 4, "output": 2})
        ae.complete_task("canary", validation_passed=True)
        ae.select_task_mode("throughput", session_id="opaque-session")
        ae.scan_rollouts(sessions)
        self.assertEqual(ae.verify_install()["status"], "pass")

        self.assertEqual(_snapshot(self.workspace), before)
        self.assertLessEqual(
            {path.name for path in self.state.iterdir()},
            ae.STATE_FILENAMES,
        )

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not authoritative on Windows")
    def test_state_directory_and_files_are_private_on_posix(self) -> None:
        ae.update_settings(default_mode="balanced")

        directory_mode = stat.S_IMODE(self.state.stat().st_mode)
        marker_mode = stat.S_IMODE((self.state / ae.STATE_DIRECTORY_MARKER).stat().st_mode)
        settings_mode = stat.S_IMODE((self.state / "settings.json").stat().st_mode)
        self.assertEqual(directory_mode & 0o077, 0)
        self.assertEqual(marker_mode & 0o077, 0)
        self.assertEqual(settings_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
