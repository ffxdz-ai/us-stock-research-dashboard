#!/usr/bin/env python3
"""Gate report generation on the Beijing-time US trading week and Futu OpenD.

The system's trading week is Monday 08:00 through Saturday 08:00 in
Asia/Shanghai.  The 08:00 boundary follows the user's Futu session convention:
Monday opens the overnight week and Saturday closes Friday post-market.  US
exchange holidays are still excluded.  A workflow may be dispatched every day
or manually, but it must not mutate the public archive unless:

* the logical run time is inside that trading-week window;
* the corresponding US market date is an exchange trading date;
* that daily slot has not already been reported (unless manually re-run); and
* an authenticated cloud snapshot proves Futu OpenD is connected now.

The command intentionally exits successfully when a gate is closed.  A closed
gate is a safe no-op, not an infrastructure failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from model_v2 import is_us_trading_day


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = ROOT / "docs" / "data" / "index.json"
DEFAULT_STATE = ROOT / "docs" / "data" / "report_update_state.json"
DEFAULT_SNAPSHOT = ROOT / "data" / "latest_futu_local_snapshot.json"
BEIJING = ZoneInfo("Asia/Shanghai")
TRADING_DAY_BOUNDARY_MINUTES = 8 * 60


@dataclass(frozen=True)
class GateDecision:
    should_update: bool
    code: str
    reason: str
    session_date: str
    previous_session_date: str
    report_slot: str
    previous_report_slot: str
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


def parse_date(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed


def parse_session_date(value: Any) -> date | None:
    parsed = parse_date(value)
    return parsed if parsed is not None and is_us_trading_day(parsed) else None


def trading_week_market_date(value: datetime) -> date | None:
    """Map a Beijing instant to its Futu-style US market date.

    Monday before 08:00, Saturday after 08:00, and all Sunday are outside the
    continuous trading week.  Before 08:00 Tuesday-Friday still belongs to the
    prior US market date; Saturday 08:00 is the Friday post-market boundary.
    """
    local = value.astimezone(BEIJING)
    minute = local.hour * 60 + local.minute
    weekday = local.weekday()
    if weekday == 6 or (weekday == 0 and minute < TRADING_DAY_BOUNDARY_MINUTES):
        return None
    if weekday == 5:
        if minute > TRADING_DAY_BOUNDARY_MINUTES:
            return None
        return local.date() - timedelta(days=1)
    if minute < TRADING_DAY_BOUNDARY_MINUTES:
        return local.date() - timedelta(days=1)
    return local.date()


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


def latest_reported_slot(index: dict[str, Any], state: dict[str, Any]) -> date | None:
    for candidate in (
        state.get("last_report_slot"),
        state.get("last_market_date"),
        state.get("last_completed_session"),  # schema v1 compatibility
        index.get("report_update_slot"),
    ):
        parsed = parse_date(candidate)
        if parsed is not None:
            return parsed

    reports = index.get("reports")
    if not isinstance(reports, list):
        return None
    for report in reports:
        if not isinstance(report, dict) or report.get("kind") != "deepseek-cloud":
            continue
        explicit = parse_date(report.get("report_update_slot") or report.get("market_session_date"))
        if explicit is not None:
            return explicit
        timestamp = report_timestamp(report)
        if timestamp is not None:
            inferred = trading_week_market_date(timestamp)
            if inferred is not None and is_us_trading_day(inferred):
                return inferred
    return None


def snapshot_status(
    snapshot: dict[str, Any],
    *,
    now: datetime,
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
    return True, "eligible", "Futu OpenD 在线且云端心跳新鲜", round(age_minutes, 2)


def evaluate_gate(
    *,
    now: datetime,
    window_time: datetime | None = None,
    index: dict[str, Any],
    state: dict[str, Any],
    snapshot: dict[str, Any],
    force: bool = False,
    max_snapshot_age_minutes: float = 5.0,
) -> GateDecision:
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    logical = window_time or current
    logical = logical if logical.tzinfo is not None else logical.replace(tzinfo=timezone.utc)
    logical = logical.astimezone(timezone.utc)
    session_date = trading_week_market_date(logical)
    previous = latest_reported_slot(index, state)
    previous_text = previous.isoformat() if previous else ""

    if session_date is None:
        return GateDecision(
            False,
            "outside_trading_week",
            "当前不在北京时间周一 08:00 至周六 08:00 的美股交易周窗口",
            "",
            previous_text,
            "",
            previous_text,
        )

    slot_text = session_date.isoformat()
    if not is_us_trading_day(session_date):
        return GateDecision(
            False,
            "us_exchange_closed",
            f"{slot_text} 为美股休市日，不更新日报",
            slot_text,
            previous_text,
            slot_text,
            previous_text,
        )

    if not force and previous is not None and previous >= session_date:
        return GateDecision(
            False,
            "report_slot_already_updated",
            f"美股交易日 {slot_text} 已生成过日报",
            slot_text,
            previous_text,
            slot_text,
            previous_text,
        )

    healthy, code, reason, age_minutes = snapshot_status(
        snapshot,
        now=current,
        max_age_minutes=max_snapshot_age_minutes,
    )
    if not healthy:
        return GateDecision(
            False,
            code,
            reason,
            slot_text,
            previous_text,
            slot_text,
            previous_text,
            age_minutes,
        )

    rerun = "（同一交易日手动重跑）" if force and previous == session_date else ""
    return GateDecision(
        True,
        "eligible",
        f"当前属于美股交易日 {slot_text}，Futu OpenD 在线{rerun}",
        slot_text,
        previous_text,
        slot_text,
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
        "report_slot": decision.report_slot,
        "previous_report_slot": decision.previous_report_slot,
        "snapshot_age_minutes": "" if decision.snapshot_age_minutes is None else decision.snapshot_age_minutes,
    }
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            clean = str(value).replace("\r", " ").replace("\n", " ")
            output.write(f"{key}={clean}\n")


def stamp_state(
    path: Path,
    *,
    session_date: date,
    report_slot: date,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": 2,
        "market": "US",
        "last_market_date": session_date.isoformat(),
        "last_report_slot": report_slot.isoformat(),
        "updated_at": now.isoformat(timespec="seconds"),
        "update_policy": "Beijing Monday 08:00-Saturday 08:00 trading week plus authenticated live Futu OpenD",
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
    check.add_argument("--now", help="actual ISO timestamp override for deterministic tests")
    check.add_argument("--window-time", help="logical scheduled time; snapshot age still uses actual time")
    check.add_argument("--force", default="false", help="allow a same-slot manual re-run")
    check.add_argument("--max-snapshot-age-minutes", type=float, default=5.0)
    check.add_argument("--github-output", type=Path)

    stamp = subparsers.add_parser("stamp", help="record the successfully published market session")
    stamp.add_argument("--state", type=Path, default=DEFAULT_STATE)
    stamp.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    stamp.add_argument("--session-date", required=True)
    stamp.add_argument("--report-slot", required=True)

    args = parser.parse_args()
    if args.command == "check":
        now = parse_datetime(args.now) if args.now else datetime.now(timezone.utc)
        if now is None:
            parser.error("--now must be a valid ISO timestamp")
        window_time = parse_datetime(args.window_time) if args.window_time else None
        if args.window_time and window_time is None:
            parser.error("--window-time must be a valid ISO timestamp")
        decision = evaluate_gate(
            now=now,
            window_time=window_time,
            index=load_json(args.index),
            state=load_json(args.state),
            snapshot=load_json(args.snapshot),
            force=bool_value(args.force),
            max_snapshot_age_minutes=max(0.5, args.max_snapshot_age_minutes),
        )
        if args.github_output:
            write_github_output(args.github_output, decision)
        print(json.dumps(asdict(decision), ensure_ascii=False, indent=2))
        return 0

    session_date = parse_session_date(args.session_date)
    if session_date is None:
        parser.error("--session-date must be a valid US trading date")
    report_slot = parse_date(args.report_slot)
    if report_slot is None:
        parser.error("--report-slot must be a valid ISO date")
    snapshot = load_json(args.snapshot)
    if not snapshot or not (snapshot.get("opend") or {}).get("connected"):
        parser.error("cannot stamp state without a connected Futu snapshot")
    payload = stamp_state(
        args.state,
        session_date=session_date,
        report_slot=report_slot,
        snapshot=snapshot,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
