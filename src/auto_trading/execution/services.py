"""Long-running paper services.

Production systemd starts the split units (account, oms, recon, watchdog,
submit, sync). ``main`` is a local one-shot helper and is not installed.
Nothing here opens a real-account session. Orders are not sent while
ReadOnlyApi is yes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from auto_trading.alerts.critical import write_ops_notice
from auto_trading.alerts.forwarder import scan_once
from auto_trading.broker import OrderIntent
from auto_trading.execution.approval_mode import (
    ApprovalSession,
    decide_approval_mode,
    is_auto_approver,
)
from auto_trading.execution.breakers import (
    maybe_auto_clear_r29,
    refresh_r30,
    risk_window,
)
from auto_trading.execution.heartbeat import heartbeat_alert_due, write_heartbeat
from auto_trading.execution.oms import OrderMachine
from auto_trading.execution.risk_state import build_risk_limits, write_risk_limits
from auto_trading.execution.sync import ReconBreak, reconcile
from auto_trading.paper_only import account_accepted
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.reports.daily_summary import write_daily_summary
from auto_trading.risk.gate import GateContext
from auto_trading.risk.r9_state import catch_up, observe_nlv

MACAU = ZoneInfo("Asia/Macau")
OMS_INTERVAL_SEC = 10
RECON_INTERVAL_SEC = 60
WATCHDOG_INTERVAL_SEC = 30
ACCOUNT_INTERVAL_SEC = 30
SUBMIT_INTERVAL_SEC = 10
SYNC_INTERVAL_SEC = 30
DISK_USAGE_MAX = 0.85
CLOCK_WARN_SEC = 1.0
CLOCK_HALT_SEC = 5.0
GATEWAY_RESTART_AFTER = 3
_GATEWAY_STALE_SEC = 90.0
_CHRONY_OFFSET = re.compile(r"Last offset\s*:\s*([+-]?\d+(?:\.\d+)?)\s+seconds", re.IGNORECASE)
_CHRONY_SYSTEM = re.compile(
    r"System time\s*:\s*(\d+(?:\.\d+)?)\s+seconds\s+(fast|slow)",
    re.IGNORECASE,
)


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


def _port() -> int:
    raw = os.environ.get("IB_PORT", os.environ.get("IBKR_PORT", "4002")).strip()
    try:
        return int(raw)
    except ValueError:
        return 0


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _step(home: Path, name: str, fn: Callable[[], Any], errors: list[str]) -> Any:
    """Run one loop step. A failure is recorded and does not stop the others."""
    try:
        return fn()
    except Exception:
        errors.append(name)
        path = home / "state" / "service_errors.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"step": name}) + "\n")
        return None


def _ended_risk_date(now: datetime) -> date | None:
    """The risk date that ended at 08:00. None before that boundary."""
    local = now.astimezone(MACAU)
    boundary = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if local < boundary:
        return None
    return (boundary - timedelta(days=1)).date()


def read_only_api() -> bool:
    """True while the Gateway template keeps ReadOnlyApi at yes.

    The default is yes. E2 is the only time this may be set to no, and the
    unit files in this repo do not do that.
    """
    flag = os.environ.get("IB_READ_ONLY_API", "yes").strip().lower()
    return flag not in {"no", "false", "0"}


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


def _roll_r9(home: Path, now: datetime, account: dict[str, Any]) -> bool:
    """Observe NLV and count each ended risk day at most once."""
    nlv = float(account.get("nlv") or 0)
    if nlv <= 0:
        return False
    _start, _end, day = risk_window(now)
    hwm = account.get("hwm")
    pin = _pin()
    observe_nlv(
        home,
        day,
        nlv,
        now,
        account=pin,
        hwm=float(hwm) if hwm is not None else None,
    )
    ended = _ended_risk_date(now)
    if ended is None:
        return False
    catch_up(home, ended, nlv, now, account=pin)
    return True


def oms_once(home: Path, now: datetime) -> dict[str, Any]:
    """One OMS pass. Summary and pool rebuild stay on their timers."""
    home.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    state = {"rolled": False}
    _step(home, "approved_hash", lambda: audit_confirmed_hashes(home, now), errors)

    def _heartbeat() -> None:
        journal = TradeJournal(home)
        OrderMachine(journal).unknown_symbols()
        write_heartbeat(home / "state" / "heartbeat.json", now)

    def _r9() -> None:
        state["rolled"] = _roll_r9(home, now, _account(home))

    def _alerts() -> None:
        scan_once(home, now)

    def _breakers() -> None:
        journal = TradeJournal(home)
        unknown = OrderMachine(journal).unknown_symbols()
        recon_path = home / "state" / "recon.json"
        frozen = False
        if recon_path.is_file():
            frozen = bool(json.loads(recon_path.read_text(encoding="utf-8")).get("freeze"))
        maybe_auto_clear_r29(home, now, unknown=bool(unknown), recon_freeze=frozen)
        refresh_r30(home, now, account=_pin())

    _step(home, "heartbeat", _heartbeat, errors)
    _step(home, "r9", _r9, errors)
    _step(home, "alerts", _alerts, errors)
    _step(home, "breakers", _breakers, errors)
    _step(home, "publish", lambda: publish_limits(home, now), errors)
    return {
        "rolled": state["rolled"],
        "summarized": False,
        "rebuilt": False,
        "errors": errors,
    }


def _hk_key(symbol: str) -> str | None:
    """``700`` and ``0700.HK`` share one key. Other symbols stay as they are."""
    text = symbol.strip()
    if text.upper().endswith(".HK"):
        head = text[:-3]
        if head.isdigit():
            return f"{int(head):04d}.HK"
        return None
    if text.isdigit() and 1 <= len(text) <= 5:
        return f"{int(text):04d}.HK"
    return None


def _align_books(
    broker: dict[str, float],
    journal: dict[str, float],
) -> tuple[dict[str, float], dict[str, float]]:
    """Fold HK names that appear on either side as ``NNNN.HK``."""
    targets: set[str] = set()
    for symbol in list(broker) + list(journal):
        if str(symbol).upper().endswith(".HK"):
            key = _hk_key(str(symbol))
            if key:
                targets.add(key)

    def key_for(symbol: str) -> str:
        folded = _hk_key(symbol)
        if folded and (symbol.upper().endswith(".HK") or folded in targets):
            return folded
        return symbol

    def fold(book: dict[str, float]) -> dict[str, float]:
        totals: dict[str, float] = {}
        for symbol, qty in book.items():
            key = key_for(str(symbol))
            totals[key] = totals.get(key, 0.0) + float(qty)
        return totals

    return fold(broker), fold(journal)


def _position_qty(rows: Any) -> dict[str, float]:
    totals: dict[str, float] = {}
    if not isinstance(rows, list):
        return totals
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("ticker") or "")
        if not symbol:
            continue
        currency = str(row.get("currency") or "")
        exchange = str(row.get("exchange") or "")
        folded = _hk_key(symbol)
        if folded and (
            symbol.upper().endswith(".HK")
            or currency.upper() == "HKD"
            or exchange.upper() in {"SEHK", "HK"}
        ):
            symbol = folded
        qty = row.get("qty", row.get("quantity", 0))
        totals[symbol] = totals.get(symbol, 0.0) + float(qty or 0)
    return {symbol: qty for symbol, qty in totals.items() if abs(qty) > 1e-9}


def recon_once(
    home: Path,
    now: datetime,
    *,
    broker_qty: dict[str, float] | None = None,
    journal_qty: dict[str, float] | None = None,
    broker_orders: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare the broker snapshot with the journal and working orders.

    The broker side is the Gateway snapshot (or an explicit argument). The
    journal side is always the local book, never a copy of the broker side.
    """
    from auto_trading.broker import BrokerPosition

    account = _account(home)
    if broker_qty is None:
        broker_qty = _position_qty(account.get("positions") or [])
    if broker_orders is None and "open_orders" in account:
        stored = account.get("open_orders")
        broker_orders = stored if isinstance(stored, list) else []
    journal = TradeJournal(home)
    if journal_qty is None:
        journal_qty = journal.quantity_by_ticker()
    broker_qty, journal_qty = _align_books(broker_qty, journal_qty)
    positions = [
        BrokerPosition(symbol=symbol, quantity=qty) for symbol, qty in sorted(broker_qty.items())
    ]
    breaks = reconcile(positions, journal_qty, unknown_orders=journal.unknown_orders())
    if broker_orders is not None:
        breaks.extend(_order_breaks(journal.working_orders(), broker_orders))
    freeze = any(row.kind in {"QTY", "ORDER"} for row in breaks)
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
                "order_id": row.order_id,
            }
            for row in breaks
        ],
    }
    _write_json(home / "state" / "recon.json", payload)
    return payload


def _order_breaks(
    local_orders: list[tuple[str, str]],
    broker_orders: list[dict[str, Any]],
) -> list[ReconBreak]:
    local = {order_id: symbol for order_id, symbol in local_orders}
    remote: dict[str, str] = {}
    for row in broker_orders:
        if not isinstance(row, dict):
            continue
        order_id = str(row.get("order_id") or "")
        if order_id:
            remote[order_id] = str(row.get("symbol") or "")
    found: list[ReconBreak] = []
    for order_id, symbol in sorted(local.items()):
        if order_id not in remote:
            found.append(ReconBreak(symbol, 0.0, 0.0, "ORDER", order_id))
    for order_id, symbol in sorted(remote.items()):
        if order_id not in local:
            found.append(ReconBreak(symbol, 0.0, 0.0, "ORDER", order_id))
    return found


def pull_account_snapshot() -> dict[str, Any]:
    """Connect to the paper Gateway and read NLV, positions, and orders."""
    import asyncio

    from auto_trading.config import get_settings
    from auto_trading.execution.ibkr import PaperGateway

    settings = get_settings()
    client_id = int(os.environ.get("IB_CLIENT_ID", "13"))
    gateway = PaperGateway(settings, client_id=client_id)
    return asyncio.run(gateway.snapshot_account())


def probe_gateway_login() -> dict[str, Any]:
    """Check Gateway login and the managed-account set. No secrets are logged."""
    import asyncio

    from auto_trading.config import get_settings
    from auto_trading.execution.ibkr import PaperGateway
    from auto_trading.execution.r0 import PaperAccountError

    settings = get_settings()
    client_id = int(os.environ.get("IB_CLIENT_ID", "14"))
    gateway = PaperGateway(settings, client_id=client_id)
    try:
        accounts = asyncio.run(asyncio.wait_for(gateway.connect(), timeout=5))
    except PaperAccountError:
        return {"logged_in": True, "accounts": [], "detail": "r0"}
    except Exception:
        return {"logged_in": False, "accounts": [], "detail": "unreachable"}
    return {"logged_in": True, "accounts": list(accounts), "detail": "connected"}


def gateway_probe(home: Path, now: datetime) -> dict[str, Any]:
    """Login status. The unit sets TRADING_GATEWAY_CONNECT=1 to open the socket."""
    if os.environ.get("TRADING_GATEWAY_CONNECT", "").strip() == "1":
        return probe_gateway_login()
    path = home / "state" / "gateway.json"
    if not path.is_file():
        return {"logged_in": False, "accounts": [], "detail": "no_probe"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        stamped = datetime.fromisoformat(str(payload.get("at")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"logged_in": False, "accounts": [], "detail": "unreadable"}
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    age = abs((now.astimezone(UTC) - stamped.astimezone(UTC)).total_seconds())
    if age > _GATEWAY_STALE_SEC:
        return {"logged_in": False, "accounts": [], "detail": "stale"}
    accounts = payload.get("accounts") or []
    return {
        "logged_in": bool(payload.get("logged_in")),
        "accounts": [str(item) for item in accounts],
        "detail": "file",
    }


def account_once(
    home: Path,
    now: datetime,
    *,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """R0-check the Gateway and write NLV plus positions to ``state/account.json``."""
    from auto_trading.execution.r0 import AccountGuard, PaperAccountError, latch_kill_switch

    data = snapshot if snapshot is not None else pull_account_snapshot()
    accounts = [str(item) for item in data.get("accounts") or []]

    def _trip(code: str) -> None:
        latch_kill_switch(home, code)

    try:
        AccountGuard(_port(), _pin(), on_violation=_trip).observe(accounts)
    except PaperAccountError as exc:
        _write_json(
            home / "state" / "gateway.json",
            {
                "logged_in": True,
                "accounts": accounts,
                "at": now.isoformat(timespec="seconds"),
                "detail": exc.code,
            },
        )
        return {"status": "r0", "wrote": False, "code": exc.code}
    if data.get("nlv") is None:
        return {"status": "nlv_unavailable", "wrote": False}
    previous = _account(home)
    nlv = float(data["nlv"])
    hwm = max(float(previous.get("hwm") or 0), nlv)
    _start, _end, day = risk_window(now)
    if previous.get("risk_date") != day or previous.get("day_start_nlv") is None:
        day_start = nlv
    else:
        day_start = float(previous.get("day_start_nlv") or nlv)
    payload = {
        "schema_version": "account/1.0",
        "at": now.isoformat(timespec="seconds"),
        "account": _pin(),
        "risk_date": day,
        "nlv": nlv,
        "hwm": hwm,
        "day_start_nlv": day_start,
        "positions": list(data.get("positions") or []),
        "open_orders": list(data.get("open_orders") or []),
        "gateway_logged_in": True,
    }
    _write_json(home / "state" / "account.json", payload)
    _write_json(
        home / "state" / "gateway.json",
        {
            "logged_in": True,
            "accounts": accounts,
            "at": now.isoformat(timespec="seconds"),
            "detail": "snapshot",
        },
    )
    return {"status": "wrote", "wrote": True, "nlv": nlv}


def sqlite_ok(home: Path) -> bool:
    """True when the journal is absent or ``PRAGMA quick_check`` is ok."""
    path = home / "data" / "trading.db"
    if not path.is_file():
        return True
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and str(row[0]) == "ok"


def disk_usage_ratio(home: Path) -> float:
    """Fraction of the filesystem in use. A full disk is 1."""
    target = home if home.exists() else Path("/")
    usage = shutil.disk_usage(target)
    if usage.total <= 0:
        return 1.0
    return usage.used / usage.total


def parse_chrony_tracking(text: str) -> float | None:
    """Seconds of chrony offset. Positive means the clock is ahead."""
    match = _CHRONY_OFFSET.search(text)
    if match:
        return abs(float(match.group(1)))
    system = _CHRONY_SYSTEM.search(text)
    if system:
        return abs(float(system.group(1)))
    return None


def chrony_offset_sec() -> float | None:
    """Chrony offset. None when chrony cannot be read. Not a self-comparison."""
    try:
        completed = subprocess.run(
            ["chronyc", "tracking"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_chrony_tracking(completed.stdout or "")


def gateway_restart_argv() -> list[str]:
    return ["systemctl", "restart", "ibgateway.service"]


def restart_gateway() -> bool:
    """Restart the paper Gateway. No-op unless the unit is allowed to connect."""
    if os.environ.get("TRADING_GATEWAY_CONNECT", "").strip() != "1":
        return False
    subprocess.run(gateway_restart_argv(), check=False)
    return True


def _set_pause(home: Path, key: str, active: bool) -> None:
    path = home / "state" / "entry_pause.json"
    payload: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            payload = loaded
    payload[key] = active
    _write_json(path, payload)


def _host_report(home: Path, now: datetime) -> dict[str, Any]:
    gateway = gateway_probe(home, now)
    accounts = tuple(gateway.get("accounts") or [])
    pin = _pin()
    logged_in = bool(gateway.get("logged_in"))
    r0_ok = logged_in and bool(pin) and set(accounts) == {pin}
    r0_ok = r0_ok and all(account_accepted(item, pin) for item in accounts)
    issues: list[str] = []
    if not logged_in:
        issues.append("gateway_login")
    if not r0_ok:
        issues.append("r0_account")
    if not sqlite_ok(home):
        issues.append("sqlite")
    usage = disk_usage_ratio(home)
    if usage > DISK_USAGE_MAX:
        issues.append("disk")
    skew = chrony_offset_sec()
    clock_warn = False
    if skew is None or skew > CLOCK_HALT_SEC:
        issues.append("clock_skew")
    elif skew > CLOCK_WARN_SEC:
        clock_warn = True
    if logged_in and accounts and not r0_ok:
        from auto_trading.execution.r0 import latch_kill_switch

        latch_kill_switch(home, "R0_ACCOUNTS_CHANGED")
    _set_pause(home, "watchdog", bool(issues))
    return {
        "at": now.isoformat(timespec="seconds"),
        "issues": issues,
        "clock_warn": clock_warn,
        "gateway_login": logged_in,
        "r0_account": r0_ok,
        "sqlite": sqlite_ok(home),
        "disk_usage": round(usage, 4),
        "clock_skew_sec": None if skew is None else round(skew, 6),
        "detail": str(gateway.get("detail") or ""),
    }


def _watchdog_streak(home: Path, failed: bool) -> int:
    path = home / "state" / "watchdog_streak.json"
    count = 0
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            count = int(loaded.get("count") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            count = 0
    count = count + 1 if failed else 0
    _write_json(path, {"count": count})
    return count


def watchdog_once(home: Path, now: datetime) -> bool:
    """Heartbeat plus Gateway login, R0, SQLite, disk, and chrony offset."""
    path = home / "state" / "heartbeat.json"
    heartbeat_due = heartbeat_alert_due(path, now)
    report = _host_report(home, now)
    streak = _watchdog_streak(home, bool(report["issues"]))
    report["fail_streak"] = streak
    report["gateway_restart"] = False
    report["gateway_restarted"] = False
    if streak >= GATEWAY_RESTART_AFTER and streak % GATEWAY_RESTART_AFTER == 0:
        report["gateway_restart"] = True
        report["gateway_restarted"] = restart_gateway()
    _write_json(home / "state" / "watchdog.json", report)
    alerted = False
    if heartbeat_due:
        marker = home / "state" / "heartbeat_alert.json"
        stamp = now.astimezone(MACAU).strftime("%Y%m%dT%H")
        if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != stamp:
            write_ops_notice(home, message_zh="心跳中斷。", now=now, kind="heartbeat_gap")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(stamp + "\n", encoding="utf-8")
            alerted = True
    if report["issues"]:
        marker = home / "state" / "watchdog_alert.json"
        stamp = now.astimezone(MACAU).strftime("%Y%m%dT%H")
        previous = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
        if previous != stamp:
            write_ops_notice(home, message_zh="主機檢查未通過。", now=now, kind="watchdog")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(stamp + "\n", encoding="utf-8")
            alerted = True
    if report.get("clock_warn") and "clock_skew" not in report["issues"]:
        marker = home / "state" / "clock_warn.json"
        stamp = now.astimezone(MACAU).strftime("%Y%m%dT%H")
        previous = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
        if previous != stamp:
            write_ops_notice(home, message_zh="時鐘偏差超過 1 秒。", now=now, kind="clock_warn")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(stamp + "\n", encoding="utf-8")
            alerted = True
    return alerted or heartbeat_due or bool(report["issues"])


def _manifest_lines(path: Path) -> list[str]:
    rows: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            rows.append(line)
    return rows


def sync_argv(root: Path, remote: str, *, direction: str) -> list[str]:
    """rsync arguments. Approved strategies move box to VPS only.

    The box-to-VPS pass uses ``--checksum`` and not ``--update``, so the box
    copy replaces a newer tampered file. The VPS-to-box pass keeps ``--update``
    and lists only VPS-owned paths. Neither pass uses ``--delete``.
    """
    if direction == "out":
        name = "sync_vps_to_box.txt"
    elif direction == "in":
        name = "sync_box_to_vps.txt"
    else:
        raise ValueError("direction must be out or in")
    manifest = root / "vps" / "deploy" / name
    rows = _manifest_lines(manifest)
    if direction == "out" and any(
        row.startswith("strategies/approved") or row == "strategies/registry.jsonl" for row in rows
    ):
        raise RuntimeError("outbound sync must not carry approved strategies")
    if direction == "out" and any(
        row == "state/" or row.endswith("adr_premium_state.json") for row in rows
    ):
        raise RuntimeError("outbound sync must not carry box-owned state")
    target = remote if remote.endswith("/") else remote + "/"
    local = f"{root}/"
    if direction == "out":
        source, dest = local, target
        flags = ["--update"]
    else:
        source, dest = target, local
        flags = ["--checksum"]
    return ["rsync", "-a", *flags, f"--files-from={manifest}", source, dest]


def sync_once(root: Path, *, remote: str = "") -> int:
    """Pull box files, then push VPS files. Exit 0 when the remote is unset."""
    target = remote or os.environ.get("TRADING_SYNC_REMOTE", "").strip()
    folder = Path(os.environ.get("TRADING_HOME", root)) / "state"
    folder.mkdir(parents=True, exist_ok=True)
    record = folder / "sync.json"
    if not target:
        record.write_text(json.dumps({"status": "remote_unset"}) + "\n", encoding="utf-8")
        return 0
    code = 0
    for direction in ("in", "out"):
        argv = sync_argv(root, target, direction=direction)
        if "--delete" in argv:
            raise RuntimeError("sync must not delete")
        completed = subprocess.run(argv, check=False)
        code = code or completed.returncode
    audit_confirmed_hashes(Path(os.environ.get("TRADING_HOME", root)), _now())
    record.write_text(json.dumps({"status": "ran", "code": code}) + "\n", encoding="utf-8")
    return code


def _strategies_root(home: Path) -> Path:
    local = home / "strategies"
    if (local / "approved").is_dir():
        return local
    return Path(__file__).resolve().parents[3] / "strategies"


def audit_confirmed_hashes(home: Path, now: datetime) -> list[str]:
    """Compare the three confirmed strategies with the files on disk.

    A mismatch pauses new entries and writes one critical alert.
    """
    from auto_trading.alerts.critical import write_critical_event
    from auto_trading.execution.strategy_book import confirmed_mismatches

    bad = confirmed_mismatches(_strategies_root(home))
    _set_pause(home, "approved_hash", bool(bad))
    if not bad:
        return bad
    marker = home / "state" / "approved_hash_alert.txt"
    stamp = ",".join(bad)
    previous = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    if previous == stamp:
        return bad
    write_critical_event(
        home,
        kind="approved_hash_mismatch",
        reason_code="R25",
        rule_id="R25",
        now=now,
        account=_pin(),
        message_zh="核准策略雜湊與確認值不符。新開倉已暫停。",
        resume_hint="在 box 還原核准檔後再同步。",
        detail={"strategies": bad},
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(stamp + "\n", encoding="utf-8")
    return bad


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _whole(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row_intent(row: dict[str, Any], envelope: dict[str, Any]) -> OrderIntent | None:
    """Map one strategy-engine row, or a flat row, onto an order."""
    symbol = str(row.get("ticker") or row.get("symbol") or "").strip()
    if not symbol:
        return None
    qty = _number(row.get("qty", row.get("quantity")))
    if qty is None or qty <= 0:
        return None
    intent_id = str(row.get("intent_id") or row.get("client_id") or "").strip()
    if not intent_id:
        return None
    raw_mult = row.get("live_size_multiplier", envelope.get("live_size_multiplier"))
    version = row.get("strategy_version", envelope.get("strategy_version"))
    market = row.get("market") or envelope.get("market")
    currency = row.get("currency") or envelope.get("currency")
    if market is None or currency is None:
        try:
            from auto_trading.market.instruments import describe

            spec = describe(symbol)
            market = market or spec.market
            currency = currency or spec.currency
        except Exception:
            market = market or None
            currency = currency or None
    limit = row.get("limit_price")
    digest = row.get("spec_hash", envelope.get("spec_hash"))
    strategy_id = row.get("strategy_id", envelope.get("strategy_id"))
    return OrderIntent(
        client_id=str(row.get("client_id") or intent_id),
        symbol=symbol,
        side=str(row.get("side") or "BUY"),
        quantity=qty,
        limit_price=None if limit is None else _number(limit),
        intent_id=intent_id,
        intent_type=str(row.get("intent_type") or "ENTRY"),
        account=str(row.get("account") or _pin()),
        mode=str(row.get("mode") or envelope.get("mode") or "paper"),
        live_size_multiplier=None if raw_mult is None else _number(raw_mult),
        spec_hash=None if digest is None else str(digest),
        strategy_id=None if strategy_id is None else str(strategy_id),
        strategy_version=None if version is None else _whole(version),
        market=None if market is None else str(market),
        currency=None if currency is None else str(currency),
        lot_size=_whole(row.get("lot_size")),
    )


def load_intent_orders(home: Path) -> list[OrderIntent]:
    """Read ``intent/1.0`` files. ``ticker`` and ``qty`` are the engine names."""
    root = home / "intents"
    if not root.is_dir():
        return []
    found: list[OrderIntent] = []
    for path in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        rows = payload.get("intents")
        envelope = payload
        if not isinstance(rows, list):
            rows = [payload]
            envelope = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            intent = _row_intent(row, envelope)
            if intent is not None:
                found.append(intent)
    return found


def _broker_place(intent: OrderIntent) -> None:
    """Send one order. Called only after E2 turns ReadOnlyApi off."""
    import asyncio

    from auto_trading.config import get_settings
    from auto_trading.execution.ibkr import PaperGateway

    settings = get_settings()
    client_id = int(os.environ.get("IB_CLIENT_ID", "11"))
    gateway = PaperGateway(settings, client_id=client_id)
    asyncio.run(gateway.submit_order(intent))


def _production_context(home: Path, now: datetime) -> GateContext:
    """Risk snapshot from the local files. Missing broker data fails closed."""
    from auto_trading.execution.risk_state import verify_risk_limits_file
    from auto_trading.risk.gate import Held
    from auto_trading.risk.limits import EXPECTED_LIMITS

    account = _account(home)
    pin = _pin()
    gateway: dict[str, Any] = {}
    gpath = home / "state" / "gateway.json"
    if gpath.is_file():
        try:
            loaded = json.loads(gpath.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                gateway = loaded
        except (OSError, json.JSONDecodeError):
            gateway = {}
    accounts = tuple(str(item) for item in gateway.get("accounts") or [])
    if not accounts and pin:
        accounts = (pin,)
    nlv = float(account.get("nlv") or 0)
    limits = dict(EXPECTED_LIMITS)
    try:
        limits = verify_risk_limits_file(home)
    except Exception:
        limits = dict(EXPECTED_LIMITS)
    positions: list[Held] = []
    for row in account.get("positions") or []:
        if isinstance(row, dict) and row.get("symbol"):
            positions.append(Held(str(row["symbol"]), float(row.get("qty") or 0), 0.0))
    recon_ok = True
    recon_path = home / "state" / "recon.json"
    if recon_path.is_file():
        try:
            recon_ok = not bool(json.loads(recon_path.read_text(encoding="utf-8")).get("freeze"))
        except (OSError, json.JSONDecodeError):
            recon_ok = False
    return GateContext(
        port=_port() or 4002,
        accounts=accounts,
        expected_account=pin,
        baseline_accounts=accounts,
        nlv=nlv,
        cash=float(account.get("cash") or nlv),
        hwm=float(account.get("hwm") or nlv),
        day_start_nlv=float(account.get("day_start_nlv") or nlv),
        limits=limits,
        positions=positions,
        now=now,
        kill_switch=(home / "KILL_SWITCH").is_file(),
        recon_ok=recon_ok,
        connected=bool(account.get("gateway_logged_in")),
        trading_home=home,
        approved_dir=_strategies_root(home),
        fx_to_base={"HKD": 1.0},
    )


def _production_session(home: Path, now: datetime, approval: dict[str, str]) -> ApprovalSession:
    pin = _pin()

    def read_accounts() -> tuple[str, ...]:
        path = home / "state" / "gateway.json"
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                rows = payload.get("accounts") or []
                if rows:
                    return tuple(str(item) for item in rows)
            except (OSError, json.JSONDecodeError, AttributeError):
                return ()
        return (pin,) if pin else ()

    forced = os.environ.get("FORCE_MANUAL_APPROVAL", "").strip().lower() in {"1", "true", "yes"}
    return ApprovalSession(
        read_accounts=read_accounts,
        port=_port() or 4002,
        pin=pin,
        baseline=(pin,) if pin else (),
        kill_switch_present=lambda: (home / "KILL_SWITCH").is_file(),
        r0_violation=lambda: False,
        force_manual=lambda: forced,
        strategy_approval=lambda: approval["value"],
        now=lambda: now,
    )


def _file_approval(intent: OrderIntent, root: Path | None) -> str:
    from auto_trading.execution.strategy_book import file_for, load_approved

    if not intent.strategy_id or intent.strategy_version is None:
        return "manual"
    path = file_for(intent.strategy_id, intent.strategy_version, root)
    if path is None:
        return "manual"
    try:
        document = load_approved(path)
    except (OSError, ValueError, TypeError):
        return "manual"
    execution = document.get("execution")
    approval = execution.get("approval") if isinstance(execution, dict) else None
    return str(approval or "manual")


def _bind_approved_hash(ctx: GateContext, intent: OrderIntent) -> None:
    from auto_trading.execution.strategy_book import CONFIRMED_SPEC_HASHES, approved_row_hash

    if not intent.strategy_id or intent.strategy_version is None:
        return
    pinned = CONFIRMED_SPEC_HASHES.get((intent.strategy_id, intent.strategy_version))
    if pinned is not None:
        ctx.approved_hash = pinned
        return
    ctx.approved_hash = approved_row_hash(
        intent.strategy_id, intent.strategy_version, ctx.approved_dir
    )


def _journal_then_send(
    home: Path,
    intent: OrderIntent,
    session: ApprovalSession,
    now: datetime,
    sender: Callable[[OrderIntent], None],
) -> None:
    """Persist the order, then call the broker. A second pass will not resend."""
    from auto_trading.execution.approval_mode import record_auto_approval
    from auto_trading.execution.oms import order_ref

    key = intent.intent_id or intent.client_id
    order_id = order_ref(key, 1)
    price = float(intent.limit_price or 0)
    payload = {
        "intent_id": key,
        "symbol": intent.symbol,
        "side": intent.side.value,
        "quantity": intent.quantity,
        "account": intent.account,
    }
    record_auto_approval(
        home,
        order_id,
        payload,
        session,
        attempt=1,
        approval_price=price,
        order=intent,
    )
    journal = TradeJournal(home)
    journal.record_intent(
        {
            "intent_id": key,
            "strategy_id": intent.strategy_id,
            "strategy_version": intent.strategy_version,
            "spec_hash": intent.spec_hash,
            "signal_id": intent.signal_id,
            "symbol": intent.symbol,
            "side": intent.side.value,
            "quantity": intent.quantity,
        }
    )
    journal.create_order(
        order_id=order_id,
        intent_id=key,
        seq=1,
        symbol=intent.symbol,
        intent_type=intent.intent_type,
    )
    journal.set_order_state(order_id, "SUBMITTING", when=now)
    sender(intent)
    journal.set_order_state(order_id, "SUBMITTED", when=now)


def submit_once(
    home: Path,
    now: datetime,
    *,
    intents: list[OrderIntent] | None = None,
    ctx: GateContext | None = None,
    session: ApprovalSession | None = None,
    place: Callable[[OrderIntent], None] | None = None,
    readonly: bool | None = None,
) -> dict[str, Any]:
    """Engine intents, the full risk gate, approval, the journal, then the broker.

    While ReadOnlyApi is yes the function returns before any of that work.
    The same ``intent_id`` is not sent twice. ``mode`` other than paper, and
    any size multiplier, never reach the broker.
    """
    from auto_trading.execution.approvals import queue_order
    from auto_trading.risk.gate import evaluate

    is_readonly = read_only_api() if readonly is None else readonly
    if is_readonly:
        payload = {"status": "readonly", "sent": 0, "reason": "ReadOnlyApi"}
        _write_json(home / "state" / "submit.json", payload)
        return payload
    audit_confirmed_hashes(home, now)
    orders = list(intents) if intents is not None else load_intent_orders(home)
    context = ctx if ctx is not None else _production_context(home, now)
    approval = {"value": "auto"}
    active = session if session is not None else _production_session(home, now, approval)
    sender = place
    if sender is None and os.environ.get("TRADING_GATEWAY_CONNECT", "").strip() == "1":
        sender = _broker_place
    sent = 0
    skipped = 0
    gated: list[str] = []
    rejected: list[str] = []
    journal = TradeJournal(home)
    for intent in orders:
        key = intent.intent_id or intent.client_id
        if journal.intent_in_flight(key):
            skipped += 1
            continue
        _bind_approved_hash(context, intent)
        gated.append(intent.symbol)
        code = evaluate(intent, context)
        blocked = intent.mode != "paper" or intent.live_size_multiplier is not None
        if code or blocked:
            reason = code or "LIVE_NOT_AUTHORIZED"
            journal.record_risk_check(symbol=intent.symbol, code=reason, intent_id=key, when=now)
            rejected.append(reason)
            skipped += 1
            continue
        approval["value"] = _file_approval(intent, context.approved_dir)
        if session is None:
            active = _production_session(home, now, approval)
        if not is_auto_approver(decide_approval_mode(intent, active)):
            pending = home / "approvals" / "pending" / f"{key}.json"
            if not pending.is_file():
                queue_order(
                    home,
                    key,
                    {
                        "intent_id": key,
                        "symbol": intent.symbol,
                        "side": intent.side.value,
                        "quantity": intent.quantity,
                    },
                )
            skipped += 1
            continue
        if sender is None:
            skipped += 1
            continue
        try:
            _journal_then_send(home, intent, active, now, sender)
        except Exception:
            skipped += 1
            rejected.append("error")
            continue
        sent += 1
    payload = {
        "status": "open",
        "sent": sent,
        "skipped": skipped,
        "gated": gated,
        "rejected": rejected,
    }
    _write_json(home / "state" / "submit.json", payload)
    return payload


def main_once(home: Path, now: datetime) -> dict[str, Any]:
    """One local pass. Production does not enable this combined loop."""
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
        "account": ACCOUNT_INTERVAL_SEC,
        "submit": SUBMIT_INTERVAL_SEC,
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
        elif command == "account":
            account_once(home, now)
        else:
            submit_once(home, now)
        time.sleep(pause)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m auto_trading.execution``."""
    parser = argparse.ArgumentParser(prog="trading-service")
    parser.add_argument(
        "command",
        choices=("main", "oms", "recon", "watchdog", "sync", "summary", "account", "submit"),
    )
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
        elif args.command == "account":
            account_once(home, now)
        elif args.command == "submit":
            submit_once(home, now)
        else:
            watchdog_once(home, now)
        return 0
    run_forever(args.command, home)
    return 0
