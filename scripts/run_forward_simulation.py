#!/usr/bin/env python3
"""Run an offline, no-order/no-notification forward simulation market pack."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from model_v2 import build_entry_path, build_signal_identifiers, canonical_content_hash, load_model_config, load_risk_policy
from simulation_engine import simulate_portfolio
from simulation_ledger import SimulationLedger


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIMULATION_POLICY = ROOT / "config" / "simulation_policy.json"


def load_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain an object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-pack", type=Path, required=True, help="Offline JSON with plans and bars_by_symbol")
    parser.add_argument("--ledger", type=Path, default=Path("data/simulation_ledger.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("data/latest_simulation_metrics.json"))
    parser.add_argument("--simulation-policy", type=Path, default=DEFAULT_SIMULATION_POLICY)
    args = parser.parse_args()
    pack = load_object(args.market_pack)
    plans = [dict(item) for item in pack.get("plans", []) if isinstance(item, dict)]
    bars = {str(key): value for key, value in (pack.get("bars_by_symbol") or {}).items() if isinstance(value, list)}
    if not plans or not bars:
        raise ValueError("market pack requires non-empty plans and bars_by_symbol")
    policy = load_risk_policy()
    simulation_policy = load_object(args.simulation_policy)
    risk_budget = simulation_policy.get("risk_budget") if isinstance(simulation_policy.get("risk_budget"), dict) else {}
    snapshot_time = str(pack.get("as_of") or pack.get("generated_at") or "unknown")
    with SimulationLedger(args.ledger) as ledger:
        snapshot_id = ledger.record_snapshot(pack, captured_at=snapshot_time, source_version=str(pack.get("source_version") or "offline-pack-v1"))
        for item in plans:
            path = build_entry_path(
                str(item.get("strategy_family") or "formal"), item.get("entry_price"), item.get("stop_loss"), item.get("target_price"),
                float(item.get("rr_required") or policy.minimum_cost_adjusted_rr),
            )
            ids = build_signal_identifiers(
                item, path, signal_time=item.get("signal_time"), strategy_id=str(item.get("strategy_id") or "fundamental_pullback_v1"),
                data_snapshot_id=snapshot_id,
            )
            item.update(ids)
            item.setdefault("plan_qualified", path.valid)
            if ledger.record_signal(item):
                ledger.append_event(item["signal_id"], "observed", str(item.get("signal_time") or "unknown"))
        result = simulate_portfolio(
            plans,
            bars,
            initial_cash=float(simulation_policy.get("initial_cash") or 100000.0),
            risk_per_trade_pct=float(risk_budget.get("per_trade_loss_pct") or 0.005),
            commission_per_order=float(simulation_policy.get("commission_per_order") or 1.0),
            slippage_bps=float(simulation_policy.get("slippage_bps") or 5.0),
            spread_bps=float(simulation_policy.get("spread_bps") or 2.0),
            minimum_lot=int(simulation_policy.get("minimum_lot") or 1),
            max_volume_participation=float(simulation_policy.get("max_volume_participation") or 0.05),
            tif_sessions=int(simulation_policy.get("default_order_tif_sessions") or 5),
            benchmark_equity=pack.get("benchmark_equity"),
            benchmarks=pack.get("benchmarks") if isinstance(pack.get("benchmarks"), dict) else None,
        )
        for signal_result in result["signal_results"]:
            signal_id = signal_result["signal_id"]
            for event in signal_result["states"]:
                ledger.append_event(
                    signal_id,
                    str(event.get("state") or "unknown"),
                    str(event.get("time") or snapshot_time),
                    {key: value for key, value in event.items() if key not in {"state", "time"}},
                )
        for fill in result["fills"]:
            ledger.record_fill(fill)
        for point in result["equity_curve"]:
            ledger.record_equity(point)
        result["ledger"] = ledger.export_public_summary()
        result["policy_version"] = policy.policy_version
        result["model_version"] = str(load_model_config().get("model_version") or "unknown")
        result["simulation_policy_version"] = str(simulation_policy.get("policy_version") or "unknown")
        result["input_snapshot_id"] = snapshot_id
        result["run_hash"] = canonical_content_hash({"snapshot": snapshot_id, "policy": result["policy_version"], "model": result["model_version"]})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"simulation complete: {result['trade_count']} trades, {len(result['equity_curve'])} equity points")
    print(f"wrote {args.output} and {args.ledger}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
