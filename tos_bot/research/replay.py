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

What it can't know: a fill is assumed at the next bar's open (and skipped when
that open has already run away from the entry), a stop and a target in the same
bar count as the stop, and slippage is a flat fraction of price.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from ..core.enums import Side, StrategyKind, Timeframe
from ..core.models import Play
from ..data.market_data import quote_from_price
from ..scanner.evaluator import with_today
from ..scanner.filters import expected_r
from ..scanner.noise import CHECKS, NoiseSettings, context_flags
from ..strategies.base import Strategy, StrategyContext
from ..util import clock

NY = "America/New_York"
BAR = pd.Timedelta(minutes=5)
LIVE_INTRADAY_BARS = 5 * 78          # the scans look at five sessions of 5-minute candles
#: fewer removed trades than this and a check's verdict is only noise itself
MIN_SAMPLE = 10
MEASURED_CHECKS = CHECKS + ("unconfirmed",)


@dataclass(frozen=True)
class ReplaySettings:
    slippage_bps: float = 5.0             # on market fills, each way
    max_entry_drift_atr: float = 0.5      # skip a day-trade fill whose open ran this many intraday ATRs from the entry
    max_entry_drift_daily_atr: float = 1.0
    warmup_bars: int = 3                  # bars into the session before the first signal
    breakeven_at_r: float = 1.3
    breakeven_lock_r: float = 0.3
    trail_start_r: float = 2.0
    trail_lock_ratio: float = 0.5
    flatten_before_close_min: int = 10
    max_swing_hold_days: int = 10

    @classmethod
    def from_exit_rules(cls, cfg) -> "ReplaySettings":
        return cls(breakeven_at_r=float(cfg.breakeven_at_r), breakeven_lock_r=float(cfg.breakeven_lock_r),
                   trail_start_r=float(cfg.trail_start_r), trail_lock_ratio=float(cfg.trail_lock_ratio),
                   flatten_before_close_min=int(cfg.flatten_intraday_before_close_min),
                   max_swing_hold_days=int(cfg.max_swing_hold_days) or 10)


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

    @property
    def sign(self) -> int:
        return 1 if self.play.side is Side.LONG else -1


# ---------------------------------------------------------------- day trades
def replay_intraday(strategies: Sequence[Strategy], symbol: str, bars: pd.DataFrame, daily: pd.DataFrame,
                    settings: ReplaySettings = ReplaySettings(), noise: NoiseSettings = NoiseSettings(),
                    sessions: Optional[int] = None) -> List[SimTrade]:
    """``bars``: 5-minute candles over several sessions; ``daily``: completed daily candles."""
    day_trades = [s for s in strategies if s.kind is StrategyKind.TECHNICAL and s.timeframe is Timeframe.INTRADAY]
    days = sorted(set(bars.index.date))
    trades: List[SimTrade] = []
    for day in days[-sessions:] if sessions else days:
        session = bars[bars.index.date == day]
        prior_daily = daily[daily.index.date < day]
        if not day_trades or len(prior_daily) < 20 or len(session) < settings.warmup_bars + 2:
            continue
        flatten_at = _at(day, clock.regular_close_time(day)) - pd.Timedelta(minutes=settings.flatten_before_close_min)
        trades += _replay_session(day_trades, symbol, session, bars, prior_daily, flatten_at, settings, noise)
    return trades


def _replay_session(strategies: Sequence[Strategy], symbol: str, session: pd.DataFrame, history: pd.DataFrame,
                    prior_daily: pd.DataFrame, flatten_at: pd.Timestamp, settings: ReplaySettings,
                    noise: NoiseSettings) -> List[SimTrade]:
    trades: List[SimTrade] = []
    open_positions: Dict[str, _Position] = {}
    seen_before: set = set()
    for i in range(settings.warmup_bars, len(session) - 1):
        closed_at, next_at, next_bar = session.index[i] + BAR, session.index[i + 1], session.iloc[i + 1]
        if closed_at >= flatten_at:
            break
        end = history.index.searchsorted(session.index[i], side="right")
        window = history.iloc[max(0, end - LIVE_INTRADAY_BARS):end]
        ctx = StrategyContext(symbol=symbol, intraday=window, daily=with_today(prior_daily, window),
                              quote=quote_from_price(symbol, float(window["close"].iloc[-1])),
                              now=closed_at.to_pydatetime())
        signals = _signals(strategies, ctx, noise)
        for strategy, play, flags in signals:
            if strategy.key not in open_positions:
                position = _enter(strategy.key, play, flags, (strategy.key, play.side) in seen_before, next_at,
                                  float(next_bar["open"]), settings.max_entry_drift_atr * ctx.intraday_atr, settings)
                if position is not None:
                    open_positions[strategy.key] = position
        seen_before = {(s.key, p.side) for s, p, _ in signals}

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
                 sessions: int = 120) -> List[SimTrade]:
    """Signals at each session's close, fills at the next open. Trades still open
    when the candles run out are left out - their result isn't known yet."""
    swing = [s for s in strategies if s.kind is StrategyKind.TECHNICAL and s.timeframe is Timeframe.SWING]
    trades: List[SimTrade] = []
    open_positions: Dict[str, _Position] = {}
    for i in range(max(60, len(daily) - sessions - 1), len(daily) - 1):
        history, next_at, next_bar = daily.iloc[:i + 1], daily.index[i + 1], daily.iloc[i + 1]
        closed_on = history.index[-1].date()
        ctx = StrategyContext(symbol=symbol, intraday=None, daily=history,
                              quote=quote_from_price(symbol, float(history["close"].iloc[-1])),
                              now=_at(closed_on, clock.regular_close_time(closed_on)).to_pydatetime())
        for strategy, play, flags in _signals(swing, ctx, noise) if swing else []:
            if strategy.key not in open_positions:
                position = _enter(strategy.key, play, flags, True, next_at, float(next_bar["open"]),
                                  settings.max_entry_drift_daily_atr * ctx.daily_atr, settings)
                if position is not None:
                    open_positions[strategy.key] = position
        for key, position in list(open_positions.items()):
            timed_out = position.bars_held + 1 >= settings.max_swing_hold_days
            done = _step(position, next_bar, next_at, settings, "time-stop" if timed_out else None)
            if done is not None:
                trades.append(done)
                del open_positions[key]
    return trades


# ---------------------------------------------------------------- shared
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
            if expected_r(play, ctx.daily_atr) < noise.min_expected_r:
                flags.append("low_expected_value")
            out.append((strategy, play, flags))
    if len({play.side for _, play, _ in out}) > 1:
        for _, _, flags in out:
            flags.append("conflict")
    return out


def _enter(key: str, play: Play, flags: List[str], confirmed: bool, at: pd.Timestamp, open_price: float,
           max_drift: float, settings: ReplaySettings) -> Optional[_Position]:
    limit = max_drift if max_drift > 0 else 0.5 * abs(play.entry - play.stop)   # no ATR yet: half the stop distance
    if abs(open_price - play.entry) > limit:
        return None                                  # ran away before it could be filled
    sign = 1 if play.side is Side.LONG else -1
    fill = open_price * (1 + sign * settings.slippage_bps / 1e4)
    risk = (fill - play.stop) * sign
    if risk <= 0:
        return None                                  # opened through the stop
    return _Position(key, play, fill, play.stop, risk, at, list(flags), confirmed, best=fill)


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
    target = position.play.targets[0]
    if (h >= target) if long else (low <= target):
        return _close(position, max(o, target) if long else min(o, target), bar_end, "target", settings, limit=True)

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
    if not limit:
        price *= 1 - position.sign * settings.slippage_bps / 1e4
    play = position.play
    return SimTrade(strategy=position.strategy, symbol=play.symbol, side=play.side.value,
                    timeframe=play.timeframe.value, entered_at=position.entered_at.isoformat(),
                    exited_at=at.isoformat(), entry=round(position.entry, 4), exit=round(price, 4),
                    r=round((price - position.entry) * position.sign / position.risk, 3), exit_reason=reason,
                    noise=list(position.noise), confirmed=position.confirmed)


def _at(day: dt.date, time: dt.time) -> pd.Timestamp:
    return pd.Timestamp(dt.datetime.combine(day, time), tz=NY)


# ---------------------------------------------------------------- what it means
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


def noise_report(trades: Sequence[SimTrade]) -> Dict[str, Dict[str, Any]]:
    """For each check: the trades it removes and how they did against the ones it keeps."""
    report = {check: _compare([t for t in trades if flagged(t, check)], [t for t in trades if not flagged(t, check)])
              for check in MEASURED_CHECKS}
    report["all_checks"] = _compare([t for t in trades if any(flagged(t, c) for c in MEASURED_CHECKS)],
                                    [t for t in trades if not any(flagged(t, c) for c in MEASURED_CHECKS)])
    return report


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


def strategy_records(trades: Sequence[SimTrade], skip_noise: Iterable[str] = (),
                     min_confirmations: int = 1) -> Dict[str, Dict[str, Any]]:
    """Each strategy's record over the trades Autopilot would actually have taken:
    none with a skipped noise flag, and day trades confirmed when it asks for that."""
    skip = set(skip_noise)
    taken = [t for t in trades if not skip.intersection(t.noise)
             and not (min_confirmations > 1 and flagged(t, "unconfirmed"))]
    by_strategy: Dict[str, List[SimTrade]] = {}
    for t in taken:
        by_strategy.setdefault(t.strategy, []).append(t)
    return {key: summarize(ts) for key, ts in by_strategy.items()}
