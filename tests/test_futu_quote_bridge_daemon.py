from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from futu_quote_bridge_daemon import (  # noqa: E402
    available_subscription_quota,
    enrich_quote,
    market_session,
    priority_codes,
)


BEIJING = ZoneInfo("Asia/Shanghai")


class FutuQuoteBridgeDaemonTests(unittest.TestCase):
    def test_us_sessions_follow_new_york_clock(self) -> None:
        self.assertEqual(
            market_session("US.CIEN", datetime(2026, 9, 9, 12, 30, tzinfo=BEIJING)),
            "overnight",
        )
        self.assertEqual(
            market_session("US.CIEN", datetime(2026, 9, 9, 22, 0, tzinfo=BEIJING)),
            "regular",
        )

    def test_extended_price_never_invents_exchange_time(self) -> None:
        row = {
            "code": "US.CIEN",
            "data_date": "2026-09-08",
            "data_time": "16:00:00.459",
            "last_price": 341.29,
            "overnight_price": 342.05,
        }
        received = datetime(2026, 9, 9, 12, 33, tzinfo=BEIJING)
        first = enrich_quote(
            row,
            received_at=received,
            previous=None,
            from_push=False,
        )
        self.assertEqual(first["live_price"], 342.05)
        self.assertEqual(first["timestamp_kind"], "transport_receipt")
        self.assertFalse(first["push_confirmed"])
        self.assertNotIn("live_quote_time", first)
        self.assertIn("exchange_quote_time", first)

        cached_push = enrich_quote(
            row,
            received_at=received,
            previous=first,
            from_push=True,
        )
        self.assertFalse(cached_push["push_confirmed"])

        changed = enrich_quote(
            {**row, "overnight_price": 342.06},
            received_at=received,
            previous=cached_push,
            from_push=True,
        )
        self.assertTrue(changed["push_confirmed"])
        repeated = enrich_quote(
            {**row, "overnight_price": 342.06},
            received_at=received,
            previous=changed,
            from_push=True,
        )
        self.assertTrue(repeated["push_confirmed"])

    def test_opportunities_are_prioritized_before_universe_at_quota(self) -> None:
        def configured(scope: str) -> list[str]:
            return ["NVDA"] if scope == "core" else ["NVDA", "MSFT", "GOOG"]

        with patch(
            "futu_quote_bridge_daemon.public_opportunity_symbols",
            return_value=["US.CIEN", "HK.00981"],
        ), patch(
            "futu_quote_bridge_daemon.configured_symbols",
            side_effect=configured,
        ):
            selected, skipped = priority_codes("all", 3)
        self.assertEqual(selected, ["US.CIEN", "HK.00981", "US.NVDA"])
        self.assertEqual(skipped, ["US.MSFT", "US.GOOG"])

    def test_subscription_quota_uses_opend_remaining_capacity(self) -> None:
        context = Mock()
        context.query_subscription.return_value = (
            0,
            {"total_used": 80, "remain": 20, "own_used": 0},
        )
        self.assertEqual(available_subscription_quota(context, 0), 20)


if __name__ == "__main__":
    unittest.main()
