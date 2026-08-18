#!/usr/bin/env python3
"""Deterministic V2 research primitives.

This module is the single source of truth for technical calculations, field
provenance, factor scores, entry-path R/R and hard risk gates.  LLM code may
describe these results but must never change them.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parents[1]
RISK_POLICY_PATH = ROOT / "config" / "risk_policy.json"
MODEL_CONFIG_PATH = ROOT / "config" / "model_v2.json"

SOURCE_QUALITY = {
    "sec": 1.00,
    "fred": 1.00,
    "company ir": 0.95,
    "futu opend": 0.95,
    "futu": 0.95,
    "fmp": 0.90,
    "finnhub": 0.85,
    "nasdaq": 0.85,
    "yahoo": 0.75,
    "fallback": 0.60,
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").replace("%", "").strip()
        try:
            parsed = float(cleaned)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, float(value)))


def parse_timestamp(value: Any, default_tz: timezone = timezone.utc) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo is not None else value.replace(tzinfo=default_tz)
        return parsed.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=default_tz).astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    if raw.endswith(" ET"):
        eastern_raw = raw[:-3].strip()
        for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p"):
            try:
                return datetime.strptime(eastern_raw, fmt).replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
            except (ValueError, ZoneInfoNotFoundError):
                pass
    for candidate in (raw, raw.replace(" EDT", "-04:00"), raw.replace(" EST", "-05:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=default_tz)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%b %d, %Y",
        "%B %d, %Y",
    ):
        try:
            parsed = datetime.strptime(raw, fmt).replace(tzinfo=default_tz)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return None


@dataclass(frozen=True)
class RiskPolicy:
    policy_version: str
    formal_min_rr: float
    starter_min_rr: float
    breakout_min_rr: float
    min_data_confidence: float
    formal_min_opportunity_score: float
    formal_min_trend_score: float
    formal_max_crowding_score: float
    starter_min_opportunity_score: float
    starter_min_trend_score: float
    starter_max_crowding_score: float
    formal_max_entry_premium_pct: float
    starter_max_entry_premium_pct: float
    formal_entry_zone_lower_pct: float
    breakout_buffer: float
    intraday_quote_max_age_minutes: int
    eod_max_age_days: int
    fallback_execution_allowed: bool


def load_risk_policy(path: Path = RISK_POLICY_PATH) -> RiskPolicy:
    if not path.exists():
        raise FileNotFoundError(f"central RiskPolicy is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    allowed = set(RiskPolicy.__dataclass_fields__)
    missing = sorted(allowed - set(payload))
    if missing:
        raise ValueError(f"central RiskPolicy is incomplete: {missing}")
    return RiskPolicy(**{key: value for key, value in payload.items() if key in allowed})


def load_model_config(path: Path = MODEL_CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def risk_policy_public(policy: RiskPolicy | None = None) -> dict[str, Any]:
    return asdict(policy or load_risk_policy())


@dataclass
class FieldValue:
    value: Any
    source: str
    source_type: str
    source_time: str | None
    retrieved_at: str | None
    fallback_used: bool
    confidence: float
    data_gap: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TechnicalSnapshot:
    bar_count: int
    ma20: float | None
    ma50: float | None
    ma200: float | None
    low20: float | None
    low60: float | None
    high20: float | None
    high60: float | None
    high252: float | None
    prior_high20: float | None
    prior_high60: float | None
    prior_high252: float | None
    low252: float | None
    momentum_1m: float | None
    momentum_3m: float | None
    momentum_6m: float | None
    realized_vol20: float | None
    volume_ratio20: float | None
    reference_bar_end_time: str | None
    technical_data_complete: bool
    breakout_reference_excludes_current_bar: bool = True
    bars: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EntryPath:
    path_type: str
    entry: float | None
    stop: float | None
    target: float | None
    rr: float | None
    valid: bool
    failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RiskGateResult:
    qualified: bool
    gate_failures: list[str]
    policy_version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _window_average(values: list[float], window: int) -> float | None:
    return round(sum(values[-window:]) / window, 4) if len(values) >= window else None


def _window_min(values: list[float], window: int) -> float | None:
    return round(min(values[-window:]), 4) if len(values) >= window else None


def _window_max(values: list[float], window: int) -> float | None:
    return round(max(values[-window:]), 4) if len(values) >= window else None


def _prior_window_max(values: list[float], window: int) -> float | None:
    return round(max(values[-(window + 1):-1]), 4) if len(values) >= window + 1 else None


def _momentum(values: list[float], periods: int) -> float | None:
    if len(values) < periods + 1 or values[-(periods + 1)] <= 0:
        return None
    return round(values[-1] / values[-(periods + 1)] - 1, 6)


def build_technical_snapshot(
    closes: Iterable[Any],
    highs: Iterable[Any] | None = None,
    lows: Iterable[Any] | None = None,
    timestamps: Iterable[Any] | None = None,
    volumes: Iterable[Any] | None = None,
) -> TechnicalSnapshot:
    raw_closes = list(closes)
    raw_highs = list(highs or [])
    raw_lows = list(lows or [])
    raw_times = list(timestamps or [])
    raw_volumes = list(volumes or [])
    close_values: list[float] = []
    high_values: list[float] = []
    low_values: list[float] = []
    time_values: list[Any] = []
    volume_values: list[float | None] = []
    for index, raw_close in enumerate(raw_closes):
        close = _number(raw_close)
        if close is None or close <= 0:
            continue
        high = _number(raw_highs[index]) if index < len(raw_highs) else None
        low = _number(raw_lows[index]) if index < len(raw_lows) else None
        volume = _number(raw_volumes[index]) if index < len(raw_volumes) else None
        close_values.append(close)
        high_values.append(high if high is not None and high > 0 else close)
        low_values.append(low if low is not None and low > 0 else close)
        time_values.append(raw_times[index] if index < len(raw_times) else None)
        volume_values.append(volume if volume is not None and volume >= 0 else None)
    bar_count = len(close_values)
    returns = [
        math.log(current / previous)
        for previous, current in zip(close_values[-21:-1], close_values[-20:])
        if previous > 0 and current > 0
    ]
    vol20 = round(statistics.stdev(returns) * math.sqrt(252), 4) if len(returns) >= 19 else None
    volume_ratio = None
    if len(volume_values) >= 21 and all(value is not None for value in volume_values[-21:]):
        volume_tail = [float(value) for value in volume_values[-21:] if value is not None]
        baseline = sum(volume_tail[:-1]) / 20
        volume_ratio = round(volume_tail[-1] / baseline, 4) if baseline > 0 else None
    reference = None
    if time_values and time_values[-1] is not None:
        parsed = parse_timestamp(time_values[-1])
        reference = parsed.isoformat() if parsed else str(time_values[-1])
    bars: list[dict[str, Any]] = []
    usable = len(close_values) if any(value is not None for value in time_values) else 0
    if usable:
        close_tail = close_values[-usable:]
        high_tail = high_values[-usable:] if len(high_values) >= usable else close_tail
        low_tail = low_values[-usable:] if len(low_values) >= usable else close_tail
        time_tail = time_values[-usable:]
        for idx in range(max(0, usable - 260), usable):
            parsed = parse_timestamp(time_tail[idx])
            bars.append(
                {
                    "time": parsed.isoformat() if parsed else str(time_tail[idx]),
                    "close": round(close_tail[idx], 4),
                    "high": round(high_tail[idx], 4),
                    "low": round(low_tail[idx], 4),
                }
            )
    complete = bar_count >= 253
    return TechnicalSnapshot(
        bar_count=bar_count,
        ma20=_window_average(close_values, 20),
        ma50=_window_average(close_values, 50),
        ma200=_window_average(close_values, 200),
        low20=_window_min(low_values, 20),
        low60=_window_min(low_values, 60),
        high20=_window_max(high_values, 20),
        high60=_window_max(high_values, 60),
        high252=_window_max(high_values, 252),
        prior_high20=_prior_window_max(high_values, 20),
        prior_high60=_prior_window_max(high_values, 60),
        prior_high252=_prior_window_max(high_values, 252),
        low252=_window_min(low_values, 252),
        momentum_1m=_momentum(close_values, 21),
        momentum_3m=_momentum(close_values, 63),
        momentum_6m=_momentum(close_values, 126),
        realized_vol20=vol20,
        volume_ratio20=volume_ratio,
        reference_bar_end_time=reference,
        technical_data_complete=complete,
        bars=bars,
    )


def technical_score(price: Any, snapshot: dict[str, Any] | TechnicalSnapshot) -> float | None:
    current = _number(price)
    data = snapshot.to_dict() if isinstance(snapshot, TechnicalSnapshot) else snapshot
    if current is None or _number(data.get("ma200")) is None:
        return None
    ma20 = _number(data.get("ma20"))
    ma50 = _number(data.get("ma50"))
    ma200 = _number(data.get("ma200"))
    score = 50.0
    if ma20 is not None:
        score += 8 if current >= ma20 else -6
    if ma50 is not None:
        score += 12 if current >= ma50 else -10
    if ma200 is not None:
        score += 16 if current >= ma200 else -25
        if current < ma200 * 0.80:
            score -= 15
    if ma20 is not None and ma50 is not None and ma200 is not None:
        if current > ma20 > ma50 > ma200:
            score += 14
        elif ma20 < ma50 < ma200:
            score -= 12
        if ma50 < ma200:
            score -= 12
    for key, weight in (("momentum_1m", 7), ("momentum_3m", 10), ("momentum_6m", 8)):
        momentum = _number(data.get(key))
        if momentum is not None:
            score += clamp(momentum * 100, -20, 20) / 20 * weight
    high252 = _number(data.get("high252"))
    if high252:
        distance = current / high252 - 1
        if -0.15 <= distance <= -0.02:
            score += 4
        elif distance < -0.35:
            score -= 8
    vol20 = _number(data.get("realized_vol20"))
    if vol20 is not None:
        score += 3 if vol20 <= 0.30 else -min(8, max(0, (vol20 - 0.45) * 20))
    volume_ratio = _number(data.get("volume_ratio20"))
    if volume_ratio is not None and current >= (ma20 or current):
        score += min(5, max(0, (volume_ratio - 1) * 5))
    return round(clamp(score), 1)


def source_quality(source: Any) -> float:
    normalized = str(source or "").strip().lower()
    # A disclosed fallback must never inherit the quality of a higher-priority
    # provider merely because its label also contains that provider's name.
    if "fallback" in normalized:
        return SOURCE_QUALITY["fallback"]
    for key, quality in SOURCE_QUALITY.items():
        if key in normalized:
            return quality
    return 0.60 if normalized else 0.0


def select_best_field_value(candidates: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose by field validity, source quality, confidence and freshness."""
    valid = [
        item for item in candidates
        if isinstance(item, dict) and item.get("value") is not None and item.get("valid", True) is not False and item.get("stale") is not True
    ]
    if not valid:
        return None

    def rank(item: dict[str, Any]) -> tuple[float, float, float]:
        quality = _number(item.get("source_quality")) or source_quality(item.get("source"))
        confidence = _number(item.get("confidence")) or quality
        stamp = parse_timestamp(item.get("source_time") or item.get("as_of") or item.get("updated_at"))
        epoch = stamp.timestamp() if stamp else 0.0
        return (quality, epoch, confidence)

    return dict(max(valid, key=rank))


def assess_price_freshness(
    quote_time: Any,
    source: Any,
    signal_time: Any = None,
    *,
    mode: str = "eod",
    policy: RiskPolicy | None = None,
) -> dict[str, Any]:
    policy = policy or load_risk_policy()
    signal = parse_timestamp(signal_time) or datetime.now(timezone.utc)
    quote = parse_timestamp(quote_time)
    source_text = str(source or "")
    is_broker = "futu opend" in source_text.lower() and "fallback" not in source_text.lower() and "snapshot" not in source_text.lower()
    if quote is None:
        return {"price_freshness": "unknown", "quote_age_minutes": None, "execution_allowed": False}
    age_minutes = max(0.0, (signal - quote).total_seconds() / 60)
    if quote > signal + timedelta(minutes=5):
        status = "stale"
    elif mode == "intraday":
        status = "fresh" if age_minutes <= policy.intraday_quote_max_age_minutes else "stale"
    else:
        status = "fresh" if age_minutes <= policy.eod_max_age_days * 24 * 60 else "stale"
    if not is_broker:
        status = "fallback_only" if status == "fresh" else status
    execution_allowed = bool(status == "fresh" and is_broker)
    return {
        "price_freshness": status,
        "quote_age_minutes": round(age_minutes, 1),
        "execution_allowed": execution_allowed,
    }


def calculate_data_confidence(
    *,
    completeness: float,
    freshness: float,
    source_quality_score: float,
    cross_source_agreement: float,
) -> tuple[float, dict[str, float]]:
    components = {
        "completeness": clamp(completeness, 0, 1),
        "freshness": clamp(freshness, 0, 1),
        "source_quality": clamp(source_quality_score, 0, 1),
        "cross_source_agreement": clamp(cross_source_agreement, 0, 1),
    }
    value = math.prod(components.values())
    return round(value, 4), {key: round(val, 4) for key, val in components.items()}


def calculate_rr(entry: Any, stop: Any, target: Any) -> float | None:
    planned_entry = _number(entry)
    stop_loss = _number(stop)
    target_price = _number(target)
    if planned_entry is None or stop_loss is None or target_price is None:
        return None
    if not stop_loss < planned_entry < target_price:
        return None
    risk = planned_entry - stop_loss
    return round((target_price - planned_entry) / risk, 4) if risk > 0 else None


def build_entry_path(path_type: str, entry: Any, stop: Any, target: Any, min_rr: float) -> EntryPath:
    rr = calculate_rr(entry, stop, target)
    failures: list[str] = []
    if rr is None:
        failures.append("invalid_or_incomplete_path")
    elif rr < min_rr:
        failures.append(f"rr_below_{min_rr:g}")
    return EntryPath(path_type, _number(entry), _number(stop), _number(target), rr, not failures, failures)


def confidence_multiplier(confidence: Any) -> float:
    value = _number(confidence)
    if value is None or value < load_risk_policy().min_data_confidence:
        return 0.0
    if value >= 0.95:
        return 1.0
    if value >= 0.80:
        return 0.95
    return 0.90


def evaluate_risk_gate(
    candidate: dict[str, Any],
    path: EntryPath,
    *,
    path_type: str = "formal",
    policy: RiskPolicy | None = None,
) -> RiskGateResult:
    policy = policy or load_risk_policy()
    failures = list(path.failures)
    opportunity = _number(candidate.get("opportunity_score"))
    trend = _number(candidate.get("trend_score") or candidate.get("technical_score_v2"))
    crowding = _number(candidate.get("crowding_score"))
    confidence = _number(candidate.get("data_confidence"))
    if path_type == "starter":
        min_score, min_trend, max_crowding = (
            policy.starter_min_opportunity_score,
            policy.starter_min_trend_score,
            policy.starter_max_crowding_score,
        )
    else:
        min_score, min_trend, max_crowding = (
            policy.formal_min_opportunity_score,
            policy.formal_min_trend_score,
            policy.formal_max_crowding_score,
        )
    if opportunity is None or opportunity < min_score:
        failures.append("opportunity_score_below_threshold")
    if trend is None or trend < min_trend:
        failures.append("trend_score_below_threshold")
    if crowding is None or crowding > max_crowding:
        failures.append("crowding_score_above_threshold")
    if confidence is None or confidence < policy.min_data_confidence:
        failures.append("data_confidence_below_threshold")
    if candidate.get("price_freshness") != "fresh":
        failures.append("price_freshness_not_pass")
    if candidate.get("technical_data_complete") is not True:
        failures.append("technical_data_incomplete")
    if candidate.get("future_function_audit") != "PASS":
        failures.append("future_function_audit_failed")
    if candidate.get("execution_allowed") is not True:
        failures.append("execution_not_allowed")
    if candidate.get("valid_path") is False:
        failures.append("valid_path_false")
    deduped = list(dict.fromkeys(failures))
    return RiskGateResult(not deduped, deduped, policy.policy_version)


def future_function_audit(candidate: dict[str, Any], signal_time: Any) -> dict[str, Any]:
    signal = parse_timestamp(signal_time)
    failures: list[str] = []
    if signal is None:
        failures.append("signal_time_unknown")
        signal = datetime.now(timezone.utc)
    quote = parse_timestamp(candidate.get("quote_time"))
    chart = parse_timestamp(candidate.get("reference_bar_end_time") or candidate.get("chart_time"))
    if quote is None:
        failures.append("quote_time_unknown")
    elif quote > signal + timedelta(minutes=5):
        failures.append("future_quote")
    if chart is None:
        failures.append("chart_time_unknown")
    elif chart > signal + timedelta(minutes=5):
        failures.append("future_chart")
    if quote and chart and chart.date() > quote.date():
        failures.append("chart_after_quote")
    if candidate.get("breakout_reference_excludes_current_bar") is not True:
        failures.append("breakout_current_bar_contamination")
    if candidate.get("technical_data_complete") is not True:
        failures.append("ma_window_incomplete")
    sec = candidate.get("sec") if isinstance(candidate.get("sec"), dict) else {}
    for filing in sec.get("recent_filings", []) if isinstance(sec.get("recent_filings"), list) else []:
        filed = parse_timestamp(filing.get("filed") if isinstance(filing, dict) else None)
        if filed and filed.date() > signal.date():
            failures.append("future_filing")
            break
    return {
        "status": "BLOCK" if failures else "PASS",
        "has_critical_error": bool(failures),
        "failures": failures,
        "signal_bar_time": signal.isoformat(),
        "reference_bar_end_time": chart.isoformat() if chart else None,
        "quote_time": quote.isoformat() if quote else None,
    }


FACTOR_NAMES = (
    "quality",
    "growth",
    "earnings_revision",
    "valuation",
    "momentum",
    "catalyst",
    "crowding",
    "data_quality",
)


def load_factor_weights() -> dict[str, float]:
    raw = load_model_config().get("factor_weights")
    if not isinstance(raw, dict):
        raise ValueError("model_v2.factor_weights is required")
    missing = sorted(set(FACTOR_NAMES) - set(raw))
    if missing:
        raise ValueError(f"model_v2.factor_weights is incomplete: {missing}")
    weights = {name: float(raw[name]) for name in FACTOR_NAMES}
    if any(value <= 0 for value in weights.values()) or abs(sum(weights.values()) - 1.0) > 1e-6:
        raise ValueError("model_v2.factor_weights must be positive and sum to 1.0")
    return weights


def infer_company_type(candidate: dict[str, Any]) -> str:
    text = " ".join(str(candidate.get(key) or "") for key in ("theme", "layer", "role", "industry")).lower()
    ticker = str(candidate.get("ticker") or candidate.get("symbol") or "").upper()
    if any(key in text for key in ("软件", "software", "saas")) or ticker in {"MSFT", "GOOGL", "ORCL", "PLTR", "SNPS", "CDNS"}:
        return "software"
    if any(key in text for key in ("半导体", "gpu", "asic", "hbm", "芯片", "晶圆")) or ticker in {"NVDA", "AMD", "AVGO", "MU", "TSM", "ASML", "AMAT"}:
        return "semiconductor"
    if any(key in text for key in ("机器人", "自动化", "automation")):
        return "automation"
    return "industrial"


def valuation_score(candidate: dict[str, Any], company_type: str | None = None) -> tuple[float | None, dict[str, Any]]:
    company_type = company_type or infer_company_type(candidate)
    metrics = {
        "forward_pe": _number(candidate.get("forward_pe") or candidate.get("valuation_pe")),
        "ev_ebitda": _number(candidate.get("ev_ebitda")),
        "ev_sales": _number(candidate.get("ev_sales")),
        "fcf_yield": _number(candidate.get("fcf_yield")),
        "peg": _number(candidate.get("peg")),
        "price_sales": _number(candidate.get("price_sales") or candidate.get("finnhub_ps")),
        "historical_percentile": _number(candidate.get("valuation_history_percentile")),
        "sector_percentile": _number(candidate.get("valuation_sector_percentile")),
    }
    available = {key: value for key, value in metrics.items() if value is not None}
    if metrics["sector_percentile"] is not None:
        return round(100 - clamp(metrics["sector_percentile"]), 1), {"company_type": company_type, "metrics": available, "method": "sector_percentile"}
    if metrics["historical_percentile"] is not None:
        return round(100 - clamp(metrics["historical_percentile"]), 1), {"company_type": company_type, "metrics": available, "method": "historical_percentile"}
    score: float | None = None
    method = "insufficient"
    if company_type == "software" and metrics["ev_sales"] is not None:
        score, method = clamp(90 - metrics["ev_sales"] * 5), "software_ev_sales"
    elif company_type == "software" and metrics["price_sales"] is not None:
        score, method = clamp(88 - metrics["price_sales"] * 5), "software_price_sales_fallback"
    elif company_type in {"semiconductor", "industrial", "automation"} and metrics["forward_pe"] is not None and metrics["forward_pe"] > 0:
        config = load_model_config()
        anchors = config.get("valuation_anchors") if isinstance(config.get("valuation_anchors"), dict) else {}
        anchor = _number(anchors.get(f"{company_type}_forward_pe")) or _number(anchors.get("default_forward_pe")) or 26
        score, method = clamp(65 + (anchor - metrics["forward_pe"]) * 2), f"{company_type}_forward_pe"
    elif metrics["fcf_yield"] is not None:
        score, method = clamp(50 + metrics["fcf_yield"] * 500), "fcf_yield"
    return (round(score, 1) if score is not None else None), {"company_type": company_type, "metrics": available, "method": method}


def _fundamental_factor(candidate: dict[str, Any]) -> tuple[float | None, float | None]:
    sec = candidate.get("sec") if isinstance(candidate.get("sec"), dict) else {}
    revenue_growth = _number(sec.get("revenue_growth_yoy"))
    margin = _number(sec.get("net_margin"))
    leverage = _number(sec.get("liabilities_to_assets"))
    fcf = _number((sec.get("latest_annual_fcf") or {}).get("val") if isinstance(sec.get("latest_annual_fcf"), dict) else None)
    quality_parts: list[float] = []
    growth = None
    if margin is not None:
        quality_parts.append(clamp(50 + margin * 150))
    if leverage is not None:
        quality_parts.append(clamp(90 - leverage * 80))
    if fcf is not None:
        quality_parts.append(75 if fcf > 0 else 20)
    if revenue_growth is not None:
        growth = round(clamp(50 + revenue_growth * 140), 1)
    quality = round(sum(quality_parts) / len(quality_parts), 1) if quality_parts else None
    return quality, growth


def factor_snapshot(candidate: dict[str, Any]) -> dict[str, Any]:
    weights = load_factor_weights()
    quality, growth = _fundamental_factor(candidate)
    valuation, valuation_detail = valuation_score(candidate)
    momentum = _number(candidate.get("technical_score_v2") or candidate.get("trend_score"))
    revision = _number(candidate.get("earnings_revision_score"))
    catalyst = _number(candidate.get("catalyst_score"))
    crowding = _number(candidate.get("crowding_score"))
    confidence = _number(candidate.get("data_confidence"))
    factors = {
        "quality": quality,
        "growth": growth,
        "earnings_revision": revision,
        "valuation": valuation,
        "momentum": momentum,
        "catalyst": catalyst,
        "crowding": None if crowding is None else round(100 - clamp(crowding), 1),
        "data_quality": None if confidence is None else round(clamp(confidence, 0, 1) * 100, 1),
    }
    available_weight = sum(weights[key] for key, value in factors.items() if value is not None)
    missing = [key for key, value in factors.items() if value is None]
    raw = None
    if available_weight > 0:
        raw = sum(float(factors[key]) * weights[key] for key in factors if factors[key] is not None) / available_weight
    multiplier = confidence_multiplier(confidence)
    opportunity = round(raw * multiplier, 1) if raw is not None else None
    return {
        "factors": factors,
        "factor_coverage": round(available_weight, 2),
        "missing_factors": missing,
        "raw_alpha_score": round(raw, 1) if raw is not None else None,
        "confidence_multiplier": multiplier,
        "opportunity_score": opportunity,
        "valuation_detail": valuation_detail,
    }


def entry_score(candidate: dict[str, Any], paths: Iterable[EntryPath]) -> float | None:
    trend = _number(candidate.get("technical_score_v2") or candidate.get("trend_score"))
    valid_rr = [path.rr for path in paths if path.valid and path.rr is not None]
    if trend is None or not valid_rr:
        return None
    rr_score = clamp(max(valid_rr) / 4 * 100)
    vol = _number((candidate.get("chart") or {}).get("realized_vol20") if isinstance(candidate.get("chart"), dict) else None)
    vol_score = 55.0 if vol is None else clamp(100 - max(0, vol - 0.20) * 120)
    freshness_score = 100.0 if candidate.get("price_freshness") == "fresh" else 0.0
    return round(trend * 0.45 + rr_score * 0.35 + vol_score * 0.10 + freshness_score * 0.10, 1)


def _percentile(values: list[float], value: float) -> float:
    if len(values) <= 1:
        return 50.0
    below = sum(1 for item in values if item < value)
    equal = sum(1 for item in values if item == value)
    return round((below + 0.5 * equal) / len(values) * 100, 1)


def apply_cross_sectional_ranks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    factor_names = list(load_factor_weights())
    for factor in factor_names:
        values = [value for row in rows if (value := _number((row.get("factor_snapshot") or {}).get("factors", {}).get(factor))) is not None]
        for row in rows:
            value = _number((row.get("factor_snapshot") or {}).get("factors", {}).get(factor))
            row.setdefault("factor_percentiles", {})[factor] = _percentile(values, value) if value is not None else None
    alpha_values = [value for row in rows if (value := _number(row.get("opportunity_score"))) is not None]
    sectors: dict[str, list[float]] = {}
    for row in rows:
        value = _number(row.get("opportunity_score"))
        if value is not None:
            sectors.setdefault(str(row.get("company_type") or "unknown"), []).append(value)
    ordered = sorted((row for row in rows if _number(row.get("opportunity_score")) is not None), key=lambda row: float(row["opportunity_score"]), reverse=True)
    rank_map = {id(row): idx + 1 for idx, row in enumerate(ordered)}
    for row in rows:
        value = _number(row.get("opportunity_score"))
        sector_values = sectors.get(str(row.get("company_type") or "unknown"), [])
        row["alpha_percentile"] = _percentile(alpha_values, value) if value is not None else None
        row["sector_rank_percentile"] = _percentile(sector_values, value) if value is not None else None
        row["universe_rank"] = rank_map.get(id(row))
    return rows


ALLOWED_LLM_STOCK_KEYS = {"symbol", "thesis", "bull_case", "bear_case", "catalysts", "risks", "missing_evidence"}
ALLOWED_LLM_MARKET_KEYS = {"summary", "stance", "key_risks", "next_checks"}
PROHIBITED_LLM_TRADE_NUMBER = re.compile(
    r"(?:买入|卖出|入场|止损|目标价|buy|sell|entry|stop|target)\s*(?:价|price|at|=|:|：)?\s*[$¥￥]?\s*\d",
    re.IGNORECASE,
)


def validate_llm_commentary(payload: Any, universe: Iterable[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("LLM output must be a JSON object")
    unknown_top = set(payload) - {"market_view", "stock_commentary"}
    if unknown_top:
        raise ValueError(f"LLM output contains unsupported top-level keys: {sorted(unknown_top)}")
    market_view = payload.get("market_view")
    if not isinstance(market_view, dict):
        raise ValueError("market_view must be an object")
    unknown_market = set(market_view) - ALLOWED_LLM_MARKET_KEYS
    if unknown_market:
        raise ValueError(f"market_view contains unsupported keys: {sorted(unknown_market)}")
    allowed_symbols = {str(symbol).upper().replace("US.", "") for symbol in universe}
    commentary = payload.get("stock_commentary")
    if not isinstance(commentary, list):
        raise ValueError("stock_commentary must be a list")
    for item in commentary:
        if not isinstance(item, dict):
            raise ValueError("stock_commentary item must be an object")
        unknown_keys = set(item) - ALLOWED_LLM_STOCK_KEYS
        if unknown_keys:
            raise ValueError(f"LLM attempted to provide deterministic trade fields: {sorted(unknown_keys)}")
        symbol = str(item.get("symbol") or "").upper().replace("US.", "")
        if symbol not in allowed_symbols:
            raise ValueError(f"LLM returned unknown symbol: {symbol}")
        for key in ("catalysts", "risks", "missing_evidence"):
            if key in item and not isinstance(item[key], list):
                raise ValueError(f"{key} must be a list")
        narrative = json.dumps(item, ensure_ascii=False)
        if PROHIBITED_LLM_TRADE_NUMBER.search(narrative):
            raise ValueError(f"LLM attempted to encode a numeric trade instruction for {symbol}")
    return payload


def evaluate_split(signal_time: Any, model_config: dict[str, Any] | None = None) -> str:
    if model_config is None:
        model_config = json.loads(MODEL_CONFIG_PATH.read_text(encoding="utf-8")) if MODEL_CONFIG_PATH.exists() else {}
    split = model_config.get("walk_forward") if isinstance(model_config.get("walk_forward"), dict) else {}
    parsed = parse_timestamp(signal_time)
    if parsed is None:
        return "unknown"
    day = parsed.date()
    train_end = date.fromisoformat(str(split.get("train_end", "2023-12-31")))
    validation_end = date.fromisoformat(str(split.get("validation_end", "2024-12-31")))
    if day <= train_end:
        return "train"
    if day <= validation_end:
        return "validation"
    return "oos"
