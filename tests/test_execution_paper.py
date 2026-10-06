"""Paper execution: R0, the main risk rules, approvals, journal, and heartbeat."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from auto_trading.broker import Balance, BrokerPosition, OrderAck, OrderIntent, OrderSide
from auto_trading.config import Settings
from auto_trading.execution.approvals import (
    approve,
    hash12,
    needs_reapproval,
    queue_order,
)
from auto_trading.execution.contracts import (
    UNVERIFIED_EXCHANGES,
    ib_contract_fields,
    map_contract,
    qualify_with,
)
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
# 22:00 Macau is 10:00 New York, inside the US cash session.
NOW = datetime(2026, 10, 2, 22, 0, tzinfo=HKT)


class MockClient:
    def __init__(self, accounts: list[str]) -> None:
        self.accounts = accounts
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self.modified: list[tuple[str, float]] = []
        self.disconnected = False

    def disconnect(self) -> None:
        self.disconnected = True

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
    home = kwargs.pop("trading_home", None)
    caller_now = "now" in kwargs
    caller_quotes = "quotes" in kwargs
    if home is None:
        home = Path(tempfile.mkdtemp())
    root = Path(home)
    if not (root / "state" / "risk_limits.json").is_file():
        write_risk_limits(root, build_risk_limits(now=NOW, nlv=1_000_000, account="DU1234567"))
    payload: dict = {
        "now": NOW,
        "trading_home": root,
        "quotes": {
            "AAPL": Quote(last=100.0, as_of=NOW, received_at=NOW, market_data_type=1),
            "2330.TW": Quote(last=2445.0, as_of=NOW, received_at=NOW, market_data_type=1),
        },
        "fx_to_base": {"TWD": 0.244, "HKD": 1.0, "USD": 1.0, "JPY": 0.05},
    }
    payload.update(kwargs)
    if caller_now and not caller_quotes:
        moment = payload["now"]
        payload["quotes"] = {
            symbol: Quote(
                last=row.last,
                as_of=moment,
                received_at=moment,
                market_data_type=row.market_data_type,
                source=row.source,
                data_type=row.data_type,
            )
            for symbol, row in payload["quotes"].items()
        }
    if "expected_account" not in kwargs and not os.environ.get("IBKR_PAPER_ACCOUNT"):
        accounts = payload.get("accounts")
        if isinstance(accounts, tuple) and len(accounts) == 1:
            payload["expected_account"] = accounts[0]
        else:
            payload["expected_account"] = "DU1234567"
    return GateContext(**payload)


def test_r0_rejects_non_du_mixed_accounts_and_real_ports(tmp_path) -> None:
    with pytest.raises(PaperAccountError, match="R0_NOT_PAPER_ACCOUNT"):
        AccountGuard(4002)
    with pytest.raises(PaperAccountError, match="R0_NOT_PAPER_ACCOUNT"):
        AccountGuard(4002, "DU1234567").observe(["U1234567"])
    with pytest.raises(PaperAccountError, match="R0_NOT_PAPER_ACCOUNT"):
        AccountGuard(4002, "DU1234567").observe(["DU1234567", "U999"])
    for port in (4001, 7496):  # refused: paper_only
        with pytest.raises(PaperAccountError, match="R0_FORBIDDEN_PORT"):
            AccountGuard(port)
    with pytest.raises(ValidationError):
        Settings(ibkr_account="U1234567")  # refused: paper_only


def test_paper_account_pattern_accepts_one_optional_letter() -> None:
    AccountGuard(4002, "DUR243736").observe(["DUR243736"])
    AccountGuard(4002, "DU1234567").observe(["DU1234567"])
    assert RiskGate(_ctx(accounts=("DUR243736",))).allow(_buy()) is None
    assert RiskGate(_ctx(accounts=("DU1234567",))).allow(_buy()) is None
    assert Settings(ibkr_account="DUR243736").ibkr_account == "DUR243736"
    assert Settings(ibkr_paper_account="DU1234567").ibkr_paper_account == "DU1234567"
    for refused in ("U1234567", "DUX", "DURR123"):
        with pytest.raises(PaperAccountError, match="R0_NOT_PAPER_ACCOUNT"):
            AccountGuard(4002, refused).observe([refused])
        with pytest.raises(RiskRejected, match="R0_NOT_PAPER_ACCOUNT"):
            RiskGate(_ctx(accounts=(refused,))).allow(_buy())
        with pytest.raises(ValidationError):
            Settings(ibkr_account=refused)  # refused: paper_only
        with pytest.raises(ValidationError):
            Settings(ibkr_paper_account=refused)  # refused: paper_only


def test_paper_account_pin_must_be_the_only_account(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", "DUR243736")
    AccountGuard(4002).observe(["DUR243736"])
    pinned = RiskGate(_ctx(accounts=("DUR243736",))).allow(_buy(account="DUR243736"))
    assert pinned is None

    async def refused(accounts: list[str], home: Path) -> None:
        client = MockClient(accounts)
        broker = IbkrBroker(client, trading_home=home)

        with pytest.raises(PaperAccountError, match="R0_ACCOUNTS_CHANGED"):
            await broker.place(_buy(account="DUR243736"))
        assert client.disconnected
        with pytest.raises(PaperAccountError, match="R0_ACCOUNTS_CHANGED"):
            await broker.modify("cid-1", 101.0, account="DUR243736")
        await broker.cancel("cid-1")
        assert (home / "KILL_SWITCH").read_text(encoding="utf-8").startswith("R0_ACCOUNTS_CHANGED")

    asyncio.run(refused(["DU1234567"], tmp_path / "mismatch"))
    asyncio.run(refused(["DUR243736", "DU1234567"], tmp_path / "extra"))
    with pytest.raises(RiskRejected, match="R0_ACCOUNTS_CHANGED"):
        RiskGate(_ctx(accounts=("DU1234567",))).allow(_buy())
    with pytest.raises(RiskRejected, match="R0_ACCOUNTS_CHANGED"):
        RiskGate(_ctx(accounts=("DUR243736", "DU1234567"))).allow(_buy(account="DUR243736"))


def test_mid_session_account_change_blocks_orders_but_not_cancels(tmp_path) -> None:
    client = MockClient(["DU1234567"])
    broker = IbkrBroker(client, trading_home=tmp_path, expected_account="DU1234567")

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
    stale = _ctx(
        quotes={
            "AAPL": Quote(
                last=100,
                as_of=NOW,
                received_at=NOW - timedelta(seconds=11),
                market_data_type=1,
            )
        }
    )
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
    tw_now = datetime(2026, 10, 2, 11, 0, tzinfo=HKT)
    with pytest.raises(RiskRejected, match="LOT_TOO_LARGE"):
        RiskGate(_ctx(now=tw_now)).allow(tw)


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
    assert fallback.ready is False
    assert fallback.reason == "OPEN_UNAVAILABLE"
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
    queued = queue_order(tmp_path, "ord-1", payload)
    stored = json.loads(queued.read_text(encoding="utf-8"))
    with pytest.raises(Exception, match="hash"):
        approve(tmp_path, "ord-1", "0" * 12)
    decided = approve(tmp_path, "ord-1", hash12(stored["order_hash"]))
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


def test_korean_qualify_uses_krx() -> None:
    fields = ib_contract_fields("005930.KS")
    assert fields == {
        "symbol": "005930",
        "secType": "STK",
        "exchange": "KRX",
        "currency": "KRW",
    }
    assert "KSE" not in fields.values()
    assert ib_contract_fields("035720.KQ")["exchange"] == "KRX"
    assert map_contract("005930.KS").unverified is False
    assert "KSE" not in UNVERIFIED_EXCHANGES
    assert "KRX" not in UNVERIFIED_EXCHANGES

    found = qualify_with(
        "005930.KS",
        lambda _fields: [{"conId": 17382528, "primaryExchange": "KRX"}],
    )
    assert found["con_id"] == 17382528
    assert found["primary_exchange"] == "KRX"
    assert found["error"] is None
    assert found["fields"]["exchange"] == "KRX"

    # An account without Korea stock permission can still return no contract.
    unknown = qualify_with("005930.KS", lambda _fields: [])
    assert unknown["con_id"] is None
    assert unknown["error"] == "Unknown contract"


def test_contract_maps_2330_and_credentials_stay_with_the_launcher(tmp_path, monkeypatch) -> None:
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
    trading = Path("src/auto_trading/config.py").read_text(encoding="utf-8")
    gateway = Path("src/auto_trading/execution/ibkr.py").read_text(encoding="utf-8")
    assert "PAPER_USERNAME" not in trading
    assert "PAPER_PASSWORD" not in trading
    assert "PAPER_USERNAME" not in gateway
    settings = Settings()
    assert "ibkr_paper_username" not in Settings.model_fields
    assert "ibkr_paper_password" not in Settings.model_fields
    assert "sekret-value" not in repr(settings)
    launcher = _launcher()
    monkeypatch.delenv("IBKR_PAPER_USERNAME", raising=False)
    monkeypatch.delenv("IBKR_PAPER_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="IBKR_PAPER_USERNAME"):
        launcher.write_ibc_config(tmp_path / "missing.ini")
    monkeypatch.setenv("IBKR_PAPER_USERNAME", "paper-user")
    monkeypatch.setenv("IBKR_PAPER_PASSWORD", "sekret-value")
    written = launcher.write_ibc_config(tmp_path / "ibc.ini")
    text = written.read_text(encoding="utf-8")
    assert "TradingMode=paper" in text
    assert "OverrideTwsApiPort=4002" in text
    assert "ReadOnlyApi=yes" in text
    assert "paper-user" in text
    assert "sekret-value" in text
    assert written.stat().st_mode & 0o777 == 0o600
    assert "sekret-value" not in repr(Settings())


def _launcher():
    spec = importlib.util.spec_from_file_location(
        "launch_ib_gateway",
        Path("scripts/launch_ib_gateway.py"),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _tty(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: enabled)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: enabled)
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)


def test_trade_cli_stop_and_local_resume(tmp_path, monkeypatch) -> None:
    from auto_trading.cli_trade import main

    main(["--home", str(tmp_path), "stop"])
    assert (tmp_path / "KILL_SWITCH").is_file()
    with pytest.raises(SystemExit):
        main(["--home", str(tmp_path), "resume"])
    _tty(monkeypatch, enabled=False)
    with pytest.raises(SystemExit):
        main(["--home", str(tmp_path), "resume", "--local"])
    _tty(monkeypatch, enabled=True)
    main(["--home", str(tmp_path), "resume", "--local"])
    assert not (tmp_path / "KILL_SWITCH").exists()
    main(["--home", str(tmp_path), "stop"])
    monkeypatch.setenv("SSH_CONNECTION", "203.0.113.4 22 198.51.100.8 4002")
    with pytest.raises(SystemExit):
        main(["--home", str(tmp_path), "resume", "--local"])
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
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


def test_gate_rereads_risk_file_and_refuses_a_mismatch(tmp_path) -> None:
    ctx = _ctx(trading_home=tmp_path)
    assert RiskGate(ctx).allow(_buy()) is None
    path = tmp_path / "state" / "risk_limits.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["trading_scope"] = "other"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RiskRejected, match="RISK_CONFIG_MISMATCH"):
        RiskGate(ctx).allow(_buy())
    payload["trading_scope"] = "paper_only"
    payload["config_hash"] = "sha256:" + "ab" * 32
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RiskRejected, match="RISK_CONFIG_MISMATCH"):
        RiskGate(ctx).allow(_buy())
    path.unlink()
    with pytest.raises(RiskRejected, match="RISK_CONFIG_MISMATCH"):
        RiskGate(ctx).allow(_buy())


def test_currency_exposure_is_separate_from_market_exposure() -> None:
    positions = [
        Held("0700.HK", 100, 0.0, currency="USD", market_value_local=80_000),
        Held("AAPL", 1, 0.0, currency="USD", market_value_local=1_000),
    ]
    ctx = _ctx(
        positions=positions,
        fx_to_base={"USD": 7.8, "HKD": 1.0, "JPY": 0.05, "TWD": 0.244},
    )
    with pytest.raises(RiskRejected, match="R5"):
        RiskGate(ctx).allow(_buy(symbol="MSFT", quantity=10, limit_price=100, currency="USD"))


def test_session_windows_block_entries_and_auctions() -> None:
    from auto_trading.execution.sessions import session_window

    us_open = datetime(2026, 10, 2, 21, 32, tzinfo=HKT)
    assert session_window("US", us_open) == "open_buffer"
    early = _buy()
    with pytest.raises(RiskRejected, match="R10"):
        RiskGate(_ctx(now=us_open)).allow(early)
    sell = _buy(side=OrderSide.SELL, intent_type="EXIT", parent_intent_id="entry-1", quantity=1)
    assert RiskGate(_ctx(now=us_open, positions=[Held("AAPL", 5, 500)])).allow(sell) is None
    us_close = datetime(2026, 10, 3, 3, 55, tzinfo=HKT)
    assert session_window("US", us_close) == "close_buffer"
    with pytest.raises(RiskRejected, match="R10"):
        RiskGate(_ctx(now=us_close)).allow(_buy())
    lunch = datetime(2026, 10, 2, 12, 30, tzinfo=HKT)
    assert session_window("HK", lunch) == "lunch"
    assert session_window("JP", datetime(2026, 10, 2, 11, 0, tzinfo=HKT)) == "lunch"
    auction = datetime(2026, 10, 2, 16, 5, tzinfo=HKT)
    assert session_window("HK", auction) == "close_auction"
    assert session_window("JP", datetime(2026, 10, 2, 14, 26, tzinfo=HKT)) == "close_auction"
    assert session_window("TW", datetime(2026, 10, 2, 13, 27, tzinfo=HKT)) == "close_auction"
    assert session_window("KR", datetime(2026, 10, 2, 14, 25, tzinfo=HKT)) == "close_auction"
    assert session_window("KR", datetime(2026, 10, 2, 7, 40, tzinfo=HKT)) == "open_auction"
    tw_mid = datetime(2026, 10, 2, 11, 0, tzinfo=HKT)
    assert session_window("TW", tw_mid) == "continuous"
    for market, moment in (
        ("HK", lunch),
        ("HK", auction),
        ("JP", datetime(2026, 10, 2, 14, 26, tzinfo=HKT)),
        ("TW", datetime(2026, 10, 2, 13, 27, tzinfo=HKT)),
    ):
        blocked = _buy(market=market, symbol="AAPL")
        with pytest.raises(RiskRejected, match="R10"):
            RiskGate(_ctx(now=moment)).allow(blocked)


def test_order_ref_state_machine_and_unknown_reconciliation(tmp_path) -> None:
    import sqlite3

    from auto_trading.execution.oms import IllegalTransition, OrderMachine, OrderRefError, order_ref
    from auto_trading.execution.sync import apply_recon

    journal = TradeJournal(tmp_path)
    machine = OrderMachine(journal)
    first = machine.create(intent_id="intent-1", symbol="AAPL", seq=1)
    assert first == order_ref("intent-1", 1)
    with pytest.raises(OrderRefError):
        machine.create(intent_id="intent-1", symbol="AAPL", seq=1)
    for state in ("DRAFT", "PENDING_APPROVAL", "APPROVED", "SUBMITTING", "SUBMITTED", "UNKNOWN"):
        machine.transition(first, state)
    with pytest.raises(IllegalTransition):
        machine.transition(first, "INTENT_RECEIVED")
    assert "AAPL" in machine.unknown_symbols()
    breaks = reconcile([], {}, unknown_orders=journal.unknown_orders())
    assert breaks[0].kind == "UNKNOWN_ORDER"
    assert breaks[0].symbol == "AAPL"
    ctx = _ctx(trading_home=tmp_path)
    apply_recon(ctx, breaks)
    assert ctx.recon_ok is True
    with pytest.raises(RiskRejected, match="UNKNOWN_ORDER"):
        RiskGate(ctx).allow(_buy(intent_id="intent-9"))
    other = _buy(symbol="MSFT", client_id="cid-msft", intent_id="intent-msft")
    ctx.quotes["MSFT"] = Quote(
        last=100.0,
        as_of=ctx.now,
        received_at=ctx.now,
        market_data_type=1,
    )
    assert RiskGate(ctx).allow(other) is None
    machine.transition(first, "FILLED")
    assert machine.unknown_symbols() == frozenset()
    ctx.unknown_symbols = frozenset()
    ctx.order_refs = frozenset({order_ref("intent-2", 1)})
    with pytest.raises(RiskRejected, match="R12"):
        RiskGate(ctx).allow(_buy(intent_id="intent-2", client_id="cid-2"))
    qty = reconcile(
        [BrokerPosition(symbol="AAPL", quantity=2, average_price=1)],
        {"AAPL": 1},
    )
    apply_recon(ctx, qty)
    with pytest.raises(RiskRejected, match="R17"):
        RiskGate(ctx).allow(_buy(client_id="cid-3", intent_id="intent-3"))
    with journal._connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM order_events")
