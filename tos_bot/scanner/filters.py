"""Pre-filter (cheap, per symbol) and the blended rank score for plays."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

import numpy as np
import pandas as pd

from ..core.models import Play, Quote, ScanCandidate
from ..data.sectors import clean_sector_list, sector_allowed
from ..indicators import ta


def passes_prefilter(
    symbol: str,
    daily: pd.DataFrame,
    intraday: Optional[pd.DataFrame],
    quote: Optional[Quote],
    pf: dict,
) -> Optional[ScanCandidate]:
    """Return a :class:`ScanCandidate` if the name is liquid / volatile enough
    to be worth running strategies on, else ``None``."""
    if daily is None or len(daily) < 30:
        return None
    close = daily["close"]
    price = float(quote.last) if (quote and quote.last) else float(close.iloc[-1])

    if not (pf.get("min_price", 0) <= price <= pf.get("max_price", 1e9)):
        return None

    dollar_vol = float((close * daily["volume"]).tail(20).mean())
    if dollar_vol < pf.get("min_dollar_volume", 0):
        return None

    atrp = float(ta.atr_pct(daily, 14).iloc[-1])
    if math.isnan(atrp) or atrp < pf.get("min_atr_pct", 0):
        return None

    spread_bps = quote.spread_bps if quote else 0.0
    if spread_bps and spread_bps > pf.get("max_spread_bps", 1e9):
        return None

    prev_close = float(close.iloc[-2]) if len(close) > 1 else price
    change_pct = (price / prev_close - 1.0) * 100.0 if prev_close else 0.0

    gap_pct = 0.0
    rvol = 1.0
    if intraday is not None and len(intraday) > 3:
        ny = intraday.index.tz_convert("America/New_York")
        today = intraday[ny.date == ny.date.max()]
        if len(today):
            gap_pct = (float(today["open"].iloc[0]) / prev_close - 1.0) * 100.0
        try:
            rvol = ta.rel_volume_intraday(intraday)
        except Exception:  # noqa: BLE001
            rvol = 1.0

    return ScanCandidate(
        symbol=symbol, price=round(price, 2), dollar_volume=round(dollar_vol, 0),
        atr_pct=round(atrp, 2), rvol=round(rvol, 2), gap_pct=round(gap_pct, 2),
        change_pct=round(change_pct, 2), spread_bps=round(spread_bps, 1),
    )


def rank_score(play: Play, cand: Optional[ScanCandidate], strategy_weight: float = 1.0) -> float:
    """Blend the ingredients into one comparable number (higher = better).

    confidence (strategy's own conviction)   x strategy weight
    + reward:risk contribution (capped)
    + activity bonus (relative volume / gap for intraday)
    + a small kicker for fundamental setups that also have technical alignment
    """
    conf = max(0.0, min(1.0, play.confidence))
    rr = min(play.reward_risk, 4.0) / 4.0
    base = (0.6 * conf + 0.4 * rr) * max(0.1, strategy_weight)

    activity = 0.0
    if cand is not None:
        if play.is_day_trade:
            activity += 0.10 * min(2.5, max(0.0, cand.rvol - 1.0))
            activity += 0.05 * min(3.0, abs(cand.gap_pct) / 2.0)
        activity += 0.03 * min(2.0, cand.atr_pct / 2.0)

    kind_bonus = 0.05 if play.kind.value == "FUNDAMENTAL" else 0.0
    return round(base + activity + kind_bonus, 4)


# --------------------------------------------------------------------------- #
#  What the bot scans for and may trade - set from the dashboard             #
# --------------------------------------------------------------------------- #
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
    def build(cls, sides: Optional[Iterable[str]] = None,
              timeframes: Optional[Iterable[str]] = None,
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

    def allows(self, play) -> bool:
        return self.refusal(play.side.value, play.timeframe.value, play.sector) is None

    def as_dict(self) -> dict:
        return {"sides": list(self.sides), "timeframes": list(self.timeframes),
                "sectors": list(self.sectors)}

    def describe(self) -> str:
        sides = " + ".join(x.lower() for x in self.sides)
        tfs = " + ".join("intraday" if x == "INTRADAY" else "swing" for x in self.timeframes)
        secs = ", ".join(self.sectors) if self.sectors else "every sector"
        return f"Scanning and trading {sides} · {tfs} · {secs}."


def _pick(values: Optional[Iterable[str]], allowed: Tuple[str, ...]) -> Tuple[str, ...]:
    if values is None:
        return tuple(allowed)
    chosen = {str(v).strip().upper() for v in values}
    return tuple(a for a in allowed if a in chosen)
