"""G4 anomaly check. A cleared R9 halt still fails the gate."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

BLOCKING = frozenset({"UNKNOWN_ORDER", "RECON_MISMATCH", "HASH_MISMATCH", "R9_HALT", "KILL_SWITCH"})


def g4_failures(events: list[Mapping[str, Any]]) -> list[str]:
    """Return the blocking kinds present in ``anomaly_events``."""
    found: list[str] = []
    for event in events:
        kind = str(event.get("kind"))
        if kind in BLOCKING and kind not in found:
            found.append(kind)
    return found


def g4_passes(events: list[Mapping[str, Any]]) -> bool:
    """False when any blocking anomaly was recorded, including a cleared halt."""
    return not g4_failures(events)
