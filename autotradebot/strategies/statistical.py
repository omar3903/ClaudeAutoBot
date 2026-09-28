"""Setups from the statistics of prices rather than chart patterns (Chan, *Algorithmic Trading*).

Both are day trades that act on today's opening gap, measured against the stock's own
history of moves so a 2% gap means something different in a quiet utility and a
biotech:

- **Gap reversion** (ch. 4, "buy-on-gap"): a gap beyond yesterday's range by more than a
  standard deviation of daily returns, against the 20-day trend, tends to be partly won
  back during the day.
- **Post-earnings drift** (ch. 7): after an earnings report released overnight, a gap of
  more than half a standard deviation of the stock's usual overnight moves tends to keep
  going the same way through the day.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any, List, Optional

import numpy as np
import pandas as pd

from ..core.enums import Side, StrategyKind, Timeframe
from ..core.models import Play
from ..quant.readings import completed_daily
from ..signals.calendar import report_before_open, surprise
from ..util import clock
from .base import Strategy, StrategyContext
from .registry import register

NY = "America/New_York"
EARNINGS_ITEM = "2.02"


def _opening(ctx: StrategyContext, max_minutes: float, min_daily: int):
    """Today's candles and the completed daily candles before today, while the setup's
    window after the open is still running."""
    today = ctx.today_intraday()
    if today is None or len(today) < 2 or ctx.minutes_since_open > max_minutes:
        return None, None
    prior = completed_daily(ctx.daily, today.index[0].date())
    if len(prior) < min_daily:
        return None, None
    return today, prior


def earnings_filing(signals: Any, day: dt.date, open_at: pd.Timestamp) -> Optional[dict]:
    """The earnings filing (8-K item 2.02) SEC accepted between the previous session's close
    and today's open, if the stock's signals hold one."""
    prev = clock.prev_trading_day(day)
    since = pd.Timestamp(dt.datetime.combine(prev, clock.regular_close_time(prev)), tz=NY)
    for filing in getattr(signals, "filings", None) or []:
        if EARNINGS_ITEM not in {c.strip() for c in str(filing.get("items") or "").split(",")}:
            continue
        try:
            at = pd.Timestamp(filing.get("published_at"))
        except (TypeError, ValueError):
            continue
        at = (at.tz_localize("UTC") if at.tzinfo is None else at).tz_convert(NY)
        if since <= at < open_at:
            return filing
    return None


@register
class GapReversion(Strategy):
    key = "gap_reversion"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.INTRADAY
    style = "reversal"
    tod_profile = "reversal"
    enabled_by_default = True
    expected_hold = (90.0, 330.0)
    title = "Gap reversion"
    thesis = ("Chan's buy-on-gap model (Algorithmic Trading, ch. 4). A stock that opens below yesterday's low by "
              "more than one standard deviation of its daily returns, while still above its 20-day average, has "
              "usually been hit by a wave of selling that runs out - and wins back part of the gap during the day. "
              "Shorts mirror it: an open above yesterday's high by more than a standard deviation, under the 20-day "
              "average. It is taken in the first half hour while the price is still outside yesterday's range, "
              "with the stop beyond the day's extreme so far, the first target at yesterday's low (high) and the "
              "second at yesterday's close. Chan holds to the close; the exit manager flattens before the bell.")
    default_params = {"sigma_days": 90, "ma_days": 20, "max_minutes": 30, "min_gap_sigmas": 1.0, "stop_atr": 0.25}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        p = self.params
        today, prior = _opening(ctx, p["max_minutes"], p["ma_days"] + 5)
        atr, price = ctx.intraday_atr, ctx.price
        if today is None or math.isnan(atr) or atr <= 0:
            return []
        closes = prior["close"].to_numpy(dtype=float)
        sigma = float(np.std(np.diff(np.log(closes))[-int(p["sigma_days"]):]))
        ma = float(np.mean(closes[-int(p["ma_days"]):]))
        low, high, close = (float(prior[k].iloc[-1]) for k in ("low", "high", "close"))
        day_open = float(today["open"].iloc[0])
        if sigma <= 0:
            return []
        threshold = p["min_gap_sigmas"] * sigma
        if day_open < low * (1 - threshold) and day_open > ma and price < low:
            gap_sigmas = (1 - day_open / low) / sigma
            stop = float(today["low"].min()) - p["stop_atr"] * atr
            targets = [low] + ([close] if close > low else [])
            return self._wrap(ctx, Side.LONG, price, stop, targets, gap_sigmas, sigma, ma, "below yesterday's low", low)
        if day_open > high * (1 + threshold) and day_open < ma and price > high:
            gap_sigmas = (day_open / high - 1) / sigma
            stop = float(today["high"].max()) + p["stop_atr"] * atr
            targets = [high] + ([close] if close < high else [])
            return self._wrap(ctx, Side.SHORT, price, stop, targets, gap_sigmas, sigma, ma, "above yesterday's high", high)
        return []

    def _wrap(self, ctx, side, entry, stop, targets, gap_sigmas, sigma, ma, where, level) -> List[Play]:
        long = side is Side.LONG
        play = self._mk_play(
            ctx, side, entry, stop, targets, confidence=min(0.72, 0.5 + 0.06 * min(3.0, gap_sigmas - 1.0)),
            rationale=f"opened {gap_sigmas:.1f} standard deviations {where}, {'above' if long else 'below'} its 20-day average",
            detail=(f"{ctx.symbol} opened {gap_sigmas:.1f} standard deviations (its daily moves are "
                    f"{100 * sigma:.1f}%) {where} {level:.2f}, while its 20-day average of {ma:.2f} says the "
                    f"trend is still {'up' if long else 'down'} - the gap is more likely to be partly won back "
                    "than to keep going."),
            evidence={"gap_sigmas": round(gap_sigmas, 2), "daily_sigma_pct": round(100 * sigma, 2),
                      "ma20": round(ma, 2), "prior_extreme": round(level, 4)},
            tags=["intraday", "gap", "reversal"],
            invalidation=f"a 5-minute close {'below' if long else 'above'} {stop:.2f}, a new extreme for the day")
        return [play] if play else []


@register
class EarningsDrift(Strategy):
    key = "earnings_drift"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.INTRADAY
    style = "momentum"
    tod_profile = "momentum"
    enabled_by_default = True
    expected_hold = (120.0, 360.0)
    title = "Post-earnings drift"
    thesis = ("Prices keep drifting the way an earnings surprise pushed them - one of the longest-lived anomalies "
              "in finance. Chan's intraday version (Algorithmic Trading, ch. 7): when a company reports after the "
              "previous close or before today's open (its SEC 8-K with item 2.02) and the stock opens more than half "
              "a standard deviation of its usual overnight moves away from yesterday's close, trade in the "
              "direction of the gap and hold to the close. It is taken in the first half hour while the price still "
              "holds beyond yesterday's close, with the stop beyond the day's extreme so far and a target at twice "
              "the risk; the exit manager flattens what's left before the bell. A report counts from SEC's filing "
              "or Finnhub's earnings calendar, whichever knows first; when the calendar has the reported EPS, a gap "
              "the other way from the surprise isn't taken (Chan, Quantitative Trading: buy the beats, short the "
              "misses).")
    default_params = {"sigma_days": 90, "min_gap_sigmas": 0.5, "max_minutes": 30, "target_r": 2.0, "stop_atr": 0.1}

    def generate(self, ctx: StrategyContext) -> List[Play]:
        p = self.params
        if ctx.signals is None and not ctx.earnings:
            return []
        today, prior = _opening(ctx, p["max_minutes"], 30)
        atr, price = ctx.intraday_atr, ctx.price
        if today is None or math.isnan(atr) or atr <= 0:
            return []
        filing = earnings_filing(ctx.signals, today.index[0].date(), today.index[0])
        report = report_before_open(ctx.earnings, today.index[0].date())
        if filing is None and report is None:
            return []
        eps_surprise = surprise(report) if report else None
        opens, closes = prior["open"].to_numpy(dtype=float), prior["close"].to_numpy(dtype=float)
        sigma = float(np.std(np.log(opens[1:] / closes[:-1])[-int(p["sigma_days"]):]))
        day_open, close = float(today["open"].iloc[0]), float(closes[-1])
        if sigma <= 0 or day_open <= 0 or close <= 0:
            return []
        gap = math.log(day_open / close)
        gap_sigmas = abs(gap) / sigma
        if gap_sigmas < p["min_gap_sigmas"]:
            return []
        long = gap > 0
        if (price <= close) if long else (price >= close):
            return []                                   # the gap has already been given back
        if eps_surprise is not None and eps_surprise != 0 and (eps_surprise > 0) != long:
            return []                                   # the market and the reported numbers disagree
        stop = (float(today["low"].min()) - p["stop_atr"] * atr) if long else (float(today["high"].max()) + p["stop_atr"] * atr)
        target = price + p["target_r"] * (price - stop)
        side = Side.LONG if long else Side.SHORT
        play = self._mk_play(
            ctx, side, price, stop, [target], confidence=min(0.75, 0.52 + 0.05 * min(3.0, gap_sigmas)),
            rationale=f"earnings overnight, gapped {100 * gap:+.1f}% ({gap_sigmas:.1f} overnight standard deviations)",
            detail=(f"{ctx.symbol} reported earnings before today's open"
                    + (f" (EPS {100 * eps_surprise:+.0f}% against the estimate)" if eps_surprise is not None else "")
                    + f" and gapped {100 * gap:+.1f}% - "
                    f"{gap_sigmas:.1f} times its usual overnight move of {100 * sigma:.1f}%. Prices tend to keep "
                    "drifting the way an earnings surprise pushed them."),
            evidence={"gap_pct": round(100 * gap, 2), "gap_sigmas": round(gap_sigmas, 2),
                      "overnight_sigma_pct": round(100 * sigma, 2),
                      "earnings_source": "SEC 8-K" if filing else "Finnhub calendar",
                      "earnings_filed_at": str(filing.get("published_at")) if filing else f"{report['date']} {report.get('hour') or ''}".strip(),
                      **({"eps_actual": report.get("eps_actual"), "eps_estimate": report.get("eps_estimate"),
                          "eps_surprise_pct": round(100 * eps_surprise, 1)} if eps_surprise is not None else {})},
            tags=["intraday", "gap", "catalyst", "earnings"],
            invalidation=f"a 5-minute close back {'below' if long else 'above'} yesterday's close of {close:.2f}")
        return [play] if play else []
