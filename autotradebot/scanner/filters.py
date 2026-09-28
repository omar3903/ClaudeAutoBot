"""What the bot may look for and trade (set from the dashboard), and how plays are ranked."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple

from ..core.models import Play
from ..data.sectors import clean_sector_list, sector_allowed

SIDES = ("LONG", "SHORT")
TIMEFRAMES = ("INTRADAY", "SWING")


@dataclass(frozen=True)
class TradeFilters:
    """Applied by the scanner (what it looks for) and at execution (what may be
    entered, by you or Autopilot). The dashboard replaces it as a whole."""

    sides: Tuple[str, ...] = SIDES
    timeframes: Tuple[str, ...] = TIMEFRAMES
    sectors: Tuple[str, ...] = ()                  # () = every sector

    @classmethod
    def build(cls, sides: Optional[Iterable[str]] = None, timeframes: Optional[Iterable[str]] = None,
              sectors: Optional[Iterable[str]] = None) -> "TradeFilters":
        """Normalise dashboard input. At least one side and one timeframe stay on."""
        s, t = _pick(sides, SIDES), _pick(timeframes, TIMEFRAMES)
        if not s:
            raise ValueError("Keep at least one of Long / Short switched on.")
        if not t:
            raise ValueError("Keep at least one of Intraday / Swing switched on.")
        return cls(sides=s, timeframes=t, sectors=tuple(clean_sector_list(sectors)))

    def refusal(self, side: str, timeframe: str, sector: Optional[str]) -> Optional[str]:
        """Why a play with these traits is filtered out, or None if it's allowed."""
        if side not in self.sides:
            return f"{side.lower()} plays are switched off in the filters"
        if timeframe not in self.timeframes:
            return f"{'intraday' if timeframe == 'INTRADAY' else 'swing'} plays are switched off in the filters"
        if not sector_allowed(sector, self.sectors):
            return f"{sector or 'Unknown'} sector is switched off in the Sectors filter"
        return None

    def allows(self, play: Play) -> bool:
        return self.refusal(play.side.value, play.timeframe.value, play.sector) is None

    def as_dict(self) -> dict:
        return {"sides": list(self.sides), "timeframes": list(self.timeframes), "sectors": list(self.sectors)}

    def describe(self) -> str:
        sides = " + ".join(x.lower() for x in self.sides)
        tfs = " + ".join("intraday" if x == "INTRADAY" else "swing" for x in self.timeframes)
        return f"Scanning and trading {sides} · {tfs} · {', '.join(self.sectors) or 'every sector'}."


def _pick(values: Optional[Iterable[str]], allowed: Tuple[str, ...]) -> Tuple[str, ...]:
    if values is None:
        return tuple(allowed)
    chosen = {str(v).strip().upper() for v in values}
    return tuple(a for a in allowed if a in chosen)


def expected_r(play: Play, daily_atr: float = math.nan) -> float:
    """What a play is worth per unit of risk, before costs: the odds it pays
    times the reward, less the odds it doesn't. A stop inside the stock's normal
    daily swing gets hit more often than the setup's own odds assume, so the odds
    are cut when the stop is under ~0.4 of the daily range."""
    odds = play.probability
    if daily_atr > 0:
        odds *= min(1.0, 0.6 + abs(play.entry - play.stop) / daily_atr)
    return odds * min(play.reward_risk, 4.0) - (1.0 - odds)


def rank_score(play: Play, activity: Any = None, strategy_weight: float = 1.0) -> float:
    """One comparable number per play (higher = better): its expected value in R,
    scaled by the strategy weight, plus a small bump for unusual volume, a gap (day
    trades) and enough range to move. Reward:risk only counts through the expected
    value, so a stop tightened to fake a big ratio doesn't win the ranking."""
    ev = play.evidence.get("expected_r")
    ev = expected_r(play) if ev is None else float(ev)
    score = max(0.0, 0.5 + 0.25 * ev) * max(0.1, strategy_weight)
    if activity is not None:
        rvol = float(getattr(activity, "rvol", 1.0) or 1.0)
        if play.is_day_trade:
            score += 0.05 * min(2.5, max(0.0, rvol - 1.0))
            score += 0.03 * min(3.0, abs(float(getattr(activity, "gap_pct", 0.0) or 0.0)) / 2.0)
        score += 0.02 * min(2.0, float(getattr(activity, "atr_pct", 0.0) or 0.0) / 2.0)
    if play.kind.value == "FUNDAMENTAL":
        score += 0.05
    return round(score, 4)
