from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from model_v2 import assess_price_freshness, build_entry_path, evaluate_plan_execution


class CrossLanguageExecutionContractTests(unittest.TestCase):
    def test_t19_python_matches_shared_fixture(self):
        fixture = json.loads((ROOT / "tests" / "fixtures" / "execution_contract.json").read_text(encoding="utf-8"))
        base = fixture["candidate"]
        path = build_entry_path("formal", base["entry_price"], base["stop_loss"], base["target_price"], base["rr_required"])
        for case in fixture["cases"]:
            with self.subTest(case=case["name"]):
                freshness = assess_price_freshness(
                    case["quote_time"], "Futu OpenD authenticated bridge", case["now"]
                )
                candidate = {
                    **base,
                    **freshness,
                    "portfolio_permission": "approved",
                }
                result = evaluate_plan_execution(
                    candidate, path, current_price=case["price"], portfolio_permission=True
                )
                self.assertEqual(result["execution_allowed"], case["expected_execution"])


if __name__ == "__main__":
    unittest.main()
