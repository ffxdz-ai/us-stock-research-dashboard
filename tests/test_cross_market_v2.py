from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cross_market_intelligence import theme_intelligence


class CrossMarketV2Tests(unittest.TestCase):
    def test_dynamic_theme_contract_has_no_legacy_static_variables(self) -> None:
        theme = {
            "id": "test",
            "name": "Test theme",
            "expectation_gap_score": 72,
            "score_components": {
                "base_score": 65,
                "dynamic_center": 74,
                "earnings_leverage": 70,
                "earnings_revision": 68,
                "market_underpricing": 62,
                "data_confidence": 85,
                "crowding_score": 40,
                "crowding_penalty": 0,
            },
            "securities": [],
        }
        result = theme_intelligence(theme, {}, {}, {}, {})
        self.assertIn("demand_acceleration_score", result)
        self.assertIn("acceleration_coverage", result)
        self.assertNotIn("demand_shift", result["score_components"])
        self.assertNotIn("supply_constraint", result["score_components"])

    def test_prior_only_theme_is_not_high_conviction(self) -> None:
        result = theme_intelligence(
            {"id": "prior", "name": "Prior only", "score_components": {"base_score": 95}, "securities": []},
            {},
            {},
            {},
            {},
        )
        self.assertEqual(result["status"], "证据不足")
        self.assertLess(result["demand_acceleration_score"], 60)


if __name__ == "__main__":
    unittest.main()
