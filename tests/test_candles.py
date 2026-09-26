"""Live candles built from the streamed ticks: prices from the trades, volume from the day's cumulative count,
closed on the clock, 5-minute candles on New York's boundaries, and only the regular session."""

from __future__ import annotations

import datetime as dt

from tos_bot.data import candles
from tos_bot.data.candles import LiveCandles
from tos_bot.util import clock

DAY = dt.date(2026, 9, 24)          # a full trading day
HALF = dt.date(2026, 11, 27)        # a 1 pm close
NEXT = dt.date(2026, 9, 25)


def _t(day: dt.date, hh: int, mm: int, ss: float = 0.0) -> float:
    """That New York time as epoch seconds."""
    return dt.datetime.combine(day, dt.time(hh, mm), clock.NY).timestamp() + ss


def _row(c):
    return c.at.strftime("%H:%M"), c.minutes, c.open, c.high, c.low, c.close, c.volume, c.partial


def test_the_days_are_what_the_tests_say():
    assert clock.is_trading_day(DAY) and not clock.is_half_day(DAY)
    assert clock.is_trading_day(HALF) and clock.is_half_day(HALF)
    assert clock.is_trading_day(NEXT)


def test_a_candle_takes_its_prices_from_the_trades_and_its_volume_from_the_cumulative():
    lc = LiveCandles()
    lc.add("AAA", 10.0, 1000, _t(DAY, 9, 59, 50))
    lc.add("AAA", 10.2, 1100, _t(DAY, 10, 0, 1))
    lc.add("AAA", 10.5, 1150, _t(DAY, 10, 0, 10))
    lc.add("AAA", 10.5, 1150, _t(DAY, 10, 0, 20))       # the bid or ask moved: no trade
    lc.add("AAA", 9.9, 1300, _t(DAY, 10, 0, 30))
    lc.add("AAA", 9.9, 1340, _t(DAY, 10, 0, 40))        # a trade at the same price: only the volume says so
    lc.add("AAA", 10.1, 1360, _t(DAY, 10, 0, 50))
    assert _row(lc.forming("AAA")) == ("10:00", 1, 10.2, 10.5, 9.9, 10.1, 360.0, False)
    lc.add("AAA", 10.1, 1360, _t(DAY, 10, 1, 5))        # a bid/ask update in the next minute opens no candle
    assert lc.forming("AAA").at.strftime("%H:%M") == "10:00"
    lc.add("AAA", 10.3, 0, _t(DAY, 10, 1, 10))          # no volume read: a trade, no baseline lost
    lc.add("AAA", 10.4, 1400, _t(DAY, 10, 1, 20))
    assert _row(lc.forming("AAA")) == ("10:01", 1, 10.3, 10.4, 10.3, 10.4, 40.0, False)
    shown = lc.forming("AAA")
    shown.close = 0.0
    assert lc.forming("AAA").close == 10.4                # a copy: a reader can't change the candle forming


def test_the_roll_closes_a_quiet_stocks_candle_on_the_clock_and_a_minute_with_no_trade_makes_none():
    lc = LiveCandles()
    lc.add("AAA", 10.0, 500, _t(DAY, 10, 0, 5))
    lc.add("AAA", 10.3, 600, _t(DAY, 10, 0, 40))
    assert lc.roll(_t(DAY, 10, 0, 59)) == {}                     # the minute isn't over
    got = lc.roll(_t(DAY, 10, 1))                                # no later tick: the clock closes it
    assert list(got) == ["AAA"] and got["AAA"].close == 10.3 and got["AAA"] is lc.latest("AAA")
    assert lc.forming("AAA") is None
    assert lc.roll(_t(DAY, 10, 2)) == {}                         # no trade in 10:01: no candle
    lc.add("AAA", 10.4, 650, _t(DAY, 10, 2, 30))
    lc.add("AAA", 10.6, 700, _t(DAY, 10, 3, 0.2))                # its next minute began before the roll...
    assert lc.roll(_t(DAY, 10, 3))["AAA"].close == 10.4          # ...and the roll still says which one closed
    assert [c.at.strftime("%H:%M") for c in lc.closed("AAA")] == ["10:00", "10:02"]
    assert lc.closed("AAA", n=1) == [lc.latest("AAA")] and lc.closed("BBB") == [] and lc.latest("BBB") is None


def test_five_minute_candles_close_on_new_yorks_boundaries_made_of_the_minutes():
    lc = LiveCandles()
    cum = 10_000
    lc.add("AAA", 20.0, cum, _t(DAY, 9, 29, 30))                 # pre-market: seen before the open
    minutes = [(20.0, 20.5, 19.8, 20.2), (20.2, 20.9, 20.1, 20.8), (20.8, 21.0, 20.6, 20.7),
               (20.7, 20.8, 20.3, 20.4), (20.4, 20.6, 20.2, 20.5), (20.5, 21.3, 20.5, 21.1),
               (21.1, 21.2, 20.7, 20.8)]
    for m, prices in enumerate(minutes):                         # 09:30 .. 09:36
        for sec, px in zip((1, 15, 30, 50), prices):
            cum += 100
            lc.add("AAA", px, cum, _t(DAY, 9, 30 + m, sec))
        if m < 6:
            lc.roll(_t(DAY, 9, 31 + m))
            if m < 4:
                assert lc.closed("AAA", minutes=5) == []         # 09:35 hasn't come
    [five] = lc.closed("AAA", minutes=5)
    assert _row(five) == ("09:30", 5, 20.0, 21.0, 19.8, 20.5, 2000.0, False) and lc.latest("AAA", 5) is five
    assert five.at == dt.datetime(2026, 9, 24, 9, 30, tzinfo=clock.NY)
    # 09:35 closed and 09:36 still forming: the 5-minute candle so far
    assert _row(lc.forming("AAA", 5)) == ("09:35", 5, 20.5, 21.3, 20.5, 20.8, 800.0, False)
    assert _row(lc.forming("AAA")) == ("09:36", 1, 21.1, 21.2, 20.7, 20.8, 400.0, False)
    lc.roll(_t(DAY, 9, 40))                                      # a quiet stretch: 09:35 still closes on time
    assert [_row(c)[:2] for c in lc.closed("AAA", 5)] == [("09:30", 5), ("09:35", 5)]
    assert lc.forming("AAA", 5) is None                          # nothing yet in 09:40


def test_extended_hours_make_no_candle_but_set_the_baseline_and_the_half_day_ends_at_1259():
    lc = LiveCandles()
    lc.add("AAA", 5.0, 50_000, _t(DAY, 8, 0))
    lc.add("AAA", 5.2, 80_000, _t(DAY, 9, 29, 59))
    assert lc.forming("AAA") is None and lc.closed("AAA") == [] and lc.roll(_t(DAY, 9, 30)) == {}
    lc.add("AAA", 5.3, 80_500, _t(DAY, 9, 30, 2))
    assert lc.forming("AAA").volume == 500                       # none of the pre-market's volume
    lc.add("AAA", 5.4, 81_000, _t(DAY, 15, 59, 30))
    assert lc.roll(_t(DAY, 16, 0))["AAA"].at.strftime("%H:%M") == "15:59"
    lc.add("AAA", 5.5, 90_000, _t(DAY, 16, 0, 1))                # after-hours
    lc.add("AAA", 5.6, 91_000, _t(DAY, 17, 30))
    assert lc.forming("AAA") is None and lc.roll(_t(DAY, 17, 31)) == {}
    assert [c.at.strftime("%H:%M") for c in lc.closed("AAA")] == ["09:30", "15:59"]

    half = LiveCandles()
    half.add("BBB", 7.0, 1000, _t(HALF, 12, 58, 30))
    half.add("BBB", 7.1, 1100, _t(HALF, 12, 59, 30))
    half.add("BBB", 7.2, 1200, _t(HALF, 13, 0, 0))               # the close has passed
    half.roll(_t(HALF, 13, 0))
    half.add("BBB", 7.3, 1300, _t(HALF, 13, 0, 30))
    assert half.forming("BBB") is None and half.roll(_t(HALF, 13, 1)) == {}
    assert [c.at.strftime("%H:%M") for c in half.closed("BBB")] == ["12:58", "12:59"]
    assert [c.at.strftime("%H:%M") for c in half.closed("BBB", 5)] == ["12:55"]


def test_a_stream_that_starts_mid_candle_makes_it_partial_and_keep_forgets_a_stream_that_ended():
    lc = LiveCandles()
    lc.add("BBB", 30.0, 1000, _t(DAY, 10, 0, 0))
    lc.add("AAA", 10.0, 1000, _t(DAY, 10, 2, 20))                # first seen 20 s into 10:02
    lc.add("BBB", 30.1, 1100, _t(DAY, 10, 2, 20))
    lc.roll(_t(DAY, 10, 3))
    lc.add("AAA", 10.2, 1200, _t(DAY, 10, 3, 10))
    lc.add("BBB", 30.2, 1200, _t(DAY, 10, 3, 10))
    lc.roll(_t(DAY, 10, 4))
    lc.roll(_t(DAY, 10, 5))
    assert [c.partial for c in lc.closed("AAA")] == [True, False]
    assert lc.latest("AAA", 5).partial                           # seen after 10:00: the 5 minutes aren't whole
    assert [c.partial for c in lc.closed("BBB")] == [False, False, False] and not lc.latest("BBB", 5).partial

    lc.keep(["BBB"])                                             # AAA's stream ended
    assert lc.symbols() == ["BBB"] and lc.closed("AAA") == [] and lc.forming("AAA", 5) is None
    lc.add("AAA", 10.5, 2000, _t(DAY, 10, 6, 0.5))               # ...and started again
    restarted = lc.forming("AAA")
    assert restarted.partial and restarted.volume == 0           # no baseline yet: no volume, never a guess

    lc.add("BBB", 30.3, 900, _t(DAY, 10, 6, 5))                  # the day's count went down
    lc.add("BBB", 30.4, 950, _t(DAY, 10, 6, 10))
    assert lc.forming("BBB").volume == 50                        # counted again from 900, never negative


def test_a_late_tick_goes_into_the_next_candle_the_history_is_capped_and_a_new_day_starts_clean(monkeypatch):
    lc = LiveCandles()
    lc.add("AAA", 10.0, 1000, _t(DAY, 10, 0, 10))
    lc.roll(_t(DAY, 10, 1))
    lc.add("AAA", 10.9, 1100, _t(DAY, 10, 0, 59.9))              # stamped before the roll, in after it
    assert _row(lc.latest("AAA"))[2:6] == (10.0, 10.0, 10.0, 10.0)      # the closed candle isn't reopened
    assert _row(lc.forming("AAA"))[:2] == ("10:01", 1) and lc.forming("AAA").close == 10.9
    lc.add("AAA", 11.0, 1200, _t(DAY, 15, 59, 59.9))
    lc.roll(_t(DAY, 16, 0))
    lc.add("AAA", 11.5, 1300, _t(DAY, 15, 59, 59.95))            # late for the last minute: the close has passed
    assert lc.forming("AAA") is None

    monkeypatch.setattr(candles, "KEEP_1M", 3)
    monkeypatch.setattr(candles, "KEEP_5M", 2)
    short = LiveCandles()
    for m in range(15):                                          # 10:00 .. 10:14, one trade a minute
        short.add("AAA", 10.0 + m, 1000 + m, _t(DAY, 10, m, 30))
        short.roll(_t(DAY, 10, m + 1))
    assert [c.at.strftime("%H:%M") for c in short.closed("AAA")] == ["10:12", "10:13", "10:14"]
    assert [c.at.strftime("%H:%M") for c in short.closed("AAA", 5)] == ["10:05", "10:10"]

    lc.add("BBB", 12.0, 500, _t(NEXT, 4, 0))                     # the next morning's first tick
    assert lc.symbols() == ["BBB"] and lc.closed("AAA") == []
    lc.roll(_t(NEXT + dt.timedelta(days=1), 0, 1))               # ...and the roll just after midnight
    assert lc.symbols() == []
    lc.add("AAA", 10.0, 100, _t(DAY, 10, 0, 10))
    lc.clear()
    assert lc.symbols() == []
