"""Quote source. IBKR is asked first. Yahoo chart is the public fallback.

The fallback runs on IBKR errors 10197, 354, and 10167, and on a timeout.
10197 means another login is holding the market-data session. The first
10197 writes a pending alert, and the next successful IBKR quote writes
the clear alert. Every quote keeps its source, data type, and timestamp.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from auto_trading.alerts.pending import build_alert, write_pending
from auto_trading.market.yahoo import public_quote
from auto_trading.risk.gate import Quote

FALLBACK_CODES = frozenset({10197, 354, 10167})


class MarketDataError(Exception):
    """IBKR did not return a usable quote."""

    def __init__(self, message: str, *, code: int | None = None, timed_out: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.timed_out = timed_out


def data_type_from_ibkr(market_data_type: int) -> str:
    """Map an IBKR market data type. ``1`` is realtime. ``3`` is delayed."""
    if market_data_type == 1:
        return "realtime"
    return "delayed"


def quote_from_ibkr(
    *,
    last: float,
    market_data_type: int,
    as_of: datetime,
    bid: float | None = None,
    ask: float | None = None,
    open_price: float | None = None,
) -> Quote:
    """Build a gate quote from one IBKR tick."""
    kind = data_type_from_ibkr(market_data_type)
    return Quote(
        last=last,
        as_of=as_of,
        bid=bid,
        ask=ask,
        open_price=open_price,
        source="ibkr",
        data_type=kind,
        delayed=kind == "delayed",
    )


def yahoo_quote(symbol: str, *, getter: Callable[[str], str] | None = None) -> Quote:
    """Public last price from the Yahoo chart client already used for daily bars."""
    price, as_of = public_quote(symbol, getter=getter)
    return Quote(last=price, as_of=as_of, source="yahoo", data_type="public")


class QuoteRouter:
    """Ask IBKR, then the public fetcher when the IBKR error is on the fallback list."""

    def __init__(
        self,
        ibkr: Callable[[str], Quote],
        public: Callable[[str], Quote],
        *,
        trading_home: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ibkr = ibkr
        self._public = public
        self._home = trading_home
        self._clock = clock or (lambda: datetime.now(UTC))
        self.competing = False

    def fetch(self, symbol: str) -> Quote:
        """Return one quote. A competing session falls back until IBKR answers again."""
        try:
            quote = self._ibkr(symbol)
        except (MarketDataError, TimeoutError) as exc:
            self._note_failure(exc)
            return self._public(symbol)
        if self.competing:
            self.competing = False
            self._alert(
                "ibkr_10197_cleared",
                "clear",
                "IBKR 行情恢復，手機登入不再佔用報價。",
            )
        return quote

    def _note_failure(self, exc: Exception) -> None:
        code = getattr(exc, "code", None)
        timed_out = isinstance(exc, TimeoutError) or bool(getattr(exc, "timed_out", False))
        if code not in FALLBACK_CODES and not timed_out:
            raise exc
        if code == 10197 and not self.competing:
            self.competing = True
            self._alert(
                "ibkr_10197_started",
                "start",
                "IBKR 回覆 10197，手機登入佔用行情。報價改走公開來源。",
            )

    def _alert(self, rule_id: str, direction: str, message: str) -> None:
        if self._home is None:
            return
        when = self._clock()
        alert = build_alert(
            rule_id=rule_id,
            alert_type=rule_id,
            severity="warning",
            ticker="IBKR",
            message_zh=message,
            trading_date=when.date().isoformat(),
            direction=direction,
            created_at=when,
        )
        write_pending(self._home, alert, filename_stamp=when.strftime("%Y%m%dT%H%M%S"))
