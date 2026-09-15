"""The plays on the dashboard.

A scan replaces the plays for the symbols it looked at and leaves the rest, so
a fast cycle over the hot list doesn't wipe the swing and valuation setups the
morning's full scan found. Plays drop off once they expire.

One setup - a stock, strategy, direction and timeframe - is one play for the
session. When a scan finds it again the play keeps its id and counts another
confirmation; once it has been acted on or dismissed, the setup isn't offered
again until the next session. Opposite setups on one stock and timeframe are
both flagged as a conflict (see scanner/noise.py).
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Callable, Collection, Dict, Iterable, List, Optional, Set, Tuple

from ..core.enums import PlayStatus
from ..core.models import Play
from ..util import clock

SetupKey = Tuple[str, str, str, str, dt.date]


def setup_key(p: Play) -> SetupKey:
    return (p.symbol, p.strategy, p.side.value, p.timeframe.value, clock.session_date(p.created_at))


class PlayBoard:
    def __init__(self) -> None:
        self._plays: Dict[str, Play] = {}
        self._settled: Set[SetupKey] = set()      # setups acted on or dismissed this session
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
            today = clock.session_date(now)
            self._settled = {k for k in self._settled if k[-1] == today}
            self._settled.update(setup_key(p) for p in self._plays.values() if p.status is not PlayStatus.PROPOSED)
            previous = {setup_key(p): p for p in self._plays.values()}
            kept = {} if scanned is None else {
                pid: p for pid, p in self._plays.items()
                if p.symbol not in scanned and (p.expires_at is None or p.expires_at > now)}
            for p in plays:
                key, old = setup_key(p), previous.get(setup_key(p))
                if key in self._settled:
                    if old is not None:
                        kept[old.id] = old                # still shown as executed / dismissed
                    continue
                if old is not None:
                    p.id, p.created_at, p.confirmations = old.id, old.created_at, old.confirmations + 1
                kept[p.id] = p
            _flag_conflicts(kept.values())
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


def _flag_conflicts(plays: Iterable[Play]) -> None:
    proposed = [p for p in plays if p.status is PlayStatus.PROPOSED]
    sides: Dict[Tuple[str, str], set] = {}
    for p in proposed:
        sides.setdefault((p.symbol, p.timeframe.value), set()).add(p.side)
    for p in proposed:
        split = len(sides[(p.symbol, p.timeframe.value)]) > 1
        if split and "conflict" not in p.noise:
            p.noise.append("conflict")
        elif not split and "conflict" in p.noise:
            p.noise.remove("conflict")
