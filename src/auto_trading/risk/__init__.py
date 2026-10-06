"""Risk gate. This is the only path that may release an order to the executor.

Strategy specs and any language model cannot bypass or relax this gate.
The gate loads on first use so a submodule can be imported without pulling
the executor back into a half-initialized package.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "GateContext",
    "Held",
    "OpenOrder",
    "Quote",
    "RiskGate",
    "RiskRejected",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from auto_trading.risk import gate

        return getattr(gate, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
