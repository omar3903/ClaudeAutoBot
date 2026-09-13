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
