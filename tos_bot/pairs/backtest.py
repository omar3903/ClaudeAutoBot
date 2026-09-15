"""The pair rules replayed on daily closes.

Signals are read at each session's close and filled at that close - live, the desk decides
in the last half hour before the bell - and every fill pays ``cost_bps`` of each leg's value.
A trade's result is in R: its P/L over what it put at risk when it went on (the spread
running from the entry to the stop), per dollar of the first stock.

Chan's warning about stock pairs is that they often stop being pairs out of sample. So the
replay chooses its pairs on the sessions *before* the ones it trades them on, and a pair on
the watch list shows how its rules did on the latest sessions after being fitted on the ones
before.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
from typing import Any, Dict, List, Mapping, Sequence

import numpy as np
import pandas as pd

from ..research.replay import SimTrade, summarize
from ..util import clock
from .finder import FinderSettings, aligned_closes, find_pairs, fit_pair
from .model import KEY, LONG_SPREAD, SHORT_SPREAD, PairModel, PairRules, rolling_stats, signal, spread

REASONS = {"exit": "mean", "stop": "stop", "time": "time-stop"}
MIN_FIT_DAYS = 120
MIN_VALIDATION_FIT = 150
MIN_VALIDATION_TEST = 60


def simulate(model: PairModel, closes_first: Sequence[float], closes_second: Sequence[float],
             dates: Sequence[dt.date], rules: PairRules, start: int = 0) -> List[SimTrade]:
    """Trade ``model`` on aligned closes, from index ``start`` on (the closes before it only feed
    the lookback). A trade still open when the closes run out is left out."""
    a, b = np.asarray(closes_first, dtype=float), np.asarray(closes_second, dtype=float)
    z, sd = rolling_stats(spread(np.log(a), np.log(b), model.hedge), model.lookback)
    cost = 2 * (1 + model.hedge) * rules.cost_bps / 1e4
    trades: List[SimTrade] = []
    position = None
    for i in range(max(start, model.lookback - 1), len(a)):
        if position is None:
            wanted = signal(z[i], None, 0, model)
            if wanted and math.isfinite(sd[i]) and sd[i] > 0:
                position = {"side": LONG_SPREAD if wanted == "enter_long" else SHORT_SPREAD, "i": i, "z": float(z[i]),
                            "risk": max(1.0, model.stop_z - abs(z[i])) * float(sd[i]), "best": 0.0}
            continue
        pl = _pl(position, a, b, i, model.hedge)
        position["best"] = max(position["best"], pl)
        wanted = signal(z[i], position["side"], i - position["i"], model)
        if wanted in REASONS:
            trades.append(SimTrade(
                strategy=KEY, symbol=model.id, side="LONG" if position["side"] == LONG_SPREAD else "SHORT",
                timeframe="SWING", entered_at=dates[position["i"]].isoformat(), exited_at=dates[i].isoformat(),
                entry=round(position["z"], 3), exit=round(float(z[i]), 3),
                r=round((pl - cost) / position["risk"], 3), exit_reason=REASONS[wanted],
                mfe_r=round(max(0.0, position["best"]) / position["risk"], 3)))
            position = None
    return trades


def _pl(position: Mapping[str, Any], a: np.ndarray, b: np.ndarray, i: int, hedge: float) -> float:
    """P/L per dollar of the first stock: its return less γ times the second's."""
    j = position["i"]
    move = (a[i] / a[j] - 1.0) - hedge * (b[i] / b[j] - 1.0)
    return move if position["side"] == LONG_SPREAD else -move


def validate(first: str, second: str, frames: Mapping[str, pd.DataFrame], rules: PairRules,
             settings: FinderSettings, test_days: int) -> Dict[str, Any]:
    """How the pair did out of sample: fitted on the sessions before the latest ``test_days``, then
    traded on those. With a year of candles (about 252) the fitting window shrinks to what's left
    before the test - never below 150 sessions - and the test to what's left after that, never
    below 60. ``stable`` is False when it wasn't a pair back then, None when there's too little history."""
    a, b, dates = aligned_closes(frames[first], frames[second])
    test_days = min(int(test_days), len(a) - MIN_VALIDATION_FIT)
    if test_days < MIN_VALIDATION_TEST:
        return {"stable": None, "note": f"only {len(a)} sessions of history - too few to test it out of sample"}
    fit = min(settings.fit_days, len(a) - test_days)
    window = slice(len(a) - fit - test_days, len(a) - test_days)
    earlier = fit_pair(first, second, a[window], b[window], rules, dataclasses.replace(settings, fit_days=fit))
    if earlier is None:
        return {"stable": False, "note": "it wasn't a pair on the sessions before - the relationship is new"}
    ea, eb = (a, b) if earlier.first == first else (b, a)
    tail = slice(len(a) - fit - test_days, len(a))
    trades = simulate(earlier, ea[tail], eb[tail], dates[tail], rules, start=fit)
    return {"stable": True, "from": dates[-test_days].isoformat(), "sessions": test_days, **summarize(trades)}


def replay_pairs(frames: Mapping[str, pd.DataFrame], groups: Mapping[str, str], rules: PairRules,
                 settings: FinderSettings, sessions: int) -> List[SimTrade]:
    """Choose the pairs on the sessions before the last ``sessions`` ones and trade them on those -
    so no pair is chosen with hindsight. The fitting window shrinks to what's left before, but
    never below 120 sessions."""
    frames = {s: f for s, f in frames.items() if f is not None and len(f)}
    if not frames:
        return []
    longest = max(len(f) for f in frames.values())
    sessions = max(0, min(int(sessions), longest - MIN_FIT_DAYS))
    if sessions < 20:
        return []
    latest = max(f.index[-1].date() for f in frames.values())
    first_traded = min(clock.last_n_sessions(latest, sessions))
    before = {s: f[f.index.date < first_traded] for s, f in frames.items()}
    fit_days = max(MIN_FIT_DAYS, min(settings.fit_days, max(len(f) for f in before.values())))
    chosen = find_pairs(before, groups, rules, dataclasses.replace(settings, fit_days=fit_days))
    trades: List[SimTrade] = []
    for model in chosen:
        a, b, dates = aligned_closes(frames[model.first], frames[model.second])
        start = next((i for i, d in enumerate(dates) if d >= first_traded), len(dates))
        trades += simulate(model, a, b, dates, rules, start=start)
    return trades
