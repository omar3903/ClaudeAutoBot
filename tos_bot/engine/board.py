"""The plays on the dashboard.

A scan replaces the plays for the symbols it looked at and leaves the rest, so
a fast cycle over the hot list doesn't wipe the swing and valuation setups the
morning's full scan found. Plays drop off once they expire.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Callable, Collection, Dict, Iterable, List, Optional

from ..core.models import Play


class PlayBoard:
    def __init__(self) -> None:
        self._plays: Dict[str, Play] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._plays)

    def get(self, play_id: str) -> Optional[Play]:
        return self._plays.get(play_id)

    @property
    def plays(self) -> Dict[str, Play]:
        return dict(self._plays)

    def ranked(self) -> List[Play]:
        return sorted(self._plays.values(), key=lambda p: p.score, reverse=True)

    def replace(self, plays: Iterable[Play], scanned: Optional[Collection[str]] = None,
                now: Optional[dt.datetime] = None) -> None:
        """``scanned`` = the symbols the scan looked at; None = it looked at everything."""
        now = now or dt.datetime.now(dt.timezone.utc)
        with self._lock:
            kept = {} if scanned is None else {
                pid: p for pid, p in self._plays.items()
                if p.symbol not in scanned and (p.expires_at is None or p.expires_at > now)}
            kept.update((p.id, p) for p in plays)
            self._plays = kept

    def keep_only(self, wanted: Callable[[Play], bool]) -> int:
        """Drop the plays that aren't wanted; returns how many went."""
        with self._lock:
            before = len(self._plays)
            self._plays = {pid: p for pid, p in self._plays.items() if wanted(p)}
            return before - len(self._plays)

    def clear(self) -> None:
        with self._lock:
            self._plays = {}
