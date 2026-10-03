#!/usr/bin/env python3
"""Local immutable research/simulation ledger backed by SQLite."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from model_v2 import canonical_content_hash


SCHEMA_VERSION = 1


class SimulationLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SimulationLedger":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS data_snapshots (
              snapshot_id TEXT PRIMARY KEY,
              captured_at TEXT NOT NULL,
              source_version TEXT NOT NULL,
              content_hash TEXT NOT NULL UNIQUE,
              payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signals (
              signal_id TEXT PRIMARY KEY,
              plan_id TEXT NOT NULL,
              symbol TEXT NOT NULL,
              strategy_id TEXT NOT NULL,
              signal_time TEXT NOT NULL,
              data_snapshot_id TEXT NOT NULL,
              policy_version TEXT NOT NULL,
              model_version TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(data_snapshot_id) REFERENCES data_snapshots(snapshot_id)
            );
            CREATE TABLE IF NOT EXISTS signal_events (
              event_id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_key TEXT NOT NULL UNIQUE,
              signal_id TEXT NOT NULL,
              state TEXT NOT NULL,
              event_time TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              FOREIGN KEY(signal_id) REFERENCES signals(signal_id)
            );
            CREATE TABLE IF NOT EXISTS fills (
              fill_id TEXT PRIMARY KEY,
              signal_id TEXT NOT NULL,
              symbol TEXT NOT NULL,
              side TEXT NOT NULL CHECK(side IN ('buy','sell')),
              quantity INTEGER NOT NULL CHECK(quantity > 0),
              price REAL NOT NULL CHECK(price > 0),
              fees REAL NOT NULL CHECK(fees >= 0),
              fill_time TEXT NOT NULL,
              reason TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              FOREIGN KEY(signal_id) REFERENCES signals(signal_id)
            );
            CREATE TABLE IF NOT EXISTS equity_curve (
              point_time TEXT PRIMARY KEY,
              cash REAL NOT NULL,
              market_value REAL NOT NULL,
              equity REAL NOT NULL,
              realized_pnl REAL NOT NULL,
              unrealized_pnl REAL NOT NULL,
              fees REAL NOT NULL,
              payload_json TEXT NOT NULL
            );
            """
        )
        current = self.connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if current and int(current["value"]) > SCHEMA_VERSION:
            raise RuntimeError("ledger schema is newer than this code")
        self.connection.execute(
            "INSERT INTO metadata(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    @staticmethod
    def _json(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    def record_snapshot(self, payload: dict[str, Any], *, captured_at: str, source_version: str) -> str:
        content_hash = canonical_content_hash(payload)
        snapshot_id = f"snapshot-{content_hash[:24]}"
        self.connection.execute(
            "INSERT OR IGNORE INTO data_snapshots(snapshot_id,captured_at,source_version,content_hash,payload_json) VALUES(?,?,?,?,?)",
            (snapshot_id, captured_at, source_version, content_hash, self._json(payload)),
        )
        return snapshot_id

    def record_signal(self, signal: dict[str, Any]) -> bool:
        required = ["signal_id", "plan_id", "symbol", "strategy_id", "signal_time", "data_snapshot_id", "policy_version", "model_version"]
        missing = [key for key in required if not signal.get(key)]
        if missing:
            raise ValueError(f"signal missing fields: {missing}")
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO signals(
                 signal_id,plan_id,symbol,strategy_id,signal_time,data_snapshot_id,policy_version,model_version,payload_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                signal["signal_id"], signal["plan_id"], signal["symbol"], signal["strategy_id"], signal["signal_time"],
                signal["data_snapshot_id"], signal["policy_version"], signal["model_version"], self._json(signal),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cursor.rowcount == 1

    def append_event(self, signal_id: str, state: str, event_time: str, payload: dict[str, Any] | None = None) -> bool:
        payload = payload or {}
        event_key = canonical_content_hash({"signal_id": signal_id, "state": state, "event_time": event_time, "payload": payload})
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO signal_events(event_key,signal_id,state,event_time,payload_json) VALUES(?,?,?,?,?)",
            (event_key, signal_id, state, event_time, self._json(payload)),
        )
        return cursor.rowcount == 1

    def record_fill(self, fill: dict[str, Any]) -> bool:
        fill_id = str(fill.get("fill_id") or canonical_content_hash(fill)[:24])
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO fills(fill_id,signal_id,symbol,side,quantity,price,fees,fill_time,reason,payload_json)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                fill_id, fill["signal_id"], fill["symbol"], fill["side"], int(fill["quantity"]), float(fill["price"]),
                float(fill.get("fees") or 0), fill["fill_time"], str(fill.get("reason") or ""), self._json(fill),
            ),
        )
        return cursor.rowcount == 1

    def record_equity(self, point: dict[str, Any]) -> None:
        fields = ("cash", "market_value", "equity", "realized_pnl", "unrealized_pnl", "fees")
        if abs(float(point["cash"]) + float(point["market_value"]) - float(point["equity"])) > 1e-6:
            raise ValueError("equity point does not reconcile: cash + market_value != equity")
        self.connection.execute(
            """INSERT INTO equity_curve(point_time,cash,market_value,equity,realized_pnl,unrealized_pnl,fees,payload_json)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(point_time) DO UPDATE SET
               cash=excluded.cash,market_value=excluded.market_value,equity=excluded.equity,
               realized_pnl=excluded.realized_pnl,unrealized_pnl=excluded.unrealized_pnl,fees=excluded.fees,payload_json=excluded.payload_json""",
            (point["point_time"], *(float(point[key]) for key in fields), self._json(point)),
        )

    def counts(self) -> dict[str, int]:
        return {
            table: int(self.connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            for table in ("data_snapshots", "signals", "signal_events", "fills", "equity_curve")
        }

    def export_public_summary(self) -> dict[str, Any]:
        counts = self.counts()
        return {
            "schema_version": SCHEMA_VERSION,
            "signal_count": counts["signals"],
            "event_count": counts["signal_events"],
            "fill_count": counts["fills"],
            "equity_point_count": counts["equity_curve"],
            "privacy": "aggregate_only_no_account_data",
        }
