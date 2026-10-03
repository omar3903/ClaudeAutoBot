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
reported, never traded or deleted on by itself: which number is right is the
operator's call, made with the warning's Fix button (engine.fix_mismatch).

Shares the account holds with no open record at all - or held the other way
round from the records - are reported once they have stayed so for
DRIFT_ALERT_S of the regular session (drift()). An account short with nothing
recorded, or on the other side from its record, is urgent: it is the position
nothing manages. Neither is ever unwound by itself - the shares' Exit in Open
positions is the operator's.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple


class PositionCheck:
    MISSES_NEEDED = 2
    GRACE_S = 90.0
    SETTLE_S = 60.0
    FRESH_ACCOUNT_S = 5.0
    #: seconds of the regular session shares with no record (or the wrong way round) may stay before it's said
    DRIFT_ALERT_S = 120.0

    def __init__(self) -> None:
        self._misses: Dict[Tuple[str, str], int] = {}
        self._apart: Dict[Tuple[str, str], Tuple[float, float]] = {}   # share counts last seen disagreeing
        #: symbols whose records and broker position disagree, as of the last check
        self.mismatches: List[Dict[str, Any]] = []
        #: (venue, symbol) -> the counts (recorded, held) of shares the records don't explain, and since when
        #: (monotonic) they have read so in the regular session; and the counts each was last reported at
        self._drift: Dict[Tuple[str, str], Tuple[Tuple[float, float], float]] = {}
        self._drift_said: Dict[Tuple[str, str], Tuple[float, float]] = {}

    def reset(self) -> None:
        self._misses.clear()
        self._apart.clear()
        self.mismatches = []
        self._drift.clear()
        self._drift_said.clear()

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

    def drift(self, venue: str, where: str, trades: Iterable[Mapping[str, Any]], held: Mapping[str, float],
              in_flight: Set[str], regular: bool, account_age_s: float, connection_age_s: float,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Shares ``held`` (signed, per symbol) that the open records don't explain: a symbol held with no record,
        or held the other way round from its records. Each is returned once it has read the same for DRIFT_ALERT_S
        of the regular session - and again when the counts change, or in the next session - ``urgent`` when the
        account is short with nothing recorded or on the other side from its records. Outside the regular session
        nothing is timed (pre-market fills settle by the open); an answer that can't be trusted - stale, or the
        connection only just up - changes nothing; a symbol with an order still working is left alone, its fills
        change the counts. Fewer or more shares the same way round as the records are share_counts' warning."""
        if not regular:
            self._drift.clear()
            self._drift_said.clear()
            return []
        if account_age_s > self.FRESH_ACCOUNT_S or connection_age_s < self.SETTLE_S:
            return []
        now = time.monotonic() if now is None else now
        recorded: Dict[str, float] = {}
        records: Dict[str, int] = {}
        for t in trades:
            sign = 1.0 if t["side"] == "LONG" else -1.0
            recorded[t["symbol"]] = recorded.get(t["symbol"], 0.0) + sign * abs(float(t["quantity"] or 0.0))
            records[t["symbol"]] = records.get(t["symbol"], 0) + 1
        seen: Dict[Tuple[str, str], Tuple[Tuple[float, float], float]] = {}
        out: List[Dict[str, Any]] = []
        for symbol, theirs in sorted(held.items()):
            theirs, mine = float(theirs), recorded.get(symbol, 0.0)
            if symbol in in_flight or abs(theirs) < 1e-9:
                continue            # orders still filling, or none held (gone() settles a record whose shares went)
            flipped = abs(mine) > 1e-9 and (theirs > 0) != (mine > 0)
            if abs(mine) > 1e-9 and not flipped:
                continue            # the same way round as the records: share_counts' warning, with its Fix
            key, counts = (venue, symbol), (mine, theirs)
            prev = self._drift.get(key)
            since = prev[1] if prev is not None and prev[0] == counts else now
            seen[key] = (counts, since)
            if now - since < self.DRIFT_ALERT_S or self._drift_said.get(key) == counts:
                continue
            self._drift_said[key] = counts
            minutes = f"{(now - since) / 60:.0f} min"
            if flipped:
                n = records[symbol]
                note = (f"URGENT - {symbol}: {where} has held {_shares(theirs)} for {minutes}, the other way round "
                        f"from the app's {'open trade' if n == 1 else f'{n} open trades'} ({_shares(mine)}). Nothing "
                        f"is traded because of it - check the position at {where}.")
            elif theirs < 0:
                note = (f"URGENT - {symbol}: {where} has been {_shares(theirs)} for {minutes} with no open record in "
                        "the app - a short nothing manages, whose loss grows as the price rises. Nothing is traded "
                        f"because of it - check it at {where}, or exit it under Shares without a record in Open "
                        "positions.")
            else:
                note = (f"{symbol}: {where} has held {_shares(theirs)} for {minutes} with no open record in the app, "
                        f"so nothing manages them - no stop, no exit. Nothing is traded because of it - check them at "
                        f"{where}, or exit them under Shares without a record in Open positions.")
            out.append({"symbol": symbol, "recorded": mine, "held": theirs, "urgent": flipped or theirs < 0,
                        "note": note})
        self._drift = seen
        self._drift_said = {k: v for k, v in self._drift_said.items() if k in seen}
        return out


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
