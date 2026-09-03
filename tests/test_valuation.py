"""Valuation engine - checks the Pignataro math behaves as the book describes."""

from __future__ import annotations

import math

import pytest

from tos_bot.data.fundamentals import Financials
from tos_bot.valuation import (
    DcfInputs,
    MethodRange,
    band_verdict,
    capm_cost_of_equity,
    compute_multiples,
    dcf_fair_value,
    enterprise_value,
    football_field,
    peer_median_multiples,
    project_series,
    relative_value_signal,
    wacc,
)


def _fin(**kw) -> Financials:
    base = dict(
        symbol="X", price=50.0, shares_out=100e6, market_cap=5e9, beta=1.1,
        sector="Technology", total_debt=1e9, cash_and_st_investments=0.4e9,
        revenue=[3e9, 3.4e9, 3.9e9, 4.4e9], ebitda=[0.7e9, 0.8e9, 0.95e9, 1.1e9],
        ebit=[0.5e9, 0.58e9, 0.7e9, 0.82e9], net_income=[0.34e9, 0.4e9, 0.5e9, 0.6e9],
        dep_amort=[0.2e9] * 4, interest_expense=[-0.05e9] * 4,
        capex=[-0.25e9] * 4, change_in_wc=[-0.02e9] * 4, sbc=[0.05e9] * 4,
        deferred_tax=[0.01e9] * 4, ttm_ebitda=1.15e9, ttm_ebit=0.85e9,
        ttm_revenue=4.6e9, ttm_net_income=0.62e9, ttm_eps=6.2, tax_rate=0.21,
    )
    base.update(kw)
    return Financials(**base)


def test_enterprise_value_bridge():
    # EV = mkt cap + net debt (+ MI + pref)
    ev = enterprise_value(market_cap=5e9, net_debt=0.6e9, minority_interest=0.1e9)
    assert ev == pytest.approx(5.7e9)


def test_capm_and_wacc_monotonic():
    ke_low = capm_cost_of_equity(0.04, 0.8, 0.05)
    ke_high = capm_cost_of_equity(0.04, 1.6, 0.05)
    assert ke_high > ke_low > 0.04
    w = wacc(equity_value=8e9, debt_value=2e9, cost_of_equity=0.10, cost_of_debt=0.05, tax_rate=0.21)
    assert 0.05 < w < 0.10          # blended sits between the two, debt is cheaper


def test_projection_methods_lengths():
    hist = [100, 110, 90, 120]
    for m in ("conservative", "aggressive", "average", "last_year", "repeat_cycle", "yoy_growth"):
        out = project_series(hist, 5, m, yoy_growth=0.1)
        assert len(out) == 5
    assert project_series(hist, 3, "conservative")[0] == 120
    assert project_series(hist, 3, "aggressive")[0] == 90


def test_comps_signal_flags_cheap_and_rich():
    target = compute_multiples(_fin())
    cheap_peer = compute_multiples(_fin(symbol="P", price=100.0))
    # build a peer set that is 2x more expensive than target
    rich = []
    for i in range(6):
        m = compute_multiples(_fin(symbol=f"P{i}"))
        m.ev_ebitda = target.ev_ebitda * 2.0
        m.pe = (target.pe or 10) * 2.0
        rich.append(m)
    med = peer_median_multiples(rich)
    sig = relative_value_signal(target, med)
    assert sig["verdict"] == "undervalued_vs_peers"
    assert sig["mean_gap_pct"] < 0


def test_dcf_multiple_vs_perpetuity_and_verdict():
    fin = _fin(price=20.0)                 # deliberately cheap -> undervalued
    res = dcf_fair_value(fin, DcfInputs(projection_years=5, perpetuity_growth=0.025))
    assert res.ok
    assert res.price_multiple > 0 and res.price_perpetuity > 0
    assert res.verdict in ("undervalued", "fairly_valued", "overvalued")
    # cheap name should read undervalued
    assert res.verdict == "undervalued"
    # WACC in a sane band
    assert 0.04 <= res.wacc <= 0.20


def test_dcf_rich_name_overvalued():
    fin = _fin(price=500.0)
    res = dcf_fair_value(fin, DcfInputs())
    assert res.ok and res.verdict == "overvalued"


def test_football_field_band_and_verdict():
    ranges = [
        MethodRange("52wk", 40, 70),
        MethodRange("Comps", 55, 95),
        MethodRange("DCF EBITDA", 60, 90),
        MethodRange("DCF Perp", 45, 65),
    ]
    band = football_field(ranges)
    assert band["ok"]
    assert band["band_low"] < band["fair_value"] < band["band_high"]
    assert band_verdict(30.0, band)["verdict"] == "below_band"
    assert band_verdict(200.0, band)["verdict"] == "above_band"
    assert band_verdict(band["fair_value"], band)["verdict"] == "inside_band"
