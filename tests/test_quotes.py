"""IBKR quotes fall back to Yahoo, and delayed paper prices stay usable."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from auto_trading.broker import OrderAck, OrderIntent, OrderSide
from auto_trading.execution.ibkr import IbkrBroker
from auto_trading.execution.pricing import apply_quote, session_open_limit
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.market.quotes import (
    MarketDataError,
    QuoteRouter,
    quote_from_ibkr,
    yahoo_quote,
)
from auto_trading.market.yahoo import parse_public_quote
from auto_trading.risk import GateContext, Held, Quote, RiskGate, RiskRejected

HKT = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=HKT)
# 22:00 Macau is inside the US cash session, matching the paper-execution fixtures.
SESSION = datetime(2026, 10, 2, 22, 0, tzinfo=HKT)


class MockClient:
    def __init__(self, accounts: list[str]) -> None:
        self.accounts = accounts
        self.placed: list[dict] = []

    async def managed_accounts(self) -> list[str]:
        return list(self.accounts)

    async def place_order(self, order: dict) -> OrderAck:
        self.placed.append(order)
        return OrderAck(client_id=order["client_id"], broker_order_id="paper-1")


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
    if home is None:
        home = Path(tempfile.mkdtemp())
    root = Path(home)
    if not (root / "state" / "risk_limits.json").is_file():
        write_risk_limits(root, build_risk_limits(now=SESSION, nlv=1_000_000, account="DU1234567"))
    payload: dict = {
        "now": SESSION,
        "trading_home": root,
        "quotes": {
            "AAPL": Quote(last=100.0, as_of=SESSION, received_at=SESSION, market_data_type=1)
        },
        "fx_to_base": {"TWD": 0.244, "HKD": 1.0, "USD": 1.0, "JPY": 0.05},
    }
    payload.update(kwargs)
    if "expected_account" not in kwargs and not os.environ.get("IBKR_PAPER_ACCOUNT"):
        payload["expected_account"] = "DU1234567"
    return GateContext(**payload)


def test_public_chart_quote_uses_the_yahoo_timestamp() -> None:
    payload = {
        "chart": {
            "result": [{"meta": {"regularMarketPrice": 70.5, "regularMarketTime": 1_759_276_800}}]
        }
    }
    price, as_of = parse_public_quote(payload, ticker="0700.HK")
    assert price == 70.5
    assert as_of.tzinfo is not None
    quote = yahoo_quote("0700.HK", getter=lambda _url: json.dumps(payload))
    assert quote.source == "yahoo"
    assert quote.data_type == "public"
    assert quote.last == 70.5


def test_router_prefers_ibkr_and_records_realtime() -> None:
    def ibkr(_symbol: str) -> Quote:
        return quote_from_ibkr(last=100, market_data_type=1, as_of=NOW, bid=99.9, ask=100.1)

    def public(_symbol: str) -> Quote:
        raise AssertionError("public source must stay idle")

    quote = QuoteRouter(ibkr, public, clock=lambda: NOW).fetch("0700.HK")
    assert quote.source == "ibkr"
    assert quote.data_type == "realtime"
    assert quote.delayed is False
    assert quote.age_seconds(NOW) == 0


def test_router_falls_back_on_competing_session_subscription_and_timeout(tmp_path) -> None:
    public_quote = Quote(last=88, as_of=NOW, source="yahoo", data_type="public")
    calls = {"ibkr": 0}

    def ibkr(_symbol: str) -> Quote:
        calls["ibkr"] += 1
        if calls["ibkr"] == 1:
            raise MarketDataError("competing session", code=10197)
        if calls["ibkr"] == 2:
            raise MarketDataError("not subscribed", code=354)
        if calls["ibkr"] == 3:
            raise MarketDataError("delayed not enabled", code=10167)
        if calls["ibkr"] == 4:
            raise TimeoutError("gateway timed out")
        return quote_from_ibkr(last=90, market_data_type=3, as_of=NOW)

    router = QuoteRouter(
        ibkr, lambda _symbol: public_quote, trading_home=tmp_path, clock=lambda: NOW
    )
    first = router.fetch("AAPL")
    assert first.source == "yahoo" and first.data_type == "public"
    assert router.competing is True
    again = router.fetch("7203.T")
    assert again.data_type == "public"
    assert router.fetch("2330.TW").data_type == "public"
    assert router.fetch("0700.HK").data_type == "public"
    restored = router.fetch("AAPL")
    assert restored.source == "ibkr"
    assert restored.data_type == "delayed"
    assert router.competing is False

    pending = sorted((tmp_path / "alerts" / "pending").glob("*.json"))
    assert len(pending) == 2
    text = "\n".join(path.read_text(encoding="utf-8") for path in pending)
    assert "10197" in text
    assert "行情恢復" in text

    def rejected(_symbol: str) -> Quote:
        raise MarketDataError("unknown", code=200)

    with pytest.raises(MarketDataError, match="unknown"):
        QuoteRouter(rejected, lambda _symbol: public_quote).fetch("AAPL")


def test_delayed_open_is_logged_and_a_stale_entry_is_refused(caplog) -> None:
    # 22:00 Macau is inside the US cash session, so the gate reaches the quote rule.
    session = datetime(2026, 10, 2, 22, 0, tzinfo=HKT)
    opened = datetime(2026, 10, 2, 21, 30, tzinfo=HKT)
    with caplog.at_level("INFO"):
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
            quote_source="ibkr",
            data_type="delayed",
            quote_as_of=opened + timedelta(minutes=5),
            market_data_type=3,
        )
    assert plan.ready
    assert plan.quote_source == "ibkr"
    assert plan.data_type == "delayed"
    assert plan.quote_age_sec == 0
    assert "delayed data" in caplog.text

    delayed = Quote(
        last=100,
        as_of=session,
        source="ibkr",
        data_type="delayed",
        received_at=session,
        market_data_type=3,
    )
    assert RiskGate(_ctx(now=session, quotes={"AAPL": delayed})).allow(_buy()) is None
    stale = _ctx(
        now=session,
        quotes={
            "AAPL": Quote(
                last=100,
                as_of=session,
                received_at=session,
                source="yahoo",
                data_type="public",
            )
        },
        positions=[Held("AAPL", 5, 500)],
    )
    with pytest.raises(RiskRejected, match="R22"):
        RiskGate(stale).allow(_buy())
    stop = _buy(
        side=OrderSide.SELL,
        intent_type="EXIT",
        parent_intent_id="entry-1",
        quantity=1,
        protective=True,
    )
    assert RiskGate(stale).allow(stop) is None


def test_delayed_freshness_uses_receipt_time_and_records_the_delay(tmp_path) -> None:
    session = datetime(2026, 10, 2, 22, 0, tzinfo=HKT)
    printed = session - timedelta(minutes=20)

    def ibkr(_symbol: str) -> Quote:
        return quote_from_ibkr(last=100, market_data_type=3, as_of=printed)

    def unused(_symbol: str) -> Quote:
        raise AssertionError("public source must stay idle")

    quote = QuoteRouter(ibkr, unused, clock=lambda: session).fetch("AAPL")
    assert quote.data_type == "delayed"
    assert quote.received_at == session
    assert quote.age_seconds(session) == 0
    assert quote.delay_sec == pytest.approx(20 * 60)
    assert RiskGate(_ctx(now=session, quotes={"AAPL": quote})).allow(_buy()) is None

    held = Quote(
        last=100,
        as_of=printed,
        source="ibkr",
        data_type="delayed",
        received_at=session - timedelta(seconds=11),
        market_data_type=3,
    )
    with pytest.raises(RiskRejected, match="R22"):
        RiskGate(_ctx(now=session, quotes={"AAPL": held})).allow(_buy())

    late_print = Quote(
        last=100,
        as_of=session - timedelta(seconds=1801),
        received_at=session,
        data_type="realtime",
        market_data_type=1,
    )
    with pytest.raises(RiskRejected, match="QUOTE_DELAY_EXCEEDED"):
        RiskGate(_ctx(now=session, quotes={"AAPL": late_print})).allow(_buy())

    opened = datetime(2026, 10, 2, 21, 30, tzinfo=HKT)
    plan = session_open_limit(
        market="US",
        side="BUY",
        now=session,
        session_open_at=opened,
        open_price=100,
        bid=None,
        ask=None,
        last=None,
        decision_ref=100,
        quote_source="ibkr",
        data_type="delayed",
        quote_as_of=printed,
        quote_received_at=session,
        market_data_type=3,
    )
    assert plan.ready
    assert plan.quote_age_sec == 0
    assert plan.quote_delay_sec == pytest.approx(20 * 60)

    client = MockClient(["DU1234567"])
    intent = _buy(account="DU1234567")
    apply_quote(intent, plan)

    async def send() -> None:
        await IbkrBroker(client, trading_home=tmp_path, expected_account="DU1234567").place(intent)

    asyncio.run(send())
    assert client.placed[0]["quote_data_type"] == "delayed"
    assert client.placed[0]["quote_age_sec"] == 0
    assert client.placed[0]["quote_delay_sec"] == pytest.approx(20 * 60)


def test_order_records_the_quote_source(tmp_path) -> None:
    opened = datetime(2026, 10, 2, 21, 30, tzinfo=HKT)
    public = session_open_limit(
        market="US",
        side="BUY",
        now=opened + timedelta(minutes=6),
        session_open_at=opened,
        open_price=100,
        bid=None,
        ask=None,
        last=None,
        decision_ref=100,
        quote_source="yahoo",
        data_type="public",
        quote_as_of=opened + timedelta(minutes=2),
        quote_received_at=opened + timedelta(minutes=6),
    )
    assert public.ready is False and public.reason == "R22"

    client = MockClient(["DU1234567"])
    broker = IbkrBroker(client, trading_home=tmp_path, expected_account="DU1234567")
    plan = session_open_limit(
        market="US",
        side="BUY",
        now=opened + timedelta(minutes=6),
        session_open_at=opened,
        open_price=100,
        bid=None,
        ask=None,
        last=None,
        decision_ref=100,
        quote_source="ibkr",
        data_type="realtime",
        quote_as_of=opened + timedelta(minutes=2),
        quote_received_at=opened + timedelta(minutes=6),
        market_data_type=1,
    )
    intent = _buy(account="DU1234567")
    apply_quote(intent, plan)

    async def send() -> None:
        await broker.place(intent)

    asyncio.run(send())
    assert client.placed[0]["quote_source"] == "ibkr"
    assert client.placed[0]["quote_data_type"] == "realtime"
    assert client.placed[0]["market_data_type"] == 1
    assert client.placed[0]["quote_age_sec"] == 0
