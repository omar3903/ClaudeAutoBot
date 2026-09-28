"""Noise checks and expected value: the flags that mark a bad moment for a setup,
and a ranking that doesn't reward a stop tightened to fake reward:risk."""

from __future__ import annotations

import numpy as np
import pandas as pd

from autotradebot.core.enums import Side, StrategyKind, Timeframe
from autotradebot.core.models import Play, Quote
from autotradebot.scanner.evaluator import with_today
from autotradebot.scanner.filters import expected_r, rank_score
from autotradebot.scanner.noise import NoiseSettings, context_flags, opposing_volume_ratio
from autotradebot.strategies import build_context
from autotradebot.util import clock

NY = "America/New_York"


def _daily(start, end, n=80):
    """Completed daily candles drifting from ``start`` to ``end``, through yesterday."""
    idx = pd.bdate_range(end=pd.Timestamp(clock.prev_trading_day(clock.session_date())), periods=n, tz=NY)
    close = np.linspace(start, end, n)
    return pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5, "close": close,
                         "volume": np.full(n, 2e6)}, index=idx)


def _today(day_open, closes, volumes):
    """Today's 5-minute candles from the open; the last one is still printing."""
    idx = pd.date_range(pd.Timestamp(f"{clock.session_date()} 09:30", tz=NY), periods=len(closes), freq="5min")
    closes = np.asarray(closes, float)
    opens = np.concatenate([[day_open], closes[:-1]])
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) + 0.02,
                         "low": np.minimum(opens, closes) - 0.02, "close": closes,
                         "volume": np.asarray(volumes, float)}, index=idx)


def _bar_volumes(day_open, closes, heavy_when_rising):
    opens = [day_open] + list(closes[:-1])
    return [9e5 if (c > o) == heavy_when_rising else 3e5 for c, o in zip(closes, opens)]


def _context(daily, today):
    last = float(today["close"].iloc[-1])
    return build_context("NZZ", today, with_today(daily, today), Quote(symbol="NZZ", bid=last, ask=last, last=last))


def _long(entry, stop, target, probability=0.58):
    return Play(symbol="NZZ", side=Side.LONG, strategy="x", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.INTRADAY, entry=entry, stop=stop, targets=[target], probability=probability)


def test_a_momentum_long_into_a_falling_gap_down_day_is_flagged_on_every_count():
    closes = [38.7, 38.6, 38.65, 38.5, 38.45, 38.5, 38.4, 38.35, 38.4, 38.3, 38.25, 38.3]
    today = _today(38.8, closes, _bar_volumes(38.8, closes, heavy_when_rising=False))
    ctx = _context(_daily(60.0, 40.0), today)                                  # 3% below yesterday's close
    play = _long(38.3, 38.0, 39.0)
    assert set(context_flags(play, ctx, "momentum", NoiseSettings())) == {
        "against_trend", "wrong_side_of_vwap", "against_gap", "volume_against"}
    assert context_flags(play, ctx, "reversal", NoiseSettings()) == []         # fading the move is the point


def test_a_momentum_long_with_the_day_is_clean():
    closes = [41.3, 41.5, 41.45, 41.7, 41.9, 41.85, 42.0, 42.2, 42.15, 42.3, 42.4, 42.35]
    today = _today(41.2, closes, _bar_volumes(41.2, closes, heavy_when_rising=True))
    ctx = _context(_daily(30.0, 40.0), today)                                  # gapped up 3% in an uptrend
    assert context_flags(_long(42.35, 42.0, 43.1), ctx, "momentum", NoiseSettings()) == []


def test_opposing_volume_leaves_out_the_bar_still_printing():
    today = _today(10.0, [9.9, 10.1, 9.8], [5e5, 1e5, 9e9])
    assert opposing_volume_ratio(today, long=True, bars=6) == 5.0


def test_a_stop_inside_the_daily_noise_is_worth_less_than_its_ratio_suggests():
    tight = _long(50.0, 49.6, 50.9)                                            # 2.25 : 1 on a 0.40 stop
    wide = _long(50.0, 49.1, 51.44, probability=0.60)                          # 1.6 : 1 on a 0.90 stop
    assert expected_r(tight) > expected_r(wide)                               # on reward:risk alone it wins...
    daily_atr = 2.0
    assert expected_r(wide, daily_atr) > expected_r(tight, daily_atr)         # ...not once its noise is priced in
    tight.evidence["expected_r"] = expected_r(tight, daily_atr)
    wide.evidence["expected_r"] = expected_r(wide, daily_atr)
    assert rank_score(wide) > rank_score(tight)
