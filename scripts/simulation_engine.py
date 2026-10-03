#!/usr/bin/env python3
"""Deterministic next-session, cash-long-only OHLC execution simulator."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from model_v2 import parse_timestamp
from performance_metrics import (
    annualized_information_ratio,
    annualized_return,
    annualized_sharpe,
    annualized_volatility,
    equity_returns,
    maximum_drawdown,
    summarize_trades,
)


TERMINAL_STATES = {"closed", "expired", "cancelled", "rejected"}


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


@dataclass
class SimulationResult:
    signal_id: str
    symbol: str
    final_state: str
    states: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    cash_adjustments: list[dict[str, Any]] = field(default_factory=list)
    net_pnl: float = 0.0
    total_cost: float = 0.0
    return_pct: float | None = None
    holding_sessions: int = 0
    flags: list[str] = field(default_factory=list)
    unfilled_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _event_time(bar: dict[str, Any]) -> str:
    return str(bar.get("time") or bar.get("date") or "")


def _after_signal(bar: dict[str, Any], signal_time: Any) -> bool:
    stamp, signal = parse_timestamp(_event_time(bar)), parse_timestamp(signal_time)
    return bool(stamp and signal and stamp.date() > signal.date())


def _slipped(price: float, bps: float, side: str) -> float:
    multiplier = 1 + bps / 10000 if side == "buy" else 1 - bps / 10000
    return round(price * multiplier, 6)


def simulate_plan(
    plan: dict[str, Any],
    bars: list[dict[str, Any]],
    *,
    cash: float = 100000.0,
    commission_per_order: float = 1.0,
    slippage_bps: float = 5.0,
    spread_bps: float = 2.0,
    minimum_lot: int = 1,
    max_volume_participation: float = 0.05,
    tif_sessions: int = 5,
    time_exit_sessions: int | None = None,
    requested_quantity: int | None = None,
) -> SimulationResult:
    signal_id = str(plan.get("signal_id") or "missing-signal-id")
    symbol = str(plan.get("symbol") or "UNKNOWN")
    result = SimulationResult(signal_id, symbol, "observed")

    def transition(state: str, stamp: str, **payload: Any) -> None:
        result.states.append({"state": state, "time": stamp, **payload})
        result.final_state = state

    signal_time = plan.get("signal_time")
    entry, stop, target = (_num(plan.get(key)) for key in ("entry_price", "stop_loss", "target_price"))
    strategy = str(plan.get("strategy_family") or "pullback")
    if not plan.get("plan_qualified") or entry is None or stop is None or target is None or not stop < entry < target:
        transition("rejected", str(signal_time or ""), reason="invalid_or_unqualified_plan")
        result.unfilled_reason = "invalid_or_unqualified_plan"
        return result
    transition("qualified", str(signal_time), strategy_family=strategy)
    transition("waiting", str(signal_time))
    future_bars = sorted((bar for bar in bars if _after_signal(bar, signal_time)), key=_event_time)
    if not future_bars:
        result.unfilled_reason = "no_post_signal_bars"
        return result
    transition("order_pending", _event_time(future_bars[0]), order_type="buy_stop" if strategy == "breakout" else "buy_limit")

    quantity = 0
    entry_fill = None
    entry_index = -1
    total_buy_cost = 0.0
    for index, bar in enumerate(future_bars[: max(1, tif_sessions)]):
        if bar.get("halted") is True:
            continue
        open_price, low, high = (_num(bar.get(key)) for key in ("open", "low", "high"))
        if open_price is None or low is None or high is None:
            continue
        base_fill = None
        if strategy == "breakout":
            if open_price >= entry:
                base_fill = open_price
            elif high >= entry:
                base_fill = entry
        else:
            if open_price <= entry:
                base_fill = open_price
            elif low <= entry:
                base_fill = entry
        if base_fill is None:
            continue
        fill_price = _slipped(base_fill, slippage_bps + spread_bps / 2, "buy")
        volume = _num(bar.get("volume"))
        volume_cap = math.floor(volume * max_volume_participation) if volume and volume > 0 else math.inf
        cash_cap = math.floor((cash - commission_per_order) / fill_price)
        desired = int(requested_quantity) if requested_quantity is not None else cash_cap
        quantity = int(min(desired, cash_cap, volume_cap))
        quantity = quantity - quantity % max(1, minimum_lot)
        if quantity <= 0:
            transition("rejected", _event_time(bar), reason="cash_volume_or_lot_constraint")
            result.unfilled_reason = "cash_volume_or_lot_constraint"
            return result
        total_buy_cost = quantity * fill_price + commission_per_order
        cash -= total_buy_cost
        entry_fill = fill_price
        entry_index = index
        fill = {
            "signal_id": signal_id, "symbol": symbol, "side": "buy", "quantity": quantity,
            "price": fill_price, "fees": commission_per_order, "fill_time": _event_time(bar), "reason": "entry_trigger",
        }
        result.fills.append(fill)
        result.total_cost += commission_per_order + quantity * base_fill * (slippage_bps + spread_bps / 2) / 10000
        transition("filled", _event_time(bar), quantity=quantity, price=fill_price)
        break
    if entry_fill is None:
        transition("expired", _event_time(future_bars[min(len(future_bars), max(1, tif_sessions)) - 1]), reason="entry_not_triggered_before_tif")
        result.unfilled_reason = "entry_not_triggered_before_tif"
        return result

    adjusted_stop, adjusted_target = stop, target
    partial_target = _num(plan.get("partial_target_price"))
    partial_fraction = _num(plan.get("partial_exit_fraction"))
    partial_enabled = bool(
        partial_target is not None and partial_fraction is not None
        and entry_fill < partial_target < adjusted_target and 0 < partial_fraction < 1
    )
    partial_done = False
    gross_exit_proceeds = 0.0
    dividend_income = 0.0
    exit_fill = None
    exit_reason = ""
    exit_rows = future_bars[entry_index:]
    for held, bar in enumerate(exit_rows, start=1):
        stamp = _event_time(bar)
        if bar.get("halted") is True:
            continue
        split_ratio = _num(bar.get("split_ratio"))
        if split_ratio and split_ratio > 0 and split_ratio != 1:
            quantity = max(1, int(round(quantity * split_ratio)))
            entry_fill /= split_ratio
            adjusted_stop /= split_ratio
            adjusted_target /= split_ratio
            result.flags.append("split_adjusted")
        dividend = _num(bar.get("dividend"))
        if dividend and dividend > 0:
            received_dividend = dividend * quantity
            cash += received_dividend
            dividend_income += received_dividend
            adjustment = {
                "signal_id": signal_id,
                "symbol": symbol,
                "kind": "cash_dividend",
                "amount_per_share": dividend,
                "event_time": stamp,
            }
            result.cash_adjustments.append(adjustment)
            result.states.append({"state": "cash_adjustment", "time": stamp, **{key: value for key, value in adjustment.items() if key not in {"signal_id", "symbol", "event_time"}}})
            result.flags.append("cash_dividend_applied")
        if bar.get("delisted") is True:
            delist_price = _num(bar.get("delisting_price")) or 0.000001
            exit_fill = _slipped(delist_price, slippage_bps, "sell")
            exit_reason = "delisted"
            result.flags.append("delisted_terminal_price")
        else:
            open_price, low, high, close = (_num(bar.get(key)) for key in ("open", "low", "high", "close"))
            if None in {open_price, low, high, close}:
                continue
            stop_hit, target_hit = low <= adjusted_stop, high >= adjusted_target
            if open_price <= adjusted_stop:
                exit_fill = _slipped(open_price, slippage_bps + spread_bps / 2, "sell")
                exit_reason = "stop_gap"
                result.flags.append("gap_through_stop")
            elif open_price >= adjusted_target:
                exit_fill = _slipped(open_price, slippage_bps + spread_bps / 2, "sell")
                exit_reason = "target_gap"
            elif stop_hit and target_hit:
                exit_fill = _slipped(adjusted_stop, slippage_bps + spread_bps / 2, "sell")
                exit_reason = "stop_first_conservative"
                result.flags.append("intrabar_ambiguous")
            elif stop_hit:
                exit_fill = _slipped(adjusted_stop, slippage_bps + spread_bps / 2, "sell")
                exit_reason = "initial_stop"
            elif target_hit:
                exit_fill = _slipped(adjusted_target, slippage_bps + spread_bps / 2, "sell")
                exit_reason = "target"
            elif plan.get("thesis_invalidated_at") and stamp >= str(plan["thesis_invalidated_at"]):
                exit_fill = _slipped(close, slippage_bps + spread_bps / 2, "sell")
                exit_reason = "thesis_invalidated"
            elif time_exit_sessions is not None and held >= time_exit_sessions:
                exit_fill = _slipped(close, slippage_bps + spread_bps / 2, "sell")
                exit_reason = "time_exit"
            if exit_fill is None and partial_enabled and not partial_done and not stop_hit and high >= partial_target:
                partial_quantity = min(quantity - 1, max(1, math.floor(quantity * partial_fraction)))
                if partial_quantity > 0:
                    partial_base = open_price if open_price >= partial_target else partial_target
                    partial_fill = _slipped(partial_base, slippage_bps + spread_bps / 2, "sell")
                    partial_proceeds = partial_quantity * partial_fill - commission_per_order
                    cash += partial_proceeds
                    gross_exit_proceeds += partial_proceeds
                    quantity -= partial_quantity
                    result.fills.append({
                        "signal_id": signal_id, "symbol": symbol, "side": "sell", "quantity": partial_quantity,
                        "price": partial_fill, "fees": commission_per_order, "fill_time": stamp, "reason": "partial_target",
                    })
                    result.total_cost += commission_per_order
                    transition("partially_exited", stamp, quantity=partial_quantity, remaining_quantity=quantity, price=partial_fill)
                    partial_done = True
        if exit_fill is not None:
            proceeds = quantity * exit_fill - commission_per_order
            cash += proceeds
            gross_exit_proceeds += proceeds
            result.fills.append({
                "signal_id": signal_id, "symbol": symbol, "side": "sell", "quantity": quantity,
                "price": exit_fill, "fees": commission_per_order, "fill_time": stamp, "reason": exit_reason,
            })
            result.total_cost += commission_per_order
            result.net_pnl = round(gross_exit_proceeds - total_buy_cost + dividend_income, 6)
            result.return_pct = round(result.net_pnl / total_buy_cost * 100, 4) if total_buy_cost > 0 else None
            result.holding_sessions = held
            transition("closed", stamp, reason=exit_reason, price=exit_fill)
            break
    if exit_fill is None:
        result.holding_sessions = len(exit_rows)
    return result


def simulate_portfolio(
    plans: list[dict[str, Any]],
    bars_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    initial_cash: float = 100000.0,
    risk_per_trade_pct: float = 0.005,
    commission_per_order: float = 1.0,
    slippage_bps: float = 5.0,
    spread_bps: float = 2.0,
    minimum_lot: int = 1,
    max_volume_participation: float = 0.05,
    tif_sessions: int = 5,
    time_exit_sessions: int | None = None,
    benchmark_equity: list[float] | None = None,
    benchmarks: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    """Run a deterministic shared-cash portfolio from independent plan events.

    Candidate fills are produced by :func:`simulate_plan`, then replayed in
    chronological order against one cash balance.  A second overlapping plan
    for the same symbol is rejected instead of silently pyramiding.
    """
    candidates: list[dict[str, Any]] = []
    simulation_results: list[SimulationResult] = []
    signal_count = len(plans)
    qualified_count = sum(1 for item in plans if item.get("plan_qualified") is True)
    expired_count = 0
    for item in plans:
        symbol = str(item.get("symbol") or "")
        entry, stop = _num(item.get("entry_price")), _num(item.get("stop_loss"))
        if entry is None or stop is None or entry <= stop:
            continue
        loss_budget = initial_cash * risk_per_trade_pct
        quantity = max(0, math.floor(loss_budget / (entry - stop + entry * slippage_bps / 10000)))
        result = simulate_plan(
            item, bars_by_symbol.get(symbol, []), cash=initial_cash,
            commission_per_order=commission_per_order, slippage_bps=slippage_bps,
            spread_bps=spread_bps, minimum_lot=minimum_lot,
            max_volume_participation=max_volume_participation,
            tif_sessions=tif_sessions, time_exit_sessions=time_exit_sessions,
            requested_quantity=quantity,
        )
        simulation_results.append(result)
        if result.final_state == "expired":
            expired_count += 1
        if result.fills:
            candidates.append({"result": result, "plan": item})

    event_rows: list[dict[str, Any]] = []
    for candidate in candidates:
        for fill in candidate["result"].fills:
            event_rows.append({**fill, "event_type": "fill", "plan": candidate["plan"]})
        for adjustment in candidate["result"].cash_adjustments:
            event_rows.append({**adjustment, "event_type": "cash_adjustment", "plan": candidate["plan"]})
    event_rows.sort(key=lambda row: (
        str(row.get("fill_time") or row.get("event_time") or ""),
        0 if row.get("event_type") == "cash_adjustment" else 1 if row.get("side") == "sell" else 2,
        str(row.get("symbol") or ""),
    ))
    cash = float(initial_cash)
    positions: dict[str, dict[str, Any]] = {}
    accepted: list[dict[str, Any]] = []
    accepted_cash_adjustments: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for event in event_rows:
        symbol = str(event["symbol"])
        if event.get("event_type") == "cash_adjustment":
            position = positions.get(symbol)
            if position and position["signal_id"] == event["signal_id"]:
                amount = position["quantity"] * float(event["amount_per_share"])
                cash += amount
                accepted_cash_adjustments.append({
                    **{key: value for key, value in event.items() if key != "plan"},
                    "quantity": position["quantity"],
                    "amount": round(amount, 6),
                })
            continue
        if event["side"] == "buy":
            if symbol in positions:
                rejected.append({"signal_id": event["signal_id"], "reason": "overlapping_symbol_position"})
                continue
            requested = int(event["quantity"])
            affordable = math.floor((cash - commission_per_order) / float(event["price"]))
            quantity = min(requested, affordable)
            if quantity <= 0:
                rejected.append({"signal_id": event["signal_id"], "reason": "shared_cash_insufficient"})
                continue
            cost = quantity * float(event["price"]) + commission_per_order
            cash -= cost
            accepted_fill = {**event, "quantity": quantity, "fees": commission_per_order}
            accepted.append(accepted_fill)
            positions[symbol] = {"quantity": quantity, "entry_price": float(event["price"]), "entry_cost": cost, "signal_id": event["signal_id"]}
        else:
            position = positions.get(symbol)
            if not position or position["signal_id"] != event["signal_id"]:
                continue
            quantity = min(position["quantity"], int(event["quantity"]))
            if quantity <= 0:
                continue
            proceeds = quantity * float(event["price"]) - commission_per_order
            cash += proceeds
            accepted.append({**event, "quantity": quantity, "fees": commission_per_order})
            allocated_cost = position["entry_cost"] * quantity / position["quantity"]
            position["quantity"] -= quantity
            position["entry_cost"] -= allocated_cost
            if position["quantity"] <= 0:
                del positions[symbol]

    dates = sorted({str(bar.get("time") or bar.get("date") or "") for bars in bars_by_symbol.values() for bar in bars if bar.get("time") or bar.get("date")})
    fills_by_date: dict[str, list[dict[str, Any]]] = {}
    for fill in accepted:
        fills_by_date.setdefault(str(fill["fill_time"]), []).append({**fill, "event_type": "fill"})
    for adjustment in accepted_cash_adjustments:
        fills_by_date.setdefault(str(adjustment["event_time"]), []).append(adjustment)
    replay_cash = float(initial_cash)
    replay_positions: dict[str, dict[str, Any]] = {}
    equity_curve: list[dict[str, Any]] = []
    last_closes: dict[str, float] = {}
    realized = 0.0
    fees = 0.0
    for day in dates:
        for symbol, bars in bars_by_symbol.items():
            for bar in bars:
                if str(bar.get("time") or bar.get("date") or "") == day:
                    close = _num(bar.get("close"))
                    if close is not None:
                        last_closes[symbol] = close
        for fill in sorted(fills_by_date.get(day, []), key=lambda row: 0 if row.get("event_type") == "cash_adjustment" else 1):
            if fill.get("event_type") == "cash_adjustment":
                replay_cash += float(fill["amount"])
                realized += float(fill["amount"])
                continue
            symbol, quantity, price, fee = str(fill["symbol"]), int(fill["quantity"]), float(fill["price"]), float(fill.get("fees") or 0)
            fees += fee
            if fill["side"] == "buy":
                replay_cash -= quantity * price + fee
                replay_positions[symbol] = {"quantity": quantity, "entry_price": price, "entry_cost": quantity * price + fee}
            else:
                position = replay_positions.get(symbol)
                if position:
                    proceeds = quantity * price - fee
                    allocated_cost = position["entry_cost"] * quantity / position["quantity"]
                    replay_cash += proceeds
                    realized += proceeds - allocated_cost
                    position["quantity"] -= quantity
                    position["entry_cost"] -= allocated_cost
                    if position["quantity"] <= 0:
                        del replay_positions[symbol]
        market_value = sum(position["quantity"] * last_closes.get(symbol, position["entry_price"]) for symbol, position in replay_positions.items())
        cost_basis = sum(position["entry_cost"] for position in replay_positions.values())
        equity_curve.append({
            "point_time": day,
            "cash": round(replay_cash, 6),
            "market_value": round(market_value, 6),
            "equity": round(replay_cash + market_value, 6),
            "realized_pnl": round(realized, 6),
            "unrealized_pnl": round(market_value - cost_basis, 6),
            "fees": round(fees, 6),
        })
    equities = [row["equity"] for row in equity_curve]
    daily_returns = equity_returns(equities)
    benchmark_curves = dict(benchmarks or {})
    if benchmark_equity is not None and "primary" not in benchmark_curves:
        benchmark_curves["primary"] = benchmark_equity
    primary_benchmark = benchmark_curves.get("primary")
    benchmark_returns = equity_returns(primary_benchmark) if primary_benchmark is not None else None
    sharpe = annualized_sharpe(daily_returns)
    information = annualized_information_ratio(daily_returns, benchmark_returns)
    annualized = annualized_return(equities)
    volatility = annualized_volatility(daily_returns)
    completed_trades = []
    open_buys: dict[str, dict[str, Any]] = {}
    for fill in accepted:
        key = f"{fill['signal_id']}:{fill['symbol']}"
        if fill["side"] == "buy":
            open_buys[key] = {
                "remaining_quantity": int(fill["quantity"]),
                "entry_price": float(fill["price"]),
                "entry_fees_remaining": float(fill["fees"]),
                "net_pnl": -float(fill["fees"]),
                "cost": float(fill["fees"]),
            }
        elif key in open_buys:
            trade = open_buys[key]
            quantity = min(trade["remaining_quantity"], int(fill["quantity"]))
            trade["net_pnl"] += (float(fill["price"]) - trade["entry_price"]) * quantity - float(fill["fees"])
            trade["cost"] += float(fill["fees"])
            trade["remaining_quantity"] -= quantity
            if trade["remaining_quantity"] <= 0:
                completed_trades.append({
                    "net_pnl": trade["net_pnl"] + sum(
                        float(row["amount"]) for row in accepted_cash_adjustments if row["signal_id"] == fill["signal_id"]
                    ),
                    "cost": trade["cost"],
                    "holding_sessions": next(
                        (result.holding_sessions for result in simulation_results if result.signal_id == fill["signal_id"]),
                        None,
                    ),
                })
                del open_buys[key]
    average_equity = sum(equities) / len(equities) if equities else 0.0
    turnover_notional = sum(abs(float(fill["price"]) * int(fill["quantity"])) for fill in accepted)
    turnover_pct = round(turnover_notional / average_equity * 100, 4) if average_equity > 0 else None
    utilization = [row["market_value"] / row["equity"] for row in equity_curve if row["equity"] > 0]
    benchmark_metrics: dict[str, dict[str, Any]] = {}
    for name in ("SPY", "QQQ", "industry_etf", "equal_weight_universe", "simple_trend"):
        curve = benchmark_curves.get(name)
        if curve is None:
            benchmark_metrics[name] = {"available": False, "reason": "benchmark_curve_unavailable"}
            continue
        aligned = len(curve) == len(equities) and len(curve) > 0
        benchmark_metrics[name] = {
            "available": aligned,
            "reason": None if aligned else "benchmark_series_misaligned",
            "sample_count": len(curve),
            "total_return_pct": round((float(curve[-1]) / float(curve[0]) - 1) * 100, 4) if aligned and float(curve[0]) > 0 else None,
            "maximum_drawdown_pct": maximum_drawdown(curve).get("value_pct") if aligned else None,
        }
    return {
        "mode": "cash_long_only",
        "signal_count": signal_count,
        "qualified_count": qualified_count,
        "triggered_count": len(candidates),
        "fill_count": len(accepted),
        "trade_count": len(completed_trades),
        "expired_unfilled_count": expired_count,
        "rejected_events": rejected,
        "fills": [{key: value for key, value in fill.items() if key != "plan"} for fill in accepted],
        "cash_adjustments": [{key: value for key, value in row.items() if key != "plan"} for row in accepted_cash_adjustments],
        "signal_results": [
            {
                "signal_id": result.signal_id,
                "symbol": result.symbol,
                "final_state": result.final_state,
                "states": result.states,
                "unfilled_reason": result.unfilled_reason,
            }
            for result in simulation_results
        ],
        "equity_curve": equity_curve,
        "total_return_pct": round((equities[-1] / initial_cash - 1) * 100, 4) if equities else None,
        "annualized_return_pct": annualized.get("value_pct"),
        "annualized_return_reason": annualized.get("reason"),
        "maximum_drawdown_pct": maximum_drawdown(equities).get("value_pct"),
        "annualized_volatility_pct": volatility.get("value_pct"),
        "annualized_volatility_reason": volatility.get("reason"),
        "sharpe": sharpe.get("value"),
        "sharpe_reason": sharpe.get("reason"),
        "information_ratio": information.get("value"),
        "information_ratio_reason": information.get("reason"),
        "performance_sample_count": len(daily_returns),
        "sample_count": len(daily_returns),
        "trade_statistics": summarize_trades(completed_trades),
        "average_holding_sessions": round(
            sum(row["holding_sessions"] for row in completed_trades if row.get("holding_sessions") is not None)
            / len([row for row in completed_trades if row.get("holding_sessions") is not None]),
            4,
        ) if any(row.get("holding_sessions") is not None for row in completed_trades) else None,
        "turnover_pct_of_average_equity": turnover_pct,
        "average_cash_utilization_pct": round(sum(utilization) / len(utilization) * 100, 4) if utilization else None,
        "maximum_gross_exposure_pct": round(max(utilization) * 100, 4) if utilization else None,
        "benchmark_metrics": benchmark_metrics,
        "open_position_count": len(replay_positions),
        "cash_ending": round(replay_cash, 6),
        "cost_assumptions": {
            "commission_per_order": commission_per_order,
            "slippage_bps": slippage_bps,
            "spread_bps": spread_bps,
            "minimum_lot": minimum_lot,
            "max_volume_participation": max_volume_participation,
            "tif_sessions": tif_sessions,
        },
        "limitations": ["daily_ohlc_liquidity_approximation", "corporate_actions_require_explicit_bar_fields"],
    }
