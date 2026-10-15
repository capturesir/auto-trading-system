"""Holdings monitor: alerts only, paper account DUR243736, client id 15."""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from auto_trading.alerts.forwarder import scan_once
from auto_trading.alerts.holdings_monitor import (
    FORBIDDEN_CLIENT_IDS,
    MonitorConfig,
    config_from_env,
    main,
    run_forever,
    run_once,
    wait_seconds,
)
from auto_trading.alerts.holdings_positions import PositionView, broker_stop_for
from auto_trading.alerts.holdings_state import atomic_write_json
from auto_trading.alerts.holdings_ta import (
    BarContext,
    MarketSnap,
    evaluate_position,
    market_open,
    resolve_stop,
)
from auto_trading.market.quotes import MarketDataError, QuoteRouter
from auto_trading.risk.gate import Quote

ROOT = Path(__file__).resolve().parents[1]
HKT = timezone(timedelta(hours=8))
OPEN = datetime(2026, 10, 2, 22, 0, tzinfo=HKT)
CLOSED = datetime(2026, 10, 17, 22, 0, tzinfo=HKT)


def _cfg(home: Path) -> MonitorConfig:
    return MonitorConfig(home)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def _snapshot(home: Path, positions: list[dict], *, at: datetime = OPEN) -> None:
    _write(
        home / "state" / "holdings_snapshot.json",
        {
            "schema_version": "holdings_snapshot/1.0",
            "trading_scope": "paper_only",
            "account": "DUR243736",
            "at": at.isoformat(timespec="seconds"),
            "generated_at": at.isoformat(timespec="seconds"),
            "source_at": at.isoformat(timespec="seconds"),
            "position_count": len(positions),
            "positions": positions,
        },
    )


def _leg(**kwargs: object) -> dict:
    row: dict = {
        "ticker": "AAPL",
        "qty": 10,
        "avg_cost": 100.0,
        "market": "US",
        "currency": "USD",
    }
    row.update(kwargs)
    return row


def _pending(home: Path) -> list[Path]:
    folder = home / "alerts" / "pending"
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob("*.json") if not path.name.endswith(".tmp"))


def _load_research(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_market_session_gate_matches_the_calendar() -> None:
    assert market_open("US", OPEN) is True
    assert market_open("US", CLOSED) is False


def test_empty_book_heartbeats_without_pending_or_intents(tmp_path: Path) -> None:
    _snapshot(tmp_path, [])
    (tmp_path / "intents").mkdir()
    (tmp_path / "intents" / "keep.txt").write_text("stay\n", encoding="utf-8")
    (tmp_path / "approvals").mkdir()
    (tmp_path / "approvals" / "keep.txt").write_text("stay\n", encoding="utf-8")
    kept = tmp_path / "alerts" / "pending" / "20260101T000000_hm_old_AAPL.json"
    kept.parent.mkdir(parents=True)
    kept.write_text("{}\n", encoding="utf-8")

    result = run_once(_cfg(tmp_path), OPEN)

    assert result["n_positions"] == 0
    assert result["alerts"] == 0
    assert result["position_source"] == "snapshot"
    state = json.loads((tmp_path / "state" / "holdings_monitor.json").read_text(encoding="utf-8"))
    assert state["n_positions"] == 0
    assert state["last_ok_at"]
    assert _pending(tmp_path) == [kept]
    assert (tmp_path / "intents" / "keep.txt").read_text(encoding="utf-8") == "stay\n"
    assert list((tmp_path / "approvals").iterdir()) == [tmp_path / "approvals" / "keep.txt"]


def test_one_position_writes_hm_alert_the_forwarder_can_take(tmp_path: Path) -> None:
    _snapshot(tmp_path, [_leg()])
    result = run_once(_cfg(tmp_path), OPEN, quotes={"AAPL": MarketSnap(last=90, source="ibkr")})

    assert result["alerts"] >= 1
    files = _pending(tmp_path)
    assert files
    body = json.loads(files[0].read_text(encoding="utf-8"))
    assert body["schema_version"] == "alert/1.0"
    assert body["rule_id"].startswith("hm_")
    assert "exec" not in body["rule_id"]
    assert "_exec_" not in files[0].name
    assert body["ticker"] == "AAPL"
    scan_once(tmp_path, OPEN)
    sent = list((tmp_path / "alerts" / "sent").glob("*.json"))
    assert sent
    forwarded = json.loads(sent[0].read_text(encoding="utf-8"))
    assert forwarded["schema_version"] == "alert/1.0"
    assert forwarded["rule_id"].startswith("hm_")
    assert list((tmp_path / "notifications" / "outbox").glob("*.json"))


def test_debounce_waits_five_minutes_for_warning_and_two_for_stop(tmp_path: Path) -> None:
    _snapshot(tmp_path, [_leg(protective_stop_pct=10)])
    quotes = {"AAPL": MarketSnap(last=80, source="ibkr")}
    home = _cfg(tmp_path)
    run_once(home, OPEN, quotes=quotes)
    assert any("_hm_stop_" in path.name for path in _pending(tmp_path))
    assert any("_hm_vs_entry_" in path.name for path in _pending(tmp_path))

    run_once(home, OPEN + timedelta(seconds=30), quotes=quotes)
    assert len([path for path in _pending(tmp_path) if "_hm_stop_" in path.name]) == 1
    assert len([path for path in _pending(tmp_path) if "_hm_vs_entry_" in path.name]) == 1

    run_once(home, OPEN + timedelta(seconds=121), quotes=quotes)
    assert len([path for path in _pending(tmp_path) if "_hm_stop_" in path.name]) == 2
    assert len([path for path in _pending(tmp_path) if "_hm_vs_entry_" in path.name]) == 1

    run_once(home, OPEN + timedelta(seconds=301), quotes=quotes)
    assert len([path for path in _pending(tmp_path) if "_hm_vs_entry_" in path.name]) == 2


def test_leaving_the_breach_arms_the_next_edge(tmp_path: Path) -> None:
    _snapshot(tmp_path, [_leg()])
    home = _cfg(tmp_path)
    run_once(home, OPEN, quotes={"AAPL": MarketSnap(last=90)})
    assert len(_pending(tmp_path)) == 1
    run_once(home, OPEN + timedelta(minutes=1), quotes={"AAPL": MarketSnap(last=100)})
    state = json.loads((tmp_path / "state" / "holdings_monitor.json").read_text(encoding="utf-8"))
    last = state["per_ticker"]["AAPL"]["last_alert"]
    assert all(row["in_breach"] is False for row in last.values())
    run_once(home, OPEN + timedelta(minutes=2), quotes={"AAPL": MarketSnap(last=90)})
    assert len(_pending(tmp_path)) == 2


def test_kill_switch_still_runs_and_raises_warning(tmp_path: Path) -> None:
    _snapshot(tmp_path, [_leg()])
    (tmp_path / "KILL_SWITCH").write_text("manual\n", encoding="utf-8")
    result = run_once(_cfg(tmp_path), OPEN, quotes={"AAPL": MarketSnap(last=90)})
    assert result["account_status"] == "kill_switch"
    assert result["n_positions"] == 1
    body = json.loads(_pending(tmp_path)[0].read_text(encoding="utf-8"))
    assert body["severity"] == "critical"
    assert body["context"]["account_status"] == "kill_switch"
    assert body["rule_id"] == "hm_vs_entry"


def test_r9_halt_keeps_the_original_warning(tmp_path: Path) -> None:
    from auto_trading.risk import r9_state

    document = r9_state._halted_document("DUR243736", OPEN, "2026-10-02", 1_000_000.0)
    r9_state.write_cycle(tmp_path, r9_state._seal(document))
    _snapshot(tmp_path, [_leg()])
    result = run_once(_cfg(tmp_path), OPEN, quotes={"AAPL": MarketSnap(last=90)})
    assert result["account_status"] == "r9_halt"
    body = json.loads(_pending(tmp_path)[0].read_text(encoding="utf-8"))
    assert body["severity"] == "warning"
    assert body["context"]["account_status"] == "r9_halt"


def test_paused_book_still_warns(tmp_path: Path) -> None:
    _snapshot(tmp_path, [_leg()])
    (tmp_path / "state" / "paused").write_text("paused\n", encoding="utf-8")
    result = run_once(_cfg(tmp_path), OPEN, quotes={"AAPL": MarketSnap(last=90)})
    assert result["account_status"] == "paused"
    body = json.loads(_pending(tmp_path)[0].read_text(encoding="utf-8"))
    assert body["severity"] == "warning"


def test_snapshot_beats_account(tmp_path: Path) -> None:
    _snapshot(tmp_path, [_leg(ticker="AAPL")])
    _write(
        tmp_path / "state" / "account.json",
        {
            "schema_version": "account/1.0",
            "at": OPEN.isoformat(),
            "positions": [_leg(ticker="MSFT")],
        },
    )
    result = run_once(
        _cfg(tmp_path),
        OPEN,
        quotes={"AAPL": MarketSnap(last=90), "MSFT": MarketSnap(last=10)},
    )
    assert result["position_source"] == "snapshot"
    names = set()
    for path in _pending(tmp_path):
        names.add(json.loads(path.read_text(encoding="utf-8"))["ticker"])
    assert names == {"AAPL"}


def test_partial_snapshot_retries_and_uses_account(tmp_path: Path) -> None:
    path = tmp_path / "state" / "holdings_snapshot.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"schema_version": "holdings_snapshot/1.0", "positions": [', encoding="utf-8")
    _write(
        tmp_path / "state" / "account.json",
        {"at": OPEN.isoformat(), "positions": [_leg(ticker="AAPL")]},
    )
    result = run_once(_cfg(tmp_path), OPEN, quotes={"AAPL": MarketSnap(last=90)})
    assert result["retry_incomplete_snapshot"] is True
    assert result["position_source"] == "account"
    state = json.loads((tmp_path / "state" / "holdings_monitor.json").read_text(encoding="utf-8"))
    assert state["retry_incomplete_snapshot"] is True
    assert _pending(tmp_path)


def test_closed_session_skips_entry_style_and_keeps_stop_distance(tmp_path: Path) -> None:
    _snapshot(tmp_path, [_leg(protective_stop_pct=10)])
    bars = {
        "AAPL": BarContext(
            ma20=95,
            ma50=90,
            prior_high=70,
            prior_low=60,
            atr=1,
            vol20=100,
            volume=500,
        )
    }
    quotes = {"AAPL": MarketSnap(last=80, high=81, low=79, volume=500)}
    run_once(_cfg(tmp_path), CLOSED, quotes=quotes, bars=bars)
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in _pending(tmp_path)]
    types = {body["type"] for body in payloads}
    rules = {body["rule_id"] for body in payloads}
    assert "holdings_ma_break" not in types
    assert "holdings_day_range" not in types
    assert "hm_stop" in rules

    for path in _pending(tmp_path):
        path.unlink()
    run_once(_cfg(tmp_path), OPEN, quotes=quotes, bars=bars)
    types = {json.loads(path.read_text(encoding="utf-8"))["type"] for path in _pending(tmp_path)}
    assert "holdings_ma_break" in types
    assert "holdings_day_range" in types


def test_broker_stop_beats_strategy_percent(tmp_path: Path) -> None:
    _snapshot(tmp_path, [_leg(protective_stop_pct=10)])
    _write(
        tmp_path / "state" / "account.json",
        {
            "at": OPEN.isoformat(),
            "positions": [],
            "open_orders": [{"symbol": "AAPL", "order_type": "STP", "stop_price": 96}],
        },
    )
    run_once(_cfg(tmp_path), OPEN, quotes={"AAPL": MarketSnap(last=96.2)})
    bodies = [json.loads(path.read_text(encoding="utf-8")) for path in _pending(tmp_path)]
    stop = next(body for body in bodies if body["rule_id"] == "hm_stop")
    assert stop["context"]["stop_source"] == "broker"
    assert stop["context"]["stop_price"] == pytest.approx(96)


def test_strategy_percent_when_no_broker_stop() -> None:
    position = PositionView(
        ticker="AAPL",
        qty=10,
        avg_cost=100,
        protective_stop_pct=10,
        side="long",
    )
    decision = resolve_stop(position, None)
    assert decision.source == "strategy"
    assert decision.price == pytest.approx(90)
    short = PositionView(ticker="AAPL", qty=-5, avg_cost=100, protective_stop_pct=10, side="short")
    assert resolve_stop(short, None).price == pytest.approx(110)
    exempt = PositionView(
        ticker="SPY",
        qty=1,
        avg_cost=100,
        protective_stop_pct=10,
        protective_stop_exempt=True,
    )
    assert resolve_stop(exempt, 90).price is None


def test_two_strategies_keep_separate_stops(tmp_path: Path) -> None:
    _snapshot(
        tmp_path,
        [
            _leg(strategy_id="alpha", protective_stop_pct=10),
            _leg(strategy_id="beta", protective_stop_pct=20),
        ],
    )
    run_once(
        _cfg(tmp_path),
        OPEN,
        quotes={"AAPL": MarketSnap(last=91)},
        bars={"AAPL": BarContext(atr=15)},
    )
    stops = []
    for path in _pending(tmp_path):
        body = json.loads(path.read_text(encoding="utf-8"))
        if body["rule_id"] != "hm_stop":
            continue
        stops.append((body["context"]["strategy_id"], body["context"]["stop_price"]))
    assert ("alpha", pytest.approx(90)) in stops
    assert ("beta", pytest.approx(80)) in stops


def test_missing_files_warn_once_per_hour(tmp_path: Path) -> None:
    first = run_once(_cfg(tmp_path), OPEN)
    assert first["position_source"] == "none"
    assert first["alerts"] == 1
    assert _pending(tmp_path)[0].name.endswith("_hm_data_error_MULTI.json")
    second = run_once(_cfg(tmp_path), OPEN + timedelta(minutes=10))
    assert second["alerts"] == 0
    assert len(_pending(tmp_path)) == 1
    third = run_once(_cfg(tmp_path), OPEN + timedelta(seconds=3601))
    assert third["alerts"] == 1
    assert len(_pending(tmp_path)) == 2


def test_many_alerts_collapse_to_one_summary(tmp_path: Path) -> None:
    positions = [_leg(ticker=f"S{index:02d}", market="US") for index in range(21)]
    _snapshot(tmp_path, positions)
    quotes = {str(row["ticker"]): MarketSnap(last=90) for row in positions}
    result = run_once(_cfg(tmp_path), OPEN, quotes=quotes)
    assert result["alerts"] == 1
    files = _pending(tmp_path)
    assert len(files) == 1
    body = json.loads(files[0].read_text(encoding="utf-8"))
    assert body["rule_id"] == "hm_summary"
    assert body["ticker"] == "MULTI"


def test_yahoo_fallback_on_10197_still_alerts(tmp_path: Path) -> None:
    _snapshot(tmp_path, [_leg()])

    def ibkr(_symbol: str) -> Quote:
        raise MarketDataError("busy", code=10197)

    def public(_symbol: str) -> Quote:
        return Quote(last=90, as_of=OPEN, source="yahoo", data_type="public", received_at=OPEN)

    result = run_once(_cfg(tmp_path), OPEN, quote_router=QuoteRouter(ibkr, public))
    assert result["quote_stats"]["yahoo"] == 1
    body = json.loads(_pending(tmp_path)[0].read_text(encoding="utf-8"))
    assert body["price_source"] == "yahoo"
    assert body["rule_id"].startswith("hm_")


def test_one_ticker_failing_does_not_stop_the_tick(tmp_path: Path) -> None:
    _snapshot(tmp_path, [_leg(ticker="AAPL"), _leg(ticker="MSFT")])

    class Router:
        def fetch(self, symbol: str) -> MarketSnap:
            if symbol == "MSFT":
                raise TimeoutError("slow")
            return MarketSnap(last=90, source="ibkr")

    result = run_once(_cfg(tmp_path), OPEN, quote_router=Router())
    assert result["n_positions"] == 2
    assert result["quote_stats"]["fail"] == 1
    names = {json.loads(path.read_text(encoding="utf-8"))["ticker"] for path in _pending(tmp_path)}
    assert names == {"AAPL"}


def test_delayed_quote_is_still_an_alert(tmp_path: Path) -> None:
    _snapshot(tmp_path, [_leg()])
    run_once(_cfg(tmp_path), OPEN, quotes={"AAPL": MarketSnap(last=90, delay_sec=120)})
    body = json.loads(_pending(tmp_path)[0].read_text(encoding="utf-8"))
    assert body["context"]["delay_sec"] == pytest.approx(120)
    assert body["data_delay_min"] == pytest.approx(2)


def test_atomic_write_fsyncs_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = os.fsync

    def spy(fd: int) -> None:
        calls.append(fd)
        real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    target = tmp_path / "state" / "holdings_monitor.json"
    atomic_write_json(target, {"schema_version": "holdings_monitor_state/1.0"})
    assert calls
    assert not list(tmp_path.rglob("*.tmp"))
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == "holdings_monitor_state/1.0"


def test_client_id_15_is_required_and_reserved_ids_refuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert frozenset({11, 12, 13, 14, 31}) == FORBIDDEN_CLIENT_IDS
    monkeypatch.setenv("IB_READ_ONLY", "1")
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", "DUR243736")
    monkeypatch.setenv("IB_CLIENT_ID_HOLDINGS", "15")
    assert config_from_env(tmp_path).client_id == 15
    for forbidden in (11, 12, 13, 14, 31, 10):
        monkeypatch.setenv("IB_CLIENT_ID_HOLDINGS", str(forbidden))
        with pytest.raises(Exception, match="client_id_forbidden"):
            config_from_env(tmp_path)
    monkeypatch.delenv("IB_CLIENT_ID_HOLDINGS")
    with pytest.raises(Exception, match="client_id_missing"):
        config_from_env(tmp_path)


def test_paper_account_and_read_only_are_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IB_CLIENT_ID_HOLDINGS", "15")
    monkeypatch.setenv("IB_READ_ONLY", "1")
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", "DU1234567")
    with pytest.raises(Exception, match="paper_account_rejected"):
        config_from_env(tmp_path)
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", "DUR243736")
    monkeypatch.setenv("IB_READ_ONLY", "0")
    with pytest.raises(Exception, match="read_only_required"):
        config_from_env(tmp_path)


def test_main_once_on_an_empty_book(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _snapshot(tmp_path, [])
    monkeypatch.setenv("IB_CLIENT_ID_HOLDINGS", "15")
    monkeypatch.setenv("IB_READ_ONLY", "1")
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", "DUR243736")
    monkeypatch.setenv("TRADING_HOME", str(tmp_path))
    assert main(["--once", "--home", str(tmp_path)]) == 0
    state = json.loads((tmp_path / "state" / "holdings_monitor.json").read_text(encoding="utf-8"))
    assert state["n_positions"] == 0
    assert _pending(tmp_path) == []
    assert not (tmp_path / "intents").exists()


def test_main_refuses_the_watchdog_client_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IB_CLIENT_ID_HOLDINGS", "14")
    monkeypatch.setenv("IB_READ_ONLY", "1")
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", "DUR243736")
    assert main(["--home", str(tmp_path)]) == 2
    files = _pending(tmp_path)
    assert len(files) == 1
    body = json.loads(files[0].read_text(encoding="utf-8"))
    assert body["schema_version"] == "alert/1.0"
    assert body["rule_id"] == "hm_startup"
    assert "exec_alert" not in files[0].read_text(encoding="utf-8")


def test_unit_file_pins_client_15_and_is_not_enabled() -> None:
    unit = ROOT / "vps" / "deploy" / "trading-holdings-monitor.service"
    text = unit.read_text(encoding="utf-8")
    assert "IB_CLIENT_ID_HOLDINGS=15" in text
    assert "IB_READ_ONLY=1" in text
    assert "TRADING_HOME=/srv/trading" in text
    assert "IBKR_PAPER_ACCOUNT=DUR243736" in text
    assert "systemctl enable" not in text
    blob = []
    for path in (ROOT / "vps").rglob("*"):
        if path.is_file() and path.suffix in {".service", ".timer", ".sh", ".md", ".txt"}:
            blob.append(path.read_text(encoding="utf-8", errors="replace"))
    joined = "\n".join(blob)
    assert "systemctl enable --now trading-holdings-monitor" not in joined
    assert "systemctl enable trading-holdings-monitor" not in joined


def test_snapshot_syncs_vps_to_box_only() -> None:
    box = (ROOT / "vps" / "deploy" / "sync_box_to_vps.txt").read_text(encoding="utf-8")
    vps = (ROOT / "vps" / "deploy" / "sync_vps_to_box.txt").read_text(encoding="utf-8")
    assert "holdings_snapshot.json" not in box
    assert "state/holdings_snapshot.json" in vps
    readme = (ROOT / "research" / "holdings_monitor" / "README.md").read_text(encoding="utf-8")
    deploy = (ROOT / "vps" / "deploy" / "README.md").read_text(encoding="utf-8")
    for text in (readme, deploy):
        assert "/workspace/invest/b-group/holdings_snapshot.json" in text
        assert "/srv/trading/state/holdings_snapshot.json" in text


def test_research_reference_matches_the_installed_checks() -> None:
    checks = _load_research("hm_checks_ref", "research/holdings_monitor/checks.py")
    position = PositionView(ticker="AAPL", qty=1, avg_cost=100, protective_stop_pct=10)
    assert checks.resolve_stop is resolve_stop
    left = checks.resolve_stop(position, 95).price
    assert left == pytest.approx(resolve_stop(position, 95).price)
    assert checks.market_open("US", CLOSED) is False


def test_loop_survives_a_tick_and_records_heartbeat(tmp_path: Path) -> None:
    _snapshot(tmp_path, [])
    slept: list[float] = []
    run_forever(_cfg(tmp_path), sleep=slept.append, clock=lambda: OPEN, max_ticks=2)
    assert slept
    assert slept[0] > 0
    state = json.loads((tmp_path / "state" / "holdings_monitor.json").read_text(encoding="utf-8"))
    assert state["n_positions"] == 0
    assert state["tick_overrun"] is False


def test_wait_seconds_reports_overrun() -> None:
    assert wait_seconds(10, 30) == (20, False)
    assert wait_seconds(31, 30) == (0.0, True)


def test_broker_order_matches_the_ib_symbol() -> None:
    orders = {"700": [(None, 80.0)]}
    position = PositionView(ticker="0700.HK", qty=1000, avg_cost=90, market="HK")
    assert broker_stop_for(position, orders) == pytest.approx(80)


def test_entry_style_drafts_need_an_open_market() -> None:
    position = PositionView(
        ticker="AAPL",
        qty=1,
        avg_cost=100,
        market="US",
        protective_stop_pct=10,
    )
    snap = MarketSnap(last=80, high=81, low=79, volume=300)
    bars = BarContext(ma20=90, prior_high=70, prior_low=85, vol20=100, atr=2)
    closed = evaluate_position(
        position,
        snap,
        bars,
        market_is_open=False,
        broker_stop=None,
        account_status="running",
    )
    opened = evaluate_position(
        position,
        snap,
        bars,
        market_is_open=True,
        broker_stop=None,
        account_status="running",
    )
    closed_types = {item.alert_type for item in closed}
    opened_types = {item.alert_type for item in opened}
    assert "holdings_ma_break" not in closed_types
    assert "holdings_day_range" not in closed_types
    assert "hm_stop" in {item.rule_id for item in closed}
    assert "holdings_ma_break" in opened_types
    assert "holdings_day_range" in opened_types


def test_monitor_sources_do_not_place_orders() -> None:
    folder = ROOT / "src" / "auto_trading" / "alerts"
    text = "\n".join(path.read_text(encoding="utf-8") for path in folder.glob("holdings_*.py"))
    assert "place_order" not in text
    assert "submit_order" not in text
    assert "intents/" not in text
    assert "exec_alert" not in text
