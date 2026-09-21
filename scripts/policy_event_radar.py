#!/usr/bin/env python3
"""Audit policy surprises and the *subsequent* market response.

This is contextual research, never a buy signal. Expectations must be recorded
before the decision; missing FRED observations remain unknown, not bullish.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
from typing import Any

from macro_regime import (
    DATA_DIR,
    DOCS_DATA_DIR,
    REPORTS_DIR,
    ROOT,
    fetch_fred_series,
    load_environment,
    load_json,
    now_local,
    number,
    write_json,
    write_text,
)

DEFAULT_CONFIG = ROOT / "config" / "policy_events.json"
DEFAULT_OUTPUT = DATA_DIR / "latest_policy_event_radar.json"
DEFAULT_DOCS_OUTPUT = DOCS_DATA_DIR / "policy_event_radar.json"
DEFAULT_REPORT = REPORTS_DIR / "latest-policy-event-radar.md"
SERIES = ("SP500", "NASDAQCOM", "DJIA", "DGS2", "DGS10", "DCOILWTICO")
SERIES_URL = "https://fred.stlouisfed.org/series/"


def parse_instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def valid_event(event: dict[str, Any], today: date) -> bool:
    try:
        event_day = date.fromisoformat(str(event.get("event_date") or ""))
    except ValueError:
        return False
    return event_day <= today


def policy_surprise(event: dict[str, Any]) -> dict[str, Any]:
    expected = number(event.get("expected_change_bps"))
    actual = number(event.get("actual_change_bps"))
    release = parse_instant(event.get("release_at"))
    as_of = parse_instant(event.get("expectation_as_of"))
    morning_pre_release = (
        not event.get("expectation_as_of")
        and release is not None
        and str(event.get("expectation_as_of_date") or "") == release.date().isoformat()
        and event.get("expectation_session") == "morning_pre_release"
        and release.hour >= 12
    )
    pre_release = release is not None and ((as_of is not None and as_of < release) or morning_pre_release)
    source = str(event.get("expectation_source_url") or "")
    decision_source = str(event.get("decision_source_url") or "")
    if expected is None or actual is None or not pre_release or not source or not decision_source:
        return {"status": "unknown", "label": "会前预期或决定缺证据", "surprise_bps": None}
    difference = round(actual - expected, 1)
    if difference > 0:
        status, label = "hawkish", "实际加息幅度高于会前预期"
    elif difference < 0:
        status, label = "dovish", "实际加息幅度低于会前预期"
    else:
        status, label = "as_expected", "利率决定符合会前预期"
    return {"status": status, "label": label, "surprise_bps": difference}


def path_revision(event: dict[str, Any]) -> dict[str, Any]:
    current = number(event.get("year_end_rate_median_current_pct"))
    prior = number(event.get("year_end_rate_median_prior_pct"))
    if current is None or prior is None or not event.get("path_source_url"):
        return {"status": "unknown", "label": "利率路径待确认", "revision_pp": None}
    revision = round(current - prior, 2)
    status = "hawkish" if revision > 0 else "dovish" if revision < 0 else "unchanged"
    label = "年末利率路径上修" if revision > 0 else "年末利率路径下修" if revision < 0 else "年末利率路径未变"
    return {"status": status, "label": label, "revision_pp": revision}


def observation(rows: list[dict[str, Any]], on_date: str) -> float | None:
    for row in rows:
        if row.get("date") == on_date:
            return number(row.get("value"))
    return None


def market_reaction(event_day: date, observations: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Compare pre-meeting close to second *post-meeting* trading close.

    FRED close-to-close data are not intraday causal evidence. Index count is a
    participation proxy, not exchange advance/decline breadth.
    """
    sp_rows = observations.get("SP500") or []
    post_dates = sorted({str(row.get("date")) for row in sp_rows if str(row.get("date")) > event_day.isoformat()})
    pre_dates = sorted({str(row.get("date")) for row in sp_rows if str(row.get("date")) < event_day.isoformat()})
    if not pre_dates or len(post_dates) < 2:
        return {"status": "unknown", "label": "会后两个交易日的收盘数据不足", "window": None, "indices": {}, "cross_assets": {}}
    start, end = pre_dates[-1], post_dates[1]
    indices: dict[str, float | None] = {}
    for key in ("SP500", "NASDAQCOM", "DJIA"):
        before = observation(observations.get(key) or [], start)
        after = observation(observations.get(key) or [], end)
        indices[key] = round((after / before - 1) * 100, 2) if before and after is not None else None
    cross_assets: dict[str, float | None] = {}
    for key in ("DGS2", "DGS10"):
        before = observation(observations.get(key) or [], start)
        after = observation(observations.get(key) or [], end)
        cross_assets[key + "_change_bps"] = round((after - before) * 100, 1) if before is not None and after is not None else None
    oil_before = observation(observations.get("DCOILWTICO") or [], start)
    oil_after = observation(observations.get("DCOILWTICO") or [], end)
    cross_assets["WTI_change_pct"] = round((oil_after / oil_before - 1) * 100, 2) if oil_before and oil_after is not None else None
    valid_indices = [value for value in indices.values() if value is not None]
    if len(valid_indices) < 2:
        status, label = "unknown", "主要指数数据不足，无法确认广泛反弹"
    elif all(value > 0 for value in valid_indices) and len(valid_indices) == 3:
        status, label = "broad_positive", "三项主要指数均高于会前收盘"
    elif all(value <= 0 for value in valid_indices):
        status, label = "broad_negative", "主要指数均未收复会前收盘"
    else:
        status, label = "mixed", "主要指数反应分化，不能概括为全面利好"
    return {"status": status, "label": label, "window": {"start": start, "end": end}, "indices": indices, "cross_assets": cross_assets}


def build_radar(
    config: dict[str, Any],
    observations: dict[str, list[dict[str, Any]]],
    *,
    now: datetime | None = None,
    fetch_errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    now = now or now_local()
    events = [item for item in config.get("events", []) if isinstance(item, dict) and valid_event(item, now.date())]
    events.sort(key=lambda item: str(item.get("event_date")), reverse=True)
    event = events[0] if events else None
    base = {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_label": now.strftime("%Y-%m-%d %H:%M"),
        "data_boundary": "政策事件只提供风险情境；不直接给股票买点、不放宽入场/止损/目标价及 R/R 门槛。",
    }
    if not event:
        return {**base, "status": "unknown", "summary": "暂无可核验的政策事件；不得按加息/降息方向推断股市。", "data_gaps": ["缺少当期会前预期与正式政策决定"]}
    event_day = date.fromisoformat(str(event["event_date"]))
    surprise = policy_surprise(event)
    path = path_revision(event)
    reaction = market_reaction(event_day, observations)
    age_days = (now.date() - event_day).days
    current_relevance = "recent" if age_days <= 7 else "historical"
    gaps = []
    if surprise["status"] == "unknown":
        gaps.append("会前时间戳、预期或正式决定来源不足")
    if path["status"] == "unknown":
        gaps.append("点阵图/利率路径缺少可比来源")
    if reaction["status"] == "unknown":
        gaps.append("会前与会后两个交易日的 FRED 收盘数据不足")
    if fetch_errors:
        gaps.append("FRED 指数/利率请求失败：" + "、".join(f"{key} ({value})" for key, value in fetch_errors.items()))
    summary = f"{surprise['label']}；{path['label']}；{reaction['label']}。"
    if current_relevance == "historical":
        summary += "该事件已超过7天，仅作历史复盘，不作为今日买入过滤器。"
    else:
        summary += "会后价格反应需要和收益率、油价及个股证据一同复核。"
    return {
        **base,
        "status": "ready" if not gaps else "limited",
        "id": event.get("id"),
        "name": event.get("name"),
        "event_date": event_day.isoformat(),
        "current_relevance": current_relevance,
        "expectation": {
            **{key: event.get(key) for key in ("expected_change_bps", "expectation_as_of", "expectation_as_of_date", "expectation_session", "expectation_note", "expectation_source", "expectation_source_url")},
            "source_type": "vendor_fallback",
            "confidence": "medium" if surprise["status"] != "unknown" else "low",
        },
        "decision": {
            **{key: event.get(key) for key in ("actual_change_bps", "decision_source", "decision_source_url")},
            "source_type": "official",
            "confidence": "high" if event.get("decision_source_url") else "low",
        },
        "path": {**path, "current_pct": event.get("year_end_rate_median_current_pct"), "prior_pct": event.get("year_end_rate_median_prior_pct"), "source": event.get("path_source"), "source_url": event.get("path_source_url"), "source_type": "official", "confidence": "high" if path["status"] != "unknown" else "low"},
        "surprise": surprise,
        "market_reaction": {**reaction, "source": "FRED", "source_urls": {series_id: SERIES_URL + series_id for series_id in SERIES}, "confidence": "medium" if reaction["status"] != "unknown" else "low"},
        "summary": summary,
        "data_gaps": gaps,
        "source_note": "FRED 为公开日收盘观察值，不能识别日内政策因果；SP500/NASDAQCOM/DJIA 仅是指数参与度代理，不是真实涨跌家数。",
    }


def build_report(payload: dict[str, Any]) -> str:
    event = str(payload.get("name") or "暂无事件")
    surprise = payload.get("surprise") or {}
    path = payload.get("path") or {}
    reaction = payload.get("market_reaction") or {}
    window = reaction.get("window") or {}
    lines = [
        "# 政策预期差与会后确认雷达", "",
        f"- 生成时间：{payload.get('generated_label')} Asia/Shanghai",
        f"- 事件：{event}",
        f"- 结论：{payload.get('summary')}",
        "", "## 重点先看", "",
        "| 维度 | 判断 |", "|---|---|",
        f"| 决定 vs 会前预期 | {surprise.get('label') or '待确认'} |",
        f"| 后续政策路径 | {path.get('label') or '待确认'} |",
        f"| 会后两日市场确认 | {reaction.get('label') or '待确认'} |",
        f"| 对选股的影响 | 不自动升级任何股票；仅提示重新核验行业、收益率敏感度及原有硬门槛 |",
        "", "## 可追溯证据", "",
    ]
    for label, url in (
        ("会前预期", (payload.get("expectation") or {}).get("expectation_source_url")),
        ("正式决定", (payload.get("decision") or {}).get("decision_source_url")),
        ("政策路径", path.get("source_url")),
    ):
        if url:
            lines.append(f"- {label}：[来源]({url})")
    if window:
        lines.extend(["", f"## 会后检验窗口：{window.get('start')} → {window.get('end')}", "", "| 指标 | 变化 | 来源 |", "|---|---:|---|"])
        for key, value in (reaction.get("indices") or {}).items():
            lines.append(f"| {key} | {value if value is not None else '待确认'}% | [FRED]({SERIES_URL}{key}) |")
        for key, value in (reaction.get("cross_assets") or {}).items():
            unit = "bp" if key.endswith("_bps") else "%"
            series_id = key.split("_change")[0] if key != "WTI_change_pct" else "DCOILWTICO"
            lines.append(f"| {key} | {value if value is not None else '待确认'}{unit} | [FRED]({SERIES_URL}{series_id}) |")
    lines.extend(["", "## 数据边界", "", f"- {payload.get('data_boundary')}", f"- {payload.get('source_note') or '证据不足时保持待确认。'}"])
    for gap in payload.get("data_gaps") or []:
        lines.append(f"- 数据缺口：{gap}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--docs-output", type=Path, default=DEFAULT_DOCS_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    load_environment()
    import os

    api_key = os.getenv("FRED_API_KEY", "").strip()
    observations: dict[str, list[dict[str, Any]]] = {}
    fetch_errors: dict[str, str] = {}
    if api_key:
        for series_id in SERIES:
            try:
                observations[series_id] = fetch_fred_series(api_key, series_id, limit=90)
            except Exception as exc:
                reason = f"HTTP {exc.code}" if hasattr(exc, "code") else type(exc).__name__
                print(f"FRED {series_id} unavailable: {reason}")
                fetch_errors[series_id] = reason
                observations[series_id] = []
    else:
        fetch_errors["all"] = "FRED_API_KEY 未配置"
    payload = build_radar(load_json(args.config, {}), observations, fetch_errors=fetch_errors)
    write_json(args.output, payload)
    write_json(args.docs_output, payload)
    write_text(args.report, build_report(payload))
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
