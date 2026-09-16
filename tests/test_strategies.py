from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import fakes
from tos_bot.core.enums import Side
from tos_bot.strategies import REGISTRY, build_context
from tos_bot.strategies import base as strategy_base
from tos_bot.strategies.registry import describe_all
from tos_bot.util import clock


def _context(symbol, **kw):
    intraday = fakes.intraday_bars(symbol)
    quote = fakes.quote_from_price(symbol, float(intraday["close"].iloc[-1]))
    return build_context(symbol, intraday, fakes.daily_bars(symbol), quote, **kw)


def test_registry_has_all_families():
    keys = set(REGISTRY)
    assert {"opening_range_breakout", "vwap_reclaim", "rsi2_mean_reversion",
            "relative_value_comps", "dcf_fair_value_gap",
            "valuation_football_field"}.issubset(keys)
    assert len(describe_all()) == len(REGISTRY)


def test_every_strategy_runs_and_geometry_is_sane():
    total = 0
    for i in range(30):
        ctx = _context(f"S{i:02d}", params={"valuation": {}})
        for cls in REGISTRY.values():
            for p in cls().generate(ctx):
                total += 1
                assert p.entry > 0 and p.stop > 0 and p.targets
                if p.side is Side.LONG:
                    assert p.stop < p.entry < p.targets[0]
                else:
                    assert p.targets[0] < p.entry < p.stop
                assert 0.3 <= p.reward_risk <= 30
                assert abs(p.entry - p.stop) / p.entry <= 0.30
                assert p.explanation and len(p.explanation) > 40
    assert total >= 1


def test_swing_setups_run_before_the_open_without_intraday_candles():
    for i in range(10):
        symbol = f"W{i:02d}"
        daily = fakes.daily_bars(symbol)
        ctx = build_context(symbol, None, daily, fakes.quote_from_price(symbol, float(daily["close"].iloc[-1])))
        for cls in REGISTRY.values():
            cls().generate(ctx)                                        # no intraday data must never crash


def test_indicators_are_computed_once_per_context(monkeypatch):
    calls = []
    real_atr = strategy_base.ta.atr
    monkeypatch.setattr(strategy_base.ta, "atr", lambda *a, **k: calls.append(1) or real_atr(*a, **k))
    ctx = _context("MEMO")
    for _ in range(5):
        assert ctx.intraday_atr > 0 and ctx.daily_atr > 0
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
#  Aziz day-trade setups on purpose-built frames                             #
# --------------------------------------------------------------------------- #
def _session_intraday(bars_open, closes, *, vol=None):
    """A tz-aware 5-minute frame for *today's* session, so ctx.today_intraday() picks it up."""
    n = len(closes)
    start = pd.Timestamp(f"{clock.session_date()} 09:30", tz="America/New_York")
    idx = pd.date_range(start, periods=n, freq="5min")
    closes = np.asarray(closes, float)
    opens = np.concatenate([[bars_open], closes[:-1]])
    high = np.maximum(opens, closes) + 0.05
    low = np.minimum(opens, closes) - 0.05
    v = np.asarray(vol if vol is not None else np.full(n, 5e5), float)
    return pd.DataFrame({"open": opens, "high": high, "low": low, "close": closes, "volume": v}, index=idx)


def _flat_daily(price=100.0, n=120):
    idx = pd.date_range("2025-03-03", periods=n, freq="B", tz="America/New_York")
    c = np.full(n, price)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": np.full(n, 3e6)}, index=idx)


def _quote(sym, last):
    from tos_bot.core.models import Quote
    return Quote(symbol=sym, bid=last - 0.02, ask=last + 0.02, last=last, volume=1e6)


def _douglas_framed(play):
    e = play.explanation
    return ("HOW TO HOLD IT (Douglas)" in e and "INVALIDATION:" in e
            and "THE PLAN" in e and 0.05 <= play.probability <= 0.90)


def test_bull_flag_fires_on_a_textbook_flag():
    from tos_bot.strategies.technical import MomentumFlag
    # 12 quiet bars (so ATR(14) is defined), then a pole 100->103 over 5 bars,
    # then a tight 3-bar flag. The live quote (103.25) breaks the flag high.
    closes = ([99.8] * 12
              + [100.2, 100.9, 101.6, 102.4, 103.0]     # pole
              + [102.9, 102.85, 102.95])                # flag
    vol = [3e5] * 12 + [4e5, 5e5, 6e5, 7e5, 9e5, 3e5, 3e5, 3e5]
    ctx = build_context("FLAG", _session_intraday(99.8, closes, vol=vol), _flat_daily(101.0),
                        _quote("FLAG", 103.25), activity={"rvol": 2.0})
    plays = MomentumFlag().generate(ctx)
    assert plays, "a clean bull flag breakout should produce a play"
    p = plays[0]
    assert p.side is Side.LONG and p.stop < p.entry < p.targets[0]
    assert p.reward_risk >= 1.5 and _douglas_framed(p) and "flag" in "".join(p.tags)


def test_divergence_reversal_reads_bearish_divergence_at_resistance():
    from tos_bot.strategies.technical import DivergenceReversal
    n = 90
    ramp = np.concatenate([np.linspace(100, 108, 30), np.linspace(108, 103, 15),
                           np.linspace(103, 111, 30), np.linspace(111, 109, 15)])    # a HIGHER high
    idx = pd.date_range("2025-02-03", periods=n, freq="B", tz="America/New_York")
    close = ramp + np.random.default_rng(2).normal(0, 0.05, n)
    daily = pd.DataFrame({"open": close, "high": close + 0.6, "low": close - 0.6,
                          "close": close, "volume": np.full(n, 4e6)}, index=idx)
    ctx = build_context("DIV", _session_intraday(close[-1], [close[-1]] * 6), daily, _quote("DIV", float(close[-1])))
    for p in DivergenceReversal().generate(ctx):
        if p.side is Side.SHORT:
            assert p.targets[0] < p.entry < p.stop
        assert _douglas_framed(p)


def test_min_stop_floor_widens_noise_tight_stops():
    """A stop closer than ~0.6% (or ~0.9 intraday ATR) to entry is noise, not a
    level - _mk_play must widen it so the reward:risk stops being a fake."""
    from tos_bot.core.enums import StrategyKind, Timeframe
    from tos_bot.strategies.base import Strategy

    class _T(Strategy):
        key, kind, timeframe = "t", StrategyKind.TECHNICAL, Timeframe.INTRADAY
        title, thesis = "T", "t"

    idx = pd.date_range(pd.Timestamp(f"{clock.session_date()} 09:30", tz="America/New_York"), periods=30, freq="5min")
    c = np.full(30, 167.0)
    df = pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": np.full(30, 5e5)}, index=idx)
    ctx = build_context("X", df, df, _quote("X", 167.0))
    p = _T()._mk_play(ctx, Side.LONG, entry=167.0, stop=166.70, targets=[170.06],
                      confidence=0.7, rationale="r", detail="d", evidence={}, tags=["intraday"])
    assert p is not None
    assert abs(p.entry - p.stop) / p.entry >= 0.006 - 1e-9          # >= 0.6% floor
    assert p.reward_risk <= 8.0                                      # no more fake 10:1


@pytest.mark.parametrize("key", ["abcd_pattern", "bull_bear_flag", "red_to_green", "intraday_reversal", "sr_bounce"])
def test_intraday_setups_never_crash_and_stay_framed(key):
    for i in range(40):
        ctx = _context(f"N{i:02d}", activity={"rvol": 2.0})
        for p in REGISTRY[key]().generate(ctx):
            if p.side is Side.LONG:
                assert p.stop < p.entry < p.targets[0]
            else:
                assert p.targets[0] < p.entry < p.stop
            assert _douglas_framed(p) and p.invalidation
            if key == "sr_bounce":
                level = p.evidence["level"]
                assert (p.stop < level) if p.side is Side.LONG else (p.stop > level)
                assert p.reward_risk >= 2.0


def test_abcd_needs_a_real_higher_low_on_a_lighter_pullback():
    from tos_bot.strategies.technical import AbcdPattern

    quiet, push = [100.0] * 12, [100.4, 100.9, 101.5, 102.0]

    def abcd(pullback, pullback_volume):
        closes = quiet + push + pullback
        vol = [3e5] * 12 + [8e5] * 4 + [pullback_volume] * len(pullback)
        ctx = build_context("ABCD", _session_intraday(100.0, closes, vol=vol), _flat_daily(100.0),
                            _quote("ABCD", pullback[-1] - 0.05), activity={"rvol": 2.0})
        return AbcdPattern().generate(ctx)

    assert abcd([101.7, 101.4, 101.3], 3e5)                  # a shallow pullback on drying volume
    assert not abcd([101.2, 100.7, 100.4], 3e5)              # gave back most of the push
    assert not abcd([101.7, 101.4, 101.3], 1.2e6)            # sold harder than it was bought


def test_stops_are_floored_at_a_slice_of_the_stocks_daily_range():
    from tos_bot.core.enums import StrategyKind, Timeframe
    from tos_bot.strategies.base import Strategy

    class _T(Strategy):
        key, kind, timeframe = "t", StrategyKind.TECHNICAL, Timeframe.INTRADAY
        title, thesis = "T", "t"

    idx = pd.date_range(pd.Timestamp(f"{clock.session_date()} 09:30", tz="America/New_York"), periods=30, freq="5min")
    c = np.full(30, 50.0)
    intraday = pd.DataFrame({"open": c, "high": c + 0.05, "low": c - 0.05, "close": c, "volume": np.full(30, 5e5)},
                            index=idx)
    ctx = build_context("X", intraday, _flat_daily(50.0), _quote("X", 50.0))          # a $2 daily range
    p = _T()._mk_play(ctx, Side.LONG, entry=50.0, stop=49.7, targets=[52.0],
                      confidence=0.7, rationale="r", detail="d", evidence={}, tags=["intraday"])
    assert p is not None and p.entry - p.stop >= 0.25 * 2.0 - 1e-9                 # a quarter of the day's range


# --------------------------------------------------------------------------- #
#  the odds a play states lean on the setup's record (Douglas, Chan)
# --------------------------------------------------------------------------- #
def test_calibrated_probability_shrinks_toward_the_record_as_it_grows():
    from tos_bot.strategies.base import calibrated_probability

    assert calibrated_probability(0.55, None) == (0.55, None)
    assert calibrated_probability(0.55, {"trades": 0, "win_rate": 0.9}) == (0.55, None)
    p30, odds = calibrated_probability(0.50, {"trades": 30, "win_rate": 0.70})
    assert abs(p30 - 0.60) < 1e-9 and odds == {"trades": 30, "win_rate": 0.7, "own": 0.5}
    p300, _ = calibrated_probability(0.50, {"trades": 300, "win_rate": 0.70})
    assert 0.60 < p300 < 0.70 and abs(p300 - (300 * 0.7 + 30 * 0.5) / 330) < 1e-9
    assert calibrated_probability(0.50, {"trades": 3000, "win_rate": 0.99})[0] == 0.90     # bounded like any play


def test_a_play_carries_the_record_its_odds_lean_on():
    from tos_bot.strategies.base import Strategy

    class Fixed(Strategy):
        key, title, thesis = "fixed", "Fixed", "t"

        def generate(self, ctx):
            p = self._mk_play(ctx, Side.LONG, 100.0, 97.0, [110.0], 0.5, "r", "d", {}, probability=0.5)
            return [p] if p else []

    plain = _context("ODDS")
    informed = _context("ODDS")
    informed.records["fixed"] = {"trades": 90, "win_rate": 0.8}
    [before] = Fixed().generate(plain)
    [after] = Fixed().generate(informed)
    assert before.probability == 0.5 and "odds_from_record" not in before.evidence
    assert abs(after.probability - (90 * 0.8 + 30 * 0.5) / 120) < 1e-9
    assert after.evidence["odds_from_record"] == {"trades": 90, "win_rate": 0.8, "own": 0.5}
    assert "90 replayed and real trades" in after.explanation and "not a call on this one" in before.explanation


def test_a_reversal_after_a_doji_waits_for_the_next_candle_to_turn():
    from tos_bot.analysis.candles import CandleRead
    from tos_bot.strategies.technical import _reversal_triggered

    doji, hammer = CandleRead("doji", None, True), CandleRead("hammer", True, False)
    bars = pd.DataFrame({"open": [10.0, 10.1], "high": [10.5, 10.4], "low": [9.8, 10.0], "close": [10.1, 10.2]})
    assert not _reversal_triggered(bars, doji, up=True)          # the bar after the doji hasn't made a new high
    assert _reversal_triggered(bars, hammer, up=True)            # a hammer is its own confirmation
    bars.loc[1, "high"] = 10.6
    assert _reversal_triggered(bars, doji, up=True)
    bars.loc[1, "low"] = 9.7
    assert _reversal_triggered(bars, doji, up=False)
    bars.loc[1, "low"] = 9.9
    assert not _reversal_triggered(bars, doji, up=False)


def test_pooled_odds_count_a_real_trade_twice():
    from tos_bot.research.weights import pooled_odds

    assert pooled_odds(None, None) is None
    assert pooled_odds({"trades": 40, "win_rate": 0.5}, None) == {"trades": 40, "win_rate": 0.5}
    pooled = pooled_odds({"trades": 40, "win_rate": 0.5}, {"trades": 10, "win_rate": 0.8})
    assert pooled == {"trades": 60, "win_rate": round((40 * 0.5 + 20 * 0.8) / 60, 4)}
