from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from model_v2 import build_entry_path, calculate_rr, evaluate_risk_gate, load_risk_policy


def base_candidate() -> dict:
    return {
        "opportunity_score": 82,
        "trend_score": 78,
        "crowding_score": 25,
        "data_confidence": 0.90,
        "price_freshness": "fresh",
        "execution_allowed": True,
        "technical_data_complete": True,
        "future_function_audit": "PASS",
        "valid_path": True,
    }


class RiskGateV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_risk_policy()

    def test_rr_uses_planned_entry(self) -> None:
        self.assertEqual(calculate_rr(100, 90, 130), 3.0)
        self.assertNotEqual(calculate_rr(105, 90, 130), 3.0)

    def test_formal_rr_gate(self) -> None:
        path = build_entry_path("formal", 100, 90, 129, self.policy.formal_min_rr)
        self.assertFalse(evaluate_risk_gate(base_candidate(), path).qualified)

    def test_starter_rr_gate(self) -> None:
        path = build_entry_path("starter", 100, 90, 124, self.policy.starter_min_rr)
        self.assertFalse(evaluate_risk_gate(base_candidate(), path, path_type="starter").qualified)

    def test_breakout_rr_gate(self) -> None:
        path = build_entry_path("breakout", 100, 90, 130, self.policy.breakout_min_rr)
        self.assertTrue(evaluate_risk_gate(base_candidate(), path, path_type="breakout").qualified)

    def test_low_confidence_blocks_formal(self) -> None:
        candidate = base_candidate()
        candidate["data_confidence"] = 0.67
        path = build_entry_path("formal", 100, 90, 130, 3.0)
        self.assertIn("data_confidence_below_threshold", evaluate_risk_gate(candidate, path).gate_failures)

    def test_stale_quote_blocks_execution(self) -> None:
        candidate = base_candidate()
        candidate["price_freshness"] = "stale"
        candidate["execution_allowed"] = False
        path = build_entry_path("formal", 100, 90, 130, 3.0)
        self.assertFalse(evaluate_risk_gate(candidate, path).qualified)

    def test_fallback_quote_blocks_execution(self) -> None:
        candidate = base_candidate()
        candidate["price_freshness"] = "fallback_only"
        candidate["execution_allowed"] = False
        path = build_entry_path("formal", 100, 90, 130, 3.0)
        self.assertFalse(evaluate_risk_gate(candidate, path).qualified)


if __name__ == "__main__":
    unittest.main()
