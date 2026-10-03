from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from model_v2 import (
    assess_price_freshness,
    build_entry_path,
    build_signal_identifiers,
    evaluate_plan_execution,
    evaluate_plan_qualification,
    factor_snapshot,
    load_risk_policy,
    select_best_field_value,
    us_market_session,
    us_session_close,
)
from opportunity_review_metrics import forward_metrics, hit_rate_accounting
from performance_metrics import annualized_information_ratio, annualized_sharpe, maximum_drawdown
from point_in_time import build_pit_record, normalized_forecast_revision, purged_walk_forward_split, usable_at
from portfolio_risk import review_portfolio_permission
from simulation_engine import simulate_plan, simulate_portfolio
from simulation_ledger import SimulationLedger
from strategy_rules import build_pullback_plan, cost_adjusted_rr


def complete_candidate(**overrides):
    candidate = {
        "symbol": "US.TEST",
        "opportunity_score": 90,
        "trend_score": 90,
        "crowding_score": 10,
        "data_confidence": 1.0,
        "price_freshness": "fresh",
        "research_data_valid": True,
        "execution_quote_valid": True,
        "execution_allowed": True,
        "technical_data_complete": True,
        "future_function_audit": "PASS",
        "valid_path": True,
        "factor_coverage": 1.0,
        "missing_required_factors": [],
    }
    candidate.update(overrides)
    return candidate


def plan(**overrides):
    value = {
        "signal_id": "signal-test",
        "symbol": "US.TEST",
        "signal_time": "2026-10-01T20:00:00Z",
        "plan_qualified": True,
        "strategy_family": "pullback",
        "entry_price": 100,
        "stop_loss": 90,
        "target_price": 130,
    }
    value.update(overrides)
    return value


class P0QualificationTests(unittest.TestCase):
    def test_t01_partial_factor_coverage_blocks_formal_qualification(self):
        snapshot = factor_snapshot({"technical_score_v2": 100, "crowding_score": 0, "data_confidence": 1.0})
        self.assertEqual(snapshot["factor_coverage"], 0.25)
        self.assertTrue(snapshot["research_only"])
        candidate = complete_candidate(
            opportunity_score=snapshot["opportunity_score"],
            factor_coverage=snapshot["factor_coverage"],
            missing_required_factors=snapshot["missing_required_factors"],
        )
        result = evaluate_plan_qualification(candidate, build_entry_path("formal", 100, 90, 130, 3))
        self.assertFalse(result.qualified)
        self.assertIn("factor_coverage_below_threshold", result.gate_failures)

    def test_t02_required_valuation_missing_blocks_otherwise_high_scores(self):
        snapshot = factor_snapshot({
            "technical_score_v2": 100,
            "crowding_score": 0,
            "data_confidence": 1,
            "earnings_revision_score": 100,
            "catalyst_score": 100,
            "sec": {"revenue_growth_yoy": 0.3, "net_margin": 0.2, "liabilities_to_assets": 0.2, "latest_annual_fcf": {"val": 1}},
        })
        self.assertIn("valuation", snapshot["missing_required_factors"])
        candidate = complete_candidate(
            factor_coverage=snapshot["factor_coverage"],
            missing_required_factors=snapshot["missing_required_factors"],
        )
        result = evaluate_plan_qualification(candidate, build_entry_path("formal", 100, 90, 130, 3))
        self.assertIn("missing_required_factors", result.gate_failures)

    def test_t03_legitimate_zero_is_not_replaced(self):
        snapshot = factor_snapshot({"technical_score_v2": 0, "trend_score": 99, "crowding_score": 0, "data_confidence": 1})
        self.assertEqual(snapshot["factors"]["momentum"], 0)
        self.assertEqual(snapshot["factors"]["crowding"], 100)
        chosen = select_best_field_value([{"value": 0, "source": "SEC", "source_time": "2026-10-01"}])
        self.assertEqual(chosen["value"], 0)

    def test_t04_three_day_old_broker_quote_is_not_executable(self):
        result = assess_price_freshness(
            "2026-09-29T14:00:00Z", "Futu OpenD authenticated bridge", "2026-10-02T14:00:00Z"
        )
        self.assertFalse(result["execution_quote_valid"])
        self.assertFalse(result["execution_allowed"])
        self.assertIn("execution_quote_stale", result["reason_codes"])

    def test_t05_weekend_friday_close_is_research_valid_but_not_executable(self):
        result = assess_price_freshness(
            "2026-10-02T20:00:00Z", "Futu OpenD authenticated bridge", "2026-10-03T14:00:00Z"
        )
        self.assertTrue(result["research_data_valid"])
        self.assertFalse(result["execution_allowed"])
        self.assertEqual(result["market_session"], "closed")

    def test_t06_dst_holiday_and_early_close(self):
        self.assertEqual(us_market_session("2026-03-09T13:30:00Z"), "regular")
        self.assertEqual(us_market_session("2026-11-26T15:00:00Z"), "closed")
        self.assertEqual(us_session_close(datetime(2026, 11, 27).date()).hour, 13)

    def test_t15_plan_can_qualify_while_price_is_not_triggered(self):
        path = build_entry_path("formal", 100, 90, 130, 3)
        result = evaluate_plan_execution(
            complete_candidate(), path, current_price=120, portfolio_permission=True
        )
        self.assertTrue(result["plan_qualified"])
        self.assertFalse(result["price_triggered"])
        self.assertFalse(result["execution_allowed"])
        stale_research = evaluate_plan_execution(
            complete_candidate(research_data_valid=False), path, current_price=100, portfolio_permission=True
        )
        self.assertFalse(stale_research["plan_qualified"])
        self.assertIn("research_data_invalid", stale_research["reason_codes"])


class P0MetricsTests(unittest.TestCase):
    def test_t07_true_peak_to_trough_drawdown(self):
        self.assertAlmostEqual(maximum_drawdown([100, 150, 110])["value_pct"], -26.6667, places=4)

    def test_t08_monotonic_and_down_then_up_drawdowns(self):
        self.assertEqual(maximum_drawdown([100, 110, 120])["value_pct"], 0)
        self.assertEqual(maximum_drawdown([100, 90, 95])["value_pct"], -10)

    def test_t09_constant_or_short_curve_has_no_sharpe(self):
        self.assertEqual(annualized_sharpe([0.0] * 30)["reason"], "zero_return_volatility")
        self.assertEqual(annualized_sharpe([0.01] * 3)["reason"], "insufficient_daily_returns")
        self.assertEqual(annualized_information_ratio([0.01] * 30, None)["reason"], "benchmark_curve_unavailable")

    def test_forward_metrics_renames_legacy_floor_and_uses_true_drawdown(self):
        bars = [
            {"time": "2026-10-02", "close": 150, "high": 151, "low": 99},
            {"time": "2026-10-05", "close": 110, "high": 152, "low": 108},
        ]
        metrics = forward_metrics("2026-10-01", 100, bars, horizons=(1, 2))
        self.assertEqual(metrics["min_close_return_from_signal"], 10)
        self.assertAlmostEqual(metrics["max_drawdown"], -26.6667, places=4)
        self.assertEqual(metrics["excursion_price_granularity"], "daily_high_low")

    def test_t17_missing_benchmark_is_not_counted_as_failure(self):
        accounting = hit_rate_accounting([
            {"return_20d": 5, "hit_20d": None},
            {"return_20d": 3, "hit_20d": True},
            {"return_20d": -1, "hit_20d": False},
        ])
        self.assertEqual(accounting["return_mature_count"], 3)
        self.assertEqual(accounting["benchmark_missing_count"], 1)
        self.assertEqual(accounting["denominator"], 2)
        self.assertEqual(accounting["hit_rate_pct"], 50)


class P1SimulationTests(unittest.TestCase):
    def test_t10_close_signal_never_fills_on_same_bar(self):
        bars = [
            {"time": "2026-10-01", "open": 99, "high": 105, "low": 95, "close": 100, "volume": 100000},
            {"time": "2026-10-02", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100000},
        ]
        result = simulate_plan(plan(), bars, requested_quantity=1)
        self.assertEqual(result.fills[0]["fill_time"], "2026-10-02")

    def test_t11_unreached_pullback_expires_without_pnl(self):
        bars = [
            {"time": f"2026-10-{day:02d}", "open": 110, "high": 112, "low": 105, "close": 110, "volume": 100000}
            for day in range(2, 8)
        ]
        result = simulate_plan(plan(), bars, requested_quantity=1, tif_sessions=3)
        self.assertEqual(result.final_state, "expired")
        self.assertEqual(result.fills, [])
        self.assertEqual(result.net_pnl, 0)

    def test_t12_gap_stop_and_same_bar_ambiguity_are_conservative(self):
        entry_bar = {"time": "2026-10-02", "open": 100, "high": 105, "low": 95, "close": 100, "volume": 100000}
        gap = simulate_plan(plan(), [entry_bar, {"time": "2026-10-05", "open": 80, "high": 85, "low": 75, "close": 82, "volume": 100000}], requested_quantity=1)
        self.assertEqual(gap.fills[-1]["reason"], "stop_gap")
        self.assertLess(gap.return_pct, -10)
        ambiguous = simulate_plan(plan(), [{**entry_bar, "high": 135, "low": 85}], requested_quantity=1)
        self.assertEqual(ambiguous.fills[-1]["reason"], "stop_first_conservative")
        self.assertIn("intrabar_ambiguous", ambiguous.flags)

    def test_t13_ledger_is_idempotent_and_reconciles(self):
        with tempfile.TemporaryDirectory() as directory:
            with SimulationLedger(Path(directory) / "ledger.sqlite") as ledger:
                snapshot_id = ledger.record_snapshot({"price": 100}, captured_at="2026-10-01T20:00:00Z", source_version="fixture-v1")
                ids = build_signal_identifiers(
                    {"symbol": "US.TEST"}, build_entry_path("formal", 100, 90, 130, 3),
                    signal_time="2026-10-01T20:00:00Z", strategy_id="fundamental_pullback_v1", data_snapshot_id=snapshot_id,
                )
                signal = {**ids, "symbol": "US.TEST", "signal_time": "2026-10-01T20:00:00Z"}
                self.assertTrue(ledger.record_signal(signal))
                self.assertFalse(ledger.record_signal(signal))
                self.assertTrue(ledger.append_event(ids["signal_id"], "observed", signal["signal_time"]))
                self.assertFalse(ledger.append_event(ids["signal_id"], "observed", signal["signal_time"]))
                ledger.record_equity({"point_time": "2026-10-01", "cash": 900, "market_value": 100, "equity": 1000, "realized_pnl": 0, "unrealized_pnl": 0, "fees": 0})
                self.assertEqual(ledger.counts()["signals"], 1)

    def test_t18_split_dividend_and_delisting_remain_in_results(self):
        result = simulate_plan(plan(), [
            {"time": "2026-10-02", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100000},
            {"time": "2026-10-05", "open": 50, "high": 52, "low": 49, "close": 51, "volume": 200000, "split_ratio": 2, "dividend": 0.5},
            {"time": "2026-10-06", "open": 20, "high": 20, "low": 20, "close": 20, "volume": 1, "delisted": True, "delisting_price": 20},
        ], requested_quantity=1)
        self.assertEqual(result.final_state, "closed")
        self.assertIn("split_adjusted", result.flags)
        self.assertIn("cash_dividend_applied", result.flags)
        self.assertIn("delisted_terminal_price", result.flags)

    def test_portfolio_reconciles_and_higher_cost_never_improves_fixed_fills(self):
        bars = {"US.TEST": [
            {"time": "2026-10-02", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100000},
            {"time": "2026-10-05", "open": 130, "high": 132, "low": 125, "close": 130, "volume": 100000},
        ]}
        low_cost = simulate_portfolio([plan()], bars, commission_per_order=0, slippage_bps=0)
        high_cost = simulate_portfolio([plan()], bars, commission_per_order=10, slippage_bps=20)
        self.assertGreaterEqual(low_cost["total_return_pct"], high_cost["total_return_pct"])
        for point in high_cost["equity_curve"]:
            self.assertAlmostEqual(point["cash"] + point["market_value"], point["equity"], places=5)

    def test_optional_partial_exit_is_auditable_and_reconciles(self):
        partial_plan = plan(partial_target_price=110, partial_exit_fraction=0.5)
        rows = [
            {"time": "2026-10-02", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100000},
            {"time": "2026-10-05", "open": 105, "high": 115, "low": 104, "close": 112, "volume": 100000},
            {"time": "2026-10-06", "open": 125, "high": 132, "low": 124, "close": 131, "volume": 100000},
        ]
        result = simulate_plan(partial_plan, rows, requested_quantity=10, commission_per_order=0, slippage_bps=0, spread_bps=0)
        self.assertEqual([fill["quantity"] for fill in result.fills], [10, 5, 5])
        self.assertIn("partially_exited", [event["state"] for event in result.states])
        portfolio = simulate_portfolio([partial_plan], {"US.TEST": rows}, commission_per_order=0, slippage_bps=0, spread_bps=0)
        self.assertEqual(portfolio["trade_count"], 1)
        self.assertEqual(portfolio["fill_count"], 3)
        for point in portfolio["equity_curve"]:
            self.assertAlmostEqual(point["cash"] + point["market_value"], point["equity"], places=5)

    def test_portfolio_cash_dividend_is_not_dropped_or_double_counted(self):
        rows = [
            {"time": "2026-10-02", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100000},
            {"time": "2026-10-05", "open": 105, "high": 106, "low": 104, "close": 105, "volume": 100000, "dividend": 1.0},
            {"time": "2026-10-06", "open": 130, "high": 132, "low": 129, "close": 131, "volume": 100000},
        ]
        with_dividend = simulate_portfolio([plan()], {"US.TEST": rows}, commission_per_order=0, slippage_bps=0, spread_bps=0)
        without_dividend = simulate_portfolio(
            [plan()], {"US.TEST": [{key: value for key, value in row.items() if key != "dividend"} for row in rows]},
            commission_per_order=0, slippage_bps=0, spread_bps=0,
        )
        self.assertEqual(len(with_dividend["cash_adjustments"]), 1)
        self.assertGreater(with_dividend["total_return_pct"], without_dividend["total_return_pct"])


class P2P3Tests(unittest.TestCase):
    def test_t14_pit_and_forecast_roll_do_not_leak(self):
        record = build_pit_record(
            {"eps": 1}, period_end="2026-09-30", published_at="2026-10-01", ingested_at="2026-10-02T00:00:00Z",
            source="SEC", source_version="fixture",
        )
        self.assertFalse(usable_at(record, "2026-10-01T15:00:00Z"))
        self.assertIsNone(normalized_forecast_revision(
            {"forecast_period": "FY1-2026", "value": 2}, {"forecast_period": "FY1-2027", "value": 3}
        )["revision"])
        rows = purged_walk_forward_split(
            [{"signal_time": "2023-12-20"}], train_end="2023-12-31", validation_end="2024-12-31",
            label_horizon_days=20, embargo_days=5,
        )
        self.assertEqual(rows[0]["exclusion_reason"], "label_crosses_split_boundary")

    def test_strategy_requires_evidenced_target_and_cost_adjusted_rr(self):
        bars = [{"high": 101 + i, "low": 99 + i, "close": 100 + i} for i in range(20)]
        result = build_pullback_plan(
            entry_zone_low=100, entry_zone_high=102, structure_stop=90, target=None,
            target_metadata=None, bars=bars,
        )
        self.assertFalse(result["plan_qualified"])
        self.assertIn("target_unknown", result["failures"])
        self.assertLess(cost_adjusted_rr(100, 90, 130, round_trip_cost_bps=100), 3)

    def test_t16_theme_budget_can_reject_individually_valid_stock(self):
        decision = review_portfolio_permission(
            {"entry_price": 100, "stop_loss": 90, "correlation_status": "known", "earnings_date_known": True},
            {"equity": 100000, "cash": 50000, "theme_exposure_pct": 0.25, "industry_exposure_pct": 0.1, "total_stop_risk_pct": 0.01},
        )
        self.assertFalse(decision["allowed"])
        self.assertIn("theme_exposure_limit", decision["reason_codes"])

    def test_t20_public_summary_contains_no_account_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            with SimulationLedger(Path(directory) / "ledger.sqlite") as ledger:
                exported = json.dumps(ledger.export_public_summary())
        self.assertNotIn("cash", exported.lower())
        self.assertNotIn("position", exported.lower())
        self.assertNotIn("api_key", exported.lower())


if __name__ == "__main__":
    unittest.main()
