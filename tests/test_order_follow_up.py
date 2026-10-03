"""After an order is sent: fills, rejections, cancellations, and orders the broker loses track of."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from autotradebot.brokers.base import BrokerError, OrderOutcomeUnknown
from autotradebot.config import get_settings
from autotradebot.core.enums import PlayStatus, Side, StrategyKind, Timeframe
from autotradebot.core.models import Account, Fill, OrderResult, Play, Position, Quote
from autotradebot.execution import exit_manager as exit_manager_module
from autotradebot.execution.executor import Executor
from autotradebot.execution.exit_manager import ExitManager

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
        self.settled = []                        # (play id, status, outcome) - what became of each sent play
        self.paid = []                           # (trade id, ENTRY | EXIT, broker order id, commission) per booking

    def settle_play(self, play_id, status, outcome=None):
        self.settled.append((play_id, status, outcome))
        return True

    def open_trades(self):
        return [dict(x) for x in self.t.values() if x["status"] == "OPEN"]

    def get_trade(self, tid):
        return dict(self.t[tid]) if tid in self.t else None

    def update_trade_risk(self, tid, **kw):
        self.t[tid].update({k: v for k, v in kw.items() if v is not None})

    def open_trade(self, play, price, qty, venue, order_id, commission=0.0, order_type="LIMIT",
                   order_session="REGULAR", entry_context=None, submitted_at=None, decision=None):
        tid = f"t{len(self.t) + 1}"
        self.t[tid] = _trade(id=tid, symbol=play.symbol, entry_price=price, quantity=qty, broker=venue)
        self.t[tid]["entry_context"], self.t[tid]["submitted_at"] = entry_context, submitted_at
        self.t[tid]["decision"] = decision
        self.paid.append((tid, "ENTRY", order_id, commission))
        return tid

    def close_trade(self, tid, exit_price, exit_reason="", decision_price=None, submitted_at=None, commission=0.0,
                    broker_order_id=""):
        self.t[tid].update(status="CLOSED", exit_price=exit_price, exit_reason=exit_reason,
                           exit_decision_price=decision_price, exit_submitted_at=submitted_at)
        self.paid.append((tid, "EXIT", broker_order_id, commission))
        return dict(self.t[tid])

    def reduce_trade(self, tid, exit_qty, exit_price, exit_reason="", commission=0.0, stop_price=None,
                     target_price=None, broker_order_id=""):
        self.paid.append((tid, "EXIT", broker_order_id, commission))
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


def _unanswered(status=None):
    raise BrokerError("the open orders didn't arrive in time")


def test_an_exit_still_goes_out_when_the_order_list_is_unknown_capped_by_the_shares_held(caplog):
    broker = _Broker({"AAA": 6})                         # the record says 10
    ex = _executor(broker, _Repo([_trade()]))
    broker.list_orders = _unanswered
    with caplog.at_level(logging.WARNING, logger="autotradebot.execution.executor"):
        assert ex.close_trade("t1")["ok"]
    assert [o.quantity for o in broker.orders] == [6]
    assert "order list unavailable" in caplog.text        # said to be unknown, not taken for "none working"


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

    from autotradebot.core.enums import PlayStatus

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


def test_orders_that_couldnt_be_listed_after_a_restart_are_taken_over_by_a_later_sync():
    broker = _Broker({"AAA": 10}, working=[_working("7"), _working("21", side=Side.LONG, tag="play_left")])
    ex = _executor(broker, _Repo([_trade()]))
    told = []
    ex.on_entries_adopted = told.append
    listed, broker.list_orders = broker.list_orders, _unanswered

    assert ex.adopt_working_orders() == []
    ex.sync_open_orders()                               # still no answer
    assert ex.pending_exit_trade_ids() == set() and ex.working_entries() == [] and told == []

    broker.list_orders = listed
    ex.sync_open_orders()
    assert ex.pending_exit_trade_ids() == {"t1"} and [w["play_id"] for w in ex.working_entries()] == ["play_left"]
    assert told == [["play_left"]]                      # Autopilot hears of the entry it sent
    ex.sync_open_orders()
    assert told == [["play_left"]] and broker.cancelled == []                   # taken over once


def test_an_entry_being_placed_is_never_taken_over_as_an_earlier_runs_beside_it():
    import threading
    import time

    broker, repo, told, heard = _Broker(), _Repo([]), [], []
    ex = _executor(broker, repo, bus=SimpleNamespace(publish=lambda topic, **p: heard.append(topic)))
    ex.on_entries_adopted = told.append
    at_broker = threading.Event()

    def place_order(req):                                # the broker has the order; its answer is on the way
        broker.orders.append(req)
        res = OrderResult(order_id="5", status="SUBMITTED", symbol=req.symbol, submitted_qty=req.quantity,
                          side=req.side, tag=req.client_tag)
        broker.working.append(res)
        at_broker.set()
        time.sleep(0.3)
        return res

    broker.place_order = place_order
    play = Play(symbol="AAA", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.INTRADAY, entry=100.0, stop=98.0, targets=[104.0], id="play_left")
    play.suggested_qty = 10
    sent = []
    placing = threading.Thread(target=lambda: sent.append(ex.execute_play(play, Account(account_id="DU"), plan=PLAN)))
    placing.start()
    assert at_broker.wait(2)
    taken = ex.adopt_working_orders()                    # an order sync's take-over, meanwhile
    placing.join(5)
    assert sent[0]["ok"] and sent[0]["order_id"] == "5"
    assert taken == [] and told == [] and "orders.adopted" not in heard        # followed once, as the entry it is
    assert [(w["order_id"], w["play_id"]) for w in ex.working_entries()] == [("5", "play_left")]


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


def test_how_long_each_order_took_to_fill_is_kept(repo):
    """Entry and exit both: from the order going out to the fill coming back. A stop or target
    resting at the broker has none - it waits for the price, not for the broker."""
    import datetime as dt

    from autotradebot.core.enums import Side, StrategyKind, Timeframe
    from autotradebot.core.models import Play

    play = Play(symbol="LAT", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.INTRADAY, entry=100.0, stop=99.0, targets=[102.0])
    repo.record_play(play)
    sent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=6)
    tid = repo.open_trade(play, 100.0, 10, "ibkr-paper", submitted_at=sent)
    opened = repo.trade_record(tid)["trade"]
    assert 5.5 <= opened["entry_latency_s"] <= 8.0 and opened["submitted_at"]

    repo.close_trade(tid, 101.0, exit_reason="target", submitted_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=2))
    closed = repo.trade_record(tid)["trade"]
    assert 1.5 <= closed["exit_latency_s"] <= 4.0 and closed["exit_submitted_at"]

    play2 = Play(symbol="RST", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                 timeframe=Timeframe.SWING, entry=10.0, stop=9.0, targets=[12.0])
    repo.record_play(play2)
    rid = repo.open_trade(play2, 10.0, 5, "ibkr-paper")                 # no send time known
    repo.close_trade(rid, 12.0, exit_reason="target")                   # a target that rested at the broker
    rested = repo.trade_record(rid)["trade"]
    assert rested["entry_latency_s"] is None and rested["exit_latency_s"] is None

    play3 = Play(symbol="SIM", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                 timeframe=Timeframe.INTRADAY, entry=10.0, stop=9.0, targets=[12.0])
    repo.record_play(play3)
    sid = repo.open_trade(play3, 10.0, 5, "paper", submitted_at=sent)         # the in-app simulator
    repo.close_trade(sid, 12.0, exit_reason="target", submitted_at=sent)
    simulated = repo.trade_record(sid)["trade"]
    assert simulated["entry_latency_s"] is None and simulated["exit_latency_s"] is None   # it fills at once


def test_an_exit_the_app_sends_records_how_long_the_broker_took(repo):
    from autotradebot.research.journal import execution_quality

    trades = [{"entry_latency_s": 2.0, "exit_latency_s": 1.0, "entry_slippage_bps": 1.0, "exit_slippage_bps": 1.0},
              {"entry_latency_s": 30.0, "exit_latency_s": 3.0},
              {"entry_latency_s": 4.0, "exit_latency_s": 2.0},
              {"entry_latency_s": 16_500.0}]                                  # a swing limit that rested for its price
    out = execution_quality(trades, assumed_bps=6.0)
    assert out["entry_latency_s"] == 4.0 and out["exit_latency_s"] == 2.0   # the middle one, not the average
    assert out["slowest_entry_s"] == 30.0 and "filled in 4.0s" in out["latency_note"]
    assert out["rested_entries"] == 1 and "1 limit entry rested longer" in out["latency_note"]
    assert execution_quality([{"r": 1.0}], assumed_bps=6.0).get("latency_note") is None


def test_an_entry_taken_back_after_a_restart_has_no_fill_time():
    """Its clock restarted at the restart (for its time-out); that isn't when it went out."""
    from autotradebot.execution.executor import _Pending
    from autotradebot.core.enums import Side, StrategyKind, Timeframe
    from autotradebot.core.models import Play
    import datetime as dt

    seen = []
    ex = SimpleNamespace(_open_trade=lambda *a, **k: seen.append(k.get("submitted_at")))
    from autotradebot.execution.executor import Executor

    play = Play(symbol="ADP", side=Side.LONG, strategy="s", kind=StrategyKind.TECHNICAL, timeframe=Timeframe.SWING,
                entry=10.0, stop=9.0, targets=[12.0])
    now = dt.datetime.now(dt.timezone.utc)
    res = SimpleNamespace(avg_fill_price=10.0, fills=[], filled_qty=5, order_id="o1", symbol="ADP")
    Executor._on_filled(ex, _Pending("o1", play, "entry", qty=5, submitted_at=now, adopted=True), res)
    Executor._on_filled(ex, _Pending("o2", play, "entry", qty=5, submitted_at=now), res)
    assert seen == [None, now]


# ---------------------------------------------------------------- an entry that bought nothing, and one that stalled
def _entry(ex, symbol="AAA", timeframe=Timeframe.INTRADAY, **kw):
    play = Play(symbol=symbol, side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=timeframe, entry=100.0, stop=98.0, targets=[104.0], **kw)
    play.suggested_qty = 10
    assert ex.execute_play(play, Account(account_id="DU"), plan=PLAN)["status"] == "SUBMITTED"
    return play


def test_an_entry_that_bought_nothing_is_told_once_and_the_play_log_says_why():
    broker, repo, heard, handed = _Broker(), _Repo([]), [], []
    ex = _executor(broker, repo, bus=SimpleNamespace(publish=lambda topic, **p: heard.append((topic, p))))
    ex.on_entry_unfilled = handed.append
    play = _entry(ex)
    assert repo.settled == [(play.id, "SUBMITTED", None)]                  # saved before the sync can hear the end
    broker.reports["1"] = OrderResult(order_id="1", status="CANCELED", symbol="AAA", submitted_qty=10,
                                      message="not filled within 10 minutes")
    report = broker.get_order("1")
    class _HeardTwice:
        """The report the Refresh button's pass reads at the same moment as the sync loop's: the other pass
        runs between this one's look at the order and its claim on it."""

        def __init__(self, r):
            self._r, self._inside = r, False

        def __getattr__(self, name):
            return getattr(self._r, name)

        @property
        def status(self):
            if not self._inside:
                self._inside = True
                ex._on_order_update(self)
            return self._r.status

    ex._on_order_update(_HeardTwice(report))
    ex._on_order_update(report)
    ex.sync_open_orders()
    assert handed == [play.id]                                             # once
    failed = [p for topic, p in heard if topic == "order.failed"]
    assert len(failed) == 1 and failed[0]["play_id"] == play.id
    (pid, status, outcome), = repo.settled[1:]
    assert (pid, status, outcome["status"]) == (play.id, "CANCELED", "CANCELED") and "10 minutes" in outcome["reason"]

    refused = _entry(ex, "BBB")                                             # refused by the broker: also nothing bought
    broker.reports["2"] = OrderResult(order_id="2", status="REJECTED", symbol="BBB", submitted_qty=10)
    ex.sync_open_orders()
    assert handed == [play.id, refused.id] and repo.settled[-1][1] == "ERROR"


def test_an_entry_that_may_have_bought_something_keeps_its_slot():
    broker, repo, handed = _Broker(), _Repo([]), []
    ex = _executor(broker, repo)
    ex.on_entry_unfilled = handed.append
    _entry(ex)
    broker.reports["1"] = OrderResult(order_id="1", status="CANCELED", symbol="AAA", submitted_qty=10,
                                      filled_qty=4, avg_fill_price=100.0)
    ex.sync_open_orders()                                                  # part of it filled: a trade, not a miss
    assert handed == [] and [t["quantity"] for t in repo.open_trades()] == [4.0]

    from autotradebot.core.models import Fill

    _entry(ex, "DDD")                                                      # the broker's count says 0, a fill says not
    broker.reports["2"] = OrderResult(order_id="2", status="CANCELED", symbol="DDD", submitted_qty=10,
                                      fills=[Fill(order_id="2", symbol="DDD", side=Side.LONG, quantity=3, price=100.0)])
    ex.sync_open_orders()
    assert handed == []
    broker.reports.clear()

    _entry(ex, "BBB")                                                      # lost: it may have filled unseen
    broker.reports["3"] = OrderResult(order_id="3", status="UNKNOWN", symbol="BBB", submitted_qty=10)
    for _ in range(ex.LOST_AFTER_POLLS):
        ex.sync_open_orders()
    assert ex.working_entries() == [] and handed == [] and repo.settled[-1][1] == "ERROR"


def test_a_part_filled_entry_that_stalls_has_the_rest_cancelled_so_its_shares_are_booked():
    import time

    broker, repo = _Broker(), _Repo([])
    ex = _executor(broker, repo)
    swing = _entry(ex, timeframe=Timeframe.SWING)                           # a swing entry too: no time-out of its own
    leg = _entry(ex, "BBB", tags=["pair-leg"])                             # the pairs desk works its own legs
    # a leg's stop is a placeholder, so its risk is left out of the open risk (TradingEngine.open_risk_usd)
    assert [(w["symbol"], w["pair_leg"]) for w in ex.working_entries()] == [("AAA", False), ("BBB", True)]
    for oid, sym in (("1", "AAA"), ("2", "BBB")):
        broker.reports[oid] = OrderResult(order_id=oid, status="WORKING", symbol=sym, submitted_qty=10,
                                          filled_qty=4, avg_fill_price=100.01)
    ex.sync_open_orders()
    first = ex._pending["1"].first_fill_at
    assert first is not None and repo.open_trades() == [] and broker.cancelled == []
    wait = ex.cfg.partial_entry_wait_s
    assert ex.expire_entries(mono=first + wait - 1) == []                  # still inside the wait
    assert ex.expire_entries(mono=first + wait) == ["1"] and broker.cancelled == ["1"]
    assert ex.expire_entries(mono=first + wait + 1) == [] and broker.cancelled == ["1"]    # asked once...
    ex.expire_entries(mono=first + wait + ex.CANCEL_AGAIN_S)
    assert broker.cancelled == ["1", "1"]                                  # ...and again when it hasn't taken
    assert "filled in part" in ex._pending["1"].expired

    broker.reports["1"] = OrderResult(order_id="1", status="CANCELED", symbol="AAA", submitted_qty=10,
                                      filled_qty=6, avg_fill_price=100.02)
    ex.sync_open_orders()
    assert [(t["symbol"], t["quantity"]) for t in repo.open_trades()] == [("AAA", 6.0)]  # what was bought by the end
    assert swing.status.value == "FILLED" and [w["symbol"] for w in ex.working_entries()] == ["BBB"]
    assert leg.status.value == "SUBMITTED"

    ex.cfg = ex.cfg.model_copy(update={"partial_entry_wait_s": 0})         # switched off: wait for the order
    _entry(ex, "CCC")
    broker.reports["3"] = OrderResult(order_id="3", status="WORKING", symbol="CCC", submitted_qty=10, filled_qty=4)
    ex.sync_open_orders()
    assert ex.expire_entries(mono=time.monotonic() + 3600) == []


def test_a_part_filled_entry_the_broker_loses_track_of_books_what_it_was_seen_to_buy():
    broker, repo = _Broker(), _Repo([])
    ex = _executor(broker, repo)
    _entry(ex)
    broker.reports["1"] = OrderResult(order_id="1", status="WORKING", symbol="AAA", submitted_qty=10,
                                      filled_qty=4, avg_fill_price=100.03)
    ex.sync_open_orders()
    broker.reports["1"] = OrderResult(order_id="1", status="UNKNOWN", symbol="AAA", submitted_qty=10)
    for _ in range(ex.LOST_AFTER_POLLS):                                   # e.g. a Gateway restart mid-cancel
        ex.sync_open_orders()
    assert [(t["quantity"], t["entry_price"]) for t in repo.open_trades()] == [(4.0, 100.03)]   # so they get a stop


def test_an_entry_the_broker_loses_track_of_books_what_its_executions_show_it_bought():
    from autotradebot.core.models import Fill

    broker, repo, heard = _Broker(), _Repo([]), []
    ex = _executor(broker, repo, bus=SimpleNamespace(publish=lambda topic, **p: heard.append((topic, p))))
    play = _entry(ex)
    broker.reports["1"] = OrderResult(order_id="1", status="UNKNOWN", symbol="?", submitted_qty=0)
    broker.get_fills = lambda symbol=None: [                           # known by the play's tag on IBKR's executions
        Fill(order_id="7", symbol="AAA", side=Side.LONG, quantity=4, price=100.02, tag=play.id),
        Fill(order_id="8", symbol="AAA", side=Side.SHORT, quantity=4, price=101.0, tag="exit:t9")]
    for _ in range(ex.LOST_AFTER_POLLS):
        ex.sync_open_orders()
    assert [(t["quantity"], t["entry_price"]) for t in repo.open_trades()] == [(4.0, 100.02)]
    [failed] = [p for topic, p in heard if topic == "order.failed"]
    assert failed["filled_qty"] == 4.0 and "after 4 of 10 shares filled" in failed["msg"]   # it ended with part bought


def test_an_entry_the_broker_loses_track_of_is_not_given_up_while_its_executions_cant_be_read():
    from autotradebot.core.models import Fill

    broker, repo, heard = _Broker(), _Repo([]), []
    ex = _executor(broker, repo, bus=SimpleNamespace(publish=lambda topic, **p: heard.append((topic, p))))
    play = _entry(ex)
    broker.reports["1"] = OrderResult(order_id="1", status="UNKNOWN", symbol="?", submitted_qty=0)
    readable = []

    def get_fills(symbol=None, strict=False):                          # IBKR's, asked strictly
        if not readable:
            raise BrokerError("IBKR's executions for AAA couldn't be read: no answer in time")
        return [Fill(order_id="1", symbol="AAA", side=Side.LONG, quantity=10, price=100.01, tag=play.id)]

    broker.get_fills = get_fills
    for _ in range(2 * ex.LOST_AFTER_POLLS):
        ex.sync_open_orders()
    assert repo.open_trades() == [] and "order.failed" not in [topic for topic, _ in heard]
    assert [e["play_id"] for e in ex.working_entries()] == [play.id]     # not known is no "none bought": followed on
    readable.append(True)
    for _ in range(ex.LOST_AFTER_POLLS):
        ex.sync_open_orders()
    assert [(t["quantity"], t["entry_price"]) for t in repo.open_trades()] == [(10.0, 100.01)]
    assert ex.working_entries() == []


def test_an_entry_reported_filled_without_a_price_is_booked_at_its_executions_price_never_at_zero():
    from autotradebot.core.models import Fill

    broker, repo = _Broker(), _Repo([])
    ex = _executor(broker, repo)
    first, _ = _entry(ex), _entry(ex, "BBB")
    for oid, symbol in (("1", "AAA"), ("2", "BBB")):                   # rebuilt after a reconnect, no price on it
        broker.reports[oid] = OrderResult(order_id=oid, status="FILLED", symbol=symbol, submitted_qty=10, filled_qty=10)
    executions = [Fill(order_id="1", symbol="AAA", side=Side.LONG, quantity=10, price=99.98, tag=first.id)]
    broker.get_fills = lambda symbol=None: [f for f in executions if symbol in (None, f.symbol)]
    ex.sync_open_orders()
    assert sorted((t["symbol"], t["entry_price"]) for t in repo.open_trades()) == [("AAA", 99.98), ("BBB", 100.0)]


# ---------------------------------------------------------------- the dashboard's countdowns on a working entry
def _at_broker(order_id, play, filled=0.0):
    """The entry the app sent, as the broker lists it while it works."""
    return OrderResult(order_id=order_id, status="WORKING", symbol=play.symbol, submitted_qty=10,
                       filled_qty=filled, side=play.side, tag=play.id)


def test_a_working_day_trade_entry_says_when_its_time_out_comes():
    import datetime as dt

    broker, repo = _Broker(), _Repo([])
    ex = _executor(broker, repo)
    day, swing = _entry(ex), _entry(ex, "BBB", timeframe=Timeframe.SWING)
    broker.working += [_at_broker("1", day), _at_broker("2", swing)]
    orders = {o["order_id"]: o for o in ex.active_orders()}
    sent, due = (dt.datetime.fromisoformat(orders["1"][k]) for k in ("submitted_at", "expires_at"))
    assert due == sent + dt.timedelta(minutes=ex.cfg.entry_timeout_min)
    assert (orders["1"]["cut_at"], orders["1"]["calling_off"]) == (None, None)
    assert orders["2"]["submitted_at"] and orders["2"]["expires_at"] is None      # a swing entry keeps its DAY life

    # the time counted down to is the one the order is called off at
    assert ex.expire_entries(now=due - dt.timedelta(seconds=1)) == []
    assert ex.expire_entries(now=due + dt.timedelta(milliseconds=1)) == ["1"]
    listed = broker.working[0]                                              # still listed until the cancel takes
    assert "not filled within" in ex._describe(listed, {})["calling_off"]
    assert ex._describe(broker.working[1], {})["calling_off"] is None


def test_a_part_filled_entry_says_when_the_rest_is_cut():
    import datetime as dt
    import time

    broker, repo = _Broker(), _Repo([])
    ex = _executor(broker, repo)
    day, leg = _entry(ex), _entry(ex, "BBB", tags=["pair-leg"])
    for oid, play in (("1", day), ("2", leg)):
        broker.reports[oid] = OrderResult(order_id=oid, status="WORKING", symbol=play.symbol, submitted_qty=10,
                                          filled_qty=4)
        broker.working.append(_at_broker(oid, play, filled=4))
    ex.sync_open_orders()
    ex._pending["1"].first_fill_at = time.monotonic() - 10                 # its first shares were bought 10 s ago
    wait = ex.cfg.partial_entry_wait_s
    now = dt.datetime.now(dt.timezone.utc)
    orders = {o["order_id"]: o for o in ex.active_orders()}
    assert (orders["1"]["filled"], orders["1"]["qty"]) == (4.0, 10.0)
    cut = dt.datetime.fromisoformat(orders["1"]["cut_at"])
    assert abs((cut - now).total_seconds() - (wait - 10)) < 1               # from the first fill, not from now
    assert orders["2"]["cut_at"] is None                                    # the pairs desk works its own legs

    ex.expire_entries(mono=ex._pending["1"].first_fill_at + wait)
    assert "filled in part" in ex._describe(broker.working[0], {})["calling_off"]   # listed until the cancel takes


def test_an_entry_taken_over_after_a_restart_times_out_from_then():
    import datetime as dt

    broker = _Broker(working=[_working("21", side=Side.LONG, tag="play_left")])
    ex = _executor(broker, _Repo([]))
    before = dt.datetime.now(dt.timezone.utc)
    ex.adopt_working_orders()
    after = dt.datetime.now(dt.timezone.utc)
    (order,) = ex.active_orders()
    limit = dt.timedelta(minutes=ex.cfg.entry_timeout_min)
    assert order["submitted_at"] is None                                    # when it really went out isn't known
    due = dt.datetime.fromisoformat(order["expires_at"])
    assert before + limit - dt.timedelta(milliseconds=1) <= due <= after + limit


# ---------------------------------------------------------------- an order the broker didn't answer in time
class _Unanswered(_Broker):
    """An account whose answer to the next order doesn't come in time (OrderOutcomeUnknown) - though the order may
    reach it all the same: ``lands`` says what became of it there, "working", "filled" or "" (it never arrived)."""

    def __init__(self, positions=None, lands="working", raw_timeout=False):
        super().__init__(positions)
        self.lands, self.raw_timeout, self.unanswered, self.fills = lands, raw_timeout, 1, []

    def place_order(self, req):
        res = super().place_order(req)
        if not self.unanswered:
            return res
        self.unanswered -= 1
        if self.lands == "working":
            self.working.append(OrderResult(order_id=res.order_id, status="SUBMITTED", symbol=req.symbol,
                                            submitted_qty=req.quantity, side=req.side, tag=req.client_tag))
        elif self.lands == "filled":
            self.fills.append(Fill(order_id=res.order_id, symbol=req.symbol, side=req.side, quantity=req.quantity,
                                   price=100.5, tag=req.client_tag))
        if self.raw_timeout:                             # an adapter that lets the bare timeout through
            raise TimeoutError()
        raise OrderOutcomeUnknown("IBKR didn't answer the order within 10 s", order_ref=req.client_tag)

    def get_fills(self, symbol=None, strict=False):
        return [f for f in self.fills if symbol is None or f.symbol == symbol]


def _autopilot_on(ex):
    """Autopilot whose approvals go through ``ex`` - as engine.approve_play's do."""
    from test_autopilot import FakeEngine, _cfg

    from autotradebot.execution.autopilot import AutoPilot

    class _Engine(FakeEngine):
        def approve_play(self, pid, operator="operator"):
            self.approved.append((pid, operator))
            return ex.execute_play(self._plays[pid], Account(account_id="DU"), plan=PLAN)

        def working_entries(self):
            return ex.working_entries()

    ap = AutoPilot(_Engine(), _cfg(max_auto_trades_per_day=2, max_auto_positions=9), bus=SILENT)
    ex.on_entry_unfilled = ap.entry_unfilled
    return ap


def _day_play(symbol="AAA"):
    play = Play(symbol=symbol, side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.INTRADAY, entry=100.0, stop=98.0, targets=[104.0])
    play.suggested_qty = 10
    return play


def test_an_entry_the_broker_didnt_answer_in_time_is_found_working_and_followed_never_sent_twice():
    from test_autopilot import _run, mkplay

    broker, repo = _Unanswered(lands="working"), _Repo([])
    ex = _executor(broker, repo)
    ap = _autopilot_on(ex)
    play = mkplay(sym="AAA")
    play.suggested_qty = 10
    _run(ap, play)
    assert len(broker.orders) == 1 and play.status is PlayStatus.SUBMITTED
    assert repo.settled == [(play.id, "SUBMITTED", None)]                  # the play log says it went out
    # Autopilot counts it like one sent: its slot kept, the order counted, the play its own
    assert ap.status()["auto_trades_today"] == 1 and ap._sent_today == 1 and play.id in ap._auto_play_ids
    assert [(w["play_id"], w["order_id"]) for w in ex.working_entries()] == [(play.id, "")]   # caps count it
    again = mkplay(sym="AAA")                                              # a second setup in the same stock meanwhile
    again.suggested_qty = 10
    _run(ap, again)
    assert len(broker.orders) == 1

    ex.sync_open_orders()                                                  # found at the broker by its tag...
    assert len(broker.orders) == 1 and ex._unknown == {}
    assert [(w["play_id"], w["order_id"]) for w in ex.working_entries()] == [(play.id, "1")]   # ...and followed
    assert ap.status()["auto_trades_today"] == 1                           # the slot is still taken
    broker.reports["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                      avg_fill_price=100.02)
    ex.sync_open_orders()
    assert [(t["symbol"], t["quantity"], t["entry_price"]) for t in repo.open_trades()] == [("AAA", 10, 100.02)]
    assert len(broker.orders) == 1 and ap.status()["auto_trades_today"] == 1


def test_an_entry_the_broker_didnt_answer_that_filled_at_once_is_booked_from_its_executions():
    broker, repo, heard = _Unanswered(lands="filled", raw_timeout=True), _Repo([]), []
    ex = _executor(broker, repo, bus=SimpleNamespace(publish=lambda topic, **p: heard.append((topic, p))))
    out = ex.execute_play(_day_play(), Account(account_id="DU"), plan=PLAN, context={"schema": 1})
    assert not out["ok"] and out["sent_unknown"] and "may be working" in out["reason"]   # a bare timeout too
    ex.sync_open_orders()
    [t] = repo.open_trades()
    assert (t["quantity"], t["entry_price"], t["entry_context"]) == (10, 100.5, {"schema": 1})
    assert ex.working_entries() == [] and ex._unknown == {} and len(broker.orders) == 1
    assert [p["adopted"][0]["order_id"] for topic, p in heard if topic == "orders.adopted"] == ["1"]


def test_an_entry_the_broker_shows_neither_working_nor_filled_is_taken_as_not_sent_once_looked_for(monkeypatch):
    from autotradebot.execution import executor as executor_module

    now = [1000.0]
    monkeypatch.setattr(executor_module.time, "monotonic", lambda: now[0])
    broker, repo, heard, handed = _Unanswered(lands=""), _Repo([]), [], []
    ex = _executor(broker, repo, bus=SimpleNamespace(publish=lambda topic, **p: heard.append((topic, p))))
    ex.on_entry_unfilled = handed.append
    play = _day_play()
    assert ex.execute_play(play, Account(account_id="DU"), plan=PLAN)["sent_unknown"]
    ex.sync_open_orders()
    now[0] += ex.UNKNOWN_GIVE_UP_S - 1
    ex.sync_open_orders()                                                  # not there yet: still looked for
    assert handed == [] and [w["play_id"] for w in ex.working_entries()] == [play.id]
    now[0] += 2
    ex.sync_open_orders()
    assert handed == [play.id] and ex.working_entries() == [] and play.status is PlayStatus.ERROR
    assert repo.settled[-1][1] == "ERROR" and repo.settled[-1][2]["status"] == "NOT_SENT"
    [failed] = [p for topic, p in heard if topic == "order.failed"]
    assert failed["play_id"] == play.id and "taken as not sent" in failed["msg"]


def test_an_entry_called_off_before_it_is_found_is_cancelled_as_it_is_found():
    broker, repo = _Unanswered(lands="working"), _Repo([])
    ex = _executor(broker, repo)
    assert ex.execute_play(_day_play(), Account(account_id="DU"), plan=PLAN)["sent_unknown"]
    assert ex.cancel_pending_entries() == 1                                # a quit
    ex.sync_open_orders()
    assert broker.cancelled == ["1"] and [w["order_id"] for w in ex.working_entries()] == ["1"]   # followed to its end


def test_an_unanswered_entry_still_not_found_while_the_broker_is_disconnected_is_said(monkeypatch):
    from autotradebot.execution import executor as executor_module

    now = [1000.0]
    monkeypatch.setattr(executor_module.time, "monotonic", lambda: now[0])
    broker, repo, heard = _Unanswered(lands="filled"), _Repo([]), []
    ex = _executor(broker, repo, bus=SimpleNamespace(publish=lambda topic, **p: heard.append((topic, p))))
    play = _day_play()
    assert ex.execute_play(play, Account(account_id="DU"), plan=PLAN)["sent_unknown"]   # (the approval says so)
    broker.is_connected = False                                            # nothing can be looked for meanwhile
    for _ in range(int(ex.UNPROTECTED_WARN_S) // 4 + 1):
        assert [topic for topic, _ in heard if topic == "order.unconfirmed"] == []
        now[0] += 4.0
        ex.sync_open_orders()
    [said] = [p for topic, p in heard if topic == "order.unconfirmed"]     # a minute and a half on
    assert (said["kind"], said["play_id"], said["bare"]) == ("entry", play.id, False)
    assert "no trade record and no stop" in said["msg"] and repo.open_trades() == []

    broker.is_connected = True                                             # back: found filled, booked, no more said
    ex.sync_open_orders()
    [t] = repo.open_trades()
    assert t["quantity"] == 10 and ex._unknown == {} and ex._unknown_said == {}


def test_an_exit_the_broker_didnt_answer_in_time_waits_and_is_followed_once_found_never_sent_twice():
    broker, repo = _Unanswered({"AAA": 10}, lands="working"), _Repo([_trade()])
    ex = _executor(broker, repo)
    em = ExitManager(repo, ex, quote_fn=lambda s: Quote(symbol=s, bid=90, ask=90, last=90), cfg=CFG, bus=SILENT,
                     venue=VENUE)
    out = ex.close_trade("t1", reason="stop")
    assert not out["ok"] and out["wait"] and out["sent_unknown"]           # the exit manager waits: no failed try
    again = ex.close_trade("t1", reason="manual")                          # a click meanwhile
    assert again["wait"] and "looked for" in again["reason"]
    em.run_once()                                                          # 90 is under the stop: it leaves it be
    assert len(broker.orders) == 1 and ex.pending_exit_trade_ids() == {"t1"}

    ex.sync_open_orders()                                                  # found at the broker by its tag: followed
    assert ex._unknown == {} and ex.pending_exit_trade_ids() == {"t1"} and len(broker.orders) == 1
    broker.reports["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                      avg_fill_price=89.9)
    ex.sync_open_orders()
    t = repo.get_trade("t1")
    assert (t["status"], t["exit_price"], t["exit_reason"]) == ("CLOSED", 89.9, "stop")


def test_an_exit_the_broker_didnt_answer_that_filled_is_booked_from_its_own_executions_only():
    import datetime as dt

    broker, repo = _Unanswered({"AAA": 10}, lands="filled"), _Repo([_trade()])
    earlier = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
    broker.fills.append(Fill(order_id="0", symbol="AAA", side=Side.SHORT, quantity=5, price=104.0, ts=earlier,
                             tag="exit:t1"))                                # an earlier exit of the trade, booked then
    ex = _executor(broker, repo)
    assert ex.close_trade("t1", reason="stop", decision_price=97.9)["sent_unknown"]
    ex.sync_open_orders()
    t = repo.get_trade("t1")
    assert (t["status"], t["exit_price"], t["exit_reason"], t["exit_decision_price"]) == ("CLOSED", 100.5, "stop", 97.9)
    assert ex.pending_exit_trade_ids() == set() and len(broker.orders) == 1


def test_an_exit_the_broker_shows_neither_working_nor_filled_goes_out_again_once_looked_for(monkeypatch):
    from autotradebot.execution import executor as executor_module

    now = [1000.0]
    monkeypatch.setattr(executor_module.time, "monotonic", lambda: now[0])
    broker, repo = _Unanswered({"AAA": 10}, lands=""), _Repo([_trade()])
    ex = _executor(broker, repo)
    assert ex.close_trade("t1", reason="stop")["sent_unknown"]
    ex.sync_open_orders()
    assert ex.close_trade("t1", reason="stop")["wait"] and len(broker.orders) == 1
    now[0] += ex.UNKNOWN_GIVE_UP_S + 1
    ex.sync_open_orders()
    assert ex.pending_exit_trade_ids() == set()
    assert ex.close_trade("t1", reason="stop")["ok"] and [o.quantity for o in broker.orders] == [10, 10]


# ---------------------------------------------------------------- a fill the database refuses to book
def _refuses_once(repo, method):
    """``repo``'s ``method`` raises the first time it is called - another thread holds the database's write lock -
    and works after. Returns the calls made."""
    real, calls = getattr(repo, method), []

    def once(*a, **k):
        calls.append(a)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return real(*a, **k)

    setattr(repo, method, once)
    return calls


def test_an_exit_fill_whose_booking_fails_is_booked_on_the_next_sync_and_no_second_exit_goes_out(caplog):
    heard = []
    broker, repo = _Broker({"AAA": 10}), _Repo([_trade()])
    ex = _executor(broker, repo, bus=SimpleNamespace(publish=lambda topic, **p: heard.append((topic, p))))
    calls = _refuses_once(repo, "close_trade")
    assert ex.close_trade("t1", reason="stop", decision_price=97.9)["ok"]
    broker.reports["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                      avg_fill_price=97.8)
    with caplog.at_level(logging.ERROR):
        ex.sync_open_orders()
    assert repo.get_trade("t1")["status"] == "OPEN" and ex.pending_exit_trade_ids() == {"t1"}   # still followed
    assert "BOOKING FAILED" in caplog.text and "database is locked" in caplog.text
    [alert] = [p for topic, p in heard if topic == "order.unbooked"]                # the dashboard is told, once
    assert (alert["symbol"], alert["kind"]) == ("AAA", "exit") and "database is locked" in alert["msg"]
    assert not ex.close_trade("t1", reason="manual")["ok"] and len(broker.orders) == 1    # no second exit meanwhile

    ex.sync_open_orders()
    t = repo.get_trade("t1")
    assert (t["status"], t["exit_price"], t["exit_reason"], t["exit_decision_price"]) == ("CLOSED", 97.8, "stop", 97.9)
    assert ex.pending_exit_trade_ids() == set() and len(calls) == 2 and len(broker.orders) == 1
    topics = [topic for topic, _ in heard]
    assert topics.count("order.unbooked") == 1 and topics.count("trade.closed") == 1


def test_an_entry_fill_whose_booking_fails_keeps_counting_as_working_and_is_booked_once_on_the_next_sync():
    broker, repo = _Broker(), _Repo([])
    ex = _executor(broker, repo)
    calls = _refuses_once(repo, "open_trade")
    play = _day_play()
    assert ex.execute_play(play, Account(account_id="DU"), plan=PLAN, context={"schema": 1})["ok"]
    broker.reports["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                      avg_fill_price=100.02)
    ex.sync_open_orders()
    assert repo.open_trades() == [] and [w["order_id"] for w in ex.working_entries()] == ["1"]   # its slot stays taken
    ex.sync_open_orders()
    [t] = repo.open_trades()
    assert (t["quantity"], t["entry_price"], t["entry_context"]) == (10, 100.02, {"schema": 1})
    assert ex.working_entries() == [] and len(calls) == 2 and play.status is PlayStatus.FILLED


def _watch_booking(ex, repo, refuse=0):
    """``repo``'s open_trade notes the working entries ``ex`` lists while each booking is under way - the database
    writing, or waiting on another writer - and raises the first ``refuse`` times. Returns what each try saw."""
    real, seen = repo.open_trade, []

    def booking(*a, **k):
        seen.append([(w["play_id"], w["risk"]) for w in ex.working_entries()])
        if len(seen) <= refuse:
            raise RuntimeError("database is locked")
        return real(*a, **k)

    repo.open_trade = booking
    return seen


def test_an_entry_whose_fill_is_being_booked_counts_as_working_until_its_record_is_saved():
    broker, repo = _Broker(), _Repo([])
    ex = _executor(broker, repo)
    play = _day_play()                                                     # 10 shares, entry 100, stop 98
    assert ex.execute_play(play, Account(account_id="DU"), plan=PLAN)["ok"]
    seen = _watch_booking(ex, repo, refuse=1)
    broker.reports["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                      avg_fill_price=100.0)
    ex.sync_open_orders()                                                  # refused: followed on
    ex.sync_open_orders()                                                  # saved
    assert seen == [[(play.id, 20.0)], [(play.id, 20.0)]]                  # its $20 counted while each try ran
    assert ex.working_entries() == [] and ex._booking == {} and len(repo.open_trades()) == 1


def test_an_unanswered_entry_found_filled_counts_as_working_until_its_record_is_saved():
    broker, repo = _Unanswered(lands="filled"), _Repo([])
    ex = _executor(broker, repo)
    play = _day_play()
    assert ex.execute_play(play, Account(account_id="DU"), plan=PLAN)["sent_unknown"]
    seen = _watch_booking(ex, repo, refuse=1)
    ex.sync_open_orders()                                                  # found filled, refused: looked for again
    ex.sync_open_orders()                                                  # saved
    assert seen == [[(play.id, 20.0)], [(play.id, 20.0)]]
    assert ex.working_entries() == [] and ex._booking == {} and len(repo.open_trades()) == 1


def test_a_working_entry_keeps_the_risk_and_cost_it_was_sent_with_whatever_its_play_is_sized_at_since():
    broker, repo = _Broker(), _Repo([])
    ex = _executor(broker, repo)
    play = _day_play()                                                     # entry 100, stop 98
    play.risk_per_share = 2.5                                              # re-priced by the last look to 100.50
    assert ex.execute_play(play, Account(account_id="DU"), plan=PLAN)["ok"]
    play.risk_per_share = 2.0                                              # sized again at its own entry since
    [w] = ex.working_entries()
    assert (w["risk"], w["notional"]) == (25.0, 1005.0)


def test_an_entry_fill_whose_booking_keeps_failing_is_said_again_and_its_shares_are_no_order_in_flight(monkeypatch):
    from autotradebot.engine.reconcile import PositionCheck
    from autotradebot.execution import executor as executor_module

    now = [1000.0]
    monkeypatch.setattr(executor_module.time, "monotonic", lambda: now[0])
    heard = []
    broker, repo = _Broker({"AAA": 10}), _Repo([])
    ex = _executor(broker, repo, bus=SimpleNamespace(publish=lambda topic, **p: heard.append((topic, p))))
    real_open = repo.open_trade

    def refuse(*a, **k):
        raise RuntimeError("database or disk is full")

    repo.open_trade = refuse
    assert ex.execute_play(_day_play(), Account(account_id="DU"), plan=PLAN)["ok"]
    assert ex.symbols_in_flight(unbooked=False) == {"AAA"}                 # working: its fills change the counts
    broker.reports["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                      avg_fill_price=100.02)
    for _ in range(int(ex.UNPROTECTED_REPEAT_S) // 4):                     # five minutes of order syncs, 4 s apart
        ex.sync_open_orders()
        now[0] += 4.0
    unbooked = [p for topic, p in heard if topic == "order.unbooked"]
    assert len(unbooked) == 1
    ex.sync_open_orders()                                                  # still failing five minutes on: said again
    unbooked = [p for topic, p in heard if topic == "order.unbooked"]
    assert len(unbooked) == 2 and unbooked[1]["tries"] == 76 and "still couldn't be saved" in unbooked[1]["msg"]
    assert "Shares without a record" in unbooked[1]["msg"]
    # it keeps its place in Autopilot's caps, but its order is done: its shares are held, with no record and no stop,
    # and the check on shares no record explains sees them
    assert [(w["symbol"], w["unbooked"]) for w in ex.working_entries()] == [("AAA", True)]
    assert ex.symbols_in_flight() == {"AAA"} and ex.symbols_in_flight(unbooked=False) == set()
    check = PositionCheck()
    said = [d for at in (0.0, check.DRIFT_ALERT_S) for d in check.drift(
        VENUE, "IBKR paper", repo.open_trades(), {"AAA": 10.0}, in_flight=ex.symbols_in_flight(unbooked=False),
        regular=True, account_age_s=0.0, connection_age_s=1e9, now=at)]
    assert [d["symbol"] for d in said] == ["AAA"]

    repo.open_trade = real_open                                            # the database takes it again
    ex.sync_open_orders()
    assert [t["quantity"] for t in repo.open_trades()] == [10] and ex.working_entries() == []
    assert ex._unbooked == {} and ex._unbooked_said == {}


def test_an_unanswered_entry_found_filled_whose_booking_fails_stays_looked_for_and_is_booked_on_the_next_sync():
    broker, repo = _Unanswered(lands="filled"), _Repo([])
    ex = _executor(broker, repo)
    calls = _refuses_once(repo, "open_trade")
    play = _day_play()
    assert ex.execute_play(play, Account(account_id="DU"), plan=PLAN)["sent_unknown"]
    ex.sync_open_orders()
    assert repo.open_trades() == [] and len(ex._unknown) == 1                   # still looked for: nothing in its place
    assert [w["play_id"] for w in ex.working_entries()] == [play.id]
    ex.sync_open_orders()
    [t] = repo.open_trades()
    assert (t["quantity"], t["entry_price"]) == (10, 100.5) and ex._unknown == {} and len(calls) == 2
    assert len(broker.orders) == 1


def test_orders_that_fill_at_once_whose_booking_fails_are_followed_and_booked_on_the_next_sync():
    class _FillsAtOnce(_Broker):
        def place_order(self, req):
            res = super().place_order(req)
            self.reports[res.order_id] = filled = OrderResult(
                order_id=res.order_id, status="FILLED", symbol=req.symbol, submitted_qty=req.quantity,
                filled_qty=req.quantity, avg_fill_price=100.01)
            return filled

    broker, repo = _FillsAtOnce({"AAA": 10}), _Repo([])
    ex = _executor(broker, repo)
    opens = _refuses_once(repo, "open_trade")
    out = ex.execute_play(_day_play(), Account(account_id="DU"), plan=PLAN)
    assert out["ok"] and out["order_id"] == "1" and repo.open_trades() == []
    assert [w["order_id"] for w in ex.working_entries()] == ["1"]               # followed instead
    ex.sync_open_orders()
    [t] = repo.open_trades()
    assert (t["quantity"], t["entry_price"]) == (10, 100.01) and ex.working_entries() == [] and len(opens) == 2

    closes = _refuses_once(repo, "close_trade")
    assert ex.close_trade(t["id"], reason="manual")["ok"]
    assert repo.get_trade(t["id"])["status"] == "OPEN" and ex.pending_exit_trade_ids() == {t["id"]}
    ex.sync_open_orders()
    assert repo.get_trade(t["id"])["status"] == "CLOSED" and ex.pending_exit_trade_ids() == set() and len(closes) == 2
    assert len(broker.orders) == 2


def _unbooked_entry(broker, repo, bus=SILENT):
    """An executor whose day-trade entry for 10 AAA filled at the broker (order 1) while the database refuses to book
    it. Returns the executor, the play, and the call that lets the database take bookings again."""
    ex = _executor(broker, repo, bus=bus)
    ex.STAND_DOWN_S = ex.STAND_DOWN_POLL_S = 0.0
    real_open = repo.open_trade

    def refuse(*a, **k):
        raise RuntimeError("database is locked")

    repo.open_trade = refuse
    play = _day_play()
    assert ex.execute_play(play, Account(account_id="DU"), plan=PLAN)["ok"]
    broker.reports["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                      avg_fill_price=100.0)
    ex.sync_open_orders()
    assert [(w["symbol"], w["unbooked"]) for w in ex.working_entries()] == [("AAA", True)]

    def allow():
        repo.open_trade = real_open

    return ex, play, allow


def test_a_quit_leaves_an_entry_whose_fill_waits_to_be_saved_followed_and_the_next_sync_books_it():
    broker, repo = _Broker({"AAA": 10}), _Repo([])
    ex, play, allow = _unbooked_entry(broker, repo)
    assert ex.cancel_pending_entries() == 0                                # a quit: nothing to cancel...
    assert broker.cancelled == [] and [w["unbooked"] for w in ex.working_entries()] == [True]   # ...still followed
    allow()                                                                # the database takes it again
    ex.sync_open_orders()
    [t] = repo.open_trades()                                               # booked: the quit closes it like any other
    assert t["quantity"] == 10 and ex.working_entries() == [] and play.status is PlayStatus.FILLED


def test_the_pairs_desk_calling_off_a_leg_whose_fill_waits_to_be_saved_keeps_it_followed_and_cancels_nothing():
    broker, repo = _Broker({"AAA": 10}), _Repo([])
    ex, play, allow = _unbooked_entry(broker, repo)
    assert ex.cancel_entries_for(play.id) == 1
    # nothing is asked of an order that has filled, and the leg is called off - but still followed: let go, its shares
    # would stay at the broker with no record, no stop and no exit. Booked, it is a leg of a pair that is over, which
    # the desk closes (test_pair_desk.py)
    assert broker.cancelled == [] and play.status is PlayStatus.CANCELED
    assert [(w["play_id"], w["unbooked"]) for w in ex.working_entries()] == [(play.id, True)]
    allow()
    ex.sync_open_orders()
    [t] = repo.open_trades()
    assert t["quantity"] == 10 and ex.working_entries() == [] and ex._unbooked == {}


def test_a_booking_that_keeps_failing_logs_its_traceback_when_the_dashboard_is_told_and_one_line_between(
        monkeypatch, caplog):
    from autotradebot.execution import executor as executor_module

    now = [1000.0]
    monkeypatch.setattr(executor_module.time, "monotonic", lambda: now[0])
    heard = []
    with caplog.at_level(logging.WARNING, logger=executor_module.__name__):
        ex, _, _ = _unbooked_entry(_Broker({"AAA": 10}), _Repo([]),
                                   bus=SimpleNamespace(publish=lambda topic, **p: heard.append((topic, p))))
        for _ in range(3):                                                 # the next order syncs, 4 s apart
            now[0] += 4.0
            ex.sync_open_orders()
        now[0] += ex.UNPROTECTED_REPEAT_S
        ex.sync_open_orders()                                              # told again five minutes on
    failed = [r for r in caplog.records if "BOOKING FAILED" in r.getMessage()]
    assert [bool(r.exc_info) for r in failed] == [True, False, False, False, True]
    assert all("database is locked" in r.getMessage() for r in failed if not r.exc_info)   # its first line, in one
    assert [p["tries"] for topic, p in heard if topic == "order.unbooked"] == [1, 5]


def test_the_order_sync_skips_an_order_the_broker_cant_read_but_logs_any_other_fault(caplog):
    broker, repo = _Broker({"AAA": 10}), _Repo([_trade()])
    ex = _executor(broker, repo)
    assert ex.close_trade("t1")["ok"]

    def unreadable(order_id):
        raise BrokerError("IBKR is not connected")

    broker.get_order = unreadable
    with caplog.at_level(logging.WARNING):
        ex.sync_open_orders()
    assert "following order" not in caplog.text and ex.pending_exit_trade_ids() == {"t1"}   # asked again next pass
    del broker.get_order
    broker.reports["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                      avg_fill_price=97.8)

    def garbled(p, res):
        raise ValueError("a report the app can't read")

    ex._on_filled = garbled
    with caplog.at_level(logging.WARNING):
        ex.sync_open_orders()
    assert "following order 1 failed" in caplog.text and "a report the app can't read" in caplog.text


# ---------------------------------------------------------------- the order audit
class _AuditedRepo(_Repo):
    """The fake repository, keeping the order audit's rows as the real one is handed them."""

    def __init__(self, trades):
        super().__init__(trades)
        self.audit = []

    def record_order_audit(self, action, request, response, ok, broker, play_id="", trade_id="", message="",
                           ts=None):
        self.audit.append(dict(action=action, request=request, response=response, ok=ok, play_id=play_id,
                               trade_id=trade_id, message=message, ts=ts))

    def rows(self, action):
        return [r for r in self.audit if r["action"] == action]


def test_the_order_audit_keeps_the_brokers_order_id_status_and_message_for_each_order_placed():
    from dataclasses import replace

    broker, repo = _Broker({"AAA": 10}), _AuditedRepo([_trade()])
    placed = broker.place_order
    broker.place_order = lambda req: replace(placed(req), message="held until the open")
    ex = _executor(broker, repo)
    play = _entry(ex, symbol="BBB")
    assert ex.close_trade("t1", reason="stop")["ok"]
    entry, exit_ = repo.rows("PLACE")
    assert (entry["play_id"], entry["request"]["tag"], entry["ok"]) == (play.id, play.id, True)
    assert (exit_["trade_id"], exit_["request"]["tag"], exit_["ok"]) == ("t1", "exit:t1", True)
    assert [(r["response"]["order_id"], r["response"]["status"], r["response"]["message"]) for r in (entry, exit_)] \
        == [("1", "SUBMITTED", "held until the open"), ("2", "SUBMITTED", "held until the open")]


def test_every_cancel_the_app_asks_for_is_audited_with_why_and_one_the_broker_refused_as_failed():
    import datetime as dt

    broker, repo = _Broker(), _AuditedRepo([])
    broker.account_id = "DU1234567"
    ex = _executor(broker, repo)
    day, swing = _entry(ex), _entry(ex, symbol="BBB", timeframe=Timeframe.SWING)
    late = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=11)
    assert ex.expire_entries(now=late) == ["1"]                    # the day trade's entry, not filled in time

    def refused(order_id):
        raise BrokerError(f"account DU1234567: order {order_id} can't be cancelled now")

    broker.cancel_order = refused
    assert ex.cancel_entries_for(swing.id) == 1                    # the pairs desk calls the other off
    timed_out, called_off = repo.rows("CANCEL")
    assert (timed_out["request"]["order_id"], timed_out["play_id"], timed_out["ok"]) == ("1", day.id, True)
    assert timed_out["message"].startswith("not filled within 10 minutes") and timed_out["request"]["symbol"] == "AAA"
    assert (called_off["request"]["order_id"], called_off["play_id"], called_off["ok"]) == ("2", swing.id, False)
    assert called_off["response"]["outcome"] == "refused"
    # the broker's words are kept, the account number isn't
    assert called_off["message"] == "called off by the pairs desk: account <account>: order 2 can't be cancelled now"
    assert "DU1234567" not in str(repo.audit)


# ---------------------------------------------------------------- fees
def test_an_orders_fees_are_booked_with_its_fill_and_the_order_that_paid_them():
    broker, repo = _Broker({"AAA": 10}), _Repo([])
    ex = _executor(broker, repo)
    _entry(ex)
    # the entry's fees are on its fills only; the exit's the broker gives for the whole order
    broker.reports["1"] = OrderResult(order_id="1", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                      avg_fill_price=100.0,
                                      fills=[Fill(order_id="1", symbol="AAA", side=Side.LONG, quantity=6, price=100.0,
                                                  commission=0.6),
                                             Fill(order_id="1", symbol="AAA", side=Side.LONG, quantity=4, price=100.0,
                                                  commission=0.4)])
    ex.sync_open_orders()
    [tid] = [t["id"] for t in repo.open_trades()]
    assert ex.close_trade(tid)["ok"]
    broker.reports["2"] = OrderResult(order_id="2", status="FILLED", symbol="AAA", submitted_qty=10, filled_qty=10,
                                      avg_fill_price=101.0, commission=1.25)
    ex.sync_open_orders()
    assert repo.paid == [(tid, "ENTRY", "1", pytest.approx(1.0)), (tid, "EXIT", "2", 1.25)]


class _Reporting(_Broker):
    """An IBKR account whose executions carry the commission reports in ``reported`` - none yet, to begin with."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.reported, self.asked = [], 0

    def get_fills(self, symbol=None, strict=False):
        self.asked += 1
        return list(self.reported)


def _booked_round_trip(repo, symbol, entry_id, exit_id, entry_fee=0.0):
    play = Play(symbol=symbol, side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.INTRADAY, entry=100.0, stop=98.0, targets=[104.0])
    repo.record_play(play)
    tid = repo.open_trade(play, 100.0, 10, VENUE, entry_id, commission=entry_fee)
    repo.close_trade(tid, 104.0, exit_reason="target", broker_order_id=exit_id)
    return tid


def test_the_fees_ibkr_reports_after_the_fills_were_booked_are_added_to_the_days_records(repo):
    tid = _booked_round_trip(repo, "FEE1", "501", "502", entry_fee=0.6)      # the entry's first report only
    assert repo.get_trade(tid)["realized_pl"] == pytest.approx(39.4)
    broker = _Reporting()
    ex = _executor(broker, repo)

    def entry(qty, fee):
        return Fill(order_id="501", symbol="FEE1", side=Side.LONG, quantity=qty, price=100.0, commission=fee)

    broker.reported = [entry(6, 0.6), entry(4, 0.0),
                       Fill(order_id="502", symbol="FEE1", side=Side.SHORT, quantity=10, price=104.0)]
    assert ex._top_up_fees(now=1000.0) == [] and broker.asked == 1           # nothing more reported yet
    broker.reported = [entry(6, 0.6), entry(4, 0.4),
                       Fill(order_id="502", symbol="FEE1", side=Side.SHORT, quantity=10, price=104.0, commission=1.0)]
    assert ex._top_up_fees(now=1030.0) == [] and broker.asked == 1           # looked at again only a minute on
    assert set(ex._top_up_fees(now=1060.0)) == {tid}
    t = repo.get_trade(tid)
    assert (t["fees"], t["realized_pl"], t["r_multiple"]) == (pytest.approx(2.0), pytest.approx(38.0),
                                                              pytest.approx(1.9))
    ex._top_up_fees(now=1120.0)
    assert repo.get_trade(tid)["realized_pl"] == pytest.approx(38.0)        # once


def test_a_fills_fee_is_settled_a_while_after_its_booking_and_the_simulator_is_never_asked(repo):
    from autotradebot.util import clock

    tid = _booked_round_trip(repo, "FEE2", "601", "602")
    broker = _Reporting()
    broker.reported = [Fill(order_id="601", symbol="FEE2", side=Side.LONG, quantity=10, price=100.0)]
    ex = _executor(broker, repo)
    ex.FEES_WAIT_S = 0.0                                                       # (fifteen minutes on, in the app)
    assert ex._top_up_fees(now=1000.0) == [] and broker.asked == 1           # an account charged nothing
    mine = {r["fill_id"] for r in repo.fills_on(VENUE, clock.now_ny().date()) if r["trade_id"] == tid}
    assert len(mine) == 2 and mine <= ex._fees_settled
    ex._top_up_fees(now=1060.0)
    assert broker.asked == 1                                                   # every fill of the day settled
    simulator = _Reporting()
    Executor(simulator, repo, cfg=get_settings().config.execution, bus=SILENT, venue="paper")._top_up_fees(now=1000.0)
    assert simulator.asked == 0
