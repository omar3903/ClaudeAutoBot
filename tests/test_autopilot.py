"""AutoPilot - hands-off entry gate.

Exits are already automatic (ExitManager); these tests pin down when the pilot
will and will not take the *entry*: trade-type filter, confidence / RR floors,
position + per-day caps, the paper-only-in-live hard gate, and dry-run.
"""

from __future__ import annotations

from types import SimpleNamespace

from tos_bot.core.enums import AssetClass, Side, StrategyKind, Timeframe
from tos_bot.core.models import Play
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

    def get_open_trade_for_symbol(self, sym):
        return {"symbol": sym} if sym in self.held else None


class FakeEngine:
    def __init__(self, mode="paper", equity=100_000.0):
        self.mode = mode
        self.repo = FakeRepo()
        self._account = SimpleNamespace(equity=equity)
        self.assess_calls = []
        self.approved = []
        self.can_execute = True
        self.est_risk = 200.0
        self._plays = {}                        # pid -> Play, populated by _run

    def assess_play(self, pid):
        self.assess_calls.append(pid)
        return {
            "ok": True,
            "can_execute": self.can_execute,
            "reasons": [] if self.can_execute else ["not executable in this session"],
            "order_preview": {"qty": 10, "est_risk": self.est_risk},
        }

    def approve_play(self, pid, operator="operator"):
        self.approved.append((pid, operator))
        tid = f"trade_{pid}"
        pl = self._plays.get(pid)
        # an open position whose $-risk equals est_risk (entry 100, stop 80, x10)
        self.repo._open.append({
            "id": tid, "symbol": getattr(pl, "symbol", "X"),
            "strategy": getattr(pl, "strategy", "opening_range_breakout"),
            "entry_price": 100.0,
            "initial_stop_price": 100.0 - self.est_risk / 10.0, "quantity": 10,
        })
        return {"ok": True, "trade_id": tid}

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


def test_engine_wires_autopilot_into_snapshot():
    """The real engine exposes autopilot in the snapshot and via set_autopilot."""
    from tos_bot.engine import TradingEngine
    eng = TradingEngine()
    snap = eng.snapshot()
    assert "autopilot" in snap and "trade_types" in snap["autopilot"]
    assert "scan_interval_s" in snap and "scan_fast" in snap
    out = eng.set_autopilot(enabled=True, trade_types=["INTRADAY", "SWING"])
    assert out["ok"] and out["autopilot"]["enabled"] is True
    assert set(out["autopilot"]["trade_types"]) == {"INTRADAY", "SWING"}
    eng.set_autopilot(enabled=False)


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


def test_effective_scan_interval_adapts_to_autopilot(monkeypatch):
    from tos_bot.engine import TradingEngine
    from tos_bot.util import clock
    eng = TradingEngine()
    base = eng.settings.config.scanner.interval_seconds
    floor = eng.settings.config.scanner.min_interval_seconds
    fast = eng.settings.config.scanner.autopilot_interval_seconds

    monkeypatch.setattr(clock, "is_market_open", lambda *a, **k: True)

    eng.set_autopilot(enabled=False)
    assert eng._effective_scan_interval() == base                 # off -> normal cadence

    eng.set_autopilot(enabled=True, trade_types=["INTRADAY"])
    eng._last_scan_elapsed = 10.0
    assert eng._effective_scan_interval() == fast                 # armed + open -> fast cadence
    eng._last_scan_elapsed = 200.0                                # a slow cycle
    assert eng._effective_scan_interval() == 203                  # ~back-to-back, not faster
    eng._last_scan_elapsed = 0.0

    monkeypatch.setattr(clock, "is_market_open", lambda *a, **k: False)
    assert eng._effective_scan_interval() == base                 # session closed -> normal
    eng.set_autopilot(enabled=False)
    assert floor <= fast <= base
