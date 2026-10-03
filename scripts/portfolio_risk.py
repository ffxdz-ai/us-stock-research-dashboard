#!/usr/bin/env python3
"""Deterministic local portfolio risk-budget checks.

The public site never supplies account state, so its only valid portfolio
answer is ``pending_local_review``.  Private callers may pass aggregate local
state; raw positions/cash must not be exported.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "config" / "simulation_policy.json"


def load_portfolio_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return dict(payload.get("risk_budget") or {})


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def review_portfolio_permission(
    plan: dict[str, Any],
    portfolio: dict[str, Any] | None,
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if portfolio is None:
        return {
            "status": "pending_local_review",
            "allowed": False,
            "quantity": 0,
            "reason_codes": ["private_portfolio_state_required"],
            "public_safe": True,
        }
    policy = policy or load_portfolio_policy()
    equity = _number(portfolio.get("equity"))
    cash = _number(portfolio.get("cash"))
    entry, stop = _number(plan.get("entry_price")), _number(plan.get("stop_loss"))
    reasons: list[str] = []
    if equity is None or equity <= 0:
        reasons.append("portfolio_equity_invalid")
    if cash is None or cash <= 0:
        reasons.append("cash_unavailable")
    if entry is None or stop is None or stop >= entry:
        reasons.append("per_share_risk_invalid")
    if reasons:
        return {"status": "denied", "allowed": False, "quantity": 0, "reason_codes": reasons, "public_safe": True}
    per_share_loss = entry - stop + entry * 0.001
    drawdown = abs(_number(portfolio.get("drawdown_pct")) or 0) / 100
    risk_multiplier = 1.0
    if drawdown >= float(policy.get("drawdown_halt_at_pct") or 1):
        reasons.append("portfolio_drawdown_halt")
        risk_multiplier = 0.0
    elif drawdown >= float(policy.get("drawdown_reduce_at_pct") or 1):
        risk_multiplier *= 0.5
    earnings_known = plan.get("earnings_date_known")
    if earnings_known is False:
        reasons.append("earnings_date_unknown")
        risk_multiplier *= float(policy.get("earnings_risk_multiplier") or 0.5)
    elif plan.get("earnings_within_restricted_window") is True:
        reasons.append("earnings_restricted_window")
        risk_multiplier = 0.0
    correlation_status = str(plan.get("correlation_status") or "unknown")
    if correlation_status == "unknown":
        reasons.append("correlation_unknown_budget_reduced")
        risk_multiplier *= float(policy.get("unknown_correlation_multiplier") or 0.5)
    loss_budget = equity * float(policy.get("per_trade_loss_pct") or 0) * risk_multiplier
    risk_quantity = math.floor(loss_budget / per_share_loss) if per_share_loss > 0 else 0
    cash_quantity = math.floor(cash / entry)
    position_quantity = math.floor(equity * float(policy.get("max_single_position_pct") or 0) / entry)

    theme_exposure = _number(portfolio.get("theme_exposure_pct")) or 0
    industry_exposure = _number(portfolio.get("industry_exposure_pct")) or 0
    total_stop_risk = _number(portfolio.get("total_stop_risk_pct")) or 0
    if theme_exposure >= float(policy.get("max_theme_exposure_pct") or 1):
        reasons.append("theme_exposure_limit")
    if industry_exposure >= float(policy.get("max_industry_exposure_pct") or 1):
        reasons.append("industry_exposure_limit")
    if total_stop_risk >= float(policy.get("max_total_stop_risk_pct") or 1):
        reasons.append("total_stop_risk_limit")
    quantity = max(0, min(risk_quantity, cash_quantity, position_quantity))
    blocking = {"portfolio_drawdown_halt", "earnings_restricted_window", "theme_exposure_limit", "industry_exposure_limit", "total_stop_risk_limit"}
    if quantity < 1:
        reasons.append("whole_share_minimum_exceeds_budget")
    allowed = quantity >= 1 and not any(value in blocking for value in reasons)
    return {
        "status": "approved" if allowed else "denied",
        "allowed": allowed,
        "quantity": quantity if allowed else 0,
        "estimated_loss_budget": round(loss_budget, 2),
        "estimated_per_share_loss": round(per_share_loss, 4),
        "reason_codes": list(dict.fromkeys(reasons)),
        "stress_note": "stop risk is an estimate; correlated gaps can exceed it",
        "policy_calibration_status": policy.get("calibration_status") or "provisional",
        "public_safe": True,
    }
