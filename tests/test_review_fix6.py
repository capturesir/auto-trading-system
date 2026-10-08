"""Section 9 leftovers: tick multiples, terminal intents, and quote receipt time."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import types
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.config import get_settings
from auto_trading.execution.ibkr import PaperGateway
from auto_trading.execution.pricing import quantize_to_tick
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.execution.services import account_once, submit_once
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


def _ready(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    write_risk_limits(home, build_risk_limits(now=NOW, nlv=1_000_000, account=PIN))
    account_once(
        home,
        NOW,
        snapshot={
            "accounts": [PIN],
            "nlv": 1_000_000.0,
            "positions": [],
            "open_orders": [],
        },
    )
    shutil.copytree(ROOT / "strategies", home / "strategies")


def _write_opening(home: Path, name: str, *, valid_from: str | None) -> None:
    row: dict[str, object] = {
        "intent_id": name,
        "intent_type": "ENTRY",
        "ticker": "AAPL",
        "market": "US",
        "currency": "USD",
        "side": "BUY",
        "qty": 10,
        "limit_price": 100,
        "strategy_id": "ta_breakout52",
    }
    if valid_from is not None:
        row["valid_from"] = valid_from
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


def _restore(home: Path, name: str) -> None:
    done = home / "intents" / "done" / f"{name}.json"
    (home / "intents" / f"{name}.json").write_bytes(done.read_bytes())


def test_gate_reject_is_terminal_and_never_reaches_the_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(tmp_path, monkeypatch)
    write_quotes(
        tmp_path,
        NOW,
        [
            {
                "symbol": "AAPL",
                "last": 100.0,
                "as_of": (NOW - timedelta(minutes=15)).isoformat(),
                "received_at": (NOW - timedelta(seconds=11)).isoformat(),
                "source": "ibkr",
                "data_type": "delayed",
                "market_data_type": 3,
            }
        ],
    )
    _write_opening(tmp_path, "gate", valid_from="2026-10-02T21:30:00+08:00")
    sent: list[OrderIntent] = []
    first = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
    assert sent == []
    assert "R22" in first["rejected"]
    assert not (tmp_path / "intents" / "gate.json").exists()
    assert TradeJournal(tmp_path).latest_order_state("gate") == "RISK_REJECTED"
    _restore(tmp_path, "gate")
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
