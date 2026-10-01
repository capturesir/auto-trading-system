"""Session clock for R10.

Continuous-session open and close come from ``exchange_calendars``.
New entries stop five minutes after the open and ten minutes before the
close. Lunch breaks (where the calendar has one) and the opening or
closing auction windows reject every new order. Taiwan's calendar has no
lunch break, so none is invented here.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from auto_trading.market.calendar import _calendar

CALENDARS = {"US": "XNYS", "HK": "XHKG", "JP": "XTKS", "KR": "XKRX", "TW": "XTAI"}
# Minutes of call auction before the continuous open.
PRE_OPEN_MIN = {"HK": 30, "KR": 30}
# Closing auction inside the continuous session, measured back from the close.
PRE_CLOSE_AUCTION_MIN = {"JP": 5, "TW": 5, "KR": 10}
# Closing auction after the continuous close.
POST_CLOSE_MIN = {"HK": 10}
BLOCK_ALL = frozenset({"closed", "lunch", "open_auction", "close_auction"})
BLOCK_ENTRIES = BLOCK_ALL | {"open_buffer", "close_buffer"}


def session_window(market: str, now: datetime) -> str:
    """Name the window ``now`` falls in for ``market``.

    One of ``continuous``, ``open_buffer``, ``close_buffer``, ``lunch``,
    ``open_auction``, ``close_auction``, or ``closed``.
    """
    if market not in CALENDARS:
        return "closed"
    if now.tzinfo is None:
        raise ValueError("session clock needs a timezone")
    calendar = _calendar(CALENDARS[market])
    moment = pd.Timestamp(now).tz_convert("UTC")
    session = _nearby_session(calendar, moment)
    if session is None:
        return "closed"
    opened = calendar.session_open(session)
    closed = calendar.session_close(session)
    if _in_lunch(calendar, session, moment):
        return "lunch"
    if moment < opened:
        if _pre_open(market, opened, moment):
            return "open_auction"
        return "closed"
    if moment >= closed:
        if _post_close(market, closed, moment):
            return "close_auction"
        return "closed"
    if _pre_close_auction(market, closed, moment):
        return "close_auction"
    if moment < opened + pd.Timedelta(minutes=5):
        return "open_buffer"
    if moment >= closed - pd.Timedelta(minutes=10):
        return "close_buffer"
    return "continuous"


def _nearby_session(calendar, moment: pd.Timestamp):
    local = moment.tz_convert(calendar.tz)
    for offset in (-1, 0, 1):
        day = (local + pd.Timedelta(days=offset)).normalize()
        label = pd.Timestamp(day.date())
        if not calendar.is_session(label):
            continue
        opened = calendar.session_open(label)
        closed = calendar.session_close(label)
        early = opened - pd.Timedelta(minutes=40)
        late = closed + pd.Timedelta(minutes=15)
        if early <= moment < late:
            return label
    return None


def _in_lunch(calendar, session, moment: pd.Timestamp) -> bool:
    if not calendar.has_break:
        return False
    start = calendar.session_break_start(session)
    end = calendar.session_break_end(session)
    if pd.isna(start) or pd.isna(end):
        return False
    return start <= moment < end


def _pre_open(market: str, opened: pd.Timestamp, moment: pd.Timestamp) -> bool:
    minutes = PRE_OPEN_MIN.get(market)
    if minutes is None:
        return False
    return opened - pd.Timedelta(minutes=minutes) <= moment < opened


def _post_close(market: str, closed: pd.Timestamp, moment: pd.Timestamp) -> bool:
    minutes = POST_CLOSE_MIN.get(market)
    if minutes is None:
        return False
    return closed <= moment < closed + pd.Timedelta(minutes=minutes)


def _pre_close_auction(market: str, closed: pd.Timestamp, moment: pd.Timestamp) -> bool:
    minutes = PRE_CLOSE_AUCTION_MIN.get(market)
    if minutes is None:
        return False
    return closed - pd.Timedelta(minutes=minutes) <= moment < closed
