from __future__ import annotations

from fakes import fixed_quote
from autotradebot.brokers.paper_adapter import PaperBroker
from autotradebot.core.enums import OrderType, Side
from autotradebot.core.models import OrderRequest


def test_state_persists_across_restart(tmp_path):
    state = tmp_path / "paper_state.json"
    first = PaperBroker(quote=fixed_quote(), starting_cash=100000.0, state_path=state)
    first.connect()
    first.place_order(OrderRequest("PERS", Side.LONG, 7, OrderType.MARKET))
    cash = first.get_account().cash
    assert state.exists()

    second = PaperBroker(quote=fixed_quote(), starting_cash=100000.0, state_path=state)
    second.connect()
    acc = second.get_account()
    assert acc.position("PERS") and acc.position("PERS").quantity == 7
    assert abs(acc.cash - cash) < 1.0                     # cash carried over

    second.reset(50000.0)
    assert second.get_account().equity == 50000.0
    third = PaperBroker(quote=fixed_quote(), starting_cash=1.0, state_path=state)
    third.connect()
    assert third.get_account().cash == 50000.0            # the reset persisted too


def test_nothing_is_saved_without_a_state_file(tmp_path, paper):
    paper.place_order(OrderRequest("TMP", Side.LONG, 1, OrderType.MARKET))
    assert not list(tmp_path.iterdir())


def test_market_fill_and_position(paper):
    r = paper.place_order(OrderRequest("AAA", Side.LONG, 10, OrderType.MARKET))
    assert r.status == "FILLED" and r.filled_qty == 10
    pos = paper.get_account().position("AAA")
    assert pos and pos.quantity == 10
    [fill] = paper.get_fills("AAA")
    assert (fill.symbol, fill.side, fill.quantity) == ("AAA", Side.LONG, 10) and fill.price > 0
    assert paper.get_fills("ZZZ") == [] and len(paper.get_fills()) >= 1


def test_realized_pnl_and_day_trade_count(paper):
    paper.place_order(OrderRequest("BBB", Side.LONG, 5, OrderType.MARKET))
    paper.place_order(OrderRequest("BBB", Side.SHORT, 5, OrderType.MARKET, is_entry=False))
    acc = paper.get_account()
    assert acc.position("BBB") is None or abs(acc.position("BBB").quantity) < 1e-9
    assert acc.round_trips >= 1                            # opened + closed the same session
    assert "realized_pl" in acc.raw


def test_bracket_children_are_oco(paper):
    r = paper.place_order(OrderRequest("CCC", Side.LONG, 4, OrderType.MARKET, take_profit=1e9, stop_loss=0.01))
    kids = [o for o in paper.list_orders() if (o.raw or {}).get("parent_id") == r.order_id]
    assert len(kids) == 2 and len({o.raw["oco_group"] for o in kids}) == 1


def test_short_then_cover(paper):
    paper.place_order(OrderRequest("DDD", Side.SHORT, 3, OrderType.MARKET))
    assert paper.get_account().position("DDD").quantity == -3
    paper.place_order(OrderRequest("DDD", Side.LONG, 3, OrderType.MARKET, is_entry=False))
    acc = paper.get_account()
    assert acc.position("DDD") is None or abs(acc.position("DDD").quantity) < 1e-9


def test_a_working_order_reports_its_type_price_and_time_in_force(paper):
    paper.place_order(OrderRequest("LMT", Side.LONG, 5, OrderType.LIMIT, limit_price=1.0))
    [order] = paper.list_orders("WORKING")
    assert (order.order_type, order.limit_price, order.stop_price, order.tif) == ("LIMIT", 1.0, None, "DAY")
