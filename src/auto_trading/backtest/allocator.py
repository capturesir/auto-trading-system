"""Cross-strategy allocator (strategy spec §5.9).

Exits run before entries. Opening orders follow
``sig_trend > sig_pullback > news_event > fv_rotation``. One opening order
per ticker per session. An issuer group shares the 9.5% room.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from auto_trading.backtest.lots import LotDecision, size_quantity

PRIORITY = ("sig_trend", "sig_pullback", "news_event", "fv_rotation")
PRICE_BUFFER = 1.02
NAME_ROOM = 0.095


@dataclass(frozen=True)
class Proposal:
    strategy_id: str
    ticker: str
    issuer_group: str
    kind: str
    budget: float
    price_local: float
    fx_to_base: float
    market: str
    lot_size: int = 1
    qty: int | None = None


@dataclass(frozen=True)
class Allocated:
    proposal: Proposal
    qty: int
    status: str
    binding_limit: str | None = None
    odd_remainder_dropped: int = 0
    adr_substituted: bool = False


def _rank(strategy_id: str, priority: tuple[str, ...] = PRIORITY) -> int:
    try:
        return priority.index(strategy_id)
    except ValueError:
        return len(priority)


def allocate(
    proposals: list[Proposal],
    *,
    nlv: float,
    issuer_notional: dict[str, float] | None = None,
    odd_lot_enabled: bool = False,
    r1: float = 0.05,
    r2: float = 200_000,
    r9_size_mult: float = 1.0,
    adr_fallback: Callable[[Proposal], Proposal | None] | None = None,
    priority: tuple[str, ...] = PRIORITY,
    price_divisor: float = PRICE_BUFFER,
    name_room: float = NAME_ROOM,
) -> list[Allocated]:
    """Clip proposals to the shared caps. Nothing is rounded up.

    ``r9_size_mult`` scales R1 and R2 during HALF_SIZE. R3 stays on the
    issuer room, which this allocator does not halve. ``priority``,
    ``price_divisor``, and ``name_room`` default to the backtest constants
    and can be filled from ``config/allocator.yaml``.
    """
    held = dict(issuer_notional or {})
    r1 *= r9_size_mult
    r2 *= r9_size_mult
    exits = [item for item in proposals if item.kind in {"EXIT", "REDUCE"}]
    entries = [item for item in proposals if item.kind in {"ENTRY", "ADD"}]
    entries.sort(key=lambda item: _rank(item.strategy_id, priority))
    order_budget = min(nlv * r1, r2) / price_divisor
    out: list[Allocated] = []
    opened: set[str] = set()
    for proposal in exits:
        qty = proposal.qty if proposal.qty is not None else 0
        out.append(Allocated(proposal, qty, "ok"))
        released = proposal.budget
        held[proposal.issuer_group] = held.get(proposal.issuer_group, 0.0) - released
    for proposal in entries:
        if proposal.ticker in opened:
            out.append(Allocated(proposal, 0, "skipped_same_ticker"))
            continue
        room = name_room * nlv - held.get(proposal.issuer_group, 0.0)
        decision: LotDecision = size_quantity(
            price_local=proposal.price_local,
            fx_to_base=proposal.fx_to_base,
            strategy_budget=proposal.budget,
            order_budget=order_budget,
            ticker_budget=max(room, 0.0),
            market=proposal.market,
            odd_lot_enabled=odd_lot_enabled,
            lot_size=proposal.lot_size,
        )
        chosen = proposal
        substituted = False
        if (
            decision.status == "LOT_TOO_LARGE"
            and proposal.market == "TW"
            and not odd_lot_enabled
            and adr_fallback is not None
        ):
            alternate = adr_fallback(proposal)
            if alternate is not None and alternate.ticker not in opened:
                alt_decision = size_quantity(
                    price_local=alternate.price_local,
                    fx_to_base=alternate.fx_to_base,
                    strategy_budget=alternate.budget,
                    order_budget=order_budget,
                    ticker_budget=max(room, 0.0),
                    market=alternate.market,
                    odd_lot_enabled=False,
                    lot_size=alternate.lot_size,
                )
                if alt_decision.status == "ok":
                    decision = alt_decision
                    chosen = alternate
                    substituted = True
        out.append(
            Allocated(
                chosen,
                decision.qty,
                decision.status,
                decision.binding_limit,
                decision.odd_remainder_dropped,
                adr_substituted=substituted,
            )
        )
        if decision.status == "ok":
            opened.add(chosen.ticker)
            held[chosen.issuer_group] = held.get(chosen.issuer_group, 0.0) + (
                decision.qty * chosen.price_local * chosen.fx_to_base
            )
    return out
