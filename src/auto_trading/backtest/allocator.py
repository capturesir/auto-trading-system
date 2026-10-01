"""Cross-strategy allocator (strategy spec §5.9).

Exits run before entries. Opening orders follow
``sig_trend > sig_pullback > news_event > fv_rotation``. One opening order
per ticker per session. An issuer group shares the 9.5% room.
"""

from __future__ import annotations

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


def _rank(strategy_id: str) -> int:
    try:
        return PRIORITY.index(strategy_id)
    except ValueError:
        return len(PRIORITY)


def allocate(
    proposals: list[Proposal],
    *,
    nlv: float,
    issuer_notional: dict[str, float] | None = None,
    odd_lot_enabled: bool = False,
    r1: float = 0.05,
    r2: float = 200_000,
) -> list[Allocated]:
    """Clip proposals to the shared caps. Nothing is rounded up."""
    held = dict(issuer_notional or {})
    exits = [item for item in proposals if item.kind in {"EXIT", "REDUCE"}]
    entries = [item for item in proposals if item.kind in {"ENTRY", "ADD"}]
    entries.sort(key=lambda item: _rank(item.strategy_id))
    order_budget = min(nlv * r1, r2) / PRICE_BUFFER
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
        room = NAME_ROOM * nlv - held.get(proposal.issuer_group, 0.0)
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
        out.append(
            Allocated(
                proposal,
                decision.qty,
                decision.status,
                decision.binding_limit,
                decision.odd_remainder_dropped,
            )
        )
        if decision.status == "ok":
            opened.add(proposal.ticker)
            held[proposal.issuer_group] = held.get(proposal.issuer_group, 0.0) + (
                decision.qty * proposal.price_local * proposal.fx_to_base
            )
    return out
