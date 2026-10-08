"""Operator commands: status, positions, pnl, why, pause, stop, approve.

The kill switch is cleared only from a local terminal. ``--reset-hwm`` is
the only way to move the high-water mark.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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


CONFIRM_PHRASE = "CONFIRM RESUME"
_WORKING = "running"
_HALTED = "halted"


def _runtime_path(home: Path) -> Path:
    return home / "state" / "strategy_runtime.json"


def _write_runtime(home: Path, status: str) -> None:
    folder = home / "state"
    folder.mkdir(parents=True, exist_ok=True)
    _runtime_path(home).write_text(
        json.dumps({"status": status}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _emit_critical(home: Path, payload: dict) -> None:
    """Write the critical event after the stop. A write error leaves the stop in place."""
    try:
        from datetime import datetime

        from auto_trading.alerts.critical import write_critical_event

        write_critical_event(home, now=datetime.now(UTC), **payload)
    except Exception:
        return


def stop(home: Path) -> None:
    """Full stop: latch the file, cancel working orders, halt the runtime."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "KILL_SWITCH").write_text("manual\n", encoding="utf-8")
    _write_runtime(home, _HALTED)
    journal = TradeJournal(home)
    cancelled = journal.cancel_unfilled_orders()
    journal.audit("stop", {"strategy_runtime": _HALTED, "cancelled": cancelled})
    from auto_trading.alerts.critical import HINT_FULL_STOP

    _emit_critical(
        home,
        {
            "kind": "r18_stop",
            "reason_code": "R18",
            "rule_id": "R18",
            "message_zh": "已手動全停。",
            "resume_hint": HINT_FULL_STOP,
            "actions": {
                "kill_switch_file": True,
                "open_orders_cancelled": True,
                "protective_stops_kept": True,
                "disconnected": False,
                "strategies_halted": True,
            },
        },
    )


def halt_r9(home: Path) -> None:
    """Drawdown halt. The latch is the R9 cycle file, not the kill switch."""
    home.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    from auto_trading.risk.r9_state import force_halt

    force_halt(home, datetime.now(UTC))


def pause(home: Path) -> None:
    folder = home / "state"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "paused").write_text("paused\n", encoding="utf-8")


def interactive_local_session() -> None:
    """Raise unless this process is an interactive terminal on the host itself."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise PermissionError("kill switch requires an interactive terminal on the host")
    remote = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
    if any(os.environ.get(name) for name in remote):
        raise PermissionError("kill switch requires an interactive terminal on the host")


def _synced_resume(home: Path) -> None:
    if (home / "state" / "resume.request").is_file():
        raise PermissionError("synced resume is refused")


def resume(home: Path, *, reset_hwm: bool, local_terminal: bool, confirm: str) -> str:
    """Clear the kill switch. Refuses a remote caller or a missing phrase."""
    _synced_resume(home)
    if not local_terminal:
        raise PermissionError("kill switch can only be reset locally")
    if confirm != CONFIRM_PHRASE:
        raise PermissionError("resume requires the confirmation phrase")
    interactive_local_session()
    switch = home / "KILL_SWITCH"
    if switch.is_file() and switch.read_text(encoding="utf-8").startswith("R0_"):
        raise PermissionError("R0 violation is still latched")
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
        journal = TradeJournal(home)
        journal.audit(
            "reset_hwm",
            {"old_hwm": hwm, "new_hwm": nlv, "by": "user"},
        )
        journal.record_hwm(hwm=nlv, nlv=nlv, reason="reset_hwm", detail={"old_hwm": hwm})
    switch = home / "KILL_SWITCH"
    if switch.exists():
        switch.unlink()
    _write_runtime(home, _WORKING)
    try:
        from datetime import datetime

        from auto_trading.alerts.critical import write_ops_notice

        write_ops_notice(home, message_zh="全停已在本機解除。", now=datetime.now(UTC))
    except Exception:
        pass
    return "resumed"


def resume_r9_early(home: Path, *, local_terminal: bool, confirm: str) -> str:
    """Cooldown early resume from a local terminal. The kill switch is left alone."""
    if not local_terminal:
        raise PermissionError("kill switch can only be reset locally")
    if confirm != CONFIRM_PHRASE:
        raise PermissionError("resume requires the confirmation phrase")
    interactive_local_session()
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from auto_trading.risk.r9_state import resume_early

    account = _account(home)
    nlv = float(account.get("nlv") or 0)
    now = datetime.now(UTC)
    local = now.astimezone(ZoneInfo("Asia/Macau"))
    if local.hour < 8:
        local = local - timedelta(days=1)
    document = resume_early(home, local.date().isoformat(), nlv, now)
    if document.get("state") != "HALF_SIZE":
        raise PermissionError("R9 early resume applies only during cooldown")
    return "half_size"


def resume_r29(home: Path, *, local_terminal: bool, confirm: str) -> str:
    """Clear R29 only. This does not clear a full stop."""
    _synced_resume(home)
    if not local_terminal:
        raise PermissionError("kill switch can only be reset locally")
    if confirm != CONFIRM_PHRASE:
        raise PermissionError("resume requires the confirmation phrase")
    interactive_local_session()
    if TradeJournal(home).unknown_orders():
        raise PermissionError("unresolved UNKNOWN order")
    from datetime import datetime

    from auto_trading.execution.breakers import clear_r29

    clear_r29(home, datetime.now(UTC))
    return "r29 cleared"


def describe_approval(home: Path) -> tuple[str, str]:
    """Current approval mode from a fresh account file, not a startup cache."""
    path = home / "state" / "approval_session.json"
    if not path.is_file():
        return "manual", "accounts_unavailable"
    from auto_trading.config import get_settings, manual_forced
    from auto_trading.execution.approval_mode import (
        ApprovalSession,
        approval_block_reason,
        decide_approval_mode,
    )

    def accounts() -> tuple[str, ...]:
        fresh = json.loads(path.read_text(encoding="utf-8"))
        return tuple(fresh.get("accounts") or [])

    payload = json.loads(path.read_text(encoding="utf-8"))
    settings = get_settings()
    pin = settings.ibkr_paper_account or str(payload.get("pin") or "")
    session = ApprovalSession(
        read_accounts=accounts,
        port=int(payload.get("port") or settings.ibkr_port),
        pin=pin,
        baseline=tuple(payload.get("baseline") or []),
        kill_switch_present=lambda: (home / "KILL_SWITCH").is_file(),
        r0_violation=lambda: bool(payload.get("r0_violation")),
        force_manual=lambda: manual_forced(os.environ.get("FORCE_MANUAL_APPROVAL", "false")),
        strategy_approval=lambda: str(payload.get("strategy_approval") or "manual"),
    )

    class _Order:
        account = pin
        execution_approval = None

    return decide_approval_mode(_Order(), session), approval_block_reason(_Order(), session)


def status_text(home: Path, now) -> str:
    killed = (home / "KILL_SWITCH").is_file()
    paused = (home / "state" / "paused").is_file()
    gap = heartbeat_gap(home / "state" / "heartbeat.json", now)
    alert = heartbeat_alert_due(home / "state" / "heartbeat.json", now)
    account = _account(home)
    mode, reason = describe_approval(home)
    from auto_trading.alerts.forwarder import status_warning, unforwarded_critical

    count, _late = unforwarded_critical(home, now)
    warn = status_warning(home, now)
    from auto_trading.risk.r9_state import status_bits

    return (
        f"kill_switch={killed} paused={paused} heartbeat_alert={alert} "
        f"gap_sec={None if gap is None else int(gap.total_seconds())} nlv={account.get('nlv')} "
        f"approval={mode} approval_reason={reason} unforwarded_critical={count} "
        f"critical_warn={'yes' if warn else 'no'} {status_bits(home)}"
    )


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``trade`` and ``auto-trading trade``."""
    from auto_trading.config import assert_startup_switches

    assert_startup_switches()
    parser = argparse.ArgumentParser(prog="trade")
    parser.add_argument("--home", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    approve_cmd = sub.add_parser("approve")
    approve_cmd.add_argument("order_id")
    approve_cmd.add_argument("--hash", required=True)
    approve_cmd.add_argument("--by", required=True)
    approve_cmd.add_argument("--channel", required=True)
    sub.add_parser("pending")
    decline_cmd = sub.add_parser("decline")
    decline_cmd.add_argument("order_id")
    decline_cmd.add_argument("--by", required=True)
    decline_cmd.add_argument("--channel", required=True)
    sub.add_parser("status")
    sub.add_parser("digest")
    sub.add_parser("pos")
    sub.add_parser("pnl")
    why = sub.add_parser("why")
    why.add_argument("key")
    recon = sub.add_parser("recon")
    recon_sub = recon.add_subparsers(dest="recon_command", required=True)
    recon_sub.add_parser("ack")
    sub.add_parser("strategies")
    sub.add_parser("pause")
    sub.add_parser("stop")
    resume_cmd = sub.add_parser("resume")
    resume_cmd.add_argument("--reset-hwm", action="store_true")
    resume_cmd.add_argument("--local", action="store_true")
    resume_cmd.add_argument("--confirm", default="")
    resume_cmd.add_argument("--breaker", default="")
    resume_cmd.add_argument("--r9-early", action="store_true")
    args = parser.parse_args(argv)
    home = _home(args.home)
    if args.command == "approve":
        try:
            document = approve(
                home,
                args.order_id,
                args.hash,
                by=args.by,
                channel=args.channel,
            )
        except ApprovalError as exc:
            raise SystemExit(str(exc)) from exc
        print(document["status"])
        return
    if args.command == "pending":
        from auto_trading.execution.approvals import list_pending

        print(json.dumps(list_pending(home), ensure_ascii=False))
        return
    if args.command == "decline":
        from auto_trading.execution.approvals import decline

        try:
            document = decline(home, args.order_id, by=args.by, channel=args.channel)
        except ApprovalError as exc:
            raise SystemExit(str(exc)) from exc
        print(document["status"])
        return
    if args.command == "digest":
        from datetime import datetime

        from auto_trading.config import get_settings
        from auto_trading.reports.daily_summary import write_daily_summary

        payload = write_daily_summary(
            home,
            now=datetime.now(UTC),
            complete=True,
            pin=get_settings().ibkr_paper_account,
        )
        print(payload["risk_date"])
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
            if args.r9_early:
                print(
                    resume_r9_early(
                        home,
                        local_terminal=args.local,
                        confirm=args.confirm,
                    )
                )
            elif args.breaker == "R30":
                raise PermissionError("R30 clears only when notifications recover")
            elif args.breaker == "R29":
                print(
                    resume_r29(
                        home,
                        local_terminal=args.local,
                        confirm=args.confirm,
                    )
                )
            elif args.breaker:
                raise PermissionError("unknown breaker")
            else:
                print(
                    resume(
                        home,
                        reset_hwm=args.reset_hwm,
                        local_terminal=args.local,
                        confirm=args.confirm,
                    )
                )
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
    if args.command == "strategies":
        from auto_trading.execution.strategy_book import startup_lines

        for line in startup_lines():
            print(line)
        return
    if args.command == "recon":
        from auto_trading.execution.strategy_positions import ack_manual

        broker_path = home / "state" / "broker_positions.json"
        payload = json.loads(broker_path.read_text(encoding="utf-8"))
        broker_qty = {row["symbol"]: float(row["qty"]) for row in payload}
        con_ids = {row["symbol"]: int(row.get("con_id") or 0) for row in payload}
        account = str(payload[0].get("account") or "") if payload else ""
        ack_manual(
            TradeJournal(home),
            account=account,
            broker_qty=broker_qty,
            con_ids=con_ids,
        )
        print("acked")
        return
    from datetime import datetime

    print(status_text(home, datetime.now(UTC)))
