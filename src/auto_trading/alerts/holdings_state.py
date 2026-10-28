"""Durable state for the holdings monitor.

Writes use a temporary file, ``fsync``, and ``rename``. A half-written state
file is ignored on the next read so a tick can continue.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from auto_trading.jsonutil import loads_strict

MACAU = timezone(timedelta(hours=8))
STATE_SCHEMA = "holdings_monitor_state/1.0"
STATE_NAME = "holdings_monitor.json"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace ``path`` via a unique temp file, fsync, and rename.

    The temp name includes the process id and a clock so two writers of the
    same path do not rename one shared ``*.tmp``. A missing temp on rename
    is retried once. The destination is left unchanged when both attempts fail.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    missing: FileNotFoundError | None = None
    for _attempt in range(2):
        try:
            _write_unique(path, text)
        except FileNotFoundError as exc:
            missing = exc
            path.parent.mkdir(parents=True, exist_ok=True)
            continue
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return
    if missing is not None:
        raise missing


def _write_unique(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def state_path(trading_home: Path) -> Path:
    return trading_home / "state" / STATE_NAME


def blank_state(interval_sec: float) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA,
        "last_tick_at": None,
        "last_ok_at": None,
        "interval_sec": interval_sec,
        "n_positions": 0,
        "quote_stats": {"ibkr": 0, "yahoo": 0, "fail": 0},
        "per_ticker": {},
        "tick_overrun": False,
        "retry_incomplete_snapshot": False,
        "account_status": "running",
        "data_error_at": None,
        "last_snapshot_at": None,
    }


def load_state(trading_home: Path, interval_sec: float) -> dict[str, Any]:
    """Return the state file, or a blank document when it is missing or partial."""
    path = state_path(trading_home)
    if not path.is_file():
        return blank_state(interval_sec)
    try:
        payload = loads_strict(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return blank_state(interval_sec)
    if not isinstance(payload, dict):
        return blank_state(interval_sec)
    if payload.get("schema_version") != STATE_SCHEMA:
        return blank_state(interval_sec)
    merged = blank_state(interval_sec)
    merged.update(payload)
    if not isinstance(merged.get("per_ticker"), dict):
        merged["per_ticker"] = {}
    if not isinstance(merged.get("quote_stats"), dict):
        merged["quote_stats"] = {"ibkr": 0, "yahoo": 0, "fail": 0}
    return merged


def save_state(trading_home: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(state_path(trading_home), payload)


def stamp(moment: datetime) -> str:
    """Filename stamp in Macau local time."""
    aware = moment if moment.tzinfo else moment.replace(tzinfo=MACAU)
    return aware.astimezone(MACAU).strftime("%Y%m%dT%H%M%S")


def as_macau(moment: datetime) -> datetime:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=MACAU)
    return aware.astimezone(MACAU)


def iso_macau(moment: datetime) -> str:
    return as_macau(moment).isoformat(timespec="seconds")


def parse_moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=MACAU)
    return parsed
