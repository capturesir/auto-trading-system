"""Missing quote FX must not KeyError the paper session.

Sizing fills a gap from Yahoo. A public-quote failure skips that symbol.
The cash gate still reads the broker snapshot unchanged.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from auto_trading.alerts.holdings_state import atomic_write_json
from auto_trading.backtest.engine import Bar, BtSignal, _fx, plan_session
from auto_trading.execution.contracts import ib_contract_fields
from auto_trading.execution.fills import read_execution_snapshot, write_execution_snapshot
from auto_trading.execution.fx import fill_missing_rates, load_rates
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.execution.services import account_once
from auto_trading.market.bars import write_parquet
from auto_trading.market.instruments import describe
from auto_trading.market.yahoo import cache_path
from auto_trading.strategy.session import run_session

ROOT = Path(__file__).resolve().parents[1]
MACAU = ZoneInfo("Asia/Macau")
NOW = datetime(2026, 10, 16, 7, 5, tzinfo=MACAU)
SESSION = "2026-10-16"
PIN = "DUR243736"
PARTIAL = {"HKD": 1.0, "USD": 7.8, "JPY": 0.052, "KRW": 0.0056}


def _chart(price: float) -> str:
    return json.dumps(
        {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "regularMarketPrice": price,
                            "regularMarketTime": 1_760_000_000,
                        }
                    }
                ]
            }
        }
    )


def _signal(ticker: str, market: str, currency: str) -> dict[str, object]:
    return {
        "signal_id": f"20261016-{ticker}-BUY",
        "ticker": ticker,
        "market": market,
        "name": "sample",
        "asset_type": "equity",
        "currency": currency,
        "side": "BUY",
        "confidence": 70,
        "fair_value": 30.0,
        "market_price": 18.0,
        "price_date": "2026-10-15",
        "upside_pct": (30.0 / 18.0 - 1) * 100,
        "horizon": "6-12m",
        "rationale": "fixture",
        "report_path": "/tmp/report.md",
        "expires_at": "2026-10-20T06:30:00+08:00",
    }


def _bars(cache: Path, ticker: str) -> None:
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


def _home(tmp: Path) -> tuple[Path, Path]:
    home = tmp / "home"
    (home / "state").mkdir(parents=True)
    signals = {
        "schema_version": "1.0",
        "generated_at": "2026-10-16T06:50:00+08:00",
        "generated_by": "test",
        "price_date": "2026-10-15",
        "signals": [
            _signal("0883.HK", "HK", "HKD"),
            _signal("2330.TW", "TW", "TWD"),
        ],
    }
    (home / "signals.json").write_text(json.dumps(signals), encoding="utf-8")
    cache = home / "bars"
    _bars(cache, "0883.HK")
    _bars(cache, "2330.TW")
    account = {
        "account": PIN,
        "nlv": 1_000_000,
        "positions": [],
        "fx_to_base": dict(PARTIAL),
    }
    (home / "state" / "account.json").write_text(json.dumps(account), encoding="utf-8")
    write_risk_limits(home, build_risk_limits(now=NOW, nlv=1_000_000, account=PIN))
    approved = tmp / "approved"
    approved.mkdir()
    for path in (ROOT / "strategies" / "approved").glob("*.json"):
        (approved / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return home, approved


def _tickers(home: Path) -> list[str]:
    found: list[str] = []
    folder = home / "intents"
    if not folder.is_dir():
        return found
    for path in folder.rglob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload.get("intents") or []:
            found.append(str(row.get("ticker")))
    return found


def _quotes(url: str) -> str:
    if "TWD=X" in url:
        return _chart(32.0)
    raise AssertionError(url)


def test_load_rates_leaves_a_partial_snapshot_unchanged(tmp_path: Path) -> None:
    rates = load_rates(tmp_path, {"fx_to_base": dict(PARTIAL)})
    assert "TWD" not in rates
    assert rates["USD"] == pytest.approx(7.8)
    assert rates["HKD"] == pytest.approx(1.0)


def test_public_fx_fills_twd_from_yahoo_without_replacing_snapshot_rates() -> None:
    rates = fill_missing_rates(PARTIAL, ["TWD", "HKD", "USD"], getter=_quotes)
    assert rates["TWD"] == pytest.approx(7.8 / 32.0)
    assert rates["USD"] == pytest.approx(7.8)
    assert rates["KRW"] == pytest.approx(0.0056)
    assert "TWD" not in PARTIAL


def test_public_fx_converts_jpy_and_krw_and_fetches_a_missing_usd() -> None:
    def getter(url: str) -> str:
        if "HKD=X" in url:
            return _chart(7.8)
        if "JPY=X" in url:
            return _chart(150.0)
        if "KRW=X" in url:
            return _chart(1400.0)
        raise AssertionError(url)

    rates = fill_missing_rates({"HKD": 1.0}, ["JPY", "KRW", "USD"], getter=getter)
    assert rates["USD"] == pytest.approx(7.8)
    assert rates["JPY"] == pytest.approx(7.8 / 150.0)
    assert rates["KRW"] == pytest.approx(7.8 / 1400.0)


def test_public_fx_failure_leaves_the_currency_missing() -> None:
    def boom(_url: str) -> str:
        raise TimeoutError("yahoo down")

    rates = fill_missing_rates(PARTIAL, ["TWD"], getter=boom)
    assert "TWD" not in rates
    assert rates["USD"] == pytest.approx(7.8)


def test_hkd_gap_is_identity_and_does_not_call_yahoo() -> None:
    def boom(_url: str) -> str:
        raise AssertionError("yahoo")

    rates = fill_missing_rates({"USD": 7.8}, ["HKD"], getter=boom)
    assert rates["HKD"] == pytest.approx(1.0)


def test_fx_lookup_returns_none_instead_of_keyerror() -> None:
    partial = {"HKD": 1.0, "USD": 7.8}
    assert _fx("TWD", partial, "HKD") is None
    assert _fx("JPY", partial, "HKD") is None
    assert _fx("KRW", partial, "HKD") is None
    assert _fx("USD", {"HKD": 1.0}, "HKD") is None
    assert _fx("HKD", {}, "HKD") == pytest.approx(1.0)
    assert _fx("USD", {}, "USD") == pytest.approx(1.0)
    assert _fx("JPY", {"JPY": 0.052, "USD": 7.8}, "HKD") == pytest.approx(0.052)


def test_plan_session_skips_missing_twd_and_keeps_other_symbols(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    day = "2026-10-16"
    strategy = {
        "id": "sig_pullback",
        "trigger": {"type": "signal", "min_confidence": 60},
        "action": {"size": {"type": "pct_equity", "value": 5.0}},
    }
    bars = {
        "2330.TW": [Bar("2026-10-15", 18, 18, 18, 18)],
        "0883.HK": [Bar("2026-10-15", 18, 18, 18, 18)],
        "7203.T": [Bar("2026-10-15", 100, 100, 100, 100)],
    }
    signals = [
        BtSignal("2330.TW", "BUY", 80, 30, "tw", "2026-10-15"),
        BtSignal("0883.HK", "BUY", 80, 30, "hk", "2026-10-15"),
        BtSignal("7203.T", "BUY", 80, 200, "jp", "2026-10-15"),
    ]
    orders = plan_session(
        strategy,
        bars,
        signals,
        day,
        nlv=1_000_000,
        rates_hkd_per_unit={"HKD": 1.0, "USD": 7.8, "KRW": 0.0056},
    )
    tickers = [order.ticker for order in orders]
    assert "0883.HK" in tickers
    assert "2330.TW" not in tickers
    assert "7203.T" not in tickers
    assert "2330.TW" in caplog.text
    assert "missing_fx" in caplog.text


def test_missing_twd_snapshot_still_writes_other_intents(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    home, approved = _home(tmp_path)
    report = run_session(
        trading_home=home,
        session_date=SESSION,
        signals_path=home / "signals.json",
        bars_cache=home / "bars",
        approved_dir=approved,
        now=NOW,
        refresh_bars=False,
        fx_getter=_quotes,
    )
    tickers = _tickers(home)
    assert "0883.HK" in tickers
    assert "2330.TW" in tickers
    assert report["written"]
    assert "fx_fallback=yahoo" in caplog.text
    assert "TWD" in caplog.text
    saved = json.loads((home / "state" / "account.json").read_text(encoding="utf-8"))
    assert "TWD" not in saved["fx_to_base"]


def test_public_fx_failure_skips_tw_without_failing_the_session(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)

    def boom(_url: str) -> str:
        raise TimeoutError("yahoo down")

    home, approved = _home(tmp_path)
    report = run_session(
        trading_home=home,
        session_date=SESSION,
        signals_path=home / "signals.json",
        bars_cache=home / "bars",
        approved_dir=approved,
        now=NOW,
        refresh_bars=False,
        fx_getter=boom,
    )
    tickers = _tickers(home)
    assert "0883.HK" in tickers
    assert "2330.TW" not in tickers
    assert any(path.endswith("sig_pullback.v3.json") for path in report["written"])
    assert "fx_fallback=skip" in caplog.text
    assert "2330.TW" in caplog.text
    assert "KeyError" not in caplog.text


def test_bare_korean_code_quotes_krw_on_krx() -> None:
    fields = ib_contract_fields("005930")
    assert fields == {
        "symbol": "005930",
        "secType": "STK",
        "exchange": "KRX",
        "currency": "KRW",
    }
    assert ib_contract_fields("005930.KS")["currency"] == "KRW"
    assert ib_contract_fields("005930.KS")["exchange"] == "KRX"
    spec = describe("005930")
    assert spec.market == "KR"
    assert spec.currency == "KRW"
    assert spec.ib_exchange == "KRX"
    us = ib_contract_fields("AAPL")
    assert us["exchange"] == "SMART"
    assert us["currency"] == "USD"


def test_atomic_write_retries_a_missing_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    real = os.replace

    def flaky(src: str | Path, dst: str | Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            Path(src).unlink(missing_ok=True)
            raise FileNotFoundError(src)
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    target = tmp_path / "state" / "executions.json"
    atomic_write_json(target, {"executions": [{"exec_id": "kept"}]})
    assert calls["n"] == 2
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["executions"][0]["exec_id"] == "kept"
    assert not list(tmp_path.rglob("*.tmp"))


def test_parallel_execution_snapshots_do_not_raise(tmp_path: Path) -> None:
    when = datetime(2026, 10, 28, 5, 10, tzinfo=UTC)
    errors: list[BaseException] = []

    def once(index: int) -> None:
        try:
            write_execution_snapshot(tmp_path, when, [{"exec_id": f"e{index}"}])
        except BaseException as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(once, range(24)))
    assert errors == []
    rows = read_execution_snapshot(tmp_path)
    assert len(rows) == 1
    assert str(rows[0]["exec_id"]).startswith("e")
    assert not list(tmp_path.rglob("*.tmp"))


def test_execution_snapshot_race_does_not_fail_the_account_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("executions.json.tmp")

    monkeypatch.setattr("auto_trading.alerts.holdings_state.atomic_write_json", boom)
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    result = account_once(
        tmp_path,
        NOW,
        snapshot={
            "accounts": [PIN],
            "nlv": 1_000_000.0,
            "positions": [],
            "open_orders": [],
            "executions": [{"exec_id": "race"}],
        },
    )
    assert result["status"] == "wrote"
    assert result["wrote"] is True
    account = json.loads((tmp_path / "state" / "account.json").read_text(encoding="utf-8"))
    assert account["nlv"] == pytest.approx(1_000_000.0)
    assert (tmp_path / "state" / "gateway.json").is_file()
