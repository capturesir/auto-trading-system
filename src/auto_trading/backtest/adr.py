"""ADR fallback for whole-lot Taiwan names (strategy spec §2.11).

Active only while ``tw_odd_lot_enabled`` is false. The ADR must have its own
BUY signal. Fair values that differ by more than 1% after conversion are not
substituted. ``news_event`` never substitutes.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True)
class AdrMapping:
    local_ticker: str
    adr_ticker: str
    adr_ratio: float
    adr_currency: str
    issuer_group: str
    verified_at: date
    source: str


@dataclass(frozen=True)
class AdrDecision:
    substitute: bool
    ticker: str | None
    signal_id: str | None
    status: str
    reason: str | None
    alert: str | None = None
    adr_checked: bool = False


def load_adr_map(path: Path) -> list[AdrMapping]:
    """Read ``config/adr_map.csv``."""
    rows: list[AdrMapping] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            adr = record.get("adr") or record.get("adr_ticker")
            ratio = record.get("ratio") or record.get("adr_ratio")
            rows.append(
                AdrMapping(
                    local_ticker=record["local_ticker"],
                    adr_ticker=str(adr),
                    adr_ratio=float(str(ratio)),
                    adr_currency=record["adr_currency"],
                    issuer_group=record["issuer_group"],
                    verified_at=date.fromisoformat(record["verified_at"]),
                    source=record["source"],
                )
            )
    return rows


def _as_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def consider_adr(
    *,
    strategy_id: str,
    local_ticker: str,
    odd_lot_enabled: bool,
    whole_lot_exceeds_cap: bool,
    mappings: list[AdrMapping],
    as_of: date,
    adr_signal_side: str | None,
    adr_signal_id: str | None,
    fv_local: float | None,
    fv_adr: float | None,
    twd_per_usd: float | None,
    max_age_days: int = 120,
    max_fv_gap: float = 0.01,
) -> AdrDecision:
    """Decide whether a Taiwan lot-too-large name becomes its ADR."""
    if odd_lot_enabled or not whole_lot_exceeds_cap:
        return AdrDecision(False, None, None, "not_applicable", None)
    if strategy_id == "news_event":
        return AdrDecision(False, None, None, "LOT_TOO_LARGE", "news_event", adr_checked=True)
    match = next((row for row in mappings if row.local_ticker == local_ticker), None)
    if match is None:
        return AdrDecision(False, None, None, "LOT_TOO_LARGE", "no_adr")
    age = (_as_date(as_of) - match.verified_at).days
    if age > max_age_days:
        return AdrDecision(False, None, None, "LOT_TOO_LARGE", "adr_expired", adr_checked=True)
    if adr_signal_side != "BUY":
        return AdrDecision(False, None, None, "LOT_TOO_LARGE", "adr_not_buy", adr_checked=True)
    if fv_local is None or fv_adr is None or not twd_per_usd:
        return AdrDecision(False, None, None, "LOT_TOO_LARGE", "adr_fv_missing", adr_checked=True)
    implied = fv_local * match.adr_ratio / twd_per_usd
    if implied <= 0 or abs(fv_adr - implied) / implied > max_fv_gap:
        return AdrDecision(
            False,
            None,
            None,
            "LOT_TOO_LARGE",
            "adr_fv_inconsistent",
            alert="adr_fv_inconsistent",
            adr_checked=True,
        )
    return AdrDecision(True, match.adr_ticker, adr_signal_id, "ok", None, adr_checked=True)
