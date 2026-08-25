#!/usr/bin/env python3
"""Send separately deduplicated Feishu candidate and steady-buy alerts."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
DEFAULT_INDEX = Path("docs/data/index.json")
DEFAULT_STATE = Path("docs/data/feishu_alert_state.json")
DEFAULT_DASHBOARD_URL = "https://ffxdz-ai.github.io/us-stock-research-dashboard/"
ALLOWED_WEBHOOK_PREFIXES = (
    "https://open.feishu.cn/open-apis/bot/v2/hook/",
    "https://open.larksuite.com/open-apis/bot/v2/hook/",
)


def now_label() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def text(value: Any, limit: int = 240) -> str:
    cleaned = " ".join(str(value or "").replace("`", "").split())
    return cleaned[:limit]


def valid_trade_path(item: dict[str, Any]) -> bool:
    entry = number(item.get("entry_price") or item.get("safe_entry_price"))
    stop = number(item.get("stop_loss"))
    target = number(item.get("target_price"))
    rr = number(item.get("rr_ratio"))
    required = number(item.get("rr_required"))
    if None in (entry, stop, target, rr, required):
        return False
    return bool(stop < entry < target and rr >= required)


def qualifies_for_steady_buy_alert(item: dict[str, Any]) -> bool:
    """Recheck every hard gate instead of trusting a display label alone."""
    if not isinstance(item, dict) or not text(item.get("symbol")):
        return False
    if item.get("status") != "executable" or item.get("entry_tier") != "formal":
        return False
    if item.get("execution_allowed") is not True or item.get("technical_data_complete") is not True:
        return False
    if item.get("future_function_audit") != "PASS" or item.get("price_freshness") != "fresh":
        return False
    if item.get("gate_failures"):
        return False
    if not valid_trade_path(item):
        return False
    if any(number(item.get(field)) is None for field in ("price", "opportunity_score", "trend_score", "crowding_score")):
        return False

    price = number(item.get("price"))
    stop = number(item.get("stop_loss"))
    signal_type = text(item.get("signal_type"))
    if price is None or stop is None or price <= stop:
        return False
    if signal_type == "formal":
        if item.get("formal_qualified") is not True or item.get("entry_execution_status") != "in_zone":
            return False
        max_price = number(item.get("safe_entry_max_price") or item.get("safe_entry_zone_high") or item.get("entry_price"))
        return max_price is not None and price <= max_price
    if signal_type == "breakout":
        trigger = number(item.get("breakout_trigger") or item.get("entry_price"))
        return trigger is not None and price >= trigger
    return False


def qualifies_for_candidate_alert(item: dict[str, Any]) -> bool:
    """Preview only qualified formal plans that still need a safe pullback."""
    if not isinstance(item, dict) or not text(item.get("symbol")):
        return False
    if item.get("status") != "waiting_entry" or item.get("entry_tier") != "formal":
        return False
    if item.get("signal_type") != "formal" or item.get("formal_qualified") is not True:
        return False
    if item.get("entry_execution_status") != "wait_pullback":
        return False
    if item.get("execution_allowed") is not True or item.get("technical_data_complete") is not True:
        return False
    if item.get("future_function_audit") != "PASS" or item.get("price_freshness") != "fresh":
        return False
    if item.get("gate_failures") or not valid_trade_path(item):
        return False
    if any(number(item.get(field)) is None for field in ("price", "opportunity_score", "trend_score", "crowding_score")):
        return False

    price = number(item.get("price"))
    zone_low = number(item.get("safe_entry_zone_low"))
    zone_high = number(item.get("safe_entry_zone_high"))
    max_price = number(item.get("safe_entry_max_price") or zone_high)
    stop = number(item.get("stop_loss"))
    if None in (price, zone_low, zone_high, max_price, stop):
        return False
    return bool(stop < zone_low <= zone_high <= max_price < price)


def signal_fingerprint(item: dict[str, Any]) -> str:
    fields = {
        "symbol": text(item.get("symbol")),
        "signal_type": text(item.get("signal_type")),
        "entry_price": number(item.get("entry_price")),
        "safe_entry_zone_low": number(item.get("safe_entry_zone_low")),
        "safe_entry_zone_high": number(item.get("safe_entry_zone_high")),
        "safe_entry_max_price": number(item.get("safe_entry_max_price")),
        "stop_loss": number(item.get("stop_loss")),
        "target_price": number(item.get("target_price")),
        "rr_ratio": number(item.get("rr_ratio")),
        "risk_policy_version": text(item.get("risk_policy_version")),
    }
    encoded = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def new_alerts(opportunities: list[dict[str, Any]], state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    current = {
        text(item.get("symbol")): signal_fingerprint(item)
        for item in opportunities
        if qualifies_for_steady_buy_alert(item)
    }
    previous = state.get("signals") if isinstance(state.get("signals"), dict) else {}
    selected = []
    for item in opportunities:
        symbol = text(item.get("symbol"))
        if symbol not in current:
            continue
        prior = previous.get(symbol) if isinstance(previous.get(symbol), dict) else {}
        if prior.get("active") is not True or prior.get("fingerprint") != current[symbol]:
            selected.append(item)
    return selected, current


def new_candidate_alerts(
    opportunities: list[dict[str, Any]], state: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    current = {
        text(item.get("symbol")): signal_fingerprint(item)
        for item in opportunities
        if qualifies_for_candidate_alert(item)
    }
    previous = state.get("candidate_signals") if isinstance(state.get("candidate_signals"), dict) else {}
    selected = []
    for item in opportunities:
        symbol = text(item.get("symbol"))
        if symbol not in current:
            continue
        prior = previous.get(symbol) if isinstance(previous.get(symbol), dict) else {}
        if prior.get("active") is not True or prior.get("fingerprint") != current[symbol]:
            selected.append(item)
    return selected, current


def fmt_price(value: Any, currency: str) -> str:
    parsed = number(value)
    return "待确认" if parsed is None else f"{parsed:,.2f} {currency}".strip()


def compact_reason(item: dict[str, Any]) -> str:
    reasons = item.get("why_changed") if isinstance(item.get("why_changed"), list) else []
    return text(next((reason for reason in reversed(reasons) if text(reason)), item.get("action")), 180)


def build_card(items: list[dict[str, Any]], dashboard_url: str, test_message: bool = False) -> dict[str, Any]:
    if test_message:
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "green",
                "title": {"tag": "plain_text", "content": "稳健买点机器人测试成功"},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "✅ 飞书机器人已接入 AI 投研驾驶舱。以后只有通过全部硬门槛的稳健买点才会通知。"}},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": f"北京时间 {now_label()}"}]},
            ],
        }

    elements: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if index:
            elements.append({"tag": "hr"})
        symbol = text(item.get("symbol"))
        name = text(item.get("name")) or "名称待确认"
        currency = text(item.get("currency"))
        zone_low = item.get("safe_entry_zone_low") or item.get("entry_price")
        zone_high = item.get("safe_entry_zone_high") or item.get("entry_price")
        max_price = item.get("safe_entry_max_price") or zone_high
        content = (
            f"**{symbol} · {name}**\n"
            f"状态：{text(item.get('status_label')) or '稳健买点'}｜{text(item.get('signal_type'))}\n"
            f"现价：{fmt_price(item.get('price'), currency)}（{text(item.get('price_time')) or '时间待确认'}）\n"
            f"稳健区间：{fmt_price(zone_low, currency)} ～ {fmt_price(zone_high, currency)}\n"
            f"最高执行价：{fmt_price(max_price, currency)}｜止损：{fmt_price(item.get('stop_loss'), currency)}｜目标：{fmt_price(item.get('target_price'), currency)}\n"
            f"R/R：{number(item.get('rr_ratio')):.2f}:1｜机会分：{number(item.get('opportunity_score')):.1f}｜趋势：{number(item.get('trend_score')):.1f}｜拥挤度：{number(item.get('crowding_score')):.1f}\n"
            f"依据：{compact_reason(item)}"
        )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content}})
    elements.extend(
        [
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "仅为系统观察信号，不自动下单；复星证券仅整股交易，开盘跳空高于最高执行价不追。"}]},
            {
                "tag": "action",
                "actions": [
                    {"tag": "button", "type": "primary", "text": {"tag": "plain_text", "content": "查看投研驾驶舱"}, "url": dashboard_url}
                ],
            },
        ]
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": f"稳健买点提醒 · {len(items)} 只"},
        },
        "elements": elements,
    }


def build_candidate_card(items: list[dict[str, Any]], dashboard_url: str) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if index:
            elements.append({"tag": "hr"})
        currency = text(item.get("currency"))
        price = number(item.get("price"))
        max_price = number(item.get("safe_entry_max_price"))
        pullback = (price - max_price) / price * 100 if price and max_price else None
        content = (
            f"**{text(item.get('symbol'))} · {text(item.get('name')) or '名称待确认'}**\n"
            f"现价：{fmt_price(price, currency)}｜仍需回调：{pullback:.2f}%\n"
            f"稳健买入区间：{fmt_price(item.get('safe_entry_zone_low'), currency)} ～ "
            f"{fmt_price(item.get('safe_entry_zone_high'), currency)}\n"
            f"最高执行价：{fmt_price(max_price, currency)}｜止损：{fmt_price(item.get('stop_loss'), currency)}"
            f"｜目标：{fmt_price(item.get('target_price'), currency)}\n"
            f"R/R：{number(item.get('rr_ratio')):.2f}:1｜机会分：{number(item.get('opportunity_score')):.1f}"
            f"｜趋势：{number(item.get('trend_score')):.1f}\n"
            "**尚未到价，不买、不追高；进入安全区间后会再次提醒。**"
        )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content}})
    elements.extend([
        {"tag": "note", "elements": [{"tag": "plain_text", "content": "候选预警不是买入指令；正式到价提醒仍须通过全部风控和真实行情校验。"}]},
        {"tag": "action", "actions": [
            {"tag": "button", "type": "primary", "text": {"tag": "plain_text", "content": "查看稳健候选"}, "url": dashboard_url}
        ]},
    ])
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"稳健候选预警 · {len(items)} 只"},
        },
        "elements": elements,
    }


def signed_payload(card: dict[str, Any], secret: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"msg_type": "interactive", "card": card}
    if secret:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        payload.update({"timestamp": timestamp, "sign": base64.b64encode(digest).decode("utf-8")})
    return payload


def send_card(webhook: str, secret: str, card: dict[str, Any]) -> None:
    if not webhook.startswith(ALLOWED_WEBHOOK_PREFIXES):
        raise ValueError("FEISHU_BOT_WEBHOOK is not an approved Feishu/Lark bot URL")
    body = json.dumps(signed_payload(card, secret), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "us-stock-research-dashboard/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response_body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Feishu HTTP {error.code}: {response_body[:200]}") from error
    result = json.loads(response_body or "{}")
    code = result.get("code", result.get("StatusCode", 0))
    if code not in (0, None):
        raise RuntimeError(f"Feishu rejected message: code={code}, message={text(result.get('msg') or result.get('StatusMessage'))}")


def update_state(
    state: dict[str, Any],
    current: dict[str, str],
    successful: list[dict[str, Any]],
    candidate_current: dict[str, str] | None = None,
    candidate_successful: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    timestamp = now_label()

    def updated_signals(key: str, active: dict[str, str], sent: list[dict[str, Any]]) -> dict[str, Any]:
        previous = state.get(key) if isinstance(state.get(key), dict) else {}
        sent_symbols = {text(item.get("symbol")) for item in sent}
        next_signals: dict[str, Any] = {}
        for symbol, prior_value in previous.items():
            prior = prior_value if isinstance(prior_value, dict) else {}
            next_signals[symbol] = {**prior, "active": symbol in active, "last_checked_at": timestamp}
        for symbol, fingerprint in active.items():
            prior = next_signals.get(symbol, {})
            if symbol in sent_symbols:
                record = {
                    **prior,
                    "fingerprint": fingerprint,
                    "active": True,
                    "last_checked_at": timestamp,
                    "last_notified_at": timestamp,
                }
                record.pop("pending_fingerprint", None)
            elif prior.get("active") is True and prior.get("fingerprint") == fingerprint:
                record = {**prior, "active": True, "last_checked_at": timestamp}
            else:
                # Never mark a failed notification as delivered: it must retry.
                record = {
                    **prior,
                    "active": False,
                    "pending_fingerprint": fingerprint,
                    "last_checked_at": timestamp,
                }
            next_signals[symbol] = record
        return next_signals

    return {
        "schema_version": 2,
        "updated_at": timestamp,
        "signals": updated_signals("signals", current, successful),
        "candidate_signals": updated_signals("candidate_signals", candidate_current or {}, candidate_successful or []),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-message", action="store_true")
    parser.add_argument("--batch-size", type=int, default=4)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    webhook = os.environ.get("FEISHU_BOT_WEBHOOK", "").strip()
    secret = os.environ.get("FEISHU_BOT_SECRET", "").strip()
    if args.test_message:
        if not webhook:
            raise SystemExit("FEISHU_BOT_WEBHOOK is not configured")
        send_card(webhook, secret, build_card([], args.dashboard_url, test_message=True))
        print("Feishu test message sent successfully.")
        return 0

    archive = load_json(args.index, {})
    opportunities = archive.get("opportunities") if isinstance(archive.get("opportunities"), list) else []
    state = load_json(args.state, {"schema_version": 2, "signals": {}, "candidate_signals": {}})
    selected, current = new_alerts(opportunities, state)
    candidates, candidate_current = new_candidate_alerts(opportunities, state)
    print(
        f"Steady-buy signals: {len(current)}; new Feishu alerts: {len(selected)}; "
        f"qualified candidates: {len(candidate_current)}; new candidate alerts: {len(candidates)}"
    )
    if args.dry_run:
        print(json.dumps({
            "steady_buy": [{"symbol": item.get("symbol"), "fingerprint": signal_fingerprint(item)} for item in selected],
            "candidates": [{"symbol": item.get("symbol"), "fingerprint": signal_fingerprint(item)} for item in candidates],
        }, ensure_ascii=False))
        return 0
    if not webhook:
        print("FEISHU_BOT_WEBHOOK is not configured; notification step skipped.")
        return 0

    successful: list[dict[str, Any]] = []
    successful_candidates: list[dict[str, Any]] = []
    failures: list[str] = []
    batch_size = max(1, min(args.batch_size, 6))
    for offset in range(0, len(selected), batch_size):
        batch = selected[offset : offset + batch_size]
        try:
            send_card(webhook, secret, build_card(batch, args.dashboard_url))
            successful.extend(batch)
        except Exception as error:  # Do not expose the webhook in logs.
            symbols = ", ".join(text(item.get("symbol")) for item in batch)
            failures.append(f"{symbols}: {error}")

    for offset in range(0, len(candidates), batch_size):
        batch = candidates[offset : offset + batch_size]
        try:
            send_card(webhook, secret, build_candidate_card(batch, args.dashboard_url))
            successful_candidates.extend(batch)
        except Exception as error:  # Never expose webhook credentials in logs.
            symbols = ", ".join(text(item.get("symbol")) for item in batch)
            failures.append(f"candidate {symbols}: {error}")

    write_json(args.state, update_state(state, current, successful, candidate_current, successful_candidates))
    if successful:
        print("Feishu alerts sent: " + ", ".join(text(item.get("symbol")) for item in successful))
    if successful_candidates:
        print("Feishu candidate alerts sent: " + ", ".join(text(item.get("symbol")) for item in successful_candidates))
    if failures:
        print("Feishu alert failures: " + " | ".join(failures))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
