"""Strict ADR premium state checks (strategy spec v0.8 §15.7).

The execution gate, the allocator, and the daily writer share this module.
Anything illegal, any calendar error, and any exception is reported as
``Invalid``. Callers treat that as missing. This module does not raise past
``Invalid`` and does not allow the order through.
"""

from __future__ import annotations

import csv
import json
import math
import re
from datetime import datetime
from typing import Any

import exchange_calendars as xcals
import pandas as pd

SCHEMA = "adr_premium_state/1.0"
BUILTIN_CONTROLLED = frozenset({"TSM"})
CAL_START, CAL_END = "2005-01-03", "2028-12-29"
REQ = (
    "local_ticker",
    "ratio",
    "pair_date",
    "valid_for_us_session",
    "premium_pct",
    "premium_5d_pct",
    "percentile_250d",
    "zscore_250d",
    "state",
    "state_since",
    "reason",
    "consecutive_uncomputable",
    "carried_forward_sessions",
)
STATES = ("active", "paused", "unknown")
REASON_PREFIX = (
    "percentile_250d",
    "tw_holiday_carry",
    "tw_holiday_extended",
    "data_missing:",
    "insufficient_history",
    "error:",
)
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
CARRY_MAX = 3
MAP_COLS = (
    "adr",
    "local_ticker",
    "ratio",
    "effective_from",
    "effective_to",
    "adr_calendar",
    "local_calendar",
)


class Invalid(Exception):
    """Illegal state. ``args[0]`` is the missing cause."""


_CALS: dict[str, Any] = {}


def get_cal(code: str) -> Any:
    if code not in ("XNYS", "XTAI"):
        raise Invalid(f"calendar_unknown:{code}")
    if code not in _CALS:
        _CALS[code] = xcals.get_calendar(code, start=CAL_START, end=CAL_END)
    return _CALS[code]


def is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_num(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_date(value: object) -> bool:
    if not (isinstance(value, str) and DATE.match(value)):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def load_map(path: str) -> tuple[list[dict[str, str]], str | None]:
    """Return ``(rows, err)``. A bad map yields ``[]`` and a cause.

    The controlled set still includes the built-in names.
    """
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if not rows:
            return [], "map_empty"
        if any(column not in rows[0] for column in MAP_COLS):
            return [], "map_columns"
        return rows, None
    except Exception as exc:
        return [], f"map_unreadable:{type(exc).__name__}"


def controlled_set(rows: list[dict[str, str]], state_obj: object = None) -> set[str]:
    symbols = set(BUILTIN_CONTROLLED) | {row["adr"] for row in rows if row.get("adr")}
    if isinstance(state_obj, dict) and isinstance(state_obj.get("adrs"), dict):
        symbols |= set(state_obj["adrs"])
    return symbols


def map_row_at(rows: list[dict[str, str]], adr: str, day: pd.Timestamp) -> dict[str, Any]:
    """The single map row in force on ``day``. Overlap or a bad cell is invalid."""
    hit: list[dict[str, str]] = []
    for row in rows:
        if row.get("adr") != adr:
            continue
        try:
            start = pd.Timestamp(datetime.strptime(row["effective_from"], "%Y-%m-%d"))
            raw_end = (row.get("effective_to") or "").strip()
            end = pd.Timestamp(datetime.strptime(raw_end, "%Y-%m-%d")) if raw_end else None
        except Exception as exc:
            raise Invalid("map_row:effective_date") from exc
        if start <= day and (end is None or day <= end):
            hit.append(row)
    if not hit:
        raise Invalid("map_row:none")
    if len(hit) > 1:
        raise Invalid("map_row:overlap")
    chosen = hit[0]
    try:
        ratio = float(chosen["ratio"])
    except Exception as exc:
        raise Invalid("map_row:ratio") from exc
    if not (math.isfinite(ratio) and ratio > 0):
        raise Invalid("map_row:ratio")
    if not chosen.get("local_ticker"):
        raise Invalid("map_row:local_ticker")
    if chosen.get("adr_calendar") != "XNYS":
        raise Invalid("map_row:adr_calendar")
    get_cal(str(chosen.get("local_calendar")))
    return dict(chosen, ratio_f=ratio)


def session_S(now: object) -> pd.Timestamp:
    calendar = get_cal("XNYS")
    try:
        now_utc = pd.Timestamp(now).tz_convert("UTC")
        ny = now_utc.tz_convert("America/New_York").normalize().tz_localize(None)
        if calendar.is_session(ny) and now_utc < calendar.session_close(ny):
            return ny
        if calendar.is_session(ny):
            return calendar.next_session(ny)
        return calendar.date_to_session(ny, direction="next")
    except Exception as exc:
        raise Invalid(f"calendar:{type(exc).__name__}") from exc


def expected_pair_date(session: pd.Timestamp, adr_cal: Any, local_cal: Any) -> pd.Timestamp:
    """Latest date on or before the previous ADR session that both calendars open."""
    try:
        left = adr_cal.previous_session(session)
        while not local_cal.is_session(left):
            left = adr_cal.previous_session(left)
        right = local_cal.date_to_session(adr_cal.previous_session(session), direction="previous")
        while not adr_cal.is_session(right):
            right = local_cal.previous_session(right)
    except Exception as exc:
        raise Invalid(f"calendar:{type(exc).__name__}") from exc
    if left != right:
        raise Invalid("calendar_mismatch")
    return left


def validate_top(state: object, expected: object) -> None:
    if not isinstance(state, dict):
        raise Invalid("parse")
    if state.get("schema_version") != SCHEMA:
        raise Invalid("schema")
    generated = state.get("generated_at")
    try:
        if not isinstance(generated, str) or datetime.fromisoformat(generated).tzinfo is None:
            raise ValueError
    except ValueError as exc:
        raise Invalid("generated_at_tz") from exc
    if not isinstance(expected, dict):
        raise Invalid("expected_file")
    rule_ok = (
        isinstance(expected.get("rule_id"), str)
        and is_int(expected.get("rule_version"))
        and isinstance(expected.get("rule_hash"), str)
        and HASH.match(expected["rule_hash"])
    )
    if not rule_ok:
        raise Invalid("expected_file")
    state_ok = (
        isinstance(state.get("rule_id"), str)
        and is_int(state.get("rule_version"))
        and isinstance(state.get("rule_hash"), str)
    )
    if not state_ok:
        raise Invalid("field_invalid:rule")
    same = (state["rule_id"], state["rule_version"], state["rule_hash"]) == (
        expected["rule_id"],
        expected["rule_version"],
        expected["rule_hash"],
    )
    if not same:
        raise Invalid("rule_mismatch")
    if not isinstance(state.get("adrs"), dict):
        raise Invalid("field_invalid:adrs")


def validate_entry(entry: object) -> None:
    """Type and range checks for one ADR. Illegal fields raise ``Invalid``."""
    if not isinstance(entry, dict):
        raise Invalid("adr_absent")
    for key in REQ:
        if key not in entry:
            raise Invalid(f"field_invalid:{key}")

    def bad(key: str) -> Invalid:
        return Invalid(f"field_invalid:{key}")

    if not (isinstance(entry["local_ticker"], str) and entry["local_ticker"]):
        raise bad("local_ticker")
    if not (is_num(entry["ratio"]) and entry["ratio"] > 0):
        raise bad("ratio")
    for key in ("pair_date", "valid_for_us_session", "state_since"):
        if not is_date(entry[key]):
            raise bad(key)
    if entry["state"] not in STATES:
        raise bad("state")
    for key in ("premium_pct", "premium_5d_pct", "percentile_250d", "zscore_250d"):
        if entry[key] is not None and not is_num(entry[key]):
            raise bad(key)
    if entry["state"] != "unknown":
        for key in ("premium_pct", "percentile_250d"):
            if entry[key] is None:
                raise bad(key)
    reason = entry["reason"]
    if not (isinstance(reason, str) and reason.startswith(REASON_PREFIX)):
        raise bad("reason")
    count = entry["consecutive_uncomputable"]
    if not (is_int(count) and count >= 0):
        raise bad("consecutive_uncomputable")
    carried = entry["carried_forward_sessions"]
    if not (is_int(carried) and 0 <= carried <= CARRY_MAX):
        raise bad("carried_forward_sessions")


def check_entry_against(
    entry: dict[str, Any],
    adr: str,
    session: pd.Timestamp,
    rows: list[dict[str, str]],
) -> str:
    """Compare one ADR with the map row for ``session``. ``ok`` or ``stale``."""
    row = map_row_at(rows, adr, session)
    if row["local_ticker"] != entry["local_ticker"] or row["ratio_f"] != float(entry["ratio"]):
        raise Invalid("map_mismatch")
    adr_cal = get_cal("XNYS")
    local_cal = get_cal(row["local_calendar"])
    pair = expected_pair_date(session, adr_cal, local_cal)
    valid = pd.Timestamp(entry["valid_for_us_session"])
    if valid != session or pd.Timestamp(entry["pair_date"]) != pair:
        return "stale"
    return "ok"


def validate_before_write(state: object, adr_map_path: str, expected_rule_path: str) -> str | None:
    """Return ``None`` when a daily writer may publish ``state``, else a cause."""
    rows, err = load_map(adr_map_path)
    if err:
        return err
    try:
        with open(expected_rule_path, encoding="utf-8") as handle:
            expected = json.load(handle)
        validate_top(state, expected)
        if not isinstance(state, dict):
            return "parse"
        for ticker, entry in state["adrs"].items():
            validate_entry(entry)
            session = pd.Timestamp(entry["valid_for_us_session"])
            if check_entry_against(entry, ticker, session, rows) == "stale":
                return "stale"
    except Invalid as exc:
        return exc.args[0]
    except Exception as exc:
        return f"exception:{type(exc).__name__}"
    return None
