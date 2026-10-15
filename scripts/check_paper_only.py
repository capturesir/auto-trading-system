"""Fail the build when a real-account switch is present."""

from __future__ import annotations

import sys
from pathlib import Path

from auto_trading.execution.r0 import DU_ACCOUNT as ADAPTER_ACCOUNT
from auto_trading.paper_account import DU_ACCOUNT
from auto_trading.paper_only import scan_tree

ROOT = Path(__file__).resolve().parents[1]
PAPER_ACCOUNT = r"^DU[A-Z]?[0-9]+$"


def main() -> int:
    patterns = (DU_ACCOUNT.pattern, ADAPTER_ACCOUNT.pattern)
    if any(pattern != PAPER_ACCOUNT for pattern in patterns):
        print("paper-only scan failed: account rule", file=sys.stderr)
        return 1
    findings = scan_tree(ROOT)
    if not findings:
        print("paper-only scan: no real-account switches")
        return 0
    print("paper-only scan failed:", file=sys.stderr)
    for line in findings:
        print(line, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
