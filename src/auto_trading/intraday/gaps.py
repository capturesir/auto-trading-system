"""How far a daily confirm is from passing.

The booleans come from the same indicator predicates the strategy engine uses.
``gap`` is how far the latest close (or reading) sits on the failing side.
Zero means the predicate passed. A missing input leaves ``gap`` null.
"""

from __future__ import annotations

from typing import Any

from auto_trading.market.indicators import bollinger, rsi_wilder, sma
from auto_trading.strategy.conditions import _indicator, confirms
from auto_trading.strategy.vocabulary import Series, slope_up


def explain_confirm(
    strategy: dict[str, Any],
    series: Series,
    refs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return ``passed`` plus one row per confirm rule and guard."""
    block = strategy.get("confirm") or {}
    info = dict(refs or {})
    if not block:
        return {"passed": True, "rules": []}
    rules: list[dict[str, Any]] = []
    if "by_source" in block:
        source = str(info.get("event_source") or "")
        chosen = (block.get("by_source") or {}).get(source)
        if not isinstance(chosen, dict):
            return {
                "passed": False,
                "rules": [
                    {
                        "role": "by_source",
                        "indicator": "missing_source",
                        "passed": False,
                        "actual": None,
                        "threshold": None,
                        "gap": None,
                    }
                ],
            }
        rules.extend(_block_rules(chosen, series, info))
    else:
        rules.extend(_block_rules(block, series, info))
    for rule in block.get("guards") or []:
        if isinstance(rule, dict):
            rules.append(_one(rule, series, info, "guard"))
    return {"passed": confirms(strategy, series, info), "rules": rules}


def reason_text(report: dict[str, Any]) -> str:
    """Short text for the watchlist: which rules are still short, and by how much."""
    if report.get("passed"):
        return "confirm_pass"
    parts: list[str] = []
    for rule in report.get("rules") or []:
        if rule.get("passed"):
            continue
        indicator = str(rule.get("indicator") or "rule")
        gap = rule.get("gap")
        if isinstance(gap, int | float):
            parts.append(f"{indicator} gap={gap:.4g}")
        else:
            parts.append(indicator)
    return "; ".join(parts) or "confirm_fail"


def _block_rules(
    block: dict[str, Any], series: Series, refs: dict[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rule in block.get("all_of") or []:
        if isinstance(rule, dict):
            rows.append(_one(rule, series, refs, "all_of"))
    for rule in block.get("any_of") or []:
        if isinstance(rule, dict):
            rows.append(_one(rule, series, refs, "any_of"))
    return rows


def _one(rule: dict[str, Any], series: Series, refs: dict[str, Any], role: str) -> dict[str, Any]:
    passed = bool(series.closes) and _indicator(rule, series, refs)
    if rule.get("indicator") == "not_limit_up":
        passed = _indicator(rule, series, refs)
    actual, threshold, gap = _distance(rule, series, passed)
    return {
        "role": role,
        "indicator": rule.get("indicator"),
        "passed": passed,
        "actual": actual,
        "threshold": threshold,
        "gap": gap,
    }


def _distance(
    rule: dict[str, Any], series: Series, passed: bool
) -> tuple[float | None, float | None, float | None]:
    name = rule.get("indicator")
    if not series.closes or name in {"no_new_low", "not_limit_up"}:
        return None, None, None
    close = series.closes[-1]
    if name == "rsi_below":
        return _rsi_gap(
            series, int(rule["period"]), float(rule["value"]), below=True, passed=passed
        )
    if name == "rsi_above":
        return _rsi_gap(
            series, int(rule["period"]), float(rule["value"]), below=False, passed=passed
        )
    if name == "close_above_ma":
        return _ma_gap(series, int(rule["period"]), above=True, passed=passed)
    if name == "close_below_bb_lower":
        return _band_gap(series, int(rule["period"]), float(rule["std"]), lower=True, passed=passed)
    if name == "close_above_bb_upper":
        return _band_gap(
            series, int(rule["period"]), float(rule["std"]), lower=False, passed=passed
        )
    if name == "pullback_to_ma":
        return _pullback_gap(rule, series, close, passed)
    if name == "ma_slope_up":
        return _slope_gap(series, int(rule["period"]), int(rule["lookback"]), passed)
    if name in {"close_above_ref", "max_chase", "event_gap_up", "event_volume_breakout"}:
        return close, None, 0.0 if passed else None
    return close, None, 0.0 if passed else None


def _rsi_gap(
    series: Series, period: int, threshold: float, *, below: bool, passed: bool
) -> tuple[float | None, float | None, float | None]:
    reading = rsi_wilder(series.closes, period)[-1]
    if reading is None:
        return None, threshold, None
    if passed:
        gap = 0.0
    elif below:
        gap = reading - threshold
    else:
        gap = threshold - reading
    return reading, threshold, gap


def _ma_gap(
    series: Series, period: int, *, above: bool, passed: bool
) -> tuple[float | None, float | None, float | None]:
    average = sma(series.closes, period)[-1]
    close = series.closes[-1]
    if average is None:
        return close, None, None
    if passed:
        gap = 0.0
    elif above:
        gap = average - close
    else:
        gap = close - average
    return close, average, gap


def _band_gap(
    series: Series, period: int, std: float, *, lower: bool, passed: bool
) -> tuple[float | None, float | None, float | None]:
    _mid, band_low, band_high = bollinger(series.closes, period, std)
    band = band_low[-1] if lower else band_high[-1]
    close = series.closes[-1]
    if band is None:
        return close, None, None
    if passed:
        gap = 0.0
    elif lower:
        gap = close - band
    else:
        gap = band - close
    return close, band, gap


def _pullback_gap(
    rule: dict[str, Any], series: Series, close: float, passed: bool
) -> tuple[float | None, float | None, float | None]:
    period = int(rule["period"])
    average = sma(series.closes, period)[-1]
    if average is None:
        return close, None, None
    ceiling = average * (1 + float(rule["within_pct"]) / 100)
    rising = rule.get("require_ma_rising")
    slope_ok = True
    if rising is not None:
        slope_ok = slope_up(series, int(rising), 5)
    if passed:
        gap = 0.0
    else:
        gap = max(close - ceiling, 0.0)
        if close <= ceiling and not slope_ok:
            gap = None
    return close, ceiling, gap


def _slope_gap(
    series: Series, period: int, lookback: int, passed: bool
) -> tuple[float | None, float | None, float | None]:
    averages = sma(series.closes, period)
    if len(averages) <= lookback:
        return None, None, None
    latest = averages[-1]
    earlier = averages[-1 - lookback]
    if latest is None or earlier is None:
        return None, None, None
    gap = 0.0 if passed else max(earlier - latest, 0.0)
    return latest - earlier, 0.0, gap
