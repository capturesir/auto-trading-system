"""Strategy document checks that do not depend on market data.

Every ``min_confidence`` must be at least 60. Unknown exit types, including
the reserved name ``reversal``, fail validation. ``strategy/1.1`` may use
the event exits; ``strategy/1.0`` may not.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CONFIRM = frozenset(
    {
        "close_above_ma",
        "ma_slope_up",
        "rsi_below",
        "rsi_above",
        "close_below_bb_lower",
        "close_above_bb_upper",
        "pullback_to_ma",
        "event_gap_up",
        "event_volume_breakout",
        "close_above_ref",
        "close_above_high",
    }
)
GUARDS = frozenset({"no_new_low", "max_chase", "not_limit_up"})
EXITS_10 = frozenset(
    {
        "stop_loss_pct",
        "take_profit_pct",
        "trailing_pct",
        "trailing_atr",
        "stop_atr",
        "fair_value_reached",
        "trend_break",
        "ma_break",
        "indicator_take_profit",
        "signal_reversal",
        "signal_missing",
        "rank_drop",
        "time",
    }
)
EXITS_11 = EXITS_10 | {"close_below_ref", "event_reversal"}
EXITS_12 = EXITS_11 | {"close_below_low", "protective_stop_pct"}
SCHEMAS = {"strategy/1.0", "strategy/1.1", "strategy/1.2"}
MIN_CONFIDENCE = 60


class StrategyValidationError(Exception):
    """One or more strategy fields failed validation."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def _walk_confidence(node: Any, errors: list[str], path: str) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            below = isinstance(value, int | float) and value < MIN_CONFIDENCE
            if key == "min_confidence" and below:
                errors.append(f"{child} {value} is below {MIN_CONFIDENCE}")
            _walk_confidence(value, errors, child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk_confidence(value, errors, f"{path}[{index}]")


def _indicators(node: Any, found: list[tuple[str, str]]) -> None:
    if isinstance(node, dict):
        indicator = node.get("indicator")
        if isinstance(indicator, str):
            found.append((indicator, "indicator"))
        for value in node.values():
            _indicators(value, found)
    elif isinstance(node, list):
        for value in node:
            _indicators(value, found)


def validate_strategy(document: Mapping[str, Any]) -> None:
    """Raise :class:`StrategyValidationError` when the document is not runnable."""
    errors: list[str] = []
    schema = document.get("schema_version")
    if schema not in SCHEMAS:
        errors.append(f"schema_version {schema!r} is not a known strategy schema")
    if schema == "strategy/1.2":
        allowed_exits = EXITS_12
    elif schema == "strategy/1.1":
        allowed_exits = EXITS_11
    else:
        allowed_exits = EXITS_10
    _walk_confidence(document, errors, "")
    exit_types: list[str] = []
    for index, exit_rule in enumerate(document.get("exits", [])):
        exit_type = exit_rule.get("type") if isinstance(exit_rule, dict) else None
        if isinstance(exit_type, str):
            exit_types.append(exit_type)
        if exit_type == "reversal" or exit_type not in allowed_exits:
            errors.append(f"exits[{index}].type {exit_type!r} is not a known exit")
    if "protective_stop_pct" in exit_types and "stop_loss_pct" in exit_types:
        errors.append("protective_stop_pct cannot be combined with stop_loss_pct")
    indicators: list[tuple[str, str]] = []
    _indicators(document.get("confirm"), indicators)
    for rule in document.get("exits", []):
        if isinstance(rule, dict):
            _indicators(rule.get("any_of"), indicators)
    known = CONFIRM | GUARDS
    if schema != "strategy/1.2":
        known = known - {"close_above_high"}
    for name, _kind in indicators:
        if name not in known:
            errors.append(f"indicator {name!r} is not in the vocabulary")
    if document.get("action", {}).get("side") not in {None, "buy"}:
        errors.append("action.side must be buy")
    mode = document.get("mode")
    if mode is not None and mode not in {"paper", "disabled"}:
        errors.append("mode must be paper or disabled")
    if errors:
        raise StrategyValidationError(errors)
