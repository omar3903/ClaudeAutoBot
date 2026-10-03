"""The chart behind a play, and the one behind a trade record: recent candles, the levels, the
ways out of it - and for a trade, where the bot got in and out and how it stands.

The exit routes follow the exit manager (execution/exit_manager.py). The stop and
the target always apply. With automatic exits on, the stop moves to lock a small
gain once the trade is ``breakeven_at_r`` in profit, trails the price past
``trail_start_r``, and a day trade is closed before the bell (a swing trade after
``max_swing_hold_days`` trading days).

A trade's chart spans from the session before its entry to today, in 5-minute
candles while that fits IBKR's window (the scans' cached five sessions first,
the trade's own request up to ten), daily candles beyond - as far as the scans
have stored them, which can stop at the session before today's. The marks come
from its fills, the stop moves from the exit manager's notes, and the standing
from the price the blotter shows it at.
"""

from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ..core.enums import Side, Timeframe
from ..core.models import Play
from ..util import clock
from .support import duration

INTRADAY_BARS = 156               # two sessions of 5-minute candles
DAILY_BARS = 120
#: the scans' cached 5-minute candles ('5 D', MarketData.intraday) hold today and four sessions back; a
#: trade whose chart fits in them costs no request, a longer one gets a request of its own up to this many
#: sessions, and past that its chart is daily candles
CACHED_SESSIONS = 5
INTRADAY_MAX_SESSIONS = 10
#: how long a trade's own candle request is kept, so repeated clicks on its record don't reach IBKR
TRADE_CANDLES_TTL_S = 60.0
#: the exit manager's note for a stop it moved: "stop->101.20 @ 1.4R" (where the trade stood then)
_STOP_NOTE = re.compile(r"stop->(\d+(?:\.\d+)?) @ (-?\d+(?:\.\d+)?)R")


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
                       "how": f"Still open after {hold_days} trading days, the entry's counted: {close} at the market "
                              + (f"{flatten} minutes before the last one's close" if flatten > 0
                                 else "at the next day's open") + ", whatever the price."})
    return routes


def chart_payload(play: Play, frame: Optional[pd.DataFrame], intraday: bool, cfg: Any) -> Dict[str, Any]:
    return {"ok": True, "play_id": play.id, "symbol": play.symbol, "side": play.side.value,
            "strategy": play.strategy, "timeframe": play.timeframe.value,
            "bars": "5-minute candles, the last two sessions" if intraday else "daily candles, the last six months",
            "candles": candles(frame, INTRADAY_BARS if intraday else DAILY_BARS),
            "levels": {"entry": play.entry, "stop": play.stop, "targets": list(play.targets)},
            "routes": exit_routes(play, cfg)}


# ---- the chart behind a trade record ------------------------------------- #
def _when(value: Any) -> Optional[dt.datetime]:
    """A stored time (ISO, naive = UTC) as an aware datetime; None when there isn't one."""
    if not value:
        return None
    try:
        at = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return at.replace(tzinfo=dt.timezone.utc) if at.tzinfo is None else at


def _stamp(value: Any) -> Optional[str]:
    """A stored time as the candles carry theirs: New York, ISO."""
    at = _when(value)
    return at.astimezone(clock.NY).isoformat() if at else None


def trade_sessions(t: Dict[str, Any], today: dt.date) -> int:
    """How many sessions the trade's chart spans: the entry's through today's, the entry's counted. Today
    even for a closed trade - an IBKR request looks back from now, so a trade closed long ago fits no
    intraday window and gets daily candles."""
    entered = _when(t.get("entry_time"))
    if entered is None:
        return 1
    day, n = clock.session_date(entered), 1
    while day < today:
        day, n = clock.next_trading_day(day), n + 1
    return n


def candle_request(sessions: int) -> Optional[str]:
    """The IBKR duration a trade's own 5-minute candles take - a session before the entry, one to spare -
    or None when the scans' cached candles cover it (see CACHED_SESSIONS)."""
    if sessions + 1 <= CACHED_SESSIONS:
        return None
    return f"{min(sessions + 2, INTRADAY_MAX_SESSIONS)} D"


def since_session_before(frame: pd.DataFrame, t: Dict[str, Any]) -> pd.DataFrame:
    """The candles from the session before the entry's onward, so what led to the trade shows too."""
    entered = _when(t.get("entry_time"))
    if entered is None or not len(frame):
        return frame
    first = clock.prev_trading_day(clock.session_date(entered))
    index = (frame.index.tz_convert(clock.NY) if frame.index.tz is not None
             else frame.index.tz_localize("UTC").tz_convert(clock.NY))
    return frame[index.date >= first]


def last_session(frame: Optional[pd.DataFrame]) -> Optional[dt.date]:
    """The session of the newest candle (its New York date), or None without candles."""
    if frame is None or not len(frame):
        return None
    at = frame.index[-1]
    at = at.tz_convert(clock.NY) if at.tzinfo is not None else at.tz_localize("UTC").tz_convert(clock.NY)
    return at.date()


def bars_words(frame: Optional[pd.DataFrame], intraday: bool) -> str:
    """What the trade's candles are. The daily candles are the store the scans keep, which holds a session
    once a scan has seen it finished - so when they stop short of today's session the caption says which
    session they run to, since the trade's exit or best point today comes after its last candle (the chart
    puts such a mark on the last candle: web/js/chart.js, candlesSVG)."""
    if intraday:
        return "5-minute candles since the session before the entry"
    last = last_session(frame)
    if last is None or last >= clock.session_date():
        return "daily candles, the last six months"
    return f"daily candles, the last six months, to {last:%b} {last.day}"


def _risk_per_share(t: Dict[str, Any]) -> float:
    """The risk the trade was opened with: entry to the original stop, R's basis (as the exit manager measures it)."""
    stop = t.get("initial_stop_price") or t.get("stop_price")
    return abs(float(t["entry_price"]) - float(stop)) if stop and t.get("entry_price") else 0.0


def _in_r(t: Dict[str, Any], key: str) -> Optional[float]:
    risk = _risk_per_share(t)
    return round(float(t[key]) / risk, 2) if risk and t.get(key) is not None else None


def trade_marks(t: Dict[str, Any], fills: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Where the bot got in and out, from the fills: the entry, the parts taken off, the exit that closed
    the trade (with its R) - and the best point the trade reached, when the exit manager saw one and the trade
    didn't close there (the exit mark already stands on it)."""
    marks: List[Dict[str, Any]] = []
    exits = [f for f in fills if f.get("leg") == "EXIT"]
    last_exit = exits[-1] if exits and t.get("status") == "CLOSED" else None
    for f in fills:
        price, qty = float(f.get("price") or 0.0), float(f.get("quantity") or 0.0)
        if f.get("leg") == "ENTRY":
            marks.append({"t": _stamp(f.get("ts")), "price": round(price, 4), "kind": "entry",
                          "title": f"Entered {qty:g} @ {price:.2f}"})
        elif f is last_exit:
            r, reason = t.get("r_multiple"), t.get("exit_reason") or ""
            words = f"{r:+.2f}R" if r is not None else "closed"
            marks.append({"t": _stamp(f.get("ts")), "price": round(price, 4), "kind": "exit", "r": r,
                          "title": f"Exit @ {price:.2f}: {words}" + (f" ({reason})" if reason else "")})
        elif f.get("leg") == "EXIT":
            marks.append({"t": _stamp(f.get("ts")), "price": round(price, 4), "kind": "part",
                          "title": f"Took {qty:g} off @ {price:.2f}"})
    best, best_at = t.get("hwm_price"), _stamp(t.get("mfe_at"))
    # the close folds its own fill into the best point (repository.close_trade): a trade that closed at its best
    # would get a second mark on top of its exit
    at_exit = (last_exit is not None and best is not None and t.get("exit_price") is not None
               and abs(float(best) - float(t["exit_price"])) < 5e-5)
    if best is not None and best_at and not at_exit:
        mfe_r = _in_r(t, "mfe")
        marks.append({"t": best_at, "price": round(float(best), 4), "kind": "best",
                      "title": f"Best point {float(best):.2f}" + (f" ({mfe_r:+.1f}R)" if mfe_r is not None else "")})
    return marks


def stop_moves(t: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every time the stop moved, from the exit manager's notes: the new stop and where the trade stood then.
    None carries a time (``t``): the notes don't, and neither do the orders sent for the trade - a stop
    resting at the broker is moved by modifying it, which leaves no order row, and a stop order placed
    afresh is the same stop placed again (after a restart, a lost order, a refused pair), so its time is
    the placement's, not a move's."""
    return [{"t": None, "price": round(float(price), 4), "r": float(r)}
            for price, r in _STOP_NOTE.findall(t.get("notes") or "")]


def trade_routes(t: Dict[str, Any], cfg: Any) -> List[Dict[str, Any]]:
    """The ways the trade can end, as exit_routes lists them for a play - built from the trade's own
    levels, the stop where it stands now."""
    stop = t.get("stop_price") or t.get("initial_stop_price")
    if not t.get("entry_price") or not stop:
        return []                           # a pair leg: its exits are the pair's
    try:
        side, timeframe = Side(t.get("side")), Timeframe(t.get("timeframe"))
    except ValueError:
        return []
    targets = [float(x) for x in (t.get("target_price"), t.get("target2_price")) if x]
    play = SimpleNamespace(side=side, timeframe=timeframe, entry=float(t["entry_price"]), stop=float(stop),
                           targets=targets)
    return exit_routes(play, cfg)


def _held(t: Dict[str, Any], now: dt.datetime) -> str:
    """How long the trade has been held (or was), in words: the time within a session, else the sessions."""
    entered, until = _when(t.get("entry_time")), _when(t.get("exit_time")) or now
    if entered is None:
        return ""
    day, last, n = clock.session_date(entered), clock.session_date(until), 1
    while day < last:
        day, n = clock.next_trading_day(day), n + 1
    return f"{n} sessions" if n > 1 else duration((until - entered).total_seconds())


def trade_standing(t: Dict[str, Any], mark: Optional[Tuple[float, Optional[str]]],
                   now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    """How the trade stands. Open: at ``mark`` (the price and when it's from, as the blotter shows the
    position) - in R and in money, its best and worst so far, and what the stop and the target would
    make of it. Closed: its result."""
    now = now or dt.datetime.now(dt.timezone.utc)
    entry, qty = float(t.get("entry_price") or 0.0), abs(float(t.get("quantity") or 0.0))
    sign, risk = (1.0 if t.get("side") == "LONG" else -1.0), _risk_per_share(t)
    out: Dict[str, Any] = {"held": _held(t, now), "mfe_r": _in_r(t, "mfe"), "mae_r": _in_r(t, "mae")}
    if t.get("status") == "CLOSED":
        return {**out, "r_multiple": t.get("r_multiple"), "realized_pl": t.get("realized_pl"),
                "exit_reason": t.get("exit_reason") or ""}
    price, at = mark if mark else (None, None)
    stop, target = t.get("stop_price") or t.get("initial_stop_price"), t.get("target_price")

    def money(level: Any) -> Optional[float]:
        return round(sign * (float(level) - entry) * qty, 2) if level and entry else None

    return {**out, "price": round(float(price), 4) if price else None, "price_at": at,
            "open_r": round(sign * (float(price) - entry) / risk, 2) if price and risk else None,
            "unrealized_pl": money(price), "at_stop_pl": money(stop), "at_target_pl": money(target)}


def trade_chart_payload(rec: Dict[str, Any], frame: Optional[pd.DataFrame], intraday: bool, cfg: Any,
                        mark: Optional[Tuple[float, Optional[str]]] = None) -> Dict[str, Any]:
    """The chart behind a trade record (``rec``: Repository.trade_record's): its candles, levels, marks,
    stop moves, routes and standing. ``frame``: the candles already cut to the trade's window."""
    t = rec["trade"]
    return {"ok": True, "trade_id": t["id"], "symbol": t["symbol"], "side": t.get("side"),
            "timeframe": t.get("timeframe"), "strategy": t.get("strategy"), "status": t.get("status"),
            "quantity": t.get("quantity"),
            "bars": bars_words(frame, intraday),
            "candles": candles(frame, len(frame) if intraday and frame is not None else DAILY_BARS),
            "levels": {"entry": t.get("entry_price"), "initial_stop": t.get("initial_stop_price"),
                       "stop": t.get("stop_price") or t.get("initial_stop_price"),
                       "targets": [x for x in (t.get("target_price"), t.get("target2_price")) if x]},
            "marks": trade_marks(t, rec.get("fills") or []),
            "stop_moves": stop_moves(t),
            "routes": trade_routes(t, cfg),
            "standing": trade_standing(t, mark)}
