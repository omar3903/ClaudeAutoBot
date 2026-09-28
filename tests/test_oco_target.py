"""The target order resting at the broker in one one-cancels-all group with the stop (execution/protective_stops.py)."""

from __future__ import annotations

from types import SimpleNamespace

from test_native_stop import _StopBroker
from test_order_follow_up import CFG, _executor, _Repo, _trade
from autotradebot.brokers.base import BrokerError
from autotradebot.core.enums import OrderType, Side, TimeInForce
from autotradebot.core.models import Fill, OrderResult
from autotradebot.execution.exit_manager import ExitManager, scale_out_plan

EXITS = SimpleNamespace(scale_out_pct=50.0, scale_out_lock_r=0.0, breakeven_buffer_bps=5.0)


class _OcaBroker(_StopBroker):
    """Rests stops and targets, and treats a fill the way IBKR's reducing one-cancels-all group does:
    the other orders of the group shrink by the shares filled, and one shrunk to nothing is cancelled."""

    def __init__(self, *a, refuse_groups=False, **kw):
        super().__init__(*a, **kw)
        self.groups, self.refuse_groups = {}, refuse_groups

    def place_order(self, req):
        if req.oca_group and self.refuse_groups:
            raise BrokerError("OCA groups are not allowed on this account")
        res = super().place_order(req)
        res.limit_price = req.limit_price
        if req.client_tag.startswith(("stop:", "tgt:")):
            self.live[res.order_id] = res
            if req.oca_group:
                self.groups[res.order_id] = (req.oca_group, req.oca_type)
        return res

    def fill(self, order_id, qty, price):
        o = self.live[order_id]
        o.status, o.filled_qty, o.avg_fill_price = "FILLED", qty, price
        group = self.groups.get(order_id, ("", 0))[0]
        for other_id, (g, _) in self.groups.items():
            other = self.live[other_id]
            if g and g == group and other_id != order_id and other.status not in ("FILLED", "CANCELED"):
                other.submitted_qty -= qty
                if other.submitted_qty <= 0:
                    other.status = "CANCELED"

    def targets(self):
        return [r for r in self.orders if r.client_tag.startswith("tgt:")]

    def exits(self):
        return [r for r in self.orders if r.client_tag.startswith("exit:")]


def _setup(trade=None, **kw):
    trade = trade or _trade(target_price=104.0, target2_price=110.0, initial_quantity=10)
    broker = _OcaBroker({"AAA": 10}, **kw)
    repo, heard = _Repo([trade]), []
    ex = _executor(broker, repo, bus=SimpleNamespace(publish=lambda topic, **p: heard.append(topic)))
    ex.exit_cfg = EXITS
    ex.STAND_DOWN_S = ex.STAND_DOWN_POLL_S = ex.STOP_MOVE_S = 0.0
    return broker, repo, ex, heard


def test_the_stop_and_the_first_targets_part_rest_together_in_one_reducing_group():
    broker, _, ex, heard = _setup()
    ex.sync_open_orders()
    (stop,), (target,) = broker.stops(), broker.targets()
    assert (stop.quantity, stop.stop_price) == (10, 98.0)
    assert (target.side, target.quantity, target.order_type, target.limit_price) == (Side.SHORT, 5, OrderType.LIMIT, 104.0)
    assert target.tif is TimeInForce.GTC and not target.is_entry and target.client_tag == "tgt:t1"
    assert stop.oca_group == target.oca_group != "" and stop.oca_type == target.oca_type == 3
    assert ex.target_resting("t1") and heard == ["stop.placed", "target.placed"]
    assert ex.resting_targets() == [{"trade_id": "t1", "symbol": "AAA", "order_id": "2", "qty": 5.0, "limit_price": 104.0}]
    ex.sync_open_orders()
    assert len(broker.orders) == 2                                             # once is enough


def test_a_position_that_exits_whole_gets_a_target_for_all_of_it():
    broker, _, ex, _ = _setup(_trade(target_price=104.0))                      # no second target: no scale-out
    ex.sync_open_orders()
    assert broker.targets()[0].quantity == 10


def test_without_the_engines_exit_settings_or_with_targets_off_only_the_stop_rests():
    broker, _, ex, _ = _setup()
    ex.exit_cfg = None
    ex.sync_open_orders()
    assert len(broker.stops()) == 1 and broker.targets() == [] and broker.stops()[0].oca_group == ""


def test_the_target_taking_half_off_is_booked_and_the_rest_gets_a_pair_of_its_own():
    broker, repo, ex, heard = _setup()
    ex.sync_open_orders()
    broker.fill("2", 5, 104.02)                                                # the broker shrinks the stop to 5 itself
    ex.sync_open_orders()
    t = repo.get_trade("t1")
    assert t["status"] == "OPEN" and t["quantity"] == 5 and t["target_price"] == 110.0
    assert abs(t["stop_price"] - 100.05) < 1e-9                                # break-even plus the buffer
    assert "stop.lost" not in heard
    # the old stop is stood down and a fresh pair rests for the five shares left: break-even stop, second target
    assert "1" in broker.cancelled
    stop, target = broker.stops()[-1], broker.targets()[-1]
    assert (stop.quantity, stop.stop_price, target.quantity, target.limit_price) == (5, 100.05, 5, 110.0)
    assert stop.oca_group == target.oca_group and stop.oca_group != broker.stops()[0].oca_group
    assert broker.exits() == []                                                # the app never sent an exit of its own

    broker.fill("4", 5, 110.0)                                                 # the second target: the position is out
    ex.sync_open_orders()
    closed = repo.get_trade("t1")
    assert closed["status"] == "CLOSED" and closed["exit_reason"] == "target" and closed["exit_price"] == 110.0
    assert ex.protective_stops() == [] and ex.resting_targets() == [] and "stop.lost" not in heard
    assert broker.exits() == []


def test_the_stop_filling_first_closes_the_trade_and_the_target_goes_quietly():
    broker, repo, ex, heard = _setup()
    ex.sync_open_orders()
    broker.fill("1", 10, 97.9)                                                 # the group cancels the target
    ex.sync_open_orders()
    closed = repo.get_trade("t1")
    assert (closed["status"], closed["exit_reason"], closed["exit_price"]) == ("CLOSED", "stop", 97.9)
    assert ex.resting_targets() == [] and ex.protective_stops() == [] and broker.exits() == []
    assert "stop.lost" not in heard and "stop.failed" not in heard


def test_the_apps_own_exit_stands_both_orders_down_first():
    broker, _, ex, _ = _setup()
    ex.sync_open_orders()
    out = ex.close_trade("t1", reason="eod-flatten")
    assert out["ok"] and sorted(broker.cancelled) == ["1", "2"] and not ex.target_resting("t1")
    (exit_order,) = broker.exits()
    assert exit_order.quantity == 10
    broker.slow_cancel = True


def test_an_exit_after_the_target_took_its_part_covers_only_what_is_left():
    broker, repo, ex, _ = _setup()
    ex.sync_open_orders()
    broker.fill("2", 5, 104.0)                                                 # filled a moment before the app's exit
    out = ex.close_trade("t1", reason="manual")
    assert out["ok"] and repo.get_trade("t1")["quantity"] == 5                 # the target's part was booked first
    assert broker.exits()[0].quantity == 5 and ex.protective_stops() == []


def test_while_a_target_rests_at_the_broker_the_exit_manager_leaves_the_target_to_it():
    sent = []
    repo = _Repo([_trade(target_price=104.0, target2_price=110.0, initial_quantity=10)])
    executor = SimpleNamespace(pending_exit_trade_ids=lambda: set(), target_resting=lambda tid: True,
                               close_trade=lambda tid, **kw: sent.append((tid, kw)) or {"ok": True, "trade": {}})
    manager = ExitManager(repo, executor, quote_fn=lambda s: SimpleNamespace(last=104.5, mid=104.5), cfg=CFG,
                          bus=SimpleNamespace(publish=lambda *a, **k: None), venue="ibkr-paper")
    manager.run_once()
    assert sent == []                                                          # at the target, and nothing sent
    executor.target_resting = lambda tid: False                                # no resting target: the app's job again
    manager.run_once()
    assert [tid for tid, _ in sent] == ["t1"]


def test_a_broker_that_refuses_the_pair_gets_a_stop_alone():
    broker, _, ex, heard = _setup(refuse_groups=True)
    ex.sync_open_orders()
    assert broker.orders == [] and heard == ["stop.failed"]
    ex._stop_retry.clear()
    ex.sync_open_orders()
    assert len(broker.stops()) == 1 and broker.targets() == [] and broker.stops()[0].oca_group == ""
    ex.sync_open_orders()
    assert len(broker.orders) == 1 and not ex.target_resting("t1")             # and it isn't tried again for this trade


def test_a_pair_the_broker_rejects_a_moment_after_taking_it_is_replaced_by_a_stop_alone():
    broker, _, ex, _ = _setup()
    ex.sync_open_orders()
    broker.live["1"].status, broker.live["1"].message = "REJECTED", "OCA group revision is not allowed"
    ex.sync_open_orders()
    assert "2" in broker.cancelled and ex.protective_stops() == []              # the target goes with it
    ex._stop_retry.clear()
    ex.sync_open_orders()
    assert broker.stops()[-1].oca_group == "" and len(broker.targets()) == 1


def test_orders_an_earlier_run_left_are_followed_and_a_stray_target_is_cancelled():
    left = [OrderResult(order_id="70", status="SUBMITTED", symbol="AAA", submitted_qty=10, side=Side.SHORT,
                        tag="stop:t1", order_type="STOP", stop_price=98.0),
            OrderResult(order_id="71", status="SUBMITTED", symbol="AAA", submitted_qty=5, side=Side.SHORT,
                        tag="tgt:t1", order_type="LIMIT", limit_price=104.0),
            OrderResult(order_id="90", status="SUBMITTED", symbol="ZZZ", submitted_qty=4, side=Side.SHORT,
                        tag="tgt:gone", order_type="LIMIT", limit_price=9.0)]
    broker, _, ex, _ = _setup(working=left)
    ex.sync_open_orders()
    assert broker.orders == [] and ex.protective_stops()[0]["order_id"] == "70"
    assert ex.resting_targets()[0]["order_id"] == "71" and broker.cancelled == ["90"]
    described = {o["order_id"]: (o["purpose"], o["trade_id"]) for o in ex.active_orders()}
    assert described["71"] == ("target", "t1")
    assert ex.cancel_working_orders()["stops_kept"] == 2 and broker.cancelled == ["90"]   # neither is touched


def test_a_target_that_filled_in_part_while_the_app_was_off_is_booked_as_the_first_target():
    left = [OrderResult(order_id="70", status="SUBMITTED", symbol="AAA", submitted_qty=7, side=Side.SHORT,
                        tag="stop:t1", order_type="STOP", stop_price=98.0),              # the group shrank it by 3
            OrderResult(order_id="71", status="SUBMITTED", symbol="AAA", submitted_qty=5, side=Side.SHORT,
                        tag="tgt:t1", order_type="LIMIT", limit_price=104.0)]
    broker, repo, ex, _ = _setup(working=left)
    broker.positions["AAA"] = 7
    broker.get_fills = lambda symbol=None: [Fill(order_id="71", symbol="AAA", side=Side.SHORT, quantity=3,
                                                 price=104.05, tag="tgt:t1")]
    got = []
    ex.bus = SimpleNamespace(publish=lambda topic, **p: got.append((topic, p)))
    ex.sync_open_orders()
    [reduced] = [p for topic, p in got if topic == "trade.reduced"]
    assert (reduced["qty"], reduced["price"], reduced["reason"]) == (3, 104.05, "target-1")
    t = repo.get_trade("t1")
    assert (t["quantity"], t["stop_price"], t["target_price"]) == (7, 100.05, 110.0)   # what the rest now has to do
    assert sorted(broker.cancelled) == ["70", "71"] and ex.protective_stops() == []
    ex._stop_retry.clear()
    ex.sync_open_orders()
    (stop,), (target,) = broker.stops(), broker.targets()
    assert (stop.quantity, stop.stop_price, target.quantity, target.limit_price) == (7, 100.05, 7, 110.0)
    ex.sync_open_orders()
    assert [topic for topic, _ in got].count("trade.reduced") == 1 and len(broker.orders) == 2


def test_the_part_a_target_covers_is_the_part_the_exit_manager_would_take():
    t = _trade(target_price=104.0, target2_price=110.0, initial_quantity=10)
    part, after = scale_out_plan(t, EXITS)
    assert part == 5 and after == {"stop_price": 100.05, "target_price": 110.0}
    assert scale_out_plan({**t, "quantity": 5}, EXITS) is None                 # already taken
    assert scale_out_plan({**t, "managed_exit": False}, EXITS) is None and scale_out_plan(t, None) is None


def test_a_plain_stop_from_before_the_update_is_replaced_by_the_pair():
    plain = [OrderResult(order_id="70", status="SUBMITTED", symbol="AAA", submitted_qty=10, side=Side.SHORT,
                         tag="stop:t1", order_type="STOP", stop_price=98.0)]
    broker, _, ex, _ = _setup()
    broker.live["70"] = plain[0]                                               # resting at the broker since the last run
    ex.sync_open_orders()                                                       # found and followed...
    ex.sync_open_orders()                                                       # ...then stood down for the pair
    assert "70" in broker.cancelled
    (stop,), (target,) = broker.stops(), broker.targets()
    assert (stop.quantity, stop.stop_price, target.quantity, target.limit_price) == (10, 98.0, 5, 104.0)
    assert stop.oca_group == target.oca_group != "" and ex.protective_stops()[0]["order_id"] != "70"
    ex.sync_open_orders()
    assert len(broker.orders) == 2                                             # settled: nothing more is sent
