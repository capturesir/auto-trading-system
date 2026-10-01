"""File-based approval queue.

``trade approve <id> --hash <first12>`` accepts a pending order only when
the first 12 hex characters match. A 1.5% move against the approval price
asks for a new approval instead of sending the order.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from auto_trading.jsonutil import sha256_prefixed


class ApprovalError(Exception):
    """The approval command was refused."""


def order_hash(payload: dict[str, Any]) -> str:
    return sha256_prefixed(payload)


def hash12(digest: str) -> str:
    return digest.removeprefix("sha256:")[:12]


def queue_order(trading_home: Path, order_id: str, payload: dict[str, Any]) -> Path:
    """Write ``approvals/pending/<id>.json``."""
    digest = order_hash(payload)
    document = {**payload, "order_id": order_id, "order_hash": digest, "status": "PENDING_APPROVAL"}
    folder = trading_home / "approvals" / "pending"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{order_id}.json"
    temporary = target.with_suffix(".json.tmp")
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)
    return target


def approve(trading_home: Path, order_id: str, given_hash: str) -> dict[str, Any]:
    """Move a pending file to ``approvals/decided`` when the hash matches."""
    source = trading_home / "approvals" / "pending" / f"{order_id}.json"
    if not source.is_file():
        raise ApprovalError("missing pending order")
    document = json.loads(source.read_text(encoding="utf-8"))
    if hash12(document["order_hash"]) != given_hash:
        raise ApprovalError("hash mismatch")
    document["status"] = "APPROVED"
    folder = trading_home / "approvals" / "decided"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{order_id}.json"
    temporary = target.with_suffix(".json.tmp")
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)
    source.unlink()
    return document


def needs_reapproval(approval_price: float, latest: float, *, limit: float = 0.015) -> bool:
    """True when the latest price has moved more than 1.5% from the snapshot."""
    if approval_price <= 0:
        return True
    return abs(latest / approval_price - 1) > limit
