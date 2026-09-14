"""The "Seven Methods of Projections" (Pignataro Ch. 1, pp. 49-51).

Used to project a base-year metric (revenue, EBITDA, UFCF, ...) forward for
the DCF. Each returns a list of length ``years``:

    conservative   max of the last 3 years
    aggressive     min of the last 3 years
    average        average of the last 3 years
    last_year      most recent actual
    repeat_cycle   replay the historical sequence
    yoy_growth     apply a constant YoY growth rate
    pct_of_line    as a % of another projected line item
"""

from __future__ import annotations

import math
import statistics
from typing import List, Optional


def _clean(history: List[float]) -> List[float]:
    return [float(v) for v in history if v is not None and not (isinstance(v, float) and math.isnan(v))]


def project_series(
    history: List[float],
    years: int,
    method: str = "last_year",
    *,
    yoy_growth: Optional[float] = None,
    pct_of: Optional[List[float]] = None,
    pct_ratio: Optional[float] = None,
    fallback_growth: float = 0.03,
) -> List[float]:
    hist = _clean(history)
    if not hist and method != "pct_of_line":
        return [math.nan] * years
    last3 = hist[-3:] if len(hist) >= 1 else []

    if method == "conservative":
        base = max(last3)
        return [base] * years
    if method == "aggressive":
        base = min(last3)
        return [base] * years
    if method == "average":
        base = statistics.fmean(last3)
        return [base] * years
    if method == "last_year":
        return [hist[-1]] * years
    if method == "repeat_cycle":
        seq = hist[-min(len(hist), 3):]
        return [seq[i % len(seq)] for i in range(years)]
    if method == "yoy_growth":
        g = yoy_growth
        if g is None:
            # infer from history, clamp to something sane
            if len(hist) >= 2 and hist[0] > 0 and hist[-1] > 0:
                n = len(hist) - 1
                g = (hist[-1] / hist[0]) ** (1.0 / n) - 1.0
            else:
                g = fallback_growth
        g = max(-0.5, min(0.6, g))
        out, cur = [], hist[-1]
        for _ in range(years):
            cur = cur * (1.0 + g)
            out.append(cur)
        return out
    if method == "pct_of_line":
        if not pct_of:
            return [math.nan] * years
        r = pct_ratio
        if r is None and hist and _clean(pct_of):
            r = hist[-1] / _clean(pct_of)[-1]
        r = r if r is not None else 0.0
        return [v * r for v in pct_of[:years]] + [math.nan] * max(0, years - len(pct_of))

    raise ValueError(f"unknown projection method '{method}'")
