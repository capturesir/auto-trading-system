"""Approved strategy files and the R25 file hash."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from auto_trading.jsonutil import loads_strict
from auto_trading.strategy.registry import latest
from auto_trading.strategy.spec import spec_hash

REPO_STRATEGIES = Path(__file__).resolve().parents[3] / "strategies"


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
) -> str | None:
    """Recompute the approved file. A changed byte rejects the intent."""
    if not strategy_id or version is None or not spec_hash_value:
        return None
    path = file_for(strategy_id, version, root)
    if path is None:
        return None
    digest = spec_hash(load_approved(path))
    if digest != spec_hash_value:
        return "R25"
    mode = registry_mode(strategy_id, version, root)
    if mode is None:
        return None
    if mode != "paper":
        return "LIVE_NOT_AUTHORIZED"
    return None


def startup_lines(root: Path | None = None) -> list[str]:
    return [f"{row['strategy_id']} v{row['version']} {row['hash12']}" for row in roster(root)]


def dump_strategy(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"
