"""Section 8: confirmed pins, a working paper submit path, and watchdog leftovers."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.execution.ibkr import PaperGateway
from auto_trading.execution.oms import order_ref
from auto_trading.execution.pricing import reference_limit
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.execution.services import (
    account_once,
    chrony_offset_sec,
    chrony_unsynchronised,
    gateway_restart_argv,
    load_intent_orders,
    parse_chrony_tracking,
    probe_gateway_login,
    recon_once,
    restart_gateway,
    submit_once,
    watchdog_once,
)
from auto_trading.execution.strategy_book import (
    CONFIRMED_SPEC_HASHES,
    confirmed_source_errors,
    load_approved,
    r25_code,
)
from auto_trading.market.quotes import write_quotes
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.strategy.spec import spec_hash

PIN = "DUR243736"
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 2, 22, 0, tzinfo=ZoneInfo("Asia/Macau"))
DIGEST = CONFIRMED_SPEC_HASHES[("ta_breakout52", 1)]


def _ready(home: Path, monkeypatch, *, at: datetime | None = None) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    moment = at or NOW
    write_risk_limits(home, build_risk_limits(now=moment, nlv=1_000_000, account=PIN))
    account_once(
        home,
        moment,
        snapshot={
            "accounts": [PIN],
            "nlv": 1_000_000.0,
            "positions": [],
            "open_orders": [],
        },
    )


def _quote(home: Path, when: datetime, *, age: timedelta) -> None:
    received = when - age
    write_quotes(
        home,
        when,
        [
            {
                "symbol": "AAPL",
                "last": 100.0,
                "as_of": (received - timedelta(minutes=15)).isoformat(),
                "received_at": received.isoformat(),
                "source": "ibkr",
                "data_type": "delayed",
                "market_data_type": 3,
            }
        ],
    )


def _intent_file(home: Path, name: str, **over: object) -> None:
    row = {
        "intent_id": name,
        "intent_type": "ENTRY",
        "ticker": "AAPL",
        "market": "US",
        "currency": "USD",
        "side": "BUY",
        "qty": 10,
        "lot_size": 1,
        "limit_offset_bps": 10,
        "ref_price_at_create": 100.0,
        "valid_from": "2026-10-02T21:30:00+08:00",
        "strategy_id": "ta_breakout52",
    }
    envelope = {
        "schema_version": "intent/1.0",
        "strategy_id": "ta_breakout52",
        "strategy_version": 1,
        "spec_hash": DIGEST,
        "mode": "paper",
        "live_size_multiplier": None,
    }
    row.update({key: value for key, value in over.items() if key in row})
    envelope.update({key: value for key, value in over.items() if key not in row})
    folder = home / "intents"
    folder.mkdir(parents=True, exist_ok=True)
    envelope["intents"] = [row]
    (folder / f"{name}.json").write_text(json.dumps(envelope), encoding="utf-8")


def test_pins_are_in_git_config_and_ci_requires_three_sources() -> None:
    assert confirmed_source_errors(ROOT) == []
    assert CONFIRMED_SPEC_HASHES[("sig_trend", 3)].startswith("sha256:9877996947f0")
    assert CONFIRMED_SPEC_HASHES[("sig_pullback", 3)].startswith("sha256:d2449671d312")
    assert DIGEST.startswith("sha256:86046b4ceb98")
    for name in ("sync_box_to_vps.txt", "sync_vps_to_box.txt"):
        text = (ROOT / "vps" / "deploy" / name).read_text(encoding="utf-8")
        assert "confirmed_strategies.json" not in text


def test_sig_new_registry_row_never_reaches_the_broker(tmp_path: Path, monkeypatch) -> None:
    _ready(tmp_path, monkeypatch)
    shutil.copytree(ROOT / "strategies", tmp_path / "strategies")
    source = load_approved(ROOT / "strategies" / "approved" / "sig_trend.v3.json")
    source["id"] = "sig_new"
    source["version"] = 1
    source["execution"]["approval"] = "auto"
    target = tmp_path / "strategies" / "approved" / "sig_new.v1.json"
    target.write_text(json.dumps(source), encoding="utf-8")
    digest = spec_hash(load_approved(target))
    with (tmp_path / "strategies" / "registry.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "ts": "2026-10-13T00:00:00+00:00",
                    "strategy_id": "sig_new",
                    "version": 1,
                    "spec_hash": digest,
                    "event": "approved",
                    "actor": "user",
                    "mode_after": "paper",
                }
            )
            + "\n"
        )
    assert (
        r25_code(
            strategy_id="sig_new",
            version=1,
            spec_hash_value=digest,
            root=tmp_path / "strategies",
            opening=True,
        )
        == "R25"
    )
    _quote(tmp_path, NOW, age=timedelta(minutes=10))
    _intent_file(
        tmp_path,
        "r1_registry_only",
        strategy_id="sig_new",
        strategy_version=1,
        spec_hash=digest,
    )
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
    assert sent == []
    assert "R25" in result["rejected"]
    pending = tmp_path / "approvals" / "pending"
    assert not pending.is_dir() or list(pending.glob("*.json")) == []


def test_anonymous_opening_is_r25_and_not_queued(tmp_path: Path, monkeypatch) -> None:
    _ready(tmp_path, monkeypatch)
    _quote(tmp_path, NOW, age=timedelta(minutes=1))
    folder = tmp_path / "intents"
    folder.mkdir()
    (folder / "anon.json").write_text(
        json.dumps(
            {
                "schema_version": "intent/1.0",
                "mode": "paper",
                "intents": [
                    {
                        "intent_id": "anon-1",
                        "intent_type": "ENTRY",
                        "ticker": "AAPL",
                        "market": "US",
                        "currency": "USD",
                        "side": "BUY",
                        "qty": 10,
                        "limit_price": 100,
                        "valid_from": "2026-10-02T21:30:00+08:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
    assert sent == []
    assert result["rejected"] == ["R25"]
    assert not (tmp_path / "approvals" / "pending").exists()


def test_delayed_quote_received_within_10_seconds_can_send(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    _ready(tmp_path, monkeypatch)
    _quote(tmp_path, NOW, age=timedelta(seconds=1))
    _intent_file(tmp_path, "n1")
    sent: list[OrderIntent] = []
    with caplog.at_level("INFO"):
        result = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
    assert result["sent"] == 1
    assert (
        sent[0].limit_price
        == reference_limit(
            market="US",
            side="BUY",
            ref_price=100.0,
            offset_bps=10,
            cap=None,
        )[0]
    )
    assert sent[0].quote_data_type == "delayed"
    assert "delayed_quote" in caplog.text
    assert sent[0].client_id == order_ref("n1", 1)
    book = recon_once(
        tmp_path,
        NOW,
        broker_orders=[{"order_id": sent[0].client_id, "symbol": "AAPL"}],
        broker_qty={},
        journal_qty={},
    )
    assert book["freeze"] is False
    mismatched = recon_once(
        tmp_path,
        NOW,
        broker_orders=[{"order_id": "n1", "symbol": "AAPL"}],
        broker_qty={},
        journal_qty={},
    )
    assert mismatched["freeze"] is True


def test_quote_received_more_than_10_seconds_ago_is_r22(tmp_path: Path, monkeypatch) -> None:
    _ready(tmp_path, monkeypatch)
    _quote(tmp_path, NOW, age=timedelta(seconds=11))
    _intent_file(tmp_path, "old-quote")
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
    assert sent == []
    assert "R22" in result["rejected"]


def test_stale_intent_is_rejected_and_leaves_the_queue(tmp_path: Path, monkeypatch) -> None:
    _ready(tmp_path, monkeypatch)
    _quote(tmp_path, NOW, age=timedelta(0))
    _intent_file(tmp_path, "stale", valid_from="2026-10-01T21:30:00+08:00")
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
    assert sent == []
    assert result["rejected"] == ["INTENT_EXPIRED"]
    assert not (tmp_path / "intents" / "stale.json").exists()
    assert (tmp_path / "intents" / "done" / "stale.json").is_file()
    again = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
    assert again["rejected"] == []
    assert load_intent_orders(tmp_path) == []


def test_stale_account_snapshot_is_not_connected(tmp_path: Path, monkeypatch) -> None:
    _ready(tmp_path, monkeypatch, at=NOW - timedelta(minutes=10))
    _quote(tmp_path, NOW, age=timedelta(0))
    _intent_file(tmp_path, "stale-account")
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
    assert sent == []
    assert "R17" in result["rejected"]


def test_same_rejection_is_logged_once_and_a_cancelled_order_leaves(
    tmp_path: Path, monkeypatch
) -> None:
    _ready(tmp_path, monkeypatch)
    _quote(tmp_path, NOW, age=timedelta(minutes=31))
    _intent_file(tmp_path, "repeat")
    for _ in range(3):
        submit_once(tmp_path, NOW, place=lambda _intent: None, readonly=False)
    with sqlite3.connect(tmp_path / "data" / "trading.db") as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM risk_checks WHERE intent_id = 'repeat' AND code = 'R22'"
        ).fetchone()
    assert count is not None
    assert count[0] == 1
    _quote(tmp_path, NOW, age=timedelta(0))
    (tmp_path / "intents" / "repeat.json").unlink()
    _intent_file(tmp_path, "done-me")
    sent: list[OrderIntent] = []
    first = submit_once(tmp_path, NOW, place=sent.append, readonly=False)
    assert first["sent"] == 1
    order_id = sent[0].client_id
    TradeJournal(tmp_path).set_order_state(order_id, "CANCELLED", when=NOW)
    sent.clear()
    submit_once(tmp_path, NOW, place=sent.append, readonly=False)
    assert sent == []
    assert (tmp_path / "intents" / "done" / "done-me.json").is_file()


def test_watchdog_uses_system_time_and_restarts_only_gateway_faults(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    skewed = (
        "Reference ID    : A9FEA97B (169.254.169.123)\n"
        "System time     : 6.000000 seconds fast of NTP time\n"
        "Last offset     : +0.000100 seconds\n"
        "Leap status     : Normal\n"
    )
    unsynced = (
        "Reference ID    : 00000000 ()\n"
        "System time     : 0.000010 seconds fast of NTP time\n"
        "Last offset     : +0.000020 seconds\n"
        "Leap status     : Not synchronised\n"
    )
    assert parse_chrony_tracking(skewed) == 6.0
    assert chrony_unsynchronised(skewed) is False
    assert chrony_unsynchronised(unsynced) is True

    class Completed:
        def __init__(self, text: str) -> None:
            self.returncode = 0
            self.stdout = text
            self.stderr = ""

    monkeypatch.setattr(
        "auto_trading.execution.services.subprocess.run",
        lambda *_args, **_kwargs: Completed(unsynced),
    )
    assert chrony_offset_sec() is None
    monkeypatch.setattr(
        "auto_trading.execution.services.gateway_probe",
        lambda home, now: {"logged_in": True, "accounts": [PIN], "detail": "ok"},
    )
    monkeypatch.setattr("auto_trading.execution.services.disk_usage_ratio", lambda home: 0.5)
    monkeypatch.setattr("auto_trading.execution.services.chrony_offset_sec", lambda: None)
    calls: list[bool] = []
    monkeypatch.setattr(
        "auto_trading.execution.services.restart_gateway",
        lambda: calls.append(False) or False,
    )
    for _ in range(3):
        watchdog_once(tmp_path, NOW)
    clock = json.loads((tmp_path / "state" / "watchdog.json").read_text(encoding="utf-8"))
    assert "clock_skew" in clock["issues"]
    assert clock["gateway_restart"] is False
    assert calls == []
    monkeypatch.setattr(
        "auto_trading.execution.services.gateway_probe",
        lambda home, now: {"logged_in": False, "accounts": [], "detail": "down"},
    )
    monkeypatch.setattr("auto_trading.execution.services.chrony_offset_sec", lambda: 0.0)
    monkeypatch.setattr("auto_trading.execution.services.disk_usage_ratio", lambda home: 0.1)
    for _ in range(3):
        watchdog_once(tmp_path, NOW)
    report = json.loads((tmp_path / "state" / "watchdog.json").read_text(encoding="utf-8"))
    assert report["gateway_restart"] is True
    assert report["gateway_restarted"] is False
    assert calls == [False]
    kinds = []
    for path in (tmp_path / "logs").glob("critical_*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            kinds.append(json.loads(line)["kind"])
    assert "gateway_restart_failed" in kinds


def test_restart_argv_is_the_sudoers_command(monkeypatch) -> None:
    rule = (ROOT / "vps" / "deploy" / "trader-ibgateway.sudoers").read_text(encoding="utf-8")
    assert "trader" in rule
    assert "/usr/bin/systemctl restart ibgateway.service" in rule
    assert "stop" not in rule
    readme = (ROOT / "vps" / "deploy" / "README.md").read_text(encoding="utf-8")
    assert "chrony" in readme
    assert "trader-ibgateway.sudoers" in readme
    argv = gateway_restart_argv()
    assert argv == [
        "sudo",
        "--non-interactive",
        "/usr/bin/systemctl",
        "restart",
        "ibgateway.service",
    ]
    monkeypatch.delenv("TRADING_GATEWAY_CONNECT", raising=False)
    assert restart_gateway() is False

    class Completed:
        def __init__(self, code: int) -> None:
            self.returncode = code
            self.stdout = ""
            self.stderr = ""

    monkeypatch.setenv("TRADING_GATEWAY_CONNECT", "1")
    monkeypatch.setattr(
        "auto_trading.execution.services.subprocess.run",
        lambda *_args, **_kwargs: Completed(1),
    )
    assert restart_gateway() is False
    monkeypatch.setattr(
        "auto_trading.execution.services.subprocess.run",
        lambda *_args, **_kwargs: Completed(0),
    )
    assert restart_gateway() is True


def test_probe_and_submit_disconnect(monkeypatch) -> None:
    closed: list[int] = []

    monkeypatch.setattr(
        "auto_trading.config.get_settings",
        lambda: SimpleNamespace(),
    )

    class FakeGateway:
        def __init__(self, settings, *, client_id: int) -> None:
            self.client_id = client_id

        async def connect(self) -> list[str]:
            raise RuntimeError("down")

        def disconnect(self) -> None:
            closed.append(self.client_id)

    monkeypatch.setattr("auto_trading.execution.ibkr.PaperGateway", FakeGateway)
    assert probe_gateway_login()["logged_in"] is False
    assert closed == [14]

    gateway = PaperGateway.__new__(PaperGateway)
    gateway._ib = None
    gateway.client_id = 11
    gateway.settings = SimpleNamespace(
        ibkr_port=4002,
        ibkr_paper_account=PIN,
        trading_home=Path("/tmp"),
    )
    flags: list[str] = []

    class Socket:
        def disconnect(self) -> None:
            flags.append("off")

    async def connect() -> list[str]:
        gateway._ib = Socket()
        return [PIN]

    async def _submit_connected(_intent: object) -> str:
        return "placed"

    gateway.connect = connect  # type: ignore[method-assign]
    gateway._submit_connected = _submit_connected  # type: ignore[method-assign]
    result = asyncio.run(
        PaperGateway.submit_order(
            gateway,
            OrderIntent(client_id="x", symbol="AAPL", side=OrderSide.BUY, quantity=1),
        )
    )
    assert result == "placed"
    assert gateway._ib is None
    assert flags == ["off"]
