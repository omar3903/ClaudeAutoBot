"""The market's regime - calm or turbulent - from Hamilton's Markov switching model
(*Time Series Analysis*, ch. 22; see quant/regime.py), fitted on SPY's daily returns.

SPY is only the benchmark here, never traded. Three years of its daily candles are
downloaded at most once a session and kept on disk, and the model is refitted when a new
session has closed. The probability the scans use for a session is the one-step-ahead
forecast from the returns through the session before - P(turbulent today) = P ξ(t-1|t-1),
Hamilton's [22.4.6] - so nothing that happens today feeds into it.

The replay asks for the same forecast for every past session, from a model fitted only on
the sessions before the replay's first day when there are enough of them - otherwise the
parameters would have seen the days being replayed.
"""

from __future__ import annotations

import datetime as dt
import logging
import pickle
import threading
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import numpy as np
import pandas as pd

from ..quant import regime
from ..util import clock

log = logging.getLogger(__name__)

BENCHMARK = "SPY"
DURATION = "5 Y"              # two years to fit on before a three-year replay begins, so its regime flags never look ahead
MIN_RETURNS = 250
TURBULENT = 0.5


class MarketRegime:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._frame: Optional[pd.DataFrame] = None
        self._reading: Optional[Dict[str, Any]] = None
        #: whether the last replay history had to use a model fitted on its own days
        self.look_ahead = False

    # ---- today ------------------------------------------------------------------ #
    def refresh(self, source, con_ids: Optional[Mapping[str, int]] = None,
                through: Optional[dt.date] = None) -> Optional[Dict[str, Any]]:
        """Bring SPY's candles up to ``through`` (the last completed session) and refit the model
        when they moved on. Safe to call on every scan: it only downloads once per session."""
        through = through or clock.prev_trading_day(clock.session_date())
        with self._lock:
            frame = self._load()
            if (frame is None or frame.index[-1].date() < through) and source is not None:
                try:
                    got = source.history_many({BENCHMARK: ("1 day", DURATION)}, con_ids)
                except Exception:  # noqa: BLE001
                    log.debug("could not download %s's candles for the market regime", BENCHMARK, exc_info=True)
                    got = {}
                new = got.get(BENCHMARK)
                if new is not None and len(new):
                    frame = new[new.index.date <= through]
                    self._save(frame)
            if frame is None or len(frame) < MIN_RETURNS + 1:
                return self._reading
            as_of = frame.index[-1].date().isoformat()
            if self._reading is None or self._reading["as_of"] != as_of:
                self._reading = self._fit_today(frame)
            return self._reading

    def reading(self) -> Optional[Dict[str, Any]]:
        return self._reading

    def context(self) -> Dict[str, Any]:
        """What a play's context carries: the probability for the next session, and its name."""
        r = self._reading
        return {} if r is None else {"p_turbulent": r["p_turbulent"], "regime": r["regime"]}

    def _fit_today(self, frame: pd.DataFrame) -> Optional[Dict[str, Any]]:
        returns = np.diff(np.log(frame["close"].to_numpy(dtype=float)))
        fit = regime.fit_markov_switching(returns[-3 * MIN_RETURNS:])
        if fit is None:
            return None
        ahead = fit.transition @ fit.filtered[-1]
        p = float(ahead[fit.turbulent])
        last = frame.index[-1].date()
        return {"p_turbulent": round(p, 3), "regime": "turbulent" if p >= TURBULENT else "calm",
                "as_of": last.isoformat(), "for_session": clock.next_trading_day(last).isoformat(),
                "p_turbulent_as_of": round(fit.p_turbulent, 3), **fit.as_dict(), "benchmark": BENCHMARK}

    # ---- past sessions ------------------------------------------------------------- #
    def history(self, first_day: dt.date) -> Dict[dt.date, float]:
        """P(turbulent) for each session in the stored candles, as forecast the evening before,
        from a model fitted on the sessions before ``first_day`` when there are enough."""
        frame = self._load()
        if frame is None or len(frame) < MIN_RETURNS + 2:
            return {}
        dates = [d.date() for d in frame.index]
        returns = np.diff(np.log(frame["close"].to_numpy(dtype=float)))
        before = sum(1 for d in dates[1:] if d < first_day)
        self.look_ahead = before < MIN_RETURNS
        fit = regime.fit_markov_switching(returns if self.look_ahead else returns[:before])
        if fit is None:
            return {}
        filtered = regime.filtered_probabilities(returns, fit)
        ahead = filtered @ fit.transition.T                  # row t: the forecast for the session after t
        out = {dates[t + 2]: float(ahead[t, fit.turbulent]) for t in range(len(returns) - 1)}
        out[clock.next_trading_day(dates[-1])] = float(ahead[-1, fit.turbulent])
        return out

    # ---- storage ------------------------------------------------------------------ #
    def _load(self) -> Optional[pd.DataFrame]:
        if self._frame is None:
            try:
                self._frame = pd.read_pickle(self.path)
            except (OSError, ValueError, EOFError, pickle.UnpicklingError):
                return None
        return self._frame

    def _save(self, frame: pd.DataFrame) -> None:
        self._frame = frame
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            frame.to_pickle(tmp)
            tmp.replace(self.path)
        except OSError:
            log.warning("could not save %s's candles", BENCHMARK, exc_info=True)
