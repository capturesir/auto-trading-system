"""Critical execution events.

The full stop is already done before these files are written. A failure
here must not undo that stop.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MACAU = timezone(timedelta(hours=8))
EXEC_SCHEMA = "exec_alert/1.0"

HINT_FULL_STOP = "只能在主機本地執行 trade resume"
HINT_R29 = (
    "下一個風控日 08:00 自動解除（需無未解決 UNKNOWN 和對帳凍結），"
    "或經理在主機本地執行 trade resume --breaker R29"
)
HINT_R30 = "outbox 恢復且每日摘要已產生後自動解除"

_KINDS = frozenset(
    {
        "kill_switch",
        "r0_violation",
        "r9_halt",
        "r9_resume_half_size",
        "r9_full_size",
        "r9_state_corrupt",
        "r18_stop",
        "r29_anomaly_breaker",
        "r30_notify_failure",
        "pool_rebuild_failed",
        "pool_stale",
        "approved_hash_mismatch",
    }
)


def write_critical_event(
    trading_home: Path,
    *,
    kind: str,
    reason_code: str,
    rule_id: str,
    now: datetime,
    account: str = "",
    event_id: str | None = None,
    actions: dict[str, bool] | None = None,
    state: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
    message_zh: str,
    resume_hint: str,
) -> dict[str, Any]:
    """Append the local jsonl line and the pending alert. May raise."""
    if kind not in _KINDS:
        raise ValueError("unknown critical kind")
    moment = now if now.tzinfo else now.replace(tzinfo=UTC)
    local = moment.astimezone(MACAU)
    utc = moment.astimezone(UTC)
    alert_id = str(uuid.uuid4())
    identity = event_id or alert_id
    suffix = account[-4:] if len(account) >= 4 else account
    document: dict[str, Any] = {
        "schema_version": EXEC_SCHEMA,
        "alert_id": alert_id,
        "event_id": identity,
        "created_at": local.isoformat(timespec="seconds"),
        "created_at_utc": utc.isoformat(timespec="seconds"),
        "source": "execution",
        "severity": "critical",
        "kind": kind,
        "rule_id": rule_id,
        "reason_code": reason_code,
        "account_suffix": suffix,
        "host": socket.gethostname(),
        "actions": actions
        or {
            "kill_switch_file": False,
            "open_orders_cancelled": False,
            "protective_stops_kept": True,
            "disconnected": False,
            "strategies_halted": False,
        },
        "state": state
        or {
            "nlv_base": None,
            "hwm_base": None,
            "drawdown_pct": None,
            "daily_pnl_pct": None,
        },
        "detail": detail or {},
        "message_zh": message_zh,
        "resume_hint": resume_hint,
        "dedup_key": f"exec|{kind}|{identity}",
    }
    day = local.strftime("%Y%m%d")
    log_dir = trading_home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / f"critical_{day}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(document, ensure_ascii=False) + "\n")
    stamp = local.strftime("%Y%m%dT%H%M%S")
    pending = trading_home / "alerts" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    target = pending / f"{stamp}_exec_{kind}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return document


def write_ops_notice(
    trading_home: Path,
    *,
    message_zh: str,
    now: datetime,
    kind: str = "ops",
) -> Path:
    """Warn-level ops note. This is not a critical alert."""
    moment = now if now.tzinfo else now.replace(tzinfo=UTC)
    local = moment.astimezone(MACAU)
    stamp = local.strftime("%Y%m%dT%H%M%S")
    folder = trading_home / "notifications" / "outbox"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{stamp}_{kind}.json"
    payload = {
        "schema_version": "notice/1.0",
        "kind": kind,
        "level": "warn",
        "source": "execution",
        "created_at": local.isoformat(timespec="seconds"),
        "message_zh": message_zh,
    }
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target
