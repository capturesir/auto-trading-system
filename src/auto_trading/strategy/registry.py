"""Append-only strategy registry.

``approved``, ``live_promoted``, and ``size_changed`` accept only
``actor=user``. ``news_event`` v2 is archived: it cannot be registered, and
the runner treats only v3 as the current news strategy.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from auto_trading.jsonutil import loads_strict
from auto_trading.strategy.spec import spec_hash

USER_ONLY = frozenset({"approved", "live_promoted", "size_changed"})
RUNNABLE_MODES = frozenset({"paper", "live"})
MACAU = ZoneInfo("Asia/Macau")


class RegistryError(Exception):
    """A registry write violated an actor or version rule."""


def append_event(path: Path, event: Mapping[str, Any]) -> None:
    """Append one JSON object. The file is never rewritten."""
    name = event.get("event")
    actor = event.get("actor")
    if name in USER_ONLY and actor != "user":
        raise RegistryError(f"{name} requires actor=user, got {actor}")
    strategy_id = event.get("strategy_id")
    version = event.get("version")
    if strategy_id == "news_event" and version == 2:
        raise RegistryError("news_event v2 is archived and cannot be registered")
    if strategy_id == "news_event" and version != 3 and name in USER_ONLY | {"paper_started"}:
        raise RegistryError("only news_event v3 can be registered to run")
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(event)
    row.setdefault("ts", datetime.now(MACAU).isoformat(timespec="seconds"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_events(path: Path) -> list[dict[str, Any]]:
    """Return every row. A missing file is an empty register."""
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(loads_strict(line))
    return rows


def latest(path: Path, strategy_id: str, version: int) -> dict[str, Any] | None:
    """Last row for one strategy version."""
    found = [
        row
        for row in read_events(path)
        if row.get("strategy_id") == strategy_id and row.get("version") == version
    ]
    if not found:
        return None
    return found[-1]


def is_runnable(path: Path, document: Mapping[str, Any]) -> bool:
    """True when the latest row matches the hash and is paper or live."""
    strategy_id = str(document.get("id"))
    version = int(document.get("version", 0))
    if strategy_id == "news_event" and version != 3:
        return False
    row = latest(path, strategy_id, version)
    if row is None:
        return False
    return row.get("spec_hash") == spec_hash(document) and row.get("mode_after") in RUNNABLE_MODES
