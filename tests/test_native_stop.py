"""The stop order the executor keeps at the broker for every open position (execution/protective_stops.py)."""

from __future__ import annotations

from types import SimpleNamespace

from test_order_follow_up import VENUE, _Broker, _executor, _Repo, _trade
from tos_bot.core.enums import OrderType, Side, TimeInForce
from tos_bot.core.models import OrderResult


class _StopBroker(_Broker):
    """An account that can rest a stop order: it stays working until a test fills or loses it."""

    supports_native_stop = True

    def __init__(self, positions=None, working=None, can_modify=True):
        super().__init__(positions, working)
        self.live, self.modified, self.can_modify = {}, [], can_modify
        self.slow_cancel = False                 # the broker hasn't confirmed the cancel yet

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

    def modify_stop(self, order_id, stop_price=None, quantity=None):
        if not self.can_modify:
            raise NotImplementedError("no modify here")
        o = self.live[order_id]
        o.stop_price, o.submitted_qty = stop_price, quantity
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
    assert broker.modified == [("1", 100.35, 10.0)] and ex.protective_stops()[0]["stop_price"] == 100.35
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
    assert repo.get_trade("t1")["quantity"] == 5 and broker.modified[-1] == ("1", 100.05, 5.0)   # ...then to break-even


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
