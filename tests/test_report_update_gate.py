from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from report_update_gate import evaluate_gate  # noqa: E402


def snapshot(*, generated_at: str, data_date: str, connected: bool = True, authenticated: bool = True):
    return {
        "generated_at": generated_at,
        "opend": {"connected": connected},
        "bridge": {"authenticated": authenticated},
        "quotes": {
            "US.SPY": {
                "code": "US.SPY",
                "last_price": 700.0,
                "quote_time": f"{data_date}T16:00:00-04:00",
                "data_date": data_date,
            }
        },
    }


class ReportUpdateGateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
        self.index = {
            "reports": [
                {
                    "kind": "deepseek-cloud",
                    "published_label": "2026-10-03 12:21",
                }
            ]
        }
        self.live_snapshot = snapshot(
            generated_at="2026-10-05T23:58:30+00:00",
            data_date="2026-10-05",
        )

    def test_new_completed_session_with_live_futu_is_eligible(self):
        decision = evaluate_gate(
            now=self.now,
            index=self.index,
            state={},
            snapshot=self.live_snapshot,
        )
        self.assertTrue(decision.should_update)
        self.assertEqual(decision.session_date, "2026-10-05")
        self.assertEqual(decision.previous_session_date, "2026-10-02")

    def test_weekend_is_outside_post_session_window(self):
        decision = evaluate_gate(
            now=datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc),
            index=self.index,
            state={},
            snapshot=snapshot(
                generated_at="2026-10-04T08:59:00+00:00",
                data_date="2026-10-02",
            ),
        )
        self.assertFalse(decision.should_update)
        self.assertEqual(decision.code, "outside_post_session_window")

    def test_exchange_holiday_is_not_treated_as_a_new_session(self):
        decision = evaluate_gate(
            now=datetime(2026, 7, 4, 0, 0, tzinfo=timezone.utc),
            index={"reports": []},
            state={},
            snapshot=snapshot(
                generated_at="2026-07-03T23:59:00+00:00",
                data_date="2026-07-02",
            ),
        )
        self.assertFalse(decision.should_update)
        self.assertEqual(decision.code, "outside_post_session_window")

    def test_disconnected_futu_blocks_report(self):
        decision = evaluate_gate(
            now=self.now,
            index=self.index,
            state={},
            snapshot=snapshot(
                generated_at="2026-10-05T23:59:00+00:00",
                data_date="2026-10-05",
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
            snapshot=snapshot(
                generated_at="2026-10-05T23:40:00+00:00",
                data_date="2026-10-05",
            ),
        )
        self.assertFalse(decision.should_update)
        self.assertEqual(decision.code, "futu_snapshot_stale")

    def test_wrong_exchange_session_blocks_report(self):
        decision = evaluate_gate(
            now=self.now,
            index=self.index,
            state={},
            snapshot=snapshot(
                generated_at="2026-10-05T23:59:00+00:00",
                data_date="2026-10-02",
            ),
        )
        self.assertFalse(decision.should_update)
        self.assertEqual(decision.code, "futu_session_not_covered")

    def test_existing_session_is_skipped_but_force_can_rerun(self):
        state = {"last_completed_session": "2026-10-05"}
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
        self.assertEqual(normal.code, "session_already_reported")
        self.assertTrue(forced.should_update)


if __name__ == "__main__":
    unittest.main()
