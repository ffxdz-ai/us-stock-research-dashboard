#!/usr/bin/env python3
"""Versioned pullback/breakout plan builders with auditable target evidence."""

from __future__ import annotations

import math
from statistics import mean
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def average_true_range(bars: list[dict[str, Any]], window: int = 14) -> float | None:
    rows: list[tuple[float, float, float]] = []
    for bar in bars:
        high, low, close = (_number(bar.get(key)) for key in ("high", "low", "close"))
        if high is not None and low is not None and close is not None and high >= low > 0:
            rows.append((high, low, close))
    if len(rows) < window + 1:
        return None
    ranges = []
    for previous, current in zip(rows[-(window + 1):-1], rows[-window:]):
        high, low, _ = current
        prior_close = previous[2]
        ranges.append(max(high - low, abs(high - prior_close), abs(low - prior_close)))
    return round(mean(ranges), 6)


def cost_adjusted_rr(entry: Any, stop: Any, target: Any, *, round_trip_cost_bps: float) -> float | None:
    entry, stop, target = _number(entry), _number(stop), _number(target)
    if entry is None or stop is None or target is None or not stop < entry < target:
        return None
    estimated_cost = entry * round_trip_cost_bps / 10000
    risk = entry - stop + estimated_cost
    reward = target - entry - estimated_cost
    return round(reward / risk, 4) if risk > 0 and reward > 0 else None


def _target_payload(target: Any, metadata: dict[str, Any] | None) -> tuple[float | None, dict[str, Any], list[str]]:
    value = _number(target)
    metadata = metadata if isinstance(metadata, dict) else {}
    failures: list[str] = []
    required = ("target_method", "evidence", "as_of", "horizon", "scenarios")
    if value is None:
        failures.append("target_unknown")
    for key in required:
        if not metadata.get(key):
            failures.append(f"target_{key}_missing")
    if str(metadata.get("target_method") or "") == "prior_high_markup":
        failures.append("unsupported_prior_high_markup_target")
    return value, metadata, failures


def build_pullback_plan(
    *,
    entry_zone_low: Any,
    entry_zone_high: Any,
    structure_stop: Any,
    target: Any,
    target_metadata: dict[str, Any] | None,
    bars: list[dict[str, Any]],
    atr_multiple: float = 1.5,
    minimum_stop_atr: float = 0.75,
    round_trip_cost_bps: float = 12.0,
    expiry_sessions: int = 10,
) -> dict[str, Any]:
    low, high, structure = _number(entry_zone_low), _number(entry_zone_high), _number(structure_stop)
    target_value, target_metadata, failures = _target_payload(target, target_metadata)
    atr = average_true_range(bars)
    if None in {low, high, structure} or not structure < low <= high:
        failures.append("invalid_pullback_structure")
        entry = stop = None
    else:
        entry = high
        volatility_stop = entry - atr_multiple * atr if atr is not None else None
        stop = min(structure, volatility_stop) if volatility_stop is not None else structure
        if atr is None:
            failures.append("atr_unavailable")
        elif entry - stop < minimum_stop_atr * atr:
            failures.append("stop_distance_too_narrow")
    rr = cost_adjusted_rr(entry, stop, target_value, round_trip_cost_bps=round_trip_cost_bps)
    if rr is None:
        failures.append("cost_adjusted_rr_unavailable")
    return {
        "strategy_id": "fundamental_pullback_v1",
        "strategy_family": "pullback",
        "entry_zone_low": low,
        "entry_price": entry,
        "order_type": "limit",
        "stop_loss": stop,
        "atr": atr,
        "target_price": target_value,
        **target_metadata,
        "cost_adjusted_rr": rr,
        "round_trip_cost_bps": round_trip_cost_bps,
        "expiry_sessions": expiry_sessions,
        "plan_qualified": not failures,
        "failures": list(dict.fromkeys(failures)),
        "validation_status": "provisional_forward_validation_required",
    }


def build_breakout_plan(
    *,
    resistance: Any,
    structure_stop: Any,
    target: Any,
    target_metadata: dict[str, Any] | None,
    bars: list[dict[str, Any]],
    breakout_buffer: float = 0.005,
    max_entry_deviation: float = 0.005,
    minimum_volume_ratio: float = 1.2,
    volume_ratio: Any = None,
    round_trip_cost_bps: float = 12.0,
    expiry_sessions: int = 5,
) -> dict[str, Any]:
    resistance, stop = _number(resistance), _number(structure_stop)
    target_value, target_metadata, failures = _target_payload(target, target_metadata)
    entry = resistance * (1 + breakout_buffer) if resistance is not None else None
    atr = average_true_range(bars)
    volume = _number(volume_ratio)
    if entry is None or stop is None or stop >= entry:
        failures.append("invalid_breakout_structure")
    if atr is None:
        failures.append("atr_unavailable")
    elif entry is not None and stop is not None and entry - stop < 0.75 * atr:
        failures.append("stop_distance_too_narrow")
    if volume is None or volume < minimum_volume_ratio:
        failures.append("breakout_volume_confirmation_missing")
    rr = cost_adjusted_rr(entry, stop, target_value, round_trip_cost_bps=round_trip_cost_bps)
    if rr is None:
        failures.append("cost_adjusted_rr_unavailable")
    return {
        "strategy_id": "fundamental_breakout_v1",
        "strategy_family": "breakout",
        "entry_price": round(entry, 6) if entry is not None else None,
        "max_execution_price": round(entry * (1 + max_entry_deviation), 6) if entry is not None else None,
        "order_type": "buy_stop",
        "stop_loss": stop,
        "atr": atr,
        "target_price": target_value,
        **target_metadata,
        "cost_adjusted_rr": rr,
        "round_trip_cost_bps": round_trip_cost_bps,
        "expiry_sessions": expiry_sessions,
        "plan_qualified": not failures,
        "failures": list(dict.fromkeys(failures)),
        "validation_status": "provisional_forward_validation_required",
    }
