"""Daily candles for the whole universe, kept on disk and topped up incrementally.

A stock's past daily bars never change, so each symbol is downloaded in full
once (a year of history) and afterwards only the sessions it's missing - one
small request per symbol per trading day, and none when it's already current.
Only completed sessions are stored; today's partial bar is built from intraday
candles when a strategy needs it.

Only recently used frames stay in memory; for the rest just the date of the
last stored session is remembered.
"""

from __future__ import annotations

import datetime as dt
import logging
import pickle
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Iterable, Optional

import pandas as pd

log = logging.getLogger(__name__)

FULL_HISTORY = "1 Y"
KEEP_SESSIONS = 300           # enough for a 200-day average and a 52-week range
_FRAMES_IN_MEMORY = 800       # the hot list, the buffers and the swing leaders


class DailyBarStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._frames: "OrderedDict[str, pd.DataFrame]" = OrderedDict()
        self._last: Dict[str, Optional[dt.date]] = {}
        self._lock = threading.Lock()

    def frame(self, symbol: str) -> Optional[pd.DataFrame]:
        with self._lock:
            if symbol in self._frames:
                self._frames.move_to_end(symbol)
                return self._frames[symbol]
        try:
            frame = pd.read_pickle(self._path(symbol))
        except (OSError, ValueError, EOFError, pickle.UnpicklingError):
            self._last[symbol] = None
            return None
        self._remember(symbol, frame)
        return frame

    def frames(self, symbols: Iterable[str]) -> Dict[str, pd.DataFrame]:
        return {s: f for s in symbols if (f := self.frame(s)) is not None and len(f)}

    def last_session(self, symbol: str) -> Optional[dt.date]:
        if symbol not in self._last:
            self.frame(symbol)
        return self._last.get(symbol)

    def duration_needed(self, symbol: str, through: dt.date) -> Optional[str]:
        """The IBKR duration that brings ``symbol`` up to ``through``, or None if it's current."""
        last = self.last_session(symbol)
        if last is None:
            return FULL_HISTORY
        if last >= through:
            return None
        gap_days = (through - last).days
        return FULL_HISTORY if gap_days > 250 else f"{gap_days + 3} D"

    def merge(self, symbol: str, bars: pd.DataFrame, through: dt.date) -> pd.DataFrame:
        """Add freshly downloaded bars (dropping any session after ``through``) and save."""
        new = bars[bars.index.date <= through]
        old = self.frame(symbol)
        combined = new if old is None else pd.concat([old, new])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index().tail(KEEP_SESSIONS)
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self._path(symbol).with_suffix(".tmp")
        combined.to_pickle(tmp)
        tmp.replace(self._path(symbol))
        self._remember(symbol, combined)
        return combined

    def _remember(self, symbol: str, frame: pd.DataFrame) -> None:
        with self._lock:
            self._frames[symbol] = frame
            self._frames.move_to_end(symbol)
            while len(self._frames) > _FRAMES_IN_MEMORY:
                self._frames.popitem(last=False)
            self._last[symbol] = frame.index[-1].date() if len(frame) else None

    def _path(self, symbol: str) -> Path:
        return self.directory / f"{symbol.replace(' ', '_')}.pkl"
