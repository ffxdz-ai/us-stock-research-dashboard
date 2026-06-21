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
from statistics import median
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
    if completed_count == 0 and age_days < 30:
        return "未成熟观察"
    if avg_return is not None and avg_return >= 15:
        return "价格验证成功"
    if score_delta is not None and score_delta >= 6:
        return "逻辑增强"
    if avg_return is not None and avg_return <= -12:
        return "价格验证失败"
    if score_delta is not None and score_delta <= -8:
        return "逻辑削弱"
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
    mature = age_days >= 30 or completed > 0
    hit = status in {"价格验证成功", "逻辑增强"} if mature else None
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
    summary = {
        "theme_count": len(themes),
        "mature_theme_count": len(mature),
        "hit_count": len(hits),
        "failed_count": len(failed),
        "hit_rate_pct": round(len(hits) / len(mature) * 100, 1) if mature else None,
        "completed_review_count": len(completed_reviews),
        "due_checkpoint_count": sum(int(item.get("due_checkpoint_count") or 0) for item in themes),
        "pending_checkpoint_count": sum(int(item.get("pending_checkpoint_count") or 0) for item in themes),
        "avg_live_return_pct": round(sum(value for value in live_returns if value is not None) / len(live_returns), 2) if live_returns else None,
        "best_theme": themes[0].get("theme_name") if themes else None,
    }
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_label": now.strftime("%Y-%m-%d %H:%M"),
        "data_boundary": {
            "role": "opportunity discovery feedback loop; not trading instruction",
            "hit_rate_rule": "Hit-rate is calculated only from mature themes or completed reviews; immature themes are not counted as wins.",
        },
        "summary": summary,
        "themes": themes,
        "completed_reviews": completed_reviews,
        "discipline": [
            "命中率只用于校准机会雷达，不用于事后改写首次发现时间。",
            "未满 30 天的机会只显示实时跟踪，不纳入命中率。",
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
        "themes": payload.get("themes", [])[:30],
        "completed_reviews": payload.get("completed_reviews", [])[:40],
        "discipline": payload.get("discipline"),
    }


def render_report(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
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
        lines.append("当前还没有完成的 30/60/90 天 checkpoint；未成熟主题不会被计入命中率。")

    lines.extend(["", "## 使用纪律", ""])
    for item in payload.get("discipline", []):
        lines.append(f"- {item}")
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
