"""Noise checks: signs that a play is more likely a false signal than an edge.

A strategy decides whether a setup is there; these checks ask whether this is a
bad moment to take it. Each check is a named flag with a plain reason, and none
of them deletes anything: flagged plays stay on the board (the dashboard hides
them unless asked), Autopilot skips the flags it's told to, and the strategy
replay measures what each check removes - so a check only stays on if the
trades it removes did worse than the ones it keeps.

Trading against the daily trend, VWAP or today's gap, or into heavier opposing
volume, only counts against momentum setups; reversal setups fade the move on
purpose. The play board adds "conflict" when setups on one stock disagree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List

import pandas as pd

from ..core.enums import Side, Timeframe
from ..core.models import Play
from ..strategies.base import StrategyContext

LABELS = {
    "against_trend": "against the daily trend",
    "wrong_side_of_vwap": "on the wrong side of VWAP",
    "against_gap": "against today's gap",
    "volume_against": "heavier volume against it",
    "conflict": "another setup points the other way",
    "low_expected_value": "too little expected value",
}
CHECKS = tuple(LABELS)


@dataclass(frozen=True)
class NoiseSettings:
    gap_pct: float = 2.0            # a gap this big from the prior close sets the day's direction
    volume_ratio: float = 1.5       # opposing volume this many times the supporting volume
    volume_bars: int = 6            # closed 5-minute bars the volume check looks back over
    min_expected_r: float = 0.15    # expected value (in R) below which a play isn't worth the risk

    @classmethod
    def from_config(cls, cfg) -> "NoiseSettings":
        return cls(gap_pct=float(cfg.gap_pct), volume_ratio=float(cfg.volume_ratio),
                   volume_bars=int(cfg.volume_bars), min_expected_r=float(cfg.min_expected_r))


def context_flags(play: Play, ctx: StrategyContext, style: str, settings: NoiseSettings) -> List[str]:
    """The checks that read the market around a play."""
    if style != "momentum":
        return []
    long = play.side is Side.LONG
    flags: List[str] = []
    if ctx.daily_trend() == ("down" if long else "up"):
        flags.append("against_trend")
    today = ctx.today_intraday()
    if play.timeframe is not Timeframe.INTRADAY or today is None or not len(today):
        return flags

    price, vwap = ctx.price, ctx.vwap
    if not math.isnan(vwap) and (price < vwap if long else price > vwap):
        flags.append("wrong_side_of_vwap")
    day_open, prev_close = float(today["open"].iloc[0]), ctx.prev_close()
    gap = (day_open / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
    if (gap <= -settings.gap_pct and price < day_open) if long else (gap >= settings.gap_pct and price > day_open):
        flags.append("against_gap")
    if opposing_volume_ratio(today, long, settings.volume_bars) >= settings.volume_ratio:
        flags.append("volume_against")
    return flags


def opposing_volume_ratio(today: pd.DataFrame, long: bool, bars: int) -> float:
    """Volume on bars moving against the play over volume on bars moving with it,
    across the last closed bars (the one still printing is left out)."""
    closed = today.iloc[:-1].tail(bars)
    up = float(closed.loc[closed["close"] > closed["open"], "volume"].sum())
    down = float(closed.loc[closed["close"] < closed["open"], "volume"].sum())
    supporting, opposing = (up, down) if long else (down, up)
    if supporting <= 0:
        return math.inf if opposing > 0 else 0.0
    return opposing / supporting
