"""Regression coverage for PR #29's non-mutation and special-file findings."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "agent-efficiency" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_efficiency as ae


class StateSecurityReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "state"
        self.state.mkdir()
        if os.name != "nt":
            self.state.chmod(0o755)
        environment = mock.patch.dict(os.environ, {
            "AGENT_EFFICIENCY_DATA": str(self.state),
            "AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH": "1",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def _marker(self) -> None:
        (self.state / ae.STATE_DIRECTORY_MARKER).write_text(json.dumps({
            "owner": "agent-efficiency",
            "schema_version": ae.STATE_DIRECTORY_SCHEMA_VERSION,
        }), encoding="utf-8")

    def _snapshot(self) -> dict[str, tuple[int, bytes | None]]:
        return {
            str(path.relative_to(self.state)): (
                stat.S_IMODE(path.stat().st_mode),
                path.read_bytes() if path.is_file() else None,
            )
            for path in [self.state, *self.state.rglob("*")]
        }

    def _assert_rejected_without_mutation(self) -> None:
        before = self._snapshot()
        with mock.patch.object(ae, "_private_mode", wraps=ae._private_mode) as secure:
            with self.assertRaises(ae.StateSecurityError):
                ae.get_settings()
        self.assertEqual(self._snapshot(), before)
        # Windows does not enforce POSIX modes, but must still validate first.
        secure.assert_not_called()
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o755)

    def test_unclaimed_foreign_directory_keeps_permissions_and_contents(self) -> None:
        (self.state / "user-notes.txt").write_bytes(b"must remain untouched\n")
        self._assert_rejected_without_mutation()

    def test_malformed_marker_keeps_permissions_and_contents(self) -> None:
        (self.state / ae.STATE_DIRECTORY_MARKER).write_bytes(b"not JSON\n")
        self._assert_rejected_without_mutation()

    def test_unrecognized_marker_keeps_permissions_and_contents(self) -> None:
        (self.state / ae.STATE_DIRECTORY_MARKER).write_bytes(b'{"owner":"another-app"}\n')
        self._assert_rejected_without_mutation()

    def test_claimed_directory_with_foreign_file_is_not_modified(self) -> None:
        self._marker()
        (self.state / "user-notes.txt").write_bytes(b"still belongs to the user\n")
        self._assert_rejected_without_mutation()

    def test_nonregular_owned_target_is_not_modified(self) -> None:
        self._marker()
        (self.state / "settings.json").mkdir()
        (self.state / "settings.json" / "keep.txt").write_bytes(b"keep\n")
        self._assert_rejected_without_mutation()

    def test_valid_legacy_directory_is_secured_then_claimed_without_rewrite(self) -> None:
        original = b'{"default_mode":"stretch"}\n'
        settings = self.state / "settings.json"
        settings.write_bytes(original)
        secured_before_claim: list[bool] = []
        original_private_mode = ae._private_mode

        def secure(path: Path, mode: int) -> None:
            if path == self.state:
                secured_before_claim.append(not (self.state / ae.STATE_DIRECTORY_MARKER).exists())
            original_private_mode(path, mode)

        with mock.patch.object(ae, "_private_mode", side_effect=secure):
            self.assertEqual(ae._prepare_state_directory(), self.state)
        self.assertEqual(secured_before_claim, [True])
        self.assertEqual(settings.read_bytes(), original)
        self.assertEqual(ae.get_settings()["default_mode"], "stretch")
        self.assertTrue((self.state / ae.STATE_DIRECTORY_MARKER).is_file())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((self.state / ae.STATE_DIRECTORY_MARKER).stat().st_mode), 0o600,
            )

    def test_nonregular_read_is_rejected_before_open(self) -> None:
        target = self.state / "settings.json"
        target.mkdir()
        with mock.patch.object(ae.os, "open", wraps=os.open) as opened:
            with self.assertRaises(ae.StateSecurityError):
                ae._read_text_unchecked(target)
        opened.assert_not_called()

    def _descriptor_swap(self, *, mode: int | None = None, inode_delta: int = 0,
                         links: int | None = None, permissions: bool = False) -> None:
        target = self.state / "settings.json"
        target.write_bytes(b"{}")
        changed = list(target.stat())
        if mode is not None:
            changed[0] = mode
        changed[1] += inode_delta
        if links is not None:
            changed[3] = links
        fake_stat = os.stat_result(changed)
        real_open = os.open
        nonblocking = getattr(os, "O_NONBLOCK", 0x40000000)
        # Windows has no FIFO open flag. Strip the injected sentinel only before
        # calling the real Windows open, while inspecting the requested flags.
        native_nonblocking = hasattr(os, "O_NONBLOCK")
        descriptors: list[int] = []

        def open_target(path: Path, flags: int, *args: object, **kwargs: object) -> int:
            self.assertTrue(flags & nonblocking, "open must not block on a raced FIFO")
            if not native_nonblocking:
                flags &= ~nonblocking
            fd = real_open(path, flags, *args, **kwargs)
            descriptors.append(fd)
            return fd

        with contextlib.ExitStack() as stack, \
                mock.patch.object(ae.os, "O_NONBLOCK", nonblocking, create=True), \
                mock.patch.object(ae.os, "open", side_effect=open_target), \
                mock.patch.object(ae.os, "fstat", return_value=fake_stat):
            if permissions:
                # Exercise the POSIX helper's decision logic on Windows too,
                # but never perform a chmod or claim real FIFO coverage here.
                stack.enter_context(mock.patch.object(ae.os, "name", "posix"))
                chmod = stack.enter_context(mock.patch.object(ae.os, "fchmod", create=True))
            with self.assertRaises(ae.StateSecurityError):
                if permissions:
                    ae._private_mode(target, 0o600)
                else:
                    ae._read_text_unchecked(target)
            if permissions:
                chmod.assert_not_called()
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        self.assertEqual(target.read_bytes(), b"{}")

    def test_read_rejects_descriptor_swapped_to_fifo_and_closes_it(self) -> None:
        self._descriptor_swap(mode=stat.S_IFIFO | 0o600)

    def test_read_rejects_replacement_regular_file_identity(self) -> None:
        self._descriptor_swap(inode_delta=1)

    def test_read_rejects_descriptor_that_became_hardlinked(self) -> None:
        self._descriptor_swap(links=2)

    def test_permission_helper_rejects_fifo_replacement_without_chmod(self) -> None:
        self._descriptor_swap(mode=stat.S_IFIFO | 0o600, permissions=True)

    def test_permission_helper_rejects_regular_replacement_without_chmod(self) -> None:
        self._descriptor_swap(inode_delta=1, permissions=True)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_security_status_rejects_fifo_marker_settings_and_history_without_hanging(self) -> None:
        for filename, issue in (
            (ae.STATE_DIRECTORY_MARKER, "ownership-marker-invalid-or-missing"),
            ("settings.json", "settings-invalid"),
            ("history.jsonl", "history-unreadable-or-oversized"),
        ):
            with self.subTest(filename=filename):
                self._run_fifo_child(filename, "status", issue)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_fifo_replacement_between_validation_and_open_does_not_hang(self) -> None:
        for operation in ("read", "permissions"):
            with self.subTest(operation=operation):
                self._run_fifo_child("settings.json", operation, "")

    def _run_fifo_child(self, filename: str, operation: str, expected_issue: str) -> None:
        # Only the disposable subprocess can encounter a blocking FIFO open.
        # subprocess.run kills and reaps it on timeout, keeping the suite bounded.
        child = r'''
import json, os, stat, sys, tempfile
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
import agent_efficiency as ae
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory) / "state"
    root.mkdir(mode=0o700)
    os.environ["AGENT_EFFICIENCY_DATA"] = str(root)
    os.environ["AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH"] = "1"
    marker = root / ae.STATE_DIRECTORY_MARKER
    marker.write_text(json.dumps({"owner": "agent-efficiency", "schema_version": ae.STATE_DIRECTORY_SCHEMA_VERSION}))
    target = root / sys.argv[2]
    operation = sys.argv[3]
    if operation == "status":
        if target.exists():
            target.unlink()
        os.mkfifo(target, 0o600)
        before = target.lstat()
        result = ae.get_security_status()
        assert result["status"] == "attention", result
        assert sys.argv[4] in result["issues"], result
        after = target.lstat()
        assert stat.S_ISFIFO(after.st_mode)
        assert os.path.samestat(before, after)
        assert stat.S_IMODE(before.st_mode) == stat.S_IMODE(after.st_mode)
    else:
        target.write_text("{}")
        real_open = os.open
        swaps = []
        def swap(path, flags, *args, **kwargs):
            if Path(path) == target:
                target.unlink()
                os.mkfifo(target, 0o600)
                swaps.append(flags)
                assert flags & os.O_NONBLOCK, "missing O_NONBLOCK"
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(ae.os, "open", side_effect=swap):
            try:
                if operation == "read":
                    ae._read_text_unchecked(target)
                else:
                    ae._private_mode(target, 0o600)
            except ae.StateSecurityError:
                pass
            else:
                raise AssertionError("accepted FIFO replacement")
        assert len(swaps) == 1, swaps
        assert stat.S_ISFIFO(target.lstat().st_mode)
print("safe")
'''
        try:
            result = subprocess.run(
                [sys.executable, "-c", child, str(SCRIPTS), filename, operation, expected_issue],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail(f"{operation} blocked on FIFO {filename} for more than five seconds")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "safe")


if __name__ == "__main__":
    unittest.main()
