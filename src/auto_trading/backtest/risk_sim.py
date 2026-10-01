"""R8 and R9 simulation.

The default resume scenario is ``manual_no_reset``: the high-water mark is
not reset, and a full stop ends only after at least five later trading days
and only once drawdown is back above −10%. ``reset_after_5td`` resumes after
five trading days and resets the high-water mark. ``never`` does not resume.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SCENARIOS = ("manual_no_reset", "reset_after_5td", "never")
HALT_TRADING_DAYS = 5
WARN = -0.05
HALT = -0.10
DAILY_LOSS = -0.02


@dataclass
class RiskSimulator:
    """Checkpoint-driven copy of the execution breakers."""

    hwm: float
    scenario: str = "manual_no_reset"
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
    rejected: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.scenario not in SCENARIOS:
            raise ValueError(f"unknown R9 scenario: {self.scenario}")

    def mark(
        self,
        nlv: float,
        *,
        new_risk_day: bool,
        trading_day: bool,
        session_id: str,
    ) -> None:
        """Update breakers from a net-liquidation checkpoint."""
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
        if self.day_start_nlv and self.day_start_nlv > 0:
            daily = nlv / self.day_start_nlv - 1
            if daily <= DAILY_LOSS and not self.r8_latched:
                self.r8_latched = True
                self.r8_trigger_days += 1

    def allow(self, side: str, kind: str) -> str | None:
        """Return a reject code, or None when the order may proceed.

        Protective stops stay live through a full stop. Strategy sells do not.
        R8 blocks new buys for the rest of the risk day and leaves sells alone.
        """
        if kind == "protective":
            return None
        if self.halted:
            self.rejected["R9"] = self.rejected.get("R9", 0) + 1
            return "R9"
        if side == "BUY" and self.r8_latched:
            self.rejected["R8"] = self.rejected.get("R8", 0) + 1
            return "R8"
        return None

    def note_protective_exit(self) -> None:
        self.protective_stop_exits += 1

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
