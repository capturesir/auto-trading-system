"""Versioned strategy JSON and the approval hash gate.

A strategy runs only when the canonical hash of its document matches the
hash a person approved. The hash is SHA-256 of sorted-key JSON with the
top-level ``mode`` removed, prefixed by ``sha256:``. Changing ``mode`` does
not change the hash. Any other edit does, and needs a new approval.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from auto_trading.jsonutil import mapping_without_mode, sha256_prefixed

INTERPRETER_VERSION = "0.6.0"
HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class StrategyNotApproved(Exception):
    """The spec is not the human-approved version."""


class ApprovedStrategy(BaseModel):
    """Hash recorded when a person approved one strategy version."""

    strategy_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    spec_hash: str = Field(pattern=HASH_PATTERN.pattern)


def spec_hash(document: Mapping[str, Any]) -> str:
    """Return ``sha256:`` plus the digest. Key order and ``mode`` are ignored."""
    return sha256_prefixed(mapping_without_mode(document))


def require_approved(document: Mapping[str, Any], approved: ApprovedStrategy) -> str:
    """Return the digest when ``document`` is exactly the approved version."""
    strategy_id = document.get("id")
    version = document.get("version")
    if strategy_id != approved.strategy_id or version != approved.version:
        raise StrategyNotApproved(
            f"approved version is {approved.strategy_id}@{approved.version}, "
            f"spec is {strategy_id}@{version}"
        )
    digest = spec_hash(document)
    if digest != approved.spec_hash.lower():
        raise StrategyNotApproved(
            f"strategy spec hash {digest} does not match approved hash {approved.spec_hash}"
        )
    return digest
