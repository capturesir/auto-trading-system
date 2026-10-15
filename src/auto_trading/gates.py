"""Paper evaluation report (G1–G4).

The report is informational. It does not change the running mode and it
does not open a real account. A cleared R9 halt still fails G4.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from auto_trading.alerts.pending import build_alert, write_pending

BLOCKING = frozenset({"UNKNOWN_ORDER", "RECON_MISMATCH", "HASH_MISMATCH", "R9_HALT", "KILL_SWITCH"})


def g4_failures(events: list[Mapping[str, Any]]) -> list[str]:
    """Return the blocking kinds present in ``anomaly_events``."""
    found: list[str] = []
    for event in events:
        kind = str(event.get("kind"))
        if kind in BLOCKING and kind not in found:
            found.append(kind)
    return found


def g4_passes(events: list[Mapping[str, Any]]) -> bool:
    """False when any blocking anomaly was recorded, including a cleared halt."""
    return not g4_failures(events)


def evaluate_paper(
    *,
    strategy_id: str,
    version: int,
    session_date: str,
    calendar_days: int,
    fill_count: int,
    period_return: float,
    drawdown: float,
    return_p5: float,
    return_p95: float,
    mdd_p5: float,
    anomalies: list[Mapping[str, Any]] | None = None,
    reference: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score G1–G4. ``mode_after`` stays paper. ``promotes`` stays false."""
    events = list(anomalies or [])
    failures = g4_failures(events)
    g1 = calendar_days >= 28
    g2 = fill_count >= 10
    g3 = return_p5 <= period_return <= return_p95 and drawdown >= mdd_p5
    return {
        "schema_version": "paper_evaluation/1.0",
        "strategy_id": strategy_id,
        "version": version,
        "date": session_date,
        "mode_after": "paper",
        "promotes": False,
        "gates": {
            "G1": {"calendar_days": calendar_days, "minimum": 28, "met": g1},
            "G2": {"fills": fill_count, "minimum": 10, "met": g2},
            "G3": {
                "period_return": period_return,
                "p5": return_p5,
                "p95": return_p95,
                "drawdown": drawdown,
                "mdd_p5": mdd_p5,
                "met": g3,
            },
            "G4": {"failures": failures, "met": not failures},
        },
        "reference": dict(reference or {}),
    }


def write_paper_evaluation(
    trading_home: Path,
    report: Mapping[str, Any],
    *,
    created_at: datetime,
) -> tuple[Path, dict[str, Any]]:
    """Write the report and a ``paper_eval_status`` alert. Mode stays paper."""
    strategy_id = str(report["strategy_id"])
    version = int(report["version"])
    session_date = str(report["date"])
    folder = trading_home / "strategies" / "evaluations"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{strategy_id}.v{version}_{session_date}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(dict(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    met = [name for name, gate in report["gates"].items() if gate.get("met")]
    alert = build_alert(
        rule_id="paper_eval",
        alert_type="paper_eval_status",
        severity="info",
        ticker="ACCOUNT",
        message_zh=(
            f"{strategy_id} v{version} 模擬盤評估完成，"
            f"達到 {', '.join(met) or '無'}。模式仍是模擬。"
        ),
        trading_date=session_date,
        created_at=created_at,
        extra={"context": {"mode_after": "paper", "promotes": False}},
    )
    write_pending(trading_home, alert, filename_stamp=session_date.replace("-", ""))
    event = {
        "strategy_id": strategy_id,
        "version": version,
        "event": "paper_evaluated",
        "actor": "system",
        "mode_after": "paper",
        "note": str(target.name),
    }
    return target, event
