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


def test_edited_spec_is_rejected() -> None:
    edited = dict(DOCUMENT)
    edited["mode"] = "live"

    with pytest.raises(StrategyNotApproved, match="does not match"):
        require_approved(edited, _approved())


def test_engine_refuses_unapproved_spec_and_does_not_trade() -> None:
    with pytest.raises(NotImplementedError, match="strategy execution"):
        StrategyEngine().run(DOCUMENT, _approved())

    wrong = _approved().model_copy(update={"spec_hash": "0" * 64})
    with pytest.raises(StrategyNotApproved):
        StrategyEngine().run(DOCUMENT, wrong)
