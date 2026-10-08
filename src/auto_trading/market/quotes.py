"""Quote source. IBKR is asked first. Yahoo chart is the public fallback.

The fallback runs on IBKR errors 10197, 354, and 10167, and on a timeout.
10197 means another login is holding the market-data session. The first
10197 writes a pending alert, and the next successful IBKR quote writes
the clear alert. Every quote keeps its source, data type, market time, and
receipt time. Delayed freshness uses the receipt time.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from auto_trading.alerts.pending import build_alert, write_pending
from auto_trading.market.yahoo import public_quote
from auto_trading.risk.gate import Quote

log = logging.getLogger(__name__)

FALLBACK_CODES = frozenset({10197, 354, 10167})


class MarketDataError(Exception):
    """IBKR did not return a usable quote."""

    def __init__(self, message: str, *, code: int | None = None, timed_out: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.timed_out = timed_out


def data_type_from_ibkr(market_data_type: int) -> str:
    """Map an IBKR market data type.

    ``1`` is realtime. ``3`` and ``4`` are delayed and are usable on paper.
    ``2`` is rejected. There is no setting that accepts ``2``.
    """
    if market_data_type == 1:
        return "realtime"
    if market_data_type in (3, 4):
        return "delayed"
    return "rejected"


def quote_from_ibkr(
    *,
    last: float,
    market_data_type: int,
    as_of: datetime,
    bid: float | None = None,
    ask: float | None = None,
    open_price: float | None = None,
    received_at: datetime | None = None,
) -> Quote:
    """Build a gate quote from one IBKR tick.

    ``as_of`` is the exchange timestamp. ``received_at`` is when this process
    accepted the tick. Delayed quotes record ``delay_sec`` from those two.
    """
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
        received_at=received_at,
        market_data_type=market_data_type,
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
            quote = self._stamp(self._ibkr(symbol))
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

    def _stamp(self, quote: Quote) -> Quote:
        """Mark when this process received an IBKR quote."""
        if quote.received_at is None:
            quote.received_at = self._clock()
        if quote.data_type == "delayed" and quote.delay_sec is None:
            quote.delay_sec = (quote.received_at - quote.as_of).total_seconds()
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


def quotes_path(home: Path) -> Path:
    return home / "state" / "quotes.json"


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def write_quotes(home: Path, now: datetime, rows: list[dict[str, Any]]) -> None:
    """Atomic quote file written by the account process."""
    payload = {
        "schema_version": "quotes/1.0",
        "at": now.isoformat(timespec="seconds"),
        "quotes": rows,
    }
    path = quotes_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_quotes(home: Path) -> dict[str, Quote]:
    """Quotes from ``state/quotes.json``. A bad file yields no prices."""
    path = quotes_path(home)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    rows = payload.get("quotes") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    found: dict[str, Quote] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        try:
            last = float(row["last"])
            as_of = _aware(str(row["as_of"]))
            received = _aware(str(row["received_at"]))
        except (KeyError, TypeError, ValueError):
            continue
        market_data_type = row.get("market_data_type")
        try:
            typed = None if market_data_type is None else int(market_data_type)
        except (TypeError, ValueError):
            typed = None
        quote = Quote(
            last=last,
            as_of=as_of,
            bid=_opt_float(row.get("bid")),
            ask=_opt_float(row.get("ask")),
            source=str(row.get("source") or "ibkr"),
            data_type=str(row.get("data_type") or "realtime"),
            received_at=received,
            market_data_type=typed,
        )
        if quote.data_type == "delayed":
            log.info(
                "delayed_quote symbol=%s source=%s market_data_type=%s delay_sec=%s",
                symbol,
                quote.source,
                quote.market_data_type,
                quote.delay_sec,
            )
        found[symbol] = quote
    return found
