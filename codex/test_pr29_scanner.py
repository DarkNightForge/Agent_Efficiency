"""PR #29: bounded rollout discovery must not starve the rest of the archive."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "agent-efficiency" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_efficiency as ae


class RolloutFairnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.sessions = self.base / "sessions"
        self.directory = self.sessions / "2026" / "09" / "12"
        self.directory.mkdir(parents=True)
        self.environment = mock.patch.dict(os.environ, {
            "AGENT_EFFICIENCY_DATA": str(self.state),
            "AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH": "1",
        })
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    @staticmethod
    def _event() -> dict:
        return {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 7, "output_tokens": 3},
        }}}

    def _rollout(self, name: str) -> Path:
        path = self.directory / f"rollout-{name}.jsonl"
        records = [
            {"type": "session_meta", "payload": {"id": f"private-session-{name}"}},
            {"type": "turn_context", "payload": {"turn_id": f"private-turn-{name}"}},
            self._event(),
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        return path

    def _cache(self) -> dict:
        return json.loads((self.state / "rollout-cache.json").read_text(encoding="utf-8"))

    def test_new_rollout_after_1000_older_files_is_eventually_measured_once(self) -> None:
        for index in range(1000):
            (self.directory / f"rollout-older-{index:04d}.jsonl").touch()
        latest = self._rollout("z-newest")
        self.assertEqual(sorted(self.directory.glob("rollout-*.jsonl"))[-1], latest)

        results = [ae.scan_rollouts(self.sessions) for _ in range(3)]

        self.assertTrue(all(result["files_seen"] <= 1000 for result in results))
        self.assertEqual(sum(result["token_events_processed"] for result in results), 1)
        cache = self._cache()
        self.assertEqual(len(cache["files"]), 1001)
        self.assertEqual(next(iter(cache["turns"].values()))["tokens"]["total"], 10)
        self.assertTrue(cache["scan_cursor"].startswith("ae-rollout-"))
        serialized = json.dumps(cache)
        for private in (str(self.sessions), "private-session", "private-turn", latest.name):
            self.assertNotIn(private, serialized)

    def test_byte_exhaustion_advances_only_past_files_actually_attempted(self) -> None:
        paths = [self._rollout(f"{index:02d}") for index in range(3)]
        file_size = paths[0].stat().st_size
        self.assertTrue(all(path.stat().st_size == file_size for path in paths))
        order = ae._rollout_scan_candidates(self.sessions, None)
        with mock.patch.object(ae, "MAX_ROLLOUT_TOTAL_BYTES_PER_SCAN", file_size):
            for expected_key, _ in order:
                result = ae.scan_rollouts(self.sessions)
                self.assertEqual(result["files_seen"], 1)
                self.assertEqual(result["token_events_processed"], 1)
                self.assertEqual(self._cache()["scan_cursor"], expected_key)
            self.assertEqual(ae.scan_rollouts(self.sessions)["token_events_processed"], 0)
        self.assertEqual(sum(turn["tokens"]["total"] for turn in self._cache()["turns"].values()), 30)

    def test_partial_lines_consume_the_byte_budget_even_when_resume_offset_rewinds(self) -> None:
        for index in range(3):
            (self.directory / f"rollout-{index}.jsonl").write_bytes(b'{"unfinished":' + b" " * 20)
        cursors = []
        with (
            mock.patch.object(ae, "MAX_ROLLOUT_TOTAL_BYTES_PER_SCAN", 10),
            mock.patch.object(ae, "MAX_ROLLOUT_FILE_BYTES_PER_SCAN", 10),
            mock.patch.object(ae, "_bounded_complete_lines", wraps=ae._bounded_complete_lines) as reader,
        ):
            for _ in range(3):
                result = ae.scan_rollouts(self.sessions)
                self.assertEqual(result["files_seen"], 1)
                self.assertEqual(result["lines_processed"], 0)
                cursors.append(self._cache()["scan_cursor"])
            self.assertEqual([call.kwargs["byte_limit"] for call in reader.call_args_list], [10, 10, 10])
        self.assertEqual(len(set(cursors)), 3)
        self.assertTrue(all(state["offset"] == 0 for state in self._cache()["files"].values()))

    def test_existing_schema3_offsets_survive_cursor_migration_and_append(self) -> None:
        path = self._rollout("existing")
        ae.scan_rollouts(self.sessions)
        cache = self._cache()
        cache.pop("scan_cursor")
        with ae._locked() as state_paths:
            ae._atomic_json(state_paths["rollout_cache"], cache)

        self.assertEqual(ae.scan_rollouts(self.sessions)["token_events_processed"], 0)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self._event()) + "\n")
        self.assertEqual(ae.scan_rollouts(self.sessions)["token_events_processed"], 1)
        self.assertEqual(ae.scan_rollouts(self.sessions)["token_events_processed"], 0)
        self.assertEqual(next(iter(self._cache()["turns"].values()))["tokens"]["total"], 20)

    def test_disappearing_file_and_nonregular_entry_do_not_block_other_rollouts(self) -> None:
        valid = self._rollout("valid")
        vanished = self.directory / "rollout-vanished.jsonl"
        nonregular = self.directory / "rollout-directory.jsonl"
        nonregular.mkdir()
        candidates = [(ae.opaque_ref(str(path.resolve()), "rollout"), path)
                      for path in (vanished, nonregular, valid)]
        with mock.patch.object(ae, "_rollout_scan_candidates", return_value=candidates):
            result = ae.scan_rollouts(self.sessions)
        self.assertEqual(result["files_seen"], 3)
        self.assertEqual(result["token_events_processed"], 1)
        self.assertEqual(len(self._cache()["files"]), 1)
        self.assertEqual(self._cache()["scan_cursor"], candidates[-1][0])

    def test_linked_rollout_is_not_opened(self) -> None:
        path = self._rollout("linked")
        with (
            mock.patch.object(ae, "_has_linked_component", return_value=True),
            mock.patch.object(ae.os, "open") as open_file,
        ):
            self.assertIsNone(ae._open_rollout_read(path))
        open_file.assert_not_called()

    def test_path_resolution_failure_does_not_abort_candidate_discovery(self) -> None:
        paths = [self._rollout("a"), self._rollout("b")]
        with mock.patch.object(Path, "resolve", side_effect=RuntimeError("cyclic link")):
            candidates = ae._rollout_scan_candidates(self.sessions, None)
        self.assertEqual({path for _, path in candidates}, set(paths))
        self.assertTrue(all(key.startswith("ae-rollout-") for key, _ in candidates))

    def test_rejected_link_cannot_displace_its_target_with_a_duplicate_cursor_key(self) -> None:
        linked = self._rollout("linked")
        target = self._rollout("target")
        original_resolve = Path.resolve

        def resolve(path: Path, *args, **kwargs) -> Path:
            return target if path == linked else original_resolve(path, *args, **kwargs)

        with (
            mock.patch.object(Path, "resolve", resolve),
            mock.patch.object(ae, "_has_linked_component", side_effect=lambda path: path == linked),
        ):
            candidates = dict(ae._rollout_scan_candidates(self.sessions, None))
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[ae.opaque_ref(str(linked.absolute()), "rollout")], linked)
        self.assertEqual(candidates[ae.opaque_ref(str(target.resolve()), "rollout")], target)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO fixture")
    def test_fifo_rollout_is_skipped_without_opening_it(self) -> None:
        path = self.directory / "rollout-fifo.jsonl"
        os.mkfifo(path)
        with mock.patch.object(ae.os, "open") as open_file:
            self.assertIsNone(ae._open_rollout_read(path))
        open_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
