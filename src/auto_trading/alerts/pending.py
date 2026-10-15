"""Pending-only alert files.

B-group code writes ``alerts/pending/`` and does not read, move, or delete
those files. ``alerts/sent/`` and ``alerts/failed/`` belong to the execution
notifier. ``alert_id`` is derived from the dedup key, the date, and the rule
version so a rerun produces the same id.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

ALERT_SCHEMA = "alert/1.0"
RULE_VERSION = "1"


def make_alert_id(dedup_key: str, created_on: str, rule_version: str = RULE_VERSION) -> str:
    """Stable id. The same inputs always hash to the same value."""
    raw = f"{dedup_key}|{created_on}|{rule_version}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def dedup_key(rule_id: str, ticker: str, direction: str, trading_date: str) -> str:
    return f"{rule_id}|{ticker}|{direction}|{trading_date}"


def write_pending(
    trading_home: Path,
    alert: Mapping[str, Any],
    *,
    filename_stamp: str,
) -> Path:
    """Atomically write one pending alert. Never touches sent or failed."""
    pending = trading_home / "alerts" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    rule_id = str(alert["rule_id"])
    ticker = str(alert["ticker"]).replace("/", "_")
    target = pending / f"{filename_stamp}_{rule_id}_{ticker}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(dict(alert), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def build_alert(
    *,
    rule_id: str,
    alert_type: str,
    severity: str,
    ticker: str,
    message_zh: str,
    trading_date: str,
    direction: str = "na",
    created_at: datetime,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fill the spec fields. ``channels`` stays empty: this process does not send."""
    key = dedup_key(rule_id, ticker, direction, trading_date)
    payload: dict[str, Any] = {
        "schema_version": ALERT_SCHEMA,
        "alert_id": make_alert_id(key, trading_date),
        "created_at": created_at.isoformat(timespec="seconds"),
        "rule_id": rule_id,
        "type": alert_type,
        "severity": severity,
        "ticker": ticker,
        "name": None,
        "market": None,
        "currency": None,
        "price": None,
        "price_time": None,
        "price_source": None,
        "data_delay_min": None,
        "provisional": False,
        "trigger": {},
        "context": {},
        "message_zh": message_zh,
        "dedup_key": key,
        "cooldown_until": None,
        "channels": [],
    }
    if extra:
        payload.update(extra)
    return payload


def summarize_b_alerts(rows: list[Mapping[str, Any]]) -> dict[str, int]:
    """Count execution-side ``b_alerts`` statuses. This does not read sent/."""
    counts = {"forwarded": 0, "suppressed_duplicate": 0, "failed": 0}
    for row in rows:
        status = str(row.get("status"))
        if status in counts:
            counts[status] += 1
    return counts
