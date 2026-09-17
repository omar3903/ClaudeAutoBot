"""Running the strategies on one symbol.

Builds the symbol's context once - the stored daily candles with today's
partial candle appended from the intraday bars, the latest price, today's
activity, the market's regime - and runs every given strategy on it. Indicators
are computed once per context and shared by the strategies (see StrategyContext).
Each play leaves with its expected value, its noise flags (see noise.py), the
quantitative readings behind them and its rank.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pandas as pd

from ..core.enums import Timeframe
from ..core.models import Play
from ..data.fundamentals import Financials
from ..data.market_data import quote_from_price
from ..strategies.base import Strategy, StrategyContext
from ..signals.book import SignalBook
from ..research.features import activity_summary
from .filters import expected_r, rank_score
from ..signals.calendar import next_report, sessions_until
from .noise import NoiseSettings, context_flags

log = logging.getLogger(__name__)

#: how long a reversal setup is expected to take, from its price's half-life
HOLD_MINUTES = (10.0, 180.0)
HOLD_DAYS = (1.0, 15.0)
SESSION_MINUTES = 390.0


def with_today(daily: pd.DataFrame, intraday: Optional[pd.DataFrame]) -> pd.DataFrame:
    """The stored daily candles plus today's candle built from the intraday bars."""
    if intraday is None or not len(intraday):
        return daily
    day = intraday.index[-1].date()
    if len(daily) and daily.index[-1].date() >= day:
        return daily
    today = intraday[intraday.index.date == day]
    bar = pd.DataFrame(
        {"open": [today["open"].iloc[0]], "high": [today["high"].max()], "low": [today["low"].min()],
         "close": [today["close"].iloc[-1]], "volume": [today["volume"].sum()]},
        index=pd.DatetimeIndex([pd.Timestamp(day).tz_localize(daily.index.tz or "America/New_York")]),
    )
    return pd.concat([daily, bar])


def evaluate(symbol: str, strategies: Sequence[Strategy], daily: pd.DataFrame,
             intraday: Optional[pd.DataFrame], *, run_id: str, equity: float, params: Dict[str, Any],
             activity: Any = None, fundamentals: Optional[Financials] = None,
             peers: Optional[List[Financials]] = None, noise: Optional[NoiseSettings] = None,
             signals: Optional[SignalBook] = None, market: Optional[Mapping[str, Any]] = None,
             evidence_weights: Optional[Mapping[str, float]] = None,
             benchmark: Optional[pd.Series] = None,
             records: Optional[Mapping[str, Mapping[str, Any]]] = None,
             premarket: Optional[Mapping[str, Any]] = None) -> List[Play]:
    """``evidence_weights``: each strategy's evidence multiplier (see research/weights.py). ``benchmark``:
    the S&P 500 ETF's closes, for the market model. ``records``: each strategy's pooled win rate and
    trade count (research/weights.py pooled_odds), which calibrate the odds its plays state.
    ``premarket``: what the gap check saw for the stock today (scanner/heat.py GapperMetrics)."""
    noise = noise or NoiseSettings()
    full_daily = with_today(daily, intraday)
    latest = intraday if intraday is not None and len(intraday) else full_daily
    ctx = StrategyContext(
        symbol=symbol, intraday=intraday, daily=full_daily,
        quote=quote_from_price(symbol, float(latest["close"].iloc[-1]), float(latest["volume"].iloc[-1])),
        fundamentals=fundamentals, peers=peers, params=params, account_equity=equity,
        activity=asdict(activity) if is_dataclass(activity) else {},
        signals=signals.get(symbol) if signals is not None else None, market=dict(market or {}),
        benchmark=benchmark, news=signals.news_reading(symbol) if signals is not None else None,
        earnings=signals.earnings_for(symbol) if signals is not None else None,
        records={k: dict(v) for k, v in (records or {}).items()}, premarket=dict(premarket or {}),
    )
    plays: List[Play] = []
    for strategy in strategies:
        try:
            for p in strategy.generate(ctx):
                p.scan_run_id = run_id
                p.evidence["expected_r"] = round(expected_r(p, ctx.daily_atr), 3)
                p.noise = context_flags(p, ctx, strategy.style, noise)
                if p.evidence["expected_r"] < noise.min_expected_r:
                    p.noise.append("low_expected_value")
                add_readings(p, ctx, strategy.style, noise)
                multiplier = float((evidence_weights or {}).get(strategy.key, 1.0))
                if abs(multiplier - 1.0) > 1e-9:
                    p.evidence["evidence_weight"] = round(multiplier, 3)
                p.score = rank_score(p, activity, strategy.weight * multiplier)
                if signals is not None:
                    signals.apply(p)
                p.evidence.setdefault("spark", ctx.spark())
                if activity is not None:
                    p.evidence.setdefault("activity", activity_summary(activity))
                plays.append(p)
        except Exception as e:  # noqa: BLE001
            log.debug("%s %s failed: %s", symbol, strategy.key, e)
    return plays


def add_readings(p: Play, ctx: StrategyContext, style: str, noise: Optional[NoiseSettings] = None) -> None:
    """Write the quantitative readings into the play's evidence, where the dashboard shows
    them and the journal keeps them. A reversal setup is expected to take about as long as
    its price takes to halve a deviation from the mean (Chan, *Algorithmic Trading* ch. 2)."""
    intraday = p.timeframe is Timeframe.INTRADAY
    character = ctx.price_character(intraday)
    if character is not None:
        p.evidence["price_character"] = character
    vol = ctx.vol_forecast()
    if vol is not None:
        p.evidence["vol_forecast"] = vol
    if ctx.market:
        p.evidence["market_regime"] = dict(ctx.market)
    noise = noise or NoiseSettings()
    move = ctx.abnormal_move(noise.market_model_sessions)
    if move is not None:
        p.evidence["market_move"] = {**move, "news": ctx.news_since_move(noise.news_fresh_minutes)}
    upcoming = next_report(ctx.earnings, ctx.now)
    if upcoming is not None:
        p.evidence["next_earnings"] = {**upcoming, "sessions": sessions_until(upcoming, ctx.now)}
    life = (character or {}).get("half_life_bars")
    if style != "reversal" or not life or character["character"] != "mean reverting":
        return                          # a random walk's half-life is only noise in the estimate
    low, high = HOLD_MINUTES if intraday else HOLD_DAYS
    typical = min(high, max(low, life * (5.0 if intraday else 1.0)))
    p.expected_hold_typical = round(typical, 1)
    p.expected_hold_max = round(min(SESSION_MINUTES if intraday else high * 2, typical * 2), 1)
    p.evidence["hold_from_half_life"] = True
