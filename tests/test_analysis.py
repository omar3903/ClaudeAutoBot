"""Chart-reading primitives: horizontal S/R, candlestick reads, oscillator
divergence (the pieces the Aziz / Murphy strategies are built on)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from autotradebot.analysis import (
    classify_candle,
    find_levels,
    is_doji,
    is_engulfing,
    is_hammer,
    is_shooting_star,
    read_row,
)
from autotradebot.indicators import ta


# --------------------------------------------------------------------------- #
#  Candles
# --------------------------------------------------------------------------- #
def test_doji_and_hammer_and_star():
    assert is_doji(100.0, 100.6, 99.4, 100.02)
    assert is_hammer(100.0, 100.1, 98.0, 99.9)          # long lower wick, tiny body/upper
    assert is_shooting_star(100.0, 102.0, 99.9, 100.1)  # long upper wick
    assert not is_hammer(100.0, 102.0, 99.9, 101.8)


def test_engulfing_both_ways():
    assert is_engulfing(99.0, 101.2, 98.9, 101.0, po=100.5, pc=99.5) == "bull_engulf"
    assert is_engulfing(101.0, 101.1, 98.8, 99.0, po=99.5, pc=100.5) == "bear_engulf"
    assert is_engulfing(100.0, 100.5, 99.5, 100.2, po=100.1, pc=100.3) is None


def test_classify_candle_reversal_flags():
    up = classify_candle(100.0, 100.1, 98.0, 99.9)      # hammer
    assert up.is_reversal_up and not up.is_reversal_down
    dn = classify_candle(100.0, 102.0, 99.9, 100.1)     # shooting star
    assert dn.is_reversal_down and not dn.is_reversal_up
    ind = classify_candle(100.0, 100.6, 99.4, 100.02)   # doji
    assert ind.indecision


def test_read_row_uses_prev_bar_for_engulfing():
    df = pd.DataFrame(
        {"open": [100.5, 99.0], "high": [100.7, 101.2],
         "low": [99.4, 98.9], "close": [99.5, 101.0], "volume": [1e4, 2e4]},
        index=pd.date_range("2026-02-02 09:30", periods=2, freq="5min", tz="America/New_York"),
    )
    assert read_row(df, -1).name == "bull_engulf"


# --------------------------------------------------------------------------- #
#  Support / resistance
# --------------------------------------------------------------------------- #
@pytest.fixture
def daily_with_shelf():
    """90 sessions oscillating in a clean channel that rejects 110 (resistance)
    and holds 95 (support) over and over - the market 'remembers' both."""
    idx = pd.date_range("2025-01-02", periods=90, freq="B", tz="America/New_York")
    # a triangle wave between 95 and 110
    t = np.arange(90)
    close = 102.5 + 7.5 * np.sin(t / 5.0)
    close = np.clip(close, 95.0, 110.0)
    hi = np.clip(close + 0.4, None, 110.4)
    lo = np.clip(close - 0.4, 94.6, None)
    return pd.DataFrame({"open": close, "high": hi, "low": lo, "close": close,
                         "volume": np.full(90, 2e6)}, index=idx)


def test_find_levels_ranks_and_locates(daily_with_shelf):
    sr = find_levels(daily_with_shelf, last_price=102.5)
    assert sr.levels, "should find at least one level"
    assert all(0.0 <= l.strength <= 1.0 for l in sr.levels)
    below = sr.nearest_below(102.5)
    above = sr.nearest_above(102.5)
    assert below is not None and below.price < 102.5
    assert above is not None and above.price > 102.5
    # the repeatedly-touched extremes should surface as levels near the channel edges
    assert min(l.price for l in sr.levels) < 99.0
    assert max(l.price for l in sr.levels) > 106.0


def test_find_levels_empty_on_thin_data():
    sr = find_levels(pd.DataFrame(), last_price=50.0)
    assert sr.levels == []
    assert sr.nearest_below() is None and sr.nearest_above() is None


# --------------------------------------------------------------------------- #
#  Divergence
# --------------------------------------------------------------------------- #
def test_rsi_bullish_divergence_detected():
    # price: lower low into the end; momentum: shallower dip -> bullish divergence
    n = 80
    base = np.concatenate([
        np.linspace(100, 90, 20),     # first low ~90
        np.linspace(90, 97, 15),
        np.linspace(97, 86, 25),      # second, LOWER low ~86
        np.linspace(86, 92, 20),
    ])
    close = pd.Series(base + np.random.default_rng(3).normal(0, 0.15, n))
    rsi = ta.rsi(close, 14)
    assert ta.rsi_divergence(close, rsi, lookback=70) in ("bullish", "")  # never crashes
    # construct a cleaner case that must read bullish
    price = pd.Series([10, 8, 6, 5, 6, 8, 7, 5, 4, 5, 6, 7] * 4, dtype=float)
    mom = pd.Series([30, 24, 18, 15, 22, 28, 26, 20, 24, 28, 30, 32] * 4, dtype=float)
    assert ta.rsi_divergence(price, mom, lookback=40) == "bullish"


def test_consecutive_run_sign_and_magnitude():
    up = pd.Series([1, 2, 3, 4, 5, 6], dtype=float)
    dn = pd.Series([9, 8, 7, 6], dtype=float)
    assert ta.consecutive_run(up) >= 4
    assert ta.consecutive_run(dn) <= -2
