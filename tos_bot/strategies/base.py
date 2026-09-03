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

from ..analysis import SupportResistance, find_levels
from ..core.enums import AssetClass, Side, StrategyKind, Timeframe
from ..core.models import Play, Quote
from ..data.fundamentals import Financials
from ..indicators import ta
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
    #: enrichment from the scanner (rvol, gap_pct, atr_pct, float_category, ...)
    candidate: Dict[str, Any] = field(default_factory=dict)
    extras: Dict[str, Any] = field(default_factory=dict)
    _sr: Optional[SupportResistance] = None

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

    @property
    def time_of_day(self) -> str:
        return clock.time_of_day(self.now)

    def enough_intraday(self, bars: int = 20) -> bool:
        return self.intraday is not None and len(self.intraday) >= bars

    def enough_daily(self, bars: int = 60) -> bool:
        return self.daily is not None and len(self.daily) >= bars

    def today_intraday(self) -> pd.DataFrame:
        if self.intraday is None or not len(self.intraday):
            return self.intraday
        ny = self.intraday.index.tz_convert("America/New_York")
        return self.intraday[ny.date == clock.session_date(self.now)]

    def prev_close(self) -> float:
        if self.daily is None or len(self.daily) < 2:
            return self.price
        return float(self.daily["close"].iloc[-2])

    def levels(self) -> SupportResistance:
        if self._sr is None:
            try:
                self._sr = find_levels(self.daily, self.price, self.intraday)
            except Exception:  # noqa: BLE001
                self._sr = find_levels(pd.DataFrame(), self.price)
        return self._sr

    def daily_trend(self, lookback: int = 40) -> str:
        """Murphy: an uptrend = higher highs *and* higher lows; downtrend the
        mirror; else 'range'. Used to keep intraday longs aligned with the day."""
        if not self.enough_daily(lookback + 5):
            return "range"
        d = self.daily.tail(lookback)
        c = d["close"]
        sma20 = ta.sma(c, 20).iloc[-1]
        sma50 = ta.sma(c, 50).iloc[-1] if len(self.daily) >= 55 else sma20
        px = float(c.iloc[-1])
        half = len(d) // 2
        hh = d["high"].iloc[half:].max() > d["high"].iloc[:half].max()
        hl = d["low"].iloc[half:].min() > d["low"].iloc[:half].min()
        if px > sma20 >= sma50 and hh and hl:
            return "up"
        if px < sma20 <= sma50 and (not hh) and (not hl):
            return "down"
        return "range"

    def rvol(self) -> float:
        v = self.candidate.get("rvol")
        if v:
            return float(v)
        try:
            return float(ta.rel_volume_intraday(self.intraday))
        except Exception:  # noqa: BLE001
            return 1.0


def build_context(
    symbol: str,
    intraday: pd.DataFrame,
    daily: pd.DataFrame,
    quote: Quote,
    fundamentals: Optional[Financials] = None,
    peers: Optional[List[Financials]] = None,
    params: Optional[Dict[str, Any]] = None,
    account_equity: float = 0.0,
    candidate: Optional[Dict[str, Any]] = None,
) -> StrategyContext:
    return StrategyContext(
        symbol=symbol, intraday=intraday, daily=daily, quote=quote,
        fundamentals=fundamentals, peers=peers, params=params or {},
        account_equity=account_equity, candidate=candidate or {},
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

    #: may this setup be entered in pre/post-market too? (limit-only there)
    extended_hours_ok: bool = False

    #: how the setup behaves across Aziz's intraday sessions (Ch. 7). Momentum /
    #: breakout setups fade at Mid-day; reversal / mean-reversion setups are fine
    #: then; trend setups are *better* Late-Morning -> Close. Scales confidence
    #: only - never the geometry.  momentum | reversal | trend | swing
    tod_profile: str = "momentum"
    _TOD_WEIGHTS = {
        "momentum": {"OPEN": 1.00, "LATE_MORNING": 1.00, "MIDDAY": 0.75, "CLOSE": 0.85, "OFF": 1.0},
        "reversal": {"OPEN": 0.85, "LATE_MORNING": 1.00, "MIDDAY": 1.00, "CLOSE": 0.90, "OFF": 1.0},
        "trend":    {"OPEN": 0.80, "LATE_MORNING": 1.00, "MIDDAY": 1.00, "CLOSE": 0.95, "OFF": 1.0},
        "swing":    {"OPEN": 1.00, "LATE_MORNING": 1.00, "MIDDAY": 1.00, "CLOSE": 1.00, "OFF": 1.0},
    }

    #: how long the trade is *expected* to take:  (typical, review-after).
    #: units are MINUTES for INTRADAY setups, TRADING DAYS for SWING setups.
    #: purely informational - it never touches the stop; it just flags a
    #: position as "aging" / "overdue - eyeball it".
    expected_hold = (4.0, 10.0)

    #: geometry guard rails - a play outside these is almost always bad data
    #: (or, for MIN_STOP, a stop tightened to fake a good reward:risk - the
    #: exact anti-pattern Aziz warns about on p.66: "define a closer stop loss
    #: to have a more favorable ratio? The answer is NO.")
    MAX_STOP_PCT = 0.25          # protective stop no further than 25% from entry
    #: a protective stop *closer* than this to entry is noise, not a level -
    #: it will be taken out by a normal wiggle (see the QCOM/DLTR 0.2% stops
    #: that produced -3R fills). Widen to the floor, then re-check reward:risk.
    MIN_STOP_PCT = {"INTRADAY": 0.006, "SWING": 0.015}
    MIN_STOP_ATR = 0.9          # ...and at least this many intraday ATRs
    MAX_TARGET_PCT = {"INTRADAY": 0.15, "SWING": 0.45}
    MIN_TARGET_PCT = 0.002
    RR_BOUNDS = (0.4, 8.0)      # an intraday "RR 9:1" is a fake-tight-stop artifact

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
        invalidation: str = "",
        edge_note: str = "",
        probability: Optional[float] = None,
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

        # widen a noise-tight stop to a real floor (% of price AND intraday ATR)
        floor = entry * self.MIN_STOP_PCT.get(self.timeframe.value, 0.006)
        if self.timeframe is Timeframe.INTRADAY:
            try:
                iatr = float(ta.atr(ctx.intraday, 14).iloc[-1])
                if iatr == iatr and iatr > 0:      # not NaN
                    floor = max(floor, self.MIN_STOP_ATR * iatr)
            except Exception:  # noqa: BLE001
                pass
        if abs(entry - stop) < floor:
            stop = entry - floor if side is Side.LONG else entry + floor

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

        tags = tags or []
        # Swing setups (and explicit gap/catalyst plays) can be entered in the
        # pre / post-market session; pure intraday structure setups cannot.
        ext_ok = (
            self.extended_hours_ok
            or self.timeframe is Timeframe.SWING
            or any(t in ("gap", "catalyst") for t in tags)
        )
        hold_typ, hold_max = self.expected_hold
        # allow a per-strategy config override
        hold_typ = float(self.params.get("hold_typical", hold_typ))
        hold_max = float(self.params.get("hold_max", hold_max))

        # -- Aziz Ch. 7: an edge is worth less in the wrong session ------- #
        tod = "OFF"
        if self.timeframe is Timeframe.INTRADAY:
            tod = clock.time_of_day(ctx.now)
            mult = self._TOD_WEIGHTS.get(self.tod_profile, {}).get(tod, 1.0)
            confidence = confidence * mult
            evidence = {**evidence, "time_of_day": tod, "tod_weight": round(mult, 2)}
        confidence = max(0.0, min(1.0, confidence))

        # -- Douglas: state the edge as a probability, never a promise ---- #
        if probability is None:
            probability = 0.40 + 0.28 * confidence
        probability = max(0.05, min(0.90, float(probability)))

        risk_ps = abs(entry - stop)
        if not invalidation:
            side_word = "below" if side is Side.LONG else "above"
            invalidation = (f"a 5-minute close {side_word} {stop:.2f} (the protective "
                            f"stop / the technical level the idea rests on)")

        explanation = self._compose_explanation(
            side, entry, stop, targets, rationale, detail,
            invalidation=invalidation, edge_note=edge_note,
            probability=probability, tod=tod,
        )
        play = Play(
            symbol=ctx.symbol, side=side, strategy=self.key, kind=self.kind,
            timeframe=self.timeframe, entry=round(entry, 4), stop=round(stop, 4),
            targets=[round(t, 4) for t in targets],
            confidence=confidence,
            rationale=rationale, explanation=explanation,
            invalidation=invalidation, probability=round(probability, 3),
            evidence=evidence, tags=tags, asset_class=AssetClass.EQUITY,
            extended_hours_ok=ext_ok,
            expected_hold_typical=hold_typ, expected_hold_max=hold_max,
            expires_at=ctx.now.astimezone(dt.timezone.utc) + dt.timedelta(minutes=ttl_minutes),
        )
        return play

    def _compose_explanation(
        self, side: Side, entry: float, stop: float, targets: List[float],
        rationale: str, detail: str, invalidation: str = "", edge_note: str = "",
        probability: float = 0.5, tod: str = "",
    ) -> str:
        """The hover pop-up. Framed the way Douglas (*Trading in the Zone*)
        argues a trader must hold a position: it is one execution of an edge -
        a higher probability of one thing over another - not a forecast. The
        numbers up top are the plan; the paragraph at the bottom is how to
        carry the trade without breaking the rules."""
        risk_ps = abs(entry - stop)
        risk_pct = (risk_ps / entry * 100.0) if entry else 0.0
        rr = risk_ps and abs(targets[0] - entry) / risk_ps or 0.0
        d = ("LONG - you buy, and profit if it rises" if side is Side.LONG
             else "SHORT - you sell short, and profit if it falls")
        tgt = f"first target {targets[0]:.2f}  (reward:risk {rr:.1f} : 1)"
        if len(targets) > 1:
            tgt += "; then " + ", ".join(f"{t:.2f}" for t in targets[1:])
        when = tod.replace("_", "-").lower() if tod and tod != "OFF" else "this setup"

        blocks = [
            f"{self.title} - {d}",
            f"THE EDGE (what tends to happen here): {self.thesis}",
            f"RIGHT NOW ({when}): {detail}",
        ]
        if edge_note:
            blocks.append(f"READING THE TAPE: {edge_note}")
        blocks.append(
            "THE PLAN\n"
            f"   - enter near {entry:.2f}\n"
            f"   - protective stop {stop:.2f}  ->  you risk {risk_ps:.2f}/share "
            f"({risk_pct:.1f}% of price) to find out whether the edge pays\n"
            f"   - {tgt}\n"
            f"   - estimated odds the edge pays: ~{probability * 100:.0f}%  "
            f"(a probability over many trades, not a call on this one)"
        )
        blocks.append(
            f"INVALIDATION: {invalidation}. If price gets there the reason for the "
            f"trade is gone - the automatic exit handles it, no decision needed."
        )
        blocks.append(
            "HOW TO HOLD IT (Douglas): this is one roll of an edge, not a prediction. "
            "Wins and losses land randomly around it, so a textbook setup can still "
            f"lose - that is normal, not a mistake. Accept the {risk_ps:.2f}/share loss "
            "before you click; if you can't, skip the trade. Then leave it alone - "
            "don't widen the stop and don't add size to be right."
        )
        return "\n\n".join(blocks)


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
