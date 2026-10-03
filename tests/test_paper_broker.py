from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from fakes import fixed_quote
from autotradebot.brokers.paper_adapter import PaperBroker
from autotradebot.core.enums import OrderType, Side, StrategyKind, Timeframe
from autotradebot.core.models import OrderRequest, Play
from autotradebot.data.market_data import quote_from_price
from autotradebot.execution.executor import Executor


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


# ---------------------------------------------------------------- its brackets, through the executor
EXECUTION = SimpleNamespace(limit_offset_bps=5.0, default_order_type="LIMIT", time_in_force="DAY", bracket_orders=True)
NATIVE = {"executable": True, "order_type": "LIMIT", "limit_price": 100.05, "order_session": "REGULAR", "tif": "DAY",
          "bracket_mode": "native"}


@pytest.fixture
def sim(repo, tmp_path):
    """The executor on the simulator, with a database of its own (these trades would count in other tests' P/L).
    ``price`` moves the stock; ``events`` is what the dashboard heard."""
    from autotradebot.persistence.db import DB

    DB.init(url=f"sqlite:///{(tmp_path / 'sim.sqlite').as_posix()}")
    DB.create_all()
    price, events = {"AAA": 100.0}, []
    broker = PaperBroker(quote=lambda s: quote_from_price(s, price[s]), starting_cash=100_000.0, slippage_bps=0.0)
    broker.connect()
    ex = Executor(broker, repo, EXECUTION, bus=SimpleNamespace(publish=lambda topic, **kw: events.append((topic, kw))),
                  venue="paper")
    yield SimpleNamespace(broker=broker, ex=ex, repo=repo, price=price, events=events)
    DB.engine.dispose()
    DB.init(url=os.environ["DATABASE_URL"])


def _enter(sim, stop=98.0, target=104.0):
    """Buy 10 AAA at 100 with the simulator's bracket attached: its stop at ``stop``, its target at ``target``."""
    play = Play(symbol="AAA", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.SWING, entry=100.0, stop=stop, targets=[target])
    play.suggested_qty = 10
    sim.repo.record_play(play)
    out = sim.ex.execute_play(play, sim.broker.get_account(), plan=NATIVE)
    assert out["status"] == "FILLED"
    return out["trade_id"]


def _held(sim):
    pos = sim.broker.get_account().position("AAA")
    return pos.quantity if pos is not None else 0.0


def test_a_bracket_stop_that_filled_closes_its_own_trade_once_and_never_the_next_one_of_the_stock(sim):
    first = _enter(sim)
    sim.price["AAA"] = 97.0
    sim.ex.sync_open_orders()                                  # the bracket's stop sells the 10 shares at 97
    t = sim.repo.get_trade(first)
    assert (t["status"], t["exit_reason"], t["exit_price"]) == ("CLOSED", "stop", 97.0)

    sim.price["AAA"] = 100.0
    second = _enter(sim)
    sim.ex.sync_open_orders()                                  # the first stop is still in the simulator's list
    sim.ex.sync_open_orders()
    assert sim.repo.get_trade(second)["status"] == "OPEN" and _held(sim) == 10
    assert [kw["trade"]["id"] for topic, kw in sim.events if topic == "trade.closed"] == [first]


def test_a_bracket_order_closes_the_trade_whose_entry_it_was_attached_to(sim):
    first, second = _enter(sim, stop=98.0), _enter(sim, stop=95.0)
    sim.price["AAA"] = 97.0
    sim.ex.sync_open_orders()                                  # the first entry's stop fills, the second's doesn't
    assert sim.repo.get_trade(first)["status"] == "CLOSED"
    assert sim.repo.get_trade(second)["status"] == "OPEN" and _held(sim) == 10


def test_the_apps_own_exit_cancels_the_simulators_bracket_so_it_cant_open_the_other_side(sim):
    tid = _enter(sim)
    assert sim.ex.close_trade(tid, "manual")["status"] == "FILLED"
    assert {o.status for o in sim.broker.list_orders() if (o.raw or {}).get("parent_id")} == {"CANCELED"}

    sim.price["AAA"] = 97.0                                    # under where the bracket's stop was
    sim.ex.sync_open_orders()
    assert _held(sim) == 0                                     # no short opened
    assert sim.repo.get_trade(tid)["exit_reason"] == "manual"


def test_a_record_closed_because_its_position_is_gone_has_its_bracket_cancelled(sim):
    tid = _enter(sim)
    sim.broker.place_order(OrderRequest("AAA", Side.SHORT, 10, OrderType.MARKET, is_entry=False))   # sold elsewhere
    sim.repo.close_trade(tid, 99.95, exit_reason="closed-outside")                  # as the position check books it
    sim.ex.forget_open("AAA", tid)
    assert {o.status for o in sim.broker.list_orders() if (o.raw or {}).get("parent_id")} == {"CANCELED"}

    sim.price["AAA"] = 97.0
    sim.ex.sync_open_orders()
    assert _held(sim) == 0
