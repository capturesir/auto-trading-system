"""Taiwan/Korea cash gate, mode A (補7b #19).

Paper only. Target cash that already covers the buy sends the stock and
no FX. A shortfall sends the stock only when ``TWKR_AUTO_FX_VERIFIED=1``
and USD plus allowed HKD cover the estimate. Otherwise the gate holds it.
Negative TWD/KRW cash alerts and blocks new opens. It never clears itself
with an API FX order. HKD IdealPro preconvert is covered in
``test_fx_preconvert.py`` and is not replaced here.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.execution.approval_mode import ApprovalSession
from auto_trading.execution.fx_convert import (
    assess,
    standalone_fx_forbidden,
    twkr_gate_code,
    venue_for,
)
from auto_trading.execution.services import submit_once
from auto_trading.execution.strategy_book import CONFIRMED_SPEC_HASHES
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.risk.gate import GateContext, Held, Quote, evaluate
from auto_trading.risk.limits import HASH_V12_ODD_LOT_OFF, config_hash, limits_v12

PIN = "DUR243736"
HKT_ZONE = ZoneInfo("Asia/Hong_Kong")
ASIA = datetime(2026, 10, 2, 11, 0, tzinfo=HKT_ZONE)
RATES = {"HKD": 1.0, "USD": 7.8, "KRW": 0.0056, "TWD": 0.244, "JPY": 0.052}


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
        "valid_from": datetime(2026, 10, 2, 9, 30, tzinfo=HKT_ZONE),
    }
    payload.update(kwargs)
    return OrderIntent(**payload)


def _ctx(home: Path, **kwargs: object) -> GateContext:
    from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits

    write_risk_limits(home, build_risk_limits(now=ASIA, nlv=1_000_000, account=PIN))
    quotes = {
        "005930.KS": Quote(last=70_000.0, as_of=ASIA, received_at=ASIA, market_data_type=1),
        "2330.TW": Quote(last=100.0, as_of=ASIA, received_at=ASIA, market_data_type=1),
    }
    payload: dict[str, object] = {
        "now": ASIA,
        "trading_home": home,
        "expected_account": PIN,
        "accounts": (PIN,),
        "baseline_accounts": (PIN,),
        "nlv": 1_000_000.0,
        "cash": 1_000_000.0,
        "hwm": 1_000_000.0,
        "day_start_nlv": 1_000_000.0,
        "quotes": quotes,
        "fx_to_base": dict(RATES),
        "port": 4002,
        "approved_hash": CONFIRMED_SPEC_HASHES[("ta_breakout52", 1)],
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


def _submit(home: Path, intent: OrderIntent, ctx: GateContext, placed: list, convert):
    return submit_once(
        home,
        ASIA,
        intents=[intent],
        ctx=ctx,
        session=_session(),
        place=placed.append,
        readonly=False,
        convert=convert,
    )


def _risk_details(home: Path) -> list[dict]:
    return TradeJournal(home).summary_rejections(ASIA - timedelta(days=1), ASIA + timedelta(days=1))


def _alert_kinds(home: Path) -> list[str]:
    found: list[str] = []
    for folder in (home / "alerts" / "pending", home / "notifications" / "outbox"):
        if not folder.is_dir():
            continue
        for path in folder.glob("*.json"):
            body = json.loads(path.read_text(encoding="utf-8"))
            found.append(str(body.get("kind")))
    return found


def test_account_snapshot_alerts_negative_krw_without_fx(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    monkeypatch.setenv("IB_PORT", "4002")
    from auto_trading.execution.services import account_once

    wrote = account_once(
        tmp_path,
        ASIA,
        snapshot={
            "accounts": [PIN],
            "nlv": 1_000_000.0,
            "cash_by_ccy": {"USD": 1000.0, "KRW": -12.5},
            "positions": [],
            "open_orders": [],
        },
    )
    assert wrote["wrote"] is True
    assert "fx_target_cash_deficit" in _alert_kinds(tmp_path)
    assert not (tmp_path / "state" / "fx_conversions.jsonl").exists()
    pending = next((tmp_path / "alerts" / "pending").glob("*.json"))
    body = json.loads(pending.read_text(encoding="utf-8"))
    assert body["clear_fx"] is False
    assert body["currency"] == "KRW"


def test_limits_hash_stays_on_v12() -> None:
    assert config_hash(limits_v12()) == HASH_V12_ODD_LOT_OFF
    assert HASH_V12_ODD_LOT_OFF.startswith("sha256:effd2188")


def test_no_standalone_fx_venue_for_tw_kr() -> None:
    assert standalone_fx_forbidden("TWD")
    assert standalone_fx_forbidden("KRW")
    assert standalone_fx_forbidden("twd")
    for currency in ("TWD", "KRW"):
        with pytest.raises(ValueError, match="retired"):
            venue_for(currency)


def test_target_cash_enough_sends_stock_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TWKR_AUTO_FX_VERIFIED", raising=False)
    placed: list[OrderIntent] = []
    fx: list[object] = []
    intent = _buy()
    result = _submit(
        tmp_path,
        intent,
        _ctx(tmp_path, cash_by_ccy={"USD": 0.0, "KRW": 80_000.0}),
        placed,
        lambda order, _intent: fx.append(order),
    )
    assert result["sent"] == 1
    assert [row.symbol for row in placed] == ["005930.KS"]
    assert fx == []
    text = (tmp_path / "state" / "fx_conversions.jsonl").read_text(encoding="utf-8")
    assert "FXCONV" not in text
    assert "target_cash" in text
    gate = json.loads((tmp_path / "state" / "fx_cash_gate.json").read_text(encoding="utf-8"))
    assert gate["mode"] == "A"
    assert gate["reason"] is None
    assert gate["twkr_auto_fx_verified"] is False


def test_env_off_gates_when_target_cash_is_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TWKR_AUTO_FX_VERIFIED", raising=False)
    placed: list[OrderIntent] = []
    fx: list[object] = []
    result = _submit(
        tmp_path,
        _buy(),
        _ctx(tmp_path, cash_by_ccy={"USD": 10_000.0, "KRW": 0.0, "HKD": 0.0}),
        placed,
        lambda order, _intent: fx.append(order),
    )
    assert result["sent"] == 0
    assert placed == []
    assert fx == []
    assert result["rejected"] == ["R31_TWKR_AUTO_FX_UNVERIFIED"]
    rows = _risk_details(tmp_path)
    assert rows[0]["code"] == "R31_TWKR_AUTO_FX_UNVERIFIED"
    assert rows[0]["detail"]["mode"] == "A"
    assert rows[0]["detail"]["verified"] is False
    assert rows[0]["detail"]["phase"] == "pre"
    assert rows[0]["detail"]["have_target"] == pytest.approx(0.0)
    assert rows[0]["detail"]["detail"] == "auto_fx_unverified"
    text = (tmp_path / "state" / "fx_conversions.jsonl").read_text(encoding="utf-8")
    assert "FXCONV" not in text
    assert "IDEALPRO" not in text


def test_env_on_and_usd_enough_sends_stock_without_fx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWKR_AUTO_FX_VERIFIED", "1")
    monkeypatch.setenv("TRADING_FX_PRECONVERT", "1")
    placed: list[OrderIntent] = []
    fx: list[object] = []
    result = _submit(
        tmp_path,
        _buy(symbol="2330.TW", currency="TWD", market="TW", quantity=1000, limit_price=100.0),
        _ctx(tmp_path, cash_by_ccy={"USD": 10_000.0, "TWD": 0.0}),
        placed,
        lambda order, _intent: fx.append(order),
    )
    assert result["sent"] == 1
    assert [row.symbol for row in placed] == ["2330.TW"]
    assert fx == []
    text = (tmp_path / "state" / "fx_conversions.jsonl").read_text(encoding="utf-8")
    assert "auto_fx" in text
    assert "FXCONV" not in text
    gate = json.loads((tmp_path / "state" / "fx_cash_gate.json").read_text(encoding="utf-8"))
    assert gate["mode"] == "A"
    assert gate["twkr_auto_fx_verified"] is True
    assert gate["reason"] is None


def test_usd_and_hkd_short_is_r7_base_cash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWKR_AUTO_FX_VERIFIED", "1")
    placed: list[OrderIntent] = []
    fx: list[object] = []
    result = _submit(
        tmp_path,
        _buy(),
        _ctx(tmp_path, cash_by_ccy={"USD": 1.0, "HKD": 10.0, "KRW": 0.0}),
        placed,
        lambda order, _intent: fx.append(order),
    )
    assert result["sent"] == 0
    assert placed == []
    assert fx == []
    assert "R7_BASE_CASH" in result["rejected"]
    detail = _risk_details(tmp_path)[0]["detail"]
    assert detail["mode"] == "A"
    assert detail["verified"] is True
    assert detail["detail"] == "usd_short"
    assert "BASE_CCY_NOT_USD" not in result["rejected"]


def test_allowed_hkd_covers_when_usd_is_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWKR_AUTO_FX_VERIFIED", "1")
    placed: list[OrderIntent] = []
    fx: list[object] = []
    result = _submit(
        tmp_path,
        _buy(),
        _ctx(tmp_path, cash_by_ccy={"USD": 0.0, "HKD": 5_000.0, "KRW": 0.0}),
        placed,
        lambda order, _intent: fx.append(order),
    )
    assert result["sent"] == 1
    assert fx == []
    assert placed[0].currency == "KRW"


def test_missing_fx_rate_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWKR_AUTO_FX_VERIFIED", "1")
    placed: list[OrderIntent] = []
    result = _submit(
        tmp_path,
        _buy(),
        _ctx(
            tmp_path,
            cash_by_ccy={"USD": 10_000.0, "KRW": 0.0},
            fx_to_base={"HKD": 1.0, "KRW": 0.0056},
        ),
        placed,
        lambda order, _intent: (_ for _ in ()).throw(AssertionError(order)),
    )
    assert result["sent"] == 0
    assert placed == []
    assert "R7_BASE_CASH" in result["rejected"]
    assert _risk_details(tmp_path)[0]["detail"]["detail"] == "no_fx_rate"


def test_negative_target_cash_alerts_and_blocks_new_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWKR_AUTO_FX_VERIFIED", "1")
    placed: list[OrderIntent] = []
    fx: list[object] = []
    cash = {"USD": 50_000.0, "KRW": -20.0, "TWD": 0.0}
    result = _submit(
        tmp_path,
        _buy(),
        _ctx(tmp_path, cash_by_ccy=cash),
        placed,
        lambda order, _intent: fx.append(order),
    )
    assert result["sent"] == 0
    assert placed == []
    assert fx == []
    assert "fx_target_cash_deficit" in result["rejected"]
    assert "fx_target_cash_deficit" in _alert_kinds(tmp_path)
    text = (tmp_path / "state" / "fx_conversions.jsonl").read_text(encoding="utf-8")
    assert "FXCONV" not in text
    assert "IdealPro" not in text and "IDEALPRO" not in text
    hold = json.loads(
        (tmp_path / "state" / "fx_cash_hold" / "KRW.json").read_text(encoding="utf-8")
    )
    assert hold["cash"] == pytest.approx(-20.0)
    gate = json.loads((tmp_path / "state" / "fx_cash_gate.json").read_text(encoding="utf-8"))
    assert gate["target_cash_deficit"]["KRW"] == pytest.approx(-20.0)
    alerts_before = _alert_kinds(tmp_path).count("fx_target_cash_deficit")
    again = _submit(
        tmp_path,
        _buy(client_id="cid-2", intent_id="intent-2"),
        _ctx(tmp_path, cash_by_ccy=cash),
        placed,
        lambda order, _intent: fx.append(order),
    )
    assert again["sent"] == 0
    assert fx == []
    assert _alert_kinds(tmp_path).count("fx_target_cash_deficit") == alerts_before


def test_negative_cash_does_not_block_an_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWKR_AUTO_FX_VERIFIED", "1")
    sell = _buy(
        side=OrderSide.SELL,
        intent_type="EXIT",
        parent_intent_id="entry-1",
        reduce_only=True,
        client_id="cid-exit",
        intent_id="intent-exit",
    )
    ctx = _ctx(
        tmp_path,
        cash_by_ccy={"USD": 10_000.0, "KRW": -50.0},
        positions=[Held("005930.KS", 1, 70_000.0, strategy_id="ta_breakout52", currency="KRW")],
    )
    assert evaluate(sell, ctx) is None
    attempt = assess(sell, cash_by_ccy=ctx.cash_by_ccy, fx_to_base=RATES)
    assert attempt.action == "skip"
    assert twkr_gate_code(attempt) is None
    placed: list[OrderIntent] = []
    result = _submit(
        tmp_path,
        sell,
        ctx,
        placed,
        lambda order, _intent: (_ for _ in ()).throw(AssertionError(order)),
    )
    assert result["sent"] == 1
    assert placed[0].side.value == "SELL"
    assert "fx_target_cash_deficit" not in result["rejected"]


def test_weird_base_warns_and_does_not_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TWKR_AUTO_FX_VERIFIED", raising=False)
    placed: list[OrderIntent] = []
    result = _submit(
        tmp_path,
        _buy(),
        _ctx(tmp_path, cash_by_ccy={"USD": 0.0, "KRW": 80_000.0}, account_base="EUR"),
        placed,
        lambda order, _intent: (_ for _ in ()).throw(AssertionError(order)),
    )
    assert result["sent"] == 1
    assert "BASE_CCY_NOT_USD" not in result["rejected"]
    notices = list((tmp_path / "notifications" / "outbox").glob("*ops.json"))
    assert notices
    body = json.loads(notices[0].read_text(encoding="utf-8"))
    assert body["level"] == "warn"
    assert body["kind"] == "ops"
    assert "EUR" in body["message_zh"]
    assert "BASE_CCY_NOT_USD" in body["message_zh"]
    detail_path = tmp_path / "state" / "fx_cash_gate.json"
    gate = json.loads(detail_path.read_text(encoding="utf-8"))
    assert gate["detail"]["account_base"] == "EUR"
    assert gate["detail"]["mode"] == "A"


def test_assess_never_returns_an_fx_order_for_tw_kr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWKR_AUTO_FX_VERIFIED", "1")
    cases = [
        {"USD": 10_000.0, "KRW": 80_000.0},
        {"USD": 10_000.0, "KRW": 0.0},
        {"USD": 1.0, "KRW": 0.0, "HKD": 1.0},
        {"USD": 0.0, "HKD": 20_000.0, "TWD": 0.0},
        {"KRW": -5.0, "USD": 10_000.0},
    ]
    for cash in cases:
        for currency, symbol, market, qty, price in (
            ("KRW", "005930.KS", "KR", 1, 70_000.0),
            ("TWD", "2330.TW", "TW", 1000, 100.0),
        ):
            attempt = assess(
                _buy(
                    symbol=symbol,
                    currency=currency,
                    market=market,
                    quantity=qty,
                    limit_price=price,
                ),
                cash_by_ccy=cash,
                fx_to_base=RATES,
            )
            assert attempt.order is None
            if attempt.action == "convert":
                raise AssertionError(cash)
    monkeypatch.delenv("TWKR_AUTO_FX_VERIFIED", raising=False)
    blocked = assess(
        _buy(),
        cash_by_ccy={"USD": 10_000.0, "KRW": 0.0},
        fx_to_base=RATES,
    )
    assert blocked.order is None
    assert twkr_gate_code(blocked) == "R31_TWKR_AUTO_FX_UNVERIFIED"
