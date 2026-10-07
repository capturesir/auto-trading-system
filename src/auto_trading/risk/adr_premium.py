"""R28 ADR premium pause. Paper buys of a controlled ADR only.

The decision order is the reference guard: missing, then stale, then paused,
then unknown. The first hit wins. There is no switch and no shadow mode.
Sells are not checked. A bad map still watches the built-in names, including
TSM. Any exception is missing and is not raised.
"""

from __future__ import annotations

import json

from auto_trading.risk import adr_state as state

PAUSED = "ADR_PREMIUM_PAUSED"


def check_buy(
    ticker: str,
    side: str,
    now: object,
    state_path: str,
    adr_map_path: str,
    expected_rule_path: str,
) -> tuple[bool, str | None, str | None, object, str | None]:
    """Return ``(allowed, reason_code, detail, session, missing_cause)``."""
    if side != "BUY":
        return True, None, None, None, None
    rows, map_err = state.load_map(adr_map_path)
    loaded: object = None
    try:
        with open(state_path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except Exception:
        loaded = None
    if ticker not in state.controlled_set(rows, loaded):
        return True, None, None, None, None
    session: object = None

    def miss(cause: str) -> tuple[bool, str, str, object, str]:
        return False, PAUSED, "missing", session, cause

    try:
        session = state.session_S(now)
        if map_err:
            return miss(map_err)
        if loaded is None:
            return miss("file_absent_or_parse_error")
        try:
            with open(expected_rule_path, encoding="utf-8") as handle:
                expected = json.load(handle)
        except Exception:
            return miss("expected_file")
        state.validate_top(loaded, expected)
        if not isinstance(loaded, dict) or ticker not in loaded["adrs"]:
            return miss("adr_absent")
        entry = loaded["adrs"][ticker]
        state.validate_entry(entry)
        if state.check_entry_against(entry, ticker, session, rows) == "stale":
            return False, PAUSED, "stale", session, None
        if entry["state"] == "paused":
            return False, PAUSED, "paused", session, None
        if entry["state"] == "unknown":
            return False, PAUSED, "unknown", session, None
        return True, None, None, session, None
    except state.Invalid as exc:
        return miss(exc.args[0])
    except Exception as exc:
        return miss(f"exception:{type(exc).__name__}")
