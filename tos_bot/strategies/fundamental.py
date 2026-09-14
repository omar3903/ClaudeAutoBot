"""Valuation-driven swing setups, built on the Pignataro engine in
:mod:`tos_bot.valuation`.

Each one turns a valuation gap into a directional trade:
    trades cheaper than its peers / its DCF / its blended band  -> long
    trades richer                                               -> short
The price target is the valuation itself; the stop is technical (ATR).
"""

from __future__ import annotations

import math
import statistics
from typing import List

from ..core.enums import Side, StrategyKind, Timeframe
from ..core.models import Play
from ..valuation import (
    DcfInputs,
    MethodRange,
    band_verdict,
    compute_multiples,
    dcf_fair_value,
    football_field,
    implied_price_from_multiple,
    peer_median_multiples,
    relative_value_signal,
)
from .base import Strategy, StrategyContext
from .registry import register


def _clamp_target(price: float, target: float, side: Side, max_move: float = 0.20) -> float:
    """Keep a valuation target realistic for the hold window. A comps/DCF
    re-rate rarely completes more than ~20% over 15-35 trading days, and an
    implied fair value further out than that is usually a data problem (a bad
    peer set or a distorted EBITDA), not a 40%-in-a-month opportunity - the
    trade that lost on TTWO had a first target 39% away. The full unclamped
    fair value still rides along in the evidence."""
    if side is Side.LONG:
        return min(target, price * (1.0 + max_move))
    return max(target, price * (1.0 - max_move))


# --------------------------------------------------------------------------- #
@register
class RelativeValueComps(Strategy):
    key = "relative_value_comps"
    kind = StrategyKind.FUNDAMENTAL
    timeframe = Timeframe.SWING
    expected_hold = (15.0, 35.0)    # trading days
    title = "Relative Value vs. Peers (Comps)"
    thesis = (
        "Comparable Company Analysis (Pignataro Ch. 8/10): value a company by "
        "how its EV/EBITDA, P/E and EV/Sales stack up against similar peers. "
        "Consistently cheaper than the peer set implies undervalued (long); "
        "consistently richer implies overvalued (short)."
    )
    default_params = {"peer_count": 8, "discount_trigger": 0.20, "premium_trigger": 0.20,
                      "stop_atr": 2.0}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        fin = ctx.fundamentals
        if fin is None or not fin.has_min_data() or not ctx.peers:
            return []
        atr = ctx.daily_atr
        if math.isnan(atr) or atr <= 0:
            return []

        target_m = compute_multiples(fin)
        peer_ms = [compute_multiples(p) for p in ctx.peers if p and p.has_min_data()]
        if len(peer_ms) < 3:
            return []
        peer_med = peer_median_multiples(peer_ms)
        sig = relative_value_signal(
            target_m, peer_med,
            self.params["discount_trigger"], self.params["premium_trigger"],
        )
        if sig["verdict"] not in ("undervalued_vs_peers", "overvalued_vs_peers"):
            return []

        price = ctx.price
        implied = implied_price_from_multiple(fin, "ev_ebitda", peer_med.ev_ebitda)
        implied_pe = implied_price_from_multiple(fin, "pe", peer_med.pe)
        cands = [x for x in (implied, implied_pe) if x and not math.isnan(x) and x > 0]
        fair = statistics.fmean(cands) if cands else math.nan
        if math.isnan(fair):
            return []

        if sig["verdict"] == "undervalued_vs_peers" and fair > price:
            side = Side.LONG
            stop = price - self.params["stop_atr"] * atr
            t1 = _clamp_target(price, (price + fair) / 2.0, side, 0.08)   # a realistic partial re-rate
            targets = [t1, _clamp_target(price, fair, side)]
        elif sig["verdict"] == "overvalued_vs_peers" and fair < price:
            side = Side.SHORT
            stop = price + self.params["stop_atr"] * atr
            t1 = _clamp_target(price, (price + fair) / 2.0, side, 0.08)
            targets = [t1, _clamp_target(price, fair, side)]
        else:
            return []

        rows = sig["rows"]
        tbl = "; ".join(f"{r['multiple']} {r['target']} vs peers {r['peer_median']} "
                        f"({r['gap_pct']:+.0f}%)" for r in rows)
        detail = (
            f"{ctx.symbol} trades at {tbl}. Mean gap to the peer set is "
            f"{sig['mean_gap_pct']:+.0f}%. Re-rating to the peer median EV/EBITDA "
            f"({peer_med.ev_ebitda:.1f}x) implies ~{fair:.2f}/share."
        )
        conf = min(0.8, 0.45 + abs(sig["mean_gap_pct"]) / 200.0)
        p = self._mk_play(
            ctx, side, price, stop, targets, conf,
            rationale=f"{sig['verdict'].replace('_', ' ')}, mean gap {sig['mean_gap_pct']:+.0f}%",
            detail=detail,
            evidence={"signal": sig, "peer_median": peer_med.as_dict(),
                      "target_multiples": target_m.as_dict(),
                      "implied_fair_value": round(fair, 2),
                      "peers": [p.symbol for p in ctx.peers]},
            tags=["swing", "valuation", "comps", "fundamental"],
            ttl_minutes=8 * 60,
        )
        return [p] if p else []


# --------------------------------------------------------------------------- #
@register
class DcfFairValueGap(Strategy):
    key = "dcf_fair_value_gap"
    kind = StrategyKind.FUNDAMENTAL
    timeframe = Timeframe.SWING
    expected_hold = (20.0, 45.0)    # trading days
    title = "DCF Fair-Value Gap"
    thesis = (
        "Discounted Cash Flow (Pignataro Ch. 9): project unlevered free cash "
        "flow, discount at WACC (CAPM cost of equity), add a terminal value "
        "by both the exit-multiple and perpetuity methods, and compare the "
        "blended per-share value to the market price. A gap beyond the margin "
        "of safety is the trade."
    )
    default_params = {"projection_years": 5, "perpetuity_growth": 0.025, "mos": 0.25,
                      "stop_atr": 2.0}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        fin = ctx.fundamentals
        if fin is None or not fin.has_min_data():
            return []
        atr = ctx.daily_atr
        if math.isnan(atr) or atr <= 0:
            return []

        v = ctx.params.get("valuation", {})
        inp = DcfInputs(
            risk_free_rate=v.get("risk_free_rate", 0.042),
            market_risk_premium=v.get("market_risk_premium", 0.055),
            tax_rate=v.get("tax_rate", 0.21),
            projection_years=int(self.params["projection_years"]),
            perpetuity_growth=float(self.params["perpetuity_growth"]),
            midyear_convention=v.get("midyear_convention", True),
            margin_of_safety=float(self.params["mos"]),
        )
        res = dcf_fair_value(fin, inp)
        if not res.ok or res.verdict in ("fairly_valued", "ambiguous"):
            return []      # "ambiguous" = the two terminal-value methods disagree
        price = ctx.price
        fair = res.price_blended

        if res.verdict == "undervalued" and fair > price:
            side = Side.LONG
            stop = price - self.params["stop_atr"] * atr
            targets = [_clamp_target(price, (price + fair) / 2.0, side),
                       _clamp_target(price, fair, side)]
        elif res.verdict == "overvalued" and fair < price:
            side = Side.SHORT
            stop = price + self.params["stop_atr"] * atr
            targets = [_clamp_target(price, (price + fair) / 2.0, side),
                       _clamp_target(price, fair, side)]
        else:
            return []

        a = res.assumptions
        detail = (
            f"5-yr DCF at WACC {a['wacc']*100:.1f}% (Ke {a['cost_of_equity']*100:.1f}%, "
            f"beta {a['beta']}). Terminal value - exit multiple {a['exit_multiple']}x "
            f"=> {res.price_multiple:.2f}/sh; perpetuity g={a['perpetuity_growth']*100:.1f}% "
            f"=> {res.price_perpetuity:.2f}/sh. Blended {fair:.2f} vs price {price:.2f} "
            f"({res.upside_blended_pct:+.0f}%)."
        )
        conf = min(0.82, 0.45 + abs(res.upside_blended_pct) / 300.0)
        p = self._mk_play(
            ctx, side, price, stop, targets, conf,
            rationale=f"DCF {res.verdict}, {res.upside_blended_pct:+.0f}% to blended FV",
            detail=detail,
            evidence={"dcf": res.as_dict()},
            tags=["swing", "valuation", "dcf", "fundamental"],
            ttl_minutes=8 * 60,
        )
        return [p] if p else []


# --------------------------------------------------------------------------- #
@register
class ValuationFootballField(Strategy):
    key = "valuation_football_field"
    kind = StrategyKind.FUNDAMENTAL
    timeframe = Timeframe.SWING
    expected_hold = (18.0, 40.0)    # trading days
    title = "Football-Field Fair-Value Band"
    thesis = (
        "Pignataro's Conclusion (Ch. 12): overlay every method - 52-week "
        "high/low, comps, DCF (exit-multiple) and DCF (perpetuity) - into one "
        "low/high band. Price below the band is a long; above it, a short; the "
        "band edges and the weighted fair value become the targets."
    )
    default_params = {"stop_atr": 2.5, "buffer_pct": 0.03}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        fin = ctx.fundamentals
        if fin is None or not fin.has_min_data() or not ctx.enough_daily(200):
            return []
        atr = ctx.daily_atr
        if math.isnan(atr) or atr <= 0:
            return []
        price = ctx.price
        d = ctx.daily.tail(252)
        lo_52, hi_52 = float(d["low"].min()), float(d["high"].max())

        ranges: List[MethodRange] = [MethodRange("52-Week High/Low", lo_52, hi_52, weight=0.8)]

        # comps range from peer multiple spread
        if ctx.peers:
            peer_ms = [compute_multiples(p) for p in ctx.peers if p and p.has_min_data()]
            evs = sorted(m.ev_ebitda for m in peer_ms if not math.isnan(m.ev_ebitda) and m.ev_ebitda > 0)
            if len(evs) >= 3:
                p25 = evs[max(0, int(0.25 * (len(evs) - 1)))]
                p75 = evs[min(len(evs) - 1, int(0.75 * (len(evs) - 1)))]
                lo = implied_price_from_multiple(fin, "ev_ebitda", p25)
                hi = implied_price_from_multiple(fin, "ev_ebitda", p75)
                if lo and hi and not math.isnan(lo) and not math.isnan(hi):
                    ranges.append(MethodRange("Comparable Companies", min(lo, hi), max(lo, hi), 1.2))

        v = ctx.params.get("valuation", {})
        res = dcf_fair_value(fin, DcfInputs(
            risk_free_rate=v.get("risk_free_rate", 0.042),
            market_risk_premium=v.get("market_risk_premium", 0.055),
            projection_years=int(v.get("projection_years", 5)),
            perpetuity_growth=float(v.get("perpetuity_growth", 0.025)),
            midyear_convention=v.get("midyear_convention", True),
        ))
        if res.ok:
            if res.price_multiple and not math.isnan(res.price_multiple):
                ranges.append(MethodRange("DCF - Exit Multiple",
                                          res.price_multiple * 0.85, res.price_multiple * 1.15, 1.0))
            if res.price_perpetuity and not math.isnan(res.price_perpetuity):
                ranges.append(MethodRange("DCF - Perpetuity",
                                          res.price_perpetuity * 0.85, res.price_perpetuity * 1.15, 1.0))

        band = football_field(ranges)
        if not band.get("ok") or len(band["rows"]) < 2:
            return []
        vd = band_verdict(price, band, self.params["buffer_pct"])
        if vd["verdict"] not in ("below_band", "above_band"):
            return []

        if vd["verdict"] == "below_band":
            side = Side.LONG
            stop = price - self.params["stop_atr"] * atr
            targets = [_clamp_target(price, band["band_low"], side),
                       _clamp_target(price, band["fair_value"], side)]
        else:
            side = Side.SHORT
            stop = price + self.params["stop_atr"] * atr
            targets = [_clamp_target(price, band["band_high"], side),
                       _clamp_target(price, band["fair_value"], side)]

        methods = ", ".join(r["method"] for r in band["rows"])
        detail = (
            f"Blended band across {len(band['rows'])} methods ({methods}): "
            f"{band['band_low']:.2f} - {band['band_high']:.2f}, weighted fair value "
            f"{band['fair_value']:.2f}. Price {price:.2f} is {vd['verdict'].replace('_', ' ')} "
            f"({vd['upside_to_fair_pct']:+.0f}% to fair value)."
        )
        conf = min(0.8, 0.45 + abs(vd["upside_to_fair_pct"]) / 250.0)
        p = self._mk_play(
            ctx, side, price, stop, targets, conf,
            rationale=f"price {vd['verdict'].replace('_', ' ')}, FV {band['fair_value']:.2f}",
            detail=detail,
            evidence={"football_field": band, "verdict": vd},
            tags=["swing", "valuation", "football-field", "fundamental"],
            ttl_minutes=8 * 60,
        )
        return [p] if p else []
