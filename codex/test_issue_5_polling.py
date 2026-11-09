"""Deterministic polling integration; no real account, clock waits, or rollouts."""
from __future__ import annotations

import datetime as dt
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
import mcp_server as mcp


class SimulatedStop:
    def __init__(self, cycles: int) -> None:
        self.cycles = cycles
        self.elapsed = 0
        self.delays: list[int] = []

    def is_set(self) -> bool:
        return len(self.delays) >= self.cycles

    def wait(self, delay: int) -> bool:
        self.delays.append(delay)
        self.elapsed += delay
        return self.is_set()

    def stamp(self) -> str:
        return (dt.datetime(2026, 9, 16, tzinfo=dt.timezone.utc)
                + dt.timedelta(seconds=self.elapsed)).isoformat()


class PollingIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "state"
        self.environment = mock.patch.dict(os.environ, {
            "AGENT_EFFICIENCY_DATA": str(self.state),
            # Exercise persistence, but replace the fetch boundary in every test.
            "AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH": "0",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    @staticmethod
    def snapshot(percent: int = 20) -> dict:
        return {"primary": {"limitId": "synthetic-window", "usedPercent": percent,
                            "windowDurationMins": 300}}

    def usage(self) -> dict:
        return json.loads((self.state / "usage.json").read_text(encoding="utf-8"))

    def test_healthy_loop_refreshes_across_more_than_five_simulated_minutes(self) -> None:
        stop = SimulatedStop(3)
        observed: list[int] = []

        def fetch(**kwargs):
            observed.append(stop.elapsed)
            return self.snapshot(20 + len(observed))

        with (mock.patch.object(ae, "fetch_rate_limits", side_effect=fetch),
              mock.patch.object(ae, "_now", side_effect=stop.stamp),
              mock.patch.object(mcp, "scan_rollouts") as scan):
            mcp._telemetry_loop(stop)

        self.assertEqual(observed, [0, 240, 480])
        self.assertEqual(stop.delays, [240, 240, 240])
        self.assertEqual(scan.call_count, 3)
        usage = self.usage()
        self.assertEqual(usage["rate_limits"][0]["used_percent"], 23)
        self.assertEqual(usage["rate_limit_poll"]["status"], "ok")
        self.assertEqual(usage["rate_limits_updated_at"], "2026-09-16T00:08:00+00:00")
        self.assertEqual(usage["rate_limit_poll"]["last_success_at"], usage["rate_limits_updated_at"])

    def test_retries_are_bounded_and_recovery_restores_normal_cadence(self) -> None:
        stop = SimulatedStop(8)
        results = [TimeoutError("PRIVATE failure") for _ in range(6)]
        results += [self.snapshot(), self.snapshot(21)]
        with (mock.patch.object(ae, "fetch_rate_limits", side_effect=results) as fetch,
              mock.patch.object(ae, "_now", side_effect=stop.stamp),
              mock.patch.object(mcp, "scan_rollouts") as scan):
            mcp._telemetry_loop(stop)

        self.assertEqual(stop.delays, [15, 30, 60, 120, 120, 120, 240, 240])
        self.assertEqual(fetch.call_count, 8)
        self.assertEqual(scan.call_count, 8)
        poll = self.usage()["rate_limit_poll"]
        self.assertEqual(poll["status"], "ok")
        self.assertIsNone(poll["last_error"])
        self.assertNotIn("PRIVATE", (self.state / "usage.json").read_text(encoding="utf-8"))

    def test_failed_refresh_preserves_previous_capture_time_and_sanitizes_error(self) -> None:
        stop = SimulatedStop(2)
        with (mock.patch.object(ae, "fetch_rate_limits", side_effect=[self.snapshot(), PermissionError("PRIVATE path")]),
              mock.patch.object(ae, "_now", side_effect=stop.stamp),
              mock.patch.object(mcp, "scan_rollouts")):
            mcp._telemetry_loop(stop)

        usage = self.usage()
        self.assertEqual(usage["rate_limits_updated_at"], "2026-09-16T00:00:00+00:00")
        self.assertEqual(usage["rate_limit_poll"]["last_attempt_at"], "2026-09-16T00:04:00+00:00")
        self.assertEqual(usage["rate_limit_poll"]["last_success_at"], usage["rate_limits_updated_at"])
        self.assertEqual(usage["rate_limit_poll"]["last_error"]["code"], "permission-denied")
        self.assertEqual(stop.delays, [240, 15])
        self.assertNotIn("PRIVATE", json.dumps(usage))

    def test_ingestion_failure_is_degraded_and_next_success_clears_it(self) -> None:
        with mock.patch.object(ae, "fetch_rate_limits", return_value=self.snapshot()):
            ae.refresh_usage_snapshot(force=True)
        original_stamp = self.usage()["rate_limits_updated_at"]
        # A dict can pass the transport check yet contain an invalid map.
        malformed = {"rateLimitsByLimitId": ["PRIVATE malformed payload"]}
        with mock.patch.object(ae, "fetch_rate_limits", return_value=malformed):
            degraded = ae.refresh_usage_snapshot(force=True)
        self.assertEqual(degraded["rate_limit_poll"]["status"], "degraded")
        self.assertEqual(degraded["rate_limit_poll"]["last_error"]["code"], "snapshot-ingestion-failed")
        self.assertEqual(degraded["rate_limits_updated_at"], original_stamp)
        self.assertNotIn("PRIVATE", json.dumps(degraded))
        with mock.patch.object(ae, "fetch_rate_limits", return_value=self.snapshot(22)):
            recovered = ae.refresh_usage_snapshot(force=True)
        self.assertEqual(recovered["rate_limit_poll"]["status"], "ok")
        self.assertIsNone(recovered["rate_limit_poll"]["last_error"])

    def test_ingestion_failure_with_raise_enabled_still_records_degraded_status(self) -> None:
        with (mock.patch.object(ae, "fetch_rate_limits", return_value=self.snapshot()),
              mock.patch.object(ae, "ingest_usage", side_effect=ValueError("PRIVATE invalid snapshot"))):
            with self.assertRaises(ValueError):
                ae.refresh_usage_snapshot(force=True, raise_on_error=True)
        self.assertEqual(self.usage()["rate_limit_poll"]["status"], "degraded")
        self.assertNotIn("PRIVATE", json.dumps(self.usage()))

    def test_scanner_failure_does_not_interrupt_usage_refresh(self) -> None:
        stop = SimulatedStop(2)
        with (mock.patch.object(ae, "fetch_rate_limits", return_value=self.snapshot()) as fetch,
              mock.patch.object(ae, "_now", side_effect=stop.stamp),
              mock.patch.object(mcp, "scan_rollouts", side_effect=OSError("PRIVATE archive"))):
            mcp._telemetry_loop(stop)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(stop.delays, [240, 240])
        self.assertEqual(self.usage()["rate_limit_poll"]["status"], "ok")

    def test_local_ingestion_errors_are_not_misreported_as_process_failures(self) -> None:
        for error_type in (PermissionError, FileNotFoundError, OSError):
            with self.subTest(error=error_type.__name__):
                with (mock.patch.object(ae, "fetch_rate_limits", return_value=self.snapshot()),
                      mock.patch.object(ae, "ingest_usage", side_effect=error_type("PRIVATE local path"))):
                    result = ae.refresh_usage_snapshot(force=True)
                poll = result["rate_limit_poll"]
                self.assertEqual(poll["status"], "degraded")
                self.assertEqual(poll["last_error"]["code"], "snapshot-ingestion-failed")
                self.assertIn("saved locally", poll["last_error"]["message"])
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_stop_before_start_does_not_fetch_or_scan(self) -> None:
        with (mock.patch.object(ae, "fetch_rate_limits") as fetch,
              mock.patch.object(mcp, "scan_rollouts") as scan):
            mcp._telemetry_loop(SimulatedStop(0))
        fetch.assert_not_called()
        scan.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_disabled_live_refresh_never_fetches_during_polling(self) -> None:
        with (mock.patch.dict(os.environ, {"AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH": "1"}),
              mock.patch.object(ae, "fetch_rate_limits") as fetch,
              mock.patch.object(mcp, "scan_rollouts") as scan):
            mcp._telemetry_loop(SimulatedStop(2))
        fetch.assert_not_called()
        self.assertEqual(scan.call_count, 2)


if __name__ == "__main__":
    unittest.main()
