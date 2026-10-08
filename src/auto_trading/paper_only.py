"""Source scan that refuses real-account switches.

Identifiers that contain the stem are accepted only when they appear, character
for character, in the allowlist file. A comment does not exempt anything else.
Ports other than 4002 and 7497 are refused. Account ids are accepted only when
they match ``^DU[A-Z]?[0-9]+$`` and equal ``IBKR_PAPER_ACCOUNT``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from auto_trading.paper_account import DU_ACCOUNT

_STEM = "liv" + "e"
_IDENT = re.compile(rf"[A-Za-z0-9_]*{_STEM}[A-Za-z0-9_]*", re.IGNORECASE)
_PORT = re.compile(r"\b(?:" + "40" + "01" + "|" + "749" + "6" + r")\b")
_SWITCH = re.compile(
    r"\b(?:" + "TRADING_" + "MODE" + "|" + "IBKR_" + "USERNAME" + "|" + "IBKR_" + "PAPER" + r")\b"
)
PAPER_PORTS = (4002, 7497)
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".eggs",
}
_SUFFIXES = {
    ".py",
    ".md",
    ".yml",
    ".yaml",
    ".json",
    ".jsonl",
    ".toml",
    ".txt",
    ".example",
    ".ini",
    ".cfg",
    ".conf",
    ".sql",
    ".sh",
    ".service",
    ".timer",
    ".template",
    ".csv",
}
_MAX_TEXT_BYTES = 5 * 1024 * 1024


def allowlist_path() -> Path:
    """Path of the exact identifier list. The name is built so this file stays clean."""
    name = "r0_" + _STEM + "_allowlist.txt"
    return Path(__file__).resolve().parents[2] / "ci" / name


def allow_tokens() -> frozenset[str]:
    """Exact identifiers that may contain the stem."""
    path = allowlist_path()
    tokens: set[str] = set()
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            token = raw.strip()
            if token and not token.startswith("#"):
                tokens.add(token)
    return frozenset(tokens)


def paper_account_pin() -> str:
    """The one account this process may manage. Empty when the pin is unset."""
    return os.environ.get("IBKR_PAPER_ACCOUNT", "").strip()


def account_accepted(account: str, pin: str | None = None) -> bool:
    """True only for the paper pattern and the pinned account."""
    chosen = paper_account_pin() if pin is None else pin.strip()
    return bool(chosen) and account == chosen and DU_ACCOUNT.fullmatch(account) is not None


def port_accepted(port: int) -> bool:
    """True only for the paper Gateway port and the paper TWS port."""
    return port in PAPER_PORTS


def line_allowed(line: str) -> bool:
    """True when ``line`` does not enable a real account.

    Every identifier that contains the stem must be on the allowlist. A
    refusal comment does not change that. Forbidden ports fail on their own.
    """
    allowed = allow_tokens()
    for token in _IDENT.findall(line):
        if token not in allowed:
            return False
    return not (_PORT.search(line) or _SWITCH.search(line))


def _looks_text(path: Path) -> bool:
    try:
        if path.stat().st_size > _MAX_TEXT_BYTES:
            return False
        chunk = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"\0" not in chunk


def _scannable(path: Path) -> bool:
    """Text files, including names with no extension, ``.cfg``, and ``.conf``.

    Directories named ``build`` or ``dist`` are not skipped. Caches and
    virtualenvs are not source.
    """
    if any(part in _SKIP_DIRS for part in path.parts):
        return False
    if path.suffix in _SUFFIXES:
        return _looks_text(path)
    if path.suffix == "":
        return _looks_text(path)
    return False


def iter_text_files(root: Path):
    """Yield scannable files under ``root``."""
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and _scannable(path):
            yield path


def scan_tree(root: Path) -> list[str]:
    """Return ``path:line: text`` for every disallowed line under ``root``."""
    findings: list[str] = []
    for path in iter_text_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if not line_allowed(line):
                findings.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    return findings
