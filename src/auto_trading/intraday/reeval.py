"""Intraday re-evaluation. Paper account only. Does not place orders.

Phase 1 reads the morning watchlist and writes ``state/intraday_reeval.json``.
Phase 2 appends ENTRY intents when ``INTRADAY_REEVAL_WRITE_INTENTS`` is on.
Phase 3 appends EXIT intents from the same strategy exit rules when
``INTRADAY_REEVAL_WRITE_EXITS`` is on. Both flags default off.

Holdings monitor stays alerts-only. This module never asks it to write intents.
Confirm is :func:`auto_trading.backtest.engine.plan_session` with today's
provisional close included. Exit checks use the same ``_exit_order`` helper
the backtest uses. Submit still applies the risk gate.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from auto_trading.alerts.holdings_state import atomic_write_json
from auto_trading.backtest.engine import (
    Bar,
    _exit_order,
    _series,
    plan_session,
)
from auto_trading.backtest.risk_sim import RiskSimulator
from auto_trading.execution.fx import load_rates
from auto_trading.execution.strategy_book import (
    REPO_ROOT,
    load_approved,
    load_confirmed_spec_hashes,
)
from auto_trading.intraday.gaps import explain_confirm, reason_text
from auto_trading.intraday.watchlist import watchlist_path
from auto_trading.jsonutil import loads_strict
from auto_trading.market.instruments import describe
from auto_trading.market.yahoo import cache_path, read_parquet
from auto_trading.paper_account import DU_ACCOUNT
from auto_trading.risk.limits import assess_files
from auto_trading.risk.r9_cycle import HALTED
from auto_trading.strategy.conditions import UnknownCondition
from auto_trading.strategy.spec import ApprovedStrategy, StrategyNotApproved, require_approved

log = logging.getLogger(__name__)

MACAU = ZoneInfo("Asia/Macau")
PAPER_ACCOUNT = "DUR243736"
DEBOUNCE_SCHEMA = "intraday_debounce/1.0"
REPORT_SCHEMA = "intraday_reeval/1.0"
SOURCE = "intraday_reeval"
FORBIDDEN_CLIENT_IDS = frozenset({11, 12, 13, 14, 15, 31})
OPEN_WINDOWS = frozenset({"continuous", "open_buffer", "close_buffer"})
_MANUAL = "_manual"


class PaperGateError(RuntimeError):
    """The process may not write. The reason is a short code, not an order."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class Held:
    """One open strategy lot the exit pass may close."""

    ticker: str
    qty: float
    avg_cost: float
    strategy_id: str
    parent_intent_id: str
    opened_on: str | None = None
    fair_value: float | None = None
    partial_taken: bool = False


def env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _flag_or_env(flag: bool | None, name: str) -> bool:
    if flag is None:
        return env_flag(name)
    return flag


def clamp_interval(raw: str | None = None) -> int:
    """Loop sleep in seconds. The allowed band is 1 to 5 minutes."""
    text = raw if raw is not None else os.environ.get("INTRADAY_REEVAL_INTERVAL_SEC", "120")
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        value = 120
    return min(300, max(60, value))


def parse_client_id(raw: str | None = None) -> int:
    """Client id 16. Submit, recon, account, watchdog, holdings, and health ids are refused."""
    text = raw if raw is not None else os.environ.get("IB_CLIENT_ID_REEVAL", "16")
    text = str(text).strip() or "16"
    try:
        value = int(text)
    except ValueError as exc:
        raise PaperGateError("client_id_forbidden") from exc
    if value <= 0 or value in FORBIDDEN_CLIENT_IDS:
        raise PaperGateError("client_id_forbidden")
    return value


def assert_paper_account(trading_home: Path) -> str:
    """Require DUR243736. Any other id, including other DU* ids, is refused."""
    pin = os.environ.get("IBKR_PAPER_ACCOUNT", "").strip()
    account = _read_account(trading_home)
    named = str(account.get("account") or "").strip() if isinstance(account, dict) else ""
    mode = "paper"
    if isinstance(account, dict) and account.get("mode") not in {None, ""}:
        mode = str(account.get("mode")).strip()
    if mode != "paper":
        raise PaperGateError("mode_refused")
    if pin and not _is_pinned_paper(pin):
        raise PaperGateError("account_refused")
    if named and not _is_pinned_paper(named):
        raise PaperGateError("account_refused")
    if pin and named and pin != named:
        raise PaperGateError("account_refused")
    if not pin and not named:
        raise PaperGateError("account_missing")
    return pin or named


def debounce_path(trading_home: Path) -> Path:
    return trading_home / "state" / "intraday_debounce.json"


def report_path(trading_home: Path) -> Path:
    return trading_home / "state" / "intraday_reeval.json"


def load_debounce(trading_home: Path, session_date: str) -> dict[str, Any]:
    path = debounce_path(trading_home)
    blank = _blank_debounce(session_date)
    if not path.is_file():
        return blank
    try:
        payload = loads_strict(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return blank
    if not isinstance(payload, dict) or payload.get("session_date") != session_date:
        return blank
    blank["entries"] = _str_list(payload.get("entries"))
    blank["exits"] = _str_list(payload.get("exits"))
    blank["full_exits"] = _str_list(payload.get("full_exits"))
    return blank


def note_session_intents(trading_home: Path, session_date: str) -> dict[str, Any]:
    """Remember ENTRY and EXIT rows already on disk for this session.

    The morning runner calls this after it writes, so a filled intent that
    later leaves the file still counts toward the one-entry debounce.
    """
    state = load_debounce(trading_home, session_date)
    entries = set(state["entries"])
    exits = set(state["exits"])
    full = set(state["full_exits"])
    folder = trading_home / "intents" / session_date
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            if path.name.endswith(".tmp"):
                continue
            _collect_file(path, session_date, entries, exits, full)
    state["entries"] = sorted(entries)
    state["exits"] = sorted(exits)
    state["full_exits"] = sorted(full)
    atomic_write_json(debounce_path(trading_home), state)
    return state


def run_once(
    trading_home: Path,
    *,
    now: datetime | None = None,
    session_date: str | None = None,
    quotes: Mapping[str, float] | None = None,
    positions: list[Held] | None = None,
    write_intents: bool | None = None,
    write_exits: bool | None = None,
    bars_cache: Path | None = None,
    approved_dir: Path | None = None,
) -> dict[str, Any]:
    """One pass. Returns the report that was written to ``intraday_reeval.json``."""
    home = trading_home.expanduser().resolve()
    clock = _clock(now)
    session = session_date or clock.date().isoformat()
    try:
        assert_paper_account(home)
        parse_client_id()
    except PaperGateError as exc:
        report = _refused(session, clock, exc.reason)
        atomic_write_json(report_path(home), report)
        return report
    allow_entry = _flag_or_env(write_intents, "INTRADAY_REEVAL_WRITE_INTENTS")
    allow_exit = _flag_or_env(write_exits, "INTRADAY_REEVAL_WRITE_EXITS")
    account = _read_account(home)
    nlv = _nlv(account)
    risk = assess_files(*_risk_paths(home), now=clock)
    blocked = _entry_block(home, risk.entries_allowed)
    if blocked is None and nlv <= 0:
        blocked = "nlv_missing"
    state = note_session_intents(home, session)
    entry_seen = set(state["entries"])
    exit_seen = set(state["exits"])
    full_seen = set(state["full_exits"])
    pins = _pins(home)
    folder = approved_dir or _approved_dir(home)
    cache = bars_cache or (home / "data" / "bars_cache")
    watch = _load_watchlist(home)
    items_in = watch.get("items") if watch.get("session_date") == session else []
    if not isinstance(items_in, list):
        items_in = []
    held = positions if positions is not None else load_held(home)
    tickers = [str(row.get("ticker")) for row in items_in if isinstance(row, dict)]
    tickers.extend(row.ticker for row in held)
    if quotes is None:
        prices = fetch_quotes(sorted(set(tickers)), parse_client_id())
    else:
        prices = dict(quotes)
    bars = _load_cache(cache, sorted(set(tickers)))
    rates = load_rates(home, account if isinstance(account, dict) else None)
    queued: dict[tuple[str, int], list[Any]] = {}
    loaded: dict[tuple[str, int], tuple[dict[str, Any], str]] = {}
    reports: list[dict[str, Any]] = []
    for raw in items_in:
        if not isinstance(raw, dict):
            continue
        reports.append(
            _evaluate_entry(
                raw,
                session=session,
                clock=clock,
                prices=prices,
                bars=bars,
                pins=pins,
                folder=folder,
                nlv=nlv,
                odd_lot=risk.odd_lot_enabled,
                rates=rates,
                blocked=blocked,
                allow_write=allow_entry,
                entry_seen=entry_seen,
                queued=queued,
                loaded=loaded,
            )
        )
    exit_reports: list[dict[str, Any]] = []
    for position in held:
        exit_reports.append(
            _evaluate_exit(
                position,
                session=session,
                clock=clock,
                prices=prices,
                bars=bars,
                pins=pins,
                folder=folder,
                blocked_entries=blocked,
                allow_write=allow_exit,
                exit_seen=exit_seen,
                full_seen=full_seen,
                queued=queued,
                loaded=loaded,
            )
        )
    written_paths: list[str] = []
    for key, orders in queued.items():
        document, digest = loaded[key]
        path = _append_orders(home, document, digest, orders, session, clock)
        if path is not None:
            written_paths.append(path.relative_to(home).as_posix())
    if queued:
        note_session_intents(home, session)
    report = {
        "schema_version": REPORT_SCHEMA,
        "session_date": session,
        "evaluated_at": clock.isoformat(),
        "refused": False,
        "reason": None,
        "write_intents": allow_entry,
        "write_exits": allow_exit,
        "entries_blocked": blocked,
        "interval_sec": clamp_interval(),
        "client_id": parse_client_id(),
        "watchlist_items": len(items_in),
        "written": written_paths,
        "items": reports,
        "exits": exit_reports,
    }
    atomic_write_json(report_path(home), report)
    return report


def load_held(trading_home: Path) -> list[Held]:
    """Strategy lots from the journal, otherwise rows on ``account.json``."""
    db = trading_home / "data" / "trading.db"
    if db.is_file():
        try:
            from auto_trading.portfolio.journal import TradeJournal

            found = _held_from_rows(TradeJournal(trading_home).strategy_position_rows())
        except (OSError, ValueError, KeyError):
            log.warning("strategy positions unreadable")
            found = []
        if found:
            return found
    account = _read_account(trading_home)
    rows = account.get("positions") if isinstance(account, dict) else None
    if not isinstance(rows, list):
        return []
    return _held_from_rows(rows)


def fetch_quotes(tickers: list[str], client_id: int) -> dict[str, float]:
    """IBKR snapshot on ``client_id``, then the public last for anything missing."""
    found: dict[str, float] = {}
    rows: list[dict[str, Any]] = []
    if tickers:
        try:
            import asyncio

            from auto_trading.config import get_settings
            from auto_trading.execution.ibkr import PaperGateway

            gateway = PaperGateway(get_settings(), client_id=client_id)
            rows = list(asyncio.run(gateway.snapshot_quotes(tickers)))
        except Exception:
            log.warning("gateway quotes unavailable")
    for row in rows:
        symbol = str(row.get("symbol") or "")
        try:
            last = float(row["last"])
        except (KeyError, TypeError, ValueError):
            continue
        if symbol and last > 0:
            found[symbol] = last
    missing = [ticker for ticker in tickers if ticker not in found]
    if not missing:
        return found
    from auto_trading.market.quotes import yahoo_quote

    for ticker in missing:
        try:
            last = float(yahoo_quote(ticker).last)
        except Exception:
            log.warning("public quote failed ticker=%s", ticker)
            continue
        if last > 0:
            found[ticker] = last
    return found


def run_loop(trading_home: Path, *, sleep: Callable[[float], None] | None = None) -> None:
    """Sleep 1–5 minutes between passes. The timer unit is the one used on the VPS."""
    pause = sleep or time.sleep
    while True:
        try:
            run_once(trading_home)
        except Exception:
            log.exception("intraday reeval pass failed")
        pause(clamp_interval())


def main(argv: list[str] | None = None) -> int:
    """``python -m auto_trading.intraday --once``."""
    parser = argparse.ArgumentParser(prog="intraday-reeval")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--home", default="")
    parser.add_argument("--session-date", default="")
    args = parser.parse_args(argv)
    home = Path(args.home) if args.home else Path(os.environ.get("TRADING_HOME", "/srv/trading"))
    if args.loop and not args.once:
        run_loop(home)
        return 0
    report = run_once(home, session_date=args.session_date or None)
    if report.get("refused"):
        log.error("intraday reeval refused: %s", report.get("reason"))
        return 2
    return 0


def _evaluate_entry(
    raw: Mapping[str, Any],
    *,
    session: str,
    clock: datetime,
    prices: Mapping[str, float],
    bars: dict[str, list[Bar]],
    pins: dict[tuple[str, int], str],
    folder: Path,
    nlv: float,
    odd_lot: bool,
    rates: dict[str, float],
    blocked: str | None,
    allow_write: bool,
    entry_seen: set[str],
    queued: dict[tuple[str, int], list[Any]],
    loaded: dict[tuple[str, int], tuple[dict[str, Any], str]],
) -> dict[str, Any]:
    ticker = str(raw.get("ticker") or "")
    strategy_id = str(raw.get("strategy_id") or "")
    market = str(raw.get("market") or "")
    if not market and ticker:
        market = describe(ticker).market
    row: dict[str, Any] = {
        "strategy_id": strategy_id,
        "ticker": ticker,
        "kind": raw.get("kind"),
        "market_open": False,
        "confirm_pass": False,
        "would_entry": False,
        "debounced": False,
        "written": False,
        "reason": None,
        "rules": [],
    }
    if not ticker or not strategy_id:
        row["reason"] = "incomplete"
        return row
    if not _market_open(market, clock):
        row["reason"] = "market_closed"
        return row
    row["market_open"] = True
    last = prices.get(ticker)
    if last is None or last <= 0:
        row["reason"] = "quote_missing"
        return row
    prepared = _prepare_strategy(raw, pins, folder, loaded)
    if prepared is None:
        row["reason"] = "strategy_not_confirmed"
        return row
    document, _digest, key = prepared
    provisional = _with_provisional(bars, ticker, session, float(last), clock)
    history = [item for item in provisional.get(ticker, []) if item.date <= session]
    signal = _signal_from_row(raw)
    from auto_trading.backtest.engine import _entry_refs

    report = explain_confirm(document, _series(history), _entry_refs(signal, history))
    row["rules"] = report["rules"]
    row["confirm_pass"] = bool(report["passed"])
    row["would_entry"] = bool(report["passed"])
    if not report["passed"]:
        row["reason"] = reason_text(report)
        return row
    entry_key = f"{strategy_id}|{ticker}|{session}"
    if entry_key in entry_seen:
        row["debounced"] = True
        row["reason"] = "debounced"
        return row
    if blocked:
        row["reason"] = blocked
        return row
    if not allow_write:
        row["reason"] = "write_flag_off"
        return row
    try:
        orders = plan_session(
            document,
            provisional,
            [signal],
            session,
            nlv=nlv,
            rates_hkd_per_unit=rates,
            odd_lot_enabled=odd_lot,
            decision_time=clock.isoformat(),
            include_session_bar=True,
        )
    except UnknownCondition:
        row["reason"] = "unsupported_trigger"
        return row
    orders = [order for order in orders if order.ticker == ticker and order.kind == "ENTRY"]
    if not orders:
        row["reason"] = "nlv_missing" if nlv <= 0 else "no_order"
        return row
    entry_seen.add(entry_key)
    queued.setdefault(key, []).extend(orders)
    row["written"] = True
    row["reason"] = "written"
    return row


def _evaluate_exit(
    position: Held,
    *,
    session: str,
    clock: datetime,
    prices: Mapping[str, float],
    bars: dict[str, list[Bar]],
    pins: dict[tuple[str, int], str],
    folder: Path,
    blocked_entries: str | None,
    allow_write: bool,
    exit_seen: set[str],
    full_seen: set[str],
    queued: dict[tuple[str, int], list[Any]],
    loaded: dict[tuple[str, int], tuple[dict[str, Any], str]],
) -> dict[str, Any]:
    market = describe(position.ticker).market
    row: dict[str, Any] = {
        "strategy_id": position.strategy_id,
        "ticker": position.ticker,
        "market_open": _market_open(market, clock),
        "would_exit": False,
        "debounced": False,
        "written": False,
        "reason": None,
        "exit_reason": None,
        "entry_gate": blocked_entries,
    }
    if not row["market_open"]:
        row["reason"] = "market_closed"
        return row
    if position.qty <= 0 or position.avg_cost <= 0 or not position.parent_intent_id:
        row["reason"] = "position_incomplete"
        return row
    version = _version_for(pins, position.strategy_id)
    if version is None:
        row["reason"] = "strategy_not_confirmed"
        return row
    prepared = _prepare_strategy(
        {
            "strategy_id": position.strategy_id,
            "strategy_version": version,
            "spec_hash": pins[(position.strategy_id, version)],
        },
        pins,
        folder,
        loaded,
    )
    if prepared is None:
        row["reason"] = "strategy_not_confirmed"
        return row
    document, _digest, key = prepared
    last = prices.get(position.ticker)
    if last is None or last <= 0:
        row["reason"] = "quote_missing"
        return row
    full_key = f"{position.strategy_id}|{position.ticker}|{session}"
    provisional = _with_provisional(bars, position.ticker, session, float(last), clock)
    history = [item for item in provisional.get(position.ticker, []) if item.date <= session]
    if not history:
        row["reason"] = "bars_missing"
        return row
    highest = max((item.high for item in history), default=float(last))
    highest = max(highest, float(last), position.avg_cost)
    hold = 0
    if position.opened_on:
        hold = sum(1 for item in history if item.date > position.opened_on)
    from auto_trading.backtest.engine import _Pos

    pos = _Pos(
        qty=int(position.qty),
        avg_cost=position.avg_cost,
        entry_intent=position.parent_intent_id,
        stop=0.0,
        highest=highest,
        hold_sessions=hold,
        atr_at_entry=None,
        partial_taken=position.partial_taken,
    )
    order = _exit_order(
        document,
        position.ticker,
        pos,
        history[-1],
        session,
        [],
        provisional,
        RiskSimulator(hwm=1.0),
        False,
        [],
    )
    if order is None or order.qty <= 0:
        row["reason"] = "no_exit"
        return row
    order.intent_id = f"{session}-{position.ticker}-{order.kind}"
    row["would_exit"] = True
    row["exit_reason"] = order.kind
    exit_key = f"{full_key}|{order.kind}"
    if full_key in full_seen or exit_key in exit_seen:
        row["debounced"] = True
        row["reason"] = "debounced"
        return row
    if not allow_write:
        row["reason"] = "write_flag_off"
        return row
    exit_seen.add(exit_key)
    if order.kind != "indicator_take_profit":
        full_seen.add(full_key)
    queued.setdefault(key, []).append(order)
    row["written"] = True
    row["reason"] = "written"
    return row


def _prepare_strategy(
    raw: Mapping[str, Any],
    pins: dict[tuple[str, int], str],
    folder: Path,
    loaded: dict[tuple[str, int], tuple[dict[str, Any], str]],
) -> tuple[dict[str, Any], str, tuple[str, int]] | None:
    strategy_id = str(raw.get("strategy_id") or "")
    try:
        version = int(raw.get("strategy_version"))
    except (TypeError, ValueError):
        return None
    key = (strategy_id, version)
    if key in loaded:
        document, digest = loaded[key]
        return document, digest, key
    expected = pins.get(key)
    if expected is None:
        return None
    claimed = raw.get("spec_hash")
    if claimed not in {None, "", expected}:
        return None
    path = folder / f"{strategy_id}.v{version}.json"
    if not path.is_file():
        return None
    document = load_approved(path)
    if document.get("mode") != "paper" or "live_size_multiplier" in document:
        return None
    try:
        digest = require_approved(
            document,
            ApprovedStrategy(strategy_id=strategy_id, version=version, spec_hash=expected),
        )
    except StrategyNotApproved:
        return None
    loaded[key] = (document, digest)
    return document, digest, key


def _append_orders(
    home: Path,
    document: dict[str, Any],
    digest: str,
    orders: list[Any],
    session_date: str,
    now: datetime,
) -> Path | None:
    from auto_trading.intents.writer import intent_path, write_intents
    from auto_trading.strategy.engine import _intent, intent_document

    fresh: list[dict[str, Any]] = []
    for order in orders:
        if getattr(order, "qty", 0) <= 0:
            continue
        row = _intent(document, order, session_date)
        row["source"] = SOURCE
        fresh.append(row)
    if not fresh:
        return None
    path = intent_path(home, session_date, str(document["id"]), int(document["version"]))
    if path.is_file():
        previous = loads_strict(path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            return None
        existing = list(previous.get("intents") or [])
        seen = {item.get("intent_id") for item in existing if isinstance(item, dict)}
        added = [row for row in fresh if row.get("intent_id") not in seen]
        if not added:
            return None
        payload = dict(previous)
        payload["intents"] = existing + added
    else:
        payload = intent_document(document, digest, [], session_date)
        payload["intents"] = fresh
    payload["mode"] = "paper"
    payload["live_size_multiplier"] = None
    payload["last_reeval_at"] = now.isoformat()
    # True keeps ENTRY rows already in the file. The caller omits new ENTRY
    # rows when kill switch, pause, R9 halt, or the risk snapshot blocks them.
    return write_intents(home, payload, session_date=session_date, entries_allowed=True)


def _with_provisional(
    bars: dict[str, list[Bar]],
    ticker: str,
    session_date: str,
    last: float,
    now: datetime,
) -> dict[str, list[Bar]]:
    copied = {key: list(rows) for key, rows in bars.items()}
    rows = [row for row in copied.get(ticker, []) if row.date <= session_date]
    previous = rows[-1].close if rows else last
    bar = Bar(
        date=session_date,
        open=previous,
        high=max(previous, last),
        low=min(previous, last),
        close=last,
        volume=0.0,
        available_at=now.isoformat(),
        adj_close=last,
    )
    if rows and rows[-1].date == session_date:
        rows[-1] = bar
    else:
        rows.append(bar)
    copied[ticker] = rows
    return copied


def _signal_from_row(raw: Mapping[str, Any]) -> Any:
    from auto_trading.backtest.engine import BtSignal

    upside = raw.get("upside_pct")
    pre = raw.get("pre_event_close")
    return BtSignal(
        ticker=str(raw["ticker"]),
        side="BUY",
        confidence=int(raw.get("confidence") or 0),
        fair_value=float(raw.get("fair_value") or 0),
        signal_id=str(raw.get("signal_id") or ""),
        price_date=str(raw.get("price_date") or ""),
        upside_pct=None if upside is None else float(upside),
        event_source=None
        if raw.get("event_source") in {None, ""}
        else str(raw.get("event_source")),
        pre_event_close=None if pre is None else float(pre),
    )


def _held_from_rows(rows: list[Any]) -> list[Held]:
    found: list[Held] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or row.get("symbol") or "").strip()
        strategy_id = str(row.get("strategy_id") or "").strip()
        parent = str(row.get("parent_intent_id") or row.get("root_intent_id") or "").strip()
        try:
            qty = float(row.get("qty") if row.get("qty") is not None else row.get("quantity") or 0)
            avg = float(row.get("avg_cost") or 0)
        except (TypeError, ValueError):
            continue
        if not ticker or not strategy_id or strategy_id == _MANUAL or qty <= 0 or not parent:
            continue
        opened = str(row.get("opened_at_utc") or row.get("opened_on") or "")[:10] or None
        fair = row.get("fair_value")
        try:
            fair_value = None if fair is None else float(fair)
        except (TypeError, ValueError):
            fair_value = None
        found.append(
            Held(
                ticker=ticker,
                qty=qty,
                avg_cost=avg,
                strategy_id=strategy_id,
                parent_intent_id=parent,
                opened_on=opened,
                fair_value=fair_value,
                partial_taken=bool(row.get("partial_taken")),
            )
        )
    return found


def _collect_file(
    path: Path,
    session_date: str,
    entries: set[str],
    exits: set[str],
    full: set[str],
) -> None:
    try:
        payload = loads_strict(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    strategy_id = str(payload.get("strategy_id") or "")
    if not strategy_id:
        return
    for item in payload.get("intents") or []:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "")
        if not ticker:
            continue
        kind = item.get("intent_type")
        base = f"{strategy_id}|{ticker}|{session_date}"
        if kind in {"ENTRY", "ADD"}:
            entries.add(base)
        elif kind in {"EXIT", "REDUCE"}:
            reason = str(item.get("exit_reason") or kind)
            exits.add(f"{base}|{reason}")
            if kind == "EXIT":
                full.add(base)


def _market_open(market: str, now: datetime) -> bool:
    if not market or now.tzinfo is None:
        return False
    from auto_trading.execution.sessions import session_window

    try:
        window = session_window(market.upper(), now)
    except (KeyError, ValueError, TypeError):
        return False
    return window in OPEN_WINDOWS


def _entry_block(home: Path, entries_allowed: bool) -> str | None:
    if (home / "KILL_SWITCH").is_file():
        return "kill_switch"
    if (home / "state" / "paused").is_file():
        return "paused"
    try:
        from auto_trading.risk.r9_state import cycle_view

        view = cycle_view(home)
    except (OSError, ValueError):
        view = None
    if view is not None and view.get("state") == HALTED:
        return "r9_halt"
    if not entries_allowed:
        return "risk_limits"
    return None


def _version_for(pins: dict[tuple[str, int], str], strategy_id: str) -> int | None:
    versions = [version for sid, version in pins if sid == strategy_id]
    if not versions:
        return None
    return max(versions)


def _load_watchlist(home: Path) -> dict[str, Any]:
    path = watchlist_path(home)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_cache(cache: Path, tickers: list[str]) -> dict[str, list[Bar]]:
    found: dict[str, list[Bar]] = {}
    for ticker in tickers:
        path = cache_path(cache, ticker)
        if not path.is_file():
            continue
        try:
            raw_rows = read_parquet(path)
        except (OSError, ValueError):
            continue
        parsed = [_as_bar(row) for row in raw_rows or []]
        bars = [row for row in parsed if row is not None]
        if bars:
            found[ticker] = bars
    return found


def _as_bar(row: Mapping[str, Any]) -> Bar | None:
    day = row.get("date")
    close = row.get("close")
    if day is None or close is None:
        return None
    text = day.isoformat()[:10] if hasattr(day, "isoformat") else str(day)[:10]
    try:
        price = float(close)
    except (TypeError, ValueError):
        return None
    adj = row.get("adj_close")
    available = row.get("available_at")
    stamp = None
    if available not in {None, ""}:
        stamp = available.isoformat() if hasattr(available, "isoformat") else str(available)
    return Bar(
        date=text,
        open=_float(row.get("open"), price),
        high=_float(row.get("high"), price),
        low=_float(row.get("low"), price),
        close=price,
        volume=_float(row.get("volume"), 0.0),
        available_at=stamp,
        adj_close=None if adj is None else _float(adj, price),
    )


def _blank_debounce(session_date: str) -> dict[str, Any]:
    return {
        "schema_version": DEBOUNCE_SCHEMA,
        "session_date": session_date,
        "entries": [],
        "exits": [],
        "full_exits": [],
    }


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _refused(session: str, clock: datetime, reason: str) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA,
        "session_date": session,
        "evaluated_at": clock.isoformat(),
        "refused": True,
        "reason": reason,
        "write_intents": False,
        "write_exits": False,
        "entries_blocked": reason,
        "interval_sec": clamp_interval(),
        "written": [],
        "items": [],
        "exits": [],
    }


def _clock(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(MACAU)
    if now.tzinfo is None or now.utcoffset() is None:
        raise PaperGateError("clock_naive")
    return now


def _is_pinned_paper(account: str) -> bool:
    return account == PAPER_ACCOUNT and DU_ACCOUNT.fullmatch(account) is not None


def _read_account(home: Path) -> dict[str, Any] | None:
    path = home / "state" / "account.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _nlv(account: Mapping[str, Any] | None) -> float:
    if not isinstance(account, Mapping) or account.get("nlv") is None:
        return 0.0
    try:
        return float(account["nlv"])
    except (TypeError, ValueError):
        return 0.0


def _risk_paths(home: Path) -> tuple[Path, Path]:
    local = home / "config" / "risk_limits_expected.json"
    expected = local if local.is_file() else REPO_ROOT / "config" / "risk_limits_expected.json"
    return home / "state" / "risk_limits.json", expected


def _pins(home: Path) -> dict[tuple[str, int], str]:
    local = home / "config" / "confirmed_strategies.json"
    path = local if local.is_file() else REPO_ROOT / "config" / "confirmed_strategies.json"
    return load_confirmed_spec_hashes(path)


def _approved_dir(home: Path) -> Path:
    local = home / "strategies" / "approved"
    if local.is_dir():
        return local
    return REPO_ROOT / "strategies" / "approved"


def _float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
