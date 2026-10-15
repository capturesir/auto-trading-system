"""R0 paper-account guard.

Every managed account must match ``^DU[A-Z]?[0-9]+$`` (one optional letter
after ``DU``, then digits). ``IBKR_PAPER_ACCOUNT`` is required and must be
that exact account; any other account or an extra account is refused. Ports
other than the paper Gateway and paper TWS ports are refused before connect.
A mismatch or a later change in the account set stops new orders and
modifications, disconnects, latches a full stop, and still allows cancels.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from pathlib import Path

from auto_trading.paper_account import DU_ACCOUNT
from auto_trading.paper_only import account_accepted, port_accepted
from auto_trading.portfolio.journal import TradeJournal

PAPER_PORTS = (4002, 7497)
_PIN_ENV = "IBKR_PAPER_ACCOUNT"


class PaperAccountError(Exception):
    """R0 refused the session or the order."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def paper_account_pin(explicit: str = "") -> str:
    """Return the required account pin. Empty means the pin is unset."""
    chosen = explicit.strip()
    if not chosen:
        chosen = os.environ.get(_PIN_ENV, "")
    return chosen.strip()


class AccountGuard:
    """Remembers the accounts seen at connect and compares later snapshots."""

    def __init__(
        self,
        port: int,
        expected_account: str = "",
        *,
        on_violation: Callable[[str], None] | None = None,
    ) -> None:
        self.port = port
        self.expected_account = paper_account_pin(expected_account)
        self.baseline: frozenset[str] | None = None
        self.violation: str | None = None
        self._on_violation = on_violation
        self.check_port()
        if not self.expected_account or DU_ACCOUNT.fullmatch(self.expected_account) is None:
            self._trip("R0_NOT_PAPER_ACCOUNT")

    def check_port(self) -> None:
        if not port_accepted(self.port):
            self._trip("R0_FORBIDDEN_PORT")

    def observe(self, accounts: list[str]) -> None:
        """Check one managedAccounts snapshot. The first clean set is the baseline."""
        if self.violation:
            raise PaperAccountError(self.violation)
        if not accounts or any(DU_ACCOUNT.fullmatch(account) is None for account in accounts):
            self._trip("R0_NOT_PAPER_ACCOUNT")
        self._require_pin(accounts)
        current = frozenset(accounts)
        if self.baseline is None:
            self.baseline = current
            return
        if current != self.baseline:
            self._trip("R0_ACCOUNTS_CHANGED")

    def assert_order_account(self, account: str) -> None:
        if self.violation:
            raise PaperAccountError(self.violation)
        if account and DU_ACCOUNT.fullmatch(account) is None:
            self._trip("R0_NOT_PAPER_ACCOUNT")
        if self.expected_account and account and account != self.expected_account:
            self._trip("R0_ACCOUNTS_CHANGED")

    def _require_pin(self, accounts: list[str]) -> None:
        """A set pin must be the only managed account. Anything else latches."""
        pin = self.expected_account
        if (
            not pin
            or frozenset(accounts) != {pin}
            or any(not account_accepted(item, pin) for item in accounts)
        ):
            self._trip("R0_ACCOUNTS_CHANGED")

    def _trip(self, code: str) -> None:
        self.violation = code
        if self._on_violation is not None:
            self._on_violation(code)
        raise PaperAccountError(code)


def latch_kill_switch(trading_home: Path, code: str) -> None:
    """Full stop used by an R0 violation. Cancels are not blocked by this file."""
    trading_home.mkdir(parents=True, exist_ok=True)
    (trading_home / "KILL_SWITCH").write_text(code + "\n", encoding="utf-8")
    with contextlib.suppress(Exception):
        TradeJournal(trading_home).record_anomaly(kind="R0_VIOLATION", detail={"code": code})
    try:
        from datetime import UTC, datetime

        from auto_trading.alerts.critical import HINT_FULL_STOP, write_critical_event

        write_critical_event(
            trading_home,
            kind="r0_violation",
            reason_code=code,
            rule_id="R0",
            now=datetime.now(UTC),
            account=paper_account_pin(),
            message_zh="模擬帳戶檢查失敗，已全停並斷線。",
            resume_hint=HINT_FULL_STOP,
            actions={
                "kill_switch_file": True,
                "open_orders_cancelled": False,
                "protective_stops_kept": True,
                "disconnected": True,
                "strategies_halted": True,
            },
        )
    except Exception:
        return
