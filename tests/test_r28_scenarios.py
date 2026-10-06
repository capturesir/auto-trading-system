"""49 R28 scenarios. Exits and stops are never paused. Missing state fails closed."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from auto_trading.risk.adr_premium import r28_result

ROOT = Path(__file__).resolve().parents[1]
_RULE = ROOT / "config" / "adr_premium_rule_expected.json"
_STATE = ROOT / "tests" / "fixtures" / "adr_premium_state.json"
EXPECTED = json.loads(_RULE.read_text(encoding="utf-8"))
PAUSED_FILE = json.loads(_STATE.read_text(encoding="utf-8"))
CLEAR = {"paused": []}
PAUSED = {"paused": ["TSM"]}
REASON = "ADR_PREMIUM_PAUSED"


def _case(name: str, expected: str | None, **overrides: object) -> tuple[str, dict, str | None]:
    payload: dict = {
        "mode": "paper",
        "side": "BUY",
        "intent_type": "ENTRY",
        "symbol": "TSM",
        "protective": False,
        "reduce_only": False,
        "state": PAUSED,
        "map_symbols": ["TSM"],
        "last_db": (),
        "rule": EXPECTED,
        "expected_rule": EXPECTED,
    }
    payload.update(overrides)
    return name, payload, expected


SCENARIOS = [
    _case("01_exit_sell", None, side="SELL", intent_type="EXIT"),
    _case("02_reduce_sell", None, side="SELL", intent_type="REDUCE"),
    _case("03_protective_sell", None, side="SELL", intent_type="EXIT", protective=True),
    _case("04_protective_buy", None, protective=True),
    _case("05_reduce_only_buy", None, reduce_only=True),
    _case("06_sell_add", None, side="SELL", intent_type="ADD"),
    _case("07_exit_missing_state", None, side="SELL", intent_type="EXIT", state=None),
    _case("08_exit_exception", None, side="SELL", intent_type="EXIT", state=RuntimeError("x")),
    _case("09_exit_empty_map", None, side="SELL", intent_type="EXIT", map_symbols=[]),
    _case("10_exit_bad_rule", None, side="SELL", intent_type="EXIT", rule={"rule_id": "nope"}),
    _case("11_reduce_other_symbol", None, side="SELL", intent_type="REDUCE", symbol="AAPL"),
    _case("12_sell_entry", None, side="SELL", intent_type="ENTRY"),
    _case("13_buy_entry_paused", REASON),
    _case("14_buy_add_paused", REASON, intent_type="ADD"),
    _case("15_buy_missing_state", REASON, state=None),
    _case("16_buy_add_missing_state", REASON, intent_type="ADD", state=None),
    _case("17_buy_exception_state", REASON, state=RuntimeError("boom")),
    _case("18_hash_mismatch", REASON, rule={**EXPECTED, "rule_hash": "deadbeef"}),
    _case("19_version_mismatch", REASON, rule={**EXPECTED, "rule_version": "1999-01-01"}),
    _case("20_id_mismatch", REASON, rule={**EXPECTED, "rule_id": "OTHER"}),
    _case("21_map_missing_builtin", REASON, map_symbols=None, state=None),
    _case(
        "22_last_db_baba_paused",
        REASON,
        symbol="BABA",
        map_symbols=None,
        last_db=frozenset({"BABA"}),
        state={"paused": ["BABA"]},
    ),
    _case("23_empty_map_keeps_tsm", REASON, map_symbols=[], state=None),
    _case("24_rule_file_absent", REASON, rule=None, state=CLEAR),
    _case("25_buy_clear", None, state=CLEAR),
    _case("26_buy_add_clear", None, intent_type="ADD", state=CLEAR),
    _case("27_extra_rule_fields", None, rule={**EXPECTED, "note": "ignored"}, state=CLEAR),
    _case("28_aapl_not_controlled", None, symbol="AAPL"),
    _case("29_aapl_missing_state", None, symbol="AAPL", state=None),
    _case(
        "30_valid_map_ignores_old_db",
        None,
        symbol="BABA",
        map_symbols=["TSM"],
        last_db=frozenset({"BABA"}),
        state={"paused": ["BABA"]},
    ),
    _case("31_valid_map_clear", None, state=CLEAR),
    _case("32_add_clear_again", None, intent_type="ADD", state=CLEAR),
    _case(
        "33_fallback_baba_clear",
        None,
        symbol="BABA",
        map_symbols=None,
        last_db=frozenset({"BABA"}),
        state=CLEAR,
    ),
    _case("34_msft_outside_fallback", None, symbol="MSFT", map_symbols=None, state=None),
    _case("35_empty_map_paused_list", REASON, map_symbols=[], state=PAUSED),
    _case(
        "36_valid_map_omits_tsm",
        None,
        map_symbols=["BABA"],
        state=PAUSED,
    ),
    _case(
        "37_valid_map_baba",
        REASON,
        symbol="BABA",
        map_symbols=["BABA"],
        state={"paused": ["BABA"]},
    ),
    _case("38_runtime_error_state", REASON, state=RuntimeError("disk")),
    _case("39_fallback_keeps_tsm", REASON, map_symbols=None, last_db=(), state=PAUSED),
    _case(
        "40_union_still_watches_tsm",
        REASON,
        map_symbols=None,
        last_db=frozenset({"BABA"}),
        state=PAUSED,
    ),
    _case("41_non_paper_paused", None, mode="disabled"),
    _case("42_non_paper_missing", None, mode="disabled", state=None),
    _case("43_paper_clear", None, state=CLEAR),
    _case("44_symbol_case", None, symbol="tsm"),
    _case("45_paused_empty", None, state=CLEAR),
    _case("46_aapl_paused_but_unwatched", None, symbol="AAPL", state={"paused": ["AAPL", "TSM"]}),
    _case("47_protective_add", None, intent_type="ADD", protective=True),
    _case("48_reduce_only_exit", None, side="SELL", intent_type="EXIT", reduce_only=True),
    _case("49_fixture_file_pauses_tsm", REASON, state=PAUSED_FILE),
]


def test_there_are_49_scenarios() -> None:
    assert len(SCENARIOS) == 49
    assert len({name for name, _payload, _expected in SCENARIOS}) == 49


@pytest.mark.parametrize(
    ("name", "payload", "expected"),
    SCENARIOS,
    ids=[row[0] for row in SCENARIOS],
)
def test_r28_scenario(name: str, payload: dict, expected: str | None) -> None:
    assert r28_result(**payload) == expected
    assert name


def test_r28_has_no_toggle_and_rule_compare_ignores_other_fields() -> None:
    parameters = inspect.signature(r28_result).parameters
    assert "shadow" not in parameters
    assert "enabled" not in parameters
    assert EXPECTED["rule_hash"].startswith("0c621402b71b")
    assert EXPECTED["rule_id"] == "ADR_PREMIUM_PAUSED"


def test_controlled_set_is_never_empty() -> None:
    from auto_trading.risk.adr_premium import controlled_symbols

    assert controlled_symbols(None, ()) == frozenset({"TSM"})
    assert "TSM" in controlled_symbols([], {"BABA"})
    assert controlled_symbols(["BABA"], ()) == frozenset({"BABA"})
