"""Benchmark comparison on the daily summary and the monthly review."""

from __future__ import annotations

import ast
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.execution.breakers import flags
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.reports.benchmarks import (
    BenchmarkFetchError,
    ManualClock,
    build_benchmarks,
    et_close,
    load_benchmarks,
)
from auto_trading.reports.daily_summary import validate_daily_summary, write_daily_summary
from auto_trading.risk.gate import GateContext, RiskGate, RiskRejected
from auto_trading.risk.limits import EXPECTED_LIMITS, HASH_ODD_LOT_OFF, config_hash

HKT = timezone(timedelta(hours=8))
NY = ZoneInfo("America/New_York")
MACAU = ZoneInfo("Asia/Macau")
ROOT = Path(__file__).resolve().parents[1]


def _yahoo(rows: list[tuple[str, float]]) -> str:
    stamps = []
    prices = []
    for day, price in rows:
        year, month, dom = (int(part) for part in day.split("-"))
        moment = datetime(year, month, dom, 16, 0, tzinfo=NY)
        stamps.append(int(moment.timestamp()))
        prices.append(price)
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": stamps,
                    "indicators": {"adjclose": [{"adjclose": prices}]},
                }
            ]
        }
    }
    return json.dumps(payload)


def _stooq(rows: list[tuple[str, float]]) -> str:
    lines = ["Date,Open,High,Low,Close,Volume"]
    for day, price in rows:
        lines.append(f"{day},1,1,1,{price},1")
    return "\n".join(lines) + "\n"


def _coinbase(rows: list[tuple[str, float]], *, decoy: set[str] | None = None) -> str:
    candles = []
    marked = decoy or set()
    for day, price in rows:
        year, month, dom = (int(part) for part in day.split("-"))
        exact = datetime(year, month, dom, 15, 0, tzinfo=NY)
        candles.append([int(exact.timestamp()), price, price, price, price, 1])
        if day in marked:
            early = datetime(year, month, dom, 14, 0, tzinfo=NY)
            late = datetime(year, month, dom, 16, 0, tzinfo=NY)
            candles.append([int(early.timestamp()), 1, 1, 1, 1, 1])
            candles.append([int(late.timestamp()), 2, 2, 2, 2, 1])
    return json.dumps(candles)


class _Stub:
    def __init__(
        self,
        routes: list[tuple[str, object]],
        delays: dict[str, float] | None = None,
    ) -> None:
        self.routes = routes
        self.delays = delays or {}
        self.clock = ManualClock()
        self.timeouts: list[float] = []

    def __call__(self, url: str, timeout: float) -> str:
        self.timeouts.append(timeout)
        delay = 0.0
        for key, value in self.delays.items():
            if key in url:
                delay = value
                break
        self.clock.advance(min(delay, timeout))
        if delay > timeout:
            raise TimeoutError
        for key, body in self.routes:
            if key in url:
                if body == "error":
                    raise BenchmarkFetchError("fetch_error")
                if body == "bad":
                    return "not-json"
                return str(body)
        raise BenchmarkFetchError("fetch_error")


def _start(home: Path, day: str, nlv: float = 1_000_000.0) -> None:
    folder = home / "state"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "paper_start.json").write_text(
        json.dumps({"paper_start_date": day, "start_nlv": nlv}),
        encoding="utf-8",
    )


def _account(home: Path, nlv: float, day_start: float) -> None:
    folder = home / "state"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "account.json").write_text(
        json.dumps({"nlv": nlv, "hwm": nlv, "day_start_nlv": day_start, "positions": []}),
        encoding="utf-8",
    )


def _summary(home: Path, moment: datetime, stub: _Stub, nlv: float, day_start: float) -> dict:
    _account(home, nlv, day_start)
    return write_daily_summary(
        home,
        now=moment,
        complete=True,
        pin="DUR243736",
        benchmark_get=stub,
        benchmark_clock=stub.clock,
    )


def _flat(days: tuple[str, ...]) -> list[tuple[str, float]]:
    return [(day, 1.0) for day in days]


def _item(block: dict, benchmark_id: str) -> dict:
    return next(row for row in block["items"] if row["id"] == benchmark_id)


def _happy_routes() -> list[tuple[str, object]]:
    spy = [
        ("2026-10-02", 100.0),
        ("2026-10-05", 110.0),
        ("2026-10-06", 99.0),
        ("2026-10-07", 105.0),
    ]
    qqq = [
        ("2026-10-02", 200.0),
        ("2026-10-05", 210.0),
        ("2026-10-06", 220.0),
        ("2026-10-07", 198.0),
    ]
    brk = [
        ("2026-10-02", 400.0),
        ("2026-10-05", 400.0),
        ("2026-10-06", 420.0),
        ("2026-10-07", 410.0),
    ]
    btc = [
        ("2026-10-04", 50000.0),
        ("2026-10-05", 51000.0),
        ("2026-10-06", 49000.0),
        ("2026-10-07", 52000.0),
    ]
    eth = [
        ("2026-10-04", 2000.0),
        ("2026-10-05", 2100.0),
        ("2026-10-06", 2200.0),
        ("2026-10-07", 1900.0),
    ]
    return [
        ("/SPY?", _yahoo(spy)),
        ("/QQQ?", _yahoo(qqq)),
        ("/BRK-B?", _yahoo(brk)),
        ("BTC-USD", _coinbase(btc)),
        ("ETH-USD", _coinbase(eth)),
    ]


def test_t27a_returns_match_the_hand_table(tmp_path: Path) -> None:
    _start(tmp_path, "2026-10-05")
    stub = _Stub(_happy_routes())
    _summary(tmp_path, datetime(2026, 10, 6, 6, 0, tzinfo=HKT), stub, 1_010_000, 1_000_000)
    _summary(tmp_path, datetime(2026, 10, 7, 6, 0, tzinfo=HKT), stub, 990_000, 1_010_000)
    summary = _summary(tmp_path, datetime(2026, 10, 8, 6, 0, tzinfo=HKT), stub, 1_020_000, 990_000)
    block = summary["benchmarks"]
    paper_day = 1_020_000 / 990_000 - 1
    assert block["paper"]["day_return"] == pytest.approx(paper_day, abs=1e-9)
    assert block["paper"]["since_start_return"] == pytest.approx(0.02, abs=1e-9)
    assert block["paper"]["max_drawdown"] == pytest.approx(990_000 / 1_010_000 - 1, abs=1e-9)
    spy = _item(block, "SPY")
    assert spy["source"] == "yahoo"
    assert spy["price_basis"] == "adj_close"
    assert spy["status"] == "ok"
    assert spy["day_return"] == pytest.approx(105 / 99 - 1, abs=1e-9)
    assert spy["since_start_return"] == pytest.approx(0.05, abs=1e-9)
    assert spy["max_drawdown"] == pytest.approx(-0.1, abs=1e-9)
    assert spy["excess_day"] == pytest.approx(paper_day - (105 / 99 - 1), abs=1e-9)
    assert spy["excess_since_start"] == pytest.approx(0.02 - 0.05, abs=1e-9)
    qqq = _item(block, "QQQ")
    assert qqq["day_return"] == pytest.approx(198 / 220 - 1, abs=1e-9)
    assert qqq["since_start_return"] == pytest.approx(-0.01, abs=1e-9)
    assert qqq["max_drawdown"] == pytest.approx(-0.1, abs=1e-9)
    brk = _item(block, "BRK.B")
    assert brk["day_return"] == pytest.approx(410 / 420 - 1, abs=1e-9)
    assert brk["since_start_return"] == pytest.approx(0.025, abs=1e-9)
    assert brk["max_drawdown"] == pytest.approx(410 / 420 - 1, abs=1e-9)
    btc = _item(block, "BTC")
    assert btc["source"] == "coinbase"
    assert btc["price_basis"] == "snapshot_1600_ET"
    assert btc["day_return"] == pytest.approx(52000 / 49000 - 1, abs=1e-9)
    assert btc["since_start_return"] == pytest.approx(0.04, abs=1e-9)
    assert btc["max_drawdown"] == pytest.approx(49000 / 51000 - 1, abs=1e-9)
    eth = _item(block, "ETH")
    assert eth["day_return"] == pytest.approx(1900 / 2200 - 1, abs=1e-9)
    assert eth["since_start_return"] == pytest.approx(-0.05, abs=1e-9)
    assert eth["max_drawdown"] == pytest.approx(1900 / 2200 - 1, abs=1e-9)


def test_t27b_crypto_uses_the_1600_et_candle_across_dst(tmp_path: Path) -> None:
    summer = et_close(datetime(2026, 10, 30).date()).astimezone(MACAU)
    winter = et_close(datetime(2026, 11, 3).date()).astimezone(MACAU)
    assert summer.hour == 4
    assert winter.hour == 5
    rows = [(f"2026-10-{day:02d}", 100.0) for day in range(29, 32)]
    rows += [("2026-11-01", 100.0), ("2026-11-02", 100.0), ("2026-11-03", 100.0)]
    special = {day: price for day, price in rows}
    special["2026-10-30"] = 111.0
    special["2026-11-03"] = 222.0
    ordered = list(special.items())
    stub = _Stub(
        [
            ("BTC-USD", _coinbase(ordered, decoy={"2026-10-30", "2026-11-03"})),
            ("ETH-USD", _coinbase(ordered, decoy={"2026-10-30", "2026-11-03"})),
            ("/SPY?", _yahoo(_flat(("2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03")))),
            ("/QQQ?", _yahoo(_flat(("2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03")))),
            ("/BRK-B?", _yahoo(_flat(("2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03")))),
        ]
    )
    _start(tmp_path, "2026-10-30", nlv=1.0)
    first = _summary(tmp_path, datetime(2026, 10, 31, 6, 0, tzinfo=HKT), stub, 1.0, 1.0)
    second = _summary(tmp_path, datetime(2026, 11, 4, 6, 0, tzinfo=HKT), stub, 1.0, 1.0)
    assert _item(first["benchmarks"], "BTC")["day_return"] == pytest.approx(111 / 100 - 1, abs=1e-9)
    assert _item(second["benchmarks"], "BTC")["as_of"] == "2026-11-03"
    stored = (tmp_path / "data" / "benchmarks" / "BTC.csv").read_text(encoding="utf-8")
    assert "2026-10-30,111.0" in stored or "2026-10-30,111" in stored
    assert "2026-11-03,222.0" in stored or "2026-11-03,222" in stored


def test_t27c_us_closed_days_leave_equities_flat_and_crypto_moves(tmp_path: Path) -> None:
    equity = [("2026-10-02", 100.0), ("2026-10-09", 130.0)]
    crypto = [("2026-10-04", 50.0)] + [(f"2026-10-{day:02d}", 50.0) for day in range(5, 10)]
    crypto.append(("2026-10-10", 60.0))
    stub = _Stub(
        [
            ("/SPY?", _yahoo(equity)),
            ("/QQQ?", _yahoo(equity)),
            ("/BRK-B?", _yahoo(equity)),
            ("BTC-USD", _coinbase(crypto)),
            ("ETH-USD", _coinbase(crypto)),
        ]
    )
    _start(tmp_path, "2026-10-05")
    weekend = _summary(
        tmp_path,
        datetime(2026, 10, 11, 6, 0, tzinfo=HKT),
        stub,
        1_000_000,
        1_000_000,
    )
    spy = _item(weekend["benchmarks"], "SPY")
    assert spy["status"] == "closed"
    assert spy["day_return"] is None
    assert spy["na_reason"] == "us_market_closed"
    assert spy["since_start_return"] == pytest.approx(0.3, abs=1e-9)
    btc = _item(weekend["benchmarks"], "BTC")
    assert btc["status"] == "ok"
    assert btc["day_return"] == pytest.approx(0.2, abs=1e-9)

    holiday = tmp_path / "holiday"
    holiday_equity = [("2026-11-24", 100.0), ("2026-11-25", 110.0)]
    holiday_crypto = [("2026-11-24", 10.0), ("2026-11-25", 10.0), ("2026-11-26", 12.0)]
    holiday_stub = _Stub(
        [
            ("/SPY?", _yahoo(holiday_equity)),
            ("/QQQ?", _yahoo(holiday_equity)),
            ("/BRK-B?", _yahoo(holiday_equity)),
            ("BTC-USD", _coinbase(holiday_crypto)),
            ("ETH-USD", _coinbase(holiday_crypto)),
        ]
    )
    _start(holiday, "2026-11-25")
    closed = _summary(holiday, datetime(2026, 11, 27, 6, 0, tzinfo=HKT), holiday_stub, 1.0, 1.0)
    brk = _item(closed["benchmarks"], "BRK.B")
    assert brk["status"] == "closed"
    assert brk["day_return"] is None
    assert brk["since_start_return"] == pytest.approx(0.1, abs=1e-9)
    assert _item(closed["benchmarks"], "ETH")["status"] == "ok"
    assert _item(closed["benchmarks"], "ETH")["day_return"] == pytest.approx(0.2, abs=1e-9)


def test_t27d_primary_failure_uses_the_backup_source(tmp_path: Path) -> None:
    rows = [("2026-10-02", 10.0), ("2026-10-05", 11.0)]
    others = [("2026-10-02", 1.0), ("2026-10-05", 1.0)]
    crypto = [("2026-10-04", 1.0), ("2026-10-05", 1.0)]
    stub = _Stub(
        [
            ("/SPY?", "error"),
            ("spy.us", _stooq(rows)),
            ("/QQQ?", "bad"),
            ("qqq.us", _stooq(rows)),
            ("/BRK-B?", _yahoo(others)),
            ("BTC-USD", _coinbase(crypto)),
            ("ETH-USD", _coinbase(crypto)),
        ]
    )
    _start(tmp_path, "2026-10-05")
    summary = _summary(tmp_path, datetime(2026, 10, 6, 6, 0, tzinfo=HKT), stub, 1.0, 1.0)
    spy = _item(summary["benchmarks"], "SPY")
    qqq = _item(summary["benchmarks"], "QQQ")
    assert spy["source"] == "stooq"
    assert spy["price_basis"] == "close"
    assert spy["day_return"] == pytest.approx(0.1, abs=1e-9)
    assert qqq["source"] == "stooq"
    assert qqq["price_basis"] == "close"


def test_t27e_failures_are_na_without_blocking_other_benchmarks(tmp_path: Path) -> None:
    stale = [("2026-10-02", 100.0)]
    no_base = [("2026-10-05", 110.0), ("2026-10-06", 110.0), ("2026-10-07", 110.0)]
    crypto = [
        ("2026-10-04", 10.0),
        ("2026-10-05", 10.0),
        ("2026-10-06", 10.0),
        ("2026-10-07", 11.0),
    ]
    stub = _Stub(
        [
            ("/SPY?", "error"),
            ("spy.us", "error"),
            ("/QQQ?", _yahoo(stale)),
            ("/BRK-B?", _yahoo(no_base)),
            ("BTC-USD", _coinbase(crypto)),
            ("ETH-USD", _coinbase(crypto)),
        ]
    )
    _start(tmp_path, "2026-10-05")
    summary = _summary(
        tmp_path,
        datetime(2026, 10, 8, 6, 0, tzinfo=HKT),
        stub,
        1_000_000,
        1_000_000,
    )
    assert summary["status"] == "final"
    assert _item(summary["benchmarks"], "SPY")["na_reason"] == "fetch_error"
    assert _item(summary["benchmarks"], "QQQ")["na_reason"] == "stale"
    assert _item(summary["benchmarks"], "BRK.B")["na_reason"] == "missing_base"
    assert _item(summary["benchmarks"], "BTC")["status"] == "ok"
    assert _item(summary["benchmarks"], "ETH")["day_return"] == pytest.approx(0.1, abs=1e-9)
    validate_daily_summary(summary)


def test_t27f_request_and_round_timeouts_still_write_the_summary(tmp_path: Path) -> None:
    rows = [("2026-10-02", 1.0), ("2026-10-05", 1.0)]
    crypto = [("2026-10-04", 1.0), ("2026-10-05", 1.0)]
    slow = _Stub(
        [
            ("/SPY?", _yahoo(rows)),
            ("spy.us", _yahoo(rows)),
            ("/QQQ?", _yahoo(rows)),
            ("/BRK-B?", _yahoo(rows)),
            ("BTC-USD", _coinbase(crypto)),
            ("ETH-USD", _coinbase(crypto)),
        ],
        delays={"/SPY?": 15.0, "spy.us": 15.0},
    )
    _start(tmp_path, "2026-10-05")
    started = time.perf_counter()
    summary = _summary(tmp_path, datetime(2026, 10, 6, 6, 0, tzinfo=HKT), slow, 1.0, 1.0)
    assert time.perf_counter() - started < 120
    assert max(slow.timeouts) <= 10
    assert _item(summary["benchmarks"], "SPY")["na_reason"] == "fetch_timeout"
    assert _item(summary["benchmarks"], "QQQ")["status"] == "ok"

    hung = tmp_path / "hung"
    delays = {
        "/SPY?": 9.0,
        "spy.us": 9.0,
        "/QQQ?": 9.0,
        "qqq.us": 9.0,
        "/BRK-B?": 9.0,
        "brk-b.us": 9.0,
        "BTC-USD": 9.0,
        "XBTUSD": 9.0,
        "bitcoin": 9.0,
        "ETH-USD": 9.0,
        "ETHUSD": 9.0,
        "ethereum": 9.0,
    }
    round_stub = _Stub([], delays=delays)
    _start(hung, "2026-10-05")
    started = time.perf_counter()
    later = _summary(hung, datetime(2026, 10, 6, 6, 0, tzinfo=HKT), round_stub, 1.0, 1.0)
    assert time.perf_counter() - started < 120
    reasons = {row["id"]: row["na_reason"] for row in later["benchmarks"]["items"]}
    assert reasons["SPY"] == "fetch_error"
    assert reasons["BTC"] == "fetch_timeout"
    assert reasons["ETH"] == "fetch_timeout"
    assert later["generated_at"].startswith("2026-10-06T06:00:00")


def test_t27g_na_does_not_raise_critical_or_r30(tmp_path: Path) -> None:
    journal = TradeJournal(tmp_path)
    before = journal.anomalies_for("BTC")
    stub = _Stub([("/SPY?", "error"), ("spy.us", "error"), ("/QQQ?", "error"), ("qqq.us", "error")])
    _start(tmp_path, "2026-10-05")
    summary = _summary(tmp_path, datetime(2026, 10, 6, 6, 0, tzinfo=HKT), stub, 1.0, 1.0)
    assert summary["benchmarks"]["items"]
    assert flags(tmp_path).get("r30_active") is not True
    assert list((tmp_path / "logs").glob("critical_*.jsonl")) == []
    assert list(tmp_path.glob("alerts/**/*exec_critical*")) == []
    assert journal.anomalies_for("BTC") == before
    events = (tmp_path / "logs" / "events_20261006.jsonl").read_text(encoding="utf-8")
    assert "benchmark_na" in events
    assert '"level": "info"' in events


def test_t27h_recovered_data_bumps_the_revision(tmp_path: Path) -> None:
    rows = [("2026-10-02", 100.0), ("2026-10-05", 110.0)]
    crypto = [("2026-10-04", 1.0), ("2026-10-05", 1.0)]
    failed = _Stub(
        [
            ("/SPY?", "error"),
            ("spy.us", "error"),
            ("/QQQ?", _yahoo(rows)),
            ("/BRK-B?", _yahoo(rows)),
            ("BTC-USD", _coinbase(crypto)),
            ("ETH-USD", _coinbase(crypto)),
        ]
    )
    _start(tmp_path, "2026-10-05")
    moment = datetime(2026, 10, 6, 6, 0, tzinfo=HKT)
    first = _summary(tmp_path, moment, failed, 1.0, 1.0)
    assert first["revision"] == 1
    assert _item(first["benchmarks"], "SPY")["status"] == "n/a"
    recovered = _Stub(
        [
            ("/SPY?", _yahoo(rows)),
            ("/QQQ?", _yahoo(rows)),
            ("/BRK-B?", _yahoo(rows)),
            ("BTC-USD", _coinbase(crypto)),
            ("ETH-USD", _coinbase(crypto)),
        ]
    )
    second = _summary(tmp_path, moment, recovered, 1.0, 1.0)
    assert second["revision"] == 2
    spy = _item(second["benchmarks"], "SPY")
    assert spy["status"] == "ok"
    assert spy["day_return"] == pytest.approx(0.1, abs=1e-9)


def test_t27i_before_e2_benchmarks_are_null(tmp_path: Path) -> None:
    summary = write_daily_summary(
        tmp_path,
        now=datetime(2026, 10, 6, 6, 0, tzinfo=HKT),
        complete=True,
        pin="DUR243736",
    )
    assert summary["benchmarks"] is None
    events = (tmp_path / "logs" / "events_20261006.jsonl").read_text(encoding="utf-8")
    assert "not_started" in events
    assert build_benchmarks(tmp_path, datetime(2026, 10, 6, 6, tzinfo=HKT), {}) is None


def test_t27j_benchmarks_stay_out_of_config_hash_and_btc_is_r26(tmp_path: Path) -> None:
    before = config_hash(EXPECTED_LIMITS)
    folder = tmp_path / "config"
    folder.mkdir()
    changed = json.loads((ROOT / "config" / "benchmarks.json").read_text(encoding="utf-8"))
    changed["items"][0]["id"] = "SPX"
    (folder / "benchmarks.json").write_text(json.dumps(changed), encoding="utf-8")
    assert load_benchmarks(tmp_path)["items"][0]["id"] == "SPX"
    assert config_hash(EXPECTED_LIMITS) == before == HASH_ODD_LOT_OFF
    built = build_risk_limits(now=datetime(2026, 10, 6, 6, tzinfo=HKT), nlv=1_000_000)
    assert built["config_hash"] == before
    assert "benchmarks" not in built["limits"]
    tree = ast.parse((ROOT / "src" / "auto_trading" / "reports" / "benchmarks.py").read_text())
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    joined = " ".join(modules)
    assert "broker" not in joined
    assert "ibkr" not in joined
    assert "ib_async" not in joined
    write_risk_limits(
        tmp_path,
        build_risk_limits(now=datetime(2026, 10, 2, 22, 0, tzinfo=HKT), nlv=1_000_000),
    )
    ctx = GateContext(
        now=datetime(2026, 10, 2, 22, 0, tzinfo=HKT),
        trading_home=tmp_path,
        accounts=("DU1234567",),
        expected_account="DU1234567",
    )
    for symbol in ("BTC", "ETH", "BTC-USD"):
        with pytest.raises(RiskRejected, match="R26"):
            RiskGate(ctx).allow(
                OrderIntent(client_id=symbol, symbol=symbol, side=OrderSide.BUY, quantity=1)
            )
    from auto_trading.risk.gate import evaluate

    equity = OrderIntent(
        client_id="aapl",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=1,
        limit_price=100,
    )
    assert evaluate(equity, ctx) != "R26"
    assert (
        evaluate(
            OrderIntent(client_id="brk2", symbol="BRK.B", side=OrderSide.BUY, quantity=1),
            ctx,
        )
        != "R26"
    )


def test_t27k_daily_schema_and_monthly_review(tmp_path: Path) -> None:
    _start(tmp_path, "2026-10-05")
    stub = _Stub(_happy_routes())
    _summary(tmp_path, datetime(2026, 10, 8, 6, 0, tzinfo=HKT), stub, 1_020_000, 990_000)
    failed = _Stub([])
    monthly = _summary(
        tmp_path,
        datetime(2026, 11, 1, 6, 0, tzinfo=HKT),
        failed,
        1_020_000,
        1_020_000,
    )
    validate_daily_summary(monthly)
    assert monthly["schema_version"] == "daily_summary/1.1"
    path = tmp_path / "reports" / "monthly" / "review_202610.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [row["id"] for row in payload["benchmarks"]] == ["SPY", "QQQ", "BRK.B", "BTC", "ETH"]
    spy = payload["benchmarks"][0]
    assert spy["month_return"] == pytest.approx(0.05, abs=1e-9)
    assert spy["since_start_return"] == pytest.approx(0.05, abs=1e-9)
    assert spy["max_drawdown"] == pytest.approx(-0.1, abs=1e-9)
    assert payload["paper"]["month_return"] == pytest.approx(0.02, abs=1e-9)
    assert (tmp_path / "reports" / "monthly" / "review_202610.md").is_file()
