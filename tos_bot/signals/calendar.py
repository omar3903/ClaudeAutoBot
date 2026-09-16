"""When companies report earnings, ahead of time, from Finnhub's earnings calendar (free with a key).

SEC's 8-K item 2.02 (signals/earnings.py) says when a report came out, but only after it did, and
some companies file it hours or days after their press release. Finnhub's calendar lists each report
in advance: the day, whether it comes before the open ("bmo"), after the close ("amc") or during the
session ("dmh"), the analysts' EPS and revenue estimates, and the actual numbers once they're out.
Two uses:

- **Post-earnings drift** (strategies/statistical.py): a report out after the last close or before
  today's open counts even when SEC hasn't the 8-K yet. And Chan's rule from *Quantitative Trading*
  - buy when earnings beat expectations, short when they fall short - becomes a check on the gap:
  a gap that goes the other way from the surprise isn't taken.
- **Earnings ahead** (scanner/noise.py): a swing trade held into a report can gap straight through
  its stop, so a swing play with a report due within its hold is flagged, and the engine warns
  about swing positions held into one.

The calendar is read once every few hours - one request covers every company - and every report
seen is kept, so a history builds up for the journal and the replay.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from ..util import clock

log = logging.getLogger(__name__)

KEEP_DAYS = 400
BEFORE_OPEN, AFTER_CLOSE, DURING = "bmo", "amc", "dmh"


def ibkr_symbol(finnhub: str) -> str:
    """Finnhub's "BRK.B" is IBKR's "BRK B"."""
    return finnhub.strip().upper().replace(".", " ")


def _number(value: Any) -> Optional[float]:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class EarningsEvent:
    symbol: str
    date: str                          # the report's day, ISO
    hour: str = ""                     # bmo | amc | dmh | "" (not said)
    eps_estimate: Optional[float] = None
    eps_actual: Optional[float] = None
    revenue_estimate: Optional[float] = None
    revenue_actual: Optional[float] = None
    quarter: Optional[int] = None
    year: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "surprise": surprise(asdict(self))}


def parse_calendar(payload: Mapping[str, Any]) -> List[EarningsEvent]:
    out: List[EarningsEvent] = []
    for row in (payload or {}).get("earningsCalendar") or []:
        symbol, day = str(row.get("symbol") or ""), str(row.get("date") or "")
        if not symbol or len(day) != 10:
            continue
        out.append(EarningsEvent(
            symbol=ibkr_symbol(symbol), date=day, hour=str(row.get("hour") or "").lower(),
            eps_estimate=_number(row.get("epsEstimate")), eps_actual=_number(row.get("epsActual")),
            revenue_estimate=_number(row.get("revenueEstimate")), revenue_actual=_number(row.get("revenueActual")),
            quarter=int(row["quarter"]) if row.get("quarter") else None, year=int(row["year"]) if row.get("year") else None))
    return out


# ---------------------------------------------------------------- reading a stock's reports
def surprise(event: Mapping[str, Any]) -> Optional[float]:
    """How far the reported EPS came from the estimate, as a fraction of the estimate's size:
    (actual - estimate) / |estimate|. None until both are known, or when the estimate is zero."""
    actual, estimate = event.get("eps_actual"), event.get("eps_estimate")
    if actual is None or estimate is None or estimate == 0:
        return None
    return round((float(actual) - float(estimate)) / abs(float(estimate)), 4)


def report_before_open(events: Optional[Sequence[Mapping[str, Any]]], day: dt.date) -> Optional[Mapping[str, Any]]:
    """The report that came out between the previous session's close and ``day``'s open: before
    the open on ``day``, or after the close the session before - or, when the hour wasn't given, the
    session before's report once its numbers are out."""
    prev = clock.prev_trading_day(day).isoformat()
    for e in events or []:
        if (e["date"] == day.isoformat() and e.get("hour") == BEFORE_OPEN) or (
                e["date"] == prev and (e.get("hour") == AFTER_CLOSE or (not e.get("hour") and e.get("eps_actual") is not None))):
            return e
    return None


def next_report(events: Optional[Sequence[Mapping[str, Any]]], now: dt.datetime) -> Optional[Mapping[str, Any]]:
    """The first report still to come: a later day, or today's after the close (or at an hour not
    given, while its numbers aren't out)."""
    today = now.date().isoformat()
    for e in sorted(events or [], key=lambda e: e["date"]):
        if e["date"] > today or (e["date"] == today and (e.get("hour") == AFTER_CLOSE
                                                         or (not e.get("hour") and e.get("eps_actual") is None))):
            return e
    return None


def sessions_until(event: Mapping[str, Any], now: dt.datetime) -> int:
    """Trading sessions from today to the report's day: 0 today, 1 the next session."""
    day, n, today = dt.date.fromisoformat(event["date"]), 0, now.date()
    cursor = today
    while cursor < day:
        cursor = clock.next_trading_day(cursor)
        n += 1
    return n


def describe(event: Mapping[str, Any]) -> str:
    when = {BEFORE_OPEN: "before the open", AFTER_CLOSE: "after the close", DURING: "during the session"}.get(
        event.get("hour") or "", "at an hour not given")
    return f"{event['date']} {when}"


# ---------------------------------------------------------------- the file it's kept in
class EarningsCalendarFile:
    """Every report seen, one entry per stock and day, in a JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fetched_at: Optional[str] = None
        self._events: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.fetched_at = doc.get("fetched_at")
        self._events = {f"{e['symbol']}|{e['date']}": e for e in doc.get("events", []) if e.get("symbol") and e.get("date")}

    def merge(self, events: Iterable[EarningsEvent], today: dt.date) -> int:
        """Add or update reports; returns how many there are now. Reports older than a year are let go."""
        for e in events:
            self._events[f"{e.symbol}|{e.date}"] = e.as_dict()
        oldest = (today - dt.timedelta(days=KEEP_DAYS)).isoformat()
        self._events = {k: e for k, e in self._events.items() if e["date"] >= oldest}
        self.fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        return len(self._events)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fetched_at": self.fetched_at, "events": list(self._events.values())}),
                           encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            log.warning("could not save the earnings calendar", exc_info=True)

    def by_symbol(self) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for e in sorted(self._events.values(), key=lambda e: e["date"]):
            out.setdefault(e["symbol"], []).append(e)
        return out

    def __len__(self) -> int:
        return len(self._events)
