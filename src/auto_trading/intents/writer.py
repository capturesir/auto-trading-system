"""Intent files, schema intent/1.0.

ENTRY leaves ``parent_intent_id`` null. ADD, REDUCE, and EXIT point at the
original ENTRY, not at the previous add or reduce. The file must have
``mode`` paper and ``live_size_multiplier`` null (paper_only). A risk-config
failure keeps only REDUCE and EXIT.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from auto_trading.alerts.pending import build_alert, write_pending
from auto_trading.jsonutil import loads_strict
from auto_trading.strategy.spec import INTERPRETER_VERSION

OPENING = frozenset({"ENTRY", "ADD"})
REDUCING = frozenset({"REDUCE", "EXIT"})
MACAU = ZoneInfo("Asia/Macau")


class PaperOnlyViolation(ValueError):
    """An intent file tried to leave paper mode. Nothing was written."""


def filter_for_risk(
    intents: list[dict[str, Any]], *, entries_allowed: bool
) -> list[dict[str, Any]]:
    """Drop ENTRY and ADD when the risk snapshot cannot be trusted."""
    if entries_allowed:
        return intents
    return [item for item in intents if item.get("intent_type") in REDUCING]


def assert_parent_links(intents: list[Mapping[str, Any]]) -> None:
    """ENTRY is parentless. Later intents in a chain share that ENTRY id."""
    for item in intents:
        kind = item.get("intent_type")
        parent = item.get("parent_intent_id")
        if kind == "ENTRY" and parent is not None:
            raise ValueError("ENTRY parent_intent_id must be null")
        if kind in {"ADD", "REDUCE", "EXIT"} and not parent:
            raise ValueError(f"{kind} requires parent_intent_id")


def intent_path(trading_home: Path, session: str, strategy_id: str, version: int) -> Path:
    return trading_home / "intents" / session / f"{strategy_id}.v{version}.json"


def position_followups(entry_id: str, reduce_qty: int, exit_qty: int) -> list[dict[str, Any]]:
    """REDUCE and the later EXIT both point at the original ENTRY."""
    return [
        {"intent_type": "REDUCE", "parent_intent_id": entry_id, "qty": reduce_qty},
        {"intent_type": "EXIT", "parent_intent_id": entry_id, "qty": exit_qty},
    ]


def _reject_unless_paper(trading_home: Path, payload: Mapping[str, Any], session_date: str) -> None:
    """Self-check before the intent file is written. Null is required."""
    mode = payload.get("mode")
    multiplier = payload.get("live_size_multiplier", None)
    if mode == "paper" and multiplier is None:
        return
    created = datetime.now(MACAU)
    alert = build_alert(
        rule_id="paper_only",
        alert_type="paper_only_violation",
        severity="critical",
        ticker="ACCOUNT",
        message_zh="意圖檔必須是模擬模式，且 live_size_multiplier 必須是 null。",
        trading_date=session_date,
        created_at=created,
    )
    write_pending(trading_home, alert, filename_stamp=session_date.replace("-", ""))
    raise PaperOnlyViolation("mode must be paper and live_size_multiplier must be null")


def write_intents(
    trading_home: Path,
    document: dict[str, Any],
    *,
    session_date: str,
    entries_allowed: bool,
) -> Path:
    """Atomically write one intent file, incrementing ``revision`` on rewrite."""
    assert_parent_links(document["intents"])
    kept = filter_for_risk(list(document["intents"]), entries_allowed=entries_allowed)
    payload = {key: value for key, value in document.items() if key != "session_date"}
    payload["intents"] = kept
    payload.setdefault("schema_version", "intent/1.0")
    payload.setdefault("interpreter_version", INTERPRETER_VERSION)
    payload.setdefault("mode", "paper")
    payload.setdefault("live_size_multiplier", None)
    _reject_unless_paper(trading_home, payload, session_date)
    path = intent_path(
        trading_home,
        session_date,
        payload["strategy_id"],
        int(payload["strategy_version"]),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    revision = 1
    if path.is_file():
        previous = loads_strict(path.read_text(encoding="utf-8"))
        revision = int(previous.get("revision", 0)) + 1
    payload["revision"] = revision
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path
