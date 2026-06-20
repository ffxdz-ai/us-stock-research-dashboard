#!/usr/bin/env python3
"""Generate a public-safe daily research report with DeepSeek."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
PROMPT_PATH = ROOT / "prompts" / "deepseek_cloud_research_skill_pack.md"
DEFAULT_COMPACT_INPUT = DATA_DIR / "latest_agent_input.json"
DEFAULT_MARKET_PACK = DATA_DIR / "latest_market_pack.json"
DEFAULT_SECONDARY_QUEUE = DATA_DIR / "latest_secondary_analysis_queue.json"
DEFAULT_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-v4-pro"

LOCAL_ENV_PATHS = (
    ROOT / ".env",
    Path("D:/codex-AI-agent/US-RMB-Agent/.env"),
)


def beijing_timezone() -> timezone:
    try:
        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return timezone(timedelta(hours=8), "Asia/Shanghai")

FORBIDDEN_PUBLIC_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"[A-Z]:\\", re.IGNORECASE),
    re.compile(r"portfolio\.json", re.IGNORECASE),
    re.compile(r"\b(?:cash_usd|cost_basis|estimated_total_assets|net_deposit_usd)\b", re.IGNORECASE),
    re.compile(r"(?:持有|买入|加仓|卖出)\s*\d+(?:\.\d+)?\s*股"),
)


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
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").replace("%", "").strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def concise_candidate(item: dict[str, Any]) -> dict[str, Any]:
    sec = item.get("sec", {}) if isinstance(item.get("sec"), dict) else {}
    chart = item.get("chart", {}) if isinstance(item.get("chart"), dict) else {}
    entry = item.get("entry", {}) if isinstance(item.get("entry"), dict) else {}
    financials = item.get("financials", {}) if isinstance(item.get("financials"), dict) else {}
    technicals = item.get("technicals", {}) if isinstance(item.get("technicals"), dict) else {}
    mechanical_scores = item.get("mechanical_scores", {}) if isinstance(item.get("mechanical_scores"), dict) else {}

    return {
        "ticker": item.get("ticker"),
        "name": item.get("name") or item.get("shortName"),
        "price": item.get("price"),
        "quote_time": item.get("quote_time"),
        "quote_source": item.get("quote_source"),
        "forward_pe": item.get("forward_pe"),
        "trailing_pe": item.get("trailing_pe"),
        "valuation_pe": item.get("valuation_pe") or financials.get("valuation_pe"),
        "valuation_pe_source": item.get("valuation_pe_source") or financials.get("valuation_pe_source"),
        "estimated_pe_from_sec": item.get("estimated_pe_from_sec") or financials.get("estimated_pe_from_sec"),
        "finnhub_pe": item.get("finnhub_pe") or financials.get("finnhub_pe"),
        "finnhub_pe_metric": item.get("finnhub_pe_metric") or financials.get("finnhub_pe_metric"),
        "finnhub_pb": item.get("finnhub_pb") or financials.get("finnhub_pb"),
        "finnhub_ps": item.get("finnhub_ps") or financials.get("finnhub_ps"),
        "finnhub_roe": item.get("finnhub_roe") or financials.get("finnhub_roe"),
        "data_confidence": item.get("data_confidence"),
        "scores": {
            "quality": item.get("quality_score") or mechanical_scores.get("quality"),
            "valuation": item.get("valuation_score") or mechanical_scores.get("valuation"),
            "technical": item.get("technical_score") or mechanical_scores.get("technical"),
            "overall": item.get("overall_score") or mechanical_scores.get("overall"),
        },
        "entry": {
            "strict_entry": item.get("strict_entry") or entry.get("strict_entry"),
            "add_zone": item.get("add_zone") or entry.get("add_zone"),
            "invalidation": item.get("invalidation") or entry.get("invalidation"),
            "mechanical_target": item.get("mechanical_target") or entry.get("mechanical_target"),
            "reward_risk": item.get("reward_risk") or entry.get("reward_risk"),
            "buyable_now": item.get("buyable_now") if "buyable_now" in item else entry.get("buyable_now"),
        },
        "technicals": {
            "ma20": chart.get("ma20") or technicals.get("ma20"),
            "ma50": chart.get("ma50") or technicals.get("ma50"),
            "ma200": chart.get("ma200") or technicals.get("ma200"),
            "low20": chart.get("low20") or technicals.get("low20"),
            "low60": chart.get("low60") or technicals.get("low60"),
            "high252": chart.get("high252") or technicals.get("high252"),
            "low252": chart.get("low252") or technicals.get("low252"),
            "realized_vol20": chart.get("realized_vol20") or technicals.get("realized_vol20"),
            "source": chart.get("source") or technicals.get("source"),
        },
        "financials": {
            "sec_coverage": sec.get("sec_coverage") if sec else financials.get("sec_coverage"),
            "revenue_growth_yoy": sec.get("revenue_growth_yoy") if sec else financials.get("revenue_growth_yoy"),
            "net_margin": sec.get("net_margin") if sec else financials.get("net_margin"),
            "liabilities_to_assets": sec.get("liabilities_to_assets") if sec else financials.get("liabilities_to_assets"),
            "latest_annual_revenue_filed": (sec.get("latest_annual_revenue") or {}).get("filed")
            if sec
            else financials.get("latest_annual_revenue_filed"),
            "recent_filings": (sec.get("recent_filings") or financials.get("recent_filings") or [])[:4],
        },
    }


def top_candidates(pack: dict[str, Any], limit: int = 14) -> list[dict[str, Any]]:
    raw = pack.get("candidates")
    if not isinstance(raw, list):
        return []
    ordered = sorted(
        [item for item in raw if isinstance(item, dict)],
        key=lambda item: number(item.get("overall_score")) if number(item.get("overall_score")) is not None else -999.0,
        reverse=True,
    )
    return [concise_candidate(item) for item in ordered[:limit]]


def prepare_public_context(compact: dict[str, Any], pack: dict[str, Any], secondary_queue: dict[str, Any]) -> dict[str, Any]:
    """Drop private portfolio fields before sending context to DeepSeek."""
    market = compact.get("market") if isinstance(compact.get("market"), dict) else pack.get("market", {})
    research_candidates = compact.get("research_candidates") if isinstance(compact.get("research_candidates"), list) else []
    public_research = [concise_candidate(item) for item in research_candidates if isinstance(item, dict)]
    candidates = public_research or top_candidates(pack)
    buyable = [concise_candidate(item) for item in pack.get("buyable_now", []) if isinstance(item, dict)]
    watchlist = [concise_candidate(item) for item in pack.get("physical_ai_watchlist", []) if isinstance(item, dict)]

    return {
        "schema_version": 1,
        "generated_for": "public DeepSeek cloud report",
        "as_of_utc": compact.get("as_of_utc") or pack.get("as_of_utc") or datetime.now(timezone.utc).isoformat(),
        "data_boundary": {
            "edition": "云端公开数据版",
            "futu_opend": "云端不可用；只有输入字段明确标注 Futu OpenD 时才可引用",
            "private_portfolio": "disabled",
            "position_sizing": "disabled; whole-share sizing requires local portfolio review",
        },
        "project_rules": {
            "broker": "复星证券",
            "whole_shares_only": True,
            "cash_floor_pct": 15,
            "max_single_position_pct": 35,
            "normal_buy_min_reward_risk": 2.0,
            "do_not_chase_overheated_setups": True,
        },
        "market": market,
        "prescreen": compact.get("prescreen", {}),
        "candidate_limit_note": "候选池为机械预筛和公开数据压缩输入；模型必须重新审查，不得把机械分数当作最终结论。",
        "candidate_pool": candidates,
        "mechanical_buyable_now": buyable,
        "physical_ai_watchlist": watchlist,
        "secondary_analysis_queue": {
            "rule": "进入二次分析后每两天复核一次；不合格则退回观察，不再占用高频 Buy-Side 分析名额；无固定冷却期，重新满足触发条件即可回池。",
            "generated_label": secondary_queue.get("generated_label"),
            "summary": secondary_queue.get("summary") if isinstance(secondary_queue.get("summary"), dict) else {},
            "deepseek_priority": secondary_queue.get("deepseek_priority") if isinstance(secondary_queue.get("deepseek_priority"), list) else [],
            "recent_reviews": secondary_queue.get("reviews") if isinstance(secondary_queue.get("reviews"), list) else [],
        },
        "source_notes": pack.get("source_notes") or compact.get("source_notes") or [],
        "cache_stats": pack.get("cache_stats") or compact.get("cache_stats") or {},
    }


def validate_public_text(text: str) -> None:
    for pattern in FORBIDDEN_PUBLIC_PATTERNS:
        if pattern.search(text):
            raise ValueError(f"public safety validation failed: {pattern.pattern}")


def normalize_model_markdown(content: str) -> str:
    lines = content.strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and lines[0].lstrip().startswith(("好的", "好，", "当然", "以下是")):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    if lines and re.match(r"^#\s+DeepSeek\s+云端美股投研报告", lines[0].strip(), re.IGNORECASE):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def build_user_prompt(context: dict[str, Any], mode: str) -> str:
    context_text = json.dumps(context, ensure_ascii=False, indent=2)
    return f"""请基于下面的公开数据，生成今天的 DeepSeek 云端美股投研报告。

模式：{mode}

强制要求：
- 输出 Markdown。
- 不要寒暄，不要写“好的/以下是”，直接从报告正文开始。
- 不要重复输出一级标题；标题由外层系统生成。
- 必须写明“云端公开数据版”，并说明 Futu OpenD 云端不可用。
- 不要输出真实持仓、现金、成本、股数、本地路径、API Key。
- 对 secondary_analysis_queue.deepseek_priority 中的股票全部覆盖；如果数量较多，先用表格逐只给结论，再挑最重要标的展开。
- 如果 secondary_analysis_queue.deepseek_priority 为空，再从候选池中选择最值得复核的重点股票，宁缺毋滥。
- 估值优先使用 valuation_pe 和 valuation_pe_source；forward_pe/trailing_pe 缺失时，不得忽略 Finnhub P/E 或 SEC 市值/净利润估算 P/E。
- 每只重点股票必须分别评估：当前价试仓、理想回调、突破确认。
- 每条可执行买入路径必须独立满足 R/R >= 2:1；不满足就写观察或等待。
- 公开版不得给最终买入股数；整股执行写“需本地组合复核”。
- 加入“第二分析师审查”部分，指出主结论的反对意见和可能打脸点。

公开数据 JSON：

```json
{context_text}
```
"""


def deepseek_request(api_key: str, system_prompt: str, user_prompt: str) -> str:
    api_url = os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL)
    primary_model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)
    candidates = [
        {
            "model": primary_model,
            "temperature": 0.2,
            "max_tokens": 7600,
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        },
        {
            "model": primary_model,
            "temperature": 0.2,
            "max_tokens": 7600,
        },
    ]
    if primary_model != "deepseek-chat":
        candidates.append({"model": "deepseek-chat", "temperature": 0.2, "max_tokens": 5600})

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    last_error = ""
    for payload_options in candidates:
        payload = {
            **payload_options,
            "messages": messages,
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            api_url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {exc.code}: {error_body[:500]}"
            continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(1)
            continue

        choices = data.get("choices") or []
        if not choices:
            last_error = f"empty choices: {str(data)[:500]}"
            continue
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        last_error = f"empty content: {str(data)[:500]}"

    raise RuntimeError(f"DeepSeek request failed: {last_error}")


def wrap_report(content: str, context: dict[str, Any], mode: str) -> str:
    bj = datetime.now(beijing_timezone())
    source_time = context.get("as_of_utc", "")
    header = [
        f"# DeepSeek 云端美股投研报告 - {bj.strftime('%Y-%m-%d')}",
        "",
        f"- 生成时间：{bj.strftime('%Y-%m-%d %H:%M')} Asia/Shanghai",
        f"- 数据时间 UTC：{source_time}",
        f"- 运行模式：{mode}",
        "- 版本：云端公开数据版",
        "",
    ]
    return "\n".join(header) + content.strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="full", choices=("quick", "full", "weekly"))
    parser.add_argument("--input", type=Path, default=DEFAULT_COMPACT_INPUT)
    parser.add_argument("--market-pack", type=Path, default=DEFAULT_MARKET_PACK)
    parser.add_argument("--secondary-queue", type=Path, default=DEFAULT_SECONDARY_QUEUE)
    parser.add_argument("--out-dir", type=Path, default=REPORTS_DIR)
    parser.add_argument("--dry-run", action="store_true", help="Write the sanitized prompt context without calling DeepSeek.")
    args = parser.parse_args()

    load_environment()
    compact = load_json(args.input, {})
    pack = load_json(args.market_pack, {})
    secondary_queue = load_json(args.secondary_queue, {})
    context = prepare_public_context(compact, pack, secondary_queue)
    context_text = json.dumps(context, ensure_ascii=False, indent=2)
    validate_public_text(context_text)

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    user_prompt = build_user_prompt(context, args.mode)

    if args.dry_run:
        out = DATA_DIR / "latest_deepseek_cloud_prompt_context.json"
        write_text(out, context_text + "\n")
        print(f"Wrote sanitized DeepSeek context to {out}")
        return 0

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set.")

    content = deepseek_request(api_key, system_prompt, user_prompt)
    report = wrap_report(normalize_model_markdown(content), context, args.mode)
    validate_public_text(report)

    bj = datetime.now(beijing_timezone())
    filename = f"deepseek-cloud-{bj.strftime('%Y%m%d-%H%M')}.md"
    report_path = args.out_dir / filename
    latest_path = args.out_dir / "latest-deepseek-cloud-report.md"
    write_text(report_path, report)
    write_text(latest_path, report)
    print(f"Wrote {report_path}")
    print(f"Wrote {latest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
