#!/usr/bin/env python3
"""Collect FRED macro data and build a market-regime signal.

The output is a research context layer for the stock system. It does not create
trade orders or override Buy-Side stock-level risk/reward discipline.
"""

from __future__ import annotations

import argparse
import json
import math
import os
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

DEFAULT_CONFIG = CONFIG_DIR / "macro_fred_series.json"
DEFAULT_OUTPUT = DATA_DIR / "latest_macro_regime.json"
DEFAULT_DOCS_OUTPUT = DOCS_DATA_DIR / "macro_regime.json"
DEFAULT_REPORT = REPORTS_DIR / "latest-macro-regime.md"
FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
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
        cleaned = value.replace(",", "").strip()
        if not cleaned or cleaned in {"."} or cleaned.lower() in {"nan", "none", "null", "n/a"}:
            return None
        try:
            parsed = float(cleaned)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))


def fetch_fred_series(api_key: str, series_id: str, *, limit: int = 520) -> list[dict[str, Any]]:
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": str(limit),
    }
    url = FRED_OBSERVATIONS_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "ffxdz-ai-us-stock-research-dashboard/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    observations = payload.get("observations")
    if not isinstance(observations, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in observations:
        if not isinstance(item, dict):
            continue
        value = number(item.get("value"))
        date = str(item.get("date") or "")
        if value is None or not date:
            continue
        rows.append({"date": date, "value": value})
    rows.sort(key=lambda row: row["date"])
    return rows


def parse_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def value_on_or_before(rows: list[dict[str, Any]], target: datetime) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    for row in rows:
        parsed = parse_date(str(row.get("date") or ""))
        if parsed is None:
            continue
        if parsed <= target:
            selected = row
        else:
            break
    return selected


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return current / previous - 1


def annualized_change(current: float | None, previous: float | None, periods_per_year: float) -> float | None:
    change = pct_change(current, previous)
    if change is None:
        return None
    base = 1 + change
    if base <= 0:
        return None
    return base**periods_per_year - 1


def summarize_series(series: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "id": series.get("id"),
            "name": series.get("name"),
            "dimension": series.get("dimension"),
            "unit": series.get("unit"),
            "status": "missing",
        }
    latest = rows[-1]
    latest_date = parse_date(latest["date"])
    latest_value = number(latest.get("value"))
    if latest_date is None or latest_value is None:
        return {
            "id": series.get("id"),
            "name": series.get("name"),
            "dimension": series.get("dimension"),
            "unit": series.get("unit"),
            "status": "missing",
        }
    one_month = value_on_or_before(rows, latest_date - timedelta(days=32))
    three_month = value_on_or_before(rows, latest_date - timedelta(days=92))
    six_month = value_on_or_before(rows, latest_date - timedelta(days=183))
    one_year = value_on_or_before(rows, latest_date - timedelta(days=366))
    one_month_value = number(one_month.get("value")) if one_month else None
    three_month_value = number(three_month.get("value")) if three_month else None
    six_month_value = number(six_month.get("value")) if six_month else None
    one_year_value = number(one_year.get("value")) if one_year else None
    return {
        "id": series.get("id"),
        "name": series.get("name"),
        "dimension": series.get("dimension"),
        "unit": series.get("unit"),
        "frequency_hint": series.get("frequency_hint"),
        "latest_date": latest["date"],
        "latest_value": round(latest_value, 4),
        "one_month_ago_value": round(one_month_value, 4) if one_month_value is not None else None,
        "three_month_ago_value": round(three_month_value, 4) if three_month_value is not None else None,
        "six_month_ago_value": round(six_month_value, 4) if six_month_value is not None else None,
        "one_year_ago_value": round(one_year_value, 4) if one_year_value is not None else None,
        "one_month_change": round(latest_value - one_month_value, 4) if one_month_value is not None else None,
        "three_month_change": round(latest_value - three_month_value, 4) if three_month_value is not None else None,
        "six_month_change": round(latest_value - six_month_value, 4) if six_month_value is not None else None,
        "one_year_change": round(latest_value - one_year_value, 4) if one_year_value is not None else None,
        "one_month_pct_change": round((pct_change(latest_value, one_month_value) or 0) * 100, 4) if one_month_value else None,
        "three_month_pct_change": round((pct_change(latest_value, three_month_value) or 0) * 100, 4) if three_month_value else None,
        "six_month_pct_change": round((pct_change(latest_value, six_month_value) or 0) * 100, 4) if six_month_value else None,
        "yoy_pct_change": round((pct_change(latest_value, one_year_value) or 0) * 100, 4) if one_year_value else None,
        "three_month_annualized_pct": round((annualized_change(latest_value, three_month_value, 4) or 0) * 100, 4)
        if three_month_value
        else None,
        "status": "ok",
    }


def metric(indicators: dict[str, dict[str, Any]], series_id: str, field: str = "latest_value") -> float | None:
    item = indicators.get(series_id)
    if not isinstance(item, dict):
        return None
    return number(item.get(field))


def add_rule(scores: list[dict[str, Any]], dimension: str, label: str, contribution: float, detail: str) -> None:
    scores.append(
        {
            "dimension": dimension,
            "label": label,
            "contribution": round(contribution, 2),
            "detail": detail,
        }
    )


def score_macro(indicators: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rules: list[dict[str, Any]] = []

    growth = 50.0
    gdp_yoy = metric(indicators, "GDPC1", "yoy_pct_change")
    if gdp_yoy is not None:
        if gdp_yoy >= 2.2:
            growth += 12
            add_rule(rules, "growth", "实际 GDP 同比强", 12, f"{gdp_yoy:.2f}%")
        elif gdp_yoy >= 1:
            growth += 5
            add_rule(rules, "growth", "实际 GDP 温和增长", 5, f"{gdp_yoy:.2f}%")
        elif gdp_yoy < 0:
            growth -= 12
            add_rule(rules, "growth", "实际 GDP 同比转负", -12, f"{gdp_yoy:.2f}%")

    indpro_yoy = metric(indicators, "INDPRO", "yoy_pct_change")
    if indpro_yoy is not None:
        if indpro_yoy >= 2:
            growth += 8
            add_rule(rules, "growth", "工业生产扩张", 8, f"{indpro_yoy:.2f}%")
        elif indpro_yoy < 0:
            growth -= 8
            add_rule(rules, "growth", "工业生产同比收缩", -8, f"{indpro_yoy:.2f}%")

    payroll_3m = metric(indicators, "PAYEMS", "three_month_change")
    if payroll_3m is not None:
        avg_monthly = payroll_3m / 3.0
        if avg_monthly >= 150:
            growth += 10
            add_rule(rules, "growth", "非农就业强", 10, f"3个月平均 {avg_monthly:.0f} 千人/月")
        elif avg_monthly >= 50:
            growth += 5
            add_rule(rules, "growth", "非农就业仍扩张", 5, f"3个月平均 {avg_monthly:.0f} 千人/月")
        elif avg_monthly < 0:
            growth -= 12
            add_rule(rules, "growth", "非农就业转弱", -12, f"3个月平均 {avg_monthly:.0f} 千人/月")

    unemployment_3m = metric(indicators, "UNRATE", "three_month_change")
    if unemployment_3m is not None:
        if unemployment_3m <= 0:
            growth += 8
            add_rule(rules, "growth", "失业率未恶化", 8, f"3个月变化 {unemployment_3m:.2f}pct")
        elif unemployment_3m >= 0.5:
            growth -= 15
            add_rule(rules, "growth", "失业率快速上行", -15, f"3个月变化 {unemployment_3m:.2f}pct")
        elif unemployment_3m >= 0.3:
            growth -= 8
            add_rule(rules, "growth", "失业率边际上行", -8, f"3个月变化 {unemployment_3m:.2f}pct")

    inflation = 50.0
    for series_id, label in [("CPILFESL", "核心 CPI"), ("PCEPILFE", "核心 PCE")]:
        yoy = metric(indicators, series_id, "yoy_pct_change")
        annualized = metric(indicators, series_id, "three_month_annualized_pct")
        if yoy is not None:
            if yoy <= 3:
                inflation += 8
                add_rule(rules, "inflation", f"{label} 同比接近目标区间", 8, f"{yoy:.2f}%")
            elif yoy <= 4:
                inflation += 3
                add_rule(rules, "inflation", f"{label} 同比温和偏高", 3, f"{yoy:.2f}%")
            else:
                inflation -= 9
                add_rule(rules, "inflation", f"{label} 同比偏高", -9, f"{yoy:.2f}%")
        if annualized is not None:
            if annualized <= 3:
                inflation += 6
                add_rule(rules, "inflation", f"{label} 3个月年化降温", 6, f"{annualized:.2f}%")
            elif annualized >= 4.2:
                inflation -= 7
                add_rule(rules, "inflation", f"{label} 3个月年化偏热", -7, f"{annualized:.2f}%")

    policy = 50.0
    fedfunds = metric(indicators, "FEDFUNDS")
    if fedfunds is not None:
        if fedfunds <= 3.5:
            policy += 7
            add_rule(rules, "policy", "政策利率压力较低", 7, f"{fedfunds:.2f}%")
        elif fedfunds >= 5:
            policy -= 8
            add_rule(rules, "policy", "政策利率仍具约束性", -8, f"{fedfunds:.2f}%")
    ten_year_3m = metric(indicators, "DGS10", "three_month_change")
    if ten_year_3m is not None:
        if ten_year_3m <= -0.35:
            policy += 7
            add_rule(rules, "policy", "长端利率下行", 7, f"3个月 {ten_year_3m:.2f}pct")
        elif ten_year_3m >= 0.5:
            policy -= 9
            add_rule(rules, "policy", "长端利率上行压估值", -9, f"3个月 {ten_year_3m:.2f}pct")
    curve_10y3m = metric(indicators, "T10Y3M")
    curve_10y2y = metric(indicators, "T10Y2Y")
    curve = curve_10y3m if curve_10y3m is not None else curve_10y2y
    if curve is not None:
        if curve >= 0.5:
            policy += 7
            add_rule(rules, "policy", "收益率曲线正斜率", 7, f"{curve:.2f}pct")
        elif curve <= -0.5:
            policy -= 10
            add_rule(rules, "policy", "收益率曲线倒挂", -10, f"{curve:.2f}pct")

    liquidity = 50.0
    m2_yoy = metric(indicators, "M2SL", "yoy_pct_change")
    if m2_yoy is not None:
        if m2_yoy >= 2:
            liquidity += 8
            add_rule(rules, "liquidity", "M2 同比扩张", 8, f"{m2_yoy:.2f}%")
        elif m2_yoy < 0:
            liquidity -= 8
            add_rule(rules, "liquidity", "M2 同比收缩", -8, f"{m2_yoy:.2f}%")
    walcl_3m = metric(indicators, "WALCL", "three_month_pct_change")
    if walcl_3m is not None:
        if walcl_3m >= 1:
            liquidity += 6
            add_rule(rules, "liquidity", "联储资产负债表扩张", 6, f"3个月 {walcl_3m:.2f}%")
        elif walcl_3m <= -1:
            liquidity -= 5
            add_rule(rules, "liquidity", "联储资产负债表收缩", -5, f"3个月 {walcl_3m:.2f}%")
    nfci = metric(indicators, "NFCI")
    if nfci is not None:
        if nfci <= -0.25:
            liquidity += 10
            add_rule(rules, "liquidity", "金融条件宽松", 10, f"NFCI {nfci:.2f}")
        elif nfci >= 0.25:
            liquidity -= 10
            add_rule(rules, "liquidity", "金融条件收紧", -10, f"NFCI {nfci:.2f}")
    credit_spread = metric(indicators, "BAA10Y")
    if credit_spread is not None:
        if credit_spread <= 2.2:
            liquidity += 7
            add_rule(rules, "liquidity", "信用利差低", 7, f"{credit_spread:.2f}pct")
        elif credit_spread >= 3.2:
            liquidity -= 10
            add_rule(rules, "liquidity", "信用利差走阔", -10, f"{credit_spread:.2f}pct")

    risk = 50.0
    vix = metric(indicators, "VIXCLS")
    if vix is not None:
        if vix <= 18:
            risk += 9
            add_rule(rules, "risk", "VIX 低位", 9, f"{vix:.2f}")
        elif vix >= 25:
            risk -= 12
            add_rule(rules, "risk", "VIX 高位", -12, f"{vix:.2f}")
    dollar_3m = metric(indicators, "DTWEXBGS", "three_month_pct_change")
    if dollar_3m is not None:
        if dollar_3m <= -2:
            risk += 4
            add_rule(rules, "risk", "美元走弱利好风险资产", 4, f"3个月 {dollar_3m:.2f}%")
        elif dollar_3m >= 3:
            risk -= 5
            add_rule(rules, "risk", "美元走强压制风险资产", -5, f"3个月 {dollar_3m:.2f}%")
    oil_3m = metric(indicators, "DCOILWTICO", "three_month_pct_change")
    if oil_3m is not None:
        if oil_3m >= 15:
            risk -= 5
            add_rule(rules, "risk", "油价快速上涨带来通胀风险", -5, f"3个月 {oil_3m:.2f}%")
        elif oil_3m <= -15:
            risk += 3
            add_rule(rules, "risk", "油价回落缓解通胀压力", 3, f"3个月 {oil_3m:.2f}%")

    dimensions = {
        "growth": round(clamp(growth), 1),
        "inflation": round(clamp(inflation), 1),
        "policy_rates": round(clamp(policy), 1),
        "liquidity": round(clamp(liquidity), 1),
        "risk_appetite": round(clamp(risk), 1),
    }
    composite = (
        dimensions["growth"] * 0.25
        + dimensions["inflation"] * 0.2
        + dimensions["policy_rates"] * 0.2
        + dimensions["liquidity"] * 0.2
        + dimensions["risk_appetite"] * 0.15
    )
    dimensions["composite"] = round(clamp(composite), 1)
    return dimensions, rules


def regime_label(composite: float) -> tuple[str, str]:
    if composite >= 70:
        return "风险偏好强，适合选择性进攻", "offensive"
    if composite >= 58:
        return "中性偏进攻，重点看结构性机会", "selective_offense"
    if composite >= 45:
        return "中性震荡，控制仓位并等待确认", "balanced"
    return "偏防守，降低追高和高估值暴露", "defensive"


def scenario_probabilities(composite: float) -> dict[str, int]:
    if composite >= 70:
        return {"base": 55, "bull": 30, "bear": 15}
    if composite >= 58:
        return {"base": 60, "bull": 25, "bear": 15}
    if composite >= 45:
        return {"base": 55, "bull": 18, "bear": 27}
    return {"base": 45, "bull": 15, "bear": 40}


def macro_implications(dimensions: dict[str, Any]) -> list[str]:
    composite = number(dimensions.get("composite")) or 50
    growth = number(dimensions.get("growth")) or 50
    liquidity = number(dimensions.get("liquidity")) or 50
    policy = number(dimensions.get("policy_rates")) or 50
    inflation = number(dimensions.get("inflation")) or 50
    notes: list[str] = []
    if composite >= 58 and liquidity >= 55:
        notes.append("风险资产可以维持结构性进攻，但必须避开估值和技术过热。")
    elif composite < 45:
        notes.append("宏观环境偏防守，优先保留现金和等待高确定性回调。")
    else:
        notes.append("宏观环境不支持无差别进攻，更适合主题内精选和分批确认。")
    if policy < 45:
        notes.append("利率压力仍在，长久期成长股需要更高的风险收益比。")
    if inflation < 45:
        notes.append("通胀仍偏粘，市场对降息或估值扩张的预期需要打折。")
    if growth >= 60:
        notes.append("增长尚可，盈利兑现优先于纯估值修复。")
    if liquidity >= 60:
        notes.append("流动性环境相对友好，可提高对突破确认机会的关注。")
    return notes


def build_regime(config: dict[str, Any], api_key: str | None) -> dict[str, Any]:
    now = now_local()
    if not api_key:
        return {
            "schema_version": 1,
            "generated_at": iso(now),
            "generated_label": now.strftime("%Y-%m-%d %H:%M"),
            "fred_enabled": False,
            "error": "FRED_API_KEY is not configured.",
            "data_boundary": "宏观层缺少 FRED API Key；DeepSeek 报告不得声称已使用 FRED 最新数据。",
            "indicators": {},
            "dimensions": {},
            "regime": {"label": "宏观数据不足", "risk_posture": "unknown"},
        }

    indicators: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    for series in config.get("series", []) if isinstance(config.get("series"), list) else []:
        if not isinstance(series, dict) or not series.get("id"):
            continue
        series_id = str(series["id"])
        try:
            rows = fetch_fred_series(api_key, series_id)
            indicators[series_id] = summarize_series(series, rows)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            indicators[series_id] = {
                "id": series_id,
                "name": series.get("name"),
                "dimension": series.get("dimension"),
                "status": "error",
            }
            errors.append({"series_id": series_id, "error": str(exc)[:220]})

    dimensions, rules = score_macro(indicators)
    composite = number(dimensions.get("composite")) or 0
    label, posture = regime_label(composite)
    probabilities = scenario_probabilities(composite)
    freshest_dates = [
        str(item.get("latest_date"))
        for item in indicators.values()
        if isinstance(item, dict) and item.get("latest_date")
    ]
    oldest_dates = freshest_dates[:]
    return {
        "schema_version": 1,
        "generated_at": iso(now),
        "generated_label": now.strftime("%Y-%m-%d %H:%M"),
        "fred_enabled": True,
        "data_boundary": "FRED 官方宏观数据层；只用于市场环境和风险偏好判断，不直接生成股票买卖指令。",
        "data_sources": [
            "FRED series observations API",
            "config/macro_fred_series.json",
        ],
        "as_of": {
            "freshest_observation_date": max(freshest_dates) if freshest_dates else None,
            "oldest_latest_observation_date": min(oldest_dates) if oldest_dates else None,
            "note": "不同宏观指标发布频率不同，日报必须引用各指标自己的 latest_date。",
        },
        "indicators": indicators,
        "dimensions": dimensions,
        "scoring_rules_triggered": rules,
        "regime": {
            "label": label,
            "risk_posture": posture,
            "base_bull_bear_probabilities": probabilities,
            "summary": macro_implications(dimensions),
        },
        "watchlist": [
            "核心 CPI / 核心 PCE 的 3个月年化是否继续降温",
            "失业率和非农就业是否出现连续恶化",
            "10年-3个月、10年-2年收益率曲线是否重新陡峭化",
            "NFCI 和信用利差是否同步收紧",
            "VIX、美元和油价是否同时对风险资产形成压力",
        ],
        "errors": errors,
    }


def fmt_num(value: Any, digits: int = 1) -> str:
    parsed = number(value)
    return "数据不足" if parsed is None else f"{parsed:.{digits}f}"


def fmt_pct(value: Any, digits: int = 1) -> str:
    parsed = number(value)
    return "数据不足" if parsed is None else f"{parsed:.{digits}f}%"


LEVEL_CHANGE_SERIES = {
    "UNRATE",
    "FEDFUNDS",
    "DGS10",
    "DGS2",
    "DGS3MO",
    "T10Y2Y",
    "T10Y3M",
    "NFCI",
    "BAA10Y",
    "VIXCLS",
}


def yearly_change_display(series_id: str, item: dict[str, Any]) -> str:
    if series_id in LEVEL_CHANGE_SERIES:
        return fmt_num(item.get("one_year_change"), 2)
    return fmt_pct(item.get("yoy_pct_change"), 2)


def three_month_change_display(series_id: str, item: dict[str, Any]) -> str:
    if series_id in LEVEL_CHANGE_SERIES:
        return fmt_num(item.get("three_month_change"), 2)
    return fmt_pct(item.get("three_month_pct_change"), 2)


def three_month_annualized_display(series_id: str, item: dict[str, Any]) -> str:
    if series_id in LEVEL_CHANGE_SERIES:
        return "—"
    return fmt_pct(item.get("three_month_annualized_pct"), 2)


def render_report(payload: dict[str, Any]) -> str:
    lines: list[str] = [
        "# FRED 宏观环境雷达",
        "",
        f"- 生成时间：{payload.get('generated_label')}",
        "- 定位：判断宏观环境、流动性和风险偏好；不直接生成股票买卖指令。",
        "- 执行纪律：宏观只决定进攻/防守强度，个股买入仍需 Buy-Side、R/R >= 2:1 和整股约束。",
        "",
    ]
    if not payload.get("fred_enabled"):
        lines.extend(["## 数据状态", "", str(payload.get("error") or "FRED 数据未启用。"), ""])
        return "\n".join(lines)

    regime = payload.get("regime") if isinstance(payload.get("regime"), dict) else {}
    dimensions = payload.get("dimensions") if isinstance(payload.get("dimensions"), dict) else {}
    probs = regime.get("base_bull_bear_probabilities") if isinstance(regime.get("base_bull_bear_probabilities"), dict) else {}
    lines.extend(
        [
            "## 核心结论",
            "",
            f"- 宏观状态：{regime.get('label')}。",
            f"- Composite：{fmt_num(dimensions.get('composite'))}/100。",
            f"- Base/Bull/Bear：{probs.get('base', 'n/a')}% / {probs.get('bull', 'n/a')}% / {probs.get('bear', 'n/a')}%。",
            f"- 数据日期：最新观测 {payload.get('as_of', {}).get('freshest_observation_date')}；最慢发布指标 {payload.get('as_of', {}).get('oldest_latest_observation_date')}。",
            "",
        ]
    )
    for item in regime.get("summary", []) if isinstance(regime.get("summary"), list) else []:
        lines.append(f"- {item}")

    lines.extend(["", "## 五维评分", ""])
    lines.extend(["| 维度 | 分数 | 解读 |", "|---|---:|---|"])
    labels = {
        "growth": "经济周期/增长",
        "inflation": "通胀压力",
        "policy_rates": "政策与利率",
        "liquidity": "流动性/信用",
        "risk_appetite": "市场风险偏好",
        "composite": "综合宏观环境",
    }
    for key, label in labels.items():
        lines.append(f"| {label} | {fmt_num(dimensions.get(key))} | {'越高越有利于风险资产' if key != 'composite' else '综合进攻/防守信号'} |")

    indicators = payload.get("indicators") if isinstance(payload.get("indicators"), dict) else {}
    important = [
        "GDPC1",
        "PAYEMS",
        "UNRATE",
        "CPILFESL",
        "PCEPILFE",
        "FEDFUNDS",
        "DGS10",
        "T10Y3M",
        "M2SL",
        "NFCI",
        "BAA10Y",
        "VIXCLS",
    ]
    lines.extend(["", "## 关键 FRED 指标", ""])
    lines.extend(["| 指标 | 最新日期 | 最新值 | 同比/1年变化 | 3个月变化 | 3个月年化 |", "|---|---|---:|---:|---:|---:|"])
    for series_id in important:
        item = indicators.get(series_id) if isinstance(indicators.get(series_id), dict) else {}
        lines.append(
            f"| {item.get('name') or series_id} | {item.get('latest_date', 'n/a')} | {fmt_num(item.get('latest_value'), 2)} | {yearly_change_display(series_id, item)} | {three_month_change_display(series_id, item)} | {three_month_annualized_display(series_id, item)} |"
        )

    rules = payload.get("scoring_rules_triggered") if isinstance(payload.get("scoring_rules_triggered"), list) else []
    lines.extend(["", "## 触发的宏观信号", ""])
    if not rules:
        lines.append("本轮没有足够规则触发，需检查 FRED 数据完整性。")
    else:
        lines.extend(["| 维度 | 信号 | 分值影响 | 细节 |", "|---|---|---:|---|"])
        for item in rules:
            lines.append(f"| {item.get('dimension')} | {item.get('label')} | {fmt_num(item.get('contribution'))} | {item.get('detail')} |")

    lines.extend(["", "## 下一步观察", ""])
    for item in payload.get("watchlist", []):
        lines.append(f"- {item}")
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    if errors:
        lines.extend(["", "## 数据异常", ""])
        for item in errors[:10]:
            lines.append(f"- {item.get('series_id')}: {item.get('error')}")
    return "\n".join(lines).strip() + "\n"


def archive_copy(report_path: Path) -> Path:
    timestamp = now_local().strftime("%Y%m%d-%H%M")
    archive = report_path.with_name(f"macro-regime-{timestamp}.md")
    archive.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--docs-out", type=Path, default=DEFAULT_DOCS_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--no-archive-copy", action="store_true")
    args = parser.parse_args()

    load_environment()
    config = load_json(args.config, {})
    if not config:
        raise SystemExit(f"Macro FRED config not found or invalid: {args.config}")
    # FRED validates API keys as lower-case alpha-numeric strings. Normalize
    # user-entered uppercase keys without exposing or rewriting the secret.
    payload = build_regime(config, os.getenv("FRED_API_KEY", "").strip().lower() or None)
    write_json(args.out, payload)
    write_json(args.docs_out, payload)
    write_text(args.report, render_report(payload))
    if not args.no_archive_copy:
        archive = archive_copy(args.report)
        print(f"Wrote {archive}")
    print(f"Wrote {args.out}")
    print(f"Wrote {args.docs_out}")
    print(f"Wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
