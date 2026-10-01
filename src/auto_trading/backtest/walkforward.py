"""Walk-forward windows and the one-shot final out-of-sample cut.

Proxy research uses three training years, one test year, stepped forward
one year. The last 20% of a sample is the final out-of-sample slice, and
each strategy version may record that purpose once.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from auto_trading.strategy.spec import INTERPRETER_VERSION

FIELDS = (
    "trial_id",
    "ts",
    "strategy_id",
    "version",
    "spec_hash",
    "params_json",
    "purpose",
    "data_snapshot_hash",
    "period_train",
    "period_test",
    "sharpe_oos",
    "cagr_oos",
    "mdd_oos",
    "n_trades_oos",
    "run_dir",
    "interpreter_version",
    "note",
)


class TrialRejected(Exception):
    """A strategy version tried to spend its final out-of-sample slice twice."""


@dataclass(frozen=True)
class Window:
    train_start: date
    train_end: date
    test_start: date
    test_end: date


def _add_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def walk_forward_windows(
    start: date,
    end: date,
    *,
    train_years: int = 3,
    test_years: int = 1,
    step_years: int = 1,
) -> list[Window]:
    """Training / test pairs that finish on or before ``end``."""
    windows: list[Window] = []
    cursor = start
    while True:
        train_end = _add_years(cursor, train_years) - timedelta(days=1)
        test_start = train_end + timedelta(days=1)
        test_end = _add_years(test_start, test_years) - timedelta(days=1)
        if test_end > end:
            break
        windows.append(Window(cursor, train_end, test_start, test_end))
        cursor = _add_years(cursor, step_years)
    return windows


def final_oos_start(start: date, end: date, *, fraction: float = 0.2) -> date:
    """First day of the last ``fraction`` of the calendar span."""
    span = (end - start).days
    return start + timedelta(days=int(span * (1 - fraction)))


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def append_trial(path: Path, row: dict[str, object]) -> None:
    """Append one trial. A second ``final_oos`` row for the same version is refused."""
    purpose = str(row.get("purpose") or "")
    if purpose == "final_oos":
        for previous in _read_rows(path):
            same_id = previous.get("strategy_id") == str(row.get("strategy_id"))
            same_version = previous.get("version") == str(row.get("version"))
            if same_id and same_version and previous.get("purpose") == "final_oos":
                raise TrialRejected(
                    f"final_oos already recorded for {row.get('strategy_id')} v{row.get('version')}"
                )
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.is_file()
    payload = {field: row.get(field, "") for field in FIELDS}
    payload["interpreter_version"] = payload["interpreter_version"] or INTERPRETER_VERSION
    with path.open("a", encoding="utf-8", newline="") as handle:
        if fresh:
            handle.write("\ufeff")
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if fresh:
            writer.writeheader()
        writer.writerow(payload)
