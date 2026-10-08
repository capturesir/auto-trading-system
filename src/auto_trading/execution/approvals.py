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

APPROVAL_TIMEOUT = timedelta(minutes=120)
SUBMIT_WINDOW = timedelta(minutes=30)
_HASH_SKIP = frozenset({"order_hash", "status", "decided_at"})


class ApprovalError(Exception):
    """The approval command was refused."""


def order_hash(payload: dict[str, Any]) -> str:
    return sha256_prefixed(payload)


def signed_body(
    payload: dict[str, Any],
    *,
    order_id: str,
    approval_price: float,
    attempt: int,
) -> dict[str, Any]:
    """Fields bound into the approval hash. Approval metadata stays outside."""
    return {
        **payload,
        "order_id": order_id,
        "approval_price": approval_price,
        "attempt": attempt,
    }


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


def _decided_path(trading_home: Path, order_id: str) -> Path:
    return trading_home / "approvals" / "decided" / f"{order_id}.json"


def approve(
    trading_home: Path,
    order_id: str,
    given_hash: str,
    *,
    by: str,
    channel: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Recompute the order hash, enforce the 120-minute timeout, and audit it."""
    if not by or not channel:
        raise ApprovalError("approval needs by and channel")
    decided = _decided_path(trading_home, order_id)
    if decided.is_file():
        existing = json.loads(decided.read_text(encoding="utf-8"))
        from auto_trading.execution.approval_mode import is_auto_approver

        if is_auto_approver(existing.get("approver")):
            raise ApprovalError("already approved automatically")
        raise ApprovalError("already decided")
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
    document["submit_by"] = (moment + SUBMIT_WINDOW).isoformat(timespec="seconds")
    document["approved_by"] = by
    document["channel"] = channel
    TradeJournal(trading_home).record_approval(
        order_id=order_id,
        order_hash=fresh,
        status="APPROVED",
        detail={"given_hash": given_hash, "by": by, "channel": channel},
        when=moment,
        approver="user",
        channel=channel,
        attempt=1,
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


def list_pending(trading_home: Path) -> list[dict[str, Any]]:
    folder = trading_home / "approvals" / "pending"
    if not folder.is_dir():
        return []
    rows = []
    for path in sorted(folder.glob("*.json")):
        rows.append(json.loads(path.read_text(encoding="utf-8")))
    return rows


def decline(
    trading_home: Path,
    order_id: str,
    *,
    by: str,
    channel: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Refuse a manual pending order. Automatic approvals are not declined here."""
    source = trading_home / "approvals" / "pending" / f"{order_id}.json"
    if not source.is_file():
        raise ApprovalError("missing pending order")
    document = json.loads(source.read_text(encoding="utf-8"))
    moment = now or datetime.now(UTC)
    document["status"] = "DECLINED"
    document["decided_at"] = moment.isoformat(timespec="seconds")
    document["approved_by"] = by
    document["channel"] = channel
    TradeJournal(trading_home).record_approval(
        order_id=order_id,
        order_hash=str(document.get("order_hash") or ""),
        status="DECLINED",
        detail={"by": by, "channel": channel},
        when=moment,
        approver="user",
        channel=channel,
    )
    folder = trading_home / "approvals" / "decided"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{order_id}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    source.unlink()
    return document


def needs_reapproval(approval_price: float, latest: float, *, limit: float = 0.015) -> bool:
    """True when the latest price has moved more than 1.5% from the snapshot."""
    if approval_price <= 0:
        return True
    return abs(latest / approval_price - 1) > limit
