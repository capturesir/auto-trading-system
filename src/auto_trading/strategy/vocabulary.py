"""Strategy vocabulary (spec §3.3 and §13.6).

Each predicate is true when the condition that would fire is met. ``reversal``
is not implemented; validation rejects it. Indicators read adjusted bars.
Fair-value comparisons use the raw close the caller passes in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from auto_trading.market.indicators import (
    atr_wilder,
    bollinger,
    ma_slope_up,
    rsi_wilder,
    sma,
)

STOP_EXITS = frozenset({"stop_loss_pct", "stop_atr", "trailing_pct", "trailing_atr"})


@dataclass
class Series:
    """Bars visible at the decision, oldest first."""

    closes: list[float]
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    volumes: list[float] = field(default_factory=list)
    opens: list[float] = field(default_factory=list)
    raw_close: float | None = None


def close_above_ma(series: Series, period: int) -> bool:
    averages = sma(series.closes, period)
    latest = averages[-1] if averages else None
    return latest is not None and series.closes[-1] > latest


def close_below_ma(series: Series, period: int) -> bool:
    averages = sma(series.closes, period)
    latest = averages[-1] if averages else None
    return latest is not None and series.closes[-1] < latest


def slope_up(series: Series, period: int, lookback: int) -> bool:
    return ma_slope_up(series.closes, period, lookback)


def rsi_below(series: Series, period: int, value: float) -> bool:
    reading = rsi_wilder(series.closes, period)[-1]
    return reading is not None and reading <= value


def rsi_above(series: Series, period: int, value: float) -> bool:
    reading = rsi_wilder(series.closes, period)[-1]
    return reading is not None and reading >= value


def close_below_bb_lower(series: Series, period: int, std: float) -> bool:
    _mid, lower, _upper = bollinger(series.closes, period, std)
    band = lower[-1]
    return band is not None and series.closes[-1] <= band


def close_above_bb_upper(series: Series, period: int, std: float) -> bool:
    _mid, _lower, upper = bollinger(series.closes, period, std)
    band = upper[-1]
    return band is not None and series.closes[-1] >= band


def pullback_to_ma(
    series: Series, period: int, within_pct: float, rising_period: int | None
) -> bool:
    averages = sma(series.closes, period)
    latest = averages[-1]
    if latest is None:
        return False
    near = series.closes[-1] <= latest * (1 + within_pct / 100)
    if rising_period is None:
        return near
    return near and slope_up(series, rising_period, 5)


def no_new_low(lows: list[float], lookback: int, within_sessions: int) -> bool:
    """True when the recent window did not print a new ``lookback``-day low."""
    if len(lows) <= lookback:
        return True
    start = max(lookback, len(lows) - within_sessions)
    for index in range(start, len(lows)):
        prior = lows[index - lookback : index]
        if prior and lows[index] < min(prior):
            return False
    return True


def stop_loss_pct(close: float, avg_cost: float, value: float) -> bool:
    return (close / avg_cost - 1) * 100 <= -value


def take_profit_pct(close: float, avg_cost: float, value: float) -> bool:
    return (close / avg_cost - 1) * 100 >= value


def trailing_pct(
    close: float, highest: float, avg_cost: float, value: float, activate_pct: float | None
) -> bool:
    if activate_pct is not None and highest < avg_cost * (1 + activate_pct / 100):
        return False
    return (highest - close) / highest * 100 >= value


def trailing_atr(close: float, highest: float, atr: float, multiple: float) -> bool:
    return highest - close >= multiple * atr


def stop_atr(close: float, entry: float, atr_at_entry: float, multiple: float) -> bool:
    return close < entry - multiple * atr_at_entry


def fair_value_reached(raw_close: float, fair_value: float, ratio: float) -> bool:
    return raw_close >= fair_value * ratio


def consecutive_below_ma(closes: list[float], period: int, sessions: int) -> bool:
    averages = sma(closes, period)
    if len(closes) < sessions:
        return False
    for index in range(-sessions, 0):
        average = averages[index]
        if average is None or closes[index] >= average:
            return False
    return True


def signal_reversal(
    side: str | None, on_side: list[str], upside: float | None, below: float | None
) -> bool:
    if side in on_side:
        return True
    return below is not None and upside is not None and upside < below


def signal_missing(sessions_without: int, required: int) -> bool:
    return sessions_without >= required


def rank_drop(rank: int, outside_top: int) -> bool:
    return rank > outside_top


def time_exit(
    hold_sessions: int, max_hold: int, pnl_pct: float | None, only_if_below: float | None
) -> bool:
    if hold_sessions < max_hold:
        return False
    if only_if_below is None or pnl_pct is None:
        return only_if_below is None
    return pnl_pct < only_if_below


def parse_hold(text: str) -> int:
    """``250td`` → 250. Other suffixes are rejected."""
    if not text.endswith("td"):
        raise ValueError(f"unsupported hold period: {text}")
    return int(text[:-2])


def close_above_ref(close: float, ref: float, min_pct: float) -> bool:
    return close >= ref * (1 + min_pct / 100)


def close_below_ref(close: float, ref: float, buffer_pct: float) -> bool:
    return close < ref * (1 - buffer_pct / 100)


def max_chase(close: float, ref: float, max_pct: float) -> bool:
    """Guard: true when the close has not chased through the cap."""
    return close <= ref * (1 + max_pct / 100)


def event_gap_up(
    open_t0: float, close_t0: float, pre_close: float, min_gap_pct: float, hold_pct: float
) -> bool:
    return open_t0 >= pre_close * (1 + min_gap_pct / 100) and close_t0 >= pre_close * (
        1 + hold_pct / 100
    )


def event_volume_breakout(
    volumes: list[float],
    highs: list[float],
    close_t0: float,
    avg_period: int,
    multiple: float,
    lookback: int,
) -> bool:
    """``volumes`` and ``highs`` include T0 as the last point."""
    if len(volumes) < avg_period + 1 or len(highs) < lookback + 1:
        return False
    prior_volume = volumes[-(avg_period + 1) : -1]
    average = sum(prior_volume) / len(prior_volume)
    prior_high = max(highs[-(lookback + 1) : -1])
    return volumes[-1] >= average * multiple and close_t0 > prior_high


def not_limit_up(market: str, is_limit_up: bool) -> bool:
    """US and HK have no daily limit, so the guard is always clear."""
    if market in {"US", "HK"}:
        return True
    return not is_limit_up


def atr_latest(series: Series, period: int) -> float | None:
    if not series.highs or not series.lows:
        return None
    values = atr_wilder(series.highs, series.lows, series.closes, period)
    return values[-1]


def exit_priority(reasons: list[str]) -> str | None:
    """Stop family, then signal reversal, then fair value, then the rest."""
    if not reasons:
        return None
    for family in (STOP_EXITS, {"signal_reversal"}, {"fair_value_reached"}):
        for reason in reasons:
            if reason in family:
                return reason
    return reasons[0]
