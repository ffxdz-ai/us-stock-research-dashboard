#!/usr/bin/env python3
"""Auditable time-series performance metrics.

All percentage outputs use percentage points (``-10.0`` means minus ten
percent).  Functions return ``None`` plus an explicit reason when the required
time series does not exist or is statistically unusable.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Iterable


def _finite_values(values: Iterable[Any]) -> list[float]:
    output: list[float] = []
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            output.append(parsed)
    return output


def equity_returns(equity: Iterable[Any]) -> list[float]:
    values = _finite_values(equity)
    return [current / previous - 1 for previous, current in zip(values, values[1:]) if previous > 0]


def annualized_return(
    equity: Iterable[Any],
    *,
    periods_per_year: int = 252,
    minimum_periods: int = 20,
) -> dict[str, Any]:
    """Compound an equally spaced equity series only when the horizon is usable."""
    values = _finite_values(equity)
    periods = max(0, len(values) - 1)
    if periods < minimum_periods:
        return {"value_pct": None, "reason": "insufficient_equity_periods", "sample_count": periods, "periods_per_year": periods_per_year}
    if values[0] <= 0 or values[-1] <= 0:
        return {"value_pct": None, "reason": "non_positive_equity", "sample_count": periods, "periods_per_year": periods_per_year}
    value = (values[-1] / values[0]) ** (periods_per_year / periods) - 1
    return {"value_pct": round(value * 100, 4), "reason": None, "sample_count": periods, "periods_per_year": periods_per_year}


def annualized_volatility(
    daily_returns: Iterable[Any],
    *,
    periods_per_year: int = 252,
    minimum_samples: int = 20,
) -> dict[str, Any]:
    returns = _finite_values(daily_returns)
    if len(returns) < minimum_samples:
        return {"value_pct": None, "reason": "insufficient_daily_returns", "sample_count": len(returns), "periods_per_year": periods_per_year}
    volatility = statistics.stdev(returns) if len(returns) >= 2 else 0.0
    if volatility <= 0:
        return {"value_pct": None, "reason": "zero_return_volatility", "sample_count": len(returns), "periods_per_year": periods_per_year}
    return {
        "value_pct": round(volatility * math.sqrt(periods_per_year) * 100, 4),
        "reason": None,
        "sample_count": len(returns),
        "periods_per_year": periods_per_year,
    }


def maximum_drawdown(equity: Iterable[Any]) -> dict[str, Any]:
    values = _finite_values(equity)
    if not values:
        return {"value_pct": None, "reason": "equity_curve_unavailable", "sample_count": 0}
    if any(value <= 0 for value in values):
        return {"value_pct": None, "reason": "non_positive_equity", "sample_count": len(values)}
    peak = values[0]
    worst = 0.0
    peak_index = trough_index = 0
    candidate_peak_index = 0
    for index, value in enumerate(values):
        if value > peak:
            peak = value
            candidate_peak_index = index
        drawdown = value / peak - 1
        if drawdown < worst:
            worst = drawdown
            peak_index = candidate_peak_index
            trough_index = index
    return {
        "value_pct": round(worst * 100, 4),
        "reason": None,
        "sample_count": len(values),
        "peak_index": peak_index,
        "trough_index": trough_index,
    }


def annualized_sharpe(
    daily_returns: Iterable[Any],
    *,
    daily_risk_free_returns: Iterable[Any] | None = None,
    periods_per_year: int = 252,
    minimum_samples: int = 20,
) -> dict[str, Any]:
    returns = _finite_values(daily_returns)
    if len(returns) < minimum_samples:
        return {"value": None, "reason": "insufficient_daily_returns", "sample_count": len(returns), "periods_per_year": periods_per_year}
    if daily_risk_free_returns is None:
        risk_free = [0.0] * len(returns)
        cash_assumption = "zero_daily_risk_free_return"
    else:
        risk_free = _finite_values(daily_risk_free_returns)
        if len(risk_free) != len(returns):
            return {"value": None, "reason": "risk_free_series_misaligned", "sample_count": len(returns), "periods_per_year": periods_per_year}
        cash_assumption = "provided_same_frequency_risk_free_series"
    excess = [value - rf for value, rf in zip(returns, risk_free)]
    volatility = statistics.stdev(excess) if len(excess) >= 2 else 0.0
    if volatility <= 0:
        return {"value": None, "reason": "zero_return_volatility", "sample_count": len(excess), "periods_per_year": periods_per_year, "cash_return_assumption": cash_assumption}
    return {
        "value": round(statistics.mean(excess) / volatility * math.sqrt(periods_per_year), 4),
        "reason": None,
        "sample_count": len(excess),
        "periods_per_year": periods_per_year,
        "cash_return_assumption": cash_assumption,
    }


def annualized_information_ratio(
    portfolio_daily_returns: Iterable[Any],
    benchmark_daily_returns: Iterable[Any] | None,
    *,
    periods_per_year: int = 252,
    minimum_samples: int = 20,
) -> dict[str, Any]:
    portfolio = _finite_values(portfolio_daily_returns)
    if benchmark_daily_returns is None:
        return {"value": None, "reason": "benchmark_curve_unavailable", "sample_count": len(portfolio), "periods_per_year": periods_per_year}
    benchmark = _finite_values(benchmark_daily_returns)
    if len(portfolio) != len(benchmark):
        return {"value": None, "reason": "benchmark_series_misaligned", "sample_count": min(len(portfolio), len(benchmark)), "periods_per_year": periods_per_year}
    active = [value - reference for value, reference in zip(portfolio, benchmark)]
    if len(active) < minimum_samples:
        return {"value": None, "reason": "insufficient_active_returns", "sample_count": len(active), "periods_per_year": periods_per_year}
    tracking_error = statistics.stdev(active) if len(active) >= 2 else 0.0
    if tracking_error <= 0:
        return {"value": None, "reason": "zero_tracking_error", "sample_count": len(active), "periods_per_year": periods_per_year}
    return {
        "value": round(statistics.mean(active) / tracking_error * math.sqrt(periods_per_year), 4),
        "reason": None,
        "sample_count": len(active),
        "periods_per_year": periods_per_year,
    }


def trade_path_excursions(fill_price: Any, bars: Iterable[dict[str, Any]]) -> dict[str, Any]:
    try:
        fill = float(fill_price)
    except (TypeError, ValueError):
        fill = 0.0
    rows = [row for row in bars if isinstance(row, dict)]
    if fill <= 0 or not rows:
        return {"mae_pct": None, "mfe_pct": None, "price_granularity": None, "reason": "holding_path_unavailable"}
    lows = _finite_values(row.get("low") for row in rows)
    highs = _finite_values(row.get("high") for row in rows)
    if len(lows) == len(rows) and len(highs) == len(rows):
        return {
            "mae_pct": round((min(lows) / fill - 1) * 100, 4),
            "mfe_pct": round((max(highs) / fill - 1) * 100, 4),
            "price_granularity": "daily_high_low",
            "reason": None,
        }
    closes = _finite_values(row.get("close") for row in rows)
    if not closes:
        return {"mae_pct": None, "mfe_pct": None, "price_granularity": None, "reason": "holding_path_unavailable"}
    return {
        "mae_pct": round((min(closes) / fill - 1) * 100, 4),
        "mfe_pct": round((max(closes) / fill - 1) * 100, 4),
        "price_granularity": "daily_close_proxy",
        "reason": None,
    }


def summarize_trades(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for row in trades if isinstance(row, dict) and row.get("net_pnl") is not None]
    pnl = _finite_values(row.get("net_pnl") for row in rows)
    costs = sum(_finite_values(row.get("cost") for row in rows))
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    return {
        "trade_count": len(pnl),
        "win_rate_pct": round(len(wins) / len(pnl) * 100, 2) if pnl else None,
        "average_win": round(statistics.mean(wins), 4) if wins else None,
        "average_loss": round(statistics.mean(losses), 4) if losses else None,
        "expectancy": round(statistics.mean(pnl), 4) if pnl else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
        "total_costs": round(costs, 4),
    }
