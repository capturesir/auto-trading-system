"""End-to-end coverage for the phase-1 gaps."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from auto_trading.backtest.adr import AdrMapping, load_adr_map
from auto_trading.backtest.allocator import Proposal, allocate
from auto_trading.backtest.deviation import TradeRef, deviation_report
from auto_trading.backtest.engine import Bar, BtSignal, run_backtest
from auto_trading.backtest.report import write_report
from auto_trading.backtest.walkforward import (
    TrialRejected,
    append_trial,
    final_oos_start,
    walk_forward_windows,
)
from auto_trading.jsonutil import loads_strict
from auto_trading.market.yahoo import load_daily_bars
from auto_trading.news.timing import TimingError, event_clock, first_session_after, judgement_time
from auto_trading.strategy.conditions import UnknownCondition
from auto_trading.strategy.validate import StrategyValidationError, validate_strategy

ROOT = Path(__file__).resolve().parents[1]
DRAFTS = ROOT / "strategies" / "drafts"


def _bars(ticker: str, rows: list[tuple[str, float]]) -> list[Bar]:
    return [
        Bar(
            day,
            price,
            price + 1,
            price - 1,
            price,
            volume=1_000_000,
            available_at=f"{day}T06:00:00+08:00",
        )
        for day, price in rows
    ]


def test_drafts_register_and_unknown_conditions_fail() -> None:
    for path in DRAFTS.glob("*.json"):
        validate_strategy(loads_strict(path.read_text(encoding="utf-8")))
    with pytest.raises(StrategyValidationError, match="not_a_real_indicator"):
        validate_strategy(
            {
                "schema_version": "strategy/1.0",
                "action": {"side": "buy"},
                "confirm": {"all_of": [{"indicator": "not_a_real_indicator", "period": 5}]},
                "exits": [{"type": "time", "max_hold": "5td"}],
            }
        )


def test_rsi_confirm_blocks_a_rising_market() -> None:
    strategy = {
        "schema_version": "strategy/1.0",
        "id": "sig_pullback",
        "version": 2,
        "mode": "paper",
        "trigger": {"type": "signal", "side": "BUY", "min_confidence": 60},
        "confirm": {"any_of": [{"indicator": "rsi_below", "period": 14, "value": 35}]},
        "action": {
            "side": "buy",
            "size": {"type": "pct_equity", "value": 5.0, "max_position_pct": 5.0},
        },
        "exits": [{"type": "time", "max_hold": "5td"}],
    }
    rising = [(f"2026-09-{day:02d}", 100 + day) for day in range(1, 29)]
    bars = {"GOOGL": _bars("GOOGL", rising)}
    signal = BtSignal("GOOGL", "BUY", 70, 200, "sig", "2026-09-01")
    result = run_backtest(strategy, bars, [signal], apply_risk=False)
    assert result.metrics["n_trades"] == 0
    strategy["confirm"] = {"all_of": [{"indicator": "made_up"}]}
    with pytest.raises(UnknownCondition):
        run_backtest(strategy, bars, [signal], apply_risk=False)


def test_adr_substitution_runs_inside_the_backtest() -> None:
    strategy = {
        "schema_version": "strategy/1.0",
        "id": "sig_trend",
        "version": 2,
        "mode": "paper",
        "trigger": {"type": "signal", "side": "BUY", "min_confidence": 60},
        "action": {
            "side": "buy",
            "size": {"type": "pct_equity", "value": 5.0, "max_position_pct": 5.0},
        },
        "exits": [{"type": "time", "max_hold": "20td"}],
    }
    days = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-02", "2026-10-05"]
    tw = _bars("2330.TW", [(day, 2445.0) for day in days])
    us = _bars("TSM", [(day, 100.0) for day in days])
    signals = [
        BtSignal("2330.TW", "BUY", 70, 1895.46, "tw", "2026-09-28"),
        BtSignal("TSM", "BUY", 70, 297.56, "adr", "2026-09-28"),
    ]
    mapping = load_adr_map(ROOT / "config" / "adr_map.csv")
    result = run_backtest(
        strategy,
        {"2330.TW": tw, "TSM": us},
        signals,
        apply_risk=False,
        adr_map=mapping,
        rates_hkd_per_unit={
            "HKD": 1.0,
            "USD": 7.8,
            "TWD": 7.8 / 31.85,
            "JPY": 0.052,
            "KRW": 0.0056,
        },
    )
    assert result.metrics["adr_substitutions"]
    assert result.metrics["adr_substitutions"][0]["adr"] == "TSM"
    bought = {order["intent_id"] for order in result.orders}
    assert any("TSM" in item for item in bought)
    news = dict(strategy)
    news["id"] = "news_event"
    blocked = run_backtest(
        news,
        {"2330.TW": tw, "TSM": us},
        signals,
        apply_risk=False,
        adr_map=mapping,
        rates_hkd_per_unit={
            "HKD": 1.0,
            "USD": 7.8,
            "TWD": 7.8 / 31.85,
            "JPY": 0.052,
            "KRW": 0.0056,
        },
    )
    assert blocked.metrics["adr_substitutions"] == []
    assert blocked.metrics["lot_too_large_count"] >= 1


def test_allocator_reruns_the_adr_proposal() -> None:
    local = Proposal(
        "sig_trend", "2330.TW", "TSMC", "ENTRY", 50_000, 2445, 0.244, "TW", lot_size=1000
    )
    adr = Proposal("sig_trend", "TSM", "TSMC", "ENTRY", 50_000, 100, 7.8, "US", lot_size=1)

    def fallback(proposal: Proposal) -> Proposal | None:
        if proposal.ticker == "2330.TW":
            return adr
        return None

    allocated = allocate([local], nlv=1_000_000, adr_fallback=fallback)
    assert allocated[0].adr_substituted
    assert allocated[0].proposal.ticker == "TSM"
    assert allocated[0].status == "ok"
    assert allocated[0].qty > 0


def test_news_clock_uses_judged_at_and_skips_the_hk_holiday() -> None:
    item = {
        "published_at": "2026-09-30T05:00:00+08:00",
        "first_seen_at": "2026-09-30T06:00:00+08:00",
        "judged_at": "2026-09-30T10:00:00+08:00",
        "generated_at": "2026-09-30T13:00:00+08:00",
        "published_at_precision": "exact",
    }
    clock = event_clock(item, "HK", max_age_sessions=1)
    assert clock.usable
    assert clock.t0 == date(2026, 10, 2)
    assert clock.t1 == date(2026, 10, 5)
    early = dict(item)
    early["published_at"] = "2026-09-30T05:00:00+08:00"
    early["first_seen_at"] = "2026-09-30T05:30:00+08:00"
    early["judged_at"] = "2026-09-30T05:55:00+08:00"
    opened = event_clock(early, "HK")
    assert opened.t0 == date(2026, 9, 30)
    assert opened.t1 == date(2026, 10, 2)
    update = dict(item)
    update["judged_at"] = "2026-10-02T12:00:00+08:00"
    update["generated_at"] = "2026-10-02T13:00:00+08:00"
    shifted = event_clock(item, "HK", update=update)
    assert shifted.available_at is not None
    assert shifted.available_at.date() == date(2026, 10, 2)
    bare = dict(item)
    del bare["judged_at"]
    with pytest.raises(TimingError, match="judged_at"):
        judgement_time(bare)
    assert first_session_after("HK", judgement_time(item)).isoformat() == "2026-10-02"


def test_yahoo_cache_and_retry(tmp_path: Path) -> None:
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1_759_276_800],
                    "indicators": {
                        "quote": [
                            {"open": [1], "high": [2], "low": [0.5], "close": [1.5], "volume": [10]}
                        ],
                        "adjclose": [{"adjclose": [1.5]}],
                    },
                }
            ]
        }
    }
    calls = {"n": 0}

    def getter(_url: str) -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("temporary")
        return json.dumps(payload)

    rows = load_daily_bars(
        "GOOGL", cache_dir=tmp_path, getter=getter, retries=3, sleep=lambda _seconds: None
    )
    assert rows[0]["close"] == 1.5
    assert calls["n"] == 3
    again = load_daily_bars("GOOGL", cache_dir=tmp_path, getter=getter, retries=1)
    assert again[0]["source"] == "yahoo"
    assert calls["n"] == 3


def test_walk_forward_oos_and_gap_attribution(tmp_path: Path) -> None:
    windows = walk_forward_windows(date(2010, 1, 1), date(2016, 1, 1))
    assert windows
    assert windows[0].test_start.year - windows[0].train_start.year == 3
    cut = final_oos_start(date(2010, 1, 1), date(2020, 1, 1))
    span = (date(2020, 1, 1) - date(2010, 1, 1)).days
    assert abs((date(2020, 1, 1) - cut).days - span * 0.2) <= 1
    path = tmp_path / "trials.csv"
    row = {
        "trial_id": "t1",
        "ts": "2026-10-01T00:00:00+08:00",
        "strategy_id": "sig_trend",
        "version": 2,
        "purpose": "final_oos",
        "period_test": "2018/2020",
    }
    append_trial(path, row)
    with pytest.raises(TrialRejected):
        append_trial(path, row)
    expected = [TradeRef("GOOGL", "BUY", date(2026, 10, 2), 100, qty=10, pnl=50)]
    actual = [TradeRef("GOOGL", "BUY", date(2026, 10, 2), 101, qty=10, pnl=40)]
    report = deviation_report(
        nlv=1_000_000,
        expected_pnl=50,
        actual_pnl=40,
        expected_equity=[1_000_000, 1_000_050],
        actual_equity=[1_000_000, 1_000_040],
        expected_trades=expected,
        actual_trades=actual,
        expected_cost=1,
        actual_cost=2,
        fx_gap=0.5,
    )
    assert report["trade_match_rate"] == 1
    assert set(report["gap_attribution"]) == {"missed", "extra", "fill", "cost", "fx", "residual"}
    strategy = {"id": "sig_trend", "version": 2}
    run = run_backtest(
        {
            "id": "sig_trend",
            "version": 2,
            "trigger": {"type": "signal", "min_confidence": 60},
            "action": {
                "side": "buy",
                "size": {"type": "pct_equity", "value": 5, "max_position_pct": 5},
            },
            "exits": [{"type": "time", "max_hold": "2td"}],
        },
        {"GOOGL": _bars("GOOGL", [("2026-10-01", 100), ("2026-10-02", 101)])},
        [BtSignal("GOOGL", "BUY", 70, 120, "s", "2026-10-01")],
        apply_risk=False,
    )
    dest = write_report(tmp_path / "run", run, strategy)
    saved = json.loads((dest / "metrics.json").read_text(encoding="utf-8"))
    assert saved["strategy_id"] == "sig_trend"


def test_mapping_file_round_trip() -> None:
    rows = load_adr_map(ROOT / "config" / "adr_map.csv")
    assert rows[0] == AdrMapping(
        "2330.TW",
        "TSM",
        5,
        "USD",
        "TSMC",
        date(2026, 10, 6),
        rows[0].source,
    )
