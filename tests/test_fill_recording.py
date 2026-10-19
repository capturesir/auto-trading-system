"""IB executions are recorded once, then the order and the strategy book move."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from auto_trading.execution.fills import (
    ingest_broker_fills,
    normalize_execution,
    read_ib_executions,
    write_execution_snapshot,
)
from auto_trading.execution.oms import order_ref
from auto_trading.execution.services import oms_once, recon_once, submit_once
from auto_trading.execution.spy_backfill import (
    ACCOUNT,
    EXEC_ID,
    FEE,
    INTENT_ID,
    PERM_ID,
    PRICE,
    QTY,
    apply_backfill,
    inspect_plan,
    order_id,
    undo_backfill,
)
from auto_trading.portfolio.journal import TradeJournal

WHEN = datetime(2026, 10, 16, 13, 35, 8, tzinfo=UTC)


def _backfill_main(argv: list[str]) -> int:
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "backfill_spy_fill_20261016.py"
    spec = importlib.util.spec_from_file_location("backfill_spy_fill_20261016", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return int(module.main(argv))


def _seed(
    home: Path,
    *,
    intent_id: str = INTENT_ID,
    symbol: str = "SPY",
    qty: float = QTY,
    strategy: str = "sig_trend",
) -> str:
    journal = TradeJournal(home)
    digest = order_ref(intent_id, 1)
    journal.record_intent(
        {
            "intent_id": intent_id,
            "strategy_id": strategy,
            "symbol": symbol,
            "side": "BUY",
            "quantity": qty,
        }
    )
    journal.create_order(
        order_id=digest,
        intent_id=intent_id,
        seq=1,
        symbol=symbol,
        intent_type="ENTRY",
    )
    journal.set_order_state(digest, "SUBMITTING", when=WHEN)
    journal.set_order_state(digest, "SUBMITTED", when=WHEN)
    return digest


def _spy_fill(order: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "exec_id": EXEC_ID,
        "order_ref": order,
        "perm_id": PERM_ID,
        "symbol": "SPY",
        "side": "BOT",
        "qty": QTY,
        "price": PRICE,
        "fee": FEE,
        "time": "2026-10-16T21:35:08+08:00",
        "account": ACCOUNT,
        "exchange": "ARCA",
    }
    payload.update(overrides)
    return payload


def _execution_count(home: Path) -> int:
    with sqlite3.connect(home / "data" / "trading.db") as conn:
        row = conn.execute("SELECT COUNT(*) FROM executions").fetchone()
    return int(row[0])


def _position_qty(home: Path, ticker: str = "SPY") -> float:
    total = 0.0
    for row in TradeJournal(home).strategy_position_rows():
        if row["ticker"] == ticker:
            total += float(row["qty"])
    return total


def test_full_spy_fill_is_recorded_once_and_unfreezes(tmp_path: Path) -> None:
    order = _seed(tmp_path)
    assert order.startswith("04ad62c5") and order.endswith("4501f4")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "account.json").write_text(
        json.dumps(
            {
                "positions": [{"symbol": "SPY", "qty": 10, "currency": "USD"}],
                "open_orders": [],
            }
        ),
        encoding="utf-8",
    )
    frozen = recon_once(tmp_path, WHEN)
    assert frozen["freeze"] is True
    kinds = {row["kind"] for row in frozen["breaks"]}
    assert kinds == {"QTY", "ORDER"}

    first = ingest_broker_fills(tmp_path, [_spy_fill(order)], now=WHEN)
    assert first["recorded"] == [EXEC_ID]
    assert first["booked"] == [EXEC_ID]
    assert first["states"][order] == "FILLED"
    assert TradeJournal(tmp_path).order_state(order) == "FILLED"
    assert TradeJournal(tmp_path).working_orders() == []
    assert _position_qty(tmp_path) == pytest.approx(10)
    rows = TradeJournal(tmp_path).strategy_position_rows()
    assert rows[0]["strategy_id"] == "sig_trend"
    assert rows[0]["avg_cost"] == pytest.approx(PRICE)
    assert rows[0]["con_id"] == 0
    with sqlite3.connect(tmp_path / "data" / "trading.db") as conn:
        stored = conn.execute(
            "SELECT price, quantity, fee, perm_id, exchange, booked FROM executions"
        ).fetchone()
    assert stored == (PRICE, QTY, FEE, PERM_ID, "ARCA", 1)

    second = ingest_broker_fills(tmp_path, [_spy_fill(order)], now=WHEN)
    assert second["recorded"] == []
    assert second["booked"] == []
    assert second["skipped"] == [EXEC_ID]
    assert _execution_count(tmp_path) == 1
    assert _position_qty(tmp_path) == pytest.approx(10)
    assert TradeJournal(tmp_path).order_state(order) == "FILLED"

    cleared = recon_once(tmp_path, WHEN)
    assert cleared["freeze"] is False
    assert cleared["breaks"] == []


def test_partial_fills_then_a_replay_do_not_double_book(tmp_path: Path) -> None:
    order = _seed(tmp_path, intent_id="partial-spy", symbol="AAPL", qty=10)
    first = {
        "exec_id": "exec-a",
        "order_ref": order,
        "symbol": "AAPL",
        "side": "BUY",
        "qty": 4,
        "price": 100.0,
        "account": ACCOUNT,
        "time": "2026-10-16T13:00:00+00:00",
    }
    second = {
        "exec_id": "exec-b",
        "order_ref": order,
        "symbol": "AAPL",
        "side": "BOT",
        "qty": 6,
        "price": 101.0,
        "account": ACCOUNT,
        "time": "2026-10-16T13:05:00+00:00",
    }
    opened = ingest_broker_fills(tmp_path, [first], now=WHEN)
    assert opened["states"][order] == "PARTIALLY_FILLED"
    assert _position_qty(tmp_path, "AAPL") == pytest.approx(4)
    done = ingest_broker_fills(tmp_path, [first, second], now=WHEN)
    assert done["recorded"] == ["exec-b"]
    assert done["states"][order] == "FILLED"
    assert _position_qty(tmp_path, "AAPL") == pytest.approx(10)
    again = ingest_broker_fills(tmp_path, [second, first], now=WHEN)
    assert again["recorded"] == []
    assert _execution_count(tmp_path) == 2
    assert _position_qty(tmp_path, "AAPL") == pytest.approx(10)


def test_unmatched_and_non_paper_fills_are_not_booked(tmp_path: Path) -> None:
    order = _seed(tmp_path)
    result = ingest_broker_fills(
        tmp_path,
        [
            _spy_fill(order, exec_id="other-account", account="U1234567"),
            _spy_fill("missing-order", exec_id="missing"),
        ],
        now=WHEN,
    )
    assert result["recorded"] == []
    assert result["unmatched"] == ["missing"]
    assert _execution_count(tmp_path) == 0
    assert _position_qty(tmp_path) == 0


def test_submit_records_fills_while_readonly_and_oms_reads_the_snapshot(tmp_path: Path) -> None:
    order = _seed(tmp_path)
    recorded = submit_once(
        tmp_path,
        WHEN,
        intents=[],
        fills=[_spy_fill(order)],
        readonly=True,
    )
    assert recorded["status"] == "readonly"
    assert recorded["sent"] == 0
    assert recorded["fills_recorded"] == 1
    assert TradeJournal(tmp_path).order_state(order) == "FILLED"

    other = _seed(tmp_path, intent_id="oms-fill", symbol="QQQ", qty=2, strategy="sig_trend")
    write_execution_snapshot(
        tmp_path,
        WHEN,
        [
            {
                "exec_id": "oms-exec",
                "order_ref": other,
                "symbol": "QQQ",
                "side": "BUY",
                "qty": 2,
                "price": 400.0,
                "account": ACCOUNT,
                "time": "2026-10-16T13:35:08+00:00",
            }
        ],
    )
    oms = oms_once(tmp_path, WHEN)
    assert "fills" not in oms["errors"]
    assert TradeJournal(tmp_path).order_state(other) == "FILLED"
    assert _position_qty(tmp_path, "QQQ") == pytest.approx(2)
    replay = oms_once(tmp_path, WHEN)
    assert "fills" not in replay["errors"]
    assert _position_qty(tmp_path, "QQQ") == pytest.approx(2)


def test_normalize_reads_an_ib_fill_and_requests_every_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Execution:
        execId = EXEC_ID
        orderRef = "abc"
        permId = PERM_ID
        side = "BOT"
        shares = 10
        price = PRICE
        acctNumber = ACCOUNT
        exchange = "ARCA"
        cumQty = 10
        time = WHEN

    class Contract:
        symbol = "SPY"
        conId = 42

    class Report:
        commission = 1.7976931348623157e308
        currency = "USD"

    raw = types.SimpleNamespace(
        execution=Execution(),
        contract=Contract(),
        commissionReport=Report(),
    )
    normalized = normalize_execution(raw)
    assert normalized is not None
    assert normalized["exec_id"] == EXEC_ID
    assert normalized["side"] == "BUY"
    assert normalized["fee"] is None
    assert normalized["con_id"] == 42
    assert normalized["perm_id"] == PERM_ID

    seen: dict[str, object] = {}

    class Filt:
        def __init__(self) -> None:
            self.clientId = 7
            self.acctCode = ""

    class IB:
        async def reqExecutionsAsync(self, filt: Filt) -> list[object]:
            seen["client"] = filt.clientId
            seen["acct"] = filt.acctCode
            return [raw]

    fake = types.ModuleType("ib_async")
    fake.ExecutionFilter = Filt
    monkeypatch.setitem(sys.modules, "ib_async", fake)
    rows = asyncio.run(read_ib_executions(IB(), ACCOUNT))
    assert seen == {"client": 0, "acct": ACCOUNT}
    assert rows[0]["exec_id"] == EXEC_ID
    assert rows[0]["fee"] is None


def test_execution_migration_accepts_an_older_table(tmp_path: Path) -> None:
    db = tmp_path / "data" / "trading.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE executions (
                fill_id TEXT PRIMARY KEY,
                intent_id TEXT,
                client_id TEXT,
                symbol TEXT,
                price REAL,
                quantity REAL,
                fee REAL,
                ts_utc TEXT
            )
            """
        )
    journal = TradeJournal(tmp_path)
    journal.record_fill(
        fill_id="old",
        intent_id="i",
        client_id="c",
        symbol="SPY",
        price=1.0,
        quantity=1,
        perm_id=5,
        exchange="ARCA",
    )
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(executions)")}
        booked = conn.execute("SELECT booked, perm_id FROM executions").fetchone()
    assert {"perm_id", "exchange", "booked"} <= columns
    assert booked == (0, 5)


def test_spy_backfill_is_reversible_and_idempotent(tmp_path: Path) -> None:
    _seed(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "account.json").write_text(
        json.dumps(
            {
                "positions": [{"symbol": "SPY", "qty": 10, "currency": "USD"}],
                "open_orders": [],
            }
        ),
        encoding="utf-8",
    )
    assert _backfill_main(["--home", str(tmp_path)]) == 0
    assert not TradeJournal(tmp_path).has_execution(EXEC_ID)

    plan = inspect_plan(tmp_path)
    assert plan["order_state"] == "SUBMITTED"
    assert plan["fee"] == 1.0
    assert FEE == 1.0

    applied = apply_backfill(tmp_path, now=WHEN)
    assert applied["ingest"]["states"][order_id()] == "FILLED"
    assert applied["recon"]["freeze"] is False
    backup = tmp_path / "data" / "backups" / "trading-before-spy-20261016.db"
    assert backup.is_file()
    backup_bytes = backup.read_bytes()
    again = apply_backfill(tmp_path, now=WHEN)
    assert backup.read_bytes() == backup_bytes
    assert again["ingest"]["recorded"] == []
    assert _position_qty(tmp_path) == pytest.approx(10)
    assert _execution_count(tmp_path) == 1

    undone = undo_backfill(tmp_path, now=WHEN)
    assert undone["restored"] is True
    assert TradeJournal(tmp_path).has_execution(EXEC_ID) is False
    assert TradeJournal(tmp_path).order_state(order_id()) == "SUBMITTED"
    assert _position_qty(tmp_path) == 0
    assert undo_backfill(tmp_path, now=WHEN)["restored"] is False


def test_backfill_refuses_a_missing_order_and_a_later_execution(tmp_path: Path) -> None:
    TradeJournal(tmp_path)
    with pytest.raises(Exception, match="not in the journal") as missing:
        inspect_plan(tmp_path)
    assert missing.value.code == 2

    _seed(tmp_path)
    apply_backfill(tmp_path, now=WHEN)
    journal = TradeJournal(tmp_path)
    journal.record_fill(
        fill_id="later",
        intent_id=INTENT_ID,
        client_id=order_id(),
        symbol="SPY",
        price=1.0,
        quantity=1,
        when=WHEN,
    )
    with pytest.raises(Exception, match="undo refused"):
        undo_backfill(tmp_path, now=WHEN)
    assert TradeJournal(tmp_path).has_execution(EXEC_ID) is True
