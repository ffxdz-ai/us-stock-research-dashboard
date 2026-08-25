#!/usr/bin/env python3
"""Move public, quote-only Futu snapshots through an authenticated HTTPS bridge.

No account, position, order, cash, share count, cost basis, or trading field is
accepted. A missing bridge degrades to the existing public-data fallback.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = ROOT / "data" / "latest_futu_local_snapshot.json"
VALID_CODE = re.compile(r"^(US|HK|SH|SZ|SG|MY|JP|CC)\.[A-Z0-9]+$")
PUBLIC_QUOTE_FIELDS = frozenset({
    "code", "symbol", "name", "last_price", "cur_price", "prev_close_price",
    "open_price", "high_price", "low_price", "volume", "turnover",
    "turnover_rate", "change_rate", "update_time", "data_date", "data_time",
    "quote_time", "pe_ttm", "pb_rate", "ps_ttm", "market_val", "pre_price",
    "after_price", "overnight_price", "source",
})
ALLOWED_FEISHU_WEBHOOK_PREFIXES = (
    "https://open.feishu.cn/open-apis/bot/v2/hook/",
    "https://open.larksuite.com/open-apis/bot/v2/hook/",
)


def configured_value(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if value or os.name != "nt":
        return value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            stored, _ = winreg.QueryValueEx(key, name)
        return str(stored or "").strip()
    except (ImportError, OSError):
        return ""


def bridge_endpoint(path: str = "/futu/snapshot") -> str:
    base_url = configured_value("FUTU_BRIDGE_URL")
    if not base_url:
        raise RuntimeError("FUTU_BRIDGE_URL is not configured")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise RuntimeError("FUTU_BRIDGE_URL must be an HTTPS endpoint without embedded credentials")
    if path not in ("/futu/snapshot", "/futu/feishu-config"):
        raise RuntimeError("Unsupported authenticated bridge endpoint")
    return base_url.rstrip("/") + path


def bridge_headers() -> dict[str, str]:
    token = configured_value("FUTU_BRIDGE_TOKEN")
    if not token:
        raise RuntimeError("FUTU_BRIDGE_TOKEN is not configured")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "us-stock-research-futu-quote-bridge/1.0",
    }


def public_snapshot(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not (payload.get("opend") or {}).get("connected"):
        raise RuntimeError("Futu OpenD is not connected")
    raw_quotes = payload.get("quotes")
    if not isinstance(raw_quotes, dict) or not raw_quotes or len(raw_quotes) > 250:
        raise RuntimeError("Futu quote snapshot is empty or exceeds the safety limit")

    quotes: dict[str, dict[str, Any]] = {}
    for raw_code, raw_quote in raw_quotes.items():
        code = str(raw_code or "").strip().upper()
        if not VALID_CODE.fullmatch(code) or not isinstance(raw_quote, dict):
            continue
        clean = {
            field: value
            for field, value in raw_quote.items()
            if field in PUBLIC_QUOTE_FIELDS and (value is None or isinstance(value, (str, int, float, bool)))
        }
        if not clean.get("quote_time"):
            continue
        clean["code"] = code
        quotes[code] = clean
    if not quotes:
        raise RuntimeError("No public Futu quotes include a real exchange timestamp")

    return {
        "schema_version": 2,
        "generated_at": str(payload.get("generated_at") or ""),
        "generated_label": str(payload.get("generated_label") or ""),
        "opend": {"connected": True, "market_data_only": True},
        "summary": {
            "scope": str((payload.get("summary") or {}).get("scope") or "all"),
            "quotes_returned": len(quotes),
        },
        "privacy": {
            "contains_account": False,
            "contains_positions": False,
            "contains_cash": False,
            "contains_cost_basis": False,
            "trading_disabled": True,
        },
        "quotes": quotes,
    }


def request_bridge(
    method: str,
    payload: dict[str, Any] | None = None,
    *,
    path: str = "/futu/snapshot",
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(bridge_endpoint(path), data=body, headers=bridge_headers(), method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Futu quote bridge returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Futu quote bridge is unavailable: {exc}") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Futu quote bridge rejected the request: {(result or {}).get('code', 'unknown')}")
    return result


def push_snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return request_bridge("PUT", public_snapshot(payload))


def configure_live_feishu_alerts() -> dict[str, Any]:
    """Provision GitHub-only bot secrets into encrypted Worker-side storage."""
    webhook = configured_value("FEISHU_BOT_WEBHOOK")
    if not webhook or not webhook.startswith(ALLOWED_FEISHU_WEBHOOK_PREFIXES):
        raise RuntimeError("FEISHU_BOT_WEBHOOK is missing or is not an approved Feishu/Lark bot URL")
    secret = configured_value("FEISHU_BOT_SECRET")
    return request_bridge(
        "PUT",
        {"webhook": webhook, "secret": secret},
        path="/futu/feishu-config",
    )


def pull_snapshot(path: Path = DEFAULT_SNAPSHOT, *, max_age_hours: float = 12.0) -> dict[str, Any]:
    result = request_bridge("GET")
    snapshot = result.get("snapshot")
    if not isinstance(snapshot, dict) or not (snapshot.get("bridge") or {}).get("authenticated"):
        raise RuntimeError("Cloud bridge did not return an authenticated quote-only snapshot")
    validated = public_snapshot(snapshot)
    generated_raw = str(validated.get("generated_at") or "").replace("Z", "+00:00")
    try:
        generated = datetime.fromisoformat(generated_raw)
    except ValueError as exc:
        raise RuntimeError("Futu snapshot has no trustworthy generation timestamp") from exc
    if generated.tzinfo is None:
        raise RuntimeError("Futu snapshot generation timestamp has no time zone")
    age_hours = (datetime.now(timezone.utc) - generated.astimezone(timezone.utc)).total_seconds() / 3600
    if age_hours > max_age_hours:
        raise RuntimeError(f"Authenticated Futu snapshot expired ({age_hours:.1f} hours old)")
    if age_hours < -5 / 60:
        raise RuntimeError("Authenticated Futu snapshot generation time is in the future")

    validated["bridge"] = {
        "authenticated": True,
        "transport": "cloudflare-worker-kv",
        "received_at": str((snapshot.get("bridge") or {}).get("received_at") or ""),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(validated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return validated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pull", "push", "configure-alerts"))
    parser.add_argument("--path", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--max-age-hours", type=float, default=12.0)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    try:
        if args.action == "pull":
            snapshot = pull_snapshot(args.path, max_age_hours=args.max_age_hours)
            count = int((snapshot.get("summary") or {}).get("quotes_returned") or 0)
            message = f"Downloaded {count} authenticated, quote-only Futu market snapshots"
        elif args.action == "push":
            snapshot = json.loads(args.path.read_text(encoding="utf-8"))
            result = push_snapshot_payload(snapshot)
            message = f"Uploaded {int(result.get('quotes_returned') or 0)} quote-only Futu market snapshots"
        else:
            configure_live_feishu_alerts()
            message = "Configured encrypted cloud Futu-to-Feishu price monitoring"
        if not args.quiet:
            print(message)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        if not args.quiet:
            print(f"Futu quote bridge unavailable; public-data fallback remains active: {exc}", file=sys.stderr)
        return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
