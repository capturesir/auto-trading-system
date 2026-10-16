"""Paper buys in a non-USD currency convert from USD before the equity order.

A short target balance, or a conversion that does not show up, never reaches
the equity sender. After the confirmed balance covers the buffered notional,
the same buy is allowed.
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.config import Settings
from auto_trading.execution.approval_mode import ApprovalSession
from auto_trading.execution.fx_convert import (
    FX_BUFFER_BPS,
    FX_COMMISSION_BPS,
    MIN_FX_FEE_USD,
    assess,
    await_target_cash,
    cash_by_currency,
    fx_rejected,
    fx_still_working,
    ib_order_spec,
    required_target_cash,
    usd_for_shortfall,
)
from auto_trading.execution.ibkr import PaperGateway
from auto_trading.execution.services import account_once, submit_once
from auto_trading.execution.strategy_book import CONFIRMED_SPEC_HASHES
from auto_trading.risk.gate import GateContext, Quote, RiskGate, RiskRejected

PIN = "DUR243736"
HKT = ZoneInfo("Asia/Hong_Kong")
ASIA = datetime(2026, 10, 2, 11, 0, tzinfo=HKT)
US = datetime(2026, 10, 2, 22, 0, tzinfo=HKT)
RATES = {"HKD": 1.0, "USD": 7.8, "KRW": 0.0056, "TWD": 0.244, "JPY": 0.052}
ROOT = Path(__file__).resolve().parents[1]


def _rates() -> dict[str, float]:
    return dict(RATES)


def _buy(**kwargs: object) -> OrderIntent:
    payload: dict[str, object] = {
        "client_id": "cid-krw",
        "intent_id": "intent-krw",
        "symbol": "005930.KS",
        "side": OrderSide.BUY,
        "quantity": 1,
        "limit_price": 70_000.0,
        "currency": "KRW",
        "market": "KR",
        "account": PIN,
        "strategy_id": "ta_breakout52",
        "strategy_version": 1,
        "spec_hash": CONFIRMED_SPEC_HASHES[("ta_breakout52", 1)],
        "valid_from": datetime(2026, 10, 2, 9, 30, tzinfo=HKT),
    }
    payload.update(kwargs)
    return OrderIntent(**payload)


def _ctx(home: Path, now: datetime, symbols: list[str], **kwargs: object) -> GateContext:
    from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits

    write_risk_limits(home, build_risk_limits(now=now, nlv=1_000_000, account=PIN))
    quotes = {
        symbol: Quote(last=100.0, as_of=now, received_at=now, market_data_type=1)
        for symbol in symbols
    }
    if "005930.KS" in quotes:
        quotes["005930.KS"] = Quote(last=70_000.0, as_of=now, received_at=now, market_data_type=1)
    payload: dict[str, object] = {
        "now": now,
        "trading_home": home,
        "expected_account": PIN,
        "accounts": (PIN,),
        "baseline_accounts": (PIN,),
        "nlv": 1_000_000.0,
        "cash": 1_000_000.0,
        "hwm": 1_000_000.0,
        "day_start_nlv": 1_000_000.0,
        "quotes": quotes,
        "fx_to_base": _rates(),
        "port": 4002,
    }
    payload.update(kwargs)
    return GateContext(**payload)


def _session() -> ApprovalSession:
    return ApprovalSession(
        read_accounts=lambda: (PIN,),
        port=4002,
        pin=PIN,
        baseline=(PIN,),
        kill_switch_present=lambda: False,
        r0_violation=lambda: False,
        force_manual=lambda: False,
        strategy_approval=lambda: "auto",
    )


def test_required_cash_includes_fee_and_buffer() -> None:
    intent = _buy()
    required = required_target_cash(intent)
    notional = 70_000.0
    commission = 4_000.0
    buffer = notional * (FX_BUFFER_BPS + FX_COMMISSION_BPS) / 10_000
    assert required == pytest.approx(notional + commission + buffer)
    assert required == pytest.approx(74_840.0)
    usd = usd_for_shortfall(required or 0.0, "KRW", RATES)
    raw = 74_840.0 * 0.0056 / 7.8 + MIN_FX_FEE_USD
    assert usd == pytest.approx(math.ceil(raw * 100 - 1e-9) / 100.0)
    assert usd == pytest.approx(55.74)


def test_cash_parser_prefers_cash_balance_and_keeps_debits() -> None:
    rows = [
        {"tag": "CashBalance", "currency": "USD", "value": "25000"},
        {"tag": "CashBalance", "currency": "KRW", "value": "-10"},
        {"tag": "CashBalance", "currency": "BASE", "value": "999"},
        {"tag": "TotalCashValue", "currency": "USD", "value": "1"},
        {"tag": "TotalCashValue", "currency": "HKD", "value": "50"},
    ]
    assert cash_by_currency(rows) == {"USD": 25000.0, "KRW": -10.0}
    fallback = [
        {"tag": "TotalCashValue", "currency": "USD", "value": "10"},
        {"tag": "TotalCashValue", "currency": "BASE", "value": "78"},
    ]
    assert cash_by_currency(fallback) == {"USD": 10.0}
    assert cash_by_currency([{"tag": "NetLiquidation", "currency": "USD", "value": "1"}]) is None


def test_idealpro_versus_cash_conversion_order_shape() -> None:
    rates = _rates()
    krw = assess(_buy(), cash_by_ccy={"USD": 10_000.0, "KRW": 0.0}, fx_to_base=rates)
    twd = assess(
        _buy(
            symbol="2330.TW",
            currency="TWD",
            market="TW",
            quantity=1000,
            limit_price=100.0,
            client_id="cid-twd",
        ),
        cash_by_ccy={"USD": 10_000.0},
        fx_to_base=rates,
    )
    hkd = assess(
        _buy(symbol="0700.HK", currency="HKD", market="HK", quantity=1000, limit_price=10.0),
        cash_by_ccy={"USD": 10_000.0},
        fx_to_base=rates,
    )
    jpy = assess(
        _buy(symbol="7203.T", currency="JPY", market="JP", quantity=100, limit_price=1000.0),
        cash_by_ccy={"USD": 10_000.0},
        fx_to_base=rates,
    )
    assert krw.action == "convert" and krw.order is not None
    assert twd.action == "convert" and twd.order is not None
    assert krw.order.venue == "FXCONV" and krw.order.order_type == "FXCONV"
    assert twd.order.venue == "FXCONV"
    assert hkd.order is not None and hkd.order.venue == "IDEALPRO" and hkd.order.order_type == "MKT"
    assert jpy.order is not None and jpy.order.venue == "IDEALPRO"
    spec = ib_order_spec(krw.order, PIN)
    assert spec["symbol"] == "USD"
    assert spec["currency"] == "KRW"
    assert spec["action"] == "SELL"
    assert spec["exchange"] == "IDEALPRO"
    assert spec["secType"] == "CASH"
    assert spec["totalQuantity"] == pytest.approx(55.74)
    assert spec["account"] == PIN
    usd = assess(
        _buy(symbol="AAPL", currency="USD", market="US", quantity=1, limit_price=100.0),
        cash_by_ccy={"USD": 0.0},
        fx_to_base=rates,
    )
    assert usd.action == "skip" and usd.order is None
    covered = assess(_buy(), cash_by_ccy={"USD": 0.0, "KRW": 80_000.0}, fx_to_base=rates)
    assert covered.action == "covered" and covered.order is None
    short = assess(_buy(), cash_by_ccy={"USD": 10.0, "KRW": 0.0}, fx_to_base=rates)
    assert short.action == "block" and short.reason == "usd_short" and short.order is None
    unknown = assess(_buy(), cash_by_ccy=None, fx_to_base=rates)
    assert unknown.action == "block" and unknown.reason == "unknown_balance"
    refused = assess(_buy(mode="off"), cash_by_ccy={"USD": 10_000.0}, fx_to_base=rates)
    assert refused.action == "block" and refused.reason == "not_paper" and refused.order is None
    sell = assess(
        _buy(side=OrderSide.SELL, intent_type="EXIT"),
        cash_by_ccy={"USD": 10_000.0, "KRW": 0.0},
        fx_to_base=rates,
    )
    assert sell.action == "skip"


def test_working_conversion_is_not_a_second_order() -> None:
    resting = SimpleNamespace(
        order=SimpleNamespace(orderRef="fx-1"),
        orderStatus=SimpleNamespace(status="Submitted"),
    )
    done = SimpleNamespace(
        order=SimpleNamespace(orderRef="fx-1"),
        orderStatus=SimpleNamespace(status="Cancelled"),
    )
    assert fx_still_working([resting], "fx-1") is True
    assert fx_still_working([done], "fx-1") is False
    assert fx_still_working([done, resting], "fx-1") is True
    assert fx_rejected([done], "fx-1") is True
    assert fx_rejected([done, resting], "fx-1") is False
    assert fx_rejected([], "fx-1") is False


def test_balance_wait_confirms_or_stops() -> None:
    clock = {"t": 0.0}

    def monotonic() -> float:
        return clock["t"]

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    reads = iter(({"KRW": 0.0, "USD": 100.0}, {"KRW": 80_000.0, "USD": 40.0}))
    confirmed = asyncio.run(
        await_target_cash(
            lambda: next(reads),
            target="KRW",
            required=74_840.0,
            timeout_sec=5,
            poll_sec=0.5,
            sleep=sleep,
            monotonic=monotonic,
        )
    )
    assert confirmed is not None
    assert confirmed["KRW"] == pytest.approx(80_000.0)
    assert clock["t"] == pytest.approx(0.5)

    clock["t"] = 0.0
    timed = asyncio.run(
        await_target_cash(
            lambda: {"KRW": 1.0},
            target="KRW",
            required=74_840.0,
            timeout_sec=1.0,
            poll_sec=0.5,
            sleep=sleep,
            monotonic=monotonic,
        )
    )
    assert timed is None
    stopped = asyncio.run(
        await_target_cash(
            lambda: {"KRW": 0.0},
            target="KRW",
            required=10.0,
            timeout_sec=30,
            sleep=sleep,
            monotonic=monotonic,
            rejected=lambda: True,
        )
    )
    assert stopped is None


def test_balance_wait_yields_so_the_loop_can_update_cash() -> None:
    """A pause must leave the running loop free to apply the cash update."""

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        state = {"reads": 0, "posted": False}

        async def post() -> None:
            state["posted"] = True

        def read() -> dict[str, float]:
            state["reads"] += 1
            if state["reads"] == 1:
                loop.create_task(post())
                return {"TWD": 0.0}
            return {"TWD": 100.0 if state["posted"] else 0.0}

        found = await await_target_cash(
            read,
            target="TWD",
            required=10.0,
            timeout_sec=1.0,
            poll_sec=0.01,
        )
        assert found is not None
        assert found["TWD"] == pytest.approx(100.0)
        assert state["posted"] is True

    asyncio.run(scenario())


def test_account_snapshot_stores_cash_by_ccy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    monkeypatch.setenv("IB_PORT", "4002")
    wrote = account_once(
        tmp_path,
        ASIA,
        snapshot={
            "accounts": [PIN],
            "nlv": 1_000_000.0,
            "cash": 200_000.0,
            "cash_by_ccy": {"USD": 25000, "KRW": 0},
            "positions": [],
            "open_orders": [],
        },
    )
    assert wrote["wrote"] is True
    body = json.loads((tmp_path / "state" / "account.json").read_text(encoding="utf-8"))
    assert body["cash_by_ccy"] == {"USD": 25000.0, "KRW": 0.0}


def test_gate_blocks_only_when_the_check_is_on(tmp_path: Path) -> None:
    intent = _buy()
    pinned = {"approved_hash": intent.spec_hash}
    off = _ctx(tmp_path, ASIA, ["005930.KS"], cash_by_ccy={"USD": 0.0, "KRW": 0.0}, **pinned)
    assert RiskGate(off).allow(intent) is None
    short = _ctx(
        tmp_path,
        ASIA,
        ["005930.KS"],
        cash_by_ccy={"USD": 10.0, "KRW": 0.0},
        fx_cash_check=True,
        **pinned,
    )
    with pytest.raises(RiskRejected, match="R31"):
        RiskGate(short).allow(intent)
    covered = _ctx(
        tmp_path,
        ASIA,
        ["005930.KS"],
        cash_by_ccy={"USD": 10.0, "KRW": 80_000.0},
        fx_cash_check=True,
        **pinned,
    )
    assert RiskGate(covered).allow(intent) is None


def _submit(
    home: Path,
    now: datetime,
    intents: list[OrderIntent],
    ctx: GateContext,
    placed: list[OrderIntent],
    convert,
):
    return submit_once(
        home,
        now,
        intents=intents,
        ctx=ctx,
        session=_session(),
        place=placed.append,
        readonly=False,
        convert=convert,
    )


def test_short_usd_sends_no_fx_and_no_equity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_FX_PRECONVERT", "1")
    monkeypatch.delenv("TRADING_GATEWAY_CONNECT", raising=False)
    placed: list[OrderIntent] = []
    fx: list[object] = []
    intent = _buy()
    result = _submit(
        tmp_path,
        ASIA,
        [intent],
        _ctx(tmp_path, ASIA, ["005930.KS"], cash_by_ccy={"USD": 10.0, "KRW": 0.0}),
        placed,
        lambda order, _intent: fx.append(order),
    )
    assert result["sent"] == 0
    assert placed == []
    assert fx == []
    assert "R31" in result["rejected"]
    log = (tmp_path / "state" / "fx_conversions.jsonl").read_text(encoding="utf-8")
    assert "usd_short" in log


def test_unconfirmed_fx_sends_no_equity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_FX_PRECONVERT", "1")
    placed: list[OrderIntent] = []
    seen: list[object] = []

    def convert(order, _intent):
        seen.append(order)
        return {"KRW": 1.0, "USD": 9_000.0}

    result = _submit(
        tmp_path,
        ASIA,
        [_buy()],
        _ctx(tmp_path, ASIA, ["005930.KS"], cash_by_ccy={"USD": 10_000.0, "KRW": 0.0}),
        placed,
        convert,
    )
    assert result["sent"] == 0
    assert placed == []
    assert len(seen) == 1
    assert "R31" in result["rejected"]
    text = (tmp_path / "state" / "fx_conversions.jsonl").read_text(encoding="utf-8")
    assert "unconfirmed" in text


def test_confirmed_fx_then_allows_the_equity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_FX_PRECONVERT", "1")
    placed: list[OrderIntent] = []
    seen: list[object] = []

    def convert(order, _intent):
        seen.append(order)
        return {"KRW": order.required_target + 10, "USD": 9_000.0}

    intent = _buy()
    result = _submit(
        tmp_path,
        ASIA,
        [intent],
        _ctx(tmp_path, ASIA, ["005930.KS"], cash_by_ccy={"USD": 10_000.0, "KRW": 0.0}),
        placed,
        convert,
    )
    assert result["sent"] == 1
    assert [row.symbol for row in placed] == ["005930.KS"]
    assert len(seen) == 1
    spec = ib_order_spec(seen[0], PIN)
    assert spec["orderType"] == "FXCONV"
    assert spec["action"] == "SELL"
    assert spec["currency"] == "KRW"
    text = (tmp_path / "state" / "fx_conversions.jsonl").read_text(encoding="utf-8")
    assert "confirmed" in text


def test_enough_target_cash_skips_conversion(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_FX_PRECONVERT", "1")
    placed: list[OrderIntent] = []
    fx: list[object] = []
    result = _submit(
        tmp_path,
        ASIA,
        [_buy()],
        _ctx(tmp_path, ASIA, ["005930.KS"], cash_by_ccy={"USD": 0.0, "KRW": 80_000.0}),
        placed,
        lambda order, _intent: fx.append(order),
    )
    assert result["sent"] == 1
    assert fx == []
    assert placed[0].symbol == "005930.KS"


def test_flag_off_does_not_block_a_foreign_buy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TRADING_FX_PRECONVERT", raising=False)
    placed: list[OrderIntent] = []
    result = _submit(
        tmp_path,
        ASIA,
        [_buy()],
        _ctx(tmp_path, ASIA, ["005930.KS"], cash_by_ccy={"USD": 0.0, "KRW": 0.0}),
        placed,
        lambda order, _intent: (_ for _ in ()).throw(AssertionError(order)),
    )
    assert result["sent"] == 1
    assert placed[0].currency == "KRW"


def test_second_buy_sees_the_reserved_balance(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_FX_PRECONVERT", "1")
    placed: list[OrderIntent] = []
    fx: list[object] = []
    first = _buy(client_id="cid-1", intent_id="intent-1")
    second = _buy(client_id="cid-2", intent_id="intent-2", symbol="000660.KS")
    ctx = _ctx(
        tmp_path,
        ASIA,
        ["005930.KS", "000660.KS"],
        cash_by_ccy={"USD": 0.0, "KRW": 74_840.0},
    )
    ctx.quotes["000660.KS"] = Quote(last=70_000.0, as_of=ASIA, received_at=ASIA, market_data_type=1)
    result = _submit(
        tmp_path,
        ASIA,
        [first],
        ctx,
        placed,
        lambda order, _intent: fx.append(order),
    )
    assert [row.symbol for row in placed] == ["005930.KS"]
    assert fx == []
    assert result["sent"] == 1
    # The next buy in this pass sees the reserved KRW. USD is already zero, so
    # it must not place another conversion or an equity order.
    nxt = assess(
        second,
        cash_by_ccy=ctx.cash_by_ccy,
        open_orders=ctx.open_orders,
        fx_to_base=ctx.fx_to_base,
    )
    assert nxt.action == "block"
    assert nxt.reason == "usd_short"
    assert nxt.order is None


def test_usd_buy_is_not_preconverted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_FX_PRECONVERT", "1")
    placed: list[OrderIntent] = []
    fx: list[object] = []
    intent = _buy(
        symbol="AAPL",
        currency="USD",
        market="US",
        quantity=10,
        limit_price=100.0,
        client_id="cid-usd",
        intent_id="intent-usd",
        valid_from=datetime(2026, 10, 2, 21, 30, tzinfo=HKT),
    )
    ctx = _ctx(tmp_path, US, ["AAPL"], cash_by_ccy={"USD": 50_000.0})
    ctx.quotes["AAPL"] = Quote(last=100.0, as_of=US, received_at=US, market_data_type=1)
    result = _submit(tmp_path, US, [intent], ctx, placed, lambda order, _intent: fx.append(order))
    assert result["sent"] == 1
    assert fx == []
    assert placed[0].symbol == "AAPL"


def test_submit_unit_does_not_enable_preconvert() -> None:
    text = (ROOT / "vps" / "deploy" / "trading-submit.service").read_text(encoding="utf-8")
    assert "TRADING_FX_PRECONVERT" not in text


def test_convert_cash_confirms_without_reentering_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cash shows up after a yield. ib.sleep must not run inside this loop.

    The paper failure was RuntimeError from ib.sleep/util.run while
    convert_cash was already running under asyncio.run. TWD stays FXCONV
    and JPY stays an IdealPro market order.
    """

    class FakeIB:
        def __init__(self) -> None:
            self.filled = False
            self.sleep_calls = 0

        def accountValues(self) -> list[SimpleNamespace]:
            amount = "1000000" if self.filled else "0"
            usd = "9000" if self.filled else "10000"
            return [
                SimpleNamespace(tag="CashBalance", currency="USD", value=usd),
                SimpleNamespace(tag="CashBalance", currency="TWD", value=amount),
                SimpleNamespace(tag="CashBalance", currency="JPY", value=amount),
            ]

        def trades(self) -> list[object]:
            return []

        def openTrades(self) -> list[object]:
            return []

        def sleep(self, _seconds: float) -> None:
            self.sleep_calls += 1
            raise RuntimeError("This event loop is already running")

        def disconnect(self) -> None:
            return None

    fake = FakeIB()
    placed: list[dict[str, object]] = []

    async def connect(self: PaperGateway) -> list[str]:
        self._ib = fake
        return [PIN]

    def place_fx(_self: PaperGateway, _ib: object, spec: dict[str, object]) -> None:
        placed.append(spec)

    async def quick(seconds: float) -> None:
        assert seconds > 0
        fake.filled = True

    monkeypatch.setattr(PaperGateway, "connect", connect)
    monkeypatch.setattr(PaperGateway, "_place_fx", place_fx)
    monkeypatch.setattr("auto_trading.execution.fx_convert.asyncio.sleep", quick)
    gateway = PaperGateway(
        Settings(ibkr_port=4002, ibkr_paper_account=PIN, trading_home=tmp_path),
        client_id=11,
    )

    twd = assess(
        _buy(
            symbol="2317.TW",
            currency="TWD",
            market="TW",
            quantity=1000,
            limit_price=100.0,
            client_id="cid-2317",
            intent_id="intent-2317",
        ),
        cash_by_ccy={"USD": 10_000.0, "TWD": 0.0},
        fx_to_base=_rates(),
    )
    assert twd.order is not None and twd.order.venue == "FXCONV"
    confirmed = asyncio.run(gateway.convert_cash(twd.order, account=PIN))
    assert confirmed is not None
    assert confirmed["TWD"] + 1e-6 >= twd.order.required_target
    assert fake.sleep_calls == 0
    assert placed[0]["orderType"] == "FXCONV"
    assert placed[0]["currency"] == "TWD"
    assert placed[0]["exchange"] == "IDEALPRO"
    assert placed[0]["action"] == "SELL"
    assert placed[0]["symbol"] == "USD"

    fake.filled = False
    jpy = assess(
        _buy(
            symbol="7203.T",
            currency="JPY",
            market="JP",
            quantity=100,
            limit_price=1000.0,
            client_id="cid-7203",
            intent_id="intent-7203",
        ),
        cash_by_ccy={"USD": 10_000.0, "JPY": 0.0},
        fx_to_base=_rates(),
    )
    assert jpy.order is not None and jpy.order.venue == "IDEALPRO"
    confirmed_jpy = asyncio.run(gateway.convert_cash(jpy.order, account=PIN))
    assert confirmed_jpy is not None
    assert confirmed_jpy["JPY"] + 1e-6 >= jpy.order.required_target
    assert fake.sleep_calls == 0
    assert placed[1]["orderType"] == "MKT"
    assert placed[1]["currency"] == "JPY"
    assert placed[1]["venue"] == "IDEALPRO"
