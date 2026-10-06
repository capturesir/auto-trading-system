"""R28 ADR premium pause. Paper entries only.

BUY ENTRY and BUY ADD on a controlled ADR are refused while that name is
paused. Exits, reduces, and protective stops are never refused by this rule.
There is no switch that turns the rule off. A bad map, a bad rule file, or
any exception is treated as missing state and fails closed for the controlled
set. The controlled set is never empty: a missing map uses the last database
set plus the built-in list, which includes TSM.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

BUILTIN_CONTROLLED = frozenset({"TSM"})
RULE_FIELDS = ("rule_id", "rule_version", "rule_hash")
REASON = "ADR_PREMIUM_PAUSED"


def controlled_symbols(
    map_symbols: Iterable[str] | None,
    last_db: Iterable[str] = (),
) -> frozenset[str]:
    """Symbols this rule watches. A missing or empty map cannot yield an empty set."""
    try:
        if map_symbols is not None:
            found = frozenset(symbol for symbol in map_symbols if symbol)
            if found:
                return found
        return frozenset(last_db) | BUILTIN_CONTROLLED
    except Exception:
        return BUILTIN_CONTROLLED


def rule_matches(rule: Mapping[str, Any] | None, expected: Mapping[str, Any] | None) -> bool:
    """Compare only ``rule_id``, ``rule_version``, and ``rule_hash``."""
    if expected is None:
        return True
    try:
        if not isinstance(rule, Mapping):
            return False
        return all(rule.get(field) == expected.get(field) for field in RULE_FIELDS)
    except Exception:
        return False


def r28_result(
    *,
    mode: str,
    side: str,
    intent_type: str,
    symbol: str,
    protective: bool = False,
    reduce_only: bool = False,
    state: object = None,
    map_symbols: Iterable[str] | None = None,
    last_db: Iterable[str] = (),
    rule: Mapping[str, Any] | None = None,
    expected_rule: Mapping[str, Any] | None = None,
) -> str | None:
    """Return ``ADR_PREMIUM_PAUSED`` or None. Never raises."""
    try:
        if protective or reduce_only:
            return None
        if side != "BUY" or intent_type not in {"ENTRY", "ADD"}:
            return None
        if mode != "paper":
            return None
        watched = controlled_symbols(map_symbols, last_db)
        if symbol not in watched:
            return None
        if not rule_matches(rule, expected_rule):
            return REASON
        if not isinstance(state, Mapping) or isinstance(state, Exception):
            return REASON
        paused = state.get("paused", ())
        if symbol in set(paused):
            return REASON
        return None
    except Exception:
        if protective or reduce_only or side != "BUY" or intent_type not in {"ENTRY", "ADD"}:
            return None
        if mode != "paper":
            return None
        return REASON
