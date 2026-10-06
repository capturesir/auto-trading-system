"""R0 paper-account guard.

Every managed account must match ``^DU[A-Z]?[0-9]+$`` (one optional letter
after ``DU``, then digits). Ports other than the paper Gateway and paper
TWS ports are refused before connect. When ``IBKR_PAPER_ACCOUNT`` is set,
``managedAccounts`` must be exactly that one account. A mismatch or a later
change in the account set stops new orders and modifications, latches a
full stop, and still allows cancels.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from auto_trading.paper_account import DU_ACCOUNT

PAPER_PORTS = (4002, 7497)
_PIN_ENV = "IBKR_PAPER_ACCOUNT"


class PaperAccountError(Exception):
    """R0 refused the session or the order."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def paper_account_pin(explicit: str = "") -> str:
    """Return the optional account pin. Empty means the pin is unset."""
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
        if self.expected_account and DU_ACCOUNT.fullmatch(self.expected_account) is None:
            self._trip("R0_NOT_PAPER_ACCOUNT")

    def check_port(self) -> None:
        if self.port not in PAPER_PORTS:
            self._trip("R0_LIVE_PORT")

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
        if self.expected_account and frozenset(accounts) != {self.expected_account}:
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
