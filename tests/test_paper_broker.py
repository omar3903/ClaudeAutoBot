from __future__ import annotations

from tos_bot.core.enums import OrderType, Side
from tos_bot.core.models import OrderRequest
from tos_bot.brokers.paper_adapter import PaperBroker


def test_state_persists_across_restart(tmp_path, md):
    sp = tmp_path / "paper_state.json"
    b1 = PaperBroker(starting_cash=100000.0, data_service=md, persist=True, state_path=sp)
    b1.connect()
    b1.place_order(OrderRequest("PERS", Side.LONG, 7, OrderType.MARKET))
    cash1 = b1.get_account().cash
    assert sp.exists()

    b2 = PaperBroker(starting_cash=100000.0, data_service=md, persist=True, state_path=sp)
    b2.connect()
    acc = b2.get_account()
    assert acc.position("PERS") and acc.position("PERS").quantity == 7
    assert abs(acc.cash - cash1) < 1.0        # cash carried over

    b2.reset(50000.0)
    assert b2.get_account().equity == 50000.0
    b3 = PaperBroker(starting_cash=1.0, data_service=md, persist=True, state_path=sp)
    b3.connect()
    assert b3.get_account().cash == 50000.0   # reset persisted too


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
