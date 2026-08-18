from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from model_v2 import (
    evaluate_split,
    factor_snapshot,
    future_function_audit,
    select_best_field_value,
    source_quality,
    validate_llm_commentary,
)


def pit_candidate(now: datetime) -> dict:
    return {
        "quote_time": now.isoformat(),
        "chart_time": (now - timedelta(hours=1)).isoformat(),
        "reference_bar_end_time": (now - timedelta(hours=1)).isoformat(),
        "technical_data_complete": True,
        "breakout_reference_excludes_current_bar": True,
        "sec": {"recent_filings": []},
    }


class PitMergeLlmV2Tests(unittest.TestCase):
    def test_future_filing_rejected(self) -> None:
        now = datetime(2026, 8, 18, tzinfo=timezone.utc)
        candidate = pit_candidate(now)
        candidate["sec"] = {"recent_filings": [{"filed": "2026-08-19"}]}
        self.assertIn("future_filing", future_function_audit(candidate, now).get("failures"))

    def test_future_chart_rejected(self) -> None:
        now = datetime(2026, 8, 18, tzinfo=timezone.utc)
        candidate = pit_candidate(now)
        candidate["chart_time"] = (now + timedelta(days=1)).isoformat()
        candidate["reference_bar_end_time"] = candidate["chart_time"]
        self.assertIn("future_chart", future_function_audit(candidate, now).get("failures"))

    def test_signal_timestamp_alignment(self) -> None:
        now = datetime(2026, 8, 18, tzinfo=timezone.utc)
        self.assertEqual(future_function_audit(pit_candidate(now), now)["status"], "PASS")

    def test_fresher_source_wins(self) -> None:
        selected = select_best_field_value([
            {"value": 100, "source": "Yahoo", "source_time": "2026-08-17", "confidence": 0.75},
            {"value": 101, "source": "Yahoo", "source_time": "2026-08-18", "confidence": 0.75},
        ])
        self.assertEqual(selected["value"], 101)

    def test_higher_quality_source_wins(self) -> None:
        selected = select_best_field_value([
            {"value": 100, "source": "Futu OpenD", "source_time": "2026-08-18", "confidence": 0.95},
            {"value": 101, "source": "Yahoo", "source_time": "2026-08-18", "confidence": 0.75},
        ])
        self.assertEqual(selected["value"], 100)

    def test_source_priority_precedes_freshness(self) -> None:
        selected = select_best_field_value([
            {"value": 100, "source": "Futu OpenD", "source_time": "2026-08-18", "confidence": 0.95},
            {"value": 101, "source": "Yahoo", "source_time": "2026-08-17", "confidence": 0.75},
        ])
        self.assertEqual(selected["value"], 100)

    def test_fallback_label_cannot_inherit_provider_quality(self) -> None:
        self.assertEqual(source_quality("Futu OpenD local snapshot fallback"), source_quality("fallback"))

    def test_missing_confidence_cannot_create_opportunity_score(self) -> None:
        snapshot = factor_snapshot({"forward_pe": 20, "technical_score_v2": 80})
        self.assertEqual(snapshot["confidence_multiplier"], 0.0)
        self.assertEqual(snapshot["opportunity_score"], 0.0)
        self.assertIn("data_quality", snapshot["missing_factors"])

    def test_walk_forward_split_is_explicit(self) -> None:
        self.assertEqual(evaluate_split("2023-12-29"), "train")
        self.assertEqual(evaluate_split("2024-08-01"), "validation")
        self.assertEqual(evaluate_split("2026-08-18"), "oos")

    def test_llm_cannot_override_trade_levels(self) -> None:
        payload = {"market_view": {}, "stock_commentary": [{"symbol": "NVDA", "thesis": "x", "entry": 100}]}
        with self.assertRaises(ValueError):
            validate_llm_commentary(payload, ["NVDA"])

    def test_llm_unknown_symbol_rejected(self) -> None:
        payload = {"market_view": {}, "stock_commentary": [{"symbol": "FAKE", "thesis": "x"}]}
        with self.assertRaises(ValueError):
            validate_llm_commentary(payload, ["NVDA"])

    def test_llm_numeric_trade_instruction_rejected(self) -> None:
        payload = {
            "market_view": {},
            "stock_commentary": [{"symbol": "NVDA", "thesis": "建议买入 100 美元"}],
        }
        with self.assertRaises(ValueError):
            validate_llm_commentary(payload, ["NVDA"])


if __name__ == "__main__":
    unittest.main()
