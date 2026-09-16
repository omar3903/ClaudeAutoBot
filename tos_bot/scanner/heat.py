"""How "in play" a stock is.

**Daily heat** ranks the whole universe before the open using only the stored
daily candles - no requests: unusual volume in the last session, a big move for
the stock's usual range, a close near a recent high or low, enough daily range
to trade and enough dollar volume to get in and out. Each ingredient is turned
into a percentile across the universe so different units combine fairly.

**Intraday heat** re-ranks the hot list and the buffer during the session from
today's 5-minute candles, on an absolute 0-1 scale so a buffer stock can be
compared with a hot-list stock in the same cycle.

**Gapper heat** ranks, just before the open, the stocks that have gapped on
pre-market volume - Aziz's "gappers" watchlist: the gap in average true ranges
and the pre-market dollar volume, each as a percentile among the gappers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import List, Mapping, Optional

import numpy as np
import pandas as pd

from ..indicators import ta

_DAILY_WEIGHTS = {"rvol": 0.35, "move_atr": 0.25, "extreme": 0.15, "atr_pct": 0.15, "dollar_volume": 0.10}


@dataclass(frozen=True)
class DailyMetrics:
    symbol: str
    price: float
    dollar_volume: float        # 20-session average
    atr_pct: float              # 14-session average true range, % of price
    rvol: float                 # last session's volume / its 20-session average
    move_atr: float             # last session's move, in average true ranges
    extreme: float              # 0 mid-range .. 1 at a 20-session high or low
    heat: float = 0.0


def daily_metrics(symbol: str, daily: pd.DataFrame) -> Optional[DailyMetrics]:
    if daily is None or len(daily) < 30:
        return None
    tail = daily.iloc[-61:]
    high, low, close, volume = (tail[c].to_numpy(dtype=float) for c in ("high", "low", "close", "volume"))
    prev = close[:-1]
    true_range = np.maximum(high[1:] - low[1:], np.maximum(abs(high[1:] - prev), abs(low[1:] - prev)))
    atr = true_range[-14:].mean()
    price = close[-1]
    avg_volume = volume[-21:-1].mean()
    hi, lo = high[-20:].max(), low[-20:].min()
    if price <= 0 or atr <= 0 or avg_volume <= 0:
        return None
    position = (price - lo) / (hi - lo) if hi > lo else 0.5
    return DailyMetrics(
        symbol=symbol,
        price=float(price),
        dollar_volume=float((close[-20:] * volume[-20:]).mean()),
        atr_pct=float(atr / price * 100.0),
        rvol=float(volume[-1] / avg_volume),
        move_atr=float(abs(close[-1] - close[-2]) / atr),
        extreme=float(abs(position - 0.5) * 2.0),
    )


def liquid(m: DailyMetrics, prefilter: Mapping[str, float]) -> bool:
    return (prefilter.get("min_price", 0.0) <= m.price <= prefilter.get("max_price", float("inf"))
            and m.dollar_volume >= prefilter.get("min_dollar_volume", 0.0)
            and m.atr_pct >= prefilter.get("min_atr_pct", 0.0))


def rank_by_daily_heat(metrics: List[DailyMetrics]) -> List[DailyMetrics]:
    """The same stocks with ``heat`` filled in, hottest first."""
    if not metrics:
        return []
    heat = np.zeros(len(metrics))
    for field, weight in _DAILY_WEIGHTS.items():
        values = np.array([getattr(m, field) for m in metrics])
        heat += weight * values.argsort().argsort() / max(1, len(values) - 1)
    return sorted((replace(m, heat=round(float(h), 4)) for m, h in zip(metrics, heat)),
                  key=lambda m: m.heat, reverse=True)


@dataclass(frozen=True)
class IntradayMetrics:
    symbol: str
    rvol: float                 # today's volume so far vs. the same time in recent sessions
    change_pct: float           # vs. the previous close
    gap_pct: float              # today's open vs. the previous close
    range_atr: float            # today's high-low, in daily average true ranges
    atr_pct: float
    heat: float


def intraday_metrics(symbol: str, intraday: pd.DataFrame, daily: pd.DataFrame) -> Optional[IntradayMetrics]:
    if intraday is None or daily is None or len(intraday) < 3 or len(daily) < 15:
        return None
    sessions = intraday.index.date
    today = intraday[sessions == sessions[-1]]
    prior_daily = daily[daily.index.date < sessions[-1]]
    if not len(today) or len(prior_daily) < 15:
        return None
    prev_close = float(prior_daily["close"].iloc[-1])
    atr = float(ta.atr(prior_daily.tail(30), 14).iloc[-1])
    if prev_close <= 0 or not atr or atr != atr:
        return None
    last = float(today["close"].iloc[-1])
    change_pct = (last / prev_close - 1.0) * 100.0
    atr_pct = atr / prev_close * 100.0
    rvol = ta.rel_volume_intraday(intraday)
    range_atr = float(today["high"].max() - today["low"].min()) / atr
    heat = (0.45 * min(rvol, 5.0) / 5.0
            + 0.30 * min(abs(change_pct) / atr_pct, 3.0) / 3.0
            + 0.25 * min(range_atr, 3.0) / 3.0)
    return IntradayMetrics(symbol=symbol, rvol=round(rvol, 2), change_pct=round(change_pct, 2),
                           gap_pct=round((float(today["open"].iloc[0]) / prev_close - 1.0) * 100.0, 2),
                           range_atr=round(range_atr, 2), atr_pct=round(atr_pct, 2), heat=round(heat, 4))


# ---------------------------------------------------------------- before the open
@dataclass(frozen=True)
class GapperMetrics:
    symbol: str
    prev_close: float
    last: float                 # the latest pre-market price
    gap_pct: float              # vs. the previous close
    gap_atr: float              # the gap in daily average true ranges
    volume: float               # pre-market shares
    dollar_volume: float
    high: float                 # the pre-market high and low: the day's first levels
    low: float
    heat: float = 0.0

    def as_dict(self) -> dict:
        return {"gap_pct": self.gap_pct, "gap_atr": self.gap_atr, "volume": self.volume,
                "dollar_volume": round(self.dollar_volume), "high": self.high, "low": self.low,
                "last": self.last, "prev_close": self.prev_close, "heat": self.heat}


def premarket_metrics(symbol: str, pre: Optional[pd.DataFrame],
                      daily: Optional[pd.DataFrame]) -> Optional[GapperMetrics]:
    """What a stock's pre-market candles say against its completed daily candles."""
    if pre is None or daily is None or not len(pre) or len(daily) < 15:
        return None
    prior = daily[daily.index.date < pre.index[-1].date()]
    if len(prior) < 15:
        return None
    prev_close = float(prior["close"].iloc[-1])
    atr = float(ta.atr(prior.tail(30), 14).iloc[-1])
    if prev_close <= 0 or not atr or atr != atr:
        return None
    last, volume = float(pre["close"].iloc[-1]), float(pre["volume"].sum())
    return GapperMetrics(
        symbol=symbol, prev_close=round(prev_close, 4), last=round(last, 4),
        gap_pct=round((last / prev_close - 1.0) * 100.0, 2), gap_atr=round((last - prev_close) / atr, 2),
        volume=round(volume), dollar_volume=float((pre["close"] * pre["volume"]).sum()),
        high=round(float(pre["high"].max()), 4), low=round(float(pre["low"].min()), 4))


def rank_gappers(metrics: List[GapperMetrics], min_gap_pct: float, min_volume: float) -> List[GapperMetrics]:
    """The stocks in play before the open, hottest first: those gapping at least ``min_gap_pct``
    either way on at least ``min_volume`` pre-market shares, ranked by the size of the gap in
    ATRs (60%) and the pre-market dollar volume (40%). The top one's heat is 1."""
    live = [m for m in metrics if abs(m.gap_pct) >= min_gap_pct and m.volume >= min_volume]
    if not live:
        return []
    heat = np.zeros(len(live))
    for name, weight in (("gap_atr", 0.6), ("dollar_volume", 0.4)):
        values = np.array([abs(getattr(m, name)) for m in live])
        heat += weight * (values.argsort().argsort() + 1) / len(values)
    return sorted((replace(m, heat=round(float(h), 4)) for m, h in zip(live, heat)),
                  key=lambda m: m.heat, reverse=True)
