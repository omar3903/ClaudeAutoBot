"""Vectorised technical indicators.

Every function takes pandas Series / DataFrame and returns the same, so the
strategies stay one-liners. OHLCV frames are expected to have lowercase
columns ``open, high, low, close, volume`` and a tz-aware DatetimeIndex.
No TA-Lib dependency - all pure pandas/numpy so it installs anywhere.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
#  Moving averages / oscillators                                             #
# --------------------------------------------------------------------------- #
def sma(s: pd.Series, length: int) -> pd.Series:
    return s.rolling(length, min_periods=length).mean()


def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False, min_periods=length).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    roll_up = up.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    roll_down = down.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    rs = roll_up / roll_down.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    return out.fillna(100.0).where(roll_down != 0, 100.0)


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": hist})


def zscore(s: pd.Series, length: int = 20) -> pd.Series:
    mean = s.rolling(length, min_periods=length).mean()
    std = s.rolling(length, min_periods=length).std(ddof=0)
    return (s - mean) / std.replace(0.0, np.nan)


def linreg_slope(s: pd.Series, length: int = 20) -> pd.Series:
    """Slope of an OLS line through the last `length` points, per bar,
    normalised by price so it reads like a % move per bar."""
    idx = np.arange(length)
    denom = (idx - idx.mean()) ** 2
    denom_sum = denom.sum()

    def _slope(window: np.ndarray) -> float:
        y = window
        x = idx - idx.mean()
        return float((x * (y - y.mean())).sum() / denom_sum)

    raw = s.rolling(length, min_periods=length).apply(_slope, raw=True)
    return raw / s.replace(0.0, np.nan)


# --------------------------------------------------------------------------- #
#  Range / volatility                                                        #
# --------------------------------------------------------------------------- #
def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def atr_pct(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return atr(df, length) / df["close"] * 100.0


def bollinger(
    close: pd.Series, length: int = 20, mult: float = 2.0
) -> pd.DataFrame:
    mid = sma(close, length)
    std = close.rolling(length, min_periods=length).std(ddof=0)
    upper = mid + mult * std
    lower = mid - mult * std
    width = (upper - lower) / mid.replace(0.0, np.nan)
    pct_b = (close - lower) / (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame(
        {"mid": mid, "upper": upper, "lower": lower, "bandwidth": width, "pct_b": pct_b}
    )


def keltner(
    df: pd.DataFrame, length: int = 20, mult: float = 1.5, atr_len: int = 20
) -> pd.DataFrame:
    mid = ema(df["close"], length)
    rng = atr(df, atr_len)
    return pd.DataFrame(
        {"mid": mid, "upper": mid + mult * rng, "lower": mid - mult * rng}
    )


def donchian(df: pd.DataFrame, length: int = 20) -> pd.DataFrame:
    upper = df["high"].rolling(length, min_periods=length).max()
    lower = df["low"].rolling(length, min_periods=length).min()
    return pd.DataFrame({"upper": upper, "lower": lower, "mid": (upper + lower) / 2.0})


def adx(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = true_range(df)
    atr_ = tr.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    plus_di = 100.0 * pd.Series(plus_dm, index=df.index).ewm(
        alpha=1.0 / length, adjust=False, min_periods=length
    ).mean() / atr_.replace(0.0, np.nan)
    minus_di = 100.0 * pd.Series(minus_dm, index=df.index).ewm(
        alpha=1.0 / length, adjust=False, min_periods=length
    ).mean() / atr_.replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx_ = dx.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    return pd.DataFrame({"adx": adx_, "plus_di": plus_di, "minus_di": minus_di})


# --------------------------------------------------------------------------- #
#  Rolling highs / lows                                                      #
# --------------------------------------------------------------------------- #
def rolling_high(s: pd.Series, length: int) -> pd.Series:
    return s.rolling(length, min_periods=1).max()


def rolling_low(s: pd.Series, length: int) -> pd.Series:
    return s.rolling(length, min_periods=1).min()


def pct_from_rolling_high(close: pd.Series, length: int) -> pd.Series:
    hi = rolling_high(close, length)
    return (close / hi - 1.0) * 100.0


def pct_from_rolling_low(close: pd.Series, length: int) -> pd.Series:
    lo = rolling_low(close, length)
    return (close / lo - 1.0) * 100.0


# --------------------------------------------------------------------------- #
#  Intraday-specific                                                         #
# --------------------------------------------------------------------------- #
def _session_key(index: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(index.tz_convert("America/New_York").date, index=index)


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price, reset at each session boundary."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    grp = _session_key(df.index)
    pv = (tp * df["volume"]).groupby(grp).cumsum()
    vol = df["volume"].groupby(grp).cumsum().replace(0.0, np.nan)
    out = pv / vol
    out.index = df.index
    return out


def anchored_vwap(df: pd.DataFrame, anchor_ts) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    mask = df.index >= pd.Timestamp(anchor_ts)
    pv = (tp * df["volume"]).where(mask, 0.0).cumsum()
    vol = df["volume"].where(mask, 0.0).cumsum().replace(0.0, np.nan)
    return pv / vol


def opening_range(df: pd.DataFrame, minutes: int = 15) -> pd.DataFrame:
    """First-`minutes` high/low for each session. Returns a frame reindexed to
    `df` so you can compare `close` against `or_high` / `or_low` per bar."""
    ny = df.index.tz_convert("America/New_York")
    since_open = (ny.hour - 9) * 60 + (ny.minute - 30)
    in_or = (since_open >= 0) & (since_open < minutes)
    grp = pd.Series(ny.date, index=df.index)
    or_high = df["high"].where(in_or).groupby(grp).transform("max")
    or_low = df["low"].where(in_or).groupby(grp).transform("min")
    return pd.DataFrame(
        {"or_high": or_high.ffill(), "or_low": or_low.ffill(), "minutes": minutes},
        index=df.index,
    )


def rel_volume_intraday(intraday: pd.DataFrame, lookback_days: int = 20) -> float:
    """Rough RVOL: today's cumulative volume so far vs. the average
    cumulative volume by this time of day over the last `lookback_days`
    sessions. > 1 means unusually active."""
    ny = intraday.index.tz_convert("America/New_York")
    grp = pd.Series(ny.date, index=intraday.index)
    sessions = list(dict.fromkeys(grp.tolist()))
    if len(sessions) < 2:
        return 1.0
    today = sessions[-1]
    minute_of_day = (ny.hour * 60 + ny.minute).to_numpy()
    cur_mask = (grp == today).to_numpy()
    if not cur_mask.any():
        return 1.0
    cur_minute_cutoff = minute_of_day[cur_mask].max()
    cur_vol = intraday.loc[cur_mask, "volume"].sum()

    prior = sessions[-(lookback_days + 1):-1]
    ratios = []
    for d in prior:
        m = (grp == d).to_numpy() & (minute_of_day <= cur_minute_cutoff)
        v = intraday.loc[m, "volume"].sum()
        if v > 0:
            ratios.append(v)
    if not ratios:
        return 1.0
    avg = float(np.mean(ratios))
    return float(cur_vol / avg) if avg else 1.0
