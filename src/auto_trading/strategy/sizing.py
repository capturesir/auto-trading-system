"""Position weights from a strategy ``action.size`` block.

``pct_equity`` and ``risk_pct`` values are in percent, matching the v2
drafts: risk 0.5, equal weight 5, confidence scale 60 → 4%, news 3.
"""

from __future__ import annotations

from collections.abc import Mapping


def target_fraction(
    size: Mapping[str, object],
    *,
    confidence: float = 100,
    stop_distance_pct: float = 12.0,
) -> float:
    """Return a fraction of NLV (0.05 means 5%)."""
    kind = size.get("type")
    value = float(size["value"])  # type: ignore[arg-type]
    if kind == "pct_equity":
        weight = value / 100
        scale = size.get("confidence_scale")
        if isinstance(scale, Mapping):
            weight *= min(float(scale["cap"]), confidence / float(scale["ref"]))
    elif kind == "risk_pct":
        if stop_distance_pct <= 0:
            raise ValueError("stop distance must be positive")
        weight = value / stop_distance_pct
    else:
        raise ValueError(f"unsupported size type: {kind}")
    cap = size.get("max_position_pct")
    if isinstance(cap, int | float):
        weight = min(weight, float(cap) / 100)
    return weight
