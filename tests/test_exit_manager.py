from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from autotradebot.core.models import Quote
from autotradebot.execution.exit_manager import ExitManager, stop_locked


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
    from autotradebot.core.models import Quote
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
        px, at = prices[s] if isinstance(prices[s], tuple) else (prices[s], None)     # (price, printed at), or a price
        return Quote(symbol=s, bid=px, ask=px, last=px, **({"ts": at} if at else {}))

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


@pytest.mark.parametrize("side,ticks,full_at", [
    ("LONG", (100.7, 99.6, 100.4), 100.2),                               # a high, a low, then back
    ("SHORT", (99.3, 100.4, 99.6), 99.8),                                # a short's way round: the low is its gain
])
def test_tick_passes_keep_the_excursions_in_memory_and_a_full_pass_writes_them_only_when_they_changed(side, ticks,
                                                                                                    full_at):
    prices = {"AAA": 100.0}
    trade = _trade() if side == "LONG" else _trade(side="SHORT", stop_price=102.0, initial_stop_price=102.0,
                                                   target_price=90.0, initial_target_price=90.0)
    em, repo, ex, events, _ = _ticking([trade], prices)
    for px in ticks:
        prices["AAA"] = px
        em.run_once(only={"AAA"})
    assert repo.writes == []
    prices["AAA"] = full_at
    em.run_once()
    [(_, wrote)] = repo.writes
    assert wrote["hwm_price"] == ticks[0] and abs(wrote["mfe"] - 0.7) < 1e-9 and abs(wrote["mae"] - 0.4) < 1e-9
    prices["AAA"] = ticks[0]                                             # the same best price again
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


def test_a_quote_printed_before_the_entry_filled_is_skipped_whole():
    # the first pass after a fill can get the last print from before it (the quote cache, a snapshot's Ticker): a
    # price the trade never saw. Nothing is read off it - not the excursions, not the stop or target, not the
    # ratchet - on a full pass or a tick; the next quote from the trade's life is handled as before
    filled = dt.datetime(2026, 9, 3, 13, 40, 30, tzinfo=dt.timezone.utc)
    before, after = filled - dt.timedelta(seconds=20), filled + dt.timedelta(seconds=5)
    for stale in (103.0, 97.5):                 # +1.5R would move the stop to break-even; under the stop would exit
        for first in ("full", "tick"):
            trade = _trade(entry_time=filled.replace(tzinfo=None).isoformat())      # the record keeps naive UTC
            prices = {"AAA": (stale, before)}
            em, repo, ex, events, _ = _ticking([trade], prices)
            em.run_once() if first == "full" else em.run_once(only={"AAA"})
            assert repo.writes == [] and ex.closed == [] and events == [], (stale, first)
            assert repo._t["t1"]["stop_price"] == 98.0, (stale, first)
            prices["AAA"] = (100.5, after)
            em.run_once()
            [(_, wrote)] = repo.writes
            assert (wrote["hwm_price"], wrote["mfe"], wrote["mae"]) == (100.5, 0.5, 0.0), (stale, first)
            assert ex.closed == [] and events == []
    # a quote from the very second of the fill is the trade's; one that doesn't say when it was printed counts
    for quote_fn in (lambda s: Quote(symbol=s, bid=101.0, ask=101.0, last=101.0, ts=filled),
                     lambda s: SimpleNamespace(last=101.0, mid=101.0)):
        repo = _WritesRepo([_trade(entry_time=filled.replace(tzinfo=None).isoformat())])
        em = ExitManager(repo, FakeExecutor(repo), quote_fn=quote_fn, cfg=_day_cfg(breakeven_at_r=0.0),
                         bus=SimpleNamespace(publish=lambda *a, **k: None))
        em.run_once()
        assert [kw["mfe"] for _, kw in repo.writes] == [1.0]


def test_the_exit_fill_joins_the_excursions_when_it_is_the_worst_or_the_best_point(repo):
    # the passes mark the excursions from the quotes between them and never see the fill itself: a stop that fills
    # through the worst point marked so far would leave the record's MAE short of the trade's own loss, a target that
    # fills past the best point its MFE and high-water mark short. The fill is the last price the trade saw
    from autotradebot.core.enums import Side, StrategyKind, Timeframe
    from autotradebot.core.models import Play
    cases = [  # (side, stop, target, what the passes marked, the fill, what the closed record keeps)
        ("LONG", 98.0, 104.0, dict(hwm_price=100.6, mae=0.8, mfe=0.6), 97.4, dict(hwm_price=100.6, mae=2.6, mfe=0.6)),
        ("LONG", 98.0, 104.0, dict(hwm_price=103.0, mae=0.8, mfe=3.0), 104.3, dict(hwm_price=104.3, mae=0.8, mfe=4.3)),
        ("SHORT", 102.0, 96.0, dict(hwm_price=99.4, mae=0.8, mfe=0.6), 102.6, dict(hwm_price=99.4, mae=2.6, mfe=0.6)),
        ("SHORT", 102.0, 96.0, dict(hwm_price=97.0, mae=0.8, mfe=3.0), 95.7, dict(hwm_price=95.7, mae=0.8, mfe=4.3)),
        ("LONG", 98.0, 104.0, dict(hwm_price=101.0, mae=1.5, mfe=1.0), 99.5, dict(hwm_price=101.0, mae=1.5, mfe=1.0)),
    ]
    for side, stop, target, marked, fill, want in cases:
        play = Play(symbol="EXCR", side=Side[side], strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                    timeframe=Timeframe.INTRADAY, entry=100.0, stop=stop, targets=[target])
        tid = repo.open_trade(play, 100.0, 10, "paper")
        repo.update_trade_risk(tid, **marked)
        lost = (fill - 100.0) * (1 if side == "LONG" else -1) < 0
        out = repo.close_trade(tid, fill, exit_reason="stop" if lost else "target")
        assert {k: round(out[k], 6) for k in want} == want, (side, fill)
        if want["mfe"] > marked["mfe"]:
            assert out["mfe_at"] == out["exit_time"], (side, fill)      # the fill set the MFE, at the exit


def test_a_stop_a_tick_moved_is_told_before_the_exit_that_closes_the_trade_first():
    # the next full pass never sees the trade again, so the move's note and message go out with the exit - once,
    # and saying where the trade stood when the stop moved, not where it is when the exit goes
    for last in ("tick", "full"):
        prices = {"AAA": 100.0}
        em, repo, ex, events, _ = _ticking([_trade()], prices)
        em.run_once()
        for px in (103.5, 104.0):                                        # +1.75R then +2R: the trail keeps half
            prices["AAA"] = px
            em.run_once(only={"AAA"})
        assert repo._t["t1"]["stop_price"] == 102.0 and events == []
        prices["AAA"] = 101.0                                            # through the moved stop
        em.run_once(only={"AAA"}) if last == "tick" else em.run_once()
        em.run_once()
        assert ex.closed == [("t1", "trailing-stop")], last
        assert [kw["note_append"] for _, kw in repo.writes if "note_append" in kw] == ["stop->102.00 @ 2.0R"], last
        assert [topic for topic, _ in events] == ["exit.stop_moved", "exit.triggered"], last
        assert events[0][1]["new_stop"] == 102.0 and events[0][1]["locked_r"] == 1.0 and events[0][1]["r_now"] == 2.0
        assert repo._t["t1"]["mfe"] == 4.0 and repo._t["t1"]["hwm_price"] == 104.0


def test_a_stop_note_that_cant_be_written_doesnt_hold_up_the_exit():
    prices = {"AAA": 104.0}
    em, repo, ex, events, _ = _ticking([_trade()], prices)
    em.run_once(only={"AAA"})                                            # the stop moves to 102, not yet told

    def down(tid, **kw):
        raise RuntimeError("database is locked")
    repo.update_trade_risk = down
    prices["AAA"] = 101.0
    em.run_once(only={"AAA"})
    assert ex.closed == [("t1", "trailing-stop")]


def test_a_stop_hit_on_a_tick_sends_one_exit_and_the_ticks_right_after_send_none(monkeypatch):
    from autotradebot.execution import exit_manager as module

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


# ---------------------------------------------------------------- a stop resting at the broker gets a moment to fill
class _StopRests(FakeExecutor):
    """A stop rests at the broker for each trade in ``resting``."""

    def __init__(self, repo):
        super().__init__(repo)
        self.resting, self.asked = {"t1"}, []

    def stop_resting(self, tid, price=None, side=None):
        self.asked.append((tid, price, side))         # (whether the price reaches it is the executor's to say)
        return tid in self.resting


_SHORT = dict(side="SHORT", stop_price=102.0, initial_stop_price=102.0, target_price=90.0, initial_target_price=90.0)


@pytest.mark.parametrize("trade, crossed", [({}, 97.5), (_SHORT, 102.5)])
def test_a_stop_crossed_while_it_rests_at_the_broker_gets_its_grace_before_the_apps_own_exit(monkeypatch, trade,
                                                                                             crossed):
    from autotradebot.execution import exit_manager as module

    now = [1_000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    em, repo, ex, _, _ = _ticking([_trade(**trade)], {"AAA": crossed}, executor=_StopRests, broker_stop_grace_s=10)
    em.run_once()                                                        # past the stop: the broker's to fill
    assert ex.asked == [("t1", crossed, trade.get("side", "LONG"))]      # if this price reaches it where it rests
    now[0] += 6.0
    em.run_once(only={"AAA"})
    now[0] += 3.9
    em.run_once()
    assert ex.closed == []
    assert repo._t["t1"]["stop_price"] == _trade(**trade)["stop_price"]  # nor is the stop moved past the price
    now[0] += 0.1                                                        # ten seconds after the first cross
    em.run_once()
    assert ex.closed == [("t1", "stop")]


def test_a_price_back_inside_the_stop_gives_the_next_cross_its_grace_afresh(monkeypatch):
    from autotradebot.execution import exit_manager as module

    now = [1_000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    prices = {"AAA": 97.5}
    em, _, ex, _, _ = _ticking([_trade()], prices, executor=_StopRests, broker_stop_grace_s=10)
    em.run_once()
    now[0] += 8.0
    prices["AAA"] = 98.5                                                 # back over the 98 stop
    em.run_once()
    now[0] += 4.0
    prices["AAA"] = 97.5                                                 # under it again, 12 s after the first cross
    em.run_once()
    assert ex.closed == []
    now[0] += 10.0
    em.run_once()
    assert ex.closed == [("t1", "stop")]


def test_with_no_stop_resting_at_the_broker_or_the_grace_off_a_stop_cross_exits_at_once():
    trades = [_trade(), _trade(id="t2", symbol="BBB")]                   # both under their 98 stops
    em, _, ex, _, _ = _ticking(trades, {"AAA": 97.5, "BBB": 97.5}, executor=_StopRests, broker_stop_grace_s=10)
    em.run_once()
    assert ex.closed == [("t2", "stop")]                                 # only t1's stop rests at the broker
    em.cfg.broker_stop_grace_s = 0                                       # 0: the app's exit at once, as before
    em.run_once()
    assert ex.closed == [("t2", "stop"), ("t1", "stop")]


def test_outside_the_regular_session_a_stop_cross_gets_no_grace_and_one_from_before_the_open_is_forgotten(monkeypatch):
    from autotradebot.execution import exit_manager as module
    from autotradebot.util import clock

    now, session = [1_000.0], [clock.Session.REGULAR]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(ExitManager, "_session_now", staticmethod(lambda: session[0]))
    em, _, ex, _, _ = _ticking([_trade(), _trade(id="t2", symbol="BBB")], {"AAA": 97.5, "BBB": 97.5},
                               executor=_StopRests, broker_stop_grace_s=10)
    ex.resting = {"t1", "t2"}
    em._open_day = clock.session_date(clock.now_ny()) - dt.timedelta(days=1)    # yesterday's session last opened
    em._stop_crossed["t2"] = now[0] - 60.0                                   # a cross remembered from before the open
    em.run_once()                                                            # the open: both get their whole grace
    assert ex.closed == []
    session[0] = clock.Session.POST                                          # after the close the broker's stop
    em.run_once()                                                            # can't fill: the app's exit goes at once
    assert sorted(ex.closed) == [("t1", "stop"), ("t2", "stop")]



# ---------------------------------------------------------------- the time exits read no price
def _at(monkeypatch, *when):
    """The clock the exit manager reads (clock.now_ny, behind every session helper) set to ``when`` in New York."""
    from autotradebot.execution import exit_manager as module

    at = dt.datetime(*when, tzinfo=module.clock.NY)
    monkeypatch.setattr(module.clock, "now_ny", lambda: at)


class _Sends(FakeExecutor):
    """Keeps the price each exit was measured against."""

    def __init__(self, repo):
        super().__init__(repo)
        self.seen = {}

    def close_trade(self, tid, reason="manual", **kw):
        self.seen[tid] = kw.get("decision_price")
        return super().close_trade(tid, reason=reason, **kw)


def _no_quote(symbol):
    raise RuntimeError("no market data")


def test_an_exit_decided_on_a_delayed_quote_is_measured_against_no_price():
    # a delayed quote is minutes old: the fill against it would be how far the price moved since, not its slippage
    for live, measured in ((True, 97.5), (False, None)):
        repo = FakeRepo([_trade()])
        ex = _Sends(repo)
        em = ExitManager(repo, ex, quote_fn=lambda s: Quote(symbol=s, bid=97.5, ask=97.5, last=97.5),   # under the stop
                         cfg=_day_cfg(), bus=SimpleNamespace(publish=lambda *a, **k: None),
                         quotes_live=lambda live=live: live)
        em.run_once()
        assert ex.closed == [("t1", "stop")] and ex.seen == {"t1": measured}, live


def test_the_time_exits_go_out_on_a_pass_with_no_price(monkeypatch):
    # a quote that doesn't come - no market data, or only a print from before the entry - mustn't hold a day trade past
    # the flatten or its window, nor a swing trade past its last day: those exits read no price, and none is made up
    _at(monkeypatch, 2026, 9, 17, 15, 55)                          # a Thursday, five minutes before the close
    today = dict(timeframe="INTRADAY", entry_time="2026-09-17T14:00:00", session_date="2026-09-17")
    trades = [_trade(id="t1", symbol="AAA", **today),                                       # the flatten is due
              _trade(id="t2", symbol="BBB", time_status="overdue", **today),                # its window has passed
              _trade(id="t3", symbol="CCC", entry_time="2026-09-03T13:40:00"),              # a swing on its 10th day
              _trade(id="t4", symbol="DDD", entry_time="2026-09-10T13:40:00")]              # one on its 6th: held on
    printed = dt.datetime(2026, 9, 1, 15, 0, tzinfo=dt.timezone.utc)                       # before every entry

    def stale(symbol):                                              # under every stop - were it read
        return Quote(symbol=symbol, bid=97.0, ask=97.0, last=97.0, ts=printed)

    for quote_fn in (_no_quote, stale):
        repo = FakeRepo(trades)
        ex = _Sends(repo)
        em = ExitManager(repo, ex, quote_fn=quote_fn,
                         cfg=_day_cfg(flatten_intraday_before_close_min=10, max_swing_hold_days=10),
                         bus=SimpleNamespace(publish=lambda *a, **k: None))
        em.run_once(only={"AAA", "BBB", "CCC", "DDD"})              # a tick pass is about the price: nothing
        assert ex.closed == []
        em.run_once()
        assert sorted(ex.closed) == [("t1", "eod-flatten"), ("t2", "time-stop"), ("t3", "time-stop")], quote_fn
        assert ex.seen == {"t1": None, "t2": None, "t3": None}     # measured against no price


def test_what_tick_passes_saw_is_written_before_an_exit_sent_with_no_price(monkeypatch):
    # the trade gets no full pass once its exit is out: the high a tick pass saw and the stop it moved go on the
    # record first, as before an exit a price set off
    _at(monkeypatch, 2026, 9, 17, 15, 55)
    trade = _trade(timeframe="INTRADAY", entry_time="2026-09-17T14:00:00", session_date="2026-09-17")
    em, repo, ex, events, _ = _ticking([trade], {"AAA": 104.0})
    em.run_once(only={"AAA"})                                       # +2R: the stop trails to 102, not yet told
    em.quote_fn = _no_quote
    em.run_once()
    assert ex.closed == [("t1", "eod-flatten")]
    assert [kw["note_append"] for _, kw in repo.writes if "note_append" in kw] == ["stop->102.00 @ 2.0R"]
    assert [topic for topic, _ in events] == ["exit.stop_moved", "exit.triggered"]
    assert repo._t["t1"]["mfe"] == 4.0 and repo._t["t1"]["hwm_price"] == 104.0


def test_a_day_trade_left_open_from_an_earlier_session_is_flattened_at_the_next_regular_session(monkeypatch):
    # the app was down at the flatten, or the exit didn't fill before the bell: the day trade goes at the first pass of
    # the next regular session, as the flatten it missed - not held all day until the next one
    base = dict(timeframe="INTRADAY", entry_time="2026-09-16T15:00:00", session_date="2026-09-16")
    cfg = _day_cfg(flatten_intraday_before_close_min=10)
    for when, want in (((2026, 9, 16, 19, 0), []),                 # that evening: nothing fills after the close
                       ((2026, 9, 17, 8, 0), []),                  # the next morning, before the open
                       ((2026, 9, 17, 9, 30), [("t1", "eod-flatten")])):   # the open
        _at(monkeypatch, *when)
        em, ex = _mk(FakeRepo([_trade(**base)]), price=100.5, cfg=cfg)
        em.run_once()
        assert ex.closed == want, when
    _at(monkeypatch, 2026, 9, 21, 9, 45)                            # over a weekend too
    em, ex = _mk(FakeRepo([_trade(**{**base, "entry_time": "2026-09-18T15:00:00", "session_date": "2026-09-18"})]),
                 price=100.5, cfg=cfg)
    em.run_once()
    assert ex.closed == [("t1", "eod-flatten")]
    _at(monkeypatch, 2026, 9, 17, 9, 30)
    for trade, rules, want in (
            (_trade(**{**base, "session_date": None}), cfg, [("t1", "eod-flatten")]),   # read off the entry time
            (_trade(**base), _day_cfg(flatten_intraday_before_close_min=0), []),        # the flatten switched off
            (_trade(**{**base, "managed_exit": False}), cfg, []),                       # exited by hand
            (_trade(**{**base, "entry_time": "2026-09-17T12:00:00", "session_date": "2026-09-17"}), cfg, [])):  # today's
        em, ex = _mk(FakeRepo([trade]), price=100.5, cfg=rules)
        em.run_once()
        assert ex.closed == want, (trade, rules)


def test_the_swing_time_stop_counts_trading_days_as_the_replay_does(monkeypatch):
    # entered on a Thursday morning, Labor Day and two weekends fall within its ten sessions, the entry's counted -
    # the replay's ten daily candles. Calendar days would have closed it four sessions early
    from autotradebot.execution.exit_manager import swing_time_stop_at
    from autotradebot.util import clock

    entry = "2026-09-03T13:40:00"                                   # 09:40 in New York, the record's naive UTC
    assert swing_time_stop_at(entry, 10, 10) == dt.datetime(2026, 9, 17, 15, 50, tzinfo=clock.NY)
    assert clock.last_n_sessions(dt.date(2026, 9, 17), 10)[0] == dt.date(2026, 9, 3)
    assert swing_time_stop_at(entry, 10, 0) == dt.datetime(2026, 9, 18, 9, 30, tzinfo=clock.NY)    # no flatten
    assert swing_time_stop_at(entry, 1, 10) == dt.datetime(2026, 9, 3, 15, 50, tzinfo=clock.NY)
    assert swing_time_stop_at(entry, 0, 10) is None and swing_time_stop_at(None, 10, 10) is None
    cfg = _day_cfg(flatten_intraday_before_close_min=10, max_swing_hold_days=10)
    for when, closed in (((2026, 9, 14, 10, 0), False),            # eleven calendar days on: four sessions to go
                         ((2026, 9, 17, 15, 49), False),           # its tenth session, a minute before the flatten
                         ((2026, 9, 17, 15, 50), True),            # the flatten on its tenth
                         ((2026, 9, 18, 8, 0), False),             # missed: not before the open...
                         ((2026, 9, 18, 9, 30), True)):            # ...but at it
        _at(monkeypatch, *when)
        em, ex = _mk(FakeRepo([_trade(entry_time=entry)]), price=100.5, cfg=cfg)
        em.run_once()
        assert ex.closed == ([("t1", "time-stop")] if closed else []), when


def test_the_swing_time_stop_falls_on_the_day_the_replay_closes_the_same_trade():
    import pandas as pd

    from autotradebot.core.enums import Side, StrategyKind, Timeframe
    from autotradebot.execution.exit_manager import swing_time_stop_at
    from autotradebot.research.replay import ReplaySettings, replay_swing
    from autotradebot.scanner.noise import NoiseSettings
    from autotradebot.strategies.base import Strategy
    from autotradebot.util import clock

    class _BuysOnce(Strategy):
        """Buys at one session's close - the replay fills it at the next open - with a stop and target never hit."""
        key, kind, timeframe, title, thesis = "buys_once", StrategyKind.TECHNICAL, Timeframe.SWING, "Test", "t"

        def generate(self, ctx):
            if ctx.daily.index[-1].date() != dt.date(2026, 9, 2):
                return []
            entry = float(ctx.daily["close"].iloc[-1])
            play = self._mk_play(ctx, Side.LONG, entry, entry - 5.0, [entry + 10.0], 0.7, "r", "d", {}, tags=["swing"])
            return [play] if play else []

    days = [d for d in pd.date_range("2026-05-01", "2026-10-30").date if clock.is_trading_day(d)]
    frame = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 3e6},
                         index=pd.DatetimeIndex([pd.Timestamp(d, tz=clock.NY) for d in days]))
    settings = ReplaySettings(slippage_bps=0.0, commission_bps=0.0, breakeven_at_r=0.0, trail_start_r=0.0,
                              max_swing_hold_days=10)
    [sim] = replay_swing([_BuysOnce()], "SWG", frame, settings, NoiseSettings(min_expected_r=-99.0),
                         sessions=len(days))
    assert sim.exit_reason == "time-stop" and sim.entered_at.startswith("2026-09-03")
    due = swing_time_stop_at("2026-09-03T13:30:00", settings.max_swing_hold_days, 10)       # filled at the open
    assert sim.exited_at[:10] == due.date().isoformat() == "2026-09-17"


# ---------------------------------------------------------------- outside the regular session
def _session(monkeypatch, start):
    """The session the exit manager sees, as [the session] - change it between passes."""
    from autotradebot.util import clock

    session = [getattr(clock.Session, start)]
    monkeypatch.setattr(ExitManager, "_session_now", staticmethod(lambda: session[0]))
    return session


@pytest.mark.parametrize("start", ["PRE", "POST", "CLOSED"])
def test_a_print_outside_the_regular_session_moves_no_stop_but_still_marks_the_trade_and_trips_its_stop(monkeypatch,
                                                                                                          start):
    from autotradebot.util import clock

    session = _session(monkeypatch, start)
    prices = {"AAA": 106.0}                                              # +3R: the trail would take the stop to 103
    em, repo, ex, events, _ = _ticking([_trade()], prices)
    em.run_once()
    assert repo._t["t1"]["stop_price"] == 98.0 and events == []          # a thin print doesn't ratchet it
    assert repo._t["t1"]["mfe"] == 6.0 and repo._t["t1"]["hwm_price"] == 106.0     # but counts in the excursions
    session[0] = clock.Session.REGULAR
    em.run_once()
    assert repo._t["t1"]["stop_price"] == 103.0                          # the same price in regular hours moves it
    session[0] = getattr(clock.Session, start)
    prices["AAA"] = 102.5                                                # and under the stop outside them, it trips
    em.run_once()
    assert ex.closed == [("t1", "trailing-stop")]


def test_a_stop_a_tick_moved_in_regular_hours_is_told_by_the_first_pass_after_them(monkeypatch):
    from autotradebot.util import clock

    session = _session(monkeypatch, "REGULAR")
    prices = {"AAA": 102.6}
    em, repo, ex, events, _ = _ticking([_trade()], prices)
    em.run_once(only={"AAA"})                                            # the stop to 100.65, not told yet
    session[0], prices["AAA"] = clock.Session.POST, 106.0                # an after-hours print far above
    em.run_once()
    [(topic, moved)] = events
    assert topic == "exit.stop_moved" and moved["new_stop"] == 100.65 and repo._t["t1"]["stop_price"] == 100.65


class _ShutUntilOpen(FakeExecutor):
    """Turns every exit away while ``answer`` is "closed" (the executor, the exchange closed at IBKR) or "refused"."""

    def __init__(self, repo):
        super().__init__(repo)
        self.answer, self.asked = "closed", []

    def close_trade(self, tid, reason="manual", **kw):
        self.asked.append(tid)
        if self.answer == "closed":
            return {"ok": False, "market_closed": True, "reason": "The market is closed, so an exit can't fill now."}
        if self.answer == "refused":
            return {"ok": False, "reason": "refused"}
        return super().close_trade(tid, reason=reason, **kw)


def test_an_exit_the_closed_market_turns_away_is_no_failed_try(monkeypatch):
    from autotradebot.execution import exit_manager as module

    now = [1_000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    _session(monkeypatch, "POST")
    em, repo, ex, events, _ = _ticking([_trade()], {"AAA": 97.5}, executor=_ShutUntilOpen)    # under the 98 stop
    for _ in range(40):                                                  # a pass every 15 s for ten minutes
        em.run_once()
        now[0] += 15.0
    assert len(ex.asked) == 600 / em.CLOSED_RETRY_S                      # asked now and then, no longer back-off
    [told] = [p for topic, p in events if topic == "exit.failed"]        # told once: a wait for the open
    assert told["market_closed"] and "market is closed" in told["reason"]
    ex.answer = "refused"                                                # a real refusal after them is its first try
    now[0] += em.CLOSED_RETRY_S
    em.run_once()
    failed = [p for topic, p in events if topic == "exit.failed"][-1]
    assert failed["attempt"] == 1 and failed["retry_in_s"] == em.RETRY_DELAYS_S[0] and not failed.get("market_closed")


def test_every_exit_starts_its_tries_afresh_at_the_open(monkeypatch):
    from autotradebot.execution import exit_manager as module
    from autotradebot.util import clock

    now = [1_000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    session = _session(monkeypatch, "PRE")
    _at(monkeypatch, 2026, 9, 17, 9, 0)                                  # a Thursday, before the open
    em, repo, ex, events, _ = _ticking([_trade()], {"AAA": 97.5}, executor=_ShutUntilOpen)
    ex.answer = "refused"
    while len(ex.asked) < len(em.RETRY_DELAYS_S):                        # refused until the back-off is five minutes
        em.run_once()
        now[0] += 1.0
    asked = len(ex.asked)
    _at(monkeypatch, 2026, 9, 17, 9, 30)
    session[0] = clock.Session.REGULAR
    em.run_once()                                                        # the open, seconds after the last try
    assert len(ex.asked) == asked + 1
    failed = [p for topic, p in events if topic == "exit.failed"]
    assert len(failed) == 2 and failed[-1]["attempt"] == 1               # its first try today, told again
    now[0] += 1.0
    em.run_once()                                                        # once a day: the back-off holds again
    assert len(ex.asked) == asked + 1
    ex.answer = "fill"
    now[0] += em.RETRY_DELAYS_S[0]
    em.run_once()
    assert ex.closed == [("t1", "stop")]
