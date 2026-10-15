"""R9 cooldown cycle (execution spec v0.8 supplement 5, section 3.10).

Pure functions only: no clock, no files, no I/O. Execution and the backtest
import this module. Inputs (NLV, risk date, whether the day is an XHKG
session, whether a kill switch blocks resume) come from the caller.

States: ACTIVE → HALTED_COOLDOWN → HALF_SIZE → ACTIVE.
A new −10% breach during HALF_SIZE starts another HALTED_COOLDOWN cycle.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

ACTIVE = "ACTIVE"
HALTED = "HALTED_COOLDOWN"
HALF = "HALF_SIZE"

DEFAULT_PARAMS: dict[str, float | int] = {
    "R9_drawdown_halt_pct": -0.1,
    "R9_cooldown_trading_days": 20,
    "R9_half_size_mult": 0.5,
    "R9_half_size_trading_days": 20,
    "R9_half_size_reset_dd_pct": -0.05,
}


def initial_state(hwm: float) -> dict[str, Any]:
    """Active book with the given high-water mark and cycle 0."""
    return {
        "state": ACTIVE,
        "hwm": float(hwm),
        "halt_risk_date": None,
        "cooldown_days_done": 0,
        "half_size_days_done": 0,
        "breach_today": False,
        "resume_pending": False,
        "cycle_no": 0,
    }


def size_mult(state: Mapping[str, Any], params: Mapping[str, Any] | None = None) -> float:
    """1.0 active, the half-size multiple, or 0 while halted."""
    table = DEFAULT_PARAMS if params is None else params
    return {ACTIVE: 1.0, HALF: float(table["R9_half_size_mult"]), HALTED: 0.0}[state["state"]]


def _halt(state: dict[str, Any], risk_date: str, nlv: float, events: list[dict[str, Any]]) -> None:
    state.update(
        state=HALTED,
        halt_risk_date=risk_date,
        cooldown_days_done=0,
        half_size_days_done=0,
        breach_today=False,
        resume_pending=False,
        cycle_no=state["cycle_no"] + 1,
    )
    events.append({"kind": "r9_halt", "risk_date": risk_date, "nlv": nlv, "hwm": state["hwm"]})


def _resume(
    state: dict[str, Any],
    risk_date: str,
    nlv: float,
    events: list[dict[str, Any]],
    by: str,
) -> None:
    old = state["hwm"]
    state.update(
        state=HALF,
        hwm=float(nlv),
        half_size_days_done=0,
        breach_today=False,
        resume_pending=False,
    )
    events.append(
        {
            "kind": "r9_resume_half_size",
            "risk_date": risk_date,
            "nlv": nlv,
            "old_hwm": old,
            "new_hwm": float(nlv),
            "by": by,
        }
    )


def on_nlv(
    state: Mapping[str, Any],
    risk_date: str,
    nlv: float,
    params: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply one NLV print. Halted books ignore the print (no HWM update)."""
    table = DEFAULT_PARAMS if params is None else params
    current: dict[str, Any] = copy.deepcopy(dict(state))
    events: list[dict[str, Any]] = []
    if current["state"] == HALTED:
        return current, events
    if nlv > current["hwm"]:
        current["hwm"] = float(nlv)
    drawdown = nlv / current["hwm"] - 1
    if drawdown <= float(table["R9_drawdown_halt_pct"]):
        _halt(current, risk_date, nlv, events)
    elif (
        current["state"] == HALF
        and drawdown <= float(table["R9_half_size_reset_dd_pct"])
        and not current["breach_today"]
    ):
        current.update(half_size_days_done=0, breach_today=True)
        events.append(
            {
                "kind": "r9_half_size_reset",
                "risk_date": risk_date,
                "nlv": nlv,
                "hwm": current["hwm"],
                "dd": round(drawdown, 6),
            }
        )
    return current, events


def on_risk_day_end(
    state: Mapping[str, Any],
    risk_date: str,
    is_xhkg_day: bool,
    nlv: float,
    blocked: bool = False,
    params: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Close one risk day (08:00 Asia/Macau). ``blocked`` means a kill switch is present."""
    table = DEFAULT_PARAMS if params is None else params
    current: dict[str, Any] = copy.deepcopy(dict(state))
    events: list[dict[str, Any]] = []
    if current["state"] == HALTED:
        halt_on = current["halt_risk_date"]
        if is_xhkg_day and halt_on is not None and risk_date > halt_on:
            current["cooldown_days_done"] += 1
        if current["cooldown_days_done"] >= int(table["R9_cooldown_trading_days"]):
            if blocked:
                if not current["resume_pending"]:
                    events.append({"kind": "r9_resume_blocked", "risk_date": risk_date})
                current["resume_pending"] = True
            else:
                _resume(current, risk_date, nlv, events, "r9_auto")
    elif current["state"] == HALF:
        if current["breach_today"]:
            current["breach_today"] = False
        elif is_xhkg_day:
            current["half_size_days_done"] += 1
        if current["half_size_days_done"] >= int(table["R9_half_size_trading_days"]):
            current.update(state=ACTIVE, half_size_days_done=0)
            events.append(
                {
                    "kind": "r9_full_size",
                    "risk_date": risk_date,
                    "nlv": nlv,
                    "hwm": current["hwm"],
                }
            )
    return current, events


def early_resume(
    state: Mapping[str, Any],
    risk_date: str,
    nlv: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Local ``trade resume --r9-early``. Halted books move to half size."""
    current: dict[str, Any] = copy.deepcopy(dict(state))
    events: list[dict[str, Any]] = []
    if current["state"] == HALTED:
        _resume(current, risk_date, nlv, events, "user")
    return current, events
