from __future__ import annotations

import datetime as dt

from tos_bot.util import clock
from tos_bot.util.clock import Session

NY = clock.NY


def _t(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=NY)


def test_sessions_regular_day():
    assert clock.current_session(_t(2026, 9, 3, 3, 0)) is Session.CLOSED
    assert clock.current_session(_t(2026, 9, 3, 7, 0)) is Session.PRE
    assert clock.current_session(_t(2026, 9, 3, 10, 0)) is Session.REGULAR
    assert clock.current_session(_t(2026, 9, 3, 17, 0)) is Session.POST
    assert clock.current_session(_t(2026, 9, 3, 21, 0)) is Session.CLOSED
    assert clock.is_market_open(_t(2026, 9, 3, 10, 0))
    assert not clock.is_market_open(_t(2026, 9, 3, 7, 0))


def test_weekend_and_holiday():
    assert clock.current_session(_t(2026, 9, 5, 12, 0)) is Session.CLOSED     # Saturday
    assert clock.is_holiday(dt.date(2026, 11, 26))                            # Thanksgiving
    assert not clock.is_trading_day(dt.date(2026, 11, 26))
    assert clock.current_session(_t(2026, 11, 26, 11, 0)) is Session.CLOSED


def test_half_day_early_close():
    # 2026-11-27 is a 1:00 pm early close
    assert clock.is_half_day(dt.date(2026, 11, 27))
    assert clock.current_session(_t(2026, 11, 27, 12, 0)) is Session.REGULAR
    assert clock.current_session(_t(2026, 11, 27, 14, 0)) is Session.POST
    assert clock.minutes_to_close(_t(2026, 11, 27, 12, 45)) == 15.0


def test_market_status_payload():
    ms = clock.market_status(_t(2026, 9, 3, 7, 0))
    assert ms["session"] == "PRE"
    assert ms["next_change_to"] == "REGULAR"
    assert ms["next_holiday"]["name"] == "Labor Day"
    assert "Pre-market" in ms["label"]


def test_next_session_skips_holiday_weekend():
    # Friday after close -> next open is Monday... but Mon 2026-09-07 is Labor Day
    nxt_dt, nxt_sess = clock.next_session_change(_t(2026, 9, 4, 20, 30))
    assert nxt_dt.date() == dt.date(2026, 9, 8)      # Tuesday
    assert nxt_sess is Session.PRE
