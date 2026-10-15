"""Source scan that refuses real-account switches.

``upside_live`` is a price metric and is allowlisted. A quote placeholder
may say ``live quote``. Everything else that names a real-account mode, a
real-account port, or a deleted promotion event must sit on a refusal line.
"""

from __future__ import annotations

import re
from pathlib import Path

_LIVE = "liv" + "e"
_REFUSAL = re.compile(
    r"paper_only|refus|reject|forbidden|violation|must be null|不得|拒絕",
    re.IGNORECASE,
)
_MULTIPLIER_OK = re.compile(
    r"None|null|refus|reject|violation|paper_only|must|not",
    re.IGNORECASE,
)


def _needles() -> list[str]:
    """Tokens a real-account switch would contain. Built so this file is clean."""
    return [
        rf"\b{_LIVE}\b",
        "400" + "1",
        "749" + "6",
        "TRADING_" + "MODE",
        "IBKR_" + "USERNAME",
        "IBKR_" + "PAPER",
        _LIVE + "_promoted",
        _LIVE + "_executable_ratio",
        "gate_" + "passed",
        "size_" + "changed",
    ]


def line_allowed(line: str) -> bool:
    """True when ``line`` does not enable a real account."""
    scrubbed = re.sub("upside_" + _LIVE, "", line, flags=re.IGNORECASE)
    scrubbed = re.sub(_LIVE + r"[ _]?quote", "", scrubbed, flags=re.IGNORECASE)
    multiplier = _LIVE + "_size_multiplier"
    if multiplier in line and not _MULTIPLIER_OK.search(line):
        return False
    if any(re.search(needle, scrubbed, re.IGNORECASE) for needle in _needles()):
        return bool(_REFUSAL.search(line))
    return True


def scan_tree(root: Path) -> list[str]:
    """Return ``path:line: text`` for every disallowed line under ``root``."""
    skip = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "data",
        "dist",
        "build",
        ".eggs",
    }
    suffixes = {".py", ".md", ".yml", ".yaml", ".json", ".toml", ".txt", ".example"}
    names = {".gitignore", ".env.example"}
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in skip for part in path.parts):
            continue
        if path.suffix not in suffixes and path.name not in names:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if not line_allowed(line):
                findings.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    return findings
