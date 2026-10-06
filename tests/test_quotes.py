"""IBKR quotes fall back to Yahoo, and delayed paper prices stay usable."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from auto_trading.broker import OrderSide
from auto_trading.execution.ibkr import IbkrBroker
from auto_trading.execution.pricing import apply_quote, session_open_limit
from auto_trading.market.quotes import (
    MarketDataError,
    QuoteRouter,
    quote_from_ibkr,
    yahoo_quote,
)
from auto_trading.market.yahoo import parse_public_quote
from auto_trading.risk import Held, Quote, RiskGate, RiskRejected
from tests.test_execution_paper import MockClient, _buy, _ctx

HKT = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=HKT)


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
        )
    assert plan.ready
    assert plan.quote_source == "ibkr"
    assert plan.data_type == "delayed"
    assert plan.quote_age_sec == 0
    assert "delayed data" in caplog.text

    delayed = Quote(last=100, as_of=session, source="ibkr", data_type="delayed")
    assert RiskGate(_ctx(now=session, quotes={"AAPL": delayed})).allow(_buy()) is None
    stale = _ctx(
        now=session,
        quotes={"AAPL": Quote(last=100, as_of=session - timedelta(seconds=11), data_type="public")},
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


def test_order_records_the_quote_source(tmp_path) -> None:
    client = MockClient(["DU1234567"])
    broker = IbkrBroker(client, trading_home=tmp_path)

    opened = datetime(2026, 10, 2, 21, 30, tzinfo=HKT)
    plan = session_open_limit(
        market="US",
        side="BUY",
        now=opened + timedelta(minutes=6),
        session_open_at=opened,
        open_price=None,
        bid=100,
        ask=100.2,
        last=100.2,
        decision_ref=100,
        quote_source="yahoo",
        data_type="public",
        quote_as_of=opened + timedelta(minutes=2),
    )
    intent = _buy(account="DU1234567")
    apply_quote(intent, plan)

    async def send() -> None:
        await broker.place(intent)

    asyncio.run(send())
    assert client.placed[0]["quote_source"] == "yahoo"
    assert client.placed[0]["quote_data_type"] == "public"
    assert client.placed[0]["quote_age_sec"] == pytest.approx(240)
