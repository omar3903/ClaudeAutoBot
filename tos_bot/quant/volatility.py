"""Volatility forecasts: GARCH(1,1), with RiskMetrics as the fallback.

**GARCH(1,1)** (Tsay, *Analysis of Financial Time Series*, section 3.5; Enders ch. 3; Hamilton
ch. 21): with a(t) the return's surprise,

    σ²(t) = ω + α a²(t-1) + β σ²(t-1),     ω > 0, α, β ≥ 0, α + β < 1

so a big move tends to be followed by more big moves. The 1-step forecast from origin h is
ω + α a²(h) + β σ²(h), and further ahead σ²(ℓ) = ω + (α + β) σ²(ℓ-1), drifting to the long-run
variance ω / (1 - α - β) (Tsay eq. 3.17). Fitted by conditional maximum likelihood with the
starting variance at the sample variance (Tsay 3.5.1) and ω set by variance targeting.

**RiskMetrics** is the special IGARCH(1,1) with ω = 0 - exponential smoothing with factor λ
(Tsay section 3.6), used when the GARCH fit fails or its α + β runs into 1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

import numpy as np

from .stationarity import clean

SCALE = 100.0                    # returns are fitted in percent, for numerical comfort
MAX_PERSISTENCE = 0.999


@dataclass(frozen=True)
class GarchFit:
    omega: float                 # in percent²
    alpha: float
    beta: float
    next_var: float              # 1-step-ahead variance, percent²
    long_run_var: float
    loglik: float

    @property
    def persistence(self) -> float:
        return self.alpha + self.beta

    def forecast_var(self, horizon: int = 1) -> float:
        """Variance ``horizon`` steps ahead, percent² (Tsay eq. 3.17)."""
        var = self.next_var
        for _ in range(max(0, horizon - 1)):
            var = self.omega + self.persistence * var
        return var


def _variances(a: np.ndarray, omega: float, alpha: float, beta: float, start: float) -> np.ndarray:
    out = np.empty(len(a) + 1)
    out[0] = start
    for i, shock in enumerate(a):
        out[i + 1] = omega + alpha * shock * shock + beta * out[i]
    return out


def _nelder_mead(f: Callable[[np.ndarray], float], x0, step: float = 0.6, iterations: int = 250,
                 tolerance: float = 1e-7) -> Tuple[np.ndarray, float]:
    """A plain Nelder-Mead minimiser - there is no scipy here."""
    n = len(x0)
    points = [np.asarray(x0, dtype=float)] + [np.asarray(x0, dtype=float) + step * np.eye(n)[i] for i in range(n)]
    values = [f(p) for p in points]
    for _ in range(iterations):
        order = np.argsort(values)
        points, values = [points[i] for i in order], [values[i] for i in order]
        if abs(values[-1] - values[0]) < tolerance:
            break
        centroid = np.mean(points[:-1], axis=0)
        reflected = centroid + (centroid - points[-1])
        fr = f(reflected)
        if fr < values[0]:
            expanded = centroid + 2 * (centroid - points[-1])
            fe = f(expanded)
            points[-1], values[-1] = (expanded, fe) if fe < fr else (reflected, fr)
        elif fr < values[-2]:
            points[-1], values[-1] = reflected, fr
        else:
            contracted = centroid + 0.5 * (points[-1] - centroid)
            fc = f(contracted)
            if fc < values[-1]:
                points[-1], values[-1] = contracted, fc
            else:
                points = [points[0]] + [points[0] + 0.5 * (p - points[0]) for p in points[1:]]
                values = [values[0]] + [f(p) for p in points[1:]]
    best = int(np.argmin(values))
    return points[best], values[best]


def _params(u: np.ndarray) -> Tuple[float, float]:
    e1, e2 = math.exp(min(50.0, u[0])), math.exp(min(50.0, u[1]))
    total = 1 + e1 + e2
    return MAX_PERSISTENCE * e1 / total, MAX_PERSISTENCE * e2 / total


def fit_garch11(returns) -> Optional[GarchFit]:
    """GARCH(1,1) by Gaussian maximum likelihood on log returns; None with fewer than 250."""
    r = clean(returns) * SCALE
    if len(r) < 250:
        return None
    a = r - r.mean()
    sample_var = float(np.var(a))
    if sample_var <= 0:
        return None

    def negative_loglik(u: np.ndarray) -> float:
        alpha, beta = _params(u)
        omega = sample_var * (1 - alpha - beta)
        var = _variances(a, omega, alpha, beta, sample_var)[:-1]
        if np.any(var <= 0):
            return 1e12
        return float(0.5 * np.sum(np.log(2 * math.pi * var) + a * a / var))

    start = np.array([math.log(0.05 / 0.049), math.log(0.90 / 0.049)])           # α 0.05, β 0.90
    best, value = _nelder_mead(negative_loglik, start)
    alpha, beta = _params(best)
    omega = sample_var * (1 - alpha - beta)
    var = _variances(a, omega, alpha, beta, sample_var)
    return GarchFit(omega=omega, alpha=alpha, beta=beta, next_var=float(var[-1]),
                    long_run_var=omega / max(1e-9, 1 - alpha - beta), loglik=-value)


def ewma_var(returns, lam: float = 0.94) -> float:
    """RiskMetrics variance (IGARCH(1,1) with ω = 0), percent²."""
    r = clean(returns) * SCALE
    if len(r) < 20:
        return math.nan
    var = float(np.var(r[:20]))
    for x in r[20:]:
        var = lam * var + (1 - lam) * x * x
    return var


def next_day_vol(closes) -> Optional[Dict[str, float]]:
    """Tomorrow's volatility of a stock's daily log returns, as a fraction: the GARCH(1,1)
    forecast, or RiskMetrics when the fit fails or its persistence runs into 1. ``ratio``
    compares it with the plain standard deviation of the last 60 returns - above 1 means
    volatility is expected to rise."""
    prices = clean(closes)
    prices = prices[prices > 0]
    if len(prices) < 61:
        return None
    r = np.diff(np.log(prices))
    recent = float(np.std(r[-60:])) * SCALE
    fit = fit_garch11(r)
    if fit is not None and fit.persistence < MAX_PERSISTENCE - 1e-3:
        forecast, model = math.sqrt(fit.next_var), "garch"
    else:
        var = ewma_var(r)
        if not math.isfinite(var):
            return None
        forecast, model = math.sqrt(var), "riskmetrics"
    return {"vol": forecast / SCALE, "recent_vol": recent / SCALE, "ratio": forecast / recent if recent > 0 else 1.0,
            "model": model, "persistence": round(fit.persistence, 3) if fit is not None else None}
