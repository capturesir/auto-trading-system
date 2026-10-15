"""Strategy v0.9 handoff: trigger_ref, per-strategy positions, and the first batch."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.execution.stops import disaster_stop_price, protective_stop_price
from auto_trading.execution.strategy_book import roster, startup_lines
from auto_trading.execution.strategy_positions import (
    WorkingSell,
    ack_manual,
    book_fill,
    prepare_exit,
    quantity_breaks,
    sell_exposure,
)
from auto_trading.execution.sync import apply_recon, reconcile
from auto_trading.execution.trigger_ref import persist_intent, raw_field, validate_intent
from auto_trading.jsonutil import loads_strict
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.reports.daily_summary import write_daily_summary
from auto_trading.risk.gate import GateContext, Held, OpenOrder, Quote, RiskGate, RiskRejected
from auto_trading.strategy.spec import spec_hash

ROOT = Path(__file__).resolve().parents[1]
HKT = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 2, 22, 0, tzinfo=HKT)
PIN = "DUR243736"
MARKET = {
    "id": "ta_breakout52",
    "trigger": {"type": "market"},
    "exits": [{"type": "protective_stop_pct", "value": 15.0}],
}
SIGNAL = {
    "id": "sig_trend",
    "trigger": {"type": "signal"},
    "exits": [{"type": "stop_loss_pct", "value": 12.0}],
}


def _ref(pool_hash: str, *, asof: str = "2026-10-01", ticker_ok: bool = True) -> str:
    return (
        '{"pool_id":"ta_pool_202610","pool_hash":"'
        + pool_hash
        + '","pool_asof":"'
        + asof
        + '","indicator":"close_above_high","period":252,'
        + '"decision_close":100.5,"ref_level":99,"rank_value":'
        + ("1" if ticker_ok else "2")
        + "}"
    )


def _intent(ref: str | None, **extra: object) -> str:
    payload: dict[str, object] = {
        "intent_id": "i1",
        "intent_type": "ENTRY",
        "strategy_id": "ta_breakout52",
        "ticker": "NVDA",
        "side": "BUY",
        "signal_id": None,
        "confidence": None,
        "report_path": None,
        "fair_value": None,
        "rationale": "kept as written",
        "protective_stop_pct": 15.0,
    }
    payload.update(extra)
    text = json.dumps(payload, ensure_ascii=False)
    if ref is None:
        return text
    return text[:-1] + ',"trigger_ref":' + ref + "}"


def _archive(home: Path, body: str) -> str:
    folder = home / "data" / "universe" / "archive"
    folder.mkdir(parents=True)
    path = folder / "ta_pool_202610.csv"
    path.write_text(body, encoding="utf-8")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _ctx(home: Path, **kwargs: object) -> GateContext:
    if not (home / "state" / "risk_limits.json").is_file():
        write_risk_limits(home, build_risk_limits(now=NOW, nlv=1_000_000, account="DU1234567"))
    quote = Quote(last=100.0, as_of=NOW, received_at=NOW, market_data_type=1)
    payload: dict[str, object] = {
        "now": NOW,
        "trading_home": home,
        "accounts": ("DU1234567",),
        "expected_account": "DU1234567",
        "quotes": {"AAPL": quote, "NVDA": quote, "MSFT": quote},
        "fx_to_base": {"HKD": 1.0, "USD": 1.0},
    }
    payload.update(kwargs)
    return GateContext(**payload)  # type: ignore[arg-type]


def _buy(**kwargs: object) -> OrderIntent:
    payload: dict[str, object] = {
        "client_id": "cid-1",
        "symbol": "AAPL",
        "side": OrderSide.BUY,
        "quantity": 10,
        "limit_price": 100.0,
        "currency": "HKD",
        "market": "US",
        "intent_type": "ENTRY",
    }
    payload.update(kwargs)
    return OrderIntent(**payload)  # type: ignore[arg-type]


def test_t17_trigger_ref_round_trip_is_byte_identical(tmp_path: Path) -> None:
    digest = "sha256:" + _archive(tmp_path, "ticker,market\nNVDA,US\n")
    ref = _ref(digest)
    text = _intent(ref)
    when = datetime(2026, 10, 8, tzinfo=UTC)
    check = persist_intent(tmp_path, text, MARKET, now=when)
    assert check.code is None
    stored = TradeJournal(tmp_path).why("missing")
    assert stored["found"] is False
    with TradeJournal(tmp_path)._connect() as conn:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        saved = conn.execute("SELECT trigger_ref_json FROM intents").fetchone()[0]
    assert saved == raw_field(text, "trigger_ref")
    assert saved == ref
    row_text = (tmp_path / "data" / "trading.db").read_bytes()
    assert ref.encode() in row_text
    log = (tmp_path / "logs" / "events_20261008.jsonl").read_text(encoding="utf-8")
    assert ref in log


def test_t17_trigger_ref_over_4kb_is_rejected() -> None:
    ref = _ref("h" * 5000)
    check = validate_intent(_intent(ref), MARKET, now=datetime(2026, 10, 8, tzinfo=UTC))
    assert check.code == "INTENT_INVALID_FIELD"


def test_t17_non_market_trigger_ref_is_intent_invalid_field() -> None:
    text = _intent(_ref("abc"), signal_id="sig-1", confidence=70, strategy_id="sig_trend")
    check = validate_intent(text, SIGNAL, now=datetime(2026, 10, 8, tzinfo=UTC))
    assert check.code == "INTENT_INVALID_FIELD"


def test_t18_market_entry_passes_and_the_failure_codes(tmp_path: Path) -> None:
    when = datetime(2026, 10, 8, tzinfo=UTC)
    digest = "sha256:" + _archive(tmp_path, "ticker,market\nNVDA,US\n")
    ok = validate_intent(_intent(_ref(digest)), MARKET, now=when, home=tmp_path)
    assert ok.code is None
    missing = validate_intent(_intent(None), MARKET, now=when, home=tmp_path)
    assert missing.code == "TRIGGER_REF_MISSING"
    bad_hash = validate_intent(_intent(_ref("0" * 64)), MARKET, now=when, home=tmp_path)
    assert bad_hash.code == "TRIGGER_REF_INVALID"
    assert bad_hash.detail == "pool_hash_mismatch"
    archive = tmp_path / "data" / "universe" / "archive" / "ta_pool_202610.csv"
    archive.write_text("ticker,market\nAAPL,US\n", encoding="utf-8")
    other = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    absent = validate_intent(_intent(_ref(other)), MARKET, now=when, home=tmp_path)
    assert absent.code == "TRIGGER_REF_INVALID"
    assert absent.detail == "ticker_not_in_pool"
    stale = validate_intent(
        _intent(_ref(digest, asof="2026-01-01")),
        MARKET,
        now=when,
        home=tmp_path,
    )
    assert stale.code == "TRIGGER_REF_INVALID"
    assert stale.detail == "pool_stale"
    confidence = validate_intent(
        _intent(_ref(digest), confidence=70),
        MARKET,
        now=when,
        home=tmp_path,
    )
    assert confidence.code == "INTENT_INVALID_FIELD"
    signal = validate_intent(
        _intent(None, signal_id=None, strategy_id="sig_trend", protective_stop_pct=None),
        SIGNAL,
        now=when,
    )
    assert signal.code == "SIGNAL_NOT_FOUND"


def test_t19_protective_stop_uses_percent_points_and_keeps_the_decimal_default() -> None:
    assert disaster_stop_price(100, protective_stop_pct=15.0) == pytest.approx(85.00)
    assert protective_stop_price(100, 0.06) == pytest.approx(91)
    assert disaster_stop_price(100, stop_loss_pct=12.0) == pytest.approx(82)
    assert disaster_stop_price(100) == pytest.approx(85)
    mismatch = validate_intent(
        _intent(_ref("abc"), protective_stop_pct=14.0),
        MARKET,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )
    assert mismatch.code == "INTENT_INVALID_FIELD"
    bounded = validate_intent(
        _intent(_ref("abc"), protective_stop_pct=100),
        MARKET,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )
    assert bounded.code == "INTENT_INVALID_FIELD"
    inherited = validate_intent(
        _intent(_ref("abc"), intent_type="ADD", protective_stop_pct=None),
        MARKET,
        now=datetime(2026, 10, 8, tzinfo=UTC),
        entry_stop_pct=15.0,
    )
    assert inherited.code is None
    assert inherited.protective_stop_pct == pytest.approx(15.0)


def test_t20_two_strategies_sum_to_the_broker_and_ack_clears_the_gap(tmp_path: Path) -> None:
    journal = TradeJournal(tmp_path)
    book_fill(
        journal,
        account=PIN,
        strategy_id="sig_trend",
        con_id=1,
        ticker="NVDA",
        side="BUY",
        qty=100,
        price=100,
        intent_id="entry-a",
    )
    book_fill(
        journal,
        account=PIN,
        strategy_id="ta_breakout52",
        con_id=1,
        ticker="NVDA",
        side="BUY",
        qty=50,
        price=110,
        intent_id="entry-b",
    )
    rows = journal.strategy_position_rows()
    assert len(rows) == 2
    assert quantity_breaks(rows, {"NVDA": 150}) == []
    assert quantity_breaks(rows, {"NVDA": 140})
    ctx = _ctx(tmp_path, recon_ok=True)
    apply_recon(ctx, reconcile([], {"NVDA": 1.0}))
    assert ctx.recon_ok is False
    ack_manual(
        journal,
        account=PIN,
        broker_qty={"NVDA": 140},
        con_ids={"NVDA": 1},
    )
    assert quantity_breaks(journal.strategy_position_rows(), {"NVDA": 140}) == []
    manual = [row for row in journal.strategy_position_rows() if row["strategy_id"] == "_manual"]
    assert manual[0]["qty"] == pytest.approx(-10)


def test_t21_one_exit_leaves_the_other_strategy_and_rejects_a_short(tmp_path: Path) -> None:
    stops = [
        WorkingSell("sig_trend", "stp-a", 100, protective=True),
        WorkingSell("ta_breakout52", "stp-b", 50, protective=True),
    ]
    plan = prepare_exit(
        strategy_id="ta_breakout52",
        position_qty=50,
        exit_qty=50,
        stops=stops,
    )
    assert plan["code"] is None
    assert plan["stop_action"] == "cancel"
    assert plan["others"] == [("sig_trend", 100, "stp-a")]
    assert sell_exposure(stops[:1], 50) == pytest.approx(150)
    blocked = prepare_exit(
        strategy_id="ta_breakout52",
        position_qty=50,
        exit_qty=60,
        stops=stops,
    )
    assert blocked["code"] == "R20"
    ctx = _ctx(
        tmp_path,
        positions=[
            Held("AAPL", 100, 10_000, strategy_id="sig_trend"),
            Held("AAPL", 50, 5_000, strategy_id="ta_breakout52"),
        ],
        open_orders=[
            OpenOrder(
                "stp-a",
                "AAPL",
                "SELL",
                quantity=100,
                strategy_id="sig_trend",
                protective=True,
            ),
            OpenOrder(
                "stp-b",
                "AAPL",
                "SELL",
                quantity=50,
                strategy_id="ta_breakout52",
                protective=True,
            ),
        ],
    )
    over = _buy(
        side=OrderSide.SELL,
        intent_type="EXIT",
        parent_intent_id="entry-b",
        quantity=60,
        strategy_id="ta_breakout52",
        reduce_only=True,
    )
    with pytest.raises(RiskRejected, match="R20"):
        RiskGate(ctx).allow(over)
    ctx.open_orders = [
        OpenOrder("stp-a", "AAPL", "SELL", quantity=100, strategy_id="sig_trend", protective=True)
    ]
    sized = _buy(
        side=OrderSide.SELL,
        intent_type="EXIT",
        parent_intent_id="entry-b",
        quantity=50,
        strategy_id="ta_breakout52",
        reduce_only=True,
    )
    assert RiskGate(ctx).allow(sized) is None
    journal = TradeJournal(tmp_path)
    book_fill(
        journal,
        account=PIN,
        strategy_id="sig_trend",
        con_id=1,
        ticker="NVDA",
        side="BUY",
        qty=100,
        price=100,
        intent_id="entry-a",
    )
    book_fill(
        journal,
        account=PIN,
        strategy_id="ta_breakout52",
        con_id=1,
        ticker="NVDA",
        side="BUY",
        qty=50,
        price=110,
        intent_id="entry-b",
    )
    book_fill(
        journal,
        account=PIN,
        strategy_id="ta_breakout52",
        con_id=1,
        ticker="NVDA",
        side="SELL",
        qty=50,
        price=120,
        intent_id="stop-b",
        parent_intent_id="entry-b",
    )
    left = {row["strategy_id"]: row["qty"] for row in journal.strategy_position_rows()}
    assert left["sig_trend"] == pytest.approx(100)
    assert left["ta_breakout52"] == pytest.approx(0)


def test_t22_account_totals_halt_both_strategies_and_summary_splits_rows(tmp_path: Path) -> None:
    combined = _ctx(
        tmp_path,
        positions=[
            Held("AAPL", 100, 60_000, strategy_id="sig_trend"),
            Held("AAPL", 50, 30_000, strategy_id="ta_breakout52"),
        ],
    )
    with pytest.raises(RiskRejected, match="R3"):
        RiskGate(combined).allow(_buy(quantity=200, limit_price=100))
    pending = _ctx(
        tmp_path,
        positions=[
            Held("0700.HK", 100, 390_000),
            Held("7203.T", 100, 290_000),
        ],
        open_orders=[
            OpenOrder(
                "open-msft",
                "MSFT",
                "BUY",
                notional_base=280_000,
                market="US",
                currency="USD",
            )
        ],
    )
    with pytest.raises(RiskRejected, match="R6"):
        RiskGate(pending).allow(_buy(quantity=100, limit_price=100))
    second = _ctx(
        tmp_path,
        open_orders=[OpenOrder("open-a", "AAPL", "BUY", strategy_id="sig_trend")],
    )
    with pytest.raises(RiskRejected, match="R14"):
        RiskGate(second).allow(_buy(strategy_id="ta_breakout52", client_id="cid-2"))
    halted = _ctx(tmp_path, nlv=890_000, cash=890_000, hwm=1_000_000, day_start_nlv=890_000)
    for strategy_id in ("sig_trend", "ta_breakout52"):
        with pytest.raises(RiskRejected, match="R9"):
            RiskGate(halted).allow(_buy(strategy_id=strategy_id, client_id=strategy_id))
    streaks = _ctx(tmp_path, loss_streaks={"sig_trend": 3, "ta_breakout52": 0})
    RiskGate(streaks).allow(_buy(strategy_id="sig_trend"))
    assert streaks.r23_note == "r23_demote_count"
    streaks.r23_note = None
    RiskGate(streaks).allow(_buy(strategy_id="ta_breakout52", client_id="cid-2"))
    assert streaks.r23_note is None
    journal = TradeJournal(tmp_path)
    book_fill(
        journal,
        account=PIN,
        strategy_id="sig_trend",
        con_id=1,
        ticker="NVDA",
        side="BUY",
        qty=10,
        price=100,
        intent_id="entry-a",
    )
    moment = datetime(2026, 10, 13, 6, 0, tzinfo=HKT)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "account.json").write_text(
        json.dumps({"nlv": 1000, "hwm": 1000, "positions": [], "realized_base": 0}),
        encoding="utf-8",
    )
    summary = write_daily_summary(tmp_path, now=moment, complete=True, pin=PIN)
    assert summary["positions"][0]["strategy_id"] == "sig_trend"


def test_t23_why_uses_the_fixed_template_and_keeps_rationale(tmp_path: Path) -> None:
    digest = "sha256:" + _archive(tmp_path, "ticker,market\nNVDA,US\n")
    text = _intent(_ref(digest))
    when = datetime(2026, 10, 8, tzinfo=UTC)
    assert persist_intent(tmp_path, text, MARKET, now=when).code is None
    TradeJournal(tmp_path).record_fill(
        fill_id="fill-1",
        intent_id="i1",
        client_id="cid-1",
        symbol="NVDA",
        price=100.5,
        quantity=10,
    )
    traced = TradeJournal(tmp_path).why("fill-1")
    expected = "收市 100.5 創 252 日新高（前高 99）；股票池 ta_pool_202610"
    assert traced["template_rationale"] == expected
    assert traced["rationale"] == "kept as written"
    assert traced["pool_hash_ok"] is True
    assert traced["signal_id"] is None
    assert "signals.json" not in json.dumps(traced)


def test_t24_readonly_copy_keeps_pool_id_and_separate_pnl(tmp_path: Path) -> None:
    journal = TradeJournal(tmp_path)
    for strategy_id, intent_id, side, qty, price in (
        ("ta_breakout52", "b-entry", "BUY", 10, 100),
        ("ta_breakout52", "b-exit", "SELL", 10, 120),
        ("sig_trend", "a-entry", "BUY", 10, 100),
        ("sig_trend", "a-exit", "SELL", 10, 90),
    ):
        journal.record_intent(
            {
                "intent_id": intent_id,
                "strategy_id": strategy_id,
                "side": side,
                "ticker": "NVDA",
            },
            trigger_ref_raw=(
                '{"pool_id":"ta_pool_202610"}' if strategy_id == "ta_breakout52" else None
            ),
            trigger_type="market" if strategy_id == "ta_breakout52" else "signal",
        )
        journal.record_fill(
            fill_id=intent_id,
            intent_id=intent_id,
            client_id=intent_id,
            symbol="NVDA",
            price=price,
            quantity=qty,
        )
    copy = tmp_path / "box" / "trading.db"
    journal.backup(copy)
    clone = sqlite_pnl(copy)
    assert {row["strategy_id"] for row in clone} == {"ta_breakout52", "sig_trend"}
    by_id = {row["strategy_id"]: row["pnl"] for row in clone}
    assert by_id["ta_breakout52"] == pytest.approx(200)
    assert by_id["sig_trend"] == pytest.approx(-100)
    import sqlite3

    with sqlite3.connect(copy) as conn:
        raw = conn.execute(
            "SELECT trigger_ref_json FROM intents WHERE strategy_id = 'ta_breakout52'"
        ).fetchone()[0]
    assert "ta_pool_202610" in raw


def sqlite_pnl(path: Path) -> list[dict[str, float | str]]:
    home = path.parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    # Query the copy itself through a journal pointed at a tree that contains it.
    target = home / "data"
    target.mkdir()
    linked = target / "trading.db"
    linked.write_bytes(path.read_bytes())
    return TradeJournal(home).strategy_pnl()


def test_t25_sync_list_and_missing_archive_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    box = Path("vps/deploy/sync_box_to_vps.txt").read_text(encoding="utf-8")
    assert "data/universe" not in box
    outbound = Path("vps/deploy/sync_vps_to_box.txt").read_text(encoding="utf-8")
    assert "data/trading.db" in outbound
    assert "data/universe/" in outbound
    assert "--delete" not in outbound
    caplog.set_level("WARNING")
    check = validate_intent(
        _intent(_ref("sha256:" + "ab" * 32)),
        MARKET,
        now=datetime(2026, 10, 8, tzinfo=UTC),
        home=tmp_path,
    )
    assert check.code == "TRIGGER_REF_INVALID"
    assert check.detail == "archive_missing"
    assert not any("pool archive missing" in message for message in caplog.messages)


def test_t26_approved_hashes_and_a_changed_byte_is_r25(tmp_path: Path) -> None:
    rows = {row["strategy_id"]: row for row in roster()}
    assert rows["sig_trend"]["hash12"] == "9877996947f0"
    assert rows["sig_pullback"]["hash12"] == "d2449671d312"
    assert rows["ta_breakout52"]["hash12"] == "86046b4ceb98"
    assert rows["sig_trend"]["spec_hash"] == (
        "sha256:9877996947f027efe89aa0991b23e93ac3e409c38660531109a2a5ce4404a334"
    )
    assert rows["sig_pullback"]["spec_hash"] == (
        "sha256:d2449671d3124d5c2da214e2a71c999e17641a99fb7df9dbf7cb80549c53f231"
    )
    assert rows["ta_breakout52"]["spec_hash"] == (
        "sha256:86046b4ceb98bb010e28fe219fac2fc72c5256d222e6f782d62f1f3b79066101"
    )
    assert rows["ta_breakout52"]["approval"] == "auto"
    document = loads_strict((ROOT / "strategies/approved/ta_breakout52.v1.json").read_text())
    assert document["trigger"]["type"] == "market"
    assert document["exits"][1]["value"] == 15.0
    assert document["portfolio"]["max_gross_pct"] == 40.0
    assert spec_hash(document) == rows["ta_breakout52"]["spec_hash"]
    assert startup_lines() == [
        "sig_pullback v3 d2449671d312",
        "sig_trend v3 9877996947f0",
        "ta_breakout52 v1 " + rows["ta_breakout52"]["hash12"],
    ]
    strategies = tmp_path / "strategies"
    approved = strategies / "approved"
    approved.mkdir(parents=True)
    source = ROOT / "strategies/approved/sig_trend.v3.json"
    target = approved / "sig_trend.v3.json"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    digest = spec_hash(loads_strict(target.read_text(encoding="utf-8")))
    from auto_trading.strategy.registry import append_event

    append_event(
        strategies / "registry.jsonl",
        {
            "strategy_id": "sig_trend",
            "version": 3,
            "spec_hash": digest,
            "event": "approved",
            "actor": "user",
            "mode_after": "paper",
        },
    )
    ctx = _ctx(tmp_path, approved_hash=digest, approved_dir=strategies)
    assert (
        RiskGate(ctx).allow(_buy(strategy_id="sig_trend", strategy_version=3, spec_hash=digest))
        is None
    )
    changed = target.read_text(encoding="utf-8").replace("auto", "manual")
    target.write_text(changed, encoding="utf-8")
    with pytest.raises(RiskRejected, match="R25"):
        RiskGate(ctx).allow(
            _buy(strategy_id="sig_trend", strategy_version=3, spec_hash=digest, client_id="cid-2")
        )
