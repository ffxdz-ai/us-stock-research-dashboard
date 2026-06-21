#!/usr/bin/env python3
"""Sync a public-safe local Futu OpenD quote snapshot.

This is the local side of the hybrid data plan:

- When the user's PC is on and OpenD is logged in, collect broker quote snapshots.
- Store only market data and connection status.
- Do not query accounts, positions, orders, cash, costs, shares, or trading unlock state.
- When OpenD is unavailable, still write a status file and exit successfully unless
  --strict is supplied, so cloud/public fallbacks can continue.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DEFAULT_OUTPUT = DATA_DIR / "latest_futu_local_snapshot.json"
DEFAULT_STATUS = DATA_DIR / "latest_futu_local_status.json"

EXCHANGE_PREFIXES = {"US", "HK", "SH", "SZ", "SG", "MY", "JP", "CC"}
PUBLIC_QUOTE_FIELDS = [
    "code",
    "name",
    "last_price",
    "cur_price",
    "prev_close_price",
    "open_price",
    "high_price",
    "low_price",
    "volume",
    "turnover",
    "turnover_rate",
    "change_rate",
    "update_time",
    "data_date",
    "data_time",
    "pe_ttm",
    "pb_rate",
    "ps_ttm",
    "market_val",
    "pre_price",
    "after_price",
    "overnight_price",
]


def beijing_timezone() -> timezone:
    try:
        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return timezone(timedelta(hours=8), "Asia/Shanghai")


def now_local() -> datetime:
    return datetime.now(beijing_timezone())


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
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_symbol(symbol: Any) -> str:
    return str(symbol or "").strip().upper()


def futu_code(symbol: Any) -> str | None:
    raw = normalize_symbol(symbol)
    if not raw or raw.startswith("^"):
        return None
    if "." in raw:
        prefix, rest = raw.split(".", 1)
        if prefix in EXCHANGE_PREFIXES and rest:
            return raw
        # Avoid guessing ambiguous US tickers such as BRK.B.
        return None
    return f"US.{raw}"


def display_symbol(code: str) -> str:
    if code.startswith("US."):
        return code.split(".", 1)[1]
    return code


def unique(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def configured_symbols(scope: str) -> list[str]:
    config = load_json(ROOT / "config" / "agent_config.json", {})
    portfolio = load_json(ROOT / "config" / "portfolio.json", {})
    symbols: list[str] = []
    symbols.extend(str(item) for item in config.get("market_symbols", []) if item)
    holdings = portfolio.get("holdings") if isinstance(portfolio.get("holdings"), list) else []
    symbols.extend(str(item.get("ticker")) for item in holdings if isinstance(item, dict) and item.get("ticker"))
    watchlist = portfolio.get("watchlist") if isinstance(portfolio.get("watchlist"), list) else []
    symbols.extend(str(item) for item in watchlist if item)
    if scope in {"universe", "all"}:
        symbols.extend(str(item) for item in config.get("universe", []) if item)
    if scope == "all":
        symbols.extend(str(item) for item in config.get("cross_market_supply_chain_universe", []) if item)
    return unique(symbols)


def connection_status(host: str, port: int) -> dict[str, Any]:
    if os.getenv("FUTU_QUOTE_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
        return {
            "enabled": False,
            "connected": False,
            "host": host,
            "port": port,
            "message": "Futu quote disabled by FUTU_QUOTE_DISABLED.",
        }
    try:
        with socket.create_connection((host, port), timeout=0.7):
            return {
                "enabled": True,
                "connected": True,
                "host": host,
                "port": port,
                "message": "Futu OpenD socket connected.",
            }
    except OSError as exc:
        return {
            "enabled": True,
            "connected": False,
            "host": host,
            "port": port,
            "message": f"Futu OpenD socket not reachable: {exc}",
        }


def clean_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        # pandas NA/NaN support without importing pandas here.
        if value != value:
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def quote_record(row: dict[str, Any]) -> dict[str, Any]:
    code = str(row.get("code") or "").upper()
    record = {
        key: clean_value(row.get(key))
        for key in PUBLIC_QUOTE_FIELDS
        if key in row and clean_value(row.get(key)) is not None
    }
    record["code"] = code
    record["symbol"] = display_symbol(code)
    record["source"] = "Futu OpenD local public-safe quote snapshot"
    return record


def collect_futu_snapshot(codes: list[str], host: str, port: int) -> tuple[dict[str, dict[str, Any]], str | None]:
    try:
        from futu import OpenQuoteContext, RET_OK  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {}, f"futu-api unavailable: {exc}"

    if not codes:
        return {}, None

    quote_ctx = None
    try:
        quote_ctx = OpenQuoteContext(host=host, port=port, is_async_connect=False)
        quotes: dict[str, dict[str, Any]] = {}
        for index in range(0, len(codes), 200):
            chunk = codes[index : index + 200]
            ret, data = quote_ctx.get_market_snapshot(chunk)
            if ret != RET_OK:
                return quotes, str(data)
            rows = data.to_dict(orient="records") if hasattr(data, "to_dict") else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                record = quote_record(row)
                code = str(record.get("code") or "")
                if code:
                    quotes[code] = record
        return quotes, None
    except Exception as exc:  # noqa: BLE001
        return {}, str(exc)
    finally:
        if quote_ctx is not None:
            try:
                quote_ctx.close()
            except Exception:
                pass


def public_status(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": payload.get("schema_version"),
        "generated_at": payload.get("generated_at"),
        "generated_label": payload.get("generated_label"),
        "opend": payload.get("opend"),
        "summary": payload.get("summary"),
        "privacy": payload.get("privacy"),
        "cloud_policy": payload.get("cloud_policy"),
    }


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.getenv("FUTU_OPEND_PORT", "11111").strip() or "11111")
    except ValueError:
        port = 11111

    requested_symbols = configured_symbols(args.scope)
    codes = unique([code for symbol in requested_symbols if (code := futu_code(symbol))])
    status = connection_status(host, port)
    quotes: dict[str, dict[str, Any]] = {}
    error: str | None = None
    if status.get("connected"):
        quotes, error = collect_futu_snapshot(codes, host, port)
        if error:
            status = {**status, "connected": False, "message": f"Futu snapshot failed: {error}"}

    generated = now_local()
    return {
        "schema_version": 1,
        "generated_at": generated.isoformat(timespec="seconds"),
        "generated_label": generated.strftime("%Y-%m-%d %H:%M"),
        "opend": status,
        "summary": {
            "scope": args.scope,
            "symbols_requested": len(requested_symbols),
            "codes_requested": len(codes),
            "quotes_returned": len(quotes),
            "skipped_symbols": len(requested_symbols) - len(codes),
        },
        "privacy": {
            "contains_account": False,
            "contains_positions": False,
            "contains_cash": False,
            "contains_cost_basis": False,
            "trading_disabled": True,
            "note": "Only public market quote fields are stored. No account, order, cash, share count, or cost basis data is queried.",
        },
        "cloud_policy": {
            "role": "local Futu data enhancement when this PC is on",
            "fallback": "cloud jobs must continue with public data when this snapshot is missing or stale",
            "do_not_expose_opend_port": True,
        },
        "quotes": quotes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("core", "universe", "all"), default="core")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--status-out", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when OpenD is unavailable or no quote is returned.")
    args = parser.parse_args()

    payload = build_payload(args)
    write_json(args.out, payload)
    write_json(args.status_out, public_status(payload))

    connected = bool((payload.get("opend") or {}).get("connected"))
    quotes_returned = int((payload.get("summary") or {}).get("quotes_returned") or 0)
    print(f"Wrote {args.out} ({quotes_returned} Futu quotes; connected={connected})")
    print(f"Wrote {args.status_out}")
    if args.strict and (not connected or quotes_returned == 0):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
