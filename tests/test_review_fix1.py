"""Review fixes for the paper scan, the R9 latch, and the resident services."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.execution.risk_state import build_risk_limits
from auto_trading.execution.services import (
    OMS_INTERVAL_SEC,
    main_once,
    oms_once,
    recon_once,
    sync_argv,
)
from auto_trading.paper_only import account_accepted, line_allowed, port_accepted, scan_tree
from auto_trading.reports.daily_summary import write_daily_summary
from auto_trading.risk.gate import GateContext, Held, Quote, RiskGate, RiskRejected
from auto_trading.risk.r9_state import cycle_view, observe_nlv

HKT = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 8, 6, 5, tzinfo=HKT)
ROOT = Path(__file__).resolve().parents[1]


def test_blocker1_exact_allowlist_rejects_the_three_fixtures(tmp_path: Path) -> None:
    stem = "liv" + "e"
    camel = "use" + stem[:1].upper() + stem[1:] + "Account()"
    lines = (
        "is_" + stem + " = True",
        stem.upper() + "_ACCOUNT = 'U123'",
        camel,
    )
    for line in lines:
        assert line_allowed(line) is False
        assert line_allowed(line + "  # refused: paper_only") is False
    assert line_allowed("min_upside_" + stem + "_pct = 0")
    assert account_accepted("DUR243736", "DUR243736")
    assert account_accepted("DU1234567", "DUR243736") is False
    assert account_accepted("U1234567", "DUR243736") is False
    assert port_accepted(4002) and port_accepted(7497)
    assert port_accepted(int("40" + "01")) is False
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "book.csv").write_text(lines[0] + "\n", encoding="utf-8")
    (tmp_path / "job.timer").write_text(lines[1] + "\n", encoding="utf-8")
    (tmp_path / "call.py").write_text(lines[2] + "\n", encoding="utf-8")
    found = "\n".join(scan_tree(tmp_path))
    assert ("is_" + stem) in found
    assert (stem.upper() + "_ACCOUNT") in found
    assert camel.split("(")[0] in found


def test_blocker3_units_and_main_loop_call_the_schedules(tmp_path: Path, monkeypatch) -> None:
    deploy = ROOT / "vps" / "deploy"
    for name in (
        "trading-oms.service",
        "trading-recon.service",
        "trading-watchdog.service",
        "trading-sync.service",
        "trading-sync.timer",
        "trading-main.service",
        "trading-summary.service",
        "trading-summary.timer",
        "pool-rebuild.service",
        "pool-rebuild.timer",
    ):
        text = (deploy / name).read_text(encoding="utf-8")
        assert "ExecStart=" in text or "Unit=" in text
    assert "python -m auto_trading.execution oms" in (deploy / "trading-oms.service").read_text()
    assert "python -m auto_trading.execution main" in (deploy / "trading-main.service").read_text()
    assert OMS_INTERVAL_SEC == 10
    called: list[str] = []

    def _rebuild(home, *, today, now, **kwargs):
        del home, today, now, kwargs
        called.append("rebuild")

        class Outcome:
            ran = True
            ok = True

        return Outcome()

    monkeypatch.setattr("auto_trading.universe.rebuild.run_rebuild", _rebuild)
    monkeypatch.setattr(
        "auto_trading.execution.services._rebuild_due",
        lambda home, now: True,
    )
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "account.json").write_text(
        json.dumps({"nlv": 1000.0, "hwm": 1000.0, "day_start_nlv": 1000.0}) + "\n",
        encoding="utf-8",
    )
    result = main_once(tmp_path, NOW)
    assert result["watchdog"] is True
    assert result["oms"]["summarized"] is True
    assert result["oms"]["rebuilt"] is True
    assert called == ["rebuild"]
    assert (tmp_path / "state" / "r9_cycle.json").is_file()
    assert (tmp_path / "reports" / "daily").is_dir()
    assert (tmp_path / "state" / "heartbeat.json").is_file()
    assert (tmp_path / "state" / "recon.json").is_file()
    later = NOW + timedelta(hours=3)
    observe_nlv(tmp_path, "2026-10-07", 899.0, later, hwm=1000.0)
    rolled = oms_once(tmp_path, later)
    assert rolled["rolled"] is True
    view = cycle_view(tmp_path)
    assert view is not None and view["state"] == "HALTED_COOLDOWN"


def test_s1_resting_exit_survives_the_halt(tmp_path: Path) -> None:
    from auto_trading.portfolio.journal import TradeJournal

    observe_nlv(tmp_path, "2026-10-07", 1000.0, NOW, hwm=1000.0)
    journal = TradeJournal(tmp_path)
    journal.create_order(
        order_id="exit-rest",
        intent_id="exit-rest",
        seq=1,
        symbol="AAPL",
        intent_type="EXIT",
    )
    journal.set_order_state("exit-rest", "SUBMITTED", when=NOW)
    observe_nlv(tmp_path, "2026-10-07", 899.0, NOW)
    assert journal.order_state("exit-rest") == "SUBMITTED"
    runtime = json.loads((tmp_path / "state" / "strategy_runtime.json").read_text())
    assert runtime["status"] == "exit_only"


def test_s2_published_limits_follow_half_size_without_changing_schema(tmp_path: Path) -> None:
    observe_nlv(tmp_path, "2026-10-07", 10_000_000.0, NOW, hwm=10_000_000.0)
    observe_nlv(tmp_path, "2026-10-07", 8_990_000.0, NOW)
    cooled = build_risk_limits(
        now=NOW,
        nlv=9_600_000.0,
        hwm=10_000_000.0,
        account="DUR243736",
        trading_home=tmp_path,
    )
    assert cooled["schema_version"] == "risk_limits/1.1"
    assert cooled["accounts"][0]["breakers"]["r9_state"] == "halt"
    document = json.loads((tmp_path / "state" / "r9_cycle.json").read_text())
    document["state"] = "HALF_SIZE"
    document["half_size_days_done"] = 1
    from auto_trading.risk.r9_cycle import size_mult
    from auto_trading.risk.r9_state import write_cycle

    document["content_sha256"] = "pending"
    # Reseal through the writer used by the runtime.
    from auto_trading.risk import r9_state as state

    sealed = dict(document)
    sealed.pop("content_sha256", None)
    state._seal(sealed)
    write_cycle(tmp_path, sealed)
    assert size_mult(sealed) == 0.5
    half = build_risk_limits(
        now=NOW,
        nlv=10_000_000.0,
        hwm=10_000_000.0,
        account="DUR243736",
        trading_home=tmp_path,
    )
    assert half["schema_version"] == "risk_limits/1.1"
    assert half["accounts"][0]["headroom"]["order_max_notional_base"] == 100_000
    assert "R9_half_size_mult" not in half["limits"]


def test_s3_deleting_the_cycle_file_keeps_the_cooldown(tmp_path: Path) -> None:
    observe_nlv(tmp_path, "2026-10-07", 1000.0, NOW, hwm=1000.0)
    observe_nlv(tmp_path, "2026-10-07", 899.0, NOW)
    (tmp_path / "state" / "r9_cycle.json").unlink()
    view = cycle_view(tmp_path)
    assert view is not None and view["state"] == "HALTED_COOLDOWN"
    ctx = GateContext(
        now=datetime(2026, 10, 2, 22, 0, tzinfo=HKT),
        trading_home=tmp_path,
        expected_account="DU1234567",
        accounts=("DU1234567",),
        nlv=1000.0,
        cash=1000.0,
        hwm=1000.0,
        day_start_nlv=1000.0,
        quotes={"AAPL": Quote(last=100, as_of=NOW, received_at=NOW, market_data_type=1)},
        fx_to_base={"HKD": 1.0},
        positions=[Held("AAPL", 5, 400)],
    )
    from auto_trading.execution.risk_state import write_risk_limits

    write_risk_limits(
        tmp_path,
        build_risk_limits(now=NOW, nlv=1000.0, account="DU1234567", trading_home=tmp_path),
    )
    with pytest.raises(RiskRejected, match="R9_HALTED_COOLDOWN"):
        RiskGate(ctx).allow(
            OrderIntent(
                client_id="cid",
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=1,
                limit_price=100.0,
                currency="HKD",
                market="US",
            )
        )


def test_s4_missing_home_still_writes_the_kill_switch(tmp_path, monkeypatch) -> None:
    from auto_trading.execution.ibkr import IbkrBroker
    from auto_trading.execution.r0 import PaperAccountError

    class Client:
        def __init__(self) -> None:
            self.disconnected = False

        async def managed_accounts(self) -> list[str]:
            return ["U1234567"]

        def disconnect(self) -> None:
            self.disconnected = True

    monkeypatch.setenv("TRADING_HOME", str(tmp_path))
    broker = IbkrBroker(Client(), expected_account="DUR243736", trading_home=None)

    async def place() -> None:
        await broker.place(
            OrderIntent(
                client_id="cid",
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=1,
                limit_price=100.0,
                account="DUR243736",
            )
        )

    with pytest.raises(PaperAccountError):
        import asyncio

        asyncio.run(place())
    assert (tmp_path / "KILL_SWITCH").is_file()


def test_s5_approver_constant_stays_in_its_module() -> None:
    hits: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "APPROVER_AUTO" in line:
                hits.append(f"{path.name}:{number}")
    assert hits
    assert all(item.startswith("approval_mode.py:") for item in hits)


def test_s6_benchmark_exception_still_writes_the_summary(tmp_path: Path, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise KeyError("price")

    monkeypatch.setattr("auto_trading.reports.daily_summary.build_benchmarks", boom)
    payload = write_daily_summary(
        tmp_path,
        now=NOW,
        complete=True,
        pin="DUR243736",
    )
    assert payload["benchmarks"]["status"] == "n/a"
    assert payload["benchmarks"]["na_reason"] == "benchmark_error"
    assert list((tmp_path / "reports" / "daily").glob("summary_*.json"))


def test_sync_plan_has_no_delete() -> None:
    argv = sync_argv(ROOT, "box.example:/srv/trading/", direction="out")
    assert "--delete" not in argv
    assert any("sync_vps_to_box.txt" in part for part in argv)


def test_recon_freeze_reaches_the_gate(tmp_path: Path) -> None:
    from auto_trading.execution.risk_state import write_risk_limits

    write_risk_limits(tmp_path, build_risk_limits(now=NOW, nlv=1_000_000, account="DU1234567"))
    recon_once(tmp_path, NOW, broker_qty={"AAPL": 5}, journal_qty={"AAPL": 4})
    ctx = GateContext(
        now=datetime(2026, 10, 2, 22, 0, tzinfo=HKT),
        trading_home=tmp_path,
        expected_account="DU1234567",
        accounts=("DU1234567",),
        nlv=1_000_000,
        cash=1_000_000,
        hwm=1_000_000,
        day_start_nlv=1_000_000,
        quotes={"AAPL": Quote(last=100, as_of=NOW, received_at=NOW, market_data_type=1)},
        fx_to_base={"HKD": 1.0},
        positions=[Held("AAPL", 4, 400)],
    )
    with pytest.raises(RiskRejected, match="R17"):
        RiskGate(ctx).allow(
            OrderIntent(
                client_id="cid",
                symbol="AAPL",
                side=OrderSide.SELL,
                quantity=1,
                limit_price=100.0,
                currency="HKD",
                market="US",
                intent_type="EXIT",
                parent_intent_id="entry-1",
            )
        )
