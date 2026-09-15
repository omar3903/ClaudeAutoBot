"""After an order is sent: fills, rejections, cancellations, and orders the broker loses track of."""

from __future__ import annotations

from types import SimpleNamespace

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

    def open_trade(self, play, price, qty, venue, order_id, order_type="LIMIT", order_session="REGULAR"):
        tid = f"t{len(self.t) + 1}"
        self.t[tid] = _trade(id=tid, symbol=play.symbol, entry_price=price, quantity=qty, broker=venue)
        return tid

    def close_trade(self, tid, exit_price, exit_reason=""):
        self.t[tid].update(status="CLOSED", exit_price=exit_price, exit_reason=exit_reason)
        return dict(self.t[tid])


class _Broker:
    """An account that takes orders and leaves them working until a test reports otherwise."""

    name, paper, supports_bracket_native = "ibkr", False, False

    def __init__(self, positions=None):
        self.positions = dict(positions or {})
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
        return []

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
    assert ex.execute_play(play, Account(account_id="DU"), plan=PLAN)["status"] == "SUBMITTED"
    assert [(w["symbol"], w["risk"]) for w in ex.working_entries()] == [("AAA", 20.0)]

    broker.reports["1"] = OrderResult(order_id="1", status="CANCELED", symbol="AAA", submitted_qty=10,
                                      filled_qty=4, avg_fill_price=100.02)
    ex.sync_open_orders()
    assert ex.working_entries() == []
    assert [(t["symbol"], t["quantity"], t["entry_price"]) for t in repo.open_trades()] == [("AAA", 4.0, 100.02)]
