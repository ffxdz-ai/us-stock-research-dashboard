#!/usr/bin/env python3
"""Build a cross-market industry-chain intelligence layer.

This layer sits above supply_chain_radar and opportunity_radar. It answers:

- Which themes show demand acceleration instead of only high static scores?
- Which supply-chain layers are spreading across US/HK/CN markets?
- Which names deserve secondary-analysis follow-up?
- What evidence gaps must be filled by future news/transcript/filing extraction?

It is deliberately not a trading engine. It only routes research attention.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from model_v2 import load_risk_policy, risk_policy_public


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DOCS_DATA_DIR = ROOT / "docs" / "data"
REPORTS_DIR = ROOT / "reports"

DEFAULT_SUPPLY_RADAR = DATA_DIR / "latest_supply_chain_radar.json"
DEFAULT_OPPORTUNITY_RADAR = DATA_DIR / "latest_opportunity_radar.json"
DEFAULT_SECONDARY_QUEUE = DOCS_DATA_DIR / "secondary_analysis_queue.json"
DEFAULT_OPPORTUNITY_JOURNAL = DOCS_DATA_DIR / "opportunity_journal.json"
DEFAULT_MARKET_PACK = DATA_DIR / "latest_market_pack.json"
DEFAULT_FMP_RESEARCH = DATA_DIR / "latest_fmp_research.json"
DEFAULT_OUTPUT = DATA_DIR / "latest_cross_market_intelligence.json"
DEFAULT_DOCS_OUTPUT = DOCS_DATA_DIR / "cross_market_intelligence.json"
DEFAULT_REPORT = REPORTS_DIR / "latest-cross-market-intelligence.md"


def beijing_timezone() -> timezone:
    try:
        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return timezone(timedelta(hours=8), "Asia/Shanghai")


def now_local() -> datetime:
    return datetime.now(beijing_timezone())


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
        cleaned = value.replace(",", "").replace("$", "").replace("%", "").strip()
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


def fmt_num(value: Any, digits: int = 1) -> str:
    parsed = number(value)
    return "数据不足" if parsed is None else f"{parsed:.{digits}f}"


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


def market_label(market: str) -> str:
    return {"US": "美股", "HK": "港股", "CN": "A股"}.get(market, market)


def us_symbol(code: str) -> str | None:
    upper = normalize_code(code)
    return upper.split(".", 1)[1] if upper.startswith("US.") else None


def index_supply_candidates(supply_radar: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    rows = supply_radar.get("candidates") if isinstance(supply_radar.get("candidates"), list) else []
    for item in rows:
        if not isinstance(item, dict):
            continue
        code = normalize_code(item.get("code"))
        if not code:
            continue
        output[code] = item
        symbol = us_symbol(code)
        if symbol:
            output[symbol] = item
    return output


def index_market_pack(market_pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    rows = market_pack.get("candidates") if isinstance(market_pack.get("candidates"), list) else []
    for item in rows:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").upper()
        if ticker:
            output[ticker] = item
            output[f"US.{ticker}"] = item
    return output


def index_secondary_queue(queue: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    records = queue.get("records") if isinstance(queue.get("records"), dict) else {}
    for code, item in records.items():
        if isinstance(item, dict):
            output[normalize_code(code)] = item
    for pool in ("deepseek_priority", "active", "reviews", "retreated"):
        rows = queue.get(pool) if isinstance(queue.get(pool), list) else []
        for item in rows:
            if isinstance(item, dict):
                code = normalize_code(item.get("code"))
                if code:
                    output[code] = item
    return output


def journal_security_state(journal: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = journal.get("securities") if isinstance(journal.get("securities"), dict) else {}
    return {normalize_code(code): item for code, item in rows.items() if isinstance(item, dict)}


def security_signal(
    security: dict[str, Any],
    supply_by_code: dict[str, dict[str, Any]],
    market_by_code: dict[str, dict[str, Any]],
    secondary_by_code: dict[str, dict[str, Any]],
    journal_by_code: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    code = normalize_code(security.get("code"))
    supply = supply_by_code.get(code) or supply_by_code.get(us_symbol(code) or "") or {}
    market_row = market_by_code.get(code) or market_by_code.get(us_symbol(code) or "") or {}
    secondary = secondary_by_code.get(code) or {}
    journal = journal_by_code.get(code) or {}

    price = number(security.get("price")) or number(supply.get("price")) or number(market_row.get("price"))
    trend = number(security.get("trend_score")) or number(supply.get("market_confirmation_score"))
    opportunity = number(security.get("opportunity_score"))
    supply_layer = number(supply.get("layer_score"))
    underpricing = number(security.get("underpricing_score"))
    crowding = number(security.get("crowding_score"))
    valuation = number(security.get("valuation_pe")) or number(market_row.get("valuation_pe"))
    rr = number(security.get("reward_risk")) or number(market_row.get("reward_risk"))
    starter_entry = number(security.get("starter_entry")) or number(market_row.get("starter_entry"))
    starter_stop = number(security.get("starter_stop")) or number(market_row.get("starter_stop"))
    starter_target = number(security.get("starter_target")) or number(market_row.get("starter_target"))
    starter_reward_risk = number(security.get("starter_reward_risk")) or number(market_row.get("starter_reward_risk"))
    breakout_trigger = number(security.get("breakout_trigger")) or number(market_row.get("breakout_trigger"))
    breakout_stop = number(security.get("breakout_stop")) or number(market_row.get("breakout_stop"))
    breakout_target = number(security.get("breakout_target")) or number(market_row.get("breakout_target"))
    breakout_reward_risk = number(security.get("breakout_reward_risk")) or number(market_row.get("breakout_reward_risk"))

    historical_first = journal.get("first_seen_at") or journal.get("first_seen")
    previous_score = number(journal.get("last_opportunity_score"))
    score_delta = round(opportunity - previous_score, 1) if opportunity is not None and previous_score is not None else None

    action_parts: list[str] = []
    if secondary:
        action_parts.append("已在二次分析生命周期中")
    if opportunity is not None and opportunity >= 76 and (crowding is None or crowding < 72):
        action_parts.append("交给 Buy-Side 二次分析")
    elif trend is not None and trend >= 78 and (opportunity is None or opportunity < 72):
        action_parts.append("趋势强，等待基本面/估值确认")
    elif supply_layer is not None and supply_layer >= 78 and (trend is None or trend < 58):
        action_parts.append("产业强但价格未确认")
    else:
        action_parts.append("观察")

    return {
        "code": code,
        "market": security.get("market") or code_market(code),
        "name": security.get("name"),
        "layer": security.get("layer") or supply.get("layer_name"),
        "role": security.get("role") or supply.get("role"),
        "price": price,
        "quote_time": security.get("quote_time") or market_row.get("quote_time") or supply.get("quote_time"),
        "quote_source": security.get("quote_source") or market_row.get("quote_source") or supply.get("quote_source"),
        "valuation_pe": valuation,
        "reward_risk": rr,
        "starter_entry": starter_entry,
        "starter_stop": starter_stop,
        "starter_target": starter_target,
        "starter_reward_risk": starter_reward_risk,
        "breakout_trigger": breakout_trigger,
        "breakout_stop": breakout_stop,
        "breakout_target": breakout_target,
        "breakout_reward_risk": breakout_reward_risk,
        "opportunity_score": opportunity,
        "entry_score": security.get("entry_score") or market_row.get("entry_score"),
        "supply_layer_score": supply_layer,
        "trend_score": trend,
        "underpricing_score": underpricing,
        "crowding_score": crowding,
        "data_confidence": security.get("data_confidence") or market_row.get("data_confidence"),
        "price_freshness": security.get("price_freshness") or market_row.get("price_freshness") or supply.get("price_freshness"),
        "execution_allowed": security.get("execution_allowed") is True or market_row.get("execution_allowed") is True or supply.get("execution_allowed") is True,
        "technical_data_complete": security.get("technical_data_complete") is True or market_row.get("technical_data_complete") is True or supply.get("technical_data_complete") is True,
        "future_function_audit": security.get("future_function_audit") or market_row.get("future_function_audit") or supply.get("future_function_audit"),
        "gate_failures": security.get("gate_failures") or market_row.get("gate_failures") or supply.get("gate_failures") or [],
        "alpha_percentile": security.get("alpha_percentile") or market_row.get("alpha_percentile"),
        "sector_rank_percentile": security.get("sector_rank_percentile") or market_row.get("sector_rank_percentile"),
        "universe_rank": security.get("universe_rank") or market_row.get("universe_rank"),
        "factor_coverage": security.get("factor_coverage") or market_row.get("factor_coverage"),
        "score_delta": score_delta,
        "first_seen_at": historical_first,
        "secondary_status": secondary.get("status") or ("tracked" if secondary else None),
        "data_status": supply.get("data_status") or security.get("data_status"),
        "action": "；".join(action_parts),
    }


def avg(values: list[float]) -> float | None:
    clean = [value for value in values if math.isfinite(value)]
    return round(sum(clean) / len(clean), 1) if clean else None


def market_breadth_score(securities: list[dict[str, Any]]) -> tuple[float, dict[str, Any]]:
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in securities:
        by_market[str(item.get("market") or code_market(str(item.get("code") or "")))].append(item)
    active_markets = []
    for market, rows in by_market.items():
        if any((number(row.get("trend_score")) or 0) >= 65 or (number(row.get("opportunity_score")) or 0) >= 70 for row in rows):
            active_markets.append(market)
    strong_count = sum(1 for item in securities if (number(item.get("opportunity_score")) or 0) >= 72)
    score = clamp(35 + len(active_markets) * 18 + min(20, strong_count * 3))
    return round(score, 1), {
        "active_markets": active_markets,
        "active_market_count": len(active_markets),
        "strong_security_count": strong_count,
        "market_counts": {market: len(rows) for market, rows in by_market.items()},
    }


def change_signal_score(theme: dict[str, Any], securities: list[dict[str, Any]]) -> float:
    changes = theme.get("changes") if isinstance(theme.get("changes"), list) else []
    score = min(16, len(changes) * 4)
    deltas = [number(item.get("score_delta")) for item in securities]
    positive_deltas = [value for value in deltas if value is not None and value >= 3]
    score += min(14, len(positive_deltas) * 4)
    return round(score, 1)


def theme_intelligence(
    theme: dict[str, Any],
    supply_by_code: dict[str, dict[str, Any]],
    market_by_code: dict[str, dict[str, Any]],
    secondary_by_code: dict[str, dict[str, Any]],
    journal_by_code: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    securities = [
        security_signal(item, supply_by_code, market_by_code, secondary_by_code, journal_by_code)
        for item in theme.get("securities", [])
        if isinstance(item, dict) and normalize_code(item.get("code"))
    ]
    breadth_score, breadth = market_breadth_score(securities)
    components = theme.get("score_components") if isinstance(theme.get("score_components"), dict) else {}
    theme_score = number(theme.get("expectation_gap_score") or components.get("final_theme_score"))
    base_score = number(components.get("base_score"))
    dynamic_center = number(components.get("dynamic_center"))
    earnings = number(components.get("earnings_leverage"))
    revision = number(components.get("earnings_revision"))
    underpricing = number(components.get("market_underpricing"))
    crowding_score = number(components.get("crowding_score"))
    crowding_penalty = number(components.get("crowding_penalty"))
    data_confidence = number(components.get("data_confidence"))
    trend_values = [value for item in securities if (value := number(item.get("trend_score"))) is not None]
    trend_avg = avg(trend_values)
    change_score = change_signal_score(theme, securities)
    acceleration_inputs = {
        "theme_score": (theme_score, 0.30),
        "dynamic_center": (dynamic_center, 0.10),
        "earnings_leverage": (earnings, 0.10),
        "earnings_revision": (revision, 0.15),
        "market_underpricing": (underpricing, 0.10),
        "trend_avg": (trend_avg, 0.10),
        "market_breadth": (breadth_score if securities else None, 0.10),
        "change_signal": (change_score if securities else None, 0.025),
        "data_confidence": (data_confidence, 0.025),
    }
    available_weight = sum(weight for value, weight in acceleration_inputs.values() if value is not None)
    raw_acceleration = (
        sum(float(value) * weight for value, weight in acceleration_inputs.values() if value is not None) / available_weight
        if available_weight
        else None
    )
    acceleration_coverage = available_weight
    # Missing evidence lowers conviction instead of silently becoming a
    # neutral 50.  A static narrative prior alone cannot imply acceleration.
    acceleration = (
        clamp(raw_acceleration * (0.45 + 0.55 * acceleration_coverage))
        if raw_acceleration is not None
        else 0.0
    )

    layer_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in securities:
        layer_rows[str(item.get("layer") or "未分类")].append(item)
    layers = []
    for layer, rows in layer_rows.items():
        layer_breadth, layer_meta = market_breadth_score(rows)
        layers.append(
            {
                "layer": layer,
                "score": round(
                    clamp(
                        (avg([number(row.get("opportunity_score")) or 0 for row in rows]) or 0) * 0.42
                        + (avg([number(row.get("trend_score")) or 0 for row in rows]) or 0) * 0.34
                        + layer_breadth * 0.24
                    ),
                    1,
                ),
                "market_breadth": layer_meta,
                "leaders": sorted(rows, key=lambda row: number(row.get("opportunity_score")) or 0, reverse=True)[:6],
            }
        )
    layers.sort(key=lambda item: number(item.get("score")) or 0, reverse=True)
    securities.sort(key=lambda item: (number(item.get("opportunity_score")) or 0, number(item.get("trend_score")) or 0), reverse=True)

    if acceleration_coverage < 0.35:
        status = "证据不足"
    elif acceleration >= 78 and breadth.get("active_market_count", 0) >= 2:
        status = "需求加速且跨市场扩散"
    elif acceleration >= 72:
        status = "需求加速，等待更多市场确认"
    elif crowding_score is not None and crowding_score >= 72:
        status = "景气强但拥挤"
    elif acceleration >= 62:
        status = "观察跟踪"
    else:
        status = "证据不足"

    return {
        "id": theme.get("id"),
        "name": theme.get("name"),
        "stage": theme.get("stage"),
        "status": status,
        "horizon": theme.get("horizon"),
        "thesis": theme.get("thesis"),
        "demand_acceleration_score": round(acceleration, 1),
        "theme_score": theme_score,
        "acceleration_coverage": round(acceleration_coverage, 3),
        "score_components": {
            "base_theme_prior": base_score,
            "dynamic_center": dynamic_center,
            "earnings_leverage": earnings,
            "earnings_revision": revision,
            "market_underpricing": underpricing,
            "trend_avg": round(trend_avg, 1) if trend_avg is not None else None,
            "market_breadth": breadth_score,
            "change_signal": change_score,
            "data_confidence": data_confidence,
            "crowding_score": crowding_score,
            "crowding_penalty": crowding_penalty,
        },
        "market_breadth": breadth,
        "leading_indicators": theme.get("leading_indicators", []),
        "catalysts": theme.get("catalysts", []),
        "beneficiary_layers": theme.get("beneficiary_layers", []),
        "layers": layers,
        "securities": securities[:30],
    }


def lead_lag_signals(themes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for theme in themes:
        for layer in theme.get("layers", [])[:8]:
            rows = layer.get("leaders") if isinstance(layer.get("leaders"), list) else []
            us = [item for item in rows if item.get("market") == "US"]
            hk_cn = [item for item in rows if item.get("market") in {"HK", "CN"}]
            if not us or not hk_cn:
                continue
            us_score = avg([number(item.get("trend_score")) or number(item.get("opportunity_score")) or 0 for item in us]) or 0
            cross_score = avg([number(item.get("trend_score")) or number(item.get("opportunity_score")) or 0 for item in hk_cn]) or 0
            if us_score >= cross_score + 8:
                signal = "美股先行，港/A 可能滞后补涨或补跌"
            elif cross_score >= us_score + 8:
                signal = "港/A 先行，美股代理需要复核"
            else:
                signal = "三地同步，主题确认度较高"
            signals.append(
                {
                    "theme": theme.get("name"),
                    "layer": layer.get("layer"),
                    "signal": signal,
                    "us_score": round(us_score, 1),
                    "cross_market_score": round(cross_score, 1),
                    "us_examples": [item.get("code") for item in us[:3]],
                    "hk_cn_examples": [item.get("code") for item in hk_cn[:4]],
                }
            )
    signals.sort(key=lambda item: abs(float(item.get("us_score") or 0) - float(item.get("cross_market_score") or 0)), reverse=True)
    return signals[:30]


def secondary_research_candidates(themes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for theme in themes:
        for item in theme.get("securities", []):
            score = number(item.get("opportunity_score")) or 0
            trend = number(item.get("trend_score")) or 0
            crowding = number(item.get("crowding_score"))
            if score >= 74 or (theme.get("demand_acceleration_score", 0) >= 72 and trend >= 70):
                rows.append(
                    {
                        "theme": theme.get("name"),
                        "code": item.get("code"),
                        "name": item.get("name"),
                        "market": item.get("market"),
                        "layer": item.get("layer"),
                        "price": item.get("price"),
                        "quote_time": item.get("quote_time"),
                        "quote_source": item.get("quote_source"),
                        "opportunity_score": item.get("opportunity_score"),
                        "trend_score": item.get("trend_score"),
                        "crowding_score": crowding,
                        "entry_score": item.get("entry_score"),
                        "data_confidence": item.get("data_confidence"),
                        "price_freshness": item.get("price_freshness"),
                        "execution_allowed": item.get("execution_allowed"),
                        "technical_data_complete": item.get("technical_data_complete"),
                        "future_function_audit": item.get("future_function_audit"),
                        "gate_failures": item.get("gate_failures"),
                        "alpha_percentile": item.get("alpha_percentile"),
                        "sector_rank_percentile": item.get("sector_rank_percentile"),
                        "universe_rank": item.get("universe_rank"),
                        "factor_coverage": item.get("factor_coverage"),
                        "reward_risk": item.get("reward_risk"),
                        "starter_entry": item.get("starter_entry"),
                        "starter_stop": item.get("starter_stop"),
                        "starter_target": item.get("starter_target"),
                        "starter_reward_risk": item.get("starter_reward_risk"),
                        "breakout_trigger": item.get("breakout_trigger"),
                        "breakout_stop": item.get("breakout_stop"),
                        "breakout_target": item.get("breakout_target"),
                        "breakout_reward_risk": item.get("breakout_reward_risk"),
                        "demand_acceleration_score": theme.get("demand_acceleration_score"),
                        "reason": item.get("action"),
                    }
                )
    rows.sort(key=lambda item: (number(item.get("demand_acceleration_score")) or 0, number(item.get("opportunity_score")) or 0), reverse=True)
    return rows[:60]


def event_extraction_backlog(opportunity_radar: dict[str, Any], fmp_research: dict[str, Any], themes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    backlog: list[dict[str, Any]] = []
    filing_changes = opportunity_radar.get("filing_changes") if isinstance(opportunity_radar.get("filing_changes"), list) else []
    metric_changes = opportunity_radar.get("metric_changes") if isinstance(opportunity_radar.get("metric_changes"), list) else []
    if filing_changes:
        backlog.append({"type": "filing", "priority": "high", "item": f"本轮新增 {len(filing_changes)} 条申报变化，需要抽取业务/订单/风险措辞。"})
    if metric_changes:
        backlog.append({"type": "metric", "priority": "medium", "item": f"本轮新增 {len(metric_changes)} 条指标变化，需要判断是逻辑增强还是价格波动。"})
    availability = fmp_research.get("data_availability") if isinstance(fmp_research.get("data_availability"), list) else []
    restricted = [item for item in availability if isinstance(item, dict) and not item.get("available")]
    if restricted:
        endpoints = "、".join(str(item.get("endpoint")) for item in restricted[:4])
        backlog.append({"type": "permission", "priority": "high", "item": f"FMP 新闻/电话会端点不可用或限流：{endpoints}；需补充新闻/公告/电话会第二数据源。"})
    for theme in themes[:4]:
        indicators = theme.get("leading_indicators") if isinstance(theme.get("leading_indicators"), list) else []
        if indicators:
            backlog.append({"type": "theme", "priority": "medium", "item": f"{theme.get('name')}：跟踪 {indicators[0]}，用于验证需求加速是否兑现。"})
    return backlog[:20]


def feedback_summary(journal: dict[str, Any], opportunity_radar: dict[str, Any]) -> dict[str, Any]:
    completed = opportunity_radar.get("completed_reviews") if isinstance(opportunity_radar.get("completed_reviews"), list) else []
    due = opportunity_radar.get("review_due") if isinstance(opportunity_radar.get("review_due"), list) else []
    securities = journal.get("securities") if isinstance(journal.get("securities"), dict) else {}
    tracked = len(securities)
    return {
        "tracked_security_count": tracked,
        "completed_review_count": len(completed),
        "due_review_count": len(due),
        "latest_reviews": completed[:12],
        "review_due": due[:12],
        "rule": "用 5/20/60/120 交易日未来收益、基准超额、MAE/MFE 与 IC 校准机会雷达；score_delta 不计命中。",
    }


def public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": payload.get("schema_version"),
        "model_version": payload.get("model_version"),
        "risk_policy": payload.get("risk_policy"),
        "generated_at": payload.get("generated_at"),
        "generated_label": payload.get("generated_label"),
        "summary": payload.get("summary"),
        "themes": payload.get("themes", [])[:10],
        "lead_lag_signals": payload.get("lead_lag_signals", [])[:20],
        "secondary_research_candidates": payload.get("secondary_research_candidates", [])[:30],
        "event_extraction_backlog": payload.get("event_extraction_backlog", [])[:12],
        "feedback": payload.get("feedback"),
        "discipline": payload.get("discipline"),
    }


def build_intelligence(
    supply_radar: dict[str, Any],
    opportunity_radar: dict[str, Any],
    secondary_queue: dict[str, Any],
    journal: dict[str, Any],
    market_pack: dict[str, Any],
    fmp_research: dict[str, Any],
) -> dict[str, Any]:
    supply_by_code = index_supply_candidates(supply_radar)
    market_by_code = index_market_pack(market_pack)
    secondary_by_code = index_secondary_queue(secondary_queue)
    journal_by_code = journal_security_state(journal)
    themes = [
        theme_intelligence(theme, supply_by_code, market_by_code, secondary_by_code, journal_by_code)
        for theme in opportunity_radar.get("themes", [])
        if isinstance(theme, dict)
    ]
    themes.sort(key=lambda item: number(item.get("demand_acceleration_score")) or 0, reverse=True)
    candidates = secondary_research_candidates(themes)
    lead_lag = lead_lag_signals(themes)
    backlog = event_extraction_backlog(opportunity_radar, fmp_research, themes)
    feedback = feedback_summary(journal, opportunity_radar)
    now = now_local()
    return {
        "schema_version": 2,
        "model_version": "2.0.0",
        "risk_policy": risk_policy_public(),
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_label": now.strftime("%Y-%m-%d %H:%M"),
        "summary": {
            "theme_count": len(themes),
            "accelerating_theme_count": sum(1 for item in themes if (number(item.get("demand_acceleration_score")) or 0) >= 72),
            "cross_market_signal_count": len(lead_lag),
            "secondary_research_candidate_count": len(candidates),
            "event_backlog_count": len(backlog),
            "tracked_security_count": feedback.get("tracked_security_count"),
        },
        "data_boundary": {
            "role": "cross-market intelligence and research routing; not trading instruction",
            "markets": "US/HK/CN public and Futu-enhanced data when available",
            "buy_side_gate": f"正式买入 R/R >= {load_risk_policy().formal_min_rr:.1f}:1；试仓 R/R >= {load_risk_policy().starter_min_rr:.1f}:1，并通过 PIT、数据质量、整股执行和本地组合复核。",
        },
        "themes": themes,
        "lead_lag_signals": lead_lag,
        "secondary_research_candidates": candidates,
        "event_extraction_backlog": backlog,
        "feedback": feedback,
        "data_sources": [
            "data/latest_supply_chain_radar.json",
            "data/latest_opportunity_radar.json",
            "docs/data/secondary_analysis_queue.json",
            "docs/data/opportunity_journal.json",
            "data/latest_market_pack.json",
            "data/latest_fmp_research.json when available",
        ],
        "discipline": [
            "需求加速不等于可以买，只代表研究优先级提高。",
            "跨市场同步强于单市场涨幅；但港股/A股必须单独复核流动性、财报和交易规则。",
            "新闻、公告、电话会若缺权限，必须标记为证据缺口，不能编造管理层表述。",
            "历史复盘用于校准模型，不用于事后改写首次发现时间。",
        ],
    }


def render_report(payload: dict[str, Any]) -> str:
    lines: list[str] = [
        "# 跨市场产业链情报雷达",
        "",
        f"- 生成时间：{payload.get('generated_label')}",
        "- 定位：把宏观/产业链/机会雷达/二次分析队列合并，识别需求加速和跨市场扩散；不构成买入建议。",
        f"- 硬约束：任何股票进入正式交易前，必须回到 Buy-Side 分析、R/R >= {load_risk_policy().formal_min_rr:.1f}:1、估值纪律和复星证券整股执行。",
        "",
        "## 本轮结论",
        "",
    ]
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines.append(
        f"- 主题数：{summary.get('theme_count', 0)}；需求加速主题：{summary.get('accelerating_theme_count', 0)}；跨市场联动信号：{summary.get('cross_market_signal_count', 0)}；二次研究候选：{summary.get('secondary_research_candidate_count', 0)}。"
    )
    lines.append(
        f"- 事件/公告待补证据：{summary.get('event_backlog_count', 0)}；历史跟踪股票：{summary.get('tracked_security_count', 0)}。"
    )

    lines.extend(["", "## 需求加速主题排行", ""])
    lines.extend(["| 主题 | 状态 | 加速分 | 原主题分 | 趋势均值 | 市场广度 | 变化信号 | 拥挤度 |", "|---|---|---:|---:|---:|---:|---:|---:|"])
    for theme in payload.get("themes", [])[:10]:
        components = theme.get("score_components") if isinstance(theme.get("score_components"), dict) else {}
        breadth = theme.get("market_breadth") if isinstance(theme.get("market_breadth"), dict) else {}
        lines.append(
            f"| {theme.get('name')} | {theme.get('status')} | {fmt_num(theme.get('demand_acceleration_score'))} | {fmt_num(theme.get('theme_score'))} | {fmt_num(components.get('trend_avg'))} | {fmt_num(components.get('market_breadth'))} | {fmt_num(components.get('change_signal'))} | {fmt_num(components.get('crowding_penalty'))} |"
        )
        markets = "、".join(market_label(str(item)) for item in breadth.get("active_markets", []) if item)
        if markets:
            lines.append(f"|  | 活跃市场 |  |  |  | {markets} |  |  |")

    lines.extend(["", "## 环节扩散强度", ""])
    lines.extend(["| 主题 | 环节 | 环节分 | 活跃市场 | 代表股票 |", "|---|---|---:|---|---|"])
    for theme in payload.get("themes", [])[:8]:
        for layer in theme.get("layers", [])[:5]:
            breadth = layer.get("market_breadth") if isinstance(layer.get("market_breadth"), dict) else {}
            markets = "、".join(market_label(str(item)) for item in breadth.get("active_markets", []) if item) or "数据不足"
            leaders = "、".join(str(item.get("code")) for item in layer.get("leaders", [])[:4])
            lines.append(f"| {theme.get('name')} | {layer.get('layer')} | {fmt_num(layer.get('score'))} | {markets} | {leaders} |")

    signals = payload.get("lead_lag_signals") if isinstance(payload.get("lead_lag_signals"), list) else []
    lines.extend(["", "## 跨市场领先/滞后信号", ""])
    if signals:
        lines.extend(["| 主题 | 环节 | 信号 | 美股例子 | 港/A例子 |", "|---|---|---|---|---|"])
        for item in signals[:15]:
            lines.append(
                f"| {item.get('theme')} | {item.get('layer')} | {item.get('signal')} | {'、'.join(item.get('us_examples', []))} | {'、'.join(item.get('hk_cn_examples', []))} |"
            )
    else:
        lines.append("本轮没有形成足够清晰的跨市场领先/滞后信号。")

    candidates = payload.get("secondary_research_candidates") if isinstance(payload.get("secondary_research_candidates"), list) else []
    lines.extend(["", "## 交给二次分析的候选", ""])
    if candidates:
        lines.extend(["| 市场 | 代码 | 名称 | 主题 | 环节 | 加速分 | 机会分 | 趋势 | 拥挤 | 动作 |", "|---|---|---|---|---|---:|---:|---:|---:|---|"])
        for item in candidates[:25]:
            lines.append(
                f"| {item.get('market')} | {item.get('code')} | {item.get('name')} | {item.get('theme')} | {item.get('layer')} | {fmt_num(item.get('demand_acceleration_score'))} | {fmt_num(item.get('opportunity_score'))} | {fmt_num(item.get('trend_score'))} | {fmt_num(item.get('crowding_score'))} | {item.get('reason')} |"
            )
    else:
        lines.append("本轮没有新增达到二次分析门槛的跨市场候选。")

    backlog = payload.get("event_extraction_backlog") if isinstance(payload.get("event_extraction_backlog"), list) else []
    lines.extend(["", "## 新闻/公告/电话会证据缺口", ""])
    if backlog:
        for item in backlog:
            lines.append(f"- [{item.get('priority')}] {item.get('item')}")
    else:
        lines.append("本轮没有新增事件提取缺口。")

    feedback = payload.get("feedback") if isinstance(payload.get("feedback"), dict) else {}
    lines.extend(["", "## 历史机会复盘反馈", ""])
    lines.append(
        f"- 已跟踪股票：{feedback.get('tracked_security_count', 0)}；已完成复盘：{feedback.get('completed_review_count', 0)}；待复盘：{feedback.get('due_review_count', 0)}。"
    )
    lines.append(f"- 规则：{feedback.get('rule')}")

    lines.extend(["", "## 使用纪律", ""])
    for item in payload.get("discipline", []):
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supply-radar", type=Path, default=DEFAULT_SUPPLY_RADAR)
    parser.add_argument("--opportunity-radar", type=Path, default=DEFAULT_OPPORTUNITY_RADAR)
    parser.add_argument("--secondary-queue", type=Path, default=DEFAULT_SECONDARY_QUEUE)
    parser.add_argument("--opportunity-journal", type=Path, default=DEFAULT_OPPORTUNITY_JOURNAL)
    parser.add_argument("--market-pack", type=Path, default=DEFAULT_MARKET_PACK)
    parser.add_argument("--fmp-research", type=Path, default=DEFAULT_FMP_RESEARCH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--docs-out", type=Path, default=DEFAULT_DOCS_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    payload = build_intelligence(
        load_json(args.supply_radar, {}),
        load_json(args.opportunity_radar, {}),
        load_json(args.secondary_queue, {}),
        load_json(args.opportunity_journal, {}),
        load_json(args.market_pack, {}),
        load_json(args.fmp_research, {}),
    )
    write_json(args.out, payload)
    write_json(args.docs_out, public_payload(payload))
    write_text(args.report, render_report(payload))
    print(f"Wrote {args.out}")
    print(f"Wrote {args.docs_out}")
    print(f"Wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
