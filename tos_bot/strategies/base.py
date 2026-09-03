"""Strategy contract + the context object handed to every strategy each scan.

A strategy inspects a :class:`StrategyContext` (already-fetched bars, quote,
optional fundamentals) and returns zero or one :class:`Play`. It never places
orders and never touches the database.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from ..core.enums import AssetClass, Side, StrategyKind, Timeframe
from ..core.models import Play, Quote
from ..data.fundamentals import Financials
from ..util import clock


# --------------------------------------------------------------------------- #
#  Context                                                                   #
# --------------------------------------------------------------------------- #
@dataclass
class StrategyContext:
    symbol: str
    intraday: pd.DataFrame           # 5-minute OHLCV, multi-session, tz-aware
    daily: pd.DataFrame              # daily OHLCV, ~400 sessions
    quote: Quote
    now: dt.datetime = field(default_factory=clock.now_ny)
    fundamentals: Optional[Financials] = None
    peers: Optional[List[Financials]] = None
    params: Dict[str, Any] = field(default_factory=dict)
    account_equity: float = 0.0
    extras: Dict[str, Any] = field(default_factory=dict)

    # -- convenience accessors ------------------------------------------- #
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

    def today_intraday(self) -> pd.DataFrame:
        if self.intraday is None or not len(self.intraday):
            return self.intraday
        ny = self.intraday.index.tz_convert("America/New_York")
        return self.intraday[ny.date == clock.session_date(self.now)]


def build_context(
    symbol: str,
    intraday: pd.DataFrame,
    daily: pd.DataFrame,
    quote: Quote,
    fundamentals: Optional[Financials] = None,
    peers: Optional[List[Financials]] = None,
    params: Optional[Dict[str, Any]] = None,
    account_equity: float = 0.0,
) -> StrategyContext:
    return StrategyContext(
        symbol=symbol, intraday=intraday, daily=daily, quote=quote,
        fundamentals=fundamentals, peers=peers, params=params or {},
        account_equity=account_equity,
    )


# --------------------------------------------------------------------------- #
#  Strategy base                                                             #
# --------------------------------------------------------------------------- #
class Strategy:
    #: registry key, must match config
    key: str = "base"
    kind: StrategyKind = StrategyKind.TECHNICAL
    timeframe: Timeframe = Timeframe.INTRADAY
    #: short human title shown in the UI
    title: str = "Base strategy"
    #: the *generic* explanation of the setup (hover pop-up header). The
    #: per-play explanation appends the specific numbers.
    thesis: str = ""
    default_params: Dict[str, Any] = {}

    def __init__(self, params: Optional[Dict[str, Any]] = None, weight: float = 1.0) -> None:
        self.params = {**self.default_params, **(params or {})}
        self.weight = weight

    # -- to implement ------------------------------------------------- #
    def generate(self, ctx: StrategyContext) -> List[Play]:
        raise NotImplementedError

    # -- helpers ---------------------------------------------------- #
    def describe(self) -> Dict[str, str]:
        return {"key": self.key, "title": self.title, "kind": self.kind.value,
                "timeframe": self.timeframe.value, "thesis": self.thesis}

    #: geometry guard rails - a play outside these is almost always bad data
    MAX_STOP_PCT = 0.25          # protective stop no further than 25% from entry
    MAX_TARGET_PCT = {"INTRADAY": 0.15, "SWING": 0.45}
    MIN_TARGET_PCT = 0.002
    RR_BOUNDS = (0.4, 25.0)

    def _mk_play(
        self,
        ctx: StrategyContext,
        side: Side,
        entry: float,
        stop: float,
        targets: List[float],
        confidence: float,
        rationale: str,
        detail: str,
        evidence: Dict[str, Any],
        tags: Optional[List[str]] = None,
        ttl_minutes: int = 45,
    ) -> Optional[Play]:
        if entry <= 0 or stop <= 0 or not targets:
            return None
        # discard nonsensical direction
        if side is Side.LONG and (stop >= entry or targets[0] <= entry):
            return None
        if side is Side.SHORT and (stop <= entry or targets[0] >= entry):
            return None

        # clamp an over-wide stop
        max_stop = entry * self.MAX_STOP_PCT
        if side is Side.LONG:
            stop = max(stop, entry - max_stop)
        else:
            stop = min(stop, entry + max_stop)

        # clamp / reject over-far targets
        max_t = entry * self.MAX_TARGET_PCT.get(self.timeframe.value, 0.4)
        clamped = []
        for t in targets:
            if side is Side.LONG:
                clamped.append(min(t, entry + max_t))
            else:
                clamped.append(max(t, entry - max_t))
        targets = clamped
        if abs(targets[0] - entry) / entry < self.MIN_TARGET_PCT:
            return None

        rr = abs(targets[0] - entry) / abs(entry - stop) if entry != stop else 0.0
        if not (self.RR_BOUNDS[0] <= rr <= self.RR_BOUNDS[1]):
            return None

        explanation = self._compose_explanation(side, entry, stop, targets, rationale, detail)
        play = Play(
            symbol=ctx.symbol, side=side, strategy=self.key, kind=self.kind,
            timeframe=self.timeframe, entry=round(entry, 4), stop=round(stop, 4),
            targets=[round(t, 4) for t in targets],
            confidence=max(0.0, min(1.0, confidence)),
            rationale=rationale, explanation=explanation, evidence=evidence,
            tags=tags or [], asset_class=AssetClass.EQUITY,
            expires_at=ctx.now.astimezone(dt.timezone.utc) + dt.timedelta(minutes=ttl_minutes),
        )
        return play

    def _compose_explanation(
        self, side: Side, entry: float, stop: float, targets: List[float],
        rationale: str, detail: str,
    ) -> str:
        rr = abs(targets[0] - entry) / abs(entry - stop) if entry != stop else 0.0
        d = "LONG (buy, profit if it rises)" if side is Side.LONG else \
            "SHORT (sell/borrow, profit if it falls)"
        return (
            f"{self.title} - {d}\n\n"
            f"What the setup means: {self.thesis}\n\n"
            f"Why now: {detail}\n\n"
            f"Plan: enter near {entry:.2f}, protective stop at {stop:.2f} "
            f"(risk {abs(entry - stop):.2f}/share), first target {targets[0]:.2f} "
            f"(reward:risk {rr:.1f}:1)."
            + (f" Further targets: {', '.join(f'{t:.2f}' for t in targets[1:])}." if len(targets) > 1 else "")
        )


def swing_low(series: pd.Series, lookback: int = 10) -> float:
    return float(series.tail(lookback).min())


def swing_high(series: pd.Series, lookback: int = 10) -> float:
    return float(series.tail(lookback).max())


def safe_last(series: pd.Series, default: float = math.nan) -> float:
    try:
        v = float(series.iloc[-1])
        return v if not math.isnan(v) else default
    except Exception:  # noqa: BLE001
        return default
