"""Strategy engine.

The engine may turn an approved spec plus signals and indicators into an
order intent. It does not place orders and it does not approve its own
specs. An LLM is not part of this package.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from auto_trading.strategy.spec import ApprovedStrategy, require_approved


class StrategyEngine:
    """Runs a strategy document only after :func:`require_approved` succeeds."""

    def run(self, document: Mapping[str, Any], approved: ApprovedStrategy) -> None:
        require_approved(document, approved)
        raise NotImplementedError("strategy execution is not implemented yet")
