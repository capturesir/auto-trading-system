"""Loader coverage for valid, empty, malformed, and expired signal files."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from auto_trading.config import DEFAULT_SIGNALS_PATH, get_settings
from auto_trading.signals import SignalFileError, SignalValidationError, load_signals
from auto_trading.signals.models import Side, Signal

FIXTURES = Path(__file__).parent / "fixtures"

HKT = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 10, 1, 12, 0, tzinfo=HKT)


def write_json(directory: Path, name: str, payload: object) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_valid_signal_file() -> None:
    document = load_signals(FIXTURES / "valid.json", now=AS_OF)

    assert document.schema_version == "1.0"
    assert document.generated_by == "投資經理 (A組)"
    assert document.generated_at == datetime(2026, 10, 1, 6, 55, tzinfo=HKT)
    assert document.price_date.isoformat() == "2026-09-30"
    assert len(document.signals) == 1

    signal = document.signals[0]
    assert signal.signal_id == "20261001-0883.HK"
    assert signal.ticker == "0883.HK"
    assert signal.market == "HK"
    assert signal.name == "中國海洋石油 CNOOC"
    assert signal.side is Side.BUY
    assert signal.confidence == 72
    assert signal.fair_value == 31.15
    assert signal.market_price == 23.96
    assert signal.upside_pct == 30.0
    assert signal.rationale.startswith("（範例）")
    assert signal.expired is False
    assert "tradable" not in Signal.model_fields


def test_empty_signals_file_is_valid() -> None:
    document = load_signals(FIXTURES / "empty.json", now=AS_OF)

    assert document.signals == []
    assert document.schema_version == "1.0"


def test_malformed_json_raises_clear_error() -> None:
    path = FIXTURES / "malformed.json"

    with pytest.raises(SignalFileError, match="not valid JSON") as caught:
        load_signals(path, now=AS_OF)

    message = str(caught.value)
    assert str(path) in message
    assert "line" in message


def test_expired_signal_file_is_flagged_and_still_loaded() -> None:
    document = load_signals(FIXTURES / "expired.json", now=AS_OF)

    assert len(document.signals) == 1
    signal = document.signals[0]
    assert signal.ticker == "7203.T"
    assert signal.side is Side.SELL
    assert signal.expired is True


def test_expiry_is_strictly_after_expires_at(tmp_path: Path, valid_payload: dict) -> None:
    expires = datetime(2026, 10, 2, 6, 30, tzinfo=HKT)
    valid_payload["signals"][0]["expires_at"] = expires.isoformat()
    path = write_json(tmp_path, "boundary.json", valid_payload)

    at_deadline = load_signals(path, now=expires)
    after_deadline = load_signals(path, now=expires + timedelta(seconds=1))

    assert at_deadline.signals[0].expired is False
    assert after_deadline.signals[0].expired is True


def test_expiry_compares_aware_clocks(tmp_path: Path, valid_payload: dict) -> None:
    path = write_json(tmp_path, "tz.json", valid_payload)
    # 2026-10-02 06:30 +08:00 == 2026-10-01 22:30 UTC.
    before = load_signals(path, now=datetime(2026, 10, 1, 22, 29, tzinfo=UTC))
    after = load_signals(path, now=datetime(2026, 10, 1, 22, 31, tzinfo=UTC))

    assert before.signals[0].expired is False
    assert after.signals[0].expired is True


def test_naive_now_is_rejected(valid_payload: dict, tmp_path: Path) -> None:
    path = write_json(tmp_path, "signals.json", valid_payload)

    with pytest.raises(SignalValidationError, match="timezone-aware"):
        load_signals(path, now=datetime(2026, 10, 1, 12, 0))


@pytest.mark.parametrize(
    ("ticker", "market"),
    [
        ("AAPL", "US"),
        ("7203.T", "JP"),
        ("0883.HK", "HK"),
        ("005930.KS", "KR"),
        ("2330.TW", "TW"),
    ],
)
def test_listed_markets_load_including_taiwan(
    tmp_path: Path,
    valid_payload: dict,
    ticker: str,
    market: str,
) -> None:
    row = valid_payload["signals"][0]
    row["ticker"] = ticker
    row["market"] = market
    row["tradable"] = False
    path = write_json(tmp_path, "markets.json", valid_payload)

    document = load_signals(path, now=AS_OF)

    signal = document.signals[0]
    assert signal.ticker == ticker
    assert signal.market == market


def test_missing_required_field(tmp_path: Path, valid_payload: dict) -> None:
    del valid_payload["signals"][0]["side"]
    path = write_json(tmp_path, "missing.json", valid_payload)

    with pytest.raises(SignalValidationError, match=r"signals\.0\.side") as caught:
        load_signals(path, now=AS_OF)

    assert "Field required" in str(caught.value)
    assert str(path) in str(caught.value)


def test_validation_reports_every_problem(tmp_path: Path, valid_payload: dict) -> None:
    row = valid_payload["signals"][0]
    row["side"] = "SHORT"
    row["confidence"] = 150
    row["expires_at"] = "2026-10-02T06:30:00Z"
    path = write_json(tmp_path, "many.json", valid_payload)

    with pytest.raises(SignalValidationError) as caught:
        load_signals(path, now=AS_OF)

    message = str(caught.value)
    assert "side must be one of BUY, SELL, HOLD" in message
    assert "confidence must be an integer between 0 and 100" in message
    assert "+08:00" in message


@pytest.mark.parametrize("side", ["BUY", "SELL", "HOLD"])
def test_accepted_sides(tmp_path: Path, valid_payload: dict, side: str) -> None:
    valid_payload["signals"][0]["side"] = side
    path = write_json(tmp_path, "side.json", valid_payload)

    document = load_signals(path, now=AS_OF)

    assert document.signals[0].side == side


@pytest.mark.parametrize("side", ["buy", "HOLD ", "SHORT", "", None])
def test_invalid_side(tmp_path: Path, valid_payload: dict, side: object) -> None:
    valid_payload["signals"][0]["side"] = side
    path = write_json(tmp_path, "side.json", valid_payload)

    with pytest.raises(SignalValidationError, match="side must be one of BUY, SELL, HOLD"):
        load_signals(path, now=AS_OF)


@pytest.mark.parametrize("confidence", [101, -1, 72.5, "72", True, False, None])
def test_confidence_must_be_integer_0_to_100(
    tmp_path: Path,
    valid_payload: dict,
    confidence: object,
) -> None:
    valid_payload["signals"][0]["confidence"] = confidence
    path = write_json(tmp_path, "confidence.json", valid_payload)

    with pytest.raises(
        SignalValidationError,
        match="confidence must be an integer between 0 and 100",
    ):
        load_signals(path, now=AS_OF)


@pytest.mark.parametrize("confidence", [0, 100])
def test_confidence_boundaries_are_accepted(
    tmp_path: Path,
    valid_payload: dict,
    confidence: int,
) -> None:
    valid_payload["signals"][0]["confidence"] = confidence
    path = write_json(tmp_path, "confidence.json", valid_payload)

    document = load_signals(path, now=AS_OF)

    assert document.signals[0].confidence == confidence


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-01T06:55:00",
        "2026-10-01T06:55:00Z",
        "2026-10-01T06:55:00+00:00",
        "2026-10-01T06:55:00+0800",
        "2026-10-01T06:55:00+09:00",
        1_759_286_100,
    ],
)
def test_timestamps_require_plus_08_offset(
    tmp_path: Path,
    valid_payload: dict,
    timestamp: object,
) -> None:
    valid_payload["generated_at"] = timestamp
    valid_payload["signals"][0]["expires_at"] = timestamp
    path = write_json(tmp_path, "offset.json", valid_payload)

    with pytest.raises(SignalValidationError, match=r"\+08:00"):
        load_signals(path, now=AS_OF)


def test_unsupported_schema_version(tmp_path: Path, valid_payload: dict) -> None:
    valid_payload["schema_version"] = "2.0"
    path = write_json(tmp_path, "version.json", valid_payload)

    with pytest.raises(SignalValidationError, match="unsupported schema_version"):
        load_signals(path, now=AS_OF)


def test_unknown_fields_are_ignored(tmp_path: Path, valid_payload: dict) -> None:
    valid_payload["signals"][0]["note"] = "upstream may add fields"
    valid_payload["desk"] = "A"
    path = write_json(tmp_path, "extra.json", valid_payload)

    document = load_signals(path, now=AS_OF)

    assert document.signals[0].ticker == "0883.HK"


def test_file_derived_expiry_flag_cannot_hide_an_expired_signal(
    tmp_path: Path,
    valid_payload: dict,
) -> None:
    valid_payload["signals"][0]["expires_at"] = "2020-01-02T08:00:00+08:00"
    valid_payload["signals"][0]["expired"] = False
    path = write_json(tmp_path, "forced.json", valid_payload)

    document = load_signals(path, now=AS_OF)

    assert document.signals[0].expired is True


def test_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "absent.json"

    with pytest.raises(SignalFileError, match="not found") as caught:
        load_signals(path, now=AS_OF)

    assert str(path) in str(caught.value)
    assert not isinstance(caught.value, FileNotFoundError)


def test_directory_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SignalFileError, match="not a file"):
        load_signals(tmp_path, now=AS_OF)


def test_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "signals.json"
    path.write_bytes(b"\xff\xfe not utf-8")

    with pytest.raises(SignalFileError, match="UTF-8"):
        load_signals(path, now=AS_OF)


def test_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "signals.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(SignalValidationError, match="failed validation"):
        load_signals(path, now=AS_OF)


def test_default_path_comes_from_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert get_settings().signals_path == DEFAULT_SIGNALS_PATH
    assert Path("/workspace/invest/signals.json") == DEFAULT_SIGNALS_PATH

    payload = json.loads((FIXTURES / "empty.json").read_text(encoding="utf-8"))
    path = write_json(tmp_path, "elsewhere.json", payload)
    monkeypatch.setenv("SIGNALS_PATH", str(path))
    get_settings.cache_clear()

    document = load_signals(now=AS_OF)

    assert document.signals == []
