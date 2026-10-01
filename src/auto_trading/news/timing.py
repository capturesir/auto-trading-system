"""Per-market session clock for ``news_event``.

The judgement is available at ``judged_at``. An update item replaces that
time with its own ``judged_at``. ``published_at`` alone never starts the
clock. Timestamps must satisfy
``published_at <= first_seen_at <= judged_at <= generated_at``.

T0 is the first session whose open is strictly after that time. ``approx``
precision shifts T0 one session later. ``date_only`` is not usable. T1 is
``max_age_sessions`` sessions after T0, skipping exchange holidays.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from auto_trading.market.calendar import is_session, us_open_in_macau
from auto_trading.market.instruments import describe

MACAU = ZoneInfo("Asia/Macau")
OPEN_MACAU = {"JP": (8, 0), "KR": (8, 0), "TW": (9, 0), "HK": (9, 30)}


class TimingError(Exception):
    """The news item cannot be placed on a session clock."""


@dataclass(frozen=True)
class EventClock:
    usable: bool
    reason: str
    t0: date | None = None
    t1: date | None = None
    pre_event_session: date | None = None
    available_at: datetime | None = None


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise TimingError("timestamps need a timezone")
    return parsed.astimezone(MACAU)


def judgement_time(item: dict[str, Any], update: dict[str, Any] | None = None) -> datetime:
    """Availability time. An update's ``judged_at`` wins over the original."""
    chosen = update if update is not None else item
    judged = chosen.get("judged_at")
    if not judged:
        raise TimingError("judged_at is required")
    published = _parse(str(item["published_at"]))
    seen = _parse(str(item["first_seen_at"]))
    judged_at = _parse(str(judged))
    generated = _parse(str(chosen.get("generated_at") or item["generated_at"]))
    if not published <= seen <= judged_at <= generated:
        raise TimingError("published_at, first_seen_at, judged_at, generated_at are out of order")
    if published > seen:
        raise TimingError("published_at is after first_seen_at")
    return judged_at


def session_open(market: str, day: date) -> datetime:
    """Macau time when that market's regular session opens."""
    if market == "US":
        return us_open_in_macau(day)
    hour, minute = OPEN_MACAU[market]
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=MACAU)


def _calendar(market: str) -> str:
    return describe(_sample_ticker(market)).calendar


def _sample_ticker(market: str) -> str:
    names = {"US": "SPY", "JP": "7203.T", "HK": "0001.HK", "KR": "005930.KS", "TW": "2330.TW"}
    return names[market]


def _sessions(market: str, start: date, end: date) -> list[date]:
    import pandas as pd

    from auto_trading.market.calendar import _calendar as load_calendar

    code = _calendar(market)
    index = load_calendar(code).sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return [ts.date() for ts in index]


def shift_session(market: str, day: date, steps: int) -> date:
    """Move ``steps`` sessions forward (positive) or back (negative)."""
    if steps == 0:
        return day
    span = abs(steps) * 10 + 21
    if steps > 0:
        found = _sessions(market, day + timedelta(days=1), day + timedelta(days=span))
        if len(found) < steps:
            raise TimingError(f"no session {steps} after {day}")
        return found[steps - 1]
    found = _sessions(market, day - timedelta(days=span), day - timedelta(days=1))
    if len(found) < abs(steps):
        raise TimingError(f"no session {steps} before {day}")
    return found[steps]


def first_session_after(market: str, moment: datetime) -> date:
    """First session whose open is strictly later than ``moment``."""
    start = moment.astimezone(MACAU).date() - timedelta(days=1)
    for day in _sessions(market, start, start + timedelta(days=21)):
        if session_open(market, day) > moment:
            return day
    raise TimingError(f"no session after {moment.isoformat()}")


def is_first_session_of_week(market: str, day: date) -> bool:
    """True when ``day`` is that market's first session of its ISO week."""
    if not is_session(_calendar(market), day):
        return False
    week = day.isocalendar()
    for offset in range(1, 7):
        earlier = day - timedelta(days=offset)
        if earlier.isocalendar()[:2] != (week.year, week.week):
            break
        if is_session(_calendar(market), earlier):
            return False
    return True


def event_clock(
    item: dict[str, Any],
    market: str,
    *,
    update: dict[str, Any] | None = None,
    max_age_sessions: int = 1,
) -> EventClock:
    """Place T0 and the entry session T1 on the market calendar."""
    precision = str(item.get("published_at_precision") or "exact")
    if precision == "date_only":
        return EventClock(False, "date_only")
    try:
        available = judgement_time(item, update)
    except TimingError as exc:
        return EventClock(False, str(exc))
    t0 = first_session_after(market, available)
    if precision == "approx":
        t0 = shift_session(market, t0, 1)
    pre = shift_session(market, t0, -1)
    t1 = shift_session(market, t0, max_age_sessions)
    return EventClock(True, "ok", t0, t1, pre, available)


def entry_due(clock: EventClock, decision_day: date) -> bool:
    """The 07:05 decision on ``decision_day`` is the T1 entry, and not later."""
    return bool(clock.usable and clock.t1 == decision_day)
