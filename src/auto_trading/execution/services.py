"""Long-running paper services.

``main`` is the loop. ``oms``, ``recon``, ``watchdog``, and ``sync`` are the
four processes systemd starts. Nothing here opens a real-account session.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from auto_trading.alerts.critical import write_ops_notice
from auto_trading.alerts.forwarder import scan_once
from auto_trading.execution.breakers import (
    maybe_auto_clear_r29,
    refresh_r30,
    risk_window,
)
from auto_trading.execution.heartbeat import heartbeat_alert_due, write_heartbeat
from auto_trading.execution.oms import OrderMachine
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.execution.sync import reconcile
from auto_trading.market.calendar import is_session
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.reports.daily_summary import write_daily_summary
from auto_trading.risk.r9_state import catch_up, close_risk_day, observe_nlv

MACAU = ZoneInfo("Asia/Macau")
HKT = ZoneInfo("Asia/Hong_Kong")
OMS_INTERVAL_SEC = 10
RECON_INTERVAL_SEC = 60
WATCHDOG_INTERVAL_SEC = 60


def _now() -> datetime:
    return datetime.now(UTC)


def _account(home: Path) -> dict[str, Any]:
    path = home / "state" / "account.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _pin() -> str:
    return os.environ.get("IBKR_PAPER_ACCOUNT", "").strip()


def _ended_risk_date(now: datetime) -> date | None:
    """The risk date that ended at 08:00. None before that boundary."""
    local = now.astimezone(MACAU)
    boundary = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if local < boundary:
        return None
    return (boundary - timedelta(days=1)).date()


def _summary_due(home: Path, now: datetime) -> bool:
    local = now.astimezone(MACAU)
    six = local.replace(hour=6, minute=0, second=0, microsecond=0)
    if local < six:
        return False
    start, _end, _day = risk_window(now)
    name = f"summary_{start.strftime('%Y%m%d')}.json"
    return not (home / "reports" / "daily" / name).is_file()


def _rebuild_marker(home: Path) -> Path:
    return home / "state" / "pool_rebuild_invoked.json"


def _rebuild_due(home: Path, now: datetime) -> bool:
    from auto_trading.universe.rebuild import retry_pending, should_run

    local = now.astimezone(HKT)
    if local.hour != 6:
        return False
    if not should_run(local.date(), retry_pending=retry_pending(home)):
        return False
    marker = _rebuild_marker(home)
    if not marker.is_file():
        return True
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return payload.get("date") != local.date().isoformat()


def _mark_rebuild(home: Path, today: date) -> None:
    path = _rebuild_marker(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": today.isoformat()}) + "\n", encoding="utf-8")


def publish_limits(home: Path, now: datetime) -> None:
    """Write risk_limits/1.1. Headroom follows the cycle when one is latched."""
    account = _account(home)
    nlv = float(account.get("nlv") or 0)
    if nlv <= 0:
        return
    hwm = account.get("hwm")
    day_start = account.get("day_start_nlv")
    payload = build_risk_limits(
        now=now,
        nlv=nlv,
        account=_pin(),
        hwm=float(hwm) if hwm is not None else None,
        day_start_nlv=float(day_start) if day_start is not None else None,
        kill_switch=(home / "KILL_SWITCH").is_file(),
        trigger="startup",
        trading_home=home,
    )
    write_risk_limits(home, payload)


def oms_once(home: Path, now: datetime) -> dict[str, Any]:
    """One OMS pass: heartbeat, R9, alerts, breakers, summary, pool rebuild."""
    home.mkdir(parents=True, exist_ok=True)
    journal = TradeJournal(home)
    unknown = OrderMachine(journal).unknown_symbols()
    write_heartbeat(home / "state" / "heartbeat.json", now)
    account = _account(home)
    nlv = float(account.get("nlv") or 0)
    pin = _pin()
    rolled = False
    if nlv > 0:
        _start, _end, day = risk_window(now)
        hwm = account.get("hwm")
        observe_nlv(
            home,
            day,
            nlv,
            now,
            account=pin,
            hwm=float(hwm) if hwm is not None else None,
        )
        ended = _ended_risk_date(now)
        if ended is not None:
            catch_up(home, ended, nlv, now, account=pin)
            close_risk_day(
                home,
                ended.isoformat(),
                nlv,
                now,
                is_xhkg=is_session("XHKG", ended),
                account=pin,
            )
            rolled = True
    scan_once(home, now)
    recon_path = home / "state" / "recon.json"
    frozen = False
    if recon_path.is_file():
        try:
            frozen = bool(json.loads(recon_path.read_text(encoding="utf-8")).get("freeze"))
        except (OSError, json.JSONDecodeError):
            frozen = True
    maybe_auto_clear_r29(home, now, unknown=bool(unknown), recon_freeze=frozen)
    refresh_r30(home, now, account=pin)
    summarized = False
    if _summary_due(home, now):
        write_daily_summary(home, now=now, complete=True, pin=pin)
        summarized = True
    rebuilt = False
    if _rebuild_due(home, now):
        from auto_trading.universe.rebuild import run_rebuild

        local = now.astimezone(HKT)
        _mark_rebuild(home, local.date())
        outcome = run_rebuild(home, today=local.date(), now=local)
        rebuilt = outcome.ran
    publish_limits(home, now)
    return {"rolled": rolled, "summarized": summarized, "rebuilt": rebuilt}


def recon_once(
    home: Path,
    now: datetime,
    *,
    broker_qty: dict[str, float] | None = None,
    journal_qty: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Compare broker quantities with the journal and store the freeze flag."""
    from auto_trading.broker import BrokerPosition

    account = _account(home)
    if broker_qty is None:
        stored = account.get("positions") or []
        broker_qty = {
            str(row.get("symbol") or row.get("ticker")): float(row.get("qty") or 0)
            for row in stored
            if isinstance(row, dict)
        }
    if journal_qty is None:
        journal_qty = dict(broker_qty)
    positions = [
        BrokerPosition(symbol=symbol, quantity=qty) for symbol, qty in sorted(broker_qty.items())
    ]
    breaks = reconcile(positions, journal_qty)
    freeze = any(row.kind == "QTY" for row in breaks)
    payload = {
        "schema_version": "recon/1.0",
        "at": now.isoformat(timespec="seconds"),
        "freeze": freeze,
        "breaks": [
            {
                "symbol": row.symbol,
                "broker_qty": row.broker_qty,
                "journal_qty": row.journal_qty,
                "kind": row.kind,
            }
            for row in breaks
        ],
    }
    folder = home / "state"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "recon.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def watchdog_once(home: Path, now: datetime) -> bool:
    """Warn when the OMS heartbeat is missing or older than two minutes."""
    path = home / "state" / "heartbeat.json"
    due = heartbeat_alert_due(path, now)
    if not due:
        return False
    marker = home / "state" / "heartbeat_alert.json"
    stamp = now.astimezone(MACAU).strftime("%Y%m%dT%H")
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == stamp:
        return False
    write_ops_notice(home, message_zh="心跳中斷。", now=now, kind="heartbeat_gap")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(stamp + "\n", encoding="utf-8")
    return True


def sync_argv(root: Path, remote: str, *, direction: str) -> list[str]:
    """rsync arguments. The universe copy never uses ``--delete``."""
    name = "sync_vps_to_box.txt" if direction == "out" else "sync_box_to_vps.txt"
    manifest = root / "vps" / "deploy" / name
    return ["rsync", "-a", f"--files-from={manifest}", f"{root}/", remote]


def sync_once(root: Path, *, remote: str = "") -> int:
    """Push and pull the manifest. Exit 0 when the remote is not configured."""
    target = remote or os.environ.get("TRADING_SYNC_REMOTE", "").strip()
    folder = Path(os.environ.get("TRADING_HOME", root)) / "state"
    folder.mkdir(parents=True, exist_ok=True)
    record = folder / "sync.json"
    if not target:
        record.write_text(json.dumps({"status": "remote_unset"}) + "\n", encoding="utf-8")
        return 0
    code = 0
    for direction in ("out", "in"):
        argv = sync_argv(root, target, direction=direction)
        if "--delete" in argv:
            raise RuntimeError("sync must not delete")
        completed = subprocess.run(argv, check=False)
        code = code or completed.returncode
    record.write_text(json.dumps({"status": "ran", "code": code}) + "\n", encoding="utf-8")
    return code


def main_once(home: Path, now: datetime) -> dict[str, Any]:
    """One pass of every resident service except the remote copy."""
    alerted = watchdog_once(home, now)
    oms = oms_once(home, now)
    recon = recon_once(home, now)
    return {"watchdog": alerted, "oms": oms, "recon": recon}


def run_forever(command: str, home: Path) -> None:
    """Block. systemd restarts the process if it exits."""
    intervals = {
        "main": OMS_INTERVAL_SEC,
        "oms": OMS_INTERVAL_SEC,
        "recon": RECON_INTERVAL_SEC,
        "watchdog": WATCHDOG_INTERVAL_SEC,
    }
    pause = intervals[command]
    while True:
        now = _now()
        if command == "main":
            main_once(home, now)
        elif command == "oms":
            oms_once(home, now)
        elif command == "recon":
            recon_once(home, now)
        elif command == "watchdog":
            watchdog_once(home, now)
        time.sleep(pause)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m auto_trading.execution``."""
    parser = argparse.ArgumentParser(prog="trading-service")
    parser.add_argument("command", choices=("main", "oms", "recon", "watchdog", "sync", "summary"))
    parser.add_argument("--home", default="")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    home = Path(args.home or os.environ.get("TRADING_HOME", Path.cwd()))
    if args.command == "sync":
        root = Path(__file__).resolve().parents[3]
        return sync_once(root)
    if args.command == "summary":
        write_daily_summary(home, now=_now(), complete=True, pin=_pin())
        return 0
    if args.once:
        now = _now()
        if args.command == "main":
            main_once(home, now)
        elif args.command == "oms":
            oms_once(home, now)
        elif args.command == "recon":
            recon_once(home, now)
        else:
            watchdog_once(home, now)
        return 0
    run_forever(args.command, home)
    return 0
