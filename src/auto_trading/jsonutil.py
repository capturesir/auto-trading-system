"""Canonical JSON helpers shared by strategy hashes and risk config hashes.

Both digests are SHA-256 of UTF-8 JSON with sorted keys and no whitespace.
``json.dumps`` uses Python's shortest float form, so ``0.10`` becomes ``0.1``
and ``200000`` (int) is not ``200000.0`` (float).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that refuses repeated keys at one object level."""
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate key: {key}")
        obj[key] = value
    return obj


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def loads_strict(text: str) -> Any:
    """Parse UTF-8 JSON, rejecting duplicate keys and NaN/Infinity."""
    return json.loads(
        text,
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=_reject_constant,
    )


def canonical_dumps(obj: Any) -> str:
    """Serialize ``obj`` the way the spec hashes it."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_prefixed(obj: Any) -> str:
    """``sha256:`` plus the hex digest of :func:`canonical_dumps`."""
    payload = canonical_dumps(obj).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def mapping_without_mode(document: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a strategy document and drop only the top-level ``mode`` key."""
    return {key: value for key, value in document.items() if key != "mode"}
