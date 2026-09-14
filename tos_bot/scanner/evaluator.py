"""Running the strategies on one symbol.

Builds the symbol's context once - the stored daily candles with today's
partial candle appended from the intraday bars, the latest price, today's
activity - and runs every given strategy on it. Indicators are computed once
per context and shared by the strategies (see StrategyContext).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from ..core.models import Play
from ..data.fundamentals import Financials
from ..data.market_data import quote_from_price
from ..strategies.base import Strategy, StrategyContext
from .filters import rank_score

log = logging.getLogger(__name__)


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
             peers: Optional[List[Financials]] = None) -> List[Play]:
    full_daily = with_today(daily, intraday)
    latest = intraday if intraday is not None and len(intraday) else full_daily
    ctx = StrategyContext(
        symbol=symbol, intraday=intraday, daily=full_daily,
        quote=quote_from_price(symbol, float(latest["close"].iloc[-1]), float(latest["volume"].iloc[-1])),
        fundamentals=fundamentals, peers=peers, params=params, account_equity=equity,
        activity=asdict(activity) if is_dataclass(activity) else {},
    )
    plays: List[Play] = []
    for strategy in strategies:
        try:
            for p in strategy.generate(ctx):
                p.scan_run_id = run_id
                p.score = rank_score(p, activity, strategy.weight)
                p.evidence.setdefault("spark", ctx.spark())
                plays.append(p)
        except Exception as e:  # noqa: BLE001
            log.debug("%s %s failed: %s", symbol, strategy.key, e)
    return plays
