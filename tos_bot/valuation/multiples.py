"""Trading multiples and Comparable Company Analysis (Pignataro Ch. 8 & 10).

Market-value multiples use market cap / price:  P/E.
Enterprise-value multiples use EV:  EV/EBITDA, EV/EBIT, EV/Sales.

Comps logic (Ch. 8, p. 287):
    "If the peers' multiples are consistently higher than the multiples of the
     company we are valuing, it could mean that our company is undervalued.
     Conversely ... overvalued."
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

from ..data.fundamentals import Financials
from .enterprise_value import enterprise_value


def _pos(x: float) -> Optional[float]:
    return x if (x is not None and not math.isnan(x) and x > 0) else None


@dataclass
class Multiples:
    symbol: str = ""
    pe: float = math.nan
    ev_ebitda: float = math.nan
    ev_ebit: float = math.nan
    ev_sales: float = math.nan

    def as_dict(self) -> Dict[str, float]:
        return {k: (None if isinstance(v, float) and math.isnan(v) else v)
                for k, v in asdict(self).items() if k != "symbol"}


def _metric(fin: Financials, ttm_attr: str, hist_attr: str) -> float:
    v = getattr(fin, ttm_attr, math.nan)
    if v is None or math.isnan(v):
        hist = getattr(fin, hist_attr, [])
        v = fin.last(hist) if hist else math.nan
    return v


def compute_multiples(fin: Financials) -> Multiples:
    mc = fin.market_cap
    if (mc is None or math.isnan(mc)) and _pos(fin.price) and _pos(fin.shares_out):
        mc = fin.price * fin.shares_out
    ev = enterprise_value(
        mc or math.nan, fin.net_debt, fin.minority_interest,
        fin.preferred_equity, fin.capital_leases,
    )
    ebitda = _metric(fin, "ttm_ebitda", "ebitda")
    ebit = _metric(fin, "ttm_ebit", "ebit")
    sales = _metric(fin, "ttm_revenue", "revenue")
    eps = fin.ttm_eps
    ni = _metric(fin, "ttm_net_income", "net_income")

    m = Multiples(symbol=fin.symbol)
    if _pos(mc):
        if _pos(eps) and _pos(fin.price):
            m.pe = fin.price / eps
        elif _pos(ni):
            m.pe = mc / ni
    if _pos(ev):
        if _pos(ebitda):
            m.ev_ebitda = ev / ebitda
        if _pos(ebit):
            m.ev_ebit = ev / ebit
        if _pos(sales):
            m.ev_sales = ev / sales
    return m


def peer_median_multiples(peers: List[Multiples]) -> Multiples:
    out = Multiples(symbol="PEER_MEDIAN")
    for fld in ("pe", "ev_ebitda", "ev_ebit", "ev_sales"):
        vals = [getattr(p, fld) for p in peers
                if not math.isnan(getattr(p, fld)) and getattr(p, fld) > 0]
        # drop extreme outliers (Pignataro keeps judgement, we trim mildly)
        if len(vals) >= 4:
            vals.sort()
            vals = vals[1:-1]
        if vals:
            setattr(out, fld, statistics.median(vals))
    return out


def implied_price_from_multiple(fin: Financials, metric: str, target_multiple: float) -> float:
    """`metric` in {pe, ev_ebitda, ev_ebit, ev_sales}."""
    if target_multiple is None or math.isnan(target_multiple) or target_multiple <= 0:
        return math.nan
    shares = fin.shares_out
    if not _pos(shares):
        return math.nan

    if metric == "pe":
        eps = fin.ttm_eps if _pos(fin.ttm_eps) else (
            fin.last(fin.net_income) / shares if fin.net_income else math.nan)
        return eps * target_multiple if _pos(eps) else math.nan

    base = {
        "ev_ebitda": _metric(fin, "ttm_ebitda", "ebitda"),
        "ev_ebit": _metric(fin, "ttm_ebit", "ebit"),
        "ev_sales": _metric(fin, "ttm_revenue", "revenue"),
    }.get(metric, math.nan)
    if not _pos(base):
        return math.nan
    implied_ev = target_multiple * base
    equity_value = implied_ev - fin.net_debt - fin.minority_interest - fin.preferred_equity
    return equity_value / shares


def relative_value_signal(
    target: Multiples, peer_median: Multiples, discount_trigger: float = 0.20,
    premium_trigger: float = 0.20,
) -> Dict[str, object]:
    """For each shared multiple, how far is the target below/above the peer
    median? Negative gap => target is 'cheaper' => long bias (Ch. 8, p. 287)."""
    rows = []
    gaps = []
    for fld, label in (("ev_ebitda", "EV/EBITDA"), ("pe", "P/E"),
                       ("ev_sales", "EV/Sales"), ("ev_ebit", "EV/EBIT")):
        tv, pv = getattr(target, fld), getattr(peer_median, fld)
        if math.isnan(tv) or math.isnan(pv) or pv <= 0 or tv <= 0:
            continue
        gap = tv / pv - 1.0            # +0.30 => target 30% richer than peers
        gaps.append(gap)
        rows.append({"multiple": label, "target": round(tv, 2),
                     "peer_median": round(pv, 2), "gap_pct": round(gap * 100, 1)})
    if not gaps:
        return {"verdict": "no_data", "rows": rows, "mean_gap_pct": None}

    mean_gap = statistics.fmean(gaps)
    if mean_gap <= -discount_trigger:
        verdict = "undervalued_vs_peers"      # long
    elif mean_gap >= premium_trigger:
        verdict = "overvalued_vs_peers"       # short
    else:
        verdict = "in_line"
    return {"verdict": verdict, "rows": rows, "mean_gap_pct": round(mean_gap * 100, 1)}
