"""config_hash serialization from execution spec §5.5.1."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from auto_trading.jsonutil import loads_strict
from auto_trading.risk.limits import (
    EXPECTED_LIMITS,
    HASH_ODD_LOT_OFF,
    HASH_ODD_LOT_ON,
    HASH_R2_AS_FLOAT,
    LIMIT_KEYS,
    assess_files,
    assess_payload,
    canonical_limits,
    config_hash,
)

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PATH = ROOT / "config" / "risk_limits_expected.json"
CANONICAL = (
    '{"R15_max_orders_per_day":20,"R1_order_size_pct_nlv":0.05,'
    '"R23_consecutive_loss_demote":3,"R2_max_order_notional_base":200000,'
    '"R3_max_position_pct_nlv":0.1,'
    '"R4_max_market_pct_nlv":{"HK":0.4,"JP":0.3,"KR":0.2,"TW":0.2,"US":0.6},'
    '"R5_max_ccy_pct_nlv":{"JPY":0.3,"KRW":0.2,"TWD":0.2,"USD":0.6},'
    '"R6_max_gross_exposure_pct_nlv":0.95,"R7_require_cash_cover":true,'
    '"R8_daily_loss_halt_pct":-0.02,"R9_drawdown_halt_pct":-0.1,'
    '"R9_drawdown_warn_pct":-0.05,"default_protective_stop_pct":0.15,'
    '"min_confidence":60,"protective_stop_mult":1.5,"tw_odd_lot_enabled":false}'
)
HKT = timezone(timedelta(hours=8))


def _payload(limits: dict, generated: datetime, digest: str | None = None) -> dict:
    return {
        "schema_version": "risk_limits/1.0",
        "generated_at": generated.isoformat(timespec="seconds"),
        "stale_after_sec": 900,
        "config_hash": digest if digest is not None else config_hash(limits),
        "limits": limits,
    }


def test_vectors_and_expected_file() -> None:
    assert len(LIMIT_KEYS) == 16
    assert canonical_limits(EXPECTED_LIMITS) == CANONICAL
    assert config_hash(EXPECTED_LIMITS) == HASH_ODD_LOT_OFF
    loaded = loads_strict(EXPECTED_PATH.read_text(encoding="utf-8"))
    assert config_hash(loaded) == HASH_ODD_LOT_OFF
    assert loaded["tw_odd_lot_enabled"] is False
    enabled = dict(EXPECTED_LIMITS)
    enabled["tw_odd_lot_enabled"] = True
    assert config_hash(enabled) == HASH_ODD_LOT_ON
    floated = dict(EXPECTED_LIMITS)
    floated["R2_max_order_notional_base"] = 200000.0
    assert config_hash(floated) == HASH_R2_AS_FLOAT
    assert config_hash(EXPECTED_LIMITS) != config_hash(floated)


def test_point_one_collapses_and_integer_does_not() -> None:
    same = dict(EXPECTED_LIMITS)
    same["R3_max_position_pct_nlv"] = 0.10
    assert config_hash(same) == HASH_ODD_LOT_OFF
    assert '"R3_max_position_pct_nlv":0.1' in canonical_limits(EXPECTED_LIMITS)
    assert "200000.0" not in canonical_limits(EXPECTED_LIMITS)


def test_fresh_snapshot_allows_entries() -> None:
    now = datetime(2026, 10, 2, 7, 5, tzinfo=HKT)
    payload = _payload(dict(EXPECTED_LIMITS), now)
    status = assess_payload(payload, EXPECTED_LIMITS, now=now)
    assert status.entries_allowed
    assert status.odd_lot_enabled is False
    assert status.config_hash == HASH_ODD_LOT_OFF


def test_stale_missing_and_mismatch_block_entries(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, 7, 5, tzinfo=HKT)
    stale = assess_payload(
        _payload(dict(EXPECTED_LIMITS), now - timedelta(seconds=901)),
        EXPECTED_LIMITS,
        now=now,
    )
    assert stale.reason == "stale" and not stale.entries_allowed
    unknown = assess_payload(
        {**_payload(dict(EXPECTED_LIMITS), now), "schema_version": "risk_limits/9.0"},
        EXPECTED_LIMITS,
        now=now,
    )
    assert unknown.reason == "unknown_schema"
    floated = dict(EXPECTED_LIMITS)
    floated["R2_max_order_notional_base"] = 200000.0
    mismatched = assess_payload(_payload(floated, now), EXPECTED_LIMITS, now=now)
    assert mismatched.reason == "hash_mismatch"
    missing = assess_files(tmp_path / "nope.json", EXPECTED_PATH, now=now)
    assert missing.reason == "missing"
    bad = tmp_path / "risk_limits.json"
    bad.write_text("{", encoding="utf-8")
    assert assess_files(bad, EXPECTED_PATH, now=now).reason == "unreadable"
