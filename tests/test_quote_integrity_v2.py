from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from export_public_reports import finalize_opportunities, merge_opportunity, opportunity_status
from futu_cloud_bridge import public_snapshot
from model_v2 import assess_price_freshness, source_quality
from sync_futu_local_snapshot import canonical_quote_time


class QuoteIntegrityV2Tests(unittest.TestCase):
    def test_us_exchange_time_is_converted_to_beijing(self) -> None:
        self.assertEqual(
            canonical_quote_time("US.MU", {"update_time": "2026-08-24 21:21:04.235"}),
            "2026-08-25T09:21:04+08:00",
        )

    def test_hong_kong_exchange_time_remains_beijing(self) -> None:
        self.assertEqual(
            canonical_quote_time("HK.00981", {"update_time": "2026-08-25 09:20:00"}),
            "2026-08-25T09:20:00+08:00",
        )

    def test_unavailable_exchange_time_is_never_fabricated(self) -> None:
        self.assertEqual(canonical_quote_time("SH.688981", {}), "")

    def test_public_quote_needing_futu_verification_is_not_futu(self) -> None:
        self.assertEqual(
            source_quality("公开日线行情已接入；需 Futu/券商复核"),
            source_quality("fallback"),
        )

    def test_authenticated_broker_quote_can_be_executable(self) -> None:
        now = datetime.now(timezone.utc)
        result = assess_price_freshness(now.isoformat(), "Futu OpenD authenticated bridge", now)
        self.assertEqual(result["price_freshness"], "fresh")
        self.assertTrue(result["execution_allowed"])

    def test_local_snapshot_fallback_is_not_executable(self) -> None:
        now = datetime.now(timezone.utc)
        result = assess_price_freshness(now.isoformat(), "Futu OpenD local snapshot fallback", now)
        self.assertEqual(result["price_freshness"], "fallback_only")
        self.assertFalse(result["execution_allowed"])

    def test_future_exchange_quote_remains_blocked(self) -> None:
        now = datetime.now(timezone.utc)
        future = (now + timedelta(minutes=15)).isoformat()
        result = assess_price_freshness(future, "Futu OpenD authenticated bridge", now)
        self.assertEqual(result["price_freshness"], "stale")
        self.assertFalse(result["execution_allowed"])

    def test_retired_queue_cannot_become_secondary_analysis(self) -> None:
        self.assertEqual(
            opportunity_status("进入二次分析候选池", None, "retreated"),
            "watchlist",
        )

    def test_fresh_quote_replaces_stale_higher_priority_quote_atomically(self) -> None:
        now = datetime.now(timezone.utc)
        stale_time = (now - timedelta(days=40)).isoformat()
        fresh_time = (now - timedelta(minutes=5)).isoformat()
        merged = merge_opportunity(
            merge_opportunity(None, {
                "symbol": "US.MU",
                "price": 100,
                "price_source": "Futu OpenD",
                "price_time": stale_time,
            }),
            {
                "symbol": "US.MU",
                "price": 110,
                "price_source": "Yahoo public market quote",
                "price_time": fresh_time,
            },
        )
        self.assertEqual(merged["price"], 110)
        self.assertEqual(merged["price_source"], "Yahoo public market quote")
        self.assertEqual(merged["price_time"], fresh_time)

    def test_report_time_cannot_be_used_as_quote_time(self) -> None:
        rows = finalize_opportunities({
            "US.MU": {
                "symbol": "US.MU",
                "price": 110,
                "price_source": "Yahoo",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        })
        self.assertEqual(rows[0]["price_time"], "")
        self.assertEqual(rows[0]["price_freshness"], "unknown")
        self.assertFalse(rows[0]["execution_allowed"])

    def test_cloud_bridge_strips_private_fields(self) -> None:
        clean = public_snapshot({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "opend": {"connected": True, "host": "127.0.0.1", "port": 11111},
            "account": "private",
            "quotes": {
                "US.MU": {
                    "code": "US.MU",
                    "last_price": 910.43,
                    "quote_time": datetime.now(timezone.utc).isoformat(),
                    "shares": 99,
                    "cost_basis": 800,
                },
            },
        })
        self.assertNotIn("account", clean)
        self.assertNotIn("host", clean["opend"])
        self.assertNotIn("shares", clean["quotes"]["US.MU"])
        self.assertNotIn("cost_basis", clean["quotes"]["US.MU"])


if __name__ == "__main__":
    unittest.main()
