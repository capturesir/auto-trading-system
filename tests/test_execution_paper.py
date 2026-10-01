"""Paper execution: R0, the main risk rules, approvals, journal, and heartbeat."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from auto_trading.broker import Balance, BrokerPosition, OrderAck, OrderIntent, OrderSide
from auto_trading.config import Settings, require_paper_credentials
from auto_trading.execution.approvals import (
    approve,
    hash12,
    needs_reapproval,
    order_hash,
    queue_order,
)
from auto_trading.execution.contracts import map_contract
from auto_trading.execution.heartbeat import heartbeat_alert_due, write_heartbeat
from auto_trading.execution.ibkr import IbkrBroker
from auto_trading.execution.pricing import session_open_limit
from auto_trading.execution.r0 import AccountGuard, PaperAccountError
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.execution.stops import cancel_keeps_protective, protective_stop_price
from auto_trading.execution.sync import reconcile
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.risk import GateContext, Held, OpenOrder, Quote, RiskGate, RiskRejected
from auto_trading.risk.limits import HASH_ODD_LOT_OFF, config_hash

HKT = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 2, 10, 0, tzinfo=HKT)


class MockClient:
    def __init__(self, accounts: list[str]) -> None:
        self.accounts = accounts
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self.modified: list[tuple[str, float]] = []

    async def managed_accounts(self) -> list[str]:
        return list(self.accounts)

    async def place_order(self, order: dict) -> OrderAck:
        self.placed.append(order)
        return OrderAck(client_id=order["client_id"], broker_order_id="paper-1")

    async def cancel_order(self, client_id: str) -> None:
        self.cancelled.append(client_id)

    async def modify_order(self, client_id: str, limit_price: float) -> None:
        self.modified.append((client_id, limit_price))

    async def balances(self) -> list[Balance]:
        return [Balance(currency="HKD", amount=1_000_000)]

    async def positions(self) -> list[BrokerPosition]:
        return [BrokerPosition(symbol="AAPL", quantity=2, average_price=100)]

    async def executions(self) -> list:
        return []

    async def order(self, client_id: str):
        from auto_trading.broker import OrderState

        return OrderState(client_id=client_id, status="Submitted")


def _buy(**kwargs: object) -> OrderIntent:
    payload: dict = {
        "client_id": "cid-1",
        "symbol": "AAPL",
        "side": OrderSide.BUY,
        "quantity": 10,
        "limit_price": 100.0,
        "currency": "HKD",
        "market": "US",
    }
    payload.update(kwargs)
    return OrderIntent(**payload)


def _ctx(**kwargs: object) -> GateContext:
    payload: dict = {
        "now": NOW,
        "quotes": {"AAPL": Quote(last=100.0, as_of=NOW), "2330.TW": Quote(last=2445.0, as_of=NOW)},
        "fx_to_base": {"TWD": 0.244, "HKD": 1.0, "USD": 1.0},
    }
    payload.update(kwargs)
    return GateContext(**payload)


def test_r0_rejects_non_du_mixed_accounts_and_real_ports(tmp_path) -> None:
    with pytest.raises(PaperAccountError, match="R0_NOT_PAPER_ACCOUNT"):
        AccountGuard(4002).observe(["U1234567"])
    with pytest.raises(PaperAccountError, match="R0_NOT_PAPER_ACCOUNT"):
        AccountGuard(4002).observe(["DU1234567", "U999"])
    for port in (4001, 7496):  # refused: paper_only
        with pytest.raises(PaperAccountError, match="R0_LIVE_PORT"):
            AccountGuard(port)
    with pytest.raises(ValidationError):
        Settings(ibkr_account="U1234567")  # refused: paper_only


def test_mid_session_account_change_blocks_orders_but_not_cancels(tmp_path) -> None:
    client = MockClient(["DU1234567"])
    broker = IbkrBroker(client, trading_home=tmp_path)

    async def scenario() -> None:
        await broker.place(_buy(account="DU1234567"))
        client.accounts = ["DU1234567", "DU7654321"]
        with pytest.raises(PaperAccountError, match="R0_ACCOUNTS_CHANGED"):
            await broker.place(_buy(client_id="cid-2", account="DU1234567"))
        with pytest.raises(PaperAccountError, match="R0_ACCOUNTS_CHANGED"):
            await broker.modify("cid-1", 101.0, account="DU1234567")
        await broker.cancel("cid-1")

    asyncio.run(scenario())
    assert len(client.placed) == 1
    assert client.modified == []
    assert client.cancelled == ["cid-1"]
    assert (tmp_path / "KILL_SWITCH").read_text(encoding="utf-8").startswith("R0_")
    assert cancel_keeps_protective(protective=True, full_stop=True) is True


def test_gate_rules_and_reapproval() -> None:
    assert RiskGate(_ctx()).allow(_buy()) is None
    with pytest.raises(RiskRejected, match="R1"):
        RiskGate(_ctx()).allow(_buy(quantity=600, limit_price=100))
    wide = _ctx(nlv=10_000_000, cash=10_000_000, hwm=10_000_000, day_start_nlv=10_000_000)
    with pytest.raises(RiskRejected, match="R2"):
        RiskGate(wide).allow(_buy(quantity=2500, limit_price=100))
    held = _ctx(positions=[Held("AAPL", 1, 90_000)])
    with pytest.raises(RiskRejected, match="R3"):
        RiskGate(held).allow(_buy(quantity=200, limit_price=100))
    with pytest.raises(RiskRejected, match="R8"):
        RiskGate(_ctx(nlv=970_000)).allow(_buy())
    sell = _buy(side=OrderSide.SELL, intent_type="EXIT", parent_intent_id="entry-1", quantity=1)
    assert RiskGate(_ctx(nlv=970_000, positions=[Held("AAPL", 5, 500)])).allow(sell) is None
    with pytest.raises(RiskRejected, match="R9"):
        RiskGate(_ctx(nlv=890_000, day_start_nlv=890_000)).allow(_buy())
    dup = _ctx(open_orders=[OpenOrder("cid-1", "AAPL", "BUY")])
    with pytest.raises(RiskRejected, match="R11"):
        RiskGate(dup).allow(_buy())
    with pytest.raises(RiskRejected, match="R16"):
        RiskGate(_ctx()).allow(_buy(limit_price=110))
    with pytest.raises(RiskRejected, match="R21"):
        RiskGate(_ctx(macro_blackout=True)).allow(_buy())
    stale = _ctx(quotes={"AAPL": Quote(last=100, as_of=NOW - timedelta(seconds=11))})
    with pytest.raises(RiskRejected, match="R22"):
        RiskGate(stale).allow(_buy())
    mismatch = _ctx(approved_hash="sha256:" + "ab" * 32)
    with pytest.raises(RiskRejected, match="R25"):
        RiskGate(mismatch).allow(_buy(spec_hash="sha256:" + "cd" * 32))
    drifted = _ctx(stage="final", approval_price=100)
    with pytest.raises(RiskRejected, match="REAPPROVAL_REQUIRED"):
        RiskGate(drifted).allow(_buy(limit_price=102))
    refused = _buy()
    refused.mode = "live"  # refused: paper_only_violation
    with pytest.raises(RiskRejected, match="LIVE_NOT_AUTHORIZED"):
        RiskGate(_ctx()).allow(refused)
    tw = _buy(
        symbol="2330.TW",
        market="TW",
        currency="TWD",
        quantity=1000,
        limit_price=2445,
        lot_size=1000,
    )
    with pytest.raises(RiskRejected, match="LOT_TOO_LARGE"):
        RiskGate(_ctx()).allow(tw)


def test_session_open_price_buffer_and_disaster_stop() -> None:
    opened = datetime(2026, 10, 2, 9, 30, tzinfo=HKT)
    plan = session_open_limit(
        market="US",
        side="BUY",
        now=opened + timedelta(minutes=5),
        session_open_at=opened,
        open_price=100,
        bid=None,
        ask=None,
        last=None,
        decision_ref=100,
    )
    assert plan.ready
    assert plan.ref_price_mode == "session_open"
    assert plan.session_open_price == 100
    assert plan.limit_price == pytest.approx(100.1)
    assert plan.ref_price_at_submit == 100
    early = session_open_limit(
        market="US",
        side="BUY",
        now=opened + timedelta(minutes=4),
        session_open_at=opened,
        open_price=100,
        bid=None,
        ask=None,
        last=None,
        decision_ref=100,
    )
    assert early.ready is False and early.reason == "R10"
    fallback = session_open_limit(
        market="US",
        side="BUY",
        now=opened + timedelta(minutes=6),
        session_open_at=opened,
        open_price=None,
        bid=100,
        ask=100.2,
        last=100.2,
        decision_ref=100,
    )
    assert fallback.ref_price_mode == "live_quote"
    assert fallback.ref_price_at_submit == pytest.approx(100.1)
    assert protective_stop_price(100, 0.06) == pytest.approx(91)
    assert protective_stop_price(100, None) == pytest.approx(85)


def test_risk_limits_file_uses_the_shared_hash(tmp_path) -> None:
    payload = build_risk_limits(now=NOW, nlv=1_000_000, account="DU1234567")
    path = write_risk_limits(tmp_path, payload)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == "risk_limits/1.1"
    assert saved["trading_scope"] == "paper_only"
    assert saved["config_hash"] == HASH_ODD_LOT_OFF
    assert config_hash(saved["limits"]) == HASH_ODD_LOT_OFF
    assert saved["config_hash"].startswith("sha256:02460f0fef18")
    assert "trading_scope" not in saved["limits"]


def test_journal_why_and_approval_and_heartbeat(tmp_path) -> None:
    journal = TradeJournal(tmp_path)
    journal.record_intent(
        {
            "intent_id": "entry-1",
            "strategy_id": "sig_trend",
            "strategy_version": 2,
            "spec_hash": "sha256:" + "ab" * 32,
            "signal_id": "sig-2330",
        }
    )
    journal.record_fill(
        fill_id="fill-1",
        intent_id="entry-1",
        client_id="cid-1",
        symbol="AAPL",
        price=100,
        quantity=10,
        when=NOW,
    )
    traced = journal.why("fill-1")
    assert traced["signal_id"] == "sig-2330"
    assert traced["spec_hash"].endswith("ab" * 32)
    assert traced["strategy_id"] == "sig_trend"
    payload = {"symbol": "AAPL", "quantity": 10, "limit_price": 100}
    queue_order(tmp_path, "ord-1", payload)
    with pytest.raises(Exception, match="hash"):
        approve(tmp_path, "ord-1", "0" * 12)
    decided = approve(tmp_path, "ord-1", hash12(order_hash(payload)))
    assert decided["status"] == "APPROVED"
    assert needs_reapproval(100, 102) is True
    assert needs_reapproval(100, 101) is False
    beat = tmp_path / "state" / "heartbeat.json"
    write_heartbeat(beat, NOW)
    assert heartbeat_alert_due(beat, NOW + timedelta(seconds=119)) is False
    assert heartbeat_alert_due(beat, NOW + timedelta(seconds=120)) is True
    breaks = reconcile(
        [BrokerPosition(symbol="AAPL", quantity=2, average_price=1)],
        {"AAPL": 1},
    )
    assert breaks[0].broker_qty == 2 and breaks[0].journal_qty == 1


def test_contract_maps_2330_and_credentials_stay_secret() -> None:
    tw = map_contract("2330.TW")
    assert tw.ib_symbol == "2330"
    assert tw.exchange == "TWSE"
    assert tw.unverified is True
    from auto_trading.execution.contracts import UnsupportedInstrument

    with pytest.raises(UnsupportedInstrument):
        map_contract("6488.TWO")
    otc = map_contract("6488.TWO", allow_tpex=True)
    assert otc.exchange == "TPEX"
    assert otc.market == "TW"
    assert otc.unverified is True
    settings = Settings(ibkr_paper_password="sekret-value")
    assert "sekret-value" not in repr(settings)
    with pytest.raises(RuntimeError, match="IBKR_PAPER_USERNAME"):
        require_paper_credentials(Settings())


def test_trade_cli_stop_and_local_resume(tmp_path) -> None:
    from auto_trading.cli_trade import main

    main(["--home", str(tmp_path), "stop"])
    assert (tmp_path / "KILL_SWITCH").is_file()
    with pytest.raises(SystemExit):
        main(["--home", str(tmp_path), "resume"])
    main(["--home", str(tmp_path), "resume", "--local"])
    assert not (tmp_path / "KILL_SWITCH").exists()
    account = {"nlv": 890_000, "hwm": 1_000_000, "day_start_nlv": 1_000_000}
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "account.json").write_text(json.dumps(account), encoding="utf-8")
    main(["--home", str(tmp_path), "stop"])
    with pytest.raises(SystemExit):
        main(["--home", str(tmp_path), "resume", "--local"])
    main(["--home", str(tmp_path), "resume", "--local", "--reset-hwm"])
    saved = json.loads((state / "account.json").read_text(encoding="utf-8"))
    assert saved["hwm"] == 890_000
