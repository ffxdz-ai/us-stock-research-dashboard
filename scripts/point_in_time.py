#!/usr/bin/env python3
"""Point-in-time records and leakage-safe walk-forward helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from model_v2 import canonical_content_hash, parse_timestamp


@dataclass(frozen=True)
class PointInTimeRecord:
    value: Any
    period_end: str | None
    published_at: str | None
    available_at: str | None
    ingested_at: str
    source: str
    source_version: str
    content_hash: str
    vintage: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_pit_record(
    value: Any,
    *,
    period_end: Any = None,
    published_at: Any = None,
    available_at: Any = None,
    ingested_at: Any,
    source: str,
    source_version: str,
    vintage: str | None = None,
    conservative_date_only: bool = True,
) -> PointInTimeRecord:
    published = parse_timestamp(published_at)
    available = parse_timestamp(available_at)
    ingested = parse_timestamp(ingested_at)
    if ingested is None:
        raise ValueError("ingested_at is required")
    if available is None and published is not None:
        available = published
        if conservative_date_only and isinstance(published_at, str) and len(published_at.strip()) == 10:
            # Date-only filings become usable after that day's regular session.
            available = datetime.combine(published.date(), time(21, 0), timezone.utc)
    payload = {
        "value": value,
        "period_end": str(period_end) if period_end is not None else None,
        "published_at": published.isoformat() if published else None,
        "available_at": available.isoformat() if available else None,
        "ingested_at": ingested.isoformat(),
        "source": source,
        "source_version": source_version,
        "vintage": vintage,
    }
    return PointInTimeRecord(**payload, content_hash=canonical_content_hash(payload))


def usable_at(record: PointInTimeRecord | dict[str, Any], signal_time: Any) -> bool:
    payload = record.to_dict() if isinstance(record, PointInTimeRecord) else record
    available = parse_timestamp(payload.get("available_at"))
    signal = parse_timestamp(signal_time)
    return bool(available and signal and available <= signal)


def normalized_forecast_revision(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    prior_period = str(previous.get("forecast_period") or "")
    current_period = str(current.get("forecast_period") or "")
    if not prior_period or not current_period or prior_period != current_period:
        return {"revision": None, "reason": "forecast_period_changed", "comparable": False}
    try:
        before, after = float(previous["value"]), float(current["value"])
    except (KeyError, TypeError, ValueError):
        return {"revision": None, "reason": "forecast_value_missing", "comparable": False}
    return {"revision": round(after - before, 6), "reason": None, "comparable": True}


def purged_walk_forward_split(
    samples: list[dict[str, Any]],
    *,
    train_end: Any,
    validation_end: Any,
    label_horizon_days: int,
    embargo_days: int,
) -> list[dict[str, Any]]:
    train_boundary = parse_timestamp(train_end)
    validation_boundary = parse_timestamp(validation_end)
    if train_boundary is None or validation_boundary is None:
        raise ValueError("walk-forward boundaries are required")
    output: list[dict[str, Any]] = []
    for sample in samples:
        stamp = parse_timestamp(sample.get("signal_time"))
        if stamp is None:
            output.append({**sample, "evaluation_split": "excluded", "exclusion_reason": "signal_time_missing"})
            continue
        label_end = stamp + timedelta(days=label_horizon_days)
        if stamp <= train_boundary:
            split = "train"
            boundary = train_boundary
        elif stamp <= validation_boundary:
            split = "validation"
            boundary = validation_boundary
        else:
            split = "oos"
            boundary = None
        reason = None
        if boundary and label_end > boundary:
            split, reason = "excluded", "label_crosses_split_boundary"
        elif train_boundary < stamp <= train_boundary + timedelta(days=embargo_days):
            split, reason = "excluded", "post_train_embargo"
        elif validation_boundary < stamp <= validation_boundary + timedelta(days=embargo_days):
            split, reason = "excluded", "post_validation_embargo"
        output.append({**sample, "evaluation_split": split, "exclusion_reason": reason})
    return output
