#!/usr/bin/env python3
"""Collect FMP analyst estimates, price targets, ratings and earnings data.

This is an expectations layer for the stock-research system. It helps detect
estimate revisions and market expectation gaps, but it never overrides Buy-Side
valuation, risk/reward, or whole-share execution rules.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DOCS_DATA_DIR = ROOT / "docs" / "data"
REPORTS_DIR = ROOT / "reports"

DEFAULT_CONFIG = CONFIG_DIR / "agent_config.json"
DEFAULT_MARKET_PACK = DATA_DIR / "latest_market_pack.json"
DEFAULT_STATE = DOCS_DATA_DIR / "fmp_research_state.json"
DEFAULT_OUTPUT = DATA_DIR / "latest_fmp_research.json"
DEFAULT_DOCS_OUTPUT = DOCS_DATA_DIR / "fmp_research.json"
DEFAULT_REPORT = REPORTS_DIR / "latest-fmp-research.md"
FMP_BASE = "https://financialmodelingprep.com/stable"
FMP_LAST_REQUEST_AT = 0.0
LOCAL_ENV_PATHS = (
    ROOT / ".env",
    Path("D:/codex-AI-agent/US-RMB-Agent/.env"),
)


def beijing_timezone() -> timezone:
    try:
        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return timezone(timedelta(hours=8), "Asia/Shanghai")


def now_local() -> datetime:
    return datetime.now(beijing_timezone())


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_environment() -> None:
    for path in LOCAL_ENV_PATHS:
        load_dotenv(path)


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
        if not cleaned or cleaned.lower() in {"n/a", "nan", "none", "null", "--"}:
            return None
        try:
            parsed = float(cleaned)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))


def parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fmp_get(api_key: str, endpoint: str, params: dict[str, Any]) -> tuple[Any, dict[str, Any] | None]:
    global FMP_LAST_REQUEST_AT
    query = {
        key: value
        for key, value in params.items()
        if value is not None and str(value) != ""
    }
    query["apikey"] = api_key
    url = FMP_BASE + endpoint + "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers={"User-Agent": "ffxdz-ai-us-stock-research-dashboard/1.0"})
    last_error: dict[str, Any] | None = None
    for attempt in range(2):
        try:
            delay = max(0.0, number(os.getenv("FMP_REQUEST_DELAY_SECONDS", "0.35")) or 0.0)
            elapsed = time.monotonic() - FMP_LAST_REQUEST_AT
            if delay and elapsed < delay:
                time.sleep(delay - elapsed)
            with urllib.request.urlopen(request, timeout=45) as response:
                FMP_LAST_REQUEST_AT = time.monotonic()
                return json.loads(response.read().decode("utf-8")), None
        except urllib.error.HTTPError as exc:
            FMP_LAST_REQUEST_AT = time.monotonic()
            body = exc.read().decode("utf-8", errors="replace")
            if api_key:
                body = body.replace(api_key, "[REDACTED]")
            if exc.code == 429 or "Limit Reach" in body:
                kind = "rate_limited"
            elif exc.code == 402 or "Restricted Endpoint" in body or "Premium Query Parameter" in body:
                kind = "restricted"
            else:
                kind = "http_error"
            return None, {"endpoint": endpoint, "status": exc.code, "kind": kind, "message": body[:240]}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            FMP_LAST_REQUEST_AT = time.monotonic()
            last_error = {"endpoint": endpoint, "status": "error", "kind": "runtime_error", "message": str(exc)[:240]}
            if attempt == 0:
                time.sleep(1.0)
    return None, last_error or {"endpoint": endpoint, "status": "error", "kind": "runtime_error", "message": "unknown runtime error"}


def first_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                return item
    if isinstance(payload, dict):
        return payload
    return {}


def dict_list(payload: Any, limit: int = 10) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    return [item for item in payload[:limit] if isinstance(item, dict)]


def select_estimate_row(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    dated = [(parse_date(row.get("date")), row) for row in rows]
    clean = [(date, row) for date, row in dated if date is not None]
    if not clean:
        return rows[0] if rows else None
    future = [(date, row) for date, row in clean if date.date() >= now.date()]
    if future:
        return sorted(future, key=lambda item: item[0])[0][1]
    return sorted(clean, key=lambda item: item[0], reverse=True)[0][1]


def market_pack_index(pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for item in pack.get("candidates", []) if isinstance(pack.get("candidates"), list) else []:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").upper()
        if ticker:
            output[ticker] = item
    return output


def fmp_symbol_universe(config: dict[str, Any], pack: dict[str, Any], limit: int) -> list[str]:
    symbols: list[str] = []
    candidates = pack.get("candidates") if isinstance(pack.get("candidates"), list) else []
    ordered = sorted(
        [item for item in candidates if isinstance(item, dict) and item.get("ticker")],
        key=lambda item: number(item.get("overall_score")) if number(item.get("overall_score")) is not None else -999,
        reverse=True,
    )
    for item in ordered:
        ticker = str(item.get("ticker") or "").upper()
        if ticker and ticker not in symbols:
            symbols.append(ticker)
    for ticker in config.get("universe", []) if isinstance(config.get("universe"), list) else []:
        upper = str(ticker or "").upper()
        if upper and "." not in upper and upper not in symbols:
            symbols.append(upper)
    return symbols[: max(1, limit)]


def estimate_snapshot(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    keys = [
        "date",
        "revenueAvg",
        "revenueLow",
        "revenueHigh",
        "epsAvg",
        "epsLow",
        "epsHigh",
        "ebitdaAvg",
        "numAnalystsRevenue",
        "numAnalystsEps",
    ]
    return {key: row.get(key) for key in keys if key in row}


def estimate_revision(current: dict[str, Any] | None, previous: dict[str, Any] | None) -> dict[str, Any]:
    if not current:
        return {"status": "missing", "status_label": "缺少分析师预期"}
    if not previous:
        return {"status": "new_snapshot", "status_label": "新建基准快照"}
    if current.get("date") and previous.get("date") and current.get("date") != previous.get("date"):
        return {
            "status": "new_period",
            "status_label": "估计财年切换，重建基准",
            "current_date": current.get("date"),
            "previous_date": previous.get("date"),
        }
    output: dict[str, Any] = {"status": "compared"}
    material = False
    for field in ["epsAvg", "revenueAvg", "ebitdaAvg"]:
        cur = number(current.get(field))
        prev = number(previous.get(field))
        if cur is None or prev is None:
            continue
        output[f"{field}_change"] = round(cur - prev, 4)
        change_pct = round((cur / prev - 1) * 100, 4) if prev != 0 else None
        output[f"{field}_change_pct"] = change_pct
        if change_pct is not None and abs(change_pct) >= 0.5:
            material = True
    output["status_label"] = "有明显修正" if material else "无明显修正"
    return output


def latest_earnings_surprise(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    dated = sorted(
        [
            row
            for row in rows
            if parse_date(row.get("date")) is not None
            and parse_date(row.get("date")) <= now
            and (number(row.get("epsActual")) is not None or number(row.get("revenueActual")) is not None)
        ],
        key=lambda row: str(row.get("date")),
        reverse=True,
    )
    if not dated:
        return None
    row = dated[0]
    eps_actual = number(row.get("epsActual"))
    eps_est = number(row.get("epsEstimated"))
    rev_actual = number(row.get("revenueActual"))
    rev_est = number(row.get("revenueEstimated"))
    return {
        "date": row.get("date"),
        "eps_actual": eps_actual,
        "eps_estimated": eps_est,
        "eps_surprise": round(eps_actual - eps_est, 4) if eps_actual is not None and eps_est is not None else None,
        "eps_surprise_pct": round((eps_actual / eps_est - 1) * 100, 2) if eps_actual is not None and eps_est not in {None, 0} else None,
        "revenue_actual": rev_actual,
        "revenue_estimated": rev_est,
        "revenue_surprise_pct": round((rev_actual / rev_est - 1) * 100, 2) if rev_actual is not None and rev_est not in {None, 0} else None,
        "last_updated": row.get("lastUpdated"),
    }


def target_upside(consensus: dict[str, Any], price: float | None) -> float | None:
    target = number(consensus.get("targetConsensus") or consensus.get("targetMedian"))
    if price is None or target is None or price <= 0:
        return None
    return round((target / price - 1) * 100, 2)


def score_symbol(row: dict[str, Any]) -> tuple[float | None, list[str]]:
    if row.get("coverage_status") == "restricted":
        return None, ["FMP 套餐或 symbol 权限受限，不能评价预期差"]
    if row.get("coverage_status") == "rate_limited":
        return None, ["FMP 今日额度/频率限制，等待下次刷新"]
    if row.get("coverage_status") == "unavailable":
        return None, ["FMP 临时请求失败，暂不评价预期差"]
    if row.get("coverage_status") == "empty":
        return None, ["FMP 无有效返回，暂不评价预期差"]
    score = 45.0
    notes: list[str] = []
    if row.get("coverage_status") == "stale":
        notes.append("沿用上次 FMP 快照，需下次刷新确认")
        score -= 6
    upside = number(row.get("price_target_upside_pct"))
    if upside is not None:
        if upside >= 25:
            score += 18
            notes.append(f"一致目标价上行空间 {upside:.1f}%")
        elif upside >= 12:
            score += 10
            notes.append(f"目标价仍有上行空间 {upside:.1f}%")
        elif upside <= -8:
            score -= 14
            notes.append(f"目标价低于现价 {abs(upside):.1f}%")

    revision = row.get("estimate_revision") if isinstance(row.get("estimate_revision"), dict) else {}
    eps_rev = number(revision.get("epsAvg_change_pct"))
    rev_rev = number(revision.get("revenueAvg_change_pct"))
    if eps_rev is not None:
        if eps_rev >= 3:
            score += 12
            notes.append(f"EPS 预期上修 {eps_rev:.1f}%")
        elif eps_rev <= -3:
            score -= 12
            notes.append(f"EPS 预期下修 {eps_rev:.1f}%")
    if rev_rev is not None:
        if rev_rev >= 2:
            score += 8
            notes.append(f"收入预期上修 {rev_rev:.1f}%")
        elif rev_rev <= -2:
            score -= 8
            notes.append(f"收入预期下修 {rev_rev:.1f}%")

    surprise = row.get("latest_earnings_surprise") if isinstance(row.get("latest_earnings_surprise"), dict) else {}
    eps_surprise_pct = number(surprise.get("eps_surprise_pct"))
    revenue_surprise_pct = number(surprise.get("revenue_surprise_pct"))
    if eps_surprise_pct is not None:
        if eps_surprise_pct >= 5:
            score += 8
            notes.append(f"最近 EPS 超预期 {eps_surprise_pct:.1f}%")
        elif eps_surprise_pct <= -5:
            score -= 8
            notes.append(f"最近 EPS 低于预期 {eps_surprise_pct:.1f}%")
    if revenue_surprise_pct is not None:
        if revenue_surprise_pct >= 3:
            score += 5
            notes.append(f"收入超预期 {revenue_surprise_pct:.1f}%")
        elif revenue_surprise_pct <= -3:
            score -= 5
            notes.append(f"收入低于预期 {revenue_surprise_pct:.1f}%")

    rating = row.get("rating_snapshot") if isinstance(row.get("rating_snapshot"), dict) else {}
    overall = number(rating.get("overallScore"))
    if overall is not None:
        if overall >= 4:
            score += 8
            notes.append(f"FMP rating score {overall:.1f}")
        elif overall <= 2:
            score -= 8
            notes.append(f"FMP rating score {overall:.1f}")

    annual_est = row.get("annual_estimate") if isinstance(row.get("annual_estimate"), dict) else {}
    if not annual_est:
        score -= 10
        notes.append("缺少分析师预期")
    return round(clamp(score), 1), notes[:5]


def action_for_score(row: dict[str, Any]) -> str:
    if row.get("coverage_status") == "restricted":
        return "FMP 权限受限，暂不评价"
    if row.get("coverage_status") == "rate_limited":
        return "FMP 限流，等待下次刷新"
    if row.get("coverage_status") == "unavailable":
        return "FMP 请求失败，等待下次刷新"
    if row.get("coverage_status") == "empty":
        return "FMP 无有效返回，暂不评价"
    score = number(row.get("expectation_score")) or 0
    upside = number(row.get("price_target_upside_pct"))
    revision = row.get("estimate_revision") if isinstance(row.get("estimate_revision"), dict) else {}
    eps_rev = number(revision.get("epsAvg_change_pct"))
    if score >= 72 and (upside is None or upside >= 10) and (eps_rev is None or eps_rev >= 0):
        return "预期差增强，进入 Buy-Side 复核"
    if score >= 62:
        return "预期面改善，保留观察"
    if eps_rev is not None and eps_rev <= -3:
        return "预期下修，谨慎或退回观察"
    return "普通观察"


def collect_symbol(
    api_key: str,
    symbol: str,
    market_item: dict[str, Any],
    previous: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    price = number(market_item.get("price"))

    annual_payload, err = fmp_get(api_key, "/analyst-estimates", {"symbol": symbol, "period": "annual", "page": 0, "limit": 3})
    if err:
        errors.append({"symbol": symbol, **err})
    quarterly_payload: Any = []
    if os.getenv("FMP_FETCH_QUARTERLY", "").strip().lower() in {"1", "true", "yes"}:
        quarterly_payload, err = fmp_get(api_key, "/analyst-estimates", {"symbol": symbol, "period": "quarter", "page": 0, "limit": 3})
        if err:
            errors.append({"symbol": symbol, **err})
    target_summary_payload, err = fmp_get(api_key, "/price-target-summary", {"symbol": symbol})
    if err:
        errors.append({"symbol": symbol, **err})
    target_consensus_payload, err = fmp_get(api_key, "/price-target-consensus", {"symbol": symbol})
    if err:
        errors.append({"symbol": symbol, **err})
    earnings_payload, err = fmp_get(api_key, "/earnings", {"symbol": symbol, "limit": 4})
    if err:
        errors.append({"symbol": symbol, **err})
    rating_payload, err = fmp_get(api_key, "/ratings-snapshot", {"symbol": symbol})
    if err:
        errors.append({"symbol": symbol, **err})

    annual_estimates = dict_list(annual_payload, 6)
    quarterly_estimates = dict_list(quarterly_payload, 3)
    annual = estimate_snapshot(select_estimate_row(annual_estimates, now_local()) if annual_estimates else None)
    quarterly = estimate_snapshot(select_estimate_row(quarterly_estimates, now_local()) if quarterly_estimates else None)
    prev_estimates = previous.get("estimates") if isinstance(previous.get("estimates"), dict) else {}
    revision = estimate_revision(annual, prev_estimates.get("annual") if isinstance(prev_estimates.get("annual"), dict) else None)
    target_summary = first_dict(target_summary_payload)
    target_consensus = first_dict(target_consensus_payload)
    earnings = dict_list(earnings_payload, 4)
    restricted_errors = [item for item in errors if item.get("kind") == "restricted"]
    rate_limited_errors = [item for item in errors if item.get("kind") == "rate_limited"]
    has_any_core_data = bool(annual or target_consensus or target_summary or earnings or first_dict(rating_payload))
    if not has_any_core_data:
        prev_estimates = previous.get("estimates") if isinstance(previous.get("estimates"), dict) else {}
        prev_target = previous.get("price_target_consensus") if isinstance(previous.get("price_target_consensus"), dict) else {}
        if rate_limited_errors and (prev_estimates.get("annual") or prev_target):
            annual = prev_estimates.get("annual") if isinstance(prev_estimates.get("annual"), dict) else annual
            quarterly = prev_estimates.get("quarterly") if isinstance(prev_estimates.get("quarterly"), dict) else quarterly
            target_consensus = prev_target
            coverage_status = "stale"
            revision = {"status": "stale_fallback", "status_label": "沿用上次快照"}
        elif rate_limited_errors:
            coverage_status = "rate_limited"
        elif restricted_errors:
            coverage_status = "restricted"
        elif errors:
            coverage_status = "unavailable"
        else:
            coverage_status = "empty"
    elif restricted_errors:
        coverage_status = "partial"
    else:
        coverage_status = "ok"
    row = {
        "symbol": symbol,
        "name": market_item.get("name") or symbol,
        "coverage_status": coverage_status,
        "restricted_endpoints": sorted({str(item.get("endpoint")) for item in restricted_errors if item.get("endpoint")}),
        "price": price,
        "quote_time": market_item.get("quote_time"),
        "annual_estimate": annual,
        "quarterly_estimate": quarterly,
        "estimate_revision": revision,
        "price_target_summary": target_summary,
        "price_target_consensus": target_consensus,
        "price_target_upside_pct": target_upside(target_consensus, price),
        "latest_earnings_surprise": latest_earnings_surprise(earnings),
        "earnings_history": earnings,
        "rating_snapshot": first_dict(rating_payload),
    }
    score, notes = score_symbol(row)
    row["expectation_score"] = score
    row["score_notes"] = notes
    row["action"] = action_for_score(row)
    return row, errors


def probe_restricted_endpoints(api_key: str) -> list[dict[str, Any]]:
    probes = [
        ("/earning-call-transcript-dates", {"symbol": "NVDA"}),
        ("/news/press-releases", {"symbol": "NVDA", "limit": 1}),
        ("/news/stock", {"symbol": "NVDA", "limit": 1}),
    ]
    output: list[dict[str, Any]] = []
    for endpoint, params in probes:
        payload, err = fmp_get(api_key, endpoint, params)
        if err:
            output.append({"endpoint": endpoint, "available": False, "status": err.get("status"), "message": err.get("message")})
        else:
            output.append({"endpoint": endpoint, "available": True, "sample_count": len(payload) if isinstance(payload, list) else 1})
    return output


def update_state(previous_state: dict[str, Any], rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    symbols = previous_state.get("symbols") if isinstance(previous_state.get("symbols"), dict) else {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        old = symbols.get(symbol) if isinstance(symbols.get(symbol), dict) else {}
        if row.get("coverage_status") in {"rate_limited", "unavailable"} and old:
            old["last_refresh_error_at"] = iso(now)
            old["last_refresh_error"] = row.get("coverage_status")
            symbols[symbol] = old
            continue
        history = old.get("history") if isinstance(old.get("history"), list) else []
        history.append(
            {
                "at": iso(now),
                "annual_estimate": row.get("annual_estimate"),
                "price_target_consensus": row.get("price_target_consensus"),
                "expectation_score": row.get("expectation_score"),
                "price_target_upside_pct": row.get("price_target_upside_pct"),
            }
        )
        symbols[symbol] = {
            "symbol": symbol,
            "first_seen_at": old.get("first_seen_at") or iso(now),
            "last_seen_at": iso(now),
            "estimates": {
                "annual": row.get("annual_estimate"),
                "quarterly": row.get("quarterly_estimate"),
            },
            "price_target_consensus": row.get("price_target_consensus"),
            "expectation_score": row.get("expectation_score"),
            "history": history[-30:],
        }
    return {
        "schema_version": 1,
        "generated_at": iso(now),
        "generated_label": now.strftime("%Y-%m-%d %H:%M"),
        "symbols": dict(sorted(symbols.items())),
    }


def build_research(
    config: dict[str, Any],
    market_pack: dict[str, Any],
    previous_state: dict[str, Any],
    api_key: str | None,
    *,
    limit: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    now = now_local()
    if not api_key:
        payload = {
            "schema_version": 1,
            "generated_at": iso(now),
            "generated_label": now.strftime("%Y-%m-%d %H:%M"),
            "fmp_enabled": False,
            "error": "FMP_API_KEY is not configured.",
            "symbols": [],
            "data_availability": [],
        }
        return payload, previous_state

    market_index = market_pack_index(market_pack)
    symbols = fmp_symbol_universe(config, market_pack, limit)
    previous_symbols = previous_state.get("symbols") if isinstance(previous_state.get("symbols"), dict) else {}
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for symbol in symbols:
        row, row_errors = collect_symbol(
            api_key,
            symbol,
            market_index.get(symbol, {"ticker": symbol, "name": symbol}),
            previous_symbols.get(symbol) if isinstance(previous_symbols.get(symbol), dict) else {},
        )
        rows.append(row)
        errors.extend(row_errors)
    rows.sort(key=lambda item: number(item.get("expectation_score")) or 0, reverse=True)
    data_availability = probe_restricted_endpoints(api_key)
    restricted_results = [item for item in errors if item.get("kind") == "restricted"]
    rate_limited_results = [item for item in errors if item.get("kind") == "rate_limited"]
    operational_errors = [item for item in errors if item.get("kind") not in {"restricted", "rate_limited"}]
    next_state = update_state(previous_state, rows, now)
    actionable = [row for row in rows if "Buy-Side" in str(row.get("action")) or "改善" in str(row.get("action"))]
    payload = {
        "schema_version": 1,
        "generated_at": iso(now),
        "generated_label": now.strftime("%Y-%m-%d %H:%M"),
        "fmp_enabled": True,
        "data_boundary": {
            "role": "FMP analyst expectations and earnings layer; not a trading instruction",
            "buy_side_gate": "目标价和分析师预期只能作为预期差证据，不能替代 Buy-Side/RR/估值纪律。",
            "transcripts_and_news": "当前套餐若返回 402，则电话会和新闻正文不可用；系统不得声称已读取电话会文本。",
        },
        "data_sources": [
            "FMP analyst-estimates",
            "FMP price-target-summary",
            "FMP price-target-consensus",
            "FMP earnings",
            "FMP ratings-snapshot",
        ],
        "summary": {
            "symbol_count": len(rows),
            "actionable_count": len(actionable),
            "restricted_endpoint_count": sum(1 for item in data_availability if not item.get("available")),
            "restricted_result_count": len(restricted_results),
            "rate_limited_result_count": len(rate_limited_results),
            "error_count": len(operational_errors),
        },
        "symbols": rows,
        "actionable": actionable[:20],
        "data_availability": data_availability,
        "restricted_results": restricted_results[:80],
        "rate_limited_results": rate_limited_results[:80],
        "errors": operational_errors[:80],
        "discipline": [
            "FMP 目标价不是目标价结论，只是卖方预期输入。",
            "预期上修要结合股价是否已反映；不能因为目标价有上行空间就买入。",
            "电话会 transcript 和新闻如果未开权限，报告必须标记为数据不足。",
            "最终交易仍需 Buy-Side 分析、R/R >= 2:1 和整股仓位复核。",
        ],
    }
    return payload, next_state


def fmt_num(value: Any, digits: int = 1) -> str:
    parsed = number(value)
    return "数据不足" if parsed is None else f"{parsed:.{digits}f}"


def fmt_pct(value: Any, digits: int = 1) -> str:
    parsed = number(value)
    return "数据不足" if parsed is None else f"{parsed:.{digits}f}%"


def fmt_money(value: Any) -> str:
    parsed = number(value)
    return "n/a" if parsed is None else f"{parsed:,.2f}"


def fmt_compact_money(value: Any) -> str:
    parsed = number(value)
    if parsed is None:
        return "n/a"
    abs_value = abs(parsed)
    if abs_value >= 1_000_000_000_000:
        return f"{parsed / 1_000_000_000_000:.2f}T"
    if abs_value >= 1_000_000_000:
        return f"{parsed / 1_000_000_000:.1f}B"
    if abs_value >= 1_000_000:
        return f"{parsed / 1_000_000:.1f}M"
    return f"{parsed:,.2f}"


def permission_explanation(endpoint: Any, status: Any, message: Any) -> str:
    endpoint_text = str(endpoint or "")
    if str(status) == "429":
        return "FMP 今日额度或调用频率达到上限；系统会等待下次刷新。"
    if str(status) == "402":
        if "transcript" in endpoint_text:
            return "当前 FMP 套餐未开通电话会 transcript 权限。"
        if "news" in endpoint_text or "press" in endpoint_text:
            return "当前 FMP 套餐未开通新闻/公告正文权限。"
        return "当前 FMP 套餐或该 symbol 未开通此查询权限。"
    return str(message or "")[:120]


def revision_display(revision: dict[str, Any]) -> str:
    status = str(revision.get("status") or "")
    if status in {"missing", "new_snapshot", "new_period"}:
        return str(revision.get("status_label") or status)
    eps = number(revision.get("epsAvg_change_pct"))
    revenue = number(revision.get("revenueAvg_change_pct"))
    parts: list[str] = []
    if eps is not None and abs(eps) >= 0.5:
        parts.append(f"EPS {eps:.1f}%")
    if revenue is not None and abs(revenue) >= 0.5:
        parts.append(f"收入 {revenue:.1f}%")
    return "；".join(parts) if parts else str(revision.get("status_label") or "无明显修正")


def render_report(payload: dict[str, Any]) -> str:
    lines = [
        "# FMP 预期与财报雷达",
        "",
        f"- 生成时间：{payload.get('generated_label')}",
        "- 定位：跟踪分析师预期、目标价、评级快照和财报 surprise；不构成买入建议。",
        "- 执行纪律：预期差只提供研究线索，最终必须回到 Buy-Side 分析、R/R >= 2:1 和整股执行。",
        "",
    ]
    if not payload.get("fmp_enabled"):
        lines.extend(["## 数据状态", "", str(payload.get("error") or "FMP 未启用。"), ""])
        return "\n".join(lines)

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines.extend(
        [
            "## 本轮概览",
            "",
            f"- 覆盖标的：{summary.get('symbol_count', 0)}；预期面候选：{summary.get('actionable_count', 0)}；受限端点：{summary.get('restricted_endpoint_count', 0)}；受限查询：{summary.get('restricted_result_count', 0)}；限流查询：{summary.get('rate_limited_result_count', 0)}；错误：{summary.get('error_count', 0)}。",
            "",
        ]
    )
    availability = payload.get("data_availability") if isinstance(payload.get("data_availability"), list) else []
    if availability:
        lines.extend(["## 数据权限", ""])
        lines.extend(["| 端点 | 可用 | 状态 | 说明 |", "|---|---|---|---|"])
        for item in availability:
            lines.append(
                f"| {item.get('endpoint')} | {'是' if item.get('available') else '否'} | {item.get('status', '')} | {permission_explanation(item.get('endpoint'), item.get('status'), item.get('message'))} |"
            )
        lines.append("")

    restricted = payload.get("restricted_results") if isinstance(payload.get("restricted_results"), list) else []
    if restricted:
        by_symbol: dict[str, int] = {}
        for item in restricted:
            symbol = str(item.get("symbol") or "UNKNOWN")
            by_symbol[symbol] = by_symbol.get(symbol, 0) + 1
        lines.extend(["## 受限查询", ""])
        lines.append("以下是套餐或 symbol 权限限制，不代表系统错误；受限股票的 FMP 预期层置信度会降低。")
        lines.append("")
        lines.extend(["| 代码 | 受限次数 | 说明 |", "|---|---:|---|"])
        for symbol, count in sorted(by_symbol.items(), key=lambda item: item[1], reverse=True)[:20]:
            lines.append(f"| {symbol} | {count} | 当前套餐对该 symbol 的部分 FMP 查询受限，不能据此判断预期差。 |")
        lines.append("")

    rate_limited = payload.get("rate_limited_results") if isinstance(payload.get("rate_limited_results"), list) else []
    if rate_limited:
        lines.extend(["## 限流保护", ""])
        lines.append("本轮部分 FMP 请求触发 429 限流；系统不会用限流结果覆盖旧快照，等待下次自动刷新。")
        lines.append("")

    lines.extend(["## 预期差候选", ""])
    actionable = payload.get("actionable") if isinstance(payload.get("actionable"), list) else []
    if not actionable:
        lines.append("本轮没有达到预期面候选门槛的股票。")
    else:
        lines.extend(["| 代码 | 价格 | 预期分 | 目标价上行 | 年度 EPS 预期 | 年度收入预期 | 最近 EPS surprise | 动作 |", "|---|---:|---:|---:|---:|---:|---:|---|"])
        for row in actionable[:20]:
            annual = row.get("annual_estimate") if isinstance(row.get("annual_estimate"), dict) else {}
            surprise = row.get("latest_earnings_surprise") if isinstance(row.get("latest_earnings_surprise"), dict) else {}
            lines.append(
                f"| {row.get('symbol')} | {fmt_money(row.get('price'))} | {fmt_num(row.get('expectation_score'))} | {fmt_pct(row.get('price_target_upside_pct'))} | {fmt_num(annual.get('epsAvg'), 2)} | {fmt_compact_money(annual.get('revenueAvg'))} | {fmt_pct(surprise.get('eps_surprise_pct'))} | {row.get('action')} |"
            )

    lines.extend(["", "## 全量跟踪表", ""])
    lines.extend(["| 代码 | 数据状态 | 预期分 | 目标价上行 | 目标价共识 | 评级 | 预期修正 | 核心证据 |", "|---|---|---:|---:|---:|---|---|---|"])
    for row in payload.get("symbols", [])[:40]:
        consensus = row.get("price_target_consensus") if isinstance(row.get("price_target_consensus"), dict) else {}
        rating = row.get("rating_snapshot") if isinstance(row.get("rating_snapshot"), dict) else {}
        revision = row.get("estimate_revision") if isinstance(row.get("estimate_revision"), dict) else {}
        coverage_status = str(row.get("coverage_status") or "ok")
        coverage_label = {
            "ok": "可用",
            "partial": "部分受限",
            "restricted": "权限受限",
            "rate_limited": "FMP限流",
            "unavailable": "请求失败",
            "stale": "沿用旧快照",
            "empty": "无返回",
        }.get(coverage_status, coverage_status)
        score_text = "n/a" if coverage_status in {"restricted", "rate_limited", "unavailable", "empty"} else fmt_num(row.get("expectation_score"))
        lines.append(
            f"| {row.get('symbol')} | {coverage_label} | {score_text} | {fmt_pct(row.get('price_target_upside_pct'))} | {fmt_money(consensus.get('targetConsensus'))} | {rating.get('rating', 'n/a')} | {revision_display(revision)} | {'；'.join(row.get('score_notes', [])[:3])} |"
        )

    lines.extend(["", "## 使用纪律", ""])
    for item in payload.get("discipline", []):
        lines.append(f"- {item}")
    return "\n".join(lines).strip() + "\n"


def archive_copy(report_path: Path) -> Path:
    timestamp = now_local().strftime("%Y%m%d-%H%M")
    archive = report_path.with_name(f"fmp-research-{timestamp}.md")
    archive.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--market-pack", type=Path, default=DEFAULT_MARKET_PACK)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--docs-out", type=Path, default=DEFAULT_DOCS_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--no-archive-copy", action="store_true")
    args = parser.parse_args()

    load_environment()
    config = load_json(args.config, {})
    if not config:
        raise SystemExit(f"Config not found or invalid: {args.config}")
    market_pack = load_json(args.market_pack, {})
    previous_state = load_json(args.state, {})
    payload, next_state = build_research(
        config,
        market_pack,
        previous_state,
        os.getenv("FMP_API_KEY", "").strip() or None,
        limit=max(1, int(args.limit)),
    )
    write_json(args.out, payload)
    write_json(args.docs_out, payload)
    write_json(args.state, next_state)
    write_text(args.report, render_report(payload))
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
