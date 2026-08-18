#!/usr/bin/env python3
"""Compute opportunity-radar review and hit-rate metrics.

The opportunity radar already records first-seen snapshots and 30/60/90 day
checkpoints. This script turns that memory into measurable feedback:

- Live return since first discovery for tracked themes and securities.
- Completed checkpoint outcomes when available.
- Pending/immature review counts so the system does not overclaim.
- Hit-rate metrics based only on mature or completed reviews.

It is a model-calibration tool, not a trading signal.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DOCS_DATA_DIR = ROOT / "docs" / "data"
REPORTS_DIR = ROOT / "reports"

DEFAULT_JOURNAL = DOCS_DATA_DIR / "opportunity_journal.json"
DEFAULT_OPPORTUNITY_RADAR = DATA_DIR / "latest_opportunity_radar.json"
DEFAULT_MARKET_PACK = DATA_DIR / "latest_market_pack.json"
DEFAULT_OUTPUT = DATA_DIR / "latest_opportunity_review_metrics.json"
DEFAULT_DOCS_OUTPUT = DOCS_DATA_DIR / "opportunity_review_metrics.json"
DEFAULT_REPORT = REPORTS_DIR / "latest-opportunity-review-metrics.md"


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


def normalize_code(code: Any) -> str:
    raw = str(code or "").strip().upper()
    if not raw:
        return ""
    return raw if "." in raw else f"US.{raw}"


def us_symbol(code: str) -> str | None:
    normalized = normalize_code(code)
    return normalized.split(".", 1)[1] if normalized.startswith("US.") else None


def fmt_num(value: Any, digits: int = 1) -> str:
    parsed = number(value)
    return "数据不足" if parsed is None else f"{parsed:.{digits}f}"


def fmt_pct(value: Any, digits: int = 1) -> str:
    parsed = number(value)
    return "数据不足" if parsed is None else f"{parsed:.{digits}f}%"


def market_prices(pack: dict[str, Any]) -> dict[str, float]:
    output: dict[str, float] = {}
    rows = pack.get("candidates") if isinstance(pack.get("candidates"), list) else []
    for item in rows:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").upper()
        price = number(item.get("price"))
        if ticker and price is not None:
            output[ticker] = price
            output[f"US.{ticker}"] = price
    return output


def chart_histories(pack: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for item in pack.get("candidates", []) if isinstance(pack.get("candidates"), list) else []:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").upper()
        chart = item.get("chart") if isinstance(item.get("chart"), dict) else {}
        bars = chart.get("bars") if isinstance(chart.get("bars"), list) else []
        if ticker and bars:
            output[ticker] = [bar for bar in bars if isinstance(bar, dict)]
            output[f"US.{ticker}"] = output[ticker]
    market_chart_rows = (pack.get("market_charts") or {}).items() if isinstance(pack.get("market_charts"), dict) else []
    for ticker, chart in market_chart_rows:
        bars = chart.get("bars") if isinstance(chart, dict) and isinstance(chart.get("bars"), list) else []
        if bars:
            output[str(ticker).upper()] = [bar for bar in bars if isinstance(bar, dict)]
    return output


def forward_metrics(signal_time: Any, signal_price: Any, bars: list[dict[str, Any]], horizons: tuple[int, ...] = (5, 20, 60, 120)) -> dict[str, Any]:
    parsed = parse_time(signal_time)
    start_price = number(signal_price)
    if parsed is None or start_price is None or start_price <= 0 or not bars:
        return {"missing_reason": "data_missing", **{f"return_{days}d": None for days in horizons}, "max_drawdown": None, "max_gain": None}
    ordered: list[tuple[datetime, float]] = []
    for bar in bars:
        stamp = parse_time(bar.get("time"))
        close = number(bar.get("close"))
        # Strict forward-only window: the signal day's bar is never counted as
        # a future observation, even when the chart provider timestamps it at
        # midnight and the signal was produced later that day.
        if stamp and close is not None and close > 0 and stamp.date() > parsed.date():
            ordered.append((stamp, close))
    ordered.sort(key=lambda row: row[0])
    returns: dict[str, Any] = {}
    for days in horizons:
        returns[f"return_{days}d"] = round((ordered[days - 1][1] / start_price - 1) * 100, 2) if len(ordered) >= days else None
    observed = [price for _, price in ordered[: max(horizons)]]
    returns["max_drawdown"] = round((min(observed) / start_price - 1) * 100, 2) if observed else None
    returns["max_gain"] = round((max(observed) / start_price - 1) * 100, 2) if observed else None
    returns["observed_trading_days"] = len(ordered)
    returns["missing_reason"] = None if observed else "data_missing"
    return returns


def benchmark_price_at_signal(signal_time: Any, bars: list[dict[str, Any]]) -> float | None:
    signal = parse_time(signal_time)
    if signal is None:
        return None
    eligible: list[tuple[datetime, float]] = []
    for bar in bars:
        stamp = parse_time(bar.get("time"))
        close = number(bar.get("close"))
        if stamp and close is not None and close > 0 and stamp.date() <= signal.date():
            eligible.append((stamp, close))
    eligible.sort(key=lambda row: row[0])
    return eligible[-1][1] if eligible else None


def classify_missing_reason(record: dict[str, Any], metrics: dict[str, Any]) -> str | None:
    if metrics.get("missing_reason") is None:
        return None
    text = " ".join(str(record.get(key) or "") for key in ("status", "data_gap", "error", "note")).lower()
    if any(token in text for token in ("delisted", "退市")):
        return "delisted"
    if any(token in text for token in ("symbol changed", "ticker changed", "代码变更", "更名")):
        return "symbol_changed"
    if any(token in text for token in ("suspended", "停牌")):
        return "suspended"
    if any(token in text for token in ("source failure", "http ", "timeout", "接口失败", "源失败")):
        return "source_failure"
    return "data_missing"


def subset_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [item for item in items if item.get("status") == "completed"]
    returns = [float(item["return_20d"]) for item in completed if item.get("return_20d") is not None]
    excess = [float(item["spy_excess_20d"]) for item in completed if item.get("spy_excess_20d") is not None]
    hits = [item for item in completed if item.get("hit_20d") is not None]
    return {
        "sample_count": len(completed),
        "hit_rate_20d": round(sum(1 for item in hits if item.get("hit_20d")) / len(hits) * 100, 1) if hits else None,
        "average_forward_return_20d": round(mean(returns), 2) if returns else None,
        "average_spy_excess_20d": round(mean(excess), 2) if excess else None,
    }


def top_bucket_turnover(security_state: dict[str, Any]) -> tuple[float | None, str]:
    histories = {
        code: record.get("rank_history")
        for code, record in security_state.items()
        if isinstance(record, dict) and isinstance(record.get("rank_history"), list) and len(record.get("rank_history")) >= 2
    }
    if not histories:
        return None, "rank history insufficient"
    bucket_size = max(1, math.ceil(len(security_state) / 10))
    previous = {
        code for code, rows in histories.items()
        if number(rows[-2].get("rank") if isinstance(rows[-2], dict) else None) is not None
        and float(rows[-2]["rank"]) <= bucket_size
    }
    current = {
        code for code, rows in histories.items()
        if number(rows[-1].get("rank") if isinstance(rows[-1], dict) else None) is not None
        and float(rows[-1]["rank"]) <= bucket_size
    }
    if not previous:
        return None, "previous top-decile membership unavailable"
    return round((1 - len(previous & current) / len(previous)) * 100, 1), "top-decile membership turnover"


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    x_mean, y_mean = mean(xs), mean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denominator = math.sqrt(sum((x - x_mean) ** 2 for x in xs) * sum((y - y_mean) ** 2 for y in ys))
    return round(numerator / denominator, 4) if denominator else None


def rank_values(values: list[float]) -> list[float]:
    ordered = sorted((value, idx) for idx, value in enumerate(values))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        while end < len(ordered) and ordered[end][0] == ordered[cursor][0]:
            end += 1
        avg_rank = (cursor + 1 + end) / 2
        for _, idx in ordered[cursor:end]:
            ranks[idx] = avg_rank
        cursor = end
    return ranks


def opportunity_current_scores(opportunity_radar: dict[str, Any]) -> dict[str, float]:
    output: dict[str, float] = {}
    themes = opportunity_radar.get("themes") if isinstance(opportunity_radar.get("themes"), list) else []
    for theme in themes:
        if isinstance(theme, dict) and theme.get("id"):
            score = number(theme.get("expectation_gap_score"))
            if score is not None:
                output[str(theme["id"])] = score
    return output


def checkpoint_status(record: dict[str, Any], now: datetime) -> tuple[int, int, int]:
    completed = 0
    due = 0
    pending = 0
    checkpoints = record.get("checkpoints") if isinstance(record.get("checkpoints"), list) else []
    for checkpoint in checkpoints:
        if not isinstance(checkpoint, dict):
            continue
        if checkpoint.get("status") == "completed":
            completed += 1
            continue
        due_at = parse_time(checkpoint.get("due_at"))
        if due_at and due_at <= now:
            due += 1
        else:
            pending += 1
    return completed, due, pending


def price_returns(initial_prices: dict[str, Any], current_prices: dict[str, float]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code, start_raw in initial_prices.items():
        normalized = normalize_code(code)
        start = number(start_raw)
        current = current_prices.get(normalized) or current_prices.get(us_symbol(normalized) or "")
        if start is None or current is None or start <= 0:
            continue
        rows.append(
            {
                "code": normalized,
                "initial_price": start,
                "current_price": current,
                "return_pct": round((current / start - 1) * 100, 2),
            }
        )
    rows.sort(key=lambda item: item["return_pct"], reverse=True)
    return rows


def classify_theme(age_days: int, avg_return: float | None, score_delta: float | None, completed_count: int) -> str:
    if completed_count == 0 and age_days < 20:
        return "未成熟观察"
    if avg_return is not None and avg_return >= 15:
        return "价格验证成功"
    if avg_return is not None and avg_return <= -12:
        return "价格验证失败"
    return "继续验证"


def theme_metrics(
    theme_id: str,
    record: dict[str, Any],
    current_scores: dict[str, float],
    current_prices: dict[str, float],
    now: datetime,
) -> dict[str, Any]:
    first_seen = parse_time(record.get("first_seen_at")) or now
    age_days = max(0, (now.date() - first_seen.date()).days)
    initial_score = number(record.get("initial_score"))
    current_score = current_scores.get(theme_id) or number(record.get("last_score"))
    score_delta = round(current_score - initial_score, 1) if current_score is not None and initial_score is not None else None
    initial_prices = record.get("initial_prices") if isinstance(record.get("initial_prices"), dict) else {}
    returns = price_returns(initial_prices, current_prices)
    return_values = [float(item["return_pct"]) for item in returns]
    avg_return = round(sum(return_values) / len(return_values), 2) if return_values else None
    med_return = round(median(return_values), 2) if return_values else None
    best = returns[0] if returns else None
    worst = returns[-1] if returns else None
    completed, due, pending = checkpoint_status(record, now)
    status = classify_theme(age_days, avg_return, score_delta, completed)
    mature = age_days >= 20 or completed > 0
    hit = status == "价格验证成功" if mature else None
    return {
        "theme_id": theme_id,
        "theme_name": record.get("name") or theme_id,
        "first_seen_at": record.get("first_seen_at"),
        "age_days": age_days,
        "initial_score": initial_score,
        "current_score": current_score,
        "score_delta": score_delta,
        "tracked_security_count": len(initial_prices),
        "priced_security_count": len(returns),
        "avg_return_pct": avg_return,
        "median_return_pct": med_return,
        "best_security": best,
        "worst_security": worst,
        "completed_checkpoint_count": completed,
        "due_checkpoint_count": due,
        "pending_checkpoint_count": pending,
        "mature": mature,
        "hit": hit,
        "status": status,
        "returns": returns[:20],
    }


def completed_review_metrics(opportunity_radar: dict[str, Any], journal: dict[str, Any]) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    for source in (
        opportunity_radar.get("completed_reviews") if isinstance(opportunity_radar.get("completed_reviews"), list) else [],
        journal.get("latest_reviews") if isinstance(journal.get("latest_reviews"), list) else [],
    ):
        for item in source:
            if isinstance(item, dict):
                reviews.append(item)
    deduped: dict[str, dict[str, Any]] = {}
    for item in reviews:
        key = f"{item.get('theme_id')}:{item.get('checkpoint_days')}:{item.get('reviewed_at')}"
        deduped[key] = item
    return list(deduped.values())[:80]


def build_payload(journal: dict[str, Any], opportunity_radar: dict[str, Any], market_pack: dict[str, Any]) -> dict[str, Any]:
    now = now_local()
    opportunities = journal.get("opportunities") if isinstance(journal.get("opportunities"), dict) else {}
    current_prices = market_prices(market_pack)
    current_scores = opportunity_current_scores(opportunity_radar)
    themes = [
        theme_metrics(str(theme_id), record, current_scores, current_prices, now)
        for theme_id, record in opportunities.items()
        if isinstance(record, dict)
    ]
    themes.sort(key=lambda item: (item.get("mature") is True, number(item.get("avg_return_pct")) or -999), reverse=True)
    completed_reviews = completed_review_metrics(opportunity_radar, journal)
    mature = [item for item in themes if item.get("mature")]
    hits = [item for item in mature if item.get("hit") is True]
    failed = [item for item in mature if item.get("hit") is False]
    live_returns = [number(item.get("avg_return_pct")) for item in themes if number(item.get("avg_return_pct")) is not None]
    histories = chart_histories(market_pack)
    benchmark_bars = histories.get("SPY", [])
    qqq_bars = histories.get("QQQ", [])
    security_state = journal.get("securities") if isinstance(journal.get("securities"), dict) else {}
    review_items: list[dict[str, Any]] = []
    missing_counts: dict[str, int] = {key: 0 for key in ("delisted", "symbol_changed", "data_missing", "suspended", "source_failure", "unknown")}
    for code, record in security_state.items():
        if not isinstance(record, dict):
            continue
        signals = record.get("signals") if isinstance(record.get("signals"), list) else []
        signal = signals[0] if signals and isinstance(signals[0], dict) else {
            "signal_time": record.get("first_seen_at"),
            "signal_price": record.get("price"),
            "signal_score": record.get("opportunity_score"),
            "signal_rank": record.get("universe_rank"),
            "signal_factors": (record.get("factor_snapshot") or {}).get("factors") if isinstance(record.get("factor_snapshot"), dict) else {},
            "signal_data_snapshot_id": None,
        }
        bars = histories.get(str(code).upper(), [])
        metrics = forward_metrics(signal.get("signal_time"), signal.get("signal_price"), bars)
        benchmark = forward_metrics(signal.get("signal_time"), benchmark_price_at_signal(signal.get("signal_time"), benchmark_bars), benchmark_bars)
        qqq = forward_metrics(signal.get("signal_time"), benchmark_price_at_signal(signal.get("signal_time"), qqq_bars), qqq_bars)
        sector_etf = str(signal.get("sector_etf") or record.get("sector_etf") or "SPY").upper()
        sector_bars = histories.get(sector_etf, [])
        sector = forward_metrics(signal.get("signal_time"), benchmark_price_at_signal(signal.get("signal_time"), sector_bars), sector_bars)
        excess = {
            f"spy_excess_{days}d": round(metrics[f"return_{days}d"] - benchmark[f"return_{days}d"], 2)
            if metrics.get(f"return_{days}d") is not None and benchmark.get(f"return_{days}d") is not None
            else None
            for days in (5, 20, 60, 120)
        }
        excess.update({
            f"qqq_excess_{days}d": round(metrics[f"return_{days}d"] - qqq[f"return_{days}d"], 2)
            if metrics.get(f"return_{days}d") is not None and qqq.get(f"return_{days}d") is not None
            else None
            for days in (5, 20, 60, 120)
        })
        excess.update({
            f"sector_excess_{days}d": round(metrics[f"return_{days}d"] - sector[f"return_{days}d"], 2)
            if metrics.get(f"return_{days}d") is not None and sector.get(f"return_{days}d") is not None
            else None
            for days in (5, 20, 60, 120)
        })
        missing_reason = classify_missing_reason(record, metrics)
        if missing_reason:
            missing_counts[missing_reason if missing_reason in missing_counts else "unknown"] += 1
        status = "completed" if metrics.get("return_20d") is not None else "pending" if metrics.get("observed_trading_days", 0) < 20 else "data_missing"
        review_items.append(
            {
                "symbol": normalize_code(code),
                "name": record.get("name"),
                "signal_time": signal.get("signal_time"),
                "signal_price": signal.get("signal_price"),
                "signal_score": signal.get("signal_score"),
                "signal_rank": signal.get("signal_rank"),
                "signal_factors": signal.get("signal_factors") or {},
                "signal_data_snapshot_id": signal.get("signal_data_snapshot_id"),
                "return_5d": metrics.get("return_5d"),
                "return_20d": metrics.get("return_20d"),
                "return_60d": metrics.get("return_60d"),
                "return_120d": metrics.get("return_120d"),
                **excess,
                "max_drawdown": metrics.get("max_drawdown"),
                "max_gain": metrics.get("max_gain"),
                "mae": metrics.get("max_drawdown"),
                "mfe": metrics.get("max_gain"),
                "observed_trading_days": metrics.get("observed_trading_days"),
                "benchmark_symbol": "SPY",
                "sector_etf": sector_etf,
                "evaluation_split": signal.get("evaluation_split") or ("oos" if str(signal.get("signal_time") or "")[:4] >= "2025" else "historical"),
                "status": status,
                "missing_reason": missing_reason,
                "hit_20d": bool(
                    metrics.get("return_20d") is not None
                    and excess.get("spy_excess_20d") is not None
                    and excess.get("qqq_excess_20d") is not None
                    and excess.get("sector_excess_20d") is not None
                    and metrics["return_20d"] > 0
                    and excess["spy_excess_20d"] > 0
                    and excess["qqq_excess_20d"] > 0
                    and excess["sector_excess_20d"] > 0
                ) if status == "completed" else None,
            }
        )
    completed_items = [item for item in review_items if item.get("status") == "completed"]
    returns20 = [float(item["return_20d"]) for item in completed_items if item.get("return_20d") is not None]
    excess20 = [float(item["spy_excess_20d"]) for item in completed_items if item.get("spy_excess_20d") is not None]
    scores20 = [float(item["signal_score"]) for item in completed_items if item.get("signal_score") is not None and item.get("return_20d") is not None]
    paired_returns = [float(item["return_20d"]) for item in completed_items if item.get("signal_score") is not None and item.get("return_20d") is not None]
    hit_values = [item for item in completed_items if item.get("hit_20d") is not None]
    sorted_by_score = sorted([item for item in completed_items if item.get("signal_score") is not None and item.get("return_20d") is not None], key=lambda item: float(item["signal_score"]), reverse=True)
    decile = max(1, len(sorted_by_score) // 10) if sorted_by_score else 0
    top_decile_spread = None
    if len(sorted_by_score) >= 10:
        top = mean(float(item["return_20d"]) for item in sorted_by_score[:decile])
        bottom = mean(float(item["return_20d"]) for item in sorted_by_score[-decile:])
        top_decile_spread = round(top - bottom, 2)
    turnover, turnover_status = top_bucket_turnover(security_state)
    metrics_summary = {
        "hit_rate_20d": round(sum(1 for item in hit_values if item.get("hit_20d")) / len(hit_values) * 100, 1) if hit_values else None,
        "win_rate_20d": round(sum(1 for value in returns20 if value > 0) / len(returns20) * 100, 1) if returns20 else None,
        "average_forward_return_20d": round(mean(returns20), 2) if returns20 else None,
        "median_forward_return_20d": round(median(returns20), 2) if returns20 else None,
        "average_spy_excess_20d": round(mean(excess20), 2) if excess20 else None,
        "top_decile_spread_20d": top_decile_spread,
        "ic_20d": pearson(scores20, paired_returns),
        "rank_ic_20d": pearson(rank_values(scores20), rank_values(paired_returns)) if len(scores20) >= 3 else None,
        "sharpe_20d": round(mean(returns20) / pstdev(returns20) * math.sqrt(252 / 20), 3) if len(returns20) >= 3 and pstdev(returns20) > 0 else None,
        "information_ratio_20d": round(mean(excess20) / pstdev(excess20) * math.sqrt(252 / 20), 3) if len(excess20) >= 3 and pstdev(excess20) > 0 else None,
        "average_max_drawdown": round(mean([float(item["max_drawdown"]) for item in review_items if item.get("max_drawdown") is not None]), 2) if any(item.get("max_drawdown") is not None for item in review_items) else None,
        "average_max_gain": round(mean([float(item["max_gain"]) for item in review_items if item.get("max_gain") is not None]), 2) if any(item.get("max_gain") is not None for item in review_items) else None,
        "turnover": turnover,
        "turnover_status": turnover_status,
        "coverage_pct": round(len(completed_items) / len(review_items) * 100, 1) if review_items else None,
    }
    split_metrics = {
        "train": subset_metrics([item for item in review_items if item.get("evaluation_split") == "train"]),
        "validation": subset_metrics([item for item in review_items if item.get("evaluation_split") == "validation"]),
        "oos": subset_metrics([item for item in review_items if item.get("evaluation_split") == "oos"]),
    }
    summary = {
        "theme_count": len(themes),
        "mature_theme_count": len(mature),
        "hit_count": len(hits),
        "failed_count": len(failed),
        "hit_rate_pct": metrics_summary["hit_rate_20d"],
        "completed_review_count": len(completed_reviews),
        "due_checkpoint_count": sum(int(item.get("due_checkpoint_count") or 0) for item in themes),
        "pending_checkpoint_count": sum(int(item.get("pending_checkpoint_count") or 0) for item in themes),
        "avg_live_return_pct": round(sum(value for value in live_returns if value is not None) / len(live_returns), 2) if live_returns else None,
        "best_theme": themes[0].get("theme_name") if themes else None,
        "tracked_security_count": len(review_items),
        "completed_security_count": len(completed_items),
        "pending_security_count": sum(1 for item in review_items if item.get("status") == "pending"),
    }
    return {
        "schema_version": 2,
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_label": now.strftime("%Y-%m-%d %H:%M"),
        "data_boundary": {
            "role": "opportunity discovery feedback loop; not trading instruction",
            "hit_rate_rule": "20D 命中要求未来收益为正，并同时跑赢 SPY、QQQ 与行业 ETF；score_delta 不参与命中判定。",
            "forward_horizons": "5/20/60/120 trading days",
            "walk_forward": "2018-2023 train / 2024 validation / 2025+ OOS；历史信号不足时不伪造结果。",
        },
        "summary": summary,
        "backtest_metrics": metrics_summary,
        "walk_forward_metrics": split_metrics,
        "items": review_items,
        "missing_data_audit": {"counts": missing_counts, "denominator": len(review_items)},
        "themes": themes,
        "completed_reviews": completed_reviews,
        "discipline": [
            "命中率只用于校准机会雷达，不用于事后改写首次发现时间。",
            "未形成 20 个交易日未来价格的机会只显示实时跟踪，不纳入命中率。",
            "价格上涨不等于买入正确；还必须结合当时 R/R、估值和执行纪律复盘。",
        ],
    }


def public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": payload.get("schema_version"),
        "generated_at": payload.get("generated_at"),
        "generated_label": payload.get("generated_label"),
        "data_boundary": payload.get("data_boundary"),
        "summary": payload.get("summary"),
        "backtest_metrics": payload.get("backtest_metrics"),
        "walk_forward_metrics": payload.get("walk_forward_metrics"),
        "items": payload.get("items", [])[:120],
        "missing_data_audit": payload.get("missing_data_audit"),
        "themes": payload.get("themes", [])[:30],
        "completed_reviews": payload.get("completed_reviews", [])[:40],
        "discipline": payload.get("discipline"),
    }


def render_report(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    backtest = payload.get("backtest_metrics") if isinstance(payload.get("backtest_metrics"), dict) else {}
    lines = [
        "# 机会雷达复盘统计",
        "",
        f"- 生成时间：{payload.get('generated_label')}",
        "- 定位：统计机会雷达是否真的提前发现机会；不构成买入建议。",
        "",
        "## 本轮概览",
        "",
        f"- 跟踪主题：{summary.get('theme_count', 0)}；成熟主题：{summary.get('mature_theme_count', 0)}；命中：{summary.get('hit_count', 0)}；失败：{summary.get('failed_count', 0)}；命中率：{fmt_pct(summary.get('hit_rate_pct'))}。",
        f"- 已完成复盘：{summary.get('completed_review_count', 0)}；到期未复盘：{summary.get('due_checkpoint_count', 0)}；待到期：{summary.get('pending_checkpoint_count', 0)}；平均实时收益：{fmt_pct(summary.get('avg_live_return_pct'))}。",
        f"- 20D 命中率：{fmt_pct(backtest.get('hit_rate_20d'))}；平均收益：{fmt_pct(backtest.get('average_forward_return_20d'))}；SPY 超额：{fmt_pct(backtest.get('average_spy_excess_20d'))}；Rank IC：{fmt_num(backtest.get('rank_ic_20d'), 3)}。",
        "",
        "## 主题复盘表",
        "",
        "| 主题 | 状态 | 年龄 | 初始分 | 当前分 | 分数变化 | 平均收益 | 最强标的 | 最弱标的 | 到期/待到期 |",
        "|---|---|---:|---:|---:|---:|---:|---|---|---|",
    ]
    for item in payload.get("themes", [])[:30]:
        best = item.get("best_security") if isinstance(item.get("best_security"), dict) else {}
        worst = item.get("worst_security") if isinstance(item.get("worst_security"), dict) else {}
        best_text = f"{best.get('code')} {fmt_pct(best.get('return_pct'))}" if best else "n/a"
        worst_text = f"{worst.get('code')} {fmt_pct(worst.get('return_pct'))}" if worst else "n/a"
        due_text = f"{item.get('due_checkpoint_count', 0)} / {item.get('pending_checkpoint_count', 0)}"
        lines.append(
            f"| {item.get('theme_name')} | {item.get('status')} | {item.get('age_days')}天 | {fmt_num(item.get('initial_score'))} | {fmt_num(item.get('current_score'))} | {fmt_num(item.get('score_delta'))} | {fmt_pct(item.get('avg_return_pct'))} | {best_text} | {worst_text} | {due_text} |"
        )

    completed = payload.get("completed_reviews") if isinstance(payload.get("completed_reviews"), list) else []
    lines.extend(["", "## 已完成 checkpoint", ""])
    if completed:
        lines.extend(["| 主题 | 天数 | 结果 | 初始分 | 当前分 | 平均价格变化 |", "|---|---:|---|---:|---:|---:|"])
        for item in completed[:30]:
            lines.append(
                f"| {item.get('theme_name')} | {item.get('checkpoint_days')} | {item.get('result')} | {fmt_num(item.get('initial_score'))} | {fmt_num(item.get('current_score'))} | {fmt_pct(item.get('avg_price_change_pct'))} |"
            )
    else:
        lines.append("当前还没有完成的 5/20/60/120 交易日 checkpoint；未成熟主题不会被计入命中率。")

    lines.extend(["", "## 使用纪律", ""])
    for item in payload.get("discipline", []):
        lines.append(f"- {item}")
    lines.extend(
        [
            "- 逻辑增强/分数上升只属于研究状态，不计入命中率。",
            "- 缺失未来价格会进入 missing-data denominator 审计，不会静默跳过。",
            "- 样本不足时 IC、Sharpe、Information Ratio 和 Top-Decile Spread 保持为空，不伪造结果。",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    parser.add_argument("--opportunity-radar", type=Path, default=DEFAULT_OPPORTUNITY_RADAR)
    parser.add_argument("--market-pack", type=Path, default=DEFAULT_MARKET_PACK)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--docs-out", type=Path, default=DEFAULT_DOCS_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    payload = build_payload(
        load_json(args.journal, {}),
        load_json(args.opportunity_radar, {}),
        load_json(args.market_pack, {}),
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
