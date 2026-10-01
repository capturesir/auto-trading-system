"""Evaluate every confirm and exit the shipped drafts reference.

An unknown indicator or exit type raises :class:`UnknownCondition`. Missing
prices make the condition false; they do not skip the rule.
"""

from __future__ import annotations

from typing import Any

from auto_trading.strategy.vocabulary import (
    Series,
    close_above_bb_upper,
    close_above_ma,
    close_above_ref,
    close_below_bb_lower,
    close_below_ref,
    consecutive_below_ma,
    event_gap_up,
    event_volume_breakout,
    exit_priority,
    fair_value_reached,
    max_chase,
    no_new_low,
    not_limit_up,
    parse_hold,
    pullback_to_ma,
    rank_drop,
    rsi_above,
    rsi_below,
    signal_missing,
    signal_reversal,
    slope_up,
    stop_atr,
    stop_loss_pct,
    time_exit,
    trailing_atr,
)


class UnknownCondition(Exception):
    """A strategy named a condition the interpreter does not implement."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"unknown condition {name!r}")


_KNOWN = frozenset(
    {
        "close_above_ma",
        "ma_slope_up",
        "rsi_below",
        "rsi_above",
        "close_below_bb_lower",
        "close_above_bb_upper",
        "pullback_to_ma",
        "no_new_low",
        "event_gap_up",
        "event_volume_breakout",
        "close_above_ref",
        "max_chase",
        "not_limit_up",
    }
)


def _indicator(rule: dict[str, Any], series: Series, refs: dict[str, Any]) -> bool:
    name = rule.get("indicator")
    if name not in _KNOWN:
        raise UnknownCondition(str(name))
    if name != "not_limit_up" and not series.closes:
        return False
    if name == "close_above_ma":
        return close_above_ma(series, int(rule["period"]))
    if name == "ma_slope_up":
        return slope_up(series, int(rule["period"]), int(rule["lookback"]))
    if name == "rsi_below":
        return rsi_below(series, int(rule["period"]), float(rule["value"]))
    if name == "rsi_above":
        return rsi_above(series, int(rule["period"]), float(rule["value"]))
    if name == "close_below_bb_lower":
        return close_below_bb_lower(series, int(rule["period"]), float(rule["std"]))
    if name == "close_above_bb_upper":
        return close_above_bb_upper(series, int(rule["period"]), float(rule["std"]))
    if name == "pullback_to_ma":
        rising = rule.get("require_ma_rising")
        period = int(rising) if rising is not None else None
        return pullback_to_ma(series, int(rule["period"]), float(rule["within_pct"]), period)
    if name == "no_new_low":
        lookback = int(rule.get("lookback_high_low") or rule.get("lookback"))
        return no_new_low(series.lows, lookback, int(rule["within_sessions"]))
    if name == "event_gap_up":
        if not series.opens or refs.get("pre_event_close") is None:
            return False
        return event_gap_up(
            series.opens[-1],
            series.closes[-1],
            float(refs["pre_event_close"]),
            float(rule["min_gap_pct"]),
            float(rule["hold_above_pre_close_pct"]),
        )
    if name == "event_volume_breakout":
        if not series.closes:
            return False
        return event_volume_breakout(
            series.volumes,
            series.highs,
            series.closes[-1],
            int(rule["avg_period"]),
            float(rule["volume_multiple"]),
            int(rule["breakout_lookback"]),
        )
    if name == "close_above_ref":
        ref = _ref_price(rule.get("ref"), refs)
        if ref is None or not series.closes:
            return False
        return close_above_ref(series.closes[-1], ref, float(rule.get("min_pct", 0)))
    if name == "max_chase":
        ref = _ref_price(rule.get("ref"), refs)
        if ref is None or not series.closes:
            return False
        return max_chase(series.closes[-1], ref, float(rule["max_pct"]))
    if name == "not_limit_up":
        return not_limit_up(str(refs.get("market") or ""), bool(refs.get("limit_up")))
    raise UnknownCondition(str(name))


def _ref_price(name: str | None, refs: dict[str, Any]) -> float | None:
    value = refs.get("pre_event_close") if name in {None, "pre_event_close"} else refs.get(name)
    if value is None:
        return None
    return float(value)


def _rules_ok(
    rules: list[dict[str, Any]] | None, series: Series, refs: dict[str, Any], *, how: str
) -> bool:
    if not rules:
        return True
    flags = [_indicator(rule, series, refs) for rule in rules]
    return all(flags) if how == "all" else any(flags)


def confirms(strategy: dict[str, Any], series: Series, refs: dict[str, Any] | None = None) -> bool:
    """True when confirm and guards pass. No confirm block means no extra filter."""
    block = strategy.get("confirm") or {}
    if not block:
        return True
    info = dict(refs or {})
    source = info.get("event_source")
    if "by_source" in block:
        chosen = (block.get("by_source") or {}).get(source or "")
        if not isinstance(chosen, dict):
            return False
        group_ok = _group(chosen, series, info)
    else:
        group_ok = _group(block, series, info)
    guards = block.get("guards") or []
    return group_ok and _rules_ok(guards, series, info, how="all")


def _group(block: dict[str, Any], series: Series, refs: dict[str, Any]) -> bool:
    if block.get("all_of") and not _rules_ok(block["all_of"], series, refs, how="all"):
        return False
    return not (block.get("any_of") and not _rules_ok(block["any_of"], series, refs, how="any"))


def _ma_break(rule: dict[str, Any], series: Series, refs: dict[str, Any]) -> bool:
    active = rule.get("active_after")
    if active == "indicator_take_profit" and not refs.get("partial_taken"):
        return False
    sessions = int(rule.get("consecutive_closes", 1))
    period = int(rule["ma_period"])
    return consecutive_below_ma(series.closes, period, sessions)


def exit_hits(
    strategy: dict[str, Any], series: Series, refs: dict[str, Any]
) -> list[tuple[str, float]]:
    """Each hit is ``(exit type, fraction of the position to sell)``."""
    hits: list[tuple[str, float]] = []
    for rule in strategy.get("exits") or []:
        kind = rule.get("type")
        if kind == "stop_loss_pct":
            if stop_loss_pct(float(refs["close"]), float(refs["avg_cost"]), float(rule["value"])):
                hits.append((kind, 1.0))
        elif kind == "stop_atr":
            atr = refs.get("atr_at_entry")
            if atr is not None and stop_atr(
                float(refs["close"]), float(refs["entry"]), float(atr), float(rule["multiple"])
            ):
                hits.append((kind, 1.0))
        elif kind == "trailing_atr":
            atr = refs.get("atr_now")
            if atr is not None and trailing_atr(
                float(refs["close"]), float(refs["highest"]), float(atr), float(rule["multiple"])
            ):
                hits.append((kind, 1.0))
        elif kind == "fair_value_reached":
            fair = refs.get("fair_value")
            if fair is not None and fair_value_reached(
                float(refs["raw_close"]), float(fair), float(rule["ratio"])
            ):
                hits.append((kind, 1.0))
        elif kind in {"trend_break", "ma_break"}:
            if _ma_break(rule, series, refs):
                hits.append((kind, 1.0))
        elif kind == "indicator_take_profit":
            if _rules_ok(rule.get("any_of"), series, refs, how="any"):
                hits.append((kind, float(rule.get("fraction", 1.0))))
        elif kind == "signal_reversal":
            if signal_reversal(
                refs.get("side"),
                list(rule.get("on_side") or []),
                refs.get("upside"),
                rule.get("upside_live_below_pct"),
            ):
                hits.append((kind, 1.0))
        elif kind == "signal_missing":
            if signal_missing(int(refs.get("sessions_without") or 0), int(rule["sessions"])):
                hits.append((kind, 1.0))
        elif kind == "rank_drop":
            rank = refs.get("rank")
            if rank is not None and rank_drop(int(rank), int(rule["outside_top"])):
                hits.append((kind, 1.0))
        elif kind == "time":
            pnl = refs.get("pnl_pct")
            below = rule.get("only_if_pnl_below_pct")
            if time_exit(int(refs["hold_sessions"]), parse_hold(rule["max_hold"]), pnl, below):
                hits.append((kind, 1.0))
        elif kind == "close_below_ref":
            ref = _ref_price(rule.get("ref"), refs)
            if ref is not None and close_below_ref(
                float(refs["raw_close"]), ref, float(rule.get("buffer_pct", 0))
            ):
                hits.append((kind, 1.0))
        elif kind == "event_reversal":
            if refs.get("event_reversed"):
                hits.append((kind, 1.0))
        else:
            raise UnknownCondition(str(kind))
    return hits


def choose_exit(hits: list[tuple[str, float]]) -> tuple[str, float] | None:
    """Stop family, then signal reversal, then fair value, then the rest."""
    if not hits:
        return None
    reason = exit_priority([name for name, _fraction in hits])
    for name, fraction in hits:
        if name == reason:
            return name, fraction
    return hits[0]
