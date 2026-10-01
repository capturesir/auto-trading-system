"""Versioned strategy JSON and the approval hash gate.

A strategy runs only when the canonical hash of its document matches the
hash a person approved. Drafts, including anything produced by an LLM, are
not executable until that hash is recorded. Changing the document, including
switching ``mode`` to ``live``, changes the hash and requires a new approval.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field


class StrategyNotApproved(Exception):
    """The spec is not the human-approved version."""


class ApprovedStrategy(BaseModel):
    """Hash recorded when a person approved one strategy version."""

    strategy_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    spec_hash: str = Field(min_length=64, max_length=64)


def spec_hash(document: Mapping[str, Any]) -> str:
    """SHA-256 of canonical JSON. Key order does not change the digest."""
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
