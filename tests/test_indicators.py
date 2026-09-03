from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tos_bot.indicators import ta


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
