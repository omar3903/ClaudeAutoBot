"""A thread-safe request pacer."""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Space request *starts* at least ``min_gap`` seconds apart across threads.

    The lock only guards reserving a start slot; the waiting happens outside
    it, so a blocked thread never holds up the bookkeeping and the requests
    themselves still overlap. Each data provider gets its own limiter, so a
    broker feed isn't paced at yfinance's rate."""

    def __init__(self, min_gap: float = 0.0) -> None:
        self.min_gap = max(0.0, float(min_gap))
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> float:
        """Block until this caller's start slot. Returns the seconds waited."""
        if self.min_gap <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.min_gap
        delay = start - now
        if delay > 0:
            time.sleep(delay)
        return delay
