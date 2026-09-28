"""How much to risk on a trade: half-Kelly (Chan, *Quantitative Trading* ch. 6 and
*Algorithmic Trading* ch. 8).

Kelly's optimal leverage for Gaussian returns is f = m / s², the mean excess return over its
variance. For a strategy's trades measured in R (multiples of the amount risked), risking a
fraction r of equity per 1R gives a per-trade return of r × R, so the growth-optimal r is
mean(R) / var(R). Estimation error and fat tails make the full Kelly dangerous - overestimate
the edge and it leads to ruin - so traders use half of it, and Chan treats it as an upper
bound rather than a target. Here it can only lower the configured risk per trade, never raise it.
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

MIN_TRADES = 30


def half_kelly_risk_pct(r_multiples: Sequence[float], max_risk_pct: float,
                        min_trades: int = MIN_TRADES) -> Optional[float]:
    """Percent of equity to risk per trade from a strategy's R multiples: half of Kelly's
    mean / variance, capped at ``max_risk_pct``. 0 when the record shows no edge; None when
    there are too few trades to say."""
    r = np.asarray(list(r_multiples), dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < min_trades:
        return None
    mean, var = float(r.mean()), float(r.var(ddof=1))
    if mean <= 0 or var <= 0:
        return 0.0
    return float(min(max_risk_pct, 0.5 * 100.0 * mean / var))
