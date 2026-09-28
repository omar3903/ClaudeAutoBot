"""Is a price series mean reverting, trending, or a random walk?

The tests Chan runs before trading any mean reversion (*Algorithmic Trading*, ch. 2):

- **ADF** - regress the change Δy(t) on the level y(t-1), a constant and lagged changes. A
  clearly negative t-statistic on y(t-1) rejects a random walk; the critical values are the
  Dickey-Fuller ones for a regression with a constant (Enders, table 4.2; Hamilton ch. 17).
- **Hurst exponent** - ⟨|z(t+τ) - z(t)|²⟩ ∝ τ^(2H) for log prices z. H = 0.5 is a random walk,
  below it the price is mean reverting, above it trending (Chan eq. 2.4). Chan also uses it as
  a momentum test (ch. 6).
- **Variance ratio** (Lo and MacKinlay) - the variance of k-period returns over k times the
  variance of 1-period returns, with the z-score that tests H = 0.5.
- **Half-life** - -ln 2 / λ from regressing Δy(t) on y(t-1) (the Ornstein-Uhlenbeck reading of
  the same regression). It is the natural lookback and the holding period to expect; λ ≥ 0
  means no pull back at all.
- **Zero crossings** (Vidyamurthy, *Pairs Trading*, ch. 7) - how often a spread crosses its
  mean; the time between crossings, bootstrapped, is the holding period and a basis for a
  time stop.

Chan's point about certainty: the ADF and variance-ratio tests demand 90% confidence, but a
short half-life is often enough to trade on, so the checks here lean on the half-life and
the Hurst exponent and report the tests alongside.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np

# Dickey-Fuller critical values for a regression with a constant and no trend, by sample size
# (Fuller 1976, as tabulated by Enders; Chan's large-sample output: -3.458 / -2.871 / -2.594)
_DF_CRITICAL = ((25, (-3.75, -3.00, -2.63)), (50, (-3.58, -2.93, -2.60)), (100, (-3.51, -2.89, -2.58)),
                (250, (-3.46, -2.88, -2.57)), (500, (-3.44, -2.87, -2.57)), (10 ** 9, (-3.43, -2.86, -2.57)))


def clean(values) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    return arr[np.isfinite(arr)]


def ols(y: np.ndarray, x: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Coefficients, their standard errors, and the residuals."""
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ beta
    dof = max(1, len(y) - x.shape[1])
    cov = (float(resid @ resid) / dof) * np.linalg.pinv(x.T @ x)
    return beta, np.sqrt(np.clip(np.diag(cov), 0.0, None)), resid


def df_critical_values(n: int) -> Dict[str, float]:
    for size, (one, five, ten) in _DF_CRITICAL:
        if n <= size:
            return {"1%": one, "5%": five, "10%": ten}
    return {"1%": -3.43, "5%": -2.86, "10%": -2.57}


@dataclass(frozen=True)
class AdfResult:
    stat: float                  # the t-statistic of λ
    lam: float                   # the coefficient on y(t-1); negative pulls back to the mean
    critical: Dict[str, float]
    n: int


def adf(series, lags: int = 1) -> Optional[AdfResult]:
    """Augmented Dickey-Fuller test with a constant and ``lags`` lagged changes (Chan uses 1)."""
    y = clean(series)
    dy = np.diff(y)
    target = dy[lags:]
    if len(target) < 20:
        return None
    columns = [y[lags:-1], np.ones(len(target))]
    columns += [dy[lags - i:-i] for i in range(1, lags + 1)]
    beta, se, _ = ols(target, np.column_stack(columns))
    stat = float(beta[0] / se[0]) if se[0] > 0 else 0.0
    return AdfResult(stat=stat, lam=float(beta[0]), critical=df_critical_values(len(target)), n=len(target))


def half_life(series) -> float:
    """Bars for a deviation from the mean to halve: -ln 2 / λ from regressing Δy(t) on y(t-1)
    and a constant (Chan, example 2.4). Infinite when the series doesn't pull back."""
    y = clean(series)
    if len(y) < 20:
        return math.inf
    beta, _, _ = ols(np.diff(y), np.column_stack([y[:-1], np.ones(len(y) - 1)]))
    lam = float(beta[0])
    return math.inf if lam >= -1e-9 else -math.log(2) / lam       # a λ within rounding of zero doesn't pull back


def hurst(log_prices, max_lag: int = 20) -> float:
    """Hurst exponent from ⟨|z(t+τ) - z(t)|²⟩ ∝ τ^(2H) over τ = 2 .. ``max_lag``."""
    z = clean(log_prices)
    lags = [lag for lag in range(2, max_lag + 1) if len(z) - lag >= 10]
    if len(lags) < 3:
        return math.nan
    moments = [float(np.mean((z[lag:] - z[:-lag]) ** 2)) for lag in lags]
    if min(moments) <= 0:
        return math.nan
    return float(np.polyfit(np.log(lags), np.log(moments), 1)[0] / 2)


def variance_ratio(log_prices, k: int = 2) -> Tuple[float, float]:
    """Lo-MacKinlay variance ratio of ``k``-period returns and its z-score (homoskedastic);
    below 1 with a negative z means mean reversion, above 1 trending."""
    z = clean(log_prices)
    r = np.diff(z)
    t = len(r)
    if k < 2 or t < 5 * k:
        return math.nan, math.nan
    mu = float(r.mean())
    var1 = float(np.sum((r - mu) ** 2)) / (t - 1)
    m = k * (t - k + 1) * (1 - k / t)
    vark = float(np.sum((z[k:] - z[:-k] - k * mu) ** 2)) / m
    if var1 <= 0:
        return math.nan, math.nan
    ratio = vark / var1
    return ratio, (ratio - 1) / math.sqrt(2 * (2 * k - 1) * (k - 1) / (3 * k * t))


def crossing_times(series) -> np.ndarray:
    """Bars between successive crossings of the series' mean."""
    s = clean(series)
    if len(s) < 3:
        return np.array([])
    side = np.sign(s - s.mean())
    at = np.nonzero(side)[0]
    crossings = at[1:][np.diff(side[at]) != 0]
    return np.diff(crossings).astype(float)


def holding_period(series, samples: int = 2000, seed: int = 7) -> Optional[Dict[str, float]]:
    """What the time between mean crossings says about holding a spread (Vidyamurthy ch. 7):
    the median gap, and bootstrapped 25th / 75th percentiles of the average gap."""
    gaps = crossing_times(series)
    if len(gaps) < 3:
        return None
    rng = np.random.default_rng(seed)
    means = rng.choice(gaps, size=(samples, len(gaps)), replace=True).mean(axis=1)
    return {"median": float(np.median(gaps)), "p25": float(np.percentile(means, 25)),
            "p75": float(np.percentile(means, 75)), "crossings": float(len(gaps) + 1)}
