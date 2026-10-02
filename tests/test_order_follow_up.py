"""After an order is sent: fills, rejections, cancellations, and orders the broker loses track of."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from autotradebot.brokers.base import BrokerError
from autotradebot.config import get_settings
from autotradebot.core.enums import Side, StrategyKind, Timeframe
from autotradebot.core.models import Account, OrderResult, Play, Position, Quote
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

    def settle_play(self, play_id, status, outcome=None):
        self.settled.append((play_id, status, outcome))
        return True

    def open_trades(self):
        return [dict(x) for x in self.t.values() if x["status"] == "OPEN"]

    def get_trade(self, tid):
        return dict(self.t[tid]) if tid in self.t else None

    def update_trade_risk(self, tid, **kw):
        self.t[tid].update({k: v for k, v in kw.items() if v is not None})

    def open_trade(self, play, price, qty, venue, order_id, order_type="LIMIT", order_session="REGULAR",
                   entry_context=None, submitted_at=None, decision=None):
        tid = f"t{len(self.t) + 1}"
        self.t[tid] = _trade(id=tid, symbol=play.symbol, entry_price=price, quantity=qty, broker=venue)
        self.t[tid]["entry_context"], self.t[tid]["submitted_at"] = entry_context, submitted_at
        self.t[tid]["decision"] = decision
        return tid

    def close_trade(self, tid, exit_price, exit_reason="", decision_price=None):
        self.t[tid].update(status="CLOSED", exit_price=exit_price, exit_reason=exit_reason,
                           exit_decision_price=decision_price)
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
