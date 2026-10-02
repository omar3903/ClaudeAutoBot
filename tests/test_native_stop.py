"""The stop order the executor keeps at the broker for every open position (execution/protective_stops.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from test_order_follow_up import VENUE, _Broker, _executor, _Repo, _trade
from autotradebot.brokers.base import BrokerError
from autotradebot.core.enums import OrderType, Side, TimeInForce
from autotradebot.core.models import Fill, OrderResult


class _StopBroker(_Broker):
    """An account that can rest a stop order: it stays working until a test fills or loses it."""

    supports_native_stop = True

    def __init__(self, positions=None, working=None, can_modify=True):
        super().__init__(positions, working)
        self.live, self.modified, self.can_modify = {}, [], can_modify
        self.slow_cancel = False                 # the broker hasn't confirmed the cancel yet
        self.unconfirmed = False                 # the stop reads cancelled, but the broker never said it cancelled it

    def place_order(self, req):
        res = super().place_order(req)
        res.side, res.tag, res.order_type, res.stop_price = req.side, req.client_tag, req.order_type.value, req.stop_price
        if req.order_type is OrderType.STOP:
            self.live[res.order_id] = res
        return res

    def get_order(self, order_id):
        return self.reports.get(order_id) or self.live.get(order_id) or super().get_order(order_id)

    def cancel_order(self, order_id):
        super().cancel_order(order_id)
        if order_id in self.live and not self.slow_cancel:
            self.live[order_id].status = "CANCELED"
            self.live[order_id].raw = {"cancel_confirmed": not self.unconfirmed}

    def modify_stop(self, order_id, stop_price=None, quantity=None):
        if not self.can_modify:
            raise NotImplementedError("no modify here")
        o = self.live[order_id]
        o.stop_price = stop_price
        if quantity is not None:                 # None leaves the size as the broker holds it
            o.submitted_qty = quantity
        self.modified.append((order_id, stop_price, quantity))
        return o

    def list_orders(self, status=None):
        resting = [o for o in self.live.values() if o.status not in ("CANCELED", "FILLED", "REJECTED")]
        return super().list_orders(status) + (resting if status == "WORKING" else [])

    def stops(self):
        return [r for r in self.orders if r.order_type is OrderType.STOP]

    def exits(self):
        return [r for r in self.orders if r.order_type is not OrderType.STOP]


def _setup(trade=None, positions=None, **kw):
    broker = _StopBroker(positions if positions is not None else {"AAA": 10}, **kw)
    repo = _Repo([trade or _trade()])
    heard = []
    ex = _executor(broker, repo, bus=SimpleNamespace(publish=lambda topic, **p: heard.append((topic, p))))
    ex.STAND_DOWN_S = ex.STAND_DOWN_POLL_S = 0.0
    return broker, repo, ex, heard


def test_every_open_position_gets_a_stop_at_the_broker_for_its_shares():
    broker, repo, ex, heard = _setup()
    ex.sync_open_orders()
    (stop,) = broker.stops()
    assert (stop.symbol, stop.side, stop.quantity, stop.stop_price) == ("AAA", Side.SHORT, 10, 98.0)
    assert stop.tif is TimeInForce.GTC and stop.client_tag == "stop:t1" and not stop.is_entry
    assert ex.protective_stops() == [{"trade_id": "t1", "symbol": "AAA", "order_id": "1", "qty": 10.0, "stop_price": 98.0}]
    assert [t for t, _ in heard] == ["stop.placed"]
    ex.sync_open_orders()
    assert len(broker.stops()) == 1                                            # once is enough
    assert ex.pending_exit_trade_ids() == set()                                # it is no exit: the exit manager keeps managing


def test_a_short_is_protected_by_a_buy_stop_above_it():
    broker, _, ex, _ = _setup(_trade(side="SHORT", stop_price=103.0, initial_stop_price=103.0), positions={"AAA": -10})
    ex.sync_open_orders()
    (stop,) = broker.stops()
    assert stop.side is Side.LONG and stop.stop_price == 103.0


def test_no_stop_is_rested_that_the_account_could_not_cover():
    broker, _, ex, heard = _setup(positions={})                                 # closed outside the app
    ex.sync_open_orders()
    assert broker.stops() == [] and heard[-1][0] == "stop.failed" and "no stop placed" in heard[-1][1]["reason"]
    broker.positions["AAA"] = -10                                              # or the account is the other way round
    ex._stop_retry.clear()
    ex.sync_open_orders()
    assert broker.stops() == []


def test_a_fill_still_arriving_in_pieces_is_looked_at_again_within_seconds():
    broker, _, ex, _ = _setup(positions={"AAA": 6})                           # six of the ten shares have landed
    ex.SHARES_RETRY_S, ex.STOP_RETRY_S = 0.0, 3600.0
    ex.sync_open_orders()
    assert broker.stops() == []
    broker.positions["AAA"] = 10
    ex.sync_open_orders()                                                      # no half-minute wait for the rest
    assert len(broker.stops()) == 1 and broker.stops()[0].quantity == 10


def test_a_venue_that_cannot_hold_a_stop_is_left_alone():
    broker, repo = _Broker({"AAA": 10}), _Repo([_trade()])
    ex = _executor(broker, repo)
    ex.sync_open_orders()
    assert broker.orders == [] and not ex.native_stops_on()


def test_the_stop_follows_the_record_as_the_exit_manager_ratchets_it_but_not_every_tick():
    broker, repo, ex, heard = _setup()
    ex.sync_open_orders()
    repo.update_trade_risk("t1", stop_price=100.35)                           # break-even
    ex.sync_open_orders()
    assert broker.modified == []                                               # moved a moment ago: wait
    ex.STOP_MOVE_S = 0.0
    ex.sync_open_orders()
    assert broker.modified == [("1", 100.35, None)] and ex.protective_stops()[0]["stop_price"] == 100.35   # the price alone
    assert broker.live["1"].submitted_qty == 10 and ex.protective_stops()[0]["qty"] == 10.0
    assert heard[-1][0] == "stop.moved" and len(broker.stops()) == 1


def test_a_venue_that_cannot_modify_has_the_stop_replaced():
    broker, repo, ex, _ = _setup(can_modify=False)
    ex.STOP_MOVE_S = 0.0
    ex.sync_open_orders()
    repo.update_trade_risk("t1", stop_price=100.35)
    ex.sync_open_orders()
    assert broker.cancelled == ["1"] and ex.protective_stops() == []
    ex._stop_retry.clear()
    ex.sync_open_orders()
    assert [s.stop_price for s in broker.stops()] == [98.0, 100.35]


def test_the_stop_stands_down_before_the_apps_own_exit_goes_out():
    broker, _, ex, _ = _setup()
    ex.sync_open_orders()
    out = ex.close_trade("t1", reason="target")
    assert out["ok"] and broker.cancelled == ["1"] and ex.protective_stops() == []
    (exit_order,) = broker.exits()
    assert exit_order.quantity == 10 and exit_order.client_tag == "exit:t1"
    ex.sync_open_orders()
    assert len(broker.stops()) == 1                                            # no new stop while the exit is working


def test_no_exit_goes_out_until_the_broker_confirms_the_stop_is_cancelled():
    broker, _, ex, _ = _setup()
    ex.sync_open_orders()
    broker.slow_cancel = True
    out = ex.close_trade("t1", reason="target")
    assert not out["ok"] and "confirm" in out["reason"] and broker.exits() == []
    broker.slow_cancel = False
    broker.live["1"].status = "CANCELED"
    assert ex.close_trade("t1", reason="target")["ok"] and len(broker.exits()) == 1


def test_a_cancel_the_broker_never_confirmed_holds_the_exit_back_and_the_stop_that_then_fills_is_booked():
    broker, repo, ex, _ = _setup()
    ex.sync_open_orders()
    broker.unconfirmed = True                                                  # it reads cancelled: an error, no 202
    out = ex.close_trade("t1", reason="stop")
    assert not out["ok"] and "confirm" in out["reason"] and broker.exits() == []
    assert ex.protective_stops()[0]["order_id"] == "1"                         # still followed
    broker.live["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                   avg_fill_price=97.9)
    out = ex.close_trade("t1", reason="stop")
    assert out["ok"] and out["by"] == "broker-stop" and broker.exits() == []
    assert repo.get_trade("t1")["status"] == "CLOSED"


def test_a_stop_found_cancelled_is_read_once_more_and_a_fill_landing_behind_the_cancel_is_booked():
    broker, repo, ex, _ = _setup()
    ex.sync_open_orders()
    looks, read = [], broker.get_order

    def get_order(order_id):
        res = read(order_id)
        looks.append(res.status)
        if looks.count("CANCELED") > 1:                                        # the last look: the fill has landed
            return OrderResult(order_id=order_id, status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                               avg_fill_price=97.9)
        return res

    broker.get_order = get_order
    out = ex.close_trade("t1", reason="stop")
    assert out["ok"] and out["by"] == "broker-stop" and broker.exits() == []
    closed = repo.get_trade("t1")
    assert (closed["status"], closed["exit_price"], closed["exit_reason"]) == ("CLOSED", 97.9, "stop")


def _ibkr_broker(monkeypatch):
    """The real IBKR adapter around the adapter tests' fake ib_async, its orders kept as ib_async keeps them,
    and past the re-sync after connecting."""
    from test_ibkr_adapter import FakeSession, IbOrders
    from autotradebot.brokers import ibkr_adapter

    async def _fast(*_a):
        return None

    monkeypatch.setattr(ibkr_adapter, "port_is_open", lambda *a, **k: True)
    monkeypatch.setattr(ibkr_adapter, "_sleep", _fast)
    broker = ibkr_adapter.IbkrBroker(port=4002, mode="paper", session_factory=FakeSession)
    broker.connect()
    broker.connected_since -= 3600.0
    held = SimpleNamespace(contract=SimpleNamespace(symbol="AAA"), position=10.0, averageCost=100.0, marketPrice=100.0)
    broker._session.ib.portfolio = lambda acct="": [held]
    return broker, IbOrders(broker._session.ib)


@pytest.mark.parametrize("state", ["PendingCancel", "Filled"])
def test_a_stop_whose_cancel_ibkr_refuses_is_never_taken_for_cancelled_and_no_exit_goes_out(monkeypatch, state):
    broker, orders = _ibkr_broker(monkeypatch)                                 # the account holds the 10 shares
    repo = _Repo([_trade()])
    ex = _executor(broker, repo)
    ex.STAND_DOWN_S = ex.STAND_DOWN_POLL_S = 0.0
    ex.sync_open_orders()
    assert ex.protective_stops()[0]["order_id"] == "1"
    # the stop is triggering as the app's own exit comes: IBKR refuses the cancel, and ib_async calls it cancelled
    orders.answer_cancel = (10148, f"OrderId 1 that needs to be cancelled cannot be cancelled, state: {state}.")
    out = ex.close_trade("t1", reason="stop")
    assert not out["ok"] and "confirm" in out["reason"]
    assert orders.book[1].orderStatus.status == "Cancelled"
    orders.fill(1, 10.0, 97.9)
    orders.status(1, "Filled", filled=10.0, avg=97.9)
    out = ex.close_trade("t1", reason="stop")
    assert out["ok"] and out["by"] == "broker-stop"
    closed = repo.get_trade("t1")
    assert (closed["status"], closed["exit_price"], closed["exit_reason"]) == ("CLOSED", 97.9, "stop")
    assert [t.order.orderType for t in orders.book.values()] == ["STP"]       # no market exit ever went out


def test_a_stop_that_filled_first_is_booked_and_no_second_exit_is_sent():
    broker, repo, ex, _ = _setup()
    ex.sync_open_orders()
    broker.live["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                   avg_fill_price=97.9)
    out = ex.close_trade("t1", reason="stop")                                  # the exit manager saw the same price
    assert out["ok"] and out["by"] == "broker-stop" and broker.exits() == []
    closed = repo.get_trade("t1")
    assert (closed["status"], closed["exit_price"], closed["exit_reason"]) == ("CLOSED", 97.9, "stop")
    assert closed["exit_decision_price"] == 98.0                               # the slippage is measured from the stop


def test_a_stop_the_broker_filled_is_booked_on_the_next_pass_with_its_reason():
    broker, repo, ex, heard = _setup()
    ex.STOP_MOVE_S = 0.0
    ex.sync_open_orders()
    repo.update_trade_risk("t1", stop_price=101.0)                            # trailed above the first stop
    ex.sync_open_orders()
    broker.live["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                   avg_fill_price=100.9)
    ex.sync_open_orders()
    closed = repo.get_trade("t1")
    assert closed["status"] == "CLOSED" and closed["exit_reason"] == "trailing-stop" and ex.protective_stops() == []
    assert "trade.closed" in [t for t, _ in heard] and len(broker.orders) == 1


def test_a_stop_that_never_moved_is_booked_as_a_stop_though_it_rests_at_the_rounded_price():
    broker, repo, ex, _ = _setup(_trade(stop_price=97.9968, initial_stop_price=97.9968))
    ex.sync_open_orders()
    assert broker.stops()[0].stop_price == 98.0                                # rounded to the cent to be placed
    broker.live["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                   avg_fill_price=97.95)
    ex.sync_open_orders()
    closed = repo.get_trade("t1")
    assert (closed["status"], closed["exit_reason"]) == ("CLOSED", "stop")    # the rounding is no move


def test_a_stop_an_earlier_run_left_at_the_rounded_first_stop_is_booked_as_a_stop():
    left = [OrderResult(order_id="77", status="SUBMITTED", symbol="AAA", submitted_qty=10, side=Side.SHORT,
                        tag="stop:t1", order_type="STOP", stop_price=98.0)]
    broker, repo, ex, _ = _setup(_trade(stop_price=97.9968, initial_stop_price=97.9968), working=left)
    ex.sync_open_orders()
    assert ex.protective_stops()[0]["order_id"] == "77" and broker.modified == []   # followed, not moved
    broker.reports["77"] = OrderResult(order_id="77", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                       avg_fill_price=97.95)
    ex.sync_open_orders()
    assert repo.get_trade("t1")["exit_reason"] == "stop"


def test_a_stop_counts_as_moved_only_by_half_a_tick_or_more():
    from autotradebot.execution.protective_stops import stop_exit_reason

    assert stop_exit_reason(97.9968, 98.0) == "stop"                           # cents from a dollar up
    assert stop_exit_reason(97.9968, 98.01) == "trailing-stop"
    assert stop_exit_reason(0.51234, 0.5123) == "stop"                         # hundredths of a cent below
    assert stop_exit_reason(0.51234, 0.5124) == "trailing-stop"
    assert stop_exit_reason(None, 98.0) == "stop"                              # no first stop on the record


def test_the_part_coming_off_shrinks_the_stop_first():
    broker, repo, ex, _ = _setup()
    ex.sync_open_orders()
    out = ex.close_trade("t1", reason="target-1", qty=5, after_fill={"stop_price": 100.05, "target_price": 106.0})
    assert out["ok"] and broker.modified == [("1", 98.0, 5.0)]                # five shares protected, five being sold
    assert broker.exits()[0].quantity == 5 and broker.cancelled == []
    broker.reports["2"] = OrderResult(order_id="2", status="FILLED", symbol="AAA", submitted_qty=5, filled_qty=5,
                                      avg_fill_price=104.0)
    ex.STOP_MOVE_S = 0.0
    ex.sync_open_orders()
    assert repo.get_trade("t1")["quantity"] == 5 and broker.modified[-1] == ("1", 100.05, None)  # ...then to break-even
    assert broker.live["1"].submitted_qty == 5 and len(broker.modified) == 2


def test_a_stop_cut_for_a_part_exit_that_never_went_grows_back_only_for_shares_the_account_shows():
    broker, _, ex, _ = _setup()
    ex.sync_open_orders()
    ex.close_trade("t1", reason="target-1", qty=5, after_fill={"stop_price": 100.05, "target_price": 106.0})
    assert broker.modified == [("1", 98.0, 5.0)]
    broker.reports["2"] = OrderResult(order_id="2", status="REJECTED", symbol="AAA", submitted_qty=5)   # it never went
    broker.positions["AAA"] = 5                                                # and the account shows five
    ex.sync_open_orders()
    assert len(broker.modified) == 1 and broker.live["1"].submitted_qty == 5  # never an order it can't cover
    broker.positions["AAA"] = 10
    ex.sync_open_orders()
    assert broker.modified[-1] == ("1", 98.0, 10.0) and ex.protective_stops()[0]["qty"] == 10.0


def test_a_stop_an_earlier_run_left_is_followed_and_strays_are_cancelled():
    left = [OrderResult(order_id="77", status="SUBMITTED", symbol="AAA", submitted_qty=10, side=Side.SHORT,
                        tag="stop:t1", order_type="STOP", stop_price=98.0),
            OrderResult(order_id="78", status="SUBMITTED", symbol="AAA", submitted_qty=10, side=Side.SHORT,
                        tag="stop:t1", order_type="STOP", stop_price=98.0),
            OrderResult(order_id="90", status="SUBMITTED", symbol="ZZZ", submitted_qty=4, side=Side.SHORT,
                        tag="stop:gone", order_type="STOP", stop_price=9.0)]
    broker, _, ex, _ = _setup(working=left)
    ex.sync_open_orders()
    assert broker.stops() == [] and ex.protective_stops()[0]["order_id"] == "77"   # followed, never doubled
    assert sorted(broker.cancelled) == ["78", "90"]                            # the double, and the one without a trade
    described = {o["order_id"]: (o["purpose"], o["trade_id"]) for o in ex.active_orders()}
    assert described["77"] == ("stop", "t1")


def _left_stop(qty=100, filled=0.0, avg=0.0):
    """The stop an earlier run left working at the broker, for a 100-share record."""
    return OrderResult(order_id="77", status="SUBMITTED", symbol="AAA", submitted_qty=qty, filled_qty=filled,
                       avg_fill_price=avg, side=Side.SHORT, tag="stop:t1", order_type="STOP", stop_price=98.0)


def test_a_stop_that_filled_in_part_while_the_app_was_off_is_booked_and_the_rest_gets_a_stop_from_the_record():
    broker, repo, ex, heard = _setup(_trade(quantity=100), positions={"AAA": 40}, working=[_left_stop()])
    # the order's own count says nothing filled; IBKR's executions of it say 60 shares did
    broker.get_fills = lambda symbol=None: [
        Fill(order_id="77", symbol="AAA", side=Side.SHORT, quantity=60, price=97.9, tag="stop:t1"),
        Fill(order_id="12", symbol="AAA", side=Side.SHORT, quantity=5, price=99.0, tag="exit:t9")]   # another order's
    ex.sync_open_orders()
    t = repo.get_trade("t1")
    assert (t["status"], t["quantity"]) == ("OPEN", 40)
    [reduced] = [p for topic, p in heard if topic == "trade.reduced"]
    assert (reduced["qty"], reduced["price"], reduced["reason"]) == (60, 97.9, "stop")
    assert broker.cancelled == ["77"] and ex.protective_stops() == []          # not followed: it would count them again
    ex._stop_retry.clear()
    ex.sync_open_orders()
    [stop] = broker.stops()
    assert (stop.quantity, stop.stop_price, stop.client_tag) == (40, 98.0, "stop:t1")   # sized from the record
    ex.sync_open_orders()
    restarted = _executor(broker, repo, bus=SimpleNamespace(publish=lambda topic, **p: heard.append((topic, p))))
    restarted.sync_open_orders()                                               # the same executions, another run
    assert restarted.protective_stops()[0]["order_id"] == "1" and len(broker.stops()) == 1   # the fresh stop, followed
    assert repo.get_trade("t1")["quantity"] == 40 and [t for t, _ in heard].count("trade.reduced") == 1


def test_a_stop_found_filled_for_the_whole_record_closes_it_once():
    broker, repo, ex, heard = _setup(_trade(quantity=100), positions={}, working=[_left_stop(filled=100, avg=97.8)])
    ex.sync_open_orders()                                                      # no executions here: the order's own count
    closed = repo.get_trade("t1")
    assert (closed["status"], closed["exit_reason"], closed["exit_price"]) == ("CLOSED", "stop", 97.8)
    ex._stop_retry.clear()
    ex.sync_open_orders()
    assert [t for t, _ in heard].count("trade.closed") == 1 and broker.stops() == [] and broker.cancelled == ["77"]


def test_executions_that_cannot_be_read_change_nothing():
    broker, repo, ex, heard = _setup(_trade(quantity=100), positions={"AAA": 40}, working=[_left_stop()])

    def unreadable(symbol=None):
        raise BrokerError("the executions didn't arrive")

    broker.get_fills = unreadable
    ex.sync_open_orders()
    assert repo.get_trade("t1")["quantity"] == 100 and broker.cancelled == []  # left to the share-count warning
    assert ex.protective_stops()[0]["order_id"] == "77" and "trade.reduced" not in [t for t, _ in heard]


def test_a_record_closed_some_other_way_takes_its_stop_with_it():
    broker, repo, ex, _ = _setup()
    ex.sync_open_orders()
    repo.close_trade("t1", 99.0, "closed in TWS")
    ex.sync_open_orders()
    assert broker.cancelled == ["1"] and ex.protective_stops() == []


def test_a_stop_the_broker_drops_is_placed_again():
    broker, _, ex, heard = _setup()
    ex.STOP_RETRY_S = 0.0
    ex.sync_open_orders()
    broker.live["1"].status, broker.live["1"].message = "CANCELED", "cancelled by the exchange"
    ex.sync_open_orders()
    assert [t for t, _ in heard if t.startswith("stop.")] == ["stop.placed", "stop.lost", "stop.placed"]
    assert len(broker.stops()) == 2 and ex.protective_stops()[0]["order_id"] == "2"


def test_pair_legs_and_other_venues_get_no_stop():
    broker = _StopBroker({"AAA": 10, "BBB": 10})
    repo = _Repo([_trade(id="p1", pair_id="pair_1"), _trade(id="o1", symbol="BBB", broker="paper")])
    ex = _executor(broker, repo)
    ex.sync_open_orders()
    assert broker.stops() == [] and VENUE == "ibkr-paper"


def test_switching_venue_forgets_the_stops_without_touching_them():
    broker, _, ex, _ = _setup()
    ex.sync_open_orders()
    ex.rebind(_Broker(), venue="paper")
    assert ex.protective_stops() == [] and broker.cancelled == []


def test_an_exit_the_exchange_cannot_fill_never_touches_the_stop(monkeypatch):
    from autotradebot.execution.executor import Executor
    from autotradebot.util import clock

    broker, _, ex, _ = _setup()
    ex.sync_open_orders()
    monkeypatch.setattr(Executor, "_session_now", staticmethod(lambda: clock.Session.POST))     # after the close
    out = ex.close_trade("t1", reason="quit")
    assert not out["ok"] and out["market_closed"] and "market is closed" in out["reason"]
    assert broker.cancelled == [] and broker.exits() == [] and len(ex.protective_stops()) == 1   # nothing was touched
    monkeypatch.setattr(Executor, "_session_now", staticmethod(lambda: clock.Session.REGULAR))
    assert ex.close_trade("t1", reason="quit")["ok"] and broker.cancelled == ["1"]


def test_cancelling_the_working_orders_keeps_the_stops_that_protect_positions():
    from autotradebot.core.enums import StrategyKind, Timeframe
    from autotradebot.core.models import Account, Play
    from test_order_follow_up import PLAN

    stray = OrderResult(order_id="90", status="SUBMITTED", symbol="ZZZ", submitted_qty=4, side=Side.LONG, tag="")
    broker, _, ex, _ = _setup(working=[stray])
    ex.sync_open_orders()                                                       # the stop for t1: order 1
    entry = Play(symbol="BBB", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                 timeframe=Timeframe.SWING, entry=50.0, stop=48.0, targets=[56.0])
    entry.suggested_qty = 10
    ex.execute_play(entry, Account(account_id="DU"), plan=PLAN)                # a working entry: order 2
    counts = ex.cancel_working_orders()
    assert counts == {"entries": 1, "exits": 0, "others": 1, "stops_kept": 1}
    assert sorted(broker.cancelled) == ["2", "90"] and len(ex.protective_stops()) == 1


def test_stopping_a_quit_calls_off_the_exits_it_sent_and_the_stops_go_back():
    broker, _, ex, _ = _setup()
    ex.sync_open_orders()                                                       # stop: order 1
    assert ex.close_trade("t1", reason="quit")["ok"]                           # the quit's exit: order 2, left working
    assert ex.cancel_exits() == 1 and broker.cancelled == ["1", "2"]
    broker.reports["2"] = OrderResult(order_id="2", status="CANCELED", symbol="AAA", submitted_qty=10)
    ex.sync_open_orders()                                                       # the exit is gone...
    assert ex.pending_exit_trade_ids() == set() and len(ex.protective_stops()) == 1   # ...and a stop rests again
    assert [s.quantity for s in broker.stops()] == [10, 10]


def test_a_position_without_a_stop_at_the_broker_is_reported_and_reported_again():
    broker, _, ex, heard = _setup(positions={})                                 # the broker shows no shares
    ex.STOP_RETRY_S = 0.0
    ex.sync_open_orders()
    assert broker.stops() == [] and ex.unprotected() == ["t1"]
    assert "stop.missing" not in [t for t, _ in heard]                         # not straight away
    ex._bare_since["t1"] -= ex.UNPROTECTED_WARN_S
    ex.sync_open_orders()
    missing = [p for t, p in heard if t == "stop.missing"]
    assert len(missing) == 1 and missing[0]["symbol"] == "AAA" and "no stop placed" in missing[0]["reason"]
    ex.sync_open_orders()
    assert len([t for t, _ in heard if t == "stop.missing"]) == 1                # not on every pass
    ex._bare_warned["t1"] -= ex.UNPROTECTED_REPEAT_S
    ex.sync_open_orders()
    assert len([t for t, _ in heard if t == "stop.missing"]) == 2                # but again a few minutes on
    assert broker.orders == []                                                 # and it never places anything itself

    broker.positions["AAA"] = 10                                               # the shares show up
    ex._stop_retry.clear()
    ex.sync_open_orders()
    assert len(broker.stops()) == 1 and ex.unprotected() == []


def test_the_shares_of_a_part_filled_entry_get_their_stop_once_the_stalled_rest_is_cancelled():
    from test_order_follow_up import PLAN
    from autotradebot.core.enums import StrategyKind, Timeframe
    from autotradebot.core.models import Account, Play

    broker, repo = _StopBroker({"AAA": 4}), _Repo([])
    ex = _executor(broker, repo)
    ex.STAND_DOWN_S = ex.STAND_DOWN_POLL_S = 0.0
    play = Play(symbol="AAA", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.INTRADAY, entry=100.0, stop=98.0, targets=[104.0])
    play.suggested_qty = 10
    ex.execute_play(play, Account(account_id="DU"), plan=PLAN)
    broker.reports["1"] = OrderResult(order_id="1", status="WORKING", symbol="AAA", submitted_qty=10,
                                      filled_qty=4, avg_fill_price=100.0)
    ex.sync_open_orders()
    assert broker.stops() == []                                           # four shares bought, no record yet
    assert ex.expire_entries(mono=ex._pending["1"].first_fill_at + ex.cfg.partial_entry_wait_s) == ["1"]
    broker.reports["1"] = OrderResult(order_id="1", status="CANCELED", symbol="AAA", submitted_qty=10,
                                      filled_qty=4, avg_fill_price=100.0)
    ex.sync_open_orders()                                                 # booked, and protected in the same pass
    [stop] = broker.stops()
    assert (stop.quantity, stop.stop_price, stop.side) == (4.0, 98.0, Side.SHORT)
