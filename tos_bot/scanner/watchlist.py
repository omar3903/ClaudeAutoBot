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
"""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional

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


@dataclass
class Decision:
    at: str
    symbol: str
    sector: str
    action: str                           # adopted | kept | dropped
    heat: float
    replaced: str = ""


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
                   universe=universe, liquid=liquid, hot=hot, queues=queues)

    # ---- what to scan ----------------------------------------------------- #
    def hot_symbols(self) -> List[str]:
        return [c.symbol for c in self.hot]

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
        buffer stock looked at (the new picks and the ones already kept)."""
        for c in self.hot:
            c.heat = heat.get(c.symbol, 0.0)
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

    @staticmethod
    def _decide(c: Candidate, action: str, heat: float, replaced: str = "") -> Decision:
        return Decision(at=_now_iso(), symbol=c.symbol, sector=c.sector, action=action,
                        heat=round(heat, 4), replaced=replaced)

    # ---- persistence and the dashboard ----------------------------------- #
    def state(self) -> dict:
        sectors = sorted(set(self.queues) | set(self.kept) | set(self.searched))
        return {
            "session": self.session.isoformat(), "bars_through": self.bars_through.isoformat(),
            "built_at": self.built_at, "universe": self.universe, "liquid": self.liquid,
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
               "searched": self.searched}
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
                )
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
