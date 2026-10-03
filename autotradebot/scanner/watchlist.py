"""Today's hot list and the sector buffer behind it.

The full scan ranks every stock and fills in two things:

* the **hot list** - the most in-play names, no more than a third of them from
  one sector. These are rescanned every cycle.
* a **buffer queue** for each sector - the next best candidates in that sector
  that haven't been looked at intraday yet.

Each cycle takes the next few names from every sector's queue and looks at them
alongside the buffer names already kept:

* **adopted** - hotter than the coolest hot-list stock, which it replaces;
* **kept** - among the best seen so far in its sector, waiting for a slot;
* **dropped** - of less use than what's already hot or kept.

So each sector's queue is worked through over the day with only a handful of
extra requests per cycle.

The day's **movers** hold slots of their own: the last session's biggest movers
from the full scan (in case they move for a second day), today's from every wide
scan. A stock in play trumps the sector cap (Aziz), so a mover replaces the
coolest hot-list name that isn't a mover itself.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .heat import DailyMetrics

#: a buffer stock must beat the coolest hot-list stock by this much to take its slot
ADOPT_MARGIN = 0.10
_DECISIONS_KEPT = 60


@dataclass
class Candidate:
    symbol: str
    sector: str
    daily_heat: float
    heat: Optional[float] = None          # latest intraday heat
    gap_pct: Optional[float] = None       # the pre-market gap the gap check saw, %
    why: str = ""                         # why it holds a slot when not by heat: a mover's move, in words


@dataclass
class Decision:
    at: str
    symbol: str
    sector: str
    action: str                           # adopted | kept | dropped
    heat: float
    replaced: str = ""
    note: str = ""                        # why, in words (the gap check says what it saw)


@dataclass
class DayWatchlist:
    session: dt.date
    bars_through: dt.date
    built_at: str
    universe: int
    liquid: int
    hot: List[Candidate]
    queues: Dict[str, List[Candidate]]
    kept: Dict[str, List[Candidate]] = field(default_factory=dict)
    searched: Dict[str, int] = field(default_factory=dict)
    decisions: List[Decision] = field(default_factory=list)
    #: every liquid stock the full scan ranked, hottest first - the pool the wider looks draw from
    ranked: List[str] = field(default_factory=list)

    @classmethod
    def build(cls, session: dt.date, bars_through: dt.date, ranked: Iterable[DailyMetrics],
              sector_of: Callable[[str], str], hot_size: int, queue_size: int,
              universe: int, liquid: int) -> "DayWatchlist":
        per_sector_cap = max(1, math.ceil(hot_size / 3))
        hot: List[Candidate] = []
        queues: Dict[str, List[Candidate]] = {}
        for m in ranked:
            sector = sector_of(m.symbol)
            if not sector:
                continue
            c = Candidate(m.symbol, sector, m.heat)
            if len(hot) < hot_size and sum(h.sector == sector for h in hot) < per_sector_cap:
                hot.append(c)
            elif len(queues.setdefault(sector, [])) < queue_size:
                queues[sector].append(c)
        return cls(session=session, bars_through=bars_through, built_at=_now_iso(),
                   universe=universe, liquid=liquid, hot=hot, queues=queues, ranked=[m.symbol for m in ranked])

    # ---- what to scan ----------------------------------------------------- #
    def hot_symbols(self) -> List[str]:
        return [c.symbol for c in self.hot]

    def leaders(self, n: int) -> List[str]:
        """The ``n`` hottest liquid stocks of the full scan's ranking (all of them when n is 0 or more
        than there are)."""
        return list(self.ranked[:n] if n > 0 else self.ranked)

    def kept_symbols(self) -> List[str]:
        return [c.symbol for cs in self.kept.values() for c in cs]

    def next_picks(self, per_sector: int, sectors: Optional[Iterable[str]] = None) -> Dict[str, List[Candidate]]:
        allowed = set(sectors) if sectors else None
        return {s: q[:per_sector] for s, q in self.queues.items()
                if q and (allowed is None or s in allowed)}

    # ---- a cycle's outcome ------------------------------------------------ #
    def apply_cycle(self, heat: Mapping[str, float], picks: Mapping[str, List[Candidate]],
                    kept_per_sector: int) -> List[Decision]:
        """Record this cycle's intraday heat, then adopt, keep or drop every
        buffer stock looked at (the new picks and the ones already kept). A hot-list
        name whose candles didn't come this cycle keeps the heat it had: a missed
        request says nothing about the stock, so it isn't made the coolest for it."""
        for c in self.hot:
            c.heat = heat.get(c.symbol, c.heat)
        for sector, chosen in picks.items():
            taken = {c.symbol for c in chosen}
            self.queues[sector] = [c for c in self.queues.get(sector, []) if c.symbol not in taken]
            self.searched[sector] = self.searched.get(sector, 0) + len(chosen)

        contenders = [c for cs in self.kept.values() for c in cs] + [c for cs in picks.values() for c in cs]
        self.kept = {}
        decisions: List[Decision] = []
        for c in sorted(contenders, key=lambda c: heat.get(c.symbol, -1.0), reverse=True):
            c.heat = heat.get(c.symbol)
            if c.heat is None:
                decisions.append(self._decide(c, "dropped", 0.0))        # no data this cycle
                continue
            coolest = min(self.hot, key=lambda h: h.heat or 0.0) if self.hot else None
            if coolest is not None and c.heat > (coolest.heat or 0.0) * (1 + ADOPT_MARGIN):
                self.hot[self.hot.index(coolest)] = c
                decisions.append(self._decide(c, "adopted", c.heat, replaced=coolest.symbol))
            elif len(self.kept.setdefault(c.sector, [])) < kept_per_sector:
                self.kept[c.sector].append(c)
                decisions.append(self._decide(c, "kept", c.heat))
            else:
                decisions.append(self._decide(c, "dropped", c.heat))
        self.decisions = (self.decisions + decisions)[-_DECISIONS_KEPT:]
        return decisions

    def apply_gappers(self, gappers: Sequence[Any], hot_size: int) -> List[Decision]:
        """Before the open (Aziz's gappers watchlist): a stock gapping on pre-market volume is in
        play whatever yesterday's ranking said, so each one, hottest first, takes the hot-list
        slot of the coolest name that isn't gapping itself - within the cap of a third per
        sector - and leaves its sector's queue. A hot-list name already gapping keeps its slot;
        every hot-list name's gap is noted."""
        per_sector_cap = max(1, math.ceil(hot_size / 3))
        known = {c.symbol: c for cs in [self.hot, *self.queues.values(), *self.kept.values()] for c in cs}
        gapping = {g.symbol: g for g in gappers}
        for c in self.hot:
            c.gap_pct = gapping[c.symbol].gap_pct if c.symbol in gapping else None
        decisions: List[Decision] = []
        for g in gappers:
            if any(c.symbol == g.symbol for c in self.hot) or g.symbol not in known:
                continue
            note = f"gapped {g.gap_pct:+.1f}% pre-market on {g.volume:,.0f} shares"
            seen = known[g.symbol]
            candidate = Candidate(g.symbol, seen.sector, seen.daily_heat, gap_pct=g.gap_pct)
            settled = [h for h in self.hot if h.symbol not in gapping]
            same = [h for h in settled if h.sector == seen.sector]
            pool = same if sum(h.sector == seen.sector for h in self.hot) >= per_sector_cap else settled
            if not pool:
                decisions.append(self._decide(candidate, "dropped", g.heat, note=note + " - no hot-list slot free"))
                continue
            coolest = min(pool, key=lambda h: h.daily_heat)
            self.hot[self.hot.index(coolest)] = candidate
            self._forget(g.symbol)
            decisions.append(self._decide(candidate, "adopted", g.heat, replaced=coolest.symbol, note=note))
        self.decisions = (self.decisions + decisions)[-_DECISIONS_KEPT:]
        return decisions

    def apply_wide(self, heat: Mapping[str, float], sector_of: Callable[[str], str],
                   kept_per_sector: int) -> List[Decision]:
        """After a look at every ranked stock: the hot list's heats are refreshed; any stock hotter
        than the coolest hot-list name by the margin takes its slot (within the cap of a third per
        sector, so it replaces a name of its own sector when that sector is full); the best of the
        rest in each sector are kept waiting. Only adoptions and keeps are recorded - a sweep of
        thousands would drown the decisions in drops."""
        per_sector_cap = max(1, math.ceil(len(self.hot) / 3))
        for c in self.hot:
            if c.symbol in heat:
                c.heat = heat[c.symbol]
        known = {c.symbol: c for cs in [*self.queues.values(), *self.kept.values()] for c in cs}
        hot_symbols = {c.symbol for c in self.hot}
        kept: Dict[str, List[Candidate]] = {}
        decisions: List[Decision] = []
        for symbol, h in sorted(heat.items(), key=lambda kv: kv[1], reverse=True):
            if symbol in hot_symbols:
                continue
            seen = known.get(symbol)
            sector = seen.sector if seen else sector_of(symbol)
            if not sector:
                continue
            c = seen or Candidate(symbol, sector, 0.0)
            c.heat = h
            same = [x for x in self.hot if x.sector == sector]
            pool = same if len(same) >= per_sector_cap else self.hot
            coolest = min(pool, key=lambda x: x.heat or 0.0) if pool else None
            if coolest is not None and h > (coolest.heat or 0.0) * (1 + ADOPT_MARGIN):
                self.hot[self.hot.index(coolest)] = c
                hot_symbols = (hot_symbols - {coolest.symbol}) | {symbol}
                self._forget(symbol)
                decisions.append(self._decide(c, "adopted", h, replaced=coolest.symbol,
                                              note="the wide scan found it hotter"))
            elif len(kept.setdefault(sector, [])) < kept_per_sector:
                kept[sector].append(c)
                decisions.append(self._decide(c, "kept", h, note="the wide scan's best in its sector"))
        self.kept = kept
        self.decisions = (self.decisions + decisions)[-_DECISIONS_KEPT:]
        return decisions

    def apply_movers(self, movers: Sequence[Tuple[str, str, float, str]], limit: int) -> List[Decision]:
        """The biggest movers - ``(symbol, sector, heat, why)``, biggest first - hold hot-list
        slots: each one not already hot takes the slot of the coolest hot-list name that isn't one
        of them, whatever its sector (a stock in play trumps the sector cap), and leaves its queue.
        At most ``limit`` of them; a mover already on the list keeps its slot and its reason."""
        chosen = list(movers[:max(0, int(limit))])
        mover_symbols = {m[0] for m in chosen}
        known = {c.symbol: c for cs in [*self.queues.values(), *self.kept.values()] for c in cs}
        decisions: List[Decision] = []
        for symbol, sector, heat, why in chosen:
            held = next((h for h in self.hot if h.symbol == symbol), None)
            if held is not None:
                held.heat, held.why = heat, why
                continue
            if not sector:
                continue
            seen = known.get(symbol)
            c = seen or Candidate(symbol, sector, 0.0)
            c.heat, c.why = heat, why
            pool = [h for h in self.hot if h.symbol not in mover_symbols]
            if not pool:
                decisions.append(self._decide(c, "dropped", heat, note=why + " - no hot-list slot free"))
                continue
            coolest = min(pool, key=lambda h: h.heat if h.heat is not None else h.daily_heat)
            self.hot[self.hot.index(coolest)] = c
            self._forget(symbol)
            decisions.append(self._decide(c, "adopted", heat, replaced=coolest.symbol, note=why))
        self.decisions = (self.decisions + decisions)[-_DECISIONS_KEPT:]
        return decisions

    def _forget(self, symbol: str) -> None:
        """Take a stock out of the queues and the kept lists - it has a hot-list slot now."""
        self.queues = {s: [c for c in q if c.symbol != symbol] for s, q in self.queues.items()}
        self.kept = {s: [c for c in q if c.symbol != symbol] for s, q in self.kept.items()}

    @staticmethod
    def _decide(c: Candidate, action: str, heat: float, replaced: str = "", note: str = "") -> Decision:
        return Decision(at=_now_iso(), symbol=c.symbol, sector=c.sector, action=action,
                        heat=round(heat, 4), replaced=replaced, note=note)

    # ---- persistence and the dashboard ----------------------------------- #
    def state(self) -> dict:
        sectors = sorted(set(self.queues) | set(self.kept) | set(self.searched))
        return {
            "session": self.session.isoformat(), "bars_through": self.bars_through.isoformat(),
            "built_at": self.built_at, "universe": self.universe, "liquid": self.liquid,
            "ranked": len(self.ranked),
            "hot": [asdict(c) for c in self.hot],
            "sectors": [{"sector": s, "kept": [asdict(c) for c in self.kept.get(s, [])],
                         "queued": len(self.queues.get(s, [])), "searched": self.searched.get(s, 0)}
                        for s in sectors],
            "decisions": [asdict(d) for d in reversed(self.decisions)],
        }

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        doc = {**self.state(), "queues": {s: [asdict(c) for c in q] for s, q in self.queues.items()},
               "kept": {s: [asdict(c) for c in q] for s, q in self.kept.items()},
               "searched": self.searched, "ranked": list(self.ranked)}
        (directory / f"watchlist_{self.session.isoformat()}.json").write_text(json.dumps(doc), encoding="utf-8")
        for old in sorted(directory.glob("watchlist_*.json"))[:-5]:
            old.unlink(missing_ok=True)

    @staticmethod
    def saved(directory: Path, session: dt.date) -> Optional[dict]:
        """A session's watchlist as it stood when last saved, while its file is still kept."""
        try:
            return json.loads((directory / f"watchlist_{session.isoformat()}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @classmethod
    def load_latest(cls, directory: Path) -> Optional["DayWatchlist"]:
        for path in sorted(directory.glob("watchlist_*.json"), reverse=True):
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                return cls(
                    session=dt.date.fromisoformat(d["session"]),
                    bars_through=dt.date.fromisoformat(d["bars_through"]),
                    built_at=d["built_at"], universe=d["universe"], liquid=d["liquid"],
                    hot=[Candidate(**c) for c in d["hot"]],
                    queues={s: [Candidate(**c) for c in q] for s, q in d["queues"].items()},
                    kept={s: [Candidate(**c) for c in q] for s, q in d["kept"].items()},
                    searched=d.get("searched", {}),
                    decisions=[Decision(**x) for x in reversed(d.get("decisions", []))],
                    ranked=[str(s) for s in d.get("ranked", [])] if isinstance(d.get("ranked"), list) else [],
                )
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
