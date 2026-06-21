#!/usr/bin/env python3
"""Maintain the two-day secondary-analysis lifecycle.

The queue is a scheduling and discipline layer, not a trade generator. It takes
strong supply-chain radar candidates, reviews them every two days, and demotes
failed names back to observation so they stop consuming high-cost Buy-Side
analysis slots unless they re-trigger. There is no time-based cooldown:
qualified names can re-enter as soon as they satisfy the re-trigger gates.
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

DEFAULT_CONFIG = CONFIG_DIR / "agent_config.json"
DEFAULT_RADAR = DATA_DIR / "latest_supply_chain_radar.json"
DEFAULT_OPPORTUNITY_RADAR = DATA_DIR / "latest_opportunity_radar.json"
DEFAULT_CROSS_MARKET_INTELLIGENCE = DATA_DIR / "latest_cross_market_intelligence.json"
DEFAULT_MARKET_PACK = DATA_DIR / "latest_market_pack.json"
DEFAULT_STATE = DOCS_DATA_DIR / "secondary_analysis_queue.json"
DEFAULT_OUTPUT = DATA_DIR / "latest_secondary_analysis_queue.json"
DEFAULT_REPORT = REPORTS_DIR / "latest-secondary-analysis-queue.md"


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


def review_timezone(name: str = "Asia/Shanghai") -> timezone:
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=8), "Asia/Shanghai")


def now_local(tz_name: str = "Asia/Shanghai") -> datetime:
    return datetime.now(review_timezone(tz_name))


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace("%", "").replace(",", "").strip()
        if not cleaned or cleaned.lower() in {"n/a", "nan", "none", "--", "null", "数据不足"}:
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
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone()


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def review_slot(base: datetime, cfg: dict[str, Any], *, days: int = 0) -> datetime:
    tz_name = str(cfg.get("review_timezone") or "Asia/Shanghai")
    tz = review_timezone(tz_name)
    local = base.astimezone(tz) + timedelta(days=days)
    return local.replace(
        hour=int(cfg.get("review_hour", 12)),
        minute=int(cfg.get("review_minute", 0)),
        second=0,
        microsecond=0,
    )


def normalize_review_slot(value: Any, fallback: datetime, cfg: dict[str, Any]) -> str:
    parsed = parse_time(value) or fallback
    return iso(review_slot(parsed, cfg))


def label_time(value: Any) -> str:
    parsed = parse_time(value)
    if parsed is None:
        return "未安排"
    return parsed.astimezone(review_timezone()).strftime("%Y-%m-%d %H:%M")


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


def us_symbol(code: str) -> str | None:
    upper = normalize_code(code)
    return upper.split(".", 1)[1] if upper.startswith("US.") else None


def state_records(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = state.get("records")
    if not isinstance(records, dict):
        return {}
    return {normalize_code(code): value for code, value in records.items() if isinstance(value, dict) and normalize_code(code)}


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


def opportunity_candidate_rows(opportunity_radar: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    raw = opportunity_radar.get("secondary_candidates")
    if not isinstance(raw, list):
        return rows
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = normalize_code(item.get("code"))
        if not code:
            continue
        action = str(item.get("action") or "")
        rows.append(
            {
                "code": code,
                "name": item.get("name") or code,
                "market": item.get("market") or code.split(".", 1)[0],
                "layer_name": item.get("layer") or item.get("theme_name") or "机会雷达",
                "chain_name": item.get("theme_name") or "机会发现雷达",
                "role": "预期差/未来机会候选",
                "price": item.get("price"),
                "layer_score": item.get("opportunity_score"),
                "market_confirmation_score": item.get("trend_score"),
                "data_status": "Opportunity Radar candidate",
                "action": action or "进入机会雷达观察",
                "source": "opportunity_radar",
            }
        )
    return rows


def cross_market_candidate_rows(cross_market_intelligence: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    raw = cross_market_intelligence.get("secondary_research_candidates")
    if not isinstance(raw, list):
        return rows
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = normalize_code(item.get("code"))
        if not code:
            continue
        opportunity_score = number(item.get("opportunity_score"))
        demand_score = number(item.get("demand_acceleration_score"))
        layer_score = max(value for value in [opportunity_score, demand_score] if value is not None) if any(
            value is not None for value in [opportunity_score, demand_score]
        ) else None
        reason = str(item.get("reason") or "")
        rows.append(
            {
                "code": code,
                "name": item.get("name") or code,
                "market": item.get("market") or code.split(".", 1)[0],
                "layer_name": item.get("layer") or item.get("theme") or "跨市场情报",
                "chain_name": item.get("theme") or "跨市场需求加速",
                "role": "需求加速 / 跨市场扩散候选",
                "price": item.get("price"),
                "layer_score": layer_score,
                "market_confirmation_score": item.get("trend_score"),
                "data_status": "Cross-market intelligence candidate",
                "action": reason or "加入观察池，交给 Buy-Side 二次分析",
                "source": "cross_market_intelligence",
            }
        )
    return rows


def merged_candidates(
    supply_radar: dict[str, Any],
    opportunity_radar: dict[str, Any],
    cross_market_intelligence: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    supply = supply_radar.get("candidates") if isinstance(supply_radar.get("candidates"), list) else []
    rows.extend([item for item in supply if isinstance(item, dict)])
    rows.extend(opportunity_candidate_rows(opportunity_radar))
    rows.extend(cross_market_candidate_rows(cross_market_intelligence))

    by_code: dict[str, dict[str, Any]] = {}
    for item in rows:
        code = normalize_code(item.get("code"))
        if not code:
            continue
        existing = by_code.get(code)
        if existing is None or candidate_priority(item) > candidate_priority(existing):
            by_code[code] = item
    return list(by_code.values())


def secondary_config(config: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "review_interval_days": 2,
        "max_active": None,
        "review_timezone": "Asia/Shanghai",
        "review_hour": 12,
        "review_minute": 0,
        "min_entry_layer_score": 75,
        "min_entry_trend_score": 70,
        "min_keep_layer_score": 70,
        "min_keep_trend_score": 55,
        "retrigger_layer_score": 78,
        "retrigger_trend_score": 80,
    }
    raw = config.get("secondary_analysis") if isinstance(config.get("secondary_analysis"), dict) else {}
    return {**defaults, **raw}


def candidate_is_eligible(item: dict[str, Any], cfg: dict[str, Any]) -> bool:
    action = str(item.get("action") or "")
    layer_score = number(item.get("layer_score"))
    trend_score = number(item.get("market_confirmation_score"))
    if layer_score is None or trend_score is None:
        return False
    if layer_score < float(cfg["min_entry_layer_score"]) or trend_score < float(cfg["min_entry_trend_score"]):
        return False
    return "二次分析" in action or "强候选" in action or "Buy-Side" in action


def candidate_priority(item: dict[str, Any]) -> float:
    return (number(item.get("layer_score")) or 0) * 1.2 + (number(item.get("market_confirmation_score")) or 0)


def retriggered(item: dict[str, Any], cfg: dict[str, Any]) -> bool:
    layer_score = number(item.get("layer_score")) or 0
    trend_score = number(item.get("market_confirmation_score")) or 0
    return layer_score >= float(cfg["retrigger_layer_score"]) and trend_score >= float(cfg["retrigger_trend_score"])


def enrich_record(record: dict[str, Any], item: dict[str, Any], timestamp: datetime) -> dict[str, Any]:
    code = normalize_code(item.get("code"))
    record.update(
        {
            "code": code,
            "name": item.get("name") or record.get("name") or code,
            "market": item.get("market") or record.get("market"),
            "layer_name": item.get("layer_name") or record.get("layer_name"),
            "chain_name": item.get("chain_name") or record.get("chain_name"),
            "role": item.get("role") or record.get("role"),
            "price": item.get("price"),
            "layer_score": item.get("layer_score"),
            "trend_score": item.get("market_confirmation_score"),
            "data_status": item.get("data_status"),
            "radar_action": item.get("action"),
            "last_seen_at": iso(timestamp),
            "priority": round(candidate_priority(item), 2),
        }
    )
    return record


def evaluate_record(record: dict[str, Any], current: dict[str, Any] | None, pack_index: dict[str, dict[str, Any]], cfg: dict[str, Any]) -> tuple[bool, str]:
    if not current:
        return False, "本轮未重新进入产业链强候选，退回普通观察。"

    layer_score = number(current.get("layer_score"))
    trend_score = number(current.get("market_confirmation_score"))
    price = number(current.get("price"))
    if price is None:
        return False, "缺少价格，不能完成二次分析。"
    if layer_score is None or layer_score < float(cfg["min_keep_layer_score"]):
        return False, f"产业链机会分低于保留门槛 {cfg['min_keep_layer_score']}。"
    if trend_score is None or trend_score < float(cfg["min_keep_trend_score"]):
        return False, f"趋势确认低于保留门槛 {cfg['min_keep_trend_score']}。"

    code = normalize_code(current.get("code"))
    symbol = us_symbol(code)
    indexed = pack_index.get(code) or (pack_index.get(symbol) if symbol else None) or {}
    reward_risk = number(indexed.get("reward_risk") or (indexed.get("entry") or {}).get("reward_risk"))
    data_confidence = number(indexed.get("data_confidence"))
    min_rr = float(cfg.get("min_reward_risk_for_buy", 2.0))
    min_confidence = float(cfg.get("min_data_confidence_for_buy", 0.68))

    if symbol and reward_risk is not None and reward_risk < min_rr:
        return False, f"美股候选 R/R {reward_risk:.2f}:1 低于 {min_rr:.1f}:1，退回观察。"
    if symbol and data_confidence is not None and data_confidence < min_confidence:
        return False, f"美股数据置信度 {data_confidence:.2f} 低于 {min_confidence:.2f}，退回观察。"

    if code.startswith(("HK.", "SH.", "SZ.")):
        data_status = str(current.get("data_status") or "")
        if "缺" in data_status or "补全" in data_status:
            return False, "港股/A股缺少可用行情，退回观察等待重新触发。"
        return True, "跨市场强候选仍满足产业链和趋势门槛；继续二次分析，但最终仍需 Futu/财报/流动性复核。"

    if reward_risk is None:
        return True, "产业链和趋势仍达标；R/R 尚未在机械包中完整给出，继续保留给 Buy-Side 完整复核。"
    return True, f"产业链、趋势和 R/R 仍达标；继续保留，下一次 {int(cfg['review_interval_days'])} 天后复核。"


def update_queue(
    radar: dict[str, Any],
    opportunity_radar: dict[str, Any],
    cross_market_intelligence: dict[str, Any],
    state: dict[str, Any],
    pack: dict[str, Any],
    config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    cfg = secondary_config(config)
    cfg["min_reward_risk_for_buy"] = float(config.get("min_reward_risk_for_buy", 2.0) or 2.0)
    cfg["min_data_confidence_for_buy"] = float(config.get("min_data_confidence_for_buy", 0.68) or 0.68)

    now = now_local(str(cfg.get("review_timezone") or "Asia/Shanghai"))
    current_review_slot = review_slot(now, cfg)
    candidates = [
        item
        for item in merged_candidates(radar, opportunity_radar, cross_market_intelligence)
        if normalize_code(item.get("code"))
    ]
    current_by_code = {normalize_code(item.get("code")): item for item in candidates}
    eligible = [item for item in candidates if candidate_is_eligible(item, cfg)]
    eligible.sort(key=candidate_priority, reverse=True)

    records = state_records(state)
    max_active_raw = cfg.get("max_active")
    max_active: int | None
    if max_active_raw is None or str(max_active_raw).strip().lower() in {"", "none", "null", "unlimited", "false"}:
        max_active = None
    else:
        max_active = int(max_active_raw)

    new_entries = 0
    reentries = 0
    for item in eligible:
        code = normalize_code(item.get("code"))
        record = records.get(code, {})
        if record.get("status") == "retreated":
            if not retriggered(item, cfg):
                enrich_record(record, item, now)
                record["cooldown_until"] = None
                record["last_result"] = "退回观察，等待重新触发"
                records[code] = record
                continue
            record["status"] = "active"
            record["reentered_at"] = iso(now)
            record["cooldown_until"] = None
            record["next_analysis_due_at"] = iso(current_review_slot)
            record["last_result"] = "重新触发"
            record["last_reason"] = "趋势确认和产业链机会分重新达到触发门槛。"
            reentries += 1
        elif not record:
            active_count = sum(1 for value in records.values() if value.get("status") == "active")
            if max_active is not None and active_count >= max_active:
                continue
            record = {
                "status": "active",
                "entered_at": iso(now),
                "next_analysis_due_at": iso(current_review_slot),
                "review_count": 0,
                "pass_count": 0,
                "fail_count": 0,
                "history": [],
                "last_result": "待二次分析",
                "last_reason": "从产业链雷达进入二次分析候选池。",
            }
            new_entries += 1
        enrich_record(record, item, now)
        records[code] = record

    for record in records.values():
        if record.get("last_analysis_at"):
            record["last_analysis_at"] = normalize_review_slot(record.get("last_analysis_at"), now, cfg)
        history = record.get("history")
        if isinstance(history, list):
            for event in history:
                if isinstance(event, dict) and event.get("reviewed_at"):
                    event["reviewed_at"] = normalize_review_slot(event.get("reviewed_at"), now, cfg)
        if record.get("status") == "active":
            record["next_analysis_due_at"] = normalize_review_slot(record.get("next_analysis_due_at"), now, cfg)
        elif record.get("status") == "retreated":
            record["cooldown_until"] = None

    active_due: list[dict[str, Any]] = []
    for record in records.values():
        if record.get("status") != "active":
            continue
        due_at = parse_time(record.get("next_analysis_due_at"))
        if due_at is None or due_at <= now:
            active_due.append(record)
    active_due.sort(key=lambda item: float(item.get("priority") or 0), reverse=True)
    due_to_review = active_due

    pack_index = market_pack_index(pack)
    reviews: list[dict[str, Any]] = []
    interval_days = int(cfg["review_interval_days"])
    analysis_time = current_review_slot
    for record in due_to_review:
        code = normalize_code(record.get("code"))
        current = current_by_code.get(code)
        passed, reason = evaluate_record(record, current, pack_index, cfg)
        record["review_count"] = int(record.get("review_count") or 0) + 1
        record["last_analysis_at"] = iso(analysis_time)
        if passed:
            record["status"] = "active"
            record["pass_count"] = int(record.get("pass_count") or 0) + 1
            record["next_analysis_due_at"] = iso(review_slot(analysis_time, cfg, days=interval_days))
            record["last_result"] = "通过复核"
            record["last_reason"] = reason
        else:
            record["status"] = "retreated"
            record["fail_count"] = int(record.get("fail_count") or 0) + 1
            record["next_analysis_due_at"] = None
            record["cooldown_until"] = None
            record["last_result"] = "退回观察"
            record["last_reason"] = reason
        history = record.get("history") if isinstance(record.get("history"), list) else []
        history.append(
            {
                "reviewed_at": iso(analysis_time),
                "result": record["last_result"],
                "reason": reason,
                "layer_score": record.get("layer_score"),
                "trend_score": record.get("trend_score"),
                "price": record.get("price"),
            }
        )
        record["history"] = history[-20:]
        records[code] = record
        reviews.append(
            {
                "code": code,
                "name": record.get("name"),
                "market": record.get("market"),
                "layer_name": record.get("layer_name"),
                "result": record.get("last_result"),
                "reason": reason,
                "price": record.get("price"),
                "layer_score": record.get("layer_score"),
                "trend_score": record.get("trend_score"),
                "next_analysis_due_at": record.get("next_analysis_due_at"),
                "cooldown_until": record.get("cooldown_until"),
            }
        )

    active = [record for record in records.values() if record.get("status") == "active"]
    retreated = [record for record in records.values() if record.get("status") == "retreated"]
    due_remaining = [
        record
        for record in active
        if (parse_time(record.get("next_analysis_due_at")) or now) <= now
        and normalize_code(record.get("code")) not in {item["code"] for item in reviews}
    ]
    deepseek_priority = [
        {
            "code": item["code"],
            "name": item.get("name"),
            "market": item.get("market"),
            "layer_name": item.get("layer_name"),
            "price": item.get("price"),
            "layer_score": item.get("layer_score"),
            "trend_score": item.get("trend_score"),
            "review_result": item.get("result"),
            "review_reason": item.get("reason"),
        }
        for item in reviews
        if item.get("result") == "通过复核"
    ]

    summary = {
        "eligible_candidates": len(eligible),
        "opportunity_candidates": len(opportunity_candidate_rows(opportunity_radar)),
        "cross_market_candidates": len(cross_market_candidate_rows(cross_market_intelligence)),
        "new_entries": new_entries,
        "reentries": reentries,
        "reviewed": len(reviews),
        "passed": sum(1 for item in reviews if item.get("result") == "通过复核"),
        "retreated": sum(1 for item in reviews if item.get("result") == "退回观察"),
        "active_count": len(active),
        "retreated_count": len(retreated),
        "due_remaining": len(due_remaining),
        "processing_limit": "none",
        "active_capacity": "unlimited" if max_active is None else max_active,
        "review_time_beijing": f"{int(cfg.get('review_hour', 12)):02d}:{int(cfg.get('review_minute', 0)):02d}",
    }
    next_state = {
        "schema_version": 1,
        "generated_at": iso(now),
        "generated_label": now.strftime("%Y-%m-%d %H:%M"),
        "rules": cfg,
        "summary": summary,
        "records": dict(sorted(records.items())),
    }
    latest = {
        "schema_version": 1,
        "generated_at": next_state["generated_at"],
        "generated_label": next_state["generated_label"],
        "rules": cfg,
        "summary": summary,
        "reviews": reviews,
        "deepseek_priority": deepseek_priority,
        "active": sorted(active, key=lambda item: str(item.get("next_analysis_due_at") or ""))[:80],
        "retreated": sorted(retreated, key=lambda item: float(item.get("priority") or 0), reverse=True)[:80],
    }
    return next_state, latest


def render_report(payload: dict[str, Any]) -> str:
    rules = payload.get("rules", {})
    summary = payload.get("summary", {})
    lines = [
        "# 二次分析队列",
        "",
        f"- 生成时间：{payload.get('generated_label')}",
        f"- 规则：进入二次分析后每 {rules.get('review_interval_days', 2)} 天复核一次；统一在北京时间 {int(rules.get('review_hour', 12)):02d}:{int(rules.get('review_minute', 0)):02d} 处理；不设每轮处理数量上限；不合格则退回观察；无冷却期，重新满足触发条件即可回到二次分析。",
        "- 定位：这是研究资源调度层，不生成买入指令；最终交易仍需完整 Buy-Side 分析、R/R 和整股仓位复核。",
        "",
        "## 本轮概览",
        "",
        "| 合格候选 | 机会雷达候选 | 跨市场候选 | 队列容量 | 新进队列 | 重新触发 | 本轮复核 | 通过 | 退回观察 | 活跃队列 | 已到期未处理 |",
        "|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|",
        f"| {summary.get('eligible_candidates', 0)} | {summary.get('opportunity_candidates', 0)} | {summary.get('cross_market_candidates', 0)} | {summary.get('active_capacity', 'unlimited')} | {summary.get('new_entries', 0)} | {summary.get('reentries', 0)} | {summary.get('reviewed', 0)} | {summary.get('passed', 0)} | {summary.get('retreated', 0)} | {summary.get('active_count', 0)} | {summary.get('due_remaining', 0)} |",
        "",
        "## 本轮二次分析复核",
        "",
    ]
    reviews = payload.get("reviews") if isinstance(payload.get("reviews"), list) else []
    if not reviews:
        lines.append("本轮没有到期复核的股票。")
    else:
        lines.extend(["| 结果 | 市场 | 代码 | 名称 | 环节 | 价格 | 机会分 | 趋势确认 | 原因 | 下次动作 |", "|---|---|---|---|---|---:|---:|---:|---|---|"])
        for item in reviews:
            next_action = label_time(item.get("next_analysis_due_at")) if item.get("result") == "通过复核" else f"等待重新触发：机会分 >= {rules.get('retrigger_layer_score')} 且趋势确认 >= {rules.get('retrigger_trend_score')}"
            lines.append(
                f"| {item.get('result')} | {item.get('market')} | {item.get('code')} | {item.get('name')} | {item.get('layer_name')} | {fmt_price(item.get('price'))} | {fmt_num(item.get('layer_score'))} | {fmt_num(item.get('trend_score'))} | {item.get('reason')} | {next_action} |"
            )

    priority = payload.get("deepseek_priority") if isinstance(payload.get("deepseek_priority"), list) else []
    lines.extend(["", "## DeepSeek / Buy-Side 优先名单", ""])
    if not priority:
        lines.append("本轮没有通过复核并需要进入 DeepSeek/Buy-Side 正文分析的新增标的。")
    else:
        lines.extend(["| 市场 | 代码 | 名称 | 环节 | 价格 | 机会分 | 趋势确认 |", "|---|---|---|---|---:|---:|---:|"])
        for item in priority:
            lines.append(
                f"| {item.get('market')} | {item.get('code')} | {item.get('name')} | {item.get('layer_name')} | {fmt_price(item.get('price'))} | {fmt_num(item.get('layer_score'))} | {fmt_num(item.get('trend_score'))} |"
            )

    active = payload.get("active") if isinstance(payload.get("active"), list) else []
    lines.extend(["", "## 活跃二次分析池", ""])
    if not active:
        lines.append("当前没有活跃二次分析标的。")
    else:
        lines.extend(["| 市场 | 代码 | 名称 | 环节 | 状态 | 上次分析 | 下次分析 | 最近结论 |", "|---|---|---|---|---|---|---|---|"])
        for item in active[:40]:
            lines.append(
                f"| {item.get('market')} | {item.get('code')} | {item.get('name')} | {item.get('layer_name')} | {item.get('status')} | {label_time(item.get('last_analysis_at'))} | {label_time(item.get('next_analysis_due_at'))} | {item.get('last_result')}：{item.get('last_reason')} |"
            )

    retreated = payload.get("retreated") if isinstance(payload.get("retreated"), list) else []
    lines.extend(["", "## 退回观察池", ""])
    if not retreated:
        lines.append("当前没有退回观察的标的。")
    else:
        lines.extend(["| 市场 | 代码 | 名称 | 环节 | 重新触发状态 | 退回原因 | 重新触发条件 |", "|---|---|---|---|---|---|---|"])
        for item in retreated[:40]:
            lines.append(
                f"| {item.get('market')} | {item.get('code')} | {item.get('name')} | {item.get('layer_name')} | 无冷却，达标即回池 | {item.get('last_reason')} | 机会分 >= {rules.get('retrigger_layer_score')} 且趋势确认 >= {rules.get('retrigger_trend_score')} |"
            )

    lines.extend(
        [
            "",
            "## 执行纪律",
            "",
            "- 通过复核只代表继续占用研究名额，不代表可以买。",
            "- 退回观察不是永久放弃；没有时间冷却，只要重新满足触发条件就可以再次进入队列。",
            "- 港股/A股即使通过队列复核，也必须单独用 Futu/财报/流动性确认后才允许进一步讨论交易。",
            "- 如果 R/R 明确低于 2:1，或趋势明显转弱，直接退回观察。",
        ]
    )
    return "\n".join(lines) + "\n"


def archive_copy(report_path: Path) -> Path:
    timestamp = now_local().strftime("%Y%m%d-%H%M")
    archive = report_path.with_name(f"secondary-analysis-queue-{timestamp}.md")
    archive.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--radar", type=Path, default=DEFAULT_RADAR)
    parser.add_argument("--opportunity-radar", type=Path, default=DEFAULT_OPPORTUNITY_RADAR)
    parser.add_argument("--cross-market-intelligence", type=Path, default=DEFAULT_CROSS_MARKET_INTELLIGENCE)
    parser.add_argument("--market-pack", type=Path, default=DEFAULT_MARKET_PACK)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--no-archive-copy", action="store_true")
    args = parser.parse_args()

    config = load_json(args.config, {})
    radar = load_json(args.radar, {})
    if not radar:
        raise SystemExit(f"Supply-chain radar not found or invalid: {args.radar}")
    opportunity_radar = load_json(args.opportunity_radar, {})
    cross_market_intelligence = load_json(args.cross_market_intelligence, {})
    pack = load_json(args.market_pack, {})
    state = load_json(args.state, {})
    next_state, latest = update_queue(radar, opportunity_radar, cross_market_intelligence, state, pack, config)
    write_json(args.state, next_state)
    write_json(args.out, latest)
    write_text(args.report, render_report(latest))
    if not args.no_archive_copy:
        archive = archive_copy(args.report)
        print(f"Wrote {archive}")
    print(f"Wrote {args.state}")
    print(f"Wrote {args.out}")
    print(f"Wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
