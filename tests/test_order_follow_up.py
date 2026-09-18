"""After an order is sent: fills, rejections, cancellations, and orders the broker loses track of."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tos_bot.config import get_settings
from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.core.models import Account, OrderResult, Play, Position, Quote
from tos_bot.execution import exit_manager as exit_manager_module
from tos_bot.execution.executor import Executor
from tos_bot.execution.exit_manager import ExitManager

VENUE = "ibkr-paper"
SILENT = SimpleNamespace(publish=lambda *a, **k: None)
CFG = SimpleNamespace(enabled=True, breakeven_at_r=0, breakeven_buffer_bps=0, trail_start_r=0,
                      trail_lock_ratio=0, flatten_intraday_before_close_min=0, max_swing_hold_days=0)
PLAN = {"executable": True, "order_type": "LIMIT", "limit_price": 100.0, "order_session": "REGULAR"}


def _trade(**kw):
    d = dict(id="t1", symbol="AAA", side="LONG", strategy="vwap_reclaim", kind="TECHNICAL", timeframe="SWING",
             status="OPEN", broker=VENUE, entry_price=100.0, quantity=10, stop_price=98.0, target_price=110.0,
             initial_stop_price=98.0, hwm_price=100.0, managed_exit=True, mae=0.0, mfe=0.0,
             entry_time="2026-09-03T09:40:00")
    d.update(kw)
    return d


class _Repo:
    def __init__(self, trades):
        self.t = {x["id"]: dict(x) for x in trades}

    def open_trades(self):
        return [dict(x) for x in self.t.values() if x["status"] == "OPEN"]

    def get_trade(self, tid):
        return dict(self.t[tid]) if tid in self.t else None

    def update_trade_risk(self, tid, **kw):
        self.t[tid].update({k: v for k, v in kw.items() if v is not None})

    def open_trade(self, play, price, qty, venue, order_id, order_type="LIMIT", order_session="REGULAR",
                   entry_context=None, submitted_at=None):
        tid = f"t{len(self.t) + 1}"
        self.t[tid] = _trade(id=tid, symbol=play.symbol, entry_price=price, quantity=qty, broker=venue)
        self.t[tid]["entry_context"], self.t[tid]["submitted_at"] = entry_context, submitted_at
        return tid

    def close_trade(self, tid, exit_price, exit_reason=""):
        self.t[tid].update(status="CLOSED", exit_price=exit_price, exit_reason=exit_reason)
        return dict(self.t[tid])

    def reduce_trade(self, tid, exit_qty, exit_price, exit_reason="", commission=0.0, stop_price=None,
                     target_price=None):
        t = self.t[tid]
        t["quantity"] -= exit_qty
        t["banked_pl"] = t.get("banked_pl", 0.0) + (exit_price - t["entry_price"]) * exit_qty
        t.update({k: v for k, v in (("stop_price", stop_price), ("target_price", target_price)) if v is not None})
        return dict(t)

    def get_play(self, play_id):
        return {"id": play_id, "symbol": "AAA", "side": "LONG", "strategy": "vwap_reclaim", "kind": "TECHNICAL",
                "timeframe": "INTRADAY", "entry": 100.0, "stop": 98.0, "targets": [104.0], "confidence": 0.7,
                "sector": "Technology"} if play_id == "play_left" else None


def _working(order_id, qty=10, side=Side.SHORT, tag="", **raw):
    """An order an earlier run of the app left working at the broker."""
    return OrderResult(order_id=order_id, status="SUBMITTED", symbol="AAA", submitted_qty=qty,
                       side=side, tag=tag, raw=raw)


class _Broker:
    """An account that takes orders and leaves them working until a test reports otherwise."""

    name, paper, supports_bracket_native = "ibkr", False, False

    def __init__(self, positions=None, working=None):
        self.positions = dict(positions or {})
        self.working = list(working or [])      # orders an earlier run left working
        self.orders, self.cancelled, self.reports = [], [], {}

    def place_order(self, req):
        self.orders.append(req)
        return OrderResult(order_id=str(len(self.orders)), status="SUBMITTED", symbol=req.symbol,
                           submitted_qty=req.quantity)

    def get_order(self, order_id):
        return self.reports.get(order_id) or OrderResult(order_id=order_id, status="WORKING", symbol="AAA",
                                                         submitted_qty=10)

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    def list_orders(self, status=None):
        return [o for o in self.working if o.order_id not in self.cancelled] if status == "WORKING" else []

    def get_account(self):
        return Account(account_id="DU", positions=[Position(symbol=s, quantity=q, avg_price=100.0)
                                                   for s, q in self.positions.items()])


def _executor(broker, repo, bus=SILENT):
    return Executor(broker, repo, cfg=get_settings().config.execution, bus=bus, venue=VENUE)


# ---------------------------------------------------------------- exits
def test_a_rejected_exit_is_reported_and_sent_again_after_a_wait(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(exit_manager_module.time, "monotonic", lambda: now[0])
    events = []
    bus = SimpleNamespace(publish=lambda topic, **kw: events.append((topic, kw)))
    broker, repo = _Broker({"AAA": 10}), _Repo([_trade()])
    ex = _executor(broker, repo, bus)
    em = ExitManager(repo, ex, quote_fn=lambda s: Quote(symbol=s, bid=90, ask=90, last=90), cfg=CFG,
                     bus=bus, venue=VENUE)

    em.run_once()                                       # 90 is under the 98 stop: sell the 10 shares
    em.run_once()                                       # still working - nothing more goes out
    assert [(o.side, o.quantity) for o in broker.orders] == [(Side.SHORT, 10)]

    broker.reports["1"] = OrderResult(order_id="1", status="CANCELED", symbol="AAA", submitted_qty=10,
                                      message="Order rejected - reason: margin")
    ex.sync_open_orders()
    [failed] = [kw for topic, kw in events if topic == "order.failed"]
    assert failed["trade_id"] == "t1" and failed["reason"] == "Order rejected - reason: margin"

    em.run_once()
    assert len(broker.orders) == 1                      # waits before the next try
    now[0] += ExitManager.RETRY_DELAYS_S[0]
    em.run_once()
    assert [(o.side, o.quantity) for o in broker.orders] == [(Side.SHORT, 10)] * 2
    assert repo.get_trade("t1")["status"] == "OPEN"


def test_exits_never_add_up_to_more_shares_than_are_held():
    broker = _Broker({"AAA": 12})
    ex = _executor(broker, _Repo([_trade(id="t1"), _trade(id="t2"), _trade(id="t3")]))   # 10 shares each
    assert ex.close_trade("t1")["ok"]
    again = ex.close_trade("t1")
    assert not again["ok"] and "already working" in again["reason"]
    assert ex.close_trade("t2")["ok"]
    third = ex.close_trade("t3")
    assert not third["ok"] and "cover all 12" in third["reason"]
    assert [o.quantity for o in broker.orders] == [10, 2]


def test_an_order_the_broker_stops_knowing_is_given_up_after_a_few_polls():
    broker = _Broker({"AAA": 10})
    ex = _executor(broker, _Repo([_trade()]))
    ex.close_trade("t1")
    broker.reports["1"] = OrderResult(order_id="1", status="UNKNOWN", symbol="?", submitted_qty=0)
    for _ in range(Executor.LOST_AFTER_POLLS - 1):
        ex.sync_open_orders()
    assert ex.pending_exit_trade_ids() == {"t1"}
    ex.sync_open_orders()
    assert ex.pending_exit_trade_ids() == set()


def test_an_order_ibkr_leaves_inactive_is_cancelled_so_it_stays_dead():
    broker = _Broker({"AAA": 10})
    ex = _executor(broker, _Repo([_trade()]))
    ex.close_trade("t1")
    broker.reports["1"] = OrderResult(order_id="1", status="REJECTED", symbol="AAA", submitted_qty=10)
    ex.sync_open_orders()
    assert broker.cancelled == ["1"] and ex.pending_exit_trade_ids() == set()


# ---------------------------------------------------------------- entries
def test_an_entry_cancelled_after_a_partial_fill_books_the_shares_bought():
    broker, repo = _Broker(), _Repo([])
    ex = _executor(broker, repo)
    play = Play(symbol="AAA", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.INTRADAY, entry=100.0, stop=98.0, targets=[104.0])
    play.suggested_qty = 10
    assert ex.execute_play(play, Account(account_id="DU"), plan=PLAN, context={"schema": 1})["status"] == "SUBMITTED"
    assert [(w["symbol"], w["risk"]) for w in ex.working_entries()] == [("AAA", 20.0)]

    broker.reports["1"] = OrderResult(order_id="1", status="CANCELED", symbol="AAA", submitted_qty=10,
                                      filled_qty=4, avg_fill_price=100.02)
    ex.sync_open_orders()
    assert ex.working_entries() == []
    assert [(t["symbol"], t["quantity"], t["entry_price"]) for t in repo.open_trades()] == [("AAA", 4.0, 100.02)]
    booked = repo.open_trades()[0]
    assert booked["entry_context"] == {"schema": 1} and booked["submitted_at"]      # the context waited for the fill


def test_a_day_trade_entry_not_filled_in_time_is_cancelled_rather_than_left_to_chase():
    import datetime as dt

    from tos_bot.core.enums import PlayStatus

    broker, repo, heard = _Broker(), _Repo([]), []
    ex = _executor(broker, repo, bus=SimpleNamespace(publish=lambda topic, **p: heard.append((topic, p))))
    day = Play(symbol="AAA", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
               timeframe=Timeframe.INTRADAY, entry=100.0, stop=98.0, targets=[104.0])
    swing = Play(symbol="BBB", side=Side.LONG, strategy="rsi2_mean_reversion", kind=StrategyKind.TECHNICAL,
                 timeframe=Timeframe.SWING, entry=50.0, stop=48.0, targets=[56.0])
    day.suggested_qty = swing.suggested_qty = 10
    assert ex.execute_play(day, Account(account_id="DU"), plan=PLAN)["status"] == "SUBMITTED"
    assert ex.execute_play(swing, Account(account_id="DU"), plan=PLAN)["status"] == "SUBMITTED"
    soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=9)
    assert ex.expire_entries(now=soon) == [] and broker.cancelled == []             # nine minutes: still fine
    late = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=11)
    assert ex.expire_entries(now=late) == ["1"] and broker.cancelled == ["1"]       # the day trade's order only...
    assert ex.expire_entries(now=late) == []                                        # ...and only once
    broker.reports["1"] = OrderResult(order_id="1", status="CANCELED", symbol="AAA", submitted_qty=10)
    ex.sync_open_orders()
    assert [w["symbol"] for w in ex.working_entries()] == ["BBB"] and day.status is PlayStatus.CANCELED
    failed = [p for topic, p in heard if topic == "order.failed"]
    assert failed and "not filled within 10 minutes" in failed[0]["reason"]


# ---------------------------------------------------------------- after a restart
def test_after_a_restart_the_exit_already_working_is_followed_not_sent_again():
    broker = _Broker({"AAA": 10}, working=[_working("7")])
    repo = _Repo([_trade()])
    ex = _executor(broker, repo)
    em = ExitManager(repo, ex, quote_fn=lambda s: Quote(symbol=s, bid=90, ask=90, last=90), cfg=CFG,
                     bus=SILENT, venue=VENUE)

    assert [a["order_id"] for a in ex.adopt_working_orders()] == ["7"]
    em.run_once()                                       # under the stop, but the exit is already out
    assert broker.orders == [] and ex.pending_exit_trade_ids() == {"t1"}

    broker.reports["7"] = OrderResult(order_id="7", status="FILLED", symbol="AAA", submitted_qty=10,
                                      filled_qty=10, avg_fill_price=89.9)
    ex.sync_open_orders()
    assert repo.get_trade("t1")["status"] == "CLOSED"


def test_duplicate_exits_the_app_left_working_are_cancelled():
    broker = _Broker({"AAA": 10}, working=[_working("7", mine=True), _working("15", mine=True)])
    ex = _executor(broker, _Repo([_trade()]))
    ex.adopt_working_orders()
    assert ex.pending_exit_trade_ids() == {"t1"} and broker.cancelled == ["15"]


def test_a_closing_order_placed_by_hand_is_followed_but_never_cancelled():
    broker = _Broker({"AAA": 10}, working=[_working("7"), _working("99", mine=False)])
    ex = _executor(broker, _Repo([_trade()]))
    ex.adopt_working_orders()
    assert broker.cancelled == []


def test_an_exit_click_follows_an_exit_already_at_the_broker():
    broker = _Broker({"AAA": 10}, working=[_working("7", tag="exit:t1")])
    ex = _executor(broker, _Repo([_trade()]))
    out = ex.close_trade("t1")
    assert out["ok"] and out["adopted"] and broker.orders == []


def test_a_brackets_target_order_is_never_taken_for_the_exit():
    broker = _Broker({"AAA": 10}, working=[_working("8", tag="play_abc:TP")])
    ex = _executor(broker, _Repo([_trade()]))
    assert ex.close_trade("t1")["ok"]
    assert [o.quantity for o in broker.orders] == [10]                  # a real exit went out


def test_an_entry_left_working_is_followed_and_booked_when_it_fills():
    broker, repo = _Broker(working=[_working("21", side=Side.LONG, tag="play_left")]), _Repo([])
    ex = _executor(broker, repo)
    assert [a["kind"] for a in ex.adopt_working_orders()] == ["entry"]
    assert [w["play_id"] for w in ex.working_entries()] == ["play_left"]

    broker.reports["21"] = OrderResult(order_id="21", status="FILLED", symbol="AAA", submitted_qty=10,
                                       filled_qty=10, avg_fill_price=100.05)
    ex.sync_open_orders()
    assert [(t["symbol"], t["quantity"]) for t in repo.open_trades()] == [("AAA", 10.0)]


# ---------------------------------------------------------------- the dashboard's list of working orders
def test_each_working_order_says_what_it_is_for():
    broker = _Broker({"AAA": 10}, working=[
        _working("7", tag="exit:t1"),
        _working("8", side=Side.LONG, tag="play_left"),
        _working("9", tag="play_left:tp", parent_id="8"),
        _working("10", side=Side.LONG, mine=False),
    ])
    ex = _executor(broker, _Repo([_trade()]))
    ex.adopt_working_orders()

    orders = {o["order_id"]: o for o in ex.active_orders()}
    assert (orders["7"]["purpose"], orders["7"]["trade_id"], orders["7"]["action"]) == ("exit", "t1", "SELL")
    assert (orders["8"]["purpose"], orders["8"]["play_id"], orders["8"]["action"]) == ("entry", "play_left", "BUY")
    assert (orders["9"]["purpose"], orders["9"]["play_id"]) == ("target", "play_left")
    assert orders["10"]["purpose"] == "outside"
    assert broker.cancelled == []


def test_a_broker_that_cant_be_asked_never_reads_as_having_no_orders():
    broker = _Broker()

    def unreachable(status=None):
        raise ConnectionError("IB Gateway went away")

    broker.list_orders = unreachable
    with pytest.raises(ConnectionError):
        _executor(broker, _Repo([])).active_orders()


def test_a_partial_exit_reduces_the_record_when_it_fills():
    repo = _Repo([_trade(quantity=10, initial_quantity=10, target2_price=120.0)])
    broker = _Broker(positions={"AAA": 10})
    ex = _executor(broker, repo)
    r = ex.close_trade("t1", reason="target-1", qty=4, after_fill={"stop_price": 100.05, "target_price": 120.0})
    assert r["ok"] and r["status"] != "FILLED" and "t1" in ex.pending_exit_trade_ids()
    [oid] = list(ex._pending)
    assert broker.orders[-1].quantity == 4 and ex._pending[oid].partial
    broker.reports[oid] = OrderResult(order_id=oid, status="FILLED", symbol="AAA", submitted_qty=4, filled_qty=4,
                                      avg_fill_price=110.0)
    ex.sync_open_orders()
    t = repo.get_trade("t1")
    assert t["status"] == "OPEN" and t["quantity"] == 6 and t["banked_pl"] == 40.0
    assert (t["stop_price"], t["target_price"]) == (100.05, 120.0) and not ex.pending_exit_trade_ids()
