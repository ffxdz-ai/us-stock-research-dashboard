#!/usr/bin/env python3
"""Build an event-evidence layer for opportunity discovery.

This script turns available public data into explicit evidence cards:

- SEC filing recency and financial facts from the market pack.
- FMP earnings/estimate/price-target signals when available.
- Price, trend and risk/reward discipline from the market pack.
- Evidence gaps when transcript/news endpoints or non-US fundamentals are missing.

It does not fetch private portfolio data and it does not produce trade orders.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

try:
    from collect_market_data import sec_summary, sec_ticker_map
except Exception:  # noqa: BLE001
    sec_summary = None  # type: ignore[assignment]
    sec_ticker_map = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DOCS_DATA_DIR = ROOT / "docs" / "data"
REPORTS_DIR = ROOT / "reports"

DEFAULT_MARKET_PACK = DATA_DIR / "latest_market_pack.json"
DEFAULT_OPPORTUNITY_RADAR = DATA_DIR / "latest_opportunity_radar.json"
DEFAULT_CROSS_MARKET = DATA_DIR / "latest_cross_market_intelligence.json"
DEFAULT_FMP_RESEARCH = DATA_DIR / "latest_fmp_research.json"
DEFAULT_OUTPUT = DATA_DIR / "latest_event_evidence.json"
DEFAULT_DOCS_OUTPUT = DOCS_DATA_DIR / "event_evidence.json"
DEFAULT_REPORT = REPORTS_DIR / "latest-event-evidence.md"


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


def normalize_code(code: Any) -> str:
    raw = str(code or "").strip().upper()
    if not raw:
        return ""
    return raw if "." in raw else f"US.{raw}"


def us_symbol(code: str) -> str | None:
    normalized = normalize_code(code)
    return normalized.split(".", 1)[1] if normalized.startswith("US.") else None


def parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=beijing_timezone())
        except ValueError:
            pass
    return None


def days_since(value: Any, now: datetime) -> int | None:
    parsed = parse_date(value)
    if parsed is None:
        return None
    return max(0, (now.date() - parsed.date()).days)


def fmt_num(value: Any, digits: int = 1) -> str:
    parsed = number(value)
    return "数据不足" if parsed is None else f"{parsed:.{digits}f}"


def fmt_pct(value: Any, digits: int = 1) -> str:
    parsed = number(value)
    return "数据不足" if parsed is None else f"{parsed:.{digits}f}%"


def fmt_money(value: Any) -> str:
    parsed = number(value)
    return "n/a" if parsed is None else f"{parsed:,.2f}"


def market_pack_index(pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    rows = pack.get("candidates") if isinstance(pack.get("candidates"), list) else []
    for item in rows:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").upper()
        if ticker:
            output[ticker] = item
            output[f"US.{ticker}"] = item
    return output


def fmp_index(fmp_research: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    rows = fmp_research.get("symbols") if isinstance(fmp_research.get("symbols"), list) else []
    for item in rows:
        if isinstance(item, dict) and item.get("symbol"):
            output[str(item["symbol"]).upper()] = item
    return output


def cross_market_index(cross_market: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}

    def add(item: dict[str, Any]) -> None:
        code = normalize_code(item.get("code"))
        if not code:
            return
        current = output.get(code, {})
        merged = {**current, **item}
        output[code] = merged
        symbol = us_symbol(code)
        if symbol:
            output[symbol] = merged

    for item in cross_market.get("secondary_research_candidates", []) if isinstance(cross_market.get("secondary_research_candidates"), list) else []:
        if isinstance(item, dict):
            add(item)
    for theme in cross_market.get("themes", []) if isinstance(cross_market.get("themes"), list) else []:
        if not isinstance(theme, dict):
            continue
        for item in theme.get("securities", []) if isinstance(theme.get("securities"), list) else []:
            if isinstance(item, dict):
                enriched = {**item, "theme": theme.get("name"), "demand_acceleration_score": theme.get("demand_acceleration_score")}
                add(enriched)
    return output


def sec_needs_fallback(sec: dict[str, Any]) -> bool:
    if not sec:
        return True
    if not sec.get("sec_coverage"):
        return True
    if not isinstance(sec.get("recent_filings"), list) or not sec.get("recent_filings"):
        return True
    if not isinstance(sec.get("latest_annual_revenue"), dict):
        return True
    return False


def merge_sec_summary(current: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    if not fallback:
        return current
    if fallback.get("sec_coverage") and sec_needs_fallback(current):
        current_non_empty = {
            k: v
            for k, v in current.items()
            if v not in (None, "", [], {})
            and k not in {"error", "submissions_error", "facts_error"}
            and not (k == "sec_coverage" and v is False)
        }
        merged = {**fallback, **current_non_empty}
        merged["fallback_source"] = "SEC EDGAR fallback in event_evidence"
        return merged
    return current


def sec_fallback_summary(symbol: str, mapping: dict[str, dict[str, Any]], errors: dict[str, str]) -> dict[str, Any]:
    if not symbol or not mapping or sec_summary is None:
        return {}
    try:
        return sec_summary(symbol, mapping, poll_hours=20.0)
    except Exception as exc:  # noqa: BLE001
        errors[symbol] = str(exc)
        return {}


def candidate_codes(opportunity_radar: dict[str, Any], cross_market: dict[str, Any], limit: int) -> list[str]:
    codes: list[str] = []

    def add(code: Any) -> None:
        normalized = normalize_code(code)
        if normalized and normalized not in codes:
            codes.append(normalized)

    for item in cross_market.get("secondary_research_candidates", []) if isinstance(cross_market.get("secondary_research_candidates"), list) else []:
        if isinstance(item, dict):
            add(item.get("code"))
    for item in opportunity_radar.get("secondary_candidates", []) if isinstance(opportunity_radar.get("secondary_candidates"), list) else []:
        if isinstance(item, dict):
            add(item.get("code"))
    for theme in opportunity_radar.get("themes", []) if isinstance(opportunity_radar.get("themes"), list) else []:
        if not isinstance(theme, dict):
            continue
        securities = theme.get("securities") if isinstance(theme.get("securities"), list) else []
        ordered = sorted(
            [item for item in securities if isinstance(item, dict)],
            key=lambda row: number(row.get("opportunity_score")) or 0,
            reverse=True,
        )
        for item in ordered[:5]:
            add(item.get("code"))
    return codes[: max(1, limit)]


def latest_filing_evidence(market_row: dict[str, Any], now: datetime) -> dict[str, Any]:
    sec = market_row.get("sec") if isinstance(market_row.get("sec"), dict) else {}
    filings = sec.get("recent_filings") if isinstance(sec.get("recent_filings"), list) else []
    filing = next((item for item in filings if isinstance(item, dict)), {})
    filed = filing.get("filed")
    age = days_since(filed, now)
    forms = [str(item.get("form")) for item in filings[:5] if isinstance(item, dict) and item.get("form")]
    return {
        "available": bool(filing),
        "form": filing.get("form"),
        "filed": filed,
        "age_days": age,
        "accession": filing.get("accession"),
        "recent_forms": forms,
        "score": 90 if age is not None and age <= 45 else 72 if age is not None and age <= 120 else 45 if filing else 0,
    }


def sec_financial_evidence(market_row: dict[str, Any]) -> dict[str, Any]:
    sec = market_row.get("sec") if isinstance(market_row.get("sec"), dict) else {}
    latest_revenue = sec.get("latest_annual_revenue") if isinstance(sec.get("latest_annual_revenue"), dict) else {}
    latest_fcf = sec.get("latest_annual_fcf") if isinstance(sec.get("latest_annual_fcf"), dict) else {}
    revenue_growth = number(sec.get("revenue_growth_yoy"))
    net_margin = number(sec.get("net_margin"))
    fcf = number(latest_fcf.get("val"))
    has_financial_fact = any(
        value is not None
        for value in [
            number(latest_revenue.get("val")),
            revenue_growth,
            net_margin,
            fcf,
            number(sec.get("liabilities_to_assets")),
        ]
    )
    score = 35.0
    if revenue_growth is not None:
        score += clamp(revenue_growth * 70, -18, 28)
    if net_margin is not None:
        score += clamp(net_margin * 50, -12, 24)
    if fcf is not None and fcf > 0:
        score += 10
    return {
        "available": has_financial_fact,
        "revenue_growth_yoy": revenue_growth,
        "net_margin": net_margin,
        "liabilities_to_assets": number(sec.get("liabilities_to_assets")),
        "latest_annual_revenue": latest_revenue.get("val"),
        "latest_annual_revenue_filed": latest_revenue.get("filed"),
        "latest_annual_fcf": latest_fcf.get("val"),
        "score": round(clamp(score), 1) if has_financial_fact else 0,
    }


def fmp_evidence(row: dict[str, Any]) -> dict[str, Any]:
    if not row:
        return {"available": False, "score": 0, "gaps": ["缺少 FMP 预期/财报快照"]}
    annual = row.get("annual_estimate") if isinstance(row.get("annual_estimate"), dict) else {}
    revision = row.get("estimate_revision") if isinstance(row.get("estimate_revision"), dict) else {}
    surprise = row.get("latest_earnings_surprise") if isinstance(row.get("latest_earnings_surprise"), dict) else {}
    rating = row.get("rating_snapshot") if isinstance(row.get("rating_snapshot"), dict) else {}
    score = 35.0
    expectation_score = number(row.get("expectation_score"))
    if expectation_score is not None:
        score = expectation_score
    upside = number(row.get("price_target_upside_pct"))
    eps_rev = number(revision.get("epsAvg_change_pct"))
    rev_rev = number(revision.get("revenueAvg_change_pct"))
    eps_surprise = number(surprise.get("eps_surprise_pct"))
    if upside is not None and upside >= 20:
        score += 6
    if eps_rev is not None and eps_rev >= 3:
        score += 8
    if rev_rev is not None and rev_rev >= 2:
        score += 5
    if eps_surprise is not None and eps_surprise >= 5:
        score += 6
    gaps: list[str] = []
    if not annual:
        gaps.append("缺少年/季度分析师预期")
    if not surprise:
        gaps.append("缺少最近财报 surprise")
    restricted = row.get("restricted_endpoints") if isinstance(row.get("restricted_endpoints"), list) else []
    if restricted:
        gaps.append("FMP 部分端点受限：" + "、".join(str(item) for item in restricted[:3]))
    return {
        "available": row.get("coverage_status") not in {"restricted", "rate_limited", "unavailable", "empty"},
        "coverage_status": row.get("coverage_status"),
        "expectation_score": expectation_score,
        "price_target_upside_pct": upside,
        "eps_revision_pct": eps_rev,
        "revenue_revision_pct": rev_rev,
        "eps_surprise_pct": eps_surprise,
        "rating": rating.get("rating"),
        "annual_eps_estimate": annual.get("epsAvg"),
        "annual_revenue_estimate": annual.get("revenueAvg"),
        "score": round(clamp(score), 1),
        "gaps": gaps,
    }


def price_evidence(market_row: dict[str, Any], fallback_row: dict[str, Any]) -> dict[str, Any]:
    chart = market_row.get("chart") if isinstance(market_row.get("chart"), dict) else {}
    price = number(market_row.get("price")) or number(fallback_row.get("price"))
    ma50 = number(chart.get("ma50"))
    ma200 = number(chart.get("ma200"))
    high252 = number(chart.get("high252"))
    low20 = number(chart.get("low20"))
    low60 = number(chart.get("low60"))
    low252 = number(chart.get("low252"))
    target = (
        number(market_row.get("mechanical_target"))
        or number(fallback_row.get("mechanical_target"))
        or number(fallback_row.get("target_price"))
        or number(fallback_row.get("target"))
    )
    invalidation = number(market_row.get("invalidation")) or number(fallback_row.get("invalidation"))
    strict_entry = number(market_row.get("strict_entry")) or number(fallback_row.get("strict_entry"))
    rr = number(market_row.get("reward_risk")) or number(fallback_row.get("reward_risk")) or number(fallback_row.get("rr_ratio"))
    fallback_reason = None
    if rr is None and price is not None:
        if invalidation is None:
            supports = [value for value in (low60, low20, low252) if value is not None and value < price]
            if supports:
                invalidation = min(supports)
                fallback_reason = "止损用可用 K 线支撑位兜底"
        if target is None and high252 is not None and high252 > price:
            target = high252
            fallback_reason = "目标价用 52 周高点兜底"
        if target is not None and invalidation is not None and price > invalidation:
            risk = price - invalidation
            reward = target - price
            if risk > 0 and reward > 0:
                rr = round(reward / risk, 2)
                fallback_reason = fallback_reason or "由当前价/目标价/止损位计算"
    trend = number(fallback_row.get("trend_score"))
    score = 45.0
    if price and ma50:
        score += 12 if price >= ma50 else -8
    if price and ma200:
        score += 14 if price >= ma200 else -10
    if price and high252:
        score += 8 if price / high252 >= 0.92 else -4
    if trend is not None:
        score += 10 if trend >= 70 else -4 if trend < 45 else 0
    if rr is not None:
        score += 18 if rr >= 2 else -12
    return {
        "available": price is not None,
        "price": price,
        "quote_time": market_row.get("quote_time"),
        "reward_risk": rr,
        "entry_path_complete": price is not None and target is not None and invalidation is not None and rr is not None,
        "entry_path_fallback": fallback_reason,
        "trend_score": trend,
        "source": "market_pack" if market_row.get("price") else "cross_market_intelligence" if price is not None else None,
        "strict_entry": strict_entry,
        "invalidation": invalidation,
        "mechanical_target": target,
        "ma50": ma50,
        "ma200": ma200,
        "high252": high252,
        "score": round(clamp(score), 1) if price is not None else 0,
    }


def evidence_card(
    code: str,
    opportunity_radar: dict[str, Any],
    cross_market: dict[str, Any],
    market_index: dict[str, dict[str, Any]],
    cross_by_code: dict[str, dict[str, Any]],
    fmp_by_symbol: dict[str, dict[str, Any]],
    sec_mapping: dict[str, dict[str, Any]],
    sec_errors: dict[str, str],
    now: datetime,
) -> dict[str, Any]:
    symbol = us_symbol(code)
    market_row = market_index.get(code) or (market_index.get(symbol) if symbol else {}) or {}
    fallback_row = cross_by_code.get(code) or (cross_by_code.get(symbol) if symbol else {}) or {}
    fmp_row = fmp_by_symbol.get(symbol or "")
    if symbol:
        current_sec = market_row.get("sec") if isinstance(market_row.get("sec"), dict) else {}
        if sec_needs_fallback(current_sec):
            fallback_sec = sec_fallback_summary(symbol, sec_mapping, sec_errors)
            if fallback_sec:
                market_row = dict(market_row)
                market_row["sec"] = merge_sec_summary(current_sec, fallback_sec)
    filing = latest_filing_evidence(market_row, now)
    financials = sec_financial_evidence(market_row)
    fmp = fmp_evidence(fmp_row or {})
    price = price_evidence(market_row, fallback_row)

    related_theme_names: list[str] = []
    for source in (opportunity_radar, cross_market):
        themes = source.get("themes") if isinstance(source.get("themes"), list) else []
        for theme in themes:
            if not isinstance(theme, dict):
                continue
            securities = theme.get("securities") if isinstance(theme.get("securities"), list) else []
            candidates = securities + (theme.get("secondary_research_candidates") if isinstance(theme.get("secondary_research_candidates"), list) else [])
            if any(normalize_code(item.get("code")) == code for item in candidates if isinstance(item, dict)):
                name = str(theme.get("name") or "")
                if name and name not in related_theme_names:
                    related_theme_names.append(name)

    gaps: list[str] = []
    if not market_row and not fallback_row:
        gaps.append("缺少市场行情/财务基础包")
    if not filing.get("available") and symbol:
        gaps.append("缺少 SEC 最近申报记录")
    if not financials.get("available") and symbol:
        gaps.append("缺少 SEC 财务事实")
    if not symbol:
        gaps.append("非美股标的缺少统一财务与公告正文数据源")
    gaps.extend(fmp.get("gaps", []))
    if not price.get("entry_path_complete"):
        gaps.append("缺少完整 R/R 或机械入场路径")
    elif number(price.get("reward_risk")) is not None and float(price["reward_risk"]) < 2:
        gaps.append("R/R 未达 2:1，不能进入普通买入")

    score = (
        float(filing.get("score") or 0) * 0.18
        + float(financials.get("score") or 0) * 0.28
        + float(fmp.get("score") or 0) * 0.24
        + float(price.get("score") or 0) * 0.20
        + max(0, 100 - len(gaps) * 14) * 0.10
    )
    if score >= 76 and len(gaps) <= 2:
        status = "证据较完整"
    elif score >= 58:
        status = "证据可用但需补强"
    else:
        status = "证据不足"

    return {
        "code": code,
        "symbol": symbol,
        "name": market_row.get("name") or fallback_row.get("name") or code,
        "themes": related_theme_names[:5],
        "evidence_status": status,
        "evidence_score": round(clamp(score), 1),
        "filing": filing,
        "financials": financials,
        "fmp": fmp,
        "price": price,
        "sec_fallback_used": bool((market_row.get("sec") or {}).get("fallback_source")) if isinstance(market_row.get("sec"), dict) else False,
        "gaps": gaps[:10],
        "action": evidence_action(status, gaps, price),
    }


def evidence_action(status: str, gaps: list[str], price: dict[str, Any]) -> str:
    rr = number(price.get("reward_risk"))
    if rr is not None and rr < 2:
        return "证据只支持观察/二次研究；R/R 未达标，不允许普通买入。"
    if status == "证据较完整":
        return "可交给 Buy-Side 做完整估值与入场路径复核。"
    if any("电话会" in item or "新闻" in item for item in gaps):
        return "先补新闻/电话会或公告正文证据，再提高结论置信度。"
    return "保留观察，等待财务、价格或事件证据补强。"


def build_theme_evidence(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_theme: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        for theme in card.get("themes", []) or ["未归类"]:
            by_theme.setdefault(str(theme), []).append(card)
    rows: list[dict[str, Any]] = []
    for theme, theme_cards in by_theme.items():
        scores = [float(card.get("evidence_score") or 0) for card in theme_cards]
        gap_count = sum(len(card.get("gaps", [])) for card in theme_cards)
        rows.append(
            {
                "theme": theme,
                "symbol_count": len(theme_cards),
                "avg_evidence_score": round(sum(scores) / len(scores), 1) if scores else 0,
                "median_evidence_score": round(median(scores), 1) if scores else 0,
                "gap_count": gap_count,
                "top_symbols": [card.get("code") for card in sorted(theme_cards, key=lambda item: item.get("evidence_score") or 0, reverse=True)[:6]],
            }
        )
    rows.sort(key=lambda item: (item["avg_evidence_score"], -item["gap_count"]), reverse=True)
    return rows


def permission_gaps(fmp_research: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    availability = fmp_research.get("data_availability") if isinstance(fmp_research.get("data_availability"), list) else []
    for item in availability:
        if isinstance(item, dict) and not item.get("available"):
            output.append(
                {
                    "endpoint": item.get("endpoint"),
                    "status": item.get("status"),
                    "impact": "新闻/电话会正文不能作为已读证据；只能标记为待补。",
                    "fallback": "使用 SEC 申报、FMP earnings/estimate、价格/RR 作为临时证据，不编造管理层表述。",
                }
            )
    return output


def classify_gap(gap: str) -> tuple[str, str, str]:
    text = str(gap or "")
    if "R/R 未达" in text:
        return "rr_discipline", "交易纪律不通过", "discipline"
    if "完整 R/R" in text or "机械入场路径" in text:
        return "entry_path_missing", "入场路径缺失", "entry"
    if "FMP 预期/财报快照" in text:
        return "fmp_estimate_snapshot", "数据缺口", "data"
    if "年/季度分析师预期" in text:
        return "analyst_estimate", "数据缺口", "data"
    if "财报 surprise" in text:
        return "earnings_surprise", "数据缺口", "data"
    if "SEC 最近申报记录" in text:
        return "sec_recent_filing", "数据缺口", "data"
    if "SEC 财务事实" in text:
        return "sec_financial_facts", "数据缺口", "data"
    if "非美股标的" in text:
        return "non_us_data_source", "数据缺口", "data"
    if "FMP 部分端点受限" in text:
        return "fmp_symbol_permission_limited", "权限受限", "permission"
    return "other", "待人工复核", "other"


def gap_label(key: str, original: str) -> str:
    labels = {
        "rr_discipline": "R/R 未达 2:1",
        "entry_path_missing": "缺少完整 R/R 或机械入场路径",
        "fmp_estimate_snapshot": "缺少 FMP 预期/财报快照",
        "analyst_estimate": "缺少年/季度分析师预期",
        "earnings_surprise": "缺少最近财报 surprise",
        "sec_recent_filing": "缺少 SEC 最近申报记录",
        "sec_financial_facts": "缺少 SEC 财务事实",
        "non_us_data_source": "非美股统一财务/公告数据不足",
        "fmp_symbol_permission_limited": "FMP 个股端点受限",
        "permission_limited": "接口权限/限流受限",
        "other": "其他待复核缺口",
    }
    return labels.get(key, original)


def gap_fallback(key: str) -> str:
    fallbacks = {
        "rr_discipline": "等待回调、上修目标价或提高止损逻辑质量。",
        "entry_path_missing": "补齐当前价、支撑/止损、目标价和 R/R。",
        "fmp_estimate_snapshot": "优先 FMP；失败时使用 Finnhub/SEC/Nasdaq earnings fallback。",
        "analyst_estimate": "优先 FMP analyst-estimates；失败时使用 Finnhub estimates。",
        "earnings_surprise": "优先 FMP earnings；失败时使用 Nasdaq earnings calendar。",
        "sec_recent_filing": "使用 SEC submissions 兜底。",
        "sec_financial_facts": "使用 SEC companyfacts 兜底。",
        "non_us_data_source": "需要 AkShare/Tushare/Futu/HKEX 补港股/A股财务与公告。",
        "fmp_symbol_permission_limited": "降低 FMP 预期层置信度；等待额度恢复或升级套餐。",
        "permission_limited": "升级套餐、降低频率或使用 SEC/公司 IR 替代证据。",
        "other": "人工检查报告正文。",
    }
    return fallbacks.get(key, "人工检查报告正文。")


def build_gap_breakdown(cards: list[dict[str, Any]], permission: list[dict[str, Any]]) -> dict[str, Any]:
    categories: dict[str, dict[str, Any]] = {}

    def add(key: str, label: str, group_label: str, group: str, affected: str | None) -> None:
        if key not in categories:
            categories[key] = {
                "key": key,
                "label": gap_label(key, label),
                "group": group,
                "group_label": group_label,
                "count": 0,
                "affected_symbols": [],
                "fallback": gap_fallback(key),
            }
        categories[key]["count"] += 1
        if affected and affected not in categories[key]["affected_symbols"]:
            categories[key]["affected_symbols"].append(affected)

    for card in cards:
        code = str(card.get("code") or "")
        for gap in card.get("gaps", []) if isinstance(card.get("gaps"), list) else []:
            key, group_label, group = classify_gap(str(gap))
            add(key, str(gap), group_label, group, code)

    for item in permission:
        endpoint = str(item.get("endpoint") or "")
        add("permission_limited", "接口权限/限流受限", "权限受限", "permission", endpoint)

    category_rows = sorted(
        categories.values(),
        key=lambda row: ({"data": 1, "permission": 2, "entry": 3, "discipline": 4, "other": 5}.get(str(row.get("group")), 9), -int(row.get("count") or 0)),
    )
    data_gap = sum(int(row["count"]) for row in category_rows if row.get("group") == "data")
    permission_gap = sum(int(row["count"]) for row in category_rows if row.get("group") == "permission")
    entry_path = sum(int(row["count"]) for row in category_rows if row.get("group") == "entry")
    rr_discipline = sum(int(row["count"]) for row in category_rows if row.get("group") == "discipline")
    other = sum(int(row["count"]) for row in category_rows if row.get("group") == "other")
    return {
        "original_total": data_gap + permission_gap + entry_path + rr_discipline + other,
        "data_gap": data_gap,
        "permission_limited": permission_gap,
        "entry_path_missing": entry_path,
        "rr_discipline": rr_discipline,
        "other": other,
        "categories": category_rows,
    }


def build_payload(
    market_pack: dict[str, Any],
    opportunity_radar: dict[str, Any],
    cross_market: dict[str, Any],
    fmp_research: dict[str, Any],
    *,
    limit: int,
) -> dict[str, Any]:
    now = now_local()
    market_index = market_pack_index(market_pack)
    cross_by_code = cross_market_index(cross_market)
    fmp_by_symbol = fmp_index(fmp_research)
    codes = candidate_codes(opportunity_radar, cross_market, limit)
    sec_mapping: dict[str, dict[str, Any]] = {}
    sec_errors: dict[str, str] = {}
    if sec_ticker_map is not None and any(us_symbol(code) for code in codes):
        try:
            sec_mapping = sec_ticker_map()
        except Exception as exc:  # noqa: BLE001
            sec_errors["mapping"] = str(exc)
    cards = [
        evidence_card(code, opportunity_radar, cross_market, market_index, cross_by_code, fmp_by_symbol, sec_mapping, sec_errors, now)
        for code in codes
    ]
    cards.sort(key=lambda item: item.get("evidence_score") or 0, reverse=True)
    gaps = [gap for card in cards for gap in card.get("gaps", [])]
    high_quality = [card for card in cards if card.get("evidence_status") == "证据较完整"]
    permission = permission_gaps(fmp_research)
    gap_breakdown = build_gap_breakdown(cards, permission)
    payload = {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_label": now.strftime("%Y-%m-%d %H:%M"),
        "data_boundary": {
            "role": "event, filing, earnings and evidence extraction; not trading instruction",
            "private_portfolio": "disabled",
            "news_transcripts": "Only marked as available when an upstream endpoint returns content; otherwise treated as evidence gap.",
        },
        "summary": {
            "symbol_count": len(cards),
            "high_quality_evidence_count": len(high_quality),
            "usable_evidence_count": sum(1 for card in cards if card.get("evidence_status") != "证据不足"),
            "gap_count": len(gaps),
            "data_gap_count": gap_breakdown.get("data_gap"),
            "entry_path_missing_count": gap_breakdown.get("entry_path_missing"),
            "rr_discipline_count": gap_breakdown.get("rr_discipline"),
            "permission_gap_count": gap_breakdown.get("permission_limited"),
            "sec_fallback_used_count": sum(1 for card in cards if card.get("sec_fallback_used")),
        },
        "cards": cards,
        "theme_evidence": build_theme_evidence(cards),
        "permission_gaps": permission,
        "evidence_gap_breakdown": gap_breakdown,
        "sec_fallback_errors": sec_errors,
        "discipline": [
            "证据卡只解决“有什么证据/缺什么证据”，不直接给买入结论。",
            "FMP 新闻或电话会端点受限时，系统必须明确未读取正文，不得编造管理层表述。",
            "R/R 低于 2:1 时，即使产业逻辑增强，也只能进入观察或二次研究。",
        ],
    }
    return payload


def public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": payload.get("schema_version"),
        "generated_at": payload.get("generated_at"),
        "generated_label": payload.get("generated_label"),
        "data_boundary": payload.get("data_boundary"),
        "summary": payload.get("summary"),
        "cards": payload.get("cards", [])[:40],
        "theme_evidence": payload.get("theme_evidence", [])[:20],
        "permission_gaps": payload.get("permission_gaps", [])[:20],
        "evidence_gap_breakdown": payload.get("evidence_gap_breakdown"),
        "discipline": payload.get("discipline"),
    }


def render_report(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        "# 事件证据提取雷达",
        "",
        f"- 生成时间：{payload.get('generated_label')}",
        "- 定位：把 SEC 申报、FMP 财报/预期、价格/RR 和证据缺口整理成可审计证据卡；不构成买入建议。",
        "",
        "## 本轮概览",
        "",
        f"- 覆盖标的：{summary.get('symbol_count', 0)}；证据较完整：{summary.get('high_quality_evidence_count', 0)}；可用证据：{summary.get('usable_evidence_count', 0)}；真实数据缺口：{summary.get('data_gap_count', 0)}；权限受限：{summary.get('permission_gap_count', 0)}；入场路径缺失：{summary.get('entry_path_missing_count', 0)}；R/R纪律不通过：{summary.get('rr_discipline_count', 0)}。",
        f"- SEC fallback 补齐：{summary.get('sec_fallback_used_count', 0)} 个美股标的。",
        "",
    ]
    breakdown = payload.get("evidence_gap_breakdown") if isinstance(payload.get("evidence_gap_breakdown"), dict) else {}
    categories = breakdown.get("categories") if isinstance(breakdown.get("categories"), list) else []
    if categories:
        lines.extend(["## 缺口拆解", "", "| 类型 | 数量 | 分组 | 影响对象 | 处理方式 |", "|---|---:|---|---|---|"])
        for item in categories[:12]:
            affected = "、".join(str(x) for x in (item.get("affected_symbols") or [])[:8]) or "待确认"
            if len(item.get("affected_symbols") or []) > 8:
                affected += f" 等 {len(item.get('affected_symbols') or [])} 项"
            lines.append(f"| {item.get('label')} | {item.get('count')} | {item.get('group_label')} | {affected} | {item.get('fallback')} |")
        lines.append("")

    permission = payload.get("permission_gaps") if isinstance(payload.get("permission_gaps"), list) else []
    if permission:
        lines.extend(["## 数据权限与替代证据", "", "| 端点 | 状态 | 影响 | 替代方案 |", "|---|---|---|---|"])
        for item in permission:
            lines.append(f"| {item.get('endpoint')} | {item.get('status')} | {item.get('impact')} | {item.get('fallback')} |")
        lines.append("")

    lines.extend(["## 主题证据强度", "", "| 主题 | 标的数 | 平均证据分 | 中位证据分 | 缺口数 | 代表标的 |", "|---|---:|---:|---:|---:|---|"])
    for item in payload.get("theme_evidence", [])[:15]:
        lines.append(
            f"| {item.get('theme')} | {item.get('symbol_count')} | {fmt_num(item.get('avg_evidence_score'))} | {fmt_num(item.get('median_evidence_score'))} | {item.get('gap_count')} | {'、'.join(item.get('top_symbols', []))} |"
        )

    lines.extend(["", "## 标的证据卡", "", "| 代码 | 名称 | 证据状态 | 证据分 | 最新申报 | 收入增速 | FMP预期分 | R/R | 主要缺口 | 动作 |", "|---|---|---|---:|---|---:|---:|---:|---|---|"])
    for card in payload.get("cards", [])[:30]:
        filing = card.get("filing") if isinstance(card.get("filing"), dict) else {}
        financials = card.get("financials") if isinstance(card.get("financials"), dict) else {}
        fmp = card.get("fmp") if isinstance(card.get("fmp"), dict) else {}
        price = card.get("price") if isinstance(card.get("price"), dict) else {}
        latest_filing = " / ".join(str(part) for part in [filing.get("form"), filing.get("filed")] if part) or "n/a"
        gaps = "；".join(card.get("gaps", [])[:3]) or "暂无关键缺口"
        lines.append(
            f"| {card.get('code')} | {card.get('name')} | {card.get('evidence_status')} | {fmt_num(card.get('evidence_score'))} | {latest_filing} | {fmt_pct((number(financials.get('revenue_growth_yoy')) or 0) * 100) if number(financials.get('revenue_growth_yoy')) is not None else '数据不足'} | {fmt_num(fmp.get('expectation_score'))} | {fmt_num(price.get('reward_risk'), 2)} | {gaps} | {card.get('action')} |"
        )

    lines.extend(["", "## 使用纪律", ""])
    for item in payload.get("discipline", []):
        lines.append(f"- {item}")
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-pack", type=Path, default=DEFAULT_MARKET_PACK)
    parser.add_argument("--opportunity-radar", type=Path, default=DEFAULT_OPPORTUNITY_RADAR)
    parser.add_argument("--cross-market-intelligence", type=Path, default=DEFAULT_CROSS_MARKET)
    parser.add_argument("--fmp-research", type=Path, default=DEFAULT_FMP_RESEARCH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--docs-out", type=Path, default=DEFAULT_DOCS_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    payload = build_payload(
        load_json(args.market_pack, {}),
        load_json(args.opportunity_radar, {}),
        load_json(args.cross_market_intelligence, {}),
        load_json(args.fmp_research, {}),
        limit=max(1, args.limit),
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
