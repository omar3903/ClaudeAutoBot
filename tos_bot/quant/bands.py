"""Where to enter a spread trade: Vidyamurthy's nonparametric band design (*Pairs Trading*, ch. 8).

A band δ standard deviations from the mean earns about δ each time the spread goes beyond it
and comes back, so the profit profile is (how often the spread is beyond δ) × δ. For white
noise that is δ(1 - N(δ)), largest at 0.75σ. For a real spread the frequency is counted from
its recent history, then:

1. forced to fall as the band widens (a wider band can't be crossed more often),
2. smoothed with Tikhonov-Miller regularization - least squares plus λ × the squared
   differences between neighbouring points - with λ at the "heel" where the fit error starts
   to rise,
3. multiplied by (δ - cost), candidate bands being at least the trading cost apart, and the
   largest value taken.
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from .stationarity import clean

DEFAULT_LEVELS = np.round(np.arange(0.25, 3.01, 0.05), 2)


def _regularize(y: np.ndarray, lam: float) -> np.ndarray:
    n = len(y)
    diff = np.diff(np.eye(n), axis=0)
    return np.linalg.solve(np.eye(n) + lam * diff.T @ diff, y)


def _heel(y: np.ndarray) -> float:
    lams = 10.0 ** np.arange(-4, 4.01, 0.25)
    errors = np.array([float(np.sum((y - _regularize(y, lam)) ** 2)) for lam in lams])
    spread = errors.max() - errors.min()
    if spread <= 0:
        return float(lams[0])
    rising = np.nonzero((errors - errors.min()) / spread > 0.02)[0]
    return float(lams[max(0, rising[0] - 1)]) if len(rising) else float(lams[-1])


def best_band(zscores, levels: Optional[Sequence[float]] = None, cost: float = 0.0,
              low: float = 0.5, high: float = 2.5) -> Optional[float]:
    """The entry band, in standard deviations, for a spread's recent z-scores."""
    z = clean(zscores)
    if len(z) < 30:
        return None
    grid = np.asarray(levels if levels is not None else DEFAULT_LEVELS, dtype=float)
    counts = np.array([(np.mean(z >= level) + np.mean(z <= -level)) / 2 for level in grid])
    counts = np.minimum.accumulate(counts)
    smooth = _regularize(counts, _heel(counts))
    profit = smooth * np.maximum(grid - cost, 0.0)
    return float(np.clip(grid[int(np.argmax(profit))], low, high))
