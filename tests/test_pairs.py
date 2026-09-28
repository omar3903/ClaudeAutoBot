"""Pairs trading: the rules and sizing, finding pairs, and replaying them out of sample."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from autotradebot.pairs.backtest import replay_pairs, simulate, validate
from autotradebot.pairs.finder import FinderSettings, aligned_closes, find_pairs, fit_pair
from autotradebot.pairs.model import (KEY, LONG_SPREAD, SHORT_SPREAD, PairModel, PairRules, current_z, pair_pl,
                                 rolling_stats, signal, size_pair)
from autotradebot.util import clock

NY = "America/New_York"
LAST = dt.date(2026, 9, 11)
GROUPS = {"AAA": "Semis", "BBB": "Semis", "EEE": "Semis", "CCC": "Banks", "DDD": "Banks", "FFF": "Banks"}


def industry(n=300, seed=1):
    """Two industries of daily candles: AAA and BBB are a pair (log AAA = 1.3 log BBB plus a spread
    that halves in about three sessions), CCC and DDD another, EEE and FFF wander on their own."""
    rng = np.random.default_rng(seed)
    days = pd.DatetimeIndex([pd.Timestamp(d, tz=NY) for d in sorted(clock.last_n_sessions(LAST, n))])

    def reverting(phi, sd):
        e = np.zeros(n)
        for i in range(1, n):
            e[i] = phi * e[i - 1] + rng.normal(0, sd)
        return e

    def walk():
        return np.cumsum(rng.normal(0.0003, 0.015, n))

    logs = {"BBB": 4.0 + walk(), "DDD": 3.0 + walk(), "EEE": 3.5 + walk(), "FFF": 3.6 + walk()}
    logs["AAA"] = 0.2 + 1.3 * logs["BBB"] + reverting(0.8, 0.01)
    logs["CCC"] = 0.5 + 0.8 * logs["DDD"] + reverting(0.85, 0.012)
    return {s: pd.DataFrame({"open": np.exp(v), "high": np.exp(v) * 1.01, "low": np.exp(v) * 0.99, "close": np.exp(v),
                             "volume": np.full(n, 5e6)}, index=days) for s, v in logs.items()}


def _model(**over):
    base = dict(first="A", second="B", hedge=1.5, half_life=5.0, lookback=10, entry_z=2.0, exit_z=0.0, stop_z=4.0,
                time_stop_days=10, adf_stat=-4.0, correlation=0.9, crossings=12)
    return PairModel(**{**base, **over})


# ---------------------------------------------------------------- the rules
def test_enter_at_the_band_exit_at_the_mean_stop_well_beyond_it_and_give_up_on_time():
    m = _model()
    assert (signal(-2.1, None, 0, m), signal(2.0, None, 0, m), signal(1.9, None, 0, m)) == ("enter_long", "enter_short", None)
    assert signal(-1.0, LONG_SPREAD, 3, m) is None and signal(0.1, LONG_SPREAD, 3, m) == "exit"
    assert signal(-4.2, LONG_SPREAD, 3, m) == "stop" and signal(-1.0, LONG_SPREAD, 10, m) == "time"
    assert signal(-0.5, SHORT_SPREAD, 2, m) == "exit" and signal(4.0, SHORT_SPREAD, 2, m) == "stop"
    assert signal(float("nan"), None, 0, m) is None


def test_the_z_score_is_read_against_the_lookback_with_the_live_prices_as_today():
    z, sd = rolling_stats([1.0, 2.0, 3.0, 4.0, 5.0], 5)
    assert np.isnan(z[:4]).all() and z[-1] == pytest.approx(2 / np.std([1, 2, 3, 4, 5], ddof=1))
    m = _model(hedge=1.0)
    a, b = np.array([100.0, 101.0] * 10), np.full(20, 50.0)
    assert abs(current_z(m, a, b)[0]) < 1.5
    assert current_z(m, a, b, 110.0, 50.0)[0] > 2.5                                 # the live price opens the spread


def test_each_leg_is_sized_off_the_distance_to_the_stop_and_capped():
    m = _model()
    # 2 z to the stop x a 0.02 spread deviation = 4 cents lost per dollar of the first stock
    size = size_pair(m, 50.0, 20.0, -2.0, 0.02, risk_dollars=1000.0, max_leg_value=100_000.0)
    assert (size.qty_first, size.qty_second, size.dollar_risk) == (500, 1875, 1000.0)
    capped = size_pair(m, 50.0, 20.0, -2.0, 0.02, risk_dollars=1000.0, max_leg_value=15_000.0)
    assert (capped.qty_first, capped.qty_second) == (200, 750) and "max position % of equity" in capped.caps
    assert size_pair(m, 50.0, 20.0, -2.0, 0.02, risk_dollars=0.5, max_leg_value=100_000.0).qty_first == 0
    assert pair_pl(LONG_SPREAD, 100, 50.0, 51.0, 150, 20.0, 20.2) == pytest.approx(70.0)
    assert pair_pl(SHORT_SPREAD, 100, 50.0, 51.0, 150, 20.0, 20.2) == pytest.approx(-70.0)


# ---------------------------------------------------------------- finding pairs
def test_the_finder_picks_the_cointegrated_pair_in_each_industry_and_nothing_else():
    frames = industry()
    models = find_pairs(frames, GROUPS, PairRules(), FinderSettings())
    assert {frozenset((m.first, m.second)) for m in models} == {frozenset(("AAA", "BBB")), frozenset(("CCC", "DDD"))}
    semis = next(m for m in models if m.group == "Semis")
    hedge = semis.hedge if semis.first == "AAA" else 1 / semis.hedge
    assert 1.1 < hedge < 1.5 and 1.0 <= semis.entry_z <= 2.5 and semis.stop_z == pytest.approx(semis.entry_z + 2.0)
    assert 5 <= semis.time_stop_days <= 40 and 10 <= semis.lookback <= 60 and semis.crossings >= 6
    frames["BBB"]["volume"] = 1.0                                                      # too thin to trade
    assert all("BBB" not in (m.first, m.second) for m in find_pairs(frames, GROUPS, PairRules(), FinderSettings()))


# ---------------------------------------------------------------- replaying them
def test_a_pair_traded_after_it_was_fitted_makes_money_and_costs_take_their_share():
    frames = industry()
    a, b, dates = aligned_closes(frames["AAA"], frames["BBB"])
    model = fit_pair("AAA", "BBB", a[:200], b[:200], PairRules(), FinderSettings())
    fa, fb = (a, b) if model.first == "AAA" else (b, a)
    trades = simulate(model, fa, fb, dates, PairRules(), start=200)
    assert len(trades) >= 5 and np.mean([t.r for t in trades]) > 0
    assert all(t.strategy == KEY and t.timeframe == "SWING" and t.exit_reason in ("mean", "stop", "time-stop")
               and t.entered_at >= dates[200].isoformat() for t in trades)
    costly = simulate(model, fa, fb, dates, PairRules(cost_bps=50.0), start=200)
    assert np.mean([t.r for t in costly]) < np.mean([t.r for t in trades])


def test_the_replay_chooses_its_pairs_before_the_sessions_it_trades_them_on():
    frames = industry()
    trades = replay_pairs(frames, GROUPS, PairRules(), FinderSettings(), sessions=150)
    first_traded = min(clock.last_n_sessions(LAST, 150)).isoformat()
    assert trades and all(t.entered_at >= first_traded for t in trades)
    assert {frozenset(t.symbol.split("/")) for t in trades} <= {frozenset(("AAA", "BBB")), frozenset(("CCC", "DDD"))}
    assert replay_pairs({s: f.tail(130) for s, f in frames.items()}, GROUPS, PairRules(), FinderSettings(), 150) == []


def test_a_watched_pair_shows_how_its_rules_did_on_the_latest_sessions():
    frames = industry()
    record = validate("AAA", "BBB", frames, PairRules(), FinderSettings(), test_days=100)
    assert record["stable"] and record["trades"] >= 3 and record["sessions"] == 100
    year = validate("AAA", "BBB", {s: f.tail(252) for s, f in frames.items()}, PairRules(), FinderSettings(), 100)
    assert year["stable"] and year["sessions"] == 100                              # a year of candles: fitted on 152
    assert validate("AAA", "BBB", {s: f.tail(200) for s, f in frames.items()}, PairRules(), FinderSettings(), 100)["stable"] is None
    assert validate("EEE", "FFF", frames, PairRules(), FinderSettings(), 100)["stable"] is False


def test_the_replay_runs_the_pairs_as_one_job():
    from autotradebot.research.runner import replay_job

    inputs = {"groups": GROUPS, "rules": PairRules(), "finder": FinderSettings()}
    rows = replay_job(("pairs", inputs, industry(), 150))
    assert rows and all(r["strategy"] == KEY and r["timeframe"] == "SWING" for r in rows)
