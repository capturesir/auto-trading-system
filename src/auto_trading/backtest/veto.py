"""Load veto-rule files by ``schema_version`` (strategy spec v0.8 §15)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class UnsupportedVetoSchema(Exception):
    """The file's ``schema_version`` has no loader."""


def _tickers_1_0(document: Mapping[str, Any]) -> frozenset[str]:
    blocks = document.get("blocks") or []
    return frozenset(str(row["ticker"]) for row in blocks if row.get("ticker"))


def _tickers_1_1(document: Mapping[str, Any]) -> frozenset[str]:
    rules = document.get("rules") or []
    names = []
    for row in rules:
        if row.get("action") not in (None, "block_entry"):
            continue
        symbol = row.get("symbol") or row.get("ticker")
        if symbol:
            names.append(str(symbol))
    return frozenset(names)


LOADERS = {
    "veto_rule/1.0": _tickers_1_0,
    "veto_rule/1.1": _tickers_1_1,
}


def load_veto_document(document: Mapping[str, Any]) -> frozenset[str]:
    """Dispatch one document. Unknown versions raise."""
    version = document.get("schema_version")
    loader = LOADERS.get(str(version))
    if loader is None:
        raise UnsupportedVetoSchema(str(version))
    return loader(document)


def load_veto_file(path: Path) -> frozenset[str]:
    """Read JSON and dispatch on ``schema_version``."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise UnsupportedVetoSchema("not an object")
    return load_veto_document(document)


def blocked_tickers(paths: list[Path] | None) -> frozenset[str]:
    """Union of every veto file. An empty list blocks nothing."""
    found: set[str] = set()
    for path in paths or []:
        found.update(load_veto_file(path))
    return frozenset(found)
