"""Which OPEN trade records no longer have a position behind them.

A position can disappear outside the app - closed in the broker's own window,
or wiped by a simulator reset. Its record should go, but a slow or dropped
feed must never erase a live position, so a record only counts as gone when:

* the account answer is fresh (seconds old) from a connected broker,
* the connection has been up long enough to have reported positions,
* the trade is past its grace period and has no close order working, and
* it's been missing on consecutive checks.

A forced check (right after a simulator reset) skips the last three.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple


class PositionCheck:
    MISSES_NEEDED = 2
    GRACE_S = 90.0
    SETTLE_S = 60.0
    FRESH_ACCOUNT_S = 5.0

    def __init__(self) -> None:
        self._misses: Dict[Tuple[str, str], int] = {}

    def reset(self) -> None:
        self._misses.clear()

    def gone(self, venue: str, trades: Iterable[Mapping[str, Any]], held: Set[str], busy: Set[str],
             account_age_s: float, connection_age_s: float, force: bool = False) -> List[Mapping[str, Any]]:
        if account_age_s > self.FRESH_ACCOUNT_S:
            return []
        if not force and connection_age_s < self.SETTLE_S:
            return []
        counted: Set[Tuple[str, str]] = set()
        out: List[Mapping[str, Any]] = []
        for t in trades:
            key = (venue, t["symbol"])
            if t["symbol"] in held:
                self._misses.pop(key, None)
                continue
            if t["id"] in busy:
                continue                              # its close is still going through
            if not force:
                if _age_s(t.get("entry_time")) < self.GRACE_S:
                    continue
                if key not in counted:
                    counted.add(key)
                    self._misses[key] = self._misses.get(key, 0) + 1
                if self._misses[key] < self.MISSES_NEEDED:
                    continue
            out.append(t)
        for t in out:
            self._misses.pop((venue, t["symbol"]), None)
        return out


def _age_s(entry_time: Optional[str]) -> float:
    """Seconds since a trade's (naive-UTC) entry time; infinite if unknown."""
    if not entry_time:
        return float("inf")
    try:
        entered = dt.datetime.fromisoformat(entry_time).replace(tzinfo=None)
    except ValueError:
        return float("inf")
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - entered).total_seconds()
