#!/usr/bin/env python3
"""Validate that the public report archive does not contain private fields."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


FORBIDDEN_PATTERNS = (
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b(?:DEEPSEEK|OPENAI|GITHUB|FUTU)_[A-Z0-9_]*(?:KEY|TOKEN|SECRET)\b", re.IGNORECASE),
    re.compile(r"\.env(?:\b|$)", re.IGNORECASE),
    re.compile(r"[A-Z]:\\", re.IGNORECASE),
    re.compile(r"portfolio\.json", re.IGNORECASE),
    re.compile(r"\b(?:cash_usd|cost_basis|estimated_total_assets|net_deposit_usd|account_id|account_number)\b", re.IGNORECASE),
    re.compile(r"(?:持有|买入|加仓|卖出)\s*\d+(?:\.\d+)?\s*股"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, nargs="?", default=Path("docs/data/reports.json"))
    args = parser.parse_args()

    raw = args.archive.read_text(encoding="utf-8")
    json.loads(raw)
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(raw):
            raise SystemExit(f"Privacy validation failed for pattern: {pattern.pattern}")
    print(f"Validated public archive: {args.archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
