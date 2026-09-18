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

log = logging.getLogger(__name__)

INTRADAY_BAR = "5 mins"
INTRADAY_DURATION = "5 D"            # today plus four sessions, for relative volume
_INTRADAY_TTL_S = 60.0
REFRESH_DURATION = "1800 S"           # the quick re-check fetches the last half hour only
PREMARKET_DURATION = "1 D"            # the pre-open gap check: today's extended-hours candles
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


def quote_from_price(symbol: str, last: float, volume: float = 0.0) -> Quote:
    """A quote with an estimated spread, for when only a last price is known."""
    spread = max(0.01, last * 0.0005)
    return Quote(symbol=symbol, bid=round(last - spread, 2), ask=round(last + spread, 2),
                 last=round(last, 4), volume=volume)


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
        self._pruned_at = 0.0
        #: the morning scan and the movers report can want the same download; the second then finds it done
        self._daily_lock = threading.Lock()

    # ---- the source --------------------------------------------------- #
    def attach(self, source: PriceSource) -> None:
        with self._lock:
            self._source = source

    def detach(self) -> None:
        with self._lock:
            self._source = None
            self._intraday.clear()
            self._quotes.clear()

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
        """The broker's quote when it has real-time data, otherwise the close of
        the latest one-minute candle."""
        src = self.source
        if not src.quotes_from_bars:
            try:
                q = src.get_quote(symbol)
                if q.last or q.bid or q.ask:
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
        q = quote_from_price(symbol, float(last["close"]), float(last["volume"]))
        with self._lock:
            self._quotes[symbol] = (now, q)
        return q
