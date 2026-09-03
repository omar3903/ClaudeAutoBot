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

from ..analysis import read_row
from ..core.enums import Side, StrategyKind, Timeframe
from ..core.models import Play
from ..indicators import ta
from .base import Strategy, StrategyContext, safe_last, swing_high, swing_low
from .registry import register


def _nan(*xs) -> bool:
    return any(x is None or (isinstance(x, float) and math.isnan(x)) for x in xs)


def _last_closed(today: pd.DataFrame):
    """The most recently *completed* bar (Aziz keys every confirmation off a
    closed 5-minute candle, never the one still printing)."""
    if today is None or len(today) < 2:
        return None
    return today.iloc[-2]


# --------------------------------------------------------------------------- #
@register
class OpeningRangeBreakout(Strategy):
    key = "opening_range_breakout"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.INTRADAY
    tod_profile = "momentum"
    expected_hold = (45.0, 120.0)   # minutes
    title = "Opening-Range Breakout"
    thesis = (
        "The high and low of the first few minutes frame the day's opening "
        "auction. When that range is tight relative to how far the stock "
        "normally travels in a day, a decisive break of it on volume and on "
        "the right side of VWAP tends to run to the next level (Aziz, Strategy 9)."
    )
    # Aziz: the opening range must be SMALLER than the daily ATR - if the stock
    # has already moved ~ATR by the time the range forms it is too volatile and
    # there is no catchable directional move left.
    default_params = {"or_minutes": 5, "rvol_min": 1.3, "max_or_vs_atr": 1.0}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        if not ctx.enough_intraday(6):
            return []
        today = ctx.today_intraday()
        if today is None or len(today) < 3:
            return []
        or_m = int(self.params["or_minutes"])
        if ctx.minutes_since_open < or_m + 4 or ctx.minutes_since_open > 150:
            return []

        orr = ta.opening_range(ctx.intraday, or_m)
        or_high, or_low = safe_last(orr["or_high"]), safe_last(orr["or_low"])
        vwap = safe_last(ta.session_vwap(ctx.intraday))
        atr = safe_last(ta.atr(ctx.intraday, 14))
        d_atr = safe_last(ta.atr(ctx.daily, 14))
        rvol = ctx.rvol()
        price = ctx.price
        if _nan(or_high, or_low, vwap, atr) or or_high <= or_low:
            return []
        or_range = or_high - or_low

        # the tightness gate
        if d_atr and or_range >= self.params["max_or_vs_atr"] * d_atr:
            return []
        if rvol < self.params["rvol_min"]:
            return []

        sr = ctx.levels()

        if price > or_high and price > vwap:
            entry = or_high
            stop = min(or_low, vwap - 0.10 * atr)        # Aziz: stop is VWAP-side
            up = sr.nearest_above(entry)
            t1 = up.price if up else entry + 1.0 * or_range
            targets = [t1, entry + 2.0 * or_range]
            conf = min(0.85, 0.45 + 0.15 * (rvol - 1) + 0.10)
            detail = (
                f"Price {price:.2f} cleared the {or_m}-min opening-range high "
                f"{or_high:.2f} holding above VWAP {vwap:.2f}, on {rvol:.1f}x typical "
                f"volume. The range ({or_range:.2f}) is inside the daily ATR "
                f"({d_atr:.2f}) - a tradable, not a runaway, open."
            )
            note = ("ORB is an entry signal only. Target is the next level up "
                    f"({t1:.2f}); if it stalls, a fresh 5-minute low is your cue "
                    "the buyers are done.")
            inval = f"a 5-minute close back below VWAP {vwap:.2f}"
            return self._one(ctx, Side.LONG, entry, stop, targets, conf, detail,
                             or_high, or_low, vwap, rvol, atr, note, inval)

        if price < or_low and price < vwap:
            entry = or_low
            stop = max(or_high, vwap + 0.10 * atr)
            dn = sr.nearest_below(entry)
            t1 = dn.price if dn else entry - 1.0 * or_range
            targets = [t1, entry - 2.0 * or_range]
            conf = min(0.85, 0.45 + 0.15 * (rvol - 1) + 0.10)
            detail = (
                f"Price {price:.2f} broke the {or_m}-min opening-range low "
                f"{or_low:.2f} below VWAP {vwap:.2f}, on {rvol:.1f}x typical volume. "
                f"The range ({or_range:.2f}) is inside the daily ATR ({d_atr:.2f})."
            )
            note = ("ORB is an entry signal only. Target is the next level down "
                    f"({t1:.2f}); cover if it prints a fresh 5-minute high.")
            inval = f"a 5-minute close back above VWAP {vwap:.2f}"
            return self._one(ctx, Side.SHORT, entry, stop, targets, conf, detail,
                             or_high, or_low, vwap, rvol, atr, note, inval)
        return []

    def _one(self, ctx, side, entry, stop, targets, conf, detail,
             or_high, or_low, vwap, rvol, atr, note, inval):
        p = self._mk_play(
            ctx, side, entry, stop, targets, conf,
            rationale=f"{or_low:.2f}-{or_high:.2f} OR break, {rvol:.1f}x vol",
            detail=detail,
            evidence={"or_high": round(or_high, 2), "or_low": round(or_low, 2),
                      "vwap": round(vwap, 2), "rvol": round(rvol, 2),
                      "atr": round(atr, 3)},
            tags=["intraday", "breakout", "momentum"],
            edge_note=note, invalidation=inval,
        )
        return [p] if p else []


# --------------------------------------------------------------------------- #
@register
class VwapReclaim(Strategy):
    key = "vwap_reclaim"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.INTRADAY
    tod_profile = "reversal"
    expected_hold = (60.0, 150.0)   # minutes
    title = "VWAP Reclaim / Loss"
    thesis = (
        "VWAP is the average institutional fill for the day - it tells you who "
        "is in control. After several bars stuck under VWAP, a 5-minute close "
        "back above it traps the sellers and they become buyers (Aziz, Strategy "
        "6). A strong stock losing VWAP is the mirror."
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
        if _nan(atr) or atr <= 0:
            return []
        above = (close > vwap_s)
        # Aziz: confirm on a CLOSED 5-minute candle, not the bar still printing
        last_closed_above = bool(above.iloc[-2]) if len(above) >= 2 else False
        prev_below = int((~above.iloc[-5:-2]).sum())
        prev_above = int((above.iloc[-5:-2]).sum())
        price = ctx.price
        vwap = safe_last(vwap_s)
        sr = ctx.levels()

        if last_closed_above and prev_below >= self.params["min_bars_below"]:
            entry = price
            stop = min(swing_low(today["low"], 6), vwap - 0.4 * atr)
            up = sr.nearest_above(entry)
            hod = float(today["high"].max())
            t1 = up.price if up else max(hod, entry + 1.5 * (entry - stop))
            targets = [t1, entry + 2.5 * (entry - stop)]
            conf = 0.55 + 0.04 * prev_below
            detail = (
                f"{int(prev_below)} of the last 3 closed bars were under VWAP; the "
                f"latest closed back above it ({price:.2f} vs VWAP {vwap:.2f}). "
                f"First objective is the next level up ({t1:.2f})."
            )
            note = ("Buy as close to VWAP as you can so the stop is small. Sellers "
                    "who shorted under VWAP now have to cover into you.")
            inval = f"a 5-minute close back below VWAP {vwap:.2f}"
            return self._wrap(ctx, Side.LONG, entry, stop, targets, conf, detail,
                              vwap, atr, prev_below, note, inval)

        if (not last_closed_above) and prev_above >= self.params["min_bars_below"]:
            entry = price
            stop = max(swing_high(today["high"], 6), vwap + 0.4 * atr)
            dn = sr.nearest_below(entry)
            lod = float(today["low"].min())
            t1 = dn.price if dn else min(lod, entry - 1.5 * (stop - entry))
            targets = [t1, entry - 2.5 * (stop - entry)]
            conf = 0.55
            detail = (
                f"Price lost VWAP after holding above it for {prev_above} closed "
                f"bars ({price:.2f} vs VWAP {vwap:.2f}); control has flipped to the "
                f"sellers. First objective is the next level down ({t1:.2f})."
            )
            note = ("Short near VWAP for a tight stop. If it reclaims VWAP on a "
                    "5-minute close the thesis is wrong - be out.")
            inval = f"a 5-minute close back above VWAP {vwap:.2f}"
            return self._wrap(ctx, Side.SHORT, entry, stop, targets, conf, detail,
                              vwap, atr, 0, note, inval)
        return []

    def _wrap(self, ctx, side, entry, stop, targets, conf, detail, vwap, atr, nb, note, inval):
        p = self._mk_play(
            ctx, side, entry, stop, targets, conf,
            rationale=f"{'reclaimed' if side is Side.LONG else 'lost'} VWAP {vwap:.2f}",
            detail=detail,
            evidence={"vwap": round(vwap, 2), "atr": round(atr, 3), "bars_below": int(nb)},
            tags=["intraday", "vwap", "reversal"],
            edge_note=note, invalidation=inval,
        )
        return [p] if p else []


# --------------------------------------------------------------------------- #
@register
class EmaPullbackTrend(Strategy):
    key = "ema_pullback_trend"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.INTRADAY
    tod_profile = "trend"
    expected_hold = (75.0, 180.0)   # minutes
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
    expected_hold = (3.0, 7.0)      # trading days
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
    expected_hold = (4.0, 9.0)      # trading days
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
    expected_hold = (7.0, 15.0)     # trading days
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
    expected_hold = (40.0, 120.0)   # minutes
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
    expected_hold = (8.0, 20.0)     # trading days
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


# ===========================================================================
#  Aziz day-trade setups  (How to Day Trade for a Living, Ch. 7)
# ===========================================================================
@register
class AbcdPattern(Strategy):
    key = "abcd_pattern"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.INTRADAY
    tod_profile = "momentum"
    expected_hold = (15.0, 45.0)   # minutes
    title = "ABCD Pattern"
    thesis = (
        "A hard push from A to a new high B, then a pullback that stops at a "
        "HIGHER low C. Buyers defending C means the move up to D (a retest of B "
        "and beyond) is the higher-probability path. You enter near C - never "
        "chase B - so the stop (a break of C) is small (Aziz, Strategy 1)."
    )
    default_params = {"window": 14, "near_c_pct": 1.5, "rvol_min": 1.3}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        if not ctx.enough_intraday(10):
            return []
        today = ctx.today_intraday()
        w = int(self.params["window"])
        if today is None or len(today) < 6:
            return []
        seg = today.tail(w)
        if len(seg) < 6:
            return []
        atr = safe_last(ta.atr(ctx.intraday, 14))
        rvol = ctx.rvol()
        if _nan(atr) or atr <= 0:
            return []

        highs = seg["high"].to_numpy()
        lows = seg["low"].to_numpy()
        b_i = int(highs.argmax())
        if b_i == 0 or b_i >= len(seg) - 1:
            return []                       # need a leg before and a pullback after
        B = float(highs[b_i])
        A = float(lows[:b_i].min())
        C = float(lows[b_i:].min())
        price = ctx.price
        if not (A < C < B):
            return []                       # C must be a HIGHER low than A
        if rvol < self.params["rvol_min"]:
            return []
        # price must be back near C (defending it), not still up at B
        if price > C * (1 + self.params["near_c_pct"] / 100.0) or price >= B:
            return []
        # last closed bar should not be collapsing through C
        lc = _last_closed(today)
        if lc is not None and float(lc["close"]) < C - 0.5 * atr:
            return []

        entry = max(price, C)
        stop = C - max(0.25 * atr, 0.02 * C * 0.0 + 0.03)
        move = B - A
        targets = [B, B + move]             # D = retest of B, then measured move
        conf = min(0.80, 0.44 + 0.12 * (rvol - 1) + 0.10 * ((C - A) / move if move else 0))
        detail = (
            f"Leg A {A:.2f} -> B {B:.2f} (+{move:.2f}), pullback held at C {C:.2f} "
            f"(a higher low), price back at {price:.2f} on {rvol:.1f}x volume. "
            f"Buyers are defending C."
        )
        note = ("Volume should spike again as it pushes back toward B (that is D). "
                "Scale half at D, stop to break-even, exit the rest on a fresh "
                "5-minute low.")
        inval = f"a 5-minute close below C {C:.2f}"
        p = self._mk_play(
            ctx, Side.LONG, entry, stop, targets, conf,
            rationale=f"ABCD - C {C:.2f} holding above A {A:.2f}",
            detail=detail,
            evidence={"A": round(A, 2), "B": round(B, 2), "C": round(C, 2),
                      "rvol": round(rvol, 2), "atr": round(atr, 3)},
            tags=["intraday", "momentum", "abcd"],
            edge_note=note, invalidation=inval,
        )
        return [p] if p else []


@register
class MomentumFlag(Strategy):
    key = "bull_bear_flag"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.INTRADAY
    tod_profile = "momentum"
    expected_hold = (10.0, 35.0)   # minutes
    title = "Bull / Bear Flag"
    thesis = (
        "A near-vertical run (the pole) followed by a few small sideways bars "
        "(the flag) where profit-takers sell but buyers keep absorbing it. The "
        "break of the flag in the pole's direction, on rising volume, tends to "
        "travel about another pole-length (Aziz, Strategy 2)."
    )
    default_params = {"pole_atr_mult": 1.6, "flag_bars": 3, "max_flag_ratio": 0.55}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        if not ctx.enough_intraday(12):
            return []
        today = ctx.today_intraday()
        fb = int(self.params["flag_bars"])
        if today is None or len(today) < fb + 5:
            return []
        atr = safe_last(ta.atr(ctx.intraday, 14))
        rvol = ctx.rvol()
        if _nan(atr) or atr <= 0:
            return []
        pole = today.iloc[-(fb + 5):-fb]
        flag = today.iloc[-fb:]
        if len(pole) < 3 or len(flag) < 2:
            return []
        pole_move = float(pole["close"].iloc[-1] - pole["open"].iloc[0])
        flag_rng = float(flag["high"].max() - flag["low"].min())
        if abs(pole_move) < self.params["pole_atr_mult"] * atr:
            return []
        if flag_rng > self.params["max_flag_ratio"] * abs(pole_move) or flag_rng <= 0:
            return []
        price = ctx.price
        fh, fl = float(flag["high"].max()), float(flag["low"].min())

        if pole_move > 0 and price >= fh:                     # bull flag break-up
            entry = fh
            stop = fl
            targets = [entry + abs(pole_move), entry + 1.6 * abs(pole_move)]
            conf = min(0.78, 0.42 + 0.12 * (rvol - 1) + 0.08)
            side, word = Side.LONG, "up"
        elif pole_move < 0 and price <= fl:                   # bear flag break-down
            entry = fl
            stop = fh
            targets = [entry - abs(pole_move), entry - 1.6 * abs(pole_move)]
            conf = min(0.72, 0.40 + 0.12 * (rvol - 1) + 0.06)
            side, word = Side.SHORT, "down"
        else:
            return []

        detail = (
            f"Pole {pole_move:+.2f} over {len(pole)} bars, then a {len(flag)}-bar "
            f"flag only {flag_rng:.2f} wide ({flag_rng / abs(pole_move) * 100:.0f}% "
            f"of the pole). Price is breaking the flag {word} at {price:.2f} on "
            f"{rvol:.1f}x volume."
        )
        note = ("Enter on the break, not during the flag. Measured move is one "
                "more pole-length; take profit into the volatility, don't wait "
                "for a reversal.")
        inval = (f"price falling back inside the flag (a close {'below' if side is Side.LONG else 'above'} "
                 f"{stop:.2f})")
        p = self._mk_play(
            ctx, side, entry, stop, targets, conf,
            rationale=f"{'bull' if side is Side.LONG else 'bear'} flag break, {rvol:.1f}x vol",
            detail=detail,
            evidence={"pole": round(pole_move, 2), "flag_high": round(fh, 2),
                      "flag_low": round(fl, 2), "rvol": round(rvol, 2)},
            tags=["intraday", "momentum", "flag"],
            edge_note=note, invalidation=inval,
        )
        return [p] if p else []


@register
class RedToGreen(Strategy):
    key = "red_to_green"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.INTRADAY
    tod_profile = "momentum"
    expected_hold = (10.0, 40.0)   # minutes
    title = "Red-to-Green (prior-day close)"
    thesis = (
        "The prior day's closing price is a magnet - it is the level the whole "
        "market agreed on yesterday. A stock that gapped down and is grinding "
        "back toward it on rising volume usually completes the trip to "
        "'green on the day' (Aziz, Strategy 8). Gapped-up and fading is the "
        "Green-to-Red short."
    )
    default_params = {"max_dist_pct": 1.5, "rvol_min": 1.3}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        if not ctx.enough_daily(3) or not ctx.enough_intraday(4):
            return []
        today = ctx.today_intraday()
        if today is None or len(today) < 3:
            return []
        prev_close = ctx.prev_close()
        day_open = float(today["open"].iloc[0])
        price = ctx.price
        atr = safe_last(ta.atr(ctx.intraday, 14))
        vwap = safe_last(ta.session_vwap(ctx.intraday))
        rvol = ctx.rvol()
        if _nan(atr, vwap, prev_close) or atr <= 0 or prev_close <= 0:
            return []
        if rvol < self.params["rvol_min"]:
            return []
        dist_pct = abs(price - prev_close) / prev_close * 100.0
        if dist_pct > self.params["max_dist_pct"]:
            return []
        lc = _last_closed(today)
        if lc is None:
            return []
        bar_green = float(lc["close"]) >= float(lc["open"])
        sr = ctx.levels()

        # gapped DOWN, pushing back UP toward prior close -> long
        if day_open < prev_close and price < prev_close and bar_green:
            entry = price
            below = sr.nearest_below(price)
            stop = min(vwap, below.price if below else price) - 0.10 * atr
            targets = [prev_close, prev_close + 0.6 * atr]
            conf = min(0.74, 0.44 + 0.10 * (rvol - 1))
            side = Side.LONG
            detail = (
                f"{ctx.symbol} gapped down (open {day_open:.2f} vs prior close "
                f"{prev_close:.2f}) and is {dist_pct:.1f}% away, pressing back up on "
                f"{rvol:.1f}x volume with the last 5-min bar closing green."
            )
        # gapped UP, fading back DOWN toward prior close -> short
        elif day_open > prev_close and price > prev_close and not bar_green:
            entry = price
            above = sr.nearest_above(price)
            stop = max(vwap, above.price if above else price) + 0.10 * atr
            targets = [prev_close, prev_close - 0.6 * atr]
            conf = min(0.70, 0.42 + 0.10 * (rvol - 1))
            side = Side.SHORT
            detail = (
                f"{ctx.symbol} gapped up (open {day_open:.2f} vs prior close "
                f"{prev_close:.2f}) and is fading back on {rvol:.1f}x volume."
            )
        else:
            return []

        note = ("Red-to-Green should work almost immediately. Take the full "
                "target at the prior-day close; if it stalls, get out flat.")
        inval = f"a 5-minute close through the {('VWAP' if abs(stop - vwap) < 1e-6 else 'nearest level')} at {stop:.2f}"
        p = self._mk_play(
            ctx, side, entry, stop, targets, conf,
            rationale=f"{'red->green' if side is Side.LONG else 'green->red'} toward {prev_close:.2f}",
            detail=detail,
            evidence={"prev_close": round(prev_close, 2), "day_open": round(day_open, 2),
                      "vwap": round(vwap, 2), "rvol": round(rvol, 2)},
            tags=["intraday", "momentum", "prev-close"],
            edge_note=note, invalidation=inval,
        )
        return [p] if p else []


@register
class IntradayReversal(Strategy):
    key = "intraday_reversal"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.INTRADAY
    tod_profile = "reversal"
    expected_hold = (15.0, 45.0)   # minutes
    title = "Top / Bottom Reversal"
    thesis = (
        "Four things line up: 5+ candles running one way, a 5-minute RSI at a "
        "wild extreme, price into a real daily level, and an indecision (or "
        "sharp opposite) candle. That combination - not price 'looking high' - "
        "is when an over-extended move hands control back (Aziz, Strategies 3 & 4)."
    )
    default_params = {"min_run": 5, "rsi_hot": 80.0, "rsi_cold": 20.0, "level_band_pct": 0.6}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        if not ctx.enough_intraday(20):
            return []
        today = ctx.today_intraday()
        if today is None or len(today) < 8:
            return []
        close = today["close"]
        rsi = ta.rsi(close, 14)
        run = ta.consecutive_run(close)
        r = safe_last(rsi)
        atr = safe_last(ta.atr(ctx.intraday, 14))
        vwap = safe_last(ta.session_vwap(ctx.intraday))
        if _nan(r, atr, vwap) or atr <= 0:
            return []
        if abs(run) < int(self.params["min_run"]):
            return []
        price = ctx.price
        sr = ctx.levels()
        cndl = read_row(today, -2)               # last CLOSED bar
        band = self.params["level_band_pct"] / 100.0

        # extended DOWN + oversold -> bottom reversal (long)
        if run <= -self.params["min_run"] and r <= self.params["rsi_cold"]:
            lvl = sr.nearest_below(price) or sr.at(price, band)
            if lvl is None or (price - lvl.price) / price > band:
                return []
            if not (cndl.is_reversal_up or cndl.indecision):
                return []
            lod = float(today["low"].min())
            entry = price
            stop = min(lod, float(today["low"].iloc[-2])) - 0.05 * atr
            up_lvl = sr.nearest_above(price)
            cands = [x for x in (vwap, up_lvl.price if up_lvl else None)
                     if x is not None and x > entry]
            t1 = min(cands) if cands else entry + 1.5 * (entry - stop)
            targets = [t1, entry + 2.0 * (entry - stop)]
            conf = 0.50 + 0.02 * (abs(run) - self.params["min_run"]) + (0.08 if r <= 10 else 0.0)
            side, kind_word = Side.LONG, "Bottom"
        # extended UP + overbought -> top reversal (short)
        elif run >= self.params["min_run"] and r >= self.params["rsi_hot"]:
            lvl = sr.nearest_above(price) or sr.at(price, band)
            if lvl is None or (lvl.price - price) / price > band:
                return []
            if not (cndl.is_reversal_down or cndl.indecision):
                return []
            hod = float(today["high"].max())
            entry = price
            stop = max(hod, float(today["high"].iloc[-2])) + 0.05 * atr
            cands = [x for x in (vwap, (sr.nearest_below(price).price if sr.nearest_below(price) else vwap)) if x < entry]
            t1 = max(cands) if cands else entry - 1.5 * (stop - entry)
            targets = [t1, entry - 2.0 * (stop - entry)]
            conf = 0.48 + 0.02 * (abs(run) - self.params["min_run"]) + (0.08 if r >= 90 else 0.0)
            side, kind_word = Side.SHORT, "Top"
        else:
            return []

        detail = (
            f"{abs(run)} straight 5-min candles {'down' if side is Side.LONG else 'up'}, "
            f"RSI(14) {r:.0f}, into the daily level {lvl.price:.2f} "
            f"({'/'.join(lvl.sources)}), and the last closed bar was a "
            f"{cndl.name.replace('_', ' ')}."
        )
        note = ("Reversals only work at the extremes and at a real level. Target "
                "is VWAP / the nearest MA or the next level; exit on a fresh "
                "5-minute extreme back the original way.")
        inval = (f"a new {'low' if side is Side.LONG else 'high'} of day beyond "
                 f"{stop:.2f} (the move never actually reversed)")
        p = self._mk_play(
            ctx, side, entry, stop, targets, conf,
            rationale=f"{kind_word} reversal - RSI {r:.0f}, {abs(run)} candles at {lvl.price:.2f}",
            detail=detail,
            evidence={"run": run, "rsi": round(r, 1), "level": round(lvl.price, 2),
                      "vwap": round(vwap, 2), "atr": round(atr, 3)},
            tags=["intraday", "reversal"],
            edge_note=note, invalidation=inval,
        )
        return [p] if p else []


@register
class SupportResistanceBounce(Strategy):
    key = "sr_bounce"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.INTRADAY
    tod_profile = "reversal"
    expected_hold = (20.0, 60.0)   # minutes
    title = "Horizontal Support / Resistance"
    thesis = (
        "The market remembers price levels, not diagonal trend lines. Price "
        "arriving at a horizontal level that has been defended before - "
        "especially with an indecision candle and volume - tends to bounce to "
        "the next level (Aziz, Strategy 7)."
    )
    default_params = {"touch_pct": 0.25, "min_strength": 0.45}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        if not ctx.enough_intraday(12) or not ctx.enough_daily(20):
            return []
        today = ctx.today_intraday()
        if today is None or len(today) < 4:
            return []
        atr = safe_last(ta.atr(ctx.intraday, 14))
        if _nan(atr) or atr <= 0:
            return []
        price = ctx.price
        sr = ctx.levels()
        here = sr.at(price, self.params["touch_pct"] / 100.0)
        if here is None or here.strength < self.params["min_strength"]:
            return []
        cndl = read_row(today, -2)

        if here.kind == "support" and (cndl.is_reversal_up or cndl.indecision):
            nxt = sr.nearest_above(price)
            if nxt is None:
                return []
            entry = price
            stop = here.price - max(0.35 * atr, 0.04)
            targets = [nxt.price, nxt.price + 0.5 * atr]
            side = Side.LONG
        elif here.kind == "resistance" and (cndl.is_reversal_down or cndl.indecision):
            nxt = sr.nearest_below(price)
            if nxt is None:
                return []
            entry = price
            stop = here.price + max(0.35 * atr, 0.04)
            targets = [nxt.price, nxt.price - 0.5 * atr]
            side = Side.SHORT
        else:
            return []

        conf = 0.46 + 0.30 * here.strength
        detail = (
            f"Price {price:.2f} is at the {here.kind} {here.price:.2f} "
            f"(strength {here.strength:.2f}; {'/'.join(here.sources)}) and the last "
            f"closed 5-min bar was a {cndl.name.replace('_', ' ')}. Next level is "
            f"{targets[0]:.2f}."
        )
        note = ("Buy/short as close to the level as possible for a small stop. "
                "Take profit at the next level - if there is no clear next level, "
                "use the nearest half- or whole-dollar.")
        inval = f"a 5-minute close through {here.price:.2f}"
        p = self._mk_play(
            ctx, side, entry, stop, targets, conf,
            rationale=f"bounce at {here.kind} {here.price:.2f}",
            detail=detail,
            evidence={"level": round(here.price, 2), "kind": here.kind,
                      "strength": round(here.strength, 2), "sources": here.sources,
                      "atr": round(atr, 3)},
            tags=["intraday", "reversal", "level"],
            edge_note=note, invalidation=inval,
        )
        return [p] if p else []


@register
class DivergenceReversal(Strategy):
    key = "divergence_reversal"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.SWING
    tod_profile = "swing"
    expected_hold = (4.0, 10.0)    # trading days
    title = "Oscillator Divergence at a Level"
    thesis = (
        "Murphy: when price makes a new extreme but the RSI (or MACD histogram) "
        "does not, the move is running out of fuel. Taken only where price is "
        "also into a horizontal level, it is one of the more reliable swing "
        "reversal warnings."
    )
    default_params = {"lookback": 40, "stop_atr": 1.5}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        lb = int(self.params["lookback"])
        if not ctx.enough_daily(lb + 20):
            return []
        d = ctx.daily
        c = d["close"]
        rsi = ta.rsi(c, 14)
        atr = safe_last(ta.atr(d, 14))
        if _nan(atr) or atr <= 0:
            return []
        rsi_div = ta.rsi_divergence(c, rsi, lb)
        div = rsi_div or ta.macd_divergence(c, lb)
        if not div:
            return []
        div_src = "RSI" if rsi_div else "MACD"
        price = ctx.price
        sr = ctx.levels()

        if div == "bullish":
            lvl = sr.nearest_below(price) or sr.at(price, 0.02)
            if lvl is None:
                return []
            entry = price
            stop = min(float(d["low"].tail(10).min()), lvl.price) - self.params["stop_atr"] * atr * 0.0 - 0.02
            stop = min(stop, entry - self.params["stop_atr"] * atr)
            up = sr.nearest_above(price)
            targets = [up.price if up else entry + 2.0 * atr, entry + 3.5 * atr]
            side = Side.LONG
        else:  # bearish
            lvl = sr.nearest_above(price) or sr.at(price, 0.02)
            if lvl is None:
                return []
            entry = price
            stop = max(float(d["high"].tail(10).max()), lvl.price)
            stop = max(stop, entry + self.params["stop_atr"] * atr)
            dn = sr.nearest_below(price)
            targets = [dn.price if dn else entry - 2.0 * atr, entry - 3.5 * atr]
            side = Side.SHORT

        conf = 0.52 + 0.10 * (lvl.strength if lvl else 0.0)
        detail = (
            f"{div.capitalize()} {div_src} divergence over ~{lb} sessions with "
            f"price into the {lvl.kind} {lvl.price:.2f}. The trend is losing "
            f"momentum even as price pushes the extreme."
        )
        note = ("Divergence times, it doesn't guarantee - wait for price to "
                "confirm by turning at the level. Swing stop is beyond the "
                "recent extreme; target the prior swing / next level.")
        inval = f"a daily close beyond {stop:.2f} (the extreme the divergence was against)"
        p = self._mk_play(
            ctx, side, entry, stop, targets, conf,
            rationale=f"{div} divergence at {lvl.kind} {lvl.price:.2f}",
            detail=detail,
            evidence={"divergence": div, "level": round(lvl.price, 2),
                      "atr": round(atr, 3)},
            tags=["swing", "reversal", "divergence"],
            edge_note=note, invalidation=inval,
        )
        return [p] if p else []
