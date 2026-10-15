"""CI checks for the approval literal, the approvals CHECK, and the sync list.

The allowlist file is compared to its frozen contents. New identifiers that
contain the stem must already be on that list.
"""

from __future__ import annotations

import ast
import io
import re
import sqlite3
import sys
import tempfile
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "auto_" + "paper"
STEM = "liv" + "e"
ALLOWLIST = (
    "upside_live",
    "upside_live_pct",
    "min_upside_live_pct",
    "upside_live_below_pct",
    "live_size_multiplier",
    "LIVE_NOT_AUTHORIZED",
    "live_quote",
)
SCAN_ROOTS = (
    "src",
    "tests",
    "config",
    "vps",
    "scripts",
    "ci",
    ".github",
    "strategies",
    "data",
    "research",
)
# Files that split the stem so the scanner cannot see it. Adding a path
# requires manager review. Do not extend this set for new code.
REVIEWED_SPLIT_FILES = frozenset(
    {
        "scripts/check_v08_ci.py",
        "src/auto_trading/paper_only.py",
        "src/auto_trading/strategy/registry.py",
        "tests/test_execution_paper.py",
        "tests/test_paper_only.py",
        "tests/test_review_fix1.py",
        "tests/test_review_fix2.py",
        "tests/test_v08d.py",
    }
)
_SPLIT_RE = re.compile(r"""[\"']liv[\"']\s*\+""")
_STEM_RE = re.compile(rf"[A-Za-z0-9_]*{STEM}[A-Za-z0-9_]*", re.IGNORECASE)


def _fold_const(node: ast.AST) -> str | None:
    """Fold a constant string expression. A plain literal is not a concatenation."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold_const(node.left)
        right = _fold_const(node.right)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
                continue
            if isinstance(value, ast.FormattedValue):
                folded = _fold_const(value.value)
                if folded is None:
                    return None
                parts.append(folded)
                continue
            return None
        return "".join(parts)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr != "join" or len(node.args) != 1:
            return None
        base = _fold_const(node.func.value)
        arg = node.args[0]
        if base is None or not isinstance(arg, ast.List | ast.Tuple):
            return None
        bits = [_fold_const(elt) for elt in arg.elts]
        if any(bit is None for bit in bits):
            return None
        return base.join([bit for bit in bits if bit is not None])
    return None


def _concat_hides_stem(text: str) -> bool:
    """True when joined string pieces contain the stem."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp | ast.JoinedStr | ast.Call):
                continue
            folded = _fold_const(node)
            if folded and _STEM_RE.search(folded):
                return True
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return False
    group: list[str] = []

    def flush() -> bool:
        nonlocal group
        if len(group) < 2:
            group = []
            return False
        try:
            joined = "".join(ast.literal_eval(tok) for tok in group)
        except (SyntaxError, ValueError):
            group = []
            return False
        group = []
        return _STEM_RE.search(joined) is not None

    for tok in tokens:
        if tok.type == tokenize.STRING:
            group.append(tok.string)
            continue
        if flush():
            return True
    return flush()


def split_stem_offenders(root: Path = ROOT) -> list[str]:
    """Paths outside the reviewed list that hide the stem by joining strings."""
    from auto_trading.paper_only import iter_text_files

    found: list[str] = []
    for path in iter_text_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in REVIEWED_SPLIT_FILES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py" and _concat_hides_stem(text):
            found.append(rel)
            continue
        if path.suffix != ".py" and _SPLIT_RE.search(text):
            found.append(rel)
    return found


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
    if "\nlogs/\n" not in "\n" + manifest + "\n" or "\nreports/\n" not in "\n" + manifest + "\n":
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
    allow_path = ROOT / "ci" / ("r0_" + STEM + "_allowlist.txt")
    raw = allow_path.read_text(encoding="utf-8").splitlines()
    listed = [line.strip() for line in raw if line.strip()]
    if listed != list(ALLOWLIST):
        print("allowlist changed", file=sys.stderr)
        return 1
    from auto_trading.paper_only import iter_text_files

    pattern = re.compile(rf"[A-Za-z0-9_]*{STEM}[A-Za-z0-9_]*", re.IGNORECASE)
    found: set[str] = set()
    for name in SCAN_ROOTS:
        base = ROOT / name
        if not base.exists():
            continue
        for path in iter_text_files(base):
            text = path.read_text(encoding="utf-8", errors="replace")
            found.update(pattern.findall(text))
    offenders = split_stem_offenders(ROOT)
    if offenders:
        print("stem split outside the reviewed files:", file=sys.stderr)
        for item in offenders:
            print(item, file=sys.stderr)
        return 1
    readme = ROOT / "README.md"
    if readme.is_file():
        found.update(pattern.findall(readme.read_text(encoding="utf-8", errors="replace")))
    extra = sorted(found - set(ALLOWLIST))
    if extra:
        print("identifiers not on the allowlist:", file=sys.stderr)
        for item in extra:
            print(item, file=sys.stderr)
        return 1
    from auto_trading.execution.strategy_book import confirmed_source_errors

    pin_errors = confirmed_source_errors(ROOT)
    if pin_errors:
        print("confirmed strategies diverged:", file=sys.stderr)
        for item in pin_errors:
            print(item, file=sys.stderr)
        return 1
    print("v08 checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
