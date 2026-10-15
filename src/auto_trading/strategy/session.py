"""Paper session runner for the existing 07:05 path.

``auto-trading run-session`` and ``scripts/run_paper_intents_once.py`` both
call :func:`run_session`. The daily shell and ``trading-strategy.timer`` stay
the only schedule. This module does not place orders.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from auto_trading.alerts.pending import build_alert, write_pending
from auto_trading.backtest.adr import load_adr_map
from auto_trading.backtest.allocator import (
    NAME_ROOM,
    PRICE_BUFFER,
    PRIORITY,
    Proposal,
    allocate,
)
from auto_trading.backtest.engine import Bar, BtSignal, plan_session
from auto_trading.execution.fx import load_rates
from auto_trading.execution.strategy_book import (
    REPO_ROOT,
    load_approved,
    load_confirmed_spec_hashes,
)
from auto_trading.intents.writer import write_intents
from auto_trading.market.instruments import describe
from auto_trading.market.yahoo import cache_path, load_daily_bars, read_parquet
from auto_trading.risk.limits import assess_files
from auto_trading.signals.errors import SignalFileError, SignalValidationError
from auto_trading.signals.loader import load_signals
from auto_trading.strategy.conditions import UnknownCondition
from auto_trading.strategy.engine import intent_document
from auto_trading.strategy.spec import ApprovedStrategy, StrategyNotApproved, require_approved

log = logging.getLogger(__name__)

MACAU = ZoneInfo("Asia/Macau")
PAPER_ACCOUNT = "DUR243736"
CONFIDENCE_FLOOR = 60
MARKET_TRIGGER = "market"
_USER_AGENT = "Mozilla/5.0 (compatible; paper-bars/1.0)"


class SessionError(RuntimeError):
    """The session cannot write intents."""


def market_trigger_supported() -> bool:
    """True when ``plan_session`` accepts ``trigger.type=market``."""
    try:
        plan_session({"trigger": {"type": MARKET_TRIGGER}}, {}, [], "2026-01-01")
    except UnknownCondition as exc:
        return exc.name != MARKET_TRIGGER
    return True


def run_session(
    *,
    trading_home: Path,
    session_date: str,
    signals_path: Path | None = None,
    bars_cache: Path | None = None,
    approved_dir: Path | None = None,
    entries_allowed: bool | None = None,
    now: datetime | None = None,
    refresh_bars: bool = True,
    bar_getter: Callable[[str], str] | None = None,
    sleep: Callable[[float], None] | None = None,
    pin_path: Path | None = None,
) -> dict[str, Any]:
    """Write one paper intent file per confirmed strategy that can run.

    A hash mismatch or an unsupported market trigger writes no file for that
    strategy. A bad risk snapshot or a missing NLV still writes the file, with
    ENTRY and ADD removed.
    """
    home = trading_home.expanduser().resolve()
    clock = _clock(now)
    _refuse_other_accounts(home)
    account = _read_account(home)
    nlv = _nlv(account)
    warnings: list[str] = []
    if nlv is None or nlv <= 0:
        warnings.append("nlv_missing")
        log.warning("account nlv missing at %s; refusing ENTRY", home / "state" / "account.json")
        nlv_for_plan = 0.0
        nlv_blocks = True
    else:
        nlv_for_plan = nlv
        nlv_blocks = False
    risk_path, expected_path = _risk_paths(home)
    status = assess_files(risk_path, expected_path, now=clock)
    allowed = status.entries_allowed and not nlv_blocks
    if entries_allowed is False:
        allowed = False
    elif entries_allowed is True and not allowed:
        log.warning("entries flag cannot override risk or nlv status %s", status.reason)
    if not status.entries_allowed:
        warnings.append(f"risk:{status.reason}")
        log.warning("risk_limits blocks ENTRY reason=%s", status.reason)
    signals = _load_signal_rows(home, signals_path, clock)
    positions = list(account.get("positions") or []) if isinstance(account, dict) else []
    _alert_flat_sells(home, signals, positions, session_date, clock)
    cache = bars_cache or (home / "data" / "bars_cache")
    bars = _load_bars(
        [row.ticker for row in signals if row.side == "BUY" and not row.expired],
        cache,
        refresh=refresh_bars,
        getter=bar_getter,
        sleep=sleep,
    )
    usable = _drop_missing_bars(signals, bars, warnings)
    priority, price_divisor, name_room = _allocator_rules(home)
    adr_map = _adr_map(home)
    rates = load_rates(home, account if isinstance(account, dict) else None)
    market_ok = market_trigger_supported()
    folder = approved_dir or _approved_dir(home)
    pins = load_confirmed_spec_hashes(_pin_path(home, pin_path))
    runnable: list[tuple[dict[str, Any], str, list[Any]]] = []
    skipped: list[dict[str, str]] = []
    ordered = sorted(pins, key=lambda item: _priority_key(item[0], priority))
    for strategy_id, version in ordered:
        outcome = _prepare_strategy(
            home,
            folder,
            strategy_id,
            version,
            pins[(strategy_id, version)],
            session_date,
            clock,
            usable,
            bars,
            nlv_for_plan,
            rates,
            status.odd_lot_enabled,
            adr_map,
            market_ok,
        )
        if outcome is None:
            continue
        if isinstance(outcome, str):
            skipped.append({"strategy_id": strategy_id, "reason": outcome})
            continue
        runnable.append(outcome)
    kept = _allocate(
        runnable,
        nlv=nlv_for_plan,
        priority=priority,
        price_divisor=price_divisor,
        name_room=name_room,
        adr_map=adr_map,
        rates=rates,
    )
    written: list[str] = []
    for document, digest, orders in kept:
        payload = intent_document(document, digest, orders, session_date)
        payload["mode"] = "paper"
        path = write_intents(home, payload, session_date=session_date, entries_allowed=allowed)
        written.append(path.relative_to(home).as_posix())
    return {
        "session_date": session_date,
        "entries_allowed": allowed,
        "nlv": None if nlv is None else nlv,
        "risk_reason": status.reason,
        "written": written,
        "skipped": skipped,
        "warnings": warnings,
    }


def _clock(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(MACAU)
    if now.tzinfo is None or now.utcoffset() is None:
        raise SessionError("now must be timezone-aware")
    return now


def _refuse_other_accounts(home: Path) -> None:
    pin = os.environ.get("IBKR_PAPER_ACCOUNT", "").strip()
    if pin and pin != PAPER_ACCOUNT:
        raise SessionError(f"account refused: {pin}")
    account = _read_account(home)
    named = str(account.get("account") or "") if isinstance(account, dict) else ""
    if named and named != PAPER_ACCOUNT:
        raise SessionError(f"account refused: {named}")


def _read_account(home: Path) -> dict[str, Any] | None:
    path = home / "state" / "account.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("account.json unreadable: %s", type(exc).__name__)
        return None
    return payload if isinstance(payload, dict) else None


def _nlv(account: Mapping[str, Any] | None) -> float | None:
    if not isinstance(account, Mapping) or "nlv" not in account or account.get("nlv") is None:
        return None
    try:
        return float(account["nlv"])
    except (TypeError, ValueError):
        return None


def _risk_paths(home: Path) -> tuple[Path, Path]:
    local = home / "config" / "risk_limits_expected.json"
    expected = local if local.is_file() else REPO_ROOT / "config" / "risk_limits_expected.json"
    return home / "state" / "risk_limits.json", expected


def _pin_path(home: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    local = home / "config" / "confirmed_strategies.json"
    if local.is_file():
        return local
    return REPO_ROOT / "config" / "confirmed_strategies.json"


def _approved_dir(home: Path) -> Path:
    local = home / "strategies" / "approved"
    if local.is_dir():
        return local
    return REPO_ROOT / "strategies" / "approved"


def _allocator_rules(home: Path) -> tuple[tuple[str, ...], float, float]:
    local = home / "config" / "allocator.yaml"
    path = local if local.is_file() else REPO_ROOT / "config" / "allocator.yaml"
    if not path.is_file():
        return PRIORITY, PRICE_BUFFER, NAME_ROOM
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return PRIORITY, PRICE_BUFFER, NAME_ROOM
    listed = raw.get("priority") or PRIORITY
    priority = tuple(str(item) for item in listed)
    buffer = float(raw.get("price_buffer", PRICE_BUFFER - 1))
    room = float(raw.get("name_room_pct", NAME_ROOM))
    return priority, 1.0 + buffer, room


def _adr_map(home: Path) -> list[Any]:
    local = home / "config" / "adr_map.csv"
    path = local if local.is_file() else REPO_ROOT / "config" / "adr_map.csv"
    if not path.is_file():
        return []
    try:
        return load_adr_map(path)
    except (OSError, ValueError, KeyError) as exc:
        log.warning("adr map skipped: %s", type(exc).__name__)
        return []


def _resolve_signals(home: Path, signals_path: Path | None) -> Path:
    if signals_path is not None:
        return signals_path
    env = os.environ.get("SIGNALS_PATH", "").strip()
    if env:
        return Path(env)
    return home / "signals.json"


def _load_signal_rows(home: Path, signals_path: Path | None, now: datetime) -> list[Any]:
    path = _resolve_signals(home, signals_path)
    try:
        document = load_signals(path, now=now)
    except (SignalFileError, SignalValidationError) as exc:
        raise SessionError(str(exc)) from exc
    return list(document.signals)


def _alert_flat_sells(
    home: Path,
    signals: list[Any],
    positions: list[Any],
    session_date: str,
    now: datetime,
) -> None:
    for signal in signals:
        side = _side(signal)
        if signal.expired or side != "SELL":
            continue
        if _position_qty(positions, signal.ticker) > 0:
            log.info("sell_with_position ticker=%s; no short", signal.ticker)
            continue
        log.warning("signal_sell_no_position ticker=%s", signal.ticker)
        _pending(
            home,
            rule_id="signal_sell_no_position",
            ticker=signal.ticker,
            message="有賣出訊號但沒有持倉，不開空倉。",
            session_date=session_date,
            now=now,
            direction="SELL",
        )


def _load_bars(
    tickers: list[str],
    cache: Path,
    *,
    refresh: bool,
    getter: Callable[[str], str] | None,
    sleep: Callable[[float], None] | None,
) -> dict[str, list[Bar]]:
    pause = sleep or time.sleep
    found: dict[str, list[Bar]] = {}
    for ticker in sorted(set(tickers)):
        path = cache_path(cache, ticker)
        rows: list[dict[str, Any]] | None = None
        downloaded = False
        if path.is_file():
            rows = read_parquet(path)
        elif refresh:
            try:
                rows = load_daily_bars(
                    ticker,
                    cache_dir=cache,
                    getter=getter or _yahoo_get,
                    sleep=pause,
                )
                downloaded = True
            except Exception as exc:
                log.warning("bars_refresh_failed ticker=%s error=%s", ticker, type(exc).__name__)
                rows = None
        parsed = [_as_bar(row) for row in rows or []]
        bars = [row for row in parsed if row is not None]
        if bars:
            found[ticker] = bars
        if downloaded:
            pause(0.5)
    return found


def _drop_missing_bars(
    signals: list[Any],
    bars: dict[str, list[Bar]],
    warnings: list[str],
) -> list[BtSignal]:
    usable: list[BtSignal] = []
    for signal in signals:
        if signal.expired:
            log.info("signal_expired id=%s", signal.signal_id)
            continue
        if _side(signal) == "BUY" and signal.ticker not in bars:
            warnings.append(f"bars_missing:{signal.ticker}")
            log.warning("bars_missing ticker=%s", signal.ticker)
            continue
        if _side(signal) == "BUY":
            usable.append(_bt(signal))
    return usable


def _prepare_strategy(
    home: Path,
    folder: Path,
    strategy_id: str,
    version: int,
    digest: str,
    session_date: str,
    now: datetime,
    signals: list[BtSignal],
    bars: dict[str, list[Bar]],
    nlv: float,
    rates: dict[str, float],
    odd_lot: bool,
    adr_map: list[Any],
    market_ok: bool,
) -> tuple[dict[str, Any], str, list[Any]] | str | None:
    path = folder / f"{strategy_id}.v{version}.json"
    if not path.is_file():
        log.warning("approved file missing %s", path.name)
        return "approved_missing"
    document = load_approved(path)
    mode = document.get("mode", "paper")
    if mode == "disabled":
        log.info("skip disabled strategy %s", strategy_id)
        return "disabled"
    if mode != "paper" or "live_size_multiplier" in document:
        _pending(
            home,
            rule_id="paper_only",
            ticker=strategy_id,
            message="策略檔必須是模擬模式，且不得帶乘數鍵。",
            session_date=session_date,
            now=now,
        )
        return "paper_only"
    try:
        actual = require_approved(
            document,
            ApprovedStrategy(strategy_id=strategy_id, version=version, spec_hash=digest),
        )
    except StrategyNotApproved:
        log.warning("strategy_hash_mismatch id=%s", strategy_id)
        _pending(
            home,
            rule_id="strategy_hash_mismatch",
            ticker=strategy_id,
            message="策略雜湊與確認檔不符，不寫意圖。",
            session_date=session_date,
            now=now,
        )
        return "strategy_hash_mismatch"
    trigger = document.get("trigger") if isinstance(document.get("trigger"), dict) else {}
    kind = trigger.get("type")
    if kind == MARKET_TRIGGER and not market_ok:
        log.warning(
            "skip %s trigger.type=market is not supported by plan_session",
            strategy_id,
        )
        _pending(
            home,
            rule_id="unsupported_market_trigger",
            ticker=strategy_id,
            message="market trigger 仍未接入，本輪略過，不當作成功。",
            session_date=session_date,
            now=now,
        )
        return "unsupported_market_trigger"
    chosen = _entry_signals(signals, document)
    try:
        orders = plan_session(
            dict(document),
            bars,
            chosen,
            session_date,
            nlv=nlv,
            rates_hkd_per_unit=rates,
            odd_lot_enabled=odd_lot,
            adr_map=adr_map,
            events=[],
        )
    except UnknownCondition as exc:
        log.warning("skip %s unknown condition %s", strategy_id, exc.name)
        _pending(
            home,
            rule_id="unsupported_market_trigger",
            ticker=strategy_id,
            message=f"條件 {exc.name} 仍未接入，本輪略過，不當作成功。",
            session_date=session_date,
            now=now,
        )
        return "unsupported_trigger"
    return document, actual, orders


def _entry_signals(signals: list[BtSignal], document: Mapping[str, Any]) -> list[BtSignal]:
    trigger = document.get("trigger") if isinstance(document.get("trigger"), dict) else {}
    exempt = document.get("id") == "ta_breakout52" or trigger.get("type") == MARKET_TRIGGER
    floor = 0 if exempt else CONFIDENCE_FLOOR
    declared = trigger.get("min_confidence", floor)
    minimum = max(int(declared), floor)
    kept: list[BtSignal] = []
    for signal in signals:
        if signal.side != "BUY":
            continue
        if signal.confidence < minimum:
            log.info(
                "confidence_rejected ticker=%s confidence=%s minimum=%s strategy=%s",
                signal.ticker,
                signal.confidence,
                minimum,
                document.get("id"),
            )
            continue
        kept.append(signal)
    return kept


def _allocate(
    runnable: list[tuple[dict[str, Any], str, list[Any]]],
    *,
    nlv: float,
    priority: tuple[str, ...],
    price_divisor: float,
    name_room: float,
    adr_map: list[Any],
    rates: Mapping[str, float],
) -> list[tuple[dict[str, Any], str, list[Any]]]:
    opening = {"ENTRY", "ADD"}
    if nlv <= 0:
        return [
            (document, digest, [order for order in orders if order.kind not in opening])
            for document, digest, orders in runnable
        ]
    proposals: list[Proposal] = []
    owners: dict[int, tuple[int, Any]] = {}
    for index, (document, _digest, orders) in enumerate(runnable):
        for order in orders:
            if order.kind not in opening:
                continue
            spec = describe(order.ticker)
            price = float(order.ref_price or 0)
            fx = float(rates.get(spec.currency) or 0)
            if price <= 0 or fx <= 0:
                log.warning("skip unpriced order ticker=%s", order.ticker)
                continue
            proposal = Proposal(
                strategy_id=str(document.get("id")),
                ticker=order.ticker,
                issuer_group=_issuer(order.ticker, adr_map),
                kind=order.kind,
                budget=max(order.qty, 0) * price * fx,
                price_local=price,
                fx_to_base=fx,
                market=spec.market,
                lot_size=spec.lot_size,
                qty=order.qty,
            )
            proposals.append(proposal)
            owners[id(proposal)] = (index, order)
    kept: dict[int, list[Any]] = {
        index: [order for order in orders if order.kind not in opening]
        for index, (_document, _digest, orders) in enumerate(runnable)
    }
    if not proposals:
        return [
            (document, digest, kept[index])
            for index, (document, digest, _orders) in enumerate(runnable)
        ]
    allocated = allocate(
        proposals,
        nlv=nlv,
        priority=priority,
        price_divisor=price_divisor,
        name_room=name_room,
    )
    for row in allocated:
        if row.status != "ok" or row.qty <= 0:
            continue
        found = owners.get(id(row.proposal))
        if found is None:
            log.warning("allocator dropped an unmatched proposal ticker=%s", row.proposal.ticker)
            continue
        index, order = found
        order.qty = row.qty
        kept[index].append(order)
    return [
        (document, digest, kept[index])
        for index, (document, digest, _orders) in enumerate(runnable)
    ]


def _issuer(ticker: str, adr_map: list[Any]) -> str:
    for row in adr_map:
        if row.local_ticker == ticker or row.adr_ticker == ticker:
            return str(row.issuer_group)
    return ticker


def _priority_key(strategy_id: str, priority: tuple[str, ...]) -> tuple[int, str]:
    try:
        return priority.index(strategy_id), strategy_id
    except ValueError:
        return len(priority), strategy_id


def _pending(
    home: Path,
    *,
    rule_id: str,
    ticker: str,
    message: str,
    session_date: str,
    now: datetime,
    direction: str = "na",
) -> None:
    alert = build_alert(
        rule_id=rule_id,
        alert_type=rule_id,
        severity="critical" if rule_id == "paper_only" else "warning",
        ticker=ticker,
        message_zh=message,
        trading_date=session_date,
        direction=direction,
        created_at=now,
    )
    write_pending(home, alert, filename_stamp=session_date.replace("-", ""))


def _position_qty(positions: list[Any], ticker: str) -> float:
    spec = describe(ticker)
    names = {ticker, spec.ib_symbol, spec.ib_symbol.lstrip("0")}
    total = 0.0
    for row in positions:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("ticker") or row.get("symbol") or "")
        if symbol not in names and symbol.lstrip("0") not in names:
            continue
        try:
            total += float(
                row.get("qty") if row.get("qty") is not None else row.get("quantity") or 0
            )
        except (TypeError, ValueError):
            continue
    return total


def _side(signal: Any) -> str:
    side = signal.side
    return side.value if hasattr(side, "value") else str(side)


def _bt(signal: Any) -> BtSignal:
    return BtSignal(
        ticker=signal.ticker,
        side=_side(signal),
        confidence=int(signal.confidence),
        fair_value=float(signal.fair_value),
        signal_id=signal.signal_id,
        price_date=signal.price_date.isoformat(),
        upside_pct=float(signal.upside_pct),
    )


def _as_bar(row: Mapping[str, Any]) -> Bar | None:
    day = _day(row.get("date"))
    close = row.get("close")
    if not day or close is None:
        return None
    try:
        price = float(close)
    except (TypeError, ValueError):
        return None
    adj = row.get("adj_close")
    available = row.get("available_at")
    return Bar(
        date=day,
        open=_float(row.get("open"), price),
        high=_float(row.get("high"), price),
        low=_float(row.get("low"), price),
        close=price,
        volume=_float(row.get("volume"), 0.0),
        available_at=None if available in {None, ""} else _stamp(available),
        adj_close=None if adj is None else _float(adj, price),
    )


def _day(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return str(value.isoformat())[:10]
    return str(value)[:10]


def _stamp(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _yahoo_get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")
