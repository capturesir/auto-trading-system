"""Tick multiples, quote receipt time, and which gate rejects stay queued."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import types
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.config import get_settings
from auto_trading.execution.ibkr import PaperGateway
from auto_trading.execution.pricing import quantize_to_tick
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.execution.services import (
    _PERMANENT_REJECTS,
    _REDUCING_BLOCK_ALERT_AFTER,
    _refresh_quotes,
    account_once,
    submit_once,
)
from auto_trading.execution.strategy_book import CONFIRMED_SPEC_HASHES
from auto_trading.market.quotes import write_quotes
from auto_trading.market.ticks import tick_size
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.risk.gate import GateContext, Quote, RiskGate, RiskRejected

PIN = "DUR243736"
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 2, 22, 0, tzinfo=ZoneInfo("Asia/Macau"))
DIGEST = CONFIRMED_SPEC_HASHES[("ta_breakout52", 1)]


def _on_tick(market: str, price: float) -> None:
    tick = Decimal(str(tick_size(market, price)))
    amount = Decimal(str(price))
    assert amount % tick == 0


def test_quantize_to_tick_rounds_to_the_band_multiple() -> None:
    assert quantize_to_tick("HK", 400.3, buy=True) == 400.2
    assert quantize_to_tick("HK", 400.3, buy=False) == 400.4
    _on_tick("HK", quantize_to_tick("HK", 400.3, buy=True))
    _on_tick("HK", quantize_to_tick("HK", 400.3, buy=False))
    assert repr(quantize_to_tick("HK", 400 * 1.001, buy=True)) == "400.4"
    assert quantize_to_tick("HK", 200.05, buy=True) == 200.0
    assert quantize_to_tick("HK", 200.05, buy=False) == 200.2
    assert quantize_to_tick("US", 100.016, buy=True) == 100.01
    assert quantize_to_tick("US", 100.016, buy=False) == 100.02
    _on_tick("US", quantize_to_tick("US", 100.016, buy=False))
    assert quantize_to_tick("JP", 3000.0, buy=True) == 3000.0
    assert quantize_to_tick("JP", 3000.4, buy=True) == 3000.0
    assert quantize_to_tick("JP", 3000.4, buy=False) == 3005.0
    assert quantize_to_tick("JP", 2999.2, buy=False) == 3000.0
    assert quantize_to_tick("KR", 2000.0, buy=True) == 2000.0
    assert quantize_to_tick("KR", 2000.4, buy=True) == 2000.0
    assert quantize_to_tick("KR", 2000.4, buy=False) == 2005.0
    assert quantize_to_tick("KR", 1999.2, buy=False) == 2000.0
    _on_tick("JP", quantize_to_tick("JP", 3000.4, buy=False))
    _on_tick("KR", quantize_to_tick("KR", 2000.4, buy=False))


def _ready(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    at: datetime | None = None,
    positions: list[dict[str, object]] | None = None,
) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    moment = at or NOW
    write_risk_limits(home, build_risk_limits(now=moment, nlv=1_000_000, account=PIN))
    account_once(
        home,
        moment,
        snapshot={
            "accounts": [PIN],
            "nlv": 1_000_000.0,
            "positions": positions or [],
            "open_orders": [],
        },
    )
    if not (home / "strategies").exists():
        shutil.copytree(ROOT / "strategies", home / "strategies")


def _quote(home: Path, when: datetime, symbol: str, *, last: float, age: timedelta) -> None:
    received = when - age
    write_quotes(
        home,
        when,
        [
            {
                "symbol": symbol,
                "last": last,
                "as_of": (received - timedelta(minutes=15)).isoformat(),
                "received_at": received.isoformat(),
                "source": "ibkr",
                "data_type": "delayed",
                "market_data_type": 3,
            }
        ],
    )


def _write_opening(
    home: Path,
    name: str,
    *,
    valid_from: str | None,
    qty: int = 10,
) -> None:
    row: dict[str, object] = {
        "intent_id": name,
        "intent_type": "ENTRY",
        "ticker": "AAPL",
        "market": "US",
        "currency": "USD",
        "side": "BUY",
        "qty": qty,
        "limit_price": 100,
        "strategy_id": "ta_breakout52",
    }
    if valid_from is not None:
        row["valid_from"] = valid_from
    _write_row(home, name, row)


def _write_row(home: Path, name: str, row: dict[str, object]) -> None:
    envelope = {
        "schema_version": "intent/1.0",
        "strategy_id": "ta_breakout52",
        "strategy_version": 1,
        "spec_hash": DIGEST,
        "mode": "paper",
        "intents": [row],
    }
    folder = home / "intents"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps(envelope), encoding="utf-8")


def _hk_row(name: str, **over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "intent_id": name,
        "intent_type": "ENTRY",
        "ticker": "0700.HK",
        "market": "HK",
        "currency": "HKD",
        "side": "BUY",
        "qty": 100,
        "lot_size": 100,
        "limit_price": 400,
        "valid_from": "2026-10-13T09:30:00+08:00",
        "strategy_id": "ta_breakout52",
    }
    row.update(over)
    return row


def _blocked_alerts(home: Path) -> int:
    count = 0
    for path in (home / "logs").glob("critical_*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if '"kind": "reducing_order_blocked"' in line:
                count += 1
    return count


def _restore(home: Path, name: str) -> None:
    done = home / "intents" / "done" / f"{name}.json"
    (home / "intents" / f"{name}.json").write_bytes(done.read_bytes())


def test_permanent_reject_codes_are_an_explicit_allowlist() -> None:
    assert (
        frozenset(
            {
                "INTENT_EXPIRED",
                "LIVE_NOT_AUTHORIZED",
                "LOT_TOO_LARGE",
                "R0_ACCOUNTS_CHANGED",
                "R0_FORBIDDEN_PORT",
                "R0_NOT_PAPER_ACCOUNT",
                "R1",
                "R2",
                "R3",
                "R4",
                "R5",
                "R6",
                "R25",
            }
        )
        == _PERMANENT_REJECTS
    )
    assert "R7" not in _PERMANENT_REJECTS
    assert "R9_HALF_SIZE_LIMIT" not in _PERMANENT_REJECTS
    assert "R10" not in _PERMANENT_REJECTS
    assert "R22" not in _PERMANENT_REJECTS
    assert "R17" not in _PERMANENT_REJECTS
    assert "ENTRIES_PAUSED" not in _PERMANENT_REJECTS


def test_r10_entry_stays_pending_and_is_sent_after_the_open_buffer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    early = datetime(2026, 10, 13, 9, 30, 10, tzinfo=ZoneInfo("Asia/Macau"))
    later = datetime(2026, 10, 13, 9, 40, tzinfo=ZoneInfo("Asia/Macau"))
    _ready(tmp_path, monkeypatch, at=early)
    _quote(tmp_path, early, "0700.HK", last=400.0, age=timedelta(seconds=1))
    _write_row(tmp_path, "s1_open_at_0930", _hk_row("s1_open_at_0930"))
    sent: list[OrderIntent] = []
    first = submit_once(tmp_path, early, place=sent.append, readonly=False)
    assert sent == []
    assert first["rejected"] == ["R10"]
    assert (tmp_path / "intents" / "s1_open_at_0930.json").is_file()
    assert TradeJournal(tmp_path).latest_order_state("s1_open_at_0930") is None
    account_once(
        tmp_path,
        later,
        snapshot={"accounts": [PIN], "nlv": 1_000_000.0, "positions": [], "open_orders": []},
    )
    _quote(tmp_path, later, "0700.HK", last=400.0, age=timedelta(seconds=1))
    second = submit_once(tmp_path, later, place=sent.append, readonly=False)
    assert second["sent"] == 1
    assert [item.intent_id for item in sent] == ["s1_open_at_0930"]
    sent.clear()
    third = submit_once(tmp_path, later, place=sent.append, readonly=False)
    assert sent == []
    assert third["sent"] == 0


def test_r22_exit_stays_pending_and_is_resent_after_the_quote_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moment = datetime(2026, 10, 13, 10, 5, tzinfo=ZoneInfo("Asia/Macau"))
    held = [{"symbol": "0700.HK", "qty": 100, "strategy_id": "ta_breakout52"}]
    _ready(tmp_path, monkeypatch, at=moment, positions=held)
    _quote(tmp_path, moment, "0700.HK", last=400.0, age=timedelta(seconds=11))
    _write_row(
        tmp_path,
        "s2_exit_stale_quote",
        _hk_row(
            "s2_exit_stale_quote",
            intent_type="EXIT",
            side="SELL",
            parent_intent_id="entry-1",
        ),
    )
    sent: list[OrderIntent] = []
    first = submit_once(tmp_path, moment, place=sent.append, readonly=False)
    assert sent == []
    assert first["rejected"] == ["R22"]
    assert (tmp_path / "intents" / "s2_exit_stale_quote.json").is_file()
    assert _blocked_alerts(tmp_path) == 0
    for _ in range(_REDUCING_BLOCK_ALERT_AFTER - 1):
        submit_once(tmp_path, moment, place=sent.append, readonly=False)
    assert sent == []
    assert (tmp_path / "intents" / "s2_exit_stale_quote.json").is_file()
    assert _blocked_alerts(tmp_path) == 1
    submit_once(tmp_path, moment, place=sent.append, readonly=False)
    assert _blocked_alerts(tmp_path) == 1
    recovered = moment + timedelta(minutes=1)
    account_once(
        tmp_path,
        recovered,
        snapshot={"accounts": [PIN], "nlv": 1_000_000.0, "positions": held, "open_orders": []},
    )
    _quote(tmp_path, recovered, "0700.HK", last=400.0, age=timedelta(seconds=1))
    second = submit_once(tmp_path, recovered, place=sent.append, readonly=False)
    assert second["sent"] == 1
    assert [item.intent_id for item in sent] == ["s2_exit_stale_quote"]
    sent.clear()
    again = submit_once(tmp_path, recovered, place=sent.append, readonly=False)
    assert sent == []
    assert again["sent"] == 0


def test_entries_paused_opening_is_resent_after_unpause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moment = datetime(2026, 10, 13, 10, 10, tzinfo=ZoneInfo("Asia/Macau"))
    _ready(tmp_path, monkeypatch, at=moment)
    _quote(tmp_path, moment, "0700.HK", last=400.0, age=timedelta(seconds=1))
    (tmp_path / "state" / "entry_pause.json").write_text(
        json.dumps({"watchdog": True}),
        encoding="utf-8",
    )
    _write_row(tmp_path, "s3_open_during_pause", _hk_row("s3_open_during_pause"))
    sent: list[OrderIntent] = []
    first = submit_once(tmp_path, moment, place=sent.append, readonly=False)
    assert sent == []
    assert first["rejected"] == ["ENTRIES_PAUSED"]
    assert (tmp_path / "intents" / "s3_open_during_pause.json").is_file()
    assert TradeJournal(tmp_path).latest_order_state("s3_open_during_pause") is None
    (tmp_path / "state" / "entry_pause.json").write_text(
        json.dumps({"watchdog": False}),
        encoding="utf-8",
    )
    later = moment + timedelta(minutes=1)
    account_once(
        tmp_path,
        later,
        snapshot={"accounts": [PIN], "nlv": 1_000_000.0, "positions": [], "open_orders": []},
    )
    _quote(tmp_path, later, "0700.HK", last=400.0, age=timedelta(seconds=1))
    second = submit_once(tmp_path, later, place=sent.append, readonly=False)
    assert second["sent"] == 1
    assert [item.intent_id for item in sent] == ["s3_open_during_pause"]


def test_r17_protective_stop_is_resent_after_the_account_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moment = datetime(2026, 10, 13, 10, 15, tzinfo=ZoneInfo("Asia/Macau"))
    held = [{"symbol": "0700.HK", "qty": 100, "strategy_id": "ta_breakout52"}]
    _ready(tmp_path, monkeypatch, at=moment - timedelta(seconds=120), positions=held)
    _quote(tmp_path, moment, "0700.HK", last=400.0, age=timedelta(seconds=1))
    _write_row(
        tmp_path,
        "s4_stop_stale_account",
        _hk_row(
            "s4_stop_stale_account",
            intent_type="EXIT",
            side="SELL",
            protective=True,
            parent_intent_id="entry-1",
        ),
    )
    sent: list[OrderIntent] = []
    first = submit_once(tmp_path, moment, place=sent.append, readonly=False)
    assert sent == []
    assert first["rejected"] == ["R17"]
    assert (tmp_path / "intents" / "s4_stop_stale_account.json").is_file()
    assert TradeJournal(tmp_path).latest_order_state("s4_stop_stale_account") is None
    later = moment + timedelta(minutes=1)
    account_once(
        tmp_path,
        later,
        snapshot={"accounts": [PIN], "nlv": 1_000_000.0, "positions": held, "open_orders": []},
    )
    second = submit_once(tmp_path, later, place=sent.append, readonly=False)
    assert second["sent"] == 1
    assert [item.intent_id for item in sent] == ["s4_stop_stale_account"]
    assert sent[0].protective is True


def test_permanent_size_reject_is_never_retried_and_never_reaches_the_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(tmp_path, monkeypatch)
    _quote(tmp_path, NOW, "AAPL", last=100.0, age=timedelta(seconds=1))
    _write_opening(tmp_path, "too-big", valid_from="2026-10-02T21:30:00+08:00", qty=600)
    sent: list[OrderIntent] = []
    first = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
    assert sent == []
    assert first["rejected"] == ["R1"]
    assert not (tmp_path / "intents" / "too-big.json").exists()
    assert TradeJournal(tmp_path).latest_order_state("too-big") == "RISK_REJECTED"
    _restore(tmp_path, "too-big")
    again = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
    assert sent == []
    assert again["rejected"] == []
    assert again["gated"] == []


def test_missing_valid_from_is_terminal_before_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(tmp_path, monkeypatch)
    _write_opening(tmp_path, "no-from", valid_from=None)
    sent: list[OrderIntent] = []
    first = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
    assert sent == []
    assert first["rejected"] == ["INTENT_EXPIRED"]
    assert first["gated"] == []
    assert not (tmp_path / "intents" / "no-from.json").exists()
    assert TradeJournal(tmp_path).latest_order_state("no-from") == "INTENT_REJECTED"
    _restore(tmp_path, "no-from")
    again = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
    assert sent == []
    assert again["rejected"] == []
    assert again["gated"] == []


def test_receipt_time_is_the_tick_callback_not_the_end_of_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrived_at = NOW - timedelta(seconds=11)
    clock = {"now": arrived_at}
    holder: dict[str, object] = {}

    class Event:
        def __init__(self) -> None:
            self.handlers: list = []

        def __iadd__(self, handler: object) -> Event:
            self.handlers.append(handler)
            return self

        def __isub__(self, handler: object) -> Event:
            if handler in self.handlers:
                self.handlers.remove(handler)
            return self

        def emit(self) -> None:
            for handler in list(self.handlers):
                handler()

    class Ticker:
        def __init__(self) -> None:
            self.last = float("nan")
            self.marketDataType = 1
            self.lastTimestamp = None
            self.updateEvent = Event()

    class Contract:
        def __init__(self, symbol: str, exchange: str, currency: str) -> None:
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency

    class IB:
        async def connectAsync(self, host: str, port: int, clientId: int) -> None:
            return None

        def managedAccounts(self) -> list[str]:
            return [PIN]

        def reqMarketDataType(self, _kind: int) -> None:
            return None

        async def qualifyContractsAsync(self, contract: Contract) -> list[Contract]:
            contract.conId = 1
            return [contract]

        def reqMktData(self, _contract: Contract, *_args: object) -> Ticker:
            ticker = Ticker()
            holder["ticker"] = ticker
            return ticker

        def cancelMktData(self, _contract: Contract) -> None:
            return None

        def disconnect(self) -> None:
            return None

    fake = types.ModuleType("ib_async")
    fake.IB = IB
    fake.Stock = Contract
    monkeypatch.setitem(sys.modules, "ib_async", fake)

    import auto_trading.execution.ibkr as ibkr

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            current = clock["now"]
            if tz is None:
                return current.replace(tzinfo=None)
            return current.astimezone(tz)

    async def pause(_seconds: float) -> None:
        ticker = holder["ticker"]
        assert isinstance(ticker, Ticker)
        if "arrived" not in holder:
            ticker.last = 100.0
            ticker.lastTimestamp = clock["now"] - timedelta(seconds=1)
            holder["arrived"] = clock["now"]
            ticker.updateEvent.emit()
            clock["now"] = clock["now"] + timedelta(seconds=2)

    monkeypatch.setattr(ibkr, "datetime", FakeDT)
    monkeypatch.setattr(ibkr.asyncio, "sleep", pause)
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    monkeypatch.setenv("IB_PORT", "4002")
    monkeypatch.setenv("IB_CLIENT_ID", "11")
    monkeypatch.setenv("TRADING_HOME", str(tmp_path))
    get_settings.cache_clear()
    try:
        rows = asyncio.run(PaperGateway(get_settings(), client_id=11).snapshot_quotes(["AAPL"]))
    finally:
        get_settings.cache_clear()
    assert len(rows) == 1
    assert holder["arrived"] == arrived_at
    assert clock["now"] - arrived_at == timedelta(seconds=2)
    received = datetime.fromisoformat(str(rows[0]["received_at"]))
    assert received == arrived_at
    quote = Quote(
        last=100.0,
        as_of=received - timedelta(seconds=1),
        received_at=received,
        source="ibkr",
        data_type="realtime",
        market_data_type=1,
    )
    judged_at = received + timedelta(seconds=11)
    assert quote.age_seconds(judged_at) == pytest.approx(11)
    home = tmp_path / "gate"
    write_risk_limits(home, build_risk_limits(now=judged_at, nlv=1_000_000, account=PIN))
    ctx = GateContext(
        now=judged_at,
        trading_home=home,
        expected_account=PIN,
        accounts=(PIN,),
        baseline_accounts=(PIN,),
        quotes={"AAPL": quote},
        fx_to_base={"USD": 1.0, "HKD": 1.0},
    )
    intent = OrderIntent(
        client_id="cid-age",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=10,
        limit_price=100.0,
        account=PIN,
        market="US",
        currency="USD",
    )
    with pytest.raises(RiskRejected, match="R22"):
        RiskGate(ctx).allow(intent)


def test_snapshot_quotes_qualifies_before_reqmktdata_and_skips_a_bad_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """012330.KS is qualified before reqMktData. A failed 2317.TW does not abort the batch."""
    calls: list[tuple[object, ...]] = []

    class Ticker:
        def __init__(self) -> None:
            self.last = float("nan")
            self.delayedLast = 250000.0
            self.marketDataType = 3
            self.delayedLastTimestamp = datetime(2026, 10, 16, 1, 0, tzinfo=UTC)

    class Contract:
        def __init__(self, symbol: str, exchange: str, currency: str) -> None:
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency
            self.conId = 0

    class IB:
        async def connectAsync(self, host: str, port: int, clientId: int) -> None:
            return None

        def managedAccounts(self) -> list[str]:
            return [PIN]

        def reqMarketDataType(self, _kind: int) -> None:
            return None

        async def qualifyContractsAsync(self, contract: Contract) -> list[Contract | None]:
            calls.append(("qualify", contract.symbol, contract.conId))
            if contract.symbol == "2317":
                return [None]
            qualified = Contract(contract.symbol, contract.exchange, contract.currency)
            qualified.conId = 17382528
            return [qualified]

        def reqMktData(self, contract: Contract, *_args: object) -> Ticker:
            calls.append(("req", contract.symbol, contract.conId))
            return Ticker()

        def cancelMktData(self, contract: Contract) -> None:
            calls.append(("cancel", contract.symbol, contract.conId))

        def disconnect(self) -> None:
            return None

    fake = types.ModuleType("ib_async")
    fake.IB = IB
    fake.Stock = Contract
    monkeypatch.setitem(sys.modules, "ib_async", fake)
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    monkeypatch.setenv("IB_PORT", "4002")
    monkeypatch.setenv("IB_CLIENT_ID", "11")
    monkeypatch.setenv("TRADING_HOME", str(tmp_path))
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="auto_trading.execution.ibkr"):
            rows = asyncio.run(
                PaperGateway(get_settings(), client_id=11).snapshot_quotes(["012330.KS", "2317.TW"])
            )
    finally:
        get_settings.cache_clear()
    assert [row["symbol"] for row in rows] == ["012330.KS"]
    assert calls == [
        ("qualify", "012330", 0),
        ("req", "012330", 17382528),
        ("cancel", "012330", 17382528),
        ("qualify", "2317", 0),
    ]
    assert "2317.TW" in caplog.text
    assert "contract not qualified" in caplog.text


def test_place_order_qualifies_an_unqualified_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []

    class Contract:
        con_id = 0

        def __init__(self, symbol: str, exchange: str, currency: str) -> None:
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency
            self.conId = type(self).con_id

    class LimitOrder:
        def __init__(self, action: str, totalQuantity: float, lmtPrice: float) -> None:
            self.action = action
            self.totalQuantity = totalQuantity
            self.lmtPrice = lmtPrice
            self.orderRef = ""
            self.account = ""
            self.tif = ""

    class IB:
        def managedAccounts(self) -> list[str]:
            return [PIN]

        async def qualifyContractsAsync(self, contract: Contract) -> list[Contract]:
            calls.append(("qualify", contract.symbol, contract.conId))
            qualified = Contract(contract.symbol, contract.exchange, contract.currency)
            qualified.conId = 17382528
            return [qualified]

        def placeOrder(self, contract: Contract, order: LimitOrder) -> None:
            calls.append(("place", contract.symbol, contract.conId, order.orderRef))

    fake = types.ModuleType("ib_async")
    fake.Stock = Contract
    fake.LimitOrder = LimitOrder
    monkeypatch.setitem(sys.modules, "ib_async", fake)
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    monkeypatch.setenv("IB_PORT", "4002")
    monkeypatch.setenv("TRADING_HOME", str(tmp_path))
    get_settings.cache_clear()

    def intent(client_id: str) -> OrderIntent:
        return OrderIntent(
            client_id=client_id,
            symbol="012330.KS",
            side=OrderSide.BUY,
            quantity=1,
            limit_price=250000,
            account=PIN,
        )

    try:
        gateway = PaperGateway(get_settings(), client_id=11)
        gateway._ib = IB()
        asyncio.run(gateway._submit_connected(intent("cid-qualify")))
        assert calls == [("qualify", "012330", 0), ("place", "012330", 17382528, "cid-qualify")]
        calls.clear()
        Contract.con_id = 424242
        asyncio.run(gateway._submit_connected(intent("cid-ready")))
        assert calls == [("place", "012330", 424242, "cid-ready")]
    finally:
        Contract.con_id = 0
        get_settings.cache_clear()


def test_refresh_quotes_logs_the_exception_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    message = (
        "Contract Stock(symbol='012330', exchange='KRX', currency='KRW') "
        "can't be hashed because no conId"
    )

    async def boom(self: PaperGateway, symbols: list[str]) -> list[dict[str, object]]:
        raise ValueError(message)

    monkeypatch.setenv("TRADING_GATEWAY_CONNECT", "1")
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    monkeypatch.setenv("IB_PORT", "4002")
    monkeypatch.setenv("TRADING_HOME", str(tmp_path))
    monkeypatch.setattr(PaperGateway, "snapshot_quotes", boom)
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="auto_trading.execution.services"):
            quotes = _refresh_quotes(tmp_path, NOW, ["012330.KS"])
    finally:
        get_settings.cache_clear()
    assert quotes == {}
    assert message in caplog.text
