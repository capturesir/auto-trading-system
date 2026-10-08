"""Indicators, registry, intents, alerts, news, and a short backtest."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from auto_trading.alerts.pending import build_alert, summarize_b_alerts, write_pending
from auto_trading.backtest.engine import Bar, BtSignal, LookaheadError, run_backtest
from auto_trading.backtest.fx import hkd_per_unit, to_base
from auto_trading.gates import g4_passes
from auto_trading.intents.writer import filter_for_risk, position_followups, write_intents
from auto_trading.market.calendar import is_session, us_open_in_macau
from auto_trading.market.indicators import bollinger, rsi_wilder, sma
from auto_trading.market.qc import qc_flags
from auto_trading.news.reader import classify_item, read_news, visible
from auto_trading.strategy.direction import direction_action
from auto_trading.strategy.registry import RegistryError, append_event, is_runnable
from auto_trading.strategy.signal_events import classify_signal_change
from auto_trading.strategy.spec import spec_hash
from auto_trading.strategy.validate import StrategyValidationError, validate_strategy
from auto_trading.strategy.vocabulary import (
    Series,
    close_above_ma,
    event_gap_up,
    event_volume_breakout,
    exit_priority,
    fair_value_reached,
    max_chase,
    no_new_low,
    not_limit_up,
    rsi_above,
    rsi_below,
    stop_loss_pct,
    take_profit_pct,
)

HKT = timezone(timedelta(hours=8))


def test_sma_rsi_and_bollinger_population_std() -> None:
    assert sma([1, 2, 3, 4, 5], 3)[2:] == [2, 3, 4]
    rising = list(range(1, 40))
    assert rsi_wilder(rising, 14)[-1] == 100
    flat = [10.0] * 30
    assert rsi_below(Series(flat), 14, 50) is False
    assert rsi_above(Series(rising), 14, 70) is True
    middle, lower, upper = bollinger([1, 2, 3, 4, 5], 3, 2)
    assert middle[2] == 2
    assert lower[2] < middle[2] < upper[2]


def test_vocabulary_pairs() -> None:
    climbs = list(range(1, 80))
    assert close_above_ma(Series(climbs), 5)
    assert close_above_ma(Series([5, 4, 3, 2, 1]), 3) is False
    assert no_new_low([5, 4, 3, 3, 3], 3, 2)
    assert no_new_low([5, 4, 3, 2, 1], 3, 2) is False
    assert stop_loss_pct(88, 100, 12)
    assert stop_loss_pct(95, 100, 12) is False
    assert take_profit_pct(110, 100, 8)
    assert fair_value_reached(100, 100, 0.98)
    assert fair_value_reached(90, 100, 0.98) is False
    assert event_gap_up(102, 101, 100, 2, 1)
    assert event_gap_up(101, 100, 100, 2, 1) is False
    assert event_volume_breakout([1, 1, 1, 3], [1, 1, 1, 2], 3, 3, 2, 3)
    assert max_chase(107, 100, 8)
    assert max_chase(110, 100, 8) is False
    assert not_limit_up("US", True)
    assert not_limit_up("TW", True) is False
    assert exit_priority(["time", "signal_reversal", "stop_loss_pct"]) == "stop_loss_pct"


def test_registry_rejects_news_event_v2_and_llm_approval(tmp_path: Path) -> None:
    path = tmp_path / "registry.jsonl"
    with pytest.raises(RegistryError, match="v2"):
        append_event(
            path,
            {
                "strategy_id": "news_event",
                "version": 2,
                "event": "approved",
                "actor": "user",
                "spec_hash": "sha256:" + "ab" * 32,
                "mode_after": "paper",
            },
        )
    with pytest.raises(RegistryError, match="actor=user"):
        append_event(
            path,
            {
                "strategy_id": "sig_trend",
                "version": 2,
                "event": "approved",
                "actor": "llm",
                "spec_hash": "sha256:" + "cd" * 32,
                "mode_after": "paper",
            },
        )
    document = {"id": "news_event", "version": 2, "mode": "paper"}
    assert is_runnable(path, document) is False


def test_runnable_requires_matching_hash(tmp_path: Path) -> None:
    path = tmp_path / "registry.jsonl"
    document = {"id": "sig_trend", "version": 2, "mode": "paper", "rules": {"a": 1}}
    append_event(
        path,
        {
            "strategy_id": "sig_trend",
            "version": 2,
            "event": "approved",
            "actor": "user",
            "spec_hash": spec_hash(document),
            "mode_after": "paper",
        },
    )
    assert is_runnable(path, document)
    changed = dict(document)
    changed["rules"] = {"a": 2}
    assert is_runnable(path, changed) is False


def test_min_confidence_and_unknown_exit() -> None:
    with pytest.raises(StrategyValidationError):
        validate_strategy(
            {
                "schema_version": "strategy/1.0",
                "trigger": {"min_confidence": 55},
                "exits": [{"type": "stop_loss_pct", "value": 8}],
                "action": {"side": "buy"},
            }
        )
    with pytest.raises(StrategyValidationError, match="reversal"):
        validate_strategy(
            {
                "schema_version": "strategy/1.0",
                "trigger": {"min_confidence": 60},
                "exits": [{"type": "reversal"}],
                "action": {"side": "buy"},
            }
        )


def test_direction_of_the_sample_book() -> None:
    book = [
        ("2330.TW", "SELL"),
        ("0883.HK", "BUY"),
        ("GOOGL", "SELL"),
        ("TSM", "SELL"),
        ("MU", "SELL"),
    ]
    actions = [direction_action(side, has_position=False) for _ticker, side in book]
    assert actions.count("entry") == 1
    assert actions.count("signal_sell_no_position") == 4
    assert direction_action("SELL", has_position=True) == "exit"
    assert direction_action("HOLD", has_position=False) == "none"


def test_parent_links_and_risk_filter(tmp_path: Path) -> None:
    followups = position_followups("entry-1", 5, 5)
    assert followups[0]["parent_intent_id"] == followups[1]["parent_intent_id"] == "entry-1"
    document = {
        "strategy_id": "sig_pullback",
        "strategy_version": 2,
        "spec_hash": "sha256:" + "11" * 32,
        "mode": "paper",
        "live_size_multiplier": None,
        "base_currency": "HKD",
        "generated_at": "2026-10-02T07:05:00+08:00",
        "intents": [
            {"intent_type": "ENTRY", "parent_intent_id": None, "ticker": "AAA"},
            *followups,
        ],
    }
    path = write_intents(tmp_path, document, session_date="2026-10-02", entries_allowed=False)
    saved = path.read_text(encoding="utf-8")
    assert "ENTRY" not in saved
    assert "REDUCE" in saved and "EXIT" in saved
    assert '"live_size_multiplier": null' in saved
    assert filter_for_risk(document["intents"], entries_allowed=True)[0]["intent_type"] == "ENTRY"


def test_alerts_stay_in_pending(tmp_path: Path) -> None:
    created = datetime(2026, 10, 2, 7, 10, tzinfo=HKT)
    alert = build_alert(
        rule_id="lot_too_large",
        alert_type="lot_too_large",
        severity="info",
        ticker="2330.TW",
        message_zh="整張超過上限",
        trading_date="2026-10-02",
        created_at=created,
    )
    first = write_pending(tmp_path, alert, filename_stamp="20261002T071000")
    again = build_alert(
        rule_id="lot_too_large",
        alert_type="lot_too_large",
        severity="info",
        ticker="2330.TW",
        message_zh="整張超過上限",
        trading_date="2026-10-02",
        created_at=created,
    )
    assert again["alert_id"] == alert["alert_id"]
    assert first.parent == tmp_path / "alerts" / "pending"
    assert not (tmp_path / "alerts" / "sent").exists()
    assert not (tmp_path / "alerts" / "failed").exists()
    counts = summarize_b_alerts(
        [
            {"status": "forwarded"},
            {"status": "suppressed_duplicate"},
            {"status": "failed"},
        ]
    )
    assert counts == {"forwarded": 1, "suppressed_duplicate": 1, "failed": 1}


def test_news_schema_and_v3_judgements(tmp_path: Path) -> None:
    bad = tmp_path / "news.json"
    bad.write_text('{"schema_version": "news/0.9", "items": []}', encoding="utf-8")
    read = read_news(bad)
    assert read.enabled is False and read.data_error == "schema"
    macro = {
        "news_id": "m",
        "status": "new",
        "event_type": "macro",
        "tickers": [],
        "market_impacts": ["x"],
    }
    assert classify_item(macro, None)[0].outcome == "shadow"
    mu = {
        "news_id": "mu",
        "status": "new",
        "event_type": "earnings",
        "tickers": [{"ticker": "MU", "relation": "subject", "direction": "positive", "impact": 3}],
    }
    assert classify_item(mu, "SELL")[0].reason == "signal_not_buy"
    oil = {
        "news_id": "oil",
        "status": "new",
        "event_type": "commodity",
        "tickers": [
            {"ticker": "0883.HK", "relation": "affected", "direction": "positive", "impact": 2}
        ],
    }
    judged = classify_item(oil, "BUY", prices_confirm=False)
    assert judged[0].outcome == "unconfirmed"
    assert visible("2026-10-02T07:05:00+08:00", "2026-10-02T07:04:00+08:00") is False


def test_signal_change_rules() -> None:
    assert (
        classify_signal_change(
            baseline=False,
            had_previous=False,
            previous_side=None,
            side="BUY",
            fv_change_pct=10,
            reason_types={"initial"},
        )
        is None
    )
    assert (
        classify_signal_change(
            baseline=True,
            had_previous=True,
            previous_side="HOLD",
            side="BUY",
            fv_change_pct=6,
            reason_types={"initial"},
        )
        is None
    )
    assert (
        classify_signal_change(
            baseline=True,
            had_previous=True,
            previous_side="BUY",
            side="BUY",
            fv_change_pct=8,
            reason_types={"method_change"},
        )
        is None
    )
    assert (
        classify_signal_change(
            baseline=True,
            had_previous=True,
            previous_side="BUY",
            side="BUY",
            fv_change_pct=6,
            reason_types={"earnings"},
        )
        == "fv_revision_up"
    )
    assert (
        classify_signal_change(
            baseline=True,
            had_previous=True,
            previous_side="BUY",
            side="BUY",
            fv_change_pct=6,
            reason_types=set(),
            bulk_method_change=True,
        )
        is None
    )


def test_fx_base_switch_and_calendar() -> None:
    hkd_per_twd = hkd_per_unit(31.85, 7.8)
    rates = {"HKD": 1.0, "USD": 7.8, "TWD": hkd_per_twd}
    amount = 100
    assert to_base(amount, "USD", rates, "HKD") == pytest.approx(780)
    assert to_base(amount, "USD", rates, "USD") == pytest.approx(100)
    assert is_session("XHKG", date(2026, 10, 1)) is False
    summer = us_open_in_macau(date(2026, 10, 30))
    winter = us_open_in_macau(date(2026, 11, 2))
    assert (summer.hour, summer.minute) == (21, 30)
    assert (winter.hour, winter.minute) == (22, 30)


def test_qc_flags_injected_errors() -> None:
    flags = qc_flags(
        [{"date": "2026-10-02", "open": 1, "high": 1, "low": 2, "close": 1, "volume": 0}],
        ["2026-10-01", "2026-10-02"],
    )
    assert any(flag.startswith("missing_session") for flag in flags)
    assert any(flag.startswith("ohlc_inconsistent") for flag in flags)
    assert any(flag.startswith("zero_volume") for flag in flags)


def test_backtest_is_deterministic_and_blocks_lookahead() -> None:
    strategy = {
        "schema_version": "strategy/1.0",
        "id": "sig_pullback",
        "version": 2,
        "mode": "paper",
        "trigger": {"type": "signal", "side": "BUY", "min_confidence": 60},
        "action": {
            "side": "buy",
            "size": {"type": "pct_equity", "value": 5.0, "max_position_pct": 5.0},
        },
        "exits": [{"type": "stop_loss_pct", "value": 8.0}, {"type": "time", "max_hold": "2td"}],
    }
    bars = {
        "GOOGL": [
            Bar("2026-10-01", 100, 101, 99, 100, available_at="2026-10-01T06:00:00+08:00"),
            Bar("2026-10-02", 100, 110, 99, 108, available_at="2026-10-02T06:00:00+08:00"),
            Bar("2026-10-05", 108, 112, 107, 111, available_at="2026-10-05T06:00:00+08:00"),
            Bar("2026-10-06", 111, 112, 90, 92, available_at="2026-10-06T06:00:00+08:00"),
        ]
    }
    signals = [BtSignal("GOOGL", "BUY", 70, 130, "sig", "2026-10-01")]
    first = run_backtest(strategy, bars, signals, compare_r9=True, apply_risk=True)
    second = run_backtest(strategy, bars, signals, compare_r9=True)
    assert first.metrics["cagr"] == second.metrics["cagr"]
    assert first.metrics["n_trades"] == second.metrics["n_trades"]
    assert set(first.metrics["r9_resume_scenario"]) == {
        "cooldown_half_size",
        "manual_no_reset",
        "reset_after_5td",
        "never",
    }
    assert "sharpe" in first.metrics and "risk_triggers" in first.metrics
    poisoned = {
        "GOOGL": [
            Bar("2026-10-01", 100, 101, 99, 100, available_at="2026-10-03T07:06:00+08:00"),
            *bars["GOOGL"][1:],
        ]
    }
    with pytest.raises(LookaheadError):
        run_backtest(strategy, poisoned, signals)


def test_g4_counts_a_cleared_halt() -> None:
    assert g4_passes([])
    assert g4_passes([{"kind": "R9_HALT", "resolved": True}]) is False
    for kind in ("UNKNOWN_ORDER", "RECON_MISMATCH", "HASH_MISMATCH", "KILL_SWITCH"):
        assert g4_passes([{"kind": kind}]) is False
