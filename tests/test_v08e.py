"""T29: R9 cooldown, half size, and the unpublished risk_limits 1.2 hash."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from auto_trading.backtest.allocator import Proposal, allocate
from auto_trading.backtest.risk_sim import RiskSimulator, fill_rejections, replay_vector
from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.cli_trade import halt_r9, main, status_text, stop
from auto_trading.config import get_settings
from auto_trading.execution.approval_mode import APPROVER_AUTO
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.intents.writer import filter_for_risk
from auto_trading.market.calendar import is_session
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.reports.daily_summary import validate_daily_summary, write_daily_summary
from auto_trading.risk.gate import GateContext, Held, Quote, RiskGate, RiskRejected
from auto_trading.risk.limits import (
    EXPECTED_LIMITS,
    HASH_ODD_LOT_OFF,
    HASH_ODD_LOT_ON,
    HASH_R2_AS_FLOAT,
    HASH_SCOPE_INSIDE_LIMITS,
    HASH_V12_COOLDOWN_AS_FLOAT,
    HASH_V12_ODD_LOT_OFF,
    HASH_V12_ODD_LOT_ON,
    HASH_V12_SCOPE_INSIDE_LIMITS,
    assess_payload,
    config_hash,
    headroom_v12,
    limits_v12,
)
from auto_trading.risk.r9_cycle import initial_state, on_nlv, on_risk_day_end
from auto_trading.risk.r9_state import (
    _seal,
    catch_up,
    close_risk_day,
    cycle_path,
    cycle_view,
    expected_resume_date,
    load_cycle,
    observe_nlv,
    resume_early,
)

NOW = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)
GATE_NOW = datetime(2026, 10, 2, 22, 0, tzinfo=timezone(timedelta(hours=8)))
PIN = "DU1234567"
_VECTORS = json.loads(
    (Path(__file__).parent / "vectors" / "r9_cycle_vectors.json").read_text(encoding="utf-8")
)


def _limits(home: Path, nlv: float = 1_000_000) -> None:
    write_risk_limits(home, build_risk_limits(now=NOW, nlv=nlv, account=PIN, hwm=nlv))


def _quote(symbol: str, moment: datetime) -> Quote:
    return Quote(last=100.0, as_of=moment, received_at=moment, market_data_type=1)


def _ctx(home: Path, **kwargs: object) -> GateContext:
    _limits(home, nlv=float(kwargs.get("nlv", 1_000_000)))
    payload: dict = {
        "now": GATE_NOW,
        "trading_home": home,
        "expected_account": PIN,
        "accounts": (PIN,),
        "nlv": 1_000_000.0,
        "cash": 1_000_000.0,
        "hwm": 1_000_000.0,
        "day_start_nlv": 1_000_000.0,
        "quotes": {"AAPL": _quote("AAPL", GATE_NOW), "NVDA": _quote("NVDA", GATE_NOW)},
        "fx_to_base": {"HKD": 1.0, "USD": 1.0},
    }
    payload.update(kwargs)
    return GateContext(**payload)


def _buy(**kwargs: object) -> OrderIntent:
    payload: dict = {
        "client_id": "cid-1",
        "symbol": "AAPL",
        "side": OrderSide.BUY,
        "quantity": 10,
        "limit_price": 100.0,
        "currency": "HKD",
        "market": "US",
    }
    payload.update(kwargs)
    return OrderIntent(**payload)


def _insert(home: Path, order_id: str, *, protective: bool) -> None:
    TradeJournal(home)
    with sqlite3.connect(home / "data" / "trading.db") as conn:
        conn.execute(
            """
            INSERT INTO orders (order_id, intent_id, seq, symbol, state, intent_type, protective)
            VALUES (?, ?, 1, 'AAPL', 'SUBMITTED', ?, ?)
            """,
            (order_id, order_id, "EXIT" if protective else "ENTRY", int(protective)),
        )


def _xhkg_days(start: date, count: int) -> list[date]:
    found: list[date] = []
    day = start
    while len(found) < count:
        if is_session("XHKG", day):
            found.append(day)
        day += timedelta(days=1)
    return found


def test_t29_2_cooldown_allows_exits_and_does_not_write_kill_switch(tmp_path: Path) -> None:
    observe_nlv(tmp_path, "2026-11-02", 1000.0, NOW, hwm=1000.0)
    _insert(tmp_path, "open-1", protective=False)
    _insert(tmp_path, "stop-1", protective=True)
    observe_nlv(tmp_path, "2026-11-02", 899.0, NOW)
    assert not (tmp_path / "KILL_SWITCH").exists()
    runtime = json.loads((tmp_path / "state" / "strategy_runtime.json").read_text(encoding="utf-8"))
    assert runtime["status"] == "halted"
    with sqlite3.connect(tmp_path / "data" / "trading.db") as conn:
        rows = {row[0]: row[1] for row in conn.execute("SELECT order_id, state FROM orders")}
    assert rows["open-1"] == "CANCEL_REQUESTED"
    assert rows["stop-1"] == "SUBMITTED"
    view = cycle_view(tmp_path)
    assert view is not None and view["state"] == "HALTED_COOLDOWN"
    ctx = _ctx(tmp_path, nlv=1000.0, cash=1000.0, hwm=1000.0, day_start_nlv=1000.0)
    ctx.positions = [Held("AAPL", 5, 400)]
    with pytest.raises(RiskRejected, match="R9_HALTED_COOLDOWN"):
        RiskGate(ctx).allow(_buy())
    with pytest.raises(RiskRejected, match="R9_HALTED_COOLDOWN"):
        RiskGate(ctx).allow(_buy(intent_type="ADD", client_id="cid-add"))
    exit_order = _buy(
        side=OrderSide.SELL,
        intent_type="EXIT",
        parent_intent_id="entry-1",
        quantity=1,
        client_id="cid-exit",
    )
    reduce = _buy(
        side=OrderSide.SELL,
        intent_type="REDUCE",
        parent_intent_id="entry-1",
        quantity=1,
        client_id="cid-reduce",
    )
    assert RiskGate(ctx).allow(exit_order) is None
    assert RiskGate(ctx).allow(reduce) is None
    other = tmp_path / "full-stop"
    stop(other)
    assert (other / "KILL_SWITCH").is_file()


def test_t29_3_real_calendar_resume_matches_the_session_count() -> None:
    start = "2026-11-02"
    state = initial_state(1000.0)
    state, _events = on_nlv(state, start, 899.0)
    day = date.fromisoformat(start)
    resumed = None
    for _ in range(90):
        day += timedelta(days=1)
        state, _ignored = on_nlv(state, day.isoformat(), 905.0)
        state, _events = on_risk_day_end(
            state,
            day.isoformat(),
            is_session("XHKG", day),
            905.0,
            False,
        )
        if state["state"] == "HALF_SIZE":
            resumed = day.isoformat()
            break
    assert resumed == expected_resume_date(start)
    assert state["cooldown_days_done"] == 20


def test_t29_4_half_size_scales_r1_r2_r6_only(tmp_path: Path) -> None:
    observe_nlv(tmp_path, "2026-11-02", 8_990_000.0, NOW, hwm=10_000_000.0)
    resume_early(tmp_path, "2026-11-03", 9_050_000.0, NOW)
    nlv = 10_000_000.0
    positions = [
        Held("AAPL", 1, 900_000),
        Held("MSFT", 1, 900_000),
        Held("GOOGL", 1, 900_000),
        Held("0700.HK", 1, 900_000),
        Held("0883.HK", 1, 900_000),
        Held("7203.T", 1, 900_000),
        Held("005930.KS", 1, 600_000),
    ]
    ctx = _ctx(
        tmp_path,
        nlv=nlv,
        cash=nlv,
        hwm=nlv,
        day_start_nlv=nlv,
        positions=positions,
    )
    grown = _buy(symbol="NVDA", quantity=500, limit_price=100.0, client_id="cid-nvda")
    with pytest.raises(RiskRejected, match="R9_HALF_SIZE_LIMIT"):
        RiskGate(ctx).allow(grown)
    wide = _buy(quantity=1500, limit_price=100.0, client_id="cid-wide")
    with pytest.raises(RiskRejected, match="R9_HALF_SIZE_LIMIT"):
        RiskGate(_ctx(tmp_path, nlv=nlv, cash=nlv, hwm=nlv, day_start_nlv=nlv)).allow(wide)
    ctx.positions = [Held("AAPL", 5, 6_000_000)]
    sell = _buy(
        side=OrderSide.SELL,
        intent_type="EXIT",
        parent_intent_id="entry-1",
        quantity=1,
        client_id="cid-sell",
    )
    before = list((tmp_path / "state").glob("*.json"))
    assert RiskGate(ctx).allow(sell) is None
    assert list((tmp_path / "state").glob("*.json")) == before


def test_t29_5_half_size_reset_is_an_ops_warn(tmp_path: Path) -> None:
    vector = next(
        item for item in _VECTORS["vectors"] if item["name"] == "half_size_breach_resets_counter"
    )
    _drive(tmp_path, vector)
    notes = list((tmp_path / "notifications" / "outbox").glob("*r9_half_size_reset.json"))
    assert notes
    body = json.loads(notes[-1].read_text(encoding="utf-8"))
    assert body["level"] == "warn"
    assert body["kind"] == "r9_half_size_reset"


def test_t29_6_retrigger_starts_a_new_cycle(tmp_path: Path) -> None:
    vector = next(item for item in _VECTORS["vectors"] if item["name"] == "retrigger_in_half_size")
    document = _drive(tmp_path, vector)
    assert document["cycle_no"] == 2
    assert document["state"] == "HALTED_COOLDOWN"
    critical = list((tmp_path / "logs").glob("critical_*.jsonl"))
    text = "\n".join(path.read_text(encoding="utf-8") for path in critical)
    assert text.count('"kind": "r9_halt"') == 2


def test_t29_7_kill_switch_blocks_resume_and_manual_approval_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observe_nlv(tmp_path, "2026-11-02", 899.0, NOW, hwm=1000.0)
    (tmp_path / "KILL_SWITCH").write_text("manual\n", encoding="utf-8")
    for index in range(1, 21):
        close_risk_day(tmp_path, f"2026-11-{index + 2:02d}", 905.0, NOW, is_xhkg=True)
    view = cycle_view(tmp_path)
    assert view is not None
    assert view["state"] == "HALTED_COOLDOWN"
    assert view["resume_pending"] is True
    blocked = list((tmp_path / "notifications" / "outbox").glob("*r9_resume_blocked.json"))
    assert len(blocked) == 1
    (tmp_path / "KILL_SWITCH").unlink()
    close_risk_day(tmp_path, "2026-12-02", 905.0, NOW, is_xhkg=True)
    assert cycle_view(tmp_path)["state"] == "HALF_SIZE"
    monkeypatch.setenv("FORCE_MANUAL_APPROVAL", "true")
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    get_settings.cache_clear()
    (tmp_path / "state" / "approval_session.json").write_text(
        json.dumps(
            {
                "accounts": [PIN],
                "baseline": [PIN],
                "port": 4002,
                "pin": PIN,
                "strategy_approval": "auto",
            }
        ),
        encoding="utf-8",
    )
    from auto_trading.cli_trade import describe_approval

    mode, _reason = describe_approval(tmp_path)
    assert mode == "manual"
    assert mode != APPROVER_AUTO


def test_t29_8_early_resume_is_local_and_synced_requests_are_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observe_nlv(tmp_path, "2026-11-02", 899.0, NOW, hwm=1000.0)
    (tmp_path / "state" / "account.json").write_text(
        json.dumps({"nlv": 910.0, "hwm": 1000.0}),
        encoding="utf-8",
    )
    (tmp_path / "state" / "r9_early.request").write_text("resume\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="synced"):
        resume_early(tmp_path, "2026-11-07", 910.0, NOW)
    with sqlite3.connect(tmp_path / "data" / "trading.db") as conn:
        audited = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'r9_early_refused'"
        ).fetchone()
    assert int(audited[0]) == 1
    assert cycle_view(tmp_path)["state"] == "HALTED_COOLDOWN"
    (tmp_path / "state" / "r9_early.request").unlink()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    main(
        [
            "--home",
            str(tmp_path),
            "resume",
            "--r9-early",
            "--local",
            "--confirm",
            "CONFIRM RESUME",
        ]
    )
    view = cycle_view(tmp_path)
    assert view["state"] == "HALF_SIZE"
    with sqlite3.connect(tmp_path / "data" / "trading.db") as conn:
        detail = conn.execute(
            "SELECT detail_json FROM hwm_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert json.loads(detail[0])["by"] == "user"
    assert not (tmp_path / "KILL_SWITCH").exists()


def test_t29_9_v12_hashes_match_and_published_output_stays_1_1(tmp_path: Path) -> None:
    assert config_hash(limits_v12()) == HASH_V12_ODD_LOT_OFF
    assert config_hash(limits_v12(odd_lot=True)) == HASH_V12_ODD_LOT_ON
    assert config_hash(limits_v12(cooldown_as_float=True)) == HASH_V12_COOLDOWN_AS_FLOAT
    assert config_hash(limits_v12(scope_inside=True)) == HASH_V12_SCOPE_INSIDE_LIMITS
    assert config_hash(EXPECTED_LIMITS) == HASH_ODD_LOT_OFF
    assert isinstance(limits_v12()["R9_cooldown_trading_days"], int)
    assert not isinstance(limits_v12()["R9_cooldown_trading_days"], bool)
    odd = dict(EXPECTED_LIMITS)
    odd["tw_odd_lot_enabled"] = True
    assert config_hash(odd) == HASH_ODD_LOT_ON
    floated = dict(EXPECTED_LIMITS)
    floated["R2_max_order_notional_base"] = 200000.0
    assert config_hash(floated) == HASH_R2_AS_FLOAT
    stuffed = dict(EXPECTED_LIMITS)
    stuffed["trading_scope"] = "paper_only"
    assert config_hash(stuffed) == HASH_SCOPE_INSIDE_LIMITS
    published = build_risk_limits(now=NOW, nlv=1_000_000, account=PIN)
    assert published["schema_version"] == "risk_limits/1.1"
    assert published["config_hash"] == HASH_ODD_LOT_OFF
    assert "R9_half_size_mult" not in published["limits"]
    assert "r9_cycle_state" not in published["accounts"][0]["breakers"]
    room = headroom_v12(10_000_000, size_mult=0.5, gross_used=6_000_000)
    assert room["order_max_notional_base"] == 100_000
    remaining = 10_000_000 * 0.95 * 0.5 - 6_000_000
    assert room["gross_exposure_remaining_base"] == pytest.approx(remaining)
    expected = json.loads(Path("config/risk_limits_expected.json").read_text(encoding="utf-8"))
    assert "R9_cooldown_trading_days" not in expected
    assert config_hash(expected) == HASH_ODD_LOT_OFF
    write_risk_limits(tmp_path, published)
    written = json.loads((tmp_path / "state" / "risk_limits.json").read_text(encoding="utf-8"))
    assert written["schema_version"] == "risk_limits/1.1"


def test_t29_10_catch_up_atomic_write_and_append_only(tmp_path: Path, monkeypatch) -> None:
    sessions = _xhkg_days(date(2026, 11, 2), 12)
    observe_nlv(tmp_path, sessions[0].isoformat(), 899.0, NOW, hwm=1000.0)
    close_risk_day(tmp_path, sessions[0].isoformat(), 905.0, NOW, is_xhkg=True)
    for day in sessions[1:8]:
        close_risk_day(tmp_path, day.isoformat(), 905.0, NOW, is_xhkg=True)
    assert cycle_view(tmp_path)["cooldown_days_done"] == 7
    catch_up(tmp_path, sessions[10], 905.0, NOW)
    assert cycle_view(tmp_path)["cooldown_days_done"] == 10
    pure = initial_state(1000.0)
    pure, _events = on_nlv(pure, sessions[0].isoformat(), 899.0)
    for day in sessions[:11]:
        pure, _events = on_risk_day_end(pure, day.isoformat(), True, 905.0, False)
    assert pure["cooldown_days_done"] == cycle_view(tmp_path)["cooldown_days_done"]
    before = cycle_path(tmp_path).read_bytes()

    def _killed(*_args, **_kwargs):
        raise OSError("killed")

    monkeypatch.setattr("auto_trading.risk.r9_state.os.replace", _killed)
    with pytest.raises(OSError, match="killed"):
        close_risk_day(tmp_path, sessions[11].isoformat(), 905.0, NOW, is_xhkg=True)
    assert cycle_path(tmp_path).read_bytes() == before
    saved = json.loads(before.decode("utf-8"))
    assert saved["content_sha256"]
    conn = sqlite3.connect(tmp_path / "data" / "trading.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM r9_cycle_events")
    finally:
        conn.close()


def test_t29_11_corrupt_state_halts_and_a_fresh_home_stays_active(tmp_path: Path) -> None:
    fresh = load_cycle(tmp_path / "fresh", now=NOW, account=PIN, hwm=1000.0)
    assert fresh["state"] == "ACTIVE"
    assert not list((tmp_path / "fresh" / "alerts").glob("**/*"))
    home = tmp_path / "bad"
    observe_nlv(home, "2026-11-02", 1000.0, NOW, hwm=1000.0)
    _case_missing(home / "missing")
    _case_parse(home / "parse")
    _case_hash(home / "hash")
    _case_state(home / "statekind")
    _case_seq(home / "seq")


def test_t29_12_full_cycle_writes_three_critical_alerts(tmp_path: Path) -> None:
    start = date(2026, 3, 2)
    observe_nlv(tmp_path, start.isoformat(), 899.0, NOW, hwm=1000.0)
    for offset in range(41):
        close_risk_day(
            tmp_path,
            (start + timedelta(days=offset)).isoformat(),
            905.0,
            NOW,
            is_xhkg=offset > 0,
        )
    assert cycle_view(tmp_path)["state"] == "ACTIVE"
    pending = list((tmp_path / "alerts").glob("*/*_exec_r9_*.json"))
    kinds = {json.loads(path.read_text(encoding="utf-8"))["kind"] for path in pending}
    assert kinds == {"r9_halt", "r9_resume_half_size", "r9_full_size"}
    for path in pending:
        body = json.loads(path.read_text(encoding="utf-8"))
        for key in ("schema_version", "alert_id", "kind", "message_zh", "resume_hint", "actions"):
            assert body[key] not in (None, "")
        assert body["schema_version"] == "exec_alert/1.0"
        assert body["actions"]["kill_switch_file"] is False
    outbox = list((tmp_path / "notifications" / "outbox").glob("*_exec_critical_*.json"))
    assert len(outbox) == 3
    assert "r9_resume_on=" in status_text(tmp_path, NOW)
    halted = tmp_path / "halted"
    observe_nlv(halted, "2026-11-02", 899.0, NOW, hwm=1000.0)
    assert f"r9_resume_on={expected_resume_date('2026-11-02')}" in status_text(halted, NOW)


def test_t29_13_daily_summary_carries_the_cycle_fields(tmp_path: Path) -> None:
    homes = {
        "ACTIVE": tmp_path / "active",
        "HALTED_COOLDOWN": tmp_path / "halted",
        "HALF_SIZE": tmp_path / "half",
    }
    load_cycle(homes["ACTIVE"], now=NOW, account=PIN, hwm=1000.0)
    observe_nlv(homes["HALTED_COOLDOWN"], "2026-11-02", 899.0, NOW, hwm=1000.0)
    observe_nlv(homes["HALF_SIZE"], "2026-11-02", 899.0, NOW, hwm=1000.0)
    resume_early(homes["HALF_SIZE"], "2026-11-03", 905.0, NOW)

    def _no_fetch(*_args, **_kwargs):
        raise ValueError("no fetch")

    for state, home in homes.items():
        payload = write_daily_summary(
            home,
            now=NOW,
            complete=True,
            pin=PIN,
            benchmark_get=_no_fetch,
        )
        validate_daily_summary(payload)
        assert payload["halts"]["r9_cycle_state"] == state
        if state == "ACTIVE":
            assert payload["halts"]["r9_cooldown_days_done"] is None
            assert payload["halts"]["r9_half_size_days_done"] is None
            assert payload["halts"]["r9_size_mult"] == 1.0
        elif state == "HALTED_COOLDOWN":
            assert payload["halts"]["r9_size_mult"] == 0.0
            assert payload["halts"]["r9_cooldown_days_done"] == 0
            assert payload["halts"]["r9_half_size_days_done"] is None
        else:
            assert payload["halts"]["r9_size_mult"] == 0.5
            assert payload["halts"]["r9_cooldown_days_done"] is None
            assert payload["halts"]["r9_half_size_days_done"] == 0
        json.loads(json.dumps(payload))


def test_t29_14_r29_does_not_stop_the_cooldown(tmp_path: Path) -> None:
    from auto_trading.gates import g4_passes

    observe_nlv(tmp_path, "2026-11-02", 899.0, NOW, hwm=1000.0)
    for index in range(1, 20):
        close_risk_day(tmp_path, f"2026-12-{index:02d}", 905.0, NOW, is_xhkg=True)
    assert cycle_view(tmp_path)["cooldown_days_done"] == 19
    (tmp_path / "state" / "breakers.json").write_text(
        json.dumps({"r29_active": True, "r30_active": False}),
        encoding="utf-8",
    )
    close_risk_day(tmp_path, "2026-12-20", 905.0, NOW, is_xhkg=True)
    assert cycle_view(tmp_path)["state"] == "HALF_SIZE"
    _limits(tmp_path)
    ctx = _ctx(tmp_path)
    with pytest.raises(RiskRejected, match="ANOMALY_BREAKER_ACTIVE"):
        RiskGate(ctx).allow(_buy())
    with sqlite3.connect(tmp_path / "data" / "trading.db") as conn:
        kinds = [row[0] for row in conn.execute("SELECT kind FROM anomaly_events")]
    assert "R9_HALT" in kinds
    assert g4_passes([{"kind": kind} for kind in kinds]) is False


def test_t29_15_and_17_half_size_mult_changes_backtest_caps() -> None:
    low = _half_sim(0.25)
    high = _half_sim(0.5)
    assert low.size_multiplier() == 0.25
    assert high.size_multiplier() == 0.5
    nlv = 10_000_000.0
    refused = fill_rejections(80_000, nlv, r1=low.order_caps()[0], r2=low.order_caps()[1])
    allowed = fill_rejections(80_000, nlv, r1=high.order_caps()[0], r2=high.order_caps()[1])
    assert refused
    assert allowed == []
    assert low.allow("SELL", "exit") is None
    assert low.allow("BUY", "entry") is None
    proposal = Proposal(
        "sig_trend", "0700.HK", "TENCENT", "ENTRY", 180_000, 10, 1, "HK", lot_size=1
    )
    full = allocate([proposal], nlv=nlv, r9_size_mult=1.0)
    half = allocate([proposal], nlv=nlv, r9_size_mult=0.5)
    assert full[0].qty > half[0].qty
    vector = next(item for item in _VECTORS["vectors"] if item["name"] == "full_cycle")
    _rows, cycles = replay_vector(vector)
    assert cycles[0]["trigger_date"]
    assert cycles[0]["resume_date"]
    assert cycles[0]["full_size_date"]
    assert cycles[0]["exposure_cap"] == pytest.approx(0.475)


def test_t29_16_backtest_replays_the_shared_vectors() -> None:
    for vector in _VECTORS["vectors"]:
        rows, _cycles = replay_vector(vector)
        expects = [record["expect"] for record in vector["steps"]]
        assert rows == expects


def test_t29_18_default_scenario_is_cooldown_half_size() -> None:
    assert RiskSimulator(hwm=1).scenario == "cooldown_half_size"
    source = Path("src/auto_trading/backtest/engine.py").read_text(encoding="utf-8")
    assert 'resume: str = "cooldown_half_size"' in source
    assert "manual_no_reset" in source
    sensitivity = RiskSimulator(hwm=100, scenario="manual_no_reset")
    sensitivity.mark(89, new_risk_day=False, trading_day=True, session_id="d0")
    assert sensitivity.allow("SELL", "exit") == "R9"


def test_t29_19_unconfirmed_v12_stays_conservative() -> None:
    limits = limits_v12()
    payload = {
        "schema_version": "risk_limits/1.2",
        "limits": limits,
        "config_hash": config_hash(limits),
        "trading_scope": "paper_only",
        "generated_at": NOW.isoformat(),
        "stale_after_sec": 900,
    }
    status = assess_payload(payload, EXPECTED_LIMITS, now=NOW)
    assert status.entries_allowed is False
    assert status.reason == "unknown_schema"
    kept = filter_for_risk(
        [
            {"intent_type": "ENTRY"},
            {"intent_type": "ADD"},
            {"intent_type": "REDUCE"},
            {"intent_type": "EXIT"},
        ],
        entries_allowed=status.entries_allowed,
    )
    assert [item["intent_type"] for item in kept] == ["REDUCE", "EXIT"]


def _drive(home: Path, vector: dict) -> dict:
    document = load_cycle(home, now=NOW, account=PIN, hwm=float(vector["initial_hwm"]))
    del document
    for record in vector["steps"]:
        step = record["step"]
        if step["op"] == "nlv":
            observe_nlv(home, step["risk_date"], step["nlv"], NOW, hwm=float(vector["initial_hwm"]))
        elif step["op"] == "day_end":
            close_risk_day(
                home,
                step["risk_date"],
                step["nlv"],
                NOW,
                is_xhkg=bool(step["xhkg"]),
                blocked=bool(step.get("blocked", False)),
            )
        else:
            resume_early(home, step["risk_date"], step["nlv"], NOW)
    loaded, _problem = None, None
    from auto_trading.risk.r9_state import _read

    loaded, _problem = _read(home)
    assert loaded is not None
    return loaded


def _half_sim(mult: float) -> RiskSimulator:
    sim = RiskSimulator(hwm=10_000_000, limits={"R9_half_size_mult": mult})
    sim.mark(8_990_000, new_risk_day=False, trading_day=True, session_id="2026-11-02")
    sim.resume_early("2026-11-03", 9_050_000)
    return sim


def _case_missing(home: Path) -> None:
    TradeJournal(home)
    with sqlite3.connect(home / "data" / "trading.db") as conn:
        conn.execute(
            """
            INSERT INTO orders (order_id, intent_id, seq, symbol, state, protective)
            VALUES ('ord', 'ord', 1, 'AAPL', 'SUBMITTED', 0)
            """
        )
    document = load_cycle(home, now=NOW, account=PIN, hwm=1000.0, risk_date="2026-11-02")
    assert document["state"] == "HALTED_COOLDOWN"
    assert document["cooldown_days_done"] == 0
    assert list((home / "alerts").glob("**/*r9_state_corrupt*"))


def _case_parse(home: Path) -> None:
    load_cycle(home, now=NOW, account=PIN, hwm=1000.0)
    cycle_path(home).write_text("{", encoding="utf-8")
    document = load_cycle(home, now=NOW, account=PIN, hwm=1000.0, risk_date="2026-11-02")
    assert document["state"] == "HALTED_COOLDOWN"
    assert list((home / "alerts").glob("**/*r9_state_corrupt*"))


def _case_hash(home: Path) -> None:
    load_cycle(home, now=NOW, account=PIN, hwm=1000.0)
    payload = json.loads(cycle_path(home).read_text(encoding="utf-8"))
    payload["content_sha256"] = "sha256:" + "ab" * 32
    cycle_path(home).write_text(json.dumps(payload), encoding="utf-8")
    document = load_cycle(home, now=NOW, account=PIN, hwm=1000.0, risk_date="2026-11-02")
    assert document["state"] == "HALTED_COOLDOWN"


def _case_state(home: Path) -> None:
    load_cycle(home, now=NOW, account=PIN, hwm=1000.0)
    payload = json.loads(cycle_path(home).read_text(encoding="utf-8"))
    payload["state"] = "NOPE"
    cycle_path(home).write_text(json.dumps(_seal(payload)), encoding="utf-8")
    document = load_cycle(home, now=NOW, account=PIN, hwm=1000.0, risk_date="2026-11-02")
    assert document["state"] == "HALTED_COOLDOWN"


def _case_seq(home: Path) -> None:
    load_cycle(home, now=NOW, account=PIN, hwm=1000.0)
    TradeJournal(home).append_r9_event(
        account=PIN,
        cycle_no=0,
        from_state="ACTIVE",
        to_state="ACTIVE",
        kind="note",
        risk_date="2026-11-02",
        nlv_base=1,
        old_hwm_base=1,
        new_hwm_base=1,
        by=None,
        seq=9,
        when=NOW,
    )
    payload = json.loads(cycle_path(home).read_text(encoding="utf-8"))
    payload["seq"] = 1
    cycle_path(home).write_text(json.dumps(_seal(payload)), encoding="utf-8")
    document = load_cycle(home, now=NOW, account=PIN, hwm=1000.0, risk_date="2026-11-02")
    assert document["state"] == "HALTED_COOLDOWN"
    assert int(document["seq"]) > 9


def test_t29_12_status_after_halt_names_the_resume_session(tmp_path: Path) -> None:
    halt_r9(tmp_path)
    assert not (tmp_path / "KILL_SWITCH").exists()
    assert "r9_state=HALTED_COOLDOWN" in status_text(tmp_path, NOW)
