"""Risk-limit snapshot checks (execution spec §5.5.1, strategy spec §2.10).

The strategy side reads ``state/risk_limits.json`` and compares it with
``config/risk_limits_expected.json``. A stale, missing, unreadable, unknown,
or hash-mismatched file blocks ENTRY and ADD. REDUCE and EXIT still run.
``tw_odd_lot_enabled`` is false until the odd-lot check (E1) passes. The
``true`` hash is only a comparison vector.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from auto_trading.jsonutil import canonical_dumps, loads_strict, sha256_prefixed

SCHEMA_VERSION = "risk_limits/1.0"
LIMIT_KEYS = (
    "R1_order_size_pct_nlv",
    "R2_max_order_notional_base",
    "R3_max_position_pct_nlv",
    "R4_max_market_pct_nlv",
    "R5_max_ccy_pct_nlv",
    "R6_max_gross_exposure_pct_nlv",
    "R7_require_cash_cover",
    "R8_daily_loss_halt_pct",
    "R9_drawdown_warn_pct",
    "R9_drawdown_halt_pct",
    "R15_max_orders_per_day",
    "R23_consecutive_loss_demote",
    "protective_stop_mult",
    "default_protective_stop_pct",
    "min_confidence",
    "tw_odd_lot_enabled",
)
INTEGER_KEYS = frozenset(
    {
        "R2_max_order_notional_base",
        "R15_max_orders_per_day",
        "R23_consecutive_loss_demote",
        "min_confidence",
    }
)
HASH_ODD_LOT_OFF = "sha256:02460f0fef18180d32c5f0645a6ac0475903568c83a7b159a92776fb8ea3907b"
HASH_ODD_LOT_ON = "sha256:39c52f1586658e8c9bd23be398dd1bd8eef75a04f6c3b8cc85da427e2533c28d"
HASH_R2_AS_FLOAT = "sha256:64039ee55267c8e567181113adc53691e435fb9bd05f308d0d786f6301328cdb"

EXPECTED_LIMITS: dict[str, Any] = {
    "R1_order_size_pct_nlv": 0.05,
    "R2_max_order_notional_base": 200000,
    "R3_max_position_pct_nlv": 0.10,
    "R4_max_market_pct_nlv": {"US": 0.60, "HK": 0.40, "JP": 0.30, "KR": 0.20, "TW": 0.20},
    "R5_max_ccy_pct_nlv": {"USD": 0.60, "JPY": 0.30, "KRW": 0.20, "TWD": 0.20},
    "R6_max_gross_exposure_pct_nlv": 0.95,
    "R7_require_cash_cover": True,
    "R8_daily_loss_halt_pct": -0.02,
    "R9_drawdown_warn_pct": -0.05,
    "R9_drawdown_halt_pct": -0.10,
    "R15_max_orders_per_day": 20,
    "R23_consecutive_loss_demote": 3,
    "protective_stop_mult": 1.5,
    "default_protective_stop_pct": 0.15,
    "min_confidence": 60,
    "tw_odd_lot_enabled": False,
}


def config_hash(limits: Mapping[str, Any]) -> str:
    """Hash the ``limits`` object only, using the locked serialization."""
    return sha256_prefixed(dict(limits))


def canonical_limits(limits: Mapping[str, Any]) -> str:
    """Return the exact string that is hashed."""
    return canonical_dumps(dict(limits))


@dataclass(frozen=True)
class RiskConfigStatus:
    """Result of comparing a live snapshot with the expected limits."""

    entries_allowed: bool
    reason: str | None
    config_hash: str | None
    odd_lot_enabled: bool


def _status(reason: str | None, digest: str | None, odd_lot: bool) -> RiskConfigStatus:
    return RiskConfigStatus(
        entries_allowed=reason is None,
        reason=reason,
        config_hash=digest,
        odd_lot_enabled=odd_lot,
    )


def _odd_lot(limits: Mapping[str, Any]) -> bool:
    return bool(limits.get("tw_odd_lot_enabled", False))


def assess_payload(
    payload: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    now: datetime,
) -> RiskConfigStatus:
    """Check one already-parsed ``risk_limits.json`` document."""
    if payload.get("schema_version") != SCHEMA_VERSION:
        return _status("unknown_schema", None, False)
    limits = payload.get("limits")
    if not isinstance(limits, Mapping) or set(limits) != set(LIMIT_KEYS):
        return _status("hash_mismatch", None, False)
    for key in INTEGER_KEYS:
        value = limits[key]
        if isinstance(value, bool) or not isinstance(value, int):
            digest = config_hash(limits)
            return _status("hash_mismatch", digest, _odd_lot(limits))
    digest = config_hash(limits)
    declared = payload.get("config_hash")
    expected_digest = config_hash(expected)
    if digest != declared or digest != expected_digest:
        return _status("hash_mismatch", digest, _odd_lot(limits))
    generated = _parse_time(payload.get("generated_at"))
    stale_after = payload.get("stale_after_sec", 900)
    if generated is None or not isinstance(stale_after, int):
        return _status("unreadable", digest, _odd_lot(limits))
    if now - generated > timedelta(seconds=stale_after):
        return _status("stale", digest, _odd_lot(limits))
    return _status(None, digest, _odd_lot(limits))


def assess_files(
    snapshot_path: Path,
    expected_path: Path,
    *,
    now: datetime,
) -> RiskConfigStatus:
    """Load both files. Any problem blocks new entries."""
    if not snapshot_path.is_file():
        return _status("missing", None, False)
    try:
        payload = loads_strict(snapshot_path.read_text(encoding="utf-8"))
        expected = loads_strict(expected_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return _status("unreadable", None, False)
    if not isinstance(payload, Mapping) or not isinstance(expected, Mapping):
        return _status("unreadable", None, False)
    return assess_payload(payload, expected, now=now)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed
