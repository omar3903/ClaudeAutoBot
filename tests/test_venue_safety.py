"""Exits only ever go to the account that actually holds the position."""

from __future__ import annotations

from types import SimpleNamespace

from tos_bot.core.models import OrderResult, Quote
from tos_bot.execution.executor import Executor
from tos_bot.execution.exit_manager import ExitManager

SILENT = SimpleNamespace(publish=lambda *a, **k: None)
CFG = SimpleNamespace(enabled=True, breakeven_at_r=0, breakeven_buffer_bps=0, trail_start_r=0,
                      trail_lock_ratio=0, flatten_intraday_before_close_min=0, max_swing_hold_days=0)


class _Repo:
    def __init__(self, trades):
        self.t = {x["id"]: x for x in trades}

    def open_trades(self):
        return [dict(x) for x in self.t.values() if x["status"] == "OPEN"]

    def get_trade(self, tid):
        return dict(self.t[tid]) if tid in self.t else None

    def update_trade_risk(self, tid, **kw):
        self.t[tid].update({k: v for k, v in kw.items() if v is not None})

    def note_overdue(self, tid):
        pass


class _Broker:
    name, paper, supports_bracket_native = "ibkr", False, False

    def __init__(self):
        self.orders = []

    def place_order(self, req):
        self.orders.append(req)
        return OrderResult(order_id="1", status="FILLED", symbol=req.symbol,
                           submitted_qty=req.quantity, filled_qty=req.quantity, avg_fill_price=97.0)


def _trade(**kw):
    d = dict(id="t1", symbol="AAA", side="LONG", timeframe="SWING", status="OPEN", broker="paper",
             entry_price=100.0, quantity=10, stop_price=98.0, target_price=110.0,
             initial_stop_price=98.0, initial_target_price=110.0, hwm_price=100.0,
             managed_exit=True, mae=0.0, mfe=0.0, entry_time="2026-09-03T09:40:00")
    d.update(kw)
    return d


def test_executor_refuses_to_close_a_position_held_on_another_venue():
    broker = _Broker()
    ex = Executor(broker, _Repo([_trade(broker="paper")]), cfg=SimpleNamespace(), bus=SILENT,
                  venue="ibkr-paper")
    r = ex.close_trade("t1")
    assert not r["ok"] and "simulator" in r["reason"]
    assert broker.orders == []                               # nothing reached IBKR


def test_exit_manager_only_manages_trades_on_its_own_venue():
    repo = _Repo([_trade(broker="paper")])                   # stop 98, price 90 -> would stop out
    closed = []
    ex = SimpleNamespace(close_trade=lambda tid, reason="manual": closed.append(tid) or {"ok": True, "trade": {}})
    em = ExitManager(repo, ex, quote_fn=lambda s: Quote(symbol=s, bid=90, ask=90, last=90),
                     cfg=CFG, bus=SILENT, venue="ibkr-live")
    em.run_once()
    assert closed == []
    em.venue = "paper"
    em.run_once()
    assert closed == ["t1"]


# ---------------------------------------------------------------- only what's actually held
from tos_bot.config import get_settings  # noqa: E402
from tos_bot.core.models import Account, Position  # noqa: E402


class _ClosingRepo(_Repo):
    def close_trade(self, tid, exit_price, exit_reason=""):
        self.t[tid].update(status="CLOSED", exit_price=exit_price, exit_reason=exit_reason)
        return dict(self.t[tid])


class _HoldingBroker(_Broker):
    name = "paper"

    def __init__(self, positions):
        super().__init__()
        self.positions = positions

    def get_account(self):
        return Account(account_id="SIM", positions=[Position(symbol=s, quantity=q, avg_price=100.0)
                                                    for s, q in self.positions.items()])


def _executor(broker):
    return Executor(broker, _ClosingRepo([_trade()]), cfg=get_settings().config.execution,
                    bus=SILENT, venue="paper")


def test_no_exit_is_sent_for_shares_the_broker_does_not_hold():
    for held in ({}, {"AAA": -10}):              # closed outside the app / only the other side
        broker = _HoldingBroker(held)
        r = _executor(broker).close_trade("t1")
        assert not r["ok"] and r["not_held"] and broker.orders == []


def test_an_exit_never_sells_more_than_the_broker_holds():
    broker = _HoldingBroker({"AAA": 4})          # 6 of the 10 were sold elsewhere
    r = _executor(broker).close_trade("t1")
    assert r["ok"] and [o.quantity for o in broker.orders] == [4]


def test_the_exit_still_goes_out_when_the_broker_cant_say():
    broker = _Broker()                           # no account call available
    broker.name = "paper"
    r = _executor(broker).close_trade("t1")
    assert r["ok"] and [o.quantity for o in broker.orders] == [10]


def test_exit_manager_reports_a_missing_position_once():
    events = []
    bus = SimpleNamespace(publish=lambda topic, **kw: events.append(topic))
    ex = SimpleNamespace(close_trade=lambda tid, reason="manual": {"ok": False, "not_held": True, "reason": "gone"})
    em = ExitManager(_Repo([_trade()]), ex, quote_fn=lambda s: Quote(symbol=s, bid=90, ask=90, last=90),
                     cfg=CFG, bus=bus, venue="paper")
    for _ in range(3):
        em.run_once()
    assert events.count("exit.not_held") == 1 and "t1" not in em._closing
