"""Issue #5: synthetic session-scoped parent/subagent attribution only."""
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


class RolloutAttributionTests(unittest.TestCase):
    PARENT = "private-synthetic-parent-session"
    CHILD = "private-synthetic-child-session"
    TURN = "private-synthetic-shared-turn"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.sessions = self.base / "synthetic-sessions"
        self.directory = self.sessions / "2033" / "05" / "18"
        self.directory.mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {
            "AGENT_EFFICIENCY_DATA": str(self.state),
            "AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH": "1",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.refresh = mock.patch.object(ae, "refresh_usage_snapshot", side_effect=AssertionError("Live refresh forbidden"))
        self.refresh.start()
        self.addCleanup(self.refresh.stop)

    @staticmethod
    def event() -> dict:
        return {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 7, "cached_input_tokens": 2,
                                 "output_tokens": 3, "reasoning_output_tokens": 1, "total_tokens": 10},
            # This is intentionally unrelated: summing cumulative usage would
            # catastrophically overstate both sessions' per-call measurements.
            "total_token_usage": {"input_tokens": 9000, "output_tokens": 999, "total_tokens": 9999},
        }}}

    def metadata(self, session: str, model: str, *, child: bool = False) -> list[dict]:
        source = {"subagent": {"thread_spawn": {"parent_thread_id": self.PARENT}}} if child else "cli"
        return [
            {"type": "session_meta", "payload": {"id": session, "source": source}},
            {"type": "turn_context", "payload": {"turn_id": self.TURN, "model": model}},
        ]

    def rollout(self, name: str, records: list[dict]) -> Path:
        path = self.directory / f"rollout-{name}.jsonl"
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        return path

    @staticmethod
    def append(path: Path, *records: dict) -> None:
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def cache(self) -> dict:
        return json.loads((self.state / "rollout-cache.json").read_text(encoding="utf-8"))

    def task(self, task_id: str, session: str) -> dict:
        return ae.start_task(task_id=task_id, session_id=session, turn_id=self.TURN,
                             mode="balanced", complexity="simple", model="initial-model")

    def test_parent_and_child_with_same_turn_id_remain_separate_and_count_each_call(self) -> None:
        self.task("parent-task", self.PARENT)
        self.task("child-task", self.CHILD)
        self.rollout("parent", self.metadata(self.PARENT, "parent-model") + [self.event(), self.event()])
        self.rollout("child", self.metadata(self.CHILD, "child-model", child=True) + [self.event()])

        result = ae.scan_rollouts(self.sessions)
        active = json.loads((self.state / "active.json").read_text(encoding="utf-8"))
        parent, child = active["parent-task"], active["child-task"]

        self.assertEqual(result["token_events_processed"], 3)
        self.assertEqual(result["turns_updated"], 2)
        self.assertEqual(parent["tokens"]["total"], 20)
        self.assertEqual(parent["tokens"]["model_calls"], 2)
        self.assertEqual(parent["tokens"]["cached_input"], 4)
        self.assertEqual(parent["tokens"]["reasoning"], 2)
        self.assertEqual(child["tokens"]["total"], 10)
        self.assertEqual(child["tokens"]["model_calls"], 1)
        self.assertEqual(parent["model"], "parent-model")
        self.assertEqual(child["model"], "child-model")
        self.assertIs(parent["is_subagent"], False)
        self.assertIs(child["is_subagent"], True)
        self.assertEqual(len(self.cache()["turns"]), 2)

    def test_repeated_scan_is_idempotent_but_an_identical_appended_call_is_counted(self) -> None:
        path = self.rollout("parent", self.metadata(self.PARENT, "parent-model") + [self.event()])
        self.assertEqual(ae.scan_rollouts(self.sessions)["token_events_processed"], 1)
        self.assertEqual(ae.scan_rollouts(self.sessions)["token_events_processed"], 0)
        self.append(path, self.event())
        self.assertEqual(ae.scan_rollouts(self.sessions)["token_events_processed"], 1)
        self.assertEqual(ae.scan_rollouts(self.sessions)["token_events_processed"], 0)
        tokens = next(iter(self.cache()["turns"].values()))["tokens"]
        self.assertEqual((tokens["total"], tokens["model_calls"]), (20, 2))

    def test_session_scoped_metadata_survives_a_scan_boundary(self) -> None:
        self.task("child-task", self.CHILD)
        path = self.rollout("child", self.metadata(self.CHILD, "child-model", child=True))
        self.assertEqual(ae.scan_rollouts(self.sessions)["token_events_processed"], 0)
        self.append(path, self.event(), self.event())
        self.assertEqual(ae.scan_rollouts(self.sessions)["token_events_processed"], 2)
        record = json.loads((self.state / "active.json").read_text(encoding="utf-8"))["child-task"]
        self.assertIs(record["is_subagent"], True)
        self.assertEqual(record["model"], "child-model")
        self.assertEqual(record["tokens"]["model_calls"], 2)

    def test_equal_hook_tokens_still_receive_history_model_and_subagent_metadata(self) -> None:
        self.task("parent-task", self.PARENT)
        self.task("child-task", self.CHILD)
        tokens = {"input": 7, "cached_input": 2, "reasoning": 1, "output": 3,
                  "total": 10, "model_calls": 1}
        ae.complete_task("parent-task", tokens=tokens)
        ae.complete_task("child-task", tokens=tokens)
        self.rollout("parent", self.metadata(self.PARENT, "parent-model") + [self.event()])
        self.rollout("child", self.metadata(self.CHILD, "child-model", child=True) + [self.event()])

        ae.scan_rollouts(self.sessions)
        history = {record["task_id"]: record for record in ae.list_history()}

        self.assertEqual(history["parent-task"]["model"], "parent-model")
        self.assertIs(history["parent-task"]["is_subagent"], False)
        self.assertEqual(history["child-task"]["model"], "child-model")
        self.assertIs(history["child-task"]["is_subagent"], True)
        self.assertEqual(history["child-task"]["tokens"]["model_calls"], 1)
        self.assertNotIn("tokens_updated_at", history["child-task"])
        before = (self.state / "history.jsonl").read_bytes()
        self.assertEqual(ae.scan_rollouts(self.sessions)["token_events_processed"], 0)
        self.assertEqual((self.state / "history.jsonl").read_bytes(), before)

    def test_changed_history_tokens_preserve_call_counts_and_scoped_metadata(self) -> None:
        self.task("child-task", self.CHILD)
        ae.complete_task("child-task", tokens={"input": 1, "output": 1, "model_calls": 1})
        self.rollout("child", self.metadata(self.CHILD, "child-model", child=True) + [self.event(), self.event()])
        ae.scan_rollouts(self.sessions)
        record = ae.list_history()[0]
        self.assertEqual((record["tokens"]["total"], record["tokens"]["model_calls"]), (20, 2))
        self.assertIs(record["is_subagent"], True)
        self.assertEqual(record["model"], "child-model")
        self.assertIn("tokens_updated_at", record)

    def test_session_ids_parent_references_and_rollout_paths_are_not_persisted(self) -> None:
        self.task("child-task", self.CHILD)
        self.rollout("private-parent-path", self.metadata(self.PARENT, "parent-model") + [self.event()])
        self.rollout("private-child-path", self.metadata(self.CHILD, "child-model", child=True) + [self.event()])
        ae.scan_rollouts(self.sessions)
        ae.complete_task("child-task")
        for name in ("active.json", "history.jsonl", "rollout-cache.json"):
            text = (self.state / name).read_text(encoding="utf-8")
            for private in (self.PARENT, self.CHILD, self.TURN, str(self.sessions),
                            "private-parent-path", "private-child-path", "parent_thread_id"):
                with self.subTest(file=name, private=private):
                    self.assertNotIn(private, text)


if __name__ == "__main__":
    unittest.main()
