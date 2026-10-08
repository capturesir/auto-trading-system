"""Market-strategy intent fields: trigger_ref and protective_stop_pct.

``trigger_ref`` is stored as the exact JSON text from the intent. A missing
universe archive skips the pool hash check and logs a warning.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_TRIGGER_BYTES = 4096
POOL_ID = re.compile(r"^ta_pool_[0-9]{6}$")
DAY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
TRIGGER_FIELDS = (
    "pool_id",
    "pool_hash",
    "pool_asof",
    "indicator",
    "period",
    "decision_close",
    "ref_level",
    "rank_value",
)
NULL_ON_MARKET = ("signal_id", "confidence", "report_path", "fair_value")


@dataclass
class IntentCheck:
    """Result of one intent. ``code`` is None when the intent may continue."""

    code: str | None
    trigger_ref_raw: str | None
    trigger_type: str | None
    protective_stop_pct: float | None
    rationale: str | None = None


def raw_field(text: str, field: str) -> str | None:
    """Exact JSON text of ``field`` inside ``text``, or None when it is absent."""
    match = re.search(rf'"{re.escape(field)}"\s*:\s*', text)
    if match is None:
        return None
    decoder = json.JSONDecoder()
    _value, end = decoder.raw_decode(text, match.end())
    return text[match.end() : end].strip()


def trigger_type_of(strategy: dict[str, Any]) -> str:
    trigger = strategy.get("trigger")
    if isinstance(trigger, dict) and isinstance(trigger.get("type"), str):
        return str(trigger["type"])
    return "signal"


def protective_stop_value(strategy: dict[str, Any]) -> float | None:
    """Strategy-file percent points, such as 15.0. None when the exit is absent."""
    for rule in strategy.get("exits") or []:
        if isinstance(rule, dict) and rule.get("type") == "protective_stop_pct":
            value = rule.get("value")
            if isinstance(value, bool) or not isinstance(value, int | float):
                return None
            return float(value)
    return None


def stop_loss_points(strategy: dict[str, Any]) -> float | None:
    """``stop_loss_pct.value`` in percent points. Defaults stay decimals elsewhere."""
    for rule in strategy.get("exits") or []:
        if isinstance(rule, dict) and rule.get("type") == "stop_loss_pct":
            value = rule.get("value")
            if isinstance(value, bool) or not isinstance(value, int | float):
                return None
            return float(value)
    return None


def fixed_rationale(trigger_ref: dict[str, Any]) -> str:
    """Template reason. The stored ``rationale`` is not rewritten."""
    close = trigger_ref["decision_close"]
    prior = trigger_ref["ref_level"]
    period = trigger_ref["period"]
    pool = trigger_ref["pool_id"]
    return f"收市 {close} 創 {period} 日新高（前高 {prior}）；股票池 {pool}"


def pool_status(home: Path, trigger_ref: dict[str, Any], ticker: str) -> tuple[str, bool | None]:
    """Return ``(detail, ok)``. A missing archive is ``skipped`` and not a failure."""
    universe = home / "data" / "universe"
    pool_id = str(trigger_ref["pool_id"])
    archive = universe / "archive" / f"{pool_id}.csv"
    if not universe.is_dir() or not archive.is_file():
        log.warning("pool archive missing; pool_hash check skipped for %s", pool_id)
        return "skipped", None
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    claimed = str(trigger_ref["pool_hash"])
    if claimed.startswith("sha256:"):
        claimed = claimed.split(":", 1)[1]
    if claimed.lower() != digest:
        return "hash_mismatch", False
    if not _ticker_in_csv(archive, ticker):
        return "ticker_missing", False
    return "matched", True


def validate_intent(
    text: str,
    strategy: dict[str, Any],
    *,
    now: datetime,
    home: Path | None = None,
    entry_stop_pct: float | None = None,
) -> IntentCheck:
    """ENTRY and ADD checks for ``trigger_ref`` and ``protective_stop_pct``."""
    payload = json.loads(text)
    intent_type = str(payload.get("intent_type") or "ENTRY")
    kind = trigger_type_of(strategy)
    raw = raw_field(text, "trigger_ref")
    stop_raw = payload.get("protective_stop_pct", None)
    stop = _as_number(stop_raw)
    if intent_type not in {"ENTRY", "ADD"}:
        return IntentCheck(None, raw, kind, stop, payload.get("rationale"))
    if raw is not None and len(raw.encode("utf-8")) > MAX_TRIGGER_BYTES:
        return IntentCheck("INTENT_INVALID_FIELD", raw, kind, stop)
    if kind == "market":
        code = _market_fields(payload, raw, now, home, payload.get("ticker"))
        if code:
            return IntentCheck(code, raw, kind, stop)
    else:
        if raw not in (None, "null"):
            return IntentCheck("INTENT_INVALID_FIELD", raw, kind, stop)
        if payload.get("signal_id") is None:
            return IntentCheck("SIGNAL_NOT_FOUND", raw, kind, stop)
    stop_code = _stop_field(strategy, stop_raw, intent_type, entry_stop_pct)
    if stop_code:
        return IntentCheck(stop_code, raw, kind, stop)
    inherited = stop
    if intent_type == "ADD" and stop is None:
        inherited = entry_stop_pct
    return IntentCheck(None, raw, kind, inherited, payload.get("rationale"))


def _market_fields(
    payload: dict[str, Any],
    raw: str | None,
    now: datetime,
    home: Path | None,
    ticker: object,
) -> str | None:
    for name in NULL_ON_MARKET:
        if payload.get(name) is not None:
            return "INTENT_INVALID_FIELD"
    if raw is None or raw == "null":
        return "TRIGGER_REF_MISSING"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return "TRIGGER_REF_INVALID"
    if not isinstance(parsed, dict) or not _trigger_shape(parsed):
        return "TRIGGER_REF_INVALID"
    decision = now.date()
    asof = date.fromisoformat(str(parsed["pool_asof"]))
    if asof < decision - timedelta(days=40):
        return "TRIGGER_REF_INVALID"
    if home is not None:
        _detail, ok = pool_status(home, parsed, str(ticker or ""))
        if ok is False:
            return "TRIGGER_REF_INVALID"
    return None


def _trigger_shape(payload: dict[str, Any]) -> bool:
    if any(name not in payload for name in TRIGGER_FIELDS):
        return False
    if not isinstance(payload["pool_id"], str) or POOL_ID.fullmatch(payload["pool_id"]) is None:
        return False
    if not isinstance(payload["pool_hash"], str) or not payload["pool_hash"]:
        return False
    if not isinstance(payload["pool_asof"], str) or DAY.fullmatch(payload["pool_asof"]) is None:
        return False
    if not isinstance(payload["indicator"], str) or not payload["indicator"]:
        return False
    if isinstance(payload["period"], bool) or not isinstance(payload["period"], int):
        return False
    return all(_is_number(payload[name]) for name in ("decision_close", "ref_level", "rank_value"))


def _stop_field(
    strategy: dict[str, Any],
    value: object,
    intent_type: str,
    entry_stop_pct: float | None,
) -> str | None:
    if value is None:
        return None
    number = _as_number(value)
    if number is None or not 0 < number < 100:
        return "INTENT_INVALID_FIELD"
    expected = protective_stop_value(strategy)
    if expected is None or abs(number - expected) > 1e-9:
        return "INTENT_INVALID_FIELD"
    if intent_type == "ADD" and entry_stop_pct is not None and abs(number - entry_stop_pct) > 1e-9:
        return "INTENT_INVALID_FIELD"
    return None


def _is_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float)


def _as_number(value: object) -> float | None:
    if not _is_number(value):
        return None
    return float(value)


def persist_intent(
    home: Path, text: str, strategy: dict[str, Any], *, now: datetime
) -> IntentCheck:
    """Validate, then store the raw ``trigger_ref`` in the journal and the event log."""
    check = validate_intent(text, strategy, now=now, home=home)
    if check.code:
        return check
    payload = json.loads(text)
    from auto_trading.portfolio.journal import TradeJournal

    TradeJournal(home).record_intent(
        payload,
        trigger_ref_raw=check.trigger_ref_raw,
        protective_stop_pct=check.protective_stop_pct,
        trigger_type=check.trigger_type,
    )
    _append_event(home, payload.get("intent_id"), check.trigger_ref_raw, now)
    return check


def _append_event(home: Path, intent_id: object, raw: str | None, now: datetime) -> None:
    path = home / "logs" / f"events_{now.strftime('%Y%m%d')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = raw if raw is not None else "null"
    line = (
        '{"event":"intent_received","intent_id":'
        + json.dumps(intent_id)
        + ',"trigger_ref":'
        + body
        + "}\n"
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _ticker_in_csv(path: Path, ticker: str) -> bool:
    import csv

    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames and "ticker" in reader.fieldnames:
            return any(row.get("ticker") == ticker for row in reader)
        handle.seek(0)
        for row in csv.reader(handle):
            if ticker in row:
                return True
    return False
