"""What the day has found so far, kept across a restart (``data/day_state.bin``).

A restart used to start the day over: the board came back empty, so the swing and
valuation setups of the morning's full scan were gone until the next morning and
the wide scan's until it ran again half an hour later; a setup already traded or
dismissed could be offered a second time; the pre-market levels of the gap check
were lost once the open had passed; and the wide scan waited a full spacing from
the start, however long ago the last one was.

So the engine saves, as the day goes:

* the plays on the board and the setups settled this session (engine/board.py),
* what the gap check saw before the open, and the session it ran for,
* the session the daily replay was started for, so a restart doesn't start it again,
* when the last wide scan finished, and the last scans' summaries,

and picks them up when it starts. Only the current session's state returns; a
file from another session, or one this version can't read, is ignored. Prices are
never restored - every scan still reads fresh candles, and an entry still needs a
current quote (engine._chase_check).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import pickle
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.enums import PlayStatus
from ..core.models import Play
from ..util import clock

log = logging.getLogger(__name__)

VERSION = 1


class DayStateFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> Optional[Dict[str, Any]]:
        try:
            data = pickle.loads(zlib.decompress(self.path.read_bytes()))
        except FileNotFoundError:
            return None
        except Exception:  # noqa: BLE001 - written by another version, or cut short: start the day afresh
            log.warning("the saved state of the day (%s) can't be read - starting afresh", self.path.name)
            return None
        return data if isinstance(data, dict) and data.get("version") == VERSION else None

    def write(self, payload: Dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(zlib.compress(pickle.dumps({"version": VERSION, **payload}, protocol=4), 1))
            tmp.replace(self.path)
        except Exception:  # noqa: BLE001 - a full disk must not stop a scan
            log.warning("could not save %s", self.path, exc_info=True)


def _as_play(saved: Any) -> Optional[Play]:
    """A saved play as today's Play class: a field added since it was saved gets its default."""
    try:
        return Play(**{f.name: getattr(saved, f.name) for f in dataclasses.fields(Play) if hasattr(saved, f.name)})
    except Exception:  # noqa: BLE001
        return None


class DayStateOps:
    """The engine's saving and restoring of the day's state (mixed into TradingEngine)."""

    #: the least time between two saves the scans ask for; a decision on a play is saved at once
    DAY_SAVE_S = 120.0

    def _init_day_state(self, path: Path) -> None:
        self._day_file = DayStateFile(path)
        self._day_lock = threading.Lock()
        self._day_dirty = False
        self._day_saved_at = float("-inf")
        self._last_wide_done: Optional[dt.datetime] = None       # wall clock, so it means something after a restart

    def _day_changed(self, now: bool = False) -> None:
        """Note that the board or the scans' state changed; ``now`` writes it without waiting."""
        self._day_dirty = True
        if now:
            self._save_day(force=True)

    def _save_day(self, force: bool = False) -> None:
        if not self._day_dirty or (not force and time.monotonic() - self._day_saved_at < self.DAY_SAVE_S):
            return
        with self._day_lock:
            self._day_dirty = False
            self._day_saved_at = time.monotonic()
            plays, settled = self.board.saved()
            gap_session = self._gappers_session
            self._day_file.write({
                "session": clock.session_date().isoformat(), "saved_at": clock.now_ny().isoformat(),
                "plays": plays, "settled": settled,
                "premarket": dict(self.scanner.premarket) if gap_session else {},
                "gappers_session": gap_session.isoformat() if gap_session else None,
                "replay_session": self._replay_session.isoformat() if self._replay_session else None,
                "last_wide_done": self._last_wide_done.isoformat() if self._last_wide_done else None,
                "last_scans": dict(self._last_scans),
            })

    def _restore_day(self) -> None:
        """Pick up what an earlier run of this session saved. Called once, when the engine starts."""
        saved = self._day_file.read()
        if not saved or saved.get("session") != clock.session_date().isoformat():
            return
        now = clock.now_ny()
        try:
            plays: List[Play] = [p for p in map(_as_play, saved.get("plays") or ()) if p is not None]
            back = self.board.restore(plays, saved.get("settled") or (), now.astimezone(dt.timezone.utc))
            active = {s.key for s in self.scanner.strategies}
            self.board.drop(lambda p: p.strategy in active and self.filters.allows(p),
                            "the filters or strategies changed while the app was off")
            self._size_plays([p for p in self.board.plays.values() if p.status is PlayStatus.PROPOSED])

            if saved.get("replay_session") == clock.session_date().isoformat():
                self._replay_session = clock.session_date()   # this session's replay has already been started
            if saved.get("gappers_session") == now.date().isoformat():
                self._gappers_session = now.date()
                self.scanner.premarket = dict(saved.get("premarket") or {})
            self._last_scans = {k: v for k, v in (saved.get("last_scans") or {}).items() if isinstance(v, dict)}
            done = saved.get("last_wide_done")
            if done:
                self._last_wide_done = dt.datetime.fromisoformat(done)
                age = max(0.0, (now - self._last_wide_done).total_seconds())
                # the wide scan keeps its rhythm: due a spacing after the last one, not after this start
                self._last_wide_at = time.monotonic() - age
        except Exception:  # noqa: BLE001 - never let a saved file stop the app from starting
            log.exception("could not restore the state of the day - starting afresh")
            self.board.clear()
            return
        on_offer = sum(p.status is PlayStatus.PROPOSED for p in self.board.plays.values())
        log.info("picked up the day where it left off: %d play(s) back on the board, %d still on offer (saved %s)",
                 back, on_offer, saved.get("saved_at"))
