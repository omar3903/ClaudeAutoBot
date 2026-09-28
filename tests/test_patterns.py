"""The swing setups from Grimes and Bulkowski (strategies/patterns.py), on hand-made daily candles."""

from __future__ import annotations

import pandas as pd

import fakes
from autotradebot.core.enums import Side
from autotradebot.strategies import REGISTRY, build_context
from autotradebot.strategies.patterns import completed_daily, pivots


def _frame(closes, lows=None, highs=None, volume=None):
    days = pd.bdate_range("2025-01-02", periods=len(closes), tz="America/New_York")
    lows, highs = dict(lows or {}), dict(highs or {})
    n = len(closes)
    rows = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        rows.append({"open": o, "high": highs.get(i - n, max(o, c) + 0.5), "low": lows.get(i - n, min(o, c) - 0.5),
                     "close": c, "volume": (volume or {}).get(i - n, 1_000_000.0)})
    return pd.DataFrame(rows, index=days)


def _plays(key, daily):
    ctx = build_context("TEST", None, daily, fakes.quote_from_price("TEST", float(daily["close"].iloc[-1])))
    return REGISTRY[key]().generate(ctx)


def _walk(start, end, steps):
    return [start + (end - start) * (i + 1) / steps for i in range(steps)]


# ---------------------------------------------------------------- the failure test
def _spring(last_close=101.5, last_low=99.4):
    closes = [110.0] * 45 + [105.0, 103.0, 101.0, 103.0, 105.0] + _walk(106.0, 109.0, 3) + _walk(108.5, 102.0, 9)
    closes += [101.6, last_close]
    return _frame(closes, lows={-17: 100.0, -2: 100.8, -1: last_low}, highs={-1: 101.9}, volume={-1: 2_000_000.0})


def test_a_probe_under_support_that_closes_back_above_it_is_bought():
    plays = _plays("failure_test", _spring())
    assert len(plays) == 1
    p = plays[0]
    assert p.side is Side.LONG and p.evidence["level"] == 100.0 and p.evidence["test_extreme"] == 99.4
    assert p.stop < 99.4 < p.entry and p.reward_risk >= 1.5 and p.evidence["heavy_volume"]
    assert "swing" in p.tags and "failure-test" in p.tags


def test_a_break_that_holds_is_not_a_failure_test():
    assert _plays("failure_test", _spring(last_close=99.7)) == []            # closed under the level: a real break
    assert _plays("failure_test", _spring(last_low=100.4)) == []             # never traded through it
    assert _plays("failure_test", _spring(last_low=96.0)) == []              # collapsed through it: not a probe


def test_the_upthrust_is_the_mirror():
    closes = [90.0] * 45 + [95.0, 97.0, 99.0, 97.0, 95.0] + _walk(94.0, 91.0, 3) + _walk(91.5, 98.0, 9) + [98.4, 98.5]
    daily = _frame(closes, highs={-17: 100.0, -2: 99.2, -1: 100.6}, lows={-1: 98.1})
    plays = _plays("failure_test", daily)
    assert len(plays) == 1 and plays[0].side is Side.SHORT and plays[0].stop > 100.6


# ---------------------------------------------------------------- the pullback
def _thrust_and_pullback(pullback):
    closes = [100.0 + 0.1 * (i % 2) for i in range(70)] + _walk(101.5, 110.0, 6) + pullback
    return _frame(closes)


def test_the_first_quiet_pullback_after_a_momentum_thrust_is_joined_when_it_turns():
    daily = _thrust_and_pullback([108.6, 107.2, 106.0, 105.4, 107.0])
    plays = _plays("trend_pullback", daily)
    assert len(plays) == 1
    p = plays[0]
    assert p.side is Side.LONG and p.stop < 105.0 < p.entry < p.targets[0] and p.reward_risk >= 1.5
    assert p.evidence["retrace_pct"] < 60 and p.evidence["thrust_extreme"] >= 110.0


def test_no_pullback_trade_without_the_turn_or_after_a_deep_retrace():
    assert _plays("trend_pullback", _thrust_and_pullback([108.6, 107.2, 106.0, 105.4, 105.2])) == []   # still falling
    assert _plays("trend_pullback", _thrust_and_pullback([107.0, 104.0, 102.0, 101.0, 102.8])) == []   # gave it all back
    assert _plays("trend_pullback", _frame([100.0 + 0.1 * (i % 2) for i in range(90)])) == []          # no thrust at all


# ---------------------------------------------------------------- the double bottom
def _twin_lows(last_close, right_low=101.0, right_close=102.0):
    lead = 55                                                                 # sessions before the pattern
    closes = (_walk(125.0, 118.0, lead) + _walk(117.0, 101.0, 12) + _walk(102.5, 112.0, 9)
              + _walk(111.0, right_close, 10) + _walk(right_close + 1.5, 111.5, 8) + [last_close])
    n = len(closes)
    return _frame(closes, lows={lead + 11 - n: 100.0, lead + 30 - n: right_low}, highs={lead + 20 - n: 112.5})


def test_a_double_bottom_is_bought_only_once_the_close_confirms_it():
    assert _plays("double_bottom", _twin_lows(111.8)) == []                   # two lows and no confirmation: nothing
    plays = _plays("double_bottom", _twin_lows(113.0))
    assert len(plays) == 1
    p = plays[0]
    ev = p.evidence
    assert p.side is Side.LONG and ev["confirmation"] == 112.5 and ev["left"] == 100.0 and ev["right"] == 101.0
    assert ev["sessions_apart"] == 19 and ev["height_pct"] == 12.5
    assert 107.0 <= p.stop <= 108.0                                           # inside the pattern, not under it
    assert abs(p.targets[0] - (112.5 + 0.8 * 12.5)) < 0.01 and p.reward_risk >= 1.5


def test_lows_too_far_apart_in_price_are_not_twins():
    assert _plays("double_bottom", _twin_lows(113.0, right_low=107.0, right_close=107.5)) == []


# ---------------------------------------------------------------- the helpers
def test_swing_points_and_the_forming_candle():
    import numpy as np

    assert pivots(np.array([5.0, 4, 3, 2, 3, 4, 5, 4, 3, 1, 3, 4, 5]), 3, lows=True) == [3, 9]
    assert pivots(np.array([1.0, 2, 3, 9, 3, 2, 1]), 3, lows=False) == [3]
    daily = _frame([100.0] * 70)
    ctx = build_context("TEST", None, daily, fakes.quote_from_price("TEST", 100.0))
    assert len(completed_daily(ctx)) == 70                                    # no live session: every candle is complete


def test_the_pattern_setups_are_registered_as_swing_setups_that_are_on_by_default():
    for key in ("failure_test", "trend_pullback", "double_bottom"):
        cls = REGISTRY[key]
        assert cls.timeframe.value == "SWING" and cls.enabled_by_default and cls().describe()["thesis"]
