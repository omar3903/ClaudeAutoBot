"""A pair: two stocks whose spread keeps coming back, and the rules for trading it.

For the log prices a and b of two stocks, the cointegrating regression (Vidyamurthy,
*Pairs Trading*, ch. 5; Chan, *Algorithmic Trading*, ch. 2) is

    a(t) = γ b(t) + μ + ε(t)

and the spread a(t) - γ b(t) keeps returning to its mean. Holding $V of the first stock
against $γV of the second earns V times the change in that spread: the P/L of long $V of
A and short $γV of B is V (Δa - γ Δb), whatever the market as a whole does.

The rules:

- **z-score** - the spread against its moving average, in moving standard deviations,
  over a lookback equal to the spread's half-life (Chan, ch. 3, "Bollinger bands");
- **entry** when |z| reaches the band - long the spread (buy the first stock, short the
  second) below -band, short it above +band. The band comes from Vidyamurthy's
  nonparametric design, net of the trading costs (ch. 8; see quant/bands.py);
- **exit** when z is back at the mean (Vidyamurthy: exit at the mean);
- **stop** well beyond the band - Chan (ch. 8) sets a mean-reversion stop wider than
  anything the backtest saw: it guards against a relationship that has broken, it isn't
  a routine exit;
- **time stop** after about two half-lives - a spread that hasn't come back in the time
  it usually takes may no longer be the same spread (Vidyamurthy, ch. 7).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import numpy as np

KEY = "pairs_reversion"
LONG_SPREAD = "LONG_SPREAD"          # buy the first stock, short the second
SHORT_SPREAD = "SHORT_SPREAD"        # short the first stock, buy the second


@dataclass(frozen=True)
class PairRules:
    entry_bounds: Tuple[float, float] = (1.0, 2.5)     # the band, in moving standard deviations
    exit_z: float = 0.0
    stop_beyond_entry: float = 2.0
    time_stop_half_lives: float = 2.0
    time_stop_bounds: Tuple[int, int] = (5, 40)         # sessions
    lookback_bounds: Tuple[int, int] = (10, 60)         # sessions
    cost_bps: float = 6.0                               # each leg, each fill: slippage + commission

    @classmethod
    def from_config(cls, cfg, cost_bps: Optional[float] = None) -> "PairRules":
        return cls(entry_bounds=_pair(getattr(cfg, "entry_z", (1.0, 2.5)), float),
                   exit_z=float(getattr(cfg, "exit_z", 0.0)),
                   stop_beyond_entry=float(getattr(cfg, "stop_beyond_entry", 2.0)),
                   time_stop_half_lives=float(getattr(cfg, "time_stop_half_lives", 2.0)),
                   cost_bps=float(cost_bps if cost_bps is not None else getattr(cfg, "cost_bps", 6.0)))


@dataclass
class PairModel:
    first: str                       # the stock bought on a long spread
    second: str                      # the stock shorted on a long spread
    hedge: float                     # γ: dollars of the second stock per dollar of the first
    half_life: float                 # sessions
    lookback: int                    # sessions in the moving average and standard deviation
    entry_z: float
    exit_z: float
    stop_z: float
    time_stop_days: int
    adf_stat: float
    correlation: float
    crossings: int
    group: str = ""                  # the industry both stocks are in
    fitted_through: str = ""         # the last session the model was fitted on
    validation: Dict[str, Any] = field(default_factory=dict)    # how the rules did out of sample

    @property
    def id(self) -> str:
        return f"{self.first}/{self.second}"

    def as_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "id": self.id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PairModel":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


def spread(log_first, log_second, hedge: float) -> np.ndarray:
    return np.asarray(log_first, dtype=float) - hedge * np.asarray(log_second, dtype=float)


def rolling_stats(series, lookback: int) -> Tuple[np.ndarray, np.ndarray]:
    """The z-score and standard deviation of each value against the ``lookback`` values
    ending with it; NaN until there are that many."""
    s = np.asarray(series, dtype=float)
    z, sd = np.full(len(s), np.nan), np.full(len(s), np.nan)
    if lookback < 2 or len(s) < lookback:
        return z, sd
    windows = np.lib.stride_tricks.sliding_window_view(s, lookback)
    mean, dev = windows.mean(axis=1), windows.std(axis=1, ddof=1)
    safe = np.where(dev > 0, dev, 1.0)
    z[lookback - 1:] = np.where(dev > 0, (s[lookback - 1:] - mean) / safe, np.nan)
    sd[lookback - 1:] = dev
    return z, sd


def current_z(model: PairModel, closes_first: Sequence[float], closes_second: Sequence[float],
              price_first: Optional[float] = None, price_second: Optional[float] = None) -> Tuple[float, float]:
    """Today's z-score and the spread's standard deviation, from aligned completed closes and,
    when both are given, the live prices standing in for today's close."""
    a = np.log(np.asarray(closes_first, dtype=float))
    b = np.log(np.asarray(closes_second, dtype=float))
    if price_first and price_second:
        a, b = np.append(a, math.log(price_first)), np.append(b, math.log(price_second))
    window = spread(a, b, model.hedge)[-model.lookback:]
    if len(window) < model.lookback:
        return math.nan, math.nan
    sd = float(np.std(window, ddof=1))
    return ((float(window[-1]) - float(np.mean(window))) / sd if sd > 0 else math.nan), sd


def signal(z: float, holding: Optional[str], days_held: int, model: PairModel) -> Optional[str]:
    """What the rules say at ``z``: "enter_long" or "enter_short" when flat; "stop", "exit"
    (back at the mean) or "time" when holding; None to do nothing."""
    if z is None or not math.isfinite(z):
        return None
    if holding is None:
        if z <= -model.entry_z:
            return "enter_long"
        if z >= model.entry_z:
            return "enter_short"
        return None
    long = holding == LONG_SPREAD
    if (z <= -model.stop_z) if long else (z >= model.stop_z):
        return "stop"
    if (z >= -model.exit_z) if long else (z <= model.exit_z):
        return "exit"
    if days_held >= model.time_stop_days:
        return "time"
    return None


@dataclass(frozen=True)
class PairSize:
    qty_first: int
    qty_second: int
    value_first: float
    value_second: float
    dollar_risk: float               # the loss if the spread runs from here to the stop
    risk_z: float
    caps: Tuple[str, ...] = ()


def size_pair(model: PairModel, price_first: float, price_second: float, z: float, spread_sd: float,
              risk_dollars: float, max_leg_value: float, buying_power: float = 0.0) -> PairSize:
    """Shares for each leg. $V in the first stock loses about V × (the z distance to the stop)
    × (the spread's standard deviation) if the spread runs to the stop, so V is the risk budget
    over that; the second leg is γ × V dollars. Neither leg may exceed ``max_leg_value``, and
    both together not the buying power."""
    risk_z = max(1.0, model.stop_z - abs(z)) if math.isfinite(z) else model.stop_z
    per_dollar = risk_z * spread_sd if spread_sd and math.isfinite(spread_sd) else 0.0
    if per_dollar <= 0 or price_first <= 0 or price_second <= 0 or model.hedge <= 0 or risk_dollars <= 0:
        return PairSize(0, 0, 0.0, 0.0, 0.0, risk_z, ("no size - missing price, spread or risk budget",))
    caps = []
    value = risk_dollars / per_dollar
    leg_cap = max_leg_value / max(1.0, model.hedge)
    if value > leg_cap:
        value, caps = leg_cap, caps + ["max position % of equity"]
    if buying_power and value * (1 + model.hedge) > buying_power:
        value, caps = buying_power / (1 + model.hedge), caps + ["buying power"]
    qty_first = int(value // price_first)
    qty_second = int(round(model.hedge * qty_first * price_first / price_second))
    if qty_first < 1 or qty_second < 1:
        return PairSize(0, 0, 0.0, 0.0, 0.0, risk_z, tuple(caps + ["too small for a share of each stock"]))
    value_first = qty_first * price_first
    return PairSize(qty_first, qty_second, round(value_first, 2), round(qty_second * price_second, 2),
                    round(value_first * per_dollar, 2), round(risk_z, 3), tuple(caps))


def pair_pl(side: str, qty_first: float, entry_first: float, price_first: float,
            qty_second: float, entry_second: float, price_second: float) -> float:
    """Open P/L of a pair trade: the first leg's gain less the second's, turned round for a short spread."""
    sign = 1.0 if side == LONG_SPREAD else -1.0
    return sign * (qty_first * (price_first - entry_first) - qty_second * (price_second - entry_second))


def _pair(value, kind) -> tuple:
    lo, hi = value
    return kind(lo), kind(hi)
