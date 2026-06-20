#!/usr/bin/env python3
"""Collect public US equity data and render a baseline market brief."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "reports"
CACHE_DIR = DATA_DIR / "cache"
CHART_CACHE_PATH = CACHE_DIR / "daily_charts.json"
FULL_PACK_PATH = DATA_DIR / "latest_market_pack.json"
BUY_SIDE_METRICS_PATH = DATA_DIR / "latest_buy_side_metrics.json"
SEC_QUARTER_METRICS_PATH = DATA_DIR / "latest_sec_quarter_metrics.json"


REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]
NET_INCOME_TAGS = ["NetIncomeLoss", "ProfitLoss"]
CFO_TAGS = ["NetCashProvidedByUsedInOperatingActivities"]
CAPEX_TAGS = ["PaymentsToAcquirePropertyPlantAndEquipment"]
ASSET_TAGS = ["Assets"]
LIABILITY_TAGS = ["Liabilities"]
EQUITY_TAGS = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
ETF_SYMBOLS = {"SPY", "QQQ", "DIA", "IWM", "TLT", "GLD", "USO"}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_compact_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def file_age_hours(path: Path) -> float | None:
    if not path.exists():
        return None
    return max(0.0, (time.time() - path.stat().st_mtime) / 3600)


def load_fresh_json(path: Path, max_age_hours: float) -> Any | None:
    age = file_age_hours(path)
    if age is None or age > max_age_hours:
        return None
    try:
        return load_json(path, None)
    except (OSError, json.JSONDecodeError):
        return None


def http_json(url: str, *, sec: bool = False, timeout: int = 6) -> Any:
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": os.getenv(
            "SEC_USER_AGENT",
            "us-stock-feishu-agent/0.1 configure_SEC_USER_AGENT@example.com",
        )
        if sec
        else "Mozilla/5.0 us-stock-feishu-agent/0.1",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def safe_http_json(url: str, *, sec: bool = False, timeout: int = 6) -> tuple[Any | None, str | None]:
    last_error = None
    for attempt in range(3):
        try:
            return http_json(url, sec=sec, timeout=timeout), None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(0.25 * (attempt + 1))
    return None, last_error


def futu_code(symbol: str) -> str | None:
    symbol = str(symbol or "").strip().upper()
    if not symbol or symbol.startswith("^"):
        return None
    return symbol if "." in symbol else f"US.{symbol}"


def collect_futu_session_quotes(symbols: list[str], base_quotes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Overlay broker-session quotes from Futu OpenD on top of public quote fields."""
    if os.getenv("FUTU_QUOTE_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
        return {}
    try:
        from portfolio_ui_server import current_us_session, fetch_session_quote
    except Exception:
        return {}

    requested_session = current_us_session()
    quotes: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        if not futu_code(symbol):
            continue
        try:
            session_quote = fetch_session_quote(symbol, requested_session)
        except Exception:
            continue
        price = parse_market_number(session_quote.get("price"))
        source = str(session_quote.get("source") or "")
        if price is None or "Futu OpenD" not in source:
            continue
        merged = dict(base_quotes.get(symbol, {}))
        merged.update(
            {
                "symbol": symbol,
                "regularMarketPrice": price,
                "regularMarketChangePercent": session_quote.get("change_pct"),
                "regularMarketTime": session_quote.get("time"),
                "futu_session": session_quote.get("session"),
                "futu_session_label": session_quote.get("session_label"),
                "futu_source_session": session_quote.get("source_session"),
                "futu_source_session_label": session_quote.get("source_session_label"),
                "futu_regular_price": session_quote.get("regular_price"),
                "futu_previous_close": session_quote.get("previous_close"),
                "source": source,
                "source_priority": "Futu OpenD first; public quote fields used only as fallback/enrichment",
            }
        )
        quotes[symbol] = merged
    return quotes


def yahoo_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    joined = ",".join(symbols)
    url = "https://query1.finance.yahoo.com/v7/finance/quote?" + urllib.parse.urlencode(
        {"symbols": joined}
    )
    data, error = safe_http_json(url)
    if error or not data:
        return collect_nasdaq_quotes(symbols)
    rows = data.get("quoteResponse", {}).get("result", [])
    found = {row.get("symbol"): row for row in rows if row.get("symbol")}
    quotes = {symbol: found.get(symbol, {"symbol": symbol, "error": "missing quote"}) for symbol in symbols}
    missing = [
        symbol
        for symbol, quote in quotes.items()
        if quote.get("error") or not quote.get("regularMarketPrice")
    ]
    if missing:
        quotes.update(collect_nasdaq_quotes(missing))
    return quotes


def parse_market_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = (
        value.replace("$", "")
        .replace("%", "")
        .replace(",", "")
        .replace("(", "-")
        .replace(")", "")
        .strip()
    )
    if not cleaned or cleaned.lower() in {"n/a", "nan"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def nasdaq_asset_class(symbol: str) -> str | None:
    if symbol.startswith("^"):
        return None
    return "etf" if symbol.upper() in ETF_SYMBOLS else "stocks"


def nasdaq_quote(symbol: str) -> dict[str, Any]:
    asset_class = nasdaq_asset_class(symbol)
    if not asset_class:
        return {"symbol": symbol, "error": "Nasdaq fallback does not support this symbol"}
    url = (
        f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(symbol)}/info?"
        + urllib.parse.urlencode({"assetclass": asset_class})
    )
    data, error = safe_http_json(url, timeout=8)
    if error or not data:
        return {"symbol": symbol, "error": error or "empty Nasdaq response"}
    payload = data.get("data") or {}
    primary = payload.get("primaryData") or {}
    price = parse_market_number(primary.get("lastSalePrice"))
    change_pct = parse_market_number(primary.get("percentageChange"))
    quote = {
        "symbol": symbol,
        "shortName": payload.get("companyName"),
        "regularMarketPrice": price,
        "regularMarketChangePercent": change_pct,
        "regularMarketTime": primary.get("lastTradeTimestamp"),
        "bid": parse_market_number(primary.get("bidPrice")),
        "ask": parse_market_number(primary.get("askPrice")),
        "regularMarketVolume": parse_market_number(primary.get("volume")),
        "source": "Nasdaq quote API fallback",
    }
    quote.update(nasdaq_summary_fields(symbol))
    return quote


def nasdaq_summary_fields(symbol: str) -> dict[str, Any]:
    asset_class = nasdaq_asset_class(symbol)
    if not asset_class:
        return {}
    url = (
        f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(symbol)}/summary?"
        + urllib.parse.urlencode({"assetclass": asset_class})
    )
    data, error = safe_http_json(url, timeout=8)
    if error or not data:
        return {}
    summary = ((data.get("data") or {}).get("summaryData") or {})

    def value(key: str) -> Any:
        item = summary.get(key)
        if isinstance(item, dict):
            return item.get("value")
        return item

    output: dict[str, Any] = {}
    market_cap = parse_market_number(value("MarketCap"))
    one_year_target = parse_market_number(value("OneYrTarget"))
    if market_cap is not None:
        output["marketCap"] = market_cap
    if one_year_target is not None:
        output["targetMeanPrice"] = one_year_target
    if output:
        output["fundamental_source"] = "Nasdaq summary API fallback"
    return output


def collect_nasdaq_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    quotes: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(nasdaq_quote, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                quotes[symbol] = future.result()
            except Exception as exc:  # noqa: BLE001
                quotes[symbol] = {"symbol": symbol, "error": str(exc)}
    return quotes


def yahoo_chart(symbol: str) -> dict[str, Any]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=1y&interval=1d"
    data, error = safe_http_json(url, timeout=5)
    if error or not data:
        fallback = nasdaq_chart(symbol)
        if fallback.get("error"):
            fallback["yahoo_error"] = error or "empty response"
        return fallback
    result = (data.get("chart", {}).get("result") or [{}])[0]
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    timestamps = [x for x in result.get("timestamp", []) if isinstance(x, (int, float))]
    closes = [float(x) for x in quote.get("close", []) if isinstance(x, (int, float))]
    highs = [float(x) for x in quote.get("high", []) if isinstance(x, (int, float))]
    lows = [float(x) for x in quote.get("low", []) if isinstance(x, (int, float))]
    if not closes:
        return {"symbol": symbol, "error": "no closes"}

    def avg(values: list[float], n: int) -> float | None:
        return round(sum(values[-n:]) / n, 4) if len(values) >= n else None

    def low(values: list[float], n: int) -> float | None:
        return round(min(values[-n:]), 4) if len(values) >= n else None

    returns = []
    for prev, current in zip(closes[-21:-1], closes[-20:]):
        if prev > 0 and current > 0:
            returns.append(math.log(current / prev))
    vol20 = None
    if len(returns) > 2:
        vol20 = round(statistics.stdev(returns) * math.sqrt(252), 4)

    return {
        "symbol": symbol,
        "last_close": round(closes[-1], 4),
        "ma20": avg(closes, 20),
        "ma50": avg(closes, 50),
        "ma200": avg(closes, 200),
        "low20": low(lows or closes, 20),
        "low60": low(lows or closes, 60),
        "high252": round(max(highs or closes), 4),
        "prior_high252": round(max((highs or closes)[:-1]), 4) if len(highs or closes) > 1 else None,
        "low252": round(min(lows or closes), 4),
        "realized_vol20": vol20,
        "chart_time": datetime.fromtimestamp(timestamps[-1], timezone.utc).isoformat() if timestamps else None,
        "source": "Yahoo chart API fallback",
    }


def nasdaq_chart(symbol: str) -> dict[str, Any]:
    asset_class = nasdaq_asset_class(symbol)
    if not asset_class:
        return {"symbol": symbol, "error": "Nasdaq fallback does not support this symbol"}
    to_date = datetime.now(timezone.utc).date()
    from_date = to_date - timedelta(days=370)
    params = urllib.parse.urlencode(
        {
            "assetclass": asset_class,
            "fromdate": from_date.isoformat(),
            "todate": to_date.isoformat(),
            "limit": 9999,
        }
    )
    url = f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(symbol)}/historical?{params}"
    data, error = safe_http_json(url, timeout=10)
    if error or not data:
        return {"symbol": symbol, "error": error or "empty Nasdaq historical response"}
    rows = (((data.get("data") or {}).get("tradesTable") or {}).get("rows") or [])
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    bar_times: list[str] = []
    for row in reversed(rows):
        close = parse_market_number(row.get("close"))
        high = parse_market_number(row.get("high"))
        low = parse_market_number(row.get("low"))
        bar_time = row.get("date")
        if close is not None:
            closes.append(close)
        if high is not None:
            highs.append(high)
        if low is not None:
            lows.append(low)
        if bar_time:
            bar_times.append(str(bar_time))
    if not closes:
        return {"symbol": symbol, "error": "no Nasdaq closes"}

    def avg(values: list[float], n: int) -> float | None:
        return round(sum(values[-n:]) / n, 4) if len(values) >= n else None

    def low(values: list[float], n: int) -> float | None:
        return round(min(values[-n:]), 4) if len(values) >= n else None

    returns = []
    for prev, current in zip(closes[-21:-1], closes[-20:]):
        if prev > 0 and current > 0:
            returns.append(math.log(current / prev))
    vol20 = round(statistics.stdev(returns) * math.sqrt(252), 4) if len(returns) > 2 else None
    return {
        "symbol": symbol,
        "last_close": round(closes[-1], 4),
        "ma20": avg(closes, 20),
        "ma50": avg(closes, 50),
        "ma200": avg(closes, 200),
        "low20": low(lows or closes, 20),
        "low60": low(lows or closes, 60),
        "high252": round(max(highs or closes), 4),
        "prior_high252": round(max((highs or closes)[:-1]), 4) if len(highs or closes) > 1 else None,
        "low252": round(min(lows or closes), 4),
        "realized_vol20": vol20,
        "chart_time": bar_times[-1] if bar_times else None,
        "source": "Nasdaq historical API fallback",
    }


def futu_chart(symbol: str) -> dict[str, Any]:
    code = futu_code(symbol)
    if not code:
        return {"symbol": symbol, "error": "Futu OpenD does not support this symbol format"}
    try:
        from futu import AuType, KLType, OpenQuoteContext, RET_OK  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {"symbol": symbol, "error": f"futu-api unavailable: {exc}"}

    host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.getenv("FUTU_OPEND_PORT", "11111").strip() or "11111")
    except ValueError:
        port = 11111

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=430)
    ctx = None
    try:
        ctx = OpenQuoteContext(host=host, port=port, is_async_connect=False)
        ret, data, _page_req_key = ctx.request_history_kline(
            code,
            start=start.isoformat(),
            end=end.isoformat(),
            ktype=KLType.K_DAY,
            autype=AuType.QFQ,
            max_count=1000,
        )
        if ret != RET_OK or data is None or len(data) == 0:
            return {"symbol": symbol, "error": f"Futu K-line unavailable: {data}"}
        def numeric_values(field: str) -> list[float]:
            values: list[float] = []
            for value in data.get(field, []):
                parsed = parse_market_number(value)
                if parsed is not None and parsed > 0:
                    values.append(parsed)
            return values

        closes = numeric_values("close")
        highs = numeric_values("high")
        lows = numeric_values("low")
        bar_times = [
            str(value)
            for value in data.get("time_key", [])
            if str(value).strip()
        ]
    except Exception as exc:  # noqa: BLE001
        return {"symbol": symbol, "error": f"Futu K-line error: {exc}"}
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass

    if not closes:
        return {"symbol": symbol, "error": "Futu K-line returned no closes"}

    def avg(values: list[float], n: int) -> float | None:
        return round(sum(values[-n:]) / n, 4) if len(values) >= n else None

    def low(values: list[float], n: int) -> float | None:
        return round(min(values[-n:]), 4) if len(values) >= n else None

    returns = []
    for prev, current in zip(closes[-21:-1], closes[-20:]):
        if prev > 0 and current > 0:
            returns.append(math.log(current / prev))
    vol20 = round(statistics.stdev(returns) * math.sqrt(252), 4) if len(returns) > 2 else None
    return {
        "symbol": symbol,
        "last_close": round(closes[-1], 4),
        "ma20": avg(closes, 20),
        "ma50": avg(closes, 50),
        "ma200": avg(closes, 200),
        "low20": low(lows or closes, 20),
        "low60": low(lows or closes, 60),
        "high252": round(max(highs or closes), 4),
        "prior_high252": round(max((highs or closes)[:-1]), 4) if len(highs or closes) > 1 else None,
        "low252": round(min(lows or closes), 4),
        "realized_vol20": vol20,
        "chart_time": bar_times[-1] if bar_times else None,
        "source": "Futu OpenD daily K-line",
    }


def collect_charts(symbols: list[str]) -> dict[str, dict[str, Any]]:
    charts: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(futu_chart, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                charts[symbol] = future.result()
            except Exception as exc:  # noqa: BLE001
                charts[symbol] = {"symbol": symbol, "error": str(exc)}
    missing = [
        symbol
        for symbol, chart in charts.items()
        if chart.get("error") or not chart.get("last_close")
    ]
    if missing:
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(nasdaq_chart, symbol): symbol for symbol in missing}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    fallback = future.result()
                except Exception as exc:  # noqa: BLE001
                    fallback = {"symbol": symbol, "error": str(exc)}
                if fallback.get("last_close"):
                    fallback["fallback_reason"] = charts.get(symbol, {}).get("error")
                    charts[symbol] = fallback
    return charts


def collect_charts_cached(
    symbols: list[str], *, max_age_hours: float = 20.0, force: bool = False
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Refresh daily K-lines at most once per day and reuse the rest."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached_payload = load_json(CHART_CACHE_PATH, {"records": {}})
    records = cached_payload.get("records", {}) if isinstance(cached_payload, dict) else {}
    now = time.time()
    charts: dict[str, dict[str, Any]] = {}
    refresh: list[str] = []

    for symbol in symbols:
        record = records.get(symbol, {}) if isinstance(records, dict) else {}
        cached_at = number(record.get("cached_at"))
        value = record.get("value")
        fresh = bool(
            not force
            and cached_at
            and now - cached_at <= max_age_hours * 3600
            and isinstance(value, dict)
            and value.get("last_close")
            and value.get("chart_time")
            and value.get("prior_high252") is not None
        )
        if fresh:
            charts[symbol] = {
                **value,
                "cache_status": "reused",
                "cached_at_utc": record.get("cached_at_utc"),
            }
        else:
            refresh.append(symbol)

    if refresh:
        refreshed = collect_charts(refresh)
        for symbol in refresh:
            value = refreshed.get(symbol, {"symbol": symbol, "error": "missing chart result"})
            value = {
                **value,
                "cache_status": "refreshed",
                "cached_at_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            }
            charts[symbol] = value
            if value.get("last_close"):
                records[symbol] = {"cached_at": now, "cached_at_utc": value.get("cached_at_utc"), "value": value}

    write_json(
        CHART_CACHE_PATH,
        {
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "records": records,
        },
    )
    return charts, {
        "reused": len(symbols) - len(refresh),
        "refreshed": len(refresh),
        "max_age_hours": max_age_hours,
    }


def sec_ticker_map() -> dict[str, dict[str, Any]]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache = DATA_DIR / "sec_company_tickers.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 24 * 3600:
        raw = json.loads(cache.read_text(encoding="utf-8"))
    else:
        raw = http_json("https://www.sec.gov/files/company_tickers.json", sec=True, timeout=8)
        cache.write_text(json.dumps(raw), encoding="utf-8")
    output: dict[str, dict[str, Any]] = {}
    for item in raw.values():
        ticker = str(item.get("ticker", "")).upper()
        if ticker:
            output[ticker] = {
                "cik": str(item.get("cik_str", "")).zfill(10),
                "title": item.get("title"),
            }
    return output


def fact_entries(facts: dict[str, Any], tags: list[str], forms: set[str]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        units = gaap.get(tag, {}).get("units", {})
        for unit_rows in units.values():
            for row in unit_rows:
                if row.get("form") not in forms or not isinstance(row.get("val"), (int, float)):
                    continue
                output.append(
                    {
                        "tag": tag,
                        "val": row.get("val"),
                        "fy": row.get("fy"),
                        "fp": row.get("fp"),
                        "form": row.get("form"),
                        "end": row.get("end"),
                        "filed": row.get("filed"),
                    }
                )
    output.sort(key=lambda x: (str(x.get("filed") or ""), str(x.get("end") or "")), reverse=True)
    return output


def latest_fact(facts: dict[str, Any], tags: list[str], forms: set[str]) -> dict[str, Any] | None:
    entries = fact_entries(facts, tags, forms)
    return entries[0] if entries else None


def annual_growth(facts: dict[str, Any], tags: list[str]) -> float | None:
    entries = [
        row
        for row in fact_entries(facts, tags, {"10-K", "20-F", "40-F"})
        if row.get("fp") == "FY" and isinstance(row.get("fy"), int)
    ]
    by_fy: dict[int, dict[str, Any]] = {}
    for row in entries:
        by_fy.setdefault(int(row["fy"]), row)
    years = sorted(by_fy.keys(), reverse=True)
    if len(years) < 2:
        return None
    latest = float(by_fy[years[0]]["val"])
    previous = float(by_fy[years[1]]["val"])
    if previous == 0:
        return None
    return round((latest / previous) - 1, 4)


def extract_recent_filings(submissions: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not submissions:
        return []
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    filings: list[dict[str, Any]] = []
    for form, filed, accession in zip(forms, dates, accessions):
        if form in {"10-K", "10-Q", "20-F", "40-F", "8-K"}:
            filings.append({"form": form, "filed": filed, "accession": accession})
        if len(filings) >= 8:
            break
    return filings


def latest_financial_accession(filings: list[dict[str, Any]]) -> str | None:
    for filing in filings:
        if filing.get("form") in {"10-K", "10-Q", "20-F", "40-F"}:
            return str(filing.get("accession") or "") or None
    return None


def sec_summary(
    ticker: str,
    mapping: dict[str, dict[str, Any]],
    *,
    poll_hours: float = 20.0,
    force: bool = False,
) -> dict[str, Any]:
    cache_dir = DATA_DIR / "sec"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"{ticker.upper()}.json"
    cached = load_json(cache, {}) if cache.exists() else {}
    if not force and cached and (file_age_hours(cache) or 0) < poll_hours:
        cached["cache_status"] = "reused_without_poll"
        return cached

    meta = mapping.get(ticker.upper())
    if not meta:
        return {"ticker": ticker, "sec_coverage": False, "error": "No SEC CIK mapping"}
    cik = meta["cik"]
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    submissions, sub_error = safe_http_json(submissions_url, sec=True, timeout=5)
    recent_filings = extract_recent_filings(submissions)
    latest_accession = latest_financial_accession(recent_filings)

    if cached and not force and latest_accession and latest_accession == cached.get("financial_filing_accession"):
        cached["recent_filings"] = recent_filings
        cached["sec_coverage"] = True
        cached["cache_status"] = "reused_financials_after_filing_poll"
        cached["filing_poll_time_utc"] = datetime.now(timezone.utc).isoformat()
        cache.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
        return cached

    facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    facts, fact_error = safe_http_json(facts_url, sec=True, timeout=5)
    time.sleep(0.12)

    if not facts and cached:
        cached["cache_status"] = "stale_cache_after_refresh_error"
        cached["facts_error"] = fact_error
        cached["submissions_error"] = sub_error
        return cached

    summary: dict[str, Any] = {
        "ticker": ticker,
        "company": meta.get("title"),
        "cik": cik,
        "sec_coverage": bool(facts and submissions),
        "financial_filing_accession": latest_accession,
        "cache_status": "refreshed_for_new_filing" if cached else "created",
        "filing_poll_time_utc": datetime.now(timezone.utc).isoformat(),
    }
    if fact_error:
        summary["facts_error"] = fact_error
    if sub_error:
        summary["submissions_error"] = sub_error
    if not facts:
        return summary

    annual_forms = {"10-K", "20-F", "40-F"}
    recent_forms = {"10-K", "10-Q", "20-F", "40-F"}
    revenue = latest_fact(facts, REVENUE_TAGS, annual_forms)
    net_income = latest_fact(facts, NET_INCOME_TAGS, annual_forms)
    cfo = latest_fact(facts, CFO_TAGS, annual_forms)
    capex = latest_fact(facts, CAPEX_TAGS, annual_forms)
    assets = latest_fact(facts, ASSET_TAGS, recent_forms)
    liabilities = latest_fact(facts, LIABILITY_TAGS, recent_forms)
    equity = latest_fact(facts, EQUITY_TAGS, recent_forms)

    summary["latest_annual_revenue"] = revenue
    summary["latest_annual_net_income"] = net_income
    summary["revenue_growth_yoy"] = annual_growth(facts, REVENUE_TAGS)
    if revenue and net_income and revenue["val"]:
        summary["net_margin"] = round(float(net_income["val"]) / float(revenue["val"]), 4)
    if cfo:
        summary["latest_annual_cfo"] = cfo
    if capex:
        summary["latest_annual_capex"] = capex
    if cfo and capex:
        summary["latest_annual_fcf"] = {
            "val": float(cfo["val"]) - abs(float(capex["val"])),
            "filed": cfo.get("filed"),
        }
    if assets and liabilities and assets["val"]:
        summary["liabilities_to_assets"] = round(float(liabilities["val"]) / float(assets["val"]), 4)
    if equity:
        summary["latest_equity"] = equity

    if recent_filings:
        summary["recent_filings"] = recent_filings
    cache.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    return summary


def collect_sec_summaries(
    universe: list[str],
    mapping: dict[str, dict[str, Any]],
    sec_map_error: str | None,
    skipped: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    skipped = skipped or set()
    if not mapping:
        return {
            ticker: {"ticker": ticker, "sec_coverage": False, "error": sec_map_error}
            for ticker in universe
        }
    summaries: dict[str, dict[str, Any]] = {}
    for ticker in skipped:
        cache = DATA_DIR / "sec" / f"{ticker.upper()}.json"
        if cache.exists():
            summaries[ticker] = load_json(cache, {})
            summaries[ticker]["cache_status"] = "reused_without_daily_poll"
        else:
            summaries[ticker] = {
                "ticker": ticker,
                "sec_coverage": False,
                "error": "SEC fetch skipped by sec_candidate_limit and no weekly cache exists",
            }
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(sec_summary, ticker, mapping): ticker for ticker in universe}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                summaries[ticker] = future.result()
            except Exception as exc:  # noqa: BLE001
                summaries[ticker] = {"ticker": ticker, "sec_coverage": False, "error": str(exc)}
    return summaries


def prioritize_sec_universe(
    universe: list[str], portfolio: dict[str, Any], config: dict[str, Any]
) -> tuple[list[str], set[str]]:
    holdings = [str(item.get("ticker", "")).upper() for item in portfolio.get("holdings", []) if item.get("ticker")]
    watchlist = [str(ticker).upper() for ticker in portfolio.get("watchlist", [])]
    physical = [str(ticker).upper() for ticker in config.get("physical_ai_focus", [])]
    prioritized = list(dict.fromkeys(holdings + watchlist + physical + universe))
    limit = int(config.get("sec_candidate_limit", 14))
    selected = [ticker for ticker in prioritized if ticker in universe][:limit]
    skipped = {ticker for ticker in universe if ticker not in set(selected)}
    return selected, skipped


def pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def money(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    value = float(value)
    if abs(value) >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.2f}T"
    if abs(value) >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    return f"${value:,.2f}"


def display_pct_value(value: Any) -> str:
    return f"{value}%" if isinstance(value, (int, float)) else "n/a"


def number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def evaluate_candidate(
    ticker: str,
    quote: dict[str, Any],
    chart: dict[str, Any],
    sec: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    price = number(quote.get("regularMarketPrice") or quote.get("postMarketPrice") or chart.get("last_close"))
    forward_pe = number(quote.get("forwardPE"))
    trailing_pe = number(quote.get("trailingPE"))
    market_cap = number(quote.get("marketCap"))
    latest_net_income = sec.get("latest_annual_net_income") if isinstance(sec.get("latest_annual_net_income"), dict) else {}
    latest_net_income_value = number(latest_net_income.get("val")) if isinstance(latest_net_income, dict) else None
    estimated_pe_from_sec = None
    if market_cap and latest_net_income_value and latest_net_income_value > 0:
        estimated_pe_from_sec = round(market_cap / latest_net_income_value, 2)
    ma50 = number(chart.get("ma50"))
    ma200 = number(chart.get("ma200"))
    low20 = number(chart.get("low20"))
    low60 = number(chart.get("low60"))
    high252 = number(chart.get("high252") or quote.get("fiftyTwoWeekHigh"))
    physical = ticker in set(config.get("physical_ai_focus", []))
    rules = config.get("entry_rules", {})
    pe_cap = rules.get("leader_forward_pe_cap" if physical else "standard_forward_pe_cap", 30)

    confidence_items = [
        bool(price),
        bool(ma50),
        bool(ma200),
        bool(sec.get("sec_coverage")),
        bool(sec.get("latest_annual_revenue")),
        bool(sec.get("recent_filings")),
    ]
    data_confidence = round(sum(confidence_items) / len(confidence_items), 2)

    revenue_growth = number(sec.get("revenue_growth_yoy"))
    net_margin = number(sec.get("net_margin"))
    liabilities_to_assets = number(sec.get("liabilities_to_assets"))
    quality_score = 0.0
    if revenue_growth is not None:
        quality_score += max(-1.0, min(1.5, revenue_growth * 5))
    if net_margin is not None:
        quality_score += max(-0.8, min(1.2, net_margin * 3))
    if liabilities_to_assets is not None:
        quality_score += max(-1.0, min(0.7, 0.75 - liabilities_to_assets))
    if sec.get("latest_annual_fcf", {}).get("val", 0) > 0:
        quality_score += 0.5

    pe = forward_pe or trailing_pe
    valuation_score = 0.0
    if pe and pe > 0:
        valuation_score = max(-1.5, min(1.2, (pe_cap - pe) / pe_cap))

    technical_score = 0.0
    if price and ma50:
        premium50 = (price / ma50) - 1
        technical_score += 0.4 if premium50 <= 0.03 else -min(0.8, premium50)
    if price and ma200:
        extension200 = (price / ma200) - 1
        technical_score += 0.4 if extension200 <= 0.18 else -min(0.8, extension200 / 2)
    if price and high252:
        drawdown = (price / high252) - 1
        technical_score += 0.2 if drawdown < -0.08 else -0.1

    technical_anchor_values = [
        x
        for x in [
            ma50 * 1.03 if ma50 else None,
            low20 * 1.05 if low20 else None,
            low60 * 1.08 if low60 else None,
            ma200 * 1.12 if ma200 else None,
        ]
        if x and price
    ]
    technical_max = min(technical_anchor_values) if technical_anchor_values else (price * 0.95 if price else None)
    valuation_max = None
    if price and pe and pe > 0:
        valuation_max = price * min(1.03, pe_cap / pe)
    elif price:
        valuation_max = price * 0.95
    strict_entry = min([x for x in [technical_max, valuation_max] if x]) if price else None
    add_zone = strict_entry * 0.94 if strict_entry else None
    invalidation = min([x for x in [low60, ma200 * 0.92 if ma200 else None] if x], default=None)

    reward_risk = None
    mechanical_target = None
    if price and strict_entry and invalidation and invalidation < price:
        mechanical_target = price * (1.18 if physical else 1.12)
        reward = mechanical_target - price
        risk = price - invalidation
        if risk > 0:
            reward_risk = round(reward / risk, 2)

    buyable = bool(
        price
        and strict_entry
        and price <= strict_entry * 1.005
        and data_confidence >= config.get("min_data_confidence_for_buy", 0.68)
        and reward_risk is not None
        and reward_risk >= config.get("min_reward_risk_for_buy", 2.0)
    )
    overall = quality_score + valuation_score + technical_score + (0.4 if physical else 0.0)
    if buyable:
        overall += 0.8

    return {
        "ticker": ticker,
        "name": quote.get("shortName") or quote.get("longName") or sec.get("company"),
        "physical_ai_focus": physical,
        "price": price,
        "quote_source": quote.get("source"),
        "quote_time": quote.get("regularMarketTime"),
        "quote_session": quote.get("futu_session"),
        "quote_source_session": quote.get("futu_source_session"),
        "quote_source_priority": quote.get("source_priority"),
        "chart_source": chart.get("source"),
        "chart_time": chart.get("chart_time"),
        "chart_cache_status": chart.get("cache_status"),
        "chart_cached_at_utc": chart.get("cached_at_utc"),
        "sec_filing_poll_time_utc": sec.get("filing_poll_time_utc"),
        "market_cap": market_cap,
        "forward_pe": forward_pe,
        "trailing_pe": trailing_pe,
        "estimated_pe_from_sec": estimated_pe_from_sec,
        "valuation_source": quote.get("fundamental_source") or quote.get("source"),
        "data_confidence": data_confidence,
        "quality_score": round(quality_score, 2),
        "valuation_score": round(valuation_score, 2),
        "technical_score": round(technical_score, 2),
        "overall_score": round(overall, 2),
        "strict_entry": round(strict_entry, 2) if strict_entry else None,
        "add_zone": round(add_zone, 2) if add_zone else None,
        "invalidation": round(invalidation, 2) if invalidation else None,
        "mechanical_target": round(mechanical_target, 2) if mechanical_target else None,
        "reward_risk": reward_risk,
        "buyable_now": buyable,
        "chart": chart,
        "sec": sec,
        "quote_error": quote.get("error"),
    }


def quote_from_cached_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": candidate.get("ticker"),
        "shortName": candidate.get("name"),
        "regularMarketPrice": candidate.get("price"),
        "regularMarketTime": candidate.get("quote_time"),
        "source": candidate.get("quote_source"),
        "forwardPE": candidate.get("forward_pe"),
        "trailingPE": candidate.get("trailing_pe"),
    }


def build_pack(
    config: dict[str, Any], portfolio: dict[str, Any], *, mode: str = "full"
) -> dict[str, Any]:
    if mode not in {"quick", "full", "weekly"}:
        raise ValueError(f"Unsupported collection mode: {mode}")

    universe = list(dict.fromkeys(config.get("universe", []) + portfolio.get("watchlist", [])))
    holdings = [str(item.get("ticker", "")).upper() for item in portfolio.get("holdings", []) if item.get("ticker")]
    market_symbols = list(config.get("market_symbols", []))
    quick_mode = mode == "quick"
    requested_universe = holdings if quick_mode else universe
    quote_symbols = list(dict.fromkeys(market_symbols + requested_universe))

    futu_quotes = collect_futu_session_quotes(quote_symbols, {})
    public_needed = [
        symbol
        for symbol in quote_symbols
        if not futu_quotes.get(symbol, {}).get("regularMarketPrice")
    ]
    public_quotes = collect_nasdaq_quotes(public_needed)
    quotes = dict(public_quotes)
    quotes.update(futu_quotes)

    cache_stats: dict[str, Any] = {
        "mode": mode,
        "quotes_refreshed": len(quote_symbols),
        "futu_quotes": len(futu_quotes),
        "public_quote_fallbacks": len(public_needed),
    }
    cached_full = load_json(FULL_PACK_PATH, {}) if FULL_PACK_PATH.exists() else {}
    cached_candidates = {
        str(item.get("ticker", "")).upper(): item
        for item in cached_full.get("candidates", [])
        if item.get("ticker")
    }

    if quick_mode:
        charts = {
            ticker: cached_candidates.get(ticker, {}).get("chart", {})
            for ticker in requested_universe
        }
        sec_summaries = {
            ticker: cached_candidates.get(ticker, {}).get(
                "sec", {"ticker": ticker, "sec_coverage": False, "error": "no full-report cache"}
            )
            for ticker in requested_universe
        }
        for ticker in requested_universe:
            if ticker not in quotes or not quotes[ticker].get("regularMarketPrice"):
                quotes[ticker] = quote_from_cached_candidate(cached_candidates.get(ticker, {}))
        cache_stats.update(
            {
                "charts_reused": sum(bool(chart.get("last_close")) for chart in charts.values()),
                "sec_reused": sum(bool(sec.get("sec_coverage")) for sec in sec_summaries.values()),
                "full_pack_as_of_utc": cached_full.get("as_of_utc"),
            }
        )
    else:
        chart_symbols = list(dict.fromkeys(market_symbols + universe + holdings))
        charts, chart_stats = collect_charts_cached(
            chart_symbols,
            max_age_hours=float(config.get("chart_cache_hours", 20)),
        )
        cache_stats["charts"] = chart_stats
        try:
            sec_map = sec_ticker_map()
        except Exception as exc:  # noqa: BLE001
            sec_map = {}
            sec_map_error = str(exc)
        else:
            sec_map_error = None

        if mode == "weekly":
            sec_universe, skipped_sec = universe, set()
        else:
            sec_universe, skipped_sec = prioritize_sec_universe(universe, portfolio, config)
        sec_summaries = collect_sec_summaries(sec_universe, sec_map, sec_map_error, skipped_sec)
        cache_stats["sec_requested"] = len(sec_universe)
        cache_stats["sec_skipped"] = len(skipped_sec)

    candidates = []
    for ticker in requested_universe:
        sec = sec_summaries.get(ticker, {"ticker": ticker, "sec_coverage": False, "error": "missing SEC summary"})
        candidates.append(evaluate_candidate(ticker, quotes.get(ticker, {}), charts.get(ticker, {}), sec, config))

    candidates.sort(key=lambda item: item.get("overall_score", -99), reverse=True)
    buyable = [item for item in candidates if item.get("buyable_now")][: config.get("max_buy_ideas", 5)]
    watchlist = [
        item
        for item in candidates
        if item.get("physical_ai_focus") and item.get("ticker") not in {x.get("ticker") for x in buyable}
    ][:12]

    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "cache_stats": cache_stats,
        "source_notes": [
            "Quotes: Futu OpenD session-aware market snapshot first when connected; Nasdaq/Yahoo public quote fields are fallback/enrichment only.",
            "Chart data: Futu OpenD daily K-line first when connected; Nasdaq historical endpoint is fallback only; daily values are cached.",
            "Financial statements and recent filings: SEC EDGAR companyfacts/submissions; financial facts refresh only when a new 10-Q/10-K/20-F/40-F is detected.",
            "Mechanical scores are only a pre-screen; the Codex Buy-Side Stock Analysis pass must verify every actionable conclusion.",
        ],
        "market": {symbol: quotes.get(symbol, {}) for symbol in market_symbols},
        "portfolio": portfolio,
        "candidates": candidates,
        "buyable_now": buyable,
        "physical_ai_watchlist": watchlist,
    }


def compact_holding(holding: dict[str, Any], candidate: dict[str, Any] | None) -> dict[str, Any]:
    item = candidate or {}
    price = number(item.get("price")) or number(holding.get("current_price_snapshot"))
    shares = number(holding.get("shares")) or 0.0
    cost = number(holding.get("cost_basis"))
    return {
        "ticker": str(holding.get("ticker", "")).upper(),
        "name": holding.get("name"),
        "shares": shares,
        "cost_basis": cost,
        "price": price,
        "market_value": round(price * shares, 2) if price is not None else holding.get("market_value_snapshot"),
        "unrealized_pnl": round((price - cost) * shares, 2) if price is not None and cost is not None else None,
        "target_weight_pct": holding.get("target_weight_pct"),
    }


def compact_candidate(
    item: dict[str, Any],
    extra_metrics: dict[str, Any] | None = None,
    quarter_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chart = item.get("chart", {})
    sec = item.get("sec", {})
    latest_revenue = sec.get("latest_annual_revenue") or {}
    latest_fcf = sec.get("latest_annual_fcf") or {}
    recent_filings = sec.get("recent_filings") or []
    quarter = quarter_metrics or {}
    quarter_revenue = quarter.get("revenue", {})
    quarter_net_income = quarter.get("net_income", {})
    output = {
        "ticker": item.get("ticker"),
        "name": item.get("name"),
        "physical_ai_focus": item.get("physical_ai_focus"),
        "price": item.get("price"),
        "quote_time": item.get("quote_time"),
        "quote_source": item.get("quote_source"),
        "quote_session": item.get("quote_session"),
        "chart_time": item.get("chart_time"),
        "chart_source": item.get("chart_source"),
        "chart_cache_status": item.get("chart_cache_status"),
        "chart_cached_at_utc": item.get("chart_cached_at_utc"),
        "sec_filing_poll_time_utc": item.get("sec_filing_poll_time_utc"),
        "forward_pe": item.get("forward_pe"),
        "trailing_pe": item.get("trailing_pe"),
        "estimated_pe_from_sec": item.get("estimated_pe_from_sec"),
        "valuation_source": item.get("valuation_source"),
        "data_confidence": item.get("data_confidence"),
        "mechanical_scores": {
            "quality": item.get("quality_score"),
            "valuation": item.get("valuation_score"),
            "technical": item.get("technical_score"),
            "overall": item.get("overall_score"),
        },
        "entry": {
            "strict_entry": item.get("strict_entry"),
            "add_zone": item.get("add_zone"),
            "invalidation": item.get("invalidation"),
            "mechanical_target": item.get("mechanical_target"),
            "reward_risk": item.get("reward_risk"),
            "buyable_now": item.get("buyable_now"),
        },
        "technicals": {
            key: chart.get(key)
            for key in (
                "last_close",
                "ma20",
                "ma50",
                "ma200",
                "low20",
                "low60",
                "high252",
                "prior_high252",
                "low252",
                "realized_vol20",
                "chart_time",
                "cached_at_utc",
                "cache_status",
                "source",
            )
        },
        "financials": {
            "sec_coverage": sec.get("sec_coverage"),
            "revenue_growth_yoy": sec.get("revenue_growth_yoy"),
            "net_margin": sec.get("net_margin"),
            "liabilities_to_assets": sec.get("liabilities_to_assets"),
            "latest_annual_revenue": latest_revenue.get("val"),
            "latest_annual_revenue_filed": latest_revenue.get("filed"),
            "latest_annual_fcf": latest_fcf.get("val"),
            "latest_financial_accession": sec.get("financial_filing_accession"),
            "recent_filings": recent_filings[:4],
            "cache_status": sec.get("cache_status"),
            "latest_quarter_end": (quarter_revenue.get("latest") or {}).get("end"),
            "quarter_revenue_growth_yoy": quarter_revenue.get("growth"),
            "quarter_net_income_growth_yoy": quarter_net_income.get("growth"),
            "quarter_gross_margin": quarter.get("gross_margin"),
            "prior_quarter_gross_margin": quarter.get("prior_gross_margin"),
        },
    }
    if extra_metrics:
        valuation = extra_metrics.get("valuation", {})
        consensus = extra_metrics.get("consensus", {})
        output["daily_buy_side_cache"] = {
            "rsi14": extra_metrics.get("rsi14"),
            "macd": extra_metrics.get("macd"),
            "macd_signal": extra_metrics.get("macd_signal"),
            "return_1m_pct": extra_metrics.get("return_1m"),
            "return_3m_pct": extra_metrics.get("return_3m"),
            "volume_ratio20": extra_metrics.get("volume_ratio20"),
            "valuation": {
                "current_pe": valuation.get("current_pe"),
                "forward_pe": valuation.get("forward_pe"),
                "one_year_average_pe": valuation.get("one_year_average_pe"),
                "pe_percentile": valuation.get("pe_percentile"),
                "last_update": valuation.get("last_update"),
            },
            "consensus": {
                "average_target": consensus.get("average"),
                "lowest_target": consensus.get("lowest"),
                "highest_target": consensus.get("highest"),
                "analyst_count": consensus.get("total"),
                "update_time": consensus.get("update_time_str"),
            },
        }
    return output


def candidate_gate(item: dict[str, Any], config: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    price = number(item.get("price"))
    entry = number(item.get("strict_entry"))
    confidence = number(item.get("data_confidence")) or 0.0
    reward_risk = number(item.get("reward_risk"))
    technical = number(item.get("technical_score"))
    if confidence < float(config.get("min_data_confidence_for_buy", 0.68)):
        reasons.append("数据覆盖不足")
    if price is None or entry is None or price > entry * 1.05:
        reasons.append("未进入买入区间附近")
    if reward_risk is None or reward_risk < float(config.get("min_reward_risk_for_buy", 2.0)):
        reasons.append("R/R低于2或无法计算")
    if technical is None or technical < -0.4:
        reasons.append("技术面过热或结构不佳")
    return not reasons, reasons


def compare_compact_inputs(current: dict[str, Any], previous: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not previous:
        return [{"scope": "all", "changes": ["首次建立精简输入基线"]}]
    old_items = {
        item.get("ticker"): item
        for item in previous.get("holdings_detail", []) + previous.get("research_candidates", [])
        if item.get("ticker")
    }
    changes: list[dict[str, Any]] = []
    for item in current.get("holdings_detail", []) + current.get("research_candidates", []):
        ticker = item.get("ticker")
        old = old_items.get(ticker)
        if not old:
            changes.append({"ticker": ticker, "changes": ["新增到本次分析范围"]})
            continue
        item_changes: list[str] = []
        price = number(item.get("price"))
        old_price = number(old.get("price"))
        if price and old_price and abs(price / old_price - 1) >= 0.02:
            item_changes.append(f"价格变化 {((price / old_price) - 1) * 100:+.1f}%")
        entry = item.get("entry", {}) if isinstance(item.get("entry"), dict) else {}
        old_entry = old.get("entry", {}) if isinstance(old.get("entry"), dict) else {}
        if entry.get("buyable_now") != old_entry.get("buyable_now"):
            item_changes.append("机械可买状态变化")
        financials = item.get("financials", {}) if isinstance(item.get("financials"), dict) else {}
        old_financials = old.get("financials", {}) if isinstance(old.get("financials"), dict) else {}
        if financials.get("latest_financial_accession") != old_financials.get("latest_financial_accession"):
            item_changes.append("检测到新财报申报")
        if item_changes:
            changes.append({"ticker": ticker, "changes": item_changes})
    return changes


def build_compact_input(
    pack: dict[str, Any], config: dict[str, Any], previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    mode = str(pack.get("mode") or "full")
    candidates = pack.get("candidates", [])
    candidate_map = {str(item.get("ticker", "")).upper(): item for item in candidates}
    holdings = pack.get("portfolio", {}).get("holdings", [])
    holding_tickers = {str(item.get("ticker", "")).upper() for item in holdings}
    buy_side_cache = load_fresh_json(BUY_SIDE_METRICS_PATH, float(config.get("valuation_cache_hours", 30))) or {}
    metric_map = buy_side_cache.get("metrics", {}) if isinstance(buy_side_cache, dict) else {}
    quarter_map = load_json(SEC_QUARTER_METRICS_PATH, {}) if SEC_QUARTER_METRICS_PATH.exists() else {}

    if mode == "weekly":
        research_pool = [item for item in candidates if item.get("ticker") not in holding_tickers]
    elif mode == "quick":
        research_pool = []
    else:
        passing = [item for item in candidates if candidate_gate(item, config)[0] and item.get("ticker") not in holding_tickers]
        research_pool = passing[: int(config.get("model_candidate_limit", 3))]

    holdings_detail: list[dict[str, Any]] = []
    for holding in holdings:
        ticker = str(holding.get("ticker", "")).upper()
        base = compact_holding(holding, candidate_map.get(ticker))
        detail = (
            compact_candidate(candidate_map[ticker], metric_map.get(ticker), quarter_map.get(ticker))
            if ticker in candidate_map
            else {"ticker": ticker}
        )
        holdings_detail.append({**detail, **base})

    research_candidates = [
        compact_candidate(
            item,
            metric_map.get(str(item.get("ticker", "")).upper()),
            quarter_map.get(str(item.get("ticker", "")).upper()),
        )
        for item in research_pool
    ]
    rejected: dict[str, int] = {}
    for item in candidates:
        if item.get("ticker") in holding_tickers or item in research_pool:
            continue
        _passed, reasons = candidate_gate(item, config)
        for reason in reasons:
            rejected[reason] = rejected.get(reason, 0) + 1

    portfolio = pack.get("portfolio", {})
    output = {
        "schema_version": 1,
        "mode": mode,
        "as_of_utc": pack.get("as_of_utc"),
        "rules": {
            "max_model_candidates": "all screened names" if mode == "weekly" else int(config.get("model_candidate_limit", 3)),
            "min_data_confidence": config.get("min_data_confidence_for_buy", 0.68),
            "min_reward_risk": config.get("min_reward_risk_for_buy", 2.0),
            "quick_mode_policy": "只报告变化；没有新判断时沿用上一份完整报告，不重新研究财报",
        },
        "market": {
            symbol: {
                key: quote.get(key)
                for key in ("regularMarketPrice", "regularMarketChangePercent", "regularMarketTime", "futu_session_label", "source")
            }
            for symbol, quote in pack.get("market", {}).items()
        },
        "portfolio_summary": {
            key: portfolio.get(key)
            for key in ("base_currency", "risk_profile", "cash_usd", "cash_pct", "cash_target_pct", "max_single_position_pct", "net_deposit_usd")
        },
        "holdings_detail": holdings_detail,
        "research_candidates": research_candidates,
        "prescreen": {
            "universe_count": len(candidates),
            "passed_for_model_count": len(research_candidates),
            "rejected_reason_counts": rejected,
        },
        "cache_stats": pack.get("cache_stats", {}),
        "source_notes": pack.get("source_notes", []),
        "previous_full_report_path": str(REPORT_DIR / "latest-public-equity-brief.md"),
    }
    output["material_changes"] = compare_compact_inputs(output, previous)
    return output


def render_report(pack: dict[str, Any]) -> str:
    as_of = pack.get("as_of_utc", "")
    lines = [
        "# 美股市场机械预筛报告",
        "",
        f"- 数据时间 UTC: {as_of}",
        "- 说明: 这是脚本生成的基础数据包，不替代 Buy-Side Stock Analysis 的完整判断。",
        "",
        "## 市场概览",
        "",
        "| 标的 | 价格 | 日涨跌 |",
        "|---|---:|---:|",
    ]
    for symbol, quote in pack.get("market", {}).items():
        price = quote.get("regularMarketPrice")
        change = quote.get("regularMarketChangePercent")
        lines.append(f"| {symbol} | {money(price) if price else 'n/a'} | {pct(change / 100) if isinstance(change, (int, float)) else 'n/a'} |")

    holdings = pack.get("portfolio", {}).get("holdings", [])
    lines.extend(["", "## 持仓输入", ""])
    if not holdings:
        lines.append("未配置真实持仓。请在 `config/portfolio.json` 填入 holdings，agent 才会输出个性化持仓建议。")
    else:
        lines.extend(["| Ticker | 股数 | 成本 | 目标仓位 |", "|---|---:|---:|---:|"])
        for item in holdings:
            lines.append(
                f"| {item.get('ticker')} | {item.get('shares', 'n/a')} | {money(item.get('cost_basis'))} | {display_pct_value(item.get('target_weight_pct'))} |"
            )

    lines.extend(["", "## 当前价位可入手预筛", ""])
    buyable = pack.get("buyable_now", [])
    if not buyable:
        lines.append("今日机械预筛没有足够高置信度的“当前价位可入手”标的。正式自动化应宁缺毋滥，不要硬凑 5 支。")
    else:
        lines.extend(["| Ticker | 价格 | 严格首买价 | 加仓区 | 失效位 | R/R | 置信度 |", "|---|---:|---:|---:|---:|---:|---:|"])
        for item in buyable:
            lines.append(
                f"| {item['ticker']} | {money(item.get('price'))} | {money(item.get('strict_entry'))} | {money(item.get('add_zone'))} | {money(item.get('invalidation'))} | {item.get('reward_risk') or 'n/a'} | {item.get('data_confidence')} |"
            )

    lines.extend(["", "## 物理 AI 观察清单", ""])
    lines.extend(["| Ticker | 价格 | 严格首买价 | Fwd P/E | 数据置信度 | 备注 |", "|---|---:|---:|---:|---:|---|"])
    for item in pack.get("physical_ai_watchlist", []):
        sec = item.get("sec", {})
        growth = sec.get("revenue_growth_yoy")
        note = f"收入同比 {pct(growth)}" if isinstance(growth, (int, float)) else "等待财报/估值复核"
        lines.append(
            f"| {item['ticker']} | {money(item.get('price'))} | {money(item.get('strict_entry'))} | {item.get('forward_pe') or item.get('trailing_pe') or 'n/a'} | {item.get('data_confidence')} | {note} |"
        )

    lines.extend(["", "## 数据源限制", ""])
    for note in pack.get("source_notes", []):
        lines.append(f"- {note}")
    lines.append("- 仅供投研辅助，不是投资建议或交易指令。")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("quick", "full", "weekly"), default="full")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--compact-out", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_json(ROOT / "config" / "agent_config.json", {})
    portfolio = load_json(ROOT / "config" / "portfolio.json", {"holdings": [], "watchlist": []})
    defaults = {
        "quick": (
            DATA_DIR / "latest_quick_market_pack.json",
            DATA_DIR / "latest_quick_agent_input.json",
            REPORT_DIR / "latest-market-quick.md",
        ),
        "full": (
            DATA_DIR / "latest_market_pack.json",
            DATA_DIR / "latest_agent_input.json",
            REPORT_DIR / "latest-market-brief.md",
        ),
        "weekly": (
            DATA_DIR / "latest_weekly_market_pack.json",
            DATA_DIR / "latest_weekly_agent_input.json",
            REPORT_DIR / "latest-weekly-market-scan.md",
        ),
    }
    default_out, default_compact, default_report = defaults[args.mode]
    out_path = args.out or default_out
    compact_path = args.compact_out or default_compact
    report_path = args.report or default_report
    previous_compact = load_json(compact_path, {}) if compact_path.exists() else None

    pack = build_pack(config, portfolio, mode=args.mode)
    compact = build_compact_input(pack, config, previous_compact)
    write_json(out_path, pack)
    write_compact_json(compact_path, compact)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(pack), encoding="utf-8")
    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes)")
    print(f"Wrote {compact_path} ({compact_path.stat().st_size} bytes)")
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
