"""Issue #6: list positions and ambiguous IDs must never become measurements."""
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


class UsageWindowIdentityTests(unittest.TestCase):
    NOW = 2_000_000_000
    RESET = NOW + 4 * 3600

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "AGENT_EFFICIENCY_DATA": self.temp.name,
            "AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH": "1",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def window(self, used: float, **identity: object) -> dict:
        return {
            "used_percent": used, "resets_at": self.RESET,
            "window_duration_mins": 300, **identity,
        }

    def write_history(self, deltas: list[dict]) -> None:
        record = {
            "mode": "balanced", "status": "completed", "elapsed_seconds": 3600,
            "usage_delta": deltas,
        }
        (Path(self.temp.name) / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    def assert_unmeasured(self, before: list[dict], after: list[dict]) -> list[dict]:
        deltas = ae._usage_deltas(before, after)
        self.assertTrue(deltas)
        self.assertTrue(all(delta["same_window"] is False for delta in deltas))
        self.assertTrue(all("used_percent_delta" not in delta for delta in deltas))
        self.assertTrue(all("observation_seconds" not in delta for delta in deltas))
        return deltas

    def test_reordered_anonymous_windows_never_invent_positive_or_negative_samples(self) -> None:
        before = ae._normalize_rate_limits([self.window(10), self.window(90)])
        after = ae._normalize_rate_limits([self.window(91), self.window(11)])
        self.assertEqual([item["limit_id"] for item in before], ["window-1", "window-2"])
        self.assertTrue(all(item["identity_source"] == "anonymous" for item in before))
        deltas = self.assert_unmeasured(before, after)
        self.write_history(deltas)
        self.assertEqual(ae._observed_percent_cost("balanced"), (None, 0))
        self.assertEqual(ae._observed_active_turn_burn("balanced", "window-1"), (None, 0))

    def test_anonymous_provenance_survives_repeated_normalization(self) -> None:
        windows = ae._normalize_rate_limits([self.window(10)])
        for _ in range(3):
            windows = ae._normalize_rate_limits(windows)
            self.assertFalse(windows[0]["identity_eligible"])
            self.assertEqual(windows[0]["identity_source"], "anonymous")
            self.assertIsNone(ae._window_instance(windows[0]))

    def test_legacy_positional_ids_fail_closed_for_all_history_readers(self) -> None:
        self.write_history([{
            "limit_id": "window-1", "same_window": True, "window_instance": "legacy-cycle",
            "used_percent_delta": 81, "observation_seconds": 3600,
        }])
        self.assertIsNone(ae._window_instance(self.window(10, limit_id="window-1")))
        self.assertEqual(ae._observed_percent_cost("balanced", "window-1"), (None, 0))
        self.assertEqual(ae._observed_active_turn_burn("balanced", "window-1"), (None, 0))
        self.assertEqual(ae._observed_burn("balanced", "window-1", "legacy-cycle"), (None, 0))

    def test_stable_ids_survive_reordering_and_numeric_zero_remains_valid(self) -> None:
        before = [self.window(10, limit_id=0), self.window(90, limit_id="weekly")]
        after = [self.window(91, limit_id="weekly"), self.window(11, limit_id=0)]
        deltas = ae._usage_deltas(ae._normalize_rate_limits(before), ae._normalize_rate_limits(after))
        self.assertEqual([item["used_percent_delta"] for item in deltas], [1, 1])
        self.assertEqual([item["limit_id"] for item in deltas], ["weekly", 0])
        self.assertTrue(all(item["identity_eligible"] for item in deltas))

    def test_explicit_provenance_can_distinguish_real_provider_window_label(self) -> None:
        before = self.window(10, limit_id="window-1", identity_source="explicit", identity_eligible=True)
        after = {**before, "used_percent": 11}
        self.assertEqual(ae._usage_deltas([before], [after])[0]["used_percent_delta"], 1)

    def test_duplicate_ids_are_rejected_on_either_side(self) -> None:
        single = [self.window(10, limit_id="five-hour")]
        duplicate = [self.window(11, limit_id="five-hour"), self.window(91, limit_id="five-hour")]
        self.assert_unmeasured(single, duplicate)
        self.assert_unmeasured(duplicate, single)
        self.assertTrue(all(item["identity_source"] == "duplicate" for item in ae._normalize_rate_limits(duplicate)))

    def test_duplicate_numeric_and_text_ids_do_not_select_last_writer(self) -> None:
        windows = ae._normalize_rate_limits([self.window(10, limit_id=0), self.window(90, limit_id="0")])
        self.assertTrue(all(not item["identity_eligible"] for item in windows))
        saved = ae.ingest_usage(rate_limits=windows)["rate_limits"]
        self.assertEqual(len(saved), 2)
        self.assertTrue(all(not item["identity_eligible"] for item in saved))

    def test_missing_empty_boolean_and_container_ids_are_unidentified(self) -> None:
        for identity in ({}, {"limit_id": None}, {"limit_id": ""}, {"limit_id": True}, {"limit_id": []}):
            with self.subTest(identity=identity):
                before = ae._normalize_rate_limits([self.window(10, **identity)])
                after = ae._normalize_rate_limits([self.window(11, **identity)])
                self.assertFalse(before[0]["identity_eligible"])
                self.assert_unmeasured(before, after)

    def test_conflicting_aliases_never_get_laundered_into_stable_id(self) -> None:
        value = self.window(10, limit_id="five-hour", limitId="other")
        normalized = ae._normalize_rate_limits([value])
        self.assertEqual(normalized[0]["identity_source"], "conflicting")
        self.assertFalse(ae._normalize_rate_limits(normalized)[0]["identity_eligible"])
        self.assertIsNone(ae._window_instance(value))

    def test_null_alias_does_not_shadow_explicit_or_map_identity(self) -> None:
        before = self.window(10, limit_id=None, limitId="stable")
        after = {**before, "used_percent": 11}
        self.assertEqual(ae._usage_deltas([before], [after])[0]["used_percent_delta"], 1)
        self.assertEqual(ae._normalize_rate_limits([before])[0]["limit_id"], "stable")
        for snapshot in (before, {"limit_id": None, "primary": self.window(10)},
                         {"limit_id": None, "limitId": "stable", "primary": self.window(10)}):
            with self.subTest(snapshot=snapshot):
                windows = ae._normalize_rate_limits({"rateLimitsByLimitId": {"stable": snapshot}})
                self.assertTrue(all(item["identity_eligible"] for item in windows))
                self.assertTrue(all(item["limit_id"].startswith("stable") for item in windows))
        conflict = {"limit_id": None, "limitId": "different", "primary": self.window(10)}
        self.assertFalse(ae._normalize_rate_limits({"rateLimitsByLimitId": {"stable": conflict}})[0]["identity_eligible"])

    def test_negative_bucket_and_map_provenance_cannot_be_promoted_by_fallback(self) -> None:
        invalid = {"identity_source": "conflicting", "identity_eligible": False}
        inputs = [
            {"primary": self.window(10, **invalid)},
            {"rateLimitsByLimitId": {"stable": {"primary": self.window(10), **invalid}}},
            {"rateLimitsByLimitId": {"stable": self.window(10, **invalid)}},
            {"limit_id": "a", "limitId": "b", "primary": self.window(10, limitId="bucket")},
        ]
        for source in inputs:
            with self.subTest(source=source):
                normalized = ae._normalize_rate_limits(source)
                self.assertTrue(normalized)
                self.assertTrue(all(not item["identity_eligible"] for item in normalized))
                self.assertTrue(all(not item["identity_eligible"] for item in ae._normalize_rate_limits(normalized)))

    def test_map_inner_conflicts_reject_flat_and_semantic_windows(self) -> None:
        for snapshot in (
            self.window(10, limitId="inner"),
            {"limitId": "inner", "primary": self.window(10), "secondary": self.window(90)},
            {"limit_id": "inner", "primary": self.window(10, limitId="bucket")},
        ):
            with self.subTest(snapshot=snapshot):
                windows = ae._normalize_rate_limits({"rateLimitsByLimitId": {"outer": snapshot}})
                self.assertTrue(windows)
                self.assertTrue(all(not item["identity_eligible"] for item in windows))
                self.assertTrue(all(item["identity_source"] == "conflicting" for item in windows))

    def test_semantic_roles_and_map_numeric_zero_remain_stable(self) -> None:
        for parent in ({}, {"limitId": 0}, {"limit_id": "stable"}):
            with self.subTest(parent=parent):
                before = ae._normalize_rate_limits({**parent, "primary": self.window(10), "secondary": self.window(90)})
                after = ae._normalize_rate_limits({**parent, "secondary": self.window(91), "primary": self.window(11)})
                self.assertEqual([item["used_percent_delta"] for item in ae._usage_deltas(before, after)], [1, 1])
        windows = ae._normalize_rate_limits({"rateLimitsByLimitId": {0: {"limitId": 0, "primary": self.window(10)}}})
        self.assertEqual(windows[0]["limit_id"], "0:primary")
        self.assertTrue(windows[0]["identity_eligible"])

    def test_bucket_id_is_preserved_but_bucket_alias_conflict_is_not_trusted(self) -> None:
        valid = ae._normalize_rate_limits({"limitId": "codex", "primary": self.window(10, limitId=0)})
        self.assertEqual(valid[0]["limit_id"], 0)
        self.assertTrue(valid[0]["identity_eligible"])
        invalid = ae._normalize_rate_limits({"primary": self.window(10, limit_id="a", limitId="b")})
        self.assertFalse(invalid[0]["identity_eligible"])

    def test_reset_changes_are_still_unmeasurable(self) -> None:
        before = self.window(90, limit_id="five-hour")
        after = {**before, "used_percent": 1, "resets_at": self.RESET + 18_000}
        self.assert_unmeasured([before], [after])

    def test_sparse_stable_update_keeps_reset_and_omitted_window_timestamp(self) -> None:
        with mock.patch.object(ae, "_now", return_value="2033-05-18T03:33:20+00:00"):
            before = ae.ingest_usage(rate_limits=[self.window(10, limit_id="a"), self.window(90, limit_id="b")])
        with mock.patch.object(ae, "_now", return_value="2033-05-18T03:34:20+00:00"):
            after = ae.ingest_usage(rate_limits=[{"limit_id": "a", "used_percent": 11}])
        saved = {item["limit_id"]: item for item in after["rate_limits"]}
        self.assertEqual(saved["a"]["resets_at"], self.RESET)
        self.assertEqual(saved["b"]["captured_at"], before["rate_limits"][1]["captured_at"])
        self.assertEqual(saved["b"]["used_percent"], 90)
        self.assertTrue(saved["a"]["identity_eligible"])

    def test_sparse_anonymous_update_never_inherits_reset_or_becomes_trusted(self) -> None:
        before = ae.ingest_usage(rate_limits=[self.window(10)])["rate_limits"]
        after = ae.ingest_usage(rate_limits=[{"used_percent": 11}])["rate_limits"]
        self.assertIsNone(after[0].get("resets_at"))
        self.assertFalse(after[0]["identity_eligible"])
        self.assert_unmeasured(before, after)

    def test_conflict_cannot_borrow_fields_from_previously_valid_window(self) -> None:
        ae.ingest_usage(rate_limits=[self.window(10, limit_id="a")])
        result = ae.ingest_usage(rate_limits=[{"limit_id": "a", "limitId": "b", "used_percent": 11}])
        self.assertFalse(result["rate_limits"][0]["identity_eligible"])
        self.assertIsNone(result["rate_limits"][0].get("resets_at"))
        recovered = ae.ingest_usage(rate_limits=[{"limit_id": "a", "used_percent": 12}])
        self.assertTrue(recovered["rate_limits"][0]["identity_eligible"])
        self.assertIsNone(recovered["rate_limits"][0].get("resets_at"))

    def test_ambiguous_capacity_and_forecast_never_reuse_named_calibration(self) -> None:
        self.write_history([{
            "limit_id": "a", "same_window": True, "used_percent_delta": 81,
            "observation_seconds": 3600, "window_instance": ae._window_instance(self.window(10, limit_id="a")),
        }])
        with mock.patch.object(ae.time, "time", return_value=self.NOW):
            capacity = ae.estimate_capacity("balanced", [self.window(10, limit_id="a"), self.window(90, limit_id="a")])
        self.assertEqual(capacity["calibration_status"], "heuristic")
        self.assertEqual(capacity["sample_count"], 0)
        for window in capacity["windows"]:
            self.assertFalse(window["identity_eligible"])
            self.assertEqual(window["active_turn_burn_sample_count"], 0)
            self.assertEqual(window["burn_sample_count"], 0)
        snapshot = ae._capacity_forecast_snapshot(capacity)
        self.assertTrue(all(window["window_instance"] is None for window in snapshot["windows"]))
        self.assertTrue(all(not window["identity_eligible"] for window in snapshot["windows"]))

    def test_legacy_positional_capacity_outcomes_are_unavailable(self) -> None:
        forecast = {"capacity": {"available": True, "windows": [{
            "limit_id": "window-1", "window_instance": "legacy-cycle", "expected_task_cost_percent": 81,
        }]}}
        outcomes = ae._capacity_forecast_outcomes(forecast, [{
            "limit_id": "window-1", "window_instance": "legacy-cycle",
            "same_window": True, "used_percent_delta": 81,
        }])
        self.assertFalse(outcomes[0]["available"])

    def test_duplicate_historical_deltas_cannot_inflate_sample_counts(self) -> None:
        delta = {"limit_id": "a", "same_window": True, "used_percent_delta": 1,
                 "observation_seconds": 3600, "window_instance": "cycle"}
        self.write_history([delta, delta])
        self.assertEqual(ae._observed_percent_cost("balanced", "a"), (None, 0))
        self.assertEqual(ae._observed_active_turn_burn("balanced", "a"), (None, 0))
        self.assertEqual(ae._observed_burn("balanced", "a", "cycle"), (None, 0))


if __name__ == "__main__":
    unittest.main()
