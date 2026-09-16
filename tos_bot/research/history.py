"""Five-minute candles for the strategy replay, kept on disk.

The replay needs several weeks of 5-minute candles for the stocks it replays.
They're downloaded from IBKR a few sessions at a time and saved per stock, so a
session already on disk is never asked for again - a replay run a week later
only fetches the week since.
"""

from __future__ import annotations

import datetime as dt
import logging
import pickle
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional

import pandas as pd

from ..util import clock

log = logging.getLogger(__name__)

NY = "America/New_York"
BAR_SIZE = "5 mins"
SESSIONS_PER_REQUEST = 5          # IBKR serves 5-minute history up to a week per request
#: how long a download waits for IB Gateway to come back (its nightly restart takes a minute or two)
GATEWAY_WAIT_S = 900.0
GATEWAY_POLL_S = 15.0


class IntradayHistory:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def load(self, source, symbols: Iterable[str], sessions: int, con_ids: Optional[Mapping[str, int]] = None,
             today: Optional[dt.date] = None,
             progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, pd.DataFrame]:
        """The last ``sessions`` completed sessions of 5-minute candles per symbol,
        downloading only the sessions that aren't on disk yet."""
        today = today or clock.session_date()
        wanted = sorted(clock.last_n_sessions(clock.prev_trading_day(today), sessions))
        symbols = list(dict.fromkeys(symbols))
        frames = {s: self._read(s) for s in symbols}
        chunks = [wanted[max(0, end - SESSIONS_PER_REQUEST):end]
                  for end in range(len(wanted), 0, -SESSIONS_PER_REQUEST)]
        for n, chunk in enumerate(chunks, 1):
            missing = [s for s in symbols if not set(chunk) <= _sessions_in(frames[s])]
            if missing:
                close = dt.datetime.combine(chunk[-1], clock.regular_close_time(chunk[-1]))
                got = _through_gateway_drops(source, lambda: source.history_many(
                    {s: (BAR_SIZE, f"{len(chunk)} D") for s in missing}, con_ids,
                    end=pd.Timestamp(close, tz=NY).to_pydatetime()))
                for symbol, frame in got.items():
                    frames[symbol] = _merge(frames[symbol], frame)
                    self._write(symbol, frames[symbol])
            if progress:
                progress(n, len(chunks))
        first = pd.Timestamp(wanted[0], tz=NY)
        return {s: f[f.index >= first] for s, f in frames.items() if f is not None and len(f)}

    def stored(self, symbol: str) -> Optional[pd.DataFrame]:
        """The candles already on disk, without asking IBKR."""
        return self._read(symbol)

    def _path(self, symbol: str) -> Path:
        return self.directory / f"{symbol.replace(' ', '_')}.pkl"

    def _read(self, symbol: str) -> Optional[pd.DataFrame]:
        try:
            return pd.read_pickle(self._path(symbol))
        except (OSError, ValueError, EOFError, pickle.UnpicklingError):
            return None

    def _write(self, symbol: str, frame: pd.DataFrame) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self._path(symbol).with_suffix(".tmp")
        frame.to_pickle(tmp)
        tmp.replace(self._path(symbol))


def _through_gateway_drops(source, request: Callable[[], Dict[str, pd.DataFrame]]) -> Dict[str, pd.DataFrame]:
    """Run a download. If IB Gateway drops part way - its nightly restart - wait for it to come back and
    ask again; an error while it's connected is a real one."""
    waited = 0.0
    while True:
        try:
            return request()
        except Exception:
            if getattr(source, "is_connected", True) or waited >= GATEWAY_WAIT_S:
                raise
            if not waited:
                log.warning("replay: IB Gateway dropped - waiting up to %d minutes for it", GATEWAY_WAIT_S // 60)
            time.sleep(GATEWAY_POLL_S)
            waited += GATEWAY_POLL_S


def _sessions_in(frame: Optional[pd.DataFrame]) -> set:
    return set(frame.index.date) if frame is not None and len(frame) else set()


def _merge(old: Optional[pd.DataFrame], new: pd.DataFrame) -> pd.DataFrame:
    combined = new if old is None else pd.concat([old, new])
    return combined[~combined.index.duplicated(keep="last")].sort_index()


def replay_symbols(watchlist) -> Dict[str, List[str]]:
    """Which stocks the replay uses: day-trade setups on the hot list and the kept
    buffer names (their 5-minute candles cost IBKR requests), swing setups on every
    watchlist stock (their daily candles are already on disk)."""
    if watchlist is None:
        return {"intraday": [], "swing": []}
    intraday = list(dict.fromkeys(watchlist.hot_symbols() + watchlist.kept_symbols()))
    swing = list(dict.fromkeys(intraday + [c.symbol for queue in watchlist.queues.values() for c in queue]))
    return {"intraday": intraday, "swing": swing}
