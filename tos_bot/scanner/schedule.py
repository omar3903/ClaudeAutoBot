"""When the scans run, and the settings that control it.

* The **full scan** looks at every US stock once a day, before the open, at the
  time set in Settings (between 04:00 and 09:00 ET, so it's done at least half
  an hour before the bell). Its hot list and buffer are for one trading session.
* The **cycle** rescans the hot list and the next buffer names every few
  minutes during the regular session.

If the app starts after the full-scan time (or on a day off) with no watchlist
for the coming session, the full scan runs as soon as prices are available, so
the board is never left empty.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, replace
from typing import Any, Dict, Mapping, Optional

from ..util import clock

EARLIEST_FULL_SCAN = dt.time(4, 0)
LATEST_FULL_SCAN = dt.time(9, 0)          # half an hour before the open


@dataclass(frozen=True)
class ScanSettings:
    premarket_time: str = "08:30"
    cycle_minutes: int = 5
    hot_list_size: int = 20
    sector_queue_size: int = 25

    LIMITS = {"cycle_minutes": (3, 5), "hot_list_size": (5, 50), "sector_queue_size": (10, 50)}

    @property
    def full_scan_time(self) -> dt.time:
        return dt.time.fromisoformat(self.premarket_time)

    def changed(self, **changes: Any) -> "ScanSettings":
        """A validated copy; raises ValueError with a message for the dashboard."""
        clean = {k: v for k, v in changes.items() if v is not None and k in asdict(self)}
        new = replace(self, **{k: (str(v) if k == "premarket_time" else int(v)) for k, v in clean.items()})
        try:
            t = new.full_scan_time
        except ValueError:
            raise ValueError("Full-scan time must look like 08:30.") from None
        if not EARLIEST_FULL_SCAN <= t <= LATEST_FULL_SCAN:
            raise ValueError("The full scan runs before the open: pick a time from 04:00 to 09:00 ET.")
        for key, (lo, hi) in self.LIMITS.items():
            if not lo <= getattr(new, key) <= hi:
                raise ValueError(f"{key.replace('_', ' ').capitalize()} must be between {lo} and {hi}.")
        return replace(new, premarket_time=t.strftime("%H:%M"))

    def as_dict(self) -> dict:
        return asdict(self)

    def clamped(self) -> "ScanSettings":
        """A copy with its numbers pulled inside LIMITS."""
        return replace(self, **self._clamp(asdict(self)))

    @classmethod
    def _clamp(cls, values: Mapping[str, Any]) -> Dict[str, Any]:
        out = dict(values)
        for key, (lo, hi) in cls.LIMITS.items():
            try:
                if out.get(key) is not None:
                    out[key] = min(hi, max(lo, int(out[key])))
            except (TypeError, ValueError):
                out.pop(key)
        return out

    @classmethod
    def load(cls, saved: Optional[Mapping[str, Any]], defaults: "ScanSettings") -> "ScanSettings":
        """The saved settings over the defaults; values saved under older limits are pulled into range."""
        defaults = defaults.clamped()
        try:
            return defaults.changed(**cls._clamp(dict(saved or {})))
        except (ValueError, TypeError):
            return defaults


def watchlist_session(now: dt.datetime) -> dt.date:
    """The trading session a watchlist built now is for: today until the
    close on a trading day, otherwise the next trading day."""
    today = now.date()
    if clock.is_trading_day(today) and now.time() < clock.regular_close_time(today):
        return today
    return clock.next_trading_day(today)


def last_completed_session(now: dt.datetime) -> dt.date:
    """The newest session whose daily candle is final."""
    today = now.date()
    if clock.is_trading_day(today) and now.time() >= clock.regular_close_time(today):
        return today
    return clock.prev_trading_day(today)


def next_full_scan_at(now: dt.datetime, settings: ScanSettings, have_session: Optional[dt.date]) -> dt.datetime:
    """When the next full scan is scheduled (in the past = it's due now)."""
    session = watchlist_session(now)
    day = clock.next_trading_day(session) if have_session == session else session
    return dt.datetime.combine(day, settings.full_scan_time, tzinfo=clock.NY)


def full_scan_due(now: dt.datetime, settings: ScanSettings, have_session: Optional[dt.date],
                  have_any: bool) -> bool:
    """``have_session`` is the session of the watchlist in hand (None if there
    isn't one); ``have_any`` says whether any watchlist exists at all."""
    session = watchlist_session(now)
    if have_session == session:
        return False
    in_window = (session == now.date() and settings.full_scan_time <= now.time()
                 < clock.regular_close_time(session))
    return in_window or not have_any
