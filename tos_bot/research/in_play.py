"""Which stocks were in play on each past session - the day-trade replay's stocks.

Day-trade setups are for stocks in play: the morning scan picks a hot list from
the whole market, and the setups only ever see those names. Replaying the last
sixty sessions on *today's* hot list tests something else. Today's names are hot
because of what they have just done, so their recent sessions hand a momentum
setup its own hindsight; and on most of those sessions they were ordinary stocks
no scan would have shown.

So for every replayed session the stocks are chosen the way the morning scan would
have chosen them, from what was known before the open:

* **the hot list** - every stock's daily heat as of the session before (the same
  ingredients and weights as scanner/heat.py: unusual volume, a big move for the
  stock, a close near a 20-session extreme, daily range, dollar volume, each a
  percentile among the liquid stocks that day), hottest first;
* **the gappers** - among the wider watchlist, the stocks whose open gapped
  ``min_gap_pct`` or more from the previous close, biggest gap in average true
  ranges first. The open is known at 09:30, before any setup can fire. (The live
  gap check also asks for pre-market volume, which daily candles don't hold.)

What it can't reproduce: the names the wide scan adopts mid-session - knowing them
needs every stock's 5-minute candles - and listings that have since gone.
"""

from __future__ import annotations

import datetime as dt
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from ..scanner.heat import _DAILY_WEIGHTS

#: a stock needs this many sessions behind it before it is ranked (scanner/heat.py daily_metrics)
MIN_SESSIONS = 30


def metrics_history(daily: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """``scanner.heat.daily_metrics`` for every session of a stock at once: row D holds what the
    scan would have read after D's close. Rows it couldn't rank are left out."""
    if daily is None or len(daily) < MIN_SESSIONS:
        return None
    high, low, close, volume = (daily[c].astype(float) for c in ("high", "low", "close", "volume"))
    prev = close.shift(1)
    true_range = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    true_range[prev.isna()] = np.nan
    atr = true_range.rolling(14).mean()
    avg_volume = volume.shift(1).rolling(20).mean()
    hi, lo = high.rolling(20).max(), low.rolling(20).min()
    position = ((close - lo) / (hi - lo)).where(hi > lo, 0.5)
    out = pd.DataFrame({
        "price": close, "dollar_volume": (close * volume).rolling(20).mean(), "atr_pct": atr / close * 100.0,
        "rvol": volume / avg_volume, "move_atr": (close - prev).abs() / atr, "extreme": (position - 0.5).abs() * 2.0,
        "atr": atr, "open": daily["open"].astype(float),
    })
    out.index = pd.Index(daily.index.date, name="session")
    ranked = (np.arange(len(out)) >= MIN_SESSIONS - 1) & (close.to_numpy() > 0) & (atr.to_numpy() > 0) \
        & (avg_volume.to_numpy() > 0)
    return out[ranked].dropna(subset=["dollar_volume", "rvol", "move_atr", "extreme", "atr_pct"])


def in_play(frame_of: Callable[[str], Optional[pd.DataFrame]], symbols: Iterable[str], sessions: Sequence[dt.date],
            hot: int, gappers: int = 0, prefilter: Optional[Mapping[str, float]] = None, min_gap_pct: float = 2.0,
            watch: int = 400, progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, List[dt.date]]:
    """For each stock, the sessions among ``sessions`` on which it was in play: on that morning's
    ``hot`` hottest, or one of the ``gappers`` biggest gaps among the ``watch`` hottest."""
    prefilter = dict(prefilter or {})
    wanted = sorted(sessions)
    if not wanted or hot <= 0:
        return {}
    symbols = list(dict.fromkeys(symbols))
    morning: Dict[str, pd.DataFrame] = {}                 # per stock: row D = the metrics after the session before D
    gaps: Dict[str, pd.Series] = {}
    for n, symbol in enumerate(symbols, 1):
        m = metrics_history(frame_of(symbol))
        if progress and (n % 200 == 0 or n == len(symbols)):
            progress(n, len(symbols))
        if m is None or not len(m):
            continue
        before = m.shift(1)                                # a stock with no candle for D isn't picked for D
        before = before[before.index.isin(wanted)].dropna(subset=["price"])
        if not len(before):
            continue
        morning[symbol] = before
        opened = m["open"].reindex(before.index)
        gaps[symbol] = (opened - before["price"]).abs() / before["atr"]
        gaps[symbol][(opened / before["price"] - 1.0).abs() * 100.0 < min_gap_pct] = np.nan

    if not morning:
        return {}
    names = list(morning)
    fields = {f: pd.DataFrame({s: morning[s][f] for s in names}).reindex(wanted).to_numpy()
              for f in ("price", "dollar_volume", "atr_pct", *_DAILY_WEIGHTS)}
    gap = pd.DataFrame({s: gaps[s] for s in names}).reindex(wanted).to_numpy()
    lo_price, hi_price = prefilter.get("min_price", 0.0), prefilter.get("max_price", float("inf"))

    out: Dict[str, List[dt.date]] = {}
    for i, day in enumerate(wanted):
        price = fields["price"][i]
        with np.errstate(invalid="ignore"):
            liquid = ((price >= lo_price) & (price <= hi_price)
                      & (fields["dollar_volume"][i] >= prefilter.get("min_dollar_volume", 0.0))
                      & (fields["atr_pct"][i] >= prefilter.get("min_atr_pct", 0.0)))
        idx = np.flatnonzero(liquid)
        if not len(idx):
            continue
        heat = np.zeros(len(idx))
        for field, weight in _DAILY_WEIGHTS.items():
            values = fields[field][i][idx]
            heat += weight * values.argsort().argsort() / max(1, len(values) - 1)
        order = idx[np.argsort(-heat, kind="stable")]
        picked = list(order[:hot])
        if gappers > 0:
            watched = [j for j in order[:watch] if gap[i][j] == gap[i][j]]
            picked += [j for j in sorted(watched, key=lambda j: -gap[i][j]) if j not in picked][:gappers]
        for j in picked:
            out.setdefault(names[j], []).append(day)
    return out
