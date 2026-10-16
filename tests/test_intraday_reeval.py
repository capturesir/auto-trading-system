"""Scheme B intraday re-eval: watchlist, debounce, paper gate, alerts-only monitor."""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.intraday.reeval import (
    Held,
    PaperGateError,
    clamp_interval,
    parse_client_id,
    run_once,
)
from auto_trading.market.bars import write_parquet
from auto_trading.market.indicators import sma
from auto_trading.market.yahoo import cache_path
from auto_trading.strategy.session import run_session

ROOT = Path(__file__).resolve().parents[1]
MACAU = ZoneInfo("Asia/Macau")
SESSION = "2026-10-16"
MORNING = datetime(2026, 10, 16, 7, 5, tzinfo=MACAU)
MIDDAY = datetime(2026, 10, 16, 11, 0, tzinfo=MACAU)
PIN = "DUR243736"


def _rising(ticker: str = "0941.HK") -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    day = date(2026, 8, 1)
    for index in range(70):
        price = 20 + index * 0.4
        stamp = day.isoformat()
        rows.append(
            {
                "date": stamp,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "adj_close": price,
                "volume": 1_000_000,
                "available_at": f"{stamp}T07:00:00+08:00",
                "source": "test",
            }
        )
        day += timedelta(days=1)
    assert rows[-1]["date"] < SESSION
    return rows


def _flat(price: float = 100.0) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    day = date(2026, 8, 1)
    for _index in range(40):
        stamp = day.isoformat()
        rows.append(
            {
                "date": stamp,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "adj_close": price,
                "volume": 1_000_000,
                "available_at": f"{stamp}T07:00:00+08:00",
                "source": "test",
            }
        )
        day += timedelta(days=1)
    return rows


def _signal(ticker: str = "0941.HK") -> dict[str, object]:
    return {
        "signal_id": f"20261016-{ticker}-BUY",
        "ticker": ticker,
        "market": "HK",
        "name": "sample",
        "asset_type": "equity",
        "currency": "HKD",
        "side": "BUY",
        "confidence": 80,
        "fair_value": 120.0,
        "market_price": 47.6,
        "price_date": "2026-10-15",
        "upside_pct": 150.0,
        "horizon": "6-12m",
        "rationale": "fixture",
        "report_path": "/tmp/report.md",
        "expires_at": "2026-10-20T06:30:00+08:00",
    }


def _account(home: Path, *, account: str | None = PIN, mode: str | None = None) -> None:
    payload: dict[str, object] = {"positions": [], "nlv": 1_000_000}
    if account is not None:
        payload["account"] = account
    if mode is not None:
        payload["mode"] = mode
    path = home / "state" / "account.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _risk(home: Path, now: datetime) -> None:
    write_risk_limits(home, build_risk_limits(now=now, nlv=1_000_000, account=PIN))


def _approved(tmp: Path) -> Path:
    dest = tmp / "approved"
    shutil.copytree(ROOT / "strategies" / "approved", dest)
    return dest


def _cache(home: Path, ticker: str, rows: list[dict[str, object]]) -> Path:
    cache = home / "bars"
    write_parquet(cache_path(cache, ticker), rows)
    return cache


def _morning(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    monkeypatch.setenv("INTRADAY_REEVAL_INCLUDE_VALID_BUYS", "1")
    monkeypatch.delenv("INTRADAY_REEVAL_WRITE_INTENTS", raising=False)
    monkeypatch.delenv("INTRADAY_REEVAL_WRITE_EXITS", raising=False)
    monkeypatch.delenv("IB_CLIENT_ID_REEVAL", raising=False)
    home = tmp / "home"
    home.mkdir()
    _account(home)
    _risk(home, MORNING)
    signals = home / "signals.json"
    signals.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-10-16T06:50:00+08:00",
                "generated_by": "test",
                "price_date": "2026-10-15",
                "signals": [_signal()],
            }
        ),
        encoding="utf-8",
    )
    cache = _cache(home, "0941.HK", _rising())
    approved = _approved(tmp)
    run_session(
        trading_home=home,
        session_date=SESSION,
        signals_path=signals,
        bars_cache=cache,
        approved_dir=approved,
        now=MORNING,
        refresh_bars=False,
    )
    _risk(home, MIDDAY)
    return home, cache, approved


def _near_ma(rows: list[dict[str, object]]) -> float:
    closes = [float(row["close"]) for row in rows]
    average = sma(closes, 20)[-1]
    assert average is not None
    return float(average) * 1.005


def _saved(home: Path, name: str) -> dict[str, object]:
    return json.loads((home / "intents" / SESSION / name).read_text(encoding="utf-8"))


def _entries(home: Path, name: str) -> list[dict[str, object]]:
    body = _saved(home, name)
    return [row for row in body["intents"] if row["intent_type"] == "ENTRY"]


def test_morning_watchlist_records_confirm_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _cache_dir, _approved_dir = _morning(tmp_path, monkeypatch)
    watch = json.loads((home / "state" / "intraday_watchlist.json").read_text(encoding="utf-8"))
    assert watch["schema_version"] == "intraday_watchlist/1.0"
    assert watch["session_date"] == SESSION
    missed = [
        row
        for row in watch["items"]
        if row["ticker"] == "0941.HK" and row["strategy_id"] == "sig_pullback"
    ]
    assert len(missed) == 1
    assert missed[0]["kind"] == "confirm_fail"
    assert missed[0]["rules"]
    assert "gap=" in missed[0]["reason"]
    passed = [
        row
        for row in watch["items"]
        if row["ticker"] == "0941.HK"
        and row["strategy_id"] == "sig_trend"
        and row["kind"] == "valid_buy"
    ]
    assert passed
    assert _entries(home, "sig_pullback.v3.json") == []
    debounce = json.loads((home / "state" / "intraday_debounce.json").read_text(encoding="utf-8"))
    assert f"sig_trend|0941.HK|{SESSION}" in debounce["entries"]
    assert f"sig_pullback|0941.HK|{SESSION}" not in debounce["entries"]


def test_dry_run_marks_would_entry_and_writes_no_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, cache, approved = _morning(tmp_path, monkeypatch)
    before = _saved(home, "sig_pullback.v3.json")
    report = run_once(
        home,
        now=MIDDAY,
        session_date=SESSION,
        quotes={"0941.HK": _near_ma(_rising())},
        positions=[],
        write_intents=False,
        write_exits=False,
        bars_cache=cache,
        approved_dir=approved,
    )
    assert report["refused"] is False
    assert report["write_intents"] is False
    row = next(item for item in report["items"] if item["strategy_id"] == "sig_pullback")
    assert row["market_open"] is True
    assert row["confirm_pass"] is True
    assert row["would_entry"] is True
    assert row["written"] is False
    assert row["reason"] == "write_flag_off"
    assert _saved(home, "sig_pullback.v3.json")["revision"] == before["revision"]
    assert _entries(home, "sig_pullback.v3.json") == []


def test_write_flag_appends_one_entry_then_debounces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, cache, approved = _morning(tmp_path, monkeypatch)
    quote = _near_ma(_rising())
    kwargs = {
        "now": MIDDAY,
        "session_date": SESSION,
        "quotes": {"0941.HK": quote},
        "positions": [],
        "write_intents": True,
        "write_exits": False,
        "bars_cache": cache,
        "approved_dir": approved,
    }
    first = run_once(home, **kwargs)
    saved = _saved(home, "sig_pullback.v3.json")
    opened = _entries(home, "sig_pullback.v3.json")
    assert len(opened) == 1
    assert opened[0]["ticker"] == "0941.HK"
    assert opened[0]["source"] == "intraday_reeval"
    assert opened[0]["side"] == "BUY"
    assert saved["mode"] == "paper"
    assert saved["live_size_multiplier"] is None
    revision = saved["revision"]
    row = next(item for item in first["items"] if item["strategy_id"] == "sig_pullback")
    assert row["written"] is True
    trend = next(item for item in first["items"] if item["strategy_id"] == "sig_trend")
    assert trend["debounced"] is True
    assert trend["written"] is False
    second = run_once(home, **kwargs)
    again = next(item for item in second["items"] if item["strategy_id"] == "sig_pullback")
    assert again["would_entry"] is True
    assert again["debounced"] is True
    assert again["written"] is False
    assert len(_entries(home, "sig_pullback.v3.json")) == 1
    assert _saved(home, "sig_pullback.v3.json")["revision"] == revision


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("kill", "kill_switch"), ("paused", "paused"), ("r9", "r9_halt")],
)
def test_entry_gates_block_a_passing_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, expected: str
) -> None:
    home, cache, approved = _morning(tmp_path, monkeypatch)
    if kind == "kill":
        (home / "KILL_SWITCH").write_text("manual\n", encoding="utf-8")
    elif kind == "paused":
        (home / "state" / "paused").write_text("paused\n", encoding="utf-8")
    else:
        from auto_trading.risk import r9_state

        document = r9_state._halted_document(PIN, MIDDAY, "2026-10-02", 1_000_000.0)
        r9_state.write_cycle(home, r9_state._seal(document))
    report = run_once(
        home,
        now=MIDDAY,
        session_date=SESSION,
        quotes={"0941.HK": _near_ma(_rising())},
        positions=[],
        write_intents=True,
        write_exits=False,
        bars_cache=cache,
        approved_dir=approved,
    )
    row = next(item for item in report["items"] if item["strategy_id"] == "sig_pullback")
    assert row["would_entry"] is True
    assert row["written"] is False
    assert row["reason"] == expected
    assert _entries(home, "sig_pullback.v3.json") == []
    trend = _entries(home, "sig_trend.v3.json")
    assert len(trend) == 1
    assert trend[0].get("source") != "intraday_reeval"


@pytest.mark.parametrize(
    ("account", "mode", "env", "reason"),
    [
        ("U1234567", None, None, "account_refused"),
        ("DU999999", None, "DU999999", "account_refused"),
        (PIN, "other", PIN, "mode_refused"),
        (None, None, None, "account_missing"),
    ],
)
def test_paper_gate_refuses_non_paper_accounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    account: str | None,
    mode: str | None,
    env: str | None,
    reason: str,
) -> None:
    if env is None:
        monkeypatch.delenv("IBKR_PAPER_ACCOUNT", raising=False)
    else:
        monkeypatch.setenv("IBKR_PAPER_ACCOUNT", env)
    home = tmp_path / "home"
    home.mkdir()
    if account is not None or mode is not None:
        _account(home, account=account, mode=mode)
    report = run_once(home, now=MIDDAY, session_date=SESSION, quotes={}, positions=[])
    assert report["refused"] is True
    assert report["reason"] == reason
    assert not (home / "intents").exists()


def test_account_file_pin_is_enough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IBKR_PAPER_ACCOUNT", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    _account(home)
    report = run_once(home, now=MIDDAY, session_date=SESSION, quotes={}, positions=[])
    assert report["refused"] is False


def test_forbidden_client_id_refuses_before_any_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    monkeypatch.setenv("IB_CLIENT_ID_REEVAL", "15")
    home = tmp_path / "home"
    home.mkdir()
    _account(home)
    report = run_once(
        home,
        now=MIDDAY,
        session_date=SESSION,
        quotes={"0941.HK": 40.0},
        positions=[],
        write_intents=True,
    )
    assert report["refused"] is True
    assert report["reason"] == "client_id_forbidden"
    assert not (home / "intents").exists()
    with pytest.raises(PaperGateError):
        parse_client_id("11")


def test_interval_stays_between_one_and_five_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTRADAY_REEVAL_INTERVAL_SEC", "10")
    assert clamp_interval() == 60
    monkeypatch.setenv("INTRADAY_REEVAL_INTERVAL_SEC", "900")
    assert clamp_interval() == 300
    monkeypatch.setenv("INTRADAY_REEVAL_INTERVAL_SEC", "120")
    assert clamp_interval() == 120


def test_exit_flag_off_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _exit_home(tmp_path, monkeypatch)
    report = run_once(
        home,
        now=MIDDAY,
        session_date=SESSION,
        quotes={"0700.HK": 80.0},
        positions=[_held()],
        write_intents=False,
        write_exits=False,
        bars_cache=home / "bars",
    )
    row = report["exits"][0]
    assert row["would_exit"] is True
    assert row["exit_reason"] == "stop_loss_pct"
    assert row["written"] is False
    assert row["reason"] == "write_flag_off"
    assert not (home / "intents").exists()


def test_exit_is_written_once_and_a_prior_reduce_does_not_block_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _exit_home(tmp_path, monkeypatch)
    from auto_trading.alerts.holdings_state import atomic_write_json
    from auto_trading.intraday.reeval import debounce_path

    atomic_write_json(
        debounce_path(home),
        {
            "schema_version": "intraday_debounce/1.0",
            "session_date": SESSION,
            "entries": [],
            "exits": [f"sig_pullback|0700.HK|{SESSION}|indicator_take_profit"],
            "full_exits": [],
        },
    )
    first = run_once(
        home,
        now=MIDDAY,
        session_date=SESSION,
        quotes={"0700.HK": 80.0},
        positions=[_held()],
        write_intents=False,
        write_exits=True,
        bars_cache=home / "bars",
    )
    assert first["exits"][0]["written"] is True
    saved = _saved(home, "sig_pullback.v3.json")
    exits = [row for row in saved["intents"] if row["intent_type"] == "EXIT"]
    assert len(exits) == 1
    assert exits[0]["exit_reason"] == "stop_loss_pct"
    assert exits[0]["parent_intent_id"] == "parent-1"
    assert exits[0]["source"] == "intraday_reeval"
    assert exits[0]["side"] == "SELL"
    assert exits[0]["qty"] == 2000
    revision = saved["revision"]
    second = run_once(
        home,
        now=MIDDAY,
        session_date=SESSION,
        quotes={"0700.HK": 80.0},
        positions=[_held()],
        write_intents=False,
        write_exits=True,
        bars_cache=home / "bars",
    )
    assert second["exits"][0]["would_exit"] is True
    assert second["exits"][0]["debounced"] is True
    assert second["exits"][0]["written"] is False
    assert _saved(home, "sig_pullback.v3.json")["revision"] == revision
    saved_again = _saved(home, "sig_pullback.v3.json")
    exits_again = [row for row in saved_again["intents"] if row["intent_type"] == "EXIT"]
    assert len(exits_again) == 1


def test_units_document_paper_flags_and_leave_the_monitor_alerts_only() -> None:
    deploy = ROOT / "vps" / "deploy"
    service = (deploy / "trading-intraday-reeval.service").read_text(encoding="utf-8")
    timer = (deploy / "trading-intraday-reeval.timer").read_text(encoding="utf-8")
    assert "IBKR_PAPER_ACCOUNT=DUR243736" in service
    assert "IB_CLIENT_ID_REEVAL=16" in service
    assert "INTRADAY_REEVAL_WRITE_INTENTS=0" in service
    assert "INTRADAY_REEVAL_WRITE_EXITS=0" in service
    assert "auto_trading.intraday" in service
    assert "holdings_monitor" not in service
    assert "Restart=always" not in service
    assert "/srv/trading/intents" in service
    assert "OnUnitActiveSec=2min" in timer
    assert "systemctl enable" not in service
    assert "systemctl enable" not in timer
    monitor = (deploy / "trading-holdings-monitor.service").read_text(encoding="utf-8")
    assert "intents" not in monitor.split("ReadWritePaths=", 1)[-1].splitlines()[0]
    source = (ROOT / "src" / "auto_trading" / "alerts" / "holdings_monitor.py").read_text(
        encoding="utf-8"
    )
    assert "write_intents" not in source
    assert "place_order" not in source
    guide = (ROOT / "docs" / "intraday_reeval_enable_steps.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "intraday_reeval_design_v1.md").read_text(encoding="utf-8")
    assert "INTRADAY_REEVAL_WRITE_INTENTS" in guide
    assert "1min" in guide and "5min" in guide
    assert "holdings-monitor" in guide
    assert "alerts" in design or "提醒" in design
    sync = (deploy / "sync_vps_to_box.txt").read_text(encoding="utf-8")
    assert "state/intraday_watchlist.json" in sync
    assert "state/intraday_reeval.json" in sync
    box = (deploy / "sync_box_to_vps.txt").read_text(encoding="utf-8")
    assert "intraday_watchlist.json" not in box


def _exit_home(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    monkeypatch.delenv("IB_CLIENT_ID_REEVAL", raising=False)
    home = tmp / "home"
    home.mkdir()
    _account(home)
    _risk(home, MIDDAY)
    _cache(home, "0700.HK", _flat())
    return home


def _held() -> Held:
    return Held(
        ticker="0700.HK",
        qty=2000,
        avg_cost=100.0,
        strategy_id="sig_pullback",
        parent_intent_id="parent-1",
    )
