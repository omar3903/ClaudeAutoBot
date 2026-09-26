"""Prices for the scanner, the strategies, the simulator and the automatic exits.

Everything comes from one *source*: the connected IB Gateway (or a test
double). Daily candles are kept on disk by :class:`DailyBarStore` and topped up
once a day; intraday candles and bar-based quotes are cached briefly so the same
request isn't sent twice in quick succession. Without a source there is no
data - nothing is ever made up.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Callable, Dict, Iterable, Mapping, Optional, Protocol, Sequence, Tuple

import pandas as pd

from ..core.models import Quote
from .bars import DailyBarStore
from .candles import LiveCandles
from .streams import StreamManager

log = logging.getLogger(__name__)

INTRADAY_BAR = "5 mins"
INTRADAY_DURATION = "5 D"            # today plus four sessions, for relative volume
_INTRADAY_TTL_S = 60.0
REFRESH_DURATION = "1800 S"           # the quick re-check fetches the last half hour only
PREMARKET_DURATION = "1 D"            # the pre-open gap check: today's extended-hours candles
SHOWN_DURATION = "3600 S"             # a price to show looks back an hour: long enough for a thin stock's last trade
_QUOTE_TTL_S = 20.0
#: candles and quotes nothing has asked for in this long are let go - the app runs for days
_CACHE_KEEP_S = 1800.0
_DAILY_CHUNK = 200
_DEEP_CHUNK = 50             # the long history comes in smaller batches, so its progress line moves


class NoDataSource(RuntimeError):
    """Raised when prices are needed but IB Gateway isn't connected."""


class PriceSource(Protocol):
    name: str

    @property
    def quotes_from_bars(self) -> bool: ...

    def history_many(self, requests: Mapping[str, Tuple[str, str]],
                     con_ids: Optional[Mapping[str, int]] = None,
                     end: Optional[dt.datetime] = None, rth: bool = True) -> Dict[str, pd.DataFrame]: ...

    def contract_details_many(self, symbols: Sequence[str]) -> Dict[str, Optional[dict]]: ...

    def get_quote(self, symbol: str) -> Quote: ...


def quote_from_price(symbol: str, last: float, volume: float = 0.0, ts: Optional[dt.datetime] = None) -> Quote:
    """A quote with an estimated spread, for when only a last price is known. ``ts``: when the price is
    from (a candle's time) - now when not given."""
    spread = max(0.01, last * 0.0005)
    extra = {"ts": ts} if ts is not None else {}
    return Quote(symbol=symbol, bid=round(last - spread, 2), ask=round(last + spread, 2),
                 last=round(last, 4), volume=volume, **extra)


def _candle_time(frame: pd.DataFrame) -> dt.datetime:
    """When the last candle of ``frame`` is from, as an aware datetime."""
    at = frame.index[-1]
    at = at.to_pydatetime() if hasattr(at, "to_pydatetime") else at
    return at if at.tzinfo else at.replace(tzinfo=dt.timezone.utc)


class MarketData:
    def __init__(self, bars: DailyBarStore, deep: Optional[DailyBarStore] = None) -> None:
        """``deep``: the research store - several years of daily candles for the stocks the replay
        runs on (see deepen_daily). The scans never read it."""
        self.bars = bars
        self.deep = deep
        self._source: Optional[PriceSource] = None
        self._lock = threading.Lock()
        self._intraday: Dict[str, Tuple[float, pd.DataFrame]] = {}
        self._quotes: Dict[str, Tuple[float, Quote]] = {}
        #: prices fetched only to be shown, pre-market and after-hours included. Kept apart from _quotes, which
        #: the exits and the entry checks trade on through quote(): an after-hours print must never move a stop
        self._shown: Dict[str, Tuple[float, Quote]] = {}
        self._shown_asked: Dict[str, float] = {}      # when the broker was last asked for each one
        self._pruned_at = 0.0
        #: the morning scan and the movers report can want the same download; the second then finds it done
        self._daily_lock = threading.Lock()
        #: 1- and 5-minute candles built from the streamed ticks, for speed and show - never for proof (candles.py)
        self.candles = LiveCandles()
        #: the real-time streams the source holds, when it can (streams.py) - quote() serves a fresh one
        self.streams = StreamManager(self)

    # ---- the source --------------------------------------------------- #
    def attach(self, source: PriceSource) -> None:
        with self._lock:
            self._source = source
        self.streams.reset()

    def detach(self) -> None:
        with self._lock:
            self._source = None
            self._intraday.clear()
            self._quotes.clear()
            self._shown.clear()
            self._shown_asked.clear()
        self.candles.clear()
        self.streams.reset()

    @property
    def source(self) -> PriceSource:
        src = self._source
        if src is None:
            raise NoDataSource("IB Gateway isn't connected, so there are no prices to scan with.")
        return src

    @property
    def attached(self) -> bool:
        return self._source is not None

    @property
    def source_name(self) -> str:
        return self._source.name if self._source is not None else "none"

    @property
    def delayed(self) -> bool:
        return bool(self._source is not None and self._source.quotes_from_bars)

    @property
    def refused(self) -> str:
        """Why the source is refusing prices right now (IBKR: the login is active elsewhere) - the
        scans read nothing and no quote can be had. Empty when prices flow, delayed or not."""
        return str(getattr(self._source, "candles_refused", "") or "") if self._source is not None else ""

    @property
    def data_problem(self) -> str:
        """Why the price source isn't sending real-time data, in words (see IbkrBroker.market_data_reason)."""
        return str(getattr(self._source, "market_data_reason", "") or "") if self._source is not None else ""

    # ---- daily candles -------------------------------------------------- #
    def update_daily(self, symbols: Iterable[str], through: dt.date,
                     con_ids: Optional[Mapping[str, int]] = None,
                     progress: Optional[Callable[[int, int], None]] = None) -> int:
        """Bring each symbol's stored daily bars up to ``through``. Returns how
        many symbols needed a request."""
        with self._daily_lock:
            plan = {s: d for s in symbols if (d := self.bars.duration_needed(s, through))}
            done = 0
            items = list(plan.items())
            for i in range(0, len(items), _DAILY_CHUNK):
                chunk = dict(items[i:i + _DAILY_CHUNK])
                got = self.source.history_many({s: ("1 day", d) for s, d in chunk.items()}, con_ids)
                for symbol, frame in got.items():
                    self.bars.merge(symbol, frame, through)
                done += len(chunk)
                if progress:
                    progress(done, len(items))
            return len(plan)

    def deepen_daily(self, symbols: Iterable[str], through: dt.date,
                     con_ids: Optional[Mapping[str, int]] = None,
                     progress: Optional[Callable[[int, int], None]] = None) -> int:
        """Bring the research store's long history of ``symbols`` up to ``through``. A stock it has
        never seen costs one request for the whole history; after that the live store's candles top
        it up for nothing. Returns how many stocks needed a request."""
        if self.deep is None:
            return 0
        with self._daily_lock:
            plan: Dict[str, str] = {}
            for s in dict.fromkeys(symbols):
                need = self.deep.duration_needed(s, through)
                if need is None:
                    continue
                last, live = self.deep.last_session(s), self.bars.frame(s)
                if last is not None and live is not None and len(live) and live.index[0].date() <= last:
                    self.deep.merge(s, live[live.index.date > last], through)      # the live store covers the gap
                    continue
                plan[s] = need
            items, done = list(plan.items()), 0
            if progress and items:
                progress(0, len(items))                   # say what is happening before the first batch is in
            for i in range(0, len(items), _DEEP_CHUNK):
                chunk = dict(items[i:i + _DEEP_CHUNK])
                got = self.source.history_many({s: ("1 day", d) for s, d in chunk.items()}, con_ids)
                for symbol, frame in got.items():
                    self.deep.merge(symbol, frame, through)
                done += len(chunk)
                if progress:
                    progress(done, len(items))
            return len(plan)

    def deep_frame(self, symbol: str) -> Optional[pd.DataFrame]:
        """A stock's longest daily history: the research store's, carried forward with the live
        store's latest candles - or just the live store's when there is no long one to join."""
        live = self.bars.frame(symbol)
        deep = self.deep.frame(symbol) if self.deep is not None else None
        if deep is None or not len(deep):
            return live
        if live is None or not len(live) or live.index[-1] <= deep.index[-1]:
            return deep
        if live.index[0] > deep.index[-1]:
            return live                                   # a hole between them: the long history is too old to join
        return pd.concat([deep, live[live.index > deep.index[-1]]])

    def daily(self, symbols: Iterable[str]) -> Dict[str, pd.DataFrame]:
        return self.bars.frames(symbols)

    def daily_frame(self, symbol: str) -> Optional[pd.DataFrame]:
        return self.bars.frame(symbol)

    # ---- intraday candles and quotes ------------------------------------- #
    def intraday(self, symbols: Sequence[str],
                 con_ids: Optional[Mapping[str, int]] = None) -> Dict[str, pd.DataFrame]:
        now = time.monotonic()
        self._prune(now)
        out, todo = {}, []
        for s in dict.fromkeys(symbols):
            hit = self._intraday.get(s)
            if hit and now - hit[0] < _INTRADAY_TTL_S:
                out[s] = hit[1]
            else:
                todo.append(s)
        if todo:
            got = self.source.history_many({s: (INTRADAY_BAR, INTRADAY_DURATION) for s in todo}, con_ids)
            with self._lock:
                for s, frame in got.items():
                    self._intraday[s] = (now, frame)
            out.update(got)
        return out

    def _prune(self, now: float) -> None:
        """Forget the candles and quotes nothing has asked for in a while - otherwise every stock the
        cycles ever looked at would stay in memory for as long as the app runs."""
        if now - self._pruned_at < 300:
            return
        with self._lock:
            self._pruned_at = now
            self._intraday = {s: hit for s, hit in self._intraday.items() if now - hit[0] < _CACHE_KEEP_S}
            self._quotes = {s: hit for s, hit in self._quotes.items() if now - hit[0] < _CACHE_KEEP_S}
            self._shown = {s: hit for s, hit in self._shown.items() if now - hit[0] < _CACHE_KEEP_S}
            self._shown_asked = {s: at for s, at in self._shown_asked.items() if now - at < _CACHE_KEEP_S}

    def refresh_intraday(self, symbols: Sequence[str],
                         con_ids: Optional[Mapping[str, int]] = None) -> Dict[str, pd.DataFrame]:
        """Brings the cached 5-minute candles of ``symbols`` up to date by fetching only the
        last half hour and merging it in - light enough to repeat every few seconds. A stock
        without cached candles gets its full history first."""
        wanted = list(dict.fromkeys(symbols))
        with self._lock:
            cached = {s: self._intraday[s][1] for s in wanted if s in self._intraday}
        out = self.intraday([s for s in wanted if s not in cached], con_ids) if len(cached) < len(wanted) else {}
        if cached:
            now = time.monotonic()
            recent = self.source.history_many({s: (INTRADAY_BAR, REFRESH_DURATION) for s in cached}, con_ids)
            with self._lock:
                for s, frame in recent.items():
                    if frame is None or not len(frame) or s not in cached:
                        continue
                    merged = pd.concat([cached[s], frame])
                    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
                    self._intraday[s] = (now, merged)
                    out[s] = merged
            for s, frame in cached.items():
                out.setdefault(s, frame)
        return out

    def premarket(self, symbols: Sequence[str],
                  con_ids: Optional[Mapping[str, int]] = None) -> Dict[str, pd.DataFrame]:
        """Today's pre-market 5-minute candles (before 09:30 ET) for ``symbols`` - one request
        each, made once a day by the gap check. Stocks with no pre-market trade are left out."""
        wanted = list(dict.fromkeys(symbols))
        if not wanted:
            return {}
        got = self.source.history_many({s: (INTRADAY_BAR, PREMARKET_DURATION) for s in wanted}, con_ids, rth=False)
        out: Dict[str, pd.DataFrame] = {}
        for symbol, frame in got.items():
            if frame is None or not len(frame):
                continue
            ny = frame.index.tz_convert("America/New_York")
            day = ny.date.max()
            early = (ny.date == day) & ((ny.hour < 9) | ((ny.hour == 9) & (ny.minute < 30)))
            if early.any():
                out[symbol] = frame[early]
        return out

    def quote(self, symbol: str) -> Quote:
        """The broker's quote when it has real-time data - its stream's latest when that came in the last
        StreamManager.FRESH_S seconds, a snapshot otherwise - else the close of the latest one-minute candle."""
        src = self.source
        if not src.quotes_from_bars:
            q = self.streams.fresh(symbol, src)
            if q is not None:
                return q                    # never kept in _quotes: the fallback below serves snapshots only
            try:
                q = src.get_quote(symbol)
                if q.last or q.bid or q.ask:
                    with self._lock:
                        self._quotes[symbol] = (time.monotonic(), q)     # kept for last_seen - never served stale
                    return q
            except Exception as e:  # noqa: BLE001
                log.debug("quote for %s failed: %s", symbol, e)
        now = time.monotonic()
        hit = self._quotes.get(symbol)
        if hit and now - hit[0] < _QUOTE_TTL_S:
            return hit[1]
        frame = src.history_many({symbol: ("1 min", "1800 S")}).get(symbol)
        if frame is None or not len(frame):
            raise RuntimeError(f"no price for {symbol}")
        last = frame.iloc[-1]
        q = quote_from_price(symbol, float(last["close"]), float(last["volume"]), ts=_candle_time(frame))
        with self._lock:
            self._quotes[symbol] = (now, q)
        return q

    # ---- the latest price, and when it's from ---------------------------- #
    def last_seen(self, symbol: str) -> Optional[Tuple[float, dt.datetime, float]]:
        """The newest price the app holds for ``symbol`` - its latest quote, its latest streamed quote, its
        latest price fetched to be shown or the close of its latest 5-minute candle, whichever is from later -
        as (price, when it's from, how many seconds ago it was fetched). None when it holds none. Asks nothing
        of the broker. For the dashboard only: it can be a pre-market or after-hours price, which nothing may
        act on."""
        now, best = time.monotonic(), None
        seen = [(hit[1], now - hit[0]) for hit in (self._quotes.get(symbol), self._shown.get(symbol))
                if hit is not None]
        streamed = self.streams.latest(symbol)
        if streamed is not None:
            seen.append(streamed)                         # its age: since the stream's last tick
        for q, age in seen:
            price = float(q.last or q.mid or 0.0)
            at = q.ts if q.ts.tzinfo else q.ts.replace(tzinfo=dt.timezone.utc)
            # the same candle fetched twice: the later fetch, so the age says how fresh it really is
            if price > 0 and (best is None or at > best[1] or (at == best[1] and age < best[2])):
                best = (price, at, age)
        bars = self._intraday.get(symbol)
        if bars is not None and bars[1] is not None and len(bars[1]):
            at = _candle_time(bars[1])
            if best is None or at > best[1]:
                best = (float(bars[1]["close"].iloc[-1]), at, now - bars[0])
        return best

    def refresh_prices(self, symbols: Sequence[str], con_ids: Optional[Mapping[str, int]] = None,
                       limit: int = 120) -> int:
        """Fetch a fresh price for each of ``symbols`` now - the close of its latest one-minute candle,
        pre-market and after-hours included, in one batch (a snapshot quote each would take a request
        apiece) - for the dashboard's Refresh. Shown, never traded on (see _shown). The first ``limit``
        only. Returns how many came back."""
        wanted = list(dict.fromkeys(symbols))[:limit]
        if not wanted:
            return 0
        return self._fetch_shown(wanted, con_ids)

    def price_now(self, symbol: str, con_id: Optional[int] = None,
                  max_age_s: float = 15.0) -> Optional[Tuple[float, dt.datetime, float]]:
        """``symbol``'s latest price for a panel that shows it, as last_seen gives it: the latest trade,
        pre-market and after-hours included, fetched now unless the broker was asked in the last
        ``max_age_s`` - so any number of open panels cost IBKR one request per stock that often at most.
        A newer price the app already holds still wins. None when there is none. A broker that fails
        isn't the caller's problem: what the app holds is returned."""
        now = time.monotonic()
        with self._lock:
            ask = now - self._shown_asked.get(symbol, now - max_age_s) >= max_age_s
            if ask:
                self._shown_asked[symbol] = now          # claimed before asking, so a second panel doesn't ask too
        if ask:
            try:
                self._fetch_shown([symbol], {symbol: con_id} if con_id else None)
            except Exception as e:  # noqa: BLE001
                log.debug("price for %s unavailable: %s", symbol, e)
        return self.last_seen(symbol)

    def _fetch_shown(self, symbols: Sequence[str], con_ids: Optional[Mapping[str, int]] = None) -> int:
        """The close of each symbol's latest one-minute candle, pre-market and after-hours included, into
        the store of prices to show. Returns how many came back."""
        asked = time.monotonic()
        with self._lock:
            self._shown_asked.update(dict.fromkeys(symbols, asked))
        got = self.source.history_many({s: ("1 min", SHOWN_DURATION) for s in symbols}, con_ids, rth=False)
        now = time.monotonic()
        fresh = {}
        for symbol, frame in got.items():
            if frame is None or not len(frame):
                continue
            last = frame.iloc[-1]
            fresh[symbol] = (now, quote_from_price(symbol, float(last["close"]), float(last["volume"]),
                                                   ts=_candle_time(frame)))
        with self._lock:
            self._shown.update(fresh)
        return len(fresh)
