"""Local heartbeat. A gap of two minutes raises an alert. No network ping."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

INTERVAL = timedelta(seconds=60)
ALERT_AFTER = timedelta(seconds=120)


def write_heartbeat(path: Path, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": "heartbeat/1.0", "at": now.isoformat(timespec="seconds")}
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def heartbeat_gap(path: Path, now: datetime) -> timedelta | None:
    """None when the file is missing. Otherwise how long since the last beat."""
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    last = datetime.fromisoformat(payload["at"])
    if last.tzinfo is None or now.tzinfo is None:
        raise ValueError("heartbeat timestamps need a timezone")
    return now - last


def heartbeat_alert_due(path: Path, now: datetime) -> bool:
    """True after two missed minutes, or when the file was never written."""
    gap = heartbeat_gap(path, now)
    if gap is None:
        return True
    return gap >= ALERT_AFTER
