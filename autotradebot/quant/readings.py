"""What the quantitative models say about one stock at one moment.

The scans ask on every play and the replay on every bar, so a reading is either cheap
(the Hurst exponent, variance ratio and half-life are a few vector operations on a few
hundred closes) or cached per stock and completed session (the GARCH forecast).

- **Price character** (Chan, *Algorithmic Trading* ch. 2 and 6): trending when the Hurst
  exponent is at least 0.55 or the variance ratio's z-score is at least +2, mean reverting
  at 0.45 or below / -2 or below, otherwise a random walk. Day trades are read on the last
  three sessions of 5-minute closes, swing trades on the last 120 daily closes.
- **Tomorrow's volatility** (Tsay ch. 3): the GARCH(1,1) forecast from completed daily
  candles only - a partial candle for today would be look-ahead in the replay.
"""

from __future__ import annotations

import datetime as dt
import math
import threading
from collections import OrderedDict
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from . import stationarity, volatility

TRENDING_HURST = 0.55
REVERTING_HURST = 0.45
VARIANCE_RATIO_Z = 2.0
INTRADAY_BARS = 3 * 78
DAILY_BARS = 120
MIN_BARS = 40

VOL_HISTORY = 500
VOL_CACHE_SIZE = 4096
_vol_cache: "OrderedDict[tuple, Optional[Dict[str, Any]]]" = OrderedDict()
_vol_lock = threading.Lock()


def classify(hurst: Optional[float], ratio_z: Optional[float], trending_hurst: float = TRENDING_HURST,
             reverting_hurst: float = REVERTING_HURST) -> str:
    h = hurst if hurst is not None and math.isfinite(hurst) else None
    z = ratio_z if ratio_z is not None and math.isfinite(ratio_z) else None
    if (h is not None and h >= trending_hurst) or (z is not None and z >= VARIANCE_RATIO_Z):
        return "trending"
    if (h is not None and h <= reverting_hurst) or (z is not None and z <= -VARIANCE_RATIO_Z):
        return "mean reverting"
    return "random walk"


def price_character(closes) -> Optional[Dict[str, Any]]:
    """The Hurst exponent, variance ratio and half-life of a run of closes, and what they add up to."""
    prices = stationarity.clean(closes)
    prices = prices[prices > 0]
    if len(prices) < MIN_BARS:
        return None
    z = np.log(prices)
    hurst = stationarity.hurst(z)
    ratio, ratio_z = stationarity.variance_ratio(z, k=2)
    life = stationarity.half_life(z)
    return {"character": classify(hurst, ratio_z), "hurst": _round(hurst, 3), "variance_ratio": _round(ratio, 3),
            "variance_ratio_z": _round(ratio_z, 2), "half_life_bars": _round(life, 1), "bars": int(len(z))}


def completed_daily(daily: pd.DataFrame, today: Optional[dt.date]) -> pd.DataFrame:
    """The daily candles without today's, which is still forming while the market is open."""
    if today is not None and len(daily) and daily.index[-1].date() >= today:
        return daily.iloc[:-1]
    return daily


def vol_forecast(symbol: str, daily: Optional[pd.DataFrame], today: Optional[dt.date] = None) -> Optional[Dict[str, Any]]:
    """Tomorrow's daily volatility (see volatility.next_day_vol), fitted once per stock and session."""
    if daily is None:
        return None
    done = completed_daily(daily, today)
    if len(done) < 61:
        return None
    key = (symbol, done.index[-1].date(), float(done["close"].iloc[-1]), len(done))
    with _vol_lock:
        if key in _vol_cache:
            _vol_cache.move_to_end(key)
            return _vol_cache[key]
    reading = volatility.next_day_vol(done["close"].to_numpy()[-VOL_HISTORY:])
    if reading is not None:
        reading = {k: round(v, 5) if isinstance(v, float) else v for k, v in reading.items()}
    with _vol_lock:
        _vol_cache[key] = reading
        while len(_vol_cache) > VOL_CACHE_SIZE:
            _vol_cache.popitem(last=False)
    return reading


def _round(value: float, digits: int) -> Optional[float]:
    return round(float(value), digits) if value is not None and math.isfinite(value) else None
