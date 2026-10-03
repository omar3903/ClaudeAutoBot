"""The stop order the executor keeps at the broker for every open position (execution/protective_stops.py)."""

from __future__ import annotations

import datetime as dt
import threading
import time
from types import SimpleNamespace

import pytest

from test_order_follow_up import VENUE, _Broker, _executor, _refuses_once, _Repo, _trade
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


def test_a_stop_the_broker_didnt_answer_in_time_is_no_refusal_and_is_taken_over_never_doubled():
    from autotradebot.brokers.base import OrderOutcomeUnknown

    broker, repo, ex, heard = _setup()
    place = broker.place_order

    def unanswered(req):                                                       # it reaches the broker; the answer doesn't
        place(req)
        raise OrderOutcomeUnknown("IBKR didn't answer the order within 10 s", order_ref=req.client_tag)

    broker.place_order = unanswered
    ex.sync_open_orders()
    assert len(broker.stops()) == 1 and ex.protective_stops() == [] and "stop.failed" not in [t for t, _ in heard]
    broker.place_order = place
    ex.sync_open_orders()                                                      # a few seconds' wait first
    assert len(broker.stops()) == 1
    ex._stop_retry.clear()
    ex.sync_open_orders()
    assert len(broker.stops()) == 1 and ex.protective_stops()[0]["order_id"] == "1"   # taken over, not placed again


def test_a_stop_move_the_broker_didnt_answer_in_time_leaves_the_stop_resting_never_replaced():
    from autotradebot.brokers.base import OrderOutcomeUnknown

    broker, repo, ex, _ = _setup()
    ex.STOP_MOVE_S = 0.0
    ex.sync_open_orders()

    def unanswered(order_id, stop_price=None, quantity=None):
        raise OrderOutcomeUnknown("IBKR didn't answer the order within 10 s", order_ref="stop:t1")

    broker.modify_stop = unanswered
    repo.update_trade_risk("t1", stop_price=100.35)
    ex.sync_open_orders()
    assert broker.cancelled == [] and len(broker.stops()) == 1 and ex.protective_stops()[0]["order_id"] == "1"


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


def test_a_cancel_the_broker_never_confirmed_holds_back_every_exit_until_a_fill_would_have_been_heard():
    broker, _, ex, _ = _setup()
    ex.sync_open_orders()
    broker.unconfirmed = True                                                  # it reads cancelled: an error, no 202
    for _ in range(3):                                                         # the exit manager's next tries too
        out = ex.close_trade("t1", reason="stop")
        assert not out["ok"] and out["wait"] and broker.exits() == []
    ex._cancels_sent["1"] -= ex.CANCEL_UNCONFIRMED_S                           # half a minute on, and no fill came
    assert ex.close_trade("t1", reason="stop")["ok"] and [o.quantity for o in broker.exits()] == [10]


def test_a_cancel_the_broker_never_confirmed_is_left_to_the_exit_by_the_order_sync_between_its_tries():
    broker, repo, ex, heard = _setup()
    ex.STOP_MOVE_S = 0.0
    ex.sync_open_orders()
    broker.unconfirmed = True                                                  # it reads cancelled: an error, no 202
    assert ex.close_trade("t1", reason="stop")["wait"]
    repo.update_trade_risk("t1", stop_price=99.0)                              # the ratchet moves the record meanwhile
    for _ in range(3):                                                         # the sync loop between the exit's tries
        ex.sync_open_orders()
        out = ex.close_trade("t1", reason="stop")
        assert not out["ok"] and out["wait"] and broker.exits() == []
    assert ex.protective_stops()[0]["order_id"] == "1" and broker.modified == [] and len(broker.stops()) == 1
    assert "stop.lost" not in [t for t, _ in heard]
    broker.live["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                   avg_fill_price=97.9)
    ex.sync_open_orders()                                                      # the fill racing the cancel is booked
    closed = repo.get_trade("t1")
    assert (closed["status"], closed["exit_price"]) == ("CLOSED", 97.9) and broker.exits() == []


def test_a_stop_dropped_from_the_books_while_an_exit_stands_it_down_is_still_waited_for(monkeypatch):
    from autotradebot.execution import protective_stops as module

    broker, _, ex, _ = _setup()
    ex.sync_open_orders()                                                      # stop 1 rests for the 10 shares
    broker.slow_cancel = True                                                  # IBKR hasn't confirmed the cancel
    ex.STAND_DOWN_POLL_S = 0.25

    def refused(order_id, stop_price=None, quantity=None):
        raise BrokerError("IBKR refused to change the order")

    def meanwhile(seconds):                                                    # another thread, while it waits:
        st = ex._stops.get("t1")
        if st is not None:
            ex._move_stop(st, 98.5)                                            # a move refused drops the stop
    broker.modify_stop = refused
    monkeypatch.setattr(module.time, "sleep", meanwhile)
    out = ex.close_trade("t1", reason="stop")
    assert not out["ok"] and out["wait"] and broker.exits() == []
    assert ex.protective_stops()[0]["order_id"] == "1"                         # still working: followed again


def test_the_order_sync_leaves_a_trades_orders_alone_while_an_exit_stands_them_down(monkeypatch):
    from autotradebot.execution import protective_stops as module

    broker, repo, ex, _ = _setup()
    ex.sync_open_orders()
    broker.slow_cancel = True
    ex.STAND_DOWN_POLL_S, ex.STOP_MOVE_S = 0.25, 0.0
    repo.update_trade_risk("t1", stop_price=99.0)                              # the ratchet moved the record's stop
    monkeypatch.setattr(module.time, "sleep", lambda seconds: ex.sync_open_orders())   # a pass on the sync thread
    out = ex.close_trade("t1", reason="stop")
    assert not out["ok"] and out["wait"] and broker.exits() == [] and broker.modified == []
    ex.sync_open_orders()                                                      # the exit has let go, but its cancel
    assert broker.modified == []                                               # awaits IBKR's word: left to its next try
    ex._cancels_sent["1"] -= ex.CANCEL_UNCONFIRMED_S                           # half a minute on, still working
    ex.sync_open_orders()
    assert broker.modified == [("1", 99.0, None)]


def test_the_order_sync_books_nothing_of_a_stop_an_exit_is_standing_down():
    broker, repo, ex, heard = _setup()
    ex.sync_open_orders()
    broker.live["1"].status, broker.live["1"].filled_qty, broker.live["1"].avg_fill_price = "CANCELED", 4, 97.9
    assert ex._claim_resting("t1")                                             # an exit on another thread holds it
    ex.sync_open_orders()
    assert repo.get_trade("t1")["quantity"] == 10 and "trade.reduced" not in [t for t, _ in heard]   # the exit's to book
    assert ex.protective_stops()[0]["order_id"] == "1"
    ex._release_resting("t1")
    ex.sync_open_orders()
    assert repo.get_trade("t1")["quantity"] == 6 and [t for t, _ in heard].count("trade.reduced") == 1


def test_a_stop_placed_by_the_order_sync_never_lands_beside_a_close_clicked_meanwhile():
    broker, repo, ex, _ = _setup()
    read, closes = broker.get_account, []

    def get_account():                                                         # the pass is placing the stop when
        if not closes:                                                         # the Close button is clicked
            closes.append(ex.close_trade("t1", reason="manual"))
        return read()

    broker.get_account = get_account
    ex.sync_open_orders()
    assert closes[0]["wait"] and broker.exits() == [] and len(broker.stops()) == 1   # it waits a moment...
    broker.get_account = read
    assert ex.close_trade("t1", reason="manual")["ok"]                         # ...and then stands the stop down
    assert broker.cancelled == ["1"] and [o.quantity for o in broker.exits()] == [10]


def test_a_close_clicked_while_the_order_sync_holds_the_trades_orders_waits_a_moment_for_them():
    import threading

    broker, _, ex, _ = _setup()
    ex.sync_open_orders()
    ex.STAND_DOWN_S, ex.STAND_DOWN_POLL_S = 2.0, 0.02
    assert ex._claim_resting("t1")                                             # the pass is booking or moving its stop
    threading.Timer(0.2, ex._release_resting, args=("t1",)).start()
    assert ex.close_trade("t1", reason="manual")["ok"]                         # not bounced back to the user
    assert broker.cancelled == ["1"] and [o.quantity for o in broker.exits()] == [10]


def test_a_close_sent_after_the_order_sync_began_gets_no_stop_beside_it():
    broker, repo, ex, _ = _setup()
    claimed, sent = ex._claimed, []

    def claimed_then_a_close():                                                # the pass has looked at what is claimed;
        out = claimed()                                                        # the Close button is clicked just after
        sent.append(ex.close_trade("t1", reason="manual"))
        return out

    ex._claimed = claimed_then_a_close
    ex.sync_open_orders()
    assert sent[0]["ok"] and [o.quantity for o in broker.exits()] == [10] and broker.stops() == []


def _account_unanswered():
    raise BrokerError("IBKR didn't answer for the account values in time")    # nothing caps an exit's size then


def test_a_close_that_waited_for_another_closes_hold_on_the_trades_orders_sends_no_second_exit(monkeypatch):
    from autotradebot.execution import protective_stops as module

    broker, _, ex, _ = _setup()
    ex.sync_open_orders()                                                      # stop 1 rests
    ex.STAND_DOWN_S, ex.STAND_DOWN_POLL_S = 2.0, 0.01
    broker.get_account = _account_unanswered
    assert ex._claim_resting("t1")                                             # a close on another thread holds them
    first, began = [], []

    def meanwhile(seconds):                                                    # ...stands the stop down, sends its exit
        if not began:
            began.append(1)
            ex._release_resting("t1")
            first.append(ex.close_trade("t1", reason="stop"))

    monkeypatch.setattr(module.time, "sleep", meanwhile)
    second = ex.close_trade("t1", reason="manual")                             # the Close button, waiting for them
    assert first[0]["ok"] and not second["ok"] and "already working" in second["reason"]
    assert [o.quantity for o in broker.exits()] == [10]


def test_a_close_that_waited_while_the_order_sync_booked_the_stops_fill_sends_nothing(monkeypatch):
    from autotradebot.execution import protective_stops as module

    broker, repo, ex, _ = _setup()
    ex.sync_open_orders()                                                      # stop 1 rests
    ex.STAND_DOWN_S, ex.STAND_DOWN_POLL_S = 2.0, 0.01
    broker.get_account = _account_unanswered
    stop = broker.live["1"]
    stop.status, stop.filled_qty, stop.avg_fill_price = "FILLED", 10, 97.9
    assert ex._claim_resting("t1")                                             # the order sync is booking its fill...

    def meanwhile(seconds):                                                    # ...and is done while the close waits
        if "t1" in ex._stops:
            ex._book_stop_fill(ex._stops["t1"], stop)
        ex._release_resting("t1")

    monkeypatch.setattr(module.time, "sleep", meanwhile)
    out = ex.close_trade("t1", reason="manual")
    closed = repo.get_trade("t1")
    assert out["ok"] and out["status"] == "FILLED" and broker.exits() == []
    assert (closed["status"], closed["exit_reason"], closed["exit_price"]) == ("CLOSED", "stop", 97.9)


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


def test_a_cancel_ibkr_answers_with_some_other_error_holds_the_exit_back_on_the_next_pass_too(monkeypatch):
    broker, orders = _ibkr_broker(monkeypatch)
    repo = _Repo([_trade()])
    ex = _executor(broker, repo)
    ex.STAND_DOWN_S = ex.STAND_DOWN_POLL_S = 0.0
    ex.sync_open_orders()
    orders.answer_cancel = (10147, "OrderId 1 that needs to be cancelled is not found.")   # no 202: may still fill
    for _ in range(2):
        out = ex.close_trade("t1", reason="stop")
        assert not out["ok"] and out["wait"]
        ex.sync_open_orders()                                                  # the order sync runs between the tries
    assert [t.order.orderType for t in orders.book.values()] == ["STP"]       # no market exit beside it
    assert ex.protective_stops()[0]["order_id"] == "1"                         # still followed...
    orders.fill(1, 10.0, 97.9)
    orders.status(1, "Filled", filled=10.0, avg=97.9)
    ex.sync_open_orders()                                                      # ...so its fill is booked
    closed = repo.get_trade("t1")
    assert (closed["status"], closed["exit_price"], closed["exit_reason"]) == ("CLOSED", 97.9, "stop")
    assert [t.order.orderType for t in orders.book.values()] == ["STP"]


def _moved_silently_at_ibkr(monkeypatch):
    """The real IBKR adapter: a stop IBKR holds until its trigger (PreSubmitted) rests at 98, and the record's stop
    moves to 99 - IBKR says nothing to the move."""
    broker, orders = _ibkr_broker(monkeypatch)
    broker.MODIFY_ANSWER_S = 0.05
    repo = _Repo([_trade()])
    ex = _executor(broker, repo)
    ex.STAND_DOWN_S = ex.STAND_DOWN_POLL_S = ex.STOP_MOVE_S = 0.0
    ex.sync_open_orders()
    orders.book[1].orderStatus.status = "PreSubmitted"
    repo.update_trade_risk("t1", stop_price=99.0)
    ex.sync_open_orders()
    assert ex.protective_stops()[0]["stop_price"] == 99.0
    return broker, orders, repo, ex


def test_a_stop_move_ibkr_refuses_after_the_app_took_it_for_done_is_seen_and_sent_again(monkeypatch):
    broker, orders, repo, ex = _moved_silently_at_ibkr(monkeypatch)
    orders.error(1, 201, "Order rejected - reason: the order can't be changed")   # IBKR's no, a moment late
    ex.STOP_MOVE_S = 3600.0
    ex.sync_open_orders()
    assert ex.protective_stops()[0]["stop_price"] == 98.0                     # where IBKR still holds it
    orders.book[1].orderStatus.status = "PreSubmitted"                         # IBKR's own status, read again
    ex.STOP_MOVE_S = 0.0
    ex.sync_open_orders()                                                      # the move goes again
    assert orders.book[1].order.auxPrice == 99.0 and ex.protective_stops()[0]["stop_price"] == 99.0


def test_a_stop_ibkr_ended_soon_after_a_move_it_took_silently_never_holds_the_exit_back_for_good(monkeypatch):
    broker, orders, repo, ex = _moved_silently_at_ibkr(monkeypatch)
    orders.error(1, 201, "Order rejected - reason: the stop could not be routed")   # IBKR ends the stop
    orders.closed.add(1)                                                       # and no longer lists it
    orders.answer_cancel = (161, "Cancel attempted when order is not in a cancellable state. Order permId =77")
    out = ex.close_trade("t1", reason="stop")                                  # its list says the stop is gone
    assert out["ok"] and [t.order.orderType for t in orders.book.values()] == ["STP", "MKT"]


def test_a_stop_whose_move_ibkr_refused_late_rests_on_and_the_move_goes_again_once_ibkr_has_listed_it(monkeypatch):
    broker, orders, repo, ex = _moved_silently_at_ibkr(monkeypatch)
    orders.error(1, 201, "Order rejected - reason: the order can't be changed")   # IBKR's no, a moment late
    orders.listed_as[1] = "PreSubmitted"                                       # still working, IBKR's list will say
    ex.sync_open_orders()                                                      # back at 98; ib_async has it cancelled
    assert ex.protective_stops()[0]["order_id"] == "1"                         # so it is left resting - not cancelled
    assert [t.order.orderType for t in orders.book.values()] == ["STP"]        # for a move, nor replaced
    assert "PendingCancel" not in [entry.status for entry in orders.book[1].log]
    ex.sync_open_orders()                                                      # the list read put ib_async right
    assert orders.book[1].order.auxPrice == 99.0 and ex.protective_stops()[0]["stop_price"] == 99.0


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


def test_an_exit_right_after_a_start_stands_down_the_stop_an_earlier_run_left_and_only_one_exit_goes_out():
    from test_order_follow_up import CFG, SILENT
    from autotradebot.core.models import Quote
    from autotradebot.execution.exit_manager import ExitManager

    broker, repo, ex, _ = _setup()
    broker.live["77"] = _left_stop(qty=10)                                     # an earlier run's stop, still working
    broker.connected_since = time.monotonic()                                  # just connected: none taken over yet
    ex.sync_open_orders()
    assert ex.protective_stops() == [] and broker.cancelled == []
    em = ExitManager(repo, ex, quote_fn=lambda s: Quote(symbol=s, bid=97, ask=97, last=97), cfg=CFG,
                     bus=SILENT, venue=VENUE)
    em.run_once()                                                              # under the 98 stop: the app's own exit
    assert broker.cancelled == ["77"]                                          # the old stop stood down first
    (exit_order,) = broker.exits()
    assert (exit_order.quantity, exit_order.client_tag) == (10, "exit:t1")
    em.run_once()
    ex.sync_open_orders()
    assert len(broker.exits()) == 1 and broker.stops() == [] and ex.protective_stops() == []


def test_no_exit_goes_out_while_the_order_list_is_reloading_or_unreadable_and_no_stop_is_followed():
    from test_order_follow_up import _unanswered

    broker = _StopBroker({"AAA": 10, "BBB": 10})
    repo = _Repo([_trade(), _trade(id="p1", symbol="BBB", pair_id="pair_1")])
    ex = _executor(broker, repo)
    ex.STAND_DOWN_S = ex.STAND_DOWN_POLL_S = 0.0
    broker.connected_since = time.monotonic()                                  # an earlier run's stop may not be listed yet
    out = ex.close_trade("t1", reason="stop")
    assert not out["ok"] and "reloading" in out["reason"] and broker.orders == []
    assert ex.close_trade("p1", reason="pair-unwind")["ok"]                    # a pair leg has no stop to wait for
    broker.connected_since -= ex.RESYNC_GRACE_S + 1
    listed, broker.list_orders = broker.list_orders, _unanswered
    out = ex.close_trade("t1", reason="stop")
    assert not out["ok"] and "couldn't be read" in out["reason"] and len(broker.exits()) == 1
    broker.list_orders = listed
    assert ex.close_trade("t1", reason="stop")["ok"]                           # read, and nothing rests: it goes
    assert [(o.symbol, o.quantity) for o in broker.exits()] == [("BBB", 10), ("AAA", 10)]


def test_an_exit_held_back_while_the_order_list_reloads_goes_out_within_seconds_of_the_reload(monkeypatch):
    from test_order_follow_up import CFG
    from autotradebot.core.models import Quote
    from autotradebot.execution import exit_manager as module
    from autotradebot.execution.exit_manager import ExitManager

    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    events = []
    broker, repo, ex, _ = _setup()
    broker.connected_since = clock[0]                                          # just connected: nothing listed yet
    em = ExitManager(repo, ex, quote_fn=lambda s: Quote(symbol=s, bid=97, ask=97, last=97), cfg=CFG,
                     bus=SimpleNamespace(publish=lambda topic, **p: events.append(topic)), venue=VENUE)
    while not broker.exits() and clock[0] < 1000.0 + 3 * ex.RESYNC_GRACE_S:
        em.run_once()                                                          # under the 98 stop, pass after pass
        clock[0] += 1.0
    assert [o.quantity for o in broker.exits()] == [10]
    assert clock[0] <= 1000.0 + ex.RESYNC_GRACE_S + em.WAIT_RETRY_S + 1.0      # not a stretched back-off later
    assert "exit.failed" not in events                                         # a wait is no failed exit


def test_an_exit_that_keeps_waiting_on_the_broker_is_reported_and_reported_again(monkeypatch):
    from test_order_follow_up import CFG, _unanswered
    from autotradebot.core.models import Quote
    from autotradebot.execution import exit_manager as module
    from autotradebot.execution.exit_manager import ExitManager

    clock = [5000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    events = []
    broker, repo, ex, _ = _setup()
    broker.list_orders = _unanswered                                           # never read since the start
    em = ExitManager(repo, ex, quote_fn=lambda s: Quote(symbol=s, bid=97, ask=97, last=97), cfg=CFG,
                     bus=SimpleNamespace(publish=lambda topic, **p: events.append((topic, p))), venue=VENUE)

    def run(seconds):                                                          # exit-manager passes a second apart,
        end = clock[0] + seconds                                               # the price under the 98 stop
        while clock[0] < end:
            em.run_once()
            clock[0] += 1.0
        return [p for topic, p in events if topic == "exit.failed"]

    assert run(em.WAIT_WARN_S - 5) == [] and broker.exits() == []              # a wait is no failed exit...
    [failed] = run(10)                                                         # ...but one that goes on is told
    assert "waiting on the broker" in failed["reason"] and "couldn't be read" in failed["reason"]
    assert len(run(em.WAIT_REPEAT_S - 20)) == 1                                # not on every try
    assert len(run(30)) == 2 and broker.exits() == []                          # but again a few minutes on


def _waiting_exit(monkeypatch, price):
    """The exit manager over a trade whose order list never answers, on a clock of its own; ``price``: [the last]."""
    from test_order_follow_up import CFG, _unanswered
    from autotradebot.core.models import Quote
    from autotradebot.execution import exit_manager as module
    from autotradebot.execution.exit_manager import ExitManager

    clock, failed = [5000.0], []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    broker, repo, ex, _ = _setup()
    broker.list_orders = _unanswered
    em = ExitManager(repo, ex, quote_fn=lambda s: Quote(symbol=s, bid=price[0], ask=price[0], last=price[0]), cfg=CFG,
                     bus=SimpleNamespace(publish=lambda topic, **p: failed.append(p) if topic == "exit.failed" else None),
                     venue=VENUE)

    def run(seconds, apart=1.0):                                               # passes ``apart`` seconds apart
        end = clock[0] + seconds
        while clock[0] < end:
            em.run_once()
            clock[0] += apart
        return failed

    return broker, em, run


def test_an_exit_held_back_by_order_list_reads_that_time_out_is_reported_though_its_tries_come_far_apart(monkeypatch):
    broker, em, run = _waiting_exit(monkeypatch, price=[97.0])                 # under the 98 stop
    # each read of the list waits out its timeout - the exit's, and the order sync's own - so tries come ~20 s apart
    [failed] = run(em.WAIT_WARN_S + 20.0, apart=20.0)
    assert "waiting on the broker" in failed["reason"] and broker.exits() == []


def test_an_exit_wait_ends_when_the_price_comes_back_and_one_wanted_again_starts_afresh(monkeypatch):
    price = [97.0]
    broker, em, run = _waiting_exit(monkeypatch, price)
    assert run(em.WAIT_WARN_S - 10) == []                                      # under the stop, waiting 80 s
    price[0] = 99.0                                                            # back over it: no exit wanted
    run(5)
    price[0] = 97.0                                                            # under it again: a fresh wait
    assert run(em.WAIT_WARN_S - 10) == []
    assert len(run(20)) == 1 and broker.exits() == []


def test_an_unreadable_order_list_no_longer_holds_back_the_exit_of_a_trade_already_looked_for():
    from test_order_follow_up import _unanswered

    broker, repo, ex, _ = _setup()
    ex.sync_open_orders()                                                      # a full list: nothing left for t1
    broker.live["1"].status = "CANCELED"                                       # the broker drops the stop
    ex.sync_open_orders()
    assert ex.protective_stops() == []
    broker.list_orders = _unanswered                                           # and its orders can't be read now
    broker.positions["AAA"] = 6
    out = ex.close_trade("t1", reason="stop")
    assert out["ok"] and [o.quantity for o in broker.exits()] == [6]           # capped by the shares held, as before


def test_an_unreadable_order_list_no_longer_holds_back_the_exit_of_a_trade_this_run_opened():
    from test_order_follow_up import _unanswered

    broker, repo = _StopBroker({"AAA": 10}), _Repo([])
    ex = _executor(broker, repo)
    ex.STAND_DOWN_S = ex.STAND_DOWN_POLL_S = 0.0
    _day_entry(ex)
    broker.list_orders = _unanswered
    broker.reports["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                      avg_fill_price=100.0)
    ex.sync_open_orders()                                                      # booked; no stop placed blind
    [t] = repo.open_trades()
    assert broker.stops() == [] and ex.close_trade(t["id"], reason="stop")["ok"]
    assert [o.quantity for o in broker.orders if o.client_tag.startswith("exit:")] == [10]


def test_a_stop_dropped_after_a_refused_move_holds_the_exit_back_again_while_the_list_cant_be_read():
    from test_order_follow_up import _unanswered

    broker, repo, ex, _ = _setup(can_modify=False)
    ex.STOP_MOVE_S = 0.0
    ex.sync_open_orders()
    repo.update_trade_risk("t1", stop_price=99.0)
    ex.sync_open_orders()                                                      # cancelled to be replaced - not yet seen gone
    assert broker.cancelled == ["1"] and ex.protective_stops() == []
    listed, broker.list_orders = broker.list_orders, _unanswered
    out = ex.close_trade("t1", reason="stop")
    assert not out["ok"] and out["wait"] and broker.exits() == []
    broker.list_orders = listed                                                # a full list shows it gone: the exit goes
    assert ex.close_trade("t1", reason="stop")["ok"] and [o.quantity for o in broker.exits()] == [10]


def test_the_empty_list_ibkr_answers_while_disconnected_is_no_look_for_an_earlier_runs_stop(monkeypatch):
    from test_order_follow_up import _unanswered
    from autotradebot.core.models import OrderRequest

    broker, orders = _ibkr_broker(monkeypatch)                                 # the account holds the 10 shares
    broker.place_order(OrderRequest(symbol="AAA", side=Side.SHORT, quantity=10, order_type=OrderType.STOP,
                                    stop_price=98.0, tif=TimeInForce.GTC, is_entry=False, client_tag="stop:t1"))
    ex = _executor(broker, _Repo([_trade()]))                                  # a fresh run: an earlier run's stop rests
    ex.STAND_DOWN_S = ex.STAND_DOWN_POLL_S = 0.0
    broker._connected = False                                                  # IBKR's servers lost: it lists nothing
    out = ex.close_trade("t1", reason="manual")
    assert not out["ok"] and out.get("wait")
    broker._connected, broker.connected_since = True, time.monotonic()        # back, its orders reloading -
    broker.list_orders = _unanswered                                           # and not answering yet
    out = ex.close_trade("t1", reason="stop")
    assert not out["ok"] and out["wait"]
    assert [t.order.orderType for t in orders.book.values()] == ["STP"]       # no market exit beside the stop


def test_a_position_left_without_a_stop_because_the_order_list_cant_be_read_is_reported():
    from test_order_follow_up import _unanswered

    broker, _, ex, heard = _setup()
    broker.list_orders = _unanswered
    ex.sync_open_orders()
    assert broker.stops() == [] and ex.unprotected() == ["t1"]
    ex._bare_since["t1"] -= ex.UNPROTECTED_WARN_S
    ex.sync_open_orders()
    [missing] = [p for topic, p in heard if topic == "stop.missing"]
    assert "couldn't be read" in missing["reason"] and broker.orders == []
    assert missing["exit_held"] is True                                        # its exit waits too: no "exits it itself"


def test_an_exit_beside_an_earlier_runs_stop_that_filled_in_part_while_the_app_was_off_books_it_and_waits():
    broker, repo, ex, heard = _setup(_trade(quantity=100), positions={"AAA": 40})
    broker.live["77"] = _left_stop(filled=60, avg=97.9)                        # 60 of its 100 shares sold while off
    out = ex.close_trade("t1", reason="manual")
    assert not out["ok"] and "filled while the app was off" in out["reason"] and broker.exits() == []
    assert repo.get_trade("t1")["quantity"] == 40 and broker.cancelled == ["77"]
    out = ex.close_trade("t1", reason="manual")                                # the cancel has landed: the rest goes
    assert out["ok"] and [o.quantity for o in broker.exits()] == [40]
    assert [t for t, _ in heard].count("trade.reduced") == 1


def test_an_exit_beside_an_earlier_runs_stop_that_filled_whole_while_the_app_was_off_books_it_and_sends_nothing():
    broker, repo, ex, _ = _setup(_trade(quantity=100), positions={})
    broker.live["77"] = _left_stop(filled=100, avg=97.8)                       # its shares all sold while off
    out = ex.close_trade("t1", reason="manual")
    assert out["ok"] and out["by"] == "broker-stop" and broker.exits() == []
    closed = repo.get_trade("t1")
    assert (closed["status"], closed["exit_reason"], closed["exit_price"]) == ("CLOSED", "stop", 97.8)


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
    assert missing[0]["exit_held"] is False                                    # the app exits it itself meanwhile
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


# ---------------------------------------------------------------- orders that finished while the app wasn't following them
def _day_entry(ex, symbol="AAA"):
    from test_order_follow_up import PLAN
    from autotradebot.core.enums import StrategyKind, Timeframe
    from autotradebot.core.models import Account, Play

    play = Play(symbol=symbol, side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.INTRADAY, entry=100.0, stop=98.0, targets=[104.0])
    play.suggested_qty = 10
    ex.execute_play(play, Account(account_id="DU"), plan=PLAN)
    return play


def test_an_entry_the_broker_no_longer_knows_is_booked_from_its_executions_and_gets_its_stop():
    broker, repo, heard = _StopBroker({"AAA": 10}), _Repo([]), []
    ex = _executor(broker, repo, bus=SimpleNamespace(publish=lambda topic, **p: heard.append((topic, p))))
    play = _day_entry(ex)
    # it filled while the connection was down: the broker no longer knows the order, its executions show the fill
    broker.reports["1"] = OrderResult(order_id="1", status="UNKNOWN", symbol="?", submitted_qty=0)
    broker.get_fills = lambda symbol=None: [
        Fill(order_id="1", symbol="AAA", side=Side.LONG, quantity=10, price=99.97, tag=play.id),
        Fill(order_id="5", symbol="AAA", side=Side.SHORT, quantity=3, price=101.0, tag="exit:t9")]   # another order's
    for _ in range(ex.LOST_AFTER_POLLS):
        ex.sync_open_orders()
    [t] = repo.open_trades()
    assert (t["symbol"], t["quantity"], t["entry_price"]) == ("AAA", 10, 99.97)
    [stop] = broker.stops()
    assert (stop.quantity, stop.stop_price, stop.side, stop.client_tag) == (10, 98.0, Side.SHORT, f"stop:{t['id']}")
    assert "order.failed" not in [topic for topic, _ in heard] and ex.working_entries() == []


def test_an_entry_found_filled_after_a_reconnect_is_booked_and_protected(monkeypatch):
    from test_ibkr_adapter import _execution, _rebuilt

    broker, orders = _ibkr_broker(monkeypatch)                                 # the account holds the 10 shares
    repo = _Repo([])
    ex = _executor(broker, repo)
    play = _day_entry(ex)
    contract = orders.book[1].contract
    orders.book.clear()                                                        # the connection drops as it fills
    _rebuilt(orders, contract, 9001, "Filled", [_execution(1, 10.0, 99.96, broker.client_id)], action="BUY",
             totalQuantity=10, orderType="LMT", lmtPrice=100.0, orderRef=play.id)
    ex.sync_open_orders()
    [t] = repo.open_trades()
    assert (t["quantity"], t["entry_price"]) == (10.0, 99.96)
    stop = ex.protective_stops()[0]
    assert (stop["trade_id"], stop["qty"], stop["stop_price"]) == (t["id"], 10.0, 98.0)


def _sent(play_id, symbol, tags=()):
    """A play-log row an earlier run sent and never heard the end of."""
    return {"id": play_id, "symbol": symbol, "side": "LONG", "strategy": "vwap_reclaim", "kind": "TECHNICAL",
            "timeframe": "INTRADAY", "entry": 100.0, "stop": 98.0, "targets": [104.0], "confidence": 0.7,
            "sector": "", "tags": list(tags), "evidence": {}, "status": "SUBMITTED"}


def test_an_entry_an_earlier_run_sent_that_filled_while_the_app_was_off_is_booked_at_the_start_and_protected():
    from test_order_follow_up import _working

    broker = _StopBroker({"AAA": 10, "BBB": 2, "CCC": 5, "FFF": 3},
                         working=[_working("31", side=Side.LONG, tag="play_rest")])
    repo, asked = _Repo([]), []
    rows = [_sent("play_off", "AAA"), _sent("play_rest", "BBB"), _sent("play_leg", "CCC", tags=["pair-leg"]),
            _sent("play_none", "DDD"), _sent("play_sold", "EEE"), _sent("play_part", "FFF")]
    repo.submitted_plays = lambda since, until: asked.append((since, until)) or rows
    broker.get_fills = lambda symbol=None: [
        Fill(order_id="11", symbol="AAA", side=Side.LONG, quantity=6, price=100.0, tag="play_off"),
        Fill(order_id="11", symbol="AAA", side=Side.LONG, quantity=4, price=100.05, tag="play_off"),
        Fill(order_id="31", symbol="BBB", side=Side.LONG, quantity=2, price=50.0, tag="play_rest"),   # still working
        Fill(order_id="41", symbol="CCC", side=Side.LONG, quantity=5, price=20.0, tag="play_leg"),    # the desk's
        Fill(order_id="51", symbol="EEE", side=Side.LONG, quantity=5, price=30.0, tag="play_sold"),   # sold by hand
        Fill(order_id="61", symbol="FFF", side=Side.LONG, quantity=8, price=40.0, tag="play_part")]   # 3 still held
    ex = _executor(broker, repo)
    broker.is_connected = False
    ex.sync_open_orders()
    assert repo.open_trades() == [] and asked == []                            # looked for once it is connected
    broker.is_connected = True
    ex.sync_open_orders()
    t, part = repo.open_trades()
    assert (t["symbol"], t["quantity"]) == ("AAA", 10) and t["entry_price"] == pytest.approx(100.02)
    assert (part["symbol"], part["quantity"], part["entry_price"]) == ("FFF", 3, 40.0)   # what the account holds
    assert [(s.symbol, s.quantity, s.stop_price, s.client_tag) for s in broker.stops()] == [
        ("AAA", 10, 98.0, f"stop:{t['id']}"), ("FFF", 3, 98.0, f"stop:{part['id']}")]
    ex.sync_open_orders()
    assert len(repo.open_trades()) == 2 and len(asked) == 1                    # once
    since, until = asked[0]
    assert until - since == dt.timedelta(hours=ex.ENTRY_LOOKBACK_H)


def test_an_executions_read_that_fails_at_the_start_is_tried_again_and_the_entry_still_booked():
    broker = _StopBroker({"AAA": 10})
    repo, asked = _Repo([]), []
    repo.submitted_plays = lambda since, until: [_sent("play_off", "AAA")]

    def get_fills(symbol=None, strict=False):                                  # IBKR's, asked strictly
        asked.append(strict)
        if len(asked) == 1:
            raise BrokerError("IBKR's executions for the account couldn't be read: no answer in time")
        return [Fill(order_id="11", symbol="AAA", side=Side.LONG, quantity=10, price=100.0, tag="play_off")]

    broker.get_fills = get_fills
    broker.connected_since = time.monotonic()                                  # just connected: its lists reload
    ex = _executor(broker, repo)
    ex.sync_open_orders()
    assert asked == []                                                         # not looked for until they have
    broker.connected_since -= ex.RESYNC_GRACE_S + 1
    ex.sync_open_orders()
    ex.sync_open_orders()
    assert repo.open_trades() == [] and asked == [True]                        # failed - tried again in a while
    ex._entries_retry_at -= ex.ENTRY_LOOK_RETRY_S
    ex.sync_open_orders()
    [t] = repo.open_trades()
    assert (t["symbol"], t["quantity"], asked) == ("AAA", 10, [True, True])
    assert [(s.symbol, s.quantity) for s in broker.stops()] == [("AAA", 10)]


def test_an_entry_whose_booking_fails_at_the_start_is_looked_for_again_and_the_others_still_booked():
    broker, repo = _StopBroker({"AAA": 10, "BBB": 5}), _Repo([])
    repo.submitted_plays = lambda since, until: [_sent("play_a", "AAA"), _sent("play_b", "BBB")]
    broker.get_fills = lambda symbol=None: [
        Fill(order_id="11", symbol="AAA", side=Side.LONG, quantity=10, price=100.0, tag="play_a"),
        Fill(order_id="12", symbol="BBB", side=Side.LONG, quantity=5, price=50.0, tag="play_b")]
    opened, tries = repo.open_trade, []

    def open_trade(play, *a, **k):
        tries.append(play.id)
        if len(tries) == 1:
            raise RuntimeError("database is locked")                          # another thread is writing
        return opened(play, *a, **k)

    repo.open_trade = open_trade
    ex = _executor(broker, repo)
    ex.sync_open_orders()
    assert [t["symbol"] for t in repo.open_trades()] == ["BBB"]                # the other one is booked all the same
    ex.sync_open_orders()
    assert len(repo.open_trades()) == 1                                        # tried again in a while...
    ex._entries_retry_at -= ex.ENTRY_LOOK_RETRY_S
    ex.sync_open_orders()
    assert sorted((t["symbol"], t["quantity"]) for t in repo.open_trades()) == [("AAA", 10), ("BBB", 5)]   # ...once


def test_a_stop_the_broker_no_longer_knows_is_booked_from_its_executions_before_it_is_given_up():
    broker, repo, ex, heard = _setup()
    ex.sync_open_orders()                                                      # the stop rests: order 1
    broker.reports["1"] = OrderResult(order_id="1", status="UNKNOWN", symbol="?", submitted_qty=0)
    broker.get_fills = lambda symbol=None: [
        Fill(order_id="1", symbol="AAA", side=Side.SHORT, quantity=10, price=97.95, tag="stop:t1"),
        Fill(order_id="9", symbol="AAA", side=Side.SHORT, quantity=10, price=97.0, tag="stop:t1")]  # another stop's
    for _ in range(ex.LOST_AFTER_POLLS):
        ex.sync_open_orders()
    closed = repo.get_trade("t1")
    assert (closed["status"], closed["exit_reason"], closed["exit_price"]) == ("CLOSED", "stop", 97.95)
    assert "stop.lost" not in [topic for topic, _ in heard] and broker.stops()[1:] == []


def _unreadable_executions(symbol=None):
    raise BrokerError("IBKR's executions didn't arrive in time")


@pytest.mark.parametrize("executions", ["filled", "none", "unreadable"])
def test_an_exit_looks_in_the_executions_of_a_stop_the_broker_no_longer_knows_before_it_goes(executions):
    broker, repo, ex, _ = _setup()
    ex.sync_open_orders()                                                      # the stop rests: order 1
    broker.reports["1"] = OrderResult(order_id="1", status="UNKNOWN", symbol="?", submitted_qty=0)
    broker.get_fills = {                                                       # the link was down as it finished
        "filled": lambda symbol=None: [Fill(order_id="1", symbol="AAA", side=Side.SHORT, quantity=10, price=97.95,
                                            tag="stop:t1")],
        "none": lambda symbol=None: [],
        "unreadable": _unreadable_executions,
    }[executions]
    out = ex.close_trade("t1", reason="manual")
    t = repo.get_trade("t1")
    if executions == "filled":                                                 # booked - and nothing more sent
        assert out["ok"] and out["by"] == "broker-stop" and broker.exits() == []
        assert (t["status"], t["exit_reason"], t["exit_price"]) == ("CLOSED", "stop", 97.95)
    elif executions == "none":                                                 # gone, unfilled: the exit goes
        assert out["ok"] and [o.quantity for o in broker.exits()] == [10]
    else:                                                                      # not known whether it filled: it waits
        assert not out["ok"] and out["wait"] and broker.exits() == [] and t["status"] == "OPEN"
        assert ex.protective_stops()[0]["order_id"] == "1"


# ---------------------------------------------------------------- one caller at a time: threads side by side
def test_two_closes_sent_together_send_one_exit_and_the_second_hears_at_once():
    broker, _, ex, _ = _setup()
    ex.sync_open_orders()                                                      # stop 1 rests
    ex.STAND_DOWN_S, ex.STAND_DOWN_POLL_S = 2.0, 0.01
    sending, place = threading.Event(), broker.place_order

    def place_order(req):                                                      # the first close's exit is on its way
        sending.set()
        time.sleep(0.5)
        return place(req)

    broker.place_order = place_order
    first = []
    closing = threading.Thread(target=lambda: first.append(ex.close_trade("t1", reason="stop")))
    closing.start()
    assert sending.wait(2)
    began = time.monotonic()
    second = ex.close_trade("t1", reason="manual")                             # the Close button, meanwhile
    waited = time.monotonic() - began
    closing.join(5)
    assert first[0]["ok"] and [o.quantity for o in broker.exits()] == [10]
    assert not second["ok"] and second["wait"] and second["reason"] == "An exit for this position is already being sent"
    assert waited < 0.3                                                        # not queued behind the first


def test_two_order_syncs_side_by_side_book_an_entry_filled_while_the_app_was_off_once_and_rest_one_stop():
    broker, repo = _StopBroker({"AAA": 10}), _Repo([])
    repo.submitted_plays = lambda since, until: [_sent("play_off", "AAA")]
    filled = [Fill(order_id="11", symbol="AAA", side=Side.LONG, quantity=10, price=100.0, tag="play_off")]
    broker.get_fills = lambda symbol=None: time.sleep(0.1) or list(filled)    # IBKR takes a moment to answer...
    opened = repo.open_trade
    repo.open_trade = lambda *a, **k: time.sleep(0.1) or opened(*a, **k)       # ...and so does the database
    ex = _executor(broker, repo)
    together = threading.Barrier(2)

    def sync():                                                                # the sync loop, and the Refresh button
        together.wait(2)
        ex.sync_open_orders()

    passes = [threading.Thread(target=sync) for _ in range(2)]
    for p in passes:
        p.start()
    for p in passes:
        p.join(10)
    [t] = repo.open_trades()
    assert (t["symbol"], t["quantity"]) == ("AAA", 10)
    assert [(s.quantity, s.client_tag) for s in broker.stops()] == [(10, f"stop:{t['id']}")]


def test_the_order_sync_books_nothing_of_a_stop_fill_an_exit_took_and_booked_first():
    broker, repo, ex, heard = _setup()
    ex.sync_open_orders()                                                      # stop 1 rests
    stop = broker.live["1"]
    stop.status, stop.filled_qty, stop.avg_fill_price = "CANCELED", 4, 97.9   # four filled, then it was cancelled
    read, inside = broker.get_order, []

    def get_order(order_id):                                                   # an exit's stand-down finds it done
        if order_id == "1" and not inside:                                     # just as the pass reads it, and books
            inside.append(1)                                                   # its fill first
            ex._stand_down("t1")
        return read(order_id)

    broker.get_order = get_order
    ex.sync_open_orders()
    assert repo.get_trade("t1")["quantity"] == 6 and [t for t, _ in heard].count("trade.reduced") == 1


def test_an_exit_books_nothing_of_a_stop_fill_the_order_sync_took_and_booked_first():
    broker, repo, ex, heard = _setup()
    ex.sync_open_orders()                                                      # stop 1 rests
    stop = broker.live["1"]
    stop.status, stop.filled_qty, stop.avg_fill_price = "CANCELED", 4, 97.9   # four filled, then it was cancelled
    read, inside = broker.get_order, []

    def get_order(order_id):                                                   # the order sync's watch finds it done
        if order_id == "1" and not inside:                                     # just as the stand-down reads it, and
            inside.append(1)                                                   # books its fill first
            ex._watch_one(ex._stops, ex._book_stop_fill, ex._stops["t1"])
        return read(order_id)

    broker.get_order = get_order
    out = ex.close_trade("t1", reason="manual")
    assert repo.get_trade("t1")["quantity"] == 6 and [t for t, _ in heard].count("trade.reduced") == 1
    assert not out["ok"] and out["wait"] and broker.exits() == []              # the rest goes on its next try
    assert ex.close_trade("t1", reason="manual")["ok"] and [o.quantity for o in broker.exits()] == [6]



def test_a_stop_fill_whose_booking_fails_is_booked_on_the_next_pass_and_nothing_else_is_done_meanwhile():
    broker, repo, ex, heard = _setup()
    ex.STOP_MOVE_S = 0.0
    ex.sync_open_orders()                                                      # the stop rests: order 1
    calls = _refuses_once(repo, "close_trade")
    broker.live["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                   avg_fill_price=97.9)
    repo.update_trade_risk("t1", stop_price=99.0)                              # the exit manager ratchets the record
    ex.sync_open_orders()
    assert repo.get_trade("t1")["status"] == "OPEN" and ex.fill_unbooked("t1")
    assert len(broker.stops()) == 1 and broker.modified == []                  # never moved, nor placed afresh
    out = ex.close_trade("t1", reason="stop")                                  # the exit manager sees the cross
    assert not out["ok"] and out["wait"] and broker.exits() == []              # no exit of the app's own beside it
    assert [t for t, _ in heard].count("order.unbooked") == 1

    ex.sync_open_orders()
    closed = repo.get_trade("t1")
    assert (closed["status"], closed["exit_price"], closed["exit_reason"]) == ("CLOSED", 97.9, "stop")
    assert not ex.fill_unbooked("t1") and ex.protective_stops() == [] and len(calls) == 2
    assert len(broker.orders) == 1 and broker.modified == []


def test_a_stop_fill_an_exit_finds_whose_booking_fails_holds_the_exit_back_until_the_next_pass_books_it():
    broker, repo, ex, _ = _setup()
    ex.sync_open_orders()                                                      # the stop rests: order 1
    calls = _refuses_once(repo, "close_trade")
    broker.live["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                   avg_fill_price=97.9)
    out = ex.close_trade("t1", reason="stop")                                  # its stand-down finds the stop filled
    assert not out["ok"] and out["wait"] and broker.exits() == [] and ex.fill_unbooked("t1")
    ex.sync_open_orders()
    closed = repo.get_trade("t1")
    assert (closed["status"], closed["exit_price"], closed["exit_reason"]) == ("CLOSED", 97.9, "stop")
    assert ex.close_trade("t1", reason="stop")["reason"] == "trade not open" and broker.exits() == []
    assert len(calls) == 2

def test_syncs_exits_entries_take_overs_and_cancels_from_many_threads_never_deadlock():
    from test_order_follow_up import PLAN
    from autotradebot.core.enums import StrategyKind, Timeframe
    from autotradebot.core.models import Account, Play

    broker = _StopBroker({"AAA": 10, "BBB": 10, "CCC": 5, "EEE": 5})
    repo = _Repo([_trade(id="t1"), _trade(id="t2", symbol="BBB")])
    repo.submitted_plays = lambda since, until: [_sent("play_off", "EEE")]
    ex = _executor(broker, repo)
    ex.STAND_DOWN_S, ex.STAND_DOWN_POLL_S = 1.0, 0.01
    ex._entries_due = False
    ex.sync_open_orders()                                                      # both positions' stops rest
    ex._entries_due = True                                                     # an entry filled while the app was off
    filled = [Fill(order_id="11", symbol="EEE", side=Side.LONG, quantity=5, price=30.0, tag="play_off")]
    broker.get_fills = lambda symbol=None: time.sleep(0.05) or [f for f in filled if symbol in (None, f.symbol)]
    opened = repo.open_trade
    repo.open_trade = lambda *a, **k: time.sleep(0.05) or opened(*a, **k)

    def slow(fn):                                                              # every broker call takes a moment
        return lambda *a, **k: time.sleep(0.005) or fn(*a, **k)

    for name in ("place_order", "get_order", "cancel_order", "list_orders", "get_account", "modify_stop"):
        setattr(broker, name, slow(getattr(broker, name)))
    entry = Play(symbol="DDD", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                 timeframe=Timeframe.SWING, entry=50.0, stop=48.0, targets=[56.0])
    entry.suggested_qty = 10
    jobs = [lambda: [ex.sync_open_orders() for _ in range(5)],                 # the sync loop...
            lambda: [ex.sync_open_orders() for _ in range(5)],                 # ...a second one beside it...
            lambda: [ex.sync_open_orders(wait=False) for _ in range(5)],       # ...and the Refresh button
            lambda: ex.close_trade("t1", reason="manual"),                     # a click, and a quit, on one position
            lambda: ex.close_trade("t1", reason="quit"),
            lambda: ex.close_trade("t2", reason="quit"),
            lambda: ex.close_untracked("CCC", "LONG", 5),
            lambda: ex.execute_play(entry, Account(account_id="DU"), plan=PLAN),   # Autopilot
            ex.adopt_working_orders,
            lambda: ex.cancel_exits(reasons=("none",)),
            lambda: ex.cancel_entries_for("play_none")]
    together = threading.Barrier(len(jobs))
    threads = [threading.Thread(target=lambda job=job: (together.wait(5), job()), daemon=True) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads)                              # every one of them finished
    assert sorted(o.client_tag for o in broker.exits() if o.client_tag.startswith("exit:")) == ["exit:t1", "exit:t2"]
    [booked] = [t for t in repo.open_trades() if t["symbol"] == "EEE"]          # booked once, with one stop
    assert [s.quantity for s in broker.stops() if s.client_tag == f"stop:{booked['id']}"] == [5]
