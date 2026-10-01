"""Session calendar helpers.

Trading dates come from ``exchange_calendars``. Hong Kong National Day
2026-10-01 is closed. US regular-hours open is 09:30 America/New_York, which
is 21:30 Macau during daylight time and 22:30 Macau from 2026-11-01.
"""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

MACAU = ZoneInfo("Asia/Macau")
NEW_YORK = ZoneInfo("America/New_York")


@lru_cache(maxsize=8)
def _calendar(code: str):
    import exchange_calendars as xcals

    return xcals.get_calendar(code)


def is_session(calendar_code: str, day: date) -> bool:
    """Return whether ``day`` is a trading session on that exchange."""
    import pandas as pd

    return bool(_calendar(calendar_code).is_session(pd.Timestamp(day)))


def us_open_in_macau(day: date) -> datetime:
    """Macau time of the US cash open on ``day`` (09:30 New York)."""
    opened = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NEW_YORK)
    return opened.astimezone(MACAU)
