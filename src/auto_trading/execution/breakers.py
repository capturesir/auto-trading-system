"""R29 and R30 stop new entries only.

Exits, reductions, protective stops, and cancels are not decided here.
The numeric limits stay out of the hashed risk-limits object.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from auto_trading.alerts.critical import (
    HINT_R29,
    HINT_R30,
    write_critical_event,
    write_ops_notice,
)
from auto_trading.execution.risk_state import write_risk_limits
from auto_trading.portfolio.journal import TradeJournal

MACAU = timezone(timedelta(hours=8))
ANOMALY_BREAKER_COUNT = 3
NOTIFY_FAIL_HALT_SEC = 300
SUMMARY_OVERDUE_MIN = 120
COUNTED_KINDS = frozenset({"BROKER_REJECTED", "UNKNOWN", "ORDER_HASH_MISMATCH"})

_pinger = None


def set_health_pinger(fn) -> None:
    """Test hook. Production uses HEALTHCHECK_PING_URL and does not log it."""
    global _pinger
    _pinger = fn


def ping_fail() -> None:
    if _pinger is not None:
        _pinger("/fail")
        return
    url = os.environ.get("HEALTHCHECK_PING_URL", "").strip()
    if not url:
        return
    target = url.rstrip("/") + "/fail"
    try:
        import urllib.request

        urllib.request.urlopen(target, timeout=2).close()  # noqa: S310
    except Exception:
        return


def risk_window(now: datetime) -> tuple[datetime, datetime, str]:
    """Macau window from 08:00 to the next 08:00. The date is the start date."""
    local = now.astimezone(MACAU)
    start = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if local < start:
        start = start - timedelta(days=1)
    end = start + timedelta(days=1)
    return start, end, start.date().isoformat()


def _state_path(home: Path) -> Path:
    return home / "state" / "breakers.json"


def flags(home: Path) -> dict[str, Any]:
    path = _state_path(home)
    if not path.is_file():
        return {"r29_active": False, "r30_active": False, "recon_freeze": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"r29_active": False, "r30_active": False, "recon_freeze": False}
    if not isinstance(payload, dict):
        return {"r29_active": False, "r30_active": False, "recon_freeze": False}
    return payload


def _save(home: Path, payload: dict[str, Any]) -> None:
    folder = home / "state"
    folder.mkdir(parents=True, exist_ok=True)
    target = _state_path(home)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def set_infra_pause(home: Path, active: bool) -> None:
    path = home / "state" / "risk_limits.json"
    if not path.is_file():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    for account in payload.get("accounts") or []:
        account.setdefault("breakers", {})["infra_pause"] = active
    write_risk_limits(home, payload)


def _activate_r29(home: Path, now: datetime, *, count: int, account: str) -> None:
    state = flags(home)
    _window_start, _end, day = risk_window(now)
    state["r29_active"] = True
    state["r29_risk_date"] = day
    state["r29_count"] = count
    _save(home, state)
    set_infra_pause(home, True)
    TradeJournal(home).record_anomaly(
        kind="ANOMALY_BREAKER",
        detail={"count": count, "risk_date": day},
        when=now,
    )
    try:
        write_critical_event(
            home,
            kind="r29_anomaly_breaker",
            reason_code="ANOMALY_BREAKER_ACTIVE",
            rule_id="R29",
            now=now,
            account=account,
            message_zh="同一風控日異常達到上限，新開倉已暫停。",
            resume_hint=HINT_R29,
            detail={"count": count},
        )
    except Exception:
        return


def note_execution_anomaly(
    home: Path,
    *,
    order_id: str,
    kind: str,
    now: datetime,
    account: str = "",
) -> bool:
    """Count one order once. Risk-gate rejections are ignored. True when R29 trips."""
    if kind not in COUNTED_KINDS:
        return False
    _window_start, _end, day = risk_window(now)
    inserted = TradeJournal(home).note_counted_anomaly(day, order_id, kind)
    count = TradeJournal(home).counted_anomalies(day)
    state = flags(home)
    if inserted and count >= ANOMALY_BREAKER_COUNT and not state.get("r29_active"):
        _activate_r29(home, now, count=count, account=account)
        return True
    return bool(flags(home).get("r29_active"))


def clear_r29(home: Path, now: datetime) -> None:
    state = flags(home)
    _window_start, _end, day = risk_window(now)
    state["r29_active"] = False
    state["r29_risk_date"] = day
    state["r29_count"] = 0
    _save(home, state)
    set_infra_pause(home, False)
    TradeJournal(home).clear_counted_anomalies(day)
    TradeJournal(home).record_anomaly(
        kind="ANOMALY_BREAKER",
        detail={"resolved": True, "risk_date": day},
        when=now,
    )
    write_ops_notice(home, message_zh="R29 連續異常熔斷已解除。", now=now)


def maybe_auto_clear_r29(
    home: Path,
    now: datetime,
    *,
    unknown: bool,
    recon_freeze: bool,
) -> bool:
    """At the next 08:00, clear R29 when nothing unresolved remains."""
    state = flags(home)
    if not state.get("r29_active"):
        return False
    _window_start, _end, day = risk_window(now)
    if day == state.get("r29_risk_date"):
        return False
    if unknown or recon_freeze or state.get("recon_freeze"):
        return False
    if TradeJournal(home).unknown_orders():
        return False
    clear_r29(home, now)
    return True


def summary_overdue(home: Path, now: datetime) -> bool:
    """True at or after 08:00 when the 06:00 summary is still missing."""
    local = now.astimezone(MACAU)
    eight = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if local < eight:
        return False
    # 06:00 plus SUMMARY_OVERDUE_MIN lands on this 08:00.
    if SUMMARY_OVERDUE_MIN != 120:
        raise RuntimeError("summary overdue window changed")
    started = eight - timedelta(days=1)
    name = f"summary_{started.strftime('%Y%m%d')}.json"
    return not (home / "reports" / "daily" / name).is_file()


def _critical_age_sec(home: Path, now: datetime) -> float:
    oldest = 0.0
    pending = home / "alerts" / "pending"
    if not pending.is_dir():
        return 0.0
    moment = now if now.tzinfo else now.replace(tzinfo=MACAU)
    for path in pending.glob("*_exec_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(str(payload["created_at_utc"]))
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age = (moment - created).total_seconds()
        oldest = max(oldest, age)
    failed = home / "alerts" / "failed"
    if failed.is_dir():
        for path in failed.glob("*_exec_*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                created = datetime.fromisoformat(str(payload["created_at_utc"]))
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age = (moment - created).total_seconds()
            oldest = max(oldest, age)
    return oldest


def notify_failure_active(home: Path, now: datetime) -> bool:
    aged = _critical_age_sec(home, now) >= NOTIFY_FAIL_HALT_SEC
    return aged or summary_overdue(home, now)


def refresh_r30(home: Path, now: datetime, *, account: str = "") -> bool:
    """Trip or clear R30 from the current outbox and summary state."""
    state = flags(home)
    active = notify_failure_active(home, now)
    if active and not state.get("r30_active"):
        state["r30_active"] = True
        _save(home, state)
        TradeJournal(home).record_anomaly(
            kind="NOTIFY_FAILURE",
            detail={"notify_fail_halt_sec": NOTIFY_FAIL_HALT_SEC},
            when=now,
        )
        try:
            write_critical_event(
                home,
                kind="r30_notify_failure",
                reason_code="NOTIFY_FAILURE_PAUSE",
                rule_id="R30",
                now=now,
                account=account,
                message_zh="關鍵通知未送出，新開倉已暫停。",
                resume_hint=HINT_R30,
            )
        except Exception:
            ping_fail()
            return True
        ping_fail()
        return True
    if not active and state.get("r30_active"):
        state["r30_active"] = False
        _save(home, state)
        TradeJournal(home).record_anomaly(
            kind="NOTIFY_FAILURE",
            detail={"resolved": True},
            when=now,
        )
        write_ops_notice(home, message_zh="R30 通知失效暫停已自動解除。", now=now)
        return False
    return bool(state.get("r30_active"))
