"""The "football field" - a blended fair-value band (Pignataro Ch. 12).

Each valuation method contributes a low/high implied share price:
    - 52-week high / low
    - Comparable Company Analysis (peer multiple range)
    - DCF, EBITDA / multiple method
    - DCF, perpetuity method

We collapse them into one band and compare the current price against it:
    price below band low   -> long bias
    price above band high  -> short bias
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class MethodRange:
    name: str
    low: float
    high: float
    weight: float = 1.0

    def valid(self) -> bool:
        return (
            self.low is not None and self.high is not None
            and not math.isnan(self.low) and not math.isnan(self.high)
            and self.low > 0 and self.high >= self.low
        )


def football_field(ranges: List[MethodRange]) -> Dict[str, object]:
    good = [r for r in ranges if r.valid()]
    if not good:
        return {"ok": False, "rows": [], "band_low": None, "band_high": None, "fair_value": None}

    lows = [r.low for r in good]
    highs = [r.high for r in good]
    mids = [(r.low + r.high) / 2.0 for r in good]
    weights = [r.weight for r in good]

    band_low = statistics.median(lows)
    band_high = statistics.median(highs)
    fair_value = sum(m * w for m, w in zip(mids, weights)) / sum(weights)

    return {
        "ok": True,
        "rows": [{"method": r.name, "low": round(r.low, 2), "high": round(r.high, 2)} for r in good],
        "band_low": round(band_low, 2),
        "band_high": round(band_high, 2),
        "fair_value": round(fair_value, 2),
        "abs_low": round(min(lows), 2),
        "abs_high": round(max(highs), 2),
    }


def band_verdict(price: float, band: Dict[str, object], buffer_pct: float = 0.03) -> Dict[str, object]:
    if not band.get("ok") or price <= 0:
        return {"verdict": "no_data", "upside_to_fair_pct": None}
    lo = float(band["band_low"])
    hi = float(band["band_high"])
    fv = float(band["fair_value"])
    if price < lo * (1.0 - buffer_pct):
        verdict = "below_band"        # long
    elif price > hi * (1.0 + buffer_pct):
        verdict = "above_band"        # short
    else:
        verdict = "inside_band"
    return {
        "verdict": verdict,
        "upside_to_fair_pct": round((fv / price - 1.0) * 100.0, 1),
        "band_low": lo, "band_high": hi, "fair_value": fv,
    }
