"""Spec v0.7 checks that sit beside the paper-execution suite."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from auto_trading.backtest.veto import UnsupportedVetoSchema, load_veto_document
from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.execution.approvals import ApprovalError, approve, hash12, queue_order
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.risk import GateContext, Held, Quote, RiskGate, RiskRejected

HKT = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 2, 22, 0, tzinfo=HKT)


def test_veto_files_dispatch_on_schema_version() -> None:
    old = load_veto_document({"schema_version": "veto_rule/1.0", "blocks": [{"ticker": "AAPL"}]})
    new = load_veto_document(
        {
            "schema_version": "veto_rule/1.1",
            "rules": [{"symbol": "2330.TW", "action": "block_entry"}],
        }
    )
    assert old == frozenset({"AAPL"})
    assert new == frozenset({"2330.TW"})
    with pytest.raises(UnsupportedVetoSchema):
        load_veto_document({"schema_version": "veto_rule/9.0", "blocks": []})


def test_approval_recomputes_hash_times_out_and_audits(tmp_path) -> None:
    queued = queue_order(tmp_path, "ord-9", {"symbol": "AAPL", "quantity": 1, "limit_price": 10})
    stored = json.loads(queued.read_text(encoding="utf-8"))
    stored["quantity"] = 2
    stored["order_hash"] = "sha256:" + "ab" * 32
    queued.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(ApprovalError, match="hash"):
        approve(tmp_path, "ord-9", "ab" * 6)

    fresh = queue_order(tmp_path, "ord-10", {"symbol": "AAPL", "quantity": 1, "limit_price": 10})
    document = json.loads(fresh.read_text(encoding="utf-8"))
    created = datetime.fromisoformat(document["created_at"])
    late = created + timedelta(minutes=16)
    soon = created + timedelta(minutes=1)
    digest = hash12(document["order_hash"])
    with pytest.raises(ApprovalError, match="timeout"):
        approve(tmp_path, "ord-10", digest, now=late)
    decided = approve(tmp_path, "ord-10", digest, now=soon)
    assert decided["status"] == "APPROVED"
    with sqlite3.connect(tmp_path / "data" / "trading.db") as conn:
        row = conn.execute("SELECT status FROM approvals").fetchone()
        audit = conn.execute("SELECT action FROM audit_log").fetchone()
    assert row[0] == "APPROVED"
    assert audit[0] == "approval"


def test_journal_tables_are_append_only(tmp_path) -> None:
    journal = TradeJournal(tmp_path)
    journal.record_risk_check(symbol="AAPL", code="R22", intent_id="i")
    journal.record_hwm(hwm=10, nlv=9, reason="reset_hwm")
    journal.record_anomaly(kind="R0_FORBIDDEN_PORT")
    journal.remember_controlled({"TSM", "BABA"})
    assert journal.last_controlled() == frozenset({"TSM", "BABA"})
    with sqlite3.connect(tmp_path / "data" / "trading.db") as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE risk_checks SET code = 'x'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM anomaly_events")


def _buy(**kwargs: object) -> OrderIntent:
    payload: dict = {
        "client_id": "cid-1",
        "symbol": "AAPL",
        "side": OrderSide.BUY,
        "quantity": 10,
        "limit_price": 100.0,
        "currency": "USD",
        "market": "US",
        "account": "DU1234567",
    }
    payload.update(kwargs)
    return OrderIntent(**payload)


def _ctx(home, **kwargs: object) -> GateContext:
    if not (home / "state" / "risk_limits.json").is_file():
        write_risk_limits(home, build_risk_limits(now=NOW, nlv=1_000_000, account="DU1234567"))
    payload: dict = {
        "now": NOW,
        "trading_home": home,
        "expected_account": "DU1234567",
        "quotes": {
            "AAPL": Quote(last=100, as_of=NOW, received_at=NOW, market_data_type=1),
            "TSM": Quote(last=100, as_of=NOW, received_at=NOW, market_data_type=1),
        },
        "fx_to_base": {"USD": 1.0, "HKD": 1.0},
    }
    payload.update(kwargs)
    return GateContext(**payload)


def test_missing_approved_hash_limits_file_type2_and_adr_pause(tmp_path) -> None:
    home_ctx = _ctx(tmp_path)
    with pytest.raises(RiskRejected, match="R25"):
        RiskGate(home_ctx).allow(_buy(spec_hash="sha256:" + "ab" * 32))
    (tmp_path / "state" / "risk_limits.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RiskRejected, match="RISK_CONFIG_MISMATCH"):
        RiskGate(home_ctx).allow(_buy())
    exit_order = _buy(
        side=OrderSide.SELL,
        intent_type="EXIT",
        parent_intent_id="entry-1",
        quantity=1,
    )
    held = _ctx(tmp_path, positions=[Held("AAPL", 5, 500)])
    assert RiskGate(held).allow(exit_order) is None
    write_risk_limits(tmp_path, build_risk_limits(now=NOW, nlv=1_000_000, account="DU1234567"))
    frozen = _ctx(
        tmp_path,
        quotes={"AAPL": Quote(last=100, as_of=NOW, received_at=NOW, market_data_type=2)},
    )
    with pytest.raises(RiskRejected, match="R22"):
        RiskGate(frozen).allow(_buy())
    paused = _ctx(tmp_path, adr_paused=frozenset({"TSM"}), positions=[Held("AAPL", 5, 500)])
    with pytest.raises(RiskRejected, match="ADR_PREMIUM_PAUSED"):
        RiskGate(paused).allow(_buy(symbol="TSM"))
    assert RiskGate(paused).allow(exit_order) is None
