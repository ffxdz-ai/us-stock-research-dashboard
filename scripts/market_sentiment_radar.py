#!/usr/bin/env python3
"""Build a public, privacy-safe market sentiment radar.

This layer is deliberately deterministic.  It summarizes broad risk appetite
from public market proxies and FRED macro/regime data, then writes:

- data/latest_market_sentiment.json
- docs/data/market_sentiment.json
- reports/latest-market-sentiment.md

It does not read or expose portfolio size, holdings, cost basis, or trading
interfaces.  The output is a research context layer only and must not override
single-stock Buy-Side risk/reward discipline.
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
DATA_DIR = ROOT / "data"
DOCS_DATA_DIR = ROOT / "docs" / "data"
REPORTS_DIR = ROOT / "reports"

DEFAULT_MARKET_PACK = DATA_DIR / "latest_market_pack.json"
DEFAULT_MACRO_REGIME = DATA_DIR / "latest_macro_regime.json"
DEFAULT_OUTPUT = DATA_DIR / "latest_market_sentiment.json"
DEFAULT_DOCS_OUTPUT = DOCS_DATA_DIR / "market_sentiment.json"
DEFAULT_REPORT = REPORTS_DIR / "latest-market-sentiment.md"


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
        cleaned = value.replace(",", "").replace("%", "").strip()
        if not cleaned or cleaned.lower() in {"n/a", "nan", "none", "null", "--", "数据不足", "待确认"}:
            return None
        try:
            parsed = float(cleaned)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))


def average(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None and math.isfinite(value)]
    if not clean:
        return None
    return sum(clean) / len(clean)


def quote(market_pack: dict[str, Any], symbol: str) -> dict[str, Any]:
    market = market_pack.get("market") if isinstance(market_pack.get("market"), dict) else {}
    item = market.get(symbol)
    return item if isinstance(item, dict) else {}


def quote_price(market_pack: dict[str, Any], symbol: str) -> float | None:
    item = quote(market_pack, symbol)
    return number(item.get("regularMarketPrice") or item.get("price") or item.get("futu_regular_price"))


def quote_change_pct(market_pack: dict[str, Any], symbol: str) -> float | None:
    item = quote(market_pack, symbol)
    direct = number(item.get("regularMarketChangePercent") or item.get("change_pct"))
    if direct is not None:
        return direct
    price = number(item.get("regularMarketPrice") or item.get("price") or item.get("futu_regular_price"))
    previous = number(item.get("previousClose") or item.get("regularMarketPreviousClose") or item.get("futu_previous_close"))
    if price is None or previous is None or previous == 0:
        return None
    return (price / previous - 1) * 100


def quote_time(market_pack: dict[str, Any], symbol: str) -> str:
    item = quote(market_pack, symbol)
    return str(item.get("regularMarketTime") or item.get("quote_time") or item.get("time") or "")


def quote_source(market_pack: dict[str, Any], symbol: str) -> str:
    item = quote(market_pack, symbol)
    return str(item.get("source") or item.get("quote_source") or "公开行情快照")


def macro_indicator(macro: dict[str, Any], series_id: str) -> dict[str, Any]:
    indicators = macro.get("indicators") if isinstance(macro.get("indicators"), dict) else {}
    item = indicators.get(series_id)
    return item if isinstance(item, dict) else {}


def macro_value(macro: dict[str, Any], series_id: str, key: str = "latest_value") -> float | None:
    return number(macro_indicator(macro, series_id).get(key))


def macro_date(macro: dict[str, Any], series_id: str) -> str:
    return str(macro_indicator(macro, series_id).get("latest_date") or "")


def status_from_score(score: float | None, vix: float | None) -> tuple[str, str, str]:
    if score is None:
        return "unknown", "待确认", "情绪数据不足，先按中性处理"
    if score >= 82 and vix is not None and vix < 15:
        return "euphoria", "Risk-on / 拥挤偏热", "风险偏好很强，但要防止低波动追高"
    if score >= 65:
        return "risk_on", "Risk-on", "风险偏好偏强，可提高进攻观察权重"
    if score >= 45:
        return "neutral", "Neutral", "情绪中性，等待方向确认"
    if score >= 30:
        return "risk_off", "Risk-off", "风险偏好偏弱，优先防守和等待"
    return "panic", "Risk-off / 恐慌", "避险压力较高，禁止硬接飞刀"


def component_status(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score >= 65:
        return "risk_on"
    if score >= 45:
        return "neutral"
    return "risk_off"


def score_vix(vix: float | None, three_month_change: float | None = None) -> float | None:
    if vix is None:
        return None
    base = 100 - (vix - 12) * 4.0
    if three_month_change is not None:
        base += clamp(-three_month_change, -8, 8)
    return round(clamp(base, 5, 95), 1)


def score_equity_tape(changes: list[float | None]) -> float | None:
    avg = average(changes)
    if avg is None:
        return None
    return round(clamp(50 + avg * 12, 5, 95), 1)


def score_relative_growth(spy: float | None, qqq: float | None, iwm: float | None) -> float | None:
    if spy is None:
        return None
    pieces: list[float] = []
    if qqq is not None:
        pieces.append((qqq - spy) * 0.65)
    if iwm is not None:
        pieces.append((iwm - spy) * 0.35)
    if not pieces:
        return None
    return round(clamp(50 + sum(pieces) * 14, 5, 95), 1)


def score_defensive_flow(spy: float | None, tlt: float | None, gld: float | None) -> float | None:
    defensive = average([tlt, gld])
    if spy is None or defensive is None:
        return None
    return round(clamp(50 + (spy - defensive) * 10, 5, 95), 1)


def score_macro_backdrop(macro: dict[str, Any]) -> float | None:
    dimensions = macro.get("dimensions") if isinstance(macro.get("dimensions"), dict) else {}
    liquidity = number(dimensions.get("liquidity"))
    risk_appetite = number(dimensions.get("risk_appetite"))
    composite = number(dimensions.get("composite"))
    credit_spread = macro_value(macro, "BAA10Y")
    credit_change = macro_value(macro, "BAA10Y", "three_month_change")
    credit_score: float | None = None
    if credit_spread is not None:
        if credit_spread <= 1.6:
            credit_score = 78
        elif credit_spread <= 2.2:
            credit_score = 65
        elif credit_spread <= 3.0:
            credit_score = 45
        else:
            credit_score = 25
        if credit_change is not None:
            credit_score += clamp(-credit_change * 20, -8, 8)
    return round(average([liquidity, risk_appetite, composite, credit_score]) or 0, 1) if average([liquidity, risk_appetite, composite, credit_score]) is not None else None


def score_oil_stress(uso_change: float | None, wti_change_3m: float | None) -> float | None:
    if uso_change is None and wti_change_3m is None:
        return None
    score = 60.0
    if uso_change is not None:
        if uso_change >= 3:
            score -= 18
        elif uso_change >= 1:
            score -= 8
        elif uso_change <= -3:
            score -= 3
        else:
            score += 2
    if wti_change_3m is not None:
        if wti_change_3m >= 10:
            score -= 10
        elif wti_change_3m <= -10:
            score += 5
    return round(clamp(score, 5, 90), 1)


def component(
    *,
    key: str,
    name: str,
    score: float | None,
    weight: float,
    message: str,
    value: Any = None,
    source: str = "",
    updated_at: str = "",
    confidence: str = "medium",
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "score": score,
        "weight": weight,
        "status": component_status(score),
        "value": value,
        "message": message,
        "source": source,
        "source_type": "official" if source == "FRED" else "vendor_fallback",
        "updated_at": updated_at,
        "confidence": confidence if score is not None else "low",
    }


def weighted_score(components: list[dict[str, Any]]) -> float | None:
    usable = [item for item in components if number(item.get("score")) is not None and number(item.get("weight")) is not None]
    weight_sum = sum(float(item["weight"]) for item in usable)
    if weight_sum <= 0:
        return None
    return round(sum(float(item["score"]) * float(item["weight"]) for item in usable) / weight_sum, 1)


def build_sentiment(market_pack: dict[str, Any], macro: dict[str, Any]) -> dict[str, Any]:
    now = now_local()
    generated_at = now.isoformat(timespec="seconds")
    generated_label = now.strftime("%Y-%m-%d %H:%M")
    data_gaps: list[dict[str, Any]] = []

    spy = quote_change_pct(market_pack, "SPY")
    qqq = quote_change_pct(market_pack, "QQQ")
    dia = quote_change_pct(market_pack, "DIA")
    iwm = quote_change_pct(market_pack, "IWM")
    tlt = quote_change_pct(market_pack, "TLT")
    gld = quote_change_pct(market_pack, "GLD")
    uso = quote_change_pct(market_pack, "USO")

    vix_quote = quote_price(market_pack, "^VIX")
    vix_macro = macro_value(macro, "VIXCLS")
    vix = vix_quote if vix_quote is not None else vix_macro
    vix_source = quote_source(market_pack, "^VIX") if vix_quote is not None else "FRED"
    vix_time = quote_time(market_pack, "^VIX") if vix_quote is not None else macro_date(macro, "VIXCLS")

    if vix is None:
        data_gaps.append(
            {
                "field": "VIX",
                "message": "缺少 VIX 实时或 FRED VIXCLS 数据，波动率情绪置信度下降",
                "impact": "无法确认恐慌/低波拥挤状态",
            }
        )
    if average([spy, qqq, dia, iwm]) is None:
        data_gaps.append(
            {
                "field": "equity_index_change",
                "message": "缺少 SPY/QQQ/DIA/IWM 指数代理涨跌幅",
                "impact": "无法完整判断股票风险偏好",
            }
        )
    if not quote(market_pack, "SPY").get("chart"):
        data_gaps.append(
            {
                "field": "index_moving_average",
                "message": "市场指数暂缺 20/50/200 日均线结构",
                "impact": "情绪雷达暂不判断中期趋势位置，只判断短期风险偏好",
            }
        )

    vix_component_score = score_vix(vix, macro_value(macro, "VIXCLS", "three_month_change"))
    equity_component_score = score_equity_tape([spy, qqq, dia, iwm])
    leadership_component_score = score_relative_growth(spy, qqq, iwm)
    defensive_component_score = score_defensive_flow(spy, tlt, gld)
    macro_component_score = score_macro_backdrop(macro)
    oil_component_score = score_oil_stress(uso, macro_value(macro, "DCOILWTICO", "three_month_pct_change"))

    components = [
        component(
            key="volatility",
            name="波动率压力",
            score=vix_component_score,
            weight=0.25,
            value=round(vix, 2) if vix is not None else None,
            message="VIX 越低代表恐慌越少，但过低也可能意味着拥挤。",
            source=vix_source,
            updated_at=vix_time,
            confidence="high" if vix_source == "FRED" else "medium",
        ),
        component(
            key="equity_tape",
            name="股票指数短线表现",
            score=equity_component_score,
            weight=0.25,
            value={
                "SPY": round(spy, 2) if spy is not None else None,
                "QQQ": round(qqq, 2) if qqq is not None else None,
                "DIA": round(dia, 2) if dia is not None else None,
                "IWM": round(iwm, 2) if iwm is not None else None,
            },
            message="用 SPY/QQQ/DIA/IWM 涨跌幅判断当天股票风险偏好。",
            source="public market quote",
            updated_at=quote_time(market_pack, "SPY") or str(market_pack.get("as_of_utc") or ""),
        ),
        component(
            key="growth_leadership",
            name="成长/小盘相对强弱",
            score=leadership_component_score,
            weight=0.15,
            value={
                "QQQ_minus_SPY": round(qqq - spy, 2) if qqq is not None and spy is not None else None,
                "IWM_minus_SPY": round(iwm - spy, 2) if iwm is not None and spy is not None else None,
            },
            message="成长股和小盘股相对大盘越强，风险偏好通常越高。",
            source="public market quote",
            updated_at=quote_time(market_pack, "QQQ") or str(market_pack.get("as_of_utc") or ""),
        ),
        component(
            key="defensive_flow",
            name="避险资产相对表现",
            score=defensive_component_score,
            weight=0.10,
            value={
                "SPY": round(spy, 2) if spy is not None else None,
                "TLT": round(tlt, 2) if tlt is not None else None,
                "GLD": round(gld, 2) if gld is not None else None,
            },
            message="股票强于长债/黄金代表风险偏好改善；反之提示避险。",
            source="public market quote",
            updated_at=quote_time(market_pack, "TLT") or str(market_pack.get("as_of_utc") or ""),
        ),
        component(
            key="macro_liquidity_credit",
            name="宏观流动性/信用背景",
            score=macro_component_score,
            weight=0.20,
            value={
                "liquidity_score": (macro.get("dimensions") or {}).get("liquidity") if isinstance(macro.get("dimensions"), dict) else None,
                "risk_appetite_score": (macro.get("dimensions") or {}).get("risk_appetite") if isinstance(macro.get("dimensions"), dict) else None,
                "BAA10Y": macro_value(macro, "BAA10Y"),
            },
            message="FRED 流动性、金融条件和信用利差用于判断风险资产背景。",
            source="FRED",
            updated_at=macro.get("generated_label") or macro.get("generated_at") or "",
            confidence="high" if macro else "low",
        ),
        component(
            key="oil_stress",
            name="油价/通胀压力",
            score=oil_component_score,
            weight=0.05,
            value={
                "USO_change_pct": round(uso, 2) if uso is not None else None,
                "WTI_3m_change_pct": macro_value(macro, "DCOILWTICO", "three_month_pct_change"),
            },
            message="油价快速上涨会压低情绪分，因为它可能强化通胀和利率压力。",
            source="public market quote/FRED",
            updated_at=quote_time(market_pack, "USO") or macro_date(macro, "DCOILWTICO"),
        ),
    ]

    score = weighted_score(components)
    status, status_label, stance = status_from_score(score, vix)

    rules_triggered: list[str] = []
    if vix is not None:
        if vix >= 25:
            rules_triggered.append(f"VIX {vix:.2f} 高于 25，风险偏好降级。")
        elif vix <= 15:
            rules_triggered.append(f"VIX {vix:.2f} 低于 15，风险偏好较强但需警惕拥挤。")
    if equity_component_score is not None and equity_component_score < 40:
        rules_triggered.append("主要股票指数同步走弱，降低进攻性。")
    if leadership_component_score is not None and leadership_component_score >= 60:
        rules_triggered.append("QQQ/IWM 相对 SPY 表现较强，成长/风险偏好得到确认。")
    if macro_component_score is not None and macro_component_score >= 65:
        rules_triggered.append("FRED 流动性/信用背景偏友好，支持结构性风险资产。")
    if not rules_triggered:
        rules_triggered.append("暂无单一极端信号，按综合分处理。")

    summary = build_summary(score, status_label, components)
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "generated_label": generated_label,
        "data_boundary": "公开脱敏市场情绪层；只用于判断风险环境，不生成交易指令，不替代个股 R/R 纪律。",
        "score": score,
        "status": status,
        "status_label": status_label,
        "stance": stance,
        "summary": summary,
        "components": components,
        "rules_triggered": rules_triggered,
        "data_gaps": data_gaps,
        "source_note": "行情代理来自 market_pack，宏观/波动率 fallback 来自 FRED；缺失字段写入 data_gaps，不编造。",
    }


def build_summary(score: float | None, status_label: str, components: list[dict[str, Any]]) -> str:
    if score is None:
        return "市场情绪数据不足，暂按待确认处理。"
    strongest = max((item for item in components if number(item.get("score")) is not None), key=lambda item: number(item.get("score")) or 0, default=None)
    weakest = min((item for item in components if number(item.get("score")) is not None), key=lambda item: number(item.get("score")) or 0, default=None)
    parts = [f"综合情绪分 {score:.1f}，状态 {status_label}。"]
    if strongest:
        parts.append(f"最强支撑来自{strongest.get('name')}（{number(strongest.get('score')):.1f}）。")
    if weakest:
        parts.append(f"主要拖累来自{weakest.get('name')}（{number(weakest.get('score')):.1f}）。")
    parts.append("该结论只决定仓位/进攻性倾向，不直接给个股买点。")
    return "".join(parts)


def fmt_score(value: Any) -> str:
    parsed = number(value)
    return "待确认" if parsed is None else f"{parsed:.1f}"


def fmt_value(value: Any) -> str:
    if isinstance(value, dict):
        pairs = []
        for key, raw in value.items():
            parsed = number(raw)
            pairs.append(f"{key}: {'待确认' if parsed is None else f'{parsed:.2f}'}")
        return "；".join(pairs)
    parsed = number(value)
    if parsed is not None:
        return f"{parsed:.2f}"
    return str(value) if value not in (None, "") else "待确认"


def build_report(payload: dict[str, Any]) -> str:
    lines = [
        "# 市场情绪雷达",
        "",
        f"- 生成时间：{payload.get('generated_label')} Asia/Shanghai",
        "- 版本：公开脱敏版",
        f"- 数据边界：{payload.get('data_boundary')}",
        "",
        "## 重点先看",
        "",
        "| 项目 | 结论 |",
        "|---|---|",
        f"| 情绪状态 | {payload.get('status_label') or '待确认'} |",
        f"| 综合情绪分 | {fmt_score(payload.get('score'))}/100 |",
        f"| 当前含义 | {payload.get('stance') or '待确认'} |",
        f"| 摘要 | {payload.get('summary') or '待确认'} |",
        "",
        "## 情绪分解",
        "",
        "| 模块 | 分数 | 状态 | 数据 | 来源 | 说明 |",
        "|---|---:|---|---|---|---|",
    ]
    for item in payload.get("components", []) if isinstance(payload.get("components"), list) else []:
        if not isinstance(item, dict):
            continue
        lines.append(
            "| {name} | {score} | {status} | {value} | {source} | {message} |".format(
                name=item.get("name") or "",
                score=fmt_score(item.get("score")),
                status=item.get("status") or "unknown",
                value=fmt_value(item.get("value")),
                source=item.get("source") or "",
                message=item.get("message") or "",
            )
        )
    lines.extend(["", "## 触发规则", ""])
    for rule in payload.get("rules_triggered", []) if isinstance(payload.get("rules_triggered"), list) else []:
        lines.append(f"- {rule}")
    gaps = payload.get("data_gaps") if isinstance(payload.get("data_gaps"), list) else []
    lines.extend(["", "## 数据缺口", ""])
    if gaps:
        for gap in gaps:
            if not isinstance(gap, dict):
                continue
            lines.append(f"- {gap.get('field') or 'unknown'}：{gap.get('message') or ''}；影响：{gap.get('impact') or '待确认'}")
    else:
        lines.append("- 暂无关键数据缺口。")
    lines.extend(
        [
            "",
            "## 使用纪律",
            "",
            "- 情绪分只决定“今天更适合进攻、平衡还是防守”，不能替代单股 Buy-Side 分析。",
            "- 个股仍必须满足完整入场价、止损价、目标价和 R/R ≥ 2:1。",
            "- 当情绪 Risk-off 时，降低试错频率；当情绪 Risk-on 时，也不得追高违反 R/R 纪律。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-pack", type=Path, default=DEFAULT_MARKET_PACK)
    parser.add_argument("--macro-regime", type=Path, default=DEFAULT_MACRO_REGIME)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--docs-output", type=Path, default=DEFAULT_DOCS_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    market_pack = load_json(args.market_pack, {})
    macro_regime = load_json(args.macro_regime, {})
    payload = build_sentiment(market_pack if isinstance(market_pack, dict) else {}, macro_regime if isinstance(macro_regime, dict) else {})

    write_json(args.output, payload)
    write_json(args.docs_output, payload)
    write_text(args.report, build_report(payload))
    print(f"Wrote {args.output}")
    print(f"Wrote {args.docs_output}")
    print(f"Wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
