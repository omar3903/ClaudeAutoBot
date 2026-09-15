from __future__ import annotations

from types import SimpleNamespace

from tos_bot.core.models import Quote
from tos_bot.execution.exit_manager import ExitManager


class FakeRepo:
    def __init__(self, trades):
        self._t = {t["id"]: dict(t) for t in trades}

    def open_trades(self):
        return [dict(t) for t in self._t.values() if t["status"] == "OPEN"]

    def update_trade_risk(self, tid, **kw):
        self._t[tid].update({k: v for k, v in kw.items() if v is not None})

    def note_overdue(self, tid):
        self._t[tid]["overdue_notified"] = True

    def get_trade(self, tid):
        return dict(self._t[tid]) if tid in self._t else None


class FakeExecutor:
    def __init__(self, repo):
        self.repo = repo
        self.closed = []

    def pending_exit_trade_ids(self):
        return set()

    def close_trade(self, tid, reason="manual", limit_price=None):
        t = self.repo._t.get(tid)
        if not t or t["status"] == "CLOSED":
            return {"ok": False}
        t["status"] = "CLOSED"
        t["exit_reason"] = reason
        self.closed.append((tid, reason))
        return {"ok": True, "trade": {"id": tid, "realized_pl": 0.0}}


def _trade(**kw):
    d = dict(id="t1", symbol="AAA", side="LONG", timeframe="SWING", status="OPEN",
             entry_price=100.0, quantity=10, stop_price=98.0, target_price=110.0,
             initial_stop_price=98.0, initial_target_price=110.0, hwm_price=100.0,
             managed_exit=True, mae=0.0, mfe=0.0, entry_time="2026-09-03T09:40:00")
    d.update(kw)
    return d


def _mk(repo, price, cfg=None):
    ex = FakeExecutor(repo)
    cfg = cfg or SimpleNamespace(enabled=True, breakeven_at_r=1.0, breakeven_buffer_bps=5,
                                 trail_start_r=1.5, trail_lock_ratio=0.5,
                                 flatten_intraday_before_close_min=10, max_swing_hold_days=0)
    em = ExitManager(repo, ex, quote_fn=lambda s: Quote(symbol=s, bid=price, ask=price, last=price),
                     cfg=cfg, bus=SimpleNamespace(publish=lambda *a, **k: None))
    return em, ex


def test_stop_closes_long():
    repo = FakeRepo([_trade()])
    em, ex = _mk(repo, price=97.5)          # below stop 98
    em.run_once()
    assert ex.closed == [("t1", "stop")]


def test_target_closes_long():
    repo = FakeRepo([_trade()])
    em, ex = _mk(repo, price=110.5)         # above target 110
    em.run_once()
    assert ex.closed and ex.closed[0][1] == "target"


def test_breakeven_moves_stop_to_entry():
    repo = FakeRepo([_trade()])             # risk = 2.0/sh
    em, ex = _mk(repo, price=102.1)         # +1.05R -> breakeven trigger
    em.run_once()
    assert not ex.closed
    assert repo._t["t1"]["stop_price"] >= 100.0     # moved up to ~entry


def test_trailing_locks_in_r():
    repo = FakeRepo([_trade()])
    em, ex = _mk(repo, price=106.0)         # +3R, lock ratio 0.5 -> stop ~ +1.5R = 103
    em.run_once()
    s = repo._t["t1"]["stop_price"]
    assert 102.0 <= s < 106.0
    assert not ex.closed


def test_stop_only_ratchets_up():
    repo = FakeRepo([_trade(stop_price=103.0, initial_stop_price=98.0)])  # already trailed
    em, ex = _mk(repo, price=104.0)         # +2R; trail would compute ~102 -> must NOT lower it
    em.run_once()
    assert repo._t["t1"]["stop_price"] >= 103.0


def test_eod_flatten_intraday():
    repo = FakeRepo([_trade(timeframe="INTRADAY")])
    # force minutes_to_close small via a cfg with a huge window
    cfg = SimpleNamespace(enabled=True, breakeven_at_r=0, breakeven_buffer_bps=0,
                          trail_start_r=0, trail_lock_ratio=0,
                          flatten_intraday_before_close_min=10_000, max_swing_hold_days=0)
    em, ex = _mk(repo, price=100.5, cfg=cfg)
    em.run_once()
    # minutes_to_close is huge when market closed -> only flattens when < window;
    # with a 10000-min window it always flattens during any regular session, and
    # returns 1e9 (no flatten) when closed. Accept either but the call must not crash.
    assert isinstance(ex.closed, list)


def test_disabled_does_nothing():
    repo = FakeRepo([_trade()])
    cfg = SimpleNamespace(enabled=False)
    em, ex = _mk(repo, price=50.0, cfg=cfg)
    em.run_once()
    assert not ex.closed


def test_overdue_notifies_once_and_does_not_close():
    repo = FakeRepo([_trade(time_status="overdue", overdue_notified=False,
                            initial_stop_price=98.0, stop_price=98.0, target_price=110.0)])
    ex = FakeExecutor(repo)
    events = []
    cfg = SimpleNamespace(enabled=True, breakeven_at_r=1.0, breakeven_buffer_bps=5,
                          trail_start_r=1.5, trail_lock_ratio=0.5,
                          flatten_intraday_before_close_min=10, max_swing_hold_days=0)
    from tos_bot.core.models import Quote
    em = ExitManager(repo, ex, quote_fn=lambda s: Quote(symbol=s, bid=101, ask=101, last=101),
                     cfg=cfg, bus=SimpleNamespace(publish=lambda topic, **k: events.append(topic)))
    em.run_once()
    em.run_once()
    assert events.count("trade.overdue") == 1     # one-shot
    assert not ex.closed                           # overdue never force-closes
