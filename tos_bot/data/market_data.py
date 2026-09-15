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
_QUOTE_TTL_S = 20.0
_DAILY_CHUNK = 200


class NoDataSource(RuntimeError):
    """Raised when prices are needed but IB Gateway isn't connected."""


class PriceSource(Protocol):
    name: str

    @property
    def quotes_from_bars(self) -> bool: ...

    def history_many(self, requests: Mapping[str, Tuple[str, str]],
                     con_ids: Optional[Mapping[str, int]] = None,
                     end: Optional[dt.datetime] = None) -> Dict[str, pd.DataFrame]: ...

    def contract_details_many(self, symbols: Sequence[str]) -> Dict[str, Optional[dict]]: ...

    def get_quote(self, symbol: str) -> Quote: ...


def quote_from_price(symbol: str, last: float, volume: float = 0.0) -> Quote:
    """A quote with an estimated spread, for when only a last price is known."""
    spread = max(0.01, last * 0.0005)
    return Quote(symbol=symbol, bid=round(last - spread, 2), ask=round(last + spread, 2),
                 last=round(last, 4), volume=volume)


class MarketData:
    def __init__(self, bars: DailyBarStore) -> None:
        self.bars = bars
        self._source: Optional[PriceSource] = None
        self._lock = threading.Lock()
        self._intraday: Dict[str, Tuple[float, pd.DataFrame]] = {}
        self._quotes: Dict[str, Tuple[float, Quote]] = {}

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

    # ---- daily candles -------------------------------------------------- #
    def update_daily(self, symbols: Iterable[str], through: dt.date,
                     con_ids: Optional[Mapping[str, int]] = None,
                     progress: Optional[Callable[[int, int], None]] = None) -> int:
        """Bring each symbol's stored daily bars up to ``through``. Returns how
        many symbols needed a request."""
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

    def daily(self, symbols: Iterable[str]) -> Dict[str, pd.DataFrame]:
        return self.bars.frames(symbols)

    def daily_frame(self, symbol: str) -> Optional[pd.DataFrame]:
        return self.bars.frame(symbol)

    # ---- intraday candles and quotes ------------------------------------- #
    def intraday(self, symbols: Sequence[str],
                 con_ids: Optional[Mapping[str, int]] = None) -> Dict[str, pd.DataFrame]:
        now = time.monotonic()
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
