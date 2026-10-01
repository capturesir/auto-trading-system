"""Publish ``state/risk_limits.json`` schema ``risk_limits/1.1``.

``config_hash`` covers only the limits object, so it matches the shared
vector while ``trading_scope`` stays outside that object.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from auto_trading.risk.limits import EXPECTED_LIMITS, config_hash


class RiskConfigMismatch(Exception):
    """``state/risk_limits.json`` failed the paper-only hash check."""


def build_risk_limits(
    *,
    now: datetime,
    nlv: float,
    account: str = "",
    seq: int = 1,
    kill_switch: bool = False,
) -> dict[str, Any]:
    """One snapshot. ``trading_scope`` is fixed and is not hashed."""
    limits = dict(EXPECTED_LIMITS)
    order_cap = min(nlv * limits["R1_order_size_pct_nlv"], limits["R2_max_order_notional_base"])
    suffix = account[-4:] if len(account) >= 4 else account
    return {
        "schema_version": "risk_limits/1.1",
        "seq": seq,
        "generated_at": now.isoformat(timespec="seconds"),
        "stale_after_sec": 900,
        "base_currency": "HKD",
        "trading_scope": "paper_only",
        "config_hash": config_hash(limits),
        "limits": limits,
        "accounts": [
            {
                "trading_mode": "paper",  # paper_only; a real-account mode is refused
                "account_suffix": suffix,
                "nlv_base": nlv,
                "breakers": {
                    "r8_daily_halt": False,
                    "r9_state": "normal",
                    "kill_switch": kill_switch,
                    "recon_freeze": False,
                    "infra_pause": False,
                    "paused_strategies": [],
                    "demoted_strategies": [],
                },
                "headroom": {"order_max_notional_base": order_cap},
            }
        ],
    }


def verify_risk_limits_file(trading_home: Path) -> dict[str, Any]:
    """Re-read the file. Refuse unless the hash and the scope both match."""
    path = trading_home / "state" / "risk_limits.json"
    if not path.is_file():
        raise RiskConfigMismatch("missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RiskConfigMismatch("unreadable") from exc
    if payload.get("schema_version") != "risk_limits/1.1":
        raise RiskConfigMismatch("schema")
    if payload.get("trading_scope") != "paper_only":
        raise RiskConfigMismatch("scope")
    limits = payload.get("limits")
    if not isinstance(limits, dict):
        raise RiskConfigMismatch("limits")
    digest = config_hash(limits)
    if digest != payload.get("config_hash"):
        raise RiskConfigMismatch("hash")
    if digest != config_hash(EXPECTED_LIMITS):
        raise RiskConfigMismatch("hash")
    return limits


def write_risk_limits(trading_home: Path, payload: dict[str, Any]) -> Path:
    """Atomic replace of ``state/risk_limits.json``, plus one JSONL history line."""
    folder = trading_home / "state"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "risk_limits.json"
    temporary = target.with_suffix(".json.tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    directory = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    stamp = str(payload.get("generated_at", ""))[:10].replace("-", "")
    if len(stamp) == 8 and stamp.isdigit():
        log = trading_home / "logs" / f"risk_limits_{stamp}.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return target
