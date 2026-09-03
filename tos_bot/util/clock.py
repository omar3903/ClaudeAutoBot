"""US equity market clock helpers.

Regular session only (09:30-16:00 America/New_York), plus a static list of
full-day holidays through 2027. Half-days are treated as full sessions - the
scanner just returns nothing outside 09:30-16:00 so that is harmless.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import List

try:
    from zoneinfo import ZoneInfo  # py3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

NY = ZoneInfo("America/New_York")

OPEN = dt.time(9, 30)
CLOSE = dt.time(16, 0)

# NYSE full-day closures (YYYY, M, D)
_HOLIDAYS = {
    dt.date(2025, 1, 1), dt.date(2025, 1, 20), dt.date(2025, 2, 17),
    dt.date(2025, 4, 18), dt.date(2025, 5, 26), dt.date(2025, 6, 19),
    dt.date(2025, 7, 4), dt.date(2025, 9, 1), dt.date(2025, 11, 27),
    dt.date(2025, 12, 25),
    dt.date(2026, 1, 1), dt.date(2026, 1, 19), dt.date(2026, 2, 16),
    dt.date(2026, 4, 3), dt.date(2026, 5, 25), dt.date(2026, 6, 19),
    dt.date(2026, 7, 3), dt.date(2026, 9, 7), dt.date(2026, 11, 26),
    dt.date(2026, 12, 25),
    dt.date(2027, 1, 1), dt.date(2027, 1, 18), dt.date(2027, 2, 15),
    dt.date(2027, 3, 26), dt.date(2027, 5, 31), dt.date(2027, 6, 18),
    dt.date(2027, 7, 5), dt.date(2027, 9, 6), dt.date(2027, 11, 25),
    dt.date(2027, 12, 24),
}


def now_ny() -> dt.datetime:
    return dt.datetime.now(NY)


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in _HOLIDAYS


def is_market_open(ts: dt.datetime | None = None) -> bool:
    ts = ts.astimezone(NY) if ts else now_ny()
    if not is_trading_day(ts.date()):
        return False
    return OPEN <= ts.time() <= CLOSE


def session_date(ts: dt.datetime | None = None) -> dt.date:
    """The trading date a timestamp belongs to (before the open -> that day;
    after the close on a weekday still that day; weekend -> previous Friday)."""
    ts = ts.astimezone(NY) if ts else now_ny()
    d = ts.date()
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def prev_trading_day(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def last_n_sessions(anchor: dt.date, n: int) -> tuple:
    """The `n` most recent trading dates up to and including `anchor`
    (or the trading day on/just before it). Used by the PDT counter's
    rolling 5-business-day window."""
    days: List[dt.date] = []
    d = anchor
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    while len(days) < n:
        if is_trading_day(d):
            days.append(d)
        d -= dt.timedelta(days=1)
    return tuple(reversed(days))


def minutes_since_open(ts: dt.datetime | None = None) -> float:
    ts = ts.astimezone(NY) if ts else now_ny()
    open_dt = dt.datetime.combine(ts.date(), OPEN, tzinfo=NY)
    return max(0.0, (ts - open_dt).total_seconds() / 60.0)
