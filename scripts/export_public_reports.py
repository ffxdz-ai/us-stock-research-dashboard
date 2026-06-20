#!/usr/bin/env python3
"""Build a privacy-safe JSON archive for the public GitHub Pages report site."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "reports"
DEFAULT_OUTPUT = ROOT / "docs" / "data" / "reports.json"

PRIVATE_SECTION_MARKERS = (
    "持仓输入",
    "当前持仓",
    "持仓明细",
    "组合约束",
    "组合整体",
    "账户概览",
    "账户摘要",
    "资金记录",
    "本地输入来源",
    "本地数据源",
    "Portfolio",
    "Holdings",
    "Account",
)

PRIVATE_LINE_PATTERNS = (
    re.compile(r"(?:估算)?总资产\s*[:：]"),
    re.compile(r"(?:剩余)?现金(?:比例)?\s*[:：]"),
    re.compile(r"账户(?:本金|收益|回报|盈亏)\s*[:：]"),
    re.compile(r"单票上限\s*[:：]"),
    re.compile(r"累计(?:充值|提取)\s*[:：]"),
    re.compile(r"(?:持有|加仓|卖出)\s*\d+(?:\.\d+)?\s*股"),
    re.compile(r"\b(?:shares|cost_basis|cash_usd|estimated_total_assets|net_deposit_usd|account_id|account_number)\b", re.IGNORECASE),
    re.compile(r"[A-Z]:\\", re.IGNORECASE),
    re.compile(r"portfolio\.json", re.IGNORECASE),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b(?:DEEPSEEK|OPENAI|GITHUB|FUTU)_[A-Z0-9_]*(?:KEY|TOKEN|SECRET)\b", re.IGNORECASE),
    re.compile(r"\.env(?:\b|$)", re.IGNORECASE),
)

KIND_LABELS = {
    "weekly": "周度扫描",
    "quick": "快速更新",
    "buy-side": "Buy-Side",
    "deepseek-cloud": "DeepSeek云端",
    "entry-radar": "入场雷达",
    "missed-review": "错过复盘",
    "future-audit": "未来函数审计",
    "daily": "每日分析",
}


def report_kind(name: str) -> str:
    lowered = name.lower()
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
    if "public-equity" in lowered or "buy-side" in lowered:
        return "buy-side"
    return "daily"


def first_title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return fallback


def sanitize_report(content: str) -> str:
    output: list[str] = []
    excluded_depth: int | None = None
    lines = content.replace("\r\n", "\n").split("\n")
    for line in lines:
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            depth = len(heading.group(1))
            title = heading.group(2).strip()
            if excluded_depth is not None and depth <= excluded_depth:
                excluded_depth = None
            if any(marker.lower() in title.lower() for marker in PRIVATE_SECTION_MARKERS):
                excluded_depth = depth
                continue
        if excluded_depth is not None:
            continue
        if any(pattern.search(line) for pattern in PRIVATE_LINE_PATTERNS):
            continue
        output.append(line.rstrip())

    compact: list[str] = []
    blank = False
    for line in output:
        is_blank = not line.strip()
        if is_blank and blank:
            continue
        compact.append(line)
        blank = is_blank
    sanitized = "\n".join(compact).strip()
    sanitized += (
        "\n\n---\n\n"
        "> 公开脱敏版：已移除账户金额、现金、股数、成本、本地路径、API Key 与持仓明细。"
        "仅供投研记录，不构成投资建议。\n"
    )
    return sanitized


def parse_report_time(path: Path, content: str) -> datetime:
    filename_patterns = (
        r"(20\d{6})[-_](\d{4})",
        r"(20\d{2}-\d{2}-\d{2})[-_](\d{2})(\d{2})",
    )
    for pattern in filename_patterns:
        match = re.search(pattern, path.name)
        if not match:
            continue
        try:
            groups = match.groups()
            if len(groups) == 2:
                return datetime.strptime("".join(groups), "%Y%m%d%H%M").astimezone()
            return datetime.strptime(" ".join((groups[0], groups[1] + ":" + groups[2])), "%Y-%m-%d %H:%M").astimezone()
        except ValueError:
            pass

    content_patterns = (
        r"生成时间\s*[:：]\s*(20\d{2}-\d{2}-\d{2})\s+(\d{2}:\d{2})",
        r"数据时间(?:\s+UTC)?\s*[:：]\s*(20\d{2}-\d{2}-\d{2})[T\s](\d{2}:\d{2})",
        r"Data timestamp(?:\s+UTC)?\s*[:：]\s*(20\d{2}-\d{2}-\d{2})[T\s](\d{2}:\d{2})",
    )
    for pattern in content_patterns:
        match = re.search(pattern, content)
        if match:
            try:
                parsed = datetime.strptime(" ".join(match.groups()), "%Y-%m-%d %H:%M")
                return parsed.astimezone()
            except ValueError:
                continue
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone()


def load_existing_reports(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    reports = payload.get("reports")
    if not isinstance(reports, list):
        return []
    clean: list[dict[str, Any]] = []
    for item in reports:
        if not isinstance(item, dict):
            continue
        content = sanitize_report(str(item.get("content", "")))
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        clean.append(
            {
                "id": str(item.get("id") or digest[:16]),
                "filename": str(item.get("filename") or "existing-report.md"),
                "title": str(item.get("title") or first_title(content, "existing-report")),
                "kind": str(item.get("kind") or "daily"),
                "kind_label": str(item.get("kind_label") or KIND_LABELS.get(str(item.get("kind") or "daily"), "每日分析")),
                "published_at": str(item.get("published_at") or datetime.now(timezone.utc).isoformat(timespec="minutes")),
                "published_label": str(item.get("published_label") or ""),
                "is_latest": bool(item.get("is_latest")),
                "content": content,
                "digest": digest,
            }
        )
    return clean


def collect_report_files() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if not REPORTS_DIR.exists():
        return candidates
    for path in REPORTS_DIR.glob("*.md"):
        content = path.read_text(encoding="utf-8")
        sanitized = sanitize_report(content)
        digest = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
        timestamp = parse_report_time(path, content)
        kind = report_kind(path.name)
        candidates.append(
            {
                "id": f"{timestamp.strftime('%Y%m%d-%H%M')}-{path.stem}",
                "filename": path.name,
                "title": first_title(sanitized, path.stem),
                "kind": kind,
                "kind_label": KIND_LABELS[kind],
                "published_at": timestamp.isoformat(timespec="minutes"),
                "published_label": timestamp.strftime("%Y-%m-%d %H:%M"),
                "is_latest": path.name.startswith("latest-"),
                "content": sanitized,
                "digest": digest,
            }
        )
    return candidates


def build_archive(output: Path, limit: int = 80, merge_existing: bool = True) -> dict[str, Any]:
    candidates = collect_report_files()
    if merge_existing:
        candidates.extend(load_existing_reports(output))

    candidates.sort(key=lambda item: str(item["published_at"]), reverse=True)
    seen: set[str] = set()
    reports: list[dict[str, Any]] = []
    for item in candidates:
        digest = str(item.pop("digest"))
        identity = f"{item.get('id')}:{digest}"
        content_identity = digest
        if identity in seen or content_identity in seen:
            continue
        seen.add(identity)
        seen.add(content_identity)
        reports.append(item)
        if len(reports) >= limit:
            break

    now = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_label": now.astimezone().strftime("%Y-%m-%d %H:%M"),
        "privacy": "public-sanitized",
        "report_count": len(reports),
        "reports": reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export sanitized reports for GitHub Pages.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--no-merge-existing", action="store_true")
    args = parser.parse_args()

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = build_archive(output, max(1, args.limit), merge_existing=not args.no_merge_existing)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {payload['report_count']} sanitized reports to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
