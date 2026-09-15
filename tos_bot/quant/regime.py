"""Calm and turbulent markets: a two-regime Markov-switching model (Hamilton, *Time Series
Analysis*, ch. 22).

The market's daily return is drawn from one of two regimes, each with its own mean and
volatility: y(t) | s(t) = j ~ N(μ_j, σ_j²). The regime s(t) follows a Markov chain with
p_ij = P(s(t) = j | s(t-1) = i). Nobody sees the regime; the model infers it.

- **Filter** [22.4.5]-[22.4.6]: ξ(t|t) = (ξ(t|t-1) ⊙ η(t)) / 1'(ξ(t|t-1) ⊙ η(t)) and
  ξ(t+1|t) = P ξ(t|t), where η(t) holds each regime's density of today's return. The log
  likelihood is the sum of log 1'(ξ(t|t-1) ⊙ η(t)) [22.4.7]-[22.4.8]. ξ(t|t) uses only data up to
  t, so it is what can be traded on - no look-ahead.
- **Smoother** (Kim's algorithm, [22.4.14]): ξ(t|T) = ξ(t|t) ⊙ {P'[ξ(t+1|T) (÷) ξ(t+1|t)]}, the
  best estimate of each past regime given all the data - used only to fit the parameters.
- **EM** fitting: the transition probabilities from the smoothed pair probabilities [22.4.16],
  the starting probabilities from ξ(1|T) [22.4.17], and each regime's mean and variance as
  probability-weighted averages - [22.4.20]-[22.4.21] with a variance per regime.

The high-volatility regime is called turbulent. Chan (*Algorithmic Trading* ch. 8) shows why this
matters: a leading risk indicator can tell one strategy to stand aside while another thrives -
short-term reversal earned more when volatility was high, an opening-gap momentum strategy far less.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import numpy as np

from .stationarity import clean

SCALE = 100.0
_MIN_VAR = 1e-6


@dataclass
class RegimeFit:
    means: Tuple[float, float]           # daily, as fractions
    vols: Tuple[float, float]
    transition: np.ndarray               # P[j, i] = P(s(t) = j | s(t-1) = i)  (Hamilton's P)
    start: np.ndarray
    loglik: float
    filtered: np.ndarray = field(repr=False)     # T × 2, P(s(t) = j | data up to t)

    @property
    def turbulent(self) -> int:
        return int(np.argmax(self.vols))

    @property
    def p_turbulent(self) -> float:
        """The probability that the market is in the turbulent regime today."""
        return float(self.filtered[-1, self.turbulent])

    def expected_days(self, regime: int) -> float:
        """How long the regime lasts on average: 1 / (1 - p_jj)."""
        stay = float(self.transition[regime, regime])
        return math.inf if stay >= 1 else 1 / (1 - stay)

    def as_dict(self) -> Dict[str, object]:
        calm = 1 - self.turbulent
        return {"p_turbulent": round(self.p_turbulent, 3),
                "turbulent": {"mean": round(self.means[self.turbulent], 5), "vol": round(self.vols[self.turbulent], 5),
                              "days": round(self.expected_days(self.turbulent), 1)},
                "calm": {"mean": round(self.means[calm], 5), "vol": round(self.vols[calm], 5),
                         "days": round(self.expected_days(calm), 1)}}


def _densities(y: np.ndarray, mu: np.ndarray, var: np.ndarray) -> np.ndarray:
    return np.exp(-0.5 * (y[:, None] - mu[None, :]) ** 2 / var[None, :]) / np.sqrt(2 * math.pi * var[None, :])


def _filter(y: np.ndarray, mu: np.ndarray, var: np.ndarray, p: np.ndarray,
            start: np.ndarray) -> Tuple[np.ndarray, np.ndarray, float]:
    eta = np.maximum(_densities(y, mu, var), 1e-300)
    t = len(y)
    filtered, predicted = np.empty((t, 2)), np.empty((t, 2))
    prior, loglik = start, 0.0
    for i in range(t):
        predicted[i] = prior
        joint = prior * eta[i]
        total = joint.sum()
        loglik += math.log(total)
        filtered[i] = joint / total
        prior = p @ filtered[i]
    return filtered, predicted, loglik


def _smooth(filtered: np.ndarray, predicted: np.ndarray, p: np.ndarray) -> np.ndarray:
    smoothed = np.empty_like(filtered)
    smoothed[-1] = filtered[-1]
    for i in range(len(filtered) - 2, -1, -1):
        smoothed[i] = filtered[i] * (p.T @ (smoothed[i + 1] / np.maximum(predicted[i + 1], 1e-300)))
    return smoothed


def fit_markov_switching(returns, iterations: int = 80, tolerance: float = 1e-6) -> Optional[RegimeFit]:
    """Fit the two-regime model to daily log returns by EM; None with fewer than 250 returns."""
    y = clean(returns) * SCALE
    if len(y) < 250:
        return None
    sd = float(np.std(y))
    mu = np.array([float(np.mean(y)), float(np.mean(y))])
    var = np.array([(0.7 * sd) ** 2, (1.6 * sd) ** 2])
    p = np.array([[0.95, 0.05], [0.05, 0.95]])
    start = np.array([0.5, 0.5])
    previous = -math.inf
    for _ in range(iterations):
        filtered, predicted, loglik = _filter(y, mu, var, p, start)
        smoothed = _smooth(filtered, predicted, p)
        # the probability of each pair of consecutive regimes, given all the data (Kim)
        pairs = (smoothed[1:, :, None] * p[None, :, :] * filtered[:-1, None, :]
                 / np.maximum(predicted[1:, :, None], 1e-300))                    # [t, j, i]
        counts = pairs.sum(axis=0)
        p = counts / np.maximum(counts.sum(axis=0, keepdims=True), 1e-300)        # [22.4.16]
        weights = smoothed.sum(axis=0)
        mu = (smoothed * y[:, None]).sum(axis=0) / weights                         # [22.4.20]
        var = np.maximum((smoothed * (y[:, None] - mu[None, :]) ** 2).sum(axis=0) / weights, _MIN_VAR)  # [22.4.21]
        start = smoothed[0]                                                        # [22.4.17]
        if abs(loglik - previous) < tolerance:
            break
        previous = loglik
    filtered, _, loglik = _filter(y, mu, var, p, start)
    return RegimeFit(means=(mu[0] / SCALE, mu[1] / SCALE), vols=(math.sqrt(var[0]) / SCALE, math.sqrt(var[1]) / SCALE),
                     transition=p, start=start, loglik=loglik, filtered=filtered)


def filtered_probabilities(returns, fit: RegimeFit) -> np.ndarray:
    """ξ(t|t) for new returns under a fitted model - for replaying days after the fit period
    without letting the fit see them."""
    y = clean(returns) * SCALE
    mu = np.array(fit.means) * SCALE
    var = np.maximum((np.array(fit.vols) * SCALE) ** 2, _MIN_VAR)
    filtered, _, _ = _filter(y, mu, var, fit.transition, fit.start)
    return filtered
