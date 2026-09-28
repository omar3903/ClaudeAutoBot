"""When companies reported earnings, from the filings they made with SEC.

A company's SEC submissions file lists its recent filings and the moment SEC accepted each
one. An 8-K carrying item 2.02 ("Results of Operations and Financial Condition") is the
earnings release, and its acceptance time is when the market could first have known. The
post-earnings drift setup (strategies/statistical.py) reads it live from the signal book;
the replay reads the history kept here, one file per stock, refreshed at most once a day.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from ..data.sec_edgar import sec_ticker
from ..util import clock
from .edgar import SUBMISSIONS_URL

log = logging.getLogger(__name__)

EARNINGS_ITEM = "2.02"


def earnings_times(submissions: Mapping) -> List[str]:
    """When SEC accepted each of a company's recent earnings releases, as UTC ISO times."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    items, accepted = recent.get("items") or [], recent.get("acceptanceDateTime") or []
    out = set()
    for i, form in enumerate(recent.get("form", [])):
        if form not in ("8-K", "8-K/A") or i >= len(accepted) or i >= len(items):
            continue
        if EARNINGS_ITEM in {c.strip() for c in str(items[i] or "").split(",")}:
            stamp = dt.datetime.fromisoformat(accepted[i].replace("Z", "+00:00"))
            out.add(stamp.astimezone(dt.timezone.utc).isoformat())
    return sorted(out)


class EarningsCalendar:
    def __init__(self, directory: Path, fetch_json: Callable[[str], dict],
                 ciks: Callable[[], Mapping[str, int]]) -> None:
        self.directory = directory
        self._fetch = fetch_json
        self._ciks = ciks

    def times(self, symbols: Sequence[str], today: Optional[dt.date] = None) -> Dict[str, List[str]]:
        """Each stock's earnings acceptance times: from disk when read today, else from SEC.
        Older reports that fell off SEC's recent list are kept."""
        today = today or clock.session_date()
        ciks: Optional[Mapping[str, int]] = None
        out: Dict[str, List[str]] = {}
        for symbol in symbols:
            cached = self._read(symbol) or {}
            known = list(cached.get("accepted", []))
            if cached.get("fetched") == today.isoformat():
                out[symbol] = known
                continue
            if ciks is None:
                ciks = self._ciks() or {}
            cik = ciks.get(sec_ticker(symbol))
            if not cik:
                out[symbol] = known
                continue
            try:
                fresh = earnings_times(self._fetch(SUBMISSIONS_URL.format(cik=cik)))
            except Exception as e:  # noqa: BLE001
                log.debug("SEC submissions for %s: %s", symbol, e)
                out[symbol] = known
                continue
            out[symbol] = sorted(set(fresh) | set(known))
            self._write(symbol, {"fetched": today.isoformat(), "accepted": out[symbol]})
        return out

    def _path(self, symbol: str) -> Path:
        return self.directory / f"{symbol.replace(' ', '_')}.json"

    def _read(self, symbol: str) -> Optional[dict]:
        try:
            return json.loads(self._path(symbol).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, symbol: str, data: dict) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._path(symbol).write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            log.debug("could not save %s's earnings dates", symbol, exc_info=True)
