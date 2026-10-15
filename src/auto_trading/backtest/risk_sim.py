"""R8 and R9 simulation.

The default scenario is ``cooldown_half_size``. It calls
:mod:`auto_trading.risk.r9_cycle` — the same functions the executor uses.
``manual_no_reset``, ``reset_after_5td``, and ``never`` stay available as
sensitivity comparisons. They are not the default.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from auto_trading.risk.r9_cycle import (
    DEFAULT_PARAMS,
    HALTED,
    early_resume,
    initial_state,
    on_nlv,
    on_risk_day_end,
    size_mult,
)

SCENARIOS = ("cooldown_half_size", "manual_no_reset", "reset_after_5td", "never")
HALT_TRADING_DAYS = 5
WARN = -0.05
HALT = -0.10
DAILY_LOSS = -0.02
_GROSS = 0.95
_R1 = 0.05
_R2 = 200_000.0


def _params(limits: Mapping[str, Any] | None) -> dict[str, Any]:
    params = dict(DEFAULT_PARAMS)
    if limits:
        for key in params:
            if key in limits:
                params[key] = limits[key]
    return params


def _xhkg(session_id: str, trading_day: bool) -> bool:
    try:
        day = date.fromisoformat(session_id)
    except ValueError:
        return trading_day
    from auto_trading.market.calendar import is_session

    return is_session("XHKG", day)


@dataclass
class RiskSimulator:
    """Checkpoint-driven copy of the execution breakers."""

    hwm: float
    scenario: str = "cooldown_half_size"
    halted: bool = False
    halt_session_id: str | None = None
    last_halt_count_session: str | None = None
    halted_sessions: int = 0
    r8_latched: bool = False
    r8_trigger_days: int = 0
    r9_warn_count: int = 0
    r9_halt_count: int = 0
    protective_stop_exits: int = 0
    in_warn: bool = False
    day_start_nlv: float | None = None
    limits: dict[str, Any] | None = None
    rejected: dict[str, int] = field(default_factory=dict)
    r9_cycles: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.scenario not in SCENARIOS:
            raise ValueError(f"unknown R9 scenario: {self.scenario}")
        self.cycle = initial_state(self.hwm)
        self._r9_params = _params(self.limits)
        self._open_cycle: dict[str, Any] | None = None

    def size_multiplier(self) -> float:
        if self.scenario != "cooldown_half_size":
            return 1.0
        return size_mult(self.cycle, self._r9_params)

    def order_caps(self) -> tuple[float, float, float]:
        """R1, R2, and R6 after the cycle size multiple."""
        mult = self.size_multiplier()
        return _R1 * mult, _R2 * mult, _GROSS * mult

    def mark(
        self,
        nlv: float,
        *,
        new_risk_day: bool,
        trading_day: bool,
        session_id: str,
    ) -> None:
        """Update breakers from a net-liquidation checkpoint."""
        if self.scenario == "cooldown_half_size":
            self._mark_cycle(
                nlv,
                new_risk_day=new_risk_day,
                trading_day=trading_day,
                session_id=session_id,
            )
            return
        if new_risk_day:
            self.day_start_nlv = nlv
            self.r8_latched = False
        if self.halted and trading_day and session_id != self.halt_session_id:
            if session_id != self.last_halt_count_session:
                self.halted_sessions += 1
                self.last_halt_count_session = session_id
            self._maybe_resume(nlv)
        if not self.halted and nlv > self.hwm:
            self.hwm = nlv
        drawdown = nlv / self.hwm - 1
        if drawdown <= HALT and not self.halted:
            self.halted = True
            self.r9_halt_count += 1
            self.halt_session_id = session_id
            self.in_warn = False
        elif (not self.halted) and drawdown <= WARN and not self.in_warn:
            self.in_warn = True
            self.r9_warn_count += 1
        elif drawdown > WARN:
            self.in_warn = False
        self._mark_r8(nlv)

    def allow(self, side: str, kind: str) -> str | None:
        """Return a reject code, or None when the order may proceed.

        Protective stops stay in force. Under ``cooldown_half_size``, strategy
        exits and reductions are allowed during cooldown; new entries are not.
        The sensitivity scenarios still reject strategy sells while halted.
        R8 blocks new buys for the rest of the risk day and leaves sells alone.
        """
        if kind == "protective":
            return None
        if self.scenario == "cooldown_half_size" and self.cycle["state"] == HALTED:
            if side == "BUY" or kind in {"entry", "add", "ENTRY", "ADD"}:
                self.rejected["R9_HALTED_COOLDOWN"] = self.rejected.get("R9_HALTED_COOLDOWN", 0) + 1
                return "R9_HALTED_COOLDOWN"
            return None
        if self.halted:
            self.rejected["R9"] = self.rejected.get("R9", 0) + 1
            return "R9"
        if side == "BUY" and self.r8_latched:
            self.rejected["R8"] = self.rejected.get("R8", 0) + 1
            return "R8"
        return None

    def resume_early(self, risk_date: str, nlv: float) -> None:
        """Same early-resume step the executor accepts from a local terminal."""
        if self.scenario != "cooldown_half_size":
            return
        self.cycle, events = early_resume(self.cycle, risk_date, nlv)
        self._note(events)
        self.hwm = float(self.cycle["hwm"])
        self.halted = self.cycle["state"] == HALTED

    def note_protective_exit(self) -> None:
        self.protective_stop_exits += 1

    def _mark_r8(self, nlv: float) -> None:
        if self.day_start_nlv and self.day_start_nlv > 0:
            daily = nlv / self.day_start_nlv - 1
            if daily <= DAILY_LOSS and not self.r8_latched:
                self.r8_latched = True
                self.r8_trigger_days += 1

    def _mark_cycle(
        self,
        nlv: float,
        *,
        new_risk_day: bool,
        trading_day: bool,
        session_id: str,
    ) -> None:
        if new_risk_day:
            self.day_start_nlv = nlv
            self.r8_latched = False
        self._note_warn(nlv)
        self._mark_r8(nlv)
        if new_risk_day:
            return
        self.cycle, nlv_events = on_nlv(self.cycle, session_id, nlv, self._r9_params)
        self.cycle, day_events = on_risk_day_end(
            self.cycle,
            session_id,
            _xhkg(session_id, trading_day),
            nlv,
            False,
            self._r9_params,
        )
        self._note(nlv_events + day_events)
        self.hwm = float(self.cycle["hwm"])
        self.halted = self.cycle["state"] == HALTED

    def _note_warn(self, nlv: float) -> None:
        if self.cycle["state"] == HALTED or self.hwm <= 0:
            return
        drawdown = nlv / self.hwm - 1
        if drawdown <= HALT:
            self.in_warn = False
        elif drawdown <= WARN and not self.in_warn:
            self.in_warn = True
            self.r9_warn_count += 1
        elif drawdown > WARN:
            self.in_warn = False

    def _note(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            kind = event["kind"]
            if kind == "r9_halt":
                self.r9_halt_count += 1
                self.in_warn = False
                self.halt_session_id = event.get("risk_date")
                self._open_cycle = {
                    "trigger_date": event.get("risk_date"),
                    "resume_date": None,
                    "full_size_date": None,
                    "exposure_cap": None,
                    "cycle_no": self.cycle["cycle_no"],
                }
                self.r9_cycles.append(self._open_cycle)
            elif kind == "r9_resume_half_size" and self._open_cycle is not None:
                self._open_cycle["resume_date"] = event.get("risk_date")
                mult = float(self._r9_params["R9_half_size_mult"])
                self._open_cycle["exposure_cap"] = _GROSS * mult
            elif kind == "r9_full_size" and self._open_cycle is not None:
                self._open_cycle["full_size_date"] = event.get("risk_date")
                self._open_cycle = None

    def _maybe_resume(self, nlv: float) -> None:
        if self.scenario == "never" or self.halted_sessions < HALT_TRADING_DAYS:
            return
        if self.scenario == "reset_after_5td":
            self.hwm = nlv
            self.halted = False
            self.in_warn = False
            return
        if nlv / self.hwm - 1 > HALT:
            self.halted = False


def fill_rejections(
    notional: float,
    nlv: float,
    *,
    r1: float = 0.05,
    r2: float = 200_000,
    position_after: float | None = None,
    hard_cap: float = 0.10,
    market_after: float | None = None,
    market_cap: float | None = None,
    ccy_after: float | None = None,
    ccy_cap: float | None = None,
    gross_after: float | None = None,
    gross_cap: float = 0.95,
    cash_after: float | None = None,
) -> list[str]:
    """Whole-order rejects at the fill. The quantity is not reduced."""
    reasons: list[str] = []
    if notional > nlv * r1 + 1e-6:
        reasons.append("R1")
    if notional > r2 + 1e-6:
        reasons.append("R2")
    if position_after is not None and position_after > nlv * hard_cap + 1e-6:
        reasons.append("R3")
    market_over = (
        market_after is not None
        and market_cap is not None
        and market_after > nlv * market_cap + 1e-6
    )
    if market_over:
        reasons.append("R4")
    if ccy_after is not None and ccy_cap is not None and ccy_after > nlv * ccy_cap + 1e-6:
        reasons.append("R5")
    if gross_after is not None and gross_after > nlv * gross_cap + 1e-6:
        reasons.append("R6")
    if cash_after is not None and cash_after < -1e-6:
        reasons.append("R7")
    return reasons


def replay_vector(
    vector: Mapping[str, Any],
    limits: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run one shared vector through the cycle module and collect the cycle report."""
    sim = RiskSimulator(hwm=float(vector["initial_hwm"]), limits=dict(limits) if limits else None)
    rows: list[dict[str, Any]] = []
    for record in vector["steps"]:
        step = record["step"]
        op = step["op"]
        if op == "nlv":
            sim.cycle, events = on_nlv(sim.cycle, step["risk_date"], step["nlv"], sim._r9_params)
        elif op == "day_end":
            sim.cycle, events = on_risk_day_end(
                sim.cycle,
                step["risk_date"],
                bool(step["xhkg"]),
                step["nlv"],
                bool(step.get("blocked", False)),
                sim._r9_params,
            )
        else:
            sim.cycle, events = early_resume(sim.cycle, step["risk_date"], step["nlv"])
        sim._note(events)
        sim.hwm = float(sim.cycle["hwm"])
        sim.halted = sim.cycle["state"] == HALTED
        rows.append(
            {
                "state": sim.cycle["state"],
                "hwm": sim.cycle["hwm"],
                "cooldown_days_done": sim.cycle["cooldown_days_done"],
                "half_size_days_done": sim.cycle["half_size_days_done"],
                "size_mult": size_mult(sim.cycle, sim._r9_params),
                "events": [event["kind"] for event in events],
            }
        )
    return rows, sim.r9_cycles
