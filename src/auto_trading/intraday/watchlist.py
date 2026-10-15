"""Morning watchlist: trigger passed, confirm did not.

Written by the 07:05 session runner. The intraday re-eval reads this file and
does not scan the whole signal universe. Scheme B may also keep BUY names whose
confirm already passed, so a name the allocator skipped can be tried again
once during the session.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from auto_trading.alerts.holdings_state import atomic_write_json
from auto_trading.backtest.engine import Bar, _entry_refs, _series
from auto_trading.execution.strategy_book import load_approved
from auto_trading.intraday.gaps import explain_confirm, reason_text
from auto_trading.market.instruments import describe
from auto_trading.strategy.conditions import UnknownCondition
from auto_trading.strategy.spec import ApprovedStrategy, StrategyNotApproved, require_approved

SCHEMA = "intraday_watchlist/1.0"
CONFIDENCE_FLOOR = 60
_SIGNAL_TRIGGERS = frozenset({None, "signal", "event", "schedule"})


def watchlist_path(trading_home: Path) -> Path:
    return trading_home / "state" / "intraday_watchlist.json"


def include_valid_buys() -> bool:
    """Scheme B keeps still-valid BUY names on the watchlist unless this is off."""
    raw = os.environ.get("INTRADAY_REEVAL_INCLUDE_VALID_BUYS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def write_watchlist(
    trading_home: Path,
    *,
    session_date: str,
    now: datetime,
    signals: list[Any],
    bars: dict[str, list[Bar]],
    approved_dir: Path,
    pins: dict[tuple[str, int], str],
    include_valid: bool | None = None,
) -> Path:
    """Replace ``state/intraday_watchlist.json``. Does not write intents."""
    keep_valid = include_valid_buys() if include_valid is None else include_valid
    items: list[dict[str, Any]] = []
    for strategy_id, version in sorted(pins, key=lambda item: (item[0], item[1])):
        path = approved_dir / f"{strategy_id}.v{version}.json"
        if not path.is_file():
            continue
        document = load_approved(path)
        if document.get("mode") != "paper":
            continue
        try:
            digest = require_approved(
                document,
                ApprovedStrategy(
                    strategy_id=strategy_id,
                    version=version,
                    spec_hash=pins[(strategy_id, version)],
                ),
            )
        except StrategyNotApproved:
            continue
        trigger = document.get("trigger") if isinstance(document.get("trigger"), dict) else {}
        if trigger.get("type") not in _SIGNAL_TRIGGERS:
            continue
        try:
            items.extend(
                _rows_for_strategy(
                    document,
                    digest,
                    signals,
                    bars,
                    session_date,
                    keep_valid=keep_valid,
                )
            )
        except UnknownCondition:
            continue
    items.sort(key=lambda row: (str(row["strategy_id"]), str(row["ticker"]), str(row["kind"])))
    payload = {
        "schema_version": SCHEMA,
        "session_date": session_date,
        "generated_at": now.isoformat(),
        "include_valid_buys": keep_valid,
        "items": items,
    }
    target = watchlist_path(trading_home)
    atomic_write_json(target, payload)
    return target


def _rows_for_strategy(
    document: dict[str, Any],
    digest: str,
    signals: list[Any],
    bars: dict[str, list[Bar]],
    session_date: str,
    *,
    keep_valid: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    markets = document.get("markets")
    minimum = _minimum_confidence(document)
    trigger = document.get("trigger") if isinstance(document.get("trigger"), dict) else {}
    upside_floor = trigger.get("min_upside_live_pct")
    for signal in signals:
        if getattr(signal, "side", None) != "BUY":
            continue
        if str(getattr(signal, "price_date", "")) > session_date:
            continue
        ticker = str(signal.ticker)
        market = describe(ticker).market
        if isinstance(markets, list) and markets and market not in markets:
            continue
        if int(signal.confidence) < minimum:
            continue
        history = [row for row in bars.get(ticker, []) if row.date < session_date]
        if not history:
            continue
        price = history[-1].close
        upside = _upside(signal, price)
        if upside_floor is not None and (upside is None or upside < float(upside_floor)):
            continue
        report = explain_confirm(document, _series(history), _entry_refs(signal, history))
        if report["passed"] and not keep_valid:
            continue
        kind = "valid_buy" if report["passed"] else "confirm_fail"
        rows.append(
            {
                "strategy_id": document["id"],
                "strategy_version": document["version"],
                "spec_hash": digest,
                "ticker": ticker,
                "market": market,
                "kind": kind,
                "signal_id": signal.signal_id,
                "side": "BUY",
                "confidence": int(signal.confidence),
                "fair_value": float(signal.fair_value),
                "price_date": str(signal.price_date),
                "upside_pct": upside,
                "pre_event_close": getattr(signal, "pre_event_close", None),
                "event_source": getattr(signal, "event_source", None),
                "reason": reason_text(report),
                "rules": report["rules"],
            }
        )
    return rows


def _minimum_confidence(document: dict[str, Any]) -> int:
    trigger = document.get("trigger") if isinstance(document.get("trigger"), dict) else {}
    exempt = document.get("id") == "ta_breakout52" or trigger.get("type") == "market"
    floor = 0 if exempt else CONFIDENCE_FLOOR
    declared = trigger.get("min_confidence", floor)
    return max(int(declared), floor)


def _upside(signal: Any, price: float | None) -> float | None:
    fair = getattr(signal, "fair_value", None)
    if price and fair:
        return (float(fair) / float(price) - 1) * 100
    raw = getattr(signal, "upside_pct", None)
    return None if raw is None else float(raw)
