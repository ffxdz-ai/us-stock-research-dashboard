from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from macro_regime import summarize_series
from policy_event_radar import build_radar, load_json, market_reaction, observations_from_macro, policy_surprise


CONFIG = Path(__file__).resolve().parents[1] / "config" / "policy_events.json"


def sample_observations() -> dict[str, list[dict[str, object]]]:
    return {
        "SP500": [{"date": "2026-09-15", "value": 100}, {"date": "2026-09-16", "value": 99}, {"date": "2026-09-17", "value": 101}, {"date": "2026-09-18", "value": 102}],
        "NASDAQCOM": [{"date": "2026-09-15", "value": 100}, {"date": "2026-09-18", "value": 103}],
        "DJIA": [{"date": "2026-09-15", "value": 100}, {"date": "2026-09-18", "value": 98}],
        "DGS2": [{"date": "2026-09-15", "value": 4.0}, {"date": "2026-09-18", "value": 4.1}],
        "DGS10": [{"date": "2026-09-15", "value": 4.5}, {"date": "2026-09-18", "value": 4.55}],
        "DCOILWTICO": [{"date": "2026-09-15", "value": 100}, {"date": "2026-09-18", "value": 106}],
    }


class PolicyEventRadarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_json(CONFIG, {})
        self.event = self.config["events"][0]

    def test_expected_hike_not_mistaken_for_dovish_surprise(self) -> None:
        result = policy_surprise(self.event)
        self.assertEqual(result["status"], "as_expected")
        self.assertEqual(result["surprise_bps"], 0)

    def test_expectation_recorded_after_decision_is_rejected(self) -> None:
        event = {**self.event, "expectation_as_of": "2026-09-16T15:00:00-04:00"}
        self.assertEqual(policy_surprise(event)["status"], "unknown")

    def test_mixed_index_confirmation_does_not_become_broad_rally(self) -> None:
        from datetime import date

        result = market_reaction(date(2026, 9, 16), sample_observations())
        self.assertEqual(result["status"], "mixed")
        self.assertEqual(result["window"], {"start": "2026-09-15", "end": "2026-09-18"})
        self.assertEqual(result["cross_assets"]["DGS2_change_bps"], 10.0)

    def test_missing_market_data_stays_unknown(self) -> None:
        payload = build_radar(self.config, {}, now=datetime(2026, 9, 21, tzinfo=timezone.utc))
        self.assertEqual(payload["market_reaction"]["status"], "unknown")
        self.assertEqual(payload["status"], "limited")
        self.assertTrue(payload["data_gaps"])

    def test_policy_audit_reuses_daily_macro_observations(self) -> None:
        source_rows = sample_observations()
        macro = {"indicators": {key: {"recent_observations": rows, "status": "ok"} for key, rows in source_rows.items()}}
        observations, errors = observations_from_macro(macro)
        self.assertFalse(errors)
        payload = build_radar(self.config, observations, now=datetime(2026, 9, 21, tzinfo=timezone.utc))
        self.assertEqual(payload["market_reaction"]["status"], "mixed")
        self.assertEqual(payload["status"], "ready")

    def test_macro_daily_series_retains_recent_rows(self) -> None:
        rows = [{"date": f"2026-09-{day:02d}", "value": float(day)} for day in range(1, 27)]
        summary = summarize_series({"id": "SP500", "frequency_hint": "daily"}, rows)
        self.assertEqual(len(summary["recent_observations"]), 24)
        self.assertEqual(summary["recent_observations"][-1]["date"], "2026-09-26")

    def test_old_event_is_historical_not_current_buy_filter(self) -> None:
        payload = build_radar(self.config, sample_observations(), now=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(payload["current_relevance"], "historical")
        self.assertIn("不作为今日买入过滤器", payload["summary"])


if __name__ == "__main__":
    unittest.main()
