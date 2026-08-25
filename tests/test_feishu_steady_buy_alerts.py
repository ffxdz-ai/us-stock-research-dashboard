import unittest

from scripts.feishu_steady_buy_alerts import (
    build_candidate_card,
    new_alerts,
    new_candidate_alerts,
    qualifies_for_candidate_alert,
    qualifies_for_steady_buy_alert,
    signal_fingerprint,
    update_state,
)


def candidate(**overrides):
    value = {
        "symbol": "US.MU",
        "name": "Micron",
        "status": "executable",
        "status_label": "稳健买点·区间内",
        "entry_tier": "formal",
        "signal_type": "formal",
        "formal_qualified": True,
        "entry_execution_status": "in_zone",
        "execution_allowed": True,
        "technical_data_complete": True,
        "future_function_audit": "PASS",
        "price_freshness": "fresh",
        "gate_failures": [],
        "price": 100.0,
        "entry_price": 100.0,
        "safe_entry_zone_low": 97.0,
        "safe_entry_zone_high": 100.0,
        "safe_entry_max_price": 101.5,
        "stop_loss": 90.0,
        "target_price": 130.0,
        "rr_ratio": 3.0,
        "rr_required": 3.0,
        "opportunity_score": 82.0,
        "trend_score": 70.0,
        "crowding_score": 45.0,
        "risk_policy_version": "2.0.0",
    }
    value.update(overrides)
    return value


class FeishuSteadyBuyAlertTests(unittest.TestCase):
    def test_only_fully_executable_formal_signal_qualifies(self):
        self.assertTrue(qualifies_for_steady_buy_alert(candidate()))
        self.assertFalse(qualifies_for_steady_buy_alert(candidate(status="waiting_entry")))
        self.assertFalse(qualifies_for_steady_buy_alert(candidate(entry_tier="trial")))
        self.assertFalse(qualifies_for_steady_buy_alert(candidate(future_function_audit="BLOCK")))
        self.assertFalse(qualifies_for_steady_buy_alert(candidate(gate_failures=["data_gap"])))
        self.assertFalse(qualifies_for_steady_buy_alert(candidate(price=102.0)))

    def test_price_noise_does_not_change_plan_fingerprint(self):
        self.assertEqual(signal_fingerprint(candidate(price=99.5)), signal_fingerprint(candidate(price=100.5)))

    def test_active_signal_is_deduplicated_and_reentry_notifies_again(self):
        item = candidate()
        first, current = new_alerts([item], {"signals": {}})
        self.assertEqual([row["symbol"] for row in first], ["US.MU"])
        active_state = update_state({"signals": {}}, current, first)
        repeated, _ = new_alerts([item], active_state)
        self.assertEqual(repeated, [])
        inactive_state = update_state(active_state, {}, [])
        reentered, _ = new_alerts([item], inactive_state)
        self.assertEqual([row["symbol"] for row in reentered], ["US.MU"])

    def test_failed_new_signal_remains_retryable(self):
        item = candidate()
        selected, current = new_alerts([item], {"signals": {}})
        self.assertEqual([row["symbol"] for row in selected], ["US.MU"])
        failed_state = update_state({"signals": {}}, current, successful=[])
        retried, _ = new_alerts([item], failed_state)
        self.assertEqual([row["symbol"] for row in retried], ["US.MU"])
        self.assertFalse(failed_state["signals"]["US.MU"]["active"])
        self.assertEqual(
            failed_state["signals"]["US.MU"]["pending_fingerprint"],
            signal_fingerprint(item),
        )

    def test_candidate_preview_requires_every_formal_gate_and_above_zone(self):
        item = candidate(status="waiting_entry", entry_execution_status="wait_pullback", price=110)
        self.assertTrue(qualifies_for_candidate_alert(item))
        self.assertFalse(qualifies_for_candidate_alert(candidate()))
        self.assertFalse(qualifies_for_candidate_alert({**item, "price": 100}))
        self.assertFalse(qualifies_for_candidate_alert({**item, "future_function_audit": "BLOCK"}))
        self.assertFalse(qualifies_for_candidate_alert({**item, "price_freshness": "stale"}))
        self.assertFalse(qualifies_for_candidate_alert({**item, "gate_failures": ["missing_fundamentals"]}))
        self.assertFalse(qualifies_for_candidate_alert({**item, "formal_qualified": False}))

    def test_candidate_preview_is_deduplicated_independently_from_entry_alert(self):
        item = candidate(status="waiting_entry", entry_execution_status="wait_pullback", price=110)
        selected, current = new_candidate_alerts([item], {"signals": {}, "candidate_signals": {}})
        self.assertEqual([row["symbol"] for row in selected], ["US.MU"])
        state = update_state({}, {}, [], current, selected)
        repeated, _ = new_candidate_alerts([item], state)
        self.assertEqual(repeated, [])
        executable, _ = new_alerts([candidate()], state)
        self.assertEqual([row["symbol"] for row in executable], ["US.MU"])
        inactive = update_state(state, {}, [], {}, [])
        requalified, _ = new_candidate_alerts([item], inactive)
        self.assertEqual([row["symbol"] for row in requalified], ["US.MU"])

    def test_failed_candidate_preview_remains_retryable(self):
        item = candidate(status="waiting_entry", entry_execution_status="wait_pullback", price=110)
        selected, current = new_candidate_alerts([item], {})
        failed_state = update_state({}, {}, [], current, [])
        self.assertFalse(failed_state["candidate_signals"]["US.MU"]["active"])
        retried, _ = new_candidate_alerts([item], failed_state)
        self.assertEqual([row["symbol"] for row in retried], ["US.MU"])

    def test_candidate_card_explains_pullback_and_does_not_claim_buy_signal(self):
        item = candidate(status="waiting_entry", entry_execution_status="wait_pullback", price=110, currency="USD")
        card = build_candidate_card([item], "https://ffxdz-ai.github.io/us-stock-research-dashboard/")
        content = card["elements"][0]["text"]["content"]
        self.assertIn("稳健买入区间", content)
        self.assertIn("尚未到价，不买、不追高", content)
        self.assertIn("仍需回调", content)


if __name__ == "__main__":
    unittest.main()
