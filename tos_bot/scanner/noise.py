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

Three checks come from the books' statistics (see quant/):

- a momentum setup on a price that has been mean reverting - its Hurst exponent or
  variance ratio says moves get taken back - is "not_trending" (Chan, *Algorithmic
  Trading* ch. 2 and 6: test for momentum before trading it);
- a reversal setup on a price that has been trending is "not_mean_reverting";
- a momentum setup while the market is in its turbulent regime (Hamilton's Markov
  switching model, ch. 22) is "turbulent_market" - Chan (ch. 8) finds momentum
  suffers in high volatility while short-term reversal does better.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List

import pandas as pd

from ..core.enums import Side, Timeframe
from ..core.models import Play
from ..quant import readings
from ..strategies.base import StrategyContext

LABELS = {
    "against_trend": "against the daily trend",
    "wrong_side_of_vwap": "on the wrong side of VWAP",
    "against_gap": "against today's gap",
    "volume_against": "heavier volume against it",
    "conflict": "another setup points the other way",
    "low_expected_value": "too little expected value",
    "not_trending": "the price keeps snapping back - breakouts in it tend to fail",
    "not_mean_reverting": "the price has been trending - fading it fights the trend",
    "turbulent_market": "the market is in its turbulent regime",
}
CHECKS = tuple(LABELS)
#: the checks from the books' statistics; Autopilot also skips one once the replay shows it helps
QUANT_CHECKS = ("not_trending", "not_mean_reverting", "turbulent_market")


@dataclass(frozen=True)
class NoiseSettings:
    gap_pct: float = 2.0            # a gap this big from the prior close sets the day's direction
    volume_ratio: float = 1.5       # opposing volume this many times the supporting volume
    volume_bars: int = 6            # closed 5-minute bars the volume check looks back over
    min_expected_r: float = 0.15    # expected value (in R) below which a play isn't worth the risk
    trending_hurst: float = readings.TRENDING_HURST
    reverting_hurst: float = readings.REVERTING_HURST
    turbulent_probability: float = 0.7

    @classmethod
    def from_config(cls, cfg) -> "NoiseSettings":
        return cls(gap_pct=float(cfg.gap_pct), volume_ratio=float(cfg.volume_ratio),
                   volume_bars=int(cfg.volume_bars), min_expected_r=float(cfg.min_expected_r),
                   trending_hurst=float(getattr(cfg, "trending_hurst", readings.TRENDING_HURST)),
                   reverting_hurst=float(getattr(cfg, "reverting_hurst", readings.REVERTING_HURST)),
                   turbulent_probability=float(getattr(cfg, "turbulent_probability", 0.7)))


def context_flags(play: Play, ctx: StrategyContext, style: str, settings: NoiseSettings) -> List[str]:
    """The checks that read the market around a play."""
    flags = quant_flags(play, ctx, style, settings)
    if style != "momentum":
        return flags
    long = play.side is Side.LONG
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


def quant_flags(play: Play, ctx: StrategyContext, style: str, settings: NoiseSettings) -> List[str]:
    """The checks from the price's statistics and the market's regime."""
    if style not in ("momentum", "reversal"):
        return []
    flags: List[str] = []
    reading = ctx.price_character(play.timeframe is Timeframe.INTRADAY)
    if reading is not None:
        kind = readings.classify(reading["hurst"], reading["variance_ratio_z"],
                                 settings.trending_hurst, settings.reverting_hurst)
        if style == "momentum" and kind == "mean reverting":
            flags.append("not_trending")
        elif style == "reversal" and kind == "trending":
            flags.append("not_mean_reverting")
    turbulent = (ctx.market or {}).get("p_turbulent")
    if style == "momentum" and turbulent is not None and turbulent >= settings.turbulent_probability:
        flags.append("turbulent_market")
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
