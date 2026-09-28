"""One shared, polite connection to SEC EDGAR.

SEC allows at most 10 requests a second from one address and wants a User-Agent
naming the tool. Everything in the app that reads EDGAR - company financials,
insider filings, 8-K checks - goes through :data:`SEC`, so together they stay
under that limit.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import requests

HEADERS = {"User-Agent": "AutoTradeBot/1.0 (personal research tool)", "Accept-Encoding": "gzip, deflate"}
MIN_GAP_S = 0.12


class SecHttp:
    def __init__(self, min_gap_s: float = MIN_GAP_S, session: Optional[requests.Session] = None) -> None:
        self._http = session or requests.Session()
        self._http.headers.update(HEADERS)
        self._gap = min_gap_s
        self._lock = threading.Lock()
        self._next_start = 0.0

    def get(self, url: str, timeout: float = 30.0) -> requests.Response:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_start)
            self._next_start = start + self._gap
        if start > now:
            time.sleep(start - now)
        r = self._http.get(url, timeout=timeout)
        r.raise_for_status()
        return r

    def json(self, url: str, timeout: float = 30.0) -> dict:
        return self.get(url, timeout).json()

    def content(self, url: str, timeout: float = 30.0) -> bytes:
        return self.get(url, timeout).content

    def text(self, url: str, timeout: float = 30.0) -> str:
        return self.get(url, timeout).text


#: the connection the whole app shares
SEC = SecHttp()
