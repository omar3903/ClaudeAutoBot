"""The chart behind a play: recent candles, the play's levels, and the ways out of it.

The exit routes follow the exit manager (execution/exit_manager.py). The stop and
the target always apply. With automatic exits on, the stop moves to lock a small
gain once the trade is ``breakeven_at_r`` in profit, trails the price past
``trail_start_r``, and a day trade is closed before the bell (a swing trade after
``max_swing_hold_days``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from ..core.enums import Side, Timeframe
from ..core.models import Play

INTRADAY_BARS = 156               # two sessions of 5-minute candles
DAILY_BARS = 120


def candles(frame: Optional[pd.DataFrame], bars: int) -> List[Dict[str, Any]]:
    """The latest ``bars`` candles as plain rows for the dashboard."""
    if frame is None or not len(frame):
        return []
    return [{"t": row.Index.isoformat(), "o": round(float(row.open), 4), "h": round(float(row.high), 4),
             "l": round(float(row.low), 4), "c": round(float(row.close), 4), "v": float(row.volume)}
            for row in frame.iloc[-bars:].itertuples()]


def exit_routes(play: Play, cfg: Any) -> List[Dict[str, Any]]:
    """Every way the trade can end, in R (multiples of the risk between entry and stop)."""
    entry, stop = float(play.entry), float(play.stop)
    target = float(play.targets[0]) if play.targets else None
    risk = abs(entry - stop)
    if entry <= 0 or risk <= 0:
        return []
    long = play.side is Side.LONG
    sign = 1.0 if long else -1.0
    toward, against, close = ("rises", "falls", "sold") if long else ("falls", "rises", "bought back")
    reward = abs(target - entry) / risk if target else None

    def at(r: float) -> float:
        return round(entry + sign * r * risk, 4)

    routes: List[Dict[str, Any]] = []
    if target:
        routes.append({"key": "target", "label": "Target", "price": round(target, 4), "r": round(reward, 2),
                       "how": f"The price {toward} to {target:.2f}: the position is {close} for about +{reward:.1f}R."})
    routes.append({"key": "stop", "label": "Stop loss", "price": round(stop, 4), "r": -1.0,
                   "how": f"The price {against} to {stop:.2f} first: the loss stops at about 1R."})
    auto = bool(getattr(cfg, "enabled", True))

    be_r = float(getattr(cfg, "breakeven_at_r", 0) or 0)
    if auto and be_r > 0 and (reward is None or be_r < reward):
        lock_r = float(getattr(cfg, "breakeven_lock_r", 0) or 0)
        buffer = entry * float(getattr(cfg, "breakeven_buffer_bps", 0) or 0) / 1e4
        locked = round(entry + sign * (lock_r * risk + buffer), 4)
        routes.append({"key": "breakeven", "label": "Locked gain", "trigger": at(be_r), "price": locked,
                       "r": round(lock_r + buffer / risk, 2),
                       "how": f"The price reaches {at(be_r):.2f} (+{be_r:g}R) and turns back: the stop has "
                              f"moved to {locked:.2f}, so about +{lock_r:g}R is kept."})

    trail_r = float(getattr(cfg, "trail_start_r", 0) or 0)
    share = float(getattr(cfg, "trail_lock_ratio", 0) or 0)
    if auto and trail_r > 0 and share > 0 and (reward is None or trail_r < reward):
        routes.append({"key": "trail", "label": "Trailing stop", "trigger": at(trail_r), "price": at(trail_r * share),
                       "r": round(trail_r * share, 2),
                       "how": f"The price passes {at(trail_r):.2f} (+{trail_r:g}R): the stop trails it and keeps "
                              f"{share:.0%} of the open gain - at least +{trail_r * share:.1f}R, more the further it runs."})

    flatten = int(getattr(cfg, "flatten_intraday_before_close_min", 0) or 0)
    hold_days = int(getattr(cfg, "max_swing_hold_days", 0) or 0)
    if auto and play.timeframe is Timeframe.INTRADAY and flatten > 0:
        routes.append({"key": "time", "label": "Before the close", "price": None, "r": None,
                       "how": f"Still open {flatten} minutes before the close: {close} at the market, whatever the price."})
    elif auto and play.timeframe is Timeframe.SWING and hold_days > 0:
        routes.append({"key": "time", "label": "Time stop", "price": None, "r": None,
                       "how": f"Still open after {hold_days} days: {close} at the market, whatever the price."})
    return routes


def chart_payload(play: Play, frame: Optional[pd.DataFrame], intraday: bool, cfg: Any) -> Dict[str, Any]:
    return {"ok": True, "play_id": play.id, "symbol": play.symbol, "side": play.side.value,
            "strategy": play.strategy, "timeframe": play.timeframe.value,
            "bars": "5-minute candles, the last two sessions" if intraday else "daily candles, the last six months",
            "candles": candles(frame, INTRADAY_BARS if intraday else DAILY_BARS),
            "levels": {"entry": play.entry, "stop": play.stop, "targets": list(play.targets)},
            "routes": exit_routes(play, cfg)}
