#!/usr/bin/env python3
"""Build a privacy-safe free data fallback layer.

This module is intentionally conservative:

- Official sources are marked high confidence only when data is actually returned.
- Vendor/open-source fallbacks are never labeled as official.
- Missing permissions, rate limits and empty responses are recorded as data gaps.
- No API key is ever written to output.
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DOCS_DATA_DIR = ROOT / "docs" / "data"
REPORTS_DIR = ROOT / "reports"
CACHE_DIR = DATA_DIR / "cache" / "free_fallback"

DEFAULT_MARKET_PACK = DATA_DIR / "latest_market_pack.json"
DEFAULT_FMP_RESEARCH = DATA_DIR / "latest_fmp_research.json"
DEFAULT_MACRO_REGIME = DATA_DIR / "latest_macro_regime.json"
DEFAULT_OPPORTUNITY_RADAR = DATA_DIR / "latest_opportunity_radar.json"
DEFAULT_CROSS_MARKET = DATA_DIR / "latest_cross_market_intelligence.json"
DEFAULT_SECONDARY_QUEUE = DATA_DIR / "latest_secondary_analysis_queue.json"
DEFAULT_OUTPUT = DATA_DIR / "latest_free_data_fallback.json"
DEFAULT_DOCS_OUTPUT = DOCS_DATA_DIR / "free_data_fallback.json"
DEFAULT_REPORT = REPORTS_DIR / "latest-free-data-fallback.md"

LOCAL_ENV_PATHS = (
    ROOT / ".env",
    Path("D:/codex-AI-agent/US-RMB-Agent/.env"),
)

FRED_SERIES = [
    "DGS10",
    "DGS2",
    "FEDFUNDS",
    "CPIAUCSL",
    "PCEPI",
    "UNRATE",
    "PAYEMS",
    "BAMLH0A0HYM2",
]

SOURCE_TYPE_OFFICIAL = "official"
SOURCE_TYPE_COMPANY_IR = "company_ir"
SOURCE_TYPE_VENDOR = "vendor_fallback"
SOURCE_TYPE_OPEN = "open_source_fallback"
ALPHA_LAST_REQUEST_AT = 0.0
CNINFO_FINANCIAL_KEYWORDS = (
    "年度报告",
    "半年度报告",
    "季度报告",
    "业绩预告",
    "业绩快报",
    "财务报表",
    "审计报告",
)
HKEX_FINANCIAL_KEYWORDS = (
    "annual results",
    "annual report",
    "interim results",
    "interim report",
    "quarterly results",
    "quarterly report",
    "financial statements",
    "results announcement",
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
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").replace("%", "").strip()
        if not cleaned or cleaned.lower() in {"none", "null", "nan", "n/a", "--"}:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def env_key(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


class Fetcher:
    def __init__(self) -> None:
        self.last_sec_at = 0.0

    def http_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        sec: bool = False,
        timeout: int = 15,
        method: str = "GET",
        body: bytes | None = None,
    ) -> tuple[Any | None, str | None, int | None]:
        if sec:
            elapsed = time.monotonic() - self.last_sec_at
            if elapsed < 0.12:
                time.sleep(0.12 - elapsed)
            self.last_sec_at = time.monotonic()
        request_headers = {
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": os.getenv(
                "SEC_USER_AGENT",
                "ffxdz-ai public research dashboard contact@example.com",
            )
            if sec
            else "Mozilla/5.0 ffxdz-ai-public-research/0.1",
        }
        if headers:
            request_headers.update(headers)
        req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw), None, int(response.status)
        except urllib.error.HTTPError as exc:
            try:
                message = exc.read().decode("utf-8")[:300]
            except Exception:  # noqa: BLE001
                message = str(exc)
            return None, message or str(exc), int(exc.code)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return None, str(exc)[:300], None

    def http_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 15,
    ) -> tuple[str | None, str | None, int | None]:
        request_headers = {
            "Accept": "application/json,text/csv,text/plain,*/*",
            "User-Agent": "Mozilla/5.0 ffxdz-ai-public-research/0.1",
        }
        if headers:
            request_headers.update(headers)
        req = urllib.request.Request(url, headers=request_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace"), None, int(response.status)
        except urllib.error.HTTPError as exc:
            try:
                message = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                message = str(exc)
            return None, message or str(exc), int(exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return None, str(exc)[:300], None

    def cached_json(
        self,
        cache_path: Path,
        url: str,
        *,
        max_age_hours: float = 24 * 7,
        sec: bool = False,
        timeout: int = 15,
    ) -> tuple[Any | None, str | None, int | None, bool]:
        if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) / 3600 <= max_age_hours:
            return load_json(cache_path, None), None, 200, True
        data, error, status = self.http_json(url, sec=sec, timeout=timeout)
        if data is not None:
            write_json(cache_path, data)
        return data, error, status, False


def field_record(
    *,
    field: str,
    value: Any,
    source: str,
    source_type: str,
    source_url: str,
    updated_at: str,
    confidence: str,
    fallback_used: bool,
    data_gap: str | None = None,
) -> dict[str, Any]:
    return {
        "field": field,
        "value": value,
        "source": source,
        "source_type": source_type,
        "source_url": source_url,
        "updated_at": updated_at,
        "confidence": confidence,
        "fallback_used": fallback_used,
        "data_gap": data_gap,
    }


def health_record(source: str, status: str, message: str, impact: str, updated_at: str) -> dict[str, Any]:
    return {
        "source": source,
        "status": status,
        "message": message[:240],
        "impact": impact,
        "updated_at": updated_at,
    }


def symbol_to_parts(symbol: str) -> tuple[str, str]:
    value = str(symbol or "").strip().upper()
    if "." in value:
        market, code = value.split(".", 1)
        return market, code
    return "US", value


def normalize_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if re.match(r"^(US|HK|CN|SH|SZ)\.", text):
        return text
    if re.match(r"^[A-Z][A-Z0-9.\-]{0,8}$", text):
        return f"US.{text}"
    return text


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_jsonp(text: str | None) -> Any:
    if not text:
        return None
    match = re.match(r"^[^(]*\((.*)\)\s*;?\s*$", text.strip(), flags=re.S)
    raw = match.group(1) if match else text
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def date_label_from_epoch_ms(value: Any) -> str | None:
    parsed = number(value)
    if parsed is None:
        return None
    try:
        return datetime.fromtimestamp(parsed / 1000, tz=beijing_timezone()).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return None


def symbol_code(symbol: str) -> str:
    market, code = symbol_to_parts(symbol)
    if market == "HK":
        digits = re.sub(r"\D", "", code)
        return digits.zfill(5) if digits else code
    return re.sub(r"\D", "", code) or code


def collect_symbols(*payloads: dict[str, Any], limit: int = 60) -> list[str]:
    found: list[str] = []

    def add(value: Any) -> None:
        symbol = normalize_symbol(value)
        if symbol and symbol not in found:
            found.append(symbol)

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for row in payload.get("research_candidates", []) if isinstance(payload.get("research_candidates"), list) else []:
            if isinstance(row, dict):
                add(row.get("ticker") or row.get("symbol") or row.get("code"))
        for row in payload.get("buyable_now", []) if isinstance(payload.get("buyable_now"), list) else []:
            if isinstance(row, dict):
                add(row.get("ticker") or row.get("symbol") or row.get("code"))
        for row in payload.get("symbols", []) if isinstance(payload.get("symbols"), list) else []:
            if isinstance(row, dict):
                add(row.get("symbol") or row.get("code"))
        for row in payload.get("secondary_research_candidates", []) if isinstance(payload.get("secondary_research_candidates"), list) else []:
            if isinstance(row, dict):
                add(row.get("code"))
        for row in payload.get("secondary_candidates", []) if isinstance(payload.get("secondary_candidates"), list) else []:
            if isinstance(row, dict):
                add(row.get("code"))
        records = payload.get("records")
        if isinstance(records, dict):
            for row in records.values():
                if isinstance(row, dict):
                    add(row.get("code"))
        elif isinstance(records, list):
            for row in records:
                if isinstance(row, dict):
                    add(row.get("code"))
        for theme in payload.get("top_opportunities", []) if isinstance(payload.get("top_opportunities"), list) else []:
            for row in theme.get("top_candidates", []) if isinstance(theme, dict) and isinstance(theme.get("top_candidates"), list) else []:
                if isinstance(row, dict):
                    add(row.get("code"))
        for theme in payload.get("themes", []) if isinstance(payload.get("themes"), list) else []:
            if not isinstance(theme, dict):
                continue
            for key in ("securities", "secondary_research_candidates"):
                for row in theme.get(key, []) if isinstance(theme.get(key), list) else []:
                    if isinstance(row, dict):
                        add(row.get("code"))
            for layer in theme.get("layers", []) if isinstance(theme.get("layers"), list) else []:
                if isinstance(layer, dict):
                    for row in layer.get("leaders", []) if isinstance(layer.get("leaders"), list) else []:
                        if isinstance(row, dict):
                            add(row.get("code"))
    return found[:limit]


def sec_ticker_maps(fetcher: Fetcher) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tickers_url = "https://www.sec.gov/files/company_tickers.json"
    exchanges_url = "https://www.sec.gov/files/company_tickers_exchange.json"
    tickers, err1, status1, cached1 = fetcher.cached_json(CACHE_DIR / "sec_company_tickers.json", tickers_url, sec=True)
    exchanges, err2, status2, cached2 = fetcher.cached_json(CACHE_DIR / "sec_company_tickers_exchange.json", exchanges_url, sec=True)

    mapping: dict[str, dict[str, Any]] = {}
    if isinstance(tickers, dict):
        for item in tickers.values():
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").upper()
            if ticker:
                mapping[ticker] = {
                    "ticker": ticker,
                    "cik": str(item.get("cik_str") or "").zfill(10),
                    "name": item.get("title"),
                    "source_url": tickers_url,
                }
    if isinstance(exchanges, dict) and isinstance(exchanges.get("data"), list) and isinstance(exchanges.get("fields"), list):
        fields = list(exchanges.get("fields") or [])
        for row in exchanges.get("data", []):
            if not isinstance(row, list):
                continue
            item = dict(zip(fields, row))
            ticker = str(item.get("ticker") or "").upper()
            if ticker:
                target = mapping.setdefault(ticker, {"ticker": ticker, "source_url": exchanges_url})
                target["exchange"] = item.get("exchange")
                target["name"] = target.get("name") or item.get("name")
                target["cik"] = str(target.get("cik") or item.get("cik") or "").zfill(10)

    records = [
        field_record(
            field="symbol_mapping.sec_company_tickers",
            value={"mapped_symbols": len(mapping), "cached": cached1},
            source="SEC",
            source_type=SOURCE_TYPE_OFFICIAL,
            source_url=tickers_url,
            updated_at=iso(now_local()),
            confidence="high" if mapping else "low",
            fallback_used=True,
            data_gap=None if mapping else err1 or "SEC ticker file unavailable",
        ),
        field_record(
            field="symbol_mapping.sec_company_tickers_exchange",
            value={"available": bool(exchanges), "cached": cached2},
            source="SEC",
            source_type=SOURCE_TYPE_OFFICIAL,
            source_url=exchanges_url,
            updated_at=iso(now_local()),
            confidence="high" if exchanges else "low",
            fallback_used=True,
            data_gap=None if exchanges else err2 or "SEC exchange ticker file unavailable",
        ),
    ]
    health = [
        health_record(
            "SEC EDGAR",
            "normal" if mapping else "error",
            "SEC ticker mapping available." if mapping else f"SEC ticker mapping failed: {err1 or err2 or status1 or status2}",
            "Impacts US official filing/companyfacts fallback.",
            iso(now_local()),
        )
    ]
    return mapping, records, health


def sec_recent_filings(submissions: dict[str, Any]) -> list[dict[str, Any]]:
    recent = submissions.get("filings", {}).get("recent", {}) if isinstance(submissions, dict) else {}
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accession = recent.get("accessionNumber", [])
    primary = recent.get("primaryDocument", [])
    rows: list[dict[str, Any]] = []
    for form, filed, acc, doc in zip(forms, dates, accession, primary):
        if form in {"10-K", "10-Q", "20-F", "40-F", "8-K"}:
            rows.append({"form": form, "filed": filed, "accession": acc, "primary_document": doc})
        if len(rows) >= 8:
            break
    return rows


def companyfacts_metrics(facts: dict[str, Any]) -> dict[str, Any]:
    gaap = facts.get("facts", {}).get("us-gaap", {}) if isinstance(facts, dict) else {}

    def latest(tags: list[str], forms: set[str]) -> dict[str, Any] | None:
        entries: list[dict[str, Any]] = []
        for tag in tags:
            units = gaap.get(tag, {}).get("units", {}) if isinstance(gaap.get(tag), dict) else {}
            for unit_rows in units.values():
                if not isinstance(unit_rows, list):
                    continue
                for row in unit_rows:
                    if row.get("form") in forms and isinstance(row.get("val"), (int, float)):
                        entries.append(
                            {
                                "tag": tag,
                                "val": row.get("val"),
                                "fy": row.get("fy"),
                                "fp": row.get("fp"),
                                "form": row.get("form"),
                                "filed": row.get("filed"),
                                "end": row.get("end"),
                            }
                        )
        entries.sort(key=lambda x: (str(x.get("filed") or ""), str(x.get("end") or "")), reverse=True)
        return entries[0] if entries else None

    annual = {"10-K", "20-F", "40-F"}
    recent = {"10-K", "10-Q", "20-F", "40-F"}
    revenue = latest(["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"], annual)
    net_income = latest(["NetIncomeLoss", "ProfitLoss"], annual)
    assets = latest(["Assets"], recent)
    liabilities = latest(["Liabilities"], recent)
    metrics: dict[str, Any] = {
        "latest_annual_revenue": revenue,
        "latest_annual_net_income": net_income,
        "latest_assets": assets,
        "latest_liabilities": liabilities,
    }
    if revenue and net_income and number(revenue.get("val")):
        metrics["net_margin"] = round(float(net_income["val"]) / float(revenue["val"]), 4)
    if assets and liabilities and number(assets.get("val")):
        metrics["liabilities_to_assets"] = round(float(liabilities["val"]) / float(assets["val"]), 4)
    return metrics


def collect_sec_layer(fetcher: Fetcher, symbols: list[str], max_symbols: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    mapping, records, health = sec_ticker_maps(fetcher)
    us_symbols = [symbol for symbol in symbols if symbol.startswith("US.")][:max_symbols]
    gaps: list[dict[str, Any]] = []
    success_count = 0
    for symbol in us_symbols:
        _, ticker = symbol_to_parts(symbol)
        meta = mapping.get(ticker)
        if not meta or not meta.get("cik"):
            gaps.append({"symbol": symbol, "source": "SEC", "data_gap": "No SEC CIK mapping"})
            continue
        cik = str(meta["cik"]).zfill(10)
        submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        submissions, sub_error, sub_status = fetcher.http_json(submissions_url, sec=True, timeout=10)
        facts, fact_error, fact_status = fetcher.http_json(facts_url, sec=True, timeout=10)
        filings = sec_recent_filings(submissions if isinstance(submissions, dict) else {})
        metrics = companyfacts_metrics(facts if isinstance(facts, dict) else {})
        records.append(
            field_record(
                field=f"{symbol}.sec.submissions",
                value={"cik": cik, "company": meta.get("name"), "recent_filings": filings},
                source="SEC",
                source_type=SOURCE_TYPE_OFFICIAL,
                source_url=submissions_url,
                updated_at=iso(now_local()),
                confidence="high" if filings else "low",
                fallback_used=True,
                data_gap=None if filings else sub_error or f"HTTP {sub_status}" if sub_status else "SEC submissions empty",
            )
        )
        records.append(
            field_record(
                field=f"{symbol}.sec.companyfacts",
                value=metrics,
                source="SEC",
                source_type=SOURCE_TYPE_OFFICIAL,
                source_url=facts_url,
                updated_at=iso(now_local()),
                confidence="high" if any(metrics.values()) else "low",
                fallback_used=True,
                data_gap=None if any(metrics.values()) else fact_error or f"HTTP {fact_status}" if fact_status else "SEC companyfacts empty",
            )
        )
        if filings or any(metrics.values()):
            success_count += 1
        if sub_error:
            gaps.append({"symbol": symbol, "source": "SEC", "data_gap": sub_error})
        if fact_error:
            gaps.append({"symbol": symbol, "source": "SEC", "data_gap": fact_error})
    health.append(
        health_record(
            "SEC EDGAR company data",
            "normal" if success_count else ("unknown" if not us_symbols else "limited"),
            f"SEC fallback returned data for {success_count}/{len(us_symbols)} checked US symbols.",
            "Impacts official US filing and financial statement evidence.",
            iso(now_local()),
        )
    )
    return records, health, gaps


def fetch_fred_series(fetcher: Fetcher, api_key: str, series_id: str) -> tuple[dict[str, Any] | None, str | None, int | None]:
    params = urllib.parse.urlencode(
        {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 8,
        }
    )
    url = f"https://api.stlouisfed.org/fred/series/observations?{params}"
    data, error, status = fetcher.http_json(url, timeout=12)
    if not isinstance(data, dict):
        return None, error, status
    observations = data.get("observations") if isinstance(data.get("observations"), list) else []
    values = [row for row in observations if row.get("value") not in {None, "."}]
    if not values:
        return None, "FRED returned no observations", status
    latest = values[0]
    return {
        "series_id": series_id,
        "latest_date": latest.get("date"),
        "latest_value": number(latest.get("value")),
        "observation_count": len(values),
    }, None, status


def collect_fred_layer(fetcher: Fetcher) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    api_key = env_key("FRED_API_KEY").lower()
    updated = iso(now_local())
    if not api_key:
        return [], [
            health_record("FRED", "unknown", "FRED_API_KEY is not configured.", "Macro/liquidity fallback disabled.", updated)
        ], [{"source": "FRED", "data_gap": "FRED_API_KEY is not configured"}]
    records: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    success = 0
    for series_id in FRED_SERIES:
        params = urllib.parse.urlencode({"series_id": series_id, "file_type": "json"})
        source_url = f"https://api.stlouisfed.org/fred/series/observations?{params}"
        value, error, status = fetch_fred_series(fetcher, api_key, series_id)
        records.append(
            field_record(
                field=f"macro.{series_id}",
                value=value,
                source="FRED",
                source_type=SOURCE_TYPE_OFFICIAL,
                source_url=source_url,
                updated_at=updated,
                confidence="high" if value else "low",
                fallback_used=True,
                data_gap=None if value else error or f"HTTP {status}" if status else "FRED unavailable",
            )
        )
        if value:
            success += 1
        else:
            gaps.append({"source": "FRED", "field": series_id, "data_gap": error or f"HTTP {status}" if status else "FRED unavailable"})
    return records, [
        health_record(
            "FRED",
            "normal" if success == len(FRED_SERIES) else ("limited" if success else "error"),
            f"FRED returned {success}/{len(FRED_SERIES)} configured macro/liquidity series.",
            "Impacts macro regime, rates, inflation, labor and credit spread evidence.",
            updated,
        )
    ], gaps


def alpha_request(fetcher: Fetcher, api_key: str, function: str, **params: Any) -> tuple[Any | None, str | None, int | None, str]:
    global ALPHA_LAST_REQUEST_AT
    query = {"function": function, "apikey": api_key, **{k: v for k, v in params.items() if v not in (None, "")}}
    safe_query = dict(query)
    safe_query["apikey"] = "REDACTED"
    url = f"https://www.alphavantage.co/query?{urllib.parse.urlencode(query)}"
    safe_url = f"https://www.alphavantage.co/query?{urllib.parse.urlencode(safe_query)}"
    delay = max(0.0, number(os.getenv("ALPHA_REQUEST_DELAY_SECONDS", "1.25")) or 0.0)
    elapsed = time.monotonic() - ALPHA_LAST_REQUEST_AT
    if delay and elapsed < delay:
        time.sleep(delay - elapsed)
    text, error, status = fetcher.http_text(url, timeout=18)
    ALPHA_LAST_REQUEST_AT = time.monotonic()
    if text is None:
        return None, error, status, safe_url
    stripped = text.strip()
    if not stripped:
        return None, "Alpha Vantage empty response", status, safe_url
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            if data.get("Note") or data.get("Information"):
                return None, str(data.get("Note") or data.get("Information")), status, safe_url
            if data.get("Error Message"):
                return None, str(data.get("Error Message")), status, safe_url
        return data, None, status, safe_url
    except json.JSONDecodeError:
        pass
    if "," in stripped and "\n" in stripped:
        rows = list(csv.DictReader(io.StringIO(stripped)))
        return {"format": "csv", "rows": rows[:200], "row_count": len(rows)}, None, status, safe_url
    if "Thank you for using Alpha Vantage" in stripped or "rate limit" in stripped.lower():
        return None, stripped[:260], status, safe_url
    return None, f"Unexpected Alpha Vantage response: {stripped[:220]}", status, safe_url


def latest_quarter_from_earnings(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    rows = data.get("quarterlyEarnings")
    if not isinstance(rows, list) or not rows:
        return None
    date = str(rows[0].get("fiscalDateEnding") or "")
    if len(date) >= 7:
        year = date[:4]
        month = int(date[5:7])
        quarter = ((month - 1) // 3) + 1
        return f"{year}Q{quarter}"
    return None


def prioritized_alpha_symbols(symbols: list[str], fmp_research: dict[str, Any], max_symbols: int) -> list[str]:
    if max_symbols <= 0:
        return []
    fmp_rows = {}
    for row in fmp_research.get("symbols", []) if isinstance(fmp_research.get("symbols"), list) else []:
        if isinstance(row, dict) and row.get("symbol"):
            fmp_rows[f"US.{str(row.get('symbol')).upper()}"] = row

    prioritized: list[str] = []

    def add(symbol: str) -> None:
        if symbol.startswith("US.") and symbol not in prioritized:
            prioritized.append(symbol)

    for symbol, row in fmp_rows.items():
        annual = row.get("annual_estimate") if isinstance(row.get("annual_estimate"), dict) else {}
        surprise = row.get("latest_earnings_surprise") if isinstance(row.get("latest_earnings_surprise"), dict) else {}
        if row.get("coverage_status") in {"restricted", "rate_limited", "unavailable", "empty"} or not annual or not surprise:
            add(symbol)

    for symbol in symbols:
        if symbol.startswith("US.") and symbol not in fmp_rows:
            add(symbol)

    for symbol in symbols:
        add(symbol)

    return prioritized[:max_symbols]


def collect_alpha_layer(
    fetcher: Fetcher,
    symbols: list[str],
    max_symbols: int,
    fmp_research: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    api_key = env_key("ALPHA_VANTAGE_API_KEY", "ALPHAVANTAGE_API_KEY", "AV_API_KEY")
    updated = iso(now_local())
    if not api_key:
        return [], [
            health_record(
                "Alpha Vantage",
                "unknown",
                "ALPHA_VANTAGE_API_KEY is not configured.",
                "Earnings estimates, surprise, calendar and transcript fallback disabled.",
                updated,
            )
        ], [{"source": "Alpha Vantage", "data_gap": "ALPHA_VANTAGE_API_KEY is not configured"}]

    us_symbols = prioritized_alpha_symbols(symbols, fmp_research, max_symbols)
    records: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    limited = False
    success = 0

    calendar, calendar_error, calendar_status, calendar_url = alpha_request(fetcher, api_key, "EARNINGS_CALENDAR", horizon="3month")
    records.append(
        field_record(
            field="earnings.calendar",
            value=calendar if calendar and not isinstance(calendar, str) else None,
            source="Alpha Vantage",
            source_type=SOURCE_TYPE_VENDOR,
            source_url=calendar_url,
            updated_at=updated,
            confidence="medium" if calendar else "low",
            fallback_used=True,
            data_gap=None if calendar else calendar_error or f"HTTP {calendar_status}" if calendar_status else "Alpha Vantage earnings calendar unavailable",
        )
    )
    if calendar:
        success += 1
    else:
        limited = True
        gaps.append({"source": "Alpha Vantage", "field": "earnings.calendar", "data_gap": calendar_error or "empty calendar"})

    for symbol in us_symbols:
        _, ticker = symbol_to_parts(symbol)
        earnings, error, status, safe_url = alpha_request(fetcher, api_key, "EARNINGS", symbol=ticker)
        quarter = latest_quarter_from_earnings(earnings)
        records.append(
            field_record(
                field=f"{symbol}.earnings.actuals_surprise",
                value=earnings if isinstance(earnings, dict) else None,
                source="Alpha Vantage",
                source_type=SOURCE_TYPE_VENDOR,
                source_url=safe_url,
                updated_at=updated,
                confidence="medium" if isinstance(earnings, dict) else "low",
                fallback_used=True,
                data_gap=None if isinstance(earnings, dict) else error or f"HTTP {status}" if status else "Alpha Vantage earnings unavailable",
            )
        )
        if isinstance(earnings, dict):
            success += 1
        else:
            limited = True
            gaps.append({"symbol": symbol, "source": "Alpha Vantage", "field": "EARNINGS", "data_gap": error or "empty earnings"})

        estimates, est_error, est_status, est_url = alpha_request(fetcher, api_key, "EARNINGS_ESTIMATES", symbol=ticker)
        records.append(
            field_record(
                field=f"{symbol}.earnings.estimates",
                value=estimates if isinstance(estimates, dict) else None,
                source="Alpha Vantage",
                source_type=SOURCE_TYPE_VENDOR,
                source_url=est_url,
                updated_at=updated,
                confidence="medium" if isinstance(estimates, dict) else "low",
                fallback_used=True,
                data_gap=None if isinstance(estimates, dict) else est_error or f"HTTP {est_status}" if est_status else "Alpha Vantage estimates unavailable",
            )
        )
        if isinstance(estimates, dict):
            success += 1
        else:
            limited = True
            gaps.append({"symbol": symbol, "source": "Alpha Vantage", "field": "EARNINGS_ESTIMATES", "data_gap": est_error or "empty estimates"})

        if quarter and os.getenv("ALPHA_FETCH_TRANSCRIPTS", "").strip().lower() in {"1", "true", "yes"}:
            transcript, tr_error, tr_status, tr_url = alpha_request(
                fetcher,
                api_key,
                "EARNINGS_CALL_TRANSCRIPT",
                symbol=ticker,
                quarter=quarter,
            )
            records.append(
                field_record(
                    field=f"{symbol}.earnings.transcript",
                    value=transcript if isinstance(transcript, dict) else None,
                    source="Alpha Vantage",
                    source_type=SOURCE_TYPE_VENDOR,
                    source_url=tr_url,
                    updated_at=updated,
                    confidence="medium" if isinstance(transcript, dict) else "low",
                    fallback_used=True,
                    data_gap=None if isinstance(transcript, dict) else tr_error or f"HTTP {tr_status}" if tr_status else "Alpha Vantage transcript unavailable",
                )
            )
            if isinstance(transcript, dict):
                success += 1
            else:
                limited = True
                gaps.append({"symbol": symbol, "source": "Alpha Vantage", "field": "EARNINGS_CALL_TRANSCRIPT", "data_gap": tr_error or "empty transcript"})

    return records, [
        health_record(
            "Alpha Vantage",
            "limited" if limited else "normal",
            f"Alpha Vantage fallback completed with {success} successful response blocks; limited/empty responses are kept as data gaps.",
            "Impacts earnings estimates, surprise, calendar and transcript fallback confidence.",
            updated,
        )
    ], gaps


def collect_openfigi_layer(fetcher: Fetcher, symbols: list[str], max_symbols: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    api_key = env_key("OPENFIGI_API_KEY")
    updated = iso(now_local())
    candidates = [symbol for symbol in symbols if symbol.startswith(("US.", "HK.", "SH.", "SZ."))][:max_symbols]
    if not api_key:
        return [], [
            health_record("OpenFIGI", "unknown", "OPENFIGI_API_KEY is not configured.", "Security identifier mapping fallback is limited to SEC ticker files.", updated)
        ], [{"source": "OpenFIGI", "data_gap": "OPENFIGI_API_KEY is not configured"}]
    jobs = []
    for symbol in candidates:
        market, code = symbol_to_parts(symbol)
        exch = {"US": "US", "HK": "HK", "SH": "CH", "SZ": "CH"}.get(market, market)
        jobs.append({"idType": "TICKER", "idValue": code, "exchCode": exch})
    if not jobs:
        return [], [health_record("OpenFIGI", "unknown", "No symbols available for OpenFIGI mapping.", "No mapping attempted.", updated)], []
    body = json.dumps(jobs).encode("utf-8")
    data, error, status = fetcher.http_json(
        "https://api.openfigi.com/v3/mapping",
        method="POST",
        body=body,
        headers={"Content-Type": "application/json", "X-OPENFIGI-APIKEY": api_key},
        timeout=20,
    )
    records: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    if isinstance(data, list):
        for symbol, result in zip(candidates, data):
            value = result.get("data", [])[:3] if isinstance(result, dict) else None
            records.append(
                field_record(
                    field=f"{symbol}.mapping.openfigi",
                    value=value,
                    source="OpenFIGI",
                    source_type=SOURCE_TYPE_OPEN,
                    source_url="https://api.openfigi.com/v3/mapping",
                    updated_at=updated,
                    confidence="medium" if value else "low",
                    fallback_used=True,
                    data_gap=None if value else clean_openfigi_error(result),
                )
            )
            if not value:
                gaps.append({"symbol": symbol, "source": "OpenFIGI", "data_gap": clean_openfigi_error(result)})
    return records, [
        health_record(
            "OpenFIGI",
            "normal" if records and not gaps else ("limited" if records else "error"),
            f"OpenFIGI mapped {len(records) - len(gaps)}/{len(candidates)} requested symbols." if records else f"OpenFIGI failed: {error or status}",
            "Impacts cross-market symbol mapping only; does not replace official filings.",
            updated,
        )
    ], gaps or ([{"source": "OpenFIGI", "data_gap": error or f"HTTP {status}"}] if error or status not in {200, None} else [])


def clean_openfigi_error(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("error") or result.get("warning") or "OpenFIGI mapping empty")[:220]
    return "OpenFIGI mapping empty"


def yahoo_symbol(symbol: str) -> str | None:
    market, ticker = symbol_to_parts(symbol)
    if market == "US":
        return ticker
    if market == "HK":
        hk_code = ticker.lstrip("0") or ticker
        return f"{hk_code.zfill(4)}.HK"
    if market == "SH":
        return f"{ticker}.SS"
    if market == "SZ":
        return f"{ticker}.SZ"
    return None


def avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def yahoo_chart_value(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    chart = data.get("chart") if isinstance(data.get("chart"), dict) else {}
    results = chart.get("result") if isinstance(chart.get("result"), list) else []
    if not results or not isinstance(results[0], dict):
        return None
    result = results[0]
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    indicators = result.get("indicators") if isinstance(result.get("indicators"), dict) else {}
    quotes = indicators.get("quote") if isinstance(indicators.get("quote"), list) else []
    quote = quotes[0] if quotes and isinstance(quotes[0], dict) else {}
    closes = [parsed for value in quote.get("close", []) if (parsed := number(value)) is not None]
    lows = [parsed for value in quote.get("low", []) if (parsed := number(value)) is not None]
    highs = [parsed for value in quote.get("high", []) if (parsed := number(value)) is not None]
    price = number(meta.get("regularMarketPrice")) or (closes[-1] if closes else None)
    if price is None:
        return None
    timestamps = result.get("timestamp") if isinstance(result.get("timestamp"), list) else []
    quote_time = None
    if timestamps:
        try:
            quote_time = datetime.fromtimestamp(int(timestamps[-1]), tz=timezone.utc).astimezone(beijing_timezone()).isoformat(timespec="seconds")
        except (ValueError, OSError, OverflowError):
            quote_time = None
    low20 = min(lows[-20:]) if len(lows) >= 5 else None
    low60 = min(lows[-60:]) if len(lows) >= 20 else low20
    low252 = min(lows[-252:]) if lows else None
    high252 = max(highs[-252:]) if highs else None
    invalidation_candidates = [value for value in (low60, low20, low252) if value is not None and value < price]
    invalidation = min(invalidation_candidates) if invalidation_candidates else None
    mechanical_target = high252 if high252 is not None and high252 > price else None
    reward_risk = None
    if invalidation is not None and mechanical_target is not None:
        risk = price - invalidation
        reward = mechanical_target - price
        if risk > 0 and reward > 0:
            reward_risk = round(reward / risk, 2)
    return {
        "price": round(price, 4),
        "currency": meta.get("currency"),
        "quote_time": quote_time,
        "exchange": meta.get("exchangeName") or meta.get("fullExchangeName"),
        "ma50": avg(closes[-50:]) if len(closes) >= 20 else None,
        "ma200": avg(closes[-200:]) if len(closes) >= 80 else None,
        "low20": round(low20, 4) if low20 is not None else None,
        "low60": round(low60, 4) if low60 is not None else None,
        "low252": round(low252, 4) if low252 is not None else None,
        "high252": round(high252, 4) if high252 is not None else None,
        "invalidation": round(invalidation, 4) if invalidation is not None else None,
        "mechanical_target": round(mechanical_target, 4) if mechanical_target is not None else None,
        "reward_risk": reward_risk,
        "bar_count": len(closes),
    }


def collect_yahoo_quote_layer(fetcher: Fetcher, symbols: list[str], max_symbols: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    updated = iso(now_local())
    candidates = [symbol for symbol in symbols if symbol.startswith(("HK.", "SH.", "SZ.", "CN."))][:max(0, max_symbols)]
    records: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    success = 0
    for symbol in candidates:
        mapped = yahoo_symbol(symbol)
        if not mapped:
            gaps.append({"symbol": symbol, "source": "Yahoo Finance", "data_gap": "No Yahoo symbol mapping"})
            continue
        safe = urllib.parse.quote(mapped, safe="")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{safe}?range=1y&interval=1d&includePrePost=false"
        data, error, status = fetcher.http_json(url, timeout=15)
        value = yahoo_chart_value(data)
        records.append(
            field_record(
                field=f"{symbol}.quote.yahoo_chart",
                value=value,
                source="Yahoo Finance",
                source_type=SOURCE_TYPE_OPEN,
                source_url=f"https://finance.yahoo.com/quote/{safe}",
                updated_at=updated,
                confidence="medium" if value else "low",
                fallback_used=True,
                data_gap=None if value else error or f"HTTP {status}" if status else "Yahoo chart unavailable",
            )
        )
        if value:
            success += 1
        else:
            gaps.append({"symbol": symbol, "source": "Yahoo Finance", "field": "quote.yahoo_chart", "data_gap": error or f"HTTP {status}" if status else "Yahoo chart unavailable"})
    return records, [
        health_record(
            "Yahoo Finance",
            "normal" if success == len(candidates) else ("limited" if success else "unknown" if not candidates else "error"),
            f"Yahoo public chart fallback returned {success}/{len(candidates)} HK/A quote blocks.",
            "Impacts HK/A price, technical levels and mechanical R/R fallback; not official filings.",
            updated,
        )
    ], gaps


def cninfo_stock_map(fetcher: Fetcher) -> tuple[dict[str, dict[str, Any]], str | None, int | None]:
    url = "http://www.cninfo.com.cn/new/data/szse_stock.json"
    data, error, status, _cached = fetcher.cached_json(CACHE_DIR / "cninfo_szse_stock.json", url, max_age_hours=24 * 7)
    mapping: dict[str, dict[str, Any]] = {}
    rows = data.get("stockList") if isinstance(data, dict) and isinstance(data.get("stockList"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        org_id = str(row.get("orgId") or "").strip()
        if code and org_id:
            mapping[code] = row
    return mapping, error, status


def cninfo_announcements(fetcher: Fetcher, symbol: str, mapping: dict[str, dict[str, Any]], now: datetime) -> tuple[dict[str, Any] | None, str | None, str]:
    code = symbol_code(symbol)
    item = mapping.get(code)
    if not item:
        return None, "CNINFO stock mapping not found", "http://www.cninfo.com.cn/"
    org_id = str(item.get("orgId") or "").strip()
    column = "sse" if symbol.startswith("SH.") or code.startswith("6") else "szse"
    start = (now - timedelta(days=370)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")
    url = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
    body = urllib.parse.urlencode(
        {
            "stock": f"{code},{org_id}",
            "tabName": "fulltext",
            "pageSize": "10",
            "pageNum": "1",
            "column": column,
            "category": "category_ndbg_szsh;category_bndbg_szsh;category_yjdbg_szsh;category_sjdbg_szsh;category_yjygjxz_szsh;category_yjkb_szsh",
            "seDate": f"{start}~{end}",
            "isHLtitle": "true",
        }
    ).encode("utf-8")
    headers = {
        "Origin": "http://www.cninfo.com.cn",
        "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
    }
    data, error, status = fetcher.http_json(url, headers=headers, method="POST", body=body, timeout=15)
    if not isinstance(data, dict):
        return None, error or (f"HTTP {status}" if status else "CNINFO unavailable"), url
    rows = data.get("announcements") if isinstance(data.get("announcements"), list) else []
    announcements: list[dict[str, Any]] = []
    for row in rows[:10]:
        if not isinstance(row, dict):
            continue
        title = clean_text(row.get("announcementTitle"))
        adjunct_url = str(row.get("adjunctUrl") or "").lstrip("/")
        file_url = f"http://static.cninfo.com.cn/{adjunct_url}" if adjunct_url else None
        announcements.append(
            {
                "title": title,
                "date": date_label_from_epoch_ms(row.get("announcementTime")),
                "url": file_url,
                "category": clean_text(row.get("announcementTypeName")),
                "source_id": row.get("announcementId"),
                "financial_related": any(keyword in title for keyword in CNINFO_FINANCIAL_KEYWORDS),
            }
        )
    if not announcements:
        return None, "CNINFO returned no recent financial/announcement records", url
    value = {
        "symbol": symbol,
        "code": code,
        "company": item.get("zwjc"),
        "org_id": org_id,
        "total": data.get("totalAnnouncement") or len(announcements),
        "financial_hits": sum(1 for row in announcements if row.get("financial_related")),
        "announcements": announcements,
    }
    source_url = f"http://www.cninfo.com.cn/new/disclosure/stock?stockCode={code}&orgId={org_id}"
    return value, None, source_url


def collect_cninfo_layer(fetcher: Fetcher, symbols: list[str], max_symbols: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    updated = iso(now_local())
    now = now_local()
    candidates = [symbol for symbol in symbols if symbol.startswith(("SH.", "SZ.", "CN."))][:max(0, max_symbols)]
    mapping, map_error, map_status = cninfo_stock_map(fetcher)
    records: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    success = 0
    for symbol in candidates:
        value, error, source_url = cninfo_announcements(fetcher, symbol, mapping, now)
        records.append(
            field_record(
                field=f"{symbol}.official_disclosure",
                value=value,
                source="CNINFO",
                source_type=SOURCE_TYPE_OFFICIAL,
                source_url=source_url,
                updated_at=updated,
                confidence="high" if value else "low",
                fallback_used=True,
                data_gap=None if value else error,
            )
        )
        if value:
            success += 1
        else:
            gaps.append({"symbol": symbol, "source": "CNINFO", "data_gap": error or map_error or (f"HTTP {map_status}" if map_status else "CNINFO official disclosure unavailable")})
        time.sleep(0.15)
    status = "normal" if success == len(candidates) else ("limited" if success else "unknown" if not candidates else "error")
    return records, [
        health_record(
            "CNINFO",
            status,
            f"CNINFO official disclosure fallback returned {success}/{len(candidates)} A-share records.",
            "Impacts A-share announcement/financial statement confidence; official CNINFO metadata is used when available.",
            updated,
        )
    ], gaps


def hkex_lookup_stock(fetcher: Fetcher, code: str) -> tuple[dict[str, Any] | None, str | None]:
    query = urllib.parse.urlencode(
        {
            "lang": "EN",
            "type": "A",
            "name": code,
            "market": "SEHK",
            "callback": "callback",
        }
    )
    url = f"https://www1.hkexnews.hk/search/prefix.do?{query}"
    text, error, status = fetcher.http_text(
        url,
        headers={"Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en"},
        timeout=15,
    )
    data = parse_jsonp(text)
    rows = data.get("stockInfo") if isinstance(data, dict) and isinstance(data.get("stockInfo"), list) else []
    for row in rows:
        if isinstance(row, dict) and str(row.get("code") or "").zfill(5) == code:
            return row, None
    return None, error or (f"HTTP {status}" if status else "HKEXnews stock mapping not found")


def hkex_announcements(fetcher: Fetcher, symbol: str, now: datetime) -> tuple[dict[str, Any] | None, str | None, str]:
    code = symbol_code(symbol)
    stock, lookup_error = hkex_lookup_stock(fetcher, code)
    if not stock:
        return None, lookup_error or "HKEXnews stock mapping not found", "https://www.hkexnews.hk/"
    stock_id = str(stock.get("stockId") or "")
    start = (now - timedelta(days=370)).strftime("%Y%m%d")
    end = now.strftime("%Y%m%d")
    params = {
        "sortDir": "0",
        "sortByOptions": "DateTime",
        "category": "0",
        "market": "SEHK",
        "stockId": stock_id,
        "documentType": "-1",
        "fromDate": start,
        "toDate": end,
        "title": "",
        "searchType": "0",
        "t1code": "-2",
        "t2Gcode": "-2",
        "t2code": "-2",
        "rowRange": "20",
        "lang": "E",
    }
    url = f"https://www1.hkexnews.hk/search/titleSearchServlet.do?{urllib.parse.urlencode(params)}"
    data, error, status = fetcher.http_json(
        url,
        headers={
            "Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=20,
    )
    if not isinstance(data, dict):
        return None, error or (f"HTTP {status}" if status else "HKEXnews unavailable"), url
    raw_result = data.get("result")
    if isinstance(raw_result, str):
        if raw_result.strip().lower() == "null":
            rows = []
        else:
            try:
                rows = json.loads(raw_result)
            except json.JSONDecodeError:
                rows = []
    else:
        rows = raw_result if isinstance(raw_result, list) else []
    announcements: list[dict[str, Any]] = []
    for row in rows[:12]:
        if not isinstance(row, dict):
            continue
        title = clean_text(row.get("TITLE"))
        long_text = clean_text(row.get("LONG_TEXT") or row.get("SHORT_TEXT"))
        link = str(row.get("FILE_LINK") or "")
        file_url = f"https://www1.hkexnews.hk{link}" if link.startswith("/") else link or None
        lower_text = f"{title} {long_text}".lower()
        announcements.append(
            {
                "title": title,
                "date": row.get("DATE_TIME"),
                "url": file_url,
                "category": long_text,
                "file_type": row.get("FILE_TYPE"),
                "source_id": row.get("NEWS_ID"),
                "financial_related": any(keyword in lower_text for keyword in HKEX_FINANCIAL_KEYWORDS),
            }
        )
    if not announcements:
        return None, "HKEXnews returned no recent disclosure records", url
    value = {
        "symbol": symbol,
        "code": code,
        "company": stock.get("name"),
        "stock_id": stock_id,
        "total": data.get("recordCnt") or len(announcements),
        "financial_hits": sum(1 for row in announcements if row.get("financial_related")),
        "announcements": announcements,
    }
    return value, None, url


def collect_hkex_layer(fetcher: Fetcher, symbols: list[str], max_symbols: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    updated = iso(now_local())
    now = now_local()
    candidates = [symbol for symbol in symbols if symbol.startswith("HK.")][:max(0, max_symbols)]
    records: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    success = 0
    for symbol in candidates:
        value, error, source_url = hkex_announcements(fetcher, symbol, now)
        records.append(
            field_record(
                field=f"{symbol}.official_disclosure",
                value=value,
                source="HKEXnews",
                source_type=SOURCE_TYPE_OFFICIAL,
                source_url=source_url,
                updated_at=updated,
                confidence="high" if value else "low",
                fallback_used=True,
                data_gap=None if value else error,
            )
        )
        if value:
            success += 1
        else:
            gaps.append({"symbol": symbol, "source": "HKEXnews", "data_gap": error or "HKEXnews official disclosure unavailable"})
        time.sleep(0.2)
    status = "normal" if success == len(candidates) else ("limited" if success else "unknown" if not candidates else "error")
    return records, [
        health_record(
            "HKEXnews",
            status,
            f"HKEXnews official disclosure fallback returned {success}/{len(candidates)} HK records.",
            "Impacts HK announcement/financial statement confidence; official HKEXnews metadata is used when available.",
            updated,
        )
    ], gaps


def collect_cn_hk_official_placeholders(symbols: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    updated = iso(now_local())
    records: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    cn_symbols = [symbol for symbol in symbols if symbol.startswith(("SH.", "SZ.", "CN."))]
    hk_symbols = [symbol for symbol in symbols if symbol.startswith("HK.")]
    for symbol in cn_symbols:
        records.append(
            field_record(
                field=f"{symbol}.official_disclosure",
                value=None,
                source="CNINFO",
                source_type=SOURCE_TYPE_OFFICIAL,
                source_url="http://www.cninfo.com.cn/",
                updated_at=updated,
                confidence="low",
                fallback_used=True,
                data_gap="CNINFO official disclosure connector is not configured; use AkShare/Tushare fallback only with downgraded confidence.",
            )
        )
        gaps.append({"symbol": symbol, "source": "CNINFO", "data_gap": "CNINFO official disclosure connector is not configured"})
    for symbol in hk_symbols:
        records.append(
            field_record(
                field=f"{symbol}.official_disclosure",
                value=None,
                source="HKEXnews",
                source_type=SOURCE_TYPE_OFFICIAL,
                source_url="https://www.hkexnews.hk/",
                updated_at=updated,
                confidence="low",
                fallback_used=True,
                data_gap="HKEXnews official disclosure connector is not configured; use Futu/AkShare/Yahoo fallback only with downgraded confidence.",
            )
        )
        gaps.append({"symbol": symbol, "source": "HKEXnews", "data_gap": "HKEXnews official disclosure connector is not configured"})
    health = [
        health_record(
            "CNINFO",
            "unknown" if cn_symbols else "normal",
            "CNINFO connector is not configured; no official A-share announcement pull was attempted." if cn_symbols else "No A-share symbols requiring CNINFO fallback.",
            "Impacts A-share announcement/financial statement confidence.",
            updated,
        ),
        health_record(
            "HKEXnews",
            "unknown" if hk_symbols else "normal",
            "HKEXnews connector is not configured; no official HK announcement pull was attempted." if hk_symbols else "No HK symbols requiring HKEXnews fallback.",
            "Impacts HK announcement/financial statement confidence.",
            updated,
        ),
    ]
    return records, health, gaps


def group_by_symbol(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        field = str(record.get("field") or "")
        symbol = field.split(".", 2)
        if len(symbol) >= 2 and symbol[0] in {"US", "HK", "SH", "SZ", "CN"}:
            key = f"{symbol[0]}.{symbol[1]}"
            output.setdefault(key, []).append(record)
    return dict(sorted(output.items()))


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    load_environment()
    now = now_local()
    fetcher = Fetcher()
    market_pack = load_json(args.market_pack, {})
    fmp_research = load_json(args.fmp_research, {})
    macro_regime = load_json(args.macro_regime, {})
    opportunity_radar = load_json(args.opportunity_radar, {})
    cross_market = load_json(args.cross_market, {})
    secondary_queue = load_json(args.secondary_queue, {})
    symbols = collect_symbols(
        market_pack,
        fmp_research,
        macro_regime,
        opportunity_radar,
        cross_market,
        secondary_queue,
        limit=max(1, int(args.symbol_limit)),
    )

    records: list[dict[str, Any]] = []
    health: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for layer_records, layer_health, layer_gaps in (
        collect_sec_layer(fetcher, symbols, max_symbols=max(0, int(args.max_sec_symbols))),
        collect_fred_layer(fetcher),
        collect_alpha_layer(fetcher, symbols, max_symbols=max(0, int(args.max_alpha_symbols)), fmp_research=fmp_research),
        collect_openfigi_layer(fetcher, symbols, max_symbols=max(0, int(args.max_openfigi_symbols))),
        collect_yahoo_quote_layer(fetcher, symbols, max_symbols=max(0, int(args.max_yahoo_symbols))),
        collect_cninfo_layer(fetcher, symbols, max_symbols=max(0, int(args.max_cninfo_symbols))),
        collect_hkex_layer(fetcher, symbols, max_symbols=max(0, int(args.max_hkex_symbols))),
    ):
        records.extend(layer_records)
        health.extend(layer_health)
        gaps.extend(layer_gaps)

    return {
        "schema_version": 1,
        "generated_at": iso(now),
        "generated_label": now.strftime("%Y-%m-%d %H:%M"),
        "privacy": "public-sanitized",
        "data_boundary": {
            "role": "free fallback evidence layer; source tracing only; not trading instruction",
            "api_keys": "loaded from GitHub Actions Secrets or local .env; never written to output",
            "discipline": "If fallback data is empty, rate-limited or unavailable, record data_gap and do not fabricate values.",
        },
        "source_priority": {
            "us_filings_financials": ["SEC EDGAR submissions", "SEC EDGAR companyfacts"],
            "macro_liquidity": ["FRED"],
            "earnings_expectations_surprise_calendar": ["Alpha Vantage vendor fallback"],
            "transcripts": ["Company IR or SEC 8-K attachment", "Alpha Vantage transcript fallback"],
            "a_share_disclosures": ["CNINFO official", "AkShare/Tushare open-source fallback"],
            "hk_disclosures": ["HKEXnews official", "Futu/AkShare/Yahoo fallback"],
            "hk_a_quotes_technicals": ["Yahoo public chart open-source fallback"],
            "symbol_mapping": ["SEC ticker files", "OpenFIGI"],
        },
        "symbols": symbols,
        "summary": {
            "symbol_count": len(symbols),
            "field_count": len(records),
            "data_gap_count": len(gaps),
            "health_normal_count": sum(1 for item in health if item.get("status") == "normal"),
            "health_limited_count": sum(1 for item in health if item.get("status") == "limited"),
            "health_error_count": sum(1 for item in health if item.get("status") == "error"),
            "health_unknown_count": sum(1 for item in health if item.get("status") == "unknown"),
        },
        "fields": records,
        "by_symbol": group_by_symbol(records),
        "data_health": health,
        "data_gaps": gaps[:500],
    }


def render_report(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        "# 免费数据源 fallback 雷达",
        "",
        f"- 生成时间：{payload.get('generated_label')}",
        "- 定位：当 FMP / Finnhub 权限不足、限流或失败时，提供官方/免费数据源的可追溯证据层。",
        "- 纪律：免费源不够稳定时只记录 data_gap，不编造数值；所有结论必须回到 source/source_type/confidence。",
        "",
        "## 概览",
        "",
        f"- 覆盖标的：{summary.get('symbol_count', 0)}；字段记录：{summary.get('field_count', 0)}；数据缺口：{summary.get('data_gap_count', 0)}。",
        f"- 健康状态：normal {summary.get('health_normal_count', 0)} / limited {summary.get('health_limited_count', 0)} / error {summary.get('health_error_count', 0)} / unknown {summary.get('health_unknown_count', 0)}。",
        "",
        "## 数据源健康",
        "",
        "| 数据源 | 状态 | 说明 | 影响 |",
        "|---|---|---|---|",
    ]
    for item in payload.get("data_health", []) if isinstance(payload.get("data_health"), list) else []:
        lines.append(f"| {item.get('source')} | {item.get('status')} | {item.get('message')} | {item.get('impact')} |")
    lines.extend(["", "## fallback 规则", ""])
    priority = payload.get("source_priority") if isinstance(payload.get("source_priority"), dict) else {}
    for key, values in priority.items():
        lines.append(f"- {key}: {' → '.join(values) if isinstance(values, list) else values}")
    gaps = payload.get("data_gaps") if isinstance(payload.get("data_gaps"), list) else []
    if gaps:
        lines.extend(["", "## 主要 data_gap", "", "| 来源 | 标的/字段 | 缺口 |", "|---|---|---|"])
        for item in gaps[:40]:
            target = item.get("symbol") or item.get("field") or ""
            lines.append(f"| {item.get('source')} | {target} | {item.get('data_gap')} |")
    return "\n".join(lines).strip() + "\n"


def archive_copy(report_path: Path) -> Path:
    timestamp = now_local().strftime("%Y%m%d-%H%M")
    archive = report_path.with_name(f"free-data-fallback-{timestamp}.md")
    archive.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-pack", type=Path, default=DEFAULT_MARKET_PACK)
    parser.add_argument("--fmp-research", type=Path, default=DEFAULT_FMP_RESEARCH)
    parser.add_argument("--macro-regime", type=Path, default=DEFAULT_MACRO_REGIME)
    parser.add_argument("--opportunity-radar", type=Path, default=DEFAULT_OPPORTUNITY_RADAR)
    parser.add_argument("--cross-market", type=Path, default=DEFAULT_CROSS_MARKET)
    parser.add_argument("--secondary-queue", type=Path, default=DEFAULT_SECONDARY_QUEUE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--docs-out", type=Path, default=DEFAULT_DOCS_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--symbol-limit", type=int, default=60)
    parser.add_argument("--max-sec-symbols", type=int, default=16)
    parser.add_argument("--max-alpha-symbols", type=int, default=10)
    parser.add_argument("--max-openfigi-symbols", type=int, default=12)
    parser.add_argument("--max-yahoo-symbols", type=int, default=24)
    parser.add_argument("--max-cninfo-symbols", type=int, default=24)
    parser.add_argument("--max-hkex-symbols", type=int, default=24)
    parser.add_argument("--no-archive-copy", action="store_true")
    args = parser.parse_args()

    payload = build_payload(args)
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
