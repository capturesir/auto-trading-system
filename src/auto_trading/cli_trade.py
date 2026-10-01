"""Operator commands: status, positions, pnl, why, pause, stop, approve.

The kill switch is cleared only from a local terminal. ``--reset-hwm`` is
the only way to move the high-water mark.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC
from pathlib import Path

from auto_trading.execution.approvals import ApprovalError, approve
from auto_trading.execution.heartbeat import heartbeat_alert_due, heartbeat_gap
from auto_trading.portfolio.journal import TradeJournal


def _home(value: str | None) -> Path:
    if value:
        return Path(value)
    from auto_trading.config import get_settings

    return get_settings().trading_home


def _account(home: Path) -> dict:
    path = home / "state" / "account.json"
    if not path.is_file():
        return {"nlv": 0.0, "hwm": 0.0, "day_start_nlv": 0.0, "positions": []}
    return json.loads(path.read_text(encoding="utf-8"))


def stop(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "KILL_SWITCH").write_text("manual\n", encoding="utf-8")


def pause(home: Path) -> None:
    folder = home / "state"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "paused").write_text("paused\n", encoding="utf-8")


def resume(home: Path, *, reset_hwm: bool, local_terminal: bool) -> str:
    """Clear the kill switch. Refuses a remote caller and a halt that is still on."""
    if not local_terminal:
        raise PermissionError("kill switch can only be reset locally")
    account = _account(home)
    hwm = float(account.get("hwm") or 0)
    nlv = float(account.get("nlv") or 0)
    drawdown = nlv / hwm - 1 if hwm else 0.0
    if drawdown <= -0.10 and not reset_hwm:
        raise PermissionError("drawdown still at the halt; pass --reset-hwm")
    if reset_hwm:
        account["hwm"] = nlv
        (home / "state").mkdir(parents=True, exist_ok=True)
        (home / "state" / "account.json").write_text(
            json.dumps(account, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        TradeJournal(home).audit(
            "reset_hwm",
            {"old_hwm": hwm, "new_hwm": nlv, "by": "user"},
        )
    switch = home / "KILL_SWITCH"
    if switch.exists():
        switch.unlink()
    return "resumed"


def status_text(home: Path, now) -> str:
    killed = (home / "KILL_SWITCH").is_file()
    paused = (home / "state" / "paused").is_file()
    gap = heartbeat_gap(home / "state" / "heartbeat.json", now)
    alert = heartbeat_alert_due(home / "state" / "heartbeat.json", now)
    account = _account(home)
    return (
        f"kill_switch={killed} paused={paused} heartbeat_alert={alert} "
        f"gap_sec={None if gap is None else int(gap.total_seconds())} nlv={account.get('nlv')}"
    )


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``trade`` and ``auto-trading trade``."""
    parser = argparse.ArgumentParser(prog="trade")
    parser.add_argument("--home", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    approve_cmd = sub.add_parser("approve")
    approve_cmd.add_argument("order_id")
    approve_cmd.add_argument("--hash", required=True)
    sub.add_parser("status")
    sub.add_parser("pos")
    sub.add_parser("pnl")
    why = sub.add_parser("why")
    why.add_argument("key")
    sub.add_parser("pause")
    sub.add_parser("stop")
    resume_cmd = sub.add_parser("resume")
    resume_cmd.add_argument("--reset-hwm", action="store_true")
    resume_cmd.add_argument("--local", action="store_true")
    args = parser.parse_args(argv)
    home = _home(args.home)
    if args.command == "approve":
        try:
            document = approve(home, args.order_id, args.hash)
        except ApprovalError as exc:
            raise SystemExit(str(exc)) from exc
        print(document["status"])
        return
    if args.command == "stop":
        stop(home)
        print("stopped")
        return
    if args.command == "pause":
        pause(home)
        print("paused")
        return
    if args.command == "resume":
        try:
            print(resume(home, reset_hwm=args.reset_hwm, local_terminal=args.local))
        except PermissionError as exc:
            raise SystemExit(str(exc)) from exc
        return
    if args.command == "pos":
        print(json.dumps(_account(home).get("positions", []), ensure_ascii=False))
        return
    if args.command == "pnl":
        account = _account(home)
        print(float(account.get("nlv") or 0) - float(account.get("day_start_nlv") or 0))
        return
    if args.command == "why":
        print(json.dumps(TradeJournal(home).why(args.key), ensure_ascii=False, default=str))
        return
    from datetime import datetime

    print(status_text(home, datetime.now(UTC)))
