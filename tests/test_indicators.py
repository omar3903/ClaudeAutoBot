from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from autotradebot.indicators import ta


@pytest.fixture
def frame():
    idx = pd.date_range("2026-02-02 09:30", periods=300, freq="5min", tz="America/New_York")
    rng = np.random.default_rng(1)
    close = 100 + np.cumsum(rng.normal(0, 0.25, len(idx)))
    return pd.DataFrame(
        {"open": close, "high": close + 0.3, "low": close - 0.3, "close": close,
         "volume": rng.integers(1e4, 8e4, len(idx)).astype(float)},
        index=idx,
    )


def test_rsi_bounds(frame):
    r = ta.rsi(frame["close"], 14).dropna()
    assert (r >= 0).all() and (r <= 100).all()


def test_ema_tracks_price(frame):
    e = ta.ema(frame["close"], 20)
    assert abs(e.iloc[-1] - frame["close"].iloc[-1]) < frame["close"].std()


def test_atr_positive(frame):
    a = ta.atr(frame, 14).dropna()
    assert (a > 0).all()


def test_bollinger_contains_price_mostly(frame):
    bb = ta.bollinger(frame["close"], 20, 2.0)
    ok = ((frame["close"] >= bb["lower"]) & (frame["close"] <= bb["upper"])).dropna()
    assert ok.mean() > 0.7


def test_session_vwap_resets_daily():
    idx = pd.date_range("2026-02-02 09:30", periods=160, freq="5min", tz="America/New_York")
    df = pd.DataFrame({"open": 10, "high": 10.1, "low": 9.9, "close": 10.0, "volume": 1000.0}, index=idx)
    v = ta.session_vwap(df)
    assert abs(v.iloc[-1] - 10.0) < 1e-6


def test_opening_range_shape(frame):
    orr = ta.opening_range(frame, 15)
    assert set(["or_high", "or_low"]).issubset(orr.columns)
    assert (orr["or_high"] >= orr["or_low"]).dropna().all()


def _session(day, bars, volume):
    idx = pd.date_range(pd.Timestamp(f"{day} 09:30", tz="America/New_York"), periods=bars, freq="5min")
    return pd.DataFrame({"open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
                         "volume": np.full(bars, volume)}, index=idx)


def test_relative_volume_compares_the_same_time_of_day():
    # two full sessions at 1,000 a bar, then the first 50 minutes of today at 2,000 a bar
    frame = pd.concat([_session("2026-02-02", 78, 1000.0), _session("2026-02-03", 78, 1000.0),
                       _session("2026-02-04", 10, 2000.0)])
    assert ta.rel_volume_intraday(frame) == pytest.approx(2.0)
    assert ta.rel_volume_intraday(_session("2026-02-04", 10, 2000.0)) == 1.0      # no history: neutral


def test_relative_volume_is_measured_through_the_last_closed_candle_on_both_sides():
    # earlier sessions at 1,000 a bar but 50,000 in the 10:20 candle; today, the 10:20 candle has just begun
    earlier = [_session(day, 78, 1000.0) for day in ("2026-02-02", "2026-02-03")]
    for frame in earlier:
        frame.iloc[10, frame.columns.get_loc("volume")] = 50_000.0
    today = _session("2026-02-04", 11, 2000.0)
    today.iloc[-1, today.columns.get_loc("volume")] = 30.0                       # its first seconds
    assert ta.rel_volume_intraday(pd.concat([*earlier, today])) == pytest.approx(2.0)   # ten closed candles a side
    # only the candle that has just begun: nothing closed today to compare
    assert ta.rel_volume_intraday(pd.concat([*earlier, _session("2026-02-04", 1, 30.0)])) == 1.0


def test_beta_measures_how_much_a_stock_moves_with_the_market():
    idx = pd.bdate_range("2025-01-01", periods=250, tz="America/New_York")
    market_returns = np.random.default_rng(3).normal(0, 0.01, len(idx))
    market = pd.Series(100 * np.cumprod(1 + market_returns), index=idx)
    stock = pd.Series(50 * np.cumprod(1 + 2 * market_returns), index=idx)
    assert ta.beta(stock, market) == pytest.approx(2.0)
    assert np.isnan(ta.beta(stock.tail(30), market.tail(30)))                   # too short to say
