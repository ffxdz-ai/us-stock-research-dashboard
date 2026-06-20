#!/usr/bin/env python3
"""Serve the local portfolio editor and save config/portfolio.json."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "portfolio-ui"
PORTFOLIO_PATH = ROOT / "config" / "portfolio.json"
REPORT_PATH = ROOT / "reports" / "latest-market-brief.md"
REPORTS_DIR = ROOT / "reports"
try:
    NEW_YORK_TZ = ZoneInfo("America/New_York")
except Exception:
    NEW_YORK_TZ = timezone(timedelta(hours=-4), "America/New_York")
SESSION_LABELS = {
    "pre": "盘前",
    "regular": "盘中",
    "post": "盘后",
    "overnight": "夜盘",
    "closed": "休市",
}

REPORT_KIND_LABELS = {
    "weekly": "周度扫描",
    "quick": "快速更新",
    "buy-side": "Buy-Side",
    "deepseek-cloud": "DeepSeek云端",
    "entry-radar": "入场雷达",
    "missed-review": "错过复盘",
    "future-audit": "未来函数审计",
    "daily": "每日分析",
}


def report_kind(filename: str) -> str:
    lowered = filename.lower()
    if "entry-radar" in lowered:
        return "entry-radar"
    if "missed-opportunity" in lowered:
        return "missed-review"
    if "future-function-audit" in lowered:
        return "future-audit"
    if "deepseek-cloud" in lowered:
        return "deepseek-cloud"
    if "weekly" in lowered:
        return "weekly"
    if "quick" in lowered:
        return "quick"
    if "public-equity" in lowered:
        return "buy-side"
    return "daily"


def report_title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return fallback


def list_reports() -> list[dict[str, object]]:
    if not REPORTS_DIR.exists():
        return []
    reports: list[dict[str, object]] = []
    for path in REPORTS_DIR.glob("*.md"):
        try:
            content = path.read_text(encoding="utf-8")
            stat = path.stat()
        except OSError:
            continue
        kind = report_kind(path.name)
        reports.append(
            {
                "name": path.name,
                "title": report_title(content, path.stem),
                "kind": kind,
                "kind_label": REPORT_KIND_LABELS[kind],
                "updated_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                "updated_label": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "size": stat.st_size,
                "is_latest": path.name.startswith("latest-"),
            }
        )
    reports.sort(key=lambda item: str(item["updated_at"]), reverse=True)
    return reports


def resolve_report(name: str) -> Path:
    clean_name = Path(name).name
    if clean_name != name or not clean_name.endswith(".md"):
        raise ValueError("Invalid report name.")
    target = (REPORTS_DIR / clean_name).resolve()
    if target.parent != REPORTS_DIR.resolve() or not target.is_file():
        raise FileNotFoundError("Report not found.")
    return target

COMPANY_PROFILE_OVERRIDES_ZH: dict[str, dict[str, str]] = {
    "AAPL": {
        "name": "苹果",
        "business_summary": "消费电子与软件生态公司，主营 iPhone、Mac、iPad、可穿戴设备、App Store、iCloud、Apple Music 等硬件、软件和服务。",
    },
    "AMD": {
        "name": "超威半导体",
        "business_summary": "高性能计算芯片公司，主营 CPU、GPU、数据中心加速卡、自适应 SoC 和嵌入式处理器，服务数据中心、PC、游戏和边缘计算市场。",
    },
    "AMAT": {
        "name": "应用材料",
        "business_summary": "半导体设备和材料工程公司，主营晶圆制造设备、显示面板设备、工艺控制和相关服务，是全球芯片制造资本开支的重要供应商。",
    },
    "AMZN": {
        "name": "亚马逊",
        "business_summary": "电商、云计算和数字服务公司，主营线上零售、第三方卖家平台、AWS 云服务、广告、会员订阅和物流履约体系。",
    },
    "ASML": {
        "name": "阿斯麦",
        "business_summary": "半导体光刻设备龙头，主营 EUV、DUV 光刻机、量测与检测系统以及相关服务，是先进制程芯片制造的关键设备供应商。",
    },
    "AVGO": {
        "name": "博通",
        "business_summary": "半导体和基础设施软件公司，主营网络通信芯片、无线射频、存储连接、定制 ASIC、VMware 等企业基础设施软件。",
    },
    "COHR": {
        "name": "相干公司",
        "business_summary": "光子与复合半导体材料公司，主营激光器、光通信器件、碳化硅材料、工业加工和电子材料，受益于 AI 数据中心光互连和先进制造需求。",
    },
    "GOOGL": {
        "name": "Alphabet A（谷歌母公司）",
        "business_summary": "互联网与人工智能平台公司，主营 Google 搜索、YouTube、广告技术、Android、Google Cloud、Waymo 和生成式 AI 产品。",
    },
    "IBM": {
        "name": "IBM",
        "business_summary": "企业科技服务公司，主营混合云、主机系统、企业软件、咨询服务和 AI 解决方案，重点服务大型企业和政府客户。",
    },
    "INTC": {
        "name": "英特尔",
        "business_summary": "半导体公司，主营 PC 和服务器 CPU、数据中心平台、代工制造、网络与边缘计算芯片，并推进先进制程和晶圆代工业务。",
    },
    "ISRG": {
        "name": "直觉外科",
        "business_summary": "手术机器人公司，主营达芬奇手术系统、相关器械耗材、系统维护服务和数字化手术平台，是机器人辅助手术领域龙头。",
    },
    "META": {
        "name": "Meta",
        "business_summary": "社交网络和人工智能平台公司，主营 Facebook、Instagram、WhatsApp、广告业务、AI 基础设施和 Reality Labs 硬件生态。",
    },
    "MSFT": {
        "name": "微软",
        "business_summary": "企业软件、云计算和人工智能平台公司，主营 Azure、Microsoft 365、Windows、Dynamics、LinkedIn、GitHub、游戏和 AI 服务。",
    },
    "MU": {
        "name": "美光科技",
        "business_summary": "存储芯片公司，主营 DRAM、NAND 闪存、HBM 高带宽存储和企业级存储产品，核心需求来自数据中心、AI 服务器、PC、手机和汽车电子。",
    },
    "NVDA": {
        "name": "英伟达",
        "business_summary": "AI 加速计算平台公司，主营数据中心 GPU、AI 加速卡、网络互连、CUDA 软件生态、游戏显卡、专业可视化和汽车计算平台。",
    },
    "ORCL": {
        "name": "甲骨文",
        "business_summary": "企业软件和云服务公司，主营数据库、ERP/HCM/CX 应用、Oracle Cloud Infrastructure 和企业级 AI 数据平台。",
    },
    "PLTR": {
        "name": "Palantir",
        "business_summary": "数据分析和 AI 软件公司，主营政府与企业数据平台、AIP 人工智能平台、Foundry、Gotham 和 Apollo，服务国防、情报、制造和商业运营场景。",
    },
    "QCOM": {
        "name": "高通",
        "business_summary": "无线通信芯片公司，主营智能手机 SoC、蜂窝基带、射频前端、汽车芯片、物联网和边缘 AI 平台，并拥有大量通信专利授权收入。",
    },
    "ROK": {
        "name": "罗克韦尔自动化",
        "business_summary": "工业自动化公司，主营 PLC、运动控制、工业软件、传感器、变频器和智能制造解决方案，服务制造业数字化和自动化升级。",
    },
    "SYM": {
        "name": "Symbotic",
        "business_summary": "仓储自动化与机器人公司，主营 AI 驱动的自动化仓库系统、移动机器人、分拣系统和供应链软件，核心客户包括大型零售和物流企业。",
    },
    "TSLA": {
        "name": "特斯拉",
        "business_summary": "电动车、能源和机器人公司，主营电动汽车、储能系统、光伏、自动驾驶软件、车载 AI 和机器人项目。",
    },
    "TSM": {
        "name": "台积电",
        "business_summary": "全球晶圆代工龙头，主营先进制程和成熟制程芯片制造，客户覆盖 AI GPU、CPU、手机 SoC、汽车和高性能计算芯片设计公司。",
    },
}

TERM_TRANSLATIONS_ZH = {
    "Basic Materials": "基础材料",
    "Capital Goods": "资本品",
    "Computer Manufacturing": "计算机制造",
    "Computer Software": "计算机软件",
    "Consumer Discretionary": "可选消费",
    "Consumer Electronics": "消费电子",
    "Consumer Services": "消费者服务",
    "Electric Vehicles": "电动汽车",
    "Electronic Components": "电子元器件",
    "Finance": "金融",
    "Health Care": "医疗保健",
    "Industrials": "工业",
    "Internet Content & Information": "互联网内容与信息",
    "Medical Specialities": "医疗专科设备",
    "Semiconductors": "半导体",
    "Technology": "科技",
    "Telecommunications Equipment": "通信设备",
}


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_company_value(data: dict[str, object], key: str) -> str:
    raw = data.get(key)
    if isinstance(raw, dict):
        value = raw.get("value")
        return str(value).strip() if value else ""
    return str(raw).strip() if raw else ""


def normalize_text(value: str) -> str:
    return " ".join(unescape(str(value or "")).split())


def has_chinese(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in str(value or ""))


@lru_cache(maxsize=512)
def translate_text_to_chinese(value: str) -> str:
    text = normalize_text(value)
    if not text or has_chinese(text):
        return text
    if text in TERM_TRANSLATIONS_ZH:
        return TERM_TRANSLATIONS_ZH[text]

    # Keep the UI note concise and avoid very long translation URLs.
    text_for_translate = text[:1800].rsplit(".", 1)[0] or text[:1800]
    query = urlencode(
        {
            "client": "gtx",
            "sl": "en",
            "tl": "zh-CN",
            "dt": "t",
            "q": text_for_translate,
        }
    )
    request = urllib.request.Request(
        f"https://translate.googleapis.com/translate_a/single?{query}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        fragments = []
        if isinstance(payload, list) and payload and isinstance(payload[0], list):
            for item in payload[0]:
                if isinstance(item, list) and item:
                    fragments.append(str(item[0]))
        translated = normalize_text("".join(fragments))
        return translated or TERM_TRANSLATIONS_ZH.get(text, text)
    except Exception:  # noqa: BLE001
        return TERM_TRANSLATIONS_ZH.get(text, text)


def build_company_profile_zh(
    symbol: str,
    name: str,
    sector: str,
    industry: str,
    description: str,
) -> dict[str, str]:
    override = COMPANY_PROFILE_OVERRIDES_ZH.get(symbol, {})
    name_zh = override.get("name") or translate_text_to_chinese(name) or name or symbol
    sector_zh = translate_text_to_chinese(sector) if sector else ""
    industry_zh = translate_text_to_chinese(industry) if industry else ""

    summary_en_parts = []
    if sector or industry:
        summary_en_parts.append(" / ".join(part for part in [sector, industry] if part))
    if description:
        summary_en_parts.append(description)

    if override.get("business_summary"):
        business_summary_zh = override["business_summary"]
    else:
        summary_zh_parts = []
        if sector_zh or industry_zh:
            summary_zh_parts.append(" / ".join(part for part in [sector_zh, industry_zh] if part))
        if description:
            summary_zh_parts.append(translate_text_to_chinese(description))
        business_summary_zh = "。".join(part for part in summary_zh_parts if part)

    return {
        "name_zh": name_zh,
        "sector_zh": sector_zh,
        "industry_zh": industry_zh,
        "business_summary_zh": business_summary_zh,
        "business_summary_en": "。".join(summary_en_parts),
        "source": (
            "Nasdaq company profile API + 本地中文公司档案"
            if override
            else "Nasdaq company profile API + 中文翻译兜底"
        ),
    }


def build_company_override_response(symbol: str, reason: str = "") -> dict[str, object] | None:
    override = COMPANY_PROFILE_OVERRIDES_ZH.get(symbol)
    if not override:
        return None
    source = "本地中文公司档案"
    if reason:
        source = f"{source}（Nasdaq 暂不可用：{reason}）"
    return {
        "ticker": symbol,
        "name": override.get("name", symbol),
        "name_en": "",
        "sector": "",
        "industry": "",
        "sector_en": "",
        "industry_en": "",
        "business_summary": override.get("business_summary", ""),
        "business_summary_en": "",
        "website": "",
        "source": source,
        "language": "zh-CN",
        "as_of_local": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
    }


def fetch_company_info(ticker: str) -> dict[str, object]:
    symbol = ticker.strip().upper()
    if not symbol:
        raise ValueError("Missing ticker.")

    url = f"https://api.nasdaq.com/api/company/{quote(symbol)}/company-profile"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        fallback = build_company_override_response(symbol, f"HTTP {exc.code}")
        if fallback:
            return fallback
        raise RuntimeError(f"Nasdaq company profile HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = str(exc.reason or exc) or exc.__class__.__name__
        fallback = build_company_override_response(symbol, reason)
        if fallback:
            return fallback
        raise RuntimeError(f"Nasdaq company profile request failed: {reason}") from exc

    data = payload.get("data")
    if not isinstance(data, dict):
        fallback = build_company_override_response(symbol, "未返回公司资料")
        if fallback:
            return fallback
        raise RuntimeError(f"No company profile returned for {symbol}.")

    name = read_company_value(data, "CompanyName")
    sector = read_company_value(data, "Sector")
    industry = read_company_value(data, "Industry")
    description = read_company_value(data, "CompanyDescription")
    website = read_company_value(data, "CompanyUrl")

    if not any([name, sector, industry, description]):
        fallback = build_company_override_response(symbol, "资料字段为空")
        if fallback:
            return fallback
        raise RuntimeError(f"No company profile fields returned for {symbol}.")

    company_zh = build_company_profile_zh(symbol, name, sector, industry, description)

    return {
        "ticker": symbol,
        "name": company_zh["name_zh"] or name or symbol,
        "name_en": name,
        "sector": company_zh["sector_zh"],
        "industry": company_zh["industry_zh"],
        "sector_en": sector,
        "industry_en": industry,
        "business_summary": company_zh["business_summary_zh"],
        "business_summary_en": company_zh["business_summary_en"],
        "website": website,
        "source": company_zh["source"],
        "language": "zh-CN",
        "as_of_local": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
    }


def parse_market_number(value: object) -> float | None:
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
    if not cleaned or cleaned.lower() in {"n/a", "nan", "none"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def current_us_session(now_ny: datetime | None = None) -> str:
    now = now_ny or datetime.now(NEW_YORK_TZ)
    if now.weekday() >= 5:
        return "closed"
    current = now.time()
    if time(4, 0) <= current < time(9, 30):
        return "pre"
    if time(9, 30) <= current < time(16, 0):
        return "regular"
    if time(16, 0) <= current < time(20, 0):
        return "post"
    return "overnight"


def yahoo_chart_extended_quote(symbol: str, requested_session: str) -> dict[str, object]:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?"
        + urlencode({"range": "1d", "interval": "1m", "includePrePost": "true"})
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))

    chart = payload.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(str(chart["error"]))
    result = (chart.get("result") or [{}])[0]
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quote_rows = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote_rows.get("close") or []
    periods = meta.get("currentTradingPeriod") or {}

    candidates: list[tuple[int, float]] = []
    for timestamp, close in zip(timestamps, closes):
        if not isinstance(timestamp, int) or not isinstance(close, (int, float)):
            continue
        candidates.append((timestamp, float(close)))

    def latest_between(start: int | None, end: int | None) -> tuple[int, float] | None:
        if not start or not end:
            return None
        rows = [(ts, price) for ts, price in candidates if start <= ts <= end]
        return rows[-1] if rows else None

    selected: tuple[int, float] | None = None
    source_session = requested_session
    fallback_reason = ""

    if requested_session in {"pre", "regular", "post"}:
        period = periods.get(requested_session) or {}
        selected = latest_between(period.get("start"), period.get("end"))
        if not selected and requested_session == "regular":
            regular_price = parse_market_number(meta.get("regularMarketPrice"))
            regular_time = meta.get("regularMarketTime")
            if regular_price is not None and isinstance(regular_time, int):
                selected = (regular_time, regular_price)
        if not selected:
            fallback_reason = f"{SESSION_LABELS[requested_session]}价格暂不可用，已回退到最新可用分钟价。"

    if requested_session == "overnight":
        fallback_reason = "公开 Yahoo/Nasdaq 源未提供完整夜盘逐笔价，已使用上一盘后最新分钟价。"

    if requested_session == "closed":
        fallback_reason = "当前为美股休市时间，已使用最新可用分钟价。"

    if not selected and candidates:
        selected = candidates[-1]
        ts = selected[0]
        source_session = classify_timestamp_session(ts, periods)

    if not selected:
        regular_price = parse_market_number(meta.get("regularMarketPrice"))
        regular_time = meta.get("regularMarketTime")
        if regular_price is None:
            raise RuntimeError("Yahoo chart did not return usable quote data.")
        selected = (regular_time if isinstance(regular_time, int) else int(datetime.now(tz=timezone.utc).timestamp()), regular_price)
        source_session = "regular"
        fallback_reason = fallback_reason or "分钟图无可用价格，已回退到常规盘价格。"

    selected_time_utc = datetime.fromtimestamp(selected[0], tz=timezone.utc)
    selected_time_ny = selected_time_utc.astimezone(NEW_YORK_TZ)
    return {
        "price": round(selected[1], 4),
        "time": selected_time_ny.strftime("%Y-%m-%d %H:%M %Z"),
        "source": "Yahoo Finance 1m extended-hours chart",
        "session": requested_session,
        "session_label": SESSION_LABELS.get(requested_session, requested_session),
        "source_session": source_session,
        "source_session_label": SESSION_LABELS.get(source_session, source_session),
        "is_fallback": bool(fallback_reason or source_session != requested_session),
        "fallback_reason": fallback_reason,
        "regular_price": parse_market_number(meta.get("regularMarketPrice")),
        "previous_close": parse_market_number(meta.get("chartPreviousClose") or meta.get("previousClose")),
    }


def classify_timestamp_session(timestamp: int, periods: dict[str, object]) -> str:
    for key in ("pre", "regular", "post"):
        period = periods.get(key)
        if not isinstance(period, dict):
            continue
        start = period.get("start")
        end = period.get("end")
        if isinstance(start, int) and isinstance(end, int) and start <= timestamp <= end:
            return key
    return current_us_session(datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(NEW_YORK_TZ))


def nasdaq_regular_quote(symbol: str) -> dict[str, object]:
    from collect_market_data import collect_nasdaq_quotes

    raw = collect_nasdaq_quotes([symbol]).get(symbol, {})
    return {
        "price": raw.get("regularMarketPrice"),
        "change_pct": raw.get("regularMarketChangePercent"),
        "time": raw.get("regularMarketTime"),
        "source": raw.get("source", "Nasdaq public quote API"),
        "session": "regular",
        "session_label": SESSION_LABELS["regular"],
        "source_session": "regular",
        "source_session_label": SESSION_LABELS["regular"],
        "is_fallback": True,
        "fallback_reason": "扩展时段价格获取失败，已回退到 Nasdaq 常规盘价格。",
        "error": raw.get("error"),
    }


def futu_connection_status() -> dict[str, object]:
    env = load_dotenv(ROOT / ".env")
    if env.get("FUTU_QUOTE_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
        return {
            "enabled": False,
            "connected": False,
            "host": env.get("FUTU_OPEND_HOST", "127.0.0.1").strip() or "127.0.0.1",
            "port": env.get("FUTU_OPEND_PORT", "11111").strip() or "11111",
            "message": "Futu quote disabled by FUTU_QUOTE_DISABLED.",
        }
    host = env.get("FUTU_OPEND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port_text = env.get("FUTU_OPEND_PORT", "11111").strip() or "11111"
    try:
        port = int(port_text)
    except ValueError:
        port = 11111
    try:
        with socket.create_connection((host, port), timeout=0.35):
            connected = True
    except OSError:
        connected = False
    return {
        "enabled": True,
        "connected": connected,
        "host": host,
        "port": port,
        "message": "Futu OpenD connected." if connected else "Futu OpenD not connected.",
    }


def fetch_session_quote(ticker: str, requested_session: str) -> dict[str, object]:
    symbol = ticker.strip().upper()
    futu_quote = fetch_futu_session_quote(symbol, requested_session)
    if futu_quote:
        return futu_quote

    try:
        quote_payload = yahoo_chart_extended_quote(symbol, requested_session)
    except Exception as exc:  # noqa: BLE001
        quote_payload = nasdaq_regular_quote(symbol)
        quote_payload["fallback_reason"] = f"{quote_payload['fallback_reason']} 原因: {exc}"

    price = parse_market_number(quote_payload.get("price"))
    previous_close = parse_market_number(quote_payload.get("previous_close"))
    regular_price = parse_market_number(quote_payload.get("regular_price"))
    baseline = previous_close or regular_price
    if price is not None and baseline:
        quote_payload["change_pct"] = round(((price / baseline) - 1) * 100, 4)
    elif "change_pct" not in quote_payload:
        quote_payload["change_pct"] = None
    return quote_payload


def first_numeric(row: object, names: list[str]) -> tuple[float | None, str | None]:
    for name in names:
        try:
            value = row.get(name)  # pandas Series and dict both support get.
        except AttributeError:
            continue
        parsed = parse_market_number(value)
        if parsed is not None and parsed > 0:
            return parsed, name
    return None, None


def fetch_futu_session_quote(symbol: str, requested_session: str) -> dict[str, object] | None:
    env = load_dotenv(ROOT / ".env")
    if env.get("FUTU_QUOTE_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
        return None

    host = env.get("FUTU_OPEND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port_text = env.get("FUTU_OPEND_PORT", "11111").strip() or "11111"
    try:
        port = int(port_text)
    except ValueError:
        port = 11111

    status = futu_connection_status()
    if not status.get("connected"):
        return None

    try:
        from futu import OpenQuoteContext, RET_OK  # type: ignore
    except Exception:
        return None

    quote_ctx = None
    try:
        quote_ctx = OpenQuoteContext(host=host, port=port, is_async_connect=False)
        ret, data = quote_ctx.get_market_snapshot([f"US.{symbol}"])
        if ret != RET_OK or data is None or len(data) == 0:
            return None
        row = data.iloc[0]
    except Exception:
        return None
    finally:
        if quote_ctx is not None:
            try:
                quote_ctx.close()
            except Exception:
                pass

    session_fields = {
        "pre": ["pre_price", "preMarketPrice", "pre_price"],
        "regular": ["last_price", "cur_price", "regularMarketPrice"],
        "post": ["after_price", "post_price", "afterMarketPrice"],
        "overnight": ["overnight_price", "night_price", "after_price", "last_price"],
        "closed": ["overnight_price", "after_price", "last_price"],
    }
    price, field = first_numeric(row, session_fields.get(requested_session, ["last_price"]))
    if price is None:
        price, field = first_numeric(
            row,
            ["overnight_price", "pre_price", "after_price", "last_price", "cur_price"],
        )
    if price is None:
        return None

    source_session = requested_session
    field_session = {
        "overnight_price": "overnight",
        "night_price": "overnight",
        "pre_price": "pre",
        "after_price": "post",
        "post_price": "post",
        "last_price": "regular",
        "cur_price": "regular",
    }.get(field or "", requested_session)
    source_session = field_session
    fallback = source_session != requested_session

    previous_close, _ = first_numeric(row, ["prev_close_price", "prev_close", "previous_close"])
    change_pct, _ = first_numeric(row, ["change_rate", "change_pct"])
    if change_pct is None and previous_close:
        change_pct = ((price / previous_close) - 1) * 100

    data_time = str(row.get("data_time") or row.get("update_time") or "").strip()
    data_date = str(row.get("data_date") or "").strip()
    quote_time = " ".join(part for part in [data_date, data_time] if part).strip()
    if not quote_time:
        quote_time = datetime.now(NEW_YORK_TZ).strftime("%Y-%m-%d %H:%M %Z")

    note = ""
    if fallback:
        note = f"Futu OpenD 暂无{SESSION_LABELS.get(requested_session, requested_session)}字段，已使用{SESSION_LABELS.get(source_session, source_session)}价。"

    return {
        "price": round(price, 4),
        "change_pct": round(change_pct, 4) if change_pct is not None else None,
        "time": quote_time,
        "source": f"Futu OpenD market snapshot ({field})",
        "session": requested_session,
        "session_label": SESSION_LABELS.get(requested_session, requested_session),
        "source_session": source_session,
        "source_session_label": SESSION_LABELS.get(source_session, source_session),
        "is_fallback": fallback,
        "fallback_reason": note,
        "regular_price": first_numeric(row, ["last_price", "cur_price"])[0],
        "previous_close": previous_close,
    }


class PortfolioHandler(SimpleHTTPRequestHandler):
    server_version = "PortfolioUI/0.1"

    def translate_path(self, path: str) -> str:
        path = unquote(path.split("?", 1)[0].split("#", 1)[0])
        if path == "/":
            path = "/index.html"
        target = (UI_ROOT / path.lstrip("/")).resolve()
        if not str(target).startswith(str(UI_ROOT.resolve())):
            return str(UI_ROOT / "index.html")
        return str(target)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[portfolio-ui] {self.address_string()} - {format % args}")

    def read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_error_json(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self.send_json({"error": message}, status)

    def do_GET(self) -> None:
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/api/company-info":
            try:
                query = parse_qs(parsed_path.query)
                ticker = (query.get("ticker") or [""])[0]
                self.send_json({"company": fetch_company_info(ticker)})
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed_path.path == "/api/portfolio":
            try:
                portfolio = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
                self.send_json({"portfolio": portfolio})
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed_path.path == "/api/status":
            env = load_dotenv(ROOT / ".env")
            latest_report = None
            latest_report_path = None
            if REPORT_PATH.exists():
                mtime = datetime.fromtimestamp(REPORT_PATH.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                latest_report = f"{mtime}"
                latest_report_path = str(REPORT_PATH)
            self.send_json(
                {
                    "feishu_configured": bool(env.get("FEISHU_WEBHOOK_URL")),
                    "latest_report": latest_report,
                    "latest_report_path": latest_report_path,
                    "portfolio_path": str(PORTFOLIO_PATH),
                }
            )
            return
        if parsed_path.path == "/api/reports":
            self.send_json({"reports": list_reports()})
            return
        if parsed_path.path == "/api/report":
            try:
                query = parse_qs(parsed_path.query)
                name = (query.get("name") or [""])[0]
                path = resolve_report(name)
                content = path.read_text(encoding="utf-8")
                kind = report_kind(path.name)
                self.send_json(
                    {
                        "report": {
                            "name": path.name,
                            "title": report_title(content, path.stem),
                            "kind": kind,
                            "kind_label": REPORT_KIND_LABELS[kind],
                            "content": content,
                        }
                    }
                )
            except FileNotFoundError as exc:
                self.send_error_json(str(exc), HTTPStatus.NOT_FOUND)
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(str(exc), HTTPStatus.BAD_REQUEST)
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path == "/api/quotes":
            try:
                body = self.read_json_body()
                tickers = body.get("tickers")
                if not isinstance(tickers, list):
                    self.send_error_json("Missing tickers array.")
                    return
                clean_tickers = [str(ticker).strip().upper() for ticker in tickers if str(ticker).strip()]
                if not clean_tickers:
                    self.send_error_json("No valid tickers supplied.")
                    return

                now_ny = datetime.now(NEW_YORK_TZ)
                requested_session = current_us_session(now_ny)
                futu_status = futu_connection_status()
                allow_public_fallback = bool(body.get("allow_public_fallback"))
                if requested_session == "overnight" and not futu_status.get("connected") and not allow_public_fallback:
                    self.send_json(
                        {
                            "as_of_local": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
                            "as_of_new_york": now_ny.strftime("%Y-%m-%d %H:%M %Z"),
                            "session": requested_session,
                            "session_label": SESSION_LABELS.get(requested_session, requested_session),
                            "futu": futu_status,
                            "source": "Futu OpenD required for broker overnight prices",
                            "blocked": True,
                            "message": "Futu OpenD is not connected. Overnight broker prices were not refreshed to avoid overwriting real broker quotes with public fallback prices.",
                            "quotes": {},
                        }
                    )
                    return
                quotes: dict[str, object] = {}
                for ticker in list(dict.fromkeys(clean_tickers)):
                    quotes[ticker] = fetch_session_quote(ticker, requested_session)
                response_source = "Yahoo Finance 1m extended-hours chart; Nasdaq regular quote fallback"
                if any("Futu OpenD" in str(quote.get("source", "")) for quote in quotes.values() if isinstance(quote, dict)):
                    response_source = "Futu OpenD market snapshot; public fallback only for missing symbols"
                self.send_json(
                    {
                        "as_of_local": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
                        "as_of_new_york": now_ny.strftime("%Y-%m-%d %H:%M %Z"),
                        "session": requested_session,
                        "session_label": SESSION_LABELS.get(requested_session, requested_session),
                        "futu": futu_status,
                        "source": response_source,
                        "quotes": quotes,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if self.path == "/api/portfolio":
            try:
                body = self.read_json_body()
                portfolio = body.get("portfolio")
                if not isinstance(portfolio, dict):
                    self.send_error_json("Missing portfolio object.")
                    return
                PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
                if PORTFOLIO_PATH.exists():
                    backup_dir = PORTFOLIO_PATH.parent / "backups"
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    shutil.copy2(PORTFOLIO_PATH, backup_dir / f"portfolio-{stamp}.json")
                PORTFOLIO_PATH.write_text(
                    json.dumps(portfolio, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                self.send_json({"portfolio": portfolio})
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if self.path == "/api/run-report":
            try:
                command = [
                    "powershell",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "run_daily_agent.ps1"),
                    "-Send",
                ]
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    timeout=300,
                    check=False,
                )
                output = (result.stdout + result.stderr).strip()
                if result.returncode != 0:
                    self.send_error_json(output or "Report command failed.", HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                sent_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
                self.send_json(
                    {
                        "output": output,
                        "sent": True,
                        "sent_at": sent_at,
                        "report_path": str(REPORT_PATH),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self.send_error_json("Unknown endpoint.", HTTPStatus.NOT_FOUND)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if not UI_ROOT.exists():
        print(f"Missing UI directory: {UI_ROOT}", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.host, args.port), PortfolioHandler)
    print(f"Portfolio UI: http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping portfolio UI.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
