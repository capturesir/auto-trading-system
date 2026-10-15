"""Long-only direction. BUY opens. SELL exits a position or alerts. HOLD does neither."""

from __future__ import annotations


def direction_action(side: str, *, has_position: bool) -> str:
    """Return ``entry``, ``exit``, ``signal_sell_no_position``, or ``none``."""
    if side == "BUY":
        return "none" if has_position else "entry"
    if side == "SELL":
        return "exit" if has_position else "signal_sell_no_position"
    if side == "HOLD":
        return "none"
    raise ValueError(f"unknown side: {side}")
