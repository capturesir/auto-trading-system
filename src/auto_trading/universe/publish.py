"""Replace the current pool only after every check has passed.

Archive placement uses ``O_EXCL``. The current CSV is replaced next, and
the meta file last. A failure between those steps restores the previous
bytes and removes an archive this attempt created.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import date
from pathlib import Path

Hook = Callable[[str], None]


class RebuildError(Exception):
    """One rebuild step failed. ``step`` is the detail code."""

    def __init__(self, step: str) -> None:
        super().__init__(step)
        self.step = step


def publish_pool(
    universe: Path,
    staging: Path,
    *,
    today: date,
    hook: Hook | None = None,
) -> list[str]:
    """Copy a verified staging tree into ``universe``. Return the step order."""
    meta = json.loads((staging / "ta_pool.meta.json").read_text(encoding="utf-8"))
    pool_id = str(meta["pool_id"])
    expected = f"ta_pool_{today.year}{today.month:02d}"
    if pool_id != expected:
        raise RebuildError("pool_month")
    _check_count(universe, meta)
    order: list[str] = []
    created: Path | None = None
    csv_prev: bytes | None = None
    meta_prev: bytes | None = None
    csv_replaced = False
    meta_replaced = False
    current_csv = universe / "ta_pool.csv"
    current_meta = universe / "ta_pool.meta.json"
    try:
        created = _place_archive(universe, pool_id, staging)
        order.append("archive")
        if hook is not None:
            hook("archive")
        csv_prev = current_csv.read_bytes() if current_csv.is_file() else None
        _replace_bytes(current_csv, (staging / "ta_pool.csv").read_bytes())
        csv_replaced = True
        order.append("csv")
        if hook is not None:
            hook("csv")
        meta_prev = current_meta.read_bytes() if current_meta.is_file() else None
        _replace_bytes(current_meta, (staging / "ta_pool.meta.json").read_bytes())
        meta_replaced = True
        order.append("meta")
        if hook is not None:
            hook("meta")
    except Exception:
        _rollback(
            created,
            current_csv,
            csv_prev,
            csv_replaced,
            current_meta,
            meta_prev,
            meta_replaced,
        )
        raise
    return order


def _check_count(universe: Path, meta: dict[str, object]) -> None:
    previous = universe / "ta_pool.meta.json"
    if not previous.is_file():
        return
    old = json.loads(previous.read_text(encoding="utf-8"))
    old_total = int(old["counts"]["base"]["total"])  # type: ignore[index]
    new_total = int(meta["counts"]["base"]["total"])  # type: ignore[index]
    if old_total and new_total < old_total * 0.9:
        raise RebuildError("constituent_count")


def _place_archive(universe: Path, pool_id: str, staging: Path) -> Path | None:
    data = (staging / "archive" / f"{pool_id}.csv").read_bytes()
    folder = universe / "archive"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{pool_id}.csv"
    if dest.exists():
        if dest.read_bytes() != data:
            raise RebuildError("archive_conflict")
        return None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(dest, flags, 0o444)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.chmod(dest, 0o444)
    return dest


def _replace_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, target)


def _rollback(
    created: Path | None,
    current_csv: Path,
    csv_prev: bytes | None,
    csv_replaced: bool,
    current_meta: Path,
    meta_prev: bytes | None,
    meta_replaced: bool,
) -> None:
    if meta_replaced:
        _restore(current_meta, meta_prev)
    if csv_replaced:
        _restore(current_csv, csv_prev)
    if created is not None and created.exists():
        created.unlink()


def _restore(target: Path, previous: bytes | None) -> None:
    if previous is None:
        if target.exists():
            target.unlink()
        return
    _replace_bytes(target, previous)
