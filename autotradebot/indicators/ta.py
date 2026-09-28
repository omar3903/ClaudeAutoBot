"""Vectorised technical indicators.

Every function takes pandas Series / DataFrames and returns the same. OHLCV
frames have lowercase ``open, high, low, close, volume`` columns and a
tz-aware DatetimeIndex. Pure pandas / numpy - no TA-Lib.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

_NY = "America/New_York"


# ---- averages and oscillators -------------------------------------------- #
def sma(s: pd.Series, length: int) -> pd.Series:
    return s.rolling(length, min_periods=length).mean()


def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False, min_periods=length).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    up = delta.clip(lower=0.0).ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    down = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    out = 100.0 - 100.0 / (1.0 + up / down.replace(0.0, np.nan))
    return out.fillna(100.0).where(down != 0, 100.0)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    signal_line = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd": line, "signal": signal_line, "hist": line - signal_line})


# ---- range and volatility -------------------------------------------------- #
def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(),
                      (df["low"] - prev_close).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def bollinger(close: pd.Series, length: int = 20, mult: float = 2.0) -> pd.DataFrame:
    mid = sma(close, length)
    std = close.rolling(length, min_periods=length).std(ddof=0)
    upper, lower = mid + mult * std, mid - mult * std
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower,
                         "pct_b": (close - lower) / (upper - lower).replace(0.0, np.nan)})


def keltner(df: pd.DataFrame, length: int = 20, mult: float = 1.5, atr_len: int = 20) -> pd.DataFrame:
    mid = ema(df["close"], length)
    band = atr(df, atr_len)
    return pd.DataFrame({"mid": mid, "upper": mid + mult * band, "lower": mid - mult * band})


def adx(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    up, down = df["high"].diff(), -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)

    def smooth(s: pd.Series) -> pd.Series:
        return s.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()

    tr = smooth(true_range(df)).replace(0.0, np.nan)
    plus_di, minus_di = 100.0 * smooth(plus_dm) / tr, 100.0 * smooth(minus_dm) / tr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return pd.DataFrame({"adx": smooth(dx), "plus_di": plus_di, "minus_di": minus_di})


def beta(asset_close: pd.Series, market_close: pd.Series, sessions: int = 252) -> float:
    """Sensitivity of the asset's daily returns to the market's."""
    returns = pd.concat([asset_close.pct_change(), market_close.pct_change()], axis=1,
                        join="inner").dropna().tail(sessions)
    if len(returns) < 60:
        return math.nan
    variance = returns.iloc[:, 1].var()
    return float(returns.cov().iloc[0, 1] / variance) if variance else math.nan


# ---- intraday -------------------------------------------------------------- #
def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price, reset at each session."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    day = df.index.tz_convert(_NY).date
    pv = (typical * df["volume"]).groupby(day).cumsum()
    volume = df["volume"].groupby(day).cumsum().replace(0.0, np.nan)
    return pv / volume


def opening_range(df: pd.DataFrame, minutes: int = 15) -> pd.DataFrame:
    """Each session's first-``minutes`` high and low, aligned to every bar."""
    ny = df.index.tz_convert(_NY)
    since_open = (ny.hour - 9) * 60 + (ny.minute - 30)
    in_range = (since_open >= 0) & (since_open < minutes)
    day = ny.date
    return pd.DataFrame({
        "or_high": df["high"].where(in_range).groupby(day).transform("max").ffill(),
        "or_low": df["low"].where(in_range).groupby(day).transform("min").ffill(),
    }, index=df.index)


def rel_volume_intraday(intraday: pd.DataFrame, lookback_days: int = 20) -> float:
    """Today's volume so far against the average volume by the same time of
    day over recent sessions. Above 1 means unusually active."""
    ny = intraday.index.tz_convert(_NY)
    day = np.asarray(ny.date)
    minute = np.asarray(ny.hour * 60 + ny.minute)
    volume = intraday["volume"].to_numpy(dtype=float)
    today = day == day[-1]
    earlier = ~today & (minute <= minute[today].max())
    by_session = pd.Series(volume[earlier]).groupby(day[earlier]).sum()
    by_session = by_session[by_session > 0].tail(lookback_days)
    if by_session.empty:
        return 1.0
    return float(volume[today].sum() / by_session.mean())


# ---- momentum structure and divergence (Murphy) ------------------------------ #
def consecutive_run(close: pd.Series) -> int:
    """Signed length of the current run of higher (+) or lower (-) closes.
    Aziz's reversal setups key off five or more candles one way."""
    n = 0
    for x in close.diff().to_numpy()[::-1]:
        if x > 0 and n >= 0:
            n += 1
        elif x < 0 and n <= 0:
            n -= 1
        else:
            break
    return n


def _last_two_extremes(series: pd.Series, lookback: int, want_high: bool):
    arr = series.tail(lookback).to_numpy()
    if len(arr) < 6:
        return None
    pick = np.max if want_high else np.min
    idx = [i for i in range(2, len(arr) - 2) if arr[i] == pick(arr[i - 2:i + 3])]
    return (float(arr[idx[-2]]), float(arr[idx[-1]])) if len(idx) >= 2 else None


def rsi_divergence(close: pd.Series, rsi_series: pd.Series, lookback: int = 40) -> str:
    """'bullish' = price made a lower low but the oscillator a higher low;
    'bearish' = price a higher high, the oscillator a lower high; else ''."""
    p_low, r_low = _last_two_extremes(close, lookback, False), _last_two_extremes(rsi_series, lookback, False)
    if p_low and r_low and p_low[1] < p_low[0] and r_low[1] > r_low[0]:
        return "bullish"
    p_hi, r_hi = _last_two_extremes(close, lookback, True), _last_two_extremes(rsi_series, lookback, True)
    if p_hi and r_hi and p_hi[1] > p_hi[0] and r_hi[1] < r_hi[0]:
        return "bearish"
    return ""


def macd_divergence(close: pd.Series, lookback: int = 40) -> str:
    return rsi_divergence(close, macd(close)["hist"], lookback)
