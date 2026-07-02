#!/usr/bin/env python3
"""Build a privacy-safe JSON archive for the public GitHub Pages report site."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "reports"
DEFAULT_OUTPUT = ROOT / "docs" / "data" / "reports.json"
DEFAULT_INDEX_OUTPUT = ROOT / "docs" / "data" / "index.json"
DEFAULT_DETAIL_DIR = ROOT / "docs" / "data" / "reports"
DEFAULT_REVIEW_METRICS_PATH = ROOT / "docs" / "data" / "opportunity_review_metrics.json"
DEFAULT_EVENT_EVIDENCE_PATH = ROOT / "data" / "latest_event_evidence.json"
DEFAULT_DOCS_EVENT_EVIDENCE_PATH = ROOT / "docs" / "data" / "event_evidence.json"
DEFAULT_OPPORTUNITY_RADAR_PATH = ROOT / "docs" / "data" / "opportunity_radar.json"
DEFAULT_CROSS_MARKET_PATH = ROOT / "docs" / "data" / "cross_market_intelligence.json"
DEFAULT_SECONDARY_QUEUE_PATH = ROOT / "docs" / "data" / "secondary_analysis_queue.json"
DEFAULT_FREE_DATA_FALLBACK_PATH = ROOT / "docs" / "data" / "free_data_fallback.json"
DEFAULT_MARKET_SENTIMENT_PATH = ROOT / "docs" / "data" / "market_sentiment.json"

PRIVATE_SECTION_MARKERS = (
    "持仓输入",
    "当前持仓",
    "持仓明细",
    "组合约束",
    "组合整体",
    "账户概览",
    "账户摘要",
    "资金记录",
    "本地输入来源",
    "本地数据源",
    "Portfolio",
    "Holdings",
    "Account",
)

PRIVATE_LINE_PATTERNS = (
    re.compile(r"(?:估算)?总资产\s*[:：]"),
    re.compile(r"(?:剩余)?现金(?:比例)?\s*[:：]"),
    re.compile(r"账户(?:本金|收益|回报|盈亏)\s*[:：]"),
    re.compile(r"单票上限\s*[:：]"),
    re.compile(r"累计(?:充值|提取)\s*[:：]"),
    re.compile(r"(?:持有|加仓|卖出)\s*\d+(?:\.\d+)?\s*股"),
    re.compile(r"\b(?:cost_basis|cash_usd|estimated_total_assets|net_deposit_usd|account_id|account_number)\b", re.IGNORECASE),
    re.compile(r"['\"]?shares['\"]?\s*[:=]", re.IGNORECASE),
    re.compile(r"[A-Z]:\\", re.IGNORECASE),
    re.compile(r"portfolio\.json", re.IGNORECASE),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b(?:DEEPSEEK|OPENAI|GITHUB|FUTU|FINNHUB|FRED|FMP)_[A-Z0-9_]*(?:KEY|TOKEN|SECRET)\b", re.IGNORECASE),
    re.compile(r"\.env(?:\b|$)", re.IGNORECASE),
)

PUBLIC_FOOTER_RE = re.compile(r"\n{0,2}---\n\n> 公开脱敏版：.*$", re.DOTALL)
VOLATILE_REPORT_LINE_RE = re.compile(r"^-\s+(?:生成时间|不可覆盖快照)：")

KIND_LABELS = {
    "weekly": "周度扫描",
    "quick": "快速更新",
    "buy-side": "Buy-Side",
    "deepseek-cloud": "DeepSeek云端",
    "entry-radar": "入场雷达",
    "missed-review": "错过复盘",
    "future-audit": "未来函数审计",
    "supply-chain": "产业链雷达",
    "opportunity-radar": "机会雷达",
    "cross-market-intelligence": "跨市场情报",
    "event-evidence": "事件证据",
    "opportunity-review-metrics": "机会复盘",
    "free-data-fallback": "免费数据源",
    "macro-regime": "宏观雷达",
    "market-sentiment": "市场情绪",
    "fmp-research": "FMP预期",
    "secondary-queue": "二次分析队列",
    "daily": "每日分析",
}

ONE_REPORT_PER_DAY_KINDS = {
    "supply-chain",
    "opportunity-radar",
    "cross-market-intelligence",
    "event-evidence",
    "opportunity-review-metrics",
    "free-data-fallback",
    "macro-regime",
    "market-sentiment",
    "fmp-research",
    "secondary-queue",
}

SYMBOL_RE = re.compile(r"\b(?:US|HK|CN|SH|SZ)\.[A-Z0-9]+\b")
THEME_HINTS = (
    "GPU",
    "ASIC",
    "HBM",
    "先进封装",
    "光模块",
    "CPO",
    "PCB",
    "覆铜板",
    "电子布",
    "铜箔",
    "液冷",
    "电力",
    "机器人",
    "Physical AI",
)


def report_kind(name: str) -> str:
    lowered = name.lower()
    if "entry-radar" in lowered:
        return "entry-radar"
    if "missed-opportunity" in lowered:
        return "missed-review"
    if "future-function-audit" in lowered:
        return "future-audit"
    if "supply-chain" in lowered or "supply_chain" in lowered:
        return "supply-chain"
    if "opportunity-radar" in lowered or "opportunity_radar" in lowered:
        return "opportunity-radar"
    if "cross-market-intelligence" in lowered or "cross_market_intelligence" in lowered:
        return "cross-market-intelligence"
    if "event-evidence" in lowered or "event_evidence" in lowered:
        return "event-evidence"
    if "opportunity-review-metrics" in lowered or "opportunity_review_metrics" in lowered:
        return "opportunity-review-metrics"
    if "free-data-fallback" in lowered or "free_data_fallback" in lowered:
        return "free-data-fallback"
    if "macro-regime" in lowered or "macro_regime" in lowered:
        return "macro-regime"
    if "market-sentiment" in lowered or "market_sentiment" in lowered:
        return "market-sentiment"
    if "fmp-research" in lowered or "fmp_research" in lowered:
        return "fmp-research"
    if "secondary-analysis" in lowered or "secondary_analysis" in lowered:
        return "secondary-queue"
    if "deepseek-cloud" in lowered:
        return "deepseek-cloud"
    if "weekly" in lowered:
        return "weekly"
    if "quick" in lowered:
        return "quick"
    if "public-equity" in lowered or "buy-side" in lowered:
        return "buy-side"
    return "daily"


def first_title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return fallback


def report_summary(content: str, max_length: int = 130) -> str:
    for raw_line in content.splitlines():
        line = re.sub(r"^[#>\-\*\s]+", "", raw_line).strip()
        if not line or line.startswith("|") or set(line) <= {"-", ":"}:
            continue
        if line.startswith("公开脱敏版"):
            continue
        return line[:max_length]
    return ""


def report_symbols(content: str, limit: int = 24) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for match in SYMBOL_RE.finditer(content):
        symbol = match.group(0)
        if symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
        if len(symbols) >= limit:
            break
    return symbols


def report_themes(content: str, limit: int = 12) -> list[str]:
    themes: list[str] = []
    lowered = content.lower()
    for theme in THEME_HINTS:
        if theme.lower() in lowered and theme not in themes:
            themes.append(theme)
        if len(themes) >= limit:
            break
    return themes


def public_report_metadata(item: dict[str, Any]) -> dict[str, Any]:
    content = str(item.get("content", ""))
    return {
        "id": item.get("id"),
        "filename": item.get("filename"),
        "title": item.get("title"),
        "kind": item.get("kind"),
        "kind_label": item.get("kind_label"),
        "published_at": item.get("published_at"),
        "published_label": item.get("published_label"),
        "is_latest": item.get("is_latest", False),
        "summary": item.get("summary") or report_summary(content),
        "symbols": item.get("symbols") if isinstance(item.get("symbols"), list) else report_symbols(content),
        "themes": item.get("themes") if isinstance(item.get("themes"), list) else report_themes(content),
    }


def strip_public_footer(content: str) -> str:
    return PUBLIC_FOOTER_RE.sub("", content.replace("\r\n", "\n")).strip()


def stable_content_identity(content: str) -> str:
    clean = strip_public_footer(content)
    lines = [
        line.rstrip()
        for line in clean.split("\n")
        if not VOLATILE_REPORT_LINE_RE.match(line.strip())
    ]
    return hashlib.sha256("\n".join(lines).strip().encode("utf-8")).hexdigest()


def sanitize_report(content: str) -> str:
    output: list[str] = []
    excluded_depth: int | None = None
    lines = strip_public_footer(content).split("\n")
    for line in lines:
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            depth = len(heading.group(1))
            title = heading.group(2).strip()
            if excluded_depth is not None and depth <= excluded_depth:
                excluded_depth = None
            if any(marker.lower() in title.lower() for marker in PRIVATE_SECTION_MARKERS):
                excluded_depth = depth
                continue
        if excluded_depth is not None:
            continue
        if any(pattern.search(line) for pattern in PRIVATE_LINE_PATTERNS):
            continue
        output.append(line.rstrip())

    compact: list[str] = []
    blank = False
    for line in output:
        is_blank = not line.strip()
        if is_blank and blank:
            continue
        compact.append(line)
        blank = is_blank
    sanitized = "\n".join(compact).strip()
    sanitized += (
        "\n\n---\n\n"
        "> 公开脱敏版：已移除账户金额、现金、股数、成本、本地路径、API Key 与持仓明细。"
        "仅供投研记录，不构成投资建议。\n"
    )
    return sanitized


def parse_report_time(path: Path, content: str) -> datetime:
    filename_patterns = (
        r"(20\d{6})[-_](\d{4})",
        r"(20\d{2}-\d{2}-\d{2})[-_](\d{2})(\d{2})",
    )
    for pattern in filename_patterns:
        match = re.search(pattern, path.name)
        if not match:
            continue
        try:
            groups = match.groups()
            if len(groups) == 2:
                return datetime.strptime("".join(groups), "%Y%m%d%H%M").astimezone()
            return datetime.strptime(" ".join((groups[0], groups[1] + ":" + groups[2])), "%Y-%m-%d %H:%M").astimezone()
        except ValueError:
            pass

    content_patterns = (
        r"生成时间\s*[:：]\s*(20\d{2}-\d{2}-\d{2})\s+(\d{2}:\d{2})",
        r"数据时间(?:\s+UTC)?\s*[:：]\s*(20\d{2}-\d{2}-\d{2})[T\s](\d{2}:\d{2})",
        r"Data timestamp(?:\s+UTC)?\s*[:：]\s*(20\d{2}-\d{2}-\d{2})[T\s](\d{2}:\d{2})",
    )
    for pattern in content_patterns:
        match = re.search(pattern, content)
        if match:
            try:
                parsed = datetime.strptime(" ".join(match.groups()), "%Y-%m-%d %H:%M")
                return parsed.astimezone()
            except ValueError:
                continue
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone()


def load_existing_reports(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    reports = payload.get("reports")
    if not isinstance(reports, list):
        return []
    clean: list[dict[str, Any]] = []
    for item in reports:
        if not isinstance(item, dict):
            continue
        content = sanitize_report(str(item.get("content", "")))
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        stable_digest = stable_content_identity(content)
        clean.append(
            {
                "id": str(item.get("id") or digest[:16]),
                "filename": str(item.get("filename") or "existing-report.md"),
                "title": str(item.get("title") or first_title(content, "existing-report")),
                "kind": str(item.get("kind") or "daily"),
                "kind_label": str(item.get("kind_label") or KIND_LABELS.get(str(item.get("kind") or "daily"), "每日分析")),
                "published_at": str(item.get("published_at") or datetime.now(timezone.utc).isoformat(timespec="minutes")),
                "published_label": str(item.get("published_label") or ""),
                "is_latest": bool(item.get("is_latest")),
                "content": content,
                "digest": digest,
                "stable_digest": stable_digest,
            }
        )
    return clean


def collect_report_files() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if not REPORTS_DIR.exists():
        return candidates
    for path in REPORTS_DIR.glob("*.md"):
        content = path.read_text(encoding="utf-8")
        sanitized = sanitize_report(content)
        digest = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
        stable_digest = stable_content_identity(sanitized)
        timestamp = parse_report_time(path, content)
        kind = report_kind(path.name)
        candidates.append(
            {
                "id": f"{timestamp.strftime('%Y%m%d-%H%M')}-{path.stem}",
                "filename": path.name,
                "title": first_title(sanitized, path.stem),
                "kind": kind,
                "kind_label": KIND_LABELS[kind],
                "published_at": timestamp.isoformat(timespec="minutes"),
                "published_label": timestamp.strftime("%Y-%m-%d %H:%M"),
                "is_latest": path.name.startswith("latest-"),
                "content": sanitized,
                "digest": digest,
                "stable_digest": stable_digest,
            }
        )
    return candidates


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def currency_for_symbol(symbol: str) -> str:
    if symbol.startswith("US."):
        return "USD"
    if symbol.startswith("HK."):
        return "HKD"
    if symbol.startswith(("CN.", "SH.", "SZ.")):
        return "CNY"
    return ""


def date_label(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:10]


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value == value:
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").replace("%", "").strip()
        if not cleaned or cleaned.lower() in {"n/a", "nan", "none", "null", "--", "数据不足", "待确认"}:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def matured_return(row: dict[str, Any], theme: dict[str, Any], days: int) -> float | None:
    """Return a conservative checkpoint return once a theme is old enough.

    The review-metrics generator currently records live per-symbol returns as
    ``return_pct``. Until it stores exact historical checkpoint closes, publish
    that value only after the requested age threshold has passed. This avoids
    showing a 7D value before the idea is seven calendar days old, while also
    avoiding stale "样本不足" after the checkpoint becomes due.
    """

    for key in (f"return_{days}d", f"return_{days}D"):
        explicit = number(row.get(key))
        if explicit is not None:
            return explicit
    age_days = number(theme.get("age_days"))
    live_return = number(row.get("return_pct"))
    if age_days is not None and age_days >= days and live_return is not None:
        return live_return
    return None


def first_metric(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        parsed = number(row.get(key))
        if parsed is not None:
            return parsed
    return None


def fmt_price(value: Any, currency: str = "") -> str:
    parsed = number(value)
    if parsed is None:
        return "待确认"
    text = f"{parsed:,.2f}".rstrip("0").rstrip(".")
    return f"{text} {currency}".strip()


def fmt_ratio(value: Any) -> str:
    parsed = number(value)
    return "待确认" if parsed is None else f"{parsed:.2f}:1"


def fmt_distance(current: float | None, trigger: float | None) -> str:
    if current is None or trigger is None or current <= 0:
        return ""
    distance = (trigger / current - 1) * 100
    if abs(distance) < 0.05:
        return "，已接近触发价"
    direction = "还需回落" if distance < 0 else "仍有上行空间"
    return f"，{direction} {abs(distance):.1f}%"


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def unique_list(values: list[Any], limit: int = 8) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = clean_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def load_first_json(*paths: Path) -> dict[str, Any]:
    for path in paths:
        payload = load_json_file(path)
        if payload:
            return payload
    return {}


def opportunity_status(action: str, rr_ratio: float | None, queue_status: str = "", evidence_status: str = "") -> str:
    text = f"{action} {queue_status} {evidence_status}"
    if re.search(r"失效|invalid", text, re.IGNORECASE):
        return "invalidated"
    if re.search(r"复盘|review", text, re.IGNORECASE):
        return "review"
    if re.search(r"退回观察", text):
        return "watchlist"
    if re.search(r"二次分析|Buy-Side|重点池", text, re.IGNORECASE) or queue_status == "active":
        return "secondary_analysis"
    if re.search(r"避免追高|禁止追高|不追高|严禁追高", text):
        return "avoid_chasing"
    if rr_ratio is not None and rr_ratio < 2:
        return "avoid_chasing"
    if re.search(r"等待|回踩|回调|买点|突破确认", text):
        return "waiting_entry"
    if rr_ratio is not None and rr_ratio >= 2:
        return "executable"
    return "watchlist"


def status_label(status: str) -> str:
    return {
        "executable": "可执行观察",
        "waiting_entry": "等待买点",
        "avoid_chasing": "禁止追高",
        "secondary_analysis": "二次分析",
        "watchlist": "观察",
        "invalidated": "逻辑失效",
        "review": "复盘中",
    }.get(status, "待确认")


def merge_opportunity(base: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if not base:
        return dict(incoming)
    merged = dict(base)
    for key, value in incoming.items():
        if key in {"why_changed", "buy_conditions", "avoid_conditions", "invalid_conditions"}:
            merged[key] = unique_list([*(merged.get(key) or []), *(value or [])], limit=8)
            continue
        if key == "score_delta":
            existing = merged.get("score_delta") if isinstance(merged.get("score_delta"), dict) else {}
            extra = value if isinstance(value, dict) else {}
            merged[key] = {**existing, **{k: v for k, v in extra.items() if v is not None}}
            continue
        if value not in (None, "", [], {}):
            if merged.get(key) in (None, "", [], {}) or key in {"action", "status", "status_label"}:
                merged[key] = value
    return merged


def upsert_opportunity(items: dict[str, dict[str, Any]], incoming: dict[str, Any]) -> None:
    symbol = clean_text(incoming.get("symbol") or incoming.get("code")).upper()
    if not symbol:
        return
    incoming["symbol"] = symbol
    incoming.setdefault("market", market_from_symbol(symbol))
    incoming.setdefault("currency", currency_for_symbol(symbol))
    items[symbol] = merge_opportunity(items.get(symbol), incoming)


def market_from_symbol(symbol: str) -> str:
    if symbol.startswith("US."):
        return "US"
    if symbol.startswith("HK."):
        return "HK"
    if symbol.startswith(("CN.", "SH.", "SZ.")):
        return "CN"
    return ""


def build_metric_change_index(opportunity_radar: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in opportunity_radar.get("metric_changes", []) if isinstance(opportunity_radar.get("metric_changes"), list) else []:
        if not isinstance(row, dict):
            continue
        symbol = clean_text(row.get("code")).upper()
        if not symbol:
            continue
        metric = clean_text(row.get("metric"))
        previous = number(row.get("previous"))
        current = number(row.get("current"))
        delta = None if previous is None or current is None else round(current - previous, 2)
        target = index.setdefault(symbol, {"score_delta": {}, "why_changed": []})
        if metric == "opportunity_score":
            target["score_delta"]["opportunity_score"] = delta
        elif metric == "trend_score":
            target["score_delta"]["trend_score"] = delta
        target["why_changed"].append(clean_text(row.get("note")) or f"{clean_text(row.get('label'))}发生变化")
    return index


def rr_trigger_price(target: float | None, stop_loss: float | None, rr_required: float) -> float | None:
    if target is None or stop_loss is None or target <= stop_loss or rr_required <= 0:
        return None
    return round((target + rr_required * stop_loss) / (rr_required + 1), 2)


def valid_path(entry: float | None, stop_loss: float | None, target: float | None) -> bool:
    return entry is not None and stop_loss is not None and target is not None and stop_loss < entry < target


def executable_starter_path(item: dict[str, Any]) -> dict[str, Any] | None:
    price = number(item.get("price"))
    entry = number(item.get("starter_entry"))
    stop_loss = number(item.get("starter_stop"))
    target = number(item.get("starter_target"))
    rr = number(item.get("starter_reward_risk"))
    mechanical_rr = number(item.get("rr_ratio"))
    opportunity = number(item.get("opportunity_score"))
    trend = number(item.get("trend_score"))
    crowding = number(item.get("crowding_score"))
    if not valid_path(entry, stop_loss, target) or rr is None or price is None:
        return None
    if price > entry * 1.01:
        return None
    if rr < 1.5:
        return None
    if opportunity is not None and opportunity < 65:
        return None
    if trend is not None and trend < 60 and not (mechanical_rr is not None and mechanical_rr >= 2 and opportunity is not None and opportunity >= 70):
        return None
    if crowding is not None and crowding >= 80:
        return None
    return {"signal_type": "starter", "entry": entry, "stop_loss": stop_loss, "target": target, "rr": rr}


def executable_breakout_path(item: dict[str, Any]) -> dict[str, Any] | None:
    price = number(item.get("price"))
    trigger = number(item.get("breakout_trigger"))
    stop_loss = number(item.get("breakout_stop"))
    target = number(item.get("breakout_target"))
    rr = number(item.get("breakout_reward_risk"))
    trend = number(item.get("trend_score"))
    if not valid_path(trigger, stop_loss, target) or rr is None or price is None:
        return None
    if price < trigger or price > trigger * 1.03:
        return None
    if rr < 2:
        return None
    if trend is not None and trend < 55:
        return None
    return {"signal_type": "breakout", "entry": trigger, "stop_loss": stop_loss, "target": target, "rr": rr}


def apply_executable_path(item: dict[str, Any], path: dict[str, Any]) -> None:
    original_rr = number(item.get("rr_ratio"))
    if original_rr is not None:
        item["mechanical_rr_ratio"] = original_rr
    item["signal_type"] = path["signal_type"]
    item["entry_price"] = round(float(path["entry"]), 2)
    item["stop_loss"] = round(float(path["stop_loss"]), 2)
    item["target_price"] = round(float(path["target"]), 2)
    item["rr_ratio"] = round(float(path["rr"]), 2)
    item["status"] = "executable"
    item["status_label"] = "可试仓观察" if path["signal_type"] == "starter" else "突破确认"
    if path["signal_type"] == "starter":
        item["action"] = "可试仓观察：不是重仓普通买入，按明确止损和目标小仓验证。"
        item["why_changed"] = unique_list([*(item.get("why_changed") or []), "新增试仓路径：严格回踩未到，但当前价位已有入场/止损/目标/R/R。"], limit=8)
    else:
        item["action"] = "突破确认：只有突破有效且不高开追涨时才执行。"
        item["why_changed"] = unique_list([*(item.get("why_changed") or []), "新增突破路径：用突破触发价、止损和目标重新计算 R/R。"], limit=8)


def derive_opportunity_conditions(item: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    rr = number(item.get("rr_ratio"))
    rr_required = number(item.get("rr_required")) or 2.0
    price = number(item.get("price"))
    strict_entry = number(item.get("entry_price") or item.get("strict_entry"))
    stop_loss = number(item.get("stop_loss") or item.get("invalidation"))
    target = number(item.get("target_price") or item.get("mechanical_target"))
    trend = number(item.get("trend_score"))
    crowding = number(item.get("crowding_score"))
    status = clean_text(item.get("status"))
    currency = currency_for_symbol(str(item.get("symbol") or ""))
    rr_entry = rr_trigger_price(target, stop_loss, rr_required)
    valid_strict_entry = (
        strict_entry
        if strict_entry is not None
        and stop_loss is not None
        and target is not None
        and stop_loss < strict_entry < target
        else None
    )
    entry = valid_strict_entry or rr_entry
    if entry is not None:
        item["entry_price"] = round(entry, 2)
    if stop_loss is not None:
        item["stop_loss"] = stop_loss
    if target is not None:
        item["target_price"] = target

    if price is not None and stop_loss is not None and price <= stop_loss:
        item["status"] = "invalidated"
        item["status_label"] = status_label("invalidated")
        item["rr_ratio"] = None
        buy = [
            f"暂不买：现价 {fmt_price(price, currency)} 已低于或等于原止损位 {fmt_price(stop_loss, currency)}，原入场路径失效",
            f"不要套用“现价 ≤ {fmt_price(entry, currency)}”触发条件；价格跌破止损后不是更便宜，而是需要重算风控",
            "重新观察条件：重新站回原止损/关键支撑上方并收盘确认，或生成新的 Buy-Side 三路径",
            f"重新计算前必须同时补齐：新入场价 / 新止损价 / 新目标价 / 新 R/R ≥ {rr_required:.0f}:1",
        ]
        avoid = [
            "跌破原止损后不抄底，不把失效路径降格成便宜买点",
            "未重新计算新止损和新目标前，不升级为买入",
            "如果反弹只是回抽原止损位且无法站稳，仍按失效处理",
        ]
        invalid = [
            f"现价低于或等于原止损位 {fmt_price(stop_loss, currency)}",
            "原目标价和原入场价已经不能直接用于当前交易计划",
            "需要等待新的 Buy-Side 复核重新定义失效位",
        ]
        return buy, avoid, invalid

    signal_type = clean_text(item.get("signal_type"))
    if signal_type in {"starter", "breakout"} and price is not None and stop_loss is not None and target is not None and entry is not None:
        if signal_type == "starter":
            headline = "什么时候买：现价不高于试仓触发价，允许小仓/1股试仓；这不是重仓买入信号"
            trigger_line = f"价格触发：现价 ≤ {fmt_price(entry, currency)} 才考虑；高于触发价不追"
        else:
            headline = "什么时候买：只在突破触发价已经确认、且没有高开过度追涨时执行"
            trigger_line = f"突破触发：价格站上 {fmt_price(entry, currency)} 且不超过触发价约 3% 才考虑"
        buy = [
            headline,
            trigger_line,
            f"风控框架：止损 {fmt_price(stop_loss, currency)}；目标 {fmt_price(target, currency)}；当前 R/R {fmt_ratio(rr)}",
            "执行纪律：只能按整股/小仓验证；跌破止损不补仓；目标或逻辑变化后重新计算",
            "确认信号：趋势不恶化，且财务/事件/产业链证据至少一项继续改善",
        ]
        avoid = [
            "高于触发价明显追涨不买，等待下一次回踩或重新计算",
            "R/R 回落到 1.5:1 以下不做试仓；普通买入仍要求 2:1 以上",
            "数据缺口未修复时只允许试仓观察，不升级为重仓买入",
        ]
        invalid = [
            f"跌破止损 {fmt_price(stop_loss, currency)} 后路径失效",
            "财报/指引/订单/价格证据弱于预期",
            "产业链需求或核心假设被证伪",
        ]
        return buy, avoid, invalid

    if price is None or stop_loss is None or target is None or entry is None:
        buy = [
            "暂不买：缺少完整的入场价、止损价或目标价，等待系统补齐后再决策",
            f"必须先补齐：当前价 {fmt_price(price, currency)} / 止损 {fmt_price(stop_loss, currency)} / 目标 {fmt_price(target, currency)}",
            "补齐后再检查 R/R 是否 ≥ 2:1，未达标不升级为买入",
            "需要财务、趋势或事件证据至少一项继续改善",
        ]
    else:
        actionable = status == "executable" and rr is not None and rr >= rr_required
        if actionable:
            headline = "可试仓观察：价格、止损、目标和 R/R 已达最低纪律，仍需盘中确认"
        elif rr is not None and rr < rr_required:
            headline = f"暂不买：当前 R/R {fmt_ratio(rr)} 低于 {rr_required:.0f}:1，等价格回到买点再看"
        else:
            headline = "暂不买：仍需二次分析或人工复核，满足以下条件才升级"
        if rr is not None and rr >= rr_required:
            price_line = (
                f"价格纪律：现价 {fmt_price(price, currency)} 已满足 R/R ≥ {rr_required:.0f}:1；"
                f"保守回踩买点 ≤ {fmt_price(valid_strict_entry, currency)}{fmt_distance(price, valid_strict_entry)}"
                if valid_strict_entry is not None and price is not None and price > valid_strict_entry
                else f"价格纪律：现价 {fmt_price(price, currency)} 已满足 R/R ≥ {rr_required:.0f}:1；盘中不跌破止损前才可继续观察"
            )
        else:
            price_line = f"价格触发：现价 ≤ {fmt_price(entry, currency)} 再考虑{fmt_distance(price, entry)}；若突破追入，必须重新计算 R/R ≥ {rr_required:.0f}:1"
        buy = [
            headline,
            price_line,
            f"风控框架：止损 {fmt_price(stop_loss, currency)}；目标 {fmt_price(target, currency)}；当前 R/R {fmt_ratio(rr)}",
            (
                f"确认信号：趋势确认从 {trend:.1f} 提升到 ≥50，且财务/事件证据补强"
                if trend is not None and trend < 50
                else "确认信号：趋势不恶化，财务/事件证据至少一项继续改善"
            ),
        ]
        if crowding is not None and crowding >= 80:
            buy.append(f"拥挤度 {crowding:.1f} 偏高：只能等回踩，不做高开追涨")
    avoid = [
        f"R/R 低于 {rr_required:.0f}:1 不追高；价格高于 {fmt_price(entry, currency)} 且未重新计算 R/R 时不买" if entry else f"R/R 低于 {rr_required:.0f}:1 不追高",
        "数据缺口未修复前不升级为买入",
        "高开大幅追涨或财报前波动率过高",
    ]
    invalid = [
        f"收盘跌破止损位 {fmt_price(stop_loss, currency)}" if stop_loss is not None else "跌破后续 Buy-Side 分析定义的失效位",
        "产业链需求、订单或价格弱于预期",
        "财报/指引/申报显示核心假设恶化",
    ]
    return buy, avoid, invalid


def finalize_opportunities(items: dict[str, dict[str, Any]], limit: int = 80) -> list[dict[str, Any]]:
    finalized: list[dict[str, Any]] = []
    for item in items.values():
        rr = number(item.get("rr_ratio"))
        status = opportunity_status(
            clean_text(item.get("action")),
            rr,
            clean_text(item.get("queue_status")),
            clean_text(item.get("evidence_status")),
        )
        item["status"] = status
        item["status_label"] = status_label(status)
        starter_path = executable_starter_path(item)
        breakout_path = executable_breakout_path(item)
        if status not in {"invalidated", "review"} and starter_path:
            apply_executable_path(item, starter_path)
        elif status not in {"invalidated", "review"} and breakout_path:
            apply_executable_path(item, breakout_path)
        elif item.get("status") == "executable" and (number(item.get("trend_score")) is not None and (number(item.get("trend_score")) or 0) < 45):
            item["status"] = "waiting_entry"
            item["status_label"] = status_label("waiting_entry")
            item["why_changed"] = unique_list([*(item.get("why_changed") or []), "R/R 达标但趋势确认不足，降级为等待买点。"], limit=8)
        if not item.get("price_source") and item.get("price") is not None:
            item["price_source"] = "公开行情快照"
        if not item.get("price_time"):
            item["price_time"] = item.get("updated_at") or item.get("last_seen_at") or ""
        buy, avoid, invalid = derive_opportunity_conditions(item)
        item["buy_conditions"] = unique_list([*(item.get("buy_conditions") or []), *buy], limit=5)
        item["avoid_conditions"] = unique_list([*(item.get("avoid_conditions") or []), *avoid], limit=5)
        item["invalid_conditions"] = unique_list([*(item.get("invalid_conditions") or []), *invalid], limit=5)
        item["why_changed"] = unique_list(item.get("why_changed") or [], limit=6)
        finalized.append(
            {
                "symbol": item.get("symbol"),
                "name": item.get("name") or "",
                "market": item.get("market") or market_from_symbol(str(item.get("symbol") or "")),
                "theme": item.get("theme") or "",
                "segment": item.get("segment") or "",
                "status": item.get("status"),
                "status_label": item.get("status_label"),
                "action": item.get("action") or "保留观察，等待结构化复核",
                "price": item.get("price"),
                "currency": item.get("currency") or currency_for_symbol(str(item.get("symbol") or "")),
                "price_time": item.get("price_time") or "",
                "price_source": item.get("price_source") or "",
                "opportunity_score": item.get("opportunity_score"),
                "trend_score": item.get("trend_score"),
                "crowding_score": item.get("crowding_score"),
                "rr_ratio": item.get("rr_ratio"),
                "rr_required": item.get("rr_required") or 2,
                "entry_price": item.get("entry_price") or item.get("strict_entry"),
                "stop_loss": item.get("stop_loss") or item.get("invalidation"),
                "target_price": item.get("target_price") or item.get("mechanical_target"),
                "signal_type": item.get("signal_type"),
                "mechanical_rr_ratio": item.get("mechanical_rr_ratio"),
                "starter_entry": item.get("starter_entry"),
                "starter_stop": item.get("starter_stop"),
                "starter_target": item.get("starter_target"),
                "starter_reward_risk": item.get("starter_reward_risk"),
                "breakout_trigger": item.get("breakout_trigger"),
                "breakout_stop": item.get("breakout_stop"),
                "breakout_target": item.get("breakout_target"),
                "breakout_reward_risk": item.get("breakout_reward_risk"),
                "score_delta": item.get("score_delta") if item.get("score_delta") else None,
                "why_changed": item.get("why_changed") or [],
                "buy_conditions": item.get("buy_conditions") or [],
                "avoid_conditions": item.get("avoid_conditions") or [],
                "invalid_conditions": item.get("invalid_conditions") or [],
                "source": item.get("source") or "structured-public-data",
                "updated_at": item.get("updated_at") or "",
            }
        )
    return sorted(
        finalized,
        key=lambda row: (
            {"executable": 0, "secondary_analysis": 1, "waiting_entry": 2, "avoid_chasing": 3, "watchlist": 4}.get(str(row.get("status")), 9),
            -(number(row.get("opportunity_score")) or 0),
            str(row.get("symbol") or ""),
        ),
    )[:limit]


def derive_opportunities() -> list[dict[str, Any]]:
    opportunity_radar = load_json_file(DEFAULT_OPPORTUNITY_RADAR_PATH)
    cross_market = load_json_file(DEFAULT_CROSS_MARKET_PATH)
    secondary_queue = load_json_file(DEFAULT_SECONDARY_QUEUE_PATH)
    event_evidence = load_first_json(DEFAULT_EVENT_EVIDENCE_PATH, DEFAULT_DOCS_EVENT_EVIDENCE_PATH)
    if not any((opportunity_radar, cross_market, secondary_queue, event_evidence)):
        return []

    items: dict[str, dict[str, Any]] = {}
    metric_changes = build_metric_change_index(opportunity_radar)
    generated_at = (
        event_evidence.get("generated_label")
        or cross_market.get("generated_label")
        or opportunity_radar.get("generated_label")
        or secondary_queue.get("generated_label")
        or ""
    )

    for theme in opportunity_radar.get("top_opportunities", []) if isinstance(opportunity_radar.get("top_opportunities"), list) else []:
        if not isinstance(theme, dict):
            continue
        theme_name = clean_text(theme.get("name"))
        for candidate in theme.get("top_candidates", []) if isinstance(theme.get("top_candidates"), list) else []:
            if not isinstance(candidate, dict):
                continue
            symbol = clean_text(candidate.get("code")).upper()
            change = metric_changes.get(symbol, {})
            upsert_opportunity(
                items,
                {
                    "symbol": symbol,
                    "name": clean_text(candidate.get("name")),
                    "market": clean_text(candidate.get("market")),
                    "theme": theme_name,
                    "segment": clean_text(candidate.get("layer")),
                    "action": clean_text(candidate.get("action")),
                    "price": number(candidate.get("price")),
                    "opportunity_score": number(candidate.get("score")),
                    "trend_score": number(candidate.get("trend_score")),
                    "crowding_score": number(candidate.get("crowding_score")),
                    "rr_ratio": number(candidate.get("reward_risk")),
                    "starter_entry": number(candidate.get("starter_entry")),
                    "starter_stop": number(candidate.get("starter_stop")),
                    "starter_target": number(candidate.get("starter_target")),
                    "starter_reward_risk": number(candidate.get("starter_reward_risk")),
                    "breakout_trigger": number(candidate.get("breakout_trigger")),
                    "breakout_stop": number(candidate.get("breakout_stop")),
                    "breakout_target": number(candidate.get("breakout_target")),
                    "breakout_reward_risk": number(candidate.get("breakout_reward_risk")),
                    "currency": currency_for_symbol(symbol),
                    "updated_at": opportunity_radar.get("generated_label") or generated_at,
                    "score_delta": change.get("score_delta"),
                    "why_changed": change.get("why_changed", []),
                    "source": "opportunity_radar",
                },
            )

    for candidate in cross_market.get("secondary_research_candidates", []) if isinstance(cross_market.get("secondary_research_candidates"), list) else []:
        if not isinstance(candidate, dict):
            continue
        symbol = clean_text(candidate.get("code")).upper()
        change = metric_changes.get(symbol, {})
        upsert_opportunity(
            items,
            {
                "symbol": symbol,
                "name": clean_text(candidate.get("name")),
                "market": clean_text(candidate.get("market")),
                "theme": clean_text(candidate.get("theme")),
                "segment": clean_text(candidate.get("layer")),
                "action": clean_text(candidate.get("reason")) or "进入二次研究候选",
                "price": number(candidate.get("price")),
                "currency": currency_for_symbol(symbol),
                "price_time": cross_market.get("generated_label") or generated_at,
                "price_source": "跨市场情报行情快照",
                "opportunity_score": number(candidate.get("opportunity_score")),
                "trend_score": number(candidate.get("trend_score")),
                "crowding_score": number(candidate.get("crowding_score")),
                "rr_ratio": number(candidate.get("reward_risk")),
                "rr_required": 2,
                "starter_entry": number(candidate.get("starter_entry")),
                "starter_stop": number(candidate.get("starter_stop")),
                "starter_target": number(candidate.get("starter_target")),
                "starter_reward_risk": number(candidate.get("starter_reward_risk")),
                "breakout_trigger": number(candidate.get("breakout_trigger")),
                "breakout_stop": number(candidate.get("breakout_stop")),
                "breakout_target": number(candidate.get("breakout_target")),
                "breakout_reward_risk": number(candidate.get("breakout_reward_risk")),
                "updated_at": cross_market.get("generated_label") or generated_at,
                "score_delta": change.get("score_delta"),
                "why_changed": change.get("why_changed", []),
                "source": "cross_market_intelligence",
            },
        )

    for theme in cross_market.get("themes", []) if isinstance(cross_market.get("themes"), list) else []:
        if not isinstance(theme, dict):
            continue
        theme_name = clean_text(theme.get("name"))
        securities: list[Any] = []
        if isinstance(theme.get("securities"), list):
            securities.extend(theme.get("securities") or [])
        for layer in theme.get("layers", []) if isinstance(theme.get("layers"), list) else []:
            if isinstance(layer, dict) and isinstance(layer.get("leaders"), list):
                securities.extend(layer.get("leaders") or [])
        for security in securities:
            if not isinstance(security, dict):
                continue
            symbol = clean_text(security.get("code")).upper()
            change = metric_changes.get(symbol, {})
            upsert_opportunity(
                items,
                {
                    "symbol": symbol,
                    "name": clean_text(security.get("name")),
                    "market": clean_text(security.get("market")),
                    "theme": theme_name,
                    "segment": clean_text(security.get("layer") or security.get("role")),
                    "action": clean_text(security.get("action")),
                    "price": number(security.get("price")),
                    "currency": currency_for_symbol(symbol),
                    "price_time": cross_market.get("generated_label") or generated_at,
                    "price_source": clean_text(security.get("data_status")) or "跨市场情报行情快照",
                    "opportunity_score": number(security.get("opportunity_score")),
                    "trend_score": number(security.get("trend_score")),
                    "crowding_score": number(security.get("crowding_score")),
                    "rr_ratio": number(security.get("reward_risk")),
                    "rr_required": 2,
                    "starter_entry": number(security.get("starter_entry")),
                    "starter_stop": number(security.get("starter_stop")),
                    "starter_target": number(security.get("starter_target")),
                    "starter_reward_risk": number(security.get("starter_reward_risk")),
                    "breakout_trigger": number(security.get("breakout_trigger")),
                    "breakout_stop": number(security.get("breakout_stop")),
                    "breakout_target": number(security.get("breakout_target")),
                    "breakout_reward_risk": number(security.get("breakout_reward_risk")),
                    "updated_at": cross_market.get("generated_label") or generated_at,
                    "score_delta": change.get("score_delta") or security.get("score_delta"),
                    "why_changed": change.get("why_changed", []),
                    "source": "cross_market_intelligence",
                },
            )

    records = secondary_queue.get("records")
    if isinstance(records, dict):
        iterable_records = records.values()
    elif isinstance(records, list):
        iterable_records = records
    else:
        iterable_records = []
    for record in iterable_records:
        if not isinstance(record, dict):
            continue
        symbol = clean_text(record.get("code")).upper()
        upsert_opportunity(
            items,
            {
                "symbol": symbol,
                "name": clean_text(record.get("name")),
                "market": clean_text(record.get("market")),
                "theme": clean_text(record.get("layer_name")),
                "segment": clean_text(record.get("role")),
                "action": clean_text(record.get("radar_action") or record.get("last_reason")),
                "price": number(record.get("price")),
                "currency": currency_for_symbol(symbol),
                "price_time": clean_text(record.get("last_seen_at") or secondary_queue.get("generated_label") or generated_at),
                "price_source": clean_text(record.get("data_status")) or "二次分析队列行情快照",
                "opportunity_score": number(record.get("layer_score")),
                "trend_score": number(record.get("trend_score")),
                "updated_at": secondary_queue.get("generated_label") or generated_at,
                "queue_status": clean_text(record.get("status")),
                "why_changed": [clean_text(record.get("last_result")), clean_text(record.get("last_reason"))],
                "source": "secondary_analysis_queue",
            },
        )

    for card in event_evidence.get("cards", []) if isinstance(event_evidence.get("cards"), list) else []:
        if not isinstance(card, dict):
            continue
        symbol = clean_text(card.get("code")).upper()
        price = card.get("price") if isinstance(card.get("price"), dict) else {}
        gaps = card.get("gaps") if isinstance(card.get("gaps"), list) else []
        upsert_opportunity(
            items,
            {
                "symbol": symbol,
                "name": clean_text(card.get("name")),
                "market": market_from_symbol(symbol),
                "theme": " / ".join(unique_list(card.get("themes") or [], limit=3)),
                "action": clean_text(card.get("action")),
                "price": number(price.get("price")),
                "currency": currency_for_symbol(symbol),
                "price_time": clean_text(price.get("quote_time") or event_evidence.get("generated_label") or generated_at),
                "price_source": clean_text(price.get("source")) or "事件证据行情快照",
                "trend_score": number(price.get("trend_score")),
                "rr_ratio": number(price.get("reward_risk")),
                "rr_required": 2,
                "entry_price": number(price.get("strict_entry")),
                "stop_loss": number(price.get("invalidation")),
                "target_price": number(price.get("mechanical_target")),
                "starter_entry": number(price.get("starter_entry")),
                "starter_stop": number(price.get("starter_stop")),
                "starter_target": number(price.get("starter_target")),
                "starter_reward_risk": number(price.get("starter_reward_risk")),
                "breakout_trigger": number(price.get("breakout_trigger")),
                "breakout_stop": number(price.get("breakout_stop")),
                "breakout_target": number(price.get("breakout_target")),
                "breakout_reward_risk": number(price.get("breakout_reward_risk")),
                "evidence_status": clean_text(card.get("evidence_status")),
                "updated_at": event_evidence.get("generated_label") or generated_at,
                "why_changed": [
                    clean_text(card.get("evidence_status")),
                    *[clean_text(gap) for gap in gaps],
                ],
                "source": "event_evidence",
            },
        )

    return finalize_opportunities(items)


def derive_review_stats(metrics_path: Path = DEFAULT_REVIEW_METRICS_PATH) -> dict[str, Any] | None:
    payload = load_json_file(metrics_path)
    if not payload:
        return None
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    themes = payload.get("themes") if isinstance(payload.get("themes"), list) else []
    items_by_symbol: dict[str, dict[str, Any]] = {}
    for theme in themes:
        if not isinstance(theme, dict):
            continue
        first_seen = date_label(theme.get("first_seen_at"))
        status = str(theme.get("status") or "pending")
        for row in theme.get("returns", []) if isinstance(theme.get("returns"), list) else []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("code") or "").strip()
            if not symbol or symbol in items_by_symbol:
                continue
            return_7d = matured_return(row, theme, 7)
            return_30d = matured_return(row, theme, 30)
            return_60d = matured_return(row, theme, 60)
            return_90d = matured_return(row, theme, 90)
            max_drawdown = first_metric(row, "max_drawdown", "max_drawdown_pct", "drawdown_pct")
            max_gain = first_metric(row, "max_gain", "max_gain_pct", "peak_gain_pct")
            lesson = row.get("lesson") or row.get("error_type")
            if return_7d is not None and return_7d >= 10:
                lesson = "错过机会复盘：7D涨幅超过10%，需要检查当时买入门槛是否过严。"
            elif not lesson and return_7d is not None and return_30d is None:
                lesson = "7D已可复盘；30D仍待到期。"
            items_by_symbol[symbol] = {
                "symbol": symbol,
                "name": "",
                "first_seen": first_seen,
                "first_price": row.get("initial_price"),
                "currency": currency_for_symbol(symbol),
                "status": "pending" if "未成熟" in status or "观察" in status else status,
                "return_7d": return_7d,
                "return_30d": return_30d,
                "return_60d": return_60d,
                "return_90d": return_90d,
                "max_drawdown": max_drawdown,
                "max_gain": max_gain,
                "error_type": row.get("error_type"),
                "lesson": lesson,
            }

    completed_count = summary.get("completed_review_count")
    pending_count = summary.get("pending_checkpoint_count")
    return {
        "updated_at": payload.get("generated_label") or payload.get("generated_at"),
        "tracked_count": len(items_by_symbol),
        "completed_count": completed_count,
        "pending_count": pending_count,
        "win_rate_30d": summary.get("hit_rate_pct"),
        "avg_max_drawdown": None,
        "avg_max_gain": None,
        "best_theme": summary.get("best_theme"),
        "worst_error_type": None,
        "items": list(items_by_symbol.values())[:80],
    }


def derive_evidence_gap_breakdown() -> dict[str, Any] | None:
    payload = load_json_file(DEFAULT_EVENT_EVIDENCE_PATH) or load_json_file(DEFAULT_DOCS_EVENT_EVIDENCE_PATH)
    if not payload:
        return None
    breakdown = payload.get("evidence_gap_breakdown")
    if not isinstance(breakdown, dict):
        return None
    categories = breakdown.get("categories") if isinstance(breakdown.get("categories"), list) else []
    safe_categories: list[dict[str, Any]] = []
    for item in categories:
        if not isinstance(item, dict):
            continue
        affected = item.get("affected_symbols") if isinstance(item.get("affected_symbols"), list) else []
        safe_categories.append(
            {
                "key": item.get("key"),
                "label": item.get("label"),
                "group": item.get("group"),
                "group_label": item.get("group_label"),
                "count": item.get("count"),
                "affected_symbols": [str(symbol) for symbol in affected[:60]],
                "fallback": item.get("fallback"),
            }
        )
    return {
        "updated_at": payload.get("generated_label") or payload.get("generated_at"),
        "original_total": breakdown.get("original_total"),
        "data_gap": breakdown.get("data_gap"),
        "permission_limited": breakdown.get("permission_limited"),
        "entry_path_missing": breakdown.get("entry_path_missing"),
        "rr_discipline": breakdown.get("rr_discipline"),
        "other": breakdown.get("other"),
        "categories": safe_categories,
    }


def derive_data_health() -> list[dict[str, Any]]:
    payload = load_json_file(DEFAULT_FREE_DATA_FALLBACK_PATH)
    health = payload.get("data_health") if isinstance(payload.get("data_health"), list) else []
    output: list[dict[str, Any]] = []
    for item in health:
        if not isinstance(item, dict):
            continue
        output.append(
            {
                "name": item.get("name") or item.get("source"),
                "source": item.get("source") or item.get("name"),
                "status": item.get("status") or "unknown",
                "message": item.get("message") or "",
                "impact": item.get("impact") or "",
                "updated_at": item.get("updated_at") or payload.get("generated_label") or payload.get("generated_at"),
            }
        )
    return output


def derive_free_data_fallback_summary() -> dict[str, Any] | None:
    payload = load_json_file(DEFAULT_FREE_DATA_FALLBACK_PATH)
    if not payload:
        return None
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {
        "generated_at": payload.get("generated_at"),
        "generated_label": payload.get("generated_label"),
        "summary": summary,
        "source_priority": payload.get("source_priority") if isinstance(payload.get("source_priority"), dict) else {},
    }


def derive_market_sentiment_summary() -> dict[str, Any] | None:
    payload = load_json_file(DEFAULT_MARKET_SENTIMENT_PATH)
    if not payload:
        return None
    components = payload.get("components") if isinstance(payload.get("components"), list) else []
    data_gaps = payload.get("data_gaps") if isinstance(payload.get("data_gaps"), list) else []
    return {
        "schema_version": payload.get("schema_version"),
        "generated_at": payload.get("generated_at"),
        "generated_label": payload.get("generated_label"),
        "score": payload.get("score"),
        "status": payload.get("status"),
        "status_label": payload.get("status_label"),
        "stance": payload.get("stance"),
        "summary": payload.get("summary"),
        "components": [
            {
                "key": item.get("key"),
                "name": item.get("name"),
                "score": item.get("score"),
                "status": item.get("status"),
                "message": item.get("message"),
                "source": item.get("source"),
                "updated_at": item.get("updated_at"),
                "confidence": item.get("confidence"),
            }
            for item in components
            if isinstance(item, dict)
        ][:8],
        "data_gaps": [
            {
                "field": item.get("field"),
                "message": item.get("message"),
                "impact": item.get("impact"),
            }
            for item in data_gaps
            if isinstance(item, dict)
        ][:20],
    }


def build_split_index(payload: dict[str, Any]) -> dict[str, Any]:
    index_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"schema_version", "reports"}
    }
    index_payload["schema_version"] = 2
    index_payload["reports"] = [public_report_metadata(item) for item in payload.get("reports", []) if isinstance(item, dict)]
    return index_payload


def write_split_archive(payload: dict[str, Any], index_output: Path, detail_dir: Path) -> None:
    detail_dir.mkdir(parents=True, exist_ok=True)
    active_ids: set[str] = set()
    for item in payload.get("reports", []):
        if not isinstance(item, dict):
            continue
        report_id = str(item.get("id") or "").strip()
        if not report_id:
            continue
        active_ids.add(report_id)
        detail_path = detail_dir / f"{report_id}.json"
        detail_payload = {
            "id": report_id,
            "content": str(item.get("content") or ""),
        }
        detail_path.write_text(json.dumps(detail_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    for stale in detail_dir.glob("*.json"):
        if stale.stem not in active_ids:
            stale.unlink()

    index_output.parent.mkdir(parents=True, exist_ok=True)
    index_payload = build_split_index(payload)
    index_output.write_text(json.dumps(index_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def build_archive(output: Path, limit: int = 80, merge_existing: bool = True) -> dict[str, Any]:
    candidates = collect_report_files()
    if merge_existing:
        candidates.extend(load_existing_reports(output))

    candidates.sort(key=lambda item: str(item["published_at"]), reverse=True)
    seen: set[str] = set()
    reports: list[dict[str, Any]] = []
    for item in candidates:
        digest = str(item.pop("digest"))
        stable_digest = str(item.pop("stable_digest", digest))
        identity = f"{item.get('id')}:{digest}"
        logical_identity = f"{item.get('filename')}:{item.get('published_at')}"
        title_time_identity = f"{item.get('kind')}:{item.get('title')}:{item.get('published_at')}:{stable_digest}"
        identities = {identity, stable_digest, logical_identity, title_time_identity}
        if item.get("kind") in ONE_REPORT_PER_DAY_KINDS:
            published_day = str(item.get("published_label") or item.get("published_at") or "")[:10]
            identities.add(f"one-per-day:{item.get('kind')}:{item.get('title')}:{published_day}")
        if any(value in seen for value in identities):
            continue
        seen.update(identities)
        reports.append(item)
        if len(reports) >= limit:
            break

    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_label": now.astimezone().strftime("%Y-%m-%d %H:%M"),
        "privacy": "public-sanitized",
        "report_count": len(reports),
        "reports": reports,
    }
    review_stats = derive_review_stats()
    if review_stats:
        payload["review_stats"] = review_stats
    evidence_gap_breakdown = derive_evidence_gap_breakdown()
    if evidence_gap_breakdown:
        payload["evidence_gap_breakdown"] = evidence_gap_breakdown
    opportunities = derive_opportunities()
    if opportunities:
        payload["opportunities"] = opportunities
    data_health = derive_data_health()
    if data_health:
        payload["data_health"] = data_health
    free_data_fallback = derive_free_data_fallback_summary()
    if free_data_fallback:
        payload["free_data_fallback"] = free_data_fallback
    market_sentiment = derive_market_sentiment_summary()
    if market_sentiment:
        payload["market_sentiment"] = market_sentiment
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Export sanitized reports for GitHub Pages.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--index-output", type=Path, default=DEFAULT_INDEX_OUTPUT)
    parser.add_argument("--detail-dir", type=Path, default=DEFAULT_DETAIL_DIR)
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--no-merge-existing", action="store_true")
    args = parser.parse_args()

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = build_archive(output, max(1, args.limit), merge_existing=not args.no_merge_existing)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    write_split_archive(payload, args.index_output.resolve(), args.detail_dir.resolve())
    print(f"Exported {payload['report_count']} sanitized reports to {output}")
    print(f"Exported split index to {args.index_output.resolve()} and details to {args.detail_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
