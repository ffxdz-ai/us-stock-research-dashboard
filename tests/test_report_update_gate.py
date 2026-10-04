from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from report_update_gate import evaluate_gate  # noqa: E402


def snapshot(*, generated_at: str, connected: bool = True, authenticated: bool = True):
    return {
        "generated_at": generated_at,
        "opend": {"connected": connected},
        "bridge": {"authenticated": authenticated},
        "quotes": {
            "US.SPY": {
                "code": "US.SPY",
                "last_price": 700.0,
                "quote_time": generated_at,
            }
        },
    }


class ReportUpdateGateTests(unittest.TestCase):
    def setUp(self):
        # Monday 08:00 Beijing: the Futu-style weekly trading window opens.
        self.now = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        self.index = {
            "reports": [
                {
                    "kind": "deepseek-cloud",
                    "published_label": "2026-10-02 08:10",
                }
            ]
        }
        self.live_snapshot = snapshot(generated_at="2026-10-04T23:58:30+00:00")

    def test_monday_0800_opens_trading_week(self):
        decision = evaluate_gate(
            now=self.now,
            index=self.index,
            state={},
            snapshot=self.live_snapshot,
        )
        self.assertTrue(decision.should_update)
        self.assertEqual(decision.report_slot, "2026-10-05")

    def test_monday_before_0800_is_closed(self):
        decision = evaluate_gate(
            now=datetime(2026, 10, 4, 23, 59, tzinfo=timezone.utc),
            index=self.index,
            state={},
            snapshot=snapshot(generated_at="2026-10-04T23:58:00+00:00"),
        )
        self.assertFalse(decision.should_update)
        self.assertEqual(decision.code, "outside_trading_week")

    def test_sunday_is_outside_trading_week(self):
        decision = evaluate_gate(
            now=datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc),
            index=self.index,
            state={},
            snapshot=snapshot(generated_at="2026-10-04T08:59:00+00:00"),
        )
        self.assertFalse(decision.should_update)
        self.assertEqual(decision.code, "outside_trading_week")

    def test_saturday_0800_is_friday_postmarket_boundary(self):
        decision = evaluate_gate(
            now=datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc),
            index={"reports": []},
            state={},
            snapshot=snapshot(generated_at="2026-10-09T23:59:00+00:00"),
        )
        self.assertTrue(decision.should_update)
        self.assertEqual(decision.report_slot, "2026-10-09")

    def test_saturday_after_0800_is_closed(self):
        decision = evaluate_gate(
            now=datetime(2026, 10, 10, 0, 1, tzinfo=timezone.utc),
            index={"reports": []},
            state={},
            snapshot=snapshot(generated_at="2026-10-10T00:00:30+00:00"),
        )
        self.assertFalse(decision.should_update)
        self.assertEqual(decision.code, "outside_trading_week")

    def test_delayed_scheduled_run_uses_logical_0800_but_actual_futu_age(self):
        decision = evaluate_gate(
            now=datetime(2026, 10, 10, 0, 10, tzinfo=timezone.utc),
            window_time=datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc),
            index={"reports": []},
            state={},
            snapshot=snapshot(generated_at="2026-10-10T00:09:00+00:00"),
        )
        self.assertTrue(decision.should_update)
        self.assertEqual(decision.report_slot, "2026-10-09")

    def test_us_exchange_holiday_is_skipped(self):
        # Friday 2026-07-03 observes Independence Day and is a NYSE holiday.
        decision = evaluate_gate(
            now=datetime(2026, 7, 3, 0, 0, tzinfo=timezone.utc),
            index={"reports": []},
            state={},
            snapshot=snapshot(generated_at="2026-07-02T23:59:00+00:00"),
        )
        self.assertFalse(decision.should_update)
        self.assertEqual(decision.code, "us_exchange_closed")

    def test_disconnected_futu_blocks_report(self):
        decision = evaluate_gate(
            now=self.now,
            index=self.index,
            state={},
            snapshot=snapshot(
                generated_at="2026-10-04T23:59:00+00:00",
                connected=False,
            ),
        )
        self.assertFalse(decision.should_update)
        self.assertEqual(decision.code, "futu_disconnected")

    def test_stale_futu_heartbeat_blocks_report(self):
        decision = evaluate_gate(
            now=self.now,
            index=self.index,
            state={},
            snapshot=snapshot(generated_at="2026-10-04T23:40:00+00:00"),
        )
        self.assertFalse(decision.should_update)
        self.assertEqual(decision.code, "futu_snapshot_stale")

    def test_existing_slot_is_skipped_but_force_can_rerun(self):
        state = {"last_report_slot": "2026-10-05"}
        normal = evaluate_gate(
            now=self.now,
            index=self.index,
            state=state,
            snapshot=self.live_snapshot,
        )
        forced = evaluate_gate(
            now=self.now,
            index=self.index,
            state=state,
            snapshot=self.live_snapshot,
            force=True,
        )
        self.assertFalse(normal.should_update)
        self.assertEqual(normal.code, "report_slot_already_updated")
        self.assertTrue(forced.should_update)


if __name__ == "__main__":
    unittest.main()
