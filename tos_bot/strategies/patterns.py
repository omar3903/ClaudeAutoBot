"""Swing setups on daily candles from the market-structure books.

* **Grimes**, *The Art and Science of Technical Analysis* (ch. 6, the trading templates): the
  **failure test** - Wyckoff's spring and upthrust, Sperandeo's 2B - and the **pullback** after a
  momentum thrust, read with his tools: a 20-period EMA inside Keltner channels 2.25 ATRs wide.
  His own tests found an edge in few patterns; these two are the ones he trades.
* **Bulkowski**, *Encyclopedia of Chart Patterns* (ch. 13-20), with **Murphy** (ch. 5): the
  **double bottom and top**, taken only once *confirmed* - a close beyond the peak (trough) between
  the two lows (highs). Bulkowski counts 64% of unconfirmed twin bottoms failing; confirmed, the
  height of the pattern projected from the confirmation price is met about two times in three.

Every setup here signals on the last *completed* daily candle - a close "back above support" on a
candle still forming is no close at all - and is entered at the price now, unless that has already
run an ATR from the signal (the replay fills at the next open under the same rule).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..core.enums import Side, StrategyKind, Timeframe
from ..core.models import Play
from ..indicators import ta
from ..util import clock
from .base import Strategy, StrategyContext
from .registry import register

KELTNER_ATRS = 2.25                # Grimes's modified Keltner channel
RAN_AWAY_ATRS = 1.0                # the price now, against the signal candle's close


def completed_daily(ctx: StrategyContext) -> pd.DataFrame:
    """The daily candles without the one still forming (it is there only in a live session)."""
    d = ctx.daily
    forming = (len(d) and ctx.intraday is not None and d.index[-1].date() == clock.session_date(ctx.now)
               and 0 < ctx.minutes_since_open < 390)
    return d.iloc[:-1] if forming else d


def pivots(values: np.ndarray, span: int, lows: bool) -> List[int]:
    """Indexes of the swing lows (highs): the extreme of the ``span`` bars on either side."""
    out = []
    for i in range(span, len(values) - span):
        window = values[i - span:i + span + 1]
        if values[i] == (window.min() if lows else window.max()) and (window == values[i]).sum() == 1:
            out.append(i)
    return out


def _ran_away(ctx: StrategyContext, signal_close: float, atr: float) -> bool:
    return abs(ctx.price - signal_close) > RAN_AWAY_ATRS * atr


class _DailyPattern(Strategy):
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.SWING
    tod_profile = "swing"
    enabled_by_default = True
    book_tags: List[str] = []

    def _frame(self, ctx: StrategyContext, bars: int) -> Optional[Tuple[pd.DataFrame, float]]:
        if not ctx.enough_daily(bars):
            return None
        d = completed_daily(ctx)
        atr = ctx.daily_atr
        if len(d) < bars or math.isnan(atr) or atr <= 0:
            return None
        return d, atr

    def _play(self, ctx, side, entry, stop, targets, conf, rationale, detail, evidence, note, inval) -> List[Play]:
        p = self._mk_play(ctx, side, entry, stop, targets, conf, rationale=rationale, detail=detail,
                          evidence=evidence, tags=["swing", *self.book_tags], ttl_minutes=24 * 60,
                          edge_note=note, invalidation=inval)
        return [p] if p else []


# --------------------------------------------------------------------------- #
@register
class FailureTest(_DailyPattern):
    key = "failure_test"
    style = "reversal"
    expected_hold = (2.0, 8.0)      # trading days - Grimes: it works within one to three bars or not at all
    title = "Failure Test (spring / upthrust)"
    book_tags = ["reversal", "failure-test"]
    thesis = (
        "Markets probe beyond obvious support and resistance for the stop orders resting there. When "
        "the probe finds no conviction - the candle trades through the level and closes back inside - "
        "the traders who sold the break are trapped and must buy back. Wyckoff's spring and upthrust, "
        "Sperandeo's 2B; Grimes's most clearly defined trade: the stop goes just beyond the extreme of "
        "the test, and if price makes a new extreme the idea is simply wrong."
    )
    default_params = {"lookback": 30, "level_age": 5, "max_probe_atr": 1.0, "approach_atr": 1.5}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        got = self._frame(ctx, 60)
        if got is None:
            return []
        d, atr = got
        look, age = int(self.params["lookback"]), int(self.params["level_age"])
        last, prev = d.iloc[-1], d.iloc[-2]
        body = d.iloc[-(look + 2):-2]                     # the candles that defined the level, before the test
        if len(body) < look or _ran_away(ctx, float(last["close"]), atr):
            return []
        vol_avg = float(d["volume"].iloc[-22:-2].mean())
        heavy = vol_avg > 0 and float(last["volume"]) >= 1.5 * vol_avg
        rsi = ta.rsi(d["close"], 14)

        for side in (Side.LONG, Side.SHORT):
            long = side is Side.LONG
            col = "low" if long else "high"
            level = float(body[col].min() if long else body[col].max())
            where = int(np.argmin(body[col].values) if long else np.argmax(body[col].values))
            if len(body) - where < age:                   # the level must be a swing of its own, not this slide
                continue
            sign = 1.0 if long else -1.0
            came_from = float(d["close"].iloc[-12])
            if (came_from - level) * sign < self.params["approach_atr"] * atr:
                continue                                  # price has been sitting on the level: no probe to fail
            extreme = float(min(last["low"], prev["low"]) if long else max(last["high"], prev["high"]))
            probed = (level - extreme) * sign
            if not 0 < probed <= self.params["max_probe_atr"] * atr:
                continue                                  # never went through it - or collapsed through it
            close, mid = float(last["close"]), (float(last["high"]) + float(last["low"])) / 2.0
            if (close - level) * sign <= 0 or (close - mid) * sign < 0:
                continue                                  # no close back inside, or a weak one
            last_pierced = (level - float(last[col])) * sign > 0
            carried_over = (level - float(prev[col])) * sign > 0 and (float(prev["close"]) - level) * sign <= 0
            if not (last_pierced or carried_over):
                continue                                  # the test and its failure were a candle ago: that was the entry
            entry = ctx.price
            stop = extreme - sign * 0.1 * atr
            risk = (entry - stop) * sign
            if risk <= 0:
                continue
            diverged = ta.rsi_divergence(d["close"], rsi, 40) == ("bullish" if long else "bearish")
            conf = 0.57 + 0.04 * diverged + 0.03 * heavy
            word = "below support" if long else "above resistance"
            detail = (
                f"{ctx.symbol} traded {probed / atr:.1f} ATR {word} {level:.2f} - a level {len(body) - where} sessions "
                f"old - and closed back {'above' if long else 'under'} it at {close:.2f}, in the "
                f"{'upper' if long else 'lower'} half of the candle"
                + (", on heavy volume (stops triggered, nobody followed)" if heavy else "")
                + (", with momentum diverging from price" if diverged else "") + "."
            )
            note = ("It should work at once - within one to three sessions. Consolidation near the level "
                    "is the look of a real break coming; get out rather than wait for the stop.")
            inval = f"any trade beyond the extreme of the test, {extreme:.2f}"
            return self._play(ctx, side, entry, stop, [entry + sign * 1.6 * risk, entry + sign * 3.0 * risk], conf,
                              f"failed {'breakdown' if long else 'breakout'} at {level:.2f}", detail,
                              {"level": round(level, 2), "test_extreme": round(extreme, 2),
                               "probe_atr": round(probed / atr, 2), "divergence": bool(diverged),
                               "heavy_volume": bool(heavy)}, note, inval)
        return []


# --------------------------------------------------------------------------- #
@register
class TrendPullback(_DailyPattern):
    key = "trend_pullback"
    style = "momentum"
    expected_hold = (4.0, 12.0)
    title = "Pullback After a Momentum Thrust"
    book_tags = ["trend", "pullback"]
    thesis = (
        "A close outside the Keltner channel shows real momentum; the first orderly pullback after it "
        "is where the trend can be joined at a good price with the risk defined by the pullback's low. "
        "Grimes: moves against the higher-timeframe trend are weaker and abort suddenly as it "
        "reasserts itself - so the pullback must be shallow, quiet and short, and the entry waits for "
        "price to turn back with the trend."
    )
    default_params = {"thrust_window": 15, "max_retrace": 0.6, "min_bars": 2, "max_bars": 8, "near_ema_atr": 0.75}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        got = self._frame(ctx, 80)
        if got is None:
            return []
        d, atr = got
        d = d.tail(120)
        if _ran_away(ctx, float(d["close"].iloc[-1]), atr):
            return []
        ema = ta.ema(d["close"], 20)
        band = ta.atr(d, 20) * KELTNER_ATRS
        window = int(self.params["thrust_window"])
        for side in (Side.LONG, Side.SHORT):
            out = self._one_side(ctx, d, ema, band, atr, window, side)
            if out:
                return out
        return []

    def _one_side(self, ctx, d, ema, band, atr, window, side) -> List[Play]:
        long = side is Side.LONG
        sign = 1.0 if long else -1.0
        close, high, low = d["close"].values, d["high"].values, d["low"].values
        e, b = ema.values, band.values
        n = len(d)
        outside = [i for i in range(n - window, n - 1)
                   if not math.isnan(b[i]) and (close[i] - e[i]) * sign > b[i]]
        if not outside or (e[-1] - e[-6]) * sign <= 0:
            return []                                        # no thrust, or the average isn't going with it
        first = outside[0]
        peak = int(np.argmax(high[first:]) + first) if long else int(np.argmin(low[first:]) + first)
        bars_back = n - 1 - peak
        if not self.params["min_bars"] <= bars_back <= self.params["max_bars"]:
            return []
        extreme = float(high[peak] if long else low[peak])
        base = float(low[max(0, first - 10):first + 1].min() if long else high[max(0, first - 10):first + 1].max())
        leg = (extreme - base) * sign
        pull = d.iloc[peak + 1:]
        pull_end = float(pull["low"].min() if long else pull["high"].max())
        retrace = (extreme - pull_end) * sign / leg if leg > 0 else 9.0
        if leg < 2.0 * atr or retrace > self.params["max_retrace"]:
            return []                                        # no leg to join, or the pullback gave too much back
        if (pull_end - e[-1]) * sign > self.params["near_ema_atr"] * atr:
            return []                                        # still stretched: it hasn't pulled back to value
        last, prev = d.iloc[-1], d.iloc[-2]
        turned = float(last["close"]) > float(prev["high"]) if long else float(last["close"]) < float(prev["low"])
        if not turned:
            return []
        leg_ranges = (d["high"] - d["low"]).iloc[max(0, first - 3):peak + 1]
        quiet = float((pull["high"] - pull["low"]).mean()) < float(leg_ranges.mean())
        dry = float(pull["volume"].mean()) < float(d["volume"].iloc[max(0, first - 3):peak + 1].mean())
        trend = ctx.daily_trend()
        aligned = trend == ("up" if long else "down")
        entry = ctx.price
        stop = pull_end - sign * 0.2 * atr
        risk = (entry - stop) * sign
        if risk <= 0:
            return []
        t1 = extreme if (extreme - entry) * sign >= 1.6 * risk else entry + sign * 1.6 * risk
        t2 = entry + sign * max(2.8 * risk, leg)
        conf = 0.55 + 0.04 * aligned + 0.03 * quiet + 0.02 * dry
        detail = (
            f"{ctx.symbol} closed outside its Keltner channel {n - 1 - first} sessions ago - a {leg / atr:.1f}-ATR "
            f"{'up' if long else 'down'} leg - and has pulled back {retrace * 100:.0f}% of it over {bars_back} sessions to "
            f"{pull_end:.2f}, near the 20-EMA ({e[-1]:.2f})"
            + (", on narrower candles" if quiet else "") + (" and lighter volume" if dry else "")
            + f". The last candle closed back {'above' if long else 'below'} the one before it."
        )
        note = ("First objective is the leg's extreme; a pullback that goes flat near the average instead of "
                "turning is failing - scratch it.")
        inval = f"a close {'below' if long else 'above'} the pullback's extreme {pull_end:.2f}"
        return self._play(ctx, side, entry, stop, [t1, t2], conf,
                          f"pullback to the 20-EMA after a {leg / atr:.1f}-ATR thrust", detail,
                          {"thrust_extreme": round(extreme, 2), "pullback_extreme": round(pull_end, 2),
                           "retrace_pct": round(retrace * 100, 1), "ema20": round(float(e[-1]), 2),
                           "quiet": bool(quiet), "dry_volume": bool(dry), "daily_trend": trend}, note, inval)


# --------------------------------------------------------------------------- #
@register
class DoubleBottomTop(_DailyPattern):
    key = "double_bottom"
    style = "momentum"              # it is entered on the confirming breakout, not at the second low
    expected_hold = (6.0, 20.0)
    title = "Confirmed Double Bottom / Top"
    book_tags = ["pattern", "double-bottom"]
    thesis = (
        "Two lows at about the same price, weeks apart, with a real rally between them - and then a "
        "close above that rally's peak. Until that close it is only two lows: Bulkowski counts most "
        "twin bottoms failing before confirmation. After it, the pattern's height projected upward is "
        "reached about two times in three; about half throw back to the breakout price first, so the "
        "stop sits inside the pattern, not at the breakout. The double top is its mirror, and weaker."
    )
    default_params = {"window": 70, "min_apart": 10, "max_apart": 35, "max_variation_pct": 4.0,
                      "min_height_pct": 10.0, "fresh_bars": 3}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        got = self._frame(ctx, 90)
        if got is None:
            return []
        d, atr = got
        d = d.tail(int(self.params["window"]))
        if _ran_away(ctx, float(d["close"].iloc[-1]), atr):
            return []
        year = completed_daily(ctx).tail(252)
        for side in (Side.LONG, Side.SHORT):
            found = self._find(d, side)
            if found:
                return self._build(ctx, d, year, atr, side, *found)
        return []

    def _find(self, d: pd.DataFrame, side: Side) -> Optional[Tuple[int, int, float, float]]:
        """The latest pair of lows (highs) that makes the pattern, and the confirmation price: the
        highest high (lowest low) between them. None unless the last few closes confirmed it."""
        long = side is Side.LONG
        edge = (d["low"] if long else d["high"]).values
        other = (d["high"] if long else d["low"]).values
        close = d["close"].values
        sign = 1.0 if long else -1.0
        marks = pivots(edge, 3, lows=long)
        p = self.params
        for right in reversed(marks):
            for left in reversed([m for m in marks if p["min_apart"] <= right - m <= p["max_apart"]]):
                a, b = float(edge[left]), float(edge[right])
                outer = min(a, b) if long else max(a, b)
                if abs(a - b) / outer * 100.0 > p["max_variation_pct"]:
                    continue
                between = other[left:right + 1]
                confirm = float(between.max() if long else between.min())
                if (confirm - outer) * sign / outer * 100.0 < p["min_height_pct"]:
                    continue
                inside = edge[left:right + 1]
                if (inside.min() < outer - 1e-9) if long else (inside.max() > outer + 1e-9):
                    continue                                 # something between them went further: not twins
                after = close[right + 1:]
                beyond = np.nonzero((after - confirm) * sign > 0)[0]
                if len(beyond) == 0 or len(after) - beyond[0] > p["fresh_bars"]:
                    continue                                 # not confirmed yet - or confirmed too long ago
                if ((edge[right + 1:] - outer) * sign < 0).any():
                    continue                                 # a third, deeper low since: the pattern is gone
                return left, right, outer, confirm
        return None

    def _build(self, ctx, d, year, atr, side, left, right, outer, confirm) -> List[Play]:
        long = side is Side.LONG
        sign = 1.0 if long else -1.0
        height = (confirm - outer) * sign
        entry = ctx.price
        stop = confirm - sign * 0.4 * height
        risk = (entry - stop) * sign
        if risk <= 0 or (entry - confirm) * sign > 0.35 * height:
            return []                                        # already a third of the way to the target: too late
        hi, lo = float(year["high"].max()), float(year["low"].min())
        where = (confirm - lo) / (hi - lo) if hi > lo else 0.5
        near_extreme = where <= 0.34 if long else where >= 0.66     # Bulkowski: best near the yearly low (high)
        vol = d["volume"].values
        fading = float(vol[right - 2:right + 3].mean()) < float(vol[left - 2:left + 3].mean())
        conf = (0.57 if long else 0.54) + 0.03 * near_extreme + 0.02 * fading
        what = "bottoms" if long else "tops"
        detail = (
            f"Two {what} at {d[('low' if long else 'high')].iloc[left]:.2f} and "
            f"{d[('low' if long else 'high')].iloc[right]:.2f}, {right - left} sessions apart, around a "
            f"{'peak' if long else 'trough'} at {confirm:.2f} - a pattern {height / outer * 100:.0f}% tall. The close "
            f"{'above' if long else 'below'} {confirm:.2f} confirms it; the height projected from there gives "
            f"{confirm + sign * height:.2f}"
            + (f", and the breakout is in the {'lower' if long else 'upper'} third of the year's range" if near_extreme else "")
            + "."
        )
        note = ("About half of these throw back to the breakout price before going on - that is not failure. "
                "A close back inside the upper part of the pattern is.")
        inval = f"a close {'below' if long else 'above'} {stop:.2f}, back inside the pattern"
        targets = [confirm + sign * 0.8 * height, confirm + sign * 1.3 * height]
        return self._play(ctx, side, entry, stop, targets, conf,
                          f"double {'bottom' if long else 'top'} confirmed at {confirm:.2f}", detail,
                          {"left": round(float(d[('low' if long else 'high')].iloc[left]), 2),
                           "right": round(float(d[('low' if long else 'high')].iloc[right]), 2),
                           "confirmation": round(confirm, 2), "height_pct": round(height / outer * 100, 1),
                           "sessions_apart": int(right - left), "near_yearly_extreme": bool(near_extreme),
                           "volume_fading": bool(fading)}, note, inval)


def describe_patterns() -> Dict[str, Any]:
    """The pattern setups and the books behind them, for the docs."""
    return {cls.key: cls.title for cls in (FailureTest, TrendPullback, DoubleBottomTop)}
