"""File-based approval queue.

``trade approve <id> --hash <first12>`` accepts a pending order only when
the first 12 hex characters match. A 1.5% move against the approval price
asks for a new approval instead of sending the order.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from auto_trading.jsonutil import sha256_prefixed
from auto_trading.portfolio.journal import TradeJournal

APPROVAL_TIMEOUT = timedelta(minutes=15)
_HASH_SKIP = frozenset({"order_hash", "status", "decided_at"})


class ApprovalError(Exception):
    """The approval command was refused."""


def order_hash(payload: dict[str, Any]) -> str:
    return sha256_prefixed(payload)


def hash12(digest: str) -> str:
    return digest.removeprefix("sha256:")[:12]


def _canonical(document: dict[str, Any]) -> dict[str, Any]:
    """Fields that ``approve`` hashes again. Status is not part of the order."""
    return {key: value for key, value in document.items() if key not in _HASH_SKIP}


def queue_order(trading_home: Path, order_id: str, payload: dict[str, Any]) -> Path:
    """Write ``approvals/pending/<id>.json``."""
    document = {
        **payload,
        "order_id": order_id,
        "status": "PENDING_APPROVAL",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    document["order_hash"] = order_hash(_canonical(document))
    folder = trading_home / "approvals" / "pending"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{order_id}.json"
    temporary = target.with_suffix(".json.tmp")
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)
    return target


def approve(
    trading_home: Path,
    order_id: str,
    given_hash: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Recompute the order hash, enforce the timeout, and write an audit row."""
    source = trading_home / "approvals" / "pending" / f"{order_id}.json"
    if not source.is_file():
        raise ApprovalError("missing pending order")
    document = json.loads(source.read_text(encoding="utf-8"))
    fresh = order_hash(_canonical(document))
    if fresh != document.get("order_hash") or hash12(fresh) != given_hash:
        raise ApprovalError("hash mismatch")
    moment = now or datetime.now(UTC)
    created_raw = document.get("created_at")
    if not created_raw:
        raise ApprovalError("approval timeout")
    created = datetime.fromisoformat(created_raw)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    if moment - created > APPROVAL_TIMEOUT:
        raise ApprovalError("approval timeout")
    document["status"] = "APPROVED"
    document["decided_at"] = moment.isoformat(timespec="seconds")
    TradeJournal(trading_home).record_approval(
        order_id=order_id,
        order_hash=fresh,
        status="APPROVED",
        detail={"given_hash": given_hash},
        when=moment,
    )
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
