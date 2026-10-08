"""Approved strategy files and the R25 file hash."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from auto_trading.jsonutil import loads_strict
from auto_trading.strategy.registry import latest, read_events
from auto_trading.strategy.spec import spec_hash

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
REPO_STRATEGIES = REPO_ROOT / "strategies"
PIN_FILE = REPO_ROOT / "config" / "confirmed_strategies.json"
# Rule rows are not tradable strategies. CI refuses any other exclusion.
EXCLUDED_RULES = frozenset({"adr_premium_veto"})


def load_confirmed_spec_hashes(path: Path | None = None) -> dict[tuple[str, int], str]:
    """Pins from the git-deployed file. A registry row alone is not a pin."""
    target = path or PIN_FILE
    raw = target.read_bytes()
    log.info("confirmed_strategies sha256=%s", hashlib.sha256(raw).hexdigest())
    document = loads_strict(raw.decode("utf-8"))
    rows = document.get("strategies")
    if not isinstance(rows, list) or not rows:
        raise ValueError("confirmed_strategies.json has no strategies")
    pins: dict[tuple[str, int], str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("confirmed strategy row is not an object")
        strategy_id = str(row.get("strategy_id") or "")
        digest = str(row.get("spec_hash") or "")
        version = int(row["version"])
        if not strategy_id or not digest.startswith("sha256:"):
            raise ValueError("confirmed strategy row is incomplete")
        pins[(strategy_id, version)] = digest
    return pins


PIN_RELATIVE = "config/confirmed_strategies.json"


def _load_pins() -> tuple[dict[tuple[str, int], str], str | None]:
    """Read the pin file. A missing or broken file is an error, not a crash."""
    try:
        return load_confirmed_spec_hashes(), None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        log.error("confirmed_strategies unreadable: %s", type(exc).__name__)
        return {}, type(exc).__name__


def refresh_confirmed_pins() -> str | None:
    """Reload the git pin. Returns the error type, or None when the file is usable."""
    global CONFIRMED_SPEC_HASHES, PIN_LOAD_ERROR
    pins, error = _load_pins()
    CONFIRMED_SPEC_HASHES = pins
    PIN_LOAD_ERROR = error
    return error


def sync_row_carries_pin(row: str) -> bool:
    """True when an rsync line would copy ``confirmed_strategies.json``.

    A parent directory such as ``config`` or ``config/`` carries the file.
    A sibling such as ``config/adr_map.csv`` does not.
    """
    text = row.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.strip("/")
    if not text or text in {".", "*"}:
        return True
    if "confirmed_strategies.json" in text:
        return True
    return text == PIN_RELATIVE or PIN_RELATIVE.startswith(text + "/")


# Loaded from config/confirmed_strategies.json. A bad file does not crash import.
CONFIRMED_SPEC_HASHES, PIN_LOAD_ERROR = _load_pins()


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

    The file hash must equal the intent hash, an ``approved`` registry row, and
    ``config/confirmed_strategies.json``. An opening order whose strategy is
    not in that file is ``R25`` even when the registry has an approved row.
    A missing file or a missing intent hash rejects an opening order.
    """
    if not strategy_id or version is None or not spec_hash_value:
        return "R25" if opening else None
    pinned = CONFIRMED_SPEC_HASHES.get((strategy_id, version))
    if opening and pinned is None:
        return "R25"
    path = file_for(strategy_id, version, root)
    if path is None:
        return "R25" if opening else None
    try:
        digest = spec_hash(load_approved(path))
    except (OSError, ValueError, TypeError):
        return "R25" if opening else None
    if digest != spec_hash_value:
        return "R25"
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


def _manifest_rows(path: Path) -> list[str]:
    if not path.is_file():
        return []
    rows: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            rows.append(line)
    return rows


def user_approved_hashes(root: Path | None = None) -> dict[tuple[str, int], str]:
    """``event=approved`` and ``actor=user``, excluding rule ids such as the veto."""
    path = registry_path(root)
    found: dict[tuple[str, int], str] = {}
    for row in read_events(path):
        if row.get("event") != "approved" or row.get("actor") != "user":
            continue
        strategy_id = str(row.get("strategy_id") or "")
        if strategy_id in EXCLUDED_RULES:
            continue
        digest = row.get("spec_hash")
        version = row.get("version")
        if not strategy_id or not isinstance(digest, str) or not isinstance(version, int):
            continue
        found[(strategy_id, version)] = digest
    return found


def approved_file_hashes(root: Path | None = None) -> dict[tuple[str, int], str]:
    """§3.4 hash of every file in ``strategies/approved``."""
    found: dict[tuple[str, int], str] = {}
    folder = approved_dir(root)
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("*.json")):
        document = load_approved(path)
        strategy_id = str(document.get("id") or "")
        version = document.get("version")
        if not strategy_id or not isinstance(version, int):
            raise ValueError(f"approved file {path.name} has no id or version")
        found[(strategy_id, int(version))] = spec_hash(document)
    return found


def confirmed_source_errors(root: Path | None = None) -> list[str]:
    """CI: the pin file, user-approved registry rows, and approved files agree.

    ``adr_premium_veto`` is the only registry row that is not a tradable
    strategy. The pin file must not appear on either sync list.
    """
    base = root or REPO_ROOT
    errors: list[str] = []
    pin_path = base / "config" / "confirmed_strategies.json"
    try:
        document = loads_strict(pin_path.read_text(encoding="utf-8"))
        pins = load_confirmed_spec_hashes(pin_path)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return [f"confirmed_strategies.json: {exc}"]
    excluded = document.get("excluded_rules")
    if not isinstance(excluded, list) or set(excluded) != EXCLUDED_RULES:
        errors.append("excluded_rules must be adr_premium_veto only")
    try:
        files = approved_file_hashes(base / "strategies")
    except (OSError, ValueError, TypeError) as exc:
        errors.append(f"approved files: {exc}")
        files = {}
    registry = user_approved_hashes(base / "strategies")
    if set(pins) != set(files) or set(pins) != set(registry):
        errors.append("pin, registry, and approved files are not the same set")
    for key, digest in pins.items():
        if files.get(key) != digest or registry.get(key) != digest:
            errors.append(f"{key[0]} v{key[1]} hash mismatch")
    for name in ("sync_box_to_vps.txt", "sync_vps_to_box.txt"):
        rows = _manifest_rows(base / "vps" / "deploy" / name)
        if any(sync_row_carries_pin(row) for row in rows):
            errors.append(f"{name} must not sync the pin file or config/")
    return errors
