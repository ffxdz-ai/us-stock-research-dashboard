import unittest

from scripts.feishu_steady_buy_alerts import (
    new_alerts,
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


if __name__ == "__main__":
    unittest.main()
