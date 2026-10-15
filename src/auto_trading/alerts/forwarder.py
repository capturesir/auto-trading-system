"""Scan pending alerts into the notification outbox.

Execution critical alerts dedupe only by alert id. B-group alerts keep
their cooldown dedupe. The scan interval is 10 seconds.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from auto_trading.alerts.critical import MACAU
from auto_trading.execution.breakers import flags, ping_fail
from auto_trading.portfolio.journal import TradeJournal

ALERT_SCAN_INTERVAL_SEC = 10
EXEC_RETRY_SEC = 10
EXEC_MAX_ATTEMPTS = 30
UNFORWARDED_WARN_SEC = 60
FAILED_RETRY_SEC = 60
B_GROUP_BACKOFF_MIN = (1, 5, 15)


def _load(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _attempts_path(path: Path) -> Path:
    return Path(str(path) + ".attempts")


def _read_attempts(path: Path) -> int:
    side = _attempts_path(path)
    if not side.is_file():
        return 0
    try:
        return int(side.read_text(encoding="utf-8").strip() or "0")
    except ValueError:
        return 0


def _write_attempts(path: Path, count: int) -> None:
    _attempts_path(path).write_text(str(count), encoding="utf-8")


def _move(path: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / path.name
    os.replace(path, target)
    side = _attempts_path(path)
    if side.is_file():
        os.replace(side, folder / side.name)
    return target


def _fail(path: Path, home: Path, reason: str) -> None:
    failed = home / "alerts" / "failed"
    moved = _move(path, failed)
    (failed / f"{moved.name}.err.json").write_text(
        json.dumps({"reason": reason}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _outbox_exec(home: Path, alert: dict[str, Any], now: datetime) -> Path:
    local = now.astimezone(MACAU)
    stamp = local.strftime("%Y%m%dT%H%M%S")
    kind = str(alert["kind"])
    folder = home / "notifications" / "outbox"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{stamp}_exec_critical_{kind}.json"
    payload = {
        "schema_version": "notice/1.0",
        "kind": "exec_critical",
        "level": "critical",
        "source": "execution",
        "alert_id": alert["alert_id"],
        "event_id": alert.get("event_id"),
        "exec_kind": kind,
        "severity": "critical",
        "message_zh": alert.get("message_zh"),
        "resume_hint": alert.get("resume_hint"),
        "created_at": alert.get("created_at"),
        "dedup_key": alert.get("dedup_key"),
    }
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def _outbox_b(home: Path, alert: dict[str, Any], now: datetime) -> Path:
    local = now.astimezone(MACAU)
    stamp = local.strftime("%Y%m%dT%H%M%S")
    folder = home / "notifications" / "outbox"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{stamp}_b_alert.json"
    payload = {
        "schema_version": "notice/1.0",
        "kind": "b_alert",
        "level": alert.get("severity") or "info",
        "source": "b_group",
        "alert_id": alert["alert_id"],
        "severity": alert.get("severity"),
        "message_zh": alert.get("message_zh"),
        "dedup_key": alert.get("dedup_key"),
    }
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def _exec_ready(alert: dict[str, Any]) -> bool:
    required = (
        "schema_version",
        "alert_id",
        "event_id",
        "created_at",
        "created_at_utc",
        "source",
        "severity",
        "kind",
        "rule_id",
        "reason_code",
        "message_zh",
        "resume_hint",
        "dedup_key",
    )
    return all(key in alert and alert[key] not in (None, "") for key in required)


def _created(alert: dict[str, Any]) -> datetime | None:
    raw = alert.get("created_at_utc")
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def unforwarded_critical(home: Path, now: datetime) -> tuple[int, bool]:
    """Count critical alerts still outside the outbox, and whether any is late."""
    count = 0
    late = False
    moment = now if now.tzinfo else now.replace(tzinfo=UTC)
    for folder_name in ("pending", "failed"):
        folder = home / "alerts" / folder_name
        if not folder.is_dir():
            continue
        for path in folder.glob("*_exec_*.json"):
            payload = _load(path)
            if payload is None or payload.get("schema_version") != "exec_alert/1.0":
                continue
            count += 1
            created = _created(payload)
            if created is not None and (moment - created).total_seconds() > UNFORWARDED_WARN_SEC:
                late = True
    return count, late


def status_warning(home: Path, now: datetime) -> bool:
    """Warn, and ping fail, when a critical alert missed the 60 second outbox."""
    _count, late = unforwarded_critical(home, now)
    if late:
        ping_fail()
    return late


def _forward_exec(home: Path, path: Path, alert: dict[str, Any], now: datetime) -> None:
    journal = TradeJournal(home)
    if journal.alert_seen(str(alert["alert_id"])):
        journal.record_alert(
            alert_id=str(alert["alert_id"]) + ":" + uuid.uuid4().hex,
            dedup_key=str(alert.get("dedup_key") or ""),
            source="execution",
            status="suppressed_duplicate",
            when=now,
        )
        sent = home / "alerts" / "sent"
        _move(path, sent)
        return
    try:
        outbox = _outbox_exec(home, alert, now)
    except OSError:
        count = _read_attempts(path) + 1
        _write_attempts(path, count)
        note_retry_time(path, now + timedelta(seconds=EXEC_RETRY_SEC))
        if count >= EXEC_MAX_ATTEMPTS:
            _fail(path, home, "outbox")
        return
    journal.record_alert(
        alert_id=str(alert["alert_id"]),
        dedup_key=str(alert.get("dedup_key") or ""),
        rule_id=str(alert.get("rule_id") or ""),
        severity=str(alert.get("severity") or ""),
        source="execution",
        status="forwarded",
        outbox_file=str(outbox),
        when=now,
    )
    journal.record_critical_event(
        alert_id=str(alert["alert_id"]),
        event_id=str(alert.get("event_id") or ""),
        kind=str(alert.get("kind") or ""),
        rule_id=str(alert.get("rule_id") or ""),
        reason_code=str(alert.get("reason_code") or ""),
        created_at_utc=str(alert.get("created_at_utc") or ""),
        pending_file=path.name,
        outbox_file=str(outbox),
        status="forwarded",
        when=now,
    )
    _move(path, home / "alerts" / "sent")


def _cooldown_hit(home: Path, alert: dict[str, Any], now: datetime) -> bool:
    key = str(alert.get("dedup_key") or "")
    raw_until = alert.get("cooldown_until")
    if not key or not raw_until:
        return False
    try:
        until = datetime.fromisoformat(str(raw_until))
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    moment = now if now.tzinfo else now.replace(tzinfo=UTC)
    if moment >= until:
        return False
    return TradeJournal(home).dedup_forwarded(key)


def _forward_b(home: Path, path: Path, alert: dict[str, Any], now: datetime) -> None:
    journal = TradeJournal(home)
    if journal.alert_seen(str(alert["alert_id"])) or _cooldown_hit(home, alert, now):
        journal.record_alert(
            alert_id=str(alert["alert_id"]) + ":dup",
            dedup_key=str(alert.get("dedup_key") or ""),
            source="b_group",
            status="suppressed_duplicate",
            when=now,
        )
        _move(path, home / "alerts" / "sent")
        return
    try:
        outbox = _outbox_b(home, alert, now)
    except OSError:
        count = _read_attempts(path) + 1
        _write_attempts(path, count)
        if count >= len(B_GROUP_BACKOFF_MIN):
            _fail(path, home, "outbox")
        return
    journal.record_alert(
        alert_id=str(alert["alert_id"]),
        dedup_key=str(alert.get("dedup_key") or ""),
        rule_id=str(alert.get("rule_id") or ""),
        ticker=str(alert.get("ticker") or ""),
        severity=str(alert.get("severity") or ""),
        source="b_group",
        status="forwarded",
        outbox_file=str(outbox),
        when=now,
    )
    _move(path, home / "alerts" / "sent")


def scan_once(home: Path, now: datetime | None = None) -> int:
    """Forward every ready pending file. Returns how many outbox files were written."""
    moment = now or datetime.now(UTC)
    pending = home / "alerts" / "pending"
    if not pending.is_dir():
        return 0
    written = 0
    before = (
        list((home / "notifications" / "outbox").glob("*.json"))
        if (home / "notifications" / "outbox").is_dir()
        else []
    )
    for path in sorted(pending.glob("*.json")):
        if path.name.endswith(".tmp"):
            continue
        alert = _load(path)
        if alert is None:
            _fail(path, home, "json")
            continue
        schema = alert.get("schema_version")
        if schema == "exec_alert/1.0":
            if not _exec_ready(alert):
                _fail(path, home, "fields")
                continue
            if _read_attempts(path) and not _retry_due(path, moment):
                continue
            _forward_exec(home, path, alert, moment)
        elif schema == "alert/1.0":
            if not alert.get("alert_id"):
                _fail(path, home, "fields")
                continue
            _forward_b(home, path, alert, moment)
        else:
            _fail(path, home, "schema")
    after_dir = home / "notifications" / "outbox"
    if after_dir.is_dir():
        written = len(list(after_dir.glob("*.json"))) - len(before)
    return max(written, 0)


def _retry_due(path: Path, now: datetime) -> bool:
    side = Path(str(path) + ".next")
    if not side.is_file():
        return True
    try:
        nxt = datetime.fromisoformat(side.read_text(encoding="utf-8").strip())
    except ValueError:
        return True
    if nxt.tzinfo is None and now.tzinfo is not None:
        nxt = nxt.replace(tzinfo=now.tzinfo)
    return now >= nxt


def note_retry_time(path: Path, when: datetime) -> None:
    Path(str(path) + ".next").write_text(when.isoformat(timespec="seconds"), encoding="utf-8")


def retry_failed_critical(home: Path, now: datetime) -> int:
    """While R30 is active, resend failed critical files every 60 seconds."""
    if not flags(home).get("r30_active"):
        return 0
    marker = home / "state" / "failed_retry_at.txt"
    if marker.is_file():
        try:
            previous = datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
        except ValueError:
            previous = None
        if previous is not None and (now - previous).total_seconds() < FAILED_RETRY_SEC:
            return 0
    failed = home / "alerts" / "failed"
    if not failed.is_dir():
        return 0
    pending = home / "alerts" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path in sorted(failed.glob("*_exec_*.json")):
        payload = _load(path)
        if payload is None or payload.get("schema_version") != "exec_alert/1.0":
            continue
        os.replace(path, pending / path.name)
        moved += 1
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(now.isoformat(timespec="seconds"), encoding="utf-8")
    if moved:
        scan_once(home, now)
    return moved
