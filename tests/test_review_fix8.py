"""Production risk context: broker symbols, journal attribution, FX, and stops."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from auto_trading.broker import OrderIntent
from auto_trading.execution.fx import rates_from_account_values, total_cash_base
from auto_trading.execution.oms import order_ref
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.execution.services import account_once, submit_once
from auto_trading.execution.strategy_book import CONFIRMED_SPEC_HASHES
from auto_trading.market.quotes import write_quotes
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.strategy.engine import _intent

PIN = "DUR243736"
ROOT = Path(__file__).resolve().parents[1]
US = datetime(2026, 10, 2, 22, 0, tzinfo=ZoneInfo("Asia/Macau"))
HK = datetime(2026, 10, 13, 10, 0, tzinfo=ZoneInfo("Asia/Macau"))
DIGEST = CONFIRMED_SPEC_HASHES[("ta_breakout52", 1)]


def _ready(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    at: datetime,
    nlv: float = 1_000_000.0,
    positions: list[dict[str, object]] | None = None,
    open_orders: list[dict[str, object]] | None = None,
    fx: dict[str, float] | None = None,
    cash: float | None = None,
    include_cash: bool = False,
) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    write_risk_limits(home, build_risk_limits(now=at, nlv=nlv, account=PIN))
    snapshot: dict[str, object] = {
        "accounts": [PIN],
        "nlv": nlv,
        "positions": positions or [],
        "open_orders": open_orders or [],
    }
    if fx is not None:
        snapshot["fx_to_base"] = fx
    if include_cash:
        snapshot["cash"] = cash
    account_once(home, at, snapshot=snapshot)
    if not (home / "strategies").exists():
        shutil.copytree(ROOT / "strategies", home / "strategies")


def _quote(home: Path, when: datetime, symbol: str, *, last: float) -> None:
    received = when - timedelta(seconds=1)
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


def _write(home: Path, name: str, row: dict[str, object]) -> None:
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


def _attribute(home: Path, ticker: str, qty: float, when: datetime, *, con_id: int = 1) -> None:
    TradeJournal(home).apply_strategy_fill(
        account=PIN,
        strategy_id="ta_breakout52",
        con_id=con_id,
        ticker=ticker,
        side="SET",
        qty=qty,
        price=1.0,
        root_intent_id="book",
        when=when,
    )


def _sell(name: str, ticker: str, *, market: str, currency: str, qty: int, price: float) -> dict:
    return {
        "intent_id": name,
        "intent_type": "EXIT",
        "parent_intent_id": "entry-1",
        "ticker": ticker,
        "market": market,
        "currency": currency,
        "side": "SELL",
        "qty": qty,
        "limit_price": price,
        "valid_from": "2026-10-13T09:30:00+08:00"
        if market == "HK"
        else "2026-10-02T21:30:00+08:00",
        "strategy_id": "ta_breakout52",
    }


def _buy(name: str, **over: object) -> dict[str, object]:
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


def _alerts(home: Path) -> int:
    count = 0
    for path in (home / "logs").glob("critical_*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if '"kind": "reducing_order_blocked"' in line:
                count += 1
    return count


def test_exchange_rate_and_total_cash_come_from_account_values() -> None:
    rows = [
        {"tag": "ExchangeRate", "currency": "USD", "value": "7.75"},
        {"tag": "ExchangeRate", "currency": "BASE", "value": "1"},
        {"tag": "TotalCashValue", "currency": "BASE", "value": "123.5"},
        {"tag": "TotalCashValue", "currency": "USD", "value": "10"},
    ]
    rates = rates_from_account_values(rows)
    assert rates["USD"] == 7.75
    assert rates["HKD"] == 1.0
    assert "BASE" not in rates
    assert total_cash_base(rows) == 123.5


def test_d4_exit_is_r20_while_the_protective_stop_is_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = [{"symbol": "700", "qty": 100, "currency": "HKD", "exchange": "SEHK"}]
    _ready(tmp_path, monkeypatch, at=HK, positions=held)
    _attribute(tmp_path, "0700.HK", 100, HK)
    _quote(tmp_path, HK, "0700.HK", last=400.0)
    _write(
        tmp_path,
        "d4-stop",
        {
            **_sell("d4-stop", "0700.HK", market="HK", currency="HKD", qty=100, price=400),
            "protective": True,
        },
    )
    sent: list[OrderIntent] = []
    first = submit_once(tmp_path, HK, place=sent.append, readonly=False)
    assert first["sent"] == 1
    assert [item.intent_id for item in sent] == ["d4-stop"]
    sent.clear()
    account_once(
        tmp_path,
        HK,
        snapshot={
            "accounts": [PIN],
            "nlv": 1_000_000.0,
            "positions": held,
            "open_orders": [
                {
                    "order_id": order_ref("d4-stop", 1),
                    "symbol": "700",
                    "side": "SELL",
                    "qty": 100,
                    "currency": "HKD",
                    "exchange": "SEHK",
                }
            ],
        },
    )
    _quote(tmp_path, HK, "0700.HK", last=400.0)
    _write(
        tmp_path,
        "d4-exit",
        _sell("d4-exit", "0700.HK", market="HK", currency="HKD", qty=100, price=400),
    )
    second = submit_once(tmp_path, HK, place=sent.append, readonly=False)
    assert sent == []
    assert second["rejected"] == ["R20"]
    assert (tmp_path / "intents" / "d4-exit.json").is_file()
    details = TradeJournal(tmp_path).working_order_details()
    assert sum(row["qty"] for row in details if row["side"] == "SELL") == 100


def test_d6_shrunk_broker_qty_blocks_the_stale_journal_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = [{"symbol": "700", "qty": 50, "currency": "HKD", "exchange": "SEHK"}]
    _ready(tmp_path, monkeypatch, at=HK, positions=held)
    _attribute(tmp_path, "0700.HK", 100, HK)
    _quote(tmp_path, HK, "0700.HK", last=400.0)
    _write(
        tmp_path,
        "d6-exit",
        _sell("d6-exit", "0700.HK", market="HK", currency="HKD", qty=100, price=400),
    )
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, HK, place=sent.append, readonly=False)
    assert sent == []
    assert result["rejected"] == ["R20"]
    assert (tmp_path / "intents" / "d6-exit.json").is_file()


def test_d7_us_exit_uses_journal_strategy_qty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = [{"symbol": "AAPL", "qty": 100, "currency": "USD"}]
    _ready(tmp_path, monkeypatch, at=US, positions=held)
    _attribute(tmp_path, "AAPL", 100, US)
    _quote(tmp_path, US, "AAPL", last=100.0)
    _write(
        tmp_path,
        "d7-exit",
        _sell("d7-exit", "AAPL", market="US", currency="USD", qty=100, price=100),
    )
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, US, place=sent.append, readonly=False)
    assert result["sent"] == 1
    assert [item.intent_id for item in sent] == ["d7-exit"]
    assert "R20" not in result["rejected"]


def test_d7b_bare_hk_symbol_matches_the_journal_ticker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = [{"symbol": "700", "qty": 100, "currency": "HKD", "exchange": "SEHK"}]
    _ready(tmp_path, monkeypatch, at=HK, positions=held)
    _attribute(tmp_path, "0700.HK", 100, HK)
    _quote(tmp_path, HK, "0700.HK", last=400.0)
    _write(
        tmp_path,
        "d7b-exit",
        _sell("d7b-exit", "0700.HK", market="HK", currency="HKD", qty=100, price=400),
    )
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, HK, place=sent.append, readonly=False)
    assert result["sent"] == 1
    assert [item.intent_id for item in sent] == ["d7b-exit"]


def test_d7c_stop_and_exit_cannot_both_sell_the_same_shares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = [{"symbol": "700", "qty": 100, "currency": "HKD", "exchange": "SEHK"}]
    _ready(tmp_path, monkeypatch, at=HK, positions=held)
    _attribute(tmp_path, "0700.HK", 100, HK)
    _quote(tmp_path, HK, "0700.HK", last=400.0)
    _write(
        tmp_path,
        "d7c-exit",
        _sell("d7c-exit", "0700.HK", market="HK", currency="HKD", qty=100, price=400),
    )
    _write(
        tmp_path,
        "d7c-stop",
        {
            **_sell("d7c-stop", "0700.HK", market="HK", currency="HKD", qty=100, price=400),
            "protective": True,
        },
    )
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, HK, place=sent.append, readonly=False)
    assert result["sent"] == 1
    assert result["rejected"] == ["R20"]
    assert len(sent) == 1
    details = TradeJournal(tmp_path).working_order_details()
    assert len(details) == 1
    assert details[0]["side"] == "SELL"
    assert details[0]["qty"] == 100
    assert (tmp_path / "intents" / "d7c-stop.json").is_file()


def test_p5_marks_the_bare_hk_holding_and_rejects_r3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = [{"symbol": "700", "qty": 2000, "currency": "HKD", "exchange": "SEHK"}]
    _ready(tmp_path, monkeypatch, at=HK, positions=held)
    _quote(tmp_path, HK, "0700.HK", last=400.0)
    _write(tmp_path, "p5-entry", _buy("p5-entry"))
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, HK, place=sent.append, readonly=False)
    assert sent == []
    assert result["rejected"] == ["R3"]
    assert (tmp_path / "intents" / "done" / "p5-entry.json").is_file()


def test_p1_us_notional_uses_usd_hkd_and_is_r1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(tmp_path, monkeypatch, at=US)
    _quote(tmp_path, US, "AAPL", last=100.0)
    _write(
        tmp_path,
        "p1-aapl",
        _buy(
            "p1-aapl",
            ticker="AAPL",
            market="US",
            currency="USD",
            qty=400,
            lot_size=1,
            limit_price=100,
            valid_from="2026-10-02T21:30:00+08:00",
        ),
    )
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, US, place=sent.append, readonly=False)
    assert sent == []
    assert result["rejected"] == ["R1"]
    assert (tmp_path / "intents" / "done" / "p1-aapl.json").is_file()


def test_p2_p3_p4_one_lots_pass_once_fx_is_in_hkd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = [
        (
            "p2-jp",
            "7203.T",
            "JP",
            "JPY",
            100,
            100,
            3000.0,
            "2026-10-13T08:00:00+08:00",
            1_000_000.0,
        ),
        (
            "p3-kr",
            "005930.KS",
            "KR",
            "KRW",
            10,
            1,
            70000.0,
            "2026-10-13T08:00:00+08:00",
            1_000_000.0,
        ),
        (
            "p4-tw",
            "2317.TW",
            "TW",
            "TWD",
            1000,
            1000,
            256.0,
            "2026-10-13T09:00:00+08:00",
            2_000_000.0,
        ),
    ]
    for name, ticker, market, currency, qty, lot, price, valid_from, nlv in cases:
        home = tmp_path / name
        home.mkdir()
        _ready(home, monkeypatch, at=HK, nlv=nlv)
        _quote(home, HK, ticker, last=price)
        _write(
            home,
            name,
            _buy(
                name,
                ticker=ticker,
                market=market,
                currency=currency,
                qty=qty,
                lot_size=lot,
                limit_price=price,
                valid_from=valid_from,
            ),
        )
        sent: list[OrderIntent] = []
        result = submit_once(home, HK, place=sent.append, readonly=False)
        assert result["rejected"] == [], name
        assert result["sent"] == 1, name
        assert [item.symbol for item in sent] == [ticker]


def test_missing_jpy_rate_is_r17_and_stays_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(tmp_path, monkeypatch, at=HK, fx={"HKD": 1.0, "USD": 7.8})
    _quote(tmp_path, HK, "7203.T", last=3000.0)
    _write(
        tmp_path,
        "p-missing-jpy",
        _buy(
            "p-missing-jpy",
            ticker="7203.T",
            market="JP",
            currency="JPY",
            qty=100,
            lot_size=100,
            limit_price=3000,
            valid_from="2026-10-13T08:00:00+08:00",
        ),
    )
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, HK, place=sent.append, readonly=False)
    assert sent == []
    assert result["rejected"] == ["R17"]
    assert "LOT_TOO_LARGE" not in result["rejected"]
    assert (tmp_path / "intents" / "p-missing-jpy.json").is_file()


def test_explicit_cash_below_the_order_is_r7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(tmp_path, monkeypatch, at=HK, include_cash=True, cash=1000.0)
    _quote(tmp_path, HK, "0700.HK", last=400.0)
    _write(tmp_path, "cash-short", _buy("cash-short"))
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, HK, place=sent.append, readonly=False)
    assert sent == []
    assert result["rejected"] == ["R7"]
    assert (tmp_path / "intents" / "cash-short.json").is_file()


def test_missing_total_cash_value_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(tmp_path, monkeypatch, at=HK, include_cash=True, cash=None)
    _quote(tmp_path, HK, "0700.HK", last=400.0)
    _write(tmp_path, "cash-missing", _buy("cash-missing"))
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, HK, place=sent.append, readonly=False)
    assert sent == []
    assert result["rejected"] == ["R17"]
    assert (tmp_path / "intents" / "cash-missing.json").is_file()


def test_lunch_r10_does_not_count_toward_the_reducing_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lunch = datetime(2026, 10, 13, 12, 30, tzinfo=ZoneInfo("Asia/Macau"))
    _ready(tmp_path, monkeypatch, at=lunch)
    _quote(tmp_path, lunch, "0700.HK", last=400.0)
    _write(
        tmp_path,
        "n17-exit",
        _sell("n17-exit", "0700.HK", market="HK", currency="HKD", qty=100, price=400),
    )
    sent: list[OrderIntent] = []
    for _ in range(3):
        result = submit_once(tmp_path, lunch, place=sent.append, readonly=False)
        assert result["rejected"] == ["R10"]
        assert result["sent"] == 0
    assert sent == []
    assert _alerts(tmp_path) == 0
    assert not (tmp_path / "state" / "reducing_blocks").exists()
    assert (tmp_path / "intents" / "n17-exit.json").is_file()


def test_broker_order_ref_is_r12_without_a_journal_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = order_ref("d3a", 1)
    _ready(
        tmp_path,
        monkeypatch,
        at=US,
        open_orders=[
            {
                "order_id": ref,
                "symbol": "AAPL",
                "side": "BUY",
                "qty": 10,
                "currency": "USD",
            }
        ],
    )
    _quote(tmp_path, US, "AAPL", last=100.0)
    _write(
        tmp_path,
        "d3a",
        _buy(
            "d3a",
            ticker="AAPL",
            market="US",
            currency="USD",
            qty=10,
            lot_size=1,
            limit_price=100,
            valid_from="2026-10-02T21:30:00+08:00",
        ),
    )
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, US, place=sent.append, readonly=False)
    assert sent == []
    assert result["rejected"] == ["R12"]
    assert (tmp_path / "intents" / "d3a.json").is_file()


def test_protective_stop_survives_the_previous_session_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = [{"symbol": "AAPL", "qty": 100, "currency": "USD"}]
    _ready(tmp_path, monkeypatch, at=US, positions=held)
    _attribute(tmp_path, "AAPL", 100, US)
    _quote(tmp_path, US, "AAPL", last=100.0)
    yesterday = "2026-10-01T21:30:00+08:00"
    _write(
        tmp_path,
        "n19-exit",
        {
            **_sell("n19-exit", "AAPL", market="US", currency="USD", qty=100, price=100),
            "valid_from": yesterday,
        },
    )
    _write(
        tmp_path,
        "n19-stop",
        {
            **_sell("n19-stop", "AAPL", market="US", currency="USD", qty=100, price=100),
            "protective": True,
            "valid_from": yesterday,
        },
    )
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, US, place=sent.append, readonly=False)
    assert [item.intent_id for item in sent] == ["n19-stop"]
    assert sent[0].protective is True
    assert "INTENT_EXPIRED" in result["rejected"]
    assert (tmp_path / "intents" / "done" / "n19-exit.json").is_file()
    assert (tmp_path / "intents" / "n19-stop.json").is_file()


def test_engine_omits_valid_from_on_a_protective_order() -> None:
    common = {
        "ticker": "AAPL",
        "side": "SELL",
        "qty": 1,
        "parent": "entry-1",
        "ref_price": 100.0,
        "signal_id": None,
        "confidence": None,
        "fair_value": None,
        "limit_cap": None,
        "news_id": None,
        "event_source": None,
        "pre_event_close": None,
    }
    stop = _intent(
        {"id": "ta_breakout52"},
        SimpleNamespace(intent_id="stop-1", kind="protective_stop", protective=True, **common),
        "2026-10-02",
    )
    assert "valid_from" not in stop
    assert stop["protective"] is True
    exit_row = _intent(
        {"id": "ta_breakout52"},
        SimpleNamespace(intent_id="exit-1", kind="EXIT", protective=False, **common),
        "2026-10-02",
    )
    assert "valid_from" in exit_row
    assert "protective" not in exit_row
