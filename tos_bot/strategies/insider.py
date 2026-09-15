"""Setups driven by signals from outside the price chart (see tos_bot/signals)."""

from __future__ import annotations

import math
from typing import List

from ..core.enums import Side, StrategyKind, Timeframe
from ..core.models import Play
from ..signals.insiders import money
from .base import Strategy, StrategyContext
from .registry import register


@register
class InsiderBuying(Strategy):
    key = "insider_buying"
    kind = StrategyKind.TECHNICAL
    timeframe = Timeframe.SWING
    style = "value"                   # insiders often buy weakness, so the trend checks don't apply
    tod_profile = "swing"
    title = "Insider buying"
    thesis = ("Company insiders buy their own stock in the open market for one reason: they expect it to rise. "
              "Studies of Form 4 filings find that buying by insiders who rarely trade - opportunistic rather "
              "than routine - is followed by better-than-market returns over the next months, most clearly in "
              "smaller companies. The setup takes unusual buying (a senior insider, a large stake, several "
              "insiders at once, outside any 10b5-1 plan), enters near the insiders' own price, puts the stop "
              "under the recent swing low (at most two daily ranges away) and aims for three times the risk.")
    default_params = {"min_score": 0.55, "max_days_since": 10, "stop_atr": 2.0, "target_r": 3.0,
                      "max_above_insiders_pct": 15.0}
    expected_hold = (20.0, 60.0)

    def generate(self, ctx: StrategyContext) -> List[Play]:
        p = self.params
        buying = getattr(ctx.signals, "buying", None)
        if buying is None or not buying.unusual or buying.score < p["min_score"] or not ctx.enough_daily(20):
            return []
        days_since = (ctx.now.date() - buying.last_date).days
        price, atr, paid = ctx.price, ctx.daily_atr, buying.avg_price
        if days_since > p["max_days_since"] or price <= 0 or not atr or math.isnan(atr):
            return []
        if paid and price > paid * (1 + p["max_above_insiders_pct"] / 100):
            return []                  # the stock has already run well past what the insiders paid
        swing_low = float(ctx.daily["low"].iloc[-10:].min())
        stop = max(swing_low - 0.25 * atr, price - p["stop_atr"] * atr)
        # at least a daily range away - the play builder would widen it anyway, and the target is measured from it
        stop = min(stop, price - self.MIN_STOP_DAILY_ATR["SWING"] * atr)
        target = price + p["target_r"] * (price - stop)
        people = f"{buying.insiders} insider{'s' if buying.insiders != 1 else ''}"
        vs_paid = f", {100 * (price / paid - 1):+.1f}% from their average price of {paid:.2f}" if paid else ""
        evidence = {"insider_score": buying.score, "insider_value": round(buying.value), "insiders": buying.insiders,
                    "days_since_insider_buy": days_since, "stop_daily_ranges": round((price - stop) / atr, 2)}
        if paid:
            evidence["insider_avg_price"] = round(paid, 4)
        play = self._mk_play(
            ctx, Side.LONG, entry=price, stop=stop, targets=[target], confidence=0.45 + 0.4 * buying.score,
            rationale=f"Unusual insider buying: {money(buying.value)} by {people} (score {buying.score:.2f})",
            detail=(f"{'. '.join(buying.reasons)}. The last insider purchase was {days_since} day"
                    f"{'s' if days_since != 1 else ''} ago; the stock is at {price:.2f}{vs_paid}."),
            evidence=evidence, tags=["insider"], ttl_minutes=3 * 24 * 60,
            invalidation=f"a daily close below {stop:.2f}, under the recent swing low - the insiders' timing was early")
        return [play] if play else []
