"""Paper-only guards: intents, registry, news v4, evaluation, and the CI scan."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from auto_trading.backtest.engine import _event_reversed
from auto_trading.config import Settings
from auto_trading.gates import evaluate_paper, g4_passes, write_paper_evaluation
from auto_trading.intents.writer import PaperOnlyViolation, write_intents
from auto_trading.news.reader import classify_item, dedupe_chain, root_news_id
from auto_trading.paper_only import line_allowed
from auto_trading.strategy import StrategyEngine, spec_hash
from auto_trading.strategy.registry import RegistryError, append_event, is_runnable, r23_record
from auto_trading.strategy.spec import ApprovedStrategy

HKT = timezone(timedelta(hours=8))
DOCUMENT = {"id": "trend-follow", "version": 1, "mode": "paper", "rules": {"lookback": 20}}


def test_scan_allows_the_price_metric_and_flags_a_switch() -> None:
    assert line_allowed("min_upside_live_pct = 0")
    quote_line = "compute remains a " + "liv" + "e quote placeholder"
    assert line_allowed(quote_line) is False
    bad = "mode = " + '"' + "liv" + "e" + '"'
    assert line_allowed(bad) is False
    assert line_allowed(bad + "  # refused: paper_only") is False
    assert line_allowed('os.environ["IBKR_PAPER_ACCOUNT"]')
    bare = "IBKR_" + "PAPER"
    assert line_allowed(bare + " = 1") is False


def test_scan_rejects_mode_switch_and_forbidden_ports(tmp_path: Path) -> None:
    from auto_trading.paper_only import scan_tree

    mode_line = "liv" + "e_" + "mode = True"
    port_line = "port = 40" + "01"
    assert line_allowed(mode_line) is False
    assert line_allowed(port_line + "  # refused: paper_only") is False
    for suffix in (".ini", ".sh", ".service", ".template"):
        (tmp_path / f"bad{suffix}").write_text(mode_line + "\n", encoding="utf-8")
    assert scan_tree(tmp_path)


def test_real_account_ports_are_refused() -> None:
    for port in (int("40" + "01"), int("749" + "6")):  # refused: paper_only
        with pytest.raises(ValidationError):
            Settings(ibkr_port=port)
    assert Settings(ibkr_port=7497).ibkr_port == 7497


def test_intent_writer_rejects_a_multiplier(tmp_path) -> None:
    document = {
        "strategy_id": "sig_pullback",
        "strategy_version": 2,
        "mode": "paper",
        "live_size_multiplier": 0.10,  # rejected: paper_only_violation
        "intents": [{"intent_type": "ENTRY", "parent_intent_id": None}],
    }
    with pytest.raises(PaperOnlyViolation):
        write_intents(tmp_path, document, session_date="2026-10-02", entries_allowed=True)
    assert list(tmp_path.glob("intents/**/*.json")) == []
    pending = list((tmp_path / "alerts" / "pending").glob("*.json"))
    assert pending
    assert "paper_only_violation" in pending[0].read_text(encoding="utf-8")


def test_engine_rejects_a_non_paper_mode(tmp_path) -> None:
    edited = dict(DOCUMENT)
    edited["mode"] = "liv" + "e"  # refused: paper_only_violation
    approved = ApprovedStrategy(
        strategy_id="trend-follow",
        version=1,
        spec_hash=spec_hash(edited),
    )
    with pytest.raises(PaperOnlyViolation):
        StrategyEngine().run(edited, approved, trading_home=tmp_path, session_date="2026-10-02")
    assert list(tmp_path.glob("intents/**/*.json")) == []


def test_registry_is_paper_terminal_and_news_v4_only(tmp_path) -> None:
    path = tmp_path / "strategies" / "registry.jsonl"
    with pytest.raises(RegistryError, match="paper_only"):
        append_event(
            path,
            {
                "strategy_id": "sig_trend",
                "version": 2,
                "event": "liv" + "e_promoted",  # refused: paper_only
                "actor": "user",
                "mode_after": "paper",
            },
        )
    with pytest.raises(RegistryError, match="paper_only"):
        append_event(
            path,
            {
                "strategy_id": "sig_trend",
                "version": 2,
                "event": "paper_started",
                "actor": "system",
                "mode_after": "liv" + "e",  # refused: paper_only_violation
            },
        )
    with pytest.raises(RegistryError, match="v4"):
        append_event(
            path,
            {
                "strategy_id": "news_event",
                "version": 3,
                "event": "paper_started",
                "actor": "system",
                "spec_hash": "sha256:" + "ab" * 32,
                "mode_after": "paper",
            },
        )
    document = {"id": "news_event", "version": 4, "mode": "paper", "rules": {"n": 1}}
    append_event(
        path,
        {
            "strategy_id": "news_event",
            "version": 4,
            "event": "paper_started",
            "actor": "system",
            "spec_hash": spec_hash(document),
            "mode_after": "paper",
        },
    )
    assert is_runnable(path, document)
    assert is_runnable(path, {"id": "news_event", "version": 3, "mode": "paper"}) is False
    record = r23_record("news_event", 4, spec_hash(document), 3)
    assert record["mode_after"] == "paper"
    append_event(path, record)
    assert is_runnable(path, document)


def test_update_items_share_the_root_id() -> None:
    assert root_news_id("20261001-MU-00e07b52-u1") == "20261001-MU-00e07b52"
    assert root_news_id("20261001-MU-00e07b52-u") == "20261001-MU-00e07b52"
    items = [
        {
            "news_id": "20261001-MU-00e07b52",
            "status": "new",
            "judged_at": "2026-10-01T13:00:00+08:00",
        },
        {
            "news_id": "20261001-MU-00e07b52-u1",
            "status": "update",
            "judged_at": "2026-10-01T15:00:00+08:00",
        },
    ]
    kept = dedupe_chain(items)
    assert len(kept) == 1 and kept[0]["status"] == "update"
    update = {
        "news_id": "20261001-MU-00e07b52-u1",
        "status": "update",
        "event_type": "earnings",
        "tickers": [{"ticker": "MU", "relation": "subject", "direction": "positive", "impact": 2}],
    }
    judged = classify_item(update, "BUY")
    assert judged[0].outcome == "candidate"
    assert judged[0].news_id == "20261001-MU-00e07b52"
    assert _event_reversed(
        [
            {
                "ticker": "MU",
                "news_id": "root-u1",
                "status": "update",
                "direction": "mixed",
                "impact": 2,
                "judged_at": "2026-10-01T15:00:00+08:00",
            }
        ],
        "MU",
        "root",
        "2026-10-02",
    )


def test_paper_evaluation_does_not_change_mode(tmp_path) -> None:
    created = datetime(2026, 10, 2, 7, 10, tzinfo=HKT)
    report = evaluate_paper(
        strategy_id="sig_trend",
        version=2,
        session_date="2026-10-02",
        calendar_days=30,
        fill_count=12,
        period_return=0.04,
        drawdown=-0.08,
        return_p5=-0.10,
        return_p95=0.20,
        mdd_p5=-0.15,
        anomalies=[{"kind": "R9_HALT", "cleared": True}],
        reference={"trade_match_rate": 0.9},
    )
    assert report["mode_after"] == "paper"
    assert report["promotes"] is False
    assert report["gates"]["G1"]["met"] and report["gates"]["G2"]["met"]
    assert report["gates"]["G3"]["met"]
    assert report["gates"]["G4"]["met"] is False
    assert g4_passes([{"kind": "R9_HALT", "cleared": True}]) is False
    path, event = write_paper_evaluation(tmp_path, report, created_at=created)
    assert path.name == "sig_trend.v2_2026-10-02.json"
    assert event["event"] == "paper_evaluated"
    assert event["mode_after"] == "paper"
    pending = next((tmp_path / "alerts" / "pending").glob("*.json"))
    assert "paper_eval_status" in pending.read_text(encoding="utf-8")
    short = evaluate_paper(
        strategy_id="sig_trend",
        version=2,
        session_date="2026-10-02",
        calendar_days=10,
        fill_count=2,
        period_return=0.5,
        drawdown=-0.40,
        return_p5=-0.10,
        return_p95=0.20,
        mdd_p5=-0.15,
    )
    assert short["gates"]["G1"]["met"] is False
    assert short["gates"]["G3"]["met"] is False
    assert short["mode_after"] == "paper"
