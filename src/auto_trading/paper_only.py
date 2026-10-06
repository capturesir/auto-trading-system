"""Source scan that refuses real-account switches.

``upside_live`` is a price metric and is allowlisted. A quote placeholder
may say ``live quote``. Everything else that names a real-account mode, a
real-account port, or a deleted promotion event must sit on a refusal line.
"""

from __future__ import annotations

import re
from pathlib import Path

_LIVE = "liv" + "e"
_EXACT_ALLOW = frozenset(
    {
        "paper_only",
        "refuse",
        "refused",
        "refuses",
        "reject",
        "rejected",
        "forbidden",
        "violation",
    }
)
_PHRASES = ("must be null", "不得", "拒絕")
_HARD_ASSIGN = re.compile(
    "live_"
    + "mode"
    + r"\s*=\s*"
    + "true"
    + "|port"
    + r"\s*=\s*"
    + "40"
    + "01"
    + "|port"
    + r"\s*=\s*"
    + "749"
    + "6",
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
    """True when ``line`` does not enable a real account.

    Refusal tokens are whole words. A mode assignment or a forbidden port
    assignment fails even when a refusal word is also on the line.
    """
    if _HARD_ASSIGN.search(line):
        return False
    scrubbed = re.sub("upside_" + _LIVE, "", line, flags=re.IGNORECASE)
    scrubbed = re.sub(_LIVE + r"[ _]?quote", "", scrubbed, flags=re.IGNORECASE)
    # Paper Gateway login names are not a real-account switch. A bare paper flag still fails.
    scrubbed = re.sub("IBKR_" + "PAPER_USERNAME", "", scrubbed, flags=re.IGNORECASE)
    scrubbed = re.sub("IBKR_" + "PAPER_PASSWORD", "", scrubbed, flags=re.IGNORECASE)
    # The account pin names one paper id. A bare paper flag still fails.
    scrubbed = re.sub("IBKR_" + "PAPER_ACCOUNT", "", scrubbed, flags=re.IGNORECASE)
    multiplier = _LIVE + "_size_multiplier"
    if multiplier in line and not _MULTIPLIER_OK.search(line):
        return False
    if not any(re.search(needle, scrubbed, re.IGNORECASE) for needle in _needles()):
        return True
    words = {token.lower() for token in re.findall(r"[A-Za-z0-9_]+", line)}
    if words & _EXACT_ALLOW:
        return True
    return any(phrase in line for phrase in _PHRASES)


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
    suffixes = {
        ".py",
        ".md",
        ".yml",
        ".yaml",
        ".json",
        ".toml",
        ".txt",
        ".example",
        ".ini",
        ".sh",
        ".service",
        ".template",
    }
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
