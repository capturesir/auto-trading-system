"""Review fixes for R9 day counting and the resident services."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.execution.approval_mode import ApprovalSession
from auto_trading.execution.services import (
    SYNC_INTERVAL_SEC,
    account_once,
    oms_once,
    read_only_api,
    recon_once,
    submit_once,
    sync_argv,
    watchdog_once,
)
from auto_trading.market.calendar import is_session
from auto_trading.paper_only import scan_tree
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.risk.gate import GateContext, Quote
from auto_trading.risk.r9_state import close_risk_day, cycle_view, observe_nlv

M = ZoneInfo("Asia/Macau")
HKT = ZoneInfo("Asia/Hong_Kong")
PIN = "DUR243736"
ROOT = Path(__file__).resolve().parents[1]
GATE_NOW = datetime(2026, 10, 2, 22, 0, tzinfo=M)


def _acct(home: Path, nlv: float, hwm: float) -> None:
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "account.json").write_text(
        json.dumps({"nlv": nlv, "hwm": hwm, "day_start_nlv": nlv}),
        encoding="utf-8",
    )


def _trigger(home: Path) -> None:
    moment = datetime(2026, 10, 9, 10, 0, tzinfo=M)
    _acct(home, 1_000_000, 1_000_000)
    oms_once(home, moment.astimezone(UTC))
    _acct(home, 890_000, 1_000_000)
    oms_once(home, (moment + timedelta(minutes=1)).astimezone(UTC))


def test_blocker4_same_morning_counts_one_cooldown_day(tmp_path: Path) -> None:
    _trigger(tmp_path)
    assert cycle_view(tmp_path)["state"] == "HALTED_COOLDOWN"
    start = datetime(2026, 10, 13, 8, 0, 10, tzinfo=M)
    seen: list[int] = []
    for index in range(60):
        now = start + timedelta(seconds=10 * index)
        oms_once(tmp_path, now.astimezone(UTC))
        view = cycle_view(tmp_path)
        if index in (0, 1, 5, 10, 19, 20, 30, 59):
            seen.append(int(view["cooldown_days_done"]))
            assert view["state"] == "HALTED_COOLDOWN"
            assert view["half_size_days_done"] == 0
    assert seen == [1, 1, 1, 1, 1, 1, 1, 1]


def test_blocker4_close_risk_day_is_idempotent(tmp_path: Path) -> None:
    observe_nlv(
        tmp_path,
        "2026-10-09",
        890_000,
        datetime(2026, 10, 9, 2, 0, tzinfo=UTC),
        hwm=1_000_000,
    )
    moment = datetime(2026, 10, 13, 0, 5, tzinfo=UTC)
    close_risk_day(tmp_path, "2026-10-12", 890_000, moment, is_xhkg=True)
    close_risk_day(tmp_path, "2026-10-12", 890_000, moment, is_xhkg=True)
    assert cycle_view(tmp_path)["cooldown_days_done"] == 1


def test_blocker4_twenty_sessions_reach_half_size(tmp_path: Path) -> None:
    _trigger(tmp_path)
    sessions: list[datetime] = []
    day = datetime(2026, 10, 10, tzinfo=M).date()
    while len(sessions) < 20:
        if is_session("XHKG", day):
            sessions.append(datetime(day.year, day.month, day.day, tzinfo=M))
        day += timedelta(days=1)
    for index, session in enumerate(sessions, start=1):
        morning = session + timedelta(days=1, hours=8, seconds=10)
        for _tick in range(3):
            oms_once(tmp_path, morning.astimezone(UTC))
        view = cycle_view(tmp_path)
        if index < 20:
            assert view["state"] == "HALTED_COOLDOWN"
            assert view["cooldown_days_done"] == index
        else:
            assert view["state"] == "HALF_SIZE"
            assert view["cooldown_days_done"] == 20
            assert view["half_size_days_done"] == 0


def test_account_once_writes_nlv_and_positions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    monkeypatch.setenv("IB_PORT", "4002")
    result = account_once(
        tmp_path,
        datetime(2026, 10, 8, 2, 0, tzinfo=UTC),
        snapshot={
            "accounts": [PIN],
            "nlv": 1_000_000.0,
            "positions": [{"symbol": "AAPL", "qty": 2, "avg": 10}],
            "open_orders": [],
        },
    )
    assert result["wrote"] is True
    body = json.loads((tmp_path / "state" / "account.json").read_text(encoding="utf-8"))
    assert body["nlv"] == 1_000_000.0
    assert body["positions"][0]["qty"] == 2
    gateway = json.loads((tmp_path / "state" / "gateway.json").read_text(encoding="utf-8"))
    assert gateway["logged_in"] is True
    assert gateway["accounts"] == [PIN]


def test_account_once_refuses_a_non_paper_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    monkeypatch.setenv("IB_PORT", "4002")
    result = account_once(
        tmp_path,
        datetime(2026, 10, 8, 2, 0, tzinfo=UTC),
        snapshot={"accounts": ["U1234567"], "nlv": 50.0, "positions": [], "open_orders": []},
    )
    assert result["wrote"] is False
    assert (tmp_path / "KILL_SWITCH").is_file()
    assert not (tmp_path / "state" / "account.json").is_file()


def test_recon_compares_broker_snapshot_with_the_journal(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "account.json").write_text(
        json.dumps(
            {
                "positions": [{"symbol": "AAPL", "qty": 5}],
                "open_orders": [{"order_id": "missing", "symbol": "AAPL"}],
            }
        ),
        encoding="utf-8",
    )
    journal = TradeJournal(tmp_path)
    journal.apply_strategy_fill(
        account=PIN,
        strategy_id="sig_trend",
        con_id=1,
        ticker="AAPL",
        side="BUY",
        qty=4,
        price=100,
        root_intent_id="entry-1",
        when=datetime(2026, 10, 8, tzinfo=UTC),
    )
    payload = recon_once(tmp_path, datetime(2026, 10, 8, 2, 0, tzinfo=UTC))
    assert payload["freeze"] is True
    kinds = {row["kind"] for row in payload["breaks"]}
    assert "QTY" in kinds
    assert "ORDER" in kinds
    matched = recon_once(
        tmp_path,
        datetime(2026, 10, 8, 2, 1, tzinfo=UTC),
        broker_qty={"AAPL": 4},
        journal_qty={"AAPL": 4},
        broker_orders=[],
    )
    assert matched["freeze"] is False


def test_watchdog_checks_gateway_r0_sqlite_disk_and_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    monkeypatch.setattr(
        "auto_trading.execution.services.gateway_probe",
        lambda home, now: {"logged_in": False, "accounts": [], "detail": "down"},
    )
    monkeypatch.setattr("auto_trading.execution.services.disk_free_bytes", lambda home: 10)
    monkeypatch.setattr("auto_trading.execution.services.clock_skew_sec", lambda now: 120.0)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "trading.db").write_bytes(b"not a database")
    assert watchdog_once(tmp_path, datetime(2026, 10, 8, 2, 0, tzinfo=UTC)) is True
    report = json.loads((tmp_path / "state" / "watchdog.json").read_text(encoding="utf-8"))
    assert "gateway_login" in report["issues"]
    assert "r0_account" in report["issues"]
    assert "sqlite" in report["issues"]
    assert "disk" in report["issues"]
    assert "clock_skew" in report["issues"]
    unit = (ROOT / "vps" / "deploy" / "trading-watchdog.service").read_text(encoding="utf-8")
    assert "TRADING_GATEWAY_CONNECT=1" in unit
    assert "User=trader" in unit
    assert "Restart=always" in unit


def _session() -> ApprovalSession:
    return ApprovalSession(
        read_accounts=lambda: (PIN,),
        port=4002,
        pin=PIN,
        baseline=(PIN,),
        kill_switch_present=lambda: False,
        r0_violation=lambda: False,
        force_manual=lambda: False,
        strategy_approval=lambda: "auto",
    )


def _intent() -> OrderIntent:
    return OrderIntent(
        client_id="cid-e2",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=10,
        limit_price=100.0,
        currency="HKD",
        market="US",
        account=PIN,
    )


def _ctx(home: Path) -> GateContext:
    from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits

    write_risk_limits(home, build_risk_limits(now=GATE_NOW, nlv=1_000_000, account=PIN))
    return GateContext(
        now=GATE_NOW,
        trading_home=home,
        expected_account=PIN,
        accounts=(PIN,),
        baseline_accounts=(PIN,),
        nlv=1_000_000,
        cash=1_000_000,
        hwm=1_000_000,
        day_start_nlv=1_000_000,
        quotes={
            "AAPL": Quote(last=100.0, as_of=GATE_NOW, received_at=GATE_NOW, market_data_type=1)
        },
        fx_to_base={"HKD": 1.0, "USD": 1.0},
    )


def test_submit_does_nothing_while_readonly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("IB_READ_ONLY_API", raising=False)
    assert read_only_api() is True
    placed: list[OrderIntent] = []
    result = submit_once(
        tmp_path,
        GATE_NOW,
        intents=[_intent()],
        ctx=_ctx(tmp_path),
        session=_session(),
        place=placed.append,
    )
    assert result["status"] == "readonly"
    assert placed == []
    unit = (ROOT / "vps" / "deploy" / "trading-submit.service").read_text(encoding="utf-8")
    assert "IB_READ_ONLY_API=yes" in unit


def test_submit_sends_only_after_e2(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    placed: list[OrderIntent] = []
    result = submit_once(
        tmp_path,
        GATE_NOW,
        intents=[_intent()],
        ctx=_ctx(tmp_path),
        session=_session(),
        place=placed.append,
        readonly=False,
    )
    assert result["sent"] == 1
    assert placed[0].symbol == "AAPL"
    manual = ApprovalSession(
        read_accounts=lambda: (PIN,),
        port=4002,
        pin=PIN,
        baseline=(PIN,),
        kill_switch_present=lambda: False,
        r0_violation=lambda: False,
        force_manual=lambda: True,
        strategy_approval=lambda: "auto",
    )
    held: list[OrderIntent] = []
    blocked = submit_once(
        tmp_path,
        GATE_NOW,
        intents=[_intent()],
        ctx=_ctx(tmp_path),
        session=manual,
        place=held.append,
        readonly=False,
    )
    assert blocked["sent"] == 0
    assert held == []


def test_sync_direction_and_interval() -> None:
    remote = "box.example:/srv/trading/"
    outbound = sync_argv(ROOT, remote, direction="out")
    inbound = sync_argv(ROOT, remote, direction="in")
    assert "--delete" not in outbound
    assert "--delete" not in inbound
    assert "--update" in outbound
    assert "--update" in inbound
    assert outbound[-2].endswith("/") and "box.example" not in outbound[-2]
    assert inbound[-2].startswith("box.example:")
    assert inbound[-1].endswith("/")
    manifest = (ROOT / "vps" / "deploy" / "sync_vps_to_box.txt").read_text(encoding="utf-8")
    assert "strategies/approved" not in manifest
    assert "logs/" in manifest
    timer = (ROOT / "vps" / "deploy" / "trading-sync.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=30s" in timer
    assert SYNC_INTERVAL_SEC == 30
    daemons = (
        "trading-account.service",
        "trading-oms.service",
        "trading-recon.service",
        "trading-watchdog.service",
        "trading-submit.service",
    )
    for name in daemons:
        text = (ROOT / "vps" / "deploy" / name).read_text(encoding="utf-8")
        assert "User=trader" in text
        assert "Restart=always" in text
        assert "PASSWORD" not in text
    readme = (ROOT / "vps" / "deploy" / "README.md").read_text(encoding="utf-8")
    assert "trading-main" in readme
    assert "30" in readme


def test_n1_scan_covers_extensionless_cfg_conf_and_build_dirs(tmp_path: Path) -> None:
    stem = "liv" + "e"
    bad = "is_" + stem + " = True\n"
    (tmp_path / "Dockerfile").write_text(bad, encoding="utf-8")
    (tmp_path / "app.cfg").write_text(bad, encoding="utf-8")
    (tmp_path / "app.conf").write_text(bad, encoding="utf-8")
    (tmp_path / "book.sql").write_text(bad, encoding="utf-8")
    nested = tmp_path / "src" / "build"
    nested.mkdir(parents=True)
    (nested / "y.py").write_text(bad, encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "z.py").write_text(bad, encoding="utf-8")
    found = "\n".join(scan_tree(tmp_path))
    assert "Dockerfile" in found
    assert "app.cfg" in found
    assert "app.conf" in found
    assert "book.sql" in found
    assert "y.py" in found
    assert "z.py" in found


def test_n2_split_stem_is_limited_to_reviewed_files(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_v08_ci", ROOT / "scripts" / "check_v08_ci.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    split_stem_offenders = module.split_stem_offenders
    assert split_stem_offenders(ROOT) == []
    (tmp_path / "extra.py").write_text('STEM = "liv" + "e"\n', encoding="utf-8")
    assert split_stem_offenders(tmp_path) == ["extra.py"]


def test_n3_loop_does_not_rebuild_or_summarize_and_isolates_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    def _scan(*_args, **_kwargs):
        called.append("scan")

    monkeypatch.setattr("auto_trading.execution.services.observe_nlv", _boom)
    monkeypatch.setattr("auto_trading.execution.services.scan_once", _scan)
    monkeypatch.setattr(
        "auto_trading.universe.rebuild.run_rebuild",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rebuild")),
    )
    _acct(tmp_path, 1_000.0, 1_000.0)
    moment = datetime(2026, 10, 2, 6, 5, tzinfo=HKT)
    result = oms_once(tmp_path, moment.astimezone(UTC))
    assert result["rebuilt"] is False
    assert result["summarized"] is False
    assert "r9" in result["errors"]
    assert called == ["scan"]
    assert (tmp_path / "state" / "heartbeat.json").is_file()
    summary = (ROOT / "vps" / "deploy" / "trading-summary.timer").read_text(encoding="utf-8")
    pool = (ROOT / "vps" / "deploy" / "pool-rebuild.timer").read_text(encoding="utf-8")
    assert "trading-summary.service" in summary
    assert "pool-rebuild.service" in pool
