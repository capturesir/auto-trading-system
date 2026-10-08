"""Approved strategy files and the R25 file hash."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from auto_trading.jsonutil import loads_strict
from auto_trading.strategy.registry import latest
from auto_trading.strategy.spec import spec_hash

REPO_STRATEGIES = Path(__file__).resolve().parents[3] / "strategies"

# User-confirmed file hashes. R25 compares the bytes on disk with these
# values and with the approved registry row. A registry line alone is not enough.
CONFIRMED_SPEC_HASHES: dict[tuple[str, int], str] = {
    ("sig_trend", 3): "sha256:9877996947f027efe89aa0991b23e93ac3e409c38660531109a2a5ce4404a334",
    ("sig_pullback", 3): "sha256:d2449671d3124d5c2da214e2a71c999e17641a99fb7df9dbf7cb80549c53f231",
    ("ta_breakout52", 1): "sha256:86046b4ceb98bb010e28fe219fac2fc72c5256d222e6f782d62f1f3b79066101",
}


def approved_dir(root: Path | None = None) -> Path:
    return (root or REPO_STRATEGIES) / "approved"


def registry_path(root: Path | None = None) -> Path:
    return (root or REPO_STRATEGIES) / "registry.jsonl"


def load_approved(path: Path) -> dict[str, Any]:
    return loads_strict(path.read_text(encoding="utf-8"))


def file_for(strategy_id: str, version: int, root: Path | None = None) -> Path | None:
    path = approved_dir(root) / f"{strategy_id}.v{version}.json"
    if path.is_file():
        return path
    return None


def roster(root: Path | None = None) -> list[dict[str, str]]:
    """Startup list: id, version, and the first 12 hex digits of the file hash."""
    rows: list[dict[str, str]] = []
    folder = approved_dir(root)
    if not folder.is_dir():
        return rows
    for path in sorted(folder.glob("*.json")):
        document = load_approved(path)
        digest = spec_hash(document)
        rows.append(
            {
                "strategy_id": str(document.get("id")),
                "version": str(document.get("version")),
                "hash12": digest.removeprefix("sha256:")[:12],
                "spec_hash": digest,
                "approval": str((document.get("execution") or {}).get("approval")),
            }
        )
    return rows


def approved_row_hash(strategy_id: str, version: int, root: Path | None = None) -> str | None:
    """Hash recorded on an ``approved`` registry row. Later self-reports do not count."""
    path = registry_path(root)
    if not path.is_file():
        return None
    from auto_trading.strategy.registry import read_events

    found: str | None = None
    for row in read_events(path):
        if row.get("strategy_id") != strategy_id or row.get("version") != version:
            continue
        if row.get("event") != "approved":
            continue
        digest = row.get("spec_hash")
        if isinstance(digest, str) and digest:
            found = digest
    return found


def registry_mode(strategy_id: str, version: int, root: Path | None = None) -> str | None:
    path = registry_path(root)
    if not path.is_file():
        return None
    row = latest(path, strategy_id, version)
    if row is None:
        return None
    mode = row.get("mode_after")
    return str(mode) if mode is not None else None


def r25_code(
    *,
    strategy_id: str | None,
    version: int | None,
    spec_hash_value: str | None,
    root: Path | None = None,
    opening: bool = False,
) -> str | None:
    """Recompute the file with the §3.4 hash and compare three values.

    The file hash must equal the intent hash and an ``approved`` registry row.
    The three confirmed strategies must also equal the pinned digests. A missing
    file, a missing intent hash, or a mismatch rejects an opening order.
    """
    if not strategy_id or version is None or not spec_hash_value:
        return "R25" if opening else None
    path = file_for(strategy_id, version, root)
    if path is None:
        return "R25" if opening else None
    try:
        digest = spec_hash(load_approved(path))
    except (OSError, ValueError, TypeError):
        return "R25" if opening else None
    if digest != spec_hash_value:
        return "R25"
    pinned = CONFIRMED_SPEC_HASHES.get((strategy_id, version))
    if pinned is not None and digest != pinned:
        return "R25"
    row_hash = approved_row_hash(strategy_id, version, root)
    if row_hash is None:
        return "R25" if opening else None
    if row_hash != digest:
        return "R25"
    mode = registry_mode(strategy_id, version, root)
    if mode is None:
        return "R25" if opening else None
    if mode != "paper":
        return "LIVE_NOT_AUTHORIZED"
    return None


def confirmed_mismatches(root: Path | None = None) -> list[str]:
    """Strategies whose on-disk hash12 does not match the confirmed pin."""
    bad: list[str] = []
    for (strategy_id, version), pinned in CONFIRMED_SPEC_HASHES.items():
        path = file_for(strategy_id, version, root)
        label = f"{strategy_id} v{version}"
        if path is None:
            bad.append(label)
            continue
        try:
            digest = spec_hash(load_approved(path))
        except (OSError, ValueError, TypeError):
            bad.append(label)
            continue
        row_hash = approved_row_hash(strategy_id, version, root)
        if digest != pinned or row_hash != pinned:
            bad.append(label)
    return bad


def startup_lines(root: Path | None = None) -> list[str]:
    return [f"{row['strategy_id']} v{row['version']} {row['hash12']}" for row in roster(root)]


def dump_strategy(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"
