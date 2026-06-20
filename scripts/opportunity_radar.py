#!/usr/bin/env python3
"""Generate an opportunity-discovery radar with review memory.

This module is intentionally upstream of Buy-Side stock analysis. It tries to
detect expectation gaps across themes and supply chains, then records 30/60/90
day review checkpoints. It must not bypass the project's risk/reward, valuation
and whole-share execution discipline.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DOCS_DATA_DIR = ROOT / "docs" / "data"
REPORTS_DIR = ROOT / "reports"

DEFAULT_CONFIG = CONFIG_DIR / "opportunity_map.json"
DEFAULT_MARKET_PACK = DATA_DIR / "latest_market_pack.json"
DEFAULT_SUPPLY_RADAR = DATA_DIR / "latest_supply_chain_radar.json"
DEFAULT_SECONDARY_QUEUE = DOCS_DATA_DIR / "secondary_analysis_queue.json"
DEFAULT_STATE = DOCS_DATA_DIR / "opportunity_journal.json"
DEFAULT_OUTPUT = DATA_DIR / "latest_opportunity_radar.json"
DEFAULT_DOCS_OUTPUT = DOCS_DATA_DIR / "opportunity_radar.json"
DEFAULT_REPORT = REPORTS_DIR / "latest-opportunity-radar.md"


def beijing_timezone() -> timezone:
    try:
        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return timezone(timedelta(hours=8), "Asia/Shanghai")


def now_local() -> datetime:
    return datetime.now(beijing_timezone())


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace("%", "").replace(",", "").strip()
        if not cleaned or cleaned.lower() in {"n/a", "nan", "none", "null", "--", "数据不足"}:
            return None
        try:
            parsed = float(cleaned)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=beijing_timezone())
    return parsed.astimezone(beijing_timezone())


def fmt_num(value: Any, digits: int = 1) -> str:
    parsed = number(value)
    return "数据不足" if parsed is None else f"{parsed:.{digits}f}"


def fmt_pct(value: Any, digits: int = 1) -> str:
    parsed = number(value)
    if parsed is None:
        return "数据不足"
    return f"{parsed * 100:.{digits}f}%" if abs(parsed) <= 3 else f"{parsed:.{digits}f}%"


def fmt_price(value: Any) -> str:
    parsed = number(value)
    return "n/a" if parsed is None else f"{parsed:,.2f}"


def normalize_code(code: Any) -> str:
    raw = str(code or "").strip().upper()
    if not raw:
        return ""
    return raw if "." in raw else f"US.{raw}"


def code_market(code: str) -> str:
    upper = normalize_code(code)
    if upper.startswith("US."):
        return "US"
    if upper.startswith("HK."):
        return "HK"
    if upper.startswith(("SH.", "SZ.")):
        return "CN"
    return upper.split(".", 1)[0] if "." in upper else "US"


def us_symbol(code: str) -> str | None:
    upper = normalize_code(code)
    return upper.split(".", 1)[1] if upper.startswith("US.") else None


def market_pack_index(pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for item in pack.get("candidates", []) if isinstance(pack.get("candidates"), list) else []:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").upper()
        if ticker:
            output[ticker] = item
            output[f"US.{ticker}"] = item
    return output


def supply_index(radar: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for item in radar.get("candidates", []) if isinstance(radar.get("candidates"), list) else []:
        if not isinstance(item, dict):
            continue
        code = normalize_code(item.get("code"))
        if code:
            output[code] = item
            symbol = us_symbol(code)
            if symbol:
                output[symbol] = item
    return output


def secondary_index(queue: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    pools = ["deepseek_priority", "active", "retreated", "reviews"]
    for pool in pools:
        rows = queue.get(pool) if isinstance(queue.get(pool), list) else []
        for item in rows:
            if not isinstance(item, dict):
                continue
            code = normalize_code(item.get("code"))
            if code:
                output[code] = item
                symbol = us_symbol(code)
                if symbol:
                    output[symbol] = item
    records = queue.get("records") if isinstance(queue.get("records"), dict) else {}
    for code, item in records.items():
        if isinstance(item, dict):
            normalized = normalize_code(code)
            output[normalized] = item
    return output


def recent_filings(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    sec = candidate.get("sec") if isinstance(candidate.get("sec"), dict) else {}
    raw = sec.get("recent_filings") if isinstance(sec.get("recent_filings"), list) else []
    return [item for item in raw if isinstance(item, dict)]


def latest_filing(candidate: dict[str, Any]) -> dict[str, Any] | None:
    filings = recent_filings(candidate)
    return filings[0] if filings else None


def chart_field(candidate: dict[str, Any], field: str) -> float | None:
    chart = candidate.get("chart") if isinstance(candidate.get("chart"), dict) else {}
    return number(chart.get(field))


def trend_from_candidate(candidate: dict[str, Any]) -> float | None:
    price = number(candidate.get("price"))
    ma50 = chart_field(candidate, "ma50")
    ma200 = chart_field(candidate, "ma200")
    high252 = chart_field(candidate, "high252")
    if price is None:
        return None
    score = 42.0
    if ma50:
        score += 18 if price >= ma50 else -8
    if ma200:
        score += 20 if price >= ma200 else -12
    if high252:
        distance = price / high252 - 1
        if distance >= -0.08:
            score += 12
        elif distance <= -0.30:
            score -= 8
    return round(clamp(score), 1)


def valuation_gap_score(candidate: dict[str, Any]) -> float:
    raw_score = number(candidate.get("valuation_score"))
    if raw_score is not None:
        if -2 <= raw_score <= 2:
            return round(clamp(50 + raw_score * 28), 1)
        return round(clamp(raw_score), 1)

    pe = number(candidate.get("valuation_pe") or candidate.get("forward_pe") or candidate.get("trailing_pe") or candidate.get("estimated_pe_from_sec"))
    revenue_growth = number((candidate.get("sec") or {}).get("revenue_growth_yoy") if isinstance(candidate.get("sec"), dict) else None)
    if pe is None or pe <= 0:
        return 45.0
    if pe <= 15:
        score = 82
    elif pe <= 22:
        score = 72
    elif pe <= 30:
        score = 62
    elif pe <= 45:
        score = 48
    elif pe <= 65:
        score = 34
    else:
        score = 22
    if revenue_growth is not None and revenue_growth >= 0.25 and pe <= 45:
        score += 6
    return round(clamp(score), 1)


def earnings_leverage_score(candidate: dict[str, Any]) -> float:
    sec = candidate.get("sec") if isinstance(candidate.get("sec"), dict) else {}
    revenue_growth = number(sec.get("revenue_growth_yoy"))
    net_margin = number(sec.get("net_margin"))
    liabilities_to_assets = number(sec.get("liabilities_to_assets"))
    score = 45.0
    if revenue_growth is not None:
        score += clamp(revenue_growth * 40, -18, 28)
    if net_margin is not None:
        score += clamp(net_margin * 35, -12, 22)
    if liabilities_to_assets is not None:
        score += clamp((0.65 - liabilities_to_assets) * 18, -10, 10)
    latest_fcf = sec.get("latest_annual_fcf") if isinstance(sec.get("latest_annual_fcf"), dict) else {}
    if number(latest_fcf.get("val")) and number(latest_fcf.get("val")) > 0:
        score += 7
    return round(clamp(score), 1)


def data_confidence_score(candidate: dict[str, Any], supply_item: dict[str, Any]) -> float:
    confidence = number(candidate.get("data_confidence"))
    if confidence is not None:
        return round(clamp(confidence * 100 if confidence <= 1.5 else confidence), 1)
    if number(supply_item.get("price")) is not None:
        return 55.0
    return 35.0


def crowding_score(candidate: dict[str, Any], supply_item: dict[str, Any]) -> float:
    price = number(candidate.get("price") or supply_item.get("price"))
    ma50 = chart_field(candidate, "ma50") or number(supply_item.get("ma50"))
    ma200 = chart_field(candidate, "ma200") or number(supply_item.get("ma200"))
    high252 = chart_field(candidate, "high252") or number(supply_item.get("high252"))
    pe = number(candidate.get("valuation_pe") or candidate.get("forward_pe") or candidate.get("trailing_pe") or candidate.get("estimated_pe_from_sec"))
    score = 22.0
    if price and ma50:
        premium = price / ma50 - 1
        if premium > 0.08:
            score += min(24, premium * 120)
        elif premium < -0.06:
            score -= 6
    if price and ma200:
        premium = price / ma200 - 1
        if premium > 0.30:
            score += min(24, premium * 55)
    if price and high252:
        distance = price / high252 - 1
        if distance > -0.04:
            score += 18
        elif distance < -0.25:
            score -= 8
    if pe:
        if pe > 60:
            score += 22
        elif pe > 40:
            score += 12
        elif pe < 20:
            score -= 4
    return round(clamp(score), 1)


def market_underpricing_score(valuation_gap: float, trend: float | None, crowding: float) -> float:
    trend_component = 55.0 if trend is None else trend
    score = valuation_gap * 0.5 + (100 - crowding) * 0.3 + trend_component * 0.2
    return round(clamp(score), 1)


def security_action(row: dict[str, Any]) -> str:
    market = str(row.get("market") or "")
    score = number(row.get("opportunity_score")) or 0
    underpricing = number(row.get("underpricing_score")) or 0
    crowding = number(row.get("crowding_score")) or 0
    trend = number(row.get("trend_score")) or 0
    data_confidence = number(row.get("data_confidence_score")) or 0
    if data_confidence < 45:
        return "数据不足，补行情/财报后再判断"
    if crowding >= 72 and underpricing < 55:
        return "景气强但拥挤，等待回踩或新催化"
    if market != "US":
        if score >= 72 and trend >= 65:
            return "跨市场二次研究候选，需 Futu/财报/流动性复核"
        return "跨市场观察"
    if score >= 76 and underpricing >= 58 and trend >= 60:
        return "进入机会重点池，交给 Buy-Side 二次分析"
    if score >= 68:
        return "保留观察，等待估值/趋势/催化进一步确认"
    return "普通观察"


def build_security_signal(
    security: dict[str, Any],
    market_index: dict[str, dict[str, Any]],
    supply_by_code: dict[str, dict[str, Any]],
    secondary_by_code: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    code = normalize_code(security.get("code"))
    symbol = us_symbol(code)
    candidate = market_index.get(code) or (market_index.get(symbol) if symbol else None) or {}
    supply_item = supply_by_code.get(code) or (supply_by_code.get(symbol) if symbol else None) or {}
    secondary = secondary_by_code.get(code) or (secondary_by_code.get(symbol) if symbol else None) or {}

    price = number(candidate.get("price") or supply_item.get("price"))
    trend = number(supply_item.get("market_confirmation_score")) or trend_from_candidate(candidate)
    valuation_gap = valuation_gap_score(candidate)
    earnings = earnings_leverage_score(candidate) if candidate else 45.0
    confidence = data_confidence_score(candidate, supply_item)
    crowding = crowding_score(candidate, supply_item)
    underpricing = market_underpricing_score(valuation_gap, trend, crowding)
    trend_component = 55.0 if trend is None else trend
    score = earnings * 0.28 + valuation_gap * 0.22 + trend_component * 0.2 + confidence * 0.15 + (100 - crowding) * 0.15
    latest = latest_filing(candidate)

    row = {
        "code": code,
        "symbol": symbol,
        "market": security.get("market") or code_market(code),
        "name": security.get("name") or candidate.get("name") or supply_item.get("name") or code,
        "layer": security.get("layer") or supply_item.get("layer_name"),
        "role": security.get("role") or supply_item.get("role"),
        "price": price,
        "valuation_pe": candidate.get("valuation_pe") or candidate.get("forward_pe") or candidate.get("trailing_pe") or candidate.get("estimated_pe_from_sec"),
        "valuation_pe_source": candidate.get("valuation_pe_source") or candidate.get("valuation_source"),
        "reward_risk": candidate.get("reward_risk"),
        "buyable_now": bool(candidate.get("buyable_now")),
        "trend_score": round(trend_component, 1),
        "valuation_gap_score": valuation_gap,
        "earnings_leverage_score": earnings,
        "underpricing_score": underpricing,
        "data_confidence_score": confidence,
        "crowding_score": crowding,
        "opportunity_score": round(clamp(score), 1),
        "recent_filing": latest,
        "secondary_status": secondary.get("status") or secondary.get("review_result") or secondary.get("last_result"),
        "source": supply_item.get("data_status") or candidate.get("quote_source") or "theme map only",
    }
    row["action"] = security_action(row)
    return row


def weighted_theme_score(theme: dict[str, Any], securities: list[dict[str, Any]], weights: dict[str, float]) -> dict[str, float]:
    if securities:
        top = sorted(securities, key=lambda item: number(item.get("opportunity_score")) or 0, reverse=True)[:5]
        earnings = sum(number(item.get("earnings_leverage_score")) or 45 for item in top) / len(top)
        underpricing = sum(number(item.get("underpricing_score")) or 45 for item in top) / len(top)
        confidence = sum(number(item.get("data_confidence_score")) or 35 for item in top) / len(top)
        crowding = sum(number(item.get("crowding_score")) or 30 for item in top) / len(top)
    else:
        earnings, underpricing, confidence, crowding = 45.0, 45.0, 35.0, 30.0

    components = {
        "demand_shift": float(theme.get("demand_shift_score", 50)),
        "supply_constraint": float(theme.get("supply_constraint_score", 50)),
        "earnings_leverage": round(earnings, 1),
        "market_underpricing": round(underpricing, 1),
        "catalyst_timing": float(theme.get("catalyst_timing_score", 50)),
        "data_confidence": round(confidence, 1),
        "crowding_penalty": round(crowding, 1),
    }
    total = (
        components["demand_shift"] * weights.get("demand_shift", 0.22)
        + components["supply_constraint"] * weights.get("supply_constraint", 0.14)
        + components["earnings_leverage"] * weights.get("earnings_leverage", 0.18)
        + components["market_underpricing"] * weights.get("market_underpricing", 0.18)
        + components["catalyst_timing"] * weights.get("catalyst_timing", 0.12)
        + components["data_confidence"] * weights.get("data_confidence", 0.1)
        - components["crowding_penalty"] * weights.get("crowding_penalty", 0.06)
    )
    components["expectation_gap_score"] = round(clamp(total), 1)
    return components


def theme_stage(components: dict[str, float], thresholds: dict[str, Any]) -> str:
    score = components.get("expectation_gap_score", 0)
    underpricing = components.get("market_underpricing", 0)
    crowding = components.get("crowding_penalty", 0)
    if crowding >= float(thresholds.get("crowded_penalty", 65)) and underpricing < 55:
        return "景气拥挤"
    if score >= float(thresholds.get("expectation_gap", 78)) and underpricing >= 55:
        return "预期差机会"
    if score >= float(thresholds.get("confirming", 68)):
        return "确认中"
    return "观察跟踪"


def compare_security_state(
    security: dict[str, Any],
    previous: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    code = str(security.get("code") or "")
    prev = previous.get(code) if isinstance(previous.get(code), dict) else {}
    filing = security.get("recent_filing") if isinstance(security.get("recent_filing"), dict) else None
    accession = filing.get("accession") if filing else None
    prev_accession = prev.get("latest_filing_accession")
    if accession and accession != prev_accession:
        changes.append(
            {
                "type": "new_filing",
                "code": code,
                "name": security.get("name"),
                "detected_at": iso(now),
                "form": filing.get("form"),
                "filed": filing.get("filed"),
                "accession": accession,
                "previous_accession": prev_accession,
                "note": "SEC 最新申报发生变化，需要检查是否改变业绩、订单、资本开支或风险假设。",
            }
        )

    metric_checks = [
        ("price", "价格"),
        ("valuation_pe", "估值 PE"),
        ("opportunity_score", "个股机会分"),
        ("trend_score", "趋势确认"),
    ]
    for field, label in metric_checks:
        current_value = number(security.get(field))
        previous_value = number(prev.get(field))
        if current_value is None or previous_value is None or previous_value == 0:
            continue
        change = current_value / previous_value - 1
        threshold = 0.12 if field == "price" else 0.18
        if abs(change) >= threshold:
            changes.append(
                {
                    "type": "metric_change",
                    "code": code,
                    "name": security.get("name"),
                    "detected_at": iso(now),
                    "metric": field,
                    "label": label,
                    "previous": round(previous_value, 4),
                    "current": round(current_value, 4),
                    "change_pct": round(change * 100, 2),
                    "note": f"{label}变化超过阈值，需确认是机会扩大、逻辑兑现还是风险暴露。",
                }
            )
    return changes


def update_journal(
    state: dict[str, Any],
    themes: list[dict[str, Any]],
    security_changes: list[dict[str, Any]],
    checkpoint_days: list[int],
    now: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    opportunities = state.get("opportunities") if isinstance(state.get("opportunities"), dict) else {}
    securities_state = state.get("securities") if isinstance(state.get("securities"), dict) else {}
    completed_reviews: list[dict[str, Any]] = []
    review_due: list[dict[str, Any]] = []

    for theme in themes:
        theme_id = str(theme.get("id"))
        record = opportunities.get(theme_id) if isinstance(opportunities.get(theme_id), dict) else {}
        first_seen = parse_time(record.get("first_seen_at")) or now
        previous_score = number(record.get("last_score"))
        current_score = number(theme.get("expectation_gap_score")) or 0.0
        checkpoints = record.get("checkpoints") if isinstance(record.get("checkpoints"), list) else []
        if not checkpoints:
            checkpoints = [
                {
                    "days": int(days),
                    "due_at": iso(first_seen + timedelta(days=int(days))),
                    "status": "pending",
                }
                for days in checkpoint_days
            ]

        initial_prices = record.get("initial_prices") if isinstance(record.get("initial_prices"), dict) else {}
        if not initial_prices:
            initial_prices = {
                str(item.get("code")): item.get("price")
                for item in theme.get("securities", [])[:10]
                if number(item.get("price")) is not None
            }

        for checkpoint in checkpoints:
            if checkpoint.get("status") == "completed":
                continue
            due_at = parse_time(checkpoint.get("due_at"))
            if due_at and due_at <= now:
                current_prices = {
                    str(item.get("code")): number(item.get("price"))
                    for item in theme.get("securities", [])[:10]
                    if number(item.get("price")) is not None
                }
                price_changes = []
                for code, start_price in initial_prices.items():
                    start = number(start_price)
                    current = current_prices.get(code)
                    if start and current:
                        price_changes.append(current / start - 1)
                avg_price_change = sum(price_changes) / len(price_changes) if price_changes else None
                score_delta = current_score - (number(record.get("initial_score")) or current_score)
                crowding = number(theme.get("score_components", {}).get("crowding_penalty")) if isinstance(theme.get("score_components"), dict) else None
                if score_delta >= 5:
                    result = "逻辑增强"
                elif score_delta <= -8:
                    result = "逻辑转弱"
                elif avg_price_change is not None and avg_price_change >= 0.2 and crowding and crowding >= 65:
                    result = "价格可能已透支"
                else:
                    result = "继续验证"
                review = {
                    "theme_id": theme_id,
                    "theme_name": theme.get("name"),
                    "checkpoint_days": checkpoint.get("days"),
                    "reviewed_at": iso(now),
                    "result": result,
                    "initial_score": record.get("initial_score"),
                    "current_score": current_score,
                    "score_delta": round(score_delta, 1),
                    "avg_price_change_pct": round(avg_price_change * 100, 2) if avg_price_change is not None else None,
                    "note": "复盘只评价机会发现质量，不等同于买卖结果。",
                }
                checkpoint["status"] = "completed"
                checkpoint["reviewed_at"] = iso(now)
                checkpoint["result"] = result
                completed_reviews.append(review)
            elif due_at:
                review_due.append(
                    {
                        "theme_id": theme_id,
                        "theme_name": theme.get("name"),
                        "checkpoint_days": checkpoint.get("days"),
                        "due_at": checkpoint.get("due_at"),
                        "status": checkpoint.get("status"),
                    }
                )

        history = record.get("history") if isinstance(record.get("history"), list) else []
        history.append(
            {
                "at": iso(now),
                "score": current_score,
                "stage": theme.get("stage"),
                "top_candidates": [item.get("code") for item in theme.get("securities", [])[:5]],
            }
        )
        record.update(
            {
                "id": theme_id,
                "name": theme.get("name"),
                "first_seen_at": iso(first_seen),
                "last_seen_at": iso(now),
                "initial_score": record.get("initial_score", current_score),
                "previous_score": previous_score,
                "last_score": current_score,
                "stage": theme.get("stage"),
                "initial_prices": initial_prices,
                "checkpoints": checkpoints,
                "history": history[-40:],
            }
        )
        opportunities[theme_id] = record

        for item in theme.get("securities", []):
            code = str(item.get("code") or "")
            if not code:
                continue
            previous = securities_state.get(code) if isinstance(securities_state.get(code), dict) else {}
            filing = item.get("recent_filing") if isinstance(item.get("recent_filing"), dict) else {}
            securities_state[code] = {
                "code": code,
                "name": item.get("name"),
                "first_seen_at": previous.get("first_seen_at") or iso(now),
                "last_seen_at": iso(now),
                "latest_filing_accession": filing.get("accession"),
                "latest_filing_form": filing.get("form"),
                "latest_filing_date": filing.get("filed"),
                "price": item.get("price"),
                "valuation_pe": item.get("valuation_pe"),
                "opportunity_score": item.get("opportunity_score"),
                "trend_score": item.get("trend_score"),
            }

    next_state = {
        "schema_version": 1,
        "generated_at": iso(now),
        "generated_label": now.strftime("%Y-%m-%d %H:%M"),
        "opportunities": dict(sorted(opportunities.items())),
        "securities": dict(sorted(securities_state.items())),
        "latest_changes": security_changes[:80],
        "latest_reviews": completed_reviews[:40],
    }
    review_due.sort(key=lambda item: str(item.get("due_at") or ""))
    return next_state, review_due[:80], completed_reviews[:40]


def build_radar(
    config: dict[str, Any],
    market_pack: dict[str, Any],
    supply_radar: dict[str, Any],
    secondary_queue: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    now = now_local()
    market_index = market_pack_index(market_pack)
    supply_by_code = supply_index(supply_radar)
    secondary_by_code = secondary_index(secondary_queue)
    weights = config.get("default_weights") if isinstance(config.get("default_weights"), dict) else {}
    thresholds = config.get("stage_thresholds") if isinstance(config.get("stage_thresholds"), dict) else {}
    previous_security_state = state.get("securities") if isinstance(state.get("securities"), dict) else {}

    themes: list[dict[str, Any]] = []
    all_security_changes: list[dict[str, Any]] = []
    for theme in config.get("themes", []) if isinstance(config.get("themes"), list) else []:
        if not isinstance(theme, dict):
            continue
        securities = [
            build_security_signal(item, market_index, supply_by_code, secondary_by_code)
            for item in theme.get("securities", [])
            if isinstance(item, dict) and normalize_code(item.get("code"))
        ]
        securities.sort(key=lambda item: number(item.get("opportunity_score")) or 0, reverse=True)
        components = weighted_theme_score(theme, securities, weights)
        stage = theme_stage(components, thresholds)
        changes: list[dict[str, Any]] = []
        for item in securities:
            changes.extend(compare_security_state(item, previous_security_state, now))
        all_security_changes.extend(changes)
        top_evidence = [
            f"{item.get('code')}：机会分 {fmt_num(item.get('opportunity_score'))}，{item.get('action')}"
            for item in securities[:3]
        ]
        themes.append(
            {
                "id": theme.get("id"),
                "name": theme.get("name"),
                "horizon": theme.get("horizon"),
                "stage": stage,
                "expectation_gap_score": components["expectation_gap_score"],
                "score_components": components,
                "thesis": theme.get("thesis"),
                "leading_indicators": theme.get("leading_indicators", []),
                "catalysts": theme.get("catalysts", []),
                "keywords": theme.get("keywords", []),
                "beneficiary_layers": theme.get("beneficiary_layers", []),
                "top_evidence": top_evidence,
                "changes": changes[:20],
                "securities": securities,
            }
        )

    themes.sort(key=lambda item: number(item.get("expectation_gap_score")) or 0, reverse=True)
    checkpoint_days = config.get("review_checkpoints_days") if isinstance(config.get("review_checkpoints_days"), list) else [30, 60, 90]
    next_state, review_due, completed_reviews = update_journal(state, themes, all_security_changes, [int(x) for x in checkpoint_days], now)
    secondary_candidates = [
        {
            "theme_id": theme.get("id"),
            "theme_name": theme.get("name"),
            "code": item.get("code"),
            "name": item.get("name"),
            "market": item.get("market"),
            "layer": item.get("layer"),
            "price": item.get("price"),
            "opportunity_score": item.get("opportunity_score"),
            "underpricing_score": item.get("underpricing_score"),
            "trend_score": item.get("trend_score"),
            "crowding_score": item.get("crowding_score"),
            "action": item.get("action"),
        }
        for theme in themes
        for item in theme.get("securities", [])
        if "二次" in str(item.get("action")) or "重点池" in str(item.get("action"))
    ]
    secondary_candidates.sort(key=lambda item: number(item.get("opportunity_score")) or 0, reverse=True)

    summary = {
        "theme_count": len(themes),
        "expectation_gap_count": sum(1 for item in themes if item.get("stage") == "预期差机会"),
        "confirming_count": sum(1 for item in themes if item.get("stage") == "确认中"),
        "crowded_count": sum(1 for item in themes if item.get("stage") == "景气拥挤"),
        "security_change_count": len(all_security_changes),
        "secondary_candidate_count": len(secondary_candidates),
        "completed_review_count": len(completed_reviews),
        "next_review_count": len(review_due),
    }
    payload = {
        "schema_version": 1,
        "generated_at": iso(now),
        "generated_label": now.strftime("%Y-%m-%d %H:%M"),
        "data_boundary": {
            "role": "opportunity discovery, not trading instruction",
            "buy_side_gate": "任何股票买入必须回到 Buy-Side 分析、R/R >= 2:1、估值纪律和复星证券整股约束。",
            "transcripts": "暂未接入财报电话会或新闻逐字稿 API；变化检测当前使用 SEC 申报、价格、估值、趋势和机会分变化。",
        },
        "phases": {
            "phase_1": "预期差评分：需求变化、供应约束、盈利弹性、低估程度、催化时点、数据置信度、拥挤度。",
            "phase_2": "产业链扩散：从主题映射到美股、港股、A股受益环节。",
            "phase_3": "变化检测：识别新申报、价格/估值/趋势/机会分的显著变化。",
            "phase_4": "复盘闭环：记录首次发现、30/60/90 天检查点和结果。",
        },
        "summary": summary,
        "top_opportunities": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "stage": item.get("stage"),
                "score": item.get("expectation_gap_score"),
                "beneficiary_layers": item.get("beneficiary_layers"),
                "top_candidates": [
                    {
                        "code": sec.get("code"),
                        "name": sec.get("name"),
                        "market": sec.get("market"),
                        "score": sec.get("opportunity_score"),
                        "action": sec.get("action"),
                    }
                    for sec in item.get("securities", [])[:5]
                ],
            }
            for item in themes[:8]
        ],
        "themes": themes,
        "filing_changes": [item for item in all_security_changes if item.get("type") == "new_filing"][:80],
        "metric_changes": [item for item in all_security_changes if item.get("type") == "metric_change"][:80],
        "review_due": review_due,
        "completed_reviews": completed_reviews,
        "secondary_candidates": secondary_candidates[:80],
        "data_sources": [
            "config/opportunity_map.json opportunity map",
            "data/latest_market_pack.json public US candidate pack",
            "data/latest_supply_chain_radar.json cross-market supply-chain radar when available",
            "docs/data/secondary_analysis_queue.json secondary-analysis lifecycle when available",
            "docs/data/opportunity_journal.json opportunity memory and review checkpoints",
        ],
        "discipline": [
            "机会雷达只解决提前发现问题，不解决买入价格和仓位问题。",
            "主题分数高但拥挤度高时，优先等待回踩、业绩兑现或新催化，而不是追高。",
            "港股/A股候选必须单独用对应市场行情、财报、流动性和交易规则复核。",
            "所有可交易结论必须回到 Buy-Side 分析和 R/R >= 2:1。",
        ],
    }
    return payload, next_state


def render_report(radar: dict[str, Any]) -> str:
    lines: list[str] = [
        "# 机会发现雷达",
        "",
        f"- 生成时间：{radar.get('generated_label')}",
        "- 定位：提前发现可能被市场低估的未来机会；不构成买入建议。",
        "- 硬约束：最终交易必须回到 Buy-Side 分析、R/R >= 2:1、估值纪律和复星证券整股执行。",
        "",
        "## 本轮结论",
        "",
    ]
    summary = radar.get("summary") if isinstance(radar.get("summary"), dict) else {}
    lines.append(
        f"- 主题数：{summary.get('theme_count', 0)}；预期差机会：{summary.get('expectation_gap_count', 0)}；确认中：{summary.get('confirming_count', 0)}；景气拥挤：{summary.get('crowded_count', 0)}。"
    )
    lines.append(
        f"- 变化检测：{summary.get('security_change_count', 0)} 条；二次研究候选：{summary.get('secondary_candidate_count', 0)} 个；本轮完成复盘：{summary.get('completed_review_count', 0)} 个。"
    )

    lines.extend(["", "## 主题机会总览", ""])
    lines.extend(["| 主题 | 阶段 | 预期差分 | 低估/错配 | 拥挤度 | 关键证据 | 下一步验证 |", "|---|---|---:|---:|---:|---|---|"])
    for theme in radar.get("themes", [])[:10]:
        components = theme.get("score_components") if isinstance(theme.get("score_components"), dict) else {}
        evidence = "<br>".join(theme.get("top_evidence", [])[:3])
        indicators = "；".join(theme.get("leading_indicators", [])[:2])
        lines.append(
            f"| {theme.get('name')} | {theme.get('stage')} | {fmt_num(theme.get('expectation_gap_score'))} | {fmt_num(components.get('market_underpricing'))} | {fmt_num(components.get('crowding_penalty'))} | {evidence} | {indicators} |"
        )

    lines.extend(["", "## 预期差评分拆解", ""])
    lines.extend(["| 主题 | 需求变化 | 供应约束 | 盈利弹性 | 市场错配 | 催化时点 | 数据置信 | 拥挤扣分 |", "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for theme in radar.get("themes", [])[:10]:
        components = theme.get("score_components") if isinstance(theme.get("score_components"), dict) else {}
        lines.append(
            f"| {theme.get('name')} | {fmt_num(components.get('demand_shift'))} | {fmt_num(components.get('supply_constraint'))} | {fmt_num(components.get('earnings_leverage'))} | {fmt_num(components.get('market_underpricing'))} | {fmt_num(components.get('catalyst_timing'))} | {fmt_num(components.get('data_confidence'))} | {fmt_num(components.get('crowding_penalty'))} |"
        )

    for theme in radar.get("themes", [])[:6]:
        lines.extend(["", f"## {theme.get('name')}", ""])
        lines.append(str(theme.get("thesis") or ""))
        lines.extend(["", "### 领先指标", ""])
        for item in theme.get("leading_indicators", [])[:6]:
            lines.append(f"- {item}")
        lines.extend(["", "### 重点股票", ""])
        lines.extend(["| 市场 | 代码 | 名称 | 环节 | 价格 | PE | 机会分 | 趋势 | 低估/错配 | 拥挤 | 动作 |", "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|"])
        for item in theme.get("securities", [])[:12]:
            lines.append(
                f"| {item.get('market')} | {item.get('code')} | {item.get('name')} | {item.get('layer')} | {fmt_price(item.get('price'))} | {fmt_num(item.get('valuation_pe'))} | {fmt_num(item.get('opportunity_score'))} | {fmt_num(item.get('trend_score'))} | {fmt_num(item.get('underpricing_score'))} | {fmt_num(item.get('crowding_score'))} | {item.get('action')} |"
            )

    filing_changes = radar.get("filing_changes") if isinstance(radar.get("filing_changes"), list) else []
    metric_changes = radar.get("metric_changes") if isinstance(radar.get("metric_changes"), list) else []
    lines.extend(["", "## 财报/申报与关键变化检测", ""])
    if not filing_changes and not metric_changes:
        lines.append("本轮没有检测到相对上次机会雷达的显著申报或指标变化。")
    else:
        if filing_changes:
            lines.extend(["| 类型 | 代码 | 名称 | 表格 | 日期 | 说明 |", "|---|---|---|---|---|---|"])
            for item in filing_changes[:20]:
                lines.append(f"| 新申报 | {item.get('code')} | {item.get('name')} | {item.get('form')} | {item.get('filed')} | {item.get('note')} |")
        if metric_changes:
            lines.extend(["", "| 指标变化 | 代码 | 名称 | 原值 | 新值 | 变化 | 说明 |", "|---|---|---|---:|---:|---:|---|"])
            for item in metric_changes[:20]:
                lines.append(
                    f"| {item.get('label')} | {item.get('code')} | {item.get('name')} | {fmt_num(item.get('previous'))} | {fmt_num(item.get('current'))} | {fmt_num(item.get('change_pct'))}% | {item.get('note')} |"
                )
    lines.extend(["", "> 电话会/新闻逐字稿：暂未接入专门 API；目前以 SEC 申报、行情、估值和评分变化做第一层变化检测。"])

    reviews = radar.get("completed_reviews") if isinstance(radar.get("completed_reviews"), list) else []
    due = radar.get("review_due") if isinstance(radar.get("review_due"), list) else []
    lines.extend(["", "## 30/60/90 天复盘闭环", ""])
    if reviews:
        lines.extend(["| 主题 | 检查点 | 结果 | 初始分 | 当前分 | 价格变化 | 说明 |", "|---|---:|---|---:|---:|---:|---|"])
        for item in reviews[:20]:
            price_change = "数据不足" if item.get("avg_price_change_pct") is None else f"{item.get('avg_price_change_pct'):.1f}%"
            lines.append(
                f"| {item.get('theme_name')} | {item.get('checkpoint_days')}天 | {item.get('result')} | {fmt_num(item.get('initial_score'))} | {fmt_num(item.get('current_score'))} | {price_change} | {item.get('note')} |"
            )
    else:
        lines.append("本轮没有到期复盘。")
    if due:
        lines.extend(["", "### 后续待复盘", ""])
        lines.extend(["| 主题 | 检查点 | 到期时间 | 状态 |", "|---|---:|---|---|"])
        for item in due[:12]:
            due_at = parse_time(item.get("due_at"))
            label = due_at.strftime("%Y-%m-%d %H:%M") if due_at else str(item.get("due_at") or "")
            lines.append(f"| {item.get('theme_name')} | {item.get('checkpoint_days')}天 | {label} | {item.get('status')} |")

    candidates = radar.get("secondary_candidates") if isinstance(radar.get("secondary_candidates"), list) else []
    lines.extend(["", "## 进入二次研究候选", ""])
    if not candidates:
        lines.append("本轮没有达到二次研究门槛的新增候选。")
    else:
        lines.extend(["| 市场 | 代码 | 名称 | 主题 | 价格 | 机会分 | 趋势 | 拥挤 | 动作 |", "|---|---|---|---|---:|---:|---:|---:|---|"])
        for item in candidates[:30]:
            lines.append(
                f"| {item.get('market')} | {item.get('code')} | {item.get('name')} | {item.get('theme_name')} | {fmt_price(item.get('price'))} | {fmt_num(item.get('opportunity_score'))} | {fmt_num(item.get('trend_score'))} | {fmt_num(item.get('crowding_score'))} | {item.get('action')} |"
            )

    lines.extend(["", "## 执行纪律", ""])
    for item in radar.get("discipline", []):
        lines.append(f"- {item}")
    return "\n".join(lines).strip() + "\n"


def archive_copy(report_path: Path) -> Path:
    timestamp = now_local().strftime("%Y%m%d-%H%M")
    archive = report_path.with_name(f"opportunity-radar-{timestamp}.md")
    archive.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--market-pack", type=Path, default=DEFAULT_MARKET_PACK)
    parser.add_argument("--supply-radar", type=Path, default=DEFAULT_SUPPLY_RADAR)
    parser.add_argument("--secondary-queue", type=Path, default=DEFAULT_SECONDARY_QUEUE)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--docs-out", type=Path, default=DEFAULT_DOCS_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--no-archive-copy", action="store_true")
    args = parser.parse_args()

    config = load_json(args.config, {})
    if not config:
        raise SystemExit(f"Opportunity map not found or invalid: {args.config}")
    market_pack = load_json(args.market_pack, {})
    supply_radar = load_json(args.supply_radar, {})
    secondary_queue = load_json(args.secondary_queue, {})
    state = load_json(args.state, {})

    radar, next_state = build_radar(config, market_pack, supply_radar, secondary_queue, state)
    write_json(args.out, radar)
    write_json(args.docs_out, radar)
    write_json(args.state, next_state)
    write_text(args.report, render_report(radar))
    if not args.no_archive_copy:
        archive = archive_copy(args.report)
        print(f"Wrote {archive}")
    print(f"Wrote {args.out}")
    print(f"Wrote {args.docs_out}")
    print(f"Wrote {args.state}")
    print(f"Wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
