"""Single- and two-bar candlestick reads used as entry triggers.

Aziz uses these as *confirmation* that a move is ending: a doji / spinning
top after a run = indecision; a hammer / shooting star = the other side lost
its push; an engulfing bar = a decisive handover. Murphy's reversal-bar
concepts are the same idea.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class CandleRead:
    name: str            # doji | hammer | shooting_star | bull_engulf | bear_engulf | strong_bull | strong_bear | none
    bullish: Optional[bool]      # True / False / None (neutral)
    indecision: bool

    @property
    def is_reversal_up(self) -> bool:
        return self.name in ("hammer", "bull_engulf", "strong_bull") or (self.indecision and self.bullish is not False)

    @property
    def is_reversal_down(self) -> bool:
        return self.name in ("shooting_star", "bear_engulf", "strong_bear") or (self.indecision and self.bullish is not True)


def _parts(o: float, h: float, low: float, c: float):
    rng = max(h - low, 1e-9)
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - low
    return rng, body, upper, lower


def is_doji(o, h, low, c, body_max: float = 0.1) -> bool:
    rng, body, *_ = _parts(o, h, low, c)
    return body / rng <= body_max


def is_hammer(o, h, low, c, wick_mult: float = 2.0, upper_max: float = 0.25) -> bool:
    rng, body, upper, lower = _parts(o, h, low, c)
    return lower >= wick_mult * body and upper <= upper_max * rng and body / rng < 0.5


def is_shooting_star(o, h, low, c, wick_mult: float = 2.0, lower_max: float = 0.25) -> bool:
    rng, body, upper, lower = _parts(o, h, low, c)
    return upper >= wick_mult * body and lower <= lower_max * rng and body / rng < 0.5


def is_engulfing(o, h, low, c, po, pc) -> Optional[str]:
    """Two-bar: current body engulfs the previous body."""
    prev_bull = pc >= po
    cur_bull = c >= o
    if cur_bull and not prev_bull and c >= po and o <= pc:
        return "bull_engulf"
    if not cur_bull and prev_bull and c <= po and o >= pc:
        return "bear_engulf"
    return None


def classify_candle(o, h, low, c, prev_o=None, prev_c=None) -> CandleRead:
    rng, body, upper, lower = _parts(o, h, low, c)
    if prev_o is not None and prev_c is not None:
        eng = is_engulfing(o, h, low, c, prev_o, prev_c)
        if eng == "bull_engulf":
            return CandleRead("bull_engulf", True, False)
        if eng == "bear_engulf":
            return CandleRead("bear_engulf", False, False)
    if is_hammer(o, h, low, c):
        return CandleRead("hammer", True, False)
    if is_shooting_star(o, h, low, c):
        return CandleRead("shooting_star", False, False)
    if is_doji(o, h, low, c):
        return CandleRead("doji", None, True)
    # spinning top: small body, wicks both sides
    if body / rng < 0.35 and upper / rng > 0.25 and lower / rng > 0.25:
        return CandleRead("doji", None, True)
    if body / rng > 0.7:
        return CandleRead("strong_bull" if c >= o else "strong_bear", c >= o, False)
    return CandleRead("none", c >= o, False)


def read_row(df, i: int = -1) -> CandleRead:
    """Classify bar ``i`` of an OHLC DataFrame (uses bar i-1 for engulfing)."""
    r = df.iloc[i]
    po = pc = None
    if len(df) >= abs(i) + 1:
        p = df.iloc[i - 1]
        po, pc = float(p["open"]), float(p["close"])
    return classify_candle(float(r["open"]), float(r["high"]), float(r["low"]),
                           float(r["close"]), po, pc)
