"""R1: explicit snapshot timestamps must not manufacture freshness."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "agent-efficiency" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_efficiency as ae


class ExplicitSnapshotFreshnessTests(unittest.TestCase):
    NOW = 2_000_000_000

    def setUp(self) -> None:
        for name in ("_paths", "fetch_rate_limits", "refresh_usage_snapshot"):
            patcher = mock.patch.object(ae, name, side_effect=AssertionError("External state forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)
        clock = mock.patch.object(ae.time, "time", return_value=self.NOW)
        clock.start()
        self.addCleanup(clock.stop)

    @staticmethod
    def snapshot(**timestamps: object) -> dict:
        return {**timestamps, "rateLimits": {
            "limitId": "synthetic", "primary": {"usedPercent": 25},
        }}

    def test_supplied_epoch_zero_is_old_not_now(self) -> None:
        for field in ("captured_at", "updated_at"):
            for value in (0, 0.0, "0", "1970-01-01T00:00:00Z"):
                with self.subTest(field=field, value=value):
                    windows, metadata = ae._window_snapshot(self.snapshot(**{field: value}))
                    self.assertEqual(windows, [])
                    self.assertFalse(metadata["fresh"])
                    self.assertEqual(metadata["age_seconds"], self.NOW)
                    self.assertEqual(metadata["captured_at"], "1970-01-01T00:00:00+00:00")

    def test_invalid_supplied_timestamp_is_unavailable_without_throwing(self) -> None:
        values = (None, "not-a-time", "", True, False, [], {},
                  float("nan"), float("inf"), float("-inf"), "NaN", "Infinity",
                  10 ** 400, -10 ** 400, 1e100, -1e100)
        for field in ("captured_at", "updated_at"):
            for value in values:
                with self.subTest(field=field, value=value):
                    windows, metadata = ae._window_snapshot(self.snapshot(**{field: value}))
                    self.assertEqual(windows, [])
                    self.assertFalse(metadata["fresh"])
                    self.assertIsNone(metadata["age_seconds"])
                    self.assertIsNone(metadata["captured_at"])

    def test_omitted_timestamp_preserves_dict_and_list_compatibility(self) -> None:
        for value in (self.snapshot(), [{"limit_id": "synthetic", "used_percent": 25}]):
            with self.subTest(value=value):
                before = copy.deepcopy(value)
                windows, metadata = ae._window_snapshot(value)
                self.assertEqual(len(windows), 1)
                self.assertTrue(metadata["fresh"])
                self.assertEqual(metadata["age_seconds"], 0)
                self.assertEqual(ae._epoch(metadata["captured_at"]), self.NOW)
                self.assertEqual(value, before)

    def test_valid_timestamp_formats_and_staleness_boundary(self) -> None:
        for field in ("captured_at", "updated_at"):
            for value in (self.NOW, str(self.NOW), self.NOW * 1000,
                          "2033-05-18T03:33:20Z"):
                with self.subTest(field=field, value=value):
                    windows, metadata = ae._window_snapshot(self.snapshot(**{field: value}))
                    self.assertEqual(len(windows), 1)
                    self.assertTrue(metadata["fresh"])
                    self.assertEqual(metadata["age_seconds"], 0)
            for age in (300, 301):
                with self.subTest(field=field, age=age):
                    windows, metadata = ae._window_snapshot(self.snapshot(**{field: self.NOW - age}))
                    self.assertEqual(bool(windows), age == 300)
                    self.assertEqual(metadata["fresh"], age == 300)
                    self.assertEqual(metadata["age_seconds"], age)

    def test_capture_field_precedence_cannot_hide_invalid_or_stale_value(self) -> None:
        for value in (None, "bad", 0):
            with self.subTest(value=value):
                windows, metadata = ae._window_snapshot(self.snapshot(
                    captured_at=value, updated_at=self.NOW))
                self.assertEqual(windows, [])
                self.assertFalse(metadata["fresh"])
        windows, metadata = ae._window_snapshot(self.snapshot(
            captured_at=self.NOW, updated_at="ignored alias"))
        self.assertEqual(len(windows), 1)
        self.assertTrue(metadata["fresh"])

    def test_invalid_timestamp_capacity_is_uncalibrated(self) -> None:
        for value in (0, None, "bad", float("nan")):
            with self.subTest(value=value):
                result = ae.estimate_capacity("balanced", self.snapshot(captured_at=value))
                self.assertFalse(result["available"])
                self.assertEqual(result["status"], "UNCALIBRATED")


if __name__ == "__main__":
    unittest.main()
