"""Real-time streams of the prices that matter most: the stocks held first, then the best plays on offer, then
the watch tier (the day's hot list and the buffer names behind it).

The price source holds the streams itself (IbkrBroker.set_streams, on its own loop); this points them at the
right stocks within the line budget, and tells MarketData.quote when a streamed quote is fresh enough to serve
instead of asking for a snapshot, and which streamed prices the dashboard hasn't been sent yet (take_moves). A
source that can't stream - delayed data, a dropped connection, a source with no streams at all - leaves it idle,
and every price is had as before.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from ..core.models import Quote

log = logging.getLogger(__name__)

#: told the symbols whose streamed price just changed - on the IB loop thread, so it may only set flags. The same
#: ticks feed the live candles first (MarketData.candles, O(1) a stock)
Listener = Callable[[FrozenSet[str]], None]


class StreamManager:
    #: a streamed quote this recent is served as it is; a quieter stock gets a snapshot, as before. A stream only
    #: sends changes, so this also bounds how long a stream that died without a word can go unnoticed
    FRESH_S = 2.0
    #: a play's stream is kept this long while the play is on offer, so a re-rank doesn't trade lines back and forth
    PLAY_MIN_HOLD_S = 60.0
    #: a play the operator opened streams ahead of the other plays this long, so an Execute click is priced off
    #: the stream - and no more than this many at once, the latest opened kept
    PREFER_S = 300.0
    PREFER_MAX = 5

    def __init__(self, md: Any) -> None:
        """``md``: the MarketData it serves - its source is read afresh on every call, never kept."""
        self._md = md
        # never held while waiting on the source or calling a listener: the source calls _on_ticks from its loop
        self._lock = threading.Lock()
        self._src: Any = None                          # the source whose on_tick is bound here
        self._since: Dict[str, float] = {}             # symbol -> when its stream started (time.monotonic)
        self._moved: Set[str] = set()                  # symbols that ticked since the dashboard last looked
        self._pushed: Dict[str, float] = {}            # symbol -> the price the dashboard was last sent (take_moves)
        self._preferred: Dict[str, float] = {}         # symbol -> when the operator last opened a play on it
        self._listeners: List[Listener] = []
        self._feed_failed = False                      # a tick the candles couldn't take is logged once

    def _source(self) -> Any:
        return self._md._source

    # ---- which stocks stream ---------------------------------------------- #
    def sync(self, held: Sequence[str], plays: Sequence[str], lines: int,
             candidates: Sequence[str] = (), watch: Sequence[str] = ()) -> List[str]:
        """One resync: stream ``held`` (the positions and working entries) first, then ``plays`` (best first),
        then ``watch`` (the watch tier, in the order given), ``lines`` at most - 0 streams nothing. Of the plays,
        the ones the operator opened lately (prefer) go first, then ``candidates`` (the plays Autopilot would
        take), then the rest - where a play streaming for less than PLAY_MIN_HOLD_S keeps its line ahead of the
        others while it's still on offer. The watch names get no such hold: the tier changes at the cycles' pace,
        not the ticks'. The source is told how many of the order are held, so a refusal of lines (101) never
        takes those. Returns the symbols streaming now. It waits on the source, so never call it from the IB
        loop."""
        src = self._source()
        if src is None or not hasattr(src, "set_streams"):
            self.reset()
            return []
        first = list(dict.fromkeys(s for s in held if s))
        taken = set(first)
        offered = [s for s in dict.fromkeys(plays) if s and s not in taken]
        # only plays still on offer go ahead: one taken since streams with the positions, one gone not at all
        on_offer = set(offered)
        ahead = [s for s in dict.fromkeys([*self.preferred(), *candidates]) if s in on_offer]
        put = set(ahead)
        rest = [s for s in offered if s not in put]
        now = time.monotonic()
        with self._lock:
            if src is not self._src:
                self._unbind()
                self._src = src
                self._since.clear()
            if getattr(src, "on_tick", None) != self._on_ticks:
                src.on_tick = self._on_ticks        # its ticks come to _on_ticks from here on
            young = [s for s in rest if now - self._since.get(s, float("-inf")) < self.PLAY_MIN_HOLD_S]
        kept = set(young)
        order = first + ahead + young + [s for s in rest if s not in kept]
        placed = set(order)
        order += [s for s in dict.fromkeys(watch) if s and s not in placed]
        streaming = list(src.set_streams(order, lines, protect=len(first)))
        with self._lock:
            if self._src is src:
                self._since = {s: self._since.get(s, now) for s in streaming}
                # a stock that stops streaming is sent afresh when it starts again: a snapshot may be shown meanwhile
                self._pushed = {s: px for s, px in self._pushed.items() if s in self._since}
        candles = self._candles()
        if candles is not None:
            candles.keep(streaming)                 # a stream that ended takes its candles along
        return streaming

    def prefer(self, symbol: str) -> None:
        """The operator opened a play on ``symbol``: it streams ahead of the other plays for PREFER_S, so the
        Execute click is priced off the stream. Only the dashboard's click calls it - Autopilot looks at every
        play it passes, and would crowd out the one on the screen."""
        if not symbol:
            return
        now = time.monotonic()
        with self._lock:
            self._preferred.pop(symbol, None)                   # opened again: it's the latest
            self._preferred[symbol] = now
            live = [(s, at) for s, at in self._preferred.items() if now - at < self.PREFER_S]
            self._preferred = dict(live[-self.PREFER_MAX:])

    def preferred(self) -> List[str]:
        """The stocks of the plays the operator opened in the last PREFER_S, the latest first."""
        now = time.monotonic()
        with self._lock:
            return [s for s, at in reversed(self._preferred.items()) if now - at < self.PREFER_S]

    def reset(self) -> None:
        """Forget the source and everything it streamed - a new connection, or none (MarketData.attach/detach).
        The source ends its own streams when it goes."""
        with self._lock:
            self._unbind()
            self._src = None
            self._since.clear()
            self._moved.clear()
            self._pushed.clear()
        candles = self._candles()
        if candles is not None:
            candles.clear()

    def _candles(self) -> Any:
        """The live candles the ticks feed (MarketData.candles) - None for an md without them."""
        return getattr(self._md, "candles", None)

    def _unbind(self) -> None:
        """Stop hearing from the source held till now (under _lock)."""
        if self._src is not None and getattr(self._src, "on_tick", None) == self._on_ticks:
            self._src.on_tick = None

    # ---- the streamed prices ---------------------------------------------- #
    def fresh(self, symbol: str, src: Any) -> Optional[Quote]:
        """``src``'s streamed quote for ``symbol`` while it is at most FRESH_S old - the same quote a snapshot
        would give, without the round trip. None otherwise, and the caller asks for a snapshot as before. It
        asks the source it's given, so a source swapped out since is never read."""
        got = self._read(src, symbol)
        return got[0] if got is not None and got[1] <= self.FRESH_S else None

    def latest(self, symbol: str) -> Optional[Tuple[Quote, float]]:
        """The current source's latest streamed quote for ``symbol`` and how many seconds ago it came, however
        old - for showing. None when it isn't streaming."""
        return self._read(self._source(), symbol)

    def take_moves(self) -> Dict[str, Tuple[float, dt.datetime]]:
        """The streamed prices the dashboard hasn't been sent: of the stocks that ticked since the last call, those
        whose price as shown - the last trade, else the mid, to 4 places - differs from the one last sent, as
        {symbol: (price, when it's from)}. A tick that left that price as it was isn't sent. For showing only."""
        src = self._source()
        with self._lock:
            moved, self._moved = self._moved, set()
        out: Dict[str, Tuple[float, dt.datetime]] = {}
        for symbol in sorted(moved):                    # read with no lock held: the source takes its own
            got = self._read(src, symbol)
            price = round(float(got[0].last or got[0].mid or 0.0), 4) if got is not None else 0.0
            if price > 0:
                ts = got[0].ts
                out[symbol] = (price, ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc))
        with self._lock:
            if src is not self._src:
                return {}                               # its source was swapped meanwhile: these ticks are the old one's
            out = {s: hit for s, hit in out.items() if self._pushed.get(s) != hit[0]}
            self._pushed.update((s, hit[0]) for s, hit in out.items())
        return out

    @staticmethod
    def _read(src: Any, symbol: str) -> Optional[Tuple[Quote, float]]:
        read = getattr(src, "streamed_quote", None) if src is not None else None
        if read is None:
            return None
        try:
            return read(symbol)
        except Exception as e:  # noqa: BLE001
            log.debug("streamed quote for %s unavailable: %s", symbol, e)
            return None

    # ---- who hears of a tick ---------------------------------------------- #
    def add_listener(self, fn: Listener) -> None:
        """``fn`` is told the symbols whose streamed price changed, on the IB loop thread: it may only set flags."""
        with self._lock:
            self._listeners.append(fn)

    def _on_ticks(self, symbols: FrozenSet[str]) -> None:
        """The source's word that ``symbols`` ticked, on its loop thread: noted, fed to the live candles, then
        passed on."""
        with self._lock:
            self._moved |= symbols
            listeners = list(self._listeners)
            src = self._src
        self._feed(src, symbols)
        for fn in listeners:
            try:
                fn(symbols)
            except Exception:  # noqa: BLE001
                log.debug("a stream listener failed", exc_info=True)

    def _feed(self, src: Any, symbols: FrozenSet[str]) -> None:
        """Each ticked stock's streamed quote to the live candles - on the IB loop, with no lock of ours held:
        the candles take their own, briefly, and do O(1) work a stock."""
        candles = self._candles()
        if candles is None:
            return
        try:
            for symbol in symbols:
                got = self._read(src, symbol)
                if got:
                    q = got[0]
                    ts = q.ts if q.ts.tzinfo else q.ts.replace(tzinfo=dt.timezone.utc)
                    candles.add(symbol, float(q.last or 0), float(q.volume or 0), ts.timestamp())
        except Exception:  # noqa: BLE001 - the listeners still hear of the tick
            if not self._feed_failed:
                self._feed_failed = True
                log.debug("a streamed tick couldn't go into the live candles (later failures are not logged)",
                          exc_info=True)
