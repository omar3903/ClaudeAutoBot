"""Horizontal support / resistance detection.

Aziz (Ch. 7) and Murphy both stress: *the market remembers price levels, not
diagonal trend lines*. A level earns strength from (a) how many daily swing
highs/lows and closes cluster there, (b) round numbers (whole and half dollars,
which act as invisible S/R), (c) the prior-day close, and (d) the pre-market
high / low.

A level is an **area**, not a number - Aziz uses roughly 5-10c per side.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd


@dataclass
class Level:
    price: float
    kind: str                       # "support" | "resistance" | "pivot"
    strength: float                 # 0..1
    sources: List[str] = field(default_factory=list)

    def contains(self, px: float, tol_pct: float = 0.0015) -> bool:
        return abs(px - self.price) <= max(self.price * tol_pct, 0.03)


class SupportResistance:
    def __init__(self, levels: List[Level], last_price: float) -> None:
        self.levels = sorted(levels, key=lambda l: l.price)
        self.last_price = last_price

    def nearest_below(self, px: Optional[float] = None, min_gap_pct: float = 0.001) -> Optional[Level]:
        px = self.last_price if px is None else px
        cands = [l for l in self.levels if l.price < px * (1 - min_gap_pct)]
        return max(cands, key=lambda l: l.price) if cands else None

    def nearest_above(self, px: Optional[float] = None, min_gap_pct: float = 0.001) -> Optional[Level]:
        px = self.last_price if px is None else px
        cands = [l for l in self.levels if l.price > px * (1 + min_gap_pct)]
        return min(cands, key=lambda l: l.price) if cands else None

    def at(self, px: Optional[float] = None, tol_pct: float = 0.0015) -> Optional[Level]:
        px = self.last_price if px is None else px
        near = [l for l in self.levels if l.contains(px, tol_pct)]
        return max(near, key=lambda l: l.strength) if near else None

    def as_rows(self) -> list:
        return [{"price": round(l.price, 2), "kind": l.kind,
                 "strength": round(l.strength, 2), "sources": l.sources} for l in self.levels]


def _round_number_levels(low: float, high: float) -> List[float]:
    """Whole- and half-dollar levels inside [low, high]. Aziz: these matter
    most under ~$10 but act as invisible S/R at any price."""
    out: List[float] = []
    step = 0.5 if high < 50 else (1.0 if high < 200 else 5.0)
    x = math.floor(low / step) * step
    while x <= high:
        if x >= low:
            out.append(round(x, 2))
        x += step
    return out


def find_levels(
    daily: pd.DataFrame,
    last_price: float,
    intraday: Optional[pd.DataFrame] = None,
    lookback_days: int = 90,
    max_levels: int = 8,
    band_pct: float = 0.30,
) -> SupportResistance:
    """Build the S/R map around ``last_price`` (only levels within ``band_pct``
    of it - Aziz: levels far from the current range don't matter)."""
    if daily is None or len(daily) < 10 or last_price <= 0:
        return SupportResistance([], last_price)

    d = daily.tail(lookback_days)
    lo_band, hi_band = last_price * (1 - band_pct), last_price * (1 + band_pct)

    # candidate pivot prices: local swing highs / lows on the daily chart
    highs = d["high"].to_numpy()
    lows = d["low"].to_numpy()
    closes = d["close"].to_numpy()
    piv: List[tuple] = []                    # (price, weight, source)
    w = 2
    for i in range(w, len(d) - w):
        if highs[i] == max(highs[i - w:i + w + 1]):
            piv.append((highs[i], 1.0, "swing high"))
        if lows[i] == min(lows[i - w:i + w + 1]):
            piv.append((lows[i], 1.0, "swing low"))
    # recent closes carry weight for swing context (Aziz p.216)
    for c in closes[-10:]:
        piv.append((c, 0.5, "recent close"))
    for rn in _round_number_levels(lo_band, hi_band):
        piv.append((rn, 0.6, "round number"))
    if len(daily) >= 2:
        piv.append((float(daily["close"].iloc[-2]), 1.2, "prior-day close"))

    if intraday is not None and len(intraday):
        try:
            ny = intraday.index.tz_convert("America/New_York")
            today = intraday[ny.date == ny.date.max()]
            pre = today[(ny[ny.date == ny.date.max()].hour < 9) |
                        ((ny[ny.date == ny.date.max()].hour == 9) &
                         (ny[ny.date == ny.date.max()].minute < 30))]
            if len(pre):
                piv.append((float(pre["high"].max()), 1.0, "pre-market high"))
                piv.append((float(pre["low"].min()), 1.0, "pre-market low"))
        except Exception:  # noqa: BLE001
            pass

    piv = [(p, wt, s) for (p, wt, s) in piv if lo_band <= p <= hi_band and p > 0]
    if not piv:
        return SupportResistance([], last_price)

    # cluster prices within ~0.4% of each other
    piv.sort(key=lambda t: t[0])
    clusters: List[list] = []
    tol = max(last_price * 0.004, 0.05)
    for price, wt, src in piv:
        if clusters and price - clusters[-1][-1][0] <= tol:
            clusters[-1].append((price, wt, src))
        else:
            clusters.append([(price, wt, src)])

    raw: List[Level] = []
    for cl in clusters:
        wsum = sum(wt for _, wt, _ in cl)
        price = sum(p * wt for p, wt, _ in cl) / wsum
        srcs = sorted({s for _, _, s in cl})
        kind = "support" if price < last_price else "resistance"
        raw.append(Level(price=round(float(price), 3), kind=kind,
                         strength=float(wsum), sources=srcs))

    if raw:
        mx = max(l.strength for l in raw) or 1.0
        for l in raw:
            l.strength = round(min(1.0, 0.25 + 0.75 * l.strength / mx), 3)

    raw.sort(key=lambda l: l.strength, reverse=True)
    return SupportResistance(raw[:max_levels], last_price)
