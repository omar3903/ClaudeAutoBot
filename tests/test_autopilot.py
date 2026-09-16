"""AutoPilot - hands-off entry gate.

Exits are already automatic (ExitManager); these tests pin down when the pilot
will and will not take the *entry*: trade-type filter, confidence / RR floors,
position + per-day caps, the paper-only-in-live hard gate, and dry-run.
"""

from __future__ import annotations

from types import SimpleNamespace

from tos_bot.core.enums import AssetClass, Side, StrategyKind, Timeframe
from tos_bot.core.models import Account, Play, Position
from tos_bot.execution.autopilot import AutoPilot

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

    def approve_play(self, pid, operator="operator"):
        self.approved.append((pid, operator))
        tid = f"trade_{pid}"
        pl = self._plays.get(pid)
        if self.fill_later:
            self.working.append({"order_id": f"o_{pid}", "play_id": pid, "symbol": pl.symbol,
                                 "strategy": pl.strategy, "qty": 10, "risk": self.est_risk})
            return {"ok": True, "status": "WORKING"}
        # an open position whose $-risk equals est_risk (entry 100, stop 80, x10)
        self.repo._open.append({
            "id": tid, "symbol": getattr(pl, "symbol", "X"),
            "strategy": getattr(pl, "strategy", "opening_range_breakout"),
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
    from tos_bot.util import clock
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
    p = mkplay()
    _run(AutoPilot(eng2, _cfg(allow_live=True), bus=SILENT), p)
    assert eng2.approved_ids() == [p.id]


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
def _closed(symbol, pl, broker="paper"):
    return {"id": f"t_{symbol}", "symbol": symbol, "status": "CLOSED", "realized_pl": pl, "broker": broker,
            "session_date": "2000-01-01"}


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
