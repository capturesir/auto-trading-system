"""The only function that chooses an approval mode.

Automatic paper approval is returned only when every R0 check passes for
the pinned paper account. Every other case stays on the manual queue.
Non-paper accounts cannot be switched by configuration.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from auto_trading.paper_account import DU_ACCOUNT

# The source text of this name is the one occurrence under src/.
APPROVER_AUTO = "auto_paper"


def approver_name() -> str:
    """Automatic approver. Other modules call this instead of reading the constant."""
    return APPROVER_AUTO


def is_auto_approver(value: object) -> bool:
    return value == APPROVER_AUTO


def approvals_check_sql() -> str:
    """SQLite CHECK. The approver text stays in this module."""
    return (
        "CHECK (approver IS NULL OR approver <> '"
        + APPROVER_AUTO
        + "' OR json_extract(r0_check_json, '$.all_ok') = 1)"
    )


MANUAL = "manual"
PAPER_PORTS = (4002, 7497)


@dataclass
class ApprovalSession:
    """Fresh inputs. Account, kill-switch, and R0 readers run on every call."""

    read_accounts: Callable[[], Sequence[str]]
    port: int
    pin: str
    baseline: tuple[str, ...]
    kill_switch_present: Callable[[], bool]
    r0_violation: Callable[[], bool]
    force_manual: Callable[[], bool]
    strategy_approval: Callable[[], str]
    now: Callable[[], datetime] | None = None


def _now(session: ApprovalSession) -> datetime:
    if session.now is None:
        return datetime.now(UTC)
    return session.now()


def _order_account(order: Any) -> str:
    if isinstance(order, Mapping):
        return str(order.get("account") or "")
    return str(getattr(order, "account", "") or "")


def _strategy_approval(order: Any, session: ApprovalSession) -> str:
    if isinstance(order, Mapping) and order.get("execution_approval") is not None:
        return str(order["execution_approval"])
    value = getattr(order, "execution_approval", None)
    if value is not None:
        return str(value)
    return session.strategy_approval()


def r0_snapshot(order: Any, session: ApprovalSession) -> dict[str, Any]:
    """Re-read accounts and build the check object stored with an approval."""
    accounts = tuple(session.read_accounts())
    pin = session.pin.strip()
    du_ok = bool(accounts) and all(DU_ACCOUNT.fullmatch(item) is not None for item in accounts)
    exact = bool(pin) and set(accounts) == {pin}
    single = len(accounts) == 1
    baseline_ok = set(accounts) == set(session.baseline)
    port_ok = session.port in PAPER_PORTS
    order_ok = _order_account(order) == pin and bool(pin)
    kill_absent = not session.kill_switch_present()
    r0_clear = not session.r0_violation()
    flags = (du_ok, exact, single, baseline_ok, port_ok, order_ok, kill_absent, r0_clear)
    return {
        "port": session.port,
        "managed_accounts": list(accounts),
        "pin": pin,
        "du_regex_ok": du_ok,
        "exact_set_ok": exact,
        "single_account_ok": single,
        "baseline_ok": baseline_ok,
        "order_account_ok": order_ok,
        "kill_switch_absent": kill_absent,
        "r0_clear": r0_clear,
        "all_ok": 1 if all(flags) else 0,
        "checked_at_utc": _now(session).astimezone(UTC).isoformat(timespec="seconds"),
    }


def approval_block_reason(order: Any, session: ApprovalSession) -> str:
    """Why this order cannot be approved automatically. ``ok`` means it can."""
    snap = r0_snapshot(order, session)
    if snap["all_ok"] != 1:
        if session.port not in PAPER_PORTS:
            return "port"
        if not snap["du_regex_ok"]:
            return "du_regex"
        if not session.pin.strip():
            return "pin_missing"
        if not snap["exact_set_ok"]:
            return "exact_set"
        if not snap["single_account_ok"]:
            return "single_account"
        if not snap["baseline_ok"]:
            return "baseline"
        if not snap["order_account_ok"]:
            return "order_account"
        if not snap["kill_switch_absent"]:
            return "kill_switch"
        return "r0_violation"
    if session.force_manual():
        return "force_manual"
    if _strategy_approval(order, session) != "auto":
        return "strategy_approval"
    return "ok"


def decide_approval_mode(order: Any, session: ApprovalSession) -> str:
    """Return automatic paper approval only when every R0 check passes.

    Callers re-read managed accounts through ``session.read_accounts``.
    A stale startup list is not consulted here.
    """
    if approval_block_reason(order, session) == "ok":
        return APPROVER_AUTO
    return MANUAL


def record_auto_approval(
    trading_home: Path,
    order_id: str,
    payload: Mapping[str, Any],
    session: ApprovalSession,
    *,
    attempt: int,
    approval_price: float,
    order: Any | None = None,
) -> dict[str, Any]:
    """Write the decided file and the approvals row. Never writes pending/."""
    from auto_trading.execution.approvals import order_hash, signed_body
    from auto_trading.portfolio.journal import TradeJournal

    if attempt not in (1, 2):
        raise ValueError("attempt must be 1 or 2")
    target = order if order is not None else payload
    snap = r0_snapshot(target, session)
    if snap["all_ok"] != 1 or decide_approval_mode(target, session) != APPROVER_AUTO:
        raise ValueError("automatic approval requires a passing R0 check")
    moment = _now(session)
    checked = datetime.fromisoformat(snap["checked_at_utc"])
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    if (moment - checked).total_seconds() > 5 or checked > moment:
        raise ValueError("r0 check is not fresh")
    document = {
        **dict(payload),
        "order_id": order_id,
        "decision": "approved",
        "approver": APPROVER_AUTO,
        "channel": "auto",
        "attempt": attempt,
        "approval_price": approval_price,
        "decided_at_utc": moment.astimezone(UTC).isoformat(timespec="seconds"),
        "r0_check": snap,
    }
    digest = order_hash(
        signed_body(
            dict(payload),
            order_id=order_id,
            approval_price=approval_price,
            attempt=attempt,
        )
    )
    document["order_hash"] = digest
    folder = trading_home / "approvals" / "decided"
    folder.mkdir(parents=True, exist_ok=True)
    target_path = folder / f"{order_id}.json"
    temporary = target_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target_path)
    pending = trading_home / "approvals" / "pending" / f"{order_id}.json"
    if pending.is_file():
        pending.unlink()
    TradeJournal(trading_home).record_approval(
        order_id=order_id,
        order_hash=digest,
        status="APPROVED",
        detail={"decision": "approved"},
        when=moment,
        approver=APPROVER_AUTO,
        channel="auto",
        attempt=attempt,
        approval_price=approval_price,
        r0_check=snap,
    )
    return document
