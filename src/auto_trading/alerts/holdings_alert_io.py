"""Write ``alert/1.0`` files under ``alerts/pending/``.

Rule ids start with ``hm_``. This writer refuses execution-alert schemas
and does not create order files.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from auto_trading.alerts.holdings_state import atomic_write_json

ALERT_SCHEMA = "alert/1.0"


class AlertWriteError(ValueError):
    """The payload is not a holdings-monitor alert."""


def require_hm_rule(rule_id: str) -> str:
    """Accept only holdings-monitor rule ids."""
    if not rule_id.startswith("hm_"):
        raise AlertWriteError("rule_id must start with hm_")
    folded = rule_id.lower()
    if folded.startswith("exec") or "exec" in folded:
        raise AlertWriteError("exec rule ids are refused")
    return rule_id


def write_hm_alert(
    trading_home: Path,
    alert: Mapping[str, Any],
    *,
    filename_stamp: str,
    file_ticker: str | None = None,
) -> Path:
    """Atomically write one pending alert. The file is not read back."""
    schema = str(alert.get("schema_version") or "")
    if schema != ALERT_SCHEMA:
        raise AlertWriteError("schema must be alert/1.0")
    rule_id = require_hm_rule(str(alert.get("rule_id") or ""))
    label = file_ticker if file_ticker else str(alert.get("ticker") or "NA")
    ticker = label.replace("/", "_").replace(" ", "_")
    pending = trading_home / "alerts" / "pending"
    target = pending / f"{filename_stamp}_{rule_id}_{ticker}.json"
    if "_exec_" in target.name:
        raise AlertWriteError("exec filenames are refused")
    atomic_write_json(target, dict(alert))
    return target
