#!/usr/bin/env python3
"""Validate V2 schemas and fail when a critical PIT/risk invariant is broken."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from model_v2 import build_entry_path, evaluate_risk_gate, future_function_audit, load_risk_policy


ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def validate_market_pack(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if int(payload.get("schema_version") or 0) < 2:
        errors.append("market_pack.schema_version must be >= 2")
    if not payload.get("as_of_utc"):
        errors.append("market_pack.as_of_utc is required")
    policy = load_risk_policy()
    for item in payload.get("candidates", []) if isinstance(payload.get("candidates"), list) else []:
        if not isinstance(item, dict):
            errors.append("candidate must be an object")
            continue
        ticker = str(item.get("ticker") or "unknown")
        chart = item.get("chart") if isinstance(item.get("chart"), dict) else {}
        bar_count = int(chart.get("bar_count") or 0)
        for window in (20, 50, 200):
            if bar_count < window and chart.get(f"ma{window}") is not None:
                errors.append(f"{ticker}: ma{window} exists with only {bar_count} bars")
        if chart.get("breakout_reference_excludes_current_bar") is not True:
            errors.append(f"{ticker}: breakout reference may include current bar")
        pit = future_function_audit(item, item.get("signal_bar_time") or payload.get("as_of_utc"))
        if pit.get("status") != item.get("future_function_audit"):
            errors.append(f"{ticker}: stored PIT status disagrees with recomputed audit: {pit.get('failures')}")
        paths = item.get("entry_paths") if isinstance(item.get("entry_paths"), dict) else {}
        formal = paths.get("formal") if isinstance(paths.get("formal"), dict) else {}
        path = build_entry_path("formal", formal.get("entry"), formal.get("stop"), formal.get("target"), policy.formal_min_rr)
        gate = evaluate_risk_gate({**item, "valid_path": path.valid}, path, policy=policy)
        if item.get("formal_qualified") is True and not gate.qualified:
            errors.append(f"{ticker}: formal_qualified bypassed hard gate: {','.join(gate.gate_failures)}")
        if item.get("future_function_audit") == "BLOCK" and item.get("buyable_now") is True:
            errors.append(f"{ticker}: buyable_now despite future-function BLOCK")
        if item.get("price_freshness") == "fallback_only" and item.get("execution_allowed") is True:
            errors.append(f"{ticker}: fallback quote incorrectly allows execution")
    return errors


def validate_public_index(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if int(payload.get("schema_version") or 0) < 2:
        errors.append("public index schema_version must be >= 2")
    for item in payload.get("opportunities", []) if isinstance(payload.get("opportunities"), list) else []:
        if not isinstance(item, dict):
            errors.append("opportunity must be an object")
            continue
        symbol = str(item.get("symbol") or "unknown")
        if item.get("formal_qualified") is True:
            required = {
                "price_freshness": "fresh",
                "execution_allowed": True,
                "technical_data_complete": True,
                "future_function_audit": "PASS",
            }
            for key, expected in required.items():
                if item.get(key) != expected:
                    errors.append(f"{symbol}: formal signal has {key}={item.get(key)!r}, expected {expected!r}")
            if item.get("gate_failures"):
                errors.append(f"{symbol}: formal signal still has gate failures")
            confidence = item.get("data_confidence")
            if not isinstance(confidence, (int, float)) or float(confidence) < load_risk_policy().min_data_confidence:
                errors.append(f"{symbol}: formal signal data_confidence below policy")
            path = build_entry_path(
                "formal",
                item.get("entry_price") or item.get("safe_entry_price"),
                item.get("stop_loss"),
                item.get("target_price"),
                load_risk_policy().formal_min_rr,
            )
            if not path.valid:
                errors.append(f"{symbol}: formal signal has invalid R/R path: {path.failures}")
        if item.get("execution_allowed") is True and item.get("price_freshness") != "fresh":
            errors.append(f"{symbol}: execution allowed on non-fresh price")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-pack", type=Path, default=ROOT / "data" / "latest_market_pack.json")
    parser.add_argument("--public-index", type=Path)
    args = parser.parse_args()
    errors = validate_market_pack(load_json(args.market_pack))
    if args.public_index and args.public_index.exists():
        errors.extend(validate_public_index(load_json(args.public_index)))
    if errors:
        print("V2 QUALITY GATE: FAIL")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)
    print("V2 QUALITY GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
