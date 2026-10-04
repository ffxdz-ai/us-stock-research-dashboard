#!/usr/bin/env python3
"""Gate daily report generation on a new US session and live Futu OpenD.

The dashboard is generated after the US market closes (08:00 Asia/Shanghai).
A workflow may be dispatched every day or manually, but it must not mutate the
public archive unless all of the following are true:

* the most recent US exchange session closed recently;
* that session has not already been reported (unless a manual re-run is used);
* an authenticated cloud snapshot proves Futu OpenD is connected now; and
* at least one US benchmark quote covers the completed session.

The command intentionally exits successfully when a gate is closed.  A closed
gate is a safe no-op, not an infrastructure failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from model_v2 import is_us_trading_day, most_recent_completed_us_session


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = ROOT / "docs" / "data" / "index.json"
DEFAULT_STATE = ROOT / "docs" / "data" / "report_update_state.json"
DEFAULT_SNAPSHOT = ROOT / "data" / "latest_futu_local_snapshot.json"
BEIJING = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")
BENCHMARKS = ("US.SPY", "US.QQQ", "US.DIA", "US.IWM")


@dataclass(frozen=True)
class GateDecision:
    should_update: bool
    code: str
    reason: str
    session_date: str
    previous_session_date: str
    snapshot_age_minutes: float | None = None


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_datetime(value: Any, *, default_tz: ZoneInfo | timezone = timezone.utc) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed.astimezone(timezone.utc)


def parse_session_date(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed if is_us_trading_day(parsed) else None


def report_timestamp(report: dict[str, Any]) -> datetime | None:
    label = str(report.get("published_label") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?", label):
        return parse_datetime(label, default_tz=BEIJING)
    published = parse_datetime(report.get("published_at"), default_tz=BEIJING)
    if published is not None:
        return published
    identifier = str(report.get("id") or "")
    match = re.match(r"^(\d{8})-(\d{4})", identifier)
    if not match:
        return None
    try:
        local = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M").replace(tzinfo=BEIJING)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def latest_reported_session(index: dict[str, Any], state: dict[str, Any]) -> date | None:
    for candidate in (
        state.get("last_completed_session"),
        index.get("market_session_date"),
    ):
        parsed = parse_session_date(candidate)
        if parsed is not None:
            return parsed

    reports = index.get("reports")
    if not isinstance(reports, list):
        return None
    for report in reports:
        if not isinstance(report, dict) or report.get("kind") != "deepseek-cloud":
            continue
        explicit = parse_session_date(report.get("market_session_date"))
        if explicit is not None:
            return explicit
        timestamp = report_timestamp(report)
        if timestamp is not None:
            session, _ = most_recent_completed_us_session(timestamp)
            return session
    return None


def quote_session_date(quote: dict[str, Any]) -> date | None:
    direct = parse_session_date(quote.get("data_date"))
    if direct is not None:
        return direct
    for key in ("exchange_quote_time", "quote_time", "live_quote_time", "update_time"):
        timestamp = parse_datetime(quote.get(key), default_tz=BEIJING)
        if timestamp is not None:
            local_date = timestamp.astimezone(NEW_YORK).date()
            if is_us_trading_day(local_date):
                return local_date
    return None


def snapshot_status(
    snapshot: dict[str, Any],
    *,
    now: datetime,
    session_date: date,
    max_age_minutes: float,
) -> tuple[bool, str, str, float | None]:
    if not snapshot:
        return False, "futu_snapshot_missing", "未取得 Futu OpenD 云端行情快照", None
    if not bool((snapshot.get("opend") or {}).get("connected")):
        return False, "futu_disconnected", "Futu OpenD 未连接", None
    if not bool((snapshot.get("bridge") or {}).get("authenticated")):
        return False, "futu_not_authenticated", "Futu 行情快照未经云端桥接认证", None

    generated_at = parse_datetime(snapshot.get("generated_at"))
    if generated_at is None:
        return False, "futu_timestamp_missing", "Futu 行情快照缺少可信生成时间", None
    age_minutes = (now - generated_at).total_seconds() / 60
    if age_minutes < -5:
        return False, "futu_timestamp_future", "Futu 行情快照时间异常（来自未来）", round(age_minutes, 2)
    if age_minutes > max_age_minutes:
        return (
            False,
            "futu_snapshot_stale",
            f"Futu OpenD 心跳已过期（{age_minutes:.1f} 分钟）",
            round(age_minutes, 2),
        )

    quotes = snapshot.get("quotes")
    if not isinstance(quotes, dict) or not quotes:
        return False, "futu_quotes_empty", "Futu OpenD 已连接但行情快照为空", round(age_minutes, 2)

    benchmark_dates = {
        symbol: quote_session_date(quotes.get(symbol) or {})
        for symbol in BENCHMARKS
        if isinstance(quotes.get(symbol), dict)
    }
    if not any(value == session_date for value in benchmark_dates.values()):
        covered = sorted({str(value) for value in benchmark_dates.values() if value is not None})
        suffix = f"（现有基准行情日期：{', '.join(covered)}）" if covered else ""
        return (
            False,
            "futu_session_not_covered",
            f"Futu 基准行情尚未覆盖 {session_date.isoformat()} 美股交易日{suffix}",
            round(age_minutes, 2),
        )
    return True, "eligible", "Futu OpenD 在线且行情覆盖最新交易日", round(age_minutes, 2)


def evaluate_gate(
    *,
    now: datetime,
    index: dict[str, Any],
    state: dict[str, Any],
    snapshot: dict[str, Any],
    force: bool = False,
    max_snapshot_age_minutes: float = 5.0,
    max_session_close_age_hours: float = 16.0,
) -> GateDecision:
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    session_date, close_at = most_recent_completed_us_session(current)
    previous = latest_reported_session(index, state)
    previous_text = previous.isoformat() if previous else ""
    close_age_hours = (current - close_at).total_seconds() / 3600

    # This window maps the 08:00 Beijing run to the US session that just
    # finished, while rejecting Sunday/Monday and post-holiday stale sessions.
    if close_age_hours < 0 or close_age_hours > max_session_close_age_hours:
        return GateDecision(
            False,
            "outside_post_session_window",
            (
                f"最近美股交易日 {session_date.isoformat()} 已收盘 {close_age_hours:.1f} 小时，"
                "当前不是日报更新窗口"
            ),
            session_date.isoformat(),
            previous_text,
        )

    if not force and previous is not None and previous >= session_date:
        return GateDecision(
            False,
            "session_already_reported",
            f"美股交易日 {session_date.isoformat()} 已生成过报告",
            session_date.isoformat(),
            previous_text,
        )

    healthy, code, reason, age_minutes = snapshot_status(
        snapshot,
        now=current,
        session_date=session_date,
        max_age_minutes=max_snapshot_age_minutes,
    )
    if not healthy:
        return GateDecision(
            False,
            code,
            reason,
            session_date.isoformat(),
            previous_text,
            age_minutes,
        )

    rerun = "（同一交易日手动重跑）" if force and previous == session_date else ""
    return GateDecision(
        True,
        "eligible",
        f"美股交易日 {session_date.isoformat()} 已完成，Futu OpenD 在线{rerun}",
        session_date.isoformat(),
        previous_text,
        age_minutes,
    )


def bool_value(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def write_github_output(path: Path, decision: GateDecision) -> None:
    values = {
        "should_update": "true" if decision.should_update else "false",
        "code": decision.code,
        "reason": decision.reason,
        "session_date": decision.session_date,
        "previous_session_date": decision.previous_session_date,
        "snapshot_age_minutes": "" if decision.snapshot_age_minutes is None else decision.snapshot_age_minutes,
    }
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            clean = str(value).replace("\r", " ").replace("\n", " ")
            output.write(f"{key}={clean}\n")


def stamp_state(path: Path, *, session_date: date, snapshot: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": 1,
        "market": "US",
        "last_completed_session": session_date.isoformat(),
        "updated_at": now.isoformat(timespec="seconds"),
        "update_policy": "completed US trading session plus authenticated live Futu OpenD",
        "futu_snapshot_generated_at": snapshot.get("generated_at"),
        "futu_bridge_authenticated": bool((snapshot.get("bridge") or {}).get("authenticated")),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="evaluate the non-mutating report gate")
    check.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    check.add_argument("--state", type=Path, default=DEFAULT_STATE)
    check.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    check.add_argument("--now", help="ISO timestamp override for deterministic tests")
    check.add_argument("--force", default="false", help="allow a same-session manual re-run")
    check.add_argument("--max-snapshot-age-minutes", type=float, default=5.0)
    check.add_argument("--max-session-close-age-hours", type=float, default=16.0)
    check.add_argument("--github-output", type=Path)

    stamp = subparsers.add_parser("stamp", help="record the successfully published market session")
    stamp.add_argument("--state", type=Path, default=DEFAULT_STATE)
    stamp.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    stamp.add_argument("--session-date", required=True)

    args = parser.parse_args()
    if args.command == "check":
        now = parse_datetime(args.now) if args.now else datetime.now(timezone.utc)
        if now is None:
            parser.error("--now must be a valid ISO timestamp")
        decision = evaluate_gate(
            now=now,
            index=load_json(args.index),
            state=load_json(args.state),
            snapshot=load_json(args.snapshot),
            force=bool_value(args.force),
            max_snapshot_age_minutes=max(0.5, args.max_snapshot_age_minutes),
            max_session_close_age_hours=max(1.0, args.max_session_close_age_hours),
        )
        if args.github_output:
            write_github_output(args.github_output, decision)
        print(json.dumps(asdict(decision), ensure_ascii=False, indent=2))
        return 0

    session_date = parse_session_date(args.session_date)
    if session_date is None:
        parser.error("--session-date must be a valid US trading date")
    snapshot = load_json(args.snapshot)
    if not snapshot or not (snapshot.get("opend") or {}).get("connected"):
        parser.error("cannot stamp state without a connected Futu snapshot")
    payload = stamp_state(args.state, session_date=session_date, snapshot=snapshot)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
