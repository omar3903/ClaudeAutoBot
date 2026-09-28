"""When the scans run, and the settings that control it.

* The **full scan** looks at every US stock once a day, before the open, at the
  time set in Settings (between 04:00 and 09:00 ET, so it's done at least half
  an hour before the bell). Its hot list and buffer are for one trading session.
* The **gap check** reads the hot list and buffer names' pre-market candles once,
  shortly before the open, and moves the stocks gapping on volume into the hot
  list (Aziz's gappers watchlist).
* The **cycle** rescans the hot list and the next buffer names every few
  minutes during the regular session.
* The **wide scan** reads every liquid stock's 5-minute candles every half hour
  or so (settable; 0 switches it off) and runs the setups on all of them, so a
  stock that heats up mid-session is seen even if the morning's ranking had it
  cold. It costs one request per stock, so it runs no more than every 15 minutes.

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
EARLIEST_GAP_CHECK = dt.time(8, 0)
LATEST_GAP_CHECK = dt.time(9, 25)         # once, before the bell
OPEN = dt.time(9, 30)
_TIMES = ("premarket_time", "gapper_time")


@dataclass(frozen=True)
class ScanSettings:
    premarket_time: str = "08:30"
    gapper_time: str = "09:15"
    cycle_minutes: int = 5
    hot_list_size: int = 20
    sector_queue_size: int = 25
    wide_minutes: int = 30                # the wide scan's spacing; 0 = off, else 15-120
    wide_stocks: int = 0                  # the hottest N of the full scan's liquid stocks; 0 = all of them
    movers: int = 10                      # today's biggest movers hold hot-list slots after each wide scan (0 = off)
    yesterday_movers: int = 10            # ...and the last session's biggest movers, from the full scan (0 = off)

    LIMITS = {"cycle_minutes": (3, 5), "hot_list_size": (5, 50), "sector_queue_size": (10, 50),
              "wide_minutes": (0, 120), "wide_stocks": (0, 6000), "movers": (0, 20), "yesterday_movers": (0, 20)}
    WIDE_MIN_MINUTES = 15

    @property
    def full_scan_time(self) -> dt.time:
        return dt.time.fromisoformat(self.premarket_time)

    @property
    def gap_check_time(self) -> dt.time:
        return dt.time.fromisoformat(self.gapper_time)

    def changed(self, **changes: Any) -> "ScanSettings":
        """A validated copy; raises ValueError with a message for the dashboard."""
        clean = {k: v for k, v in changes.items() if v is not None and k in asdict(self)}
        new = replace(self, **{k: (str(v) if k in _TIMES else int(v)) for k, v in clean.items()})
        try:
            t = new.full_scan_time
        except ValueError:
            raise ValueError("Full-scan time must look like 08:30.") from None
        if not EARLIEST_FULL_SCAN <= t <= LATEST_FULL_SCAN:
            raise ValueError("The full scan runs before the open: pick a time from 04:00 to 09:00 ET.")
        try:
            g = new.gap_check_time
        except ValueError:
            raise ValueError("Gap-check time must look like 09:15.") from None
        if not EARLIEST_GAP_CHECK <= g <= LATEST_GAP_CHECK:
            raise ValueError("The gap check runs just before the open: pick a time from 08:00 to 09:25 ET.")
        for key, (lo, hi) in self.LIMITS.items():
            if not lo <= getattr(new, key) <= hi:
                raise ValueError(f"{key.replace('_', ' ').capitalize()} must be between {lo} and {hi}.")
        if 0 < new.wide_minutes < self.WIDE_MIN_MINUTES:
            raise ValueError(f"The wide scan runs every {self.WIDE_MIN_MINUTES} to {self.LIMITS['wide_minutes'][1]} "
                             "minutes - one request per stock - or 0 switches it off.")
        return replace(new, premarket_time=t.strftime("%H:%M"), gapper_time=g.strftime("%H:%M"))

    @property
    def wide_on(self) -> bool:
        return self.wide_minutes > 0

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
        if 0 < out.get("wide_minutes", 0) < cls.WIDE_MIN_MINUTES:
            out["wide_minutes"] = cls.WIDE_MIN_MINUTES
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


def gap_check_due(now: dt.datetime, settings: ScanSettings, have_session: Optional[dt.date],
                  done_session: Optional[dt.date]) -> bool:
    """The gap check runs once a session, from its time until the open, on the watchlist
    built for today's session. ``done_session`` is the session it last ran for."""
    today = now.date()
    if have_session != today or done_session == today or not clock.is_trading_day(today):
        return False
    return settings.gap_check_time <= now.time() < OPEN
