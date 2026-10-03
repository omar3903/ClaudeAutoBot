"""Running the strategies on one symbol.

Builds the symbol's context once - the stored daily candles with today's
partial candle appended from the intraday bars, the latest price, today's
activity, the market's regime - and runs every given strategy on it. Indicators
are computed once per context and shared by the strategies (see StrategyContext).
Each play leaves with its expected value, its noise flags (see noise.py), the
quantitative readings behind them and its rank.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
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
from .schedule import last_completed_session
from ..util import clock

log = logging.getLogger(__name__)

#: how long a reversal setup is expected to take, from its price's half-life
HOLD_MINUTES = (10.0, 180.0)
HOLD_DAYS = (1.0, 15.0)
SESSION_MINUTES = 390.0

#: the session each setup last failed in: its first failure of a session is a warning with the traceback, the
#: rest are DEBUG lines - a broken setup is seen without a traceback for every stock it runs on. _NO_PREV_CLOSE
#: keeps the session a stock last went without day setups for want of yesterday's close the same way
_failed_in: Dict[str, dt.date] = {}
_failed_lock = threading.Lock()
_NO_PREV_CLOSE = "(no prior close)"


def with_today(daily: pd.DataFrame, intraday: Optional[pd.DataFrame]) -> pd.DataFrame:
    """The stored daily candles plus today's candle built from the intraday bars. When the store stops short of
    the session before today's - the last one the bars hold (last_session, stale_daily) - that session's candle is
    built from the intraday bars too, so the candle before today's - the close the setups read as yesterday's - is
    never an older one. When the bars hold no session before today's, today's candle follows the store's last as
    before; evaluate() runs no day setups on a stock whose candles miss the last session (prev_close_known)."""
    if intraday is None or not len(intraday):
        return daily
    day = intraday.index[-1].date()
    if len(daily) and daily.index[-1].date() >= day:
        return daily
    tz = daily.index.tz or "America/New_York"
    candles = [daily]
    last = last_session(day, intraday)
    if stale_daily(daily, day, last=last):
        candles.append(_session_candle(intraday, last, tz))
    candles.append(_session_candle(intraday, day, tz))
    return pd.concat([c for c in candles if c is not None])


def _session_candle(intraday: pd.DataFrame, day: dt.date, tz: Any) -> Optional[pd.DataFrame]:
    """``day``'s daily candle built from its 5-minute bars in ``intraday`` - None when they hold none of it."""
    bars = intraday[intraday.index.date == day]
    if not len(bars):
        return None
    return pd.DataFrame(
        {"open": [bars["open"].iloc[0]], "high": [bars["high"].max()], "low": [bars["low"].min()],
         "close": [bars["close"].iloc[-1]], "volume": [bars["volume"].sum()]},
        index=pd.DatetimeIndex([pd.Timestamp(day).tz_localize(tz)]),
    )


def last_session(day: dt.date, *frames: Any) -> dt.date:
    """The session before ``day`` as the candles have it: the latest date before ``day`` in those of ``frames`` (a
    stock's 5-minute bars, the S&P 500 ETF's closes) that reach ``day``. A weekday the market was shut has no
    bars, so a closure the holiday calendar doesn't list (clock._HOLIDAYS holds the scheduled ones, through 2028)
    isn't taken for a session the store missed - every stock would lose its day setups for the session after it.
    The calendar's (clock.prev_trading_day) when none of them reaches ``day`` with a date before it: candles that
    stop short of ``day`` can't say what came after them."""
    found: Optional[dt.date] = None
    for frame in frames:
        if frame is None or not len(frame) or frame.index[-1].date() < day:
            continue
        index = frame.index
        midnight = pd.Timestamp(day)
        at = index.searchsorted(midnight.tz_localize(index.tz) if index.tz is not None else midnight)
        if at:
            seen = index[at - 1].date()
            found = seen if found is None else max(found, seen)
    return found or clock.prev_trading_day(day)


def stale_daily(daily: Optional[pd.DataFrame], day: dt.date, *frames: Any, last: Optional[dt.date] = None) -> bool:
    """Whether ``daily``'s candles before ``day`` stop short of the session before it - ``last``, or the one
    ``frames`` show (last_session). The morning's ranking lets a stock's store miss a session (scanner.py _rank)
    and a download can fail, so during the session a store can end two sessions back or more - and its last close
    isn't yesterday's."""
    if daily is None or not len(daily):
        return True
    stored = daily.index[-1].date()
    if stored >= day:                               # the store holds ``day`` already: the candle before it counts
        before = daily.index[daily.index.date < day]
        if not len(before):
            return True
        stored = before[-1].date()
    return stored < (last or last_session(day, *frames))


def prev_close_known(daily: Optional[pd.DataFrame], intraday: Optional[pd.DataFrame], day: dt.date,
                     *frames: Any) -> bool:
    """Whether the close of the session before ``day`` is at hand: in the daily store, or in the 5-minute bars that
    with_today builds the store's missing session from. That session is the last one ``intraday`` and ``frames``
    (the S&P 500 ETF's candles) show (last_session). evaluate() runs no day setups on a stock without it - a gap
    or a prior close read off an older session would be made up - and the live scan admits no such stock
    (engine/engine.py _live_scan_once)."""
    last = last_session(day, intraday, *frames)
    if not stale_daily(daily, day, last=last):
        return True
    return intraday is not None and bool(len(intraday)) and bool((intraday.index.date == last).any())


def _first_today(key: str) -> bool:
    """Whether ``key`` - a setup that failed, or _NO_PREV_CLOSE - comes up for the first time this session (and
    marks it): its first time is logged louder than the rest."""
    session = clock.now_ny().date()
    with _failed_lock:
        first = _failed_in.get(key) != session
        _failed_in[key] = session
    return first


def closed_bar_at(intraday: Optional[pd.DataFrame]) -> Optional[str]:
    """When the last closed 5-minute candle started - the one the day setups trigger on, since the newest
    is still printing (strategies/technical.py _last_closed) - as ISO text a play's evidence can keep.
    The board counts a day play's confirmations by it (engine/board.py)."""
    if intraday is None or not len(intraday):
        return None
    return pd.Timestamp(intraday.index[-2 if len(intraday) >= 2 else -1]).isoformat()


ADV_SESSIONS = 20   # sessions behind a stock's usual daily volume


def median_volume(daily: pd.DataFrame, sessions: int = ADV_SESSIONS, now=None) -> Optional[float]:
    """A stock's usual daily volume in shares: the median of its last ``sessions`` completed sessions.
    Today's candle counts only once the session has closed, and the median shrugs off a one-off spike
    day. None when the stock has no daily history, or none with volume. The sizer caps an order at a
    slice of it (risk/position_sizing.py)."""
    if daily is None or not len(daily) or "volume" not in daily:
        return None
    final = last_completed_session(now or clock.now_ny())
    done = daily[daily.index.date <= final]["volume"].dropna().tail(sessions)
    median = float(done.median()) if len(done) else 0.0
    return median if median > 0 else None      # no volume is missing data, not a stock nobody trades


def evaluate(symbol: str, strategies: Sequence[Strategy], daily: pd.DataFrame,
             intraday: Optional[pd.DataFrame], *, run_id: str, equity: float, params: Dict[str, Any],
             activity: Any = None, fundamentals: Optional[Financials] = None,
             peers: Optional[List[Financials]] = None, noise: Optional[NoiseSettings] = None,
             signals: Optional[SignalBook] = None, market: Optional[Mapping[str, Any]] = None,
             evidence_weights: Optional[Mapping[str, float]] = None,
             benchmark: Optional[pd.Series] = None,
             records: Optional[Mapping[str, Mapping[str, Any]]] = None,
             premarket: Optional[Mapping[str, Any]] = None,
             failures: Optional[Dict[str, int]] = None) -> List[Play]:
    """``evidence_weights``: each strategy's evidence multiplier (see research/weights.py). ``benchmark``:
    the S&P 500 ETF's closes, for the market model. ``records``: each strategy's pooled win rate and
    trade count (research/weights.py pooled_odds), which calibrate the odds its plays state.
    ``premarket``: what the gap check saw for the stock today (scanner/heat.py GapperMetrics).
    ``failures``: a strategy that raises adds one to its count here (the scan's ScanResult.strategy_errors);
    the other strategies still run."""
    noise = noise or NoiseSettings()
    full_daily = with_today(daily, intraday)
    if (intraday is not None and len(intraday)
            and not prev_close_known(daily, intraday, intraday.index[-1].date(), benchmark)):
        if _first_today(_NO_PREV_CLOSE):        # a session where every stock goes without them is seen
            log.info("%s: neither its daily candles nor its 5-minute bars reach the last session - no day setups "
                     "(further stocks like it today are logged at DEBUG)", symbol)
        else:
            log.debug("%s: neither its daily candles nor its 5-minute bars reach the last session - no day setups",
                      symbol)
        strategies = [s for s in strategies if s.timeframe is not Timeframe.INTRADAY]
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
    bar_at = closed_bar_at(intraday)
    adv = median_volume(daily)
    for strategy in strategies:
        try:
            for p in strategy.generate(ctx):
                p.scan_run_id = run_id
                if bar_at is not None and p.timeframe is Timeframe.INTRADAY:
                    p.evidence["bar_at"] = bar_at          # the board counts a day play's confirmations by it
                if adv is not None:
                    p.evidence["adv_shares"] = round(adv)   # the sizer caps the order at a slice of it
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
        except Exception as e:  # noqa: BLE001 - one setup's bug, not the scan
            if failures is not None:
                failures[strategy.key] = failures.get(strategy.key, 0) + 1
            if _first_today(strategy.key):
                log.warning("%s %s failed: %s - its further failures today are logged at DEBUG", symbol,
                            strategy.key, e, exc_info=True)
            else:
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
    hold_from_half_life(p, ctx, style)


def hold_from_half_life(p: Play, ctx: StrategyContext, style: str) -> None:
    """A reversal setup is expected to take about as long as its price takes to halve a deviation
    from the mean (Chan ch. 2): its hold is set from the half-life when the price reads mean
    reverting. The replay calls it too, so the day-trade time stop there uses the window live uses."""
    intraday = p.timeframe is Timeframe.INTRADAY
    character = ctx.price_character(intraday)
    life = (character or {}).get("half_life_bars")
    if style != "reversal" or not life or character["character"] != "mean reverting":
        return                          # a random walk's half-life is only noise in the estimate
    low, high = HOLD_MINUTES if intraday else HOLD_DAYS
    typical = min(high, max(low, life * (5.0 if intraday else 1.0)))
    p.expected_hold_typical = round(typical, 1)
    p.expected_hold_max = round(min(SESSION_MINUTES if intraday else high * 2, typical * 2), 1)
    p.evidence["hold_from_half_life"] = True
