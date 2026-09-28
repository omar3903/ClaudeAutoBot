"""Company news and the earnings calendar from Finnhub (finnhub.io): free with a key, 60 requests a minute.

The key travels in a request header, never in the address, so it doesn't end up in
logs.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any, Iterable, List, Mapping, Optional

import requests

from .calendar import EarningsEvent, parse_calendar
from .news import NewsItem

URL = "https://finnhub.io/api/v1/company-news"
CALENDAR_URL = "https://finnhub.io/api/v1/calendar/earnings"
MIN_GAP_S = 1.05                   # 60 requests a minute on the free plan


def finnhub_symbol(symbol: str) -> str:
    """IBKR's "BRK B" is Finnhub's "BRK.B"."""
    return symbol.replace(" ", ".").upper()


def parse_company_news(symbol: str, rows: Iterable[Mapping[str, Any]]) -> List[NewsItem]:
    out: List[NewsItem] = []
    for row in rows or []:
        headline, stamp = (row.get("headline") or "").strip(), row.get("datetime")
        if not headline or not stamp:
            continue
        out.append(NewsItem(symbol=symbol, source="finnhub", provider=str(row.get("source") or "")[:40],
                            headline=headline, published_at=dt.datetime.fromtimestamp(int(stamp), dt.timezone.utc),
                            url=str(row.get("url") or ""), ref=str(row.get("id") or "")))
    return out


class FinnhubNews:
    def __init__(self, api_key: str, session: Optional[requests.Session] = None) -> None:
        self.api_key = (api_key or "").strip()
        self._http = session or requests.Session()
        self._lock = threading.Lock()
        self._next_start = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def company_news(self, symbol: str, since: dt.date, until: dt.date) -> List[NewsItem]:
        if not self.configured:
            return []
        return parse_company_news(symbol, self._get(URL, {"symbol": finnhub_symbol(symbol), "from": since.isoformat(),
                                                          "to": until.isoformat()}))

    def earnings_calendar(self, since: dt.date, until: dt.date) -> List[EarningsEvent]:
        """Every company's earnings reports between ``since`` and ``until`` - one request."""
        if not self.configured:
            return []
        return parse_calendar(self._get(CALENDAR_URL, {"from": since.isoformat(), "to": until.isoformat()}))

    def _get(self, url: str, params: Mapping[str, str]) -> Any:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_start)
            self._next_start = start + MIN_GAP_S
        if start > now:
            time.sleep(start - now)
        r = self._http.get(url, params=dict(params), headers={"X-Finnhub-Token": self.api_key}, timeout=30)
        r.raise_for_status()
        return r.json()
