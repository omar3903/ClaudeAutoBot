"""Live 1- and 5-minute candles built from the streamed ticks - for speed and for show, never for proof.

They only approximate IBKR's own bars. IBKR sends a stock's top-of-book ticks sampled about every 250 ms (about 4
a second), so a candle's high or low can miss a print that came and went between two samples; and the day-volume
tick (type 8) can lag the price, and counts prints that IBKR's TRADES bars leave out. What they are good for is
SPEED - a move is seen within a second of its minute's close, not at the next scan - and SHOW. Never proof: every
setup, confirmation and the replay stay on IBKR's own 5-minute TRADES bars (MarketData.intraday), the bars the
strategies were proven on.

Units: ib_async 2.1.0 passes IBKR's size fields through unchanged (wrapper.tickSize, no multiplier) at server
version 178, and IBKR sends US-stock sizes to API v10+ clients in shares - unless IB Gateway's "Send market data
in lots for US stocks for dual-mode API clients" box is ticked, when they come in lots of 100 (the legacy
tick-type table still says "multiplier 100"). Nothing here depends on the unit: a candle's volume is only ever
compared with the stream's own.

The IB loop feeds every tick to add() (O(1), its own short lock); the engine's candle loop calls roll() a moment
after each minute, so a candle closes on the clock even when its stock goes quiet.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections import deque
from dataclasses import dataclass, replace
from typing import Deque, Dict, Iterable, List, Optional, Tuple

from ..util import clock

#: closed candles kept per stock: one regular session of 1-minute candles (6.5 hours), and of 5-minute ones
KEEP_1M = 390
KEEP_5M = 156
#: 5-minute candles made up at most by one roll after the loop slept (an hour's worth)
CATCH_UP_5M = 12


@dataclass(slots=True)
class Candle:
    start: float            # the candle's first second, UTC epoch seconds - a whole minute
    minutes: int            # 1 or 5
    open: float
    high: float
    low: float
    close: float
    volume: float
    partial: bool = False   # the stream didn't cover the whole candle: it started, or started again, within it

    @property
    def at(self) -> dt.datetime:
        """The candle's start in New York time."""
        return dt.datetime.fromtimestamp(self.start, clock.NY)


class _Stock:
    """One stock's candles and where its stream stood at its last tick."""
    __slots__ = ("forming", "ones", "fives", "last_price", "last_cum", "first_seen")

    def __init__(self, first_seen: float) -> None:
        self.forming: Optional[Candle] = None
        self.ones: Deque[Candle] = deque(maxlen=KEEP_1M)
        self.fives: Deque[Candle] = deque(maxlen=KEEP_5M)
        self.last_price = 0.0
        self.last_cum: Optional[float] = None       # the Ticker's cumulative day volume last seen
        self.first_seen = first_seen


class LiveCandles:
    def __init__(self) -> None:
        # held only inside these methods, never while calling anything outside this module: the IB loop takes it
        # on every tick
        self._lock = threading.Lock()
        self._stocks: Dict[str, _Stock] = {}
        #: the candles a tick closed before the roll came for them (a busy stock's next minute began first)
        self._closed: Dict[str, Candle] = {}
        self._day: Tuple[float, float] = (0.0, 0.0)       # the New York day held: [midnight, next midnight)
        self._session: Tuple[float, float] = (0.0, 0.0)   # its regular session [09:30, the close) - empty when closed
        self._rolled = 0.0              # the minute the last roll closed up to
        self._boundary = 0.0            # the last 5-minute boundary made up

    # ---- the ticks (IB loop) ---------------------------------------------- #
    def add(self, symbol: str, price: float, cum_volume: float, at: float) -> None:
        """A streamed quote for ``symbol``: its last trade ``price``, the day's cumulative volume so far and when
        it came (epoch seconds). On the IB loop, so O(1): at most one Candle made a stock a minute. A quote whose
        price and volume didn't move is a bid/ask update and changes nothing; only a trade in regular hours goes
        into a candle, but every tick moves the volume baseline, so the 09:30 candle holds no pre-market volume."""
        if price <= 0:
            return
        with self._lock:
            if not self._day[0] <= at < self._day[1]:
                self._new_day(at)
            st = self._stocks.get(symbol)
            if st is None:
                st = self._stocks[symbol] = _Stock(at)
            known = cum_volume > 0                   # IBKR's missing volume reads as 0: no baseline from it
            traded = price != st.last_price or (st.last_cum is not None and cum_volume > st.last_cum)
            added = 0.0
            if known:
                if st.last_cum is not None and cum_volume >= st.last_cum:
                    added = cum_volume - st.last_cum
                st.last_cum = cum_volume             # a lower cumulative only starts the count again
            st.last_price = price
            if not traded:
                return
            # a tick stamped before the last roll (late off a busy loop) goes into the next candle - never into
            # one already closed
            minute = max(at, self._rolled) // 60 * 60
            c = st.forming
            if c is not None and minute < c.start:
                minute = c.start
            if at < self._session[0] or minute >= self._session[1]:
                return                               # pre-market or after-hours: no candle, as IBKR's TRADES bars
            if c is not None and c.start == minute:
                c.high = max(c.high, price)
                c.low = min(c.low, price)
                c.close = price
                c.volume += added
                return
            if c is not None:                        # its minute ended and the roll hasn't come yet: closed here
                st.ones.append(c)
                self._closed[symbol] = c
            st.forming = Candle(minute, 1, price, price, price, price, added, partial=st.first_seen > minute)

    # ---- on the clock (the candle loop) ----------------------------------- #
    def roll(self, now: float) -> Dict[str, Candle]:
        """Close every 1-minute candle whose minute ended by ``now`` (epoch seconds) - a quiet stock's too, on
        time - then make up each stock's 5-minute candle for every boundary passed since the last roll (:00,
        :05 ... New York time; at most CATCH_UP_5M after a sleep). A minute with no trade makes no candle, as
        with IBKR's TRADES bars. Returns {symbol: the 1-minute candle just closed}."""
        out: Dict[str, Candle] = {}
        with self._lock:
            if not self._day[0] <= now < self._day[1]:
                self._new_day(now)
            for symbol, c in list(self._closed.items()):
                if c.start + 60 <= now:
                    out[symbol] = c
                    del self._closed[symbol]
            for symbol, st in self._stocks.items():
                c = st.forming
                if c is not None and c.start + 60 <= now:
                    st.ones.append(c)
                    st.forming = None
                    out[symbol] = c
            self._rolled = max(self._rolled, now // 60 * 60)
            last = now // 300 * 300
            # the first roll makes up the boundary just passed: candles from before it may be there already
            first = self._boundary + 300 if self._boundary else last
            first = max(first, last - (CATCH_UP_5M - 1) * 300)
            b = first
            while b <= last:
                for st in self._stocks.values():
                    five = self._five(st, b - 300, b)
                    if five is not None:
                        st.fives.append(five)
                b += 300
            self._boundary = max(self._boundary, last)
        return out

    # ---- reading (any thread) --------------------------------------------- #
    def closed(self, symbol: str, minutes: int = 1, n: Optional[int] = None) -> List[Candle]:
        """``symbol``'s closed candles of ``minutes`` (1 or 5), oldest first - the last ``n`` when given."""
        with self._lock:
            st = self._stocks.get(symbol)
            if st is None:
                return []
            got = list(st.fives if minutes == 5 else st.ones)
        if n is None:
            return got
        return got[-n:] if n > 0 else []

    def latest(self, symbol: str, minutes: int = 1) -> Optional[Candle]:
        """``symbol``'s last closed candle of ``minutes`` (1 or 5)."""
        with self._lock:
            st = self._stocks.get(symbol)
            held = (st.fives if minutes == 5 else st.ones) if st is not None else None
            return held[-1] if held else None

    def forming(self, symbol: str, minutes: int = 1) -> Optional[Candle]:
        """A copy of ``symbol``'s candle still forming. The 5-minute one is the current interval's closed
        1-minute candles and the forming one together; None when there are none."""
        with self._lock:
            st = self._stocks.get(symbol)
            if st is None:
                return None
            if minutes != 5:
                return replace(st.forming) if st.forming is not None else None
            ref = max(self._rolled, st.forming.start if st.forming else 0.0, st.ones[-1].start if st.ones else 0.0)
            start = ref // 300 * 300
            return self._five(st, start, start + 300, with_forming=True)

    def symbols(self) -> List[str]:
        """The stocks with candles or a tick held."""
        with self._lock:
            return list(self._stocks)

    def keep(self, symbols: Iterable[str]) -> None:
        """Forget every stock not in ``symbols``: its stream ended, so its candles go, and a stream that starts
        again starts partial."""
        wanted = set(symbols)
        with self._lock:
            for symbol in [s for s in self._stocks if s not in wanted]:
                del self._stocks[symbol]
                self._closed.pop(symbol, None)

    def clear(self) -> None:
        """Forget everything - a new connection, or none."""
        with self._lock:
            self._stocks.clear()
            self._closed.clear()

    # ---- inside (under _lock) --------------------------------------------- #
    def _new_day(self, t: float) -> None:
        """Hold the New York day ``t`` falls in - its regular session as epochs, worked out once a day so a tick
        costs no time-zone sums - and forget every stock: yesterday's candles aren't today's."""
        day = dt.datetime.fromtimestamp(t, clock.NY).date()
        midnight = dt.datetime.combine(day, dt.time(0), clock.NY).timestamp()
        self._day = (midnight, dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), clock.NY).timestamp())
        if clock.is_trading_day(day):
            self._session = (dt.datetime.combine(day, clock.OPEN, clock.NY).timestamp(),
                             dt.datetime.combine(day, clock.regular_close_time(day), clock.NY).timestamp())
        else:
            self._session = (midnight, midnight)
        self._stocks.clear()
        self._closed.clear()

    @staticmethod
    def _five(st: _Stock, start: float, end: float, with_forming: bool = False) -> Optional[Candle]:
        """The 5-minute candle for [start, end) out of ``st``'s closed 1-minute candles in it (and with
        ``with_forming`` the one forming): the first open, the highest high, the lowest low, the last close and
        the summed volume. Partial when any of them is, or the stream began after ``start``."""
        parts: List[Candle] = []
        if with_forming and st.forming is not None and start <= st.forming.start < end:
            parts.append(st.forming)
        for c in reversed(st.ones):                  # newest first: stop at the first one before the interval
            if c.start < start:
                break
            if c.start < end:
                parts.append(c)
        if not parts:
            return None
        parts.reverse()
        return Candle(start, 5, parts[0].open, max(c.high for c in parts), min(c.low for c in parts),
                      parts[-1].close, sum(c.volume for c in parts),
                      partial=any(c.partial for c in parts) or st.first_seen > start)
