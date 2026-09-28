"""How much of a stock's move the market explains: the market model.

Tsay, *Analysis of Financial Time Series* (ch. 9, eq. 9.5), writes a stock's return as the market's
times its beta plus the stock's own part:

    r_i,t = alpha_i + beta_i * r_m,t + e_i,t

Fitted by least squares on the sessions before today, the residual e is the move the market doesn't
explain, and its standard deviation says how big such moves usually are. Today's

    z = (r_i - alpha - beta * r_m) / sigma_e

is the stock's own move in those units. A big z is Aziz's "stock in play" - moving on its own, not
with the market (*How to Day Trade for a Living*, rule 4). Whether news came with it decides what
kind of move it is: Chan finds moves on news keep going and moves without news tend to be taken back
(*Quantitative Trading*, on stop losses; *Algorithmic Trading* ch. 4, on buying gaps).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

SESSIONS = 60
MIN_SESSIONS = 30


def _by_day(closes: pd.Series) -> pd.Series:
    s = closes.dropna()
    s.index = pd.Index([ts.date() for ts in pd.DatetimeIndex(s.index)])
    return s[~s.index.duplicated(keep="last")]


def abnormal_move(stock_closes: pd.Series, market_closes: pd.Series, sessions: int = SESSIONS) -> Optional[Dict[str, Any]]:
    """The last session's move (today's so far, when the last close is today's latest price) against
    the market model fitted on the ``sessions`` before it. None when the market's close for that
    session is missing or there aren't enough sessions to fit on."""
    stock, market = _by_day(stock_closes), _by_day(market_closes)
    if not len(stock) or stock.index[-1] not in market.index:
        return None
    joined = pd.concat({"s": stock, "m": market}, axis=1, join="inner").sort_index()
    returns = joined.pct_change().dropna()
    if len(returns) < MIN_SESSIONS + 1 or returns.index[-1] != stock.index[-1]:
        return None
    history, today = returns.iloc[-(sessions + 1):-1], returns.iloc[-1]
    s, m = history["s"].to_numpy(dtype=float), history["m"].to_numpy(dtype=float)
    var = float(m.var())
    if var <= 0:
        return None
    beta = float(((m - m.mean()) * (s - s.mean())).mean() / var)
    alpha = float(s.mean() - beta * m.mean())
    sigma = float(np.std(s - alpha - beta * m, ddof=2))
    if sigma <= 0:
        return None
    expected = alpha + beta * float(today["m"])
    return {"z": round((float(today["s"]) - expected) / sigma, 2), "beta": round(beta, 2),
            "move_pct": round(100 * float(today["s"]), 2), "market_pct": round(100 * float(today["m"]), 2),
            "expected_pct": round(100 * expected, 2), "usual_pct": round(100 * sigma, 2),
            "sessions": len(history), "day": returns.index[-1].isoformat()}
