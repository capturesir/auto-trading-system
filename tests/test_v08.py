"""v0.8 approval, critical alerts, daily summary, R29, and R30."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_trading.alerts.critical import write_critical_event
from auto_trading.alerts.forwarder import (
    ALERT_SCAN_INTERVAL_SEC,
    scan_once,
    status_warning,
    unforwarded_critical,
)
from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.cli_trade import (
    describe_approval,
    halt_r9,
    main,
    resume_r29,
    status_text,
    stop,
)
from auto_trading.config import (
    assert_startup_switches,
    get_settings,
    manual_forced,
)
from auto_trading.execution.approval_mode import (
    APPROVER_AUTO,
    ApprovalSession,
    decide_approval_mode,
    record_auto_approval,
)
from auto_trading.execution.approvals import (
    ApprovalError,
    approve,
    hash12,
    queue_order,
)
from auto_trading.execution.breakers import (
    ANOMALY_BREAKER_COUNT,
    clear_r29,
    flags,
    maybe_auto_clear_r29,
    note_execution_anomaly,
    refresh_r30,
    set_health_pinger,
    summary_overdue,
)
from auto_trading.execution.order_flow import (
    AUTO_SUBMIT_WINDOW_SEC,
    expire_submit_window,
    run_entry_flow,
)
from auto_trading.execution.r0 import latch_kill_switch
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.reports.daily_summary import validate_daily_summary, write_daily_summary
from auto_trading.risk.gate import GateContext, Held, Quote, RiskGate, RiskRejected
from auto_trading.risk.limits import (
    EXPECTED_LIMITS,
    HASH_ODD_LOT_OFF,
    HASH_ODD_LOT_ON,
    HASH_R2_AS_FLOAT,
    HASH_SCOPE_INSIDE_LIMITS,
    config_hash,
)

HKT = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 2, 22, 0, tzinfo=HKT)
PIN = "DUR243736"


def _session(
    accounts: tuple[str, ...] = (PIN,),
    *,
    port: int = 4002,
    pin: str = PIN,
    baseline: tuple[str, ...] | None = None,
    kill: bool = False,
    r0: bool = False,
    force: bool = False,
    strategy: str = "auto",
    now: datetime | None = None,
    reader: object | None = None,
) -> ApprovalSession:
    current = {"accounts": accounts}

    def read() -> tuple[str, ...]:
        if reader is not None:
            return tuple(reader())  # type: ignore[operator]
        return tuple(current["accounts"])

    return ApprovalSession(
        read_accounts=read,
        port=port,
        pin=pin,
        baseline=baseline if baseline is not None else accounts,
        kill_switch_present=lambda: kill,
        r0_violation=lambda: r0,
        force_manual=lambda: force,
        strategy_approval=lambda: strategy,
        now=None if now is None else (lambda: now),
    )


def _order(account: str = PIN, approval: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(account=account, execution_approval=approval)


def _intent(**kwargs: object) -> OrderIntent:
    payload: dict = {
        "client_id": "cid-1",
        "symbol": "AAPL",
        "side": OrderSide.BUY,
        "quantity": 10,
        "limit_price": 100.0,
        "currency": "HKD",
        "market": "US",
        "account": PIN,
        "intent_id": "intent-1",
        "intent_type": "ENTRY",
    }
    payload.update(kwargs)
    return OrderIntent(**payload)


def _ctx(home: Path, **kwargs: object) -> GateContext:
    if not (home / "state" / "risk_limits.json").is_file():
        write_risk_limits(home, build_risk_limits(now=NOW, nlv=1_000_000, account=PIN))
    payload: dict = {
        "now": NOW,
        "trading_home": home,
        "accounts": (PIN,),
        "expected_account": PIN,
        "quotes": {"AAPL": Quote(last=100.0, as_of=NOW, received_at=NOW, market_data_type=1)},
        "fx_to_base": {"HKD": 1.0, "USD": 1.0},
    }
    payload.update(kwargs)
    return GateContext(**payload)


def _set_price(ctx: GateContext, intent: OrderIntent, price: float) -> None:
    intent.limit_price = price
    ctx.quotes["AAPL"] = Quote(last=price, as_of=NOW, received_at=NOW, market_data_type=1)


def test_each_failed_r0_check_stays_manual() -> None:
    assert decide_approval_mode(_order(), _session()) == APPROVER_AUTO
    foreign = _session(accounts=("U1234567",), pin="U1234567", baseline=("U1234567",))
    assert decide_approval_mode(_order("U1234567"), foreign) == "manual"
    refused_gateway = int("400" + "1")  # refused: paper_only
    refused_tws = int("749" + "6")  # refused: paper_only
    assert decide_approval_mode(_order(), _session(port=refused_gateway)) == "manual"
    assert decide_approval_mode(_order(), _session(port=refused_tws)) == "manual"
    assert decide_approval_mode(_order(), _session(accounts=(PIN, "DU1234567"))) == "manual"
    assert decide_approval_mode(_order(), _session(baseline=("DU1234567",))) == "manual"
    assert decide_approval_mode(_order(), _session(pin="")) == "manual"
    assert decide_approval_mode(_order(), _session(pin="DU1234567")) == "manual"
    assert decide_approval_mode(_order("DU1234567"), _session()) == "manual"
    assert decide_approval_mode(_order(), _session(kill=True)) == "manual"
    assert decide_approval_mode(_order(), _session(r0=True)) == "manual"
    assert decide_approval_mode(_order(), _session(force=True)) == "manual"
    assert decide_approval_mode(_order(), _session(strategy="manual")) == "manual"
    assert decide_approval_mode(_order(), _session(strategy="confirm_window")) == "manual"
    assert decide_approval_mode(_order(approval="manual"), _session()) == "manual"


def test_account_set_is_reread_after_startup() -> None:
    accounts = [PIN]

    def read() -> tuple[str, ...]:
        return tuple(accounts)

    session = _session(reader=read, baseline=(PIN,))
    assert decide_approval_mode(_order(), session) == "auto_paper"
    accounts[:] = ["DU1234567"]
    assert decide_approval_mode(_order(), session) == "manual"


def test_auto_approval_record_is_fresh_and_checked(tmp_path: Path) -> None:
    moment = NOW
    session = _session(now=moment)
    document = record_auto_approval(
        tmp_path,
        "ord-1",
        {"symbol": "AAPL", "quantity": 10, "limit_price": 100.0},
        session,
        attempt=1,
        approval_price=100.0,
        order=_order(),
    )
    checked = datetime.fromisoformat(document["r0_check"]["checked_at_utc"])
    decided = datetime.fromisoformat(document["decided_at_utc"])
    assert document["approver"] == "auto_paper"
    assert document["channel"] == "auto"
    assert document["r0_check"]["all_ok"] == 1
    assert 0 <= (decided - checked).total_seconds() <= 5
    assert not (tmp_path / "approvals" / "pending" / "ord-1.json").exists()
    assert (tmp_path / "approvals" / "decided" / "ord-1.json").is_file()
    with sqlite3.connect(tmp_path / "data" / "trading.db") as conn:
        row = conn.execute(
            "SELECT approver, channel, attempt, r0_check_json FROM approvals"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO approvals (
                    ts_utc, order_id, order_hash, status, detail_json, approver, r0_check_json
                ) VALUES ('t', 'bad', 'h', 'APPROVED', '{}', ?, '{"all_ok": 0}')
                """,
                ("auto_paper",),
            )
    assert row[0] == "auto_paper"
    assert row[1] == "auto"
    assert row[2] == 1
    assert json.loads(row[3])["all_ok"] == 1


def test_flow_blocks_non_du_before_final_and_before_place(tmp_path: Path) -> None:
    accounts = [PIN]
    session = _session(reader=lambda: tuple(accounts), now=NOW)
    ctx = _ctx(tmp_path)
    intent = _intent()
    placed: list[str] = []

    def before_final() -> None:
        accounts[:] = ["U1234567"]
        ctx.accounts = ("U1234567",)

    outcome = run_entry_flow(
        trading_home=tmp_path,
        intent=intent,
        ctx=ctx,
        session=session,
        order_id="ord-final",
        payload={"symbol": "AAPL", "quantity": 10, "limit_price": 100.0},
        priceable=True,
        minutes_to_close=30,
        now=NOW,
        place=lambda _order: placed.append("sent"),
        before_final=before_final,
    )
    assert outcome.sent is False
    assert outcome.code == "R0_NOT_PAPER_ACCOUNT"
    assert placed == []

    accounts[:] = [PIN]
    ctx.accounts = (PIN,)
    intent = _intent(client_id="cid-2", intent_id="intent-2")

    def before_place() -> None:
        accounts[:] = ["U1234567"]

    outcome = run_entry_flow(
        trading_home=tmp_path,
        intent=intent,
        ctx=ctx,
        session=session,
        order_id="ord-place",
        payload={"symbol": "AAPL", "quantity": 10, "limit_price": 100.0},
        priceable=True,
        minutes_to_close=30,
        now=NOW,
        place=lambda _order: placed.append("sent"),
        before_place=before_place,
    )
    assert outcome.sent is False
    assert str(outcome.code).startswith("R0_")
    assert placed == []


def test_tampered_payload_is_rejected_at_final(tmp_path: Path) -> None:
    session = _session(now=NOW)
    ctx = _ctx(tmp_path)
    payload = {"symbol": "AAPL", "quantity": 10, "limit_price": 100.0}

    def tamper() -> None:
        payload["quantity"] = 11

    outcome = run_entry_flow(
        trading_home=tmp_path,
        intent=_intent(),
        ctx=ctx,
        session=session,
        order_id="ord-hash",
        payload=payload,
        priceable=True,
        minutes_to_close=30,
        now=NOW,
        place=lambda _order: None,
        before_final=tamper,
    )
    assert outcome.code == "ORDER_HASH_MISMATCH"
    assert outcome.sent is False


def test_deviation_reapproves_once_then_chase_expires(tmp_path: Path) -> None:
    session = _session(now=NOW)
    ctx = _ctx(tmp_path)
    intent = _intent()
    placed: list[str] = []

    def drift() -> None:
        _set_price(ctx, intent, 110.0)

    once = run_entry_flow(
        trading_home=tmp_path,
        intent=intent,
        ctx=ctx,
        session=session,
        order_id="ord-once",
        payload={"symbol": "AAPL", "quantity": 10, "limit_price": 100.0},
        priceable=True,
        minutes_to_close=30,
        now=NOW,
        place=lambda _order: placed.append("once"),
        before_final=drift,
    )
    assert once.sent is True
    assert once.attempt == 2

    intent = _intent(client_id="cid-3", intent_id="intent-3")
    ctx = _ctx(tmp_path)
    _set_price(ctx, intent, 100.0)

    def drift_again() -> None:
        _set_price(ctx, intent, 110.0)

    def chase() -> None:
        _set_price(ctx, intent, 130.0)

    chased = run_entry_flow(
        trading_home=tmp_path,
        intent=intent,
        ctx=ctx,
        session=session,
        order_id="ord-chase",
        payload={"symbol": "AAPL", "quantity": 10, "limit_price": 100.0},
        priceable=True,
        minutes_to_close=30,
        now=NOW,
        place=lambda _order: placed.append("chase"),
        before_final=drift_again,
        after_reapproval=chase,
    )
    assert chased.code == "CHASE_REAPPROVAL_EXCEEDED"
    assert chased.sent is False
    with sqlite3.connect(tmp_path / "data" / "trading.db") as conn:
        attempts = [
            row[0]
            for row in conn.execute("SELECT attempt FROM approvals WHERE order_id = 'ord-chase'")
        ]
    assert attempts == [1, 2]
    assert 3 not in attempts

    expired = run_entry_flow(
        trading_home=tmp_path,
        intent=_intent(client_id="cid-4", intent_id="intent-4"),
        ctx=_ctx(tmp_path),
        session=session,
        order_id="ord-close",
        payload={"symbol": "AAPL", "quantity": 1},
        priceable=False,
        minutes_to_close=9,
        now=NOW,
        place=lambda _order: None,
    )
    assert expired.code == "APPROVAL_EXPIRED"
    assert expired.detail == "not_priced_by_close"

    record_auto_approval(
        tmp_path,
        "ord-window",
        {"symbol": "AAPL", "quantity": 10, "limit_price": 100.0},
        session,
        attempt=1,
        approval_price=100.0,
        order=_order(),
    )
    later = NOW + timedelta(seconds=AUTO_SUBMIT_WINDOW_SEC + 1)
    session_later = _session(now=later)
    step = expire_submit_window(
        tmp_path,
        intent=_intent(client_id="cid-5", intent_id="intent-5"),
        ctx=_ctx(tmp_path),
        session=session_later,
        order_id="ord-window",
        payload={"symbol": "AAPL", "quantity": 10, "limit_price": 100.0},
        decided_at=NOW,
        now=later,
        attempt=1,
    )
    assert step.attempt == 2
    second = expire_submit_window(
        tmp_path,
        intent=_intent(client_id="cid-5", intent_id="intent-5"),
        ctx=_ctx(tmp_path),
        session=_session(now=later + timedelta(seconds=AUTO_SUBMIT_WINDOW_SEC + 1)),
        order_id="ord-window",
        payload={"symbol": "AAPL", "quantity": 10, "limit_price": 100.0},
        decided_at=later,
        now=later + timedelta(seconds=AUTO_SUBMIT_WINDOW_SEC + 1),
        attempt=2,
    )
    assert second.code == "APPROVAL_EXPIRED"
    assert second.detail == "submit_window"
    with sqlite3.connect(tmp_path / "data" / "trading.db") as conn:
        window_attempts = [
            row[0]
            for row in conn.execute(
                "SELECT attempt FROM approvals WHERE order_id = 'ord-window' ORDER BY attempt"
            )
        ]
    assert window_attempts == [1, 2]


def test_removed_switch_refuses_startup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for value in ("1", "true", "false", ""):
        monkeypatch.setenv("PAPER_AUTO_APPROVE", value)
        get_settings.cache_clear()
        with pytest.raises(SystemExit):
            get_settings()
    monkeypatch.delenv("PAPER_AUTO_APPROVE", raising=False)
    get_settings.cache_clear()
    assert get_settings().force_manual_approval == "false"
    config = tmp_path / "execution.env"
    config.write_text("PAPER_AUTO_APPROVE=false\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        assert_startup_switches(config_path=config)
    forced = _session(force=manual_forced("true"))
    assert decide_approval_mode(_order(), forced) == "manual"
    assert decide_approval_mode(_order(), _session(strategy="manual")) == "manual"


def test_manual_cli_still_checks_hash_and_rejects_auto(tmp_path: Path, monkeypatch) -> None:
    queued = queue_order(tmp_path, "ord-m", {"symbol": "AAPL", "quantity": 1, "limit_price": 10})
    with pytest.raises(SystemExit):
        main(
            [
                "--home",
                str(tmp_path),
                "approve",
                "ord-m",
                "--hash",
                "0" * 12,
                "--by",
                "user",
                "--channel",
                "cli",
            ]
        )
    stored = json.loads(queued.read_text(encoding="utf-8"))
    stored["quantity"] = 9
    queued.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(ApprovalError, match="hash"):
        approve(tmp_path, "ord-m", hash12(stored["order_hash"]), by="user", channel="cli")
    fresh = queue_order(tmp_path, "ord-n", {"symbol": "AAPL", "quantity": 1, "limit_price": 10})
    document = json.loads(fresh.read_text(encoding="utf-8"))
    created = datetime.fromisoformat(document["created_at"])
    with pytest.raises(ApprovalError, match="timeout"):
        approve(
            tmp_path,
            "ord-n",
            hash12(document["order_hash"]),
            by="user",
            channel="cli",
            now=created + timedelta(minutes=121),
        )
    listed = json.loads(
        _capture(
            lambda: main(["--home", str(tmp_path), "pending"]),
        )
    )
    assert any(row["order_id"] == "ord-n" for row in listed)
    main(["--home", str(tmp_path), "decline", "ord-n", "--by", "user", "--channel", "cli"])
    assert not (tmp_path / "approvals" / "pending" / "ord-n.json").exists()
    record_auto_approval(
        tmp_path,
        "ord-auto",
        {"symbol": "AAPL", "quantity": 1, "limit_price": 10},
        _session(now=NOW),
        attempt=1,
        approval_price=10.0,
        order=_order(),
    )
    with pytest.raises(ApprovalError, match="automatically"):
        approve(tmp_path, "ord-auto", "0" * 12, by="user", channel="cli")
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    get_settings.cache_clear()
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "approval_session.json").write_text(
        json.dumps(
            {
                "accounts": [PIN],
                "baseline": [PIN],
                "port": 4002,
                "strategy_approval": "auto",
            }
        ),
        encoding="utf-8",
    )
    mode, reason = describe_approval(tmp_path)
    text = status_text(tmp_path, NOW)
    assert mode == "auto_paper"
    assert reason == "ok"
    assert "approval=auto_paper" in text
    assert "unforwarded_critical=0" in text
    main(["--home", str(tmp_path), "digest"])
    assert list((tmp_path / "reports" / "daily").glob("summary_*.json"))


def _capture(fn) -> str:
    from io import StringIO

    buffer = StringIO()
    previous = sys.stdout
    sys.stdout = buffer
    try:
        fn()
    finally:
        sys.stdout = previous
    return buffer.getvalue()


_BAD = [
    ("non_du", {"accounts": ("U1234567",), "pin": PIN, "baseline": (PIN,), "order": "U1234567"}),
    ("port_gateway", {"port": int("400" + "1")}),  # refused: paper_only
    ("port_tws", {"port": int("749" + "6")}),  # refused: paper_only
    ("port_other", {"port": 1}),
    ("multi_du", {"accounts": (PIN, "DU1234567"), "baseline": (PIN, "DU1234567")}),
    ("multi_mixed", {"accounts": (PIN, "U7654321"), "baseline": (PIN, "U7654321")}),
    ("changed", {"baseline": ("DU1234567",)}),
    ("pin_missing", {"pin": ""}),
    ("pin_mismatch", {"pin": "DU1234567"}),
    ("order_mismatch", {"order": "DU1234567"}),
]
_ENVS: list[dict[str, str]] = []
for _force in ("true", "false", "garbage"):
    for _approver in ("file", "chat", "other"):
        for _notifier in ("file", "hook"):
            for _flag in ("1", "true", "false", ""):
                _ENVS.append(
                    {
                        "FORCE_MANUAL_APPROVAL": _force,
                        "APPROVER": _approver,
                        "NOTIFIER": _notifier,
                        "PAPER_AUTO_APPROVE": _flag,
                    }
                )
_ENVS.append(
    {
        "IB_PORT": "4002",
        "BASE_CURRENCY": "HKD",
        "ALLOWED_MARKETS": "US",
        "REF_PRICE_MODE": "session_open",
        "NOTIFIER": "file",
        "APPROVER": "file",
        "FORCE_MANUAL_APPROVAL": "false",
        "PAPER_AUTO_APPROVE": "yes",
    }
)


@pytest.mark.parametrize(("name", "case"), _BAD, ids=[item[0] for item in _BAD])
@pytest.mark.parametrize("env", _ENVS)
def test_non_paper_never_auto_approves(name: str, case: dict, env: dict, monkeypatch) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    accounts = tuple(case.get("accounts", (PIN,)))
    session = _session(
        accounts,
        port=int(case.get("port", 4002)),
        pin=str(case.get("pin", PIN)),
        baseline=tuple(case.get("baseline", accounts)),
        force=manual_forced(os.environ.get("FORCE_MANUAL_APPROVAL")),
        strategy="auto",
    )
    assert decide_approval_mode(_order(str(case.get("order", PIN))), session) == "manual"
    assert name


def test_critical_events_follow_the_stop(tmp_path: Path, monkeypatch) -> None:
    stop(tmp_path)
    halt_home = tmp_path / "r9"
    halt_r9(halt_home)
    r0_home = tmp_path / "r0"
    latch_kill_switch(r0_home, "R0_NOT_PAPER_ACCOUNT")
    for home, kind in (
        (tmp_path, "r18_stop"),
        (halt_home, "r9_halt"),
        (r0_home, "r0_violation"),
    ):
        pending = list((home / "alerts" / "pending").glob(f"*_exec_{kind}.json"))
        assert len(pending) == 1
        payload = json.loads(pending[0].read_text(encoding="utf-8"))
        for key in (
            "schema_version",
            "alert_id",
            "event_id",
            "created_at",
            "created_at_utc",
            "source",
            "severity",
            "kind",
            "rule_id",
            "reason_code",
            "message_zh",
            "resume_hint",
            "dedup_key",
            "actions",
            "state",
        ):
            assert payload[key] not in (None, "")
        assert payload["schema_version"] == "exec_alert/1.0"
        assert list((home / "logs").glob("critical_*.jsonl"))
        assert (home / "KILL_SWITCH").is_file()

    def boom(*_args, **_kwargs):
        raise OSError("disk")

    monkeypatch.setattr("auto_trading.alerts.critical.write_critical_event", boom)
    broken = tmp_path / "broken"
    stop(broken)
    assert (broken / "KILL_SWITCH").is_file()
    assert (broken / "state" / "strategy_runtime.json").is_file()


def test_forwarder_reaches_outbox_once_within_sixty_seconds(tmp_path: Path) -> None:
    assert ALERT_SCAN_INTERVAL_SEC == 10
    first = write_critical_event(
        tmp_path,
        kind="r18_stop",
        reason_code="R18",
        rule_id="R18",
        now=NOW,
        account=PIN,
        message_zh="全停",
        resume_hint="只能在主機本地執行 trade resume",
    )
    second = write_critical_event(
        tmp_path,
        kind="r9_halt",
        reason_code="R9",
        rule_id="R9",
        now=NOW,
        account=PIN,
        message_zh="回撤",
        resume_hint="只能在主機本地執行 trade resume",
    )
    scan_once(tmp_path, NOW)
    scan_once(tmp_path, NOW)
    outbox = list((tmp_path / "notifications" / "outbox").glob("*_exec_critical_*.json"))
    assert len(outbox) == 2
    body = json.loads(outbox[0].read_text(encoding="utf-8"))
    assert body["kind"] == "exec_critical"
    assert body["source"] == "execution"
    created = datetime.fromisoformat(first["created_at_utc"])
    assert (NOW - created).total_seconds() <= 60
    pending = tmp_path / "alerts" / "pending"
    (pending / "again_exec_r18_stop.json").write_text(
        json.dumps(first, ensure_ascii=False),
        encoding="utf-8",
    )
    scan_once(tmp_path, NOW)
    assert len(list((tmp_path / "notifications" / "outbox").glob("*_exec_critical_*.json"))) == 2
    assert second["alert_id"] != first["alert_id"]
    with sqlite3.connect(tmp_path / "data" / "trading.db") as conn:
        sources = [row[0] for row in conn.execute("SELECT source FROM b_alerts")]
        critical = conn.execute("SELECT COUNT(*) FROM critical_events").fetchone()[0]
    assert "execution" in sources
    assert critical == 2


def test_late_critical_warns_and_pings(tmp_path: Path) -> None:
    write_critical_event(
        tmp_path,
        kind="kill_switch",
        reason_code="R18",
        rule_id="R18",
        now=NOW - timedelta(seconds=61),
        account=PIN,
        message_zh="全停",
        resume_hint="只能在主機本地執行 trade resume",
    )
    seen: list[str] = []
    set_health_pinger(seen.append)
    try:
        count, late = unforwarded_critical(tmp_path, NOW)
        assert count == 1 and late is True
        assert status_warning(tmp_path, NOW) is True
        assert seen == ["/fail"]
    finally:
        set_health_pinger(None)


def test_daily_summary_matches_the_journal(tmp_path: Path) -> None:
    moment = datetime(2026, 10, 13, 6, 0, tzinfo=HKT)
    inside = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)
    journal = TradeJournal(tmp_path)
    journal.create_order(
        order_id="o-delayed",
        intent_id="i-delayed",
        seq=1,
        symbol="AAPL",
        quote_data_type="delayed",
        market_data_type=3,
        delay_sec=900,
    )
    journal.create_order(
        order_id="o-rt",
        intent_id="i-rt",
        seq=1,
        symbol="MSFT",
        quote_data_type="realtime",
        market_data_type=1,
        delay_sec=0,
    )
    journal.record_fill(
        fill_id="f1",
        intent_id="i-delayed",
        client_id="o-delayed",
        symbol="AAPL",
        price=100.0,
        quantity=10,
        when=inside,
    )
    journal.record_fill(
        fill_id="f2",
        intent_id="i-rt",
        client_id="o-rt",
        symbol="MSFT",
        price=50.0,
        quantity=2,
        when=inside,
    )
    journal.record_risk_check(
        symbol="TSM",
        code="ADR_PREMIUM_PAUSED",
        intent_id="i-rej",
        detail={"stage": "pre", "rule": "R28", "market": "US", "market_data_type": 3},
        when=inside,
    )
    journal.set_order_state("o-delayed", "SUBMITTED", when=inside)
    journal.set_order_state("o-rt", "SUBMITTED", when=inside)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "account.json").write_text(
        json.dumps({"nlv": 1000, "hwm": 1000, "positions": [], "realized_base": 0}),
        encoding="utf-8",
    )
    provisional = write_daily_summary(tmp_path, now=moment, complete=False, pin=PIN)
    validate_daily_summary(provisional)
    assert provisional["status"] == "provisional"
    assert provisional["revision"] == 1
    assert len(provisional["fills"]) == 2
    assert len(provisional["rejections"]) == 1
    assert provisional["r15"]["submitted"] == 2
    assert provisional["by_quote_type"]["delayed"]["fills"] == 1
    assert provisional["by_quote_type"]["realtime"]["fills"] == 1
    assert provisional["by_market"]["HK"]["fills"] == 0
    assert provisional["approvals"]["auto_paper"] == 0
    final = write_daily_summary(tmp_path, now=moment, complete=True, pin=PIN)
    assert final["status"] == "final"
    assert final["revision"] == 2
    assert (tmp_path / "reports" / "daily" / "summary_20261012.md").is_file()
    notes = list((tmp_path / "notifications" / "outbox").glob("*_daily_summary.json"))
    assert notes
    assert json.loads(notes[-1].read_text(encoding="utf-8"))["kind"] == "daily_summary"


def test_sync_list_includes_critical_logs_and_reports() -> None:
    text = Path("vps/deploy/sync_vps_to_box.txt").read_text(encoding="utf-8")
    assert "logs/critical_*.jsonl" in text
    assert "reports/" in text
    completed = subprocess.run(
        [sys.executable, "scripts/check_v08_ci.py"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_r29_trips_on_the_third_distinct_order(tmp_path: Path, monkeypatch) -> None:
    moment = datetime(2026, 10, 12, 12, 0, tzinfo=HKT)
    write_risk_limits(tmp_path, build_risk_limits(now=moment, nlv=1_000_000, account=PIN))
    ignored = note_execution_anomaly(tmp_path, order_id="a", kind="R1", now=moment, account=PIN)
    assert ignored is False
    note_execution_anomaly(tmp_path, order_id="a", kind="BROKER_REJECTED", now=moment, account=PIN)
    note_execution_anomaly(tmp_path, order_id="a", kind="UNKNOWN", now=moment, account=PIN)
    assert TradeJournal(tmp_path).counted_anomalies("2026-10-12") == 1
    note_execution_anomaly(tmp_path, order_id="b", kind="UNKNOWN", now=moment, account=PIN)
    assert flags(tmp_path).get("r29_active") is not True
    assert ANOMALY_BREAKER_COUNT == 3
    tripped = note_execution_anomaly(
        tmp_path, order_id="c", kind="ORDER_HASH_MISMATCH", now=moment, account=PIN
    )
    assert tripped is True
    limits = json.loads((tmp_path / "state" / "risk_limits.json").read_text(encoding="utf-8"))
    assert limits["accounts"][0]["breakers"]["infra_pause"] is True
    ctx = _ctx(tmp_path, now=NOW)
    with pytest.raises(RiskRejected, match="ANOMALY_BREAKER_ACTIVE"):
        RiskGate(ctx).allow(_intent())
    final = _ctx(tmp_path, now=NOW, stage="final")
    with pytest.raises(RiskRejected, match="ANOMALY_BREAKER_ACTIVE"):
        RiskGate(final).allow(_intent(client_id="cid-f", intent_id="intent-f"))
    exit_ctx = _ctx(
        tmp_path,
        now=NOW,
        positions=[Held(symbol="AAPL", quantity=10, market_value_base=1000)],
    )
    assert (
        RiskGate(exit_ctx).allow(
            _intent(
                client_id="cid-x",
                intent_id="intent-x",
                side=OrderSide.SELL,
                intent_type="EXIT",
                parent_intent_id="intent-1",
                quantity=1,
            )
        )
        is None
    )
    pending = list((tmp_path / "alerts" / "pending").glob("*_exec_r29_anomaly_breaker.json"))
    assert pending
    hint = json.loads(pending[0].read_text(encoding="utf-8"))["resume_hint"]
    assert "trade resume --breaker R29" in hint
    kinds = [
        row[0]
        for row in sqlite3.connect(tmp_path / "data" / "trading.db").execute(
            "SELECT kind FROM anomaly_events"
        )
    ]
    assert "ANOMALY_BREAKER" in kinds
    assert maybe_auto_clear_r29(tmp_path, moment, unknown=False, recon_freeze=False) is False
    assert (
        maybe_auto_clear_r29(
            tmp_path, datetime(2026, 10, 13, 8, 0, tzinfo=HKT), unknown=True, recon_freeze=False
        )
        is False
    )
    assert flags(tmp_path).get("r29_active") is True
    journal = TradeJournal(tmp_path)
    journal.create_order(order_id="u1", intent_id="iu", seq=1, symbol="AAPL")
    journal.set_order_state("u1", "UNKNOWN", when=moment)
    assert (
        maybe_auto_clear_r29(
            tmp_path, datetime(2026, 10, 13, 8, 0, tzinfo=HKT), unknown=False, recon_freeze=False
        )
        is False
    )
    journal.set_order_state("u1", "CANCELLED", when=moment)
    assert (
        maybe_auto_clear_r29(
            tmp_path,
            datetime(2026, 10, 13, 8, 0, tzinfo=HKT),
            unknown=False,
            recon_freeze=True,
        )
        is False
    )
    assert maybe_auto_clear_r29(
        tmp_path, datetime(2026, 10, 13, 8, 0, tzinfo=HKT), unknown=False, recon_freeze=False
    )
    assert flags(tmp_path).get("r29_active") is False
    note_execution_anomaly(tmp_path, order_id="d", kind="BROKER_REJECTED", now=moment, account=PIN)
    note_execution_anomaly(tmp_path, order_id="e", kind="UNKNOWN", now=moment, account=PIN)
    note_execution_anomaly(
        tmp_path, order_id="f", kind="ORDER_HASH_MISMATCH", now=moment, account=PIN
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(PermissionError):
        resume_r29(tmp_path, local_terminal=True, confirm="CONFIRM RESUME")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("SSH_CONNECTION", "1")
    with pytest.raises(PermissionError):
        resume_r29(tmp_path, local_terminal=True, confirm="CONFIRM RESUME")
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    journal.set_order_state("u1", "UNKNOWN", when=moment)
    with pytest.raises(PermissionError, match="UNKNOWN"):
        resume_r29(tmp_path, local_terminal=True, confirm="CONFIRM RESUME")
    journal.set_order_state("u1", "CANCELLED", when=moment)
    assert resume_r29(tmp_path, local_terminal=True, confirm="CONFIRM RESUME") == "r29 cleared"
    assert flags(tmp_path).get("r29_active") is False
    (tmp_path / "state" / "resume.request").write_text("remote\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="synced"):
        resume_r29(tmp_path, local_terminal=True, confirm="CONFIRM RESUME")
    with pytest.raises(SystemExit, match="recover"):
        main(
            [
                "--home",
                str(tmp_path),
                "resume",
                "--breaker",
                "R30",
                "--local",
                "--confirm",
                "CONFIRM RESUME",
            ]
        )
    clear_r29(tmp_path, moment)


def test_r30_pauses_entries_until_outbox_and_summary_recover(tmp_path: Path) -> None:
    moment = datetime(2026, 10, 13, 6, 30, tzinfo=HKT)
    write_critical_event(
        tmp_path,
        kind="r18_stop",
        reason_code="R18",
        rule_id="R18",
        now=moment,
        account=PIN,
        message_zh="全停",
        resume_hint="只能在主機本地執行 trade resume",
    )
    outbox = tmp_path / "notifications" / "outbox"
    outbox.parent.mkdir(parents=True, exist_ok=True)
    outbox.write_text("blocked", encoding="utf-8")
    scan_once(tmp_path, moment)
    seen: list[str] = []
    set_health_pinger(seen.append)
    try:
        assert refresh_r30(tmp_path, moment + timedelta(seconds=301), account=PIN) is True
    finally:
        set_health_pinger(None)
    assert "/fail" in seen
    assert flags(tmp_path).get("r30_active") is True
    ctx = _ctx(tmp_path, now=NOW)
    with pytest.raises(RiskRejected, match="NOTIFY_FAILURE_PAUSE"):
        RiskGate(ctx).allow(_intent())
    exit_ctx = _ctx(
        tmp_path,
        now=NOW,
        positions=[Held(symbol="AAPL", quantity=10, market_value_base=1000)],
    )
    assert (
        RiskGate(exit_ctx).allow(
            _intent(
                side=OrderSide.SELL,
                intent_type="EXIT",
                parent_intent_id="p",
                quantity=1,
                client_id="cid-e",
                intent_id="intent-e",
            )
        )
        is None
    )
    text = (tmp_path / "logs").glob("critical_*.jsonl")
    assert any("r30_notify_failure" in path.read_text(encoding="utf-8") for path in text)
    outbox.unlink()
    from auto_trading.alerts.forwarder import retry_failed_critical

    pending = next((tmp_path / "alerts" / "pending").glob("*_exec_*.json"))
    (tmp_path / "alerts" / "failed").mkdir(parents=True, exist_ok=True)
    failed = tmp_path / "alerts" / "failed" / pending.name
    pending.replace(failed)
    later = moment + timedelta(seconds=400)
    retry_failed_critical(tmp_path, later)
    assert list((tmp_path / "notifications" / "outbox").glob("*_exec_critical_*.json"))
    assert refresh_r30(tmp_path, later, account=PIN) is False
    assert flags(tmp_path).get("r30_active") is False
    notes = list((tmp_path / "notifications" / "outbox").glob("*_ops.json"))
    assert any(json.loads(path.read_text(encoding="utf-8"))["level"] == "warn" for path in notes)

    overdue = tmp_path / "overdue"
    at_eight = datetime(2026, 10, 13, 8, 0, tzinfo=HKT)
    assert summary_overdue(overdue, at_eight) is True
    assert refresh_r30(overdue, at_eight, account=PIN) is True
    write_daily_summary(
        overdue,
        now=datetime(2026, 10, 13, 6, 0, tzinfo=HKT),
        complete=False,
        pin=PIN,
    )
    assert summary_overdue(overdue, at_eight) is False
    assert refresh_r30(overdue, at_eight, account=PIN) is False


def test_r29_parameters_stay_out_of_config_hash() -> None:
    assert config_hash(EXPECTED_LIMITS) == HASH_ODD_LOT_OFF
    assert HASH_ODD_LOT_ON.startswith("sha256:39c52f15")
    assert HASH_R2_AS_FLOAT.startswith("sha256:64039ee5")
    assert HASH_SCOPE_INSIDE_LIMITS.startswith("sha256:5bc6cf96")
    assert "ANOMALY_BREAKER_COUNT" not in EXPECTED_LIMITS
    assert "NOTIFY_FAIL_HALT_SEC" not in EXPECTED_LIMITS
    built = build_risk_limits(now=NOW, nlv=1_000_000, infra_pause=True)
    assert built["schema_version"] == "risk_limits/1.1"
    assert built["config_hash"] == HASH_ODD_LOT_OFF
    assert built["accounts"][0]["breakers"]["infra_pause"] is True
