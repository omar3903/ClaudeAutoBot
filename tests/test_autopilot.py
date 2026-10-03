"""AutoPilot - hands-off entry gate.

Exits are already automatic (ExitManager); these tests pin down when the pilot
will and will not take the *entry*: trade-type filter, confidence / RR floors,
position + per-day caps, the paper-only-in-live hard gate, and dry-run.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from autotradebot.core.enums import AssetClass, PlayStatus, Side, StrategyKind, Timeframe
from autotradebot.core.models import Account, Play, Position
from autotradebot.execution.autopilot import AutoPilot

SILENT = SimpleNamespace(publish=lambda *a, **k: None)


# --------------------------------------------------------------------------- #
#  test doubles
# --------------------------------------------------------------------------- #
class FakeRepo:
    def __init__(self):
        self._open = []
        self.held = set()
        self._closed = []                       # for cooldown_after_loss

    def open_trades(self):
        return list(self._open)

    def recent_trades(self, limit=100):
        return list(self._closed)[:limit]

    def trades_on(self, day):
        return list(self._closed)

    def get_open_trade_for_symbol(self, sym):
        return {"symbol": sym} if sym in self.held else None


class FakeEngine:
    def __init__(self, mode="paper", equity=100_000.0):
        self.mode = mode
        self.repo = FakeRepo()
        self._account = Account(account_id="SIM", equity=equity)
        self.working = []                       # entry orders sent but not filled
        self.fill_later = False                 # approve_play leaves the entry working
        self.assess_calls = []
        self.approved = []
        self.can_execute = True
        self.est_risk = 200.0
        self.gross = 0.0                        # dollars already in positions
        self.records = {}                       # replayed strategy records
        self.live = {}                          # closed real trades by strategy (live_stats)
        self._plays = {}                        # pid -> Play, populated by _run

    def assess_play(self, pid):
        self.assess_calls.append(pid)
        return {
            "ok": True,
            "can_execute": self.can_execute,
            "reasons": [] if self.can_execute else ["not executable in this session"],
            "order_preview": {"qty": 10, "est_risk": self.est_risk, "est_cost": 1_000.0},
        }

    def gross_exposure(self):
        return self.gross

    def strategy_record(self, key):
        return self.records.get(key)

    def live_stats(self):
        return self.live

    def approve_play(self, pid, operator="operator"):
        self.approved.append((pid, operator))
        tid = f"trade_{pid}"
        pl = self._plays.get(pid)
        if self.fill_later:
            self.working.append({"order_id": f"o_{pid}", "play_id": pid, "symbol": pl.symbol,
                                 "strategy": pl.strategy, "timeframe": pl.timeframe.value, "qty": 10,
                                 "risk": self.est_risk})
            return {"ok": True, "status": "WORKING"}
        # an open position whose $-risk equals est_risk (entry 100, stop 80, x10)
        self.repo._open.append({
            "id": tid, "symbol": getattr(pl, "symbol", "X"),
            "strategy": getattr(pl, "strategy", "opening_range_breakout"),
            "timeframe": getattr(getattr(pl, "timeframe", None), "value", "INTRADAY"),
            "entry_price": 100.0,
            "initial_stop_price": 100.0 - self.est_risk / 10.0, "quantity": 10,
        })
        return {"ok": True, "trade_id": tid}

    def working_entries(self):
        return list(self.working)

    def approved_ids(self):
        return [pid for pid, _ in self.approved]


def _cfg(**over):
    base = dict(
        enabled=True, allow_live=False, trade_types=["INTRADAY"],
        min_confidence=0.6, min_reward_risk=2.0, max_auto_positions=2,
        max_auto_trades_per_day=3, max_open_risk_pct=4.0,
        require_catalyst=False, dry_run=False,
        # cap tests below isolate one cap at a time; keep these wide open
        max_per_strategy=99, max_new_per_cycle=99, cooldown_after_loss=False,
        min_confirmations=1, skip_noise=[], require_proven=False,
        min_minutes_to_close=0,             # the clock-of-day rule has a test of its own
    )
    base.update(over)
    return SimpleNamespace(**base)


def mkplay(sym="AAA", *, tf=Timeframe.INTRADAY, conf=0.75, entry=100.0, stop=98.0,
           target=106.0, kind=StrategyKind.TECHNICAL, sector="Technology", tags=None):
    return Play(
        symbol=sym, side=Side.LONG, strategy="opening_range_breakout", kind=kind,
        timeframe=tf, entry=entry, stop=stop, targets=[target], confidence=conf,
        rationale="x", explanation="x", evidence={}, tags=tags or ["intraday"],
        asset_class=AssetClass.EQUITY, sector=sector,
    )


def _run(ap, *plays):
    d = {p.id: p for p in plays}
    eng = getattr(ap, "engine", None)
    if eng is not None and hasattr(eng, "_plays"):
        eng._plays.update(d)
    return ap.consider(d)


# --------------------------------------------------------------------------- #
def test_happy_path_enters_via_approve():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(), bus=SILENT)
    p = mkplay()
    acts = _run(ap, p)
    assert eng.approved == [(p.id, "autopilot")]
    assert acts and acts[0]["action"] == "entered"
    assert ap.status()["auto_trades_today"] == 1


def test_disabled_is_a_noop():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(enabled=False), bus=SILENT)
    assert _run(ap, mkplay()) == []
    assert eng.approved == []


def test_trade_type_filter_blocks_swing_when_only_intraday():
    eng = FakeEngine()
    _run(AutoPilot(eng, _cfg(trade_types=["INTRADAY"]), bus=SILENT), mkplay(tf=Timeframe.SWING))
    assert eng.approved == []

    eng2 = FakeEngine()
    p = mkplay(tf=Timeframe.SWING)
    _run(AutoPilot(eng2, _cfg(trade_types=["INTRADAY", "SWING"]), bus=SILENT), p)
    assert eng2.approved_ids() == [p.id]


def test_confidence_and_rr_floors():
    eng = FakeEngine()
    _run(AutoPilot(eng, _cfg(min_confidence=0.8), bus=SILENT), mkplay(conf=0.7))
    assert eng.approved == []
    # RR = (106-100)/(100-98) = 3.0 ; require 4:1 -> skip
    _run(AutoPilot(eng, _cfg(min_reward_risk=4.0), bus=SILENT), mkplay())
    assert eng.approved == []


def test_autopilots_own_boxes_cap_what_the_filters_put_on_the_board():
    from types import SimpleNamespace

    eng = FakeEngine()
    eng.filters = SimpleNamespace(timeframes=["INTRADAY", "SWING"])          # day plays are scanned and shown...
    ap = AutoPilot(eng, _cfg(trade_types=["SWING", "PAIRS"], min_confidence=0.5, min_swing_confidence=0.5),
                   bus=SILENT)
    day, swing = mkplay(sym="DAY", conf=0.7), mkplay(sym="SWG", tf=Timeframe.SWING, conf=0.6)
    assert ap.play_types() == ["SWING"] and ap.effective_trade_types() == ["SWING", "PAIRS"]
    _run(ap, day, swing)
    assert eng.approved_ids() == [swing.id]                                  # ...but only the swing is taken
    assert "switched off" in ap.verdict(day) and "own boxes" in ap.verdict(day)
    assert ap.status()["own_trade_types"] == ["SWING", "PAIRS"] and ap.status()["trade_types"] == ["SWING", "PAIRS"]

    ap.configure(trade_types=["INTRADAY", "SWING", "PAIRS"])
    assert ap.play_types() == ["INTRADAY", "SWING"]
    eng.filters = SimpleNamespace(timeframes=["SWING"])                     # the filter bar still caps it too
    assert ap.play_types() == ["SWING"]


def test_no_new_day_trades_in_the_last_minutes_before_the_close(monkeypatch):
    from autotradebot.execution import autopilot as module

    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(trade_types=["INTRADAY", "SWING"], min_minutes_to_close=30), bus=SILENT)
    monkeypatch.setattr(module.clock, "minutes_to_close", lambda ts=None: 20.0)
    day, swing = mkplay(sym="DAY"), mkplay(sym="SWG", tf=Timeframe.SWING, conf=0.7)
    _run(ap, day, swing)
    assert eng.approved_ids() == [swing.id]                                  # the swing trade has all week
    assert "minutes to the close" in ap.verdict(day) and ap.status()["min_minutes_to_close"] == 30
    ap.configure(min_minutes_to_close=0)                                     # off: the day trade goes
    _run(ap, day)
    assert eng.approved_ids() == [swing.id, day.id]


def test_swing_plays_have_a_confidence_floor_of_their_own():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(trade_types=["INTRADAY", "SWING"], min_confidence=0.62, min_swing_confidence=0.5),
                   bus=SILENT)
    swing = mkplay(sym="SWG", tf=Timeframe.SWING, conf=0.55)      # a Bollinger fade's flat confidence
    day = mkplay(sym="DAY", conf=0.55)
    _run(ap, swing, day)
    assert eng.approved_ids() == [swing.id]                        # the day trade misses 0.62, the swing clears 0.5
    assert "swing floor" not in ap.verdict(day) and "0.62" in ap.verdict(day)
    strict = AutoPilot(FakeEngine(), _cfg(trade_types=["SWING"], min_swing_confidence=0.6), bus=SILENT)
    p = mkplay(sym="SWG", tf=Timeframe.SWING, conf=0.55)
    assert _run(strict, p) == [] and "swing floor" in strict.verdict(p)
    strict.configure(min_swing_confidence=0.5)
    assert strict.to_runtime()["min_swing_confidence"] == 0.5 and strict.status()["min_swing_confidence"] == 0.5


def test_fundamental_plays_are_never_auto_traded():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(trade_types=["INTRADAY", "SWING"]), bus=SILENT)
    _run(ap, mkplay(kind=StrategyKind.FUNDAMENTAL, tf=Timeframe.SWING))
    assert eng.approved == []


def test_already_holding_symbol_is_skipped():
    eng = FakeEngine()
    eng.repo.held.add("AAA")
    _run(AutoPilot(eng, _cfg(), bus=SILENT), mkplay(sym="AAA"))
    assert eng.approved == []


def test_position_cap():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(max_auto_positions=1), bus=SILENT)
    _run(ap, mkplay(sym="AAA"), mkplay(sym="BBB"))
    assert len(eng.approved) == 1


def test_per_day_cap():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(max_auto_trades_per_day=2, max_auto_positions=9), bus=SILENT)
    _run(ap, *[mkplay(sym=f"S{i}") for i in range(5)])
    assert len(eng.approved) == 2
    assert ap.status()["auto_trades_today"] == 2


def test_open_risk_cap():
    eng = FakeEngine(equity=10_000.0)          # 4% => $400 budget, est_risk 200 each
    ap = AutoPilot(eng, _cfg(max_open_risk_pct=4.0, max_auto_positions=9), bus=SILENT)
    _run(ap, *[mkplay(sym=f"S{i}") for i in range(4)])
    assert len(eng.approved) == 2              # 200 + 200 == 400 ok; 3rd would be 600 > 400


def test_max_new_per_cycle_prevents_bursts():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(max_new_per_cycle=1, max_auto_positions=9,
                             max_auto_trades_per_day=9), bus=SILENT)
    _run(ap, *[mkplay(sym=f"S{i}") for i in range(5)])
    assert len(eng.approved) == 1             # only one entry this cycle
    _run(ap, *[mkplay(sym=f"T{i}") for i in range(5)])
    assert len(eng.approved) == 2             # one more next cycle


def test_max_per_strategy_cap():
    eng = FakeEngine()
    # already holding two 'sr_bounce' auto trades
    eng.repo._open += [
        {"id": "t1", "symbol": "AA", "strategy": "sr_bounce", "entry_price": 100.0,
         "initial_stop_price": 98.0, "quantity": 10},
        {"id": "t2", "symbol": "BB", "strategy": "sr_bounce", "entry_price": 100.0,
         "initial_stop_price": 98.0, "quantity": 10},
    ]
    ap = AutoPilot(eng, _cfg(max_per_strategy=2, max_auto_positions=9), bus=SILENT)
    ap._auto_trade_ids |= {"t1", "t2"}
    p = mkplay(sym="CC")
    p.strategy = "sr_bounce"
    _run(ap, p)
    assert eng.approved == []                 # 3rd sr_bounce blocked
    q = mkplay(sym="DD"); q.strategy = "vwap_reclaim"
    _run(ap, q)
    assert eng.approved_ids() == [q.id]       # a different strategy still goes


def test_cooldown_after_loss_skips_a_stopped_name():
    eng = FakeEngine()
    from autotradebot.util import clock
    eng.repo._closed = [{
        "symbol": "AAA", "status": "CLOSED",
        "session_date": clock.session_date().isoformat(),
        "realized_pl": -100.0,
    }]
    ap = AutoPilot(eng, _cfg(cooldown_after_loss=True), bus=SILENT)
    _run(ap, mkplay(sym="AAA"))
    assert eng.approved == []
    # a winner earlier today does NOT trigger the cooldown
    eng.repo._closed[0]["realized_pl"] = 50.0
    _run(ap, mkplay(sym="AAA"))
    assert eng.approved_ids() and eng.approved_ids()[0].startswith("play_")


def test_live_mode_paper_only_gate():
    eng = FakeEngine(mode="live")
    published = []
    bus = SimpleNamespace(publish=lambda topic, **k: published.append((topic, k)))
    ap = AutoPilot(eng, _cfg(allow_live=False), bus=bus)
    assert _run(ap, mkplay()) == []
    assert eng.approved == []
    assert any(t == "autopilot.blocked" for t, _ in published)
    assert ap.status()["effective"] is False

    eng2 = FakeEngine(mode="live")
    eng2.records["opening_range_breakout"] = {"trades": 40, "expectancy_r": 0.20}   # real money wants proof
    p = mkplay()
    _run(AutoPilot(eng2, _cfg(allow_live=True), bus=SILENT), p)
    assert eng2.approved_ids() == [p.id]


def test_with_real_money_proof_is_asked_for_whatever_the_setting_says():
    paper, live = FakeEngine(), FakeEngine(mode="live")
    terms = dict(allow_live=True, require_proven=False, min_replay_trades=30, min_replay_expectancy_r=0.05)
    on_paper, with_money = AutoPilot(paper, _cfg(**terms), bus=SILENT), AutoPilot(live, _cfg(**terms), bus=SILENT)
    p, q = mkplay(), mkplay()
    _run(on_paper, p)
    _run(with_money, q)
    assert paper.approved_ids() == [p.id] and live.approved == []              # practice on paper; never with money
    assert "replay" in with_money.verdict(q)
    status = with_money.status()
    assert status["proof_required"] and status["proof_forced"] and status["require_proven"] is False
    assert not on_paper.status()["proof_required"] and not on_paper.status()["proof_forced"]

    live.records["opening_range_breakout"] = {"trades": 40, "expectancy_r": 0.20}
    _run(with_money, q)
    assert live.approved_ids() == [q.id]                                        # proven: taken
    live.mode = "paper"
    assert not with_money.proof_required                                        # back on paper the choice applies again


def test_dry_run_places_nothing():
    eng = FakeEngine()
    acts = _run(AutoPilot(eng, _cfg(dry_run=True), bus=SILENT), mkplay())
    assert eng.approved == []
    assert acts and acts[0]["action"] == "would_enter"


def test_not_executable_from_engine_is_skipped():
    eng = FakeEngine()
    eng.can_execute = False
    ap = AutoPilot(eng, _cfg(), bus=SILENT)
    _run(ap, mkplay())
    assert eng.approved == []
    assert eng.assess_calls, "it should have asked the engine"


def test_a_play_it_tried_and_was_refused_says_so_on_its_row():
    heard = []
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(), bus=SimpleNamespace(publish=lambda topic, **kw: heard.append((topic, kw))))
    unassessed, unsent, entered, untried = mkplay(sym="AAA"), mkplay(sym="BBB"), mkplay(sym="CCC"), mkplay(sym="DDD")
    eng.can_execute = False
    _run(ap, unassessed)                                                        # the engine's assessment refuses it
    eng.can_execute = True
    eng.approve_play = lambda pid, operator="operator": {"ok": False, "reason": "the broker refused the order"}
    _run(ap, unsent)                                                            # the order is refused
    del eng.approve_play
    _run(ap, entered)

    bars = {p.symbol: ap.decorate_play(p.to_row())["autopilot"] for p in (unassessed, unsent, entered, untried)}
    assert bars["AAA"]["skipped"] and bars["AAA"]["reason"] == "not executable in this session"
    assert bars["BBB"]["skipped"] and bars["BBB"]["reason"] == "the broker refused the order"
    assert [kw["play_id"] for topic, kw in heard if topic == "autopilot.skipped"] == [unassessed.id, unsent.id]
    assert bars["CCC"]["acted"] and not bars["CCC"]["skipped"]                 # taken, not skipped
    assert not bars["DDD"]["acted"] and not bars["DDD"]["skipped"]             # never looked at
    ap.settings_changed()                                                       # judged again on the next pass
    assert not ap.decorate_play(unassessed.to_row())["autopilot"]["skipped"]


def test_configure_updates_and_persists():
    calls = []
    ap = AutoPilot(FakeEngine(), _cfg(), bus=SILENT, persist=lambda: calls.append(1))
    st = ap.configure(trade_types=["INTRADAY", "SWING"], min_confidence=0.7, dry_run=True)
    assert st["trade_types"] == ["INTRADAY", "SWING"]
    assert st["min_confidence"] == 0.7 and st["dry_run"] is True
    assert calls, "configure() should persist"


def test_day_mode_active_truth_table():
    ap = AutoPilot(FakeEngine(), _cfg(enabled=False, trade_types=["INTRADAY"]), bus=SILENT)
    assert ap.day_mode_active(market_open=True) is False          # disabled
    ap.configure(enabled=True)
    assert ap.day_mode_active(market_open=True) is True
    assert ap.day_mode_active(market_open=False) is False         # session closed
    ap.configure(trade_types=["SWING"])
    assert ap.day_mode_active(market_open=True) is False          # day trades not enabled
    # live gate: enabled in live without allow_live -> not active
    eng_live = FakeEngine(mode="live")
    ap_live = AutoPilot(eng_live, _cfg(enabled=True, allow_live=False, trade_types=["INTRADAY"]), bus=SILENT)
    assert ap_live.day_mode_active(market_open=True) is False


# --------------------------------------------------------------------------- #
#  orders still working
# --------------------------------------------------------------------------- #
def test_an_entry_still_working_counts_as_a_position_and_so_does_its_late_fill():
    eng = FakeEngine()
    eng.fill_later = True
    ap = AutoPilot(eng, _cfg(max_auto_positions=1), bus=SILENT)
    first = mkplay(sym="AAA")
    _run(ap, first)
    _run(ap, mkplay(sym="BBB"))
    assert eng.approved_ids() == [first.id] and ap.status()["open_auto_positions"] == 1

    # the fill lands after approve_play returned, so its trade only carries the play id
    eng.working.clear()
    eng.repo._open.append({"id": "late", "play_id": first.id, "symbol": "AAA", "strategy": first.strategy,
                           "entry_price": 100.0, "initial_stop_price": 80.0, "quantity": 10})
    _run(ap, mkplay(sym="CCC"))
    assert eng.approved_ids() == [first.id] and ap.status()["open_auto_positions"] == 1


def test_no_entry_while_an_order_for_the_symbol_is_working():
    eng = FakeEngine()
    eng.working.append({"order_id": "o1", "play_id": "someone_elses", "symbol": "AAA",
                        "strategy": "vwap_reclaim", "qty": 10, "risk": 20.0})
    _run(AutoPilot(eng, _cfg(), bus=SILENT), mkplay(sym="AAA"))
    assert eng.approved == []


def test_no_entry_in_a_symbol_the_account_already_holds():
    eng = FakeEngine()
    eng._account.positions.append(Position(symbol="AAA", quantity=500, avg_price=5.0))
    _run(AutoPilot(eng, _cfg(), bus=SILENT), mkplay(sym="AAA"))
    assert eng.approved == []


def test_autopilot_never_puts_more_than_the_account_into_positions():
    eng = FakeEngine(equity=100_000.0)
    eng.gross = 99_500.0                         # positions already use almost the whole account
    ap = AutoPilot(eng, _cfg(max_gross_exposure_pct=100.0), bus=SILENT)
    _run(ap, mkplay(sym="AAA"))
    assert eng.approved == []
    eng.gross = 50_000.0
    p = mkplay(sym="BBB")
    _run(ap, p)
    assert eng.approved_ids() == [p.id]


# --------------------------------------------------------------------------- #
#  noise
# --------------------------------------------------------------------------- #
def test_autopilot_skips_plays_flagged_by_the_noise_checks_switched_on():
    eng = FakeEngine()
    p = mkplay(sym="AAA")
    p.noise = ["against_trend"]
    _run(AutoPilot(eng, _cfg(skip_noise=["against_trend"]), bus=SILENT), p)
    assert eng.approved == []
    q = mkplay(sym="BBB")
    q.noise = ["against_trend"]
    _run(AutoPilot(eng, _cfg(skip_noise=["conflict"]), bus=SILENT), q)
    assert eng.approved_ids() == [q.id]


def test_a_day_trade_setup_must_show_up_twice_before_autopilot_takes_it():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(min_confirmations=2), bus=SILENT)
    p = mkplay()
    _run(ap, p)
    assert eng.approved == []
    p.confirmations = 2
    _run(ap, p)
    assert eng.approved_ids() == [p.id]


def test_counting_confirmations_on_candles_is_a_setting_that_is_remembered():
    ap = AutoPilot(FakeEngine(), _cfg(), bus=SILENT)
    assert ap.confirm_on_new_candle and ap.status()["confirm_on_new_candle"]     # on unless config says otherwise
    ap.configure(confirm_on_new_candle=False)
    assert ap.to_runtime()["confirm_on_new_candle"] is False and not ap.status()["confirm_on_new_candle"]
    other = AutoPilot(FakeEngine(), _cfg(), bus=SILENT)
    other.load_runtime(ap.to_runtime())
    assert other.confirm_on_new_candle is False
    assert AutoPilot(FakeEngine(), _cfg(confirm_on_new_candle=False), bus=SILENT).confirm_on_new_candle is False


def test_the_exposure_ceiling_is_a_share_of_the_trading_capital_margin_included():
    eng = FakeEngine()
    eng.exposure_ceiling = lambda: 340_000.0                     # worth 100k, with margin: 340k all told
    ap = AutoPilot(eng, _cfg(max_gross_exposure_pct=100.0), bus=SILENT)
    assert ap.exposure_ceiling(100_000.0) == 340_000.0          # past the account's value: margin is allowed
    ap.configure(max_gross_exposure_pct=80)
    assert ap.exposure_ceiling(100_000.0) == pytest.approx(272_000.0)   # a buffer under the broker's own limit
    ap.configure(max_gross_exposure_pct=400)                    # the capital has the margin in it already
    assert ap.max_gross_exposure_pct == 100.0
    again = AutoPilot(eng, _cfg(max_gross_exposure_pct=100.0), bus=SILENT)
    again.load_runtime({"max_gross_exposure_pct": 150})          # an older file's value
    assert again.max_gross_exposure_pct == 100.0
    assert AutoPilot(FakeEngine(), _cfg(max_gross_exposure_pct=100.0), bus=SILENT).exposure_ceiling(100_000.0) == 100_000.0


def test_autopilot_takes_only_the_setups_named_and_remembers_them():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(), bus=SILENT)
    assert ap.strategies == [] and ap.status()["strategies"] == []             # none named: every setup
    ap.configure(strategies=["abcd_pattern", "abcd_pattern", " "])
    assert ap.strategies == ["abcd_pattern"]
    other = mkplay()                                                             # an opening range breakout
    assert "setups Autopilot takes" in ap._play_check(*ap._facts(other))
    named = mkplay("BBB")
    named.strategy = "abcd_pattern"
    _run(ap, other, named)
    assert eng.approved_ids() == [named.id]
    again = AutoPilot(FakeEngine(), _cfg(), bus=SILENT)
    again.load_runtime(ap.to_runtime())
    assert again.strategies == ["abcd_pattern"]
    again.load_runtime({"strategies": "abcd_pattern"})                           # not a list: left as it was
    assert again.strategies == ["abcd_pattern"]
    ap.configure(strategies=[])
    assert ap.strategies == [] and ap._play_check(*ap._facts(other)) is None
    assert AutoPilot(FakeEngine(), _cfg(strategies=["abcd_pattern"]), bus=SILENT).strategies == ["abcd_pattern"]


def test_autopilot_only_trades_strategies_the_replay_has_proven():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(require_proven=True, min_replay_trades=30, min_replay_expectancy_r=0.05), bus=SILENT)
    p = mkplay()
    _run(ap, p)
    assert eng.approved == []                                                   # never replayed
    eng.records["opening_range_breakout"] = {"trades": 40, "expectancy_r": -0.10}
    _run(ap, p)
    assert eng.approved == []                                                   # replayed, and it lost
    eng.records["opening_range_breakout"] = {"trades": 40, "expectancy_r": 0.20}
    _run(ap, p)
    assert eng.approved_ids() == [p.id]


def test_autopilot_wants_a_strategy_to_have_made_money_in_the_held_out_sessions_too():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(require_proven=True, min_replay_trades=30, min_replay_expectancy_r=0.05), bus=SILENT)
    p = mkplay()
    record = {"trades": 40, "expectancy_r": 0.20}
    for held in ({"trades": 12, "expectancy_r": -0.10}, {"trades": 4, "expectancy_r": 0.50}):
        eng.records["opening_range_breakout"] = {**record, "out_of_sample": held}
        _run(ap, p)
        assert eng.approved == []
    assert "held-out" in ap.verdict(p)
    eng.records["opening_range_breakout"] = {**record, "out_of_sample": {"trades": 12, "expectancy_r": 0.10}}
    _run(ap, p)
    assert eng.approved_ids() == [p.id]


def test_autopilot_skips_a_statistical_noise_flag_once_the_replay_shows_it_helps():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(), bus=SILENT)
    p = mkplay()
    p.noise = ["not_trending"]
    eng.learned_skips = lambda: ["not_trending"]
    _run(ap, p)
    assert eng.approved == [] and ap.status()["learned_skip_noise"] == ["not_trending"]
    eng.learned_skips = lambda: []
    _run(ap, p)
    assert eng.approved_ids() == [p.id]


def test_autopilot_can_be_told_to_take_pairs_only():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(), bus=SILENT)
    ap.configure(trade_types=["PAIRS", "OPTIONS"])
    assert ap.trade_types == ["PAIRS"]
    _run(ap, mkplay())
    assert eng.approved == []                                                   # plays aren't pairs


# --------------------------------------------------------------------------- #
#  the daily loss stop (Aziz: a daily maximum loss - live to trade another day)
# --------------------------------------------------------------------------- #
def _closed(symbol, pl, broker="paper", timeframe="INTRADAY"):
    return {"id": f"t_{symbol}", "symbol": symbol, "status": "CLOSED", "realized_pl": pl, "broker": broker,
            "session_date": "2000-01-01", "timeframe": timeframe}


def test_the_daily_loss_limit_stops_new_entries_for_the_session():
    eng = FakeEngine(equity=10_000.0)                      # 2% of equity = $200
    eng.repo._closed = [_closed("AAA", -150.0), _closed("BBB", -60.0)]
    ap = AutoPilot(eng, _cfg(max_daily_loss_pct=2.0), bus=SILENT)
    p = mkplay(sym="CCC")
    assert _run(ap, p) == [] and eng.approved == []
    st = ap.status()
    assert st["daily_loss_stop"] and st["realized_today"] == -210.0
    assert "daily limit" in ap.verdict(p)
    # a loss short of the limit, or the limit switched off, changes nothing
    eng2 = FakeEngine(equity=10_000.0)
    eng2.repo._closed = [_closed("AAA", -150.0)]
    ap2 = AutoPilot(eng2, _cfg(max_daily_loss_pct=2.0), bus=SILENT)
    assert len(_run(ap2, mkplay(sym="CCC"))) == 1 and not ap2.status()["daily_loss_stop"]
    eng3 = FakeEngine(equity=10_000.0)
    eng3.repo._closed = [_closed("AAA", -900.0)]
    ap3 = AutoPilot(eng3, _cfg(max_daily_loss_pct=0.0), bus=SILENT)
    assert len(_run(ap3, mkplay(sym="CCC"))) == 1


def test_the_daily_loss_counts_only_the_venue_autopilot_trades_on():
    eng = FakeEngine(equity=10_000.0)
    eng._venue = "ibkr-paper"
    eng.repo._closed = [_closed("AAA", -500.0, broker="paper"), _closed("BBB", -50.0, broker="ibkr-paper")]
    ap = AutoPilot(eng, _cfg(max_daily_loss_pct=2.0), bus=SILENT)
    assert len(_run(ap, mkplay(sym="CCC"))) == 1
    assert ap.status()["realized_today"] == -50.0


def test_the_give_back_rule_stops_the_day_once_a_gain_is_mostly_gone():
    eng = FakeEngine(equity=10_000.0)                      # floor: a peak of at least 0.25% = $25
    ap = AutoPilot(eng, _cfg(max_daily_loss_pct=0.0, max_giveback_pct=30.0, giveback_floor_pct=0.25), bus=SILENT)
    eng.repo._closed = [_closed("AAA", 200.0)]
    assert len(_run(ap, mkplay(sym="BBB"))) == 1 and ap.status()["peak_realized"] == 200.0
    ap._realized = (float("-inf"), 0.0)                    # forget the cached figure
    eng.repo._closed = [_closed("AAA", 200.0), _closed("BBB", -50.0)]   # 150 left: 25% given back
    assert len(_run(ap, mkplay(sym="CCC"))) == 1
    ap._realized = (float("-inf"), 0.0)
    eng.repo._closed = [_closed("AAA", 200.0), _closed("BBB", -50.0), _closed("CCC", -20.0)]   # 130 left: 35%
    p = mkplay(sym="DDD")
    assert _run(ap, p) == [] and "give-back" in ap.verdict(p) and ap.status()["daily_loss_stop"]
    assert not ap.decorate_play({"id": "x", "timeframe": "INTRADAY", "confidence": 0.9, "reward_risk": 3,
                                 "kind": "TECHNICAL", "status": "PROPOSED", "noise": [], "confirmations": 5,
                                 "strategy": "opening_range_breakout"})["autopilot"]["eligible"]
    # a small gain that fades is noise, not a give-back
    eng2 = FakeEngine(equity=10_000.0)
    ap2 = AutoPilot(eng2, _cfg(max_daily_loss_pct=0.0, max_giveback_pct=30.0, giveback_floor_pct=0.25), bus=SILENT)
    eng2.repo._closed = [_closed("AAA", 20.0)]
    _run(ap2, mkplay(sym="BBB"))
    ap2._realized = (float("-inf"), 0.0)
    eng2.repo._closed = [_closed("AAA", 20.0), _closed("BBB", -15.0)]
    assert len(_run(ap2, mkplay(sym="CCC"))) == 1


def test_the_daily_loss_limit_is_tunable_and_remembered():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(max_daily_loss_pct=2.0), bus=SILENT)
    ap.configure(max_daily_loss_pct=3.5, max_giveback_pct=40)
    assert ap.to_runtime()["max_daily_loss_pct"] == 3.5 and ap.to_runtime()["max_giveback_pct"] == 40.0
    other = AutoPilot(FakeEngine(), _cfg(), bus=SILENT)
    other.load_runtime(ap.to_runtime())
    assert other.max_daily_loss_pct == 3.5 and other.max_giveback_pct == 40.0


def test_a_daily_stop_holds_for_the_rest_of_the_day_when_the_pl_recovers(monkeypatch):
    import datetime as dt

    from autotradebot.execution import autopilot as module

    eng = FakeEngine(equity=10_000.0)                      # 2% of equity = $200
    eng.repo._closed = [_closed("AAA", -250.0)]
    ap = AutoPilot(eng, _cfg(max_daily_loss_pct=2.0), bus=SILENT)
    assert _run(ap, mkplay(sym="BBB")) == [] and ap.status()["daily_loss_stop"]
    ap._realized = (float("-inf"), 0.0)
    eng.repo._closed = [_closed("AAA", -250.0), _closed("SWG", 400.0, timeframe="SWING")]   # back above the limit
    p = mkplay(sym="CCC")
    assert _run(ap, p) == [] and eng.approved == []
    st = ap.status()
    assert st["daily_loss_stop"] and "past the daily limit" in st["daily_loss_reason"]
    assert "past the daily limit" in ap.verdict(p)

    ap.configure(max_giveback_pct=40.0)                    # moving a limit judges the day afresh: the gain lifts it
    assert not ap.status()["daily_loss_stop"]
    assert len(_run(ap, mkplay(sym="DDD"))) == 1

    eng.repo._closed = [_closed("AAA", -250.0)]
    ap._realized = (float("-inf"), 0.0)
    assert _run(ap, mkplay(sym="EEE")) == [] and ap.stopped_for_the_day
    tomorrow = module.clock.session_date() + dt.timedelta(days=1)
    monkeypatch.setattr(module.clock, "session_date", lambda ts=None: tomorrow)
    eng.repo._closed = []                                  # a new session starts unstopped
    ap._realized = (float("-inf"), 0.0)
    assert not ap.status()["daily_loss_stop"] and ap._loss_stop_reason == "" and ap._day == tomorrow.isoformat()
    assert len(_run(ap, mkplay(sym="FFF"))) == 1


def test_a_daily_stop_survives_a_restart_the_same_day_only():
    import datetime as dt

    from autotradebot.util import clock

    eng = FakeEngine(equity=10_000.0)
    eng.repo._closed = [_closed("AAA", -250.0)]
    ap = AutoPilot(eng, _cfg(max_daily_loss_pct=2.0), bus=SILENT)
    _run(ap, mkplay(sym="BBB"))
    saved = ap.to_runtime()
    assert saved["loss_stop_day"] == clock.session_date().isoformat()

    eng2 = FakeEngine(equity=10_000.0)                     # the restarted app reads no loss (the record isn't back yet)
    again = AutoPilot(eng2, _cfg(max_daily_loss_pct=2.0), bus=SILENT)
    again.load_runtime(saved)
    assert _run(again, mkplay(sym="CCC")) == [] and eng2.approved == []
    assert again.status()["daily_loss_stop"] and "past the daily limit" in again.status()["headline"]["text"]

    yesterday = (clock.session_date() - dt.timedelta(days=1)).isoformat()
    fresh = AutoPilot(eng2, _cfg(max_daily_loss_pct=2.0), bus=SILENT)
    fresh.load_runtime({**saved, "day": yesterday, "loss_stop_day": yesterday})
    assert len(_run(fresh, mkplay(sym="DDD"))) == 1 and not fresh.status()["daily_loss_stop"]


def test_the_give_back_peak_counts_day_trades_only_and_the_loss_limit_counts_everything():
    eng = FakeEngine(equity=10_000.0)                      # give-back floor $25; daily limit $200
    ap = AutoPilot(eng, _cfg(max_daily_loss_pct=2.0, max_giveback_pct=30.0, giveback_floor_pct=0.25), bus=SILENT)
    eng.repo._closed = [_closed("SWG", 500.0, timeframe="SWING")]                   # a swing closed at a profit
    assert len(_run(ap, mkplay(sym="AAA"))) == 1 and ap.status()["peak_realized"] == 0.0
    ap._realized = (float("-inf"), 0.0)
    eng.repo._closed += [_closed("AAA", -180.0)]           # an ordinary day-trade loss isn't giving back the swing's gain
    assert len(_run(ap, mkplay(sym="BBB"))) == 1 and not ap.status()["daily_loss_stop"]

    eng2 = FakeEngine(equity=10_000.0)
    ap2 = AutoPilot(eng2, _cfg(max_daily_loss_pct=2.0, max_giveback_pct=30.0, giveback_floor_pct=0.25), bus=SILENT)
    eng2.repo._closed = [_closed("SWG", 500.0, timeframe="SWING"), _closed("AAA", 200.0)]
    assert len(_run(ap2, mkplay(sym="BBB"))) == 1 and ap2.status()["peak_realized"] == 200.0
    ap2._realized = (float("-inf"), 0.0)
    eng2.repo._closed += [_closed("BBB", -70.0)]           # the day trades kept 130 of 200, whatever the swing made
    p = mkplay(sym="CCC")
    assert _run(ap2, p) == [] and "day trades' realized gain has fallen from 200 to 130" in ap2.verdict(p)

    eng3 = FakeEngine(equity=10_000.0)                     # the loss limit still counts a swing trade's loss
    eng3.repo._closed = [_closed("SWG", -250.0, timeframe="SWING")]
    ap3 = AutoPilot(eng3, _cfg(max_daily_loss_pct=2.0), bus=SILENT)
    assert _run(ap3, mkplay(sym="AAA")) == [] and ap3.status()["daily_loss_stop"]


def test_proof_also_asks_whether_the_edge_is_luck_drift_or_eaten_by_costs():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(require_proven=True, min_replay_trades=30, min_replay_expectancy_r=0.05,
                             proof_p_value=0.10), bus=SILENT)
    p = mkplay()
    record = {"trades": 40, "expectancy_r": 0.20, "edge_r": 0.18, "p_adjusted": 0.04, "setups_tested": 20}
    for worse, words in (({"p_adjusted": 0.40}, "could be luck"),              # the best of 20 tries proves little
                         ({"edge_r": 0.01}, "own drift"),                      # long in a rising market isn't an edge
                         ({"cost_share": 0.50}, "speed limit")):               # costs past a third of the edge
        eng.records["opening_range_breakout"] = {**record, **worse}
        _run(ap, p)
        assert eng.approved == [] and words in ap.verdict(p)
    eng.records["opening_range_breakout"] = {**record, "cost_share": 0.2}
    _run(ap, p)
    assert eng.approved_ids() == [p.id] and ap.status()["proof_p_value"] == 0.10
    ap.proof_p_value = 0.0                                                     # the luck test switched off
    eng.records["opening_range_breakout"] = {**record, "p_adjusted": 0.9}
    assert ap.proof_missing("opening_range_breakout") is None


def test_one_entry_per_scan_cycle_means_a_scan_of_the_market_not_a_recheck_of_the_board(monkeypatch):
    from autotradebot.execution import autopilot as module

    eng = FakeEngine()
    eng.entry_pace_seconds = lambda timeframe: 60.0 if timeframe == "INTRADAY" else 300.0
    ap = AutoPilot(eng, _cfg(trade_types=["INTRADAY", "SWING"], max_new_per_cycle=1, max_auto_positions=9), bus=SILENT)
    clock_ = {"t": 1000.0}
    monkeypatch.setattr(module.time, "monotonic", lambda: clock_["t"])
    first, second, day = (mkplay(sym="ONE", tf=Timeframe.SWING), mkplay(sym="TWO", tf=Timeframe.SWING),
                          mkplay(sym="DAY"))
    _run(ap, first, second, day)
    assert eng.approved_ids() == [first.id] and "per scan cycle - the next in" in ap.verdict(second)
    clock_["t"] += 15.0                                                       # the 15-second re-check of the board
    _run(ap, first, second, day)
    assert eng.approved_ids() == [first.id]
    clock_["t"] += 50.0                                                       # a fast cycle on: the day trade may go
    _run(ap, first, second, day)
    assert eng.approved_ids() == [first.id, day.id]
    clock_["t"] += 300.0                                                      # a whole regular cycle since the last entry of any kind
    _run(ap, first, second, day)
    assert eng.approved_ids() == [first.id, day.id, second.id]


def test_no_entries_while_prices_cannot_be_read():
    heard = []
    eng = FakeEngine()
    state = {"why": "the login is active somewhere else"}
    eng.prices_refused = lambda: state["why"]
    ap = AutoPilot(eng, _cfg(), bus=SimpleNamespace(publish=lambda topic, **p: heard.append(topic)))
    p = mkplay()
    _run(ap, p)
    _run(ap, p)
    assert eng.approved == [] and "prices can't be read" in ap.verdict(p) and heard.count("autopilot.blocked") == 1
    state["why"] = ""                                                          # the other session logged out
    _run(ap, p)
    assert eng.approved_ids() == [p.id]


# ---------------------------------------------------------------- the day / swing split
def _split_pilot(day_pct=70.0, **over):
    eng = FakeEngine()
    eng.effective_day_pct = lambda: day_pct
    cfg = dict(trade_types=["INTRADAY", "SWING"], max_auto_positions=10, max_auto_trades_per_day=10,
               min_swing_confidence=0.5)
    cfg.update(over)
    return eng, AutoPilot(eng, _cfg(**cfg), bus=SILENT)


def test_the_slots_divide_the_way_the_trading_capital_does():
    _, ap = _split_pilot(70.0)
    assert ap.kind_slots(10) == {"INTRADAY": 7, "SWING": 3}
    assert ap.kind_slots(3) == {"INTRADAY": 2, "SWING": 1}
    assert ap.kind_slots(2) == {"INTRADAY": 1, "SWING": 1}                     # a kind with a share keeps a slot
    assert ap.kind_slots(1) == {"INTRADAY": 1, "SWING": 1}                     # one slot: whichever comes first
    assert ap.kind_slots(0) == {"INTRADAY": 0, "SWING": 0}
    ap.engine.effective_day_pct = lambda: 95.0
    assert ap.kind_slots(10) == {"INTRADAY": 9, "SWING": 1}
    ap.engine.effective_day_pct = lambda: 100.0                                # one filter box off: no split
    assert ap.kind_slots(10) == {"INTRADAY": 10, "SWING": 0}
    ap.engine.effective_day_pct = lambda: 0.0
    assert ap.kind_slots(10) == {"INTRADAY": 0, "SWING": 10}
    assert AutoPilot(FakeEngine(), _cfg(), bus=SILENT).kind_slots(10) is None  # an engine that doesn't say: no split


def test_swing_trades_cant_take_the_slots_the_split_keeps_for_day_trades():
    eng, ap = _split_pilot(70.0)
    swings = [mkplay(sym=f"S{i}", tf=Timeframe.SWING) for i in range(6)]
    days = [mkplay(sym=f"D{i}") for i in range(3)]
    _run(ap, *swings)
    assert len(eng.approved) == 3                                              # three of ten, not six
    assert "swing trades hold 3 of the 3 positions" in ap.verdict(swings[-1]) and "70% / 30%" in ap.verdict(swings[-1])
    _run(ap, *swings, *days)
    taken = {eng._plays[pid].symbol for pid in eng.approved_ids()}
    assert {"D0", "D1", "D2"} <= taken and len(eng.approved) == 6              # the day trades still had their room
    card = ap.status()["slots"]
    assert (card["SWING"]["open"], card["SWING"]["max"], card["INTRADAY"]["open"], card["INTRADAY"]["max"]) == (3, 3, 3, 7)
    assert card["SWING"]["taking"] and card["INTRADAY"]["taking"] and card["day_pct"] == 70.0


def test_a_days_entries_divide_the_same_way_and_the_count_survives_a_restart():
    eng, ap = _split_pilot(50.0, max_auto_positions=20, max_auto_trades_per_day=4)
    _run(ap, *[mkplay(sym=f"S{i}", tf=Timeframe.SWING) for i in range(4)])
    assert len(eng.approved) == 2                                              # two of the day's four entries
    saved = ap.to_runtime()
    assert saved["count_today_by_kind"] == {"SWING": 2}
    again = AutoPilot(eng, _cfg(trade_types=["INTRADAY", "SWING"], max_auto_positions=20, max_auto_trades_per_day=4),
                      bus=SILENT)
    again.load_runtime(saved)
    late = mkplay(sym="S9", tf=Timeframe.SWING)
    _run(again, late)
    assert len(eng.approved) == 2 and "2 of the 2 entries a day" in again.verdict(late)
    again.load_runtime({**saved, "count_today_by_kind": "nonsense"})           # an older or damaged file
    assert again._count_by_kind == {}


def test_without_both_kinds_switched_on_nothing_is_divided():
    eng, ap = _split_pilot(0.0)                                                # the Intraday filter box is off
    _run(ap, *[mkplay(sym=f"S{i}", tf=Timeframe.SWING) for i in range(6)])
    assert len(eng.approved) == 6


def test_after_a_restart_autopilot_still_knows_the_positions_it_opened():
    eng, ap = _split_pilot(70.0)
    eng._venue = "ibkr-paper"
    mine = {"strategy": "s", "timeframe": "SWING", "entry_price": 100.0, "initial_stop_price": 95.0, "quantity": 10,
            "broker": "ibkr-paper", "entry_context": {"by": "autopilot"}}
    eng.repo._open = [{**mine, "id": "t1", "symbol": "AAA"}, {**mine, "id": "t2", "symbol": "BBB"},
                      {**mine, "id": "t3", "symbol": "CCC"},
                      {**mine, "id": "t4", "symbol": "DDD", "entry_context": {"by": "operator"}},     # taken by hand
                      {**mine, "id": "t5", "symbol": "EEE", "pair_id": "p1"},                          # the pair desk's
                      {**mine, "id": "t6", "symbol": "FFF", "broker": "paper"}]                        # another account
    assert [t["id"] for t in ap._open_auto_trades()] == ["t1", "t2", "t3"]
    assert ap.status()["open_auto_positions"] == 3 and ap.status()["slots"]["SWING"]["open"] == 3
    swing = mkplay(sym="NEW", tf=Timeframe.SWING)
    _run(ap, swing)
    assert eng.approved == [] and "swing trades hold 3 of the 3" in ap.verdict(swing)    # its share is full already


def test_entries_an_earlier_run_left_working_count_against_its_caps_again():
    eng, ap = _split_pilot(70.0, max_auto_positions=2)
    eng.working = [{"order_id": "o1", "play_id": "play_mine", "symbol": "AAA", "strategy": "s", "timeframe": "SWING",
                    "qty": 10, "risk": 100.0},
                   {"order_id": "o2", "play_id": "play_hand", "symbol": "BBB", "strategy": "s", "timeframe": "SWING",
                    "qty": 10, "risk": 100.0}]
    eng.repo.get_play = lambda pid: {"play_mine": {"decided_by": "autopilot"}, "play_hand": {"decided_by": "operator"}}.get(pid)
    assert ap._working_auto_entries() == []                                  # a fresh run knows neither
    ap.recognise_entries(["play_mine", "play_hand", "play_gone"])
    assert [w["play_id"] for w in ap._working_auto_entries()] == ["play_mine"]
    assert ap.status()["slots"]["SWING"]["open"] == 1


def test_a_play_refused_for_the_day_gets_another_look_when_a_setting_changes():
    eng, ap = _split_pilot(70.0)
    eng.can_execute = False                                                    # no room in its share, say
    refused, entered = mkplay(sym="REF", tf=Timeframe.SWING), mkplay(sym="ENT")
    _run(ap, refused)
    eng.can_execute = True
    _run(ap, refused, entered)
    assert eng.approved_ids() == [entered.id]                                  # refused once: handled for the day
    ap.settings_changed()                                                      # the slider moved, the capital was raised...
    assert ap.verdict(refused).startswith("passes its checks")
    _run(ap, refused, entered)
    assert eng.approved_ids() == [entered.id, refused.id]                      # ...judged again; what it entered isn't


def test_a_daily_loss_limit_that_is_raised_stops_saying_stopped():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(max_daily_loss_pct=1.0), bus=SILENT)
    ap._realized_today = lambda: -2_000.0
    _run(ap, mkplay())
    assert ap.status()["daily_loss_stop"] and eng.approved == []
    ap.configure(max_daily_loss_pct=5.0)
    _run(ap, mkplay(sym="BBB"))
    assert not ap.status()["daily_loss_stop"] and len(eng.approved) == 1


# ---------------------------------------------------------------- the play's bar: would take it now, or waiting
def _row(tf, strategy="opening_range_breakout"):
    return {"id": f"row_{tf}", "timeframe": tf, "confidence": 0.9, "reward_risk": 3, "kind": "TECHNICAL",
            "status": "PROPOSED", "noise": [], "confirmations": 5, "strategy": strategy}


def test_a_play_that_passes_but_finds_its_cap_full_says_what_it_waits_for():
    eng, ap = _split_pilot(70.0)
    free = ap.decorate_play(_row("SWING"))["autopilot"]
    assert free["eligible"] and free["waiting"] is None                          # green: it would go on the next pass
    _run(ap, *[mkplay(sym=f"S{i}", tf=Timeframe.SWING) for i in range(3)])
    ap._room_cache = None
    swing = ap.decorate_play(_row("SWING"))["autopilot"]
    assert swing["eligible"] and "swing trades hold 3 of the 3 positions" in swing["waiting"]
    assert ap.decorate_play(_row("INTRADAY"))["autopilot"]["waiting"] is None  # the day trades' room is still there

    ap.max_per_strategy = 3
    ap._room_cache = None
    assert "already holding 3 of this setup" in ap.decorate_play(_row("INTRADAY"))["autopilot"]["waiting"]
    ap.max_auto_positions = 3
    ap._room_cache = None
    assert "all 3 auto positions are taken" in ap.decorate_play(_row("INTRADAY"))["autopilot"]["waiting"]
    ap._count_today = ap.max_auto_trades_per_day
    assert "auto entries are used" in ap.decorate_play(_row("INTRADAY"))["autopilot"]["waiting"]

    ap.enabled = False                                                           # not taking it at all: no bar
    off = ap.decorate_play(_row("INTRADAY"))["autopilot"]
    assert not off["eligible"] and off["waiting"] is None


# ---------------------------------------------------------------- the play's bar says why not, in the gate's words
def _refused(status=None, noise=(), **over):
    p = mkplay(**over)
    p.noise = list(noise)
    if status is not None:
        p.status = status
    return p


@pytest.mark.parametrize("cfg, play, words", [
    ({}, _refused(status=PlayStatus.REJECTED), "not a fresh proposed play"),
    ({}, _refused(tf=Timeframe.SWING), "swing trades are switched off"),
    ({"min_confidence": 0.8}, _refused(conf=0.7), "confidence 0.70 < 0.80"),
    ({}, _refused(conf=0.5996), "confidence 0.60 < 0.60"),                     # just under: a row rounds it to 0.6
    ({"min_reward_risk": 1.5}, _refused(target=102.4), "reward:risk 1.20 < 1.50"),
    ({}, _refused(target=103.995), "reward:risk 2.00 < 2.00"),                 # 1.9975: a row rounds it to 2.0
    ({}, _refused(kind=StrategyKind.FUNDAMENTAL), "valuation plays"),
    ({"skip_noise": ["against_trend"]}, _refused(noise=["against_trend"]), "noise: against the daily trend"),
    ({"min_confirmations": 2}, _refused(), "seen on 1 of 2 five-minute candles in a row"),
    ({"min_confirmations": 2, "confirm_on_new_candle": False}, _refused(), "seen in 1 of 2 scans in a row"),
    ({"require_proven": True}, _refused(), "isn't proven yet"),
    ({"records": {"opening_range_breakout": {"trades": 40, "expectancy_r": -0.10,
                                             "out_of_sample": {"trades": 12, "expectancy_r": -0.08}}}},
     _refused(), "loses in the replay"),
    ({}, _refused(), None),
])
def test_the_bar_gives_the_gates_own_reason_for_every_check_on_the_play(cfg, play, words):
    eng = FakeEngine()
    eng.records = dict(cfg.pop("records", {}))
    ap = AutoPilot(eng, _cfg(**cfg), bus=SILENT)
    gate = ap._pre_gate(play, 100_000.0)
    bar = ap.decorate_play(play.to_row(), play)["autopilot"]                     # as engine._decorate asks
    assert bar["why_not"] == gate                                                # one set of checks, one wording
    if words is None:
        assert gate is None and bar["eligible"] and bar["waiting"] is None
    else:
        assert words in gate and not bar["eligible"] and bar["waiting"] is None


def test_the_bar_says_when_autopilot_itself_is_why_not():
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(), bus=SILENT)
    ap.enabled = False
    assert ap.decorate_play(_row("INTRADAY"))["autopilot"]["why_not"] == "Autopilot is off"
    ap.enabled = True
    ap._roll_day()
    ap._loss_stop_day = ap._day                                                  # the daily loss limit was reached today
    assert "stopped for the day" in ap.decorate_play(_row("INTRADAY"))["autopilot"]["why_not"]
    live = AutoPilot(FakeEngine(mode="live"), _cfg(allow_live=False), bus=SILENT)
    assert "paper-only" in live.decorate_play(_row("INTRADAY"))["autopilot"]["why_not"]


def test_a_play_the_engine_refused_says_why_on_the_bar_not_a_cap_it_would_wait_for():
    eng = FakeEngine()
    eng.can_execute = False                                                      # the engine's assessment refuses it
    ap = AutoPilot(eng, _cfg(max_auto_trades_per_day=2), bus=SILENT)
    p = mkplay()
    _run(ap, p)
    ap._count_today = ap.max_auto_trades_per_day                                 # and a cap fills up afterwards
    bar = ap.decorate_play(p.to_row(), p)["autopilot"]
    assert bar["acted"] and bar["why_not"] is None and bar["waiting"] is None
    assert bar["reason"] == "not executable in this session"                     # the refusal, not the full cap
    ap.settings_changed()                                                        # judged afresh: the cap shows again
    assert "auto entries are used" in ap.decorate_play(p.to_row(), p)["autopilot"]["waiting"]


def test_the_clock_and_what_is_held_reach_the_bar_as_the_last_passes_reason(monkeypatch):
    from autotradebot.execution import autopilot as module

    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(trade_types=["INTRADAY", "SWING"], min_minutes_to_close=30), bus=SILENT)
    monkeypatch.setattr(module.clock, "minutes_to_close", lambda ts=None: 20.0)
    late, held = mkplay(sym="AAA"), mkplay(sym="BBB", tf=Timeframe.SWING)
    eng.repo.held.add("BBB")
    _run(ap, late, held)
    assert eng.approved == []
    late_bar, held_bar = ap.decorate_play(late.to_row())["autopilot"], ap.decorate_play(held.to_row())["autopilot"]
    assert late_bar["why_not"] is None and "minutes to the close" in late_bar["reason"]
    assert held_bar["why_not"] is None and held_bar["reason"] == "already holding BBB"


def test_a_board_of_plays_reads_each_strategys_record_once_and_the_gate_every_time():
    eng = FakeEngine()
    reads = []
    eng.strategy_record = lambda key: reads.append(key) or eng.records.get(key)
    ap = AutoPilot(eng, _cfg(require_proven=True), bus=SILENT)
    for i in range(5):
        assert "isn't proven yet" in ap.decorate_play({**_row("INTRADAY"), "id": f"row_{i}"})["autopilot"]["why_not"]
    assert reads == ["opening_range_breakout"]                                   # one read for the whole board
    eng.records["opening_range_breakout"] = {"trades": 40, "expectancy_r": 0.30}
    ap.settings_changed()                                                        # a new replay lands here
    assert ap.decorate_play(_row("INTRADAY"))["autopilot"]["eligible"] and len(reads) == 2
    p = mkplay()
    assert ap._pre_gate(p, 100_000.0) is None and ap._pre_gate(p, 100_000.0) is None
    assert len(reads) == 4                                                       # the gate reads it afresh each pass


# ---------------------------------------------------------------- an entry that bought nothing hands its slot back
def _two_a_day(**over):
    eng = FakeEngine()
    eng.fill_later = True                                                   # the entries stay working
    return eng, AutoPilot(eng, _cfg(max_auto_trades_per_day=2, max_auto_positions=9, **over), bus=SILENT)


def test_an_entry_that_bought_nothing_hands_its_days_slot_back_once():
    eng, ap = _two_a_day()
    first, second, third = mkplay(sym="AAA"), mkplay(sym="BBB"), mkplay(sym="CCC")
    _run(ap, first, second, third)
    assert eng.approved_ids() == [first.id, second.id]                     # the day's two
    eng.working = [w for w in eng.working if w["play_id"] != first.id]      # the first timed out, nothing bought
    assert ap.entry_unfilled(first.id) and ap.status()["auto_trades_today"] == 1
    assert not ap.entry_unfilled(first.id)                                  # once
    assert not ap.entry_unfilled("play_by_hand") and ap.status()["auto_trades_today"] == 1
    assert ap.to_runtime()["count_today_by_kind"] == {"INTRADAY": 1}        # its kind's count comes back too
    _run(ap, first, third)
    assert eng.approved_ids() == [first.id, second.id, third.id]           # the slot went to a new setup, not a chase


def test_the_slots_that_can_come_back_survive_a_restart_but_not_the_night():
    import datetime as dt

    from autotradebot.util import clock

    eng, ap = _two_a_day()
    play = mkplay(sym="AAA")
    _run(ap, play)
    saved = ap.to_runtime()
    assert saved["counted_today"] == {play.id: "INTRADAY"} and saved["sent_today"] == 1
    again = AutoPilot(eng, _cfg(max_auto_trades_per_day=2, max_auto_positions=9), bus=SILENT)
    again.load_runtime(saved)
    assert again.entry_unfilled(play.id) and again.status()["auto_trades_today"] == 0
    assert again.to_runtime()["sent_today"] == 1                            # an order went out all the same

    tomorrow = AutoPilot(eng, _cfg(), bus=SILENT)
    tomorrow.load_runtime({**saved, "day": (clock.session_date() - dt.timedelta(days=1)).isoformat()})
    assert not tomorrow.entry_unfilled(play.id)
    damaged = AutoPilot(eng, _cfg(), bus=SILENT)
    damaged.load_runtime({**saved, "counted_today": ["not", "a", "map"], "sent_today": "x"})
    assert damaged._counted == {} and damaged._sent_today == damaged._count_today


def test_an_entry_that_ends_before_approve_returns_still_hands_its_slot_back():
    class _Quick(FakeEngine):
        """The order sync hears the broker cancel it while approve_play is still busy."""

        def approve_play(self, pid, operator="operator"):
            out = super().approve_play(pid, operator)
            ap.entry_unfilled(pid)
            return out

    eng = _Quick()
    eng.fill_later = True
    ap = AutoPilot(eng, _cfg(max_auto_trades_per_day=2, max_auto_positions=9), bus=SILENT)
    _run(ap, mkplay(sym="AAA"))
    assert len(eng.approved) == 1 and ap.status()["auto_trades_today"] == 0 and ap._sent_today == 1


def test_an_entry_the_broker_didnt_answer_in_time_counts_as_sent_and_keeps_its_slot_until_known_unsent():
    eng, ap = _two_a_day()
    eng.approve_play = lambda pid, operator="operator": {"ok": False, "sent_unknown": True,
                                                         "reason": "IBKR didn't answer the order in time"}
    play = mkplay(sym="AAA")
    _run(ap, play)
    assert ap.status()["auto_trades_today"] == 1 and ap._sent_today == 1 and play.id in ap._auto_play_ids
    assert play.id not in ap._refused
    assert ap.entry_unfilled(play.id) and ap.status()["auto_trades_today"] == 0   # it never went out, as it turns out
    assert ap._sent_today == 1


def test_an_approve_that_fails_takes_no_slot_and_one_that_crashes_keeps_it():
    eng, ap = _two_a_day()
    eng.approve_play = lambda pid, operator="operator": {"ok": False, "reason": "size rounds to zero"}
    _run(ap, mkplay(sym="AAA"))
    assert ap.status()["auto_trades_today"] == 0 and ap._counted == {} and ap._sent_today == 0

    def crash(pid, operator="operator"):
        raise RuntimeError("the database went away after the order was sent")

    eng.approve_play = crash
    play = mkplay(sym="BBB")
    with pytest.raises(RuntimeError):
        _run(ap, play)
    assert ap.status()["auto_trades_today"] == 1 and play.id in ap._auto_play_ids   # the order may be out


def test_orders_sent_stop_at_twice_the_daily_cap_even_when_none_of_them_bought_anything():
    eng, ap = _two_a_day()
    for i in range(4):
        play = mkplay(sym=f"S{i}")
        _run(ap, play)
        eng.working.clear()
        assert ap.entry_unfilled(play.id)                                   # refused by the broker, every one
    last = mkplay(sym="S9")
    _run(ap, last)
    assert len(eng.approved) == 4 and "4 entry orders sent today" in ap.verdict(last)
    ap._room_cache = None
    bar = ap.decorate_play(_row("INTRADAY"))["autopilot"]                  # the play's bar says so too: amber, not green
    assert bar["eligible"] and "4 entry orders sent today" in bar["waiting"]
    assert (ap.status()["sent_today"], ap.status()["sent_ceiling"], ap.status()["auto_trades_today"]) == (4, 4, 0)


def test_a_new_session_starts_the_slots_and_the_orders_sent_afresh():
    eng, ap = _two_a_day()
    play = mkplay(sym="AAA")
    _run(ap, play)
    ap._sent_today = 4
    ap._day = "2000-01-03"                                                  # the session has turned over
    assert not ap.entry_unfilled(play.id)                                   # yesterday's entry hands back nothing today
    assert (ap._count_today, ap._sent_today, ap._counted) == (0, 0, {})


# ---------------------------------------------------------------- setups that lose in the replay are skipped in practice too
def _losing(trades=40, r=-0.10, held=12, held_r=-0.08):
    return {"trades": trades, "expectancy_r": r, "out_of_sample": {"trades": held, "expectancy_r": held_r}}


def _practice(record=None, **over):
    eng = FakeEngine()
    if record is not None:
        eng.records["opening_range_breakout"] = record
    return eng, AutoPilot(eng, _cfg(**{"require_proven": False, "trade_types": ["INTRADAY", "SWING"], **over}), bus=SILENT)


def test_a_day_setup_that_loses_in_the_replay_is_skipped_with_proof_off():
    eng, ap = _practice(_losing())
    p = mkplay()
    _run(ap, p)
    assert eng.approved == [] and eng.assess_calls == []
    assert "loses in the replay the way Autopilot takes it: -0.10R a trade over 40 trades, -0.08R over the 12"         in ap.verdict(p)
    assert p.id not in ap._acted                                             # never cached: a new record lifts it
    eng.records["opening_range_breakout"] = _losing(held_r=-0.01)
    _run(ap, p)
    assert eng.approved_ids() == [p.id]


@pytest.mark.parametrize("record, over", [
    (_losing(held_r=-0.01), {}),                                            # the held-out sessions don't lose
    (_losing(trades=29), {}),                                               # not enough trades to say
    (_losing(held=9), {}),                                                  # too few in the held-out sessions to say
    (_losing(), {"skip_replay_losers": "off"}),
])
def test_a_setup_without_evidence_it_loses_is_still_practised(record, over):
    eng, ap = _practice(record, **over)
    p = mkplay()
    _run(ap, p)
    assert eng.approved_ids() == [p.id]


def test_the_loser_bar_is_the_replay_loser_r_setting():
    eng, ap = _practice(_losing(r=-0.08, held_r=-0.06))
    assert "loses in the replay" in ap._pre_gate(mkplay(), 100_000.0)      # past the 0.05R default
    ap.configure(replay_loser_r=0.10)
    assert ap._pre_gate(mkplay(), 100_000.0) is None                        # short of a 0.10R bar


def test_swing_losers_are_skipped_only_when_asked():
    eng, ap = _practice(_losing())
    swing = mkplay(tf=Timeframe.SWING)
    assert ap._pre_gate(swing, 100_000.0) is None                           # 'day' by default
    ap.configure(skip_replay_losers="all")
    assert "loses in the replay" in ap._pre_gate(swing, 100_000.0)


def test_a_setup_losing_in_its_own_trades_is_skipped():
    eng, ap = _practice()
    eng.live["opening_range_breakout"] = {"trades": 10, "expectancy_r": -0.40}
    assert "is losing in the app's own trades" in ap._pre_gate(mkplay(), 100_000.0)
    assert "simulator" in ap._pre_gate(mkplay(), 100_000.0)
    eng.live["opening_range_breakout"] = {"trades": 10, "expectancy_r": -0.30}
    assert "is losing in the app's own trades" in ap._pre_gate(mkplay(), 100_000.0)   # at the bar counts
    eng.live["opening_range_breakout"] = {"trades": 10, "expectancy_r": -0.20}
    assert ap._pre_gate(mkplay(), 100_000.0) is None                        # losing, but short of the bar
    eng.live["opening_range_breakout"] = {"trades": 9, "expectancy_r": -0.40}
    assert ap._pre_gate(mkplay(), 100_000.0) is None


def _one_setup():
    return SimpleNamespace(strategies=[SimpleNamespace(key="opening_range_breakout", timeframe=Timeframe.INTRADAY)])


def test_with_proof_asked_for_the_proof_message_wins():
    eng, ap = _practice(_losing(), require_proven=True)
    eng.scanner = _one_setup()                                              # a setup for the settings' list to name
    assert "averaged -0.10R" in ap._pre_gate(mkplay(), 100_000.0)
    assert ap.status()["replay_losers"] == []
    ap.configure(require_proven=False)
    assert [row["strategy"] for row in ap.status()["replay_losers"]] == ["opening_range_breakout"]
    live = AutoPilot(FakeEngine(mode="live"), _cfg(allow_live=True, require_proven=False), bus=SILENT)
    live.engine.records["opening_range_breakout"] = _losing()
    live.engine.scanner = _one_setup()
    assert "averaged -0.10R" in live._pre_gate(mkplay(), 100_000.0)
    assert live.status()["replay_losers"] == []


def test_the_badge_and_the_settings_name_the_skipped_setup():
    eng, ap = _practice(_losing())
    eng.scanner = SimpleNamespace(strategies=[SimpleNamespace(key="opening_range_breakout", timeframe=Timeframe.INTRADAY),
                                              SimpleNamespace(key="other_setup", timeframe=Timeframe.INTRADAY)])
    bar = ap.decorate_play(mkplay().to_row())["autopilot"]
    assert not bar["eligible"] and "loses in the replay" in bar["why_not"]
    [row] = ap.status()["replay_losers"]
    assert row["strategy"] == "opening_range_breakout" and "loses in the replay" in row["why"]


def test_a_pass_reads_each_record_once():
    eng, ap = _practice(_losing(held_r=-0.01), max_auto_positions=9, max_auto_trades_per_day=9)
    reads = []
    eng.strategy_record = lambda key: reads.append(key) or eng.records.get(key)
    _run(ap, mkplay(sym="AAA"), mkplay(sym="BBB"), mkplay(sym="CCC"))
    assert len(eng.approved) == 3 and reads == ["opening_range_breakout"]


def test_the_loser_settings_are_saved_and_restored():
    eng, ap = _practice()
    assert ap.skip_replay_losers == "day" and ap.replay_loser_r == 0.05
    ap.configure(skip_replay_losers="all", replay_loser_r=0.1)
    ap.configure(skip_replay_losers="nonsense")                             # unknown -> the default scope
    assert ap.skip_replay_losers == "day"
    ap.configure(skip_replay_losers="all")
    saved = ap.to_runtime()
    assert saved["skip_replay_losers"] == "all" and saved["replay_loser_r"] == 0.1
    again = AutoPilot(eng, _cfg(), bus=SILENT)
    again.load_runtime(saved)
    assert again.skip_replay_losers == "all" and again.replay_loser_r == 0.1
    assert again.status()["skip_replay_losers"] == "all"


# ---------------------------------------------------------------- the status strip: what it is doing now and why
@pytest.fixture
def session_open(monkeypatch):
    """The regular session open, hours from the close - the clock-of-day states have a test of their own."""
    from autotradebot.execution import autopilot as module

    monkeypatch.setattr(module.clock, "is_market_open", lambda ts=None: True)
    monkeypatch.setattr(module.clock, "minutes_to_close", lambda ts=None: 300.0)
    return module


def _headline(ap):
    return ap.status()["headline"]


def test_the_strip_says_off_paper_only_blind_and_stopped(session_open):
    assert _headline(AutoPilot(FakeEngine(), _cfg(enabled=False), bus=SILENT))["state"] == "off"
    h = _headline(AutoPilot(FakeEngine(mode="live"), _cfg(), bus=SILENT))
    assert h["state"] == "blocked" and "paper-only" in h["text"]
    eng = FakeEngine()
    eng.prices_refused = lambda: "the login is active somewhere else"
    h = _headline(AutoPilot(eng, _cfg(), bus=SILENT))
    assert h["state"] == "blind" and h["text"].endswith("the login is active somewhere else")
    eng = FakeEngine(equity=10_000.0)
    eng.repo._closed = [_closed("AAA", -250.0)]
    ap = AutoPilot(eng, _cfg(max_daily_loss_pct=2.0), bus=SILENT)
    _run(ap, mkplay(sym="BBB"))
    h = _headline(ap)
    assert h["state"] == "stopped" and "past the daily limit of 2% of equity" in h["text"]
    assert "practice size" not in h["text"]                                  # it won't resume today


def test_when_two_states_hold_the_strip_says_the_one_that_comes_first(session_open, monkeypatch):
    # blind before done: prices refused on a day whose entries are all used
    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(max_auto_trades_per_day=2, max_auto_positions=9), bus=SILENT)
    _run(ap, mkplay(sym="AAA"), mkplay(sym="BBB"))
    assert _headline(ap)["state"] == "done"
    eng.prices_refused = lambda: "the login is active somewhere else"
    assert _headline(ap)["state"] == "blind"

    # stopped before full: the daily loss stop while every position is taken
    eng = FakeEngine(equity=10_000.0)                                        # 2% of equity = 200
    eng.repo._closed = [_closed("AAA", -250.0)]
    eng.repo._open = [{"id": "t_BBB", "symbol": "BBB", "strategy": "opening_range_breakout",
                       "timeframe": "INTRADAY", "entry_context": {"by": "autopilot"}}]  # taken before a restart
    ap = AutoPilot(eng, _cfg(max_auto_positions=1, max_daily_loss_pct=2.0), bus=SILENT)
    assert _headline(ap)["state"] == "full"
    _run(ap, mkplay(sym="CCC"))                                              # the gate finds the loss
    assert _headline(ap)["state"] == "stopped"
    eng.prices_refused = lambda: "the login is active somewhere else"       # ...and blind before stopped
    assert _headline(ap)["state"] == "blind"

    # done before full: the day's entries used and every position taken
    ap = AutoPilot(FakeEngine(), _cfg(max_auto_trades_per_day=1, max_auto_positions=1), bus=SILENT)
    _run(ap, mkplay())
    assert _headline(ap)["state"] == "done"
    ap.configure(max_auto_trades_per_day=5)
    assert _headline(ap)["state"] == "full"

    # full before closed: every position taken while day trades wait for the open
    monkeypatch.setattr(session_open.clock, "is_market_open", lambda ts=None: False)
    assert _headline(ap)["state"] == "full"
    ap.configure(max_auto_positions=2)
    assert _headline(ap)["state"] == "closed"


def test_the_strip_says_done_once_the_days_entries_are_used(session_open):
    eng = FakeEngine()
    eng.fill_later = True
    ap = AutoPilot(eng, _cfg(max_auto_trades_per_day=2, max_auto_positions=9), bus=SILENT)
    first, second = mkplay(sym="AAA"), mkplay(sym="BBB")
    _run(ap, first, second)
    assert _headline(ap)["text"].startswith("Done for today: 2 of the 2 entries a day are used")
    assert ap.entry_unfilled(first.id)                                       # it bought nothing: its slot is back
    eng.working = [w for w in eng.working if w["play_id"] != first.id]
    assert _headline(ap)["state"] == "taking"
    _run(ap, mkplay(sym="CCC"))
    h = _headline(ap)
    assert h["state"] == "done" and "(1 more bought nothing and gave their slots back)" in h["text"]


def test_the_strip_says_full_while_every_position_is_taken(session_open):
    ap = AutoPilot(FakeEngine(), _cfg(max_auto_positions=1, max_auto_trades_per_day=5), bus=SILENT)
    _run(ap, mkplay())
    h = _headline(ap)
    assert h["state"] == "full" and "1 of 1 positions taken" in h["text"]


def test_the_strip_says_which_kind_is_full_under_the_split(session_open):
    eng, ap = _split_pilot(70.0, trade_types=["SWING"])
    _run(ap, *[mkplay(sym=f"S{i}", tf=Timeframe.SWING) for i in range(5)])
    h = _headline(ap)
    assert h["state"] == "kind-full" and h["text"].startswith("Swing trades hold 3 of the 3 positions")
    ap.configure(trade_types=["INTRADAY", "SWING"])                           # the day trades still have room
    h = _headline(ap)
    assert h["state"] == "taking" and h["text"].startswith("Taking day trades")
    assert "swing trades hold 3 of the 3 positions" in h["text"]


def test_the_strip_says_when_the_clock_keeps_day_trades_out(monkeypatch):
    from autotradebot.execution import autopilot as module

    ap = AutoPilot(FakeEngine(), _cfg(min_minutes_to_close=30), bus=SILENT)
    monkeypatch.setattr(module.clock, "is_market_open", lambda ts=None: True)
    monkeypatch.setattr(module.clock, "minutes_to_close", lambda ts=None: 20.0)
    h = _headline(ap)
    assert h["state"] == "late" and h["text"].startswith("No new day trades in the last 30 minutes")
    monkeypatch.setattr(module.clock, "is_market_open", lambda ts=None: False)
    monkeypatch.setattr(module.clock, "minutes_to_close", lambda ts=None: 1e9)
    assert _headline(ap)["state"] == "closed"
    ap.configure(trade_types=["INTRADAY", "SWING"])                           # swing trades don't wait for the open
    h = _headline(ap)
    assert h["state"] == "taking" and h["text"].startswith("Taking swing trades")
    assert "day trades wait for the open" in h["text"]


def test_the_strip_counts_down_to_the_next_entry_without_touching_the_entry_times(session_open, monkeypatch):
    eng = FakeEngine()
    eng.entry_pace_seconds = lambda timeframe: 60.0
    ap = AutoPilot(eng, _cfg(max_new_per_cycle=1, max_auto_positions=9, max_auto_trades_per_day=9), bus=SILENT)
    clock_ = {"t": 10_000.0}
    monkeypatch.setattr(session_open.time, "monotonic", lambda: clock_["t"])
    _run(ap, mkplay(sym="AAA"))
    ap._entries_at.insert(0, clock_["t"] - 7200.0)                           # an old one the gate's own check drops
    before = list(ap._entries_at)
    clock_["t"] += 18.0
    h = _headline(ap)
    assert h["state"] == "pacing" and h["next_entry_in_s"] == 42 and "1 new entry per scan cycle" in h["text"]
    assert ap._entries_at == before                                          # read, never pruned
    clock_["t"] += 45.0
    h = _headline(ap)
    assert h["state"] == "taking" and h["next_entry_in_s"] == 0


def test_the_strip_says_whether_unproven_setups_trade_at_practice_size(session_open):
    ap = AutoPilot(FakeEngine(), _cfg(require_proven=False), bus=SILENT)
    h = _headline(ap)
    assert h["state"] == "taking" and h["practice"] and "unproven setups at practice size" in h["text"]
    assert h["text"].startswith("Taking day trades - 0 of 2 positions, 0 of 3 entries today")
    ap.configure(require_proven=True)
    h = _headline(ap)
    assert h["state"] == "taking" and not h["practice"] and "practice" not in h["text"]


def test_the_strip_names_the_replay_losers_it_skips(session_open):
    eng, ap = _practice(_losing())
    eng.scanner = SimpleNamespace(strategies=[SimpleNamespace(key="opening_range_breakout", timeframe=Timeframe.INTRADAY)])
    h = _headline(ap)
    assert h["skipping"] == ["opening_range_breakout"]
    assert "skipping the replay losers: opening_range_breakout" in h["text"]


def test_todays_tally_groups_the_venues_closed_trades_by_setup():
    eng = FakeEngine()
    eng._venue = "ibkr-paper"

    def closed(sym, strategy, pl, r, broker="ibkr-paper"):
        return {"id": f"t_{sym}", "symbol": sym, "strategy": strategy, "status": "CLOSED", "realized_pl": pl,
                "r_multiple": r, "broker": broker}

    eng.repo._closed = [closed("AAA", "setup_a", 120.0, 1.2), closed("BBB", "setup_a", -50.0, -0.5),
                        closed("CCC", "setup_b", -80.0, -1.0),
                        closed("DDD", "setup_b", -500.0, -1.0, broker="paper"),              # another account
                        {**closed("EEE", "pairs_x", 40.0, None), "pair_id": "p1"},          # one leg of a pair
                        {**closed("FFF", "setup_c", 0.0, None), "status": "OPEN"}]
    reads = []
    trades_on = eng.repo.trades_on
    eng.repo.trades_on = lambda day: reads.append(day) or trades_on(day)
    ap = AutoPilot(eng, _cfg(), bus=SILENT)
    st = ap.status()
    assert st["today"] == [{"strategy": "setup_a", "closed": 2, "wins": 1, "r": 0.7, "pl": 70.0},
                           {"strategy": "setup_b", "closed": 1, "wins": 0, "r": -1.0, "pl": -80.0}]
    assert st["realized_today"] == 30.0                                       # the pair leg still counts in the P/L
    ap.status()
    assert len(reads) == 1                                                   # one read for both, kept 20 s


def test_a_bare_off_in_config_yaml_means_off():
    import yaml

    from autotradebot.config import AutopilotCfg

    cfg = AutopilotCfg(**yaml.safe_load("skip_replay_losers: off"))         # YAML reads a bare off as false
    assert cfg.skip_replay_losers == "off"
    assert AutopilotCfg(**yaml.safe_load("skip_replay_losers: on")).skip_replay_losers == "day"
    eng, ap = _practice(skip_replay_losers=False)
    assert ap.skip_replay_losers == "off"
    ap.configure(skip_replay_losers="day")
    ap.load_runtime({"skip_replay_losers": False})
    assert ap.skip_replay_losers == "off"



def test_a_zero_per_cycle_cap_from_the_files_reads_as_one_and_the_strip_still_draws(session_open):
    eng = FakeEngine()
    eng.entry_pace_seconds = lambda timeframe: 60.0
    ap = AutoPilot(eng, _cfg(max_new_per_cycle=0), bus=SILENT)             # as config.yaml might say
    assert ap.max_new_per_cycle == 1 and ap.status()["headline"]["state"] == "taking"
    ap.load_runtime({"max_new_per_cycle": 0})                                # as runtime.json might say
    assert ap.max_new_per_cycle == 1 and ap.status()["headline"]["state"] == "taking"
