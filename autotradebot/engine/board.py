"""The plays on the dashboard.

A scan replaces the plays for the symbols it looked at and leaves the rest, so
a fast cycle over the hot list doesn't wipe the swing and valuation setups the
morning's full scan found. Plays drop off once they expire.

One setup - a stock, strategy, direction and timeframe - is one play for the
session. When a scan finds it again the play keeps its id and counts another
confirmation; once it has been acted on or dismissed, the setup isn't offered
again until the next session. Opposite setups on one stock and timeframe are
both flagged as a conflict (see scanner/noise.py).

A day setup triggers on the last closed 5-minute candle, and the scans read
that candle several times over before the next one closes. With
``new_candle`` on, a day play counts a confirmation only when it shows on a
newer candle than the one it last counted - from any scan, the quick re-check
too - so "seen twice" means two candles running, the entry the replay tests.

Every change says why: a new setup was found, or a play left because its setup
no longer shows on the latest candles, it expired, or a filter or strategy switch
dropped it. The engine turns these into the dashboard's Autopilot notes.

The board outlives a restart: the engine saves ``saved()`` as the day goes
(engine/day_state.py) and hands it back to ``restore()`` when it starts, so the
setups the morning's full scan and the last wide scan found are on offer at once,
and a setup already acted on or dismissed today stays settled.
"""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass
from typing import Callable, Collection, Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd

from ..core.enums import PlayStatus
from ..core.models import Play
from ..util import clock

SetupKey = Tuple[str, str, str, str, dt.date]

#: the scans' candles (data/market_data.py INTRADAY_BAR), and the most of them a day setup may move on
#: between two counted sightings and still be seen "in a row" - a scan can land a candle late
CANDLE = dt.timedelta(minutes=5)
MAX_CANDLE_GAP = 2


def setup_key(p: Play) -> SetupKey:
    return (p.symbol, p.strategy, p.side.value, p.timeframe.value, clock.session_date(p.created_at))


@dataclass
class BoardChange:
    kind: str                     # added | removed
    play: Play
    why: str


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

    def holds(self, p: Play) -> bool:
        """``p`` itself is on the board - a scan's play replace() took, not one it left out."""
        with self._lock:
            return self._plays.get(p.id) is p

    def ranked(self) -> List[Play]:
        return sorted(self._plays.values(), key=lambda p: p.score, reverse=True)

    def replace(self, plays: Iterable[Play], scanned: Optional[Collection[str]] = None,
                now: Optional[dt.datetime] = None, keep: Optional[Callable[[Play], bool]] = None,
                confirm: bool = True, new_candle: bool = False) -> List[BoardChange]:
        """``scanned`` = the symbols the scan looked at; None = it looked at everything.
        ``keep`` spares the plays on those symbols that the scan doesn't re-evaluate
        (valuation setups, say). ``confirm`` = False when a quick re-check shouldn't count
        as another scan confirming a setup. ``new_candle``: a day play that knows its candle
        counts by candles instead (see _confirmations). Returns what was added and removed, and why."""
        now = now or dt.datetime.now(dt.timezone.utc)
        with self._lock:
            today = clock.session_date(now)
            self._settled = {k for k in self._settled if k[-1] == today}
            self._settled.update(setup_key(p) for p in self._plays.values() if p.status is not PlayStatus.PROPOSED)
            before = dict(self._plays)
            previous = {setup_key(p): p for p in before.values()}
            kept = {} if scanned is None else {
                pid: p for pid, p in before.items()
                if (p.symbol not in scanned or (keep is not None and keep(p)))
                and (p.expires_at is None or p.expires_at > now)}
            changes: List[BoardChange] = []
            for p in plays:
                key, old = setup_key(p), previous.get(setup_key(p))
                if key in self._settled:
                    if old is not None:
                        kept[old.id] = old                # still shown as executed / dismissed
                    continue
                if old is not None:
                    p.id, p.created_at = old.id, old.created_at
                    p.scan_run_id = p.scan_run_id or old.scan_run_id
                    p.confirmations = _confirmations(old, p, confirm, new_candle)
                else:
                    changes.append(BoardChange("added", p, p.rationale or "a new setup was found"))
                kept[p.id] = p
            _flag_conflicts(kept.values())
            changes += [BoardChange("removed", p, _why_gone(p, scanned, now))
                        for pid, p in before.items() if pid not in kept and p.status is PlayStatus.PROPOSED]
            self._plays = kept
            return changes

    def saved(self) -> Tuple[List[Play], List[SetupKey]]:
        """The plays and the session's settled setups, for the day's state file."""
        with self._lock:
            return list(self._plays.values()), sorted(self._settled)

    def restore(self, plays: Iterable[Play], settled: Iterable[SetupKey] = (),
                now: Optional[dt.datetime] = None) -> int:
        """Put back what an earlier run saved. Only this session's plays that haven't expired
        return; a setup acted on or dismissed stays settled even when its play has expired.
        Returns how many plays are back on the board."""
        now = now or dt.datetime.now(dt.timezone.utc)
        today = clock.session_date(now)
        with self._lock:
            mine = [p for p in plays if setup_key(p)[-1] == today]
            self._settled = {tuple(k) for k in settled if k[-1] == today}
            self._settled.update(setup_key(p) for p in mine if p.status is not PlayStatus.PROPOSED)
            self._plays = {p.id: p for p in mine if p.expires_at is None or p.expires_at > now}
            _flag_conflicts(self._plays.values())
            return len(self._plays)

    def drop(self, wanted: Callable[[Play], bool], why: str) -> List[BoardChange]:
        """Drop the plays that aren't wanted, saying ``why``."""
        with self._lock:
            gone = [p for p in self._plays.values() if not wanted(p)]
            self._plays = {pid: p for pid, p in self._plays.items() if wanted(p)}
        return [BoardChange("removed", p, why) for p in gone if p.status is PlayStatus.PROPOSED]

    def clear(self) -> None:
        with self._lock:
            self._plays = {}


def _confirmations(old: Play, new: Play, confirm: bool, new_candle: bool) -> int:
    """The confirmations of a setup found again. With ``new_candle``, a day play counts one only on a
    newer candle than the one it last counted (``counted_bar`` in its evidence), whichever scan saw it:
    a scan reading the same candle again is the same sighting, and a sighting more than MAX_CANDLE_GAP
    candles on starts the count again, like a setup that went away and came back. A swing play, or a
    play from before the candles were kept, counts each scan that may confirm, as before."""
    bar = _candle(new.evidence.get("bar_at"))
    if not (new_candle and new.is_day_trade and bar is not None):
        return old.confirmations + 1 if confirm else old.confirmations
    counted_at = old.evidence.get("counted_bar") or old.evidence.get("bar_at")
    counted = _candle(counted_at)
    if counted is not None and bar <= counted:
        new.evidence["counted_bar"] = counted_at          # the same candle again: nothing new was seen
        return old.confirmations
    new.evidence["counted_bar"] = new.evidence["bar_at"]
    if counted is None:                                    # saved before the candles were kept
        return old.confirmations + 1 if confirm else old.confirmations
    return old.confirmations + 1 if bar - counted <= MAX_CANDLE_GAP * CANDLE else 1


def _candle(value: object) -> Optional[pd.Timestamp]:
    """A candle's time kept in a play's evidence, timezone-aware; None when there is none to read."""
    if not value:
        return None
    try:
        at = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    return at.tz_localize("UTC") if at.tzinfo is None else at


def _why_gone(p: Play, scanned: Optional[Collection[str]], now: dt.datetime) -> str:
    if p.expires_at is not None and p.expires_at <= now:
        return "it expired - the setup is too old to act on"
    if scanned is None:
        return "the full scan didn't find the setup again"
    return "the setup no longer shows on the latest candles"


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
