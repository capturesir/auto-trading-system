"""Approved strategy specs are identified by a canonical hash."""

from __future__ import annotations

import pytest

from auto_trading.strategy import (
    ApprovedStrategy,
    StrategyEngine,
    StrategyNotApproved,
    require_approved,
    spec_hash,
)

DOCUMENT = {
    "id": "trend-follow",
    "version": 1,
    "mode": "paper",
    "rules": {"lookback": 20},
}


def _approved() -> ApprovedStrategy:
    return ApprovedStrategy(
        strategy_id="trend-follow",
        version=1,
        spec_hash=spec_hash(DOCUMENT),
    )


def test_hash_ignores_key_order() -> None:
    reordered = {
        "rules": {"lookback": 20},
        "mode": "paper",
        "version": 1,
        "id": "trend-follow",
    }

    assert spec_hash(DOCUMENT) == spec_hash(reordered)


def test_matching_hash_is_approved() -> None:
    assert require_approved(DOCUMENT, _approved()) == spec_hash(DOCUMENT)


def test_mode_is_not_part_of_the_hash() -> None:
    edited = dict(DOCUMENT)
    edited["mode"] = "live"

    assert spec_hash(edited) == spec_hash(DOCUMENT)
    assert require_approved(edited, _approved()) == spec_hash(DOCUMENT)


def test_edited_spec_is_rejected() -> None:
    edited = dict(DOCUMENT)
    edited["rules"] = {"lookback": 21}

    with pytest.raises(StrategyNotApproved, match="does not match"):
        require_approved(edited, _approved())


def test_engine_writes_intents_for_the_approved_hash(tmp_path) -> None:
    wrong = _approved().model_copy(update={"spec_hash": "sha256:" + "0" * 64})
    with pytest.raises(StrategyNotApproved):
        StrategyEngine().run(DOCUMENT, wrong, trading_home=tmp_path, session_date="2026-10-02")
    assert list(tmp_path.rglob("*.json")) == []

    path = StrategyEngine().run(
        DOCUMENT, _approved(), trading_home=tmp_path, session_date="2026-10-02"
    )
    saved = path.read_text(encoding="utf-8")
    assert '"schema_version": "intent/1.0"' in saved
    assert spec_hash(DOCUMENT) in saved
    assert '"intents": []' in saved
