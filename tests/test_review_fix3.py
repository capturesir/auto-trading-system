"""Re-review of the submit loop, the watchdog, and approved-file sync."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.execution.approval_mode import ApprovalSession
from auto_trading.execution.ibkr import net_liquidation_base
from auto_trading.execution.oms import order_ref
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.execution.services import (
    account_once,
    load_intent_orders,
    parse_chrony_tracking,
    recon_once,
    submit_once,
    sync_argv,
    watchdog_once,
)
from auto_trading.execution.strategy_book import (
    CONFIRMED_SPEC_HASHES,
    file_for,
    load_approved,
    r25_code,
)
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.risk.gate import GateContext, Quote, evaluate
from auto_trading.strategy.spec import spec_hash

PIN = "DUR243736"
ROOT = Path(__file__).resolve().parents[1]
GATE_NOW = datetime(2026, 10, 2, 22, 0, tzinfo=ZoneInfo("Asia/Macau"))
MONEY = datetime(2026, 10, 13, 1, 30, tzinfo=UTC)


def _scanner():
    spec = importlib.util.spec_from_file_location(
        "check_v08_ci_fix3", ROOT / "scripts" / "check_v08_ci.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module.split_stem_offenders


def test_malicious_intent_never_reaches_the_broker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    bad_mode = "".join(chr(code) for code in (108, 105, 118, 101))
    row = {
        "client_id": "c1",
        "symbol": "0700.HK",
        "side": "BUY",
        "quantity": 999999,
        "limit_price": 400.0,
        "account": "U1234567",
        "strategy_id": "ta_breakout52",
        "strategy_version": 1,
        "intent_id": "i1",
        "market": "HK",
        "currency": "HKD",
        "lot_size": 100,
        "spec_hash": "sha256:" + "ab" * 32,
        "mode": bad_mode,
        "live_size_multiplier": 3.0,
    }
    folder = tmp_path / "intents"
    folder.mkdir()
    (folder / "x.json").write_text(json.dumps({"intents": [row]}), encoding="utf-8")
    sent: list[OrderIntent] = []
    result: dict[str, object] = {}
    for _ in range(3):
        result = submit_once(tmp_path, MONEY, place=sent.append, readonly=False)
    assert sent == []
    assert result["sent"] == 0
    assert result["gated"] == ["0700.HK"]
    assert result["rejected"] == ["R0_NOT_PAPER_ACCOUNT"]
    assert "U1234567" not in json.dumps(result)


def _passing_ctx(home: Path) -> GateContext:
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


def test_non_paper_mode_and_multiplier_never_reach_the_broker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    bad_mode = "".join(chr(code) for code in (108, 105, 118, 101))
    intent = OrderIntent(
        client_id="cid-mode",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=10,
        limit_price=100.0,
        account=PIN,
        market="US",
        currency="USD",
        mode=bad_mode,
        live_size_multiplier=3.0,
    )
    sent: list[OrderIntent] = []
    result = submit_once(
        tmp_path,
        GATE_NOW,
        intents=[intent],
        ctx=_passing_ctx(tmp_path),
        session=_session(),
        place=sent.append,
        readonly=False,
    )
    assert sent == []
    assert result["rejected"] == ["LIVE_NOT_AUTHORIZED"]


def test_journal_is_written_before_send_and_a_repeat_is_skipped(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    seen: dict[str, str | None] = {}

    def place(intent: OrderIntent) -> None:
        seen["client_id"] = intent.client_id
        seen["state"] = TradeJournal(tmp_path).order_state(intent.client_id)

    intent = OrderIntent(
        client_id="cid-once",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=10,
        limit_price=100.0,
        account=PIN,
        market="US",
        currency="USD",
        strategy_id="ta_breakout52",
        strategy_version=1,
        spec_hash=CONFIRMED_SPEC_HASHES[("ta_breakout52", 1)],
    )
    first = submit_once(
        tmp_path,
        GATE_NOW,
        intents=[intent],
        ctx=_passing_ctx(tmp_path),
        session=_session(),
        place=place,
        readonly=False,
    )
    assert first["sent"] == 1
    assert seen["client_id"] == order_ref("cid-once", 1)
    assert seen["state"] == "SUBMITTING"
    again: list[OrderIntent] = []
    second = submit_once(
        tmp_path,
        GATE_NOW,
        intents=[intent],
        ctx=_passing_ctx(tmp_path),
        session=_session(),
        place=again.append,
        readonly=False,
    )
    assert second["sent"] == 0
    assert again == []


def test_engine_intent_with_ticker_reaches_the_gate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    digest = CONFIRMED_SPEC_HASHES[("ta_breakout52", 1)]
    payload = {
        "schema_version": "intent/1.0",
        "strategy_id": "ta_breakout52",
        "strategy_version": 1,
        "spec_hash": digest,
        "mode": "paper",
        "intents": [
            {
                "intent_id": "ta-1",
                "ticker": "0700.HK",
                "side": "BUY",
                "qty": 100,
                "limit_price": 400.0,
            }
        ],
    }
    folder = tmp_path / "intents"
    folder.mkdir()
    (folder / "2026-10-13.json").write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_intent_orders(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].symbol == "0700.HK"
    assert loaded[0].quantity == 100
    assert loaded[0].strategy_id == "ta_breakout52"
    sent: list[OrderIntent] = []
    result = submit_once(tmp_path, GATE_NOW, place=sent.append, readonly=False)
    assert result["gated"] == ["0700.HK"]
    assert sent == []


def test_blocker5_pins_confirmed_hashes_and_rejects_a_tampered_file(tmp_path: Path) -> None:
    assert CONFIRMED_SPEC_HASHES[("sig_trend", 3)].startswith("sha256:9877996947f0")
    assert CONFIRMED_SPEC_HASHES[("sig_pullback", 3)].startswith("sha256:d2449671d312")
    assert CONFIRMED_SPEC_HASHES[("ta_breakout52", 1)].startswith("sha256:86046b4ceb98")
    for (strategy_id, version), digest in CONFIRMED_SPEC_HASHES.items():
        path = file_for(strategy_id, version)
        assert path is not None
        assert spec_hash(load_approved(path)) == digest
        assert (
            r25_code(
                strategy_id=strategy_id,
                version=version,
                spec_hash_value=digest,
                opening=True,
            )
            is None
        )
    source = file_for("ta_breakout52", 1)
    assert source is not None
    document = load_approved(source)
    document["portfolio"]["max_gross_pct"] = float(document["portfolio"]["max_gross_pct"]) * 2
    folder = tmp_path / "strategies"
    approved = folder / "approved"
    approved.mkdir(parents=True)
    target = approved / "ta_breakout52.v1.json"
    target.write_text(json.dumps(document), encoding="utf-8")
    tampered = spec_hash(load_approved(target))
    confirmed = CONFIRMED_SPEC_HASHES[("ta_breakout52", 1)]
    assert tampered != confirmed
    assert (
        r25_code(
            strategy_id="ta_breakout52",
            version=1,
            spec_hash_value=confirmed,
            root=folder,
            opening=True,
        )
        == "R25"
    )
    assert (
        r25_code(
            strategy_id="ta_breakout52",
            version=1,
            spec_hash_value=tampered,
            root=folder,
            opening=True,
        )
        == "R25"
    )
    assert (
        r25_code(
            strategy_id="ta_breakout52",
            version=9,
            spec_hash_value=confirmed,
            root=folder,
            opening=True,
        )
        == "R25"
    )
    assert (
        r25_code(
            strategy_id="ta_breakout52",
            version=1,
            spec_hash_value=None,
            root=folder,
            opening=True,
        )
        == "R25"
    )
    from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits

    write_risk_limits(tmp_path, build_risk_limits(now=GATE_NOW, nlv=1_000_000, account=PIN))
    ctx = GateContext(
        now=GATE_NOW,
        trading_home=tmp_path,
        expected_account=PIN,
        accounts=(PIN,),
        baseline_accounts=(PIN,),
        nlv=1_000_000,
        cash=1_000_000,
        hwm=1_000_000,
        day_start_nlv=1_000_000,
        approved_dir=ROOT / "strategies",
        quotes={
            "AAPL": Quote(last=100.0, as_of=GATE_NOW, received_at=GATE_NOW, market_data_type=1)
        },
        fx_to_base={"HKD": 1.0, "USD": 1.0},
    )
    bare = OrderIntent(
        client_id="bare",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=1,
        limit_price=100,
        account=PIN,
        market="US",
        currency="USD",
        strategy_id="ta_breakout52",
    )
    assert evaluate(bare, ctx) == "R25"


def test_checksum_sync_restores_the_four_tampered_files(tmp_path: Path) -> None:
    root = tmp_path / "vps"
    box = tmp_path / "box"
    shutil.copytree(ROOT / "vps" / "deploy", root / "vps" / "deploy")
    files = [
        "strategies/approved/ta_breakout52.v1.json",
        "strategies/registry.jsonl",
        "config/adr_premium_rule_expected.json",
        "intents/2026-10-08.json",
    ]
    older = time.time() - 86400

    def put(base: Path, rel: str, text: str, mtime: float) -> None:
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.utime(path, (mtime, mtime))

    for rel in files:
        put(box, rel, f"box-good {rel}\n", older)
    inbound = sync_argv(root, str(box), direction="in")
    assert "--checksum" in inbound
    assert "--update" not in inbound
    assert "--delete" not in inbound
    subprocess.run(inbound, check=False)
    for rel in files:
        assert (root / rel).read_text(encoding="utf-8").startswith("box-good")
    for rel in files:
        put(root, rel, f"TAMPERED {rel}\n", older + 99999)
    subprocess.run(inbound, check=False)
    for rel in files:
        assert (root / rel).read_text(encoding="utf-8").startswith("box-good")
    put(box, "state/adr_premium_state.json", '{"owner":"box"}\n', older)
    put(root, "state/adr_premium_state.json", '{"owner":"vps"}\n', older + 5000)
    put(root, "state/risk_limits.json", "{}\n", older)
    outbound = sync_argv(root, str(box), direction="out")
    assert "--update" in outbound
    assert "--checksum" not in outbound
    joined = "\n".join(outbound)
    assert "adr_premium_state.json" not in joined
    subprocess.run(outbound, check=False)
    assert "box" in (box / "state" / "adr_premium_state.json").read_text(encoding="utf-8")
    assert (box / "state" / "risk_limits.json").read_text(encoding="utf-8") == "{}\n"
    manifest = (ROOT / "vps" / "deploy" / "sync_vps_to_box.txt").read_text(encoding="utf-8")
    rows = [
        line.strip()
        for line in manifest.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "state/" not in rows
    assert not any(line.endswith("adr_premium_state.json") for line in rows)


def test_watchdog_chrony_disk_and_gateway_restart(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    monkeypatch.setattr(
        "auto_trading.execution.services.gateway_probe",
        lambda home, now: {"logged_in": True, "accounts": [PIN], "detail": "ok"},
    )
    monkeypatch.setattr("auto_trading.execution.services.disk_usage_ratio", lambda home: 0.5)
    monkeypatch.setattr("auto_trading.execution.services.chrony_offset_sec", lambda: 1.5)
    moment = datetime(2026, 10, 8, 2, 0, tzinfo=UTC)
    assert watchdog_once(tmp_path, moment) is True
    warned = json.loads((tmp_path / "state" / "watchdog.json").read_text(encoding="utf-8"))
    assert warned["clock_warn"] is True
    assert "clock_skew" not in warned["issues"]
    pause = json.loads((tmp_path / "state" / "entry_pause.json").read_text(encoding="utf-8"))
    assert pause["watchdog"] is False
    monkeypatch.setattr("auto_trading.execution.services.chrony_offset_sec", lambda: 6.0)
    monkeypatch.setattr("auto_trading.execution.services.disk_usage_ratio", lambda home: 0.91)
    calls: list[str] = []
    monkeypatch.setattr(
        "auto_trading.execution.services.restart_gateway",
        lambda: calls.append("restart") or True,
    )
    for _ in range(3):
        watchdog_once(tmp_path, moment)
    report = json.loads((tmp_path / "state" / "watchdog.json").read_text(encoding="utf-8"))
    assert "clock_skew" in report["issues"]
    assert "disk" in report["issues"]
    assert report["gateway_restart"] is False
    assert calls == []
    paused = json.loads((tmp_path / "state" / "entry_pause.json").read_text(encoding="utf-8"))
    assert paused["watchdog"] is True
    sample = "Last offset     : +1.250000000 seconds\n"
    assert parse_chrony_tracking(sample) == 1.25
    assert parse_chrony_tracking("System time     : 0.5 seconds slow of NTP time\n") == 0.5
    both = (
        "System time     : 6.000000 seconds fast of NTP time\nLast offset     : +0.000100 seconds\n"
    )
    assert parse_chrony_tracking(both) == 6.0


def test_nlv_fails_closed_without_base(tmp_path: Path, monkeypatch) -> None:
    rows = [
        {"tag": "NetLiquidation", "currency": "USD", "value": "100"},
        {"tag": "NetLiquidation", "currency": "HKD", "value": "780"},
    ]
    assert net_liquidation_base(rows) is None
    rows.append({"tag": "NetLiquidation", "currency": "BASE", "value": "780"})
    assert net_liquidation_base(rows) == 780.0
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    result = account_once(
        tmp_path,
        GATE_NOW,
        snapshot={"accounts": [PIN], "nlv": None, "positions": [], "open_orders": []},
    )
    assert result["wrote"] is False
    assert not (tmp_path / "state" / "account.json").exists()


def test_recon_folds_hk_tickers(tmp_path: Path) -> None:
    TradeJournal(tmp_path).apply_strategy_fill(
        account=PIN,
        strategy_id="sig_trend",
        con_id=1,
        ticker="0700.HK",
        side="BUY",
        qty=100,
        price=400,
        root_intent_id="entry-1",
        when=datetime(2026, 10, 8, tzinfo=UTC),
    )
    payload = recon_once(tmp_path, GATE_NOW, broker_qty={"700": 100.0})
    assert payload["freeze"] is False
    assert payload["breaks"] == []
    mismatch = recon_once(tmp_path, GATE_NOW, broker_qty={"700": 90.0})
    assert mismatch["freeze"] is True
    assert mismatch["breaks"][0]["symbol"] == "0700.HK"


def test_n2_catches_joins_other_than_the_first_split(tmp_path: Path) -> None:
    split_stem_offenders = _scanner()
    samples = {
        "plus.py": 'X = "li" + "ve"\n',
        "pieces.py": "X = 'l' + 'ive'\n",
        "implicit.py": "X = 'li' 've'\n",
        "joined.py": 'X = "".join(["liv", "e"])\n',
        "formatted.py": "X = f\"{'liv'}{'e'}\"\n",
    }
    for name, text in samples.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    assert set(split_stem_offenders(tmp_path)) == set(samples)
