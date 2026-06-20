#!/usr/bin/env python3
"""Generate entry-path radar, thesis cards, and missed-opportunity review."""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
CONFIG_DIR = ROOT / "config"
OBSIDIAN_ROOT = Path("D:/codex-AI-agent/US-Agent")
THESIS_DIR = OBSIDIAN_ROOT / "stocks" / "thesis"
DISCIPLINE_DIR = OBSIDIAN_ROOT / "research-discipline"
STATE_DIR = DATA_DIR / "research_discipline"
HISTORY_DIR = STATE_DIR / "history"

MARKER_START = "<!-- AUTO-SNAPSHOT:START -->"
MARKER_END = "<!-- AUTO-SNAPSHOT:END -->"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def now_local() -> datetime:
    return datetime.now().astimezone()


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace("%", "").replace(",", "").strip()
        if not cleaned or cleaned.lower() in {"n/a", "nan", "none", "--"}:
            return None
        try:
            parsed = float(cleaned)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def parse_source_time(value: Any) -> tuple[datetime, str] | None:
    """Parse common quote/chart/filing timestamps.

    Returns (aware UTC datetime, precision). Date-only values are marked as
    "date"; they are useful for ordering but not intraday alignment.
    """
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        return datetime.fromtimestamp(timestamp, timezone.utc), "datetime"
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or raw.lower() in {"n/a", "none", "null", "--", "数据不足"}:
        return None
    iso = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc), "datetime" if "T" in raw or ":" in raw else "date"
    except ValueError:
        pass

    formats = (
        ("%b %d, %Y", "date"),
        ("%B %d, %Y", "date"),
        ("%Y-%m-%d", "date"),
        ("%Y/%m/%d", "date"),
        ("%m/%d/%Y", "date"),
        ("%Y-%m-%d %H:%M:%S", "datetime"),
        ("%Y-%m-%d %H:%M", "datetime"),
        ("%Y/%m/%d %H:%M:%S", "datetime"),
        ("%Y/%m/%d %H:%M", "datetime"),
    )
    for fmt, precision in formats:
        try:
            parsed = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return parsed, precision
        except ValueError:
            continue
    return None


def pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "数据不足"
    return f"{value:.{digits}f}%"


def money(value: float | None) -> str:
    if value is None:
        return "数据不足"
    return f"${value:,.2f}"


def ratio(value: float | None) -> str:
    if value is None:
        return "数据不足"
    return f"{value:.2f}:1"


def pe_value(value: float | None) -> str:
    if value is None:
        return "数据不足"
    return f"{value:.1f}x"


def valuation_summary(row: dict[str, Any]) -> str:
    parts = [
        f"Forward P/E {pe_value(row.get('forward_pe'))}",
        f"Trailing P/E {pe_value(row.get('trailing_pe'))}",
    ]
    valuation_pe = row.get("valuation_pe")
    if valuation_pe is not None:
        source = str(row.get("valuation_pe_source") or row.get("valuation_source") or "").strip()
        parts.append(f"采用估值P/E {pe_value(valuation_pe)}" + (f"（{source}）" if source else ""))
    finnhub_pe = row.get("finnhub_pe")
    if finnhub_pe is not None:
        metric = str(row.get("finnhub_pe_metric") or "").strip()
        parts.append(f"Finnhub P/E {pe_value(finnhub_pe)}" + (f"（{metric}）" if metric else ""))
    estimated = row.get("estimated_pe_from_sec")
    if estimated is not None:
        parts.append(f"粗算市值/年净利PE {pe_value(estimated)}")
    else:
        parts.append("粗算市值/年净利PE 数据不足")
    source = str(row.get("valuation_source") or "").strip()
    if source:
        parts.append(f"来源：{source}")
    return "；".join(parts)


def rr(entry: float | None, target: float | None, stop: float | None) -> float | None:
    if entry is None or target is None or stop is None:
        return None
    if stop >= entry or target <= entry:
        return None
    downside = entry - stop
    if downside <= 0:
        return None
    return round((target - entry) / downside, 2)


def gap_pct(current: float | None, trigger: float | None) -> float | None:
    if current is None or trigger is None or trigger <= 0:
        return None
    return round((current / trigger - 1) * 100, 2)


def safe_slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().upper())
    return cleaned.strip("-") or "UNKNOWN"


@dataclass
class PathCheck:
    label: str
    trigger: float | None
    stop: float | None
    target: float | None
    reward_risk: float | None
    status: str
    action: str


def classify_current_path(
    price: float | None,
    strict_entry: float | None,
    target: float | None,
    stop: float | None,
    min_rr: float,
) -> PathCheck:
    path_rr = rr(price, target, stop)
    if price is None:
        return PathCheck("当前价试仓", None, stop, target, None, "数据不足", "缺少当前价，不能执行。")
    if path_rr is None or path_rr < min_rr:
        return PathCheck("当前价试仓", price, stop, target, path_rr, "不满足", "当前价风险收益比不足，不能普通买入。")
    if strict_entry is not None and price > strict_entry * 1.05:
        return PathCheck("当前价试仓", price, stop, target, path_rr, "谨慎", "R/R 达标但偏离首买区，需等待结构确认或缩小观察。")
    return PathCheck("当前价试仓", price, stop, target, path_rr, "可复核", "进入本地组合复核：现金、单票上限、整股数量。")


def classify_pullback_path(
    price: float | None,
    pullback_entry: float | None,
    target: float | None,
    stop: float | None,
    min_rr: float,
) -> PathCheck:
    path_rr = rr(pullback_entry, target, stop)
    if pullback_entry is None:
        return PathCheck("理想回调", None, stop, target, None, "数据不足", "缺少支撑/回调价，不能设为计划。")
    if path_rr is None or path_rr < min_rr:
        return PathCheck("理想回调", pullback_entry, stop, target, path_rr, "不满足", "即使回调到该位置，R/R 仍不足。")
    gap = None
    if price is not None and price > 0:
        gap = round((price - pullback_entry) / price * 100, 2)
    if gap is not None and gap <= 2:
        return PathCheck("理想回调", pullback_entry, stop, target, path_rr, "接近/进入", "价格已接近回调区，进入本地组合复核。")
    if gap is not None and gap <= 8:
        return PathCheck("理想回调", pullback_entry, stop, target, path_rr, "临近", "距离回调区不远，设置提醒，不机械错过。")
    if gap is not None and gap > 15:
        return PathCheck(
            "理想回调",
            pullback_entry,
            stop,
            target,
            path_rr,
            "深度等待",
            f"需要从当前价回落约 {gap:.1f}%，这是低概率深回调，不作为主入场；优先跟踪突破确认/浅回踩，避免长期踏空。",
        )
    return PathCheck("理想回调", pullback_entry, stop, target, path_rr, "等待", "回调价仍较远，不能把它当作唯一入场方案。")


def classify_breakout_path(
    price: float | None,
    high252: float | None,
    ma50: float | None,
    low20: float | None,
    target: float | None,
    min_rr: float,
) -> PathCheck:
    if high252 is None or high252 <= 0:
        return PathCheck("突破确认", None, None, target, None, "数据不足", "缺少 52 周高点/阻力数据。")
    trigger = round(high252 * 1.005, 2)
    stop_candidates = [value for value in [ma50, low20, trigger * 0.93] if value is not None and value < trigger]
    stop = round(max(stop_candidates), 2) if stop_candidates else None
    path_rr = rr(trigger, target, stop)
    if path_rr is None or path_rr < min_rr:
        return PathCheck("突破确认", trigger, stop, target, path_rr, "不满足", "突破价位对应 R/R 不足，不能用突破追高解决错过焦虑。")
    gap = gap_pct(price, trigger)
    if gap is not None and gap >= 0:
        return PathCheck("突破确认", trigger, stop, target, path_rr, "已突破待验证", "需要收盘站稳、量能确认，并用本地组合复核。")
    if gap is not None and gap >= -3:
        return PathCheck("突破确认", trigger, stop, target, path_rr, "接近触发", "接近突破触发价，加入重点观察。")
    return PathCheck("突破确认", trigger, stop, target, path_rr, "等待", "尚未接近突破触发。")


def candidate_map(pack: dict[str, Any], compact: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for item in pack.get("candidates", []) if isinstance(pack.get("candidates"), list) else []:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").upper()
        if ticker:
            output[ticker] = item
    for field in ("research_candidates", "holdings_detail"):
        for item in compact.get(field, []) if isinstance(compact.get(field), list) else []:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").upper()
            if ticker:
                output.setdefault(ticker, {}).update(item)
    return output


def extract_candidate_fields(ticker: str, item: dict[str, Any]) -> dict[str, Any]:
    chart = item.get("chart") if isinstance(item.get("chart"), dict) else {}
    technicals = item.get("technicals") if isinstance(item.get("technicals"), dict) else {}
    entry = item.get("entry") if isinstance(item.get("entry"), dict) else {}
    sec = item.get("sec") if isinstance(item.get("sec"), dict) else {}
    financials = item.get("financials") if isinstance(item.get("financials"), dict) else {}
    scores = item.get("mechanical_scores") if isinstance(item.get("mechanical_scores"), dict) else {}
    return {
        "ticker": ticker,
        "name": item.get("name") or item.get("shortName") or ticker,
        "price": number(item.get("price")),
        "quote_time": item.get("quote_time"),
        "quote_source": item.get("quote_source"),
        "chart_time": item.get("chart_time") or chart.get("chart_time") or technicals.get("chart_time"),
        "chart_source": item.get("chart_source") or chart.get("source") or technicals.get("source"),
        "chart_cached_at_utc": item.get("chart_cached_at_utc") or chart.get("cached_at_utc") or technicals.get("cached_at_utc"),
        "sec_filing_poll_time_utc": item.get("sec_filing_poll_time_utc") or sec.get("filing_poll_time_utc"),
        "strict_entry": number(item.get("strict_entry") or entry.get("strict_entry")),
        "add_zone": number(item.get("add_zone") or entry.get("add_zone")),
        "invalidation": number(item.get("invalidation") or entry.get("invalidation")),
        "target": number(item.get("mechanical_target") or entry.get("mechanical_target")),
        "mechanical_rr": number(item.get("reward_risk") or entry.get("reward_risk")),
        "overall_score": number(item.get("overall_score") or scores.get("overall")),
        "quality_score": number(item.get("quality_score") or scores.get("quality")),
        "valuation_score": number(item.get("valuation_score") or scores.get("valuation")),
        "technical_score": number(item.get("technical_score") or scores.get("technical")),
        "data_confidence": number(item.get("data_confidence")),
        "forward_pe": number(item.get("forward_pe")),
        "trailing_pe": number(item.get("trailing_pe")),
        "finnhub_pe": number(item.get("finnhub_pe") or financials.get("finnhub_pe")),
        "finnhub_pe_metric": item.get("finnhub_pe_metric") or financials.get("finnhub_pe_metric"),
        "estimated_pe_from_sec": number(item.get("estimated_pe_from_sec") or financials.get("estimated_pe_from_sec")),
        "valuation_pe": number(item.get("valuation_pe") or financials.get("valuation_pe")),
        "valuation_pe_source": item.get("valuation_pe_source") or financials.get("valuation_pe_source"),
        "valuation_source": item.get("valuation_source") or financials.get("valuation_source"),
        "ma20": number(chart.get("ma20") or technicals.get("ma20")),
        "ma50": number(chart.get("ma50") or technicals.get("ma50")),
        "ma200": number(chart.get("ma200") or technicals.get("ma200")),
        "low20": number(chart.get("low20") or technicals.get("low20")),
        "low60": number(chart.get("low60") or technicals.get("low60")),
        "high252": number(chart.get("high252") or technicals.get("high252")),
        "prior_high252": number(chart.get("prior_high252") or technicals.get("prior_high252")),
        "low252": number(chart.get("low252") or technicals.get("low252")),
        "revenue_growth_yoy": number(sec.get("revenue_growth_yoy") if sec else financials.get("revenue_growth_yoy")),
        "net_margin": number(sec.get("net_margin") if sec else financials.get("net_margin")),
        "recent_filings": (sec.get("recent_filings") if sec else financials.get("recent_filings")) or [],
    }


def build_radar_rows(
    tickers: list[str],
    cmap: dict[str, dict[str, Any]],
    min_rr: float,
    focus_score_threshold: float,
    min_confidence: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ticker in tickers:
        fields = extract_candidate_fields(ticker, cmap.get(ticker, {"ticker": ticker}))
        price = fields["price"]
        target = fields["target"]
        stop = fields["invalidation"]
        strict_entry = fields["strict_entry"]
        pullback_entry = fields["add_zone"] or strict_entry
        breakout_high = fields["prior_high252"] or fields["high252"]
        paths = [
            classify_current_path(price, strict_entry, target, stop, min_rr),
            classify_pullback_path(price, pullback_entry, target, stop, min_rr),
            classify_breakout_path(price, breakout_high, fields["ma50"], fields["low20"], target, min_rr),
        ]
        actionable = [path for path in paths if path.status in {"可复核", "接近/进入", "接近触发", "已突破待验证"}]
        score_ok = fields.get("overall_score") is not None and fields["overall_score"] >= focus_score_threshold
        confidence_ok = fields.get("data_confidence") is not None and fields["data_confidence"] >= min_confidence
        if actionable:
            status = "重点" if score_ok and confidence_ok else "观察"
        elif any(path.status in {"临近", "谨慎"} for path in paths):
            status = "观察"
        elif all(path.status == "数据不足" for path in paths):
            status = "数据不足"
        else:
            status = "等待"
        rows.append({**fields, "paths": paths, "radar_status": status})
    rows.sort(
        key=lambda row: (
            {"重点": 0, "观察": 1, "等待": 2, "数据不足": 3}.get(str(row["radar_status"]), 9),
            -(row.get("overall_score") or -999),
        )
    )
    return rows


def source_summary(pack: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for row in rows:
        source = str(row.get("quote_source") or "数据不足")
        counts[source] = counts.get(source, 0) + 1
    futu_count = sum(count for source, count in counts.items() if "Futu OpenD" in source)
    fallback_count = len(rows) - futu_count
    cache_stats = pack.get("cache_stats") if isinstance(pack.get("cache_stats"), dict) else {}
    note = (
        f"报价源统计：Futu {futu_count} 个，公开 fallback {fallback_count} 个；"
        f"本轮行情包记录 Futu 报价 {cache_stats.get('futu_quotes', '数据不足')} 个、公开 fallback {cache_stats.get('public_quote_fallbacks', '数据不足')} 个。"
    )
    if fallback_count:
        note += " 公开 fallback 价格只适合观察，执行前必须用本地 Futu/券商报价复核。"
    return note, counts


def path_summary(path: PathCheck) -> str:
    return f"{path.status} / 触发 {money(path.trigger)} / 止损 {money(path.stop)} / 目标 {money(path.target)} / R/R {ratio(path.reward_risk)}"


def render_entry_radar(
    rows: list[dict[str, Any]],
    as_of: str,
    min_rr: float,
    pack: dict[str, Any],
    focus_score_threshold: float,
    min_confidence: float,
) -> str:
    source_note, _counts = source_summary(pack, rows)
    lines = [
        "# 入场路径雷达",
        "",
        f"- 生成时间：{now_local().strftime('%Y-%m-%d %H:%M')}",
        f"- 数据时间：{as_of or '数据不足'}",
        f"- 硬门槛：普通买入、当前价试仓、突破确认均需独立满足 R/R >= {min_rr:.1f}:1。",
        "- 防未来函数：突破确认价优先使用上一交易日 252 日高点；缺失时只降级观察，并在审计报告中提示。",
        f"- 重点门槛：路径可行 + 机械分数 >= {focus_score_threshold:.1f} + 数据置信度 >= {min_confidence:.2f}。",
        f"- 数据质量：{source_note}",
        "- 执行边界：复星证券只能买整股；最终股数必须回到本地组合复核。",
        "",
        "## 雷达总览",
        "",
        "| 股票 | 状态 | 当前价 | 当前价试仓 | 理想回调 | 突破确认 | 分数 | 置信度 | 报价源 |",
        "|---|---|---:|---|---|---|---:|---:|---|",
    ]
    for row in rows:
        paths = {path.label: path for path in row["paths"]}
        lines.append(
            "| {ticker} | {status} | {price} | {current} | {pullback} | {breakout} | {score} | {confidence} | {source} |".format(
                ticker=row["ticker"],
                status=row["radar_status"],
                price=money(row["price"]),
                current=path_summary(paths["当前价试仓"]),
                pullback=path_summary(paths["理想回调"]),
                breakout=path_summary(paths["突破确认"]),
                score=f"{row['overall_score']:.0f}" if row.get("overall_score") is not None else "数据不足",
                confidence=f"{row['data_confidence']:.2f}" if row.get("data_confidence") is not None else "数据不足",
                source=str(row.get("quote_source") or "数据不足").replace("|", "/"),
            )
        )

    lines.extend(["", "## 重点观察", ""])
    focus_rows = [row for row in rows if row["radar_status"] in {"重点", "观察"}]
    if not focus_rows:
        lines.append("今日没有进入重点或临近观察的标的。")
    for row in focus_rows[:12]:
        lines.append(f"### {row['ticker']} - {row.get('name') or row['ticker']}")
        lines.append("")
        lines.append(f"- 当前价：{money(row['price'])}")
        lines.append(f"- 估值：{valuation_summary(row)}")
        lines.append(f"- 技术：MA50 {money(row['ma50'])}；MA200 {money(row['ma200'])}；52周高点 {money(row['high252'])}")
        for path in row["paths"]:
            lines.append(f"- {path.label}：{path_summary(path)}。{path.action}")
        lines.append("")

    lines.extend(
        [
            "## 使用纪律",
            "",
            "- 理想回调超过当前价 15% 时只作为深度等待，不作为主方案；用突破确认/浅回踩提醒防止长期踏空。",
            "- 不因为理想回调价遥远就追高；突破确认也必须重新计算 R/R。",
            "- 如果当前价、回调、突破三条路径都不满足 R/R，结论就是等待。",
            "- 如果标的进入“重点”，只代表进入本地组合复核，不代表自动买入。",
        ]
    )
    return "\n".join(lines) + "\n"


def compact_history_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        paths = {path.label: path for path in row["paths"]}
        output.append(
            {
                "ticker": row["ticker"],
                "price": row["price"],
                "status": row["radar_status"],
                "overall_score": row.get("overall_score"),
                "quote_time": row.get("quote_time"),
                "quote_source": row.get("quote_source"),
                "chart_time": row.get("chart_time"),
                "chart_source": row.get("chart_source"),
                "chart_cached_at_utc": row.get("chart_cached_at_utc"),
                "sec_filing_poll_time_utc": row.get("sec_filing_poll_time_utc"),
                "high252": row.get("high252"),
                "prior_high252": row.get("prior_high252"),
                "current_path_status": paths["当前价试仓"].status,
                "pullback_status": paths["理想回调"].status,
                "breakout_status": paths["突破确认"].status,
            }
        )
    return output


def build_missed_review(rows: list[dict[str, Any]], previous: dict[str, Any], threshold_pct: float) -> tuple[str, list[dict[str, Any]]]:
    prev_items = {
        str(item.get("ticker", "")).upper(): item
        for item in previous.get("rows", [])
        if isinstance(item, dict) and item.get("ticker")
    }
    misses: list[dict[str, Any]] = []
    for row in rows:
        ticker = row["ticker"]
        previous_row = prev_items.get(ticker)
        if not previous_row:
            continue
        previous_price = number(previous_row.get("price"))
        current_price = number(row.get("price"))
        move = gap_pct(current_price, previous_price)
        if move is None or move < threshold_pct:
            continue
        previous_status = str(previous_row.get("status") or "")
        previous_current = str(previous_row.get("current_path_status") or "")
        if previous_status in {"等待", "观察", "数据不足"} and previous_current not in {"可复核"}:
            misses.append(
                {
                    "ticker": ticker,
                    "previous_price": previous_price,
                    "current_price": current_price,
                    "move_pct": move,
                    "previous_status": previous_status,
                    "current_status": row["radar_status"],
                    "review_question": "当时没买是纪律正确，还是入场条件过于苛刻？检查是否应启用突破确认路径。",
                }
            )

    lines = [
        "# 错过机会复盘",
        "",
        f"- 生成时间：{now_local().strftime('%Y-%m-%d %H:%M')}",
        f"- 触发阈值：上次记录后上涨 >= {threshold_pct:.1f}%，且此前不是当前价可复核。",
        "",
    ]
    if not misses:
        lines.append("本次没有触发错过机会复盘的标的。")
    else:
        lines.extend(["| 股票 | 上次价 | 当前价 | 涨幅 | 上次状态 | 当前状态 | 复盘问题 |", "|---|---:|---:|---:|---|---|---|"])
        for item in misses:
            lines.append(
                f"| {item['ticker']} | {money(item['previous_price'])} | {money(item['current_price'])} | {pct(item['move_pct'])} | {item['previous_status']} | {item['current_status']} | {item['review_question']} |"
            )
        lines.extend(
            [
                "",
                "## 复盘纪律",
                "",
                "- 如果当时 R/R 不足，错过上涨不自动等于错误。",
                "- 如果突破确认路径已经达标但系统没有提醒，需要提高突破雷达权重。",
                "- 如果多次因为理想回调价过远而错过，需要重新校准首买区和突破确认规则。",
            ]
        )
    return "\n".join(lines) + "\n", misses


def add_audit_issue(
    issues: list[dict[str, Any]],
    severity: str,
    area: str,
    detail: str,
    *,
    ticker: str = "-",
    source_time: Any = None,
) -> None:
    issues.append(
        {
            "severity": severity,
            "area": area,
            "ticker": ticker,
            "detail": detail,
            "source_time": source_time,
        }
    )


def audit_time_field(
    issues: list[dict[str, Any]],
    *,
    ticker: str,
    area: str,
    raw_time: Any,
    signal_time: datetime,
    required: bool = True,
    stale_after: timedelta | None = None,
) -> None:
    parsed = parse_source_time(raw_time)
    if parsed is None:
        if required:
            add_audit_issue(issues, "WARN", area, "缺少可解析时间戳，不能证明该字段没有未来函数。", ticker=ticker, source_time=raw_time)
        return
    source_time, precision = parsed
    if source_time > signal_time + timedelta(minutes=5):
        add_audit_issue(
            issues,
            "FAIL",
            area,
            f"数据时间晚于信号时间：source={source_time.isoformat()}，signal={signal_time.isoformat()}。",
            ticker=ticker,
            source_time=raw_time,
        )
    elif precision == "date":
        add_audit_issue(issues, "INFO", area, "只有日期级时间戳，无法做盘中级别对齐。", ticker=ticker, source_time=raw_time)
    if stale_after is not None and signal_time - source_time > stale_after:
        add_audit_issue(
            issues,
            "WARN",
            area,
            f"数据偏旧：距离信号时间超过 {int(stale_after.total_seconds() // 3600)} 小时。",
            ticker=ticker,
            source_time=raw_time,
        )


def build_future_function_audit(
    rows: list[dict[str, Any]],
    pack: dict[str, Any],
    compact: dict[str, Any],
    as_of: str,
    previous: dict[str, Any],
    history_path: Path,
) -> tuple[str, list[dict[str, Any]]]:
    parsed_signal = parse_source_time(as_of)
    signal_time = parsed_signal[0] if parsed_signal else datetime.now(timezone.utc)
    issues: list[dict[str, Any]] = []

    if parsed_signal is None:
        add_audit_issue(issues, "FAIL", "信号时间", "行情包缺少可解析 as_of_utc，无法进行严格未来函数审计。")
    if not previous.get("rows"):
        add_audit_issue(issues, "INFO", "历史快照", "没有上一轮入场雷达快照；错过机会复盘从本次之后才具备连续性。")
    if not history_path.exists():
        add_audit_issue(issues, "WARN", "历史快照", f"本轮不可覆盖快照尚未写入：{history_path.name}。")

    cache_stats = pack.get("cache_stats") if isinstance(pack.get("cache_stats"), dict) else {}
    public_fallbacks = number(cache_stats.get("public_quote_fallbacks"))
    futu_quotes = number(cache_stats.get("futu_quotes"))
    if public_fallbacks and public_fallbacks > 0:
        add_audit_issue(
            issues,
            "WARN",
            "报价源",
            f"本轮存在 {int(public_fallbacks)} 个公开 fallback 报价；执行交易前必须用 Futu/券商报价复核。",
        )
    if futu_quotes == 0 and public_fallbacks:
        add_audit_issue(issues, "WARN", "报价源", "Futu 实时报价为 0，当前入场信号不能直接作为下单依据。")

    for row in rows:
        ticker = str(row.get("ticker") or "-")
        audit_time_field(
            issues,
            ticker=ticker,
            area="报价时间",
            raw_time=row.get("quote_time"),
            signal_time=signal_time,
            required=True,
            stale_after=timedelta(hours=96),
        )
        audit_time_field(
            issues,
            ticker=ticker,
            area="K线时间",
            raw_time=row.get("chart_time"),
            signal_time=signal_time,
            required=True,
            stale_after=timedelta(days=7),
        )
        audit_time_field(
            issues,
            ticker=ticker,
            area="K线缓存",
            raw_time=row.get("chart_cached_at_utc"),
            signal_time=signal_time,
            required=False,
        )
        audit_time_field(
            issues,
            ticker=ticker,
            area="SEC轮询时间",
            raw_time=row.get("sec_filing_poll_time_utc"),
            signal_time=signal_time,
            required=False,
        )

        quote_parsed = parse_source_time(row.get("quote_time"))
        chart_parsed = parse_source_time(row.get("chart_time"))
        if quote_parsed and chart_parsed and chart_parsed[0].date() > quote_parsed[0].date():
            add_audit_issue(
                issues,
                "WARN",
                "数据对齐",
                "K线日期晚于报价日期；当前价、MA、52周高低点不在同一时间截面，执行前必须刷新报价。",
                ticker=ticker,
                source_time=f"quote={row.get('quote_time')} / chart={row.get('chart_time')}",
            )

        if row.get("high252") is not None and row.get("prior_high252") is None:
            add_audit_issue(
                issues,
                "WARN",
                "突破触发",
                "缺少 prior_high252；突破确认只能降级参考，不能用于严格回测。",
                ticker=ticker,
            )

        if row.get("radar_status") in {"重点", "观察"} and "fallback" in str(row.get("quote_source") or "").lower():
            add_audit_issue(
                issues,
                "WARN",
                "执行纪律",
                "入场雷达关注项使用公开 fallback 报价；下单前必须用券商实时价重新计算整股数量与 R/R。",
                ticker=ticker,
                source_time=row.get("quote_time"),
            )

        recent_filings = row.get("recent_filings") if isinstance(row.get("recent_filings"), list) else []
        for filing in recent_filings[:5]:
            if not isinstance(filing, dict):
                continue
            filed = filing.get("filed")
            parsed_filed = parse_source_time(filed)
            if parsed_filed and parsed_filed[0].date() > signal_time.date():
                add_audit_issue(
                    issues,
                    "FAIL",
                    "财报/公告",
                    f"公告 filed 日期晚于信号日期：{filed}。",
                    ticker=ticker,
                    source_time=filed,
                )

    severity_order = {"FAIL": 0, "WARN": 1, "INFO": 2}
    issues.sort(key=lambda item: (severity_order.get(str(item["severity"]), 9), str(item["ticker"]), str(item["area"])))
    counts = {level: sum(1 for item in issues if item["severity"] == level) for level in ("FAIL", "WARN", "INFO")}
    verdict = "FAIL" if counts["FAIL"] else "WARN" if counts["WARN"] else "PASS"
    lines = [
        "# 未来函数与交易纪律审计",
        "",
        f"- 生成时间：{now_local().strftime('%Y-%m-%d %H:%M')}",
        f"- 信号时间：{as_of or '数据不足'}",
        f"- 审计结论：{verdict}",
        f"- 统计：FAIL {counts['FAIL']} / WARN {counts['WARN']} / INFO {counts['INFO']}",
        f"- 不可覆盖快照：data/research_discipline/history/{history_path.name}",
        "",
        "## 结论解释",
        "",
        "- PASS：未发现未来时间戳或关键缺口。",
        "- WARN：没有确认未来函数，但存在时间戳不足、数据源降级、缓存或执行纪律风险。",
        "- FAIL：发现数据时间晚于信号时间，不能用于交易或复盘，需要先修复。",
        "",
        "## 审计明细",
        "",
    ]
    if not issues:
        lines.append("未发现未来函数或交易纪律问题。")
    else:
        lines.extend(["| 级别 | 股票 | 模块 | 来源时间 | 问题 |", "|---|---|---|---|---|"])
        for item in issues[:120]:
            source_time = str(item.get("source_time") or "-").replace("|", "/")
            detail = str(item.get("detail") or "").replace("|", "/")
            lines.append(f"| {item['severity']} | {item['ticker']} | {item['area']} | {source_time} | {detail} |")
        if len(issues) > 120:
            lines.append(f"| INFO | - | 截断 | - | 仅展示前 120 条，完整问题数量 {len(issues)}。 |")

    lines.extend(
        [
            "",
            "## 已加固规则",
            "",
            "- 入场雷达每次运行写入不可覆盖快照，避免用当前数据重写过去判断。",
            "- 突破确认优先使用上一交易日 252 日高点，减少盘中和回测穿越。",
            "- 报价、K线、SEC 公告分别检查 source_time 是否晚于 signal_time。",
            "- 公开 fallback 报价只允许观察，交易执行前必须回到 Futu/券商报价复核。",
            "",
        ]
    )
    return "\n".join(lines), issues


def thesis_template(ticker: str, name: str) -> str:
    return f"""---
type: stock-thesis
ticker: {ticker}
status: active
---

# {ticker} - {name}

## 投资论点

- 关注/持有原因：
- 核心增长逻辑：
- 估值锚：
- 关键风险：
- 失效条件：
- 下一次财报重点：
- 当前结论：未填写

## 人工复盘记录

-

{MARKER_START}
{MARKER_END}
"""


def render_auto_snapshot(row: dict[str, Any]) -> str:
    paths = {path.label: path for path in row["paths"]}
    lines = [
        MARKER_START,
        "",
        "## 自动快照",
        "",
        f"- 更新时间：{now_local().strftime('%Y-%m-%d %H:%M')}",
        f"- 当前价：{money(row['price'])}",
        f"- 雷达状态：{row['radar_status']}",
        f"- 机械分数：{row['overall_score']:.0f}" if row.get("overall_score") is not None else "- 机械分数：数据不足",
        f"- 数据置信度：{row['data_confidence']:.2f}" if row.get("data_confidence") is not None else "- 数据置信度：数据不足",
        f"- Forward P/E：{row['forward_pe']:.1f}" if row.get("forward_pe") is not None else "- Forward P/E：数据不足",
        f"- 收入同比：{pct(row['revenue_growth_yoy'] * 100 if row.get('revenue_growth_yoy') is not None and abs(row['revenue_growth_yoy']) < 5 else row.get('revenue_growth_yoy'))}",
        "",
        "### 入场路径",
        "",
        f"- 当前价试仓：{path_summary(paths['当前价试仓'])}",
        f"- 理想回调：{path_summary(paths['理想回调'])}",
        f"- 突破确认：{path_summary(paths['突破确认'])}",
        "",
        "### 下次人工检查",
        "",
        "- 投资论点是否增强/削弱：",
        "- 估值锚是否需要调整：",
        "- 失效条件是否触发：",
        "",
        MARKER_END,
    ]
    return "\n".join(lines)


def update_thesis_card(row: dict[str, Any]) -> Path:
    ticker = safe_slug(row["ticker"])
    path = THESIS_DIR / f"{ticker}.md"
    name = str(row.get("name") or ticker)
    snapshot = render_auto_snapshot(row)
    if path.exists():
        content = path.read_text(encoding="utf-8")
        pattern = re.compile(re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END), re.DOTALL)
        if pattern.search(content):
            content = pattern.sub(snapshot, content)
        else:
            content = content.rstrip() + "\n\n" + snapshot + "\n"
    else:
        content = thesis_template(ticker, name)
        content = content.replace(f"{MARKER_START}\n{MARKER_END}", snapshot)
    write_text(path, content)
    return path


def render_thesis_index(paths: list[Path]) -> str:
    lines = [
        "# 股票投资论点档案库",
        "",
        f"- 更新时间：{now_local().strftime('%Y-%m-%d %H:%M')}",
        f"- 卡片数量：{len(paths)}",
        "",
        "| 股票 | 档案 |",
        "|---|---|",
    ]
    for path in sorted(paths):
        ticker = path.stem
        rel = path.relative_to(OBSIDIAN_ROOT).as_posix()
        lines.append(f"| {ticker} | [[{rel[:-3]}]] |")
    return "\n".join(lines) + "\n"


def ordered_tickers(config: dict[str, Any], portfolio: dict[str, Any], cmap: dict[str, dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for holding in portfolio.get("holdings", []) if isinstance(portfolio.get("holdings"), list) else []:
        if isinstance(holding, dict) and holding.get("ticker"):
            values.append(str(holding["ticker"]).upper())
    for ticker in portfolio.get("watchlist", []) if isinstance(portfolio.get("watchlist"), list) else []:
        values.append(str(ticker).upper())
    for ticker in config.get("universe", []) if isinstance(config.get("universe"), list) else []:
        values.append(str(ticker).upper())
    for ticker in cmap:
        values.append(ticker)
    seen: set[str] = set()
    output = []
    for ticker in values:
        clean = safe_slug(ticker)
        if clean and clean not in seen:
            seen.add(clean)
            output.append(clean)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-pack", type=Path, default=DATA_DIR / "latest_market_pack.json")
    parser.add_argument("--compact-input", type=Path, default=DATA_DIR / "latest_agent_input.json")
    parser.add_argument("--portfolio", type=Path, default=CONFIG_DIR / "portfolio.json")
    parser.add_argument("--config", type=Path, default=CONFIG_DIR / "agent_config.json")
    parser.add_argument("--miss-threshold-pct", type=float, default=5.0)
    parser.add_argument("--max-thesis-cards", type=int, default=80)
    parser.add_argument("--focus-score-threshold", type=float, default=3.5)
    args = parser.parse_args()

    config = load_json(args.config, {})
    portfolio = load_json(args.portfolio, {})
    pack = load_json(args.market_pack, {})
    compact = load_json(args.compact_input, {})
    min_rr = float(config.get("min_reward_risk_for_buy", 2.0) or 2.0)
    cmap = candidate_map(pack, compact)
    tickers = ordered_tickers(config, portfolio, cmap)
    min_confidence = float(config.get("min_data_confidence_for_buy", 0.68) or 0.68)
    rows = build_radar_rows(tickers, cmap, min_rr, args.focus_score_threshold, min_confidence)
    as_of = str(pack.get("as_of_utc") or compact.get("as_of_utc") or datetime.now(timezone.utc).isoformat())

    entry_report = render_entry_radar(rows, as_of, min_rr, pack, args.focus_score_threshold, min_confidence)
    write_text(REPORTS_DIR / "latest-entry-radar.md", entry_report)
    stamp = now_local().strftime("%Y%m%d-%H%M")
    write_text(REPORTS_DIR / f"entry-radar-{stamp}.md", entry_report)

    previous = load_json(STATE_DIR / "entry_radar_latest.json", {})
    missed_report, misses = build_missed_review(rows, previous, args.miss_threshold_pct)
    write_text(REPORTS_DIR / "latest-missed-opportunity-review.md", missed_report)
    if misses:
        write_text(REPORTS_DIR / f"missed-opportunity-review-{stamp}.md", missed_report)
    snapshot = {
        "schema_version": 2,
        "as_of": as_of,
        "generated_at": now_local().isoformat(timespec="seconds"),
        "rows": compact_history_rows(rows),
    }
    history_path = HISTORY_DIR / f"entry_radar_{stamp}.json"
    write_json(
        STATE_DIR / "entry_radar_latest.json",
        snapshot,
    )
    write_json(history_path, snapshot)
    audit_report, audit_issues = build_future_function_audit(rows, pack, compact, as_of, previous, history_path)
    write_text(REPORTS_DIR / "latest-future-function-audit.md", audit_report)
    write_text(REPORTS_DIR / f"future-function-audit-{stamp}.md", audit_report)
    if misses:
        history = load_json(STATE_DIR / "missed_opportunities.json", [])
        if not isinstance(history, list):
            history = []
        history.extend({"detected_at": now_local().isoformat(timespec="seconds"), **item} for item in misses)
        write_json(STATE_DIR / "missed_opportunities.json", history[-500:])

    thesis_paths = [update_thesis_card(row) for row in rows[: max(1, args.max_thesis_cards)]]
    write_text(DISCIPLINE_DIR / "入场路径雷达.md", entry_report)
    write_text(DISCIPLINE_DIR / "错过机会复盘.md", missed_report)
    write_text(DISCIPLINE_DIR / "未来函数与交易纪律审计.md", audit_report)
    write_text(THESIS_DIR / "00-股票投资论点档案库.md", render_thesis_index(thesis_paths))

    print(f"Wrote {REPORTS_DIR / 'latest-entry-radar.md'}")
    print(f"Wrote {REPORTS_DIR / 'latest-missed-opportunity-review.md'}")
    print(f"Wrote {REPORTS_DIR / 'latest-future-function-audit.md'} ({len(audit_issues)} issues)")
    print(f"Updated {len(thesis_paths)} thesis cards under {THESIS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
