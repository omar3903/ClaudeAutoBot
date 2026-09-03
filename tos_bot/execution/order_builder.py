"""Turn a Play (or an open Trade) into a broker-neutral OrderRequest.

`plan_order()` decides the *concrete* order given the live market session, so
the dashboard can show exactly what will be sent - "LIMIT (regular hours)",
"pre-market limit", "after-hours limit", "MARKET", "STOP_LIMIT", etc. - and
so we never fire a market/stop order into a session that can't accept it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..core.enums import OrderType, Side, TimeInForce
from ..core.models import OrderRequest, Play
from ..util.clock import Session


def _tif(name: str) -> TimeInForce:
    return {"DAY": TimeInForce.DAY, "GTC": TimeInForce.GTC,
            "GOOD_TILL_CANCEL": TimeInForce.GTC, "IOC": TimeInForce.IOC}.get(
        (name or "DAY").upper(), TimeInForce.DAY)


def _marketable_limit(play: Play, offset_bps: float) -> float:
    off = float(offset_bps) / 1e4
    px = play.entry * (1 + off) if play.side is Side.LONG else play.entry * (1 - off)
    return round(px, 2)


def plan_order(play: Play, session: Session, cfg) -> Dict[str, Any]:
    """What order would we send right now? ``executable`` gates the UI."""
    offset = float(getattr(cfg, "limit_offset_bps", 5.0))
    want = str(getattr(cfg, "default_order_type", "LIMIT")).upper()
    bracket_cfg = bool(getattr(cfg, "bracket_orders", True))

    if session is Session.CLOSED:
        return {
            "executable": False,
            "reason": "The market is closed (weekend / holiday / overnight). "
                      "No session can accept this order.",
            "order_type": None, "session_label": "closed",
        }

    if session.is_extended and not play.extended_hours_ok:
        which = "pre-market" if session is Session.PRE else "after-hours"
        return {
            "executable": False,
            "reason": f"This is a regular-hours setup - it can't be priced in the "
                      f"{which} session. Wait for the 09:30 ET open.",
            "order_type": None, "session_label": which,
        }

    if session.is_extended:
        which = "pre-market" if session is Session.PRE else "after-hours"
        return {
            "executable": True,
            "order_type": "LIMIT",
            "order_session": "EXTENDED",
            "session_label": f"{which} limit",
            "limit_price": _marketable_limit(play, offset),
            "stop_price": None,
            "tif": "DAY",
            "bracket_mode": "managed",
            "note": (f"{which.capitalize()} orders are limit-only and cannot carry a "
                     f"native stop. The automatic exit manager holds the stop "
                     f"({play.stop:.2f}) and target ({(play.primary_target or 0):.2f}) instead."),
        }

    # regular session
    otype = want if want in ("LIMIT", "MARKET", "STOP_LIMIT") else "LIMIT"
    limit = None
    stop = None
    if otype == "LIMIT":
        limit = _marketable_limit(play, offset)
    elif otype == "STOP_LIMIT":                 # breakout-style: trigger at entry
        stop = round(play.entry, 2)
        limit = _marketable_limit(play, offset)
    label = {"LIMIT": "limit", "MARKET": "market", "STOP_LIMIT": "stop-limit"}[otype]
    return {
        "executable": True,
        "order_type": otype,
        "order_session": "REGULAR",
        "session_label": f"{label} (regular hours)",
        "limit_price": limit,
        "stop_price": stop,
        "tif": str(getattr(cfg, "time_in_force", "DAY")),
        "bracket_mode": "native" if bracket_cfg else "managed",
        "note": "",
    }


def build_entry_order(play: Play, qty: int, cfg, plan: Optional[Dict[str, Any]] = None) -> OrderRequest:
    if plan is None:
        from ..util import clock
        plan = plan_order(play, clock.current_session(), cfg)

    otype = {
        "LIMIT": OrderType.LIMIT, "MARKET": OrderType.MARKET,
        "STOP_LIMIT": OrderType.STOP_LIMIT,
    }.get(plan.get("order_type") or "LIMIT", OrderType.LIMIT)

    req = OrderRequest(
        symbol=play.symbol, side=play.side, quantity=qty, order_type=otype,
        limit_price=plan.get("limit_price"), stop_price=plan.get("stop_price"),
        tif=_tif(plan.get("tif", "DAY")), asset_class=play.asset_class,
        is_entry=True, session=plan.get("order_session", "REGULAR"),
        client_tag=play.id,
    )
    # native bracket only in the regular session; otherwise the ExitManager owns it
    if plan.get("bracket_mode") == "native":
        req.take_profit = play.primary_target
        req.stop_loss = play.stop
    return req


def build_exit_order(
    symbol: str, position_side: str, qty: float, limit_price: Optional[float] = None,
    cfg=None, tag: str = "", session: str = "REGULAR",
) -> OrderRequest:
    exit_side = Side.SHORT if position_side == "LONG" else Side.LONG
    otype = OrderType.LIMIT if limit_price else OrderType.MARKET
    return OrderRequest(
        symbol=symbol, side=exit_side, quantity=qty, order_type=otype,
        limit_price=round(limit_price, 2) if limit_price else None,
        tif=_tif(getattr(cfg, "time_in_force", "DAY")) if cfg else TimeInForce.DAY,
        is_entry=False, session=session, client_tag=tag or f"exit:{symbol}",
    )
