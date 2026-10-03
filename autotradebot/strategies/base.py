"""Strategy contract, and the context object handed to every strategy.

A strategy inspects a :class:`StrategyContext` (candles, latest price, optional
fundamentals) and returns plays. It never places orders and never touches the
database. Indicators most strategies need - ATRs, VWAP, relative volume, the
support/resistance map - are computed once per context and shared.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import pandas as pd

from ..analysis import SupportResistance, find_levels
from ..core.enums import AssetClass, Side, StrategyKind, Timeframe
from ..core.models import Play, Quote
from ..data.fundamentals import Financials
from ..indicators import ta
from ..quant import market_model, readings
from ..signals.calendar import DURING, report_before_open
from ..util import clock


@dataclass
class StrategyContext:
    symbol: str
    intraday: Optional[pd.DataFrame]      # 5-minute OHLCV across recent sessions (None pre-market)
    daily: pd.DataFrame                   # daily OHLCV, today's partial candle last when trading
    quote: Quote
    now: dt.datetime = field(default_factory=clock.now_ny)
    fundamentals: Optional[Financials] = None
    peers: Optional[List[Financials]] = None
    params: Dict[str, Any] = field(default_factory=dict)
    account_equity: float = 0.0
    #: what the scanner measured for this symbol (rvol, gap_pct, atr_pct, ...)
    activity: Dict[str, Any] = field(default_factory=dict)
    #: what insiders and the news say about the stock (signals/book.py SymbolSignals), when known
    signals: Any = None
    #: the market's regime (engine/market_regime.py): {"p_turbulent": ..., "regime": ...}, when known
    market: Dict[str, Any] = field(default_factory=dict)
    #: the S&P 500 ETF's closes by session - today's latest price last while trading - for the market model
    benchmark: Optional[pd.Series] = None
    #: the stock's news as last read, {"checked_at", "stories"} - None when it isn't followed (signals/book.py)
    news: Optional[Dict[str, Any]] = None
    #: its earnings reports from the calendar (signals/calendar.py), when known
    earnings: Optional[List[Dict[str, Any]]] = None
    #: each strategy's record, {"trades", "win_rate"} pooled from the replay and real trades
    #: (research/weights.py pooled_odds) - it calibrates the odds a play states; {} in the replay
    records: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: what the gap check saw before the open - "high", "low", "gap_pct", "volume" (scanner/heat.py
    #: GapperMetrics.as_dict); {} when it hasn't run. The pre-market high and low are levels.
    premarket: Dict[str, Any] = field(default_factory=dict)
    #: series that are the same for every bar of a session - the session VWAP, the opening range -
    #: computed once and shared between the contexts of one session (the replay fills it; a live
    #: scan builds one context per stock, so it has nothing to share)
    shared: Dict[str, Any] = field(default_factory=dict, repr=False)
    _memo: Dict[str, Any] = field(default_factory=dict, repr=False)

    def _cached(self, key: str, compute: Callable[[], Any]) -> Any:
        if key not in self._memo:
            self._memo[key] = compute()
        return self._memo[key]

    # ---- the quantitative readings (see quant/readings.py) ------------------ #
    def price_character(self, intraday: bool) -> Optional[Dict[str, Any]]:
        """Trending, mean reverting or a random walk: read on the last sessions' 5-minute
        closes for a day trade, on the daily closes for a swing trade."""
        def compute():
            frame, bars = (self.intraday, readings.INTRADAY_BARS) if intraday else (self.daily, readings.DAILY_BARS)
            if frame is None or len(frame) < readings.MIN_BARS:
                return None
            return readings.price_character(frame["close"].to_numpy()[-bars:])
        return self._cached("character_5m" if intraday else "character_1d", compute)

    def abnormal_move(self, sessions: int = market_model.SESSIONS) -> Optional[Dict[str, Any]]:
        """The latest session's move beyond what the market explains (quant/market_model.py)."""
        if self.benchmark is None or self.daily is None or not len(self.daily):
            return None
        return self._cached(f"abnormal_{sessions}",
                            lambda: market_model.abnormal_move(self.daily["close"], self.benchmark, sessions))

    def news_since_move(self, fresh_minutes: float = 60.0) -> Optional[int]:
        """The stories out since the close before the latest session - an earnings report around it
        counts as one. None when the stock's news isn't being read, or wasn't lately."""
        reading = self.news or {}
        checked, now = reading.get("checked_at"), pd.Timestamp(self.now)
        now = now.tz_localize("America/New_York") if now.tzinfo is None else now
        if checked is None or self.daily is None or not len(self.daily):
            return None
        checked = pd.Timestamp(checked)
        if (now - (checked.tz_localize("UTC") if checked.tzinfo is None else checked)).total_seconds() > fresh_minutes * 60:
            return None
        day = self.daily.index[-1].date()
        prev = clock.prev_trading_day(day)
        since = pd.Timestamp(dt.datetime.combine(prev, clock.regular_close_time(prev)), tz="America/New_York")
        count = 0
        for story in reading.get("stories") or []:
            try:
                at = pd.Timestamp(story["at"])
            except (KeyError, TypeError, ValueError):
                continue
            if since <= (at.tz_localize("UTC") if at.tzinfo is None else at) <= now:
                count += 1
        reported = report_before_open(self.earnings, day) or next(
            (e for e in self.earnings or [] if e["date"] == day.isoformat() and e.get("hour") == DURING), None)
        return count + (1 if reported else 0)

    def vol_forecast(self) -> Optional[Dict[str, Any]]:
        """Tomorrow's volatility from the completed daily candles (GARCH, or RiskMetrics)."""
        return self._cached("vol_forecast", lambda: readings.vol_forecast(self.symbol, self.daily, self.now.date()))

    # ---- price and time --------------------------------------------------- #
    @property
    def price(self) -> float:
        if self.quote and self.quote.last:
            return float(self.quote.last)
        if self.intraday is not None and len(self.intraday):
            return float(self.intraday["close"].iloc[-1])
        return float(self.daily["close"].iloc[-1])

    @property
    def minutes_since_open(self) -> float:
        return clock.minutes_since_open(self.now)

    def enough_intraday(self, bars: int = 20) -> bool:
        return self.intraday is not None and len(self.intraday) >= bars

    def enough_daily(self, bars: int = 60) -> bool:
        return self.daily is not None and len(self.daily) >= bars

    def today_intraday(self) -> Optional[pd.DataFrame]:
        def compute():
            if self.intraday is None or not len(self.intraday):
                return self.intraday
            return self.intraday[self.intraday.index.date == clock.session_date(self.now)]
        return self._cached("today", compute)

    def prev_close(self) -> float:
        if self.daily is None or len(self.daily) < 2:
            return self.price
        return float(self.daily["close"].iloc[-2])

    # ---- shared indicators ----------------------------------------------- #
    @property
    def intraday_atr(self) -> float:
        return self._cached("intraday_atr", lambda: safe_last(ta.atr(self.intraday, 14))
                            if self.enough_intraday(15) else math.nan)

    @property
    def daily_atr(self) -> float:
        return self._cached("daily_atr", lambda: safe_last(ta.atr(self.daily, 14))
                            if self.enough_daily(20) else math.nan)

    @property
    def daily_adx(self) -> pd.DataFrame:
        """ADX on the completed daily candles, shared by the swing setups that read the trend's strength."""
        return self._cached("daily_adx", lambda: ta.adx(completed_daily(self), 14))

    @property
    def vwap_series(self) -> pd.Series:
        """The session VWAP at each intraday bar. It only depends on the bar's own session, so a
        series shared for the session is sliced rather than recomputed."""
        def compute():
            full = self.shared.get("session_vwap")
            if full is not None:
                sliced = full.reindex(self.intraday.index)
                if not sliced.isna().any():
                    return sliced
            return ta.session_vwap(self.intraday)
        return self._cached("vwap", compute)

    def opening_range(self, minutes: int) -> pd.DataFrame:
        """Each session's opening-range high and low at every bar (indicators/ta.py opening_range),
        shared for the session when the replay has computed it."""
        def compute():
            full = self.shared.get(f"opening_range_{minutes}")
            if full is not None:
                sliced = full.reindex(self.intraday.index)
                if not sliced.isna().all().all():
                    return sliced
            return ta.opening_range(self.intraday, minutes)
        return self._cached(f"opening_range_{minutes}", compute)

    @property
    def vwap(self) -> float:
        return safe_last(self.vwap_series) if self.enough_intraday(1) else math.nan

    def rvol(self) -> float:
        def compute():
            known = self.activity.get("rvol")
            if known:
                return float(known)
            return ta.rel_volume_intraday(self.intraday) if self.enough_intraday(2) else 1.0
        return self._cached("rvol", compute)

    def levels(self) -> SupportResistance:
        def compute():
            extra = [(self.premarket.get(k), f"pre-market {k}") for k in ("high", "low")]
            return find_levels(self.daily, self.price, self.intraday, extra_levels=[(v, s) for v, s in extra if v])
        return self._cached("levels", compute)

    def daily_trend(self, lookback: int = 40) -> str:
        """Murphy: an uptrend is higher highs *and* higher lows; a downtrend the
        mirror; anything else is a range. Keeps intraday entries aligned with the day."""
        def compute():
            if not self.enough_daily(lookback + 5):
                return "range"
            d = self.daily.tail(lookback)
            close = d["close"]
            sma20 = ta.sma(close, 20).iloc[-1]
            sma50 = ta.sma(self.daily["close"], 50).iloc[-1] if len(self.daily) >= 55 else sma20
            half = len(d) // 2
            higher_highs = d["high"].iloc[half:].max() > d["high"].iloc[:half].max()
            higher_lows = d["low"].iloc[half:].min() > d["low"].iloc[:half].min()
            px = float(close.iloc[-1])
            if px > sma20 >= sma50 and higher_highs and higher_lows:
                return "up"
            if px < sma20 <= sma50 and not higher_highs and not higher_lows:
                return "down"
            return "range"
        return self._cached(f"trend{lookback}", compute)

    def spark(self) -> List[float]:
        source = self.intraday if self.enough_intraday(2) else self.daily
        return self._cached("spark", lambda: [round(float(x), 3) for x in source["close"].tail(60)])


def build_context(symbol: str, intraday: Optional[pd.DataFrame], daily: pd.DataFrame, quote: Quote,
                  fundamentals: Optional[Financials] = None, peers: Optional[List[Financials]] = None,
                  params: Optional[Dict[str, Any]] = None, account_equity: float = 0.0,
                  activity: Optional[Dict[str, Any]] = None) -> StrategyContext:
    return StrategyContext(symbol=symbol, intraday=intraday, daily=daily, quote=quote,
                           fundamentals=fundamentals, peers=peers, params=params or {},
                           account_equity=account_equity, activity=activity or {})


def completed_daily(ctx: StrategyContext) -> pd.DataFrame:
    """The daily candles without the one still forming (it is there only in a live session). The swing
    setups read their signals on these and enter at the price now: a close beyond a band on a candle still
    printing is no close at all, and the replay, which signals at each session's close, never sees one.
    Their stops keep the shared daily ATR, the one Strategy._mk_play floors every technical stop with."""
    def compute():
        d = ctx.daily
        forming = (len(d) and ctx.intraday is not None and d.index[-1].date() == clock.session_date(ctx.now)
                   and 0 < ctx.minutes_since_open < 390)
        return d.iloc[:-1] if forming else d
    return ctx._cached("completed_daily", compute)


class Strategy:
    #: registry key, matches config.yaml
    key: str = "base"
    kind: StrategyKind = StrategyKind.TECHNICAL
    timeframe: Timeframe = Timeframe.INTRADAY
    #: short title shown in the UI
    title: str = "Base strategy"
    #: the generic explanation of the setup; each play's explanation adds its numbers
    thesis: str = ""
    default_params: Dict[str, Any] = {}

    #: switched on when config.yaml doesn't list the setup (newer setups the file predates)
    enabled_by_default: bool = False

    #: may this setup be entered pre- or post-market too? (limit orders only there)
    extended_hours_ok: bool = False

    #: how the setup relates to the move in progress: "momentum" trades with it,
    #: "reversal" fades it on purpose, "value" ignores price action. The noise
    #: checks for going against the trend, VWAP or the gap apply to momentum only.
    style: str = "momentum"

    #: how the setup behaves across Aziz's intraday sessions (Ch. 7). Momentum /
    #: breakout setups fade at midday; reversal setups hold up; trend setups get
    #: better into the close; an "open" setup (the flag) belongs to the first hour and
    #: is rarely worth taking later. Scales confidence only, never the geometry.
    tod_profile: str = "momentum"
    _TOD_WEIGHTS = {
        "momentum": {"OPEN": 1.00, "LATE_MORNING": 1.00, "MIDDAY": 0.75, "CLOSE": 0.85, "OFF": 1.0},
        "open":     {"OPEN": 1.00, "LATE_MORNING": 0.85, "MIDDAY": 0.60, "CLOSE": 0.70, "OFF": 1.0},
        "reversal": {"OPEN": 0.85, "LATE_MORNING": 1.00, "MIDDAY": 1.00, "CLOSE": 0.90, "OFF": 1.0},
        "trend":    {"OPEN": 0.80, "LATE_MORNING": 1.00, "MIDDAY": 1.00, "CLOSE": 0.95, "OFF": 1.0},
        "swing":    {"OPEN": 1.00, "LATE_MORNING": 1.00, "MIDDAY": 1.00, "CLOSE": 1.00, "OFF": 1.0},
    }

    #: how long the trade is expected to take: (typical, review after). Minutes
    #: for intraday setups, trading days for swing setups. Informational only -
    #: it never moves the stop, it flags a position as aging / overdue.
    expected_hold = (4.0, 10.0)

    #: geometry guard rails - a play outside these is almost always bad data, or
    #: a stop tightened to fake a good reward:risk (Aziz p.66: "define a closer
    #: stop loss to have a more favorable ratio? The answer is NO.")
    MAX_STOP_PCT = 0.25
    MIN_STOP_PCT = {"INTRADAY": 0.006, "SWING": 0.015}
    MIN_STOP_ATR = 0.9
    MIN_STOP_DAILY_ATR = {"INTRADAY": 0.25, "SWING": 1.0}
    #: the stop floor in forecast daily standard deviations, while volatility is rising
    VOL_STOP_SIGMAS = {"INTRADAY": 0.35, "SWING": 1.4}
    MAX_TARGET_PCT = {"INTRADAY": 0.15, "SWING": 0.45}
    MIN_TARGET_PCT = 0.002
    RR_BOUNDS = (0.4, 8.0)

    def __init__(self, params: Optional[Dict[str, Any]] = None, weight: float = 1.0) -> None:
        self.params = {**self.default_params, **(params or {})}
        self.weight = weight

    def generate(self, ctx: StrategyContext) -> List[Play]:
        raise NotImplementedError

    def describe(self) -> Dict[str, str]:
        return {"key": self.key, "title": self.title, "kind": self.kind.value,
                "timeframe": self.timeframe.value, "thesis": self.thesis}

    def _mk_play(
        self, ctx: StrategyContext, side: Side, entry: float, stop: float, targets: List[float],
        confidence: float, rationale: str, detail: str, evidence: Dict[str, Any],
        tags: Optional[List[str]] = None, ttl_minutes: int = 45, invalidation: str = "",
        edge_note: str = "", probability: Optional[float] = None,
    ) -> Optional[Play]:
        if entry <= 0 or stop <= 0 or not targets:
            return None
        if side is Side.LONG and (stop >= entry or targets[0] <= entry):
            return None
        if side is Side.SHORT and (stop <= entry or targets[0] >= entry):
            return None

        max_stop = entry * self.MAX_STOP_PCT
        stop = max(stop, entry - max_stop) if side is Side.LONG else min(stop, entry + max_stop)

        # widen a noise-tight stop to a real floor: % of price, the intraday ATR, and
        # a slice of the stock's usual daily range - a stop well inside a normal
        # day's swing is noise, not a level
        floor = entry * self.MIN_STOP_PCT.get(self.timeframe.value, 0.006)
        if self.timeframe is Timeframe.INTRADAY and ctx.intraday_atr > 0:
            floor = max(floor, self.MIN_STOP_ATR * ctx.intraday_atr)
        if self.kind is StrategyKind.TECHNICAL and ctx.daily_atr > 0:
            floor = max(floor, self.MIN_STOP_DAILY_ATR.get(self.timeframe.value, 0.25) * ctx.daily_atr)
        # volatility clusters (Tsay ch. 3): when tomorrow's forecast volatility is above the last
        # months', the usual daily range understates the noise a stop has to sit outside of
        vol = ctx.vol_forecast() if self.kind is StrategyKind.TECHNICAL else None
        if vol and vol.get("vol") and float(vol.get("ratio") or 0.0) > 1.0:
            floor = max(floor, self.VOL_STOP_SIGMAS.get(self.timeframe.value, 0.35) * float(vol["vol"]) * entry)
        if floor > max_stop:
            return None                           # too volatile for this setup's stop
        if abs(entry - stop) < floor:
            stop = entry - floor if side is Side.LONG else entry + floor

        max_move = entry * self.MAX_TARGET_PCT.get(self.timeframe.value, 0.4)
        targets = [min(t, entry + max_move) if side is Side.LONG else max(t, entry - max_move) for t in targets]
        # the nearest target comes first, however the setup listed them: it is the one the reward:risk is
        # judged on, and the first the exits take. A level further out than the setup's fallback multiple
        # would otherwise pass for the first target and flatter the play
        ahead = {t for t in targets if (t > entry if side is Side.LONG else t < entry)}
        targets = sorted(ahead, reverse=side is Side.SHORT)
        if abs(targets[0] - entry) / entry < self.MIN_TARGET_PCT:
            return None
        rr = abs(targets[0] - entry) / abs(entry - stop)
        if not self.RR_BOUNDS[0] <= rr <= self.RR_BOUNDS[1]:
            return None

        tags = tags or []
        ext_ok = (self.extended_hours_ok or self.timeframe is Timeframe.SWING
                  or any(t in ("gap", "catalyst") for t in tags))
        hold_typ = float(self.params.get("hold_typical", self.expected_hold[0]))
        hold_max = float(self.params.get("hold_max", self.expected_hold[1]))

        # Aziz Ch. 7: an edge is worth less in the wrong part of the day
        tod = "OFF"
        if self.timeframe is Timeframe.INTRADAY:
            tod = clock.time_of_day(ctx.now)
            weight = self._TOD_WEIGHTS.get(self.tod_profile, {}).get(tod, 1.0)
            confidence *= weight
            evidence = {**evidence, "time_of_day": tod, "tod_weight": round(weight, 2)}
        confidence = max(0.0, min(1.0, confidence))

        # Douglas: state the edge as a probability, never a promise - and Chan: measure it.
        # The setup's own read is blended with the win rate of its replayed and real trades
        # (calibrated_probability), so the odds shown lean on the record as it grows.
        own = float(probability if probability is not None else 0.40 + 0.28 * confidence)
        probability, odds = calibrated_probability(own, ctx.records.get(self.key))
        if odds is not None:
            evidence = {**evidence, "odds_from_record": odds}
        if not invalidation:
            invalidation = (f"a 5-minute close {'below' if side is Side.LONG else 'above'} {stop:.2f} "
                            "(the protective stop / the technical level the idea rests on)")
        return Play(
            symbol=ctx.symbol, side=side, strategy=self.key, kind=self.kind, timeframe=self.timeframe,
            entry=round(entry, 4), stop=round(stop, 4), targets=[round(t, 4) for t in targets],
            confidence=confidence, rationale=rationale,
            explanation=self._compose_explanation(side, entry, stop, targets, detail, invalidation,
                                                  edge_note, probability, tod, odds),
            invalidation=invalidation, probability=round(probability, 3), evidence=evidence, tags=tags,
            asset_class=AssetClass.EQUITY, extended_hours_ok=ext_ok,
            expected_hold_typical=hold_typ, expected_hold_max=hold_max,
            expires_at=ctx.now.astimezone(dt.timezone.utc) + dt.timedelta(minutes=ttl_minutes),
        )

    def _compose_explanation(self, side: Side, entry: float, stop: float, targets: List[float],
                             detail: str, invalidation: str, edge_note: str, probability: float,
                             tod: str, odds: Optional[Dict[str, Any]] = None) -> str:
        """The hover pop-up, framed the way Douglas (*Trading in the Zone*) says
        a position should be held: one execution of an edge - a higher
        probability of one outcome over another - not a forecast."""
        risk_ps = abs(entry - stop)
        risk_pct = risk_ps / entry * 100.0
        rr = abs(targets[0] - entry) / risk_ps
        direction = ("LONG - you buy, and profit if it rises" if side is Side.LONG
                     else "SHORT - you sell short, and profit if it falls")
        target_line = f"first target {targets[0]:.2f}  (reward:risk {rr:.1f} : 1)"
        if len(targets) > 1:
            target_line += "; then " + ", ".join(f"{t:.2f}" for t in targets[1:])
        when = tod.replace("_", "-").lower() if tod and tod != "OFF" else "this setup"

        blocks = [f"{self.title} - {direction}",
                  f"THE EDGE (what tends to happen here): {self.thesis}",
                  f"RIGHT NOW ({when}): {detail}"]
        if edge_note:
            blocks.append(f"READING THE TAPE: {edge_note}")
        blocks += [
            "THE PLAN\n"
            f"   - enter near {entry:.2f}\n"
            f"   - protective stop {stop:.2f}  ->  you risk {risk_ps:.2f}/share ({risk_pct:.1f}% of price) "
            "to find out whether the edge pays\n"
            f"   - {target_line}\n"
            f"   - estimated odds the edge pays: ~{probability * 100:.0f}%  {_odds_note(odds)}",
            f"INVALIDATION: {invalidation}. If price gets there the reason for the trade is gone - "
            "the automatic exit handles it, no decision needed.",
            "HOW TO HOLD IT (Douglas): this is one roll of an edge, not a prediction. Wins and losses "
            "land randomly around it, so a textbook setup can still lose - that is normal, not a mistake. "
            f"Accept the {risk_ps:.2f}/share loss before you click; if you can't, skip the trade. Then "
            "leave it alone - don't widen the stop and don't add size to be right.",
        ]
        return "\n\n".join(blocks)


#: trades of record that weigh as much as the setup's own read of its odds
PRIOR_ODDS_TRADES = 30.0


def calibrated_probability(own: float, record: Optional[Mapping[str, Any]],
                           prior_trades: float = PRIOR_ODDS_TRADES) -> Tuple[float, Optional[Dict[str, Any]]]:
    """The odds a play states: the setup's own read of them, shrunk toward the win rate of the
    strategy's replayed and real trades as those accumulate - a small sample proves little (Chan),
    and an edge is a probability over a series of trades (Douglas). Returns the probability,
    bounded like any play's, and the record it leaned on (None when there is none)."""
    n = float((record or {}).get("trades") or 0)
    rate = (record or {}).get("win_rate")
    if n <= 0 or rate is None:
        return max(0.05, min(0.90, own)), None
    blended = (n * float(rate) + prior_trades * own) / (n + prior_trades)
    return (max(0.05, min(0.90, blended)),
            {"trades": int(n), "win_rate": round(float(rate), 3), "own": round(own, 3)})


def _odds_note(odds: Optional[Dict[str, Any]]) -> str:
    if not odds:
        return "(a probability over many trades, not a call on this one)"
    return (f"(the setup's own read, blended with its record: {odds['trades']} replayed and real trades, "
            f"{odds['win_rate'] * 100:.0f}% of them winners)")


def swing_low(series: pd.Series, lookback: int = 10) -> float:
    return float(series.tail(lookback).min())


def swing_high(series: pd.Series, lookback: int = 10) -> float:
    return float(series.tail(lookback).max())


def safe_last(series: pd.Series, default: float = math.nan) -> float:
    try:
        v = float(series.iloc[-1])
        return v if not math.isnan(v) else default
    except (IndexError, TypeError, ValueError, AttributeError):
        return default
