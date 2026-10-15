"""Paper session runner: one CLI, one daily script, one timer."""

from __future__ import annotations

import importlib.util
import json
import logging
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.execution.services import load_intent_orders, sync_argv
from auto_trading.market.bars import write_parquet
from auto_trading.market.yahoo import cache_path
from auto_trading.strategy.session import SessionError, market_trigger_supported, run_session

ROOT = Path(__file__).resolve().parents[1]
MACAU = ZoneInfo("Asia/Macau")
NOW = datetime(2026, 10, 16, 7, 5, tzinfo=MACAU)
SESSION = "2026-10-16"


def _signal(
    *,
    ticker: str = "0883.HK",
    side: str = "BUY",
    confidence: int = 70,
    fair: float = 30.0,
    price: float = 18.0,
) -> dict[str, object]:
    return {
        "signal_id": f"20261016-{ticker}-{side}",
        "ticker": ticker,
        "market": "HK",
        "name": "sample",
        "asset_type": "equity",
        "currency": "HKD",
        "side": side,
        "confidence": confidence,
        "fair_value": fair,
        "market_price": price,
        "price_date": "2026-10-15",
        "upside_pct": (fair / price - 1) * 100,
        "horizon": "6-12m",
        "rationale": "fixture",
        "report_path": "/tmp/report.md",
        "expires_at": "2026-10-20T06:30:00+08:00",
    }


def _write_signals(path: Path, rows: list[dict[str, object]]) -> None:
    payload = {
        "schema_version": "1.0",
        "generated_at": "2026-10-16T06:50:00+08:00",
        "generated_by": "test",
        "price_date": "2026-10-15",
        "signals": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_bars(cache: Path, ticker: str = "0883.HK") -> None:
    rows = []
    day = date(2026, 9, 1)
    for price in [24.0] * 29 + [18.0]:
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
    write_parquet(cache_path(cache, ticker), rows)


def _account(
    home: Path,
    *,
    nlv: float | None = 1_000_000,
    account: str | None = "DUR243736",
    positions: list[dict[str, object]] | None = None,
) -> None:
    payload: dict[str, object] = {"positions": positions or []}
    if nlv is not None:
        payload["nlv"] = nlv
    if account is not None:
        payload["account"] = account
    path = home / "state" / "account.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _approved(tmp: Path) -> Path:
    dest = tmp / "approved"
    shutil.copytree(ROOT / "strategies" / "approved", dest)
    return dest


def _prepare(
    tmp: Path,
    rows: list[dict[str, object]],
    *,
    nlv: float | None = 1_000_000,
    risk_now: datetime | None = NOW,
    account: str | None = "DUR243736",
    bars: bool = True,
) -> tuple[Path, Path]:
    tmp.mkdir(parents=True, exist_ok=True)
    home = tmp / "home"
    home.mkdir()
    _write_signals(home / "signals.json", rows)
    if bars:
        _write_bars(home / "bars")
    _account(home, nlv=nlv, account=account)
    if risk_now is not None:
        write_risk_limits(home, build_risk_limits(now=risk_now, nlv=1_000_000, account="DUR243736"))
    return home, _approved(tmp)


def _run(home: Path, approved: Path, **kwargs: object) -> dict[str, object]:
    return run_session(
        trading_home=home,
        session_date=SESSION,
        signals_path=home / "signals.json",
        bars_cache=home / "bars",
        approved_dir=approved,
        now=NOW,
        refresh_bars=False,
        **kwargs,
    )


def _saved(home: Path, name: str) -> dict[str, object]:
    return json.loads((home / "intents" / SESSION / name).read_text(encoding="utf-8"))


def test_buy_with_bars_writes_a_paper_entry(tmp_path: Path) -> None:
    home, approved = _prepare(tmp_path, [_signal()])
    report = _run(home, approved)
    saved = _saved(home, "sig_pullback.v3.json")
    assert saved["mode"] == "paper"
    assert saved["live_size_multiplier"] is None
    assert saved["revision"] == 1
    assert saved["intents"]
    assert saved["intents"][0]["intent_type"] == "ENTRY"
    assert saved["intents"][0]["side"] == "BUY"
    assert saved["intents"][0]["ticker"] == "0883.HK"
    assert any(path.endswith("sig_pullback.v3.json") for path in report["written"])
    assert not list(home.rglob("*.tmp"))


def test_empty_signals_still_write_revision(tmp_path: Path) -> None:
    home, approved = _prepare(tmp_path, [], bars=False)
    _run(home, approved)
    _run(home, approved)
    saved = _saved(home, "sig_trend.v3.json")
    assert saved["intents"] == []
    assert saved["revision"] == 2


def test_low_confidence_is_rejected(tmp_path: Path) -> None:
    home, approved = _prepare(tmp_path, [_signal(confidence=57)])
    _run(home, approved)
    saved = _saved(home, "sig_pullback.v3.json")
    assert saved["intents"] == []


def test_sell_without_a_position_does_not_open_a_short(tmp_path: Path) -> None:
    home, approved = _prepare(tmp_path, [_signal(side="SELL", confidence=80)], bars=False)
    _run(home, approved)
    for path in (home / "intents").rglob("*.json"):
        for row in json.loads(path.read_text(encoding="utf-8"))["intents"]:
            assert row.get("side") != "SELL"
            assert row.get("qty", 0) >= 0
    alerts = list((home / "alerts" / "pending").glob("*signal_sell_no_position*"))
    assert alerts


def test_hash_mismatch_writes_no_file(tmp_path: Path) -> None:
    home, approved = _prepare(tmp_path, [_signal()])
    path = approved / "sig_trend.v3.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["name"] = "changed"
    path.write_text(json.dumps(document), encoding="utf-8")
    report = _run(home, approved)
    assert not (home / "intents" / SESSION / "sig_trend.v3.json").exists()
    assert any(row["reason"] == "strategy_hash_mismatch" for row in report["skipped"])
    assert list((home / "alerts" / "pending").glob("*strategy_hash_mismatch*"))
    assert (home / "intents" / SESSION / "sig_pullback.v3.json").is_file()


def test_stale_or_bad_risk_limits_block_entries(tmp_path: Path) -> None:
    home, approved = _prepare(tmp_path, [_signal()], risk_now=NOW - timedelta(hours=2))
    report = _run(home, approved)
    assert report["entries_allowed"] is False
    assert _saved(home, "sig_pullback.v3.json")["intents"] == []

    home, approved = _prepare(tmp_path / "bad", [_signal()])
    limits = home / "state" / "risk_limits.json"
    payload = json.loads(limits.read_text(encoding="utf-8"))
    payload["config_hash"] = "sha256:" + "ab" * 32
    limits.write_text(json.dumps(payload), encoding="utf-8")
    report = _run(home, approved)
    assert report["entries_allowed"] is False
    assert _saved(home, "sig_pullback.v3.json")["intents"] == []


def test_missing_nlv_refuses_entries(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    home, approved = _prepare(tmp_path, [_signal()], nlv=None)
    caplog.set_level(logging.WARNING, logger="auto_trading.strategy.session")
    report = _run(home, approved)
    assert report["entries_allowed"] is False
    assert "nlv_missing" in report["warnings"]
    assert _saved(home, "sig_pullback.v3.json")["intents"] == []
    assert any("nlv missing" in message for message in caplog.messages)


def test_other_account_is_refused(tmp_path: Path) -> None:
    home, approved = _prepare(tmp_path, [_signal()], account="DU999999")
    with pytest.raises(SessionError, match="refused"):
        _run(home, approved)
    assert not (home / "intents").exists()


def test_breakout_market_trigger_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert market_trigger_supported() is False
    home, approved = _prepare(tmp_path, [])
    caplog.set_level(logging.WARNING, logger="auto_trading.strategy.session")
    report = _run(home, approved)
    assert {
        "strategy_id": "ta_breakout52",
        "reason": "unsupported_market_trigger",
    } in report["skipped"]
    assert not list((home / "intents").glob("**/*ta_breakout52*"))
    assert any("ta_breakout52" in message and "market" in message for message in caplog.messages)
    assert list((home / "alerts" / "pending").glob("*unsupported_market_trigger*"))


def test_allocator_keeps_the_higher_priority_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, approved = _prepare(tmp_path, [_signal(confidence=80, fair=40, price=20)])

    def fake(strategy, bars, signals, session_date, **kwargs):
        from auto_trading.backtest.engine import _Order
        from auto_trading.strategy.conditions import UnknownCondition

        if strategy.get("trigger", {}).get("type") == "market":
            raise UnknownCondition("market")
        if strategy.get("id") not in {"sig_trend", "sig_pullback"}:
            return []
        return [
            _Order(
                "0883.HK",
                "BUY",
                1000,
                "ENTRY",
                f"{session_date}-{strategy['id']}",
                None,
                session_date,
                ref_price=20.0,
                confidence=80,
                fair_value=40.0,
                signal_id="sig",
            )
        ]

    monkeypatch.setattr("auto_trading.strategy.session.plan_session", fake)
    _run(home, approved)
    trend = _saved(home, "sig_trend.v3.json")
    pullback = _saved(home, "sig_pullback.v3.json")
    assert [row["ticker"] for row in trend["intents"]] == ["0883.HK"]
    assert pullback["intents"] == []


def test_killed_replace_does_not_publish_a_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(src: Path, dst: Path) -> None:
        raise OSError("killed")

    monkeypatch.setattr("auto_trading.intents.writer.os.replace", boom)
    from auto_trading.intents.writer import write_intents

    document = {
        "strategy_id": "sig_pullback",
        "strategy_version": 3,
        "spec_hash": "sha256:" + "11" * 32,
        "mode": "paper",
        "base_currency": "HKD",
        "generated_at": "2026-10-16T07:05:00+08:00",
        "intents": [],
    }
    with pytest.raises(OSError, match="killed"):
        write_intents(tmp_path, document, session_date=SESSION, entries_allowed=True)
    assert list(tmp_path.rglob("*.json")) == []
    assert list(tmp_path.rglob("*.tmp"))
    assert load_intent_orders(tmp_path) == []


def test_cli_and_once_script_share_the_session(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _write_signals(home / "signals.json", [])
    from auto_trading.cli import main

    main(
        [
            "run-session",
            "--date",
            SESSION,
            "--trading-home",
            str(home),
            "--signals",
            str(home / "signals.json"),
            "--bars-cache",
            str(home / "bars"),
        ]
    )
    saved = _saved(home, "sig_pullback.v3.json")
    assert saved["intents"] == []
    assert saved["mode"] == "paper"
    assert saved["live_size_multiplier"] is None
    other = tmp_path / "other"
    other.mkdir()
    _write_signals(other / "signals.json", [])
    module = _load_once()
    code = module.main(
        [
            "--trading-home",
            str(other),
            "--signals",
            str(other / "signals.json"),
            "--bars-cache",
            str(other / "bars"),
            "--session-date",
            SESSION,
        ]
    )
    assert code == 0
    assert _saved(other, "sig_pullback.v3.json")["revision"] == 1


def test_units_and_sync_keep_one_vps_runner() -> None:
    deploy = ROOT / "vps" / "deploy"
    assert sorted(path.name for path in deploy.glob("*strategy*.timer")) == [
        "trading-strategy.timer"
    ]
    assert sorted(path.name for path in deploy.glob("*strategy*.service")) == [
        "trading-strategy.service"
    ]
    service = (deploy / "trading-strategy.service").read_text(encoding="utf-8")
    timer = (deploy / "trading-strategy.timer").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "run_paper_intents_daily.sh").read_text(encoding="utf-8")
    assert "run_paper_intents_daily.sh" in service
    assert "Restart=always" not in service
    assert "23:05:00 UTC" in timer
    assert "Persistent=true" in timer
    assert "run-session" in script
    assert "systemctl" not in script
    box_lines = _manifest_lines(deploy / "sync_box_to_vps.txt")
    vps_lines = _manifest_lines(deploy / "sync_vps_to_box.txt")
    assert "intents/" not in box_lines
    assert "intents/" in vps_lines


def test_inbound_sync_refuses_intents(tmp_path: Path) -> None:
    deploy = tmp_path / "vps" / "deploy"
    deploy.mkdir(parents=True)
    (deploy / "sync_box_to_vps.txt").write_text("signals.json\nintents/\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="intents"):
        sync_argv(tmp_path, "box.example:/srv/trading", direction="in")


def _manifest_lines(path: Path) -> list[str]:
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            rows.append(line)
    return rows


def _load_once():
    spec = importlib.util.spec_from_file_location(
        "run_paper_intents_once",
        ROOT / "scripts" / "run_paper_intents_once.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
