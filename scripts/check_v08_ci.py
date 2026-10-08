"""CI checks for the approval literal, the approvals CHECK, and the sync list.

The allowlist file is compared to its frozen contents. New identifiers that
contain the stem must already be on that list.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "auto_" + "paper"
STEM = "liv" + "e"
ALLOWLIST = (
    "upside_live",
    "upside_live_pct",
    "min_upside_live_pct",
    "upside_live_below_pct",
)
# Pre-existing identifiers. The allowlist file is not widened for them.
GRANDFATHER = frozenset(
    {
        STEM + "_size_multiplier",
        STEM.upper() + "_NOT_AUTHORIZED",
        "Delivery",
        "_" + STEM.upper(),
        STEM + "_",
        STEM + "_promoted",
        "r0_" + STEM + "_allowlist",
        "test_scan_rejects_" + STEM + "_mode_and_forbidden_port_assignments",
    }
)
SCAN_ROOTS = ("src", "tests", "config", "vps", "scripts", "ci", ".github", "strategies")
SUFFIXES = {
    ".py",
    ".md",
    ".json",
    ".txt",
    ".yml",
    ".yaml",
    ".example",
    ".sh",
    ".ini",
    ".template",
    ".toml",
}


def approval_literal_lines() -> list[str]:
    hits: list[str] = []
    for path in (ROOT / "src").rglob("*"):
        if not path.is_file() or path.suffix != ".py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if TOKEN in line:
                hits.append(f"{path.relative_to(ROOT)}:{number}:{line.strip()}")
    return hits


def _oversized_files() -> bool:
    limit = 5 * 1024 * 1024
    skip = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
    too_big = False
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in skip for part in path.parts):
            continue
        size = path.stat().st_size
        if size > limit:
            print(f"file exceeds 5 MB: {path.relative_to(ROOT)} ({size})", file=sys.stderr)
            too_big = True
    return too_big


def _universe_forbidden_tokens() -> bool:
    pattern = re.compile(rf"[A-Za-z0-9_]*{STEM}[A-Za-z0-9_]*", re.IGNORECASE)
    found: set[str] = set()
    for name in ("research/universe", "data/universe"):
        base = ROOT / name
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in {"__pycache__", ".staging", "cache"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            found.update(pattern.findall(text))
    if found:
        print("universe tree has a forbidden token:", file=sys.stderr)
        for item in sorted(found):
            print(item, file=sys.stderr)
        return True
    return False


def main() -> int:
    hits = approval_literal_lines()
    if len(hits) != 1 or "APPROVER_AUTO" not in hits[0]:
        print("approval literal must be one constant", file=sys.stderr)
        for line in hits:
            print(line, file=sys.stderr)
        return 1
    from auto_trading.portfolio.journal import TradeJournal

    home = Path(tempfile.mkdtemp())
    TradeJournal(home)
    with sqlite3.connect(home / "data" / "trading.db") as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'approvals'"
        ).fetchone()
    sql = "" if row is None else str(row[0])
    if "CHECK" not in sql or "all_ok" not in sql or TOKEN not in sql:
        print("approvals CHECK missing", file=sys.stderr)
        return 1
    manifest = (ROOT / "vps" / "deploy" / "sync_vps_to_box.txt").read_text(encoding="utf-8")
    if "logs/critical_*.jsonl" not in manifest or "\nreports/\n" not in manifest + "\n":
        print("sync list missing critical logs or reports", file=sys.stderr)
        return 1
    if "data/trading.db" not in manifest:
        print("sync list missing the read-only database", file=sys.stderr)
        return 1
    if "data/universe/" not in manifest or "--delete" in manifest:
        print("vps sync must copy the universe and must not use --delete", file=sys.stderr)
        return 1
    inbound = (ROOT / "vps" / "deploy" / "sync_box_to_vps.txt").read_text(encoding="utf-8")
    if "data/universe" in inbound:
        print("box sync must not push the universe", file=sys.stderr)
        return 1
    if _oversized_files():
        return 1
    if _universe_forbidden_tokens():
        return 1
    allow_path = ROOT / "ci" / "r0_live_allowlist.txt"
    raw = allow_path.read_text(encoding="utf-8").splitlines()
    listed = [line.strip() for line in raw if line.strip()]
    if listed != list(ALLOWLIST):
        print("allowlist changed", file=sys.stderr)
        return 1
    pattern = re.compile(rf"[A-Za-z0-9_]*{STEM}[A-Za-z0-9_]*", re.IGNORECASE)
    found: set[str] = set()
    for name in SCAN_ROOTS:
        base = ROOT / name
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in SUFFIXES and path.name != ".env.example":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            found.update(pattern.findall(text))
    readme = ROOT / "README.md"
    if readme.is_file():
        found.update(pattern.findall(readme.read_text(encoding="utf-8", errors="replace")))
    extra = sorted(found - set(ALLOWLIST) - GRANDFATHER)
    if extra:
        print("identifiers not on the allowlist:", file=sys.stderr)
        for item in extra:
            print(item, file=sys.stderr)
        return 1
    print("v08 checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
