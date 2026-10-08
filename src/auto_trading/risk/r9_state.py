"""Persist ``state/r9_cycle.json`` and append ``r9_cycle_events``.

The transition rules are in :mod:`auto_trading.risk.r9_cycle`. This module
only stores them, catches up missed XHKG sessions, and emits alerts.
A missing file on a home that has never traded is the first start (ACTIVE).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from auto_trading.jsonutil import sha256_prefixed
from auto_trading.market.calendar import is_session
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.risk.r9_cycle import (
    ACTIVE,
    HALF,
    HALTED,
    early_resume,
    on_nlv,
    on_risk_day_end,
    size_mult,
)

SCHEMA = "r9_cycle/1.0"
_WARN_KINDS = frozenset({"r9_half_size_reset", "r9_resume_blocked"})


def cycle_path(home: Path) -> Path:
    return home / "state" / "r9_cycle.json"


def _suffix(account: str) -> str:
    return account[-4:] if len(account) >= 4 else account


def _stamp(now: datetime) -> str:
    moment = now if now.tzinfo else now.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seal(document: dict[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in document.items() if key != "content_sha256"}
    document["content_sha256"] = sha256_prefixed(body)
    return document


def _hash_ok(document: dict[str, Any]) -> bool:
    declared = document.get("content_sha256")
    body = {key: value for key, value in document.items() if key != "content_sha256"}
    return declared == sha256_prefixed(body)


def _blank(account: str, hwm: float, now: datetime) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA,
        "account_suffix": _suffix(account),
        "state": ACTIVE,
        "cycle_no": 0,
        "hwm_base": float(hwm),
        "halt_risk_date": None,
        "halt_nlv_base": None,
        "cooldown_days_done": 0,
        "resume_risk_date": None,
        "resume_by": None,
        "half_size_days_done": 0,
        "breach_today": False,
        "resume_pending": False,
        "last_counted_risk_date": None,
        "seq": 0,
        "updated_at_utc": _stamp(now),
    }


def _pure(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": document["state"],
        "hwm": float(document["hwm_base"]),
        "halt_risk_date": document.get("halt_risk_date"),
        "cooldown_days_done": int(document.get("cooldown_days_done") or 0),
        "half_size_days_done": int(document.get("half_size_days_done") or 0),
        "breach_today": bool(document.get("breach_today")),
        "resume_pending": bool(document.get("resume_pending")),
        "cycle_no": int(document.get("cycle_no") or 0),
    }


def _view(document: dict[str, Any]) -> dict[str, Any]:
    pure = _pure(document)
    return {
        "state": pure["state"],
        "size_mult": size_mult(pure),
        "cooldown_days_done": pure["cooldown_days_done"],
        "half_size_days_done": pure["half_size_days_done"],
        "hwm": pure["hwm"],
        "halt_risk_date": pure["halt_risk_date"],
        "resume_pending": pure["resume_pending"],
        "cycle_no": pure["cycle_no"],
    }


def write_cycle(home: Path, document: dict[str, Any]) -> None:
    """Atomic replace. A crash before replace leaves the previous file intact."""
    folder = home / "state"
    folder.mkdir(parents=True, exist_ok=True)
    target = cycle_path(home)
    temporary = target.with_suffix(".json.tmp")
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    directory = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read(home: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(document, problem)``. ``problem`` is None when the file is usable."""
    path = cycle_path(home)
    if not path.is_file():
        return None, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None, "parse"
    if not isinstance(payload, dict):
        return None, "parse"
    if not _hash_ok(payload):
        return payload, "hash"
    if payload.get("state") not in {ACTIVE, HALTED, HALF}:
        return payload, "state"
    if payload.get("schema_version") != SCHEMA:
        return payload, "parse"
    return payload, None


def _halted_document(account: str, now: datetime, risk_date: str, hwm: float) -> dict[str, Any]:
    document = _blank(account, hwm, now)
    document.update(
        state=HALTED,
        cycle_no=1,
        halt_risk_date=risk_date,
        halt_nlv_base=hwm,
        cooldown_days_done=0,
        half_size_days_done=0,
        breach_today=False,
        resume_pending=False,
    )
    return document


def _blocked(home: Path) -> bool:
    return (home / "KILL_SWITCH").is_file()


def _first_start(home: Path, journal: TradeJournal) -> bool:
    no_file = not cycle_path(home).is_file()
    return no_file and journal.r9_event_count() == 0 and journal.order_count() == 0


def cycle_view(home: Path) -> dict[str, Any] | None:
    """Read-only view. A missing file is not a halt (first-start homes stay active)."""
    document, problem = _read(home)
    if document is None or problem == "missing":
        return None
    if problem is not None:
        return {
            "state": HALTED,
            "size_mult": 0.0,
            "cooldown_days_done": 0,
            "half_size_days_done": 0,
            "hwm": float(document.get("hwm_base") or 0),
            "halt_risk_date": document.get("halt_risk_date"),
            "resume_pending": False,
            "cycle_no": int(document.get("cycle_no") or 0),
        }
    return _view(document)


def summary_fields(home: Path) -> dict[str, Any]:
    """Daily-summary halt fields. Counts are null outside the state that uses them."""
    view = cycle_view(home)
    if view is None:
        return {
            "r9_cycle_state": ACTIVE,
            "r9_size_mult": 1.0,
            "r9_cooldown_days_done": None,
            "r9_half_size_days_done": None,
        }
    state = view["state"]
    return {
        "r9_cycle_state": state,
        "r9_size_mult": view["size_mult"],
        "r9_cooldown_days_done": view["cooldown_days_done"] if state == HALTED else None,
        "r9_half_size_days_done": view["half_size_days_done"] if state == HALF else None,
    }


def expected_resume_date(halt_risk_date: str, sessions: int = 20) -> str:
    """The XHKG session on which cooldown completes. The trigger day does not count."""
    day = date.fromisoformat(halt_risk_date)
    found = 0
    while found < sessions:
        day += timedelta(days=1)
        if is_session("XHKG", day):
            found += 1
    return day.isoformat()


def status_bits(home: Path) -> str:
    view = cycle_view(home)
    if view is None:
        return "r9_state=ACTIVE r9_resume_on=none"
    if view["state"] != HALTED or not view.get("halt_risk_date"):
        return f"r9_state={view['state']} r9_resume_on=none"
    if view.get("resume_pending"):
        return f"r9_state={HALTED} r9_resume_on=pending_kill_switch"
    when = expected_resume_date(str(view["halt_risk_date"]))
    return f"r9_state={HALTED} r9_resume_on={when}"


def _seq_problem(document: dict[str, Any], journal: TradeJournal) -> bool:
    last = journal.last_r9_seq()
    if last is None:
        return False
    return int(document.get("seq") or 0) < last


def load_cycle(
    home: Path,
    *,
    now: datetime,
    account: str = "",
    hwm: float | None = None,
    risk_date: str | None = None,
) -> dict[str, Any]:
    """Load the file, or create ACTIVE on a true first start. Corrupt files halt."""
    journal = TradeJournal(home)
    document, problem = _read(home)
    if problem == "missing" and _first_start(home, journal):
        fresh = _seal(_blank(account, float(hwm or 0.0), now))
        fresh["seq"] = 1
        fresh = _seal(fresh)
        write_cycle(home, fresh)
        return fresh
    corrupt = problem is not None or (document is not None and _seq_problem(document, journal))
    if corrupt or document is None:
        mark = float(hwm or 0.0)
        if document is not None and document.get("hwm_base"):
            mark = float(document["hwm_base"])
        day = risk_date or _stamp(now)[:10]
        halted = _halted_document(account, now, day, mark or 1.0)
        _commit(home, halted, now, events=[], risk_date=day, account=account, corrupt=True)
        loaded, _problem = _read(home)
        assert loaded is not None
        return loaded
    return document


def _apply(
    document: dict[str, Any],
    pure: dict[str, Any],
    events: list[dict[str, Any]],
    risk_date: str,
    *,
    count_day: bool,
) -> str:
    previous = str(document["state"])
    document["state"] = pure["state"]
    document["hwm_base"] = pure["hwm"]
    document["halt_risk_date"] = pure["halt_risk_date"]
    document["cooldown_days_done"] = pure["cooldown_days_done"]
    document["half_size_days_done"] = pure["half_size_days_done"]
    document["breach_today"] = pure["breach_today"]
    document["resume_pending"] = pure["resume_pending"]
    document["cycle_no"] = pure["cycle_no"]
    for event in events:
        if event["kind"] == "r9_halt":
            document["halt_nlv_base"] = event["nlv"]
            document["resume_risk_date"] = None
            document["resume_by"] = None
        elif event["kind"] == "r9_resume_half_size":
            document["resume_risk_date"] = risk_date
            document["resume_by"] = event["by"]
    if count_day:
        document["last_counted_risk_date"] = risk_date
    return previous


def _commit(
    home: Path,
    document: dict[str, Any],
    now: datetime,
    *,
    events: list[dict[str, Any]],
    risk_date: str,
    account: str,
    previous: str | None = None,
    corrupt: bool = False,
) -> None:
    journal = TradeJournal(home)
    last = journal.last_r9_seq() or 0
    document["updated_at_utc"] = _stamp(now)
    document["seq"] = max(int(document.get("seq") or 0), last) + 1
    document["account_suffix"] = document.get("account_suffix") or _suffix(account)
    _seal(document)
    write_cycle(home, document)
    prior = previous if previous is not None else str(document["state"])
    for event in events:
        journal.append_r9_event(
            account=account,
            cycle_no=int(document["cycle_no"]),
            from_state=prior,
            to_state=str(document["state"]),
            kind=str(event["kind"]),
            risk_date=risk_date,
            nlv_base=event.get("nlv"),
            old_hwm_base=event.get("old_hwm", event.get("hwm")),
            new_hwm_base=event.get("new_hwm"),
            by=event.get("by"),
            seq=int(document["seq"]),
            when=now,
        )
        prior = str(document["state"])
        _side_effects(home, journal, event, now, account, document)
    if corrupt:
        journal.append_r9_event(
            account=account,
            cycle_no=int(document["cycle_no"]),
            from_state="CORRUPT",
            to_state=HALTED,
            kind="r9_state_corrupt",
            risk_date=risk_date,
            nlv_base=document.get("halt_nlv_base"),
            old_hwm_base=document.get("hwm_base"),
            new_hwm_base=None,
            by=None,
            seq=int(document["seq"]),
            when=now,
        )
        _critical(
            home,
            kind="r9_state_corrupt",
            now=now,
            account=account,
            message_zh="R9 狀態檔損壞，冷卻由 0 重新起計。",
            resume_hint="冷卻滿 20 個 XHKG 交易日後自動進入半倉",
            document=document,
        )


def _side_effects(
    home: Path,
    journal: TradeJournal,
    event: dict[str, Any],
    now: datetime,
    account: str,
    document: dict[str, Any],
) -> None:
    kind = str(event["kind"])
    journal.audit(
        "r9_cycle",
        {"kind": kind, "risk_date": event.get("risk_date"), "by": event.get("by")},
    )
    if kind == "r9_halt":
        _halt_book(home, journal)
        journal.record_anomaly(
            kind="R9_HALT",
            detail={"risk_date": event.get("risk_date")},
            when=now,
        )
        _critical(
            home,
            kind="r9_halt",
            now=now,
            account=account,
            message_zh="回撤達到全停門檻，進入冷卻。",
            resume_hint="冷卻滿 20 個 XHKG 交易日後自動進入半倉",
            document=document,
            halted=True,
        )
    elif kind == "r9_resume_half_size":
        _record_reset(home, journal, event, now)
        _critical(
            home,
            kind="r9_resume_half_size",
            now=now,
            account=account,
            message_zh="冷卻結束，以半倉恢復。",
            resume_hint="連續 20 個 XHKG 交易日未再跌破 5% 後回復全倉",
            document=document,
        )
    elif kind == "r9_full_size":
        _critical(
            home,
            kind="r9_full_size",
            now=now,
            account=account,
            message_zh="半倉期滿，回復全倉。",
            resume_hint="已回復全倉",
            document=document,
        )
    elif kind in _WARN_KINDS:
        _warn(home, kind, now)


def _halt_book(home: Path, journal: TradeJournal) -> None:
    folder = home / "state"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "strategy_runtime.json").write_text(
        json.dumps({"status": "halted"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    journal.cancel_unfilled_orders()


def _record_reset(home: Path, journal: TradeJournal, event: dict[str, Any], now: datetime) -> None:
    by = str(event.get("by") or "r9_auto")
    new_hwm = float(event["new_hwm"])
    old = event.get("old_hwm")
    journal.record_hwm(
        hwm=new_hwm,
        nlv=event.get("nlv"),
        reason="reset",
        detail={"event": "reset", "by": by, "old_hwm": old, "new_hwm": new_hwm},
        when=now,
    )
    path = home / "state" / "account.json"
    if path.is_file():
        account = json.loads(path.read_text(encoding="utf-8"))
        account["hwm"] = new_hwm
        path.write_text(json.dumps(account, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _critical(
    home: Path,
    *,
    kind: str,
    now: datetime,
    account: str,
    message_zh: str,
    resume_hint: str,
    document: dict[str, Any],
    halted: bool = False,
) -> None:
    try:
        from auto_trading.alerts.critical import write_critical_event
        from auto_trading.alerts.forwarder import scan_once

        write_critical_event(
            home,
            kind=kind,
            reason_code="R9",
            rule_id="R9",
            now=now,
            account=account,
            message_zh=message_zh,
            resume_hint=resume_hint,
            actions={
                "kill_switch_file": False,
                "open_orders_cancelled": halted,
                "protective_stops_kept": True,
                "disconnected": False,
                "strategies_halted": halted,
            },
            state={
                "nlv_base": document.get("halt_nlv_base"),
                "hwm_base": document.get("hwm_base"),
                "drawdown_pct": None,
                "daily_pnl_pct": None,
            },
            detail={"r9_state": document.get("state"), "cycle_no": document.get("cycle_no")},
        )
        scan_once(home, now)
    except Exception:
        return


def _warn(home: Path, kind: str, now: datetime) -> None:
    try:
        from auto_trading.alerts.critical import write_ops_notice

        text = {
            "r9_half_size_reset": "半倉回撤超過 5%，計數歸零。",
            "r9_resume_blocked": "冷卻已滿，但全停旗標仍在，未進入半倉。",
        }[kind]
        write_ops_notice(home, message_zh=text, now=now, kind=kind)
    except Exception:
        return


def _store(
    home: Path,
    document: dict[str, Any],
    pure: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    now: datetime,
    risk_date: str,
    account: str,
    count_day: bool,
    previous: str,
) -> dict[str, Any]:
    changed = _pure(document) != pure
    _apply(document, pure, events, risk_date, count_day=count_day)
    if events or count_day or changed:
        _commit(
            home,
            document,
            now,
            events=events,
            risk_date=risk_date,
            account=account,
            previous=previous,
        )
    return document


def observe_nlv(
    home: Path,
    risk_date: str,
    nlv: float,
    now: datetime,
    *,
    account: str = "",
    hwm: float | None = None,
) -> dict[str, Any]:
    """Reconciliation print. Creates the file on first start."""
    mark = nlv if hwm is None else hwm
    document = load_cycle(home, now=now, account=account, hwm=mark, risk_date=risk_date)
    if document["state"] == ACTIVE and not document.get("hwm_base"):
        document["hwm_base"] = float(hwm if hwm is not None else nlv)
    previous = str(document["state"])
    pure, events = on_nlv(_pure(document), risk_date, nlv)
    return _store(
        home,
        document,
        pure,
        events,
        now=now,
        risk_date=risk_date,
        account=account,
        count_day=False,
        previous=previous,
    )


def close_risk_day(
    home: Path,
    risk_date: str,
    nlv: float,
    now: datetime,
    *,
    is_xhkg: bool,
    account: str = "",
    blocked: bool | None = None,
) -> dict[str, Any]:
    """One risk-day end. The trigger day does not increment the cooldown."""
    document = load_cycle(home, now=now, account=account, hwm=nlv, risk_date=risk_date)
    previous = str(document["state"])
    held = _blocked(home) if blocked is None else blocked
    pure, events = on_risk_day_end(_pure(document), risk_date, is_xhkg, nlv, held)
    return _store(
        home,
        document,
        pure,
        events,
        now=now,
        risk_date=risk_date,
        account=account,
        count_day=True,
        previous=previous,
    )


def catch_up(
    home: Path,
    through: date,
    nlv: float,
    now: datetime,
    *,
    account: str = "",
) -> dict[str, Any]:
    """Apply missed risk days through ``through``, using the XHKG calendar."""
    document = load_cycle(home, now=now, account=account, hwm=nlv, risk_date=through.isoformat())
    marker = document.get("last_counted_risk_date") or document.get("halt_risk_date")
    if not marker:
        return document
    day = date.fromisoformat(str(marker))
    if document.get("last_counted_risk_date"):
        day = day + timedelta(days=1)
    current = document
    while day <= through:
        current = close_risk_day(
            home,
            day.isoformat(),
            nlv,
            now,
            is_xhkg=is_session("XHKG", day),
            account=account,
        )
        day += timedelta(days=1)
    return current


def resume_early(
    home: Path,
    risk_date: str,
    nlv: float,
    now: datetime,
    *,
    account: str = "",
) -> dict[str, Any]:
    """User early resume. A request file in the sync directory is refused and audited."""
    journal = TradeJournal(home)
    for name in ("resume.request", "r9_early.request"):
        if (home / "state" / name).is_file():
            journal.audit("r9_early_refused", {"path": name})
            raise PermissionError("synced resume is refused")
    document = load_cycle(home, now=now, account=account, hwm=nlv, risk_date=risk_date)
    previous = str(document["state"])
    pure, events = early_resume(_pure(document), risk_date, nlv)
    return _store(
        home,
        document,
        pure,
        events,
        now=now,
        risk_date=risk_date,
        account=account,
        count_day=False,
        previous=previous,
    )


def force_halt(home: Path, now: datetime, *, account: str = "") -> dict[str, Any]:
    """Enter cooldown from the current account file. Does not write the kill switch."""
    path = home / "state" / "account.json"
    nlv = 0.0
    hwm = 0.0
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        nlv = float(payload.get("nlv") or 0)
        hwm = float(payload.get("hwm") or 0)
    if hwm <= 0:
        hwm = nlv or 1.0
    if nlv / hwm - 1 > -0.1:
        nlv = hwm * 0.899
    risk_date = _stamp(now)[:10]
    return observe_nlv(home, risk_date, nlv, now, account=account, hwm=hwm)
