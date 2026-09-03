"""Technical day-trade / swing setups.

These come from standard market-structure practice (opening range, VWAP,
moving-average pullbacks, Connors RSI-2, Bollinger mean reversion, Keltner
breakout, gap-and-go, 52-week momentum). The 52-week setup ties back to
Pignataro Ch. 12, which uses the 52-week high/low as a valuation anchor.
"""

from __future__ import annotations

import math
from typing import List

import numpy as np
import pandas as pd

from ..core.enums import Side, StrategyKind, Timeframe
from ..core.models import Play
from ..indicators import ta
from .base import Strategy, StrategyContext, safe_last, swing_high, swing_low
from .registry import register


# --------------------------------------------------------------------------- #
@register
class OpeningRangeBreakout(Strategy):
    key = "opening_range_breakout"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.INTRADAY
    title = "Opening-Range Breakout"
    thesis = (
        "The high and low of the first few minutes frame the day's early "
        "battle. A decisive move beyond that range, backed by heavier volume "
        "and the right side of VWAP, tends to continue in that direction."
    )
    default_params = {"or_minutes": 15, "rvol_min": 1.3, "target_mult": (1.0, 2.0)}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        if not ctx.enough_intraday(15):
            return []
        today = ctx.today_intraday()
        if today is None or len(today) < 4:
            return []
        or_m = int(self.params["or_minutes"])
        if ctx.minutes_since_open < or_m + 5 or ctx.minutes_since_open > 150:
            return []

        orr = ta.opening_range(ctx.intraday, or_m)
        or_high = safe_last(orr["or_high"])
        or_low = safe_last(orr["or_low"])
        if math.isnan(or_high) or math.isnan(or_low) or or_high <= or_low:
            return []
        vwap = safe_last(ta.session_vwap(ctx.intraday))
        atr = safe_last(ta.atr(ctx.intraday, 14))
        rvol = ta.rel_volume_intraday(ctx.intraday)
        price = ctx.price
        or_range = or_high - or_low
        tp1, tp2 = self.params["target_mult"]

        if price > or_high and price > vwap and rvol >= self.params["rvol_min"]:
            entry = or_high
            stop = min(or_low, entry - 1.0 * atr) if atr else or_low
            targets = [entry + tp1 * or_range, entry + tp2 * or_range]
            conf = min(0.85, 0.45 + 0.15 * (rvol - 1) + 0.1 * (price > vwap))
            detail = (
                f"Price {price:.2f} cleared the {or_m}-min opening range high "
                f"{or_high:.2f} while holding above VWAP {vwap:.2f}, on {rvol:.1f}x "
                f"typical volume for this time of day."
            )
            return self._one(ctx, Side.LONG, entry, stop, targets, conf, detail,
                             or_high, or_low, vwap, rvol, atr)

        if price < or_low and price < vwap and rvol >= self.params["rvol_min"]:
            entry = or_low
            stop = max(or_high, entry + 1.0 * atr) if atr else or_high
            targets = [entry - tp1 * or_range, entry - tp2 * or_range]
            conf = min(0.85, 0.45 + 0.15 * (rvol - 1) + 0.1 * (price < vwap))
            detail = (
                f"Price {price:.2f} broke the {or_m}-min opening range low "
                f"{or_low:.2f} while below VWAP {vwap:.2f}, on {rvol:.1f}x "
                f"typical volume."
            )
            return self._one(ctx, Side.SHORT, entry, stop, targets, conf, detail,
                             or_high, or_low, vwap, rvol, atr)
        return []

    def _one(self, ctx, side, entry, stop, targets, conf, detail,
             or_high, or_low, vwap, rvol, atr):
        p = self._mk_play(
            ctx, side, entry, stop, targets, conf,
            rationale=f"{or_low:.2f}-{or_high:.2f} OR break, {rvol:.1f}x vol",
            detail=detail,
            evidence={"or_high": round(or_high, 2), "or_low": round(or_low, 2),
                      "vwap": round(vwap, 2), "rvol": round(rvol, 2),
                      "atr": round(atr, 3)},
            tags=["intraday", "breakout", "momentum"],
        )
        return [p] if p else []


# --------------------------------------------------------------------------- #
@register
class VwapReclaim(Strategy):
    key = "vwap_reclaim"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.INTRADAY
    title = "VWAP Reclaim / Loss"
    thesis = (
        "VWAP is where the average buyer today sits. When price spends time "
        "below it and then reclaims it on strength, trapped sellers become "
        "buyers - and vice-versa when a strong stock loses VWAP."
    )
    default_params = {"min_bars_below": 3}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        if not ctx.enough_intraday(20):
            return []
        today = ctx.today_intraday()
        if today is None or len(today) < 8:
            return []
        vwap_s = ta.session_vwap(ctx.intraday).reindex(today.index)
        close = today["close"]
        atr = safe_last(ta.atr(ctx.intraday, 14))
        if math.isnan(atr) or atr <= 0:
            return []
        above = (close > vwap_s)
        prev_below = (~above.iloc[-4:-1]).sum()
        price = ctx.price
        vwap = safe_last(vwap_s)

        if above.iloc[-1] and prev_below >= self.params["min_bars_below"]:
            entry = price
            stop = min(swing_low(today["low"], 6), vwap - 0.5 * atr)
            hod = float(today["high"].max())
            targets = [max(hod, entry + 1.5 * (entry - stop)), entry + 2.5 * (entry - stop)]
            conf = 0.55 + 0.05 * prev_below
            detail = (
                f"After {int(prev_below)} bars under VWAP, price reclaimed it "
                f"({price:.2f} vs VWAP {vwap:.2f}). Prior session structure holds; "
                f"first objective is the day's high {hod:.2f}."
            )
            return self._wrap(ctx, Side.LONG, entry, stop, targets, conf, detail, vwap, atr, prev_below)

        if (not above.iloc[-1]) and (above.iloc[-4:-1]).sum() >= self.params["min_bars_below"]:
            entry = price
            stop = max(swing_high(today["high"], 6), vwap + 0.5 * atr)
            lod = float(today["low"].min())
            targets = [min(lod, entry - 1.5 * (stop - entry)), entry - 2.5 * (stop - entry)]
            conf = 0.55
            detail = (
                f"Price lost VWAP after holding above it ({price:.2f} vs VWAP "
                f"{vwap:.2f}); momentum has flipped. First objective is the day's "
                f"low {lod:.2f}."
            )
            return self._wrap(ctx, Side.SHORT, entry, stop, targets, conf, detail, vwap, atr, 0)
        return []

    def _wrap(self, ctx, side, entry, stop, targets, conf, detail, vwap, atr, nb):
        p = self._mk_play(
            ctx, side, entry, stop, targets, conf,
            rationale=f"{'reclaimed' if side is Side.LONG else 'lost'} VWAP {vwap:.2f}",
            detail=detail,
            evidence={"vwap": round(vwap, 2), "atr": round(atr, 3), "bars_below": int(nb)},
            tags=["intraday", "vwap", "reversal"],
        )
        return [p] if p else []


# --------------------------------------------------------------------------- #
@register
class EmaPullbackTrend(Strategy):
    key = "ema_pullback_trend"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.INTRADAY
    title = "Trend Pullback to Moving Average"
    thesis = (
        "In an established intraday trend (fast EMA over slow EMA over the "
        "50-EMA), the first pullback into the slow EMA that holds is a lower-"
        "risk continuation entry in the trend's direction."
    )
    default_params = {"fast": 9, "slow": 20, "trend": 50}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        if not ctx.enough_intraday(60):
            return []
        c = ctx.intraday["close"]
        f = ta.ema(c, self.params["fast"])
        s = ta.ema(c, self.params["slow"])
        t = ta.ema(c, self.params["trend"])
        atr = safe_last(ta.atr(ctx.intraday, 14))
        if any(math.isnan(safe_last(x)) for x in (f, s, t)) or math.isnan(atr) or atr <= 0:
            return []
        price = ctx.price
        last = ctx.intraday.iloc[-1]
        near_slow = abs(last["low"] - safe_last(s)) <= 0.4 * atr or abs(price - safe_last(s)) <= 0.3 * atr

        up = safe_last(f) > safe_last(s) > safe_last(t) and price > safe_last(t)
        dn = safe_last(f) < safe_last(s) < safe_last(t) and price < safe_last(t)

        if up and near_slow and last["close"] >= last["open"]:
            entry = price
            stop = min(float(last["low"]), safe_last(s) - 1.0 * atr)
            rr = entry - stop
            targets = [swing_high(ctx.intraday["high"], 12), entry + 2.0 * rr]
            detail = (
                f"Fast/slow/trend EMAs stacked bullishly ({safe_last(f):.2f} > "
                f"{safe_last(s):.2f} > {safe_last(t):.2f}); price pulled back into "
                f"the {self.params['slow']}-EMA and printed an up bar."
            )
            return self._wrap(ctx, Side.LONG, entry, stop, targets, 0.6, detail, f, s, t, atr)

        if dn and near_slow and last["close"] <= last["open"]:
            entry = price
            stop = max(float(last["high"]), safe_last(s) + 1.0 * atr)
            rr = stop - entry
            targets = [swing_low(ctx.intraday["low"], 12), entry - 2.0 * rr]
            detail = (
                f"EMAs stacked bearishly ({safe_last(f):.2f} < {safe_last(s):.2f} "
                f"< {safe_last(t):.2f}); price rallied into the "
                f"{self.params['slow']}-EMA and rolled over."
            )
            return self._wrap(ctx, Side.SHORT, entry, stop, targets, 0.6, detail, f, s, t, atr)
        return []

    def _wrap(self, ctx, side, entry, stop, targets, conf, detail, f, s, t, atr):
        p = self._mk_play(
            ctx, side, entry, stop, targets, conf,
            rationale=f"pullback to {self.params['slow']}-EMA in trend",
            detail=detail,
            evidence={"ema_fast": round(safe_last(f), 2), "ema_slow": round(safe_last(s), 2),
                      "ema_trend": round(safe_last(t), 2), "atr": round(atr, 3)},
            tags=["intraday", "trend", "pullback"],
        )
        return [p] if p else []


# --------------------------------------------------------------------------- #
@register
class Rsi2MeanReversion(Strategy):
    key = "rsi2_mean_reversion"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.SWING
    title = "RSI(2) Mean Reversion"
    thesis = (
        "Larry Connors' setup: in a security above its 200-day average, a "
        "2-period RSI below 10 is a short-term oversold bounce (long). Below "
        "the 200-day with RSI(2) above 90 is short-term overbought (short)."
    )
    default_params = {"rsi_len": 2, "buy_below": 10, "sell_above": 90, "trend_sma": 200,
                      "stop_atr": 2.5}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        if not ctx.enough_daily(self.params["trend_sma"] + 5):
            return []
        d = ctx.daily
        c = d["close"]
        sma = ta.sma(c, self.params["trend_sma"])
        rsi = ta.rsi(c, self.params["rsi_len"])
        atr = safe_last(ta.atr(d, 14))
        sma5 = ta.sma(c, 5)
        price = ctx.price
        if math.isnan(safe_last(sma)) or math.isnan(atr) or atr <= 0:
            return []
        r = safe_last(rsi)

        if price > safe_last(sma) and r < self.params["buy_below"]:
            entry = price
            stop = entry - self.params["stop_atr"] * atr
            targets = [safe_last(sma5) if safe_last(sma5) > entry else entry + 1.0 * atr,
                       entry + 2.0 * atr]
            conf = 0.5 + (self.params["buy_below"] - r) / 100.0
            detail = (
                f"{ctx.symbol} sits above its 200-day SMA ({safe_last(sma):.2f}) but "
                f"RSI(2) has dropped to {r:.1f} - a short-term washout. Reversion "
                f"target is the 5-day SMA {safe_last(sma5):.2f}."
            )
            return self._wrap(ctx, Side.LONG, entry, stop, targets, conf, detail, sma, r, atr)

        if price < safe_last(sma) and r > self.params["sell_above"]:
            entry = price
            stop = entry + self.params["stop_atr"] * atr
            targets = [safe_last(sma5) if safe_last(sma5) < entry else entry - 1.0 * atr,
                       entry - 2.0 * atr]
            conf = 0.5 + (r - self.params["sell_above"]) / 100.0
            detail = (
                f"{ctx.symbol} is below its 200-day SMA ({safe_last(sma):.2f}) and "
                f"RSI(2) has spiked to {r:.1f} - an overbought bounce into "
                f"resistance to fade."
            )
            return self._wrap(ctx, Side.SHORT, entry, stop, targets, conf, detail, sma, r, atr)
        return []

    def _wrap(self, ctx, side, entry, stop, targets, conf, detail, sma, r, atr):
        p = self._mk_play(
            ctx, side, entry, stop, targets, conf,
            rationale=f"RSI(2)={r:.0f}, {'above' if side is Side.LONG else 'below'} 200SMA",
            detail=detail,
            evidence={"rsi2": round(r, 1), "sma200": round(safe_last(sma), 2), "atr": round(atr, 3)},
            tags=["swing", "mean-reversion"],
        )
        return [p] if p else []


# --------------------------------------------------------------------------- #
@register
class BollingerFade(Strategy):
    key = "bollinger_fade"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.SWING
    title = "Bollinger Band Fade"
    thesis = (
        "In a range-bound market (low ADX), a close outside the 2-sigma "
        "Bollinger Band is usually an over-extension that snaps back toward "
        "the 20-day mean."
    )
    default_params = {"length": 20, "mult": 2.0, "adx_max": 20, "stop_atr": 1.5}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        if not ctx.enough_daily(self.params["length"] + 20):
            return []
        d = ctx.daily
        bb = ta.bollinger(d["close"], self.params["length"], self.params["mult"])
        adx = safe_last(ta.adx(d, 14)["adx"])
        atr = safe_last(ta.atr(d, 14))
        price = ctx.price
        mid, lower, upper = safe_last(bb["mid"]), safe_last(bb["lower"]), safe_last(bb["upper"])
        pctb = safe_last(bb["pct_b"])
        if any(math.isnan(x) for x in (mid, lower, upper, adx, atr)) or atr <= 0:
            return []
        if adx > self.params["adx_max"]:
            return []

        if price <= lower or pctb < 0.02:
            entry = price
            stop = entry - self.params["stop_atr"] * atr
            targets = [mid, upper]
            detail = (
                f"ADX is {adx:.0f} (range regime) and price {price:.2f} closed at/"
                f"below the lower band {lower:.2f}. Mean-reversion target is the "
                f"20-day mid-band {mid:.2f}."
            )
            return self._wrap(ctx, Side.LONG, entry, stop, targets, 0.55, detail, adx, pctb, mid, atr)

        if price >= upper or pctb > 0.98:
            entry = price
            stop = entry + self.params["stop_atr"] * atr
            targets = [mid, lower]
            detail = (
                f"ADX is {adx:.0f} (range regime) and price {price:.2f} closed at/"
                f"above the upper band {upper:.2f}. Fade back toward the mid-band "
                f"{mid:.2f}."
            )
            return self._wrap(ctx, Side.SHORT, entry, stop, targets, 0.55, detail, adx, pctb, mid, atr)
        return []

    def _wrap(self, ctx, side, entry, stop, targets, conf, detail, adx, pctb, mid, atr):
        p = self._mk_play(
            ctx, side, entry, stop, targets, conf,
            rationale=f"tag of {'lower' if side is Side.LONG else 'upper'} band, ADX {adx:.0f}",
            detail=detail,
            evidence={"adx": round(adx, 1), "pct_b": round(pctb, 3), "mid_band": round(mid, 2),
                      "atr": round(atr, 3)},
            tags=["swing", "mean-reversion", "range"],
        )
        return [p] if p else []


# --------------------------------------------------------------------------- #
@register
class AtrChannelBreakout(Strategy):
    key = "atr_channel_breakout"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.SWING
    title = "Keltner / ATR Channel Breakout"
    thesis = (
        "A close beyond an ATR-based channel around the 20-EMA, while ADX is "
        "rising through 20, signals a volatility expansion likely to trend."
    )
    default_params = {"length": 20, "mult": 1.5, "adx_min": 20}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        if not ctx.enough_daily(60):
            return []
        d = ctx.daily
        kc = ta.keltner(d, self.params["length"], self.params["mult"])
        adxdf = ta.adx(d, 14)
        adx = safe_last(adxdf["adx"])
        adx_prev = float(adxdf["adx"].iloc[-3]) if len(adxdf) > 3 else adx
        atr = safe_last(ta.atr(d, 14))
        price = ctx.price
        upper, lower, mid = safe_last(kc["upper"]), safe_last(kc["lower"]), safe_last(kc["mid"])
        if any(math.isnan(x) for x in (upper, lower, mid, adx, atr)) or atr <= 0:
            return []
        rising = adx >= adx_prev and adx >= self.params["adx_min"]

        if price > upper and rising:
            entry = price
            stop = mid
            targets = [entry + 2.0 * atr, entry + 3.5 * atr]
            detail = (
                f"Close {price:.2f} broke the upper ATR channel {upper:.2f} with "
                f"ADX {adx:.0f} and rising - a trend day likely follows."
            )
            return self._wrap(ctx, Side.LONG, entry, stop, targets, 0.58, detail, adx, upper, lower, atr)

        if price < lower and rising:
            entry = price
            stop = mid
            targets = [entry - 2.0 * atr, entry - 3.5 * atr]
            detail = (
                f"Close {price:.2f} broke the lower ATR channel {lower:.2f} with "
                f"ADX {adx:.0f} and rising."
            )
            return self._wrap(ctx, Side.SHORT, entry, stop, targets, 0.58, detail, adx, upper, lower, atr)
        return []

    def _wrap(self, ctx, side, entry, stop, targets, conf, detail, adx, upper, lower, atr):
        p = self._mk_play(
            ctx, side, entry, stop, targets, conf,
            rationale=f"channel breakout, ADX {adx:.0f}",
            detail=detail,
            evidence={"adx": round(adx, 1), "kc_upper": round(upper, 2),
                      "kc_lower": round(lower, 2), "atr": round(atr, 3)},
            tags=["swing", "breakout", "trend"],
        )
        return [p] if p else []


# --------------------------------------------------------------------------- #
@register
class GapAndGo(Strategy):
    key = "gap_and_go"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.INTRADAY
    title = "Gap & Go"
    thesis = (
        "A stock that gaps meaningfully on a news catalyst and then holds "
        "above (below) its opening VWAP on heavy relative volume often "
        "continues the gap direction into the first hour or two."
    )
    default_params = {"min_gap_pct": 2.0, "rvol_min": 2.0}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        if not ctx.enough_daily(5) or not ctx.enough_intraday(6):
            return []
        today = ctx.today_intraday()
        if today is None or len(today) < 3 or ctx.minutes_since_open > 120:
            return []
        prior_close = float(ctx.daily["close"].iloc[-2]) if len(ctx.daily) >= 2 else float(ctx.daily["close"].iloc[-1])
        day_open = float(today["open"].iloc[0])
        gap_pct = (day_open / prior_close - 1.0) * 100.0
        rvol = ta.rel_volume_intraday(ctx.intraday)
        vwap = safe_last(ta.session_vwap(ctx.intraday))
        atr = safe_last(ta.atr(ctx.intraday, 14))
        price = ctx.price
        if math.isnan(vwap) or math.isnan(atr) or atr <= 0 or rvol < self.params["rvol_min"]:
            return []

        if gap_pct >= self.params["min_gap_pct"] and price > vwap and price > day_open:
            entry = price
            stop = min(vwap, float(today["low"].min())) - 0.1 * atr
            move = day_open - prior_close
            targets = [entry + max(1.0 * abs(move), 1.5 * (entry - stop)), entry + 2.5 * (entry - stop)]
            detail = (
                f"{ctx.symbol} gapped +{gap_pct:.1f}% and is holding above the "
                f"opening VWAP {vwap:.2f} on {rvol:.1f}x volume - continuation setup."
            )
            return self._wrap(ctx, Side.LONG, entry, stop, targets,
                              min(0.8, 0.45 + 0.1 * rvol), detail, gap_pct, rvol, vwap, atr)

        if gap_pct <= -self.params["min_gap_pct"] and price < vwap and price < day_open:
            entry = price
            stop = max(vwap, float(today["high"].max())) + 0.1 * atr
            targets = [entry - 1.5 * (stop - entry), entry - 2.5 * (stop - entry)]
            detail = (
                f"{ctx.symbol} gapped {gap_pct:.1f}% and is failing under the "
                f"opening VWAP {vwap:.2f} on {rvol:.1f}x volume - breakdown continuation."
            )
            return self._wrap(ctx, Side.SHORT, entry, stop, targets,
                              min(0.8, 0.45 + 0.1 * rvol), detail, gap_pct, rvol, vwap, atr)
        return []

    def _wrap(self, ctx, side, entry, stop, targets, conf, detail, gap_pct, rvol, vwap, atr):
        p = self._mk_play(
            ctx, side, entry, stop, targets, conf,
            rationale=f"gap {gap_pct:+.1f}%, {rvol:.1f}x vol, {'above' if side is Side.LONG else 'below'} VWAP",
            detail=detail,
            evidence={"gap_pct": round(gap_pct, 2), "rvol": round(rvol, 2),
                      "vwap": round(vwap, 2), "atr": round(atr, 3)},
            tags=["intraday", "gap", "momentum", "catalyst"],
        )
        return [p] if p else []


# --------------------------------------------------------------------------- #
@register
class Week52Breakout(Strategy):
    key = "week52_breakout"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.SWING
    title = "52-Week High/Low Momentum"
    thesis = (
        "Pignataro (Ch. 12) uses the 52-week high/low as a valuation anchor; "
        "technically, a push to new 52-week highs on volume is momentum "
        "confirmation, while new 52-week lows signal distribution."
    )
    default_params = {"proximity_pct": 1.0, "vol_mult": 1.3}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        if not ctx.enough_daily(200):
            return []
        d = ctx.daily.tail(252)
        hi_52 = float(d["high"].max())
        lo_52 = float(d["low"].min())
        price = ctx.price
        atr = safe_last(ta.atr(ctx.daily, 14))
        vol = float(d["volume"].iloc[-1])
        vol_avg = float(d["volume"].tail(20).mean())
        if math.isnan(atr) or atr <= 0 or vol_avg <= 0:
            return []
        vmult = vol / vol_avg
        near_hi = price >= hi_52 * (1 - self.params["proximity_pct"] / 100.0)
        near_lo = price <= lo_52 * (1 + self.params["proximity_pct"] / 100.0)

        if near_hi and vmult >= self.params["vol_mult"]:
            entry = price
            stop = min(entry - 2.0 * atr, float(d["low"].tail(10).min()))
            targets = [entry + 2.0 * atr, entry + 4.0 * atr]
            detail = (
                f"{ctx.symbol} is pressing its 52-week high {hi_52:.2f} on "
                f"{vmult:.1f}x average volume - momentum breakout."
            )
            return self._wrap(ctx, Side.LONG, entry, stop, targets, 0.57, detail, hi_52, lo_52, vmult, atr)

        if near_lo and vmult >= self.params["vol_mult"]:
            entry = price
            stop = max(entry + 2.0 * atr, float(d["high"].tail(10).max()))
            targets = [entry - 2.0 * atr, entry - 4.0 * atr]
            detail = (
                f"{ctx.symbol} is breaking its 52-week low {lo_52:.2f} on "
                f"{vmult:.1f}x average volume - distribution / breakdown."
            )
            return self._wrap(ctx, Side.SHORT, entry, stop, targets, 0.57, detail, hi_52, lo_52, vmult, atr)
        return []

    def _wrap(self, ctx, side, entry, stop, targets, conf, detail, hi, lo, vmult, atr):
        p = self._mk_play(
            ctx, side, entry, stop, targets, conf,
            rationale=f"{'new 52w high' if side is Side.LONG else 'new 52w low'}, {vmult:.1f}x vol",
            detail=detail,
            evidence={"high_52w": round(hi, 2), "low_52w": round(lo, 2),
                      "vol_mult": round(vmult, 2), "atr": round(atr, 3)},
            tags=["swing", "momentum", "52-week"],
        )
        return [p] if p else []
