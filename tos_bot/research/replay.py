"""Strategy replay: how each setup would have done on past candles, and what each
noise check removes.

The replay walks recorded candles the way the scans see them live. At the close
of every 5-minute bar (day trades) or every session (swing trades) it builds the
same context the evaluator builds, runs the strategies, flags each play the same
way, and follows it the way the automatic exits would: stop, target, break-even,
trailing stop, the flatten before the close, the swing time limit. Results are
in R - multiples of the risk taken - so a $5 stock and a $400 stock count alike.

Two things come out of it:

* per strategy, a record: trades, win rate, average R, profit factor and the
  worst drawdown. Autopilot only trades a strategy whose record - counting just
  the plays it would actually take - is good enough (see AutoPilot).
* per noise check, the trades it would have removed against the ones it keeps.
  A check whose removed trades did better than the kept ones is throwing winners
  away, and shows up that way.

Chan's rules for a backtest worth believing (*Quantitative Trading*, ch. 3):

* **costs** - every fill pays ``commission_bps``, and market fills ``slippage_bps`` on
  top; a strategy that only wins before costs doesn't win.
* **out of sample** - the latest third of the sessions replayed is held out: every
  record and every noise verdict is also given for those sessions alone, and Autopilot
  wants a strategy to have made money there too. A result that holds up only on the
  earlier sessions was probably luck.
* **no look-ahead** - the market's regime on a day comes from a model fitted on earlier
  days only, tomorrow's volatility from completed candles, an earnings report counts
  from the moment SEC accepted the filing, and a headline from the moment it was
  published - the news checks see, at each bar, the stories out by then and the S&P 500
  ETF's candles up to then (as far back as the app has been storing headlines).

What it can't know: a fill is assumed at the next bar's open (and skipped when
that open has already run away from the entry), a stop and a target in the same
bar count as the stop, and slippage is a flat fraction of price.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from ..core.enums import Side, StrategyKind, Timeframe
from ..core.models import Play
from ..data.market_data import quote_from_price
from ..scanner.evaluator import with_today
from ..scanner.filters import expected_r
from ..scanner.heat import daily_metrics, intraday_metrics
from .features import play_features
from ..scanner.noise import CHECKS, LEARNABLE_CHECKS, NoiseSettings, context_flags
from ..strategies.base import Strategy, StrategyContext
from ..util import clock

NY = "America/New_York"
BAR = pd.Timedelta(minutes=5)
LIVE_INTRADAY_BARS = 5 * 78          # the scans look at five sessions of 5-minute candles
#: fewer removed trades than this and a check's verdict is only noise itself
MIN_SAMPLE = 10
MEASURED_CHECKS = CHECKS + ("unconfirmed",)
HELD_OUT_FRACTION = 1 / 3


@dataclass(frozen=True)
class ReplaySettings:
    slippage_bps: float = 5.0             # on market fills, each way
    commission_bps: float = 1.0           # on every fill
    max_entry_drift_atr: float = 0.5      # skip a day-trade fill whose open ran this many intraday ATRs from the entry
    max_entry_drift_daily_atr: float = 1.0
    warmup_bars: int = 3                  # bars into the session before the first signal
    breakeven_at_r: float = 1.3
    breakeven_lock_r: float = 0.3
    trail_start_r: float = 2.0
    trail_lock_ratio: float = 0.5
    flatten_before_close_min: int = 10
    max_swing_hold_days: int = 10
    scale_out_pct: float = 50.0           # at the first target of a play with two, this much comes off...
    scale_out_lock_r: float = 0.0         # ...and the stop goes to the entry plus this R; the rest runs to the second

    @classmethod
    def from_exit_rules(cls, cfg, costs=None) -> "ReplaySettings":
        """``cfg``: the exit manager's settings; ``costs``: the replay's (slippage and commission)."""
        extra = {} if costs is None else {"slippage_bps": float(costs.slippage_bps),
                                          "commission_bps": float(costs.commission_bps)}
        return cls(breakeven_at_r=float(cfg.breakeven_at_r), breakeven_lock_r=float(cfg.breakeven_lock_r),
                   trail_start_r=float(cfg.trail_start_r), trail_lock_ratio=float(cfg.trail_lock_ratio),
                   flatten_before_close_min=int(cfg.flatten_intraday_before_close_min),
                   max_swing_hold_days=int(cfg.max_swing_hold_days) or 10,
                   scale_out_pct=float(getattr(cfg, "scale_out_pct", 0.0) or 0.0),
                   scale_out_lock_r=float(getattr(cfg, "scale_out_lock_r", 0.0) or 0.0), **extra)


@dataclass
class SimTrade:
    strategy: str
    symbol: str
    side: str
    timeframe: str
    entered_at: str
    exited_at: str
    entry: float
    exit: float
    r: float
    exit_reason: str
    noise: List[str] = field(default_factory=list)
    confirmed: bool = True                # the setup had also shown up on the bar before
    mfe_r: float = 0.0                    # the best it got, in R, before it closed
    scaled: bool = False                  # part of it was taken off at the first target
    features: Dict[str, Any] = field(default_factory=dict)   # the play at the signal (research/features.py)


@dataclass
class _Position:
    strategy: str
    play: Play
    entry: float
    stop: float
    risk: float                           # per share, from the fill to the original stop
    entered_at: pd.Timestamp
    noise: List[str]
    confirmed: bool
    best: float
    bars_held: int = 0
    fraction: float = 1.0                 # of the position still on
    banked_r: float = 0.0                 # in R of the whole position, from the part taken off
    scaled: bool = False
    features: Dict[str, Any] = field(default_factory=dict)

    @property
    def sign(self) -> int:
        return 1 if self.play.side is Side.LONG else -1


# ---------------------------------------------------------------- day trades
def replay_intraday(strategies: Sequence[Strategy], symbol: str, bars: pd.DataFrame, daily: pd.DataFrame,
                    settings: ReplaySettings = ReplaySettings(), noise: NoiseSettings = NoiseSettings(),
                    sessions: Optional[int] = None, market: Optional[Mapping[dt.date, float]] = None,
                    earnings: Sequence[str] = (), news: Sequence[Mapping[str, Any]] = (),
                    benchmark_bars: Optional[pd.DataFrame] = None,
                    benchmark_daily: Optional[pd.DataFrame] = None) -> List[SimTrade]:
    """``bars``: 5-minute candles over several sessions; ``daily``: completed daily candles;
    ``market``: the probability of the turbulent regime for each day, known before it opened;
    ``earnings``: when SEC accepted the stock's earnings filings (8-K item 2.02), ISO times in UTC;
    ``news``: the stock's stored stories, each with ``at`` (ISO, UTC) - the news checks see, at
    every bar, the ones out by then; ``benchmark_bars`` / ``benchmark_daily``: the S&P 500 ETF's
    5-minute and daily candles, for the market model behind those checks."""
    day_trades = [s for s in strategies if s.kind is StrategyKind.TECHNICAL and s.timeframe is Timeframe.INTRADAY]
    days = sorted(set(bars.index.date))
    stories = _stories(news)
    trades: List[SimTrade] = []
    for day in days[-sessions:] if sessions else days:
        session = bars[bars.index.date == day]
        prior_daily = daily[daily.index.date < day]
        if not day_trades or len(prior_daily) < 20 or len(session) < settings.warmup_bars + 2:
            continue
        flatten_at = _at(day, clock.regular_close_time(day)) - pd.Timedelta(minutes=settings.flatten_before_close_min)
        benchmark = _Benchmark(benchmark_daily, benchmark_bars, day)
        trades += _replay_session(day_trades, symbol, session, bars, prior_daily, flatten_at, settings, noise,
                                  regime(market, day), earnings_signals(earnings, day), stories, benchmark)
    return trades


def _replay_session(strategies: Sequence[Strategy], symbol: str, session: pd.DataFrame, history: pd.DataFrame,
                    prior_daily: pd.DataFrame, flatten_at: pd.Timestamp, settings: ReplaySettings,
                    noise: NoiseSettings, market: Dict[str, Any], signals: Any,
                    stories: Sequence[Tuple[pd.Timestamp, Mapping[str, Any]]] = (),
                    benchmark: Optional["_Benchmark"] = None) -> List[SimTrade]:
    trades: List[SimTrade] = []
    open_positions: Dict[str, _Position] = {}
    seen_before: set = set()
    shared = session_series(history, strategies)
    for i in range(settings.warmup_bars, len(session) - 1):
        closed_at, next_at, next_bar = session.index[i] + BAR, session.index[i + 1], session.iloc[i + 1]
        if closed_at >= flatten_at:
            break
        end = history.index.searchsorted(session.index[i], side="right")
        window = history.iloc[max(0, end - LIVE_INTRADAY_BARS):end]
        ctx = StrategyContext(symbol=symbol, intraday=window, daily=with_today(prior_daily, window),
                              quote=quote_from_price(symbol, float(window["close"].iloc[-1])),
                              now=closed_at.to_pydatetime(), signals=signals, market=dict(market),
                              news=news_at(stories, closed_at),
                              benchmark=benchmark.closes_at(closed_at) if benchmark is not None else None,
                              shared=shared)
        signals_now = _signals(strategies, ctx, noise)
        activity = intraday_metrics(symbol, window, prior_daily) if signals_now else None
        for strategy, play, flags in signals_now:
            if strategy.key not in open_positions:
                confirmed = (strategy.key, play.side) in seen_before
                position = _enter(strategy.key, play, flags, confirmed, next_at,
                                  float(next_bar["open"]), settings.max_entry_drift_atr * ctx.intraday_atr, settings,
                                  features=play_features(play, now=ctx.now, market=ctx.market, noise=flags,
                                                         confirmations=2 if confirmed else 1, activity=activity))
                if position is not None:
                    open_positions[strategy.key] = position
        seen_before = {(s.key, p.side) for s, p, _ in signals_now}

        flatten = next_at + BAR >= flatten_at
        for key, position in list(open_positions.items()):
            done = _step(position, next_bar, next_at + BAR, settings, "eod-flatten" if flatten else None)
            if done is not None:
                trades.append(done)
                del open_positions[key]
    last_at, last = session.index[-1] + BAR, session.iloc[-1]
    trades += [_close(p, float(last["close"]), last_at, "eod-flatten", settings) for p in open_positions.values()]
    return trades


# ---------------------------------------------------------------- swing trades
def replay_swing(strategies: Sequence[Strategy], symbol: str, daily: pd.DataFrame,
                 settings: ReplaySettings = ReplaySettings(), noise: NoiseSettings = NoiseSettings(),
                 sessions: int = 250, market: Optional[Mapping[dt.date, float]] = None,
                 news: Sequence[Mapping[str, Any]] = (),
                 benchmark_daily: Optional[pd.DataFrame] = None) -> List[SimTrade]:
    """Signals at each session's close, fills at the next open. Trades still open
    when the candles run out are left out - their result isn't known yet."""
    swing = [s for s in strategies if s.kind is StrategyKind.TECHNICAL and s.timeframe is Timeframe.SWING]
    stories = _stories(news)
    trades: List[SimTrade] = []
    open_positions: Dict[str, _Position] = {}
    for i in range(max(60, len(daily) - sessions - 1), len(daily) - 1):
        history, next_at, next_bar = daily.iloc[:i + 1], daily.index[i + 1], daily.iloc[i + 1]
        closed_on = history.index[-1].date()
        close_at = _at(closed_on, clock.regular_close_time(closed_on))
        benchmark = _Benchmark(benchmark_daily, None, closed_on)
        ctx = StrategyContext(symbol=symbol, intraday=None, daily=history,
                              quote=quote_from_price(symbol, float(history["close"].iloc[-1])),
                              now=close_at.to_pydatetime(), market=regime(market, next_at.date()),
                              news=news_at(stories, close_at), benchmark=benchmark.closes_at(close_at, whole_day=True))
        signals_now = _signals(swing, ctx, noise) if swing else []
        activity = daily_metrics(symbol, history) if signals_now else None
        for strategy, play, flags in signals_now:
            if strategy.key not in open_positions:
                position = _enter(strategy.key, play, flags, True, next_at, float(next_bar["open"]),
                                  settings.max_entry_drift_daily_atr * ctx.daily_atr, settings,
                                  features=play_features(play, now=ctx.now, market=ctx.market, noise=flags,
                                                         confirmations=1, activity=activity))
                if position is not None:
                    open_positions[strategy.key] = position
        for key, position in list(open_positions.items()):
            timed_out = position.bars_held + 1 >= settings.max_swing_hold_days
            done = _step(position, next_bar, next_at, settings, "time-stop" if timed_out else None)
            if done is not None:
                trades.append(done)
                del open_positions[key]
    return trades


# ---------------------------------------------------------------- plays that weren't taken
def shadow_trade(play: Play, session: pd.DataFrame, seen_at: pd.Timestamp,
                 settings: ReplaySettings = ReplaySettings(),
                 features: Optional[Mapping[str, Any]] = None) -> Optional[SimTrade]:
    """How a day-trade play seen at ``seen_at`` would have gone if it had been taken: filled
    at the open of the next 5-minute bar, then managed like any other trade on the rest of
    ``session``. None when the price had already run away from the entry, or no bars follow."""
    if not len(session):
        return None
    day = session.index[0].date()
    flatten_at = _at(day, clock.regular_close_time(day)) - pd.Timedelta(minutes=settings.flatten_before_close_min)
    after = session[session.index >= seen_at]
    if len(after) < 2:
        return None
    position = _enter(play.strategy, play, list(play.noise), play.confirmations > 1, after.index[0],
                      float(after["open"].iloc[0]), 0.0, settings, features=features)
    if position is None:
        return None
    for at, bar in after.iterrows():
        done = _step(position, bar, at + BAR, settings, "eod-flatten" if at + BAR >= flatten_at else None)
        if done is not None:
            return done
    return _close(position, float(after["close"].iloc[-1]), after.index[-1] + BAR, "eod-flatten", settings)


# ---------------------------------------------------------------- shared
def regime(market: Optional[Mapping[dt.date, float]], day: dt.date) -> Dict[str, Any]:
    p = (market or {}).get(day)
    return {} if p is None else {"p_turbulent": round(float(p), 3), "regime": "turbulent" if p >= 0.5 else "calm"}


def session_series(history: pd.DataFrame, strategies: Sequence[Strategy]) -> Dict[str, Any]:
    """The series every bar of a session shares (StrategyContext.shared): the session VWAP over
    the whole history, and the opening range for each width the setups ask for. A bar's value
    depends only on its own session's candles up to it, so slicing these equals recomputing."""
    from ..indicators import ta

    out: Dict[str, Any] = {"session_vwap": ta.session_vwap(history)}
    for strategy in strategies:
        minutes = strategy.params.get("or_minutes") if isinstance(strategy.params, dict) else None
        if minutes:
            out.setdefault(f"opening_range_{int(minutes)}", ta.opening_range(history, int(minutes)))
    return out


def _stories(news: Sequence[Mapping[str, Any]]) -> List[Tuple[pd.Timestamp, Mapping[str, Any]]]:
    """The stories with a readable time, oldest first, each stamped as an aware timestamp."""
    out = []
    for story in news or ():
        try:
            at = pd.Timestamp(story["at"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((at.tz_localize("UTC") if at.tzinfo is None else at, story))
    return sorted(out, key=lambda pair: pair[0])


def news_at(stories: Sequence[Tuple[pd.Timestamp, Mapping[str, Any]]], now: pd.Timestamp,
            days: int = 5) -> Optional[Dict[str, Any]]:
    """The stock's news as it stood at ``now``: the stories out by then (the last few days), read
    just now. None when the replay was given no stories for the stock - then, as live, the news
    checks stay silent rather than claim the move came without news."""
    if not stories:
        return None
    since = now - pd.Timedelta(days=days)
    return {"checked_at": now.to_pydatetime(),
            "stories": [dict(story, at=at.isoformat()) for at, story in stories if since <= at <= now]}


class _Benchmark:
    """The S&P 500 ETF's closes as the market model would see them at a moment of ``day``: the
    completed sessions before it, plus the day's own price so far from its 5-minute candles."""

    def __init__(self, daily: Optional[pd.DataFrame], bars: Optional[pd.DataFrame], day: dt.date) -> None:
        self.before = daily["close"][daily.index.date < day] if daily is not None and len(daily) else None
        self.through_day = daily["close"][daily.index.date <= day] if daily is not None and len(daily) else None
        self.session = bars[bars.index.date == day] if bars is not None and len(bars) else None
        self.day = day

    def closes_at(self, now: pd.Timestamp, whole_day: bool = False) -> Optional[pd.Series]:
        if self.before is None or len(self.before) < 2:
            return None
        if whole_day:                       # a swing signal comes at the close: the session is a completed candle
            return self.through_day
        if self.session is None:
            return None
        so_far = self.session[self.session.index < now]
        if not len(so_far):
            return None
        today = pd.Series([float(so_far["close"].iloc[-1])], index=pd.DatetimeIndex([_at(self.day, dt.time(16, 0))]))
        return pd.concat([self.before, today])


def earnings_signals(accepted: Sequence[str], day: dt.date) -> Any:
    """The stock's signals as a day-trade setup saw them on ``day``: the earnings filings SEC
    accepted since the previous session's close, shaped like the signal book's filings."""
    if not accepted:
        return None
    since = _at(clock.prev_trading_day(day), clock.regular_close_time(clock.prev_trading_day(day)))
    until = _at(day, dt.time(16, 0))
    found = [stamp for stamp in accepted if since <= pd.Timestamp(stamp).tz_convert(NY) < until]
    if not found:
        return None
    return SimpleNamespace(filings=[{"items": "2.02", "published_at": stamp, "kind": "filing"} for stamp in found],
                           buying=None, selling=None, news=None)


def _signals(strategies: Sequence[Strategy], ctx: StrategyContext,
             noise: NoiseSettings) -> List[Tuple[Strategy, Play, List[str]]]:
    """Each strategy's play at this moment, flagged the way the scans flag it."""
    out: List[Tuple[Strategy, Play, List[str]]] = []
    for strategy in strategies:
        try:
            plays = strategy.generate(ctx)
        except Exception:  # noqa: BLE001 - a strategy that trips on odd candles just sits this bar out
            continue
        for play in plays[:1]:
            flags = context_flags(play, ctx, strategy.style, noise)
            play.evidence["expected_r"] = round(expected_r(play, ctx.daily_atr), 3)
            if play.evidence["expected_r"] < noise.min_expected_r:
                flags.append("low_expected_value")
            out.append((strategy, play, flags))
    if len({play.side for _, play, _ in out}) > 1:
        for _, _, flags in out:
            flags.append("conflict")
    return out


def _enter(key: str, play: Play, flags: List[str], confirmed: bool, at: pd.Timestamp, open_price: float,
           max_drift: float, settings: ReplaySettings,
           features: Optional[Mapping[str, Any]] = None) -> Optional[_Position]:
    limit = max_drift if max_drift > 0 else 0.5 * abs(play.entry - play.stop)   # no ATR yet: half the stop distance
    if abs(open_price - play.entry) > limit:
        return None                                  # ran away before it could be filled
    sign = 1 if play.side is Side.LONG else -1
    fill = open_price * (1 + sign * (settings.slippage_bps + settings.commission_bps) / 1e4)
    risk = (fill - play.stop) * sign
    if risk <= 0:
        return None                                  # opened through the stop
    return _Position(key, play, fill, play.stop, risk, at, list(flags), confirmed, best=fill,
                     features=dict(features or {}))


def _step(position: _Position, bar: pd.Series, bar_end: pd.Timestamp, settings: ReplaySettings,
          force_exit: Optional[str] = None) -> Optional[SimTrade]:
    """Move a position through one bar: its stop first, then its target, then
    tighten the stop the way the exit manager would."""
    o, h, low, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
    long = position.sign > 0
    position.bars_held += 1
    if (low <= position.stop) if long else (h >= position.stop):
        price = min(o, position.stop) if long else max(o, position.stop)
        reason = "stop" if position.stop == position.play.stop else "trailing-stop"
        return _close(position, price, bar_end, reason, settings)
    targets = position.play.targets
    target = targets[1] if position.scaled else targets[0]
    if (h >= target) if long else (low <= target):
        price = max(o, target) if long else min(o, target)
        if not position.scaled and len(targets) > 1 and 0.0 < settings.scale_out_pct < 100.0:
            # Aziz: part off at the first target, stop to break-even, the rest runs to the second
            part = settings.scale_out_pct / 100.0
            fill = price * (1 - position.sign * settings.commission_bps / 1e4)          # a limit fill
            position.banked_r += part * (fill - position.entry) * position.sign / position.risk
            position.fraction -= part
            position.scaled = True
            _tighten(position, position.entry + position.sign * settings.scale_out_lock_r * position.risk)
        else:
            position.best = max(position.best, target) if long else min(position.best, target)
            return _close(position, price, bar_end, "target", settings, limit=True)

    position.best = max(position.best, h) if long else min(position.best, low)
    best_r = (position.best - position.entry) * position.sign / position.risk
    if settings.breakeven_at_r > 0 and best_r >= settings.breakeven_at_r:
        _tighten(position, position.entry + position.sign * settings.breakeven_lock_r * position.risk)
    if settings.trail_start_r > 0 and best_r >= settings.trail_start_r:
        _tighten(position, position.entry + position.sign * best_r * settings.trail_lock_ratio * position.risk)
    return _close(position, c, bar_end, force_exit, settings) if force_exit else None


def _tighten(position: _Position, stop: float) -> None:
    position.stop = max(position.stop, stop) if position.sign > 0 else min(position.stop, stop)


def _close(position: _Position, price: float, at: pd.Timestamp, reason: str, settings: ReplaySettings,
           limit: bool = False) -> SimTrade:
    cost_bps = settings.commission_bps + (0.0 if limit else settings.slippage_bps)
    price *= 1 - position.sign * cost_bps / 1e4
    play = position.play
    rest = position.fraction * (price - position.entry) * position.sign / position.risk
    return SimTrade(strategy=position.strategy, symbol=play.symbol, side=play.side.value,
                    timeframe=play.timeframe.value, entered_at=position.entered_at.isoformat(),
                    exited_at=at.isoformat(), entry=round(position.entry, 4), exit=round(price, 4),
                    r=round(position.banked_r + rest, 3), exit_reason=reason,
                    noise=list(position.noise), confirmed=position.confirmed,
                    mfe_r=round(max(0.0, (position.best - position.entry) * position.sign / position.risk), 3),
                    scaled=position.scaled, features=dict(position.features))


def _at(day: dt.date, time: dt.time) -> pd.Timestamp:
    return pd.Timestamp(dt.datetime.combine(day, time), tz=NY)


# ---------------------------------------------------------------- what it means
def held_out_from(last_session: dt.date, sessions: int, fraction: float = HELD_OUT_FRACTION) -> Optional[str]:
    """The first session of the held-out latest ``fraction`` of ``sessions`` ending on ``last_session``."""
    days = sorted(clock.last_n_sessions(last_session, max(1, int(sessions))))
    held = max(1, int(round(len(days) * fraction)))
    return days[-held].isoformat() if len(days) > held else None


def held_out(trade: SimTrade, split: Optional[Mapping[str, Optional[str]]]) -> bool:
    since = (split or {}).get(trade.timeframe)
    return bool(since) and trade.entered_at[:10] >= since


def summarize(trades: Iterable[SimTrade]) -> Dict[str, Any]:
    rs = [t.r for t in sorted(trades, key=lambda t: t.exited_at)]
    if not rs:
        return {"trades": 0}
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    total, peak, worst = 0.0, 0.0, 0.0
    for r in rs:
        total += r
        peak = max(peak, total)
        worst = min(worst, total - peak)
    return {
        "trades": len(rs), "win_rate": round(len(wins) / len(rs), 3), "expectancy_r": round(sum(rs) / len(rs), 3),
        "avg_win_r": round(sum(wins) / len(wins), 3) if wins else 0.0,
        "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else 0.0,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if sum(losses) < 0 else None,
        "worst_drawdown_r": round(worst, 2), "total_r": round(sum(rs), 2),
    }


def flagged(trade: SimTrade, check: str) -> bool:
    if check == "unconfirmed":
        return trade.timeframe == Timeframe.INTRADAY.value and not trade.confirmed
    return check in trade.noise


def noise_report(trades: Sequence[SimTrade], split: Optional[Mapping[str, Optional[str]]] = None) -> Dict[str, Dict[str, Any]]:
    """For each check: the trades it removes and how they did against the ones it keeps -
    over every session, and over the held-out sessions alone when ``split`` says which."""
    late = [t for t in trades if held_out(t, split)] if split else []

    def measure(test) -> Dict[str, Any]:
        out = _compare(*_partition(trades, test))
        if split:
            out["held_out"] = _compare(*_partition(late, test))
        return out

    report = {check: measure(lambda t, c=check: flagged(t, c)) for check in MEASURED_CHECKS}
    report["all_checks"] = measure(lambda t: any(flagged(t, c) for c in MEASURED_CHECKS))
    return report


def _partition(trades: Sequence[SimTrade], test) -> Tuple[List[SimTrade], List[SimTrade]]:
    """The trades ``test`` picks out and the rest, in one pass."""
    picked: List[SimTrade] = []
    rest: List[SimTrade] = []
    for t in trades:
        (picked if test(t) else rest).append(t)
    return picked, rest


def learned_skips(report: Optional[Mapping[str, Mapping[str, Any]]]) -> List[str]:
    """The checks from the books' statistics and the news that the replay shows are worth
    skipping: the trades they remove did worse over every session and over the held-out
    sessions too."""
    out = []
    for check in LEARNABLE_CHECKS:
        row = (report or {}).get(check) or {}
        if str(row.get("verdict", "")).startswith("helps") and \
                str((row.get("held_out") or {}).get("verdict", "")).startswith("helps"):
            out.append(check)
    return out


def _compare(removed: Sequence[SimTrade], kept: Sequence[SimTrade]) -> Dict[str, Any]:
    def avg(ts):
        return round(sum(t.r for t in ts) / len(ts), 3) if ts else None
    out = {"removes": len(removed), "keeps": len(kept), "removed_avg_r": avg(removed), "kept_avg_r": avg(kept)}
    if len(removed) < MIN_SAMPLE or not kept:
        out["verdict"] = "too few trades to tell"
    elif out["removed_avg_r"] < out["kept_avg_r"]:
        out["verdict"] = "helps - the trades it removes did worse"
    else:
        out["verdict"] = "hurts - the trades it removes did as well or better"
    return out


def taken(trades: Sequence[SimTrade], skip_noise: Iterable[str] = (), min_confirmations: int = 1) -> List[SimTrade]:
    """The trades Autopilot would actually have taken: none with a skipped noise flag, and
    day trades confirmed when it asks for that."""
    skip = set(skip_noise)
    return [t for t in trades if not skip.intersection(t.noise)
            and not (min_confirmations > 1 and flagged(t, "unconfirmed"))]


def strategy_records(trades: Sequence[SimTrade], skip_noise: Iterable[str] = (), min_confirmations: int = 1,
                     split: Optional[Mapping[str, Optional[str]]] = None) -> Dict[str, Dict[str, Any]]:
    """Each strategy's record over the trades Autopilot would have taken, with the held-out
    sessions' record alongside when ``split`` says where they start."""
    return records_by_strategy(taken(trades, skip_noise, min_confirmations), split)


def records_by_strategy(trades: Iterable[SimTrade],
                        split: Optional[Mapping[str, Optional[str]]] = None) -> Dict[str, Dict[str, Any]]:
    """Each strategy's record over ``trades``, with the held-out sessions' record alongside."""
    by_strategy: Dict[str, List[SimTrade]] = {}
    for t in trades:
        by_strategy.setdefault(t.strategy, []).append(t)
    records = {}
    for key, ts in by_strategy.items():
        records[key] = summarize(ts)
        if split:
            records[key]["out_of_sample"] = summarize([t for t in ts if held_out(t, split)])
    return records
