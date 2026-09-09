#!/usr/bin/env python3
"""Stream public-safe Futu quotes to the authenticated cloud bridge.

The process keeps one OpenQuoteContext alive, consumes QUOTE callbacks, and
uploads coalesced snapshots. It never opens a trade context or reads account,
position, order, cash, share-count, or cost-basis data.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from futu_cloud_bridge import push_snapshot_payload
from sync_futu_local_snapshot import (
    DEFAULT_OUTPUT,
    DEFAULT_STATUS,
    beijing_timezone,
    canonical_quote_time,
    configured_symbols,
    futu_code,
    load_json,
    now_local,
    quote_record,
    unique,
    write_json,
)


NEW_YORK = ZoneInfo("America/New_York")
MAX_DEFAULT_SUBSCRIPTIONS = 100
PUBLIC_INDEX = Path(__file__).resolve().parents[1] / "docs" / "data" / "index.json"


def windows_single_instance() -> tuple[object | None, bool]:
    if not hasattr(ctypes, "WinDLL"):
        return None, True
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, True, "Local\\USStockFutuQuoteBridge")
    return handle, bool(handle and ctypes.get_last_error() != 183)


def currency_for(code: str) -> str:
    prefix = code.split(".", 1)[0].upper()
    return {
        "US": "USD",
        "HK": "HKD",
        "SH": "CNY",
        "SZ": "CNY",
        "CN": "CNY",
    }.get(prefix, "")


def market_session(code: str, value: datetime | None = None) -> str:
    """Return a conservative clock session; timestamp freshness stays decisive."""
    current = value or now_local()
    prefix = code.split(".", 1)[0].upper()
    if prefix == "US":
        local = current.astimezone(NEW_YORK)
        if local.weekday() >= 5:
            return "closed"
        minute = local.hour * 60 + local.minute
        if minute >= 20 * 60 or minute < 4 * 60:
            return "overnight"
        if minute < 9 * 60 + 30:
            return "pre_market"
        if minute < 16 * 60:
            return "regular"
        return "after_hours"

    local = current.astimezone(beijing_timezone())
    if local.weekday() >= 5:
        return "closed"
    minute = local.hour * 60 + local.minute
    if prefix == "HK":
        return "regular" if (
            9 * 60 + 30 <= minute < 12 * 60
            or 13 * 60 <= minute < 16 * 60
        ) else "closed"
    if prefix in {"CN", "SH", "SZ"}:
        return "regular" if (
            9 * 60 + 30 <= minute < 11 * 60 + 30
            or 13 * 60 <= minute < 15 * 60
        ) else "closed"
    return "unknown"


def positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def selected_live_field(
    code: str,
    row: dict[str, Any],
    received_at: datetime,
) -> tuple[str, float | None, str]:
    session = market_session(code, received_at)
    field = {
        "overnight": "overnight_price",
        "pre_market": "pre_price",
        "regular": "last_price",
        "after_hours": "after_price",
    }.get(session, "last_price")
    price = positive(row.get(field))
    if price is not None:
        return field, price, session
    # Missing extended quotes fall back to an exchange-timestamped regular price,
    # never to a receive-timestamped pseudo-live value.
    fallback = positive(row.get("last_price") or row.get("cur_price"))
    return "last_price", fallback, "closed" if session != "regular" else session


def enrich_quote(
    raw_row: dict[str, Any],
    *,
    received_at: datetime,
    previous: dict[str, Any] | None,
    from_push: bool,
) -> dict[str, Any]:
    code = str(raw_row.get("code") or "").strip().upper()
    record = quote_record(raw_row)
    exchange_time = canonical_quote_time(code, raw_row)
    field, live_price, session = selected_live_field(code, raw_row, received_at)
    receipt_time = received_at.astimezone(beijing_timezone()).isoformat(timespec="seconds")
    extended = field in {"pre_price", "after_price", "overnight_price"}
    previous_price = positive((previous or {}).get(field))
    previously_confirmed = (previous or {}).get("push_confirmed") is True
    changed = (
        live_price is not None
        and previous_price is not None
        and abs(live_price - previous_price) > 1e-12
    )

    record.update({
        "code": code,
        "currency": currency_for(code),
        "exchange_quote_time": exchange_time,
        "received_at": receipt_time,
        "live_price": live_price,
        "live_session": session,
        "timestamp_kind": "transport_receipt" if extended else "exchange",
        "push_confirmed": bool(
            from_push
            and ((previously_confirmed or changed) if extended else exchange_time)
        ),
        "quote_transport": "futu-opend-push" if from_push else "futu-opend-snapshot",
        "source": "Futu OpenD subscribed quote",
    })
    if not extended and exchange_time:
        record["live_quote_time"] = exchange_time
    else:
        record.pop("live_quote_time", None)
    return {key: value for key, value in record.items() if value is not None}


def public_opportunity_symbols(path: Path = PUBLIC_INDEX) -> list[str]:
    archive = load_json(path, {})
    items = archive.get("opportunities") if isinstance(archive, dict) else []
    return unique([
        str(item.get("symbol") or "")
        for item in (items or [])
        if isinstance(item, dict) and item.get("symbol")
    ])


def priority_codes(scope: str, limit: int) -> tuple[list[str], list[str]]:
    symbols = unique(
        public_opportunity_symbols()
        + configured_symbols("core")
        + configured_symbols(scope)
    )
    codes = unique([code for symbol in symbols if (code := futu_code(symbol))])
    safe_limit = max(0, int(limit))
    return codes[:safe_limit], codes[safe_limit:]


class QuoteBook:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._quotes: dict[str, dict[str, Any]] = {}
        self.revision = 0
        self.last_error = ""

    def update_rows(self, rows: list[dict[str, Any]], *, from_push: bool) -> None:
        received = now_local()
        changed_any = False
        with self._lock:
            for row in rows:
                if not isinstance(row, dict) or not row.get("code"):
                    continue
                code = str(row.get("code")).strip().upper()
                previous = self._quotes.get(code)
                updated = enrich_quote(
                    row,
                    received_at=received,
                    previous=previous,
                    from_push=from_push,
                )
                if updated != previous:
                    changed_any = True
                self._quotes[code] = updated
            if changed_any:
                self.revision += 1

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._quotes)

    def set_error(self, message: Any) -> None:
        with self._lock:
            self.last_error = str(message or "")[:500]


def rows_from(value: Any) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict"):
        rows = value.to_dict(orient="records")
        return [row for row in rows if isinstance(row, dict)]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def initial_snapshot(
    quote_ctx: Any,
    book: QuoteBook,
    codes: list[str],
    ret_ok: int,
) -> list[str]:
    errors: list[str] = []
    for index in range(0, len(codes), 200):
        ret, data = quote_ctx.get_market_snapshot(codes[index:index + 200])
        if ret == ret_ok:
            book.update_rows(rows_from(data), from_push=False)
        else:
            errors.append(str(data)[:300])
    return errors


def available_subscription_quota(
    quote_ctx: Any,
    ret_ok: int,
    default: int = MAX_DEFAULT_SUBSCRIPTIONS,
) -> int:
    try:
        ret, data = quote_ctx.query_subscription(is_all_conn=True)
        if ret == ret_ok and isinstance(data, dict):
            return max(0, int(data.get("remain", default)))
    except Exception:
        pass
    return default


def subscribe_codes(
    quote_ctx: Any,
    codes: list[str],
    subtype: Any,
    ret_ok: int,
) -> tuple[list[str], list[str]]:
    subscribed: list[str] = []
    errors: list[str] = []
    groups = (
        [code for code in codes if code.startswith("US.")],
        [code for code in codes if not code.startswith("US.")],
    )
    for group in groups:
        if not group:
            continue
        extended = group[0].startswith("US.")
        for index in range(0, len(group), 25):
            chunk = group[index:index + 25]
            ret, data = quote_ctx.subscribe(
                chunk,
                [subtype],
                is_first_push=True,
                subscribe_push=True,
                extended_time=extended,
            )
            if ret == ret_ok:
                subscribed.extend(chunk)
                continue
            # Retry individually so one market/symbol permission failure cannot
            # disable every other quote.
            for code in chunk:
                single_ret, single_data = quote_ctx.subscribe(
                    [code],
                    [subtype],
                    is_first_push=True,
                    subscribe_push=True,
                    extended_time=extended,
                )
                if single_ret == ret_ok:
                    subscribed.append(code)
                else:
                    errors.append(f"{code}: {str(single_data)[:180]}")
    return unique(subscribed), errors


def build_stream_payload(
    book: QuoteBook,
    args: argparse.Namespace,
    subscribed: list[str],
    skipped: list[str],
    errors: list[str],
) -> dict[str, Any]:
    current = now_local()
    quotes = book.snapshot()
    return {
        "schema_version": 3,
        "generated_at": current.isoformat(timespec="seconds"),
        "generated_label": current.strftime("%Y-%m-%d %H:%M:%S"),
        "opend": {
            "enabled": True,
            "connected": True,
            "market_data_only": True,
            "message": "Futu OpenD subscribed quote stream connected.",
        },
        "summary": {
            "scope": args.scope,
            "quotes_returned": len(quotes),
            "subscribed": len(subscribed),
            "skipped_due_quota": len(skipped),
            "subscription_errors": len(errors),
        },
        "privacy": {
            "contains_account": False,
            "contains_positions": False,
            "contains_cash": False,
            "contains_cost_basis": False,
            "trading_disabled": True,
        },
        "cloud_policy": {
            "transport": "authenticated HTTPS quote-only bridge",
            "secrets_embedded": False,
            "extended_session_time_semantics": (
                "transport receipt is not an exchange trade timestamp"
            ),
        },
        "subscription": {
            "requested": len(subscribed) + len(skipped),
            "subscribed_codes": subscribed,
            "skipped_due_quota": skipped,
            "errors": errors[:30],
        },
        "quotes": quotes,
    }


def write_failure_status(message: Any, *, reconnect_seconds: int) -> None:
    current = now_local()
    write_json(DEFAULT_STATUS, {
        "schema_version": 3,
        "generated_at": current.isoformat(timespec="seconds"),
        "opend": {
            "enabled": True,
            "connected": False,
            "market_data_only": True,
            "message": str(message or "unknown OpenD error")[:500],
        },
        "reconnect_in_seconds": reconnect_seconds,
        "privacy": {
            "trading_disabled": True,
            "secrets_embedded": False,
        },
    })


def upload(
    book: QuoteBook,
    args: argparse.Namespace,
    subscribed: list[str],
    skipped: list[str],
    errors: list[str],
) -> bool:
    payload = build_stream_payload(book, args, subscribed, skipped, errors)
    write_json(DEFAULT_OUTPUT, payload)
    status: dict[str, Any] = {
        "schema_version": payload["schema_version"],
        "generated_at": payload["generated_at"],
        "opend": payload["opend"],
        "summary": payload["summary"],
        "privacy": payload["privacy"],
        "last_callback_error": book.last_error,
    }
    try:
        result = push_snapshot_payload(payload)
        status["cloud"] = {
            "uploaded": True,
            "received_at": result.get("received_at"),
            "quotes_returned": result.get("quotes_returned"),
            "storage": result.get("storage"),
            "live_alerts": result.get("live_alerts"),
        }
        write_json(DEFAULT_STATUS, status)
        return True
    except Exception as exc:  # noqa: BLE001 - retain stream and retry safely
        status["cloud"] = {
            "uploaded": False,
            "message": str(exc)[:500],
        }
        write_json(DEFAULT_STATUS, status)
        return False


def run_stream(args: argparse.Namespace) -> None:
    from futu import (  # type: ignore
        OpenQuoteContext,
        RET_OK,
        StockQuoteHandlerBase,
        SubType,
    )

    book = QuoteBook()

    class QuoteHandler(StockQuoteHandlerBase):
        def on_recv_rsp(self, rsp_pb: Any) -> tuple[int, Any]:
            ret, data = super().on_recv_rsp(rsp_pb)
            if ret == RET_OK:
                book.update_rows(rows_from(data), from_push=True)
            else:
                book.set_error(data)
            return ret, data

    quote_ctx = OpenQuoteContext(
        host=args.host,
        port=args.port,
        is_async_connect=False,
    )
    try:
        quota = min(
            args.max_subscriptions,
            available_subscription_quota(
                quote_ctx,
                RET_OK,
                args.max_subscriptions,
            ),
        )
        requested, skipped = priority_codes(args.scope, quota)
        if not requested:
            raise RuntimeError(
                "No eligible public symbols or Futu subscription quota is available"
            )
        snapshot_errors = initial_snapshot(
            quote_ctx,
            book,
            requested,
            RET_OK,
        )
        quote_ctx.set_handler(QuoteHandler())
        subscribed, subscription_errors = subscribe_codes(
            quote_ctx,
            requested,
            SubType.QUOTE,
            RET_OK,
        )
        errors = snapshot_errors + subscription_errors
        if not subscribed:
            raise RuntimeError(
                "No Futu QUOTE subscriptions succeeded: "
                + "; ".join(errors[:3])
            )

        upload(book, args, subscribed, skipped, errors)
        if args.once:
            return
        last_revision = book.revision
        last_upload = time.monotonic()
        last_health_check = time.monotonic()
        retry_after_failure = False
        while True:
            time.sleep(1)
            now_tick = time.monotonic()
            changed = book.revision != last_revision
            heartbeat_due = (
                now_tick - last_upload >= args.heartbeat_seconds
            )
            change_due = (
                changed
                and now_tick - last_upload >= args.interval_seconds
            )
            retry_due = (
                retry_after_failure
                and now_tick - last_upload >= args.interval_seconds
            )
            if change_due or heartbeat_due or retry_due:
                retry_after_failure = not upload(
                    book,
                    args,
                    subscribed,
                    skipped,
                    errors,
                )
                last_revision = book.revision
                last_upload = now_tick
            if now_tick - last_health_check >= 30:
                ret, data = quote_ctx.get_global_state()
                if ret != RET_OK:
                    raise RuntimeError(f"OpenD health check failed: {data}")
                last_health_check = now_tick
    finally:
        try:
            quote_ctx.close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope",
        choices=("core", "universe", "all"),
        default="all",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=15,
        help="minimum changed-quote upload interval",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=int,
        default=60,
        help="cloud heartbeat when no quote changes",
    )
    parser.add_argument("--reconnect-seconds", type=int, default=5)
    parser.add_argument(
        "--max-subscriptions",
        type=int,
        default=MAX_DEFAULT_SUBSCRIPTIONS,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="connect, upload once, and exit",
    )
    args = parser.parse_args()
    args.interval_seconds = max(5, args.interval_seconds)
    args.heartbeat_seconds = max(30, args.heartbeat_seconds)
    args.reconnect_seconds = max(2, args.reconnect_seconds)
    args.max_subscriptions = max(
        1,
        min(250, args.max_subscriptions),
    )

    _mutex, acquired = windows_single_instance()
    if not acquired:
        return 0
    while True:
        try:
            run_stream(args)
            if args.once:
                return 0
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # noqa: BLE001 - reconnect after OpenD restarts
            write_failure_status(
                exc,
                reconnect_seconds=args.reconnect_seconds,
            )
            if args.once:
                return 1
            time.sleep(args.reconnect_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
