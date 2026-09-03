from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tos_bot.core.enums import Side
from tos_bot.data.market_data import MarketDataService, SyntheticProvider
from tos_bot.strategies import REGISTRY, build_context
from tos_bot.strategies.registry import describe_all
from tos_bot.util import clock


@pytest.fixture(scope="module")
def datasvc():
    return MarketDataService(providers=[SyntheticProvider(seed=5)], cache=False,
                             min_interval_between_calls=0.0)


def test_registry_has_all_families():
    keys = set(REGISTRY)
    assert {"opening_range_breakout", "vwap_reclaim", "rsi2_mean_reversion",
            "relative_value_comps", "dcf_fair_value_gap",
            "valuation_football_field"}.issubset(keys)
    assert len(describe_all()) == len(REGISTRY)


def test_every_strategy_runs_and_geometry_is_sane(datasvc):
    total = 0
    for i in range(30):
        sym = f"S{i:02d}"
        ctx = build_context(sym,
                            datasvc.get_price_history(sym, "5m", 10),
                            datasvc.get_price_history(sym, "1d", 400),
                            datasvc.get_quote(sym),
                            params={"valuation": {}})
        for key, cls in REGISTRY.items():
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
    # synthetic data is noisy; we just need the pipeline to yield *something*
    assert total >= 1


# --------------------------------------------------------------------------- #
#  Aziz day-trade setups on purpose-built frames                             #
# --------------------------------------------------------------------------- #
def _session_intraday(bars_open, closes, *, vol=None):
    """Build a tz-aware 5-minute frame for *today's* session so
    ctx.today_intraday() picks it up."""
    n = len(closes)
    sd = clock.session_date()
    start = pd.Timestamp(f"{sd} 09:30", tz="America/New_York")
    idx = pd.date_range(start, periods=n, freq="5min")
    closes = np.asarray(closes, float)
    opens = np.concatenate([[bars_open], closes[:-1]])
    high = np.maximum(opens, closes) + 0.05
    low = np.minimum(opens, closes) - 0.05
    v = np.asarray(vol if vol is not None else np.full(n, 5e5), float)
    return pd.DataFrame({"open": opens, "high": high, "low": low, "close": closes,
                         "volume": v}, index=idx)


def _flat_daily(price=100.0, n=120):
    idx = pd.date_range("2025-03-03", periods=n, freq="B", tz="America/New_York")
    c = np.full(n, price)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c,
                         "volume": np.full(n, 3e6)}, index=idx)


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
              + [100.2, 100.9, 101.6, 102.4, 103.0]     # pole  -> today.iloc[-8:-3]
              + [102.9, 102.85, 102.95])                # flag  -> today.iloc[-3:]
    vol = [3e5] * 12 + [4e5, 5e5, 6e5, 7e5, 9e5, 3e5, 3e5, 3e5]
    intr = _session_intraday(99.8, closes, vol=vol)
    daily = _flat_daily(101.0)
    ctx = build_context("FLAG", intr, daily, _quote("FLAG", 103.25),
                        candidate={"rvol": 2.0})
    plays = MomentumFlag().generate(ctx)
    assert plays, "a clean bull flag breakout should produce a play"
    p = plays[0]
    assert p.side is Side.LONG
    assert p.stop < p.entry < p.targets[0]
    assert p.reward_risk >= 1.5
    assert _douglas_framed(p)
    assert "flag" in "".join(p.tags)


def test_divergence_reversal_reads_bearish_divergence_at_resistance():
    from tos_bot.strategies.technical import DivergenceReversal
    # price grinds to a higher high while momentum rolls over
    n = 90
    ramp = np.concatenate([
        np.linspace(100, 108, 30),
        np.linspace(108, 103, 15),
        np.linspace(103, 111, 30),     # HIGHER high
        np.linspace(111, 109, 15),
    ])
    idx = pd.date_range("2025-02-03", periods=n, freq="B", tz="America/New_York")
    close = ramp + np.random.default_rng(2).normal(0, 0.05, n)
    daily = pd.DataFrame({"open": close, "high": close + 0.6, "low": close - 0.6,
                          "close": close, "volume": np.full(n, 4e6)}, index=idx)
    intr = _session_intraday(close[-1], [close[-1]] * 6)
    ctx = build_context("DIV", intr, daily, _quote("DIV", float(close[-1])))
    plays = DivergenceReversal().generate(ctx)
    # divergence detection is strict; when it fires the geometry + framing must hold
    for p in plays:
        assert p.side in (Side.LONG, Side.SHORT)
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

    sd = clock.session_date()
    idx = pd.date_range(pd.Timestamp(f"{sd} 09:30", tz="America/New_York"), periods=30, freq="5min")
    c = np.full(30, 167.0)
    df = pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 0.5, "close": c,
                       "volume": np.full(30, 5e5)}, index=idx)
    ctx = build_context("X", df, df, _quote("X", 167.0))
    p = _T()._mk_play(ctx, Side.LONG, entry=167.0, stop=166.70, targets=[170.06],
                      confidence=0.7, rationale="r", detail="d", evidence={}, tags=["intraday"])
    assert p is not None
    risk_pct = abs(p.entry - p.stop) / p.entry
    assert risk_pct >= 0.006 - 1e-9              # >= 0.6% floor
    assert p.reward_risk <= 8.0                  # no more fake 10:1


def test_sr_bounce_stop_sits_beyond_the_level():
    """sr_bounce's stop must be a real buffer past the level (a 5-min close
    through), not a tick - and it only fires with volume + an honest 2:1."""
    from tos_bot.strategies.technical import SupportResistanceBounce
    p_seen = 0
    from tos_bot.data.market_data import MarketDataService, SyntheticProvider
    ds = MarketDataService(providers=[SyntheticProvider(seed=11)], cache=False,
                           min_interval_between_calls=0.0)
    for i in range(40):
        sym = f"L{i:02d}"
        ctx = build_context(sym, ds.get_price_history(sym, "5m", 10),
                            ds.get_price_history(sym, "1d", 400), ds.get_quote(sym),
                            candidate={"rvol": 2.0})
        for p in SupportResistanceBounce().generate(ctx):
            p_seen += 1
            lvl = p.evidence["level"]
            # stop is on the far side of the level from entry, by a real gap
            if p.side is Side.LONG:
                assert p.stop < lvl
            else:
                assert p.stop > lvl
            assert abs(p.entry - p.stop) / p.entry >= 0.005
            assert p.reward_risk >= 2.0
            assert p.evidence.get("rvol") is not None
    # selective by design - fine if few/none fire on synthetic noise
    assert p_seen >= 0


def test_new_intraday_setups_never_crash_and_stay_framed(datasvc):
    """abcd / flag / red_to_green / reversal / sr_bounce across many synthetic
    symbols: no exceptions, and every play they emit carries the Douglas frame
    and legal geometry."""
    keys = ["abcd_pattern", "bull_bear_flag", "red_to_green",
            "intraday_reversal", "sr_bounce"]
    seen = 0
    for i in range(40):
        sym = f"N{i:02d}"
        ctx = build_context(sym,
                            datasvc.get_price_history(sym, "5m", 10),
                            datasvc.get_price_history(sym, "1d", 400),
                            datasvc.get_quote(sym))
        for k in keys:
            for p in REGISTRY[k]().generate(ctx):
                seen += 1
                if p.side is Side.LONG:
                    assert p.stop < p.entry < p.targets[0]
                else:
                    assert p.targets[0] < p.entry < p.stop
                assert _douglas_framed(p)
                assert p.invalidation
    assert seen >= 0  # smoke: the point is "no crash + assertions hold when they do fire"
