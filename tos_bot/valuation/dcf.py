"""Discounted Cash Flow (Pignataro Ch. 9).

Pipeline:
    base UFCF  ->  project N years  ->  discount to PV  ->  add PV(terminal value)
    ->  Enterprise Value  ->  - Net Debt  ->  Equity Value  ->  / shares  ->  price

UFCF  (p. 292):
    Net income + D&A + Deferred taxes + Other non-cash + WC changes
    - CapEx + After-tax net interest expense

Cost of equity - CAPM (p. 307):     Ke = rf + beta * MRP
Terminal value - two ways (pp. 308-309):
    multiple:   EV/EBITDA(exit) * EBITDA(final year)
    perpetuity: UFCF(final) * (1 + g) / (WACC - g)
Discounting - mid-year or end-of-year convention (p. 291).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..data.fundamentals import Financials
from .enterprise_value import enterprise_value
from .projections import project_series


# --------------------------------------------------------------------------- #
#  Cost of capital                                                           #
# --------------------------------------------------------------------------- #
def capm_cost_of_equity(risk_free: float, beta: float, market_risk_premium: float) -> float:
    return risk_free + beta * market_risk_premium


def wacc(
    equity_value: float, debt_value: float,
    cost_of_equity: float, cost_of_debt: float, tax_rate: float,
) -> float:
    v = equity_value + debt_value
    if v <= 0:
        return cost_of_equity
    we, wd = equity_value / v, debt_value / v
    return we * cost_of_equity + wd * cost_of_debt * (1.0 - tax_rate)


# --------------------------------------------------------------------------- #
#  Inputs / result                                                           #
# --------------------------------------------------------------------------- #
@dataclass
class DcfInputs:
    risk_free_rate: float = 0.042
    market_risk_premium: float = 0.055
    tax_rate: float = 0.21
    projection_years: int = 5
    ufcf_growth: Optional[float] = None      # override; else inferred from revenue CAGR
    perpetuity_growth: float = 0.025
    exit_multiple: Optional[float] = None    # else current EV/EBITDA (book's default)
    cost_of_debt: Optional[float] = None     # else interest_expense / total_debt
    midyear_convention: bool = True
    margin_of_safety: float = 0.25


@dataclass
class DcfResult:
    symbol: str
    ok: bool = False
    reason: str = ""

    wacc: float = math.nan
    cost_of_equity: float = math.nan
    cost_of_debt: float = math.nan

    base_ufcf: float = math.nan
    projected_ufcf: List[float] = field(default_factory=list)
    pv_explicit: float = math.nan
    tv_multiple: float = math.nan
    tv_perpetuity: float = math.nan
    pv_tv_multiple: float = math.nan
    pv_tv_perpetuity: float = math.nan

    ev_multiple: float = math.nan
    ev_perpetuity: float = math.nan
    price_multiple: float = math.nan
    price_perpetuity: float = math.nan
    price_blended: float = math.nan

    upside_blended_pct: float = math.nan
    verdict: str = "no_data"
    ufcf_method: str = ""                    # "nopat" | "net_income" | "reported_fcf"
    disagreement_ratio: float = math.nan     # price_multiple / price_perpetuity (>=1)
    methods_agree: bool = True                # do both TV methods lean the same way?
    assumptions: Dict[str, float] = field(default_factory=dict)
    sensitivity: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        d = self.__dict__.copy()
        for k, v in d.items():
            if isinstance(v, float) and math.isnan(v):
                d[k] = None
        return d


# --------------------------------------------------------------------------- #
#  Core                                                                      #
# --------------------------------------------------------------------------- #
def _base_ufcf(fin: Financials, tax_rate: float):
    """Unlevered free cash flow, returned as (value, method).

    Primary = the NOPAT method Pignataro's Amazon model actually uses
    (Ch. 9, Table 9.3):
        EBIT x (1 - tax) + D&A + deferred taxes + other non-cash + dNWC - CapEx
    Fallbacks, in order: the net-income form (Ch. 9 p. 292 "simplified"),
    then the provider's reported free cash flow.
    """
    def z(x):
        return 0.0 if (x is None or math.isnan(x)) else float(x)

    da = z(fin.last(fin.dep_amort))
    dtx = z(fin.last(fin.deferred_tax)) if fin.deferred_tax else 0.0
    sbc = z(fin.last(fin.sbc)) if fin.sbc else 0.0
    dwc = z(fin.last(fin.change_in_wc)) if fin.change_in_wc else 0.0
    capex_out = -abs(z(fin.last(fin.capex))) if fin.capex else 0.0

    ebit = fin.ttm_ebit if not math.isnan(fin.ttm_ebit) else fin.last(fin.ebit)
    if (ebit is None or math.isnan(ebit)) and not math.isnan(fin.ttm_ebitda) and da:
        ebit = fin.ttm_ebitda - da
    if ebit is not None and not math.isnan(ebit) and ebit != 0.0:
        return ebit * (1.0 - tax_rate) + da + dtx + sbc + dwc + capex_out, "nopat"

    ni = fin.last(fin.net_income)
    intr = abs(z(fin.last(fin.interest_expense))) if fin.interest_expense else 0.0
    if ni is not None and not math.isnan(ni) and ni != 0.0:
        return ni + da + dtx + sbc + dwc + capex_out + intr * (1.0 - tax_rate), "net_income"

    if not math.isnan(fin.ttm_fcf):
        return float(fin.ttm_fcf), "reported_fcf"
    return math.nan, "none"


def _discount_periods(n: int, midyear: bool) -> List[float]:
    return [(i - 0.5) if midyear else float(i) for i in range(1, n + 1)]


def dcf_fair_value(fin: Financials, inp: DcfInputs) -> DcfResult:
    r = DcfResult(symbol=fin.symbol)
    if not fin.has_min_data():
        r.reason = "insufficient fundamentals"
        return r

    shares = fin.shares_out
    price = fin.price
    mc = fin.market_cap if not math.isnan(fin.market_cap) else price * shares
    net_debt = fin.net_debt
    # WACC debt weight = ST debt + LT debt + capital/finance leases (Pignataro p.317)
    debt_value = max(0.0, (0.0 if math.isnan(fin.total_debt) else fin.total_debt)
                     + (0.0 if math.isnan(fin.capital_leases) else fin.capital_leases))

    # --- cost of capital ------------------------------------------------ #
    beta = fin.beta if not math.isnan(fin.beta) else 1.1
    tax = fin.tax_rate if not math.isnan(fin.tax_rate) else inp.tax_rate
    ke = capm_cost_of_equity(inp.risk_free_rate, beta, inp.market_risk_premium)
    if inp.cost_of_debt is not None:
        kd = inp.cost_of_debt
    else:
        intr = abs(fin.last(fin.interest_expense)) if fin.interest_expense else 0.0
        kd = (intr / debt_value) if debt_value > 0 and intr > 0 else inp.risk_free_rate + 0.015
        kd = min(max(kd, 0.02), 0.15)
    w = wacc(mc, debt_value, ke, kd, tax)
    w = min(max(w, 0.04), 0.20)
    r.wacc, r.cost_of_equity, r.cost_of_debt = w, ke, kd

    # --- project UFCF ------------------------------------------------- #
    base, r.ufcf_method = _base_ufcf(fin, tax)
    r.base_ufcf = base
    if math.isnan(base) or base == 0.0:
        r.reason = "no usable free cash flow"
        return r

    growth = inp.ufcf_growth
    if growth is None:
        g = fin.revenue_cagr(3)
        growth = 0.04 if math.isnan(g) else max(-0.05, min(0.25, g))
    if base < 0:
        # negative FCF firm: taper losses toward zero rather than compounding them
        proj = []
        cur = base
        for _ in range(inp.projection_years):
            cur = cur * 0.6
            proj.append(cur)
    else:
        proj = project_series([base], inp.projection_years, "yoy_growth", yoy_growth=growth)
    r.projected_ufcf = [round(x, 2) for x in proj]

    # --- discount explicit period --------------------------------- #
    periods = _discount_periods(inp.projection_years, inp.midyear_convention)
    pv_explicit = sum(cf / (1.0 + w) ** t for cf, t in zip(proj, periods))
    r.pv_explicit = pv_explicit

    # --- terminal value (both methods) --------------------------- #
    final_ufcf = proj[-1]
    # EBITDA in the final year: grow current EBITDA at the same rate
    cur_ebitda = fin.ttm_ebitda if not math.isnan(fin.ttm_ebitda) else fin.last(fin.ebitda)
    exit_mult = inp.exit_multiple
    if exit_mult is None:
        cur_ev = enterprise_value(mc, net_debt, fin.minority_interest,
                                  fin.preferred_equity, fin.capital_leases)
        exit_mult = (cur_ev / cur_ebitda) if (cur_ebitda and cur_ebitda > 0) else 10.0
        # book uses the current market multiple; cap it so a 60x growth name
        # doesn't produce a nonsense terminal value
        exit_mult = min(max(exit_mult, 4.0), 45.0)
    final_ebitda = (cur_ebitda or 0.0) * (1.0 + (growth if base > 0 else 0.0)) ** inp.projection_years

    g = min(inp.perpetuity_growth, w - 0.01)
    tv_perp = final_ufcf * (1.0 + g) / (w - g) if w > g else math.nan
    tv_mult = final_ebitda * exit_mult if final_ebitda > 0 else math.nan
    last_t = periods[-1] + (0.5 if inp.midyear_convention else 0.0)  # TV at year-end
    disc_last = (1.0 + w) ** last_t
    r.tv_multiple, r.tv_perpetuity = tv_mult, tv_perp
    r.pv_tv_multiple = tv_mult / disc_last if not math.isnan(tv_mult) else math.nan
    r.pv_tv_perpetuity = tv_perp / disc_last if not math.isnan(tv_perp) else math.nan

    # --- EV -> equity -> price ---------------------------------- #
    def _price(pv_tv: float) -> float:
        if math.isnan(pv_tv):
            return math.nan
        ev = pv_explicit + pv_tv
        equity = ev - net_debt - fin.minority_interest - fin.preferred_equity
        return equity / shares if shares > 0 else math.nan

    r.ev_multiple = pv_explicit + r.pv_tv_multiple
    r.ev_perpetuity = pv_explicit + r.pv_tv_perpetuity
    r.price_multiple = _price(r.pv_tv_multiple)
    r.price_perpetuity = _price(r.pv_tv_perpetuity)

    prices = [p for p in (r.price_multiple, r.price_perpetuity) if p and not math.isnan(p) and p > 0]
    if not prices:
        r.reason = "terminal value not computable"
        return r
    r.price_blended = sum(prices) / len(prices)
    r.upside_blended_pct = (r.price_blended / price - 1.0) * 100.0 if price else math.nan

    # --- do the two terminal-value methods agree? ------------------- #
    # (Pignataro Ch. 9/12: for growth names the multiple method over-states and
    #  the perpetuity method under-states - when they diverge a lot the DCF is
    #  ambiguous and should not drive a confident call.)
    if len(prices) == 2 and min(prices) > 0:
        r.disagreement_ratio = round(max(prices) / min(prices), 2)
        pm_dir = 1 if r.price_multiple >= price else -1
        pp_dir = 1 if r.price_perpetuity >= price else -1
        r.methods_agree = (pm_dir == pp_dir) and r.disagreement_ratio <= 2.5
    else:
        r.disagreement_ratio = 1.0
        r.methods_agree = True

    mos = inp.margin_of_safety
    if not r.methods_agree:
        r.verdict = "ambiguous"            # DCF inconclusive - strategy won't trade it
    elif r.price_blended >= price * (1.0 + mos):
        r.verdict = "undervalued"          # long
    elif r.price_blended <= price * (1.0 - mos):
        r.verdict = "overvalued"           # short
    else:
        r.verdict = "fairly_valued"

    # --- sensitivity (assumption swings, football-field style) ----- #
    def _px_perp(gg):
        tv = final_ufcf * (1.0 + gg) / (w - gg) if w > gg else math.nan
        return _price(tv / disc_last) if not math.isnan(tv) else math.nan

    def _px_mult(mm):
        tv = final_ebitda * mm if final_ebitda > 0 else math.nan
        return _price(tv / disc_last) if not math.isnan(tv) else math.nan

    def _r2(x):
        return round(x, 2) if (x is not None and not math.isnan(x)) else None

    r.sensitivity = {
        "perp_g_-1pct": _r2(_px_perp(max(0.0, g - 0.01))),
        "perp_g_+1pct": _r2(_px_perp(min(w - 0.005, g + 0.01))),
        "exit_mult_-20%": _r2(_px_mult(exit_mult * 0.8)),
        "exit_mult_+20%": _r2(_px_mult(exit_mult * 1.2)),
    }

    r.ok = True
    r.assumptions = {
        "beta": round(beta, 2), "tax_rate": round(tax, 3),
        "cost_of_equity": round(ke, 4), "cost_of_debt": round(kd, 4),
        "wacc": round(w, 4), "ufcf_growth": round(growth, 4),
        "ufcf_method": r.ufcf_method,
        "perpetuity_growth": round(g, 4), "exit_multiple": round(exit_mult, 2),
        "projection_years": inp.projection_years,
        "midyear_convention": inp.midyear_convention,
    }
    return r
