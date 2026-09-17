"""The quantitative models, checked on series whose answers are known."""

from __future__ import annotations

import math

import numpy as np

from tos_bot.quant import bands, cointegration, readings, regime, sizing, stationarity, volatility


def _ar1(phi, n, seed, mean=0.0, sd=1.0):
    rng = np.random.default_rng(seed)
    y = np.empty(n)
    y[0] = mean
    for i in range(1, n):
        y[i] = mean + phi * (y[i - 1] - mean) + rng.normal(0, sd)
    return y


def _random_walk(n, seed, sd=0.01, drift=0.0):
    return np.cumsum(np.random.default_rng(seed).normal(drift, sd, n))


# ---------------------------------------------------------------- stationarity
def test_the_half_life_of_a_mean_reverting_series_is_recovered():
    series = _ar1(0.9, 3000, seed=1)                     # λ = -0.1, so -ln 2 / λ ≈ 6.9 bars
    assert 5.5 < stationarity.half_life(series) < 8.5
    assert stationarity.half_life(np.arange(100.0)) == math.inf


def test_adf_tells_mean_reversion_from_a_random_walk():
    reverting = stationarity.adf(_ar1(0.9, 500, seed=2))
    walk = stationarity.adf(_random_walk(500, seed=3))
    assert reverting.stat < reverting.critical["5%"] and reverting.lam < 0
    assert not walk.stat < walk.critical["5%"]
    assert stationarity.df_critical_values(100) == {"1%": -3.51, "5%": -2.89, "10%": -2.58}


def test_hurst_and_the_variance_ratio_read_random_walks_reversion_and_trends():
    walk = _random_walk(4000, seed=4)
    reverting = _ar1(0.5, 4000, seed=5, sd=0.01)
    trending = np.cumsum(_ar1(0.5, 4000, seed=6, sd=0.01))           # returns that follow through
    assert 0.4 < stationarity.hurst(walk) < 0.6
    assert stationarity.hurst(reverting) < 0.2
    assert stationarity.hurst(trending) > 0.6
    ratio, z = stationarity.variance_ratio(reverting, k=2)
    assert ratio < 1 and z < -3
    assert abs(stationarity.variance_ratio(walk, k=2)[1]) < 3


def test_the_mean_reversion_reading_and_holding_period():
    prices = np.exp(4 + _ar1(0.9, 400, seed=7, sd=0.01))
    reading = readings.price_character(prices)
    assert reading["character"] == "mean reverting" and 4 < reading["half_life_bars"] < 12
    hold = stationarity.holding_period(np.sin(np.arange(200) / 5))
    assert 14 < hold["median"] < 17                                   # half a period of 2π × 5
    assert readings.price_character([100.0] * 10) is None


# ---------------------------------------------------------------- cointegration
def test_engle_granger_finds_a_cointegrated_pair_and_its_hedge_ratio():
    b = 4 + _random_walk(600, seed=8)
    a = 0.3 + 1.5 * b + _ar1(0.7, 600, seed=9, sd=0.01)
    fit = cointegration.engle_granger(a, b)
    assert fit.cointegrated("5%") and fit.half_life < 10
    hedge = fit.hedge if fit.dependent == 0 else 1 / fit.hedge
    assert 1.4 < hedge < 1.6
    unrelated = cointegration.engle_granger(4 + _random_walk(600, seed=10), 4 + _random_walk(600, seed=11))
    assert not unrelated.cointegrated("5%")


def test_johansen_counts_the_cointegrating_relations():
    # prices drift, as the critical values for a model with a constant assume
    b = 4 + _random_walk(800, seed=12, drift=0.001)
    a = 1.2 * b + _ar1(0.6, 800, seed=13, sd=0.01)
    assert cointegration.johansen(np.column_stack([a, b])).rank("95%") == 1
    walks = np.column_stack([_random_walk(800, seed=14, drift=0.001), _random_walk(800, seed=15, drift=0.0005)])
    assert cointegration.johansen(walks).rank("95%") == 0


def test_return_correlation_shortlists_pairs():
    common = _random_walk(300, seed=16)
    a = np.exp(4 + common + _random_walk(300, seed=17, sd=0.002))
    b = np.exp(3 + common + _random_walk(300, seed=18, sd=0.002))
    assert cointegration.return_correlation(a, b) > 0.8


# ---------------------------------------------------------------- volatility
def test_garch_recovers_volatility_clustering_and_forecasts():
    rng = np.random.default_rng(19)
    omega, alpha, beta = 0.02e-4, 0.08, 0.90
    var, returns = omega / (1 - alpha - beta), []
    for _ in range(2500):
        shock = rng.normal(0, math.sqrt(var))
        returns.append(shock)
        var = omega + alpha * shock * shock + beta * var
    fit = volatility.fit_garch11(returns)
    assert 0.03 < fit.alpha < 0.15 and 0.8 < fit.beta < 0.96
    prices = 50 * np.exp(np.cumsum(returns))
    reading = volatility.next_day_vol(prices)
    assert reading["model"] in ("garch", "riskmetrics") and 0.005 < reading["vol"] < 0.05


def test_riskmetrics_tracks_a_steady_volatility():
    returns = np.random.default_rng(20).normal(0, 0.02, 1000)
    assert 1.5 < math.sqrt(volatility.ewma_var(returns)) < 2.6                       # percent


# ---------------------------------------------------------------- regimes
def test_markov_switching_finds_the_calm_and_turbulent_regimes():
    rng = np.random.default_rng(21)
    state, states, returns = 0, [], []
    for _ in range(1500):
        if rng.random() > 0.98:
            state = 1 - state
        states.append(state)
        returns.append(rng.normal(0.0005, 0.007) if state == 0 else rng.normal(-0.001, 0.02))
    fit = regime.fit_markov_switching(returns)
    turbulent = fit.turbulent
    assert 0.015 < fit.vols[turbulent] < 0.025 and 0.005 < fit.vols[1 - turbulent] < 0.009
    guessed = (fit.filtered[:, turbulent] > 0.5).astype(int)
    assert np.mean(guessed == np.array(states)) > 0.85
    assert fit.expected_days(turbulent) > 10
    later = regime.filtered_probabilities(returns[-100:], fit)
    assert later.shape == (100, 2) and np.allclose(later.sum(axis=1), 1)


# ---------------------------------------------------------------- sizing and bands
def test_half_kelly_only_ever_lowers_the_risk_per_trade():
    strong = [2.0, -1.0] * 20                                        # mean 0.5R: Kelly far above 1%
    thin = [1.04, -1.0] * 20                                         # mean 0.02R, variance ≈ 1.07: Kelly ≈ 1.9%
    thinner = [1.01, -1.0] * 20                                      # mean 0.005R: Kelly ≈ 0.49%
    assert sizing.half_kelly_risk_pct(strong, max_risk_pct=1.0) == 1.0
    assert 0.9 < sizing.half_kelly_risk_pct(thin, max_risk_pct=1.0) < 1.0
    assert 0.2 < sizing.half_kelly_risk_pct(thinner, max_risk_pct=1.0) < 0.3
    assert sizing.half_kelly_risk_pct([-1.0, 0.5] * 20, max_risk_pct=1.0) == 0.0
    assert sizing.half_kelly_risk_pct([1.0] * 10, max_risk_pct=1.0) is None


def test_the_band_for_white_noise_is_about_three_quarters_of_a_standard_deviation():
    noise = np.random.default_rng(22).normal(0, 1, 5000)
    assert 0.6 <= bands.best_band(noise) <= 0.9
    assert bands.best_band(noise, cost=0.5) > bands.best_band(noise)              # costs push the band out
