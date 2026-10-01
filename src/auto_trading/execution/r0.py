"""R0 paper-account guard.

Every managed account must match ``^DU[0-9]+$``. Ports other than the paper
Gateway and paper TWS ports are refused before connect. A change in the
account set after the session baseline is a violation: new orders and
modifications stop, a full stop is latched, and cancels still pass.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

DU_ACCOUNT = re.compile(r"^DU[0-9]+$")
PAPER_PORTS = (4002, 7497)


class PaperAccountError(Exception):
    """R0 refused the session or the order."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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
        self.expected_account = expected_account
        self.baseline: frozenset[str] | None = None
        self.violation: str | None = None
        self._on_violation = on_violation
        self.check_port()

    def check_port(self) -> None:
        if self.port not in PAPER_PORTS:
            self._trip("R0_LIVE_PORT")

    def observe(self, accounts: list[str]) -> None:
        """Check one managedAccounts snapshot. The first clean set is the baseline."""
        if self.violation:
            raise PaperAccountError(self.violation)
        if not accounts or any(DU_ACCOUNT.fullmatch(account) is None for account in accounts):
            self._trip("R0_NOT_PAPER_ACCOUNT")
        if self.expected_account and self.expected_account not in accounts:
            self._trip("R0_NOT_PAPER_ACCOUNT")
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
            self._trip("R0_NOT_PAPER_ACCOUNT")

    def _trip(self, code: str) -> None:
        self.violation = code
        if self._on_violation is not None:
            self._on_violation(code)
        raise PaperAccountError(code)


def latch_kill_switch(trading_home: Path, code: str) -> None:
    """Full stop used by an R0 violation. Cancels are not blocked by this file."""
    trading_home.mkdir(parents=True, exist_ok=True)
    (trading_home / "KILL_SWITCH").write_text(code + "\n", encoding="utf-8")
