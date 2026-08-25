#!/usr/bin/env python3
"""Refresh quote-only Futu bridge snapshots invisibly while this user is logged in."""

from __future__ import annotations

import argparse
import ctypes
import time

from futu_cloud_bridge import push_snapshot_payload
from sync_futu_local_snapshot import (
    DEFAULT_OUTPUT,
    DEFAULT_STATUS,
    build_payload,
    public_status,
    write_json,
)


def windows_single_instance() -> tuple[object | None, bool]:
    if not hasattr(ctypes, "WinDLL"):
        return None, True
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, True, "Local\\USStockFutuQuoteBridge")
    return handle, bool(handle and ctypes.get_last_error() != 183)


def refresh_snapshot(scope: str) -> None:
    payload = build_payload(argparse.Namespace(scope=scope))
    write_json(DEFAULT_OUTPUT, payload)
    write_json(DEFAULT_STATUS, public_status(payload))
    if (payload.get("opend") or {}).get("connected") and (payload.get("summary") or {}).get("quotes_returned"):
        push_snapshot_payload(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("core", "universe", "all"), default="all")
    parser.add_argument("--interval-seconds", type=int, default=300)
    args = parser.parse_args()
    _mutex, acquired = windows_single_instance()
    if not acquired:
        return 0
    while True:
        try:
            refresh_snapshot(args.scope)
        except Exception:  # noqa: BLE001 - background data enhancement must self-recover
            pass
        time.sleep(max(60, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
