from __future__ import annotations

from tos_bot.core.enums import OrderType, Side
from tos_bot.core.models import OrderRequest


def test_market_fill_and_position(paper):
    r = paper.place_order(OrderRequest("AAA", Side.LONG, 10, OrderType.MARKET))
    assert r.status == "FILLED" and r.filled_qty == 10
    acc = paper.get_account()
    pos = acc.position("AAA")
    assert pos and pos.quantity == 10


def test_realized_pnl_and_day_trade_count(paper):
    paper.place_order(OrderRequest("BBB", Side.LONG, 5, OrderType.MARKET))
    entry = paper.get_account().position("BBB").avg_price
    paper.place_order(OrderRequest("BBB", Side.SHORT, 5, OrderType.MARKET, is_entry=False))
    acc = paper.get_account()
    assert acc.position("BBB") is None or abs(acc.position("BBB").quantity) < 1e-9
    assert acc.round_trips >= 1                 # opened + closed same session
    assert "realized_pl" in acc.raw


def test_bracket_children_are_oco(paper):
    r = paper.place_order(OrderRequest("CCC", Side.LONG, 4, OrderType.MARKET,
                                       take_profit=1e9, stop_loss=0.01))
    kids = [o for o in paper.list_orders() if (o.raw or {}).get("parent_id") == r.order_id]
    assert len(kids) == 2
    grp = {o.raw["oco_group"] for o in kids}
    assert len(grp) == 1


def test_short_then_cover(paper):
    paper.place_order(OrderRequest("DDD", Side.SHORT, 3, OrderType.MARKET))
    assert paper.get_account().position("DDD").quantity == -3
    paper.place_order(OrderRequest("DDD", Side.LONG, 3, OrderType.MARKET, is_entry=False))
    acc = paper.get_account()
    assert acc.position("DDD") is None or abs(acc.position("DDD").quantity) < 1e-9
