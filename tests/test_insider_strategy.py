"""The insider-buying swing setup (made-up stock and candles)."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from tos_bot.core.enums import Side
from tos_bot.core.models import Quote
from tos_bot.signals.book import SymbolSignals
from tos_bot.signals.insiders import InsiderSignal
from tos_bot.strategies.base import StrategyContext
from tos_bot.strategies.insider import InsiderBuying
from tos_bot.util import clock

NOW = dt.datetime(2026, 9, 15, 10, 30, tzinfo=clock.NY)


def _daily(last=20.0, days=60):
    index = pd.date_range(end="2026-09-14", periods=days, freq="B", tz=clock.NY)
    close = np.linspace(last * 1.1, last, days)
    return pd.DataFrame({"open": close, "high": close + 0.4, "low": close - 0.4, "close": close,
                         "volume": np.full(days, 1_000_000.0)}, index=index)


def _ctx(buying, last=20.0):
    return StrategyContext(symbol="EXH", intraday=None, daily=_daily(last), now=NOW,
                           quote=Quote(symbol="EXH", bid=last, ask=last, last=last),
                           signals=SymbolSignals("EXH", buying=buying))


def _buying(score=0.8, unusual=True, days_ago=3, paid=19.5):
    day = NOW.date() - dt.timedelta(days=days_ago)
    return InsiderSignal(symbol="EXH", direction="buying", score=score, unusual=unusual, insiders=2, trades=3,
                         value=paid * 40_000, first_date=day, last_date=day, top_role="ceo_cfo",
                         reasons=["2 insiders bought $780K in the open market on the same day, led by the CEO"],
                         shares=40_000.0)


def test_unusual_insider_buying_becomes_a_swing_buy_near_their_price():
    [play] = InsiderBuying().generate(_ctx(_buying()))
    assert (play.symbol, play.side, play.strategy, play.timeframe.value) == ("EXH", Side.LONG, "insider_buying", "SWING")
    assert play.stop < play.entry < play.targets[0]
    assert play.reward_risk >= 2.5
    assert play.evidence["insider_score"] == 0.8 and play.evidence["insider_avg_price"] == 19.5
    assert "2 insiders bought" in play.explanation and "+2.6% from their average price of 19.50" in play.explanation


def test_no_play_without_unusual_recent_buying_or_once_the_stock_has_run():
    setup = InsiderBuying()
    assert setup.generate(_ctx(None)) == []
    assert setup.generate(_ctx(_buying(unusual=False))) == []
    assert setup.generate(_ctx(_buying(days_ago=15))) == []
    assert setup.generate(_ctx(_buying(paid=16.0))) == []          # 25% above what the insiders paid
