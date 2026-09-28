"""How much of a record is skill, and how much is luck, drift or costs.

* **Aronson**, *Evidence-Based Technical Analysis* (ch. 1, 5, 6): a rule's mean return is tested
  against zero with a bootstrap of its zero-centred returns; when many rules were tried, the best one
  is tested against the best that luck alone produces (White's reality check), or data mining makes
  a winner out of noise. Returns are detrended first - a rule that is long in a rising market earns
  nothing for being long.
* **López de Prado**, *Advances in Financial Machine Learning* (ch. 11-14): a backtest is a
  hypothesis test, and every trial counts against it.
* **Tharp**, *Trade Your Way to Financial Freedom* (ch. 7, 13): expectancy is the mean R-multiple,
  and the spread of the R-multiples decides how hard a system is to trade - the ratio he later named
  the System Quality Number. Draw the R-multiples like marbles from a bag to see the drawdowns to
  expect before they happen.
* **Carver**, *Systematic Trading* (ch. 12): costs are the one number known in advance - a rule
  that pays more than a third of its pre-cost return in costs is trading too fast.

Everything here is numpy only and seeded, so the same trades always give the same verdict.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional, Sequence

import numpy as np

DRAWS = 2000
SEED = 20060926
MIN_TRADES = 5                 # fewer than this and nothing is said about luck
BIG_SAMPLE = 3000              # from here the bootstrap of a mean is the normal curve, to every digit shown
SQN_CAP = 100                  # Tharp caps the count so a flood of small trades can't buy a grade
SPEED_LIMIT = 1.0 / 3.0        # Carver: the share of a rule's pre-cost return that costs may take
_CHUNK = 250

SQN_GRADES = ((7.0, "holy grail"), (5.0, "superb"), (3.0, "excellent"), (2.5, "good"), (2.0, "average"),
              (1.6, "below average"))


def _clean(rs: Sequence[float]) -> np.ndarray:
    arr = np.asarray(list(rs), dtype=float)
    return arr[np.isfinite(arr)]


def sqn(rs: Sequence[float]) -> Optional[float]:
    """Tharp's System Quality Number: the mean R over its standard deviation, times the root of
    the trade count (capped at 100). Under 1.6 a system is hard to trade whatever its expectancy."""
    arr = _clean(rs)
    if len(arr) < MIN_TRADES:
        return None
    spread = float(arr.std(ddof=1))
    if spread <= 0:
        return None
    return round(math.sqrt(min(len(arr), SQN_CAP)) * float(arr.mean()) / spread, 2)


def sqn_grade(value: Optional[float]) -> str:
    if value is None:
        return ""
    return next((name for floor, name in SQN_GRADES if value >= floor), "hard to trade")


def _normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _bootstrap_means(centred: np.ndarray, rng: np.random.Generator, draws: int) -> np.ndarray:
    """Means of ``draws`` resamples of ``centred``, drawn with replacement."""
    n = len(centred)
    if n >= BIG_SAMPLE:
        return rng.normal(0.0, float(centred.std(ddof=1)) / math.sqrt(n), size=draws) + float(centred.mean())
    out = np.empty(draws)
    for start in range(0, draws, _CHUNK):
        size = min(_CHUNK, draws - start)
        out[start:start + size] = centred[rng.integers(0, n, size=(size, n))].mean(axis=1)
    return out


def luck_test(rs: Sequence[float], draws: int = DRAWS, seed: int = SEED) -> Optional[Dict[str, float]]:
    """Aronson's bootstrap test of one record: could a mean this high come from trades whose true
    mean is zero? ``p_value`` is the share of resampled zero-centred means at least as high as
    the one observed; ``ci_low`` / ``ci_high`` bound the true mean at 90%."""
    arr = _clean(rs)
    if len(arr) < MIN_TRADES or float(arr.std(ddof=1)) <= 0:
        return None
    rng = np.random.default_rng(seed)
    mean = float(arr.mean())
    null = _bootstrap_means(arr - mean, rng, draws)
    p = (1.0 + float((null >= mean).sum())) / (draws + 1.0)
    real = null + mean                                   # the same resamples, around the observed mean
    return {"p_value": round(p, 4), "ci_low": round(float(np.percentile(real, 5)), 3),
            "ci_high": round(float(np.percentile(real, 95)), 3)}


def reality_check(groups: Mapping[str, Sequence[float]], draws: int = DRAWS, seed: int = SEED) -> Dict[str, float]:
    """White's reality check across every setup that was tried (Aronson ch. 6), on studentised
    means so a thin record and a thick one compete fairly. Each setup's adjusted p-value is the
    share of resamples in which the *best* of all the setups, with every true mean set to zero,
    looked at least as good as this one does. The setups are resampled independently, which makes
    the best-of-luck a little better than it is - the test errs toward "not proven"."""
    stats: Dict[str, float] = {}
    nulls = []
    rng = np.random.default_rng(seed)
    for key in sorted(groups):
        arr = _clean(groups[key])
        if len(arr) < MIN_TRADES:
            continue
        spread = float(arr.std(ddof=1))
        if spread <= 0:
            continue
        error = spread / math.sqrt(len(arr))
        stats[key] = float(arr.mean()) / error
        nulls.append(_bootstrap_means(arr - arr.mean(), rng, draws) / error)
    if not nulls:
        return {}
    best = np.max(np.vstack(nulls), axis=0)
    return {key: round((1.0 + float((best >= t).sum())) / (draws + 1.0), 4) for key, t in stats.items()}


def marble_bag(rs: Sequence[float], trades: int = 100, runs: int = DRAWS, seed: int = SEED) -> Optional[Dict[str, float]]:
    """Tharp's marble bag: draw ``trades`` R-multiples with replacement, many times over, and
    read off the drawdown to expect - the median and the one-in-twenty worst - and how often the
    run ends under water. What a position size has to survive."""
    arr = _clean(rs)
    if len(arr) < MIN_TRADES:
        return None
    rng = np.random.default_rng(seed)
    paths = np.cumsum(rng.choice(arr, size=(runs, trades), replace=True), axis=1)
    peaks = np.maximum.accumulate(np.maximum(paths, 0.0), axis=1)
    depth = (paths - peaks).min(axis=1)
    return {"median_drawdown_r": round(float(np.median(depth)), 1),
            "p95_drawdown_r": round(float(np.percentile(depth, 5)), 1),
            "p_losing_run": round(float((paths[:, -1] < 0).mean()), 3)}


def cost_share(net_r: float, cost_r: float) -> Optional[float]:
    """Carver's speed limit: the share of the pre-cost edge that costs take. None when there is no
    pre-cost edge to take a share of."""
    gross = float(net_r) + float(cost_r)
    if gross <= 0 or cost_r <= 0:
        return None
    return round(float(cost_r) / gross, 3)


def judge(rs: Sequence[float], drift: Optional[Sequence[float]] = None,
          costs: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """Everything the books ask of one record. ``drift``: what each trade would have made, in R,
    from the stock's own average drift while it was held (the record is judged net of it);
    ``costs``: what each trade paid in slippage and commission, in R."""
    arr = _clean(rs)
    out: Dict[str, Any] = {}
    if len(arr) < MIN_TRADES:
        return out
    edge = arr - _clean(drift) if drift is not None and len(_clean(drift)) == len(arr) else arr
    out["edge_r"] = round(float(edge.mean()), 3)
    quality = sqn(edge)
    if quality is not None:
        out["sqn"], out["sqn_grade"] = quality, sqn_grade(quality)
    luck = luck_test(edge)
    if luck:
        out.update(p_value=luck["p_value"], ci_low_r=luck["ci_low"], ci_high_r=luck["ci_high"])
    bag = marble_bag(arr)
    if bag:
        out["drawdown"] = bag
    if costs is not None and len(_clean(costs)) == len(arr):
        paid = float(_clean(costs).mean())
        out["cost_r"] = round(paid, 3)
        share = cost_share(float(arr.mean()), paid)
        if share is not None:
            out["cost_share"] = share
    return out
