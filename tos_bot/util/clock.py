"""US equity market clock + calendar.

Knows the four sessions (closed / pre-market / regular / post-market), NYSE
full-day holidays and half-days (1:00 pm early close) through 2028, and can
say exactly what the market is doing right now and when it next opens/closes.

Extended hours used here (America/New_York):
    pre-market   04:00 - 09:30
    regular      09:30 - 16:00   (13:00 on half-days)
    post-market  16:00 - 20:00   (17:00 on half-days)
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from functools import lru_cache
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo  # py3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

NY = ZoneInfo("America/New_York")

PRE_OPEN = dt.time(4, 0)
OPEN = dt.time(9, 30)
CLOSE = dt.time(16, 0)
POST_CLOSE = dt.time(20, 0)
HALF_DAY_CLOSE = dt.time(13, 0)
HALF_DAY_POST_CLOSE = dt.time(17, 0)


class Session(str, Enum):
    CLOSED = "CLOSED"
    PRE = "PRE"          # pre-market
    REGULAR = "REGULAR"
    POST = "POST"        # after-hours

    @property
    def is_open(self) -> bool:
        return self is not Session.CLOSED

    @property
    def is_extended(self) -> bool:
        return self in (Session.PRE, Session.POST)


# NYSE full-day closures  ->  name
_HOLIDAYS: Dict[dt.date, str] = {
    dt.date(2025, 1, 1): "New Year's Day", dt.date(2025, 1, 20): "Martin Luther King Jr. Day",
    dt.date(2025, 2, 17): "Washington's Birthday", dt.date(2025, 4, 18): "Good Friday",
    dt.date(2025, 5, 26): "Memorial Day", dt.date(2025, 6, 19): "Juneteenth",
    dt.date(2025, 7, 4): "Independence Day", dt.date(2025, 9, 1): "Labor Day",
    dt.date(2025, 11, 27): "Thanksgiving Day", dt.date(2025, 12, 25): "Christmas Day",

    dt.date(2026, 1, 1): "New Year's Day", dt.date(2026, 1, 19): "Martin Luther King Jr. Day",
    dt.date(2026, 2, 16): "Washington's Birthday", dt.date(2026, 4, 3): "Good Friday",
    dt.date(2026, 5, 25): "Memorial Day", dt.date(2026, 6, 19): "Juneteenth",
    dt.date(2026, 7, 3): "Independence Day (observed)", dt.date(2026, 9, 7): "Labor Day",
    dt.date(2026, 11, 26): "Thanksgiving Day", dt.date(2026, 12, 25): "Christmas Day",

    dt.date(2027, 1, 1): "New Year's Day", dt.date(2027, 1, 18): "Martin Luther King Jr. Day",
    dt.date(2027, 2, 15): "Washington's Birthday", dt.date(2027, 3, 26): "Good Friday",
    dt.date(2027, 5, 31): "Memorial Day", dt.date(2027, 6, 18): "Juneteenth (observed)",
    dt.date(2027, 7, 5): "Independence Day (observed)", dt.date(2027, 9, 6): "Labor Day",
    dt.date(2027, 11, 25): "Thanksgiving Day", dt.date(2027, 12, 24): "Christmas Day (observed)",

    dt.date(2028, 1, 17): "Martin Luther King Jr. Day",
    dt.date(2028, 2, 21): "Washington's Birthday", dt.date(2028, 4, 14): "Good Friday",
    dt.date(2028, 5, 29): "Memorial Day", dt.date(2028, 6, 19): "Juneteenth",
    dt.date(2028, 7, 4): "Independence Day", dt.date(2028, 9, 4): "Labor Day",
    dt.date(2028, 11, 23): "Thanksgiving Day", dt.date(2028, 12, 25): "Christmas Day",
}

# NYSE 1:00 pm early closes (day before July 4, day after Thanksgiving, Christmas Eve)
_HALF_DAYS: Dict[dt.date, str] = {
    dt.date(2025, 7, 3): "Independence Day (early close)",
    dt.date(2025, 11, 28): "day after Thanksgiving (early close)",
    dt.date(2025, 12, 24): "Christmas Eve (early close)",
    dt.date(2026, 11, 27): "day after Thanksgiving (early close)",
    dt.date(2026, 12, 24): "Christmas Eve (early close)",
    dt.date(2027, 11, 26): "day after Thanksgiving (early close)",
    dt.date(2028, 7, 3): "Independence Day (early close)",
    dt.date(2028, 11, 24): "day after Thanksgiving (early close)",
}


def now_ny() -> dt.datetime:
    return dt.datetime.now(NY)


def _as_ny(ts: Optional[dt.datetime]) -> dt.datetime:
    return ts.astimezone(NY) if ts else now_ny()


def is_holiday(d: dt.date) -> bool:
    return d in _HOLIDAYS


def holiday_name(d: dt.date) -> Optional[str]:
    return _HOLIDAYS.get(d)


def is_half_day(d: dt.date) -> bool:
    return d in _HALF_DAYS


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in _HOLIDAYS


def regular_close_time(d: dt.date) -> dt.time:
    return HALF_DAY_CLOSE if d in _HALF_DAYS else CLOSE


def post_close_time(d: dt.date) -> dt.time:
    return HALF_DAY_POST_CLOSE if d in _HALF_DAYS else POST_CLOSE


# --------------------------------------------------------------------------- #
def current_session(ts: Optional[dt.datetime] = None) -> Session:
    ts = _as_ny(ts)
    if not is_trading_day(ts.date()):
        return Session.CLOSED
    t = ts.time()
    rclose = regular_close_time(ts.date())
    if PRE_OPEN <= t < OPEN:
        return Session.PRE
    if OPEN <= t < rclose:
        return Session.REGULAR
    if rclose <= t < post_close_time(ts.date()):
        return Session.POST
    return Session.CLOSED


def is_market_open(ts: Optional[dt.datetime] = None) -> bool:
    """True only during the REGULAR session (what the scanner keys off)."""
    return current_session(ts) is Session.REGULAR


def is_any_session_open(ts: Optional[dt.datetime] = None) -> bool:
    return current_session(ts).is_open


def next_session_change(ts: Optional[dt.datetime] = None):
    """(datetime, Session) of the next boundary - when the market next changes
    state. Looks up to ~10 days ahead to clear long weekends / holidays."""
    ts = _as_ny(ts)
    cur = current_session(ts)
    probe = ts
    for _ in range(10 * 24 * 4):          # 15-min steps, ~10 days
        probe = probe + dt.timedelta(minutes=15)
        s = current_session(probe)
        if s != cur:
            # snap to the exact minute of the change
            lo = probe - dt.timedelta(minutes=15)
            for _ in range(15):
                lo += dt.timedelta(minutes=1)
                if current_session(lo) == s:
                    return lo, s
            return probe, s
    return None, cur


def market_status(ts: Optional[dt.datetime] = None) -> dict:
    ts = _as_ny(ts)
    d = ts.date()
    sess = current_session(ts)
    nxt_dt, nxt_sess = next_session_change(ts)
    nh_date, nh_name = next_holiday(d)
    rclose = regular_close_time(d)
    return {
        "now": ts.isoformat(),
        "session": sess.value,
        "is_trading_day": is_trading_day(d),
        "is_holiday": is_holiday(d),
        "holiday_name": holiday_name(d),
        "is_half_day": is_half_day(d),
        "half_day_reason": _HALF_DAYS.get(d),
        "regular_open": dt.datetime.combine(d, OPEN, tzinfo=NY).isoformat(),
        "regular_close": dt.datetime.combine(d, rclose, tzinfo=NY).isoformat(),
        "minutes_to_regular_close": (
            (dt.datetime.combine(d, rclose, tzinfo=NY) - ts).total_seconds() / 60.0
            if sess is Session.REGULAR else None
        ),
        "next_change_at": nxt_dt.isoformat() if nxt_dt else None,
        "next_change_to": nxt_sess.value if nxt_dt else None,
        "next_holiday": {"date": nh_date.isoformat(), "name": nh_name} if nh_date else None,
        "label": _status_label(sess, d, nxt_dt, nxt_sess),
    }


def _status_label(sess: Session, d: dt.date, nxt_dt, nxt_sess) -> str:
    if sess is Session.CLOSED:
        if is_holiday(d):
            base = f"Closed - {holiday_name(d)}"
        elif d.weekday() >= 5:
            base = "Closed - weekend"
        else:
            base = "Closed"
        if nxt_dt:
            base += f" (next: {nxt_sess.lower()} at {nxt_dt.strftime('%a %H:%M ET')})"
        return base
    if sess is Session.PRE:
        return "Pre-market" + (f" - closes into regular at {nxt_dt.strftime('%H:%M ET')}" if nxt_dt else "")
    if sess is Session.POST:
        return "After-hours" + (f" - closes {nxt_dt.strftime('%H:%M ET')}" if nxt_dt else "")
    tag = " (half day)" if is_half_day(d) else ""
    return f"Regular hours{tag}" + (f" - close {nxt_dt.strftime('%H:%M ET')}" if nxt_dt else "")


@lru_cache(maxsize=8)
def next_holiday(after: dt.date):
    upcoming = sorted(dd for dd in _HOLIDAYS if dd >= after)
    return (upcoming[0], _HOLIDAYS[upcoming[0]]) if upcoming else (None, None)


# --------------------------------------------------------------------------- #
def session_date(ts: Optional[dt.datetime] = None) -> dt.date:
    ts = _as_ny(ts)
    d = ts.date()
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def prev_trading_day(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def next_trading_day(d: dt.date) -> dt.date:
    d += dt.timedelta(days=1)
    while not is_trading_day(d):
        d += dt.timedelta(days=1)
    return d


def add_trading_days(ts: dt.datetime, n: float) -> dt.datetime:
    """`ts` + `n` trading days (fractional ok). Time-of-day is preserved; only
    whole trading days are stepped, then the fractional part is added as a
    fraction of a 6.5h session."""
    ts = _as_ny(ts)
    whole = int(n)
    frac = n - whole
    d = ts.date()
    step = 1 if whole >= 0 else -1
    for _ in range(abs(whole)):
        d += dt.timedelta(days=step)
        while not is_trading_day(d):
            d += dt.timedelta(days=step)
    out = dt.datetime.combine(d, ts.timetz())
    return out + dt.timedelta(hours=6.5 * frac)


def trading_days_between(a: dt.datetime, b: dt.datetime) -> float:
    """Approximate number of trading days between two instants (>= 0 if b > a).
    Whole trading dates in the open interval + a same-day fraction of a 6.5h
    session. Good enough for an 'is this trade overdue' gauge."""
    a, b = _as_ny(a), _as_ny(b)
    if b < a:
        return -trading_days_between(b, a)
    n = 0
    d = a.date()
    while d < b.date():
        d += dt.timedelta(days=1)
        if is_trading_day(d):
            n += 1
    # subtract the un-elapsed part of the first day, add the elapsed part of last
    day_sec = 6.5 * 3600
    if is_trading_day(a.date()):
        secs_after_open = max(0.0, (a - dt.datetime.combine(a.date(), OPEN, tzinfo=NY)).total_seconds())
        n -= min(1.0, secs_after_open / day_sec) if a.date() != b.date() else 0.0
    if is_trading_day(b.date()) and a.date() != b.date():
        secs_after_open = max(0.0, (b - dt.datetime.combine(b.date(), OPEN, tzinfo=NY)).total_seconds())
        n += min(1.0, secs_after_open / day_sec)
    elif a.date() == b.date():
        n = max(0.0, (b - a).total_seconds() / day_sec)
    return round(max(0.0, n), 3)


@lru_cache(maxsize=64)
def last_n_sessions(anchor: dt.date, n: int) -> tuple:
    days: List[dt.date] = []
    d = anchor
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    while len(days) < n:
        if is_trading_day(d):
            days.append(d)
        d -= dt.timedelta(days=1)
    return tuple(reversed(days))


def minutes_since_open(ts: Optional[dt.datetime] = None) -> float:
    ts = _as_ny(ts)
    open_dt = dt.datetime.combine(ts.date(), OPEN, tzinfo=NY)
    return max(0.0, (ts - open_dt).total_seconds() / 60.0)


def minutes_to_close(ts: Optional[dt.datetime] = None) -> float:
    """Minutes until the regular close (respects half-days). Large number when
    the market is not in the regular session."""
    ts = _as_ny(ts)
    if current_session(ts) is not Session.REGULAR:
        return 1e9
    close_dt = dt.datetime.combine(ts.date(), regular_close_time(ts.date()), tzinfo=NY)
    return max(0.0, (close_dt - ts).total_seconds() / 60.0)
