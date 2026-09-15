"""Cointegration: two prices that each wander, but whose spread keeps coming back.

- **Engle-Granger** (Chan, *Algorithmic Trading* ch. 2 "CADF"; Enders ch. 6; Vidyamurthy ch. 7):
  regress one log price on the other, then test the residual spread with an ADF test against
  the two-variable critical values (-3.88 / -3.36 / -3.04 at 1 / 5 / 10%, Chan's CADF output).
  The result depends on which price is the independent one, so both orders are tried and the
  more negative statistic kept, as Chan advises.
- **Johansen trace test** (Chan ch. 2; Johansen 1995, ch. 6; Juselius ch. 7-8; Hamilton ch. 20):
  order-free, from the eigenvalues of the reduced-rank regression of ΔY(t) on Y(t-1) after the
  short-run terms are concentrated out. Critical values for two series with a constant
  (Chan's output): r = 0: 13.43 / 15.49 / 19.94 and r ≤ 1: 2.71 / 3.84 / 6.63 at 90 / 95 / 99%.

Chan's warning for US stocks: pairs of single stocks often lose cointegration out of sample, so
a pair is only as good as its out-of-sample record; ETF pairs hold up better.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from .stationarity import adf, clean, half_life, ols

EG_CRITICAL = {"1%": -3.88, "5%": -3.36, "10%": -3.04}
JOHANSEN_TRACE_CRITICAL = ({"90%": 13.429, "95%": 15.494, "99%": 19.935},     # r = 0
                           {"90%": 2.705, "95%": 3.841, "99%": 6.635})        # r <= 1


@dataclass
class PairFit:
    """log(dependent) = hedge × log(independent) + mean + spread."""

    dependent: int               # 0 when the first series is the dependent one, 1 when the second
    hedge: float
    mean: float
    adf_stat: float
    half_life: float             # bars
    spread: np.ndarray = field(repr=False)

    def cointegrated(self, level: str = "10%") -> bool:
        return self.adf_stat < EG_CRITICAL[level]


def engle_granger(log_a, log_b, lags: int = 1) -> Optional[PairFit]:
    """The better of the two Engle-Granger regressions of two log-price series."""
    a, b = np.asarray(log_a, dtype=float), np.asarray(log_b, dtype=float)
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    if len(a) < 60:
        return None
    best: Optional[PairFit] = None
    for dependent, (y, x) in enumerate(((a, b), (b, a))):
        beta, _, resid = ols(y, np.column_stack([x, np.ones(len(x))]))
        test = adf(resid, lags)
        if test is None:
            continue
        fit = PairFit(dependent=dependent, hedge=float(beta[0]), mean=float(beta[1]), adf_stat=test.stat,
                      half_life=half_life(resid), spread=resid)
        if best is None or fit.adf_stat < best.adf_stat:
            best = fit
    return best


@dataclass(frozen=True)
class JohansenResult:
    trace: List[float]           # trace statistics for r = 0, r <= 1, ...
    eigenvalues: np.ndarray
    vectors: np.ndarray          # columns are the cointegrating vectors, strongest first

    def rank(self, level: str = "95%") -> int:
        """How many cointegrating relations the trace test finds (two-series critical values)."""
        rank = 0
        for stat, critical in zip(self.trace, JOHANSEN_TRACE_CRITICAL):
            if stat > critical[level]:
                rank += 1
            else:
                break
        return rank


def johansen(levels, lags: int = 1) -> Optional[JohansenResult]:
    """Johansen trace test on a T × n array of (log) prices, with a constant and ``lags`` lagged
    differences."""
    y = np.asarray(levels, dtype=float)
    y = y[np.all(np.isfinite(y), axis=1)]
    t, n = y.shape
    if t < 60 or n < 2:
        return None
    dy = np.diff(y, axis=0)
    z0, z1 = dy[lags:], y[lags:-1]
    z2 = np.column_stack([dy[lags - i:-i] for i in range(1, lags + 1)] + [np.ones(len(z0))])

    def residual(m: np.ndarray) -> np.ndarray:
        beta, *_ = np.linalg.lstsq(z2, m, rcond=None)
        return m - z2 @ beta

    r0, r1 = residual(z0), residual(z1)
    m = len(r0)
    s00, s01, s11 = r0.T @ r0 / m, r0.T @ r1 / m, r1.T @ r1 / m
    try:
        product = np.linalg.solve(s11, s01.T @ np.linalg.solve(s00, s01))
    except np.linalg.LinAlgError:
        return None
    values, vectors = np.linalg.eig(product)
    order = np.argsort(values.real)[::-1]
    values = np.clip(values.real[order], 0.0, 1 - 1e-12)
    vectors = vectors.real[:, order]
    trace = [float(-m * np.sum(np.log(1 - values[r:]))) for r in range(n)]
    return JohansenResult(trace=trace, eigenvalues=values, vectors=vectors)


def return_correlation(prices_a, prices_b) -> float:
    """Correlation of daily log returns - Vidyamurthy's distance measure for shortlisting pairs
    before any cointegration test (ch. 6)."""
    a, b = clean(prices_a), clean(prices_b)
    size = min(len(a), len(b))
    if size < 30:
        return math.nan
    ra, rb = np.diff(np.log(a[-size:])), np.diff(np.log(b[-size:]))
    if ra.std() == 0 or rb.std() == 0:
        return math.nan
    return float(np.corrcoef(ra, rb)[0, 1])
