"""What the broker's positions say about the OPEN trade records.

A position can disappear outside the app - closed in the broker's own window,
or wiped by a simulator reset. Its record should go, but a slow or dropped
feed must never erase a live position, so a record only counts as gone when:

* the account answer is fresh (seconds old) from a connected broker,
* the connection has been up long enough to have reported positions,
* the trade is past its grace period and has no close order working, and
* it's been missing on consecutive checks.

A forced check (right after a simulator reset) skips the last three.

A position can also be a different size than its records add up to - an order
that filled twice, or shares traded in the broker's own window. That is only
reported, never traded or deleted on: which number is right is the operator's call.
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
        self._apart: Dict[Tuple[str, str], Tuple[float, float]] = {}   # share counts last seen disagreeing
        #: symbols whose records and broker position disagree, as of the last check
        self.mismatches: List[Dict[str, Any]] = []

    def reset(self) -> None:
        self._misses.clear()
        self._apart.clear()
        self.mismatches = []

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

    def share_counts(self, venue: str, where: str, trades: Iterable[Mapping[str, Any]],
                     held: Mapping[str, float], in_flight: Set[str],
                     account_age_s: float, connection_age_s: float) -> List[Dict[str, Any]]:
        """Symbols whose open records add up to a different position than the broker
        holds (``held``: signed shares per symbol), seen the same on consecutive
        checks. ``self.mismatches`` becomes the current list; the ones not reported
        before are returned."""
        if account_age_s > self.FRESH_ACCOUNT_S or connection_age_s < self.SETTLE_S:
            return []
        recorded: Dict[str, float] = {}
        records: Dict[str, int] = {}
        for t in trades:
            sign = 1.0 if t["side"] == "LONG" else -1.0
            recorded[t["symbol"]] = recorded.get(t["symbol"], 0.0) + sign * abs(float(t["quantity"] or 0.0))
            records[t["symbol"]] = records.get(t["symbol"], 0) + 1
        previous, self._apart = self._apart, {}
        found: List[Dict[str, Any]] = []
        for symbol, mine in sorted(recorded.items()):
            theirs = float(held.get(symbol, 0.0))
            if symbol in in_flight or abs(theirs) < 1e-9 or abs(theirs - mine) < 1e-6:
                continue            # orders still filling, gone entirely (see gone()), or in agreement
            key = (venue, symbol)
            self._apart[key] = (mine, theirs)
            if previous.get(key) != (mine, theirs):
                continue            # confirm on the next check - a fill may still be landing
            n = records[symbol]
            found.append({
                "symbol": symbol, "recorded": mine, "held": theirs, "records": n,
                "note": (f"{symbol}: {where} holds {_shares(theirs)}, but the app's "
                         f"{'open trade is' if n == 1 else f'{n} open trades are'} for {_shares(mine)}. "
                         f"Nothing is traded or deleted because of it - check the position at {where}."),
            })
        new = [m for m in found if m not in self.mismatches]
        self.mismatches = found
        return new


def _shares(signed: float) -> str:
    return f"{abs(signed):,.0f} shares {'long' if signed > 0 else 'short'}"


def _age_s(entry_time: Optional[str]) -> float:
    """Seconds since a trade's (naive-UTC) entry time; infinite if unknown."""
    if not entry_time:
        return float("inf")
    try:
        entered = dt.datetime.fromisoformat(entry_time).replace(tzinfo=None)
    except ValueError:
        return float("inf")
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - entered).total_seconds()
