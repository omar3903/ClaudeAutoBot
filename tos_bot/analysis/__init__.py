"""Chart-reading primitives shared by the strategies.

* :mod:`levels`  - horizontal support / resistance (Aziz Ch. 7; Murphy chs. on
  S/R). The market remembers price *levels*, not diagonal trend lines.
* :mod:`candles` - single-bar / two-bar candlestick reads (doji, hammer,
  shooting star, engulfing) used as entry triggers.
"""

from .candles import (
    CandleRead,
    classify_candle,
    is_doji,
    is_engulfing,
    is_hammer,
    is_shooting_star,
    read_row,
)
from .levels import Level, SupportResistance, find_levels

__all__ = [
    "CandleRead", "classify_candle", "is_doji", "is_engulfing", "is_hammer",
    "is_shooting_star", "read_row",
    "Level", "SupportResistance", "find_levels",
]
