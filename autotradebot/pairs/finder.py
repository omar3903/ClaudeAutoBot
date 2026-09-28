"""Finding pairs worth watching (Vidyamurthy, *Pairs Trading*, ch. 6-7; Chan, *Algorithmic
Trading*, ch. 2-4).

1. Both stocks are in the same industry (IBKR's), trade at least $10M a day and cost at
   least $5.
2. Their daily returns move together - a correlation of at least 0.6 over the fitting
   window. Vidyamurthy uses this "distance" only to narrow the search; it proves nothing.
3. Their log prices are cointegrated: Engle-Granger at 5% (both orders tried, the more
   negative statistic kept) with a positive hedge ratio, and Johansen's trace test agreeing
   at 90%.
4. The spread is tradable: a half-life of 2 to 30 sessions and at least 6 crossings of its
   mean in the window.
5. Each stock is in one pair at most, an industry gives two at most, and the most strongly
   cointegrated pairs are kept.

The band comes from Vidyamurthy's nonparametric design on the window's z-scores, net of a
round trip's costs; the stop, the time stop and the lookback follow from the band and the
half-life.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..quant import bands, cointegration, stationarity
from .model import PairModel, PairRules, rolling_stats, spread


@dataclass(frozen=True)
class FinderSettings:
    min_correlation: float = 0.6
    min_price: float = 5.0
    min_dollar_volume: float = 10_000_000.0
    fit_days: int = 200
    half_life_bounds: Tuple[float, float] = (2.0, 30.0)
    min_crossings: int = 6
    per_group: int = 2
    max_pairs: int = 12
    candidates_per_group: int = 15          # the most correlated pairs tested in each industry

    @classmethod
    def from_config(cls, cfg) -> "FinderSettings":
        lo, hi = getattr(cfg, "half_life_days", (2.0, 30.0))
        return cls(min_correlation=float(cfg.min_correlation), min_price=float(cfg.min_price),
                   min_dollar_volume=float(cfg.min_dollar_volume), fit_days=int(cfg.fit_days),
                   half_life_bounds=(float(lo), float(hi)), min_crossings=int(cfg.min_crossings),
                   per_group=int(cfg.per_group), max_pairs=int(cfg.max_pairs))


def aligned_closes(frame_a: pd.DataFrame, frame_b: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray, List[dt.date]]:
    """Both stocks' closes on the sessions they both have, oldest first."""
    joined = pd.concat([frame_a["close"].rename("a"), frame_b["close"].rename("b")], axis=1, join="inner").dropna()
    joined = joined[(joined["a"] > 0) & (joined["b"] > 0)]
    return joined["a"].to_numpy(dtype=float), joined["b"].to_numpy(dtype=float), [d.date() for d in joined.index]


def liquid(frame: Optional[pd.DataFrame], settings: FinderSettings) -> bool:
    if frame is None or len(frame) < settings.fit_days:
        return False
    tail = frame.tail(20)
    return (float(tail["close"].iloc[-1]) >= settings.min_price
            and float((tail["close"] * tail["volume"]).mean()) >= settings.min_dollar_volume)


def fit_pair(symbol_a: str, symbol_b: str, closes_a: Sequence[float], closes_b: Sequence[float],
             rules: PairRules, settings: FinderSettings, group: str = "", through: str = "") -> Optional[PairModel]:
    """Test and fit one candidate on the closes given (its fitting window). The model's first
    stock is the regression's dependent one. None when it doesn't qualify."""
    la, lb = np.log(np.asarray(closes_a, dtype=float)), np.log(np.asarray(closes_b, dtype=float))
    fit = cointegration.engle_granger(la, lb)
    if fit is None or not fit.cointegrated("5%") or fit.hedge <= 0:
        return None
    lo, hi = settings.half_life_bounds
    if not lo <= fit.half_life <= hi:
        return None
    test = cointegration.johansen(np.column_stack([la, lb]))
    if test is None or test.rank("90%") < 1:
        return None
    hold = stationarity.holding_period(fit.spread)
    crossings = int(hold["crossings"]) if hold else 0
    if crossings < settings.min_crossings:
        return None
    first, second, log_first, log_second = ((symbol_a, symbol_b, la, lb) if fit.dependent == 0
                                            else (symbol_b, symbol_a, lb, la))
    lookback = int(min(rules.lookback_bounds[1], max(rules.lookback_bounds[0], round(fit.half_life))))
    z, sd = rolling_stats(spread(log_first, log_second, fit.hedge), lookback)
    typical_sd = float(np.nanmedian(sd)) if np.isfinite(sd).any() else 0.0
    if typical_sd <= 0:
        return None
    # a round trip pays the cost on both legs twice; in the spread's units per dollar of the first stock
    cost_sigma = 2 * (1 + fit.hedge) * rules.cost_bps / 1e4 / typical_sd
    band = bands.best_band(z[np.isfinite(z)], cost=cost_sigma, low=rules.entry_bounds[0], high=rules.entry_bounds[1])
    if band is None:
        return None
    lo_t, hi_t = rules.time_stop_bounds
    return PairModel(
        first=first, second=second, hedge=round(float(fit.hedge), 4), half_life=round(float(fit.half_life), 2),
        lookback=lookback, entry_z=round(band, 2), exit_z=rules.exit_z,
        stop_z=round(band + rules.stop_beyond_entry, 2),
        time_stop_days=int(min(hi_t, max(lo_t, round(rules.time_stop_half_lives * fit.half_life)))),
        adf_stat=round(float(fit.adf_stat), 3),
        correlation=round(float(cointegration.return_correlation(closes_a, closes_b)), 3),
        crossings=crossings, group=group, fitted_through=through)


def find_pairs(frames: Mapping[str, pd.DataFrame], groups: Mapping[str, str], rules: PairRules,
               settings: FinderSettings) -> List[PairModel]:
    """The pairs worth watching among ``frames`` (daily candles), fitted on each group's last
    ``settings.fit_days`` sessions. ``groups`` maps a symbol to its industry."""
    members: Dict[str, List[str]] = defaultdict(list)
    for symbol, frame in frames.items():
        if groups.get(symbol) and liquid(frame, settings):
            members[groups[symbol]].append(symbol)
    candidates: List[PairModel] = []
    for group, symbols in members.items():
        if len(symbols) < 2:
            continue
        closes = pd.concat({s: frames[s]["close"] for s in symbols}, axis=1, join="inner").dropna()
        closes = closes[(closes > 0).all(axis=1)].tail(settings.fit_days)
        if len(closes) < min(settings.fit_days, 120):
            continue
        corr = np.log(closes).diff().dropna().corr().to_numpy()
        ranked = sorted(((corr[i, j], symbols[i], symbols[j]) for i in range(len(symbols))
                         for j in range(i + 1, len(symbols)) if corr[i, j] >= settings.min_correlation), reverse=True)
        through = closes.index[-1].date().isoformat()
        for _, a, b in ranked[:settings.candidates_per_group]:
            model = fit_pair(a, b, closes[a].to_numpy(), closes[b].to_numpy(), rules, settings, group, through)
            if model is not None:
                candidates.append(model)
    chosen: List[PairModel] = []
    used: set = set()
    per_group: Counter = Counter()
    for model in sorted(candidates, key=lambda m: m.adf_stat):
        if model.first in used or model.second in used or per_group[model.group] >= settings.per_group:
            continue
        chosen.append(model)
        used |= {model.first, model.second}
        per_group[model.group] += 1
        if len(chosen) >= settings.max_pairs:
            break
    return chosen
