"""Section 9: quote freshness, pin failures, sync prefixes, expiry, and ticks."""

from __future__ import annotations

import json
import shutil
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from auto_trading.broker import OrderIntent
from auto_trading.config import get_settings
from auto_trading.execution import breakers
from auto_trading.execution.ibkr import PaperGateway, _quote_row
from auto_trading.execution.pricing import align_tick, reference_limit
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.execution.services import (
    account_once,
    load_intent_orders,
    oms_once,
    submit_once,
    sync_argv,
)
from auto_trading.execution.strategy_book import (
    CONFIRMED_SPEC_HASHES,
    confirmed_source_errors,
    refresh_confirmed_pins,
    sync_row_carries_pin,
)
from auto_trading.market.quotes import write_quotes
from auto_trading.portfolio.journal import TradeJournal


def _release_working(home: Path, when: datetime) -> None:
    """Drop working buys so the next case is about the quote, not R14."""
    journal = TradeJournal(home)
    for order_id, _symbol in journal.working_orders():
        journal.set_order_state(order_id, "CANCELLED", when=when)


PIN = "DUR243736"
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 2, 22, 0, tzinfo=ZoneInfo("Asia/Macau"))
DIGEST = CONFIRMED_SPEC_HASHES[("ta_breakout52", 1)]


def _ready(home: Path, monkeypatch, *, at: datetime | None = None) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    moment = at or NOW
    write_risk_limits(home, build_risk_limits(now=moment, nlv=1_000_000, account=PIN))
    account_once(
        home,
        moment,
        snapshot={
            "accounts": [PIN],
            "nlv": 1_000_000.0,
            "positions": [],
            "open_orders": [],
        },
    )


def _quote(home: Path, when: datetime, *, age: timedelta) -> None:
    received = when - age
    write_quotes(
        home,
        when,
        [
            {
                "symbol": "AAPL",
                "last": 100.0,
                "as_of": (received - timedelta(minutes=15)).isoformat(),
                "received_at": received.isoformat(),
                "source": "ibkr",
                "data_type": "delayed",
                "market_data_type": 3,
            }
        ],
    )


def _file(home: Path, name: str, row: dict[str, object], envelope: dict[str, object]) -> None:
    folder = home / "intents"
    folder.mkdir(parents=True, exist_ok=True)
    body = dict(envelope)
    body["intents"] = [row]
    (folder / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")


def _opening(name: str) -> tuple[dict[str, object], dict[str, object]]:
    row = {
        "intent_id": name,
        "intent_type": "ENTRY",
        "ticker": "AAPL",
        "market": "US",
        "currency": "USD",
        "side": "BUY",
        "qty": 10,
        "limit_price": 100,
        "valid_from": "2026-10-02T21:30:00+08:00",
        "strategy_id": "ta_breakout52",
    }
    envelope = {
        "schema_version": "intent/1.0",
        "strategy_id": "ta_breakout52",
        "strategy_version": 1,
        "spec_hash": DIGEST,
        "mode": "paper",
    }
    return row, envelope


def test_quote_row_reads_tick_88_and_refuses_a_missing_timestamp() -> None:
    now = datetime(2026, 10, 13, 2, 0, tzinfo=UTC)
    printed = now - timedelta(minutes=15)
    missing = SimpleNamespace(last=float("nan"), delayedLast=400.2, marketDataType=3)
    assert _quote_row("0700.HK", missing, now) is None
    untyped = SimpleNamespace(delayedLast=400.2, delayedLastTimestamp=printed)
    assert _quote_row("0700.HK", untyped, now) is None
    stamped = SimpleNamespace(
        last=float("nan"),
        delayedLast=400.2,
        marketDataType=3,
        delayedLastTimestamp=printed,
    )
    row = _quote_row("0700.HK", stamped, now)
    assert row is not None
    assert row["delay_sec"] == pytest.approx(900)
    assert row["delay_sec"] != 0
    assert row["as_of"] != row["received_at"]
    via_tick = SimpleNamespace(
        last=float("nan"),
        delayedLast=400.2,
        marketDataType=3,
        ticks=[SimpleNamespace(tickType=88, time=printed)],
    )
    delayed = _quote_row("0700.HK", via_tick, now)
    assert delayed is not None
    assert delayed["delay_sec"] == pytest.approx(900)


def test_fake_gateway_refetches_stale_quotes_and_does_not_stick_on_r22(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's fake Gateway: refetch at 10:20, delay_sec set, stale receipt is R22."""
    clock = {"now": datetime(2026, 10, 13, 2, 0, tzinfo=UTC)}
    log: dict[str, list] = {"mdtype": [], "req": [], "place": []}

    class Ticker:
        def __init__(self) -> None:
            self.last = float("nan")
            self.delayedLast = 400.2
            self.marketDataType = 3
            self.delayedLastTimestamp = clock["now"] - timedelta(minutes=15)

    class Contract:
        def __init__(self, symbol: str, exchange: str, currency: str) -> None:
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency

    class LimitOrder:
        def __init__(self, action: str, totalQuantity: float, lmtPrice: float) -> None:
            self.action = action
            self.totalQuantity = totalQuantity
            self.lmtPrice = lmtPrice
            self.orderRef = ""
            self.account = ""
            self.tif = ""

    class IB:
        async def connectAsync(self, host: str, port: int, clientId: int) -> None:
            self.cid = clientId

        def managedAccounts(self) -> list[str]:
            return [PIN]

        def reqMarketDataType(self, kind: int) -> None:
            log["mdtype"].append(kind)

        async def qualifyContractsAsync(self, contract: Contract) -> list[Contract]:
            contract.conId = 1
            return [contract]

        def reqMktData(self, contract: Contract, *_args: object) -> Ticker:
            log["req"].append(contract.symbol)
            return Ticker()

        def cancelMktData(self, _contract: Contract) -> None:
            return None

        def placeOrder(self, contract: Contract, order: LimitOrder) -> None:
            log["place"].append(
                {
                    "symbol": contract.symbol,
                    "lmt": order.lmtPrice,
                    "account": order.account,
                    "orderRef": order.orderRef,
                }
            )

        def disconnect(self) -> None:
            return None

    fake = types.ModuleType("ib_async")
    fake.IB = IB
    fake.Stock = Contract
    fake.LimitOrder = LimitOrder
    monkeypatch.setitem(sys.modules, "ib_async", fake)

    import auto_trading.execution.ibkr as ibkr

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            current = clock["now"]
            if tz is None:
                return current.replace(tzinfo=None)
            return current.astimezone(tz)

    monkeypatch.setattr(ibkr, "datetime", FakeDT)
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    monkeypatch.setenv("IB_PORT", "4002")
    monkeypatch.setenv("IB_CLIENT_ID", "11")
    monkeypatch.setenv("TRADING_GATEWAY_CONNECT", "1")
    monkeypatch.setenv("TRADING_HOME", str(tmp_path))
    get_settings.cache_clear()
    shutil.copytree(ROOT / "strategies", tmp_path / "strategies")
    digest = DIGEST

    def prep(moment: datetime) -> None:
        write_risk_limits(tmp_path, build_risk_limits(now=moment, nlv=1_000_000, account=PIN))
        account_once(
            tmp_path,
            moment,
            snapshot={
                "accounts": [PIN],
                "nlv": 1_000_000.0,
                "positions": [],
                "open_orders": [],
            },
        )
        oms_once(tmp_path, moment)
        state = breakers.flags(tmp_path)
        state["r30_active"] = False
        breakers._save(tmp_path, state)

    def intent(name: str) -> None:
        _file(
            tmp_path,
            name,
            {
                "intent_id": name,
                "intent_type": "ENTRY",
                "ticker": "0700.HK",
                "market": "HK",
                "exchange": "SEHK",
                "currency": "HKD",
                "side": "BUY",
                "qty": 100,
                "lot_size": 100,
                "order_type": "LMT",
                "tif": "DAY",
                "limit_offset_bps": 10,
                "ref_price_at_create": 400.0,
                "valid_from": "2026-10-13T09:30:00+08:00",
                "strategy_id": "ta_breakout52",
            },
            {
                "schema_version": "intent/1.0",
                "strategy_id": "ta_breakout52",
                "strategy_version": 1,
                "spec_hash": digest,
                "mode": "paper",
            },
        )

    def quotes() -> list[dict[str, object]]:
        path = tmp_path / "state" / "quotes.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        return list(payload["quotes"])

    try:
        moment = clock["now"]
        prep(moment)
        intent("n1_normal")
        first = submit_once(tmp_path, moment, readonly=False)
        assert first["rejected"] == []
        assert first["sent"] == 1
        assert first["rejected"] == []
        assert log["mdtype"] == [3]
        assert len(log["req"]) == 1
        stored = quotes()[0]
        assert stored["delay_sec"] == pytest.approx(15 * 60)
        assert stored["delay_sec"] != 0
        assert log["place"][0]["lmt"] == 400.4
        assert repr(log["place"][0]["lmt"]) == "400.4"
        assert log["place"][0]["account"] == PIN

        clock["now"] = moment + timedelta(seconds=10)
        prep(clock["now"])
        again = submit_once(tmp_path, clock["now"], readonly=False)
        assert again["sent"] == 0
        assert len(log["req"]) == 1
        _release_working(tmp_path, clock["now"])

        real = PaperGateway.snapshot_quotes

        async def empty(self: PaperGateway, symbols: list[str]) -> list[dict[str, object]]:
            return []

        monkeypatch.setattr(PaperGateway, "snapshot_quotes", empty)
        clock["now"] = moment + timedelta(seconds=11)
        prep(clock["now"])
        intent("n_stale_receipt")
        stale = submit_once(tmp_path, clock["now"], readonly=False)
        assert stale["sent"] == 0
        assert "R22" in stale["rejected"]
        assert len(log["place"]) == 1
        (tmp_path / "intents" / "n_stale_receipt.json").unlink(missing_ok=True)
        monkeypatch.setattr(PaperGateway, "snapshot_quotes", real)

        clock["now"] = moment + timedelta(minutes=20)
        prep(clock["now"])
        _release_working(tmp_path, clock["now"])
        intent("n2_twenty_min_later")
        later = submit_once(tmp_path, clock["now"], readonly=False)
        assert later["sent"] == 1
        assert len(log["req"]) == 2
        assert quotes()[0]["received_at"].startswith("2026-10-13T02:20:00")
        assert quotes()[0]["delay_sec"] == pytest.approx(15 * 60)

        clock["now"] = moment + timedelta(minutes=40)
        prep(clock["now"])
        _release_working(tmp_path, clock["now"])
        intent("n3_forty_min_later")
        last = submit_once(tmp_path, clock["now"], readonly=False)
        assert last["sent"] == 1
        assert "R22" not in last["rejected"]
        assert len(log["req"]) == 3
    finally:
        get_settings.cache_clear()


def test_unreadable_pin_rejects_openings_and_still_sends_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import auto_trading.execution.strategy_book as book

    original = book.PIN_FILE
    try:
        _ready(tmp_path, monkeypatch)
        shutil.copytree(ROOT / "strategies", tmp_path / "strategies")
        for label, text in (
            ("missing", None),
            ("corrupt", "{"),
            ("empty", json.dumps({"strategies": []})),
        ):
            target = tmp_path / f"{label}.json"
            if text is None:
                book.PIN_FILE = tmp_path / "absent.json"
            else:
                target.write_text(text, encoding="utf-8")
                book.PIN_FILE = target
            row, envelope = _opening(f"open-{label}")
            _file(tmp_path, f"open-{label}", row, envelope)
            _quote(tmp_path, NOW, age=timedelta(seconds=1))
            sent: list[OrderIntent] = []
            result = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
            assert sent == []
            assert "R25" in result["rejected"]
            (tmp_path / "intents" / f"open-{label}.json").unlink(missing_ok=True)
        pause = json.loads((tmp_path / "state" / "entry_pause.json").read_text(encoding="utf-8"))
        assert pause["pin_file"] is True
        critical = list((tmp_path / "logs").glob("critical_*.jsonl"))
        assert critical
        body = critical[0].read_text(encoding="utf-8")
        assert "confirmed_pin_unreadable" in body
        oms = oms_once(tmp_path, NOW)
        assert "approved_hash" not in oms["errors"]
        account_once(
            tmp_path,
            NOW,
            snapshot={
                "accounts": [PIN],
                "nlv": 1_000_000.0,
                "positions": [{"symbol": "AAPL", "qty": 10, "strategy_id": "ta_breakout52"}],
                "open_orders": [],
            },
        )
        _file(
            tmp_path,
            "exit-pin",
            {
                "intent_id": "exit-pin",
                "intent_type": "EXIT",
                "parent_intent_id": "entry-1",
                "ticker": "AAPL",
                "market": "US",
                "currency": "USD",
                "side": "SELL",
                "qty": 5,
                "limit_price": 100,
                "strategy_id": "ta_breakout52",
            },
            {
                "schema_version": "intent/1.0",
                "strategy_id": "ta_breakout52",
                "strategy_version": 1,
                "spec_hash": DIGEST,
                "mode": "paper",
            },
        )
        _file(
            tmp_path,
            "stop-pin",
            {
                "intent_id": "stop-pin",
                "intent_type": "EXIT",
                "parent_intent_id": "entry-1",
                "ticker": "AAPL",
                "market": "US",
                "currency": "USD",
                "side": "SELL",
                "qty": 5,
                "limit_price": 90,
                "protective": True,
                "strategy_id": "ta_breakout52",
            },
            {
                "schema_version": "intent/1.0",
                "strategy_id": "ta_breakout52",
                "strategy_version": 1,
                "spec_hash": DIGEST,
                "mode": "paper",
            },
        )
        sent = []
        result = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
        assert {item.intent_id for item in sent} == {"exit-pin", "stop-pin"}
        assert "R25" not in result["rejected"]
    finally:
        book.PIN_FILE = original
        refresh_confirmed_pins()


def test_ci_rejects_a_config_directory_on_a_sync_list(tmp_path: Path) -> None:
    assert sync_row_carries_pin("config")
    assert sync_row_carries_pin("config/")
    assert sync_row_carries_pin("config/confirmed_strategies.json")
    assert not sync_row_carries_pin("config/adr_map.csv")
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "config", root / "config")
    shutil.copytree(ROOT / "strategies", root / "strategies")
    shutil.copytree(ROOT / "vps" / "deploy", root / "vps" / "deploy")
    assert confirmed_source_errors(root) == []
    manifest = root / "vps" / "deploy" / "sync_vps_to_box.txt"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "config/\n", encoding="utf-8")
    errors = confirmed_source_errors(root)
    assert any("config" in item for item in errors)
    with pytest.raises(RuntimeError, match="git, not sync"):
        sync_argv(root, "box.example:/srv/trading", direction="out")


def test_opening_without_valid_from_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(tmp_path, monkeypatch)
    shutil.copytree(ROOT / "strategies", tmp_path / "strategies")
    row, envelope = _opening("no-valid")
    row.pop("valid_from")
    _file(tmp_path, "a-no-valid", row, envelope)
    _file(
        tmp_path,
        "z-exit",
        {
            "intent_id": "exit-open",
            "intent_type": "EXIT",
            "parent_intent_id": "entry-1",
            "ticker": "AAPL",
            "market": "US",
            "currency": "USD",
            "side": "SELL",
            "qty": 1,
            "limit_price": 100,
            "strategy_id": "ta_breakout52",
        },
        {
            "schema_version": "intent/1.0",
            "strategy_id": "ta_breakout52",
            "strategy_version": 1,
            "spec_hash": DIGEST,
            "mode": "paper",
        },
    )
    account_once(
        tmp_path,
        NOW,
        snapshot={
            "accounts": [PIN],
            "nlv": 1_000_000.0,
            "positions": [{"symbol": "AAPL", "qty": 1, "strategy_id": "ta_breakout52"}],
            "open_orders": [],
        },
    )
    _quote(tmp_path, NOW, age=timedelta(seconds=1))
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
    assert [item.intent_id for item in sent] == ["exit-open"]
    assert "INTENT_EXPIRED" in result["rejected"]
    assert not (tmp_path / "intents" / "a-no-valid.json").exists()
    assert (tmp_path / "intents" / "done" / "a-no-valid.json").is_file()
    assert all(item.intent_id != "no-valid" for item in load_intent_orders(tmp_path))


def test_limit_price_is_rounded_to_the_tick() -> None:
    limit, code = reference_limit(
        market="HK",
        side="BUY",
        ref_price=400.0,
        offset_bps=10,
        cap=None,
    )
    assert code is None
    assert limit == 400.4
    assert repr(limit) == "400.4"
    assert repr(align_tick(400 * 1.001, 0.2, buy=True)) == "400.4"
