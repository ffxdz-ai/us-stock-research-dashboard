#!/usr/bin/env python3
"""Generate a cross-market supply-chain demand radar report.

The radar is intentionally upstream of trading. It identifies industry-chain
segments and securities that deserve Buy-Side follow-up, but it does not create
orders or bypass the existing entry-path/risk-reward discipline.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
DEFAULT_MAP_PATH = CONFIG_DIR / "supply_chain_map.json"
DEFAULT_MARKET_PACK = DATA_DIR / "latest_market_pack.json"
DEFAULT_OUTPUT = DATA_DIR / "latest_supply_chain_radar.json"
DEFAULT_REPORT = REPORTS_DIR / "latest-supply-chain-radar.md"


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


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace("%", "").replace(",", "").strip()
        if not cleaned or cleaned.lower() in {"n/a", "nan", "none", "--", "null"}:
            return None
        try:
            parsed = float(cleaned)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys([item for item in items if item]))


def code_market(code: str) -> str:
    upper = code.upper()
    if upper.startswith("US."):
        return "US"
    if upper.startswith("HK."):
        return "HK"
    if upper.startswith(("SH.", "SZ.")):
        return "CN"
    if "." not in upper:
        return "US"
    return upper.split(".", 1)[0]


def us_symbol(code: str) -> str | None:
    upper = code.upper()
    if upper.startswith("US."):
        return upper.split(".", 1)[1]
    if "." not in upper:
        return upper
    return None


def display_code(code: str) -> str:
    upper = code.upper()
    return upper if "." in upper else f"US.{upper}"


def compact_code(code: str) -> str:
    upper = code.upper()
    return upper.split(".", 1)[1] if upper.startswith("US.") else upper


def candidate_index(market_pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for item in market_pack.get("candidates", []) or []:
        ticker = str(item.get("ticker") or "").upper()
        if ticker:
            output[ticker] = item
            output[f"US.{ticker}"] = item
    return output


def collect_extra_us_data(symbols: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]:
    """Collect public quote/chart data for US symbols not present in market pack."""
    if not symbols:
        return {}, {}, "no extra US symbols"
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import collect_market_data as cmd  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {}, {}, f"collect_market_data unavailable: {exc}"

    try:
        quotes = cmd.collect_nasdaq_quotes(symbols)
    except Exception as exc:  # noqa: BLE001
        quotes = {symbol: {"symbol": symbol, "error": str(exc)} for symbol in symbols}
    try:
        charts, _stats = cmd.collect_charts_cached(symbols, max_age_hours=20.0)
    except Exception as exc:  # noqa: BLE001
        charts = {symbol: {"symbol": symbol, "error": str(exc)} for symbol in symbols}
    return quotes, charts, "public US quote/chart fallback"


def collect_futu_snapshots(codes: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
    """Best-effort Futu snapshot collection for HK/A/US codes."""
    if not codes or os.getenv("FUTU_QUOTE_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
        return {}, "Futu disabled or no cross-market codes"
    try:
        from futu import OpenQuoteContext, RET_OK  # type: ignore
    except Exception:  # noqa: BLE001
        return {}, "Futu/OpenD unavailable in current runtime; HK/A股行情需本地补全"

    host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.getenv("FUTU_OPEND_PORT", "11111").strip() or "11111")
    except ValueError:
        port = 11111

    ctx = None
    output: dict[str, dict[str, Any]] = {}
    try:
        ctx = OpenQuoteContext(host=host, port=port, is_async_connect=False)
        for start in range(0, len(codes), 200):
            batch = codes[start : start + 200]
            ret, data = ctx.get_market_snapshot(batch)
            if ret != RET_OK or data is None:
                continue
            records = data.to_dict("records") if hasattr(data, "to_dict") else []
            for row in records:
                code = str(row.get("code") or "").upper()
                if code:
                    output[code] = row
    except Exception as exc:  # noqa: BLE001
        return output, f"Futu snapshot error: {exc}"
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
    return output, "Futu OpenD market snapshot"


def yahoo_symbol_for_code(code: str) -> str | None:
    upper = display_code(code)
    if upper.startswith("HK."):
        raw = upper.split(".", 1)[1]
        return f"{raw.lstrip('0').zfill(4)}.HK"
    if upper.startswith("SH."):
        return f"{upper.split('.', 1)[1]}.SS"
    if upper.startswith("SZ."):
        return f"{upper.split('.', 1)[1]}.SZ"
    return None


def collect_public_cross_market_data(codes: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]:
    """Collect disclosed public fallback data for HK/A shares when Futu is unavailable."""
    yahoo_map = {symbol: display_code(code) for code in codes if (symbol := yahoo_symbol_for_code(code))}
    if not yahoo_map:
        return {}, {}, "no HK/A public fallback symbols"
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import collect_market_data as cmd  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {}, {}, f"HK/A public fallback unavailable: {exc}"

    symbols = list(yahoo_map.keys())
    try:
        raw_quotes = cmd.yahoo_quotes(symbols)
    except Exception:  # noqa: BLE001
        raw_quotes = {}

    raw_charts: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        try:
            raw_charts[symbol] = cmd.yahoo_chart(symbol)
        except Exception as exc:  # noqa: BLE001
            raw_charts[symbol] = {"symbol": symbol, "error": str(exc)}

    quotes: dict[str, dict[str, Any]] = {}
    charts: dict[str, dict[str, Any]] = {}
    for yahoo_symbol, code in yahoo_map.items():
        quote = dict(raw_quotes.get(yahoo_symbol, {}) or {})
        chart = dict(raw_charts.get(yahoo_symbol, {}) or {})
        if not number(quote.get("regularMarketPrice")) and number(chart.get("last_close")) is not None:
            quote["regularMarketPrice"] = chart.get("last_close")
            quote["regularMarketTime"] = chart.get("chart_time")
            quote["source"] = "Yahoo chart API cross-market fallback"
        quote["yahoo_symbol"] = yahoo_symbol
        chart["yahoo_symbol"] = yahoo_symbol
        quotes[code.upper()] = quote
        charts[code.upper()] = chart
    return quotes, charts, "Yahoo public HK/A chart fallback; Futu preferred when connected"


def quote_chart_from_market_pack(code: str, market_pack: dict[str, Any], indexed: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    symbol = us_symbol(code)
    if not symbol:
        return {}, {}
    market_quote = (market_pack.get("market") or {}).get(symbol) or {}
    candidate = indexed.get(symbol) or indexed.get(f"US.{symbol}") or {}
    quote = {
        "symbol": symbol,
        "regularMarketPrice": candidate.get("price") or market_quote.get("regularMarketPrice"),
        "regularMarketChangePercent": market_quote.get("regularMarketChangePercent"),
        "regularMarketTime": candidate.get("quote_time") or market_quote.get("regularMarketTime"),
        "source": candidate.get("quote_source") or market_quote.get("source"),
        "shortName": candidate.get("name"),
    }
    chart = candidate.get("chart") if isinstance(candidate.get("chart"), dict) else {}
    return quote, chart


def price_from_sources(
    code: str,
    market_pack: dict[str, Any],
    indexed: dict[str, dict[str, Any]],
    extra_quotes: dict[str, dict[str, Any]],
    extra_charts: dict[str, dict[str, Any]],
    futu_quotes: dict[str, dict[str, Any]],
    public_cross_quotes: dict[str, dict[str, Any]],
    public_cross_charts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    market = code_market(code)
    display = display_code(code)
    symbol = us_symbol(code)

    quote, chart = quote_chart_from_market_pack(code, market_pack, indexed)
    if symbol:
        quote = {**extra_quotes.get(symbol, {}), **{k: v for k, v in quote.items() if v is not None}}
        chart = {**extra_charts.get(symbol, {}), **chart}

    public_quote = public_cross_quotes.get(display.upper()) or {}
    public_chart = public_cross_charts.get(display.upper()) or {}
    if public_quote or public_chart:
        quote = {
            **quote,
            "regularMarketPrice": number(public_quote.get("regularMarketPrice") or public_chart.get("last_close")),
            "regularMarketChangePercent": number(public_quote.get("regularMarketChangePercent")),
            "regularMarketTime": public_quote.get("regularMarketTime") or public_chart.get("chart_time"),
            "source": public_quote.get("source") or public_chart.get("source"),
            "shortName": public_quote.get("shortName") or quote.get("shortName"),
            "yahoo_symbol": public_quote.get("yahoo_symbol") or public_chart.get("yahoo_symbol"),
        }
        chart = {**chart, **public_chart}

    futu_row = futu_quotes.get(display.upper()) or futu_quotes.get(code.upper()) or {}
    if futu_row:
        quote = {
            **quote,
            "regularMarketPrice": number(futu_row.get("last_price") or futu_row.get("price") or futu_row.get("nominal_price")),
            "regularMarketChangePercent": number(futu_row.get("change_rate") or futu_row.get("change_pct")),
            "regularMarketTime": futu_row.get("update_time"),
            "source": "Futu OpenD market snapshot",
            "shortName": futu_row.get("name") or quote.get("shortName"),
        }

    price = number(quote.get("regularMarketPrice") or chart.get("last_close"))
    ma50 = number(chart.get("ma50"))
    ma200 = number(chart.get("ma200"))
    high252 = number(chart.get("high252"))
    change_pct = number(quote.get("regularMarketChangePercent"))
    market_score, market_notes = market_confirmation_score(price, ma50, ma200, high252, change_pct)
    if price:
        if market == "US":
            data_status = "行情已接入"
        elif futu_row:
            data_status = "Futu/OpenD 行情已接入"
        else:
            data_status = "公开日线行情已接入；需 Futu/券商复核"
    else:
        data_status = "美股公开行情缺失" if market == "US" else "需本地 Futu/OpenD 补全行情"
    return {
        "code": display,
        "market": market,
        "symbol": symbol,
        "price": price,
        "ma50": ma50,
        "ma200": ma200,
        "high252": high252,
        "change_pct": change_pct,
        "quote_time": quote.get("regularMarketTime"),
        "quote_source": quote.get("source") or chart.get("source"),
        "market_confirmation_score": market_score,
        "market_notes": market_notes,
        "data_status": data_status,
    }


def market_confirmation_score(
    price: float | None,
    ma50: float | None,
    ma200: float | None,
    high252: float | None,
    change_pct: float | None,
) -> tuple[float | None, list[str]]:
    if price is None:
        return None, ["缺少价格，不能做趋势确认"]
    score = 40.0
    notes: list[str] = []
    if ma50:
        if price >= ma50:
            score += 18
            notes.append("站上 MA50")
        else:
            score -= 8
            notes.append("低于 MA50")
    if ma200:
        if price >= ma200:
            score += 18
            notes.append("站上 MA200")
        else:
            score -= 12
            notes.append("低于 MA200")
    if high252:
        distance = price / high252 - 1
        if distance >= -0.08:
            score += 12
            notes.append("接近 52 周高位，趋势强")
        elif distance <= -0.30:
            score -= 8
            notes.append("距离 52 周高位较远")
    if change_pct is not None:
        if change_pct > 0:
            score += min(6, change_pct)
        elif change_pct < -3:
            score -= 4
    return round(max(0, min(100, score)), 1), notes


def weighted_layer_score(layer: dict[str, Any], market_score: float | None, weights: dict[str, float]) -> float:
    confirmation = market_score if market_score is not None else 45.0
    raw = (
        float(layer.get("structural_score", 50)) * weights.get("downstream_demand", 0.3)
        + float(layer.get("supply_constraint_score", 50)) * weights.get("supply_constraint", 0.2)
        + float(layer.get("earnings_translation_score", 50)) * weights.get("earnings_translation", 0.15)
        + float(layer.get("margin_leverage_score", 50)) * weights.get("margin_leverage", 0.15)
        + confirmation * weights.get("market_confirmation", 0.1)
        + float(layer.get("valuation_gap_score", 50)) * weights.get("valuation_gap", 0.1)
    )
    return round(raw, 1)


def layer_status(score: float) -> str:
    if score >= 85:
        return "高景气但可能拥挤"
    if score >= 75:
        return "强景气，进入股票筛选"
    if score >= 60:
        return "趋势确认，继续观察"
    if score >= 45:
        return "早期观察"
    return "噪音或证据不足"


def security_action(security: dict[str, Any], score: float, market_score: float | None, market: str) -> str:
    if market_score is None:
        return "补行情后再判断"
    if market != "US":
        if score >= 75 and market_score >= 70:
            return "跨市场强候选；需 Futu/财报/流动性复核后进入二次分析"
        if score >= 75:
            return "产业强但股价未确认；跨市场观察"
        return "跨市场观察；需 Futu 复核"
    if score >= 75 and market_score >= 70:
        return "加入观察池，交给 Buy-Side 二次分析"
    if score >= 75 and market_score < 55:
        return "产业强但股价未确认，等待转强"
    if market_score >= 78:
        return "趋势强，避免追高，等待回踩/突破确认"
    return "保留观察"


def build_radar(config: dict[str, Any], market_pack: dict[str, Any]) -> dict[str, Any]:
    indexed = candidate_index(market_pack)
    all_codes: list[str] = []
    for chain in config.get("chains", []):
        for layer in chain.get("layers", []):
            for security in layer.get("securities", []):
                code = str(security.get("code") or "").strip()
                if code:
                    all_codes.append(code)

    us_symbols = unique([symbol for code in all_codes if (symbol := us_symbol(code))])
    existing_us_symbols = {
        compact_code(code)
        for code in all_codes
        if us_symbol(code) and indexed.get(compact_code(code))
    }
    extra_us_symbols = [symbol for symbol in us_symbols if symbol not in existing_us_symbols]
    extra_quotes, extra_charts, extra_source = collect_extra_us_data(extra_us_symbols)

    futu_codes = unique([display_code(code) for code in all_codes if code_market(code) != "US"])
    futu_quotes, futu_source = collect_futu_snapshots(futu_codes)
    public_cross_quotes, public_cross_charts, public_cross_source = collect_public_cross_market_data(futu_codes)

    weights = config.get("default_chain_weights", {})
    chains: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for chain in config.get("chains", []):
        layer_rows: list[dict[str, Any]] = []
        for layer in chain.get("layers", []):
            securities: list[dict[str, Any]] = []
            market_scores: list[float] = []
            for security in layer.get("securities", []):
                code = str(security.get("code") or "").strip()
                market_data = price_from_sources(
                    code,
                    market_pack,
                    indexed,
                    extra_quotes,
                    extra_charts,
                    futu_quotes,
                    public_cross_quotes,
                    public_cross_charts,
                )
                if market_data.get("market_confirmation_score") is not None:
                    market_scores.append(float(market_data["market_confirmation_score"]))
                securities.append(
                    {
                        **security,
                        **market_data,
                    }
                )
            avg_market = round(sum(market_scores) / len(market_scores), 1) if market_scores else None
            score = weighted_layer_score(layer, avg_market, weights)
            enriched = {
                **layer,
                "opportunity_score": score,
                "status": layer_status(score),
                "market_confirmation_avg": avg_market,
                "securities": securities,
            }
            layer_rows.append(enriched)
            for security in securities:
                action = security_action(security, score, security.get("market_confirmation_score"), security.get("market"))
                candidates.append(
                    {
                        "chain_id": chain.get("id"),
                        "chain_name": chain.get("name"),
                        "layer_id": layer.get("id"),
                        "layer_name": layer.get("name"),
                        "layer_score": score,
                        "layer_status": enriched["status"],
                        "code": security.get("code"),
                        "name": security.get("name"),
                        "market": security.get("market"),
                        "role": security.get("role"),
                        "price": security.get("price"),
                        "market_confirmation_score": security.get("market_confirmation_score"),
                        "data_status": security.get("data_status"),
                        "action": action,
                    }
                )
        chain_score = round(sum(float(layer["opportunity_score"]) for layer in layer_rows) / len(layer_rows), 1) if layer_rows else 0.0
        chains.append(
            {
                **chain,
                "opportunity_score": chain_score,
                "status": layer_status(chain_score),
                "layers": sorted(layer_rows, key=lambda item: item["opportunity_score"], reverse=True),
            }
        )

    candidates.sort(
        key=lambda item: (
            float(item.get("layer_score") or 0),
            float(item.get("market_confirmation_score") or 0),
        ),
        reverse=True,
    )
    now = datetime.now().astimezone()
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_label": now.strftime("%Y-%m-%d %H:%M"),
        "data_sources": [
            "config/supply_chain_map.json structural demand map",
            "data/latest_market_pack.json existing US market pack",
            extra_source,
            futu_source,
            public_cross_source,
        ],
        "discipline": [
            "产业链雷达只发现候选，不直接给买入指令。",
            "港股/A股候选必须回到 Futu/券商行情、财报与交易规则复核。",
            "所有候选进入 Buy-Side 二次分析和入场路径雷达后，才允许讨论仓位。"
        ],
        "chains": chains,
        "candidates": candidates[:80],
    }


def fmt_num(value: Any, digits: int = 1) -> str:
    parsed = number(value)
    return "数据不足" if parsed is None else f"{parsed:.{digits}f}"


def fmt_price(value: Any) -> str:
    parsed = number(value)
    return "n/a" if parsed is None else f"{parsed:,.2f}"


def render_report(radar: dict[str, Any]) -> str:
    lines: list[str] = [
        "# 产业链需求雷达",
        "",
        f"- 生成时间：{radar.get('generated_label')}",
        "- 定位：从终端需求和供应链瓶颈发现候选股票；不构成买入建议。",
        "- 执行纪律：候选必须进入 Buy-Side 二次分析、重新计算 R/R，并按券商实时价复核。",
        "",
        "## 数据边界",
        "",
    ]
    for source in radar.get("data_sources", []):
        lines.append(f"- {source}")
    lines.extend(["", "## 产业链总览", ""])
    lines.extend(["| 产业链 | 机会分 | 状态 | Base/Bull/Bear |", "|---|---:|---|---|"])
    for chain in radar.get("chains", []):
        probs = f"{fmt_num(float(chain.get('base_case_probability', 0)) * 100, 0)}% / {fmt_num(float(chain.get('bull_case_probability', 0)) * 100, 0)}% / {fmt_num(float(chain.get('bear_case_probability', 0)) * 100, 0)}%"
        lines.append(f"| {chain.get('name')} | {fmt_num(chain.get('opportunity_score'))} | {chain.get('status')} | {probs} |")

    for chain in radar.get("chains", []):
        lines.extend(["", f"## {chain.get('name')}", ""])
        indicators = chain.get("watch_indicators", [])
        if indicators:
            lines.append("### 关键观察指标")
            lines.append("")
            for item in indicators:
                lines.append(f"- {item}")
            lines.append("")
        lines.append("### 环节强度")
        lines.append("")
        lines.extend(["| 环节 | 位置 | 机会分 | 趋势确认 | 状态 | 核心逻辑 |", "|---|---|---:|---:|---|---|"])
        for layer in chain.get("layers", []):
            evidence = "；".join(layer.get("evidence", [])[:2])
            lines.append(
                f"| {layer.get('name')} | {layer.get('chain_position')} | {fmt_num(layer.get('opportunity_score'))} | {fmt_num(layer.get('market_confirmation_avg'))} | {layer.get('status')} | {evidence} |"
            )

    lines.extend(["", "## 候选股票池", ""])
    lines.extend(["| 市场 | 代码 | 名称 | 环节 | 角色 | 价格 | 趋势确认 | 动作 |", "|---|---|---|---|---|---:|---:|---|"])
    for item in radar.get("candidates", [])[:50]:
        lines.append(
            f"| {item.get('market')} | {item.get('code')} | {item.get('name')} | {item.get('layer_name')} | {item.get('role')} | {fmt_price(item.get('price'))} | {fmt_num(item.get('market_confirmation_score'))} | {item.get('action')} |"
        )

    cross = [item for item in radar.get("candidates", []) if item.get("market") != "US"]
    if cross:
        lines.extend(["", "## 港股/A股跨市场候选", ""])
        lines.append("- 这些标的用于产业链映射和观察，不会自动进入美股交易建议。若需要执行，必须单独按对应市场财报、流动性、估值、交易规则复核。")
        lines.append("")
        lines.extend(["| 市场 | 代码 | 名称 | 环节 | 当前状态 |", "|---|---|---|---|---|"])
        for item in cross[:40]:
            lines.append(f"| {item.get('market')} | {item.get('code')} | {item.get('name')} | {item.get('layer_name')} | {item.get('data_status')} |")

    lines.extend(
        [
            "",
            "## 使用纪律",
            "",
            "- 产业链强不等于股票马上可以买；如果股价已大幅透支，只能等待回踩或突破确认。",
            "- 对覆铜板、电子布、光模块、CPO 等高弹性环节，要确认订单、产能、涨价、毛利率四项是否同步。",
            "- 港股/A股候选只作为跨市场雷达，不纳入复星证券美股整股约束；若后续交易，需单独建对应账户规则。",
            "- 系统不会因为主题热度直接买入，最终仍以 Buy-Side 分析和 R/R >= 2:1 为硬门槛。",
        ]
    )
    return "\n".join(lines) + "\n"


def archive_copy(report_path: Path) -> Path:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M")
    archive = report_path.with_name(f"supply-chain-radar-{timestamp}.md")
    archive.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP_PATH)
    parser.add_argument("--market-pack", type=Path, default=DEFAULT_MARKET_PACK)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--no-archive-copy", action="store_true")
    args = parser.parse_args()

    config = load_json(args.map, {})
    if not config:
        raise SystemExit(f"Supply-chain map not found or invalid: {args.map}")
    market_pack = load_json(args.market_pack, {})
    radar = build_radar(config, market_pack)
    write_json(args.out, radar)
    write_text(args.report, render_report(radar))
    if not args.no_archive_copy:
        archive = archive_copy(args.report)
        print(f"Wrote {archive}")
    print(f"Wrote {args.out}")
    print(f"Wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
