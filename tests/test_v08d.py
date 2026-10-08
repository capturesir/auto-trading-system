"""T28: mandatory pool archives, monthly rebuild, and the universe files."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import stat
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import pytest

from auto_trading.alerts.forwarder import scan_once
from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.cli_trade import main as trade_main
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.execution.trigger_ref import raw_field, validate_intent
from auto_trading.jsonutil import sha256_prefixed
from auto_trading.market.calendar import is_session
from auto_trading.market.symbols import ib_to_yahoo, yahoo_to_ib
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.risk.gate import GateContext, Held, Quote, RiskGate, RiskRejected
from auto_trading.strategy.spec import spec_hash
from auto_trading.universe.publish import RebuildError, publish_pool
from auto_trading.universe.rebuild import (
    check_pool_age,
    first_xhkg_business_day,
    retry_pending,
    run_rebuild,
    set_retry,
    should_run,
)

ROOT = Path(__file__).resolve().parents[1]
HKT = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 8, 22, 0, tzinfo=HKT)
POOL_HASH = "sha256:d1a84b42e758eb1950906878541d290a440f8eade63ac87b8e8bd723a74bfca8"
ARCHIVE_NAME = "ta_pool_202610.csv"
MARKET = {
    "id": "ta_breakout52",
    "trigger": {"type": "market"},
    "exits": [{"type": "protective_stop_pct", "value": 15.0}],
}
UNCHANGED = {
    "research/universe/README.md": (
        "eea1416b27a8107600ea300cef08bd9a2a6357e258bb25867342f4fad2dac5bb"
    ),
    "research/universe/ta_pool.py": (
        "35d69abc2c771f8e54e4416cd1f89a3cc09ccc4f3d08bc5e341d58d0bd13b453"
    ),
    "research/universe/build_pool.py": (
        "459486923bedb461f74ec6b990c6e7408626ca64331faf8e35f8caaa17dc4d9b"
    ),
    "research/universe/fetch_sources.py": (
        "63e80a19888324a7315d36ae243da2245231de6fe147f14d6b170d8b79105713"
    ),
    "research/universe/.gitignore": (
        "5fb39ee8df9cda2dc290c32e9cba441c295f11cbfdc485754ce3c107d39aafef"
    ),
    "data/universe/ta_pool.csv": (
        "d1a84b42e758eb1950906878541d290a440f8eade63ac87b8e8bd723a74bfca8"
    ),
    "data/universe/ta_pool.meta.json": (
        "4cb9d514c5b7cb40b20591dcdc92f0c22bccae190ec8e64885029c96db79712c"
    ),
    "data/universe/archive/ta_pool_202610.csv": POOL_HASH.split(":", 1)[1],
    "data/universe/.gitignore": (
        "5c64e6848f4f469f6e0c6132e77232b3829d83af675836b1d7b8602c99eca2b5"
    ),
}
EDITED = {
    "research/universe/verify_pool.py": (
        "d8d8d0e31c51a3f5548ba73b1b28e1ce912b2ddd6fa984ab9bd5cc5b652deb6b"
    ),
    "research/universe/tests/test_ta_pool.py": (
        "0402c2b1314e50ca1f49029e708c87528b41d9fadebcfe4d1b4812228ffb4ca1"
    ),
}


def _ref(pool_hash: str, *, asof: str = "2026-10-05", pool_id: str = "ta_pool_202610") -> str:
    return (
        '{"pool_id":"'
        + pool_id
        + '","pool_hash":"'
        + pool_hash
        + '","pool_asof":"'
        + asof
        + '","indicator":"close_above_high","period":252,'
        + '"decision_close":100.5,"ref_level":99,"rank_value":1}'
    )


def _intent(ticker: str, ref: str, *, intent_type: str = "ENTRY") -> str:
    side = "BUY" if intent_type in {"ENTRY", "ADD"} else "SELL"
    stop: float | None = 15.0 if intent_type in {"ENTRY", "ADD"} else None
    payload: dict[str, object] = {
        "intent_id": "i1",
        "intent_type": intent_type,
        "strategy_id": "ta_breakout52",
        "ticker": ticker,
        "side": side,
        "signal_id": None,
        "confidence": None,
        "report_path": None,
        "fair_value": None,
        "rationale": "kept as written",
        "protective_stop_pct": stop,
    }
    return json.dumps(payload, ensure_ascii=False)[:-1] + ',"trigger_ref":' + ref + "}"


def _install_archive(home: Path, data: bytes, name: str = ARCHIVE_NAME) -> Path:
    path = home / "data" / "universe" / "archive" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _check(home: Path, ticker: str, pool_hash: str, *, when: datetime = NOW, **kwargs: object):
    text = _intent(ticker, _ref(pool_hash, **kwargs))  # type: ignore[arg-type]
    return validate_intent(text, MARKET, now=when, home=home)


def _official() -> bytes:
    return (ROOT / "data" / "universe" / "archive" / ARCHIVE_NAME).read_bytes()


def _meta(pool_id: str, total: int, *, asof: str = "2026-10-05", digest: str = POOL_HASH) -> dict:
    return {
        "pool_id": pool_id,
        "asof": asof,
        "pool_hash": digest,
        "filters_hash": "sha256:" + "ab" * 32,
        "counts": {"base": {"total": total}},
        "csv": {
            "file": "data/universe/ta_pool.csv",
            "archive": f"data/universe/archive/{pool_id}.csv",
        },
    }


def _staging(folder: Path, meta: dict, csv_bytes: bytes) -> None:
    pool_id = str(meta["pool_id"])
    archive = folder / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / f"{pool_id}.csv").write_bytes(csv_bytes)
    (folder / "ta_pool.csv").write_bytes(csv_bytes)
    (folder / "ta_pool.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False),
        encoding="utf-8",
    )


def _hashes(folder: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if not folder.exists():
        return found
    for path in folder.rglob("*"):
        if not path.is_file() or ".staging" in path.parts:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        found[str(path.relative_to(folder))] = digest
    return found


def _gate(home: Path, **kwargs: object) -> GateContext:
    if not (home / "state" / "risk_limits.json").is_file():
        write_risk_limits(home, build_risk_limits(now=NOW, nlv=1_000_000, account="DU1234567"))
    quote = Quote(last=100.0, as_of=NOW, received_at=NOW, market_data_type=1)
    payload: dict[str, object] = {
        "now": NOW,
        "trading_home": home,
        "accounts": ("DU1234567",),
        "expected_account": "DU1234567",
        "quotes": {"GOOG": quote, "GOOGL": quote, "NVDA": quote},
        "fx_to_base": {"HKD": 1.0, "USD": 1.0},
    }
    payload.update(kwargs)
    return GateContext(**payload)  # type: ignore[arg-type]


def test_t28_1_pool_hash_is_an_exact_string(tmp_path: Path) -> None:
    data = _official()
    _install_archive(tmp_path, data)
    assert hashlib.sha256(data).hexdigest() == POOL_HASH.split(":", 1)[1]
    ok = _check(tmp_path, "AAPL", POOL_HASH)
    assert ok.code is None
    upper = "sha256:" + POOL_HASH.split(":", 1)[1].upper()
    bare = POOL_HASH.split(":", 1)[1]
    spaced = POOL_HASH + " "
    cases = {
        upper: "pool_hash_mismatch",
        bare: "pool_hash_mismatch",
        spaced: "pool_hash_mismatch",
    }
    for claimed, detail in cases.items():
        check = _check(tmp_path, "AAPL", claimed)
        assert check.code == "TRIGGER_REF_INVALID"
        assert check.detail == detail
    flipped = bytearray(data)
    flipped[0] ^= 0x01
    _install_archive(tmp_path, bytes(flipped))
    changed = _check(tmp_path, "AAPL", POOL_HASH)
    assert changed.code == "TRIGGER_REF_INVALID"
    assert changed.detail == "pool_hash_mismatch"
    (tmp_path / "data" / "universe" / "archive" / ARCHIVE_NAME).unlink()
    missing = _check(tmp_path, "AAPL", POOL_HASH)
    assert missing.code == "TRIGGER_REF_INVALID"
    assert missing.detail == "archive_missing"
    meta = json.loads((ROOT / "data" / "universe" / "ta_pool.meta.json").read_text())
    assert meta["filters_hash"].startswith("sha256:")
    assert len(meta["filters_hash"]) == len("sha256:") + 64


def test_t28_2_ticker_is_yahoo_exact_and_month_is_not_derived(tmp_path: Path) -> None:
    _install_archive(tmp_path, _official())
    outside = _check(tmp_path, "FDXF", POOL_HASH)
    assert outside.code is None
    ibkr = _check(tmp_path, "BRK B", POOL_HASH)
    assert ibkr.code == "TRIGGER_REF_INVALID"
    assert ibkr.detail == "ticker_not_in_pool"
    october = _check(tmp_path, "BRK-B", POOL_HASH, asof="2026-10-05")
    assert october.code is None
    with (ROOT / "data" / "universe" / "archive" / ARCHIVE_NAME).open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1088
    assert any(row["ticker"] == "FDXF" and row["in_pool"] == "false" for row in rows)


def test_t28_3_yahoo_ibkr_conversion(tmp_path: Path) -> None:
    pairs = (
        ("BRK-B", "BRK B", {}),
        ("7203.T", "7203", {"market": "JP"}),
        ("0883.HK", "883", {"market": "HK"}),
        ("005930.KS", "005930", {"market": "KR", "primary_exchange": "KRX"}),
        ("005930.KQ", "005930", {"market": "KR", "primary_exchange": "KOSDAQ"}),
        ("2330.TW", "2330", {"market": "TW"}),
    )
    for yahoo, ibkr, reverse in pairs:
        assert yahoo_to_ib(yahoo) == ibkr
        assert ib_to_yahoo(ibkr, **reverse) == yahoo
    table = tmp_path / "symbols.csv"
    table.write_text(
        "ticker,ib_symbol,ib_exchange,issuer_group,con_id\nBRK-B,BRK.B,SMART,berkshire,1\n",
        encoding="utf-8",
    )
    assert yahoo_to_ib("BRK-B", symbols_csv=table) == "BRK.B"
    assert ib_to_yahoo("BRK.B", symbols_csv=table) == "BRK-B"


def test_t28_4_execution_does_not_merge_issuer_groups(tmp_path: Path) -> None:
    gate = (ROOT / "src" / "auto_trading" / "risk" / "gate.py").read_text(encoding="utf-8")
    assert "issuer_group" not in gate
    for path in (ROOT / "src" / "auto_trading" / "execution").rglob("*.py"):
        assert "issuer_group" not in path.read_text(encoding="utf-8")
    separate = _gate(
        tmp_path,
        positions=[Held("GOOG", 10, 90_000, currency="USD")],
    )
    order = OrderIntent(
        client_id="goog-1",
        symbol="GOOGL",
        side=OrderSide.BUY,
        quantity=400,
        limit_price=100,
        currency="USD",
        market="US",
    )
    assert RiskGate(separate).allow(order) is None
    merged = _gate(
        tmp_path,
        positions=[Held("GOOGL", 10, 90_000, currency="USD")],
    )
    with pytest.raises(RiskRejected, match="R3"):
        RiskGate(merged).allow(order)
    archive = (ROOT / "data" / "universe" / "archive" / ARCHIVE_NAME).read_text(encoding="utf-8")
    assert "GOOG," in archive
    assert "GOOGL," in archive


def test_t28_5_timer_runs_on_the_first_xhkg_day_and_timeout_fails(tmp_path: Path) -> None:
    assert is_session("XHKG", date(2026, 10, 1)) is False
    assert first_xhkg_business_day(2026, 10) == date(2026, 10, 2)
    assert first_xhkg_business_day(2026, 11) == date(2026, 11, 2)
    assert should_run(date(2026, 11, 2), retry_pending=False) is True
    assert should_run(date(2026, 11, 3), retry_pending=False) is False
    assert should_run(date(2026, 11, 3), retry_pending=True) is True
    called: list[str] = []

    def runner(step: str, _argv: list[str], _remaining: float) -> int:
        called.append(step)
        return 1

    moment = datetime(2026, 11, 3, 6, 0, tzinfo=HKT)
    skipped = run_rebuild(tmp_path, today=date(2026, 11, 3), now=moment, runner=runner)
    assert skipped.ran is False
    assert called == []
    set_retry(tmp_path, True, step="fetch_sources")
    run_rebuild(tmp_path, today=date(2026, 11, 3), now=moment, runner=runner)
    assert called == ["fetch_sources"]
    service = (ROOT / "vps" / "deploy" / "pool-rebuild.service").read_text(encoding="utf-8")
    timer = (ROOT / "vps" / "deploy" / "pool-rebuild.timer").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "pool_rebuild.sh").read_text(encoding="utf-8")
    assert "TimeoutStartSec=45min" in service
    assert "06:00:00" in timer
    assert "Asia/Hong_Kong" in timer
    assert "pool_rebuild.sh" in service
    packages = script + "\n" + (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for name in ("pandas", "pyarrow", "lxml", "openpyxl", "yfinance", "exchange_calendars"):
        assert name.replace("_", "-") in packages or name in packages
    sleep = [sys.executable, "-c", "import time; time.sleep(30)"]
    timed = run_rebuild(
        tmp_path,
        today=date(2026, 11, 2),
        now=datetime(2026, 11, 2, 6, 0, tzinfo=HKT),
        timeout_sec=0.4,
        steps=[("fetch_sources", sleep)],
    )
    assert timed.ran is True
    assert timed.ok is False
    assert timed.step == "timeout"
    assert list((tmp_path / "notifications" / "outbox").glob("*pool_rebuild_failed.json"))


def test_t28_6_verify_root_checks_the_staging_tree(tmp_path: Path) -> None:
    payload = b"ticker,market\nAAA,US\n"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    filters = {"min_history_sessions": 260}
    staging = tmp_path / "staging"
    meta = _meta("ta_pool_202611", 1000, digest=digest)
    meta["filters_hash"] = sha256_prefixed(filters)
    _staging(staging, meta, payload)
    strategy = tmp_path / "strategy.json"
    strategy.write_text(
        json.dumps({"universe": {"filters": filters}}),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "research/universe/verify_pool.py",
            "--root",
            str(staging),
            "--strategy",
            str(strategy),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    source = (ROOT / "research" / "universe" / "verify_pool.py").read_text(encoding="utf-8")
    assert "current_csv" in source
    assert "--root" in source
    assert "--current" in source


def test_t28_7_publish_rolls_back_between_steps_and_keeps_order(tmp_path: Path) -> None:
    universe = tmp_path / "data" / "universe"
    universe.mkdir(parents=True)
    original = b"ticker,market\nOLD,US\n"
    (universe / "ta_pool.csv").write_bytes(original)
    previous = _meta("ta_pool_202610", 1000)
    (universe / "ta_pool.meta.json").write_text(json.dumps(previous), encoding="utf-8")
    old_archive = universe / "archive" / "ta_pool_202610.csv"
    old_archive.parent.mkdir()
    old_archive.write_bytes(original)
    fresh = b"ticker,market\nNEW,US\n"
    digest = "sha256:" + hashlib.sha256(fresh).hexdigest()
    staging = tmp_path / "staging"
    meta = _meta("ta_pool_202611", 1000, digest=digest)
    _staging(staging, meta, fresh)
    before = _hashes(universe)
    for broken in ("archive", "csv", "meta"):

        def hook(name: str, broken: str = broken) -> None:
            if name == broken:
                raise RebuildError("injected")

        with pytest.raises(RebuildError):
            publish_pool(universe, staging, today=date(2026, 11, 2), hook=hook)
        assert _hashes(universe) == before
    order: list[str] = []
    publish_pool(
        universe,
        staging,
        today=date(2026, 11, 2),
        hook=order.append,
    )
    assert order == ["archive", "csv", "meta"]
    placed = universe / "archive" / "ta_pool_202611.csv"
    assert stat.S_IMODE(placed.stat().st_mode) == 0o444
    assert old_archive.read_bytes() == original
    clash = tmp_path / "clash"
    _staging(clash, meta, b"ticker,market\nOTHER,US\n")
    with pytest.raises(RebuildError, match="archive_conflict"):
        publish_pool(universe, clash, today=date(2026, 11, 2))
    assert placed.read_bytes() == fresh
    short = _meta("ta_pool_202611", 100, digest=digest)
    thin = tmp_path / "thin"
    _staging(thin, short, fresh)
    with pytest.raises(RebuildError, match="constituent_count"):
        publish_pool(universe, thin, today=date(2026, 11, 2))
    wrong_month = _meta("ta_pool_202610", 1000, digest=digest)
    early = tmp_path / "early"
    _staging(early, wrong_month, fresh)
    with pytest.raises(RebuildError, match="pool_month"):
        publish_pool(universe, early, today=date(2026, 11, 2))


def test_t28_8_failures_alert_and_leave_the_current_files(tmp_path: Path) -> None:
    universe = tmp_path / "data" / "universe"
    universe.mkdir(parents=True)
    body = b"ticker,market\nKEEP,US\n"
    (universe / "ta_pool.csv").write_bytes(body)
    (universe / "ta_pool.meta.json").write_text("{}\n", encoding="utf-8")
    archive = universe / "archive" / ARCHIVE_NAME
    archive.parent.mkdir()
    archive.write_bytes(body)
    before = _hashes(universe)

    def fail_fetch(step: str, _argv: list[str], _remaining: float) -> int:
        return 1 if step == "fetch_sources" else 0

    def fail_verify(step: str, _argv: list[str], _remaining: float) -> int:
        return 1 if step == "verify_pool" else 0

    moment = datetime(2026, 11, 2, 6, 0, tzinfo=HKT)
    fetch = run_rebuild(tmp_path, today=date(2026, 11, 2), now=moment, runner=fail_fetch)
    assert fetch.step == "fetch_sources"
    assert fetch.ok is False
    verify = run_rebuild(
        tmp_path,
        today=date(2026, 11, 2),
        now=moment + timedelta(minutes=1),
        runner=fail_verify,
    )
    assert verify.step == "verify_pool"
    sleep = [sys.executable, "-c", "import time; time.sleep(30)"]
    timed = run_rebuild(
        tmp_path,
        today=date(2026, 11, 2),
        now=moment + timedelta(minutes=2),
        timeout_sec=0.4,
        steps=[("fetch_sources", sleep)],
    )
    assert timed.step == "timeout"
    assert _hashes(universe) == before
    assert not (universe / ".staging").exists()
    assert retry_pending(tmp_path) is True
    notes = list((tmp_path / "notifications" / "outbox").glob("*pool_rebuild_failed.json"))
    assert len(notes) == 3
    for note in notes:
        payload = json.loads(note.read_text(encoding="utf-8"))
        assert payload["level"] == "critical"
        assert payload["kind"] == "exec_critical"
    critical = list((tmp_path / "logs").glob("critical_*.jsonl"))
    lines = critical[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for line in lines:
        detail = json.loads(line)["detail"]
        assert detail["step"] in {"fetch_sources", "verify_pool", "timeout"}
        assert detail["log_path"]
    scan_once(tmp_path, moment)
    assert len(list((tmp_path / "notifications" / "outbox").glob("*pool_rebuild_failed.json"))) == 3


def test_t28_9_old_pool_id_survives_rebuild_and_r25_is_unchanged(tmp_path: Path) -> None:
    data = _official()
    _install_archive(tmp_path, data)
    current = tmp_path / "data" / "universe" / "ta_pool.csv"
    current.write_bytes(b"ticker,market\nNOT_IN_ARCHIVE,US\n")
    before = _check(tmp_path, "AAPL", POOL_HASH)
    assert before.code is None
    only_current = _check(tmp_path, "NOT_IN_ARCHIVE", POOL_HASH)
    assert only_current.detail == "ticker_not_in_pool"
    strategy = ROOT / "strategies" / "approved" / "ta_breakout52.v1.json"
    digest = spec_hash(json.loads(strategy.read_text(encoding="utf-8")))
    fresh = b"ticker,market\nNEW,US\n"
    new_hash = "sha256:" + hashlib.sha256(fresh).hexdigest()
    staging = tmp_path / "staging"
    meta = _meta("ta_pool_202611", 1088, digest=new_hash)
    previous = _meta("ta_pool_202610", 1088)
    meta_path = tmp_path / "data" / "universe" / "ta_pool.meta.json"
    meta_path.write_text(json.dumps(previous), encoding="utf-8")
    _staging(staging, meta, fresh)
    publish_pool(tmp_path / "data" / "universe", staging, today=date(2026, 11, 2))
    after = _check(tmp_path, "AAPL", POOL_HASH)
    assert after.code is None
    assert spec_hash(json.loads(strategy.read_text(encoding="utf-8"))) == digest
    ctx = _gate(tmp_path, approved_hash=digest, approved_dir=ROOT / "strategies")
    assert (
        RiskGate(ctx).allow(
            OrderIntent(
                client_id="r25",
                symbol="NVDA",
                side=OrderSide.BUY,
                quantity=1,
                limit_price=100,
                currency="USD",
                market="US",
                strategy_id="ta_breakout52",
                strategy_version=1,
                spec_hash=digest,
            )
        )
        is None
    )


def test_t28_10_age_warns_then_blocks_entries_and_lets_exits_through(tmp_path: Path) -> None:
    asof = date(2026, 10, 5)
    meta = _meta("ta_pool_202610", 1088, asof=asof.isoformat())
    folder = tmp_path / "data" / "universe"
    folder.mkdir(parents=True)
    (folder / "ta_pool.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    _install_archive(tmp_path, _official())
    warn_day = asof + timedelta(days=33)
    limit_day = asof + timedelta(days=40)
    stale_day = asof + timedelta(days=41)
    assert warn_day == date(2026, 11, 7)
    assert limit_day == date(2026, 11, 14)
    assert stale_day == date(2026, 11, 15)
    assert check_pool_age(tmp_path, warn_day, datetime(2026, 11, 7, tzinfo=UTC)) == (
        "pool_age_warn"
    )
    assert check_pool_age(tmp_path, limit_day, datetime(2026, 11, 14, tzinfo=UTC)) == (
        "pool_age_warn"
    )
    notes = list((tmp_path / "notifications" / "outbox").glob("*pool_age_warn.json"))
    assert len(notes) == 2
    check_pool_age(tmp_path, limit_day, datetime(2026, 11, 14, 1, tzinfo=UTC))
    assert len(list((tmp_path / "notifications" / "outbox").glob("*pool_age_warn.json"))) == 2
    allowed = _check(tmp_path, "AAPL", POOL_HASH, when=datetime(2026, 11, 14, tzinfo=UTC))
    assert allowed.code is None
    refused = _check(tmp_path, "AAPL", POOL_HASH, when=datetime(2026, 11, 15, tzinfo=UTC))
    assert refused.code == "TRIGGER_REF_INVALID"
    assert refused.detail == "pool_stale"
    assert check_pool_age(tmp_path, stale_day, datetime(2026, 11, 15, tzinfo=UTC)) == "pool_stale"
    check_pool_age(tmp_path, stale_day + timedelta(days=1), datetime(2026, 11, 16, tzinfo=UTC))
    stale_notes = list((tmp_path / "logs").glob("critical_*.jsonl"))
    assert len(stale_notes[0].read_text(encoding="utf-8").splitlines()) == 1
    exit_text = _intent("AAPL", _ref(POOL_HASH, asof="2026-10-05"), intent_type="EXIT")
    exited = validate_intent(
        exit_text,
        MARKET,
        now=datetime(2026, 11, 15, tzinfo=UTC),
        home=tmp_path,
    )
    assert exited.code is None
    ctx = _gate(
        tmp_path,
        positions=[Held("NVDA", 10, 1_000, currency="USD")],
    )
    assert (
        RiskGate(ctx).allow(
            OrderIntent(
                client_id="exit-1",
                symbol="NVDA",
                side=OrderSide.SELL,
                quantity=10,
                limit_price=100,
                currency="USD",
                market="US",
                intent_type="EXIT",
                parent_intent_id="entry-1",
                protective=True,
            )
        )
        is None
    )


def test_t28_11_universe_syncs_from_vps_without_delete(tmp_path: Path) -> None:
    box_list = (ROOT / "vps" / "deploy" / "sync_box_to_vps.txt").read_text(encoding="utf-8")
    vps_list = (ROOT / "vps" / "deploy" / "sync_vps_to_box.txt").read_text(encoding="utf-8")
    assert "data/universe" not in box_list
    assert "\ndata/universe/\n" in "\n" + vps_list
    assert "--delete" not in vps_list
    vps = tmp_path / "vps" / "data" / "universe" / "archive"
    box = tmp_path / "box" / "data" / "universe" / "archive"
    vps.mkdir(parents=True)
    (vps / ARCHIVE_NAME).write_bytes(b"pool\n")
    box.mkdir(parents=True)
    restored = box / ARCHIVE_NAME
    restored.write_bytes((vps / ARCHIVE_NAME).read_bytes())
    restored.unlink()
    assert (vps / ARCHIVE_NAME).read_bytes() == b"pool\n"
    restored.write_bytes((vps / ARCHIVE_NAME).read_bytes())
    assert restored.read_bytes() == b"pool\n"


def test_t28_12_why_reads_an_old_archive(tmp_path: Path) -> None:
    body = b"ticker,market\nNVDA,US\n"
    path = _install_archive(tmp_path, body, "ta_pool_202609.csv")
    os.chmod(path, 0o444)
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    text = _intent("NVDA", _ref(digest, pool_id="ta_pool_202609"))
    assert validate_intent(text, MARKET, now=NOW, home=tmp_path).code is None
    journal = TradeJournal(tmp_path)
    payload = json.loads(text)
    journal.record_intent(
        payload,
        trigger_ref_raw=raw_field(text, "trigger_ref"),
        trigger_type="market",
    )
    journal.record_fill(
        fill_id="fill-old",
        intent_id="i1",
        client_id="cid-old",
        symbol="NVDA",
        price=10,
        quantity=1,
    )
    buffer = StringIO()
    with redirect_stdout(buffer):
        trade_main(["--home", str(tmp_path), "why", "fill-old"])
    traced = json.loads(buffer.getvalue())
    assert traced["pool_hash_ok"] is True
    assert traced["pool_hash_check"] == "matched"
    assert traced["pool_archive"].endswith("ta_pool_202609.csv")
    assert stat.S_IMODE(path.stat().st_mode) == 0o444


def test_t28_13_universe_files_keep_the_inbox_bytes() -> None:
    listed = subprocess.check_output(
        ["git", "ls-files", "research/universe", "data/universe"],
        cwd=ROOT,
        text=True,
    ).split()
    expected = set(UNCHANGED) | set(EDITED)
    assert expected <= set(listed)
    for rel, digest in UNCHANGED.items():
        assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == digest
    for rel, digest in EDITED.items():
        assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() != digest


def test_t28_14_universe_gitignores_cover_cache_and_staging(tmp_path: Path) -> None:
    del tmp_path
    samples = (
        "research/universe/__pycache__/x.pyc",
        "research/universe/cache/raw.bin",
        "research/universe/.staging/run/x",
        "data/universe/.staging/run/x",
        "data/universe/foo.tmp",
    )
    created: list[Path] = []
    try:
        for rel in samples:
            path = ROOT / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
            created.append(path)
            completed = subprocess.run(
                ["git", "check-ignore", "-q", rel],
                cwd=ROOT,
                check=False,
            )
            assert completed.returncode == 0
    finally:
        for path in created:
            path.unlink(missing_ok=True)


def test_t28_15_no_secrets_and_no_file_over_five_megabytes() -> None:
    needles = ("api_key", "password", "secret", "Bearer ")
    for base in (ROOT / "research" / "universe", ROOT / "data" / "universe"):
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".md", ".json", ".csv"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            for needle in needles:
                assert needle.lower() not in text
            assert path.stat().st_size < 5 * 1024 * 1024
    completed = subprocess.run(
        [sys.executable, "scripts/check_v08_ci.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_t28_16_raw_data_tests_skip_on_a_clean_checkout() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "-v", "tests.test_ta_pool"],
        cwd=ROOT / "research" / "universe",
        check=False,
        capture_output=True,
        text=True,
    )
    blob = completed.stdout + completed.stderr
    assert completed.returncode == 0, blob
    assert blob.count("skipped") >= 2


def test_t28_17_universe_tree_has_no_forbidden_token() -> None:
    import re

    pattern = re.compile(
        "[A-Za-z0-9_]*" + ("liv" + "e") + "[A-Za-z0-9_]*",
        re.IGNORECASE,
    )
    hits: list[str] = []
    for base in (ROOT / "research" / "universe", ROOT / "data" / "universe"):
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in {"__pycache__", ".staging", "cache"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            hits.extend(pattern.findall(text))
    assert hits == []
    allow = (ROOT / "ci" / "r0_live_allowlist.txt").read_text(encoding="utf-8").splitlines()
    assert allow == [
        "upside_live",
        "upside_live_pct",
        "min_upside_live_pct",
        "upside_live_below_pct",
    ]
