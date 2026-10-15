"""Shared R9 cycle vectors. Execution and the backtest use one state machine."""

from __future__ import annotations

import json
from pathlib import Path

from auto_trading.risk.r9_cycle import (
    early_resume,
    initial_state,
    on_nlv,
    on_risk_day_end,
    size_mult,
)

_VECTORS = json.loads(
    (Path(__file__).parent / "vectors" / "r9_cycle_vectors.json").read_text(encoding="utf-8")
)


def test_shared_vectors() -> None:
    for vector in _VECTORS["vectors"]:
        state = initial_state(vector["initial_hwm"])
        for index, record in enumerate(vector["steps"]):
            step = record["step"]
            if step["op"] == "nlv":
                state, events = on_nlv(state, step["risk_date"], step["nlv"])
            elif step["op"] == "day_end":
                state, events = on_risk_day_end(
                    state,
                    step["risk_date"],
                    step["xhkg"],
                    step["nlv"],
                    step.get("blocked", False),
                )
            else:
                state, events = early_resume(state, step["risk_date"], step["nlv"])
            got = {
                "state": state["state"],
                "hwm": state["hwm"],
                "cooldown_days_done": state["cooldown_days_done"],
                "half_size_days_done": state["half_size_days_done"],
                "size_mult": size_mult(state),
                "events": [event["kind"] for event in events],
            }
            assert got == record["expect"], f"{vector['name']} step {index}"


def test_one_state_machine_definition() -> None:
    needles = ("def on_nlv(", "def on_risk_day_end(", "def early_resume(")
    hits: list[Path] = []
    for path in Path("src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(needle in text for needle in needles):
            hits.append(path)
    assert hits == [Path("src/auto_trading/risk/r9_cycle.py")]
