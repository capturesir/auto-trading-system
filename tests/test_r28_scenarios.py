"""The 49 R28 scenarios from the reference guard, with the same expected results."""

from __future__ import annotations

import csv
import inspect
import json
import tempfile
from pathlib import Path

import pytest

from auto_trading.risk import adr_state as state
from auto_trading.risk.adr_premium import PAUSED, check_buy
from auto_trading.risk.adr_state import validate_before_write

ROOT = Path(__file__).resolve().parents[1]
AMAP = ROOT / "config" / "adr_map.csv"
EXPECTED_PATH = ROOT / "config" / "adr_premium_rule_expected.json"
FIXTURE = ROOT / "tests" / "fixtures" / "adr_premium_state.json"
FULL_HASH = "sha256:0c621402b71b5dd903c25aa65c5787369376c0301bce1789e731ebc589b24ba4"
T = "2026-10-06T20:00:00+08:00"
OK = (True, None, None)


def _tmp(text: str, suffix: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        name = handle.name
    Path(name).write_text(text, encoding="utf-8")
    return name


def _paused(detail: str) -> tuple[bool, str, str]:
    return (False, PAUSED, detail)


def _set(**fields: object):
    def mutate(document: dict) -> None:
        document["adrs"]["TSM"].update(**fields)

    return mutate


def _scenarios() -> list[tuple[str, tuple, tuple]]:
    base = json.loads(FIXTURE.read_text(encoding="utf-8"))
    amap = str(AMAP)
    exp = str(EXPECTED_PATH)
    map_rows = list(csv.DictReader(AMAP.open(encoding="utf-8")))
    columns = list(map_rows[0].keys())

    def run(mut, now, ticker="TSM", side="BUY", amap_=amap, exp_=exp):
        document = json.loads(json.dumps(base))
        mut(document)
        return check_buy(ticker, side, now, _tmp(json.dumps(document), ".json"), amap_, exp_)

    def map_with(rows: list[dict]) -> str:
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as handle:
            path = Path(handle.name)
        with path.open("w", encoding="utf-8", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return str(path)

    def exp_with(**fields: object) -> str:
        document = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
        document.update(fields)
        return _tmp(json.dumps(document), ".json")

    cases: list[tuple[str, tuple, tuple]] = []

    def add(name: str, got: tuple, expected: tuple) -> None:
        cases.append((name, got[:3], expected))

    original = [
        ("active，S 相同", lambda d: None, T, OK),
        ("盤中（澳門 23:00）S 仍是 10-06", lambda d: None, "2026-10-06T23:00:00+08:00", OK),
        (
            "美東收市後 S=10-07（用 10-06 檔）→ stale",
            lambda d: None,
            "2026-10-07T04:01:00+08:00",
            _paused("stale"),
        ),
        ("valid = S−1 → stale", _set(valid_for_us_session="2026-10-05"), T, _paused("stale")),
        ("pair_date 錯（10-02）→ stale", _set(pair_date="2026-10-02"), T, _paused("stale")),
        (
            "10/9 台股休市：S=10-12、pair 10-08、沿用 1",
            _set(
                valid_for_us_session="2026-10-12",
                pair_date="2026-10-08",
                carried_forward_sessions=1,
            ),
            "2026-10-10T07:05:00+08:00",
            OK,
        ),
        (
            "10/9 台股休市：pair 寫 10-09 → stale",
            _set(valid_for_us_session="2026-10-12", pair_date="2026-10-09"),
            "2026-10-10T07:05:00+08:00",
            _paused("stale"),
        ),
        ("paused", _set(state="paused"), T, _paused("paused")),
        (
            "unknown（數值為 null）",
            _set(
                state="unknown",
                premium_pct=None,
                premium_5d_pct=None,
                percentile_250d=None,
                zscore_250d=None,
            ),
            T,
            _paused("unknown"),
        ),
        ("active 但數值為 null → missing", _set(premium_pct=None), T, _paused("missing")),
        ("ratio 與 adr_map 不符 → missing", _set(ratio=4), T, _paused("missing")),
        ("local_ticker 不符 → missing", _set(local_ticker="2317.TW"), T, _paused("missing")),
        (
            "rule_hash 與 expected 不符 → missing",
            lambda d: d.update(rule_hash="sha256:" + "0" * 64),
            T,
            _paused("missing"),
        ),
        (
            "generated_at 無時區 → missing",
            lambda d: d.update(generated_at="2026-10-06T07:05:00"),
            T,
            _paused("missing"),
        ),
        (
            "缺 carried_forward_sessions → missing",
            lambda d: d["adrs"]["TSM"].pop("carried_forward_sessions"),
            T,
            _paused("missing"),
        ),
        ("TSM 不在檔內 → missing", lambda d: d["adrs"].pop("TSM"), T, _paused("missing")),
        (
            "schema 不符 → missing",
            lambda d: d.update(schema_version="adr_premium_state/0.9"),
            T,
            _paused("missing"),
        ),
    ]
    for name, mut, now, expected in original:
        add(name, run(mut, now), expected)
    add("SELL 不受影響（paused）", run(_set(state="paused"), T, side="SELL"), OK)
    add("非受控標的（AAPL）", run(lambda d: None, T, ticker="AAPL"), OK)
    add(
        "狀態檔缺失 → missing",
        check_buy("TSM", "BUY", T, "/nonexistent.json", amap, exp),
        _paused("missing"),
    )

    overlap = dict(map_rows[0])
    overlap["effective_from"] = "2020-01-01"
    overlap["note"] = "重疊測試"
    bad_ratio = dict(map_rows[0])
    bad_ratio["ratio"] = "abc"
    newer = [
        (
            "a adr_map.csv 缺失：TSM（內置清單）仍擋",
            run(lambda d: None, T, amap_="/nonexistent.csv"),
            _paused("missing"),
        ),
        (
            "a adr_map.csv 無法解析（空檔）：TSM 仍擋",
            run(lambda d: None, T, amap_=_tmp("", ".csv")),
            _paused("missing"),
        ),
        (
            "a adr_map.csv 缺欄：TSM 仍擋",
            run(lambda d: None, T, amap_=_tmp("foo,bar\n1,2\n", ".csv")),
            _paused("missing"),
        ),
        (
            "a adr_map 與狀態檔都缺失：TSM 仍擋",
            check_buy("TSM", "BUY", T, "/nonexistent.json", "/nonexistent.csv", exp),
            _paused("missing"),
        ),
        (
            "a adr_map 缺失時 SELL 放行",
            run(lambda d: None, T, side="SELL", amap_="/nonexistent.csv"),
            OK,
        ),
        (
            "b rule_version = true → missing",
            run(lambda d: d.update(rule_version=True), T),
            _paused("missing"),
        ),
        (
            'b rule_version = "1" → missing',
            run(lambda d: d.update(rule_version="1"), T),
            _paused("missing"),
        ),
        (
            "b expected 檔 rule_version = true → missing",
            run(lambda d: d.update(rule_version=True), T, exp_=exp_with(rule_version=True)),
            _paused("missing"),
        ),
        (
            "b carried_forward_sessions = 4 → missing",
            run(_set(carried_forward_sessions=4), T),
            _paused("missing"),
        ),
        (
            'b carried_forward_sessions = "1" → missing',
            run(_set(carried_forward_sessions="1"), T),
            _paused("missing"),
        ),
        (
            "b carried_forward_sessions = true → missing",
            run(_set(carried_forward_sessions=True), T),
            _paused("missing"),
        ),
        (
            "b carried_forward_sessions = −1 → missing",
            run(_set(carried_forward_sessions=-1), T),
            _paused("missing"),
        ),
        ('b ratio = "5" → missing', run(_set(ratio="5"), T), _paused("missing")),
        ("b ratio = true → missing", run(_set(ratio=True), T), _paused("missing")),
        ("b ratio = 5.0 → 通過（數值比較）", run(_set(ratio=5.0), T), OK),
        (
            "b percentile_250d = true → missing",
            run(_set(percentile_250d=True), T),
            _paused("missing"),
        ),
        (
            'b consecutive_uncomputable = "0" → missing',
            run(_set(consecutive_uncomputable="0"), T),
            _paused("missing"),
        ),
        (
            "b pair_date = 2026-02-30（不存在）→ missing",
            run(_set(pair_date="2026-02-30"), T),
            _paused("missing"),
        ),
        ("b reason 前綴不合法 → missing", run(_set(reason="p 62"), T), _paused("missing")),
        (
            "c adr_map 同一 ADR 區間重疊 → missing",
            run(lambda d: None, T, amap_=map_with(map_rows + [overlap])),
            _paused("missing"),
        ),
        (
            "c adr_map 區間不重疊（舊行有 effective_to）→ 通過",
            run(
                lambda d: None,
                T,
                amap_=map_with(
                    [
                        dict(map_rows[0], effective_to="2019-12-31", ratio="4"),
                        dict(overlap, note="新行"),
                    ]
                ),
            ),
            OK,
        ),
        (
            'd adr_map ratio = "abc" → missing',
            run(lambda d: None, T, amap_=map_with([bad_ratio])),
            _paused("missing"),
        ),
        (
            "d 日曆超出範圍（2029-03-01）→ missing",
            run(
                _set(valid_for_us_session="2029-03-01", pair_date="2029-02-28"),
                "2029-03-01T20:00:00+08:00",
            ),
            _paused("missing"),
        ),
        (
            "d adr_map local_calendar 不認得 → missing",
            run(lambda d: None, T, amap_=map_with([dict(map_rows[0], local_calendar="XXXX")])),
            _paused("missing"),
        ),
        ("d now 無法解析 → missing", run(lambda d: None, "not-a-time"), _paused("missing")),
        (
            "— approval_status 改 approved 不改變判斷（paused 仍擋）",
            run(_set(state="paused"), T, exp_=exp_with(approval_status="approved")),
            _paused("paused"),
        ),
    ]
    for name, got, expected in newer:
        add(name, got, expected)

    class Skew:
        def __init__(self, calendar: object) -> None:
            self.c = calendar

        def __getattr__(self, key: str):
            return getattr(self.c, key)

        def date_to_session(self, day, direction="previous"):
            found = self.c.date_to_session(day, direction=direction)
            return self.c.previous_session(found)

    real = state.get_cal("XTAI")
    state._CALS["XTAI"] = Skew(real)
    try:
        add(
            "d 兩個日曆算出不同的 expected_pair_date → missing",
            run(lambda d: None, T),
            _paused("missing"),
        )
    finally:
        state._CALS["XTAI"] = real

    good = json.loads(json.dumps(base))
    bad = json.loads(json.dumps(base))
    bad["adrs"]["TSM"]["carried_forward_sessions"] = "1"
    add(
        "寫入端：合法狀態檔通過驗證",
        (validate_before_write(good, amap, exp) is None, None, None),
        (True, None, None),
    )
    add(
        '寫入端：carried "1" 被攔下',
        (validate_before_write(bad, amap, exp) is None, None, None),
        (False, None, None),
    )
    return cases


SCENARIOS = _scenarios()


def test_there_are_49_scenarios() -> None:
    assert len(SCENARIOS) == 49
    assert len({name for name, _got, _expected in SCENARIOS}) == 49


@pytest.mark.parametrize(
    ("name", "got", "expected"),
    SCENARIOS,
    ids=[row[0] for row in SCENARIOS],
)
def test_r28_scenario(name: str, got: tuple, expected: tuple) -> None:
    assert got == expected, name


def test_rule_hash_is_the_full_digest_and_there_is_no_toggle() -> None:
    document = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    assert document["rule_id"] == "adr_premium_veto"
    assert document["rule_version"] == 1
    assert document["rule_hash"] == FULL_HASH
    parameters = inspect.signature(check_buy).parameters
    assert "shadow" not in parameters
    assert "enabled" not in parameters
