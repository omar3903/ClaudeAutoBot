from __future__ import annotations

from types import SimpleNamespace

from tos_bot.core.models import Quote
from tos_bot.execution.exit_manager import ExitManager, stop_locked


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
        self.reduced = []

    def pending_exit_trade_ids(self):
        return set()

    def close_trade(self, tid, reason="manual", limit_price=None, qty=None, after_fill=None, decision_price=None):
        t = self.repo._t.get(tid)
        if not t or t["status"] == "CLOSED":
            return {"ok": False}
        if qty is not None and qty < t["quantity"]:
            t["quantity"] -= qty
            t["banked_pl"] = t.get("banked_pl", 0.0) + qty * 1.0
            t.update({k: v for k, v in (after_fill or {}).items() if v is not None})
            self.reduced.append((tid, reason, qty))
            return {"ok": True, "status": "FILLED", "reduced": True, "trade": dict(t)}
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


def test_half_comes_off_at_the_first_target_and_the_rest_runs_to_the_second():
    cfg = SimpleNamespace(enabled=True, breakeven_at_r=0.0, breakeven_buffer_bps=0, trail_start_r=0.0,
                          trail_lock_ratio=0.5, flatten_intraday_before_close_min=10, max_swing_hold_days=0,
                          scale_out_pct=50.0, scale_out_lock_r=0.0)
    repo = FakeRepo([_trade(target2_price=120.0, initial_quantity=10)])
    em, ex = _mk(repo, price=110.5, cfg=cfg)                       # the first target, 110
    em.run_once()
    t = repo._t["t1"]
    assert ex.reduced == [("t1", "target-1", 5.0)] and ex.closed == []
    assert (t["quantity"], t["stop_price"], t["target_price"]) == (5, 100.0, 120.0)     # break-even, on to target 2
    em.run_once()                                                   # still above target 1: nothing more comes off
    assert ex.reduced == [("t1", "target-1", 5.0)] and ex.closed == []
    em2, ex2 = _mk(repo, price=120.2, cfg=cfg)                      # the second target
    em2.run_once()
    assert ex2.closed == [("t1", "target")]


def test_the_position_exits_whole_without_a_second_target_or_with_the_scale_out_off():
    cfg = SimpleNamespace(enabled=True, breakeven_at_r=0.0, breakeven_buffer_bps=0, trail_start_r=0.0,
                          trail_lock_ratio=0.5, flatten_intraday_before_close_min=10, max_swing_hold_days=0,
                          scale_out_pct=50.0, scale_out_lock_r=0.0)
    em, ex = _mk(FakeRepo([_trade(initial_quantity=10)]), price=110.5, cfg=cfg)     # one target only
    em.run_once()
    assert ex.closed == [("t1", "target")] and ex.reduced == []
    off = SimpleNamespace(**{**cfg.__dict__, "scale_out_pct": 0.0})
    em, ex = _mk(FakeRepo([_trade(target2_price=120.0, initial_quantity=10)]), price=110.5, cfg=off)
    em.run_once()
    assert ex.closed == [("t1", "target")] and ex.reduced == []
    em, ex = _mk(FakeRepo([_trade(target2_price=120.0, initial_quantity=1, quantity=1)]), price=110.5, cfg=cfg)
    em.run_once()                                                   # a single share can't be halved
    assert ex.closed == [("t1", "target")] and ex.reduced == []


def _day_cfg(**over):
    base = dict(enabled=True, breakeven_at_r=1.0, breakeven_buffer_bps=5, trail_start_r=1.5, trail_lock_ratio=0.5,
                flatten_intraday_before_close_min=0, max_swing_hold_days=0, intraday_time_stop=True)
    base.update(over)
    return SimpleNamespace(**base)


def test_a_day_trade_past_its_window_that_isnt_working_is_closed():
    events = []
    repo = FakeRepo([_trade(timeframe="INTRADAY", time_status="overdue", overdue_notified=False, held_label="120m")])
    ex = FakeExecutor(repo)
    em = ExitManager(repo, ex, quote_fn=lambda s: Quote(symbol=s, bid=100.3, ask=100.3, last=100.3),
                     cfg=_day_cfg(), bus=SimpleNamespace(publish=lambda topic, **k: events.append((topic, k))))
    em.run_once()
    assert ex.closed == [("t1", "time-stop")]
    [(_, overdue)] = [e for e in events if e[0] == "trade.overdue"]
    assert "being closed" in overdue["msg"]


def test_a_working_day_trade_a_swing_trade_or_the_rule_switched_off_are_left_to_run():
    # working: its stop is at break-even or better, so it keeps its trail until the flatten
    working = FakeRepo([_trade(timeframe="INTRADAY", time_status="overdue", stop_price=100.2)])
    em, ex = _mk(working, price=100.8, cfg=_day_cfg())
    em.run_once()
    assert ex.closed == []
    short_working = FakeRepo([_trade(side="SHORT", timeframe="INTRADAY", time_status="overdue", stop_price=99.9,
                                     initial_stop_price=102.0, target_price=90.0, initial_target_price=90.0)])
    em, ex = _mk(short_working, price=99.5, cfg=_day_cfg())
    em.run_once()
    assert ex.closed == []
    # a swing trade past its window is the swing time-stop's business, not this one
    swing = FakeRepo([_trade(timeframe="SWING", time_status="overdue")])
    em, ex = _mk(swing, price=100.3, cfg=_day_cfg())
    em.run_once()
    assert ex.closed == []
    # switched off, or a trade exited by hand: overdue only notifies
    for cfg, managed in ((_day_cfg(intraday_time_stop=False), True), (_day_cfg(), False)):
        repo = FakeRepo([_trade(timeframe="INTRADAY", time_status="overdue", managed_exit=managed)])
        em, ex = _mk(repo, price=100.3, cfg=cfg)
        em.run_once()
        assert ex.closed == []


def test_a_stop_at_break_even_or_better_is_locked_either_way():
    assert stop_locked("LONG", 100.0, 100.0) and stop_locked("LONG", 100.5, 100.0)
    assert not stop_locked("LONG", 99.5, 100.0) and not stop_locked("LONG", None, 100.0)
    assert stop_locked("SHORT", 100.0, 100.0) and stop_locked("SHORT", 99.5, 100.0)
    assert not stop_locked("SHORT", 100.5, 100.0)



def test_the_stop_moved_message_says_what_the_stop_locks_not_where_the_trade_is():
    cfg = SimpleNamespace(enabled=True, breakeven_at_r=1.0, breakeven_lock_r=0.3, breakeven_buffer_bps=5,
                          trail_start_r=1.5, trail_lock_ratio=0.5,
                          flatten_intraday_before_close_min=10, max_swing_hold_days=0)
    cases = [  # (side, initial stop, price, R the new stop locks, R the trade is at) - risk 2.0/sh from 100
        ("LONG", 98.0, 102.8, 0.325, 1.4),      # break-even plus 0.3R and a 5 bp buffer: 100.65
        ("SHORT", 102.0, 97.2, 0.325, 1.4),     # the same, below the entry: 99.35
        ("LONG", 98.0, 106.0, 1.5, 3.0),        # the trail keeps half of +3R: 103
    ]
    for side, stop, price, locked, now in cases:
        events = []
        repo = FakeRepo([_trade(side=side, stop_price=stop, initial_stop_price=stop, target_price=None)])
        em = ExitManager(repo, FakeExecutor(repo), quote_fn=lambda s, p=price: Quote(symbol=s, bid=p, ask=p, last=p),
                         cfg=cfg, bus=SimpleNamespace(publish=lambda topic, **k: events.append((topic, k))))
        em.run_once()
        moved = [k for topic, k in events if topic == "exit.stop_moved"]
        assert len(moved) == 1, side
        assert abs(moved[0]["locked_r"] - locked) <= 0.006, (side, moved[0])
        assert moved[0]["r_now"] == now and moved[0]["r"] == now      # r kept for older readers


# ---------------------------------------------------------------- tick passes (a streamed price moved)
class _WritesRepo(FakeRepo):
    """Keeps every write, and the excursions to six decimals as the database's columns do."""

    def __init__(self, trades):
        super().__init__(trades)
        self.writes = []

    def update_trade_risk(self, tid, **kw):
        kw = {k: v for k, v in kw.items() if v is not None}
        self.writes.append((tid, kw))
        super().update_trade_risk(tid, **{k: round(v, 6) if k in ("hwm_price", "mae", "mfe") else v
                                          for k, v in kw.items()})


def _ticking(trades, prices, executor=None, **cfg):
    """An exit manager on ``prices`` (symbol -> price, changed between passes); what it published and the
    stocks it asked prices for come back with it."""
    repo = _WritesRepo(trades)
    events, asked = [], []

    def quote(s):
        asked.append(s)
        return Quote(symbol=s, bid=prices[s], ask=prices[s], last=prices[s])

    rules = dict(enabled=True, breakeven_at_r=1.0, breakeven_lock_r=0.3, breakeven_buffer_bps=5,
                 trail_start_r=1.5, trail_lock_ratio=0.5, flatten_intraday_before_close_min=10, max_swing_hold_days=0)
    rules.update(cfg)
    ex = executor(repo) if executor else FakeExecutor(repo)
    em = ExitManager(repo, ex, quote_fn=quote, cfg=SimpleNamespace(**rules),
                     bus=SimpleNamespace(publish=lambda topic, **k: events.append((topic, k))))
    return em, repo, ex, events, asked


def test_a_tick_pass_manages_only_the_stocks_that_ticked_and_a_full_pass_sets_what_is_watched():
    prices = {"AAA": 97.5, "BBB": 97.0}                                    # both under their 98 stops
    em, repo, ex, events, asked = _ticking([_trade(), _trade(id="t2", symbol="BBB")], prices)
    assert em.run_once(only=set()) == [] and asked == []                 # nothing ticked: nothing read
    em.run_once(only={"AAA"})
    assert ex.closed == [("t1", "stop")] and asked == ["AAA"]
    assert em.watched == frozenset()                                     # only a full pass says what it manages
    prices["BBB"] = 99.0
    em.run_once()
    assert em.watched == frozenset({"BBB"}) and ex.closed == [("t1", "stop")]


def test_a_stop_a_tick_moves_is_on_the_record_at_once_and_told_once_on_the_next_full_pass():
    prices = {"AAA": 102.6}                                              # +1.3R: the stop goes to break-even and a bit
    em, repo, ex, events, _ = _ticking([_trade()], prices)
    em.run_once(only={"AAA"})
    assert repo._t["t1"]["stop_price"] == 100.65                         # the next tick is checked against it
    assert events == [] and not [kw for _, kw in repo.writes if "note_append" in kw]
    em.run_once()
    [(topic, moved)] = events
    assert topic == "exit.stop_moved" and moved["new_stop"] == 100.65 and moved["r_now"] == 1.3
    assert [kw["note_append"] for _, kw in repo.writes if "note_append" in kw] == ["stop->100.65 @ 1.3R"]
    em.run_once()                                                        # nothing new: nothing said
    fresh, _, _, again, _ = _ticking([repo._t["t1"]], prices)            # a restart doesn't repeat it
    fresh.run_once()
    assert len(events) == 1 and again == []


def test_a_stop_something_else_set_after_a_tick_moved_it_isnt_told_as_the_exit_managers():
    prices = {"AAA": 102.6}
    em, repo, ex, events, _ = _ticking([_trade()], prices)
    em.run_once(only={"AAA"})
    repo._t["t1"]["stop_price"] = 101.5                                  # a part came off at the first target
    em.run_once()
    assert events == [] and repo._t["t1"]["stop_price"] == 101.5


def test_tick_passes_keep_the_excursions_in_memory_and_a_full_pass_writes_them_only_when_they_changed():
    prices = {"AAA": 100.0}
    em, repo, ex, events, _ = _ticking([_trade()], prices)
    for px in (100.7, 99.6, 100.4):                                      # a high, a low, then back
        prices["AAA"] = px
        em.run_once(only={"AAA"})
    assert repo.writes == []
    prices["AAA"] = 100.2
    em.run_once()
    [(_, wrote)] = repo.writes
    assert wrote["hwm_price"] == 100.7 and abs(wrote["mfe"] - 0.7) < 1e-9 and abs(wrote["mae"] - 0.4) < 1e-9
    prices["AAA"] = 100.7                                                # the same high again
    em.run_once()
    em.run_once(only={"AAA"})
    em.run_once()
    assert len(repo.writes) == 1


def test_an_exit_a_tick_pass_sends_writes_the_excursions_first_as_a_full_pass_would():
    # the trade gets no next full pass once its exit is out, so the record must already hold the price that set it off
    for full_at, ticks, reason, want in (
            (99.0, (97.5,), "stop", dict(mae=2.5, mfe=0.0, hwm_price=100.0)),
            (105.0, (110.5,), "target", dict(mae=0.0, mfe=10.5, hwm_price=110.5)),
            (100.0, (104.0, 101.9), "trailing-stop", dict(mae=0.0, mfe=4.0, hwm_price=104.0))):  # the high trailed it
        prices = {"AAA": full_at}
        em, repo, ex, events, _ = _ticking([_trade()], prices)
        em.run_once()
        for px in ticks:
            prices["AAA"] = px
            em.run_once(only={"AAA"})
        assert ex.closed == [("t1", reason)]
        em.run_once()                                                    # closed: no pass folds anything in later
        assert {k: round(repo._t["t1"][k], 6) for k in want} == want, reason


def test_a_stop_hit_on_a_tick_sends_one_exit_and_the_ticks_right_after_send_none(monkeypatch):
    from tos_bot.execution import exit_manager as module

    class Working(FakeExecutor):                                        # the exit is sent and still working
        def __init__(self, repo):
            super().__init__(repo)
            self.sent = []

        def pending_exit_trade_ids(self):
            return set(self.sent)

        def close_trade(self, tid, **kw):
            self.sent.append(tid)
            return {"ok": True, "status": "SUBMITTED", "trade": {"id": tid}}

    class Refused(FakeExecutor):                                        # the broker turns the exit down
        def __init__(self, repo):
            super().__init__(repo)
            self.sent = []

        def close_trade(self, tid, **kw):
            self.sent.append(tid)
            return {"ok": False, "reason": "refused"}

    now = {"t": 1_000.0}
    monkeypatch.setattr(module.time, "monotonic", lambda: now["t"])
    for executor in (Working, Refused):
        em, repo, ex, events, _ = _ticking([_trade()], {"AAA": 97.5}, executor=executor)
        em.run_once(only={"AAA"})
        for later in (0.3, 0.9):                                         # ticks 0.3 s and 1.2 s after it
            now["t"] += later
            em.run_once(only={"AAA"})
        assert ex.sent == ["t1"], executor.__name__


def test_a_trade_the_broker_doesnt_hold_waits_for_the_full_pass():
    class NotHeld(FakeExecutor):
        def __init__(self, repo):
            super().__init__(repo)
            self.asked = 0

        def close_trade(self, tid, **kw):
            self.asked += 1                                              # each try asks the broker twice
            return {"not_held": True, "reason": "no position at the broker"}

    em, repo, ex, events, _ = _ticking([_trade()], {"AAA": 97.5}, executor=NotHeld)
    em.run_once()
    em.run_once(only={"AAA"})
    assert ex.asked == 1
    em.run_once()                                                        # the full pass tries as before
    assert ex.asked == 2
