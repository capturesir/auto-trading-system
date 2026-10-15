"""Allocator, R8/R9, ADR fallback, and v2 position weights."""

from __future__ import annotations

from datetime import date

import pytest

from auto_trading.backtest.adr import AdrMapping, consider_adr
from auto_trading.backtest.allocator import Proposal, allocate
from auto_trading.backtest.risk_sim import RiskSimulator, fill_rejections
from auto_trading.strategy.sizing import target_fraction

NLV = 1_000_000.0


def _entry(strategy: str, ticker: str, group: str, budget: float = 50_000) -> Proposal:
    return Proposal(strategy, ticker, group, "ENTRY", budget, 24.0, 1.0, "HK", lot_size=1000)


def test_one_opening_order_per_ticker_and_priority() -> None:
    results = allocate(
        [_entry("sig_pullback", "0883.HK", "CNOOC"), _entry("sig_trend", "0883.HK", "CNOOC")],
        nlv=NLV,
    )
    by_strategy = {item.proposal.strategy_id: item for item in results}
    assert by_strategy["sig_trend"].status == "ok"
    assert by_strategy["sig_trend"].qty == 2000
    assert by_strategy["sig_pullback"].status == "skipped_same_ticker"


def test_exit_frees_issuer_room_before_entries() -> None:
    held = {"TSMC": 0.095 * NLV}
    exit_order = Proposal(
        "sig_trend", "2330.TW", "TSMC", "EXIT", 0.095 * NLV, 2445, 0.244, "TW", qty=1000
    )
    entry = Proposal("sig_pullback", "TSM", "TSMC", "ENTRY", 40_000, 100, 7.8, "US", lot_size=1)
    blocked = allocate([entry], nlv=NLV, issuer_notional=held)
    assert blocked[0].status == "LOT_TOO_LARGE"
    assert blocked[0].binding_limit == "ticker_cap"
    freed = allocate([entry, exit_order], nlv=NLV, issuer_notional=dict(held))
    statuses = {item.proposal.kind: item.status for item in freed}
    assert statuses["EXIT"] == "ok"
    assert statuses["ENTRY"] == "ok"


def test_gap_rejects_the_whole_order() -> None:
    assert fill_rejections(60_000, NLV) == ["R1"]
    assert fill_rejections(250_000, NLV) == ["R1", "R2"]


def test_r8_blocks_buys_and_leaves_sells() -> None:
    sim = RiskSimulator(hwm=100)
    sim.mark(100, new_risk_day=True, trading_day=True, session_id="d0")
    sim.mark(97, new_risk_day=False, trading_day=True, session_id="d0")
    assert sim.r8_trigger_days == 1
    assert sim.allow("BUY", "entry") == "R8"
    assert sim.allow("SELL", "exit") is None


def _halted(scenario: str = "cooldown_half_size") -> RiskSimulator:
    sim = RiskSimulator(hwm=100, scenario=scenario)
    sim.mark(100, new_risk_day=True, trading_day=True, session_id="d0")
    sim.mark(94, new_risk_day=False, trading_day=True, session_id="d0")
    sim.mark(89, new_risk_day=False, trading_day=True, session_id="d0")
    return sim


def test_r9_warn_then_halt_keeps_protective_stops() -> None:
    sim = _halted()
    assert sim.r9_warn_count == 1
    assert sim.r9_halt_count == 1
    assert sim.halted_sessions == 0
    assert sim.allow("SELL", "exit") is None
    assert sim.allow("BUY", "entry") == "R9_HALTED_COOLDOWN"
    assert sim.allow("SELL", "protective") is None
    sim.note_protective_exit()
    assert sim.protective_stop_exits == 1


def test_manual_resume_needs_five_days_and_a_recovery() -> None:
    sim = _halted("manual_no_reset")
    for index in range(1, 5):
        sim.mark(95, new_risk_day=True, trading_day=True, session_id=f"d{index}")
        assert sim.halted
    sim.mark(95, new_risk_day=True, trading_day=True, session_id="d5")
    assert sim.halted is False
    assert sim.hwm == 100
    stuck = _halted("manual_no_reset")
    for index in range(1, 6):
        stuck.mark(89, new_risk_day=True, trading_day=True, session_id=f"d{index}")
    assert stuck.halted
    assert stuck.hwm == 100


def test_reset_and_never_scenarios() -> None:
    reset = RiskSimulator(hwm=100, scenario="reset_after_5td")
    reset.mark(100, new_risk_day=True, trading_day=True, session_id="d0")
    reset.mark(89, new_risk_day=False, trading_day=True, session_id="d0")
    for index in range(1, 6):
        reset.mark(89, new_risk_day=True, trading_day=True, session_id=f"d{index}")
    assert reset.halted is False
    assert reset.hwm == 89
    never = RiskSimulator(hwm=100, scenario="never")
    never.mark(100, new_risk_day=True, trading_day=True, session_id="d0")
    never.mark(89, new_risk_day=False, trading_day=True, session_id="d0")
    for index in range(1, 8):
        never.mark(110, new_risk_day=True, trading_day=True, session_id=f"d{index}")
    assert never.halted
    assert never.hwm == 100


def test_v2_weights() -> None:
    trend = target_fraction(
        {"type": "risk_pct", "value": 0.5, "max_position_pct": 5.0},
        stop_distance_pct=8,
    )
    assert trend == 0.05
    pullback = target_fraction(
        {
            "type": "pct_equity",
            "value": 5.0,
            "confidence_scale": {"ref": 75, "cap": 1.0},
            "max_position_pct": 5.0,
        },
        confidence=60,
    )
    assert pullback == pytest.approx(0.04)
    news = target_fraction({"type": "pct_equity", "value": 3.0, "max_position_pct": 5.0})
    assert news == 0.03


def test_adr_cases() -> None:
    mapping = [AdrMapping("2330.TW", "TSM", 5, "USD", "TSMC", date(2026, 10, 1), "watchlist")]
    common = {
        "strategy_id": "sig_trend",
        "local_ticker": "2330.TW",
        "odd_lot_enabled": False,
        "whole_lot_exceeds_cap": True,
        "mappings": mapping,
        "as_of": date(2026, 10, 2),
        "fv_local": 1895.46,
        "twd_per_usd": 31.85,
    }
    sell = consider_adr(**common, adr_signal_side="SELL", adr_signal_id="tsm", fv_adr=297.56)
    assert sell.substitute is False and sell.adr_checked and sell.reason == "adr_not_buy"
    buy = consider_adr(**common, adr_signal_side="BUY", adr_signal_id="tsm-buy", fv_adr=297.56)
    assert buy.substitute and buy.ticker == "TSM" and buy.signal_id == "tsm-buy"
    gap = consider_adr(**common, adr_signal_side="BUY", adr_signal_id="tsm-buy", fv_adr=320)
    assert gap.reason == "adr_fv_inconsistent" and gap.alert == "adr_fv_inconsistent"
    news = consider_adr(
        **{**common, "strategy_id": "news_event"},
        adr_signal_side="BUY",
        adr_signal_id="tsm-buy",
        fv_adr=297.56,
    )
    assert news.reason == "news_event"
    expired = consider_adr(
        **{**common, "as_of": date(2027, 3, 1)},
        adr_signal_side="BUY",
        adr_signal_id="tsm-buy",
        fv_adr=297.56,
    )
    assert expired.reason == "adr_expired"
