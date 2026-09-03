"""Translate a Play (or an open Trade) into a broker-neutral OrderRequest.

Entries are marketable-limit by default: cross the spread by
``limit_offset_bps`` so they fill promptly but with a price cap.
"""

from __future__ import annotations

from typing import Optional

from ..core.enums import OrderType, Side, TimeInForce
from ..core.models import OrderRequest, Play


def _tif(name: str) -> TimeInForce:
    return {"DAY": TimeInForce.DAY, "GTC": TimeInForce.GTC,
            "GOOD_TILL_CANCEL": TimeInForce.GTC, "IOC": TimeInForce.IOC}.get(
        (name or "DAY").upper(), TimeInForce.DAY)


def build_entry_order(play: Play, qty: int, cfg) -> OrderRequest:
    offset = float(getattr(cfg, "limit_offset_bps", 5.0)) / 1e4
    otype = OrderType.LIMIT if str(getattr(cfg, "default_order_type", "LIMIT")).upper() == "LIMIT" \
        else OrderType.MARKET
    limit = None
    if otype is OrderType.LIMIT:
        limit = play.entry * (1 + offset) if play.side is Side.LONG else play.entry * (1 - offset)
        limit = round(limit, 2)

    req = OrderRequest(
        symbol=play.symbol, side=play.side, quantity=qty, order_type=otype,
        limit_price=limit, tif=_tif(getattr(cfg, "time_in_force", "DAY")),
        asset_class=play.asset_class, is_entry=True, client_tag=play.id,
    )
    if getattr(cfg, "bracket_orders", True):
        req.take_profit = play.primary_target
        req.stop_loss = play.stop
    return req


def build_exit_order(
    symbol: str, position_side: str, qty: float, limit_price: Optional[float] = None,
    cfg=None, tag: str = "",
) -> OrderRequest:
    exit_side = Side.SHORT if position_side == "LONG" else Side.LONG
    otype = OrderType.LIMIT if limit_price else OrderType.MARKET
    return OrderRequest(
        symbol=symbol, side=exit_side, quantity=qty, order_type=otype,
        limit_price=round(limit_price, 2) if limit_price else None,
        tif=_tif(getattr(cfg, "time_in_force", "DAY")) if cfg else TimeInForce.DAY,
        is_entry=False, client_tag=tag or f"exit:{symbol}",
    )
