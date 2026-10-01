"""Degenerate ``a_signal_change`` events. ``initial`` is not a fair-value upgrade."""

from __future__ import annotations

ACCEPTED_REASONS = frozenset({"news_event", "earnings", "data_update"})
IGNORED_REASONS = frozenset({"method_change", "price_only"})


def classify_signal_change(
    *,
    baseline: bool,
    had_previous: bool,
    previous_side: str | None,
    side: str,
    fv_change_pct: float | None,
    reason_types: set[str],
    bulk_method_change: bool = False,
) -> str | None:
    """Return an event name, or None when the day is not an event.

    ``baseline`` is false on the first archive day, which produces nothing.
    ``initial`` only supports ``new_buy``; it is not a fair-value upgrade.
    """
    if not baseline:
        return None
    if not reason_types:
        if bulk_method_change:
            return None
        return _by_sides(had_previous, previous_side, side, fv_change_pct, require_reason=False)
    if "initial" in reason_types:
        if not had_previous and side == "BUY":
            return "new_buy"
        return None
    if reason_types <= IGNORED_REASONS:
        return None
    if not (reason_types & ACCEPTED_REASONS):
        return None
    return _by_sides(had_previous, previous_side, side, fv_change_pct, require_reason=True)


def _by_sides(
    had_previous: bool,
    previous_side: str | None,
    side: str,
    fv_change_pct: float | None,
    *,
    require_reason: bool,
) -> str | None:
    change = fv_change_pct or 0.0
    if side != "BUY":
        return None
    if not had_previous:
        return "new_buy"
    if previous_side in {"HOLD", "SELL"} and change >= 1:
        return "upgrade_to_buy"
    if change >= 5:
        return "fv_revision_up"
    if not require_reason and previous_side in {"HOLD", "SELL"} and change >= 1:
        return "upgrade_to_buy"
    return None
