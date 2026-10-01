"""Append-only strategy registry.

``approved`` accepts only ``actor=user``. The lifecycle ends at paper.
``news_event`` v2 and v3 are archived: only v4 can be registered to run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from auto_trading.alerts.pending import build_alert, write_pending
from auto_trading.jsonutil import loads_strict
from auto_trading.strategy.spec import spec_hash

USER_ONLY = frozenset({"approved"})
ALLOWED_EVENTS = frozenset(
    {
        "drafted",
        "approved",
        "paper_started",
        "paper_evaluated",
        "demoted",
        "disabled",
        "retired",
        "rejected",
    }
)
DELETED_EVENTS = frozenset(
    {"live_promoted", "size_changed", "gate_passed"}  # refused: paper_only
)
RUNNABLE_MODES = frozenset({"paper"})
MODES = frozenset({"paper", "disabled"})
MACAU = ZoneInfo("Asia/Macau")


class RegistryError(Exception):
    """A registry write violated an actor, version, or paper-only rule."""


def _paper_only(path: Path, message: str) -> None:
    home = path.parent.parent if path.parent.name == "strategies" else path.parent
    stamp = datetime.now(MACAU).strftime("%Y%m%dT%H%M%S")
    alert = build_alert(
        rule_id="paper_only",
        alert_type="paper_only_violation",
        severity="critical",
        ticker="ACCOUNT",
        message_zh="登記表拒絕真實帳戶模式。策略只留在模擬盤。",
        trading_date=datetime.now(MACAU).date().isoformat(),
        created_at=datetime.now(MACAU),
    )
    write_pending(home, alert, filename_stamp=stamp)
    raise RegistryError(message)


def append_event(path: Path, event: Mapping[str, Any]) -> None:
    """Append one JSON object. The file is never rewritten."""
    if "live_size_multiplier" in event:  # rejected: paper_only_violation, must be absent
        _paper_only(path, "multiplier field is rejected: paper_only_violation")
    name = event.get("event")
    if name in DELETED_EVENTS:
        _paper_only(path, f"{name} is rejected: paper_only_violation")
    if name not in ALLOWED_EVENTS:
        raise RegistryError(f"unknown event {name}")
    mode_after = event.get("mode_after")
    if mode_after not in MODES:
        _paper_only(path, "mode_after must be paper or disabled: paper_only_violation")
    actor = event.get("actor")
    if name in USER_ONLY and actor != "user":
        raise RegistryError(f"{name} requires actor=user, got {actor}")
    strategy_id = event.get("strategy_id")
    version = event.get("version")
    if strategy_id == "news_event" and version == 2:
        raise RegistryError("news_event v2 is archived and cannot be registered")
    if (
        strategy_id == "news_event"
        and version != 4
        and name
        in {
            "approved",
            "paper_started",
            "paper_evaluated",
        }
    ):
        raise RegistryError("only news_event v4 can be registered to run")
    if name == "paper_started" and _other_version_running(path, strategy_id, version):
        raise RegistryError("one running version per strategy id")
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(event)
    row.setdefault("ts", datetime.now(MACAU).isoformat(timespec="seconds"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def r23_record(strategy_id: str, version: int, digest: str, streak: int) -> dict[str, Any]:
    """R23 only records. The running mode stays paper."""
    return {
        "strategy_id": strategy_id,
        "version": version,
        "spec_hash": digest,
        "event": "demoted",
        "actor": "system",
        "mode_after": "paper",
        "note": f"r23_demote_count={streak}",
    }


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


def _other_version_running(path: Path, strategy_id: object, version: object) -> bool:
    latest_by_version: dict[int, dict[str, Any]] = {}
    for row in read_events(path):
        if row.get("strategy_id") != strategy_id:
            continue
        latest_by_version[int(row["version"])] = row
    return any(
        ver != version and row.get("mode_after") == "paper"
        for ver, row in latest_by_version.items()
    )


def is_runnable(path: Path, document: Mapping[str, Any]) -> bool:
    """True when the latest row matches the hash and ``mode_after`` is paper."""
    if document.get("mode") != "paper":
        return False
    strategy_id = str(document.get("id"))
    version = int(document.get("version", 0))
    if strategy_id == "news_event" and version != 4:
        return False
    row = latest(path, strategy_id, version)
    if row is None:
        return False
    return row.get("spec_hash") == spec_hash(document) and row.get("mode_after") in RUNNABLE_MODES
