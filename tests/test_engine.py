"""The engine: where orders go, dashboard changes applying live, scans on
schedule, quitting with positions open, trading capital, and the check that
removes open-trade records the broker no longer holds."""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import fakes
from test_native_stop import _StopBroker
from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.core.models import Account, Fill, OrderResult, Play, Position
from tos_bot.engine import TradingEngine
from tos_bot.execution.executor import Executor
from tos_bot.engine.research_ops import PRACTICE_LABEL
from tos_bot.engine.reconcile import PositionCheck
from tos_bot.engine.runtime import load_filters
from tos_bot.risk.position_sizing import size_play
from tos_bot.scanner.filters import TradeFilters
from tos_bot.scanner.scanner import BENCHMARK
from tos_bot.util import clock


@pytest.fixture
def gateway():
    return fakes.FakeGateway(fakes.SYMBOLS + [BENCHMARK])


@pytest.fixture
def port():
    """Whether IB Gateway answers on its port."""
    return {"open": False}


def _new_engine(tmp_path, gateway, port) -> TradingEngine:
    return TradingEngine(data_dir=tmp_path / "data", runtime_path=tmp_path / "runtime.json",
                         broker_factory=fakes.broker_factory(gateway), port_check=lambda host, p: port["open"],
                         listings=fakes.FakeListings(fakes.SYMBOLS), fundamentals=fakes.NoFundamentals(),
                         init_database=False)


@pytest.fixture
def engine(tmp_path, gateway, port):
    from tos_bot.persistence.db import DB

    # a database of its own, so open trades left by other tests can't leak in
    DB.init(url=f"sqlite:///{(tmp_path / 'engine.sqlite').as_posix()}")
    DB.create_all()
    e = _new_engine(tmp_path, gateway, port)
    e._bind()
    yield e
    e.stop()
    DB.engine.dispose()
    DB.init(url=os.environ["DATABASE_URL"])


def _play(symbol="AAPL", side=Side.LONG, strategy="vwap_reclaim"):
    return Play(symbol=symbol, side=side, strategy=strategy, kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.SWING, entry=100.0, stop=95.0, targets=[110.0])


def _open(engine, symbol="AAPL", venue="paper", side=Side.LONG, qty=5):
    p = _play(symbol, side)
    p.suggested_qty = qty
    engine.repo.record_play(p)
    return engine.repo.open_trade(p, 100.0, qty, venue)


def _closes_fill(engine):
    """Stand-in for the broker: every close fills at 101."""
    closed = []

    def close(tid, reason="manual"):
        closed.append(engine.repo.get_trade(tid)["symbol"])
        return {"ok": True, "status": "FILLED",
                "trade": engine.repo.close_trade(tid, exit_price=101.0, exit_reason=reason)}

    engine.executor.close_trade = close
    return closed


def _connect(engine, port):
    port["open"] = True
    assert engine._retry_connection(force=True)


# ---------------------------------------------------------------- where orders go
def test_orders_go_to_the_simulator_while_the_gateway_is_down(engine):
    snap = engine.snapshot()
    assert engine.paper_platform == "ibkr" and snap["venue"]["trading_on"] == "paper"
    assert engine.broker.name == "paper" and not snap["data"]["connected"]
    assert snap["connection"]["cls"] == "bad" and snap["connection"]["action"] == "connections"
    assert any("IB Gateway" in b for b in engine.connections.blockers)


def test_the_chosen_account_connects_once_the_gateway_answers(engine, port, gateway):
    assert not engine._retry_connection(force=True)                   # still down
    _connect(engine, port)
    snap = engine.snapshot()
    assert engine._venue == "ibkr-paper" and engine.broker is gateway and gateway.kw["readonly"] is False
    assert snap["data"] == {"source": "ibkr", "connected": True, "delayed": False, "reason": ""}
    assert snap["account"]["equity"] == 50_000 and snap["connection"]["cls"] == "good"
    assert not engine._retry_connection(force=True)                   # already connected
    assert engine.exit_manager.quote_fn("T01").last > 0               # exits are priced off the Gateway


def test_the_snapshot_shows_only_the_end_of_the_account_id(engine, port, gateway, monkeypatch):
    # every open tab gets the snapshot: the account id is masked there as on the Connections panel
    status = gateway.session_status
    monkeypatch.setattr(gateway, "session_status", lambda: {**status(), "account": "DU1234567"})
    _connect(engine, port)
    session = engine.snapshot()["venue"]["ibkr_session"]
    assert session["account"] == "…4567" and session["market_data"] == "live"
    assert gateway.session_status()["account"] == "DU1234567"          # the connection's own copy is untouched
    monkeypatch.setattr(gateway, "session_status", status)
    assert engine.snapshot()["venue"]["ibkr_session"]["account"] is None   # not known yet: nothing to show


def test_the_simulator_takes_only_prices_from_the_gateway(engine, port, gateway):
    assert engine.set_paper_platform("simulator")["ok"]
    _connect(engine, port)
    assert engine._venue == "paper" and engine.broker.name == "paper"
    assert gateway.kw["readonly"] is True and engine.md.attached
    assert engine.snapshot()["connection"]["label"] == "Simulator"


def test_auto_connect_never_moves_orders_away_from_open_positions(engine, port):
    tid = _open(engine, "AAPL")                                          # on the simulator
    port["open"] = True
    assert not engine._retry_connection(force=True)
    assert engine._venue == "paper" and "AAPL" in engine.connections.blockers[0]
    engine.repo.delete_trade(tid)
    engine.quit_state = {"mode": "paper"}
    assert not engine._retry_connection(force=True)                   # never while quitting


def test_live_is_refused_when_the_live_gateway_is_unreachable(engine):
    r = engine.set_mode("live")
    assert not r["ok"] and r["blockers"] and engine.mode == "paper"


def test_switching_is_blocked_while_positions_are_open_here(engine, monkeypatch):
    monkeypatch.setattr(engine.repo, "open_trades", lambda: [{"id": "trd_1", "symbol": "AAPL", "broker": "paper"}])
    r = engine.set_mode("live")
    assert not r["ok"] and "AAPL" in r["reason"] and engine.mode == "paper"
    assert engine.set_paper_platform("simulator")["ok"]                # orders stay on the simulator


def test_unknown_platforms_and_bad_settings_are_rejected(engine):
    assert not engine.set_paper_platform("schwab")["ok"]
    r = engine.save_secrets({"IBKR_PAPER_PORT": "nope"})
    assert not r["ok"] and "whole number" in r["reason"]


# ---------------------------------------------------------------- filters and strategies
def test_trade_filters_normalise_and_explain_refusals():
    f = TradeFilters.build(["long"], ["swing"], ["energy"])
    assert (f.sides, f.timeframes, f.sectors) == (("LONG",), ("SWING",), ("Energy",))
    assert f.refusal("LONG", "SWING", "Energy") is None
    assert "short" in f.refusal("SHORT", "SWING", "Energy")
    assert "intraday" in f.refusal("LONG", "INTRADAY", "Energy")
    assert "Technology" in f.refusal("LONG", "SWING", "Technology")
    assert TradeFilters.build() == TradeFilters()
    with pytest.raises(ValueError):
        TradeFilters.build(sides=[])
    with pytest.raises(ValueError):
        TradeFilters.build(timeframes=["weekly"])


def test_filter_changes_apply_to_the_board_the_scanner_and_the_next_run(engine):
    engine.board.replace([_play("AAPL"), _play("TSLA", side=Side.SHORT)])
    r = engine.set_filters(sides=["LONG"])
    assert r["ok"] and r["removed_plays"] == 1 and not r["rescanning"]     # narrowing just trims
    assert engine.scanner.filters is engine.filters and engine.filters.sides == ("LONG",)
    assert load_filters(engine.runtime.read()["filters"], []) == engine.filters   # remembered
    assert engine._scan_request is None

    r = engine.set_filters(sides=["LONG", "SHORT"])
    assert r["ok"] and r["rescanning"] and engine._scan_request == "full"   # widening rescans
    assert not engine.set_filters(timeframes=[])["ok"]


def test_sector_filter_is_normalised_and_remembered(engine):
    r = engine.set_filters(sectors=["Energy", "Consumer Cyclical"])
    assert r["ok"] and r["filters"]["sectors"] == ["Consumer Discretionary", "Energy"]
    assert engine.runtime.read()["filters"]["sectors"] == ["Consumer Discretionary", "Energy"]
    assert engine.set_filters(sectors=[])["filters"]["sectors"] == []


def test_strategy_panel_switches_setups_live_and_remembers_only_changes(engine):
    key = next(r["key"] for r in engine.strategy_state() if r["enabled"] and r["timeframe"] == "INTRADAY")
    engine.board.replace([_play(strategy=key)])

    r = engine.set_strategy(key, enabled=False)
    assert r["ok"] and key not in {s.key for s in engine.scanner.strategies}
    assert engine.board.plays == {} and engine._scan_request is None       # its plays leave the board
    assert engine.runtime.read()["strategies"] == {key: {"enabled": False}}

    r = engine.set_strategy(key, weight=2.5)
    assert r["ok"] and next(x for x in r["strategies"] if x["key"] == key)["weight"] == 2.5
    assert engine._scan_request == "full"                                 # no watchlist yet
    assert not engine.set_strategy(key, weight=9)["ok"]
    assert not engine.set_strategy(key, weight="heavy")["ok"]
    assert not engine.set_strategy("no_such_setup", enabled=True)["ok"]

    engine.set_strategy(key, enabled=True)                                # back to the config default
    assert engine.strategy_overrides == {key: {"weight": 2.5}}
    assert key in {s.key for s in engine.scanner.strategies}
    assert engine.reset_strategies()["ok"] and engine.strategy_overrides == {}


def test_autopilot_is_part_of_the_snapshot_and_remembered(engine):
    snap = engine.snapshot()
    assert "trade_types" in snap["autopilot"] and snap["scan"]["fast"] is False
    # the status strip: what it is doing now and why, and today's closed trades by setup
    assert snap["autopilot"]["headline"]["state"] == "off" and snap["autopilot"]["today"] == []
    out = engine.set_autopilot(enabled=True, trade_types=["INTRADAY", "SWING"])
    assert out["ok"] and out["autopilot"]["enabled"] is True
    assert set(out["autopilot"]["trade_types"]) == {"INTRADAY", "SWING"}
    assert out["autopilot"]["headline"]["state"] != "off" and out["autopilot"]["headline"]["text"]
    assert engine.runtime.read()["autopilot"]["enabled"] is True


# ---------------------------------------------------------------- scans
def test_scan_settings_are_checked_saved_and_used(engine, tmp_path, gateway, port):
    r = engine.set_scan_settings(cycle_minutes=4, premarket_time="07:45")
    assert r["ok"] and "07:45" in r["note"] and "4 minutes" in r["note"]
    assert r["scan"]["settings"]["premarket_time"] == "07:45"
    assert engine.runtime.read()["scan"]["cycle_minutes"] == 4
    assert "04:00 to 09:00" in engine.set_scan_settings(premarket_time="09:45")["reason"]
    assert "between 3 and 5" in engine.set_scan_settings(cycle_minutes=2)["reason"]
    assert engine.set_scan_settings(cycle_minutes=4)["note"] == "No change."
    assert _new_engine(tmp_path, gateway, port).scan_settings.cycle_minutes == 4      # after a restart


def test_scan_requests_queue_and_never_downgrade_a_full_scan(engine):
    r = engine.request_scan("cycle")
    assert r["ok"] and r["kind"] == "full"                                # no watchlist yet
    assert engine.request_scan("cycle")["ok"] and engine._scan_request == "full"
    assert not engine.request_scan("weekly")["ok"]


def test_without_the_gateway_a_scan_backs_off_quietly(engine):
    engine._run_scan("full")
    assert engine._scan_running is None and len(engine.board) == 0
    assert engine._scan_retry_at > time.monotonic() and engine._due_scan() is None


def test_scans_follow_the_schedule(engine, port, monkeypatch):
    assert engine._due_scan() == "full"                                   # nothing built yet
    _connect(engine, port)
    engine._run_scan("full")
    assert engine.scanner.watchlist.hot_symbols() and engine.watchlist_state()["watchlist"]["hot"]

    engine._gappers_session = engine.scanner.watchlist.session           # the gap check is covered below
    monkeypatch.setattr(clock, "is_market_open", lambda *a, **k: False)
    assert engine._due_scan() is None                                     # built for this session; market shut
    monkeypatch.setattr(clock, "is_market_open", lambda *a, **k: True)
    assert engine._due_scan() == "cycle"
    engine._run_scan("cycle")
    engine._last_plays_at = time.monotonic()                              # the quick re-check has just run too
    assert engine._due_scan() is None                                     # the next cycle is minutes away
    engine.board.replace([_play("AAPL")], None)
    engine._last_plays_at -= engine.settings.config.scanner.plays_refresh_seconds + 1
    assert engine._due_scan() == "plays"                                  # plays on the board are re-checked
    engine._run_scan("plays")
    assert engine._due_scan() is None and engine.scan_status()["running"] is None

    engine._last_wide_at -= engine.scan_settings.wide_minutes * 60 + 1     # half an hour on: every stock
    assert engine._due_scan() == "wide"
    engine._run_scan("wide")
    assert engine.scan_status()["last_wide"]["kind"] == "wide" and engine._due_scan() is None
    engine._last_wide_at -= engine.scan_settings.wide_minutes * 60 + 1
    assert engine.set_scan_settings(wide_minutes=0)["note"] == "The wide scan is off."
    assert engine._due_scan() is None                                     # switched off: never due
    assert engine.request_scan("wide")["ok"]                              # but it can still be asked for...
    assert engine._due_scan() == "wide" and engine._due_scan() is None    # ...and the request is honoured once

    engine.set_autopilot(enabled=True, trade_types=["INTRADAY"])
    engine._last_fast_at -= engine.settings.config.scanner.fast_cycle_seconds + 1
    assert engine._due_scan() == "fast"
    status = engine.scan_status()
    assert status["last_full"]["kind"] == "full" and status["last_cycle"]["kind"] == "cycle"

    # the gap check: due once between its time and the open, on today's watchlist; it moves no plays
    session = engine.scanner.watchlist.session
    engine._gappers_session = None
    at = dt.datetime.combine(session, dt.time(9, 20), tzinfo=clock.NY)
    monkeypatch.setattr(clock, "now_ny", lambda: at)
    assert engine._due_scan() == "gappers"
    before = set(engine.board.plays)
    engine._run_scan("gappers")
    assert engine._gappers_session == session and set(engine.board.plays) == before
    assert engine.scan_status()["last_gappers"]["kind"] == "gappers" and engine._due_scan() != "gappers"
    assert engine.request_scan("gappers")["ok"] and engine._scan_request == "gappers"


def test_the_days_replay_starts_itself_after_the_morning_scan_and_only_once(engine, port, monkeypatch):
    _connect(engine, port)
    started = []
    monkeypatch.setattr(engine, "start_replay", lambda *a, **k: started.append(1) or {"ok": True, "note": "n"})
    engine.settings.config.replay.daily = True

    monkeypatch.setattr(clock, "is_market_open", lambda *a, **k: True)
    engine._run_scan("full")
    assert started == []                                                    # not while the session is open
    monkeypatch.setattr(clock, "is_market_open", lambda *a, **k: False)
    engine._run_scan("full")
    assert started == [1] and engine._replay_session == clock.session_date()
    engine._run_scan("full")
    assert started == [1]                                                   # once a session, however often it scans
    engine._run_scan("cycle")
    assert started == [1]                                                   # and only after the full scan

    engine._replay_session = None
    engine.replay._thread = None
    monkeypatch.setattr(type(engine.replay), "running", property(lambda self: True))
    engine._run_scan("full")
    assert started == [1]                                                   # not while one is already running
    monkeypatch.setattr(type(engine.replay), "running", property(lambda self: False))
    engine._replay_session, engine.quit_state = None, {"by": "operator"}
    engine._run_scan("full")
    assert started == [1]                                                   # nor while the app is quitting
    engine.quit_state = None
    engine.settings.config.replay.daily = False
    engine._replay_session = None
    engine._run_scan("full")
    assert started == [1]                                                   # switched off in config.yaml


def test_a_replay_that_couldnt_start_is_tried_again_and_never_stops_the_scans(engine, port, monkeypatch):
    _connect(engine, port)
    monkeypatch.setattr(clock, "is_market_open", lambda *a, **k: False)
    engine.settings.config.replay.daily = True
    monkeypatch.setattr(engine, "start_replay", lambda *a, **k: {"ok": False, "reason": "not connected"})
    engine._run_scan("full")
    assert engine._replay_session is None                                   # refused: not done for the day

    def gateway_dropped(*a, **k):
        raise RuntimeError("IB Gateway isn't connected")

    monkeypatch.setattr(engine, "start_replay", gateway_dropped)
    engine._run_scan("full")                                                # raises inside - the scan still completes
    assert engine.scan_status()["last_full"]["kind"] == "full"

    # the scan loop itself keeps going whatever one pass throws
    passes = []

    def one_bad_pass():
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("boom")
        engine._stop.set()

    monkeypatch.setattr(engine, "_due_scan", one_bad_pass)
    monkeypatch.setattr(engine._stop, "wait", lambda *a, **k: None)
    monkeypatch.setattr(engine._scan_wake, "wait", lambda *a, **k: None)
    engine._scan_loop()
    assert passes == [1, 1]


def test_the_session_the_daily_replay_ran_for_survives_a_restart(engine, tmp_path, gateway, port):
    engine._replay_session = clock.session_date()
    engine.stop()
    again = _started_again(tmp_path, gateway, port)
    try:
        assert again._replay_session == clock.session_date()
    finally:
        again.stop()


def test_a_replay_a_restart_interrupted_is_resumed_once_and_an_older_one_is_dropped(engine, port, monkeypatch):
    last = clock.prev_trading_day(clock.session_date())
    checkpoint = engine.replay.checkpoint
    checkpoint.begin({"fingerprint": "f", "last": last.isoformat(), "sessions": 7, "swing_sessions": 30, "jobs": 5},
                     resume=False)
    checkpoint.add(("swing", "T01"), [])
    started, events = [], []
    monkeypatch.setattr(engine.replay, "start", lambda **kw: started.append(kw) or {"ok": True, "note": "n"})
    monkeypatch.setattr(engine, "_publish", lambda topic, **p: events.append((topic, p)))

    engine._resume_replay()                                                  # no Gateway yet
    _connect(engine, port)
    engine._resume_replay()                                                  # no watchlist yet
    engine.settings.config.replay.daily = False
    engine._run_scan("full")
    engine.quit_state = {"by": "operator"}
    engine._resume_replay()                                                  # quitting
    engine.quit_state = None
    monkeypatch.setattr(type(engine.replay), "running", property(lambda self: True))
    engine._resume_replay()                                                  # another replay running
    monkeypatch.setattr(type(engine.replay), "running", property(lambda self: False))
    assert started == [] and checkpoint.path.exists()

    engine._resume_replay()
    engine._resume_replay()
    assert len(started) == 1 and started[0]["resume"]                        # once, as a resumed run...
    assert (started[0]["sessions"], started[0]["swing_sessions"]) == (7, 30)  # ...of the interrupted run's sessions
    assert [p for topic, p in events if topic == "replay.started"][-1]["resumed"]

    engine._replay_resumed = False                                           # a later start, with an older run's file
    checkpoint.begin({"fingerprint": "f", "last": clock.prev_trading_day(last).isoformat(), "sessions": 7,
                      "swing_sessions": 30, "jobs": 5}, resume=False)
    engine._resume_replay()
    assert len(started) == 1 and not checkpoint.path.exists()                # deleted, nothing started


# ---------------------------------------------------------------- the position size factor
def test_the_size_factor_resizes_the_plays_is_remembered_and_refuses_what_is_out_of_range(engine, port):
    _connect(engine, port)
    assert engine.size_factor == 1.0 and engine.capital_state()["size_factor"] == 1.0
    p = _play()                                                                 # entry 100, stop 95
    engine.board.replace([p])
    engine._size_plays([p])
    usual = p.suggested_qty
    out = engine.set_size_factor(2)
    assert out["ok"] and out["capital"]["size_factor"] == 2.0 and "2 times the usual size" in out["note"]
    assert p.suggested_qty > usual                                              # the board is sized again at once
    assert engine.runtime.read()["sizing"] == {"factor": 2.0}                   # remembered
    for bad in (-0.1, 5.5, "lots", None, float("nan")):
        assert not engine.set_size_factor(bad)["ok"] and engine.size_factor == 2.0
    zero = engine.set_size_factor(0)
    assert zero["ok"] and "sized at nothing" in zero["note"] and p.suggested_qty == 0
    assert any("size factor is 0" in r for r in engine.assess_play(p.id)["reasons"])


def test_the_size_factor_on_disk_is_read_back_and_a_bad_one_is_the_usual_size():
    from tos_bot.engine.runtime import load_size_factor
    assert load_size_factor({"factor": 3.5}) == 3.5
    assert load_size_factor(None) == 1.0 and load_size_factor({"factor": 9}) == 1.0
    assert load_size_factor({"factor": True}) == 1.0 and load_size_factor({"factor": "2"}) == 1.0


# ---------------------------------------------------------------- the split, and changes made while it runs
def test_entries_still_working_count_in_their_kinds_share_of_the_capital(engine, port):
    _connect(engine, port)
    engine.set_filters(timeframes=["INTRADAY", "SWING"])
    engine.set_capital_split(50)
    room = lambda kind: engine.sizing_account(kind).raw["capital_room"]          # noqa: E731
    before = {k: room(k) for k in ("INTRADAY", "SWING")}
    engine.executor.working_entries = lambda: [{"symbol": "AAPL", "timeframe": "SWING", "notional": 7_000.0,
                                                "play_id": "p", "strategy": "s", "qty": 70, "risk": 100.0}]
    assert room("SWING") == pytest.approx(before["SWING"] - 7_000.0)             # sent a moment ago: it has its room
    assert room("INTRADAY") == pytest.approx(before["INTRADAY"])                 # the day trades' share is untouched
    split = engine.capital_state()["split"]
    assert split["swing"]["invested"] > 0 and split["swing"]["over"] == 0 and split["day"]["invested"] == 0


def test_a_kind_over_its_share_says_so_and_takes_nothing_new(engine, port):
    _connect(engine, port)
    engine.set_filters(timeframes=["INTRADAY", "SWING"])
    engine.set_capital_split(50)
    half = engine.capital_state()["split"]["swing"]["limit"]
    engine.executor.working_entries = lambda: [{"symbol": "AAPL", "timeframe": "SWING", "notional": half * 0.8,
                                                "play_id": "p", "strategy": "s", "qty": 1, "risk": 1.0}]
    out = engine.set_capital_split(90)                                           # swing's share shrinks under what it holds
    swing = out["capital"]["split"]["swing"]
    assert swing["over"] > 0 and swing["available"] == 0 and "more than that share" in out["note"]
    play = _play("MSFT")                                                         # a swing play
    engine.board.replace([play], None)
    pre = engine.assess_play(play.id)
    assert not pre["can_execute"] and any("swing trades already hold their 10% share" in r for r in pre["reasons"])


def test_pairs_are_held_to_the_swing_share_only_while_the_split_is_on(engine, port, monkeypatch):
    _connect(engine, port)
    sent = []
    monkeypatch.setattr(engine.pairs, "model", lambda pid: SimpleNamespace(first="AAPL", second="MSFT"))
    monkeypatch.setattr(engine.pairs, "enter", lambda pid, **k: sent.append(k["buying_power"]) or {"ok": True})
    monkeypatch.setattr(engine, "_quotes", lambda symbols: {s: 100.0 for s in symbols})

    engine.set_filters(timeframes=["INTRADAY"])                             # Swing box off: the split is off
    engine.set_capital_split(70)
    assert engine.effective_day_pct() == 100.0
    assert engine.enter_pair("AAPL/MSFT")["ok"] and sent[-1] > 0            # sized on the whole capital, not refused

    engine.set_filters(timeframes=["INTRADAY", "SWING"])                    # the split is on: swing gets 30%
    whole = sent[-1]
    assert engine.enter_pair("AAPL/MSFT")["ok"] and 0 < sent[-1] <= whole * 0.3 + 1
    engine.set_capital_split(100)                                           # ...and none at 100% day
    out = engine.enter_pair("AAPL/MSFT")
    assert not out["ok"] and "share of it" in out["reason"] and len(sent) == 2


def test_every_change_made_while_it_runs_reaches_autopilot_at_once(engine, port, monkeypatch):
    _connect(engine, port)
    heard = []
    monkeypatch.setattr(engine, "_publish", lambda topic, **payload: heard.append(topic))
    refused = _play("AAPL")
    engine.board.replace([refused], None)

    def changed_by(change):
        engine.autopilot._acted.add(refused.id)
        engine.autopilot._refused.add(refused.id)
        engine.autopilot._last_reason[refused.id] = "no room"
        engine._last_plays_at = time.monotonic()
        heard.clear()
        assert change()["ok"]
        assert refused.id not in engine.autopilot._acted and not engine.autopilot._last_reason, change
        assert "plays.updated" in heard and engine._last_plays_at == float("-inf"), change   # judged again at once
        return heard

    changed_by(lambda: engine.set_capital_split(40))
    changed_by(lambda: engine.set_capital(5_000))
    changed_by(lambda: engine.set_filters(timeframes=["SWING"]))
    changed_by(lambda: engine.set_filters(timeframes=["INTRADAY", "SWING"]))
    changed_by(lambda: engine.set_autopilot(max_auto_positions=4))
    changed_by(lambda: engine.set_strategy("vwap_reclaim", enabled=False))
    # Autopilot's state goes to the dashboard with each of them
    published = []
    engine.autopilot.bus = SimpleNamespace(publish=lambda topic, **payload: published.append((topic, payload)))
    engine.set_capital_split(70)
    [(topic, state)] = [x for x in published if x[0] == "autopilot.config"]
    assert state["slots"]["day_pct"] == 70.0 and state["slots"]["SWING"]["max"] + state["slots"]["INTRADAY"]["max"] == 4
    assert engine._entry_context(refused, "autopilot")["settings"]["day_trade_pct"] == 70.0


# ---------------------------------------------------------------- practice size
def test_a_strategy_the_replay_hasnt_proven_trades_at_a_quarter_of_the_risk(engine, monkeypatch):
    cap = engine.settings.config.risk.max_risk_per_trade_pct
    strong = [1.0, 1.0, -0.5] * 20                                              # half-Kelly would take the full risk
    monkeypatch.setattr(engine.replay, "r_multiples", lambda key, *terms: strong)
    assert engine.autopilot.proof_missing("vwap_reclaim")                       # never replayed: not proven
    assert engine.strategy_risk_pct("vwap_reclaim") == 0.25 * cap
    assert engine._entry_context(_play(), "autopilot")["settings"]["proof_required"] == engine.autopilot.proof_required
    # the order card names practice size - not the record's half-Kelly, which would have taken the full risk
    engine._refresh_account()
    engine.board.replace([_play()], None)
    [p] = engine.board.plays.values()
    caps = engine.assess_play(p.id)["order_preview"]["caps"]
    assert PRACTICE_LABEL in caps and "half-Kelly from the strategy's record" not in caps

    monkeypatch.setattr(engine.autopilot, "proof_missing", lambda key: None)    # once it is proven...
    engine._risk_pct_for = None
    assert engine.strategy_risk_pct("vwap_reclaim") == cap                      # ...its record sizes it
    assert engine.strategy_risk_why("vwap_reclaim") is None


def test_a_trade_keeps_the_setups_skipped_as_losers_when_it_was_taken(engine, monkeypatch):
    engine.autopilot.configure(require_proven=False)
    losing = {"trades": 40, "expectancy_r": -0.10, "out_of_sample": {"trades": 12, "expectancy_r": -0.08}}
    monkeypatch.setattr(engine.replay, "records", lambda *terms: {"vwap_reclaim": losing})
    settings = engine._entry_context(_play(), "autopilot")["settings"]
    assert "vwap_reclaim" in settings["replay_losers"] and settings["skip_replay_losers"] == "day"


def test_with_proof_asked_for_a_trade_keeps_no_setups_skipped_as_losers(engine, monkeypatch):
    engine.autopilot.configure(require_proven=True)                          # the proof gate governs, not the loser skip
    losing = {"trades": 40, "expectancy_r": -0.10, "out_of_sample": {"trades": 12, "expectancy_r": -0.08}}
    monkeypatch.setattr(engine.replay, "records", lambda *terms: {"vwap_reclaim": losing})
    settings = engine._entry_context(_play(), "autopilot")["settings"]
    assert settings["replay_losers"] == [] and settings["require_proven"] is True


# ---------------------------------------------------------------- a restart
def _started_again(tmp_path, gateway, port, before=None):
    again = _new_engine(tmp_path, gateway, port)
    again._bind()
    if before:
        before(again)
    again._restore_day()
    return again


def test_a_restart_picks_the_day_up_where_it_left_off(engine, tmp_path, gateway, port):
    offered, dismissed = _play("AAPL"), _play("MSFT")
    engine.board.replace([offered, dismissed], None)
    engine.board.replace([_play("AAPL"), _play("MSFT")], None)                 # both confirmed by a second scan
    assert engine.reject_play(dismissed.id)["ok"]                              # a decision is saved at once
    assert engine._day_file.read()["plays"]
    today = clock.now_ny().date()
    engine._gappers_session, engine.scanner.premarket = today, {"AAPL": {"high": 101.0, "low": 99.0}}
    engine._last_wide_done = clock.now_ny() - dt.timedelta(minutes=10)
    engine._last_scans["wide"] = {"kind": "wide", "started_at": clock.now_ny().isoformat(), "n_plays": 2}
    engine.stop()                                                              # and the rest when the app stops

    again = _started_again(tmp_path, gateway, port)
    try:
        back = again.board.plays
        assert set(back) == {offered.id, dismissed.id} and back[offered.id].confirmations == 2
        assert back[dismissed.id].status.value == "REJECTED"
        again.board.replace([_play("AAPL"), _play("MSFT")], {"AAPL", "MSFT"})
        assert [p.id for p in again.board.plays.values() if p.symbol == "MSFT"] == [dismissed.id]   # not offered again
        assert again.board.plays[offered.id].confirmations == 3

        assert again._gappers_session == today and again.scanner.premarket["AAPL"]["high"] == 101.0
        assert again.scan_status()["last_wide"]["n_plays"] == 2
        since_wide = time.monotonic() - again._last_wide_at                    # a spacing after the last one,
        assert 595 <= since_wide <= 660                                        # not after this start
    finally:
        again.stop()


def test_a_saved_day_from_another_session_or_one_that_cant_be_read_is_ignored(engine, tmp_path, gateway, port):
    engine._day_file.write({"session": "2020-01-02", "plays": [_play("AAPL")], "settled": [],
                            "last_wide_done": "2020-01-02T10:00:00-05:00", "last_scans": {"wide": {"kind": "wide"}}})
    again = _started_again(tmp_path, gateway, port)
    started = again._last_wide_at
    assert not again.board.plays and again._last_scans == {} and again._last_wide_at == started
    again.stop()

    engine._day_file.path.write_bytes(b"not a saved day")
    again = _started_again(tmp_path, gateway, port)
    assert not again.board.plays
    again.stop()


def test_plays_the_filters_stopped_allowing_while_the_app_was_off_stay_off(engine, tmp_path, gateway, port):
    engine.board.replace([_play("AAPL", Side.LONG), _play("MSFT", Side.SHORT)], None)
    engine.stop()

    def longs_only(e):
        e.filters = e.scanner.filters = TradeFilters.build(["LONG"], None, None)

    again = _started_again(tmp_path, gateway, port, before=longs_only)
    assert [p.symbol for p in again.board.plays.values()] == ["AAPL"]
    again.stop()


# ---------------------------------------------------------------- quitting
def test_paper_quit_closes_everything_resets_and_shuts_down(engine):
    _open(engine, "AAPL")
    _open(engine, "MSFT")
    closed = _closes_fill(engine)
    done = threading.Event()
    engine.on_shutdown = done.set

    r = engine.begin_quit(close_all=True)
    assert r["ok"] and sorted(closed) == ["AAPL", "MSFT"]
    assert engine.quit_state is None and engine.repo.open_trades() == []
    assert engine._account.cash == pytest.approx(engine.settings.config.account.paper_start_cash)
    assert "quit" not in engine.runtime.read()
    assert done.wait(3)


def test_a_quit_can_keep_the_swing_positions_that_have_a_stop_at_the_broker(engine):
    engine._venue = "ibkr-paper"
    kept, closed_one = _open(engine, "AAPL", venue="ibkr-paper"), _open(engine, "MSFT", venue="ibkr-paper")
    engine.executor.native_stops_on = lambda: True
    engine.executor.protective_stops = lambda: [{"trade_id": kept, "symbol": "AAPL"}]      # MSFT has no stop resting
    closed = _closes_fill(engine)
    done = threading.Event()
    engine.on_shutdown = done.set

    preview = engine.quit_preview()
    assert [b["id"] for b in preview["keepable"]] == [kept] and preview["left"] == 2
    r = engine.begin_quit(close_all=True, keep=True)
    assert r["ok"] and closed == ["MSFT"]                                  # only the unprotected one is closed
    assert [t["id"] for t in engine.repo.open_trades()] == [kept] and engine.quit_state is None
    assert done.wait(3)
    assert closed_one not in [t["id"] for t in engine.repo.open_trades()]


def test_right_after_a_start_the_stops_an_earlier_run_left_at_the_broker_let_a_quit_keep_the_positions(engine):
    engine._venue = "ibkr-paper"
    whole, short_of_shares, untagged = (_open(engine, s, venue="ibkr-paper", qty=100) for s in ("AAA", "BBB", "CCC"))
    engine.executor.native_stops_on = lambda: True
    engine.executor.protective_stops = lambda: []                          # the first pass hasn't taken them over yet

    def resting(tid, qty, filled=0.0, tag=None):
        return OrderResult(order_id=f"o-{tid}", status="SUBMITTED", symbol="", submitted_qty=qty, filled_qty=filled,
                           side=Side.SHORT, tag=tag if tag is not None else f"stop:{tid}", order_type="STOP")

    working = [resting(whole, 100), resting(short_of_shares, 100, filled=40), resting(untagged, 100, tag="")]
    engine.executor.broker.list_orders = lambda status=None: working
    assert [b["id"] for b in engine.quit_preview()["keepable"]] == [whole]  # all its shares, under its own tag

    def unreadable(status=None):
        raise RuntimeError("no answer")

    engine.executor.broker.list_orders = unreadable                        # nothing known: nothing kept on its word
    assert engine.quit_preview()["keepable"] == []
    engine.executor.protective_stops = lambda: [{"trade_id": short_of_shares, "symbol": "BBB"}]
    assert [b["id"] for b in engine.quit_preview()["keepable"]] == [short_of_shares]   # followed ones still count


def test_without_stops_at_the_broker_nothing_can_be_kept(engine):
    _open(engine, "AAPL")
    closed = _closes_fill(engine)
    assert engine.quit_preview()["keepable"] == []                          # the simulator rests no stops
    engine.begin_quit(close_all=True, keep=True)
    assert closed == ["AAPL"] and engine.repo.open_trades() == []


def test_after_hours_a_quit_keeps_the_positions_or_is_refused_and_a_stuck_quit_can_be_stopped(engine):
    engine._venue = "ibkr-paper"
    a, b = _open(engine, "AAPL", venue="ibkr-paper"), _open(engine, "MSFT", venue="ibkr-paper")
    closed = _closes_fill(engine)
    engine.executor._exchange_closed = lambda: "The market is closed, so an exit can't fill now."
    refused = engine.begin_quit(close_all=True)
    assert not refused["ok"] and refused["market_closed"] and "Keep them open" in refused["reason"]
    assert engine.quit_state is None and closed == []                       # nothing locked, nothing sent
    done = threading.Event()
    engine.on_shutdown = done.set
    assert engine.begin_quit(close_all=True, keep=True)["ok"] and done.wait(3)
    assert closed == [] and {t["id"] for t in engine.repo.open_trades()} == {a, b}   # every position kept

    engine.executor._exchange_closed = lambda: None
    engine.executor.close_trade = lambda tid, reason="manual": {"ok": False, "reason": "rejected"}   # a quit that can't finish
    engine.on_shutdown = None
    assert engine.begin_quit(close_all=True)["ok"] and engine.quit_state
    out = engine.cancel_quit()
    assert out["ok"] and engine.quit_state is None and "quit" not in engine.runtime.read()
    assert not engine.cancel_quit()["ok"]


def test_while_quitting_nothing_but_exits_can_change(engine):
    tid = _open(engine, "AAPL")
    engine.executor.close_trade = lambda tid, reason="manual": {"ok": True, "status": "WORKING", "order_id": "o1"}
    key = engine.strategy_state()[0]["key"]

    assert engine.begin_quit()["ok"] and engine.quit_state
    assert engine.snapshot()["quit"]["left"] == 1
    for refused in (engine.set_mode("live"), engine.set_filters(sides=["LONG"]), engine.request_scan(),
                    engine.reset_paper(), engine.set_strategy(key, enabled=False),
                    engine.set_autopilot(enabled=True), engine.set_paper_platform("simulator"),
                    engine.approve_play("any"), engine.set_scan_settings(cycle_minutes=4),
                    engine.set_capital(1_000)):
        assert not refused["ok"] and refused["reason"].startswith("Quitting")
    assert engine.close_position(tid)["ok"]                                 # exits still go through
    assert engine.runtime.read()["quit"]["mode"] == "paper"                 # survives a restart
    assert engine._due_scan() is None


def test_a_paper_quit_never_gets_stuck_on_a_close_that_wont_fill(engine):
    tid = _open(engine, "AAPL")
    engine.executor.close_trade = lambda tid, reason="manual": {"ok": False, "reason": "no quote"}
    done = threading.Event()
    engine.on_shutdown = done.set

    engine.begin_quit()
    for _ in range(2):                                                      # two retry rounds
        assert engine.quit_state is not None
        engine._quit_retry_at = 0.0
        engine._check_quit_progress()
    # the simulator reset wipes the position, and with it the record
    assert engine.quit_state is None and engine.repo.get_trade(tid) is None
    assert done.wait(3)


def test_live_quit_can_be_cancelled_and_leaves_positions_alone(engine):
    _open(engine, "AAPL")
    closed = _closes_fill(engine)
    engine.mode = "live"

    preview = engine.quit_preview()
    assert not preview["paper"] and preview["left"] == 1
    r = engine.begin_quit(close_all=False)
    assert not r["ok"] and engine.quit_state is None and closed == []
    assert len(engine.repo.open_trades()) == 1


def test_close_all_sends_every_exit(engine):
    for s in ("AAPL", "MSFT", "NVDA"):
        _open(engine, s)
    parked = _open(engine, "XOM", venue="ibkr-paper")
    closed = _closes_fill(engine)
    r = engine.close_all_positions()
    assert r["ok"] and sorted(closed) == ["AAPL", "MSFT", "NVDA"]
    assert [t["id"] for t in engine.repo.open_trades()] == [parked]         # other platforms untouched


# ---------------------------------------------------------------- broker vs database
def test_a_record_goes_only_after_repeated_misses_on_a_settled_connection(engine):
    tid = _open(engine, "AAPL")
    engine.position_check.GRACE_S = 0.0
    engine._refresh_account()
    assert engine._reconcile_open_trades() == []                            # connection just came up
    engine._broker_since -= PositionCheck.SETTLE_S + 1
    assert engine._reconcile_open_trades() == [] and engine.repo.get_trade(tid)   # first miss
    removed = engine._reconcile_open_trades()                               # second miss in a row
    assert [r["id"] for r in removed] == [tid] and engine.repo.get_trade(tid) is None


def test_nothing_is_removed_on_an_answer_that_cant_be_trusted(engine, monkeypatch):
    tid = _open(engine, "AAPL")
    engine.position_check.GRACE_S = engine.position_check.SETTLE_S = 0.0

    engine._refresh_account()
    engine._account_at -= 10                                                # stale account
    assert all(engine._reconcile_open_trades() == [] for _ in range(3))

    engine._refresh_account()
    engine.executor.pending_exit_trade_ids = lambda: {tid}                  # its exit is going through
    assert all(engine._reconcile_open_trades() == [] for _ in range(3))
    del engine.executor.pending_exit_trade_ids

    monkeypatch.setattr(type(engine.broker), "is_connected", property(lambda self: False))
    assert all(engine._reconcile_open_trades() == [] for _ in range(3))     # disconnected
    assert engine.repo.get_trade(tid)


def test_young_trades_and_held_positions_are_kept(engine):
    young = _open(engine, "AAPL")
    held = _open(engine, "MSFT")
    engine.position_check.SETTLE_S = 0.0
    engine._refresh_account()
    engine._account.positions = [Position(symbol="MSFT", quantity=5, avg_price=100.0)]

    assert all(engine._reconcile_open_trades() == [] for _ in range(3))     # AAPL is inside the grace period
    engine.position_check.GRACE_S = 0.0
    engine._reconcile_open_trades()
    assert [r["id"] for r in engine._reconcile_open_trades()] == [young]
    assert engine.repo.get_trade(held)


def test_a_position_of_another_size_than_its_records_is_reported_and_left_alone(engine):
    tid = _open(engine, "AAPL", qty=5)
    engine.position_check.SETTLE_S = 0.0
    engine._refresh_account()
    engine._account.positions = [Position(symbol="AAPL", quantity=20, avg_price=100.0)]

    engine._reconcile_open_trades()
    assert engine.snapshot()["mismatches"] == []                            # confirmed on the next check
    engine._reconcile_open_trades()
    [m] = engine.snapshot()["mismatches"]
    assert (m["symbol"], m["recorded"], m["held"]) == ("AAPL", 5.0, 20.0) and "20 shares long" in m["note"]
    assert engine.repo.get_trade(tid)["quantity"] == 5                      # nothing traded or deleted

    engine._account.positions[0].quantity = 5                               # back in agreement
    engine._reconcile_open_trades()
    assert engine.snapshot()["mismatches"] == []


def test_an_account_that_cant_be_read_keeps_the_last_snapshot_and_touches_nothing(engine, monkeypatch):
    from tos_bot.brokers.base import BrokerError

    tid = _open(engine, "AAPL")
    engine.position_check.GRACE_S = engine.position_check.SETTLE_S = 0.0
    engine._refresh_account()
    engine._account.positions = [Position(symbol="AAPL", quantity=5, avg_price=100.0),
                                 Position(symbol="MSFT", quantity=3, avg_price=50.0)]
    read_at = engine._account_at

    def slow(self):
        raise BrokerError("IBKR didn't answer for the positions in time (TimeoutError)")

    monkeypatch.setattr(type(engine._broker), "get_account", slow)
    assert engine._refresh_account() is False
    assert [p.symbol for p in engine._account.positions] == ["AAPL", "MSFT"] and engine._account_at == read_at
    engine._account_at -= 10                                                 # the snapshot is stale now...
    assert all(engine._reconcile_open_trades() == [] for _ in range(3))     # ...so nothing is deleted or closed
    assert engine.repo.get_trade(tid)["status"] == "OPEN"
    assert [r["symbol"] for r in engine.untracked_positions()] == ["MSFT"]  # the last known strays still show


def test_a_position_closed_outside_the_app_is_booked_from_the_brokers_fills(engine):
    import datetime as dt

    from tos_bot.core.models import Fill

    tid = _open(engine, "AAPL", qty=5)
    engine.position_check.GRACE_S = engine.position_check.SETTLE_S = 0.0
    engine._refresh_account()
    now = dt.datetime.now(dt.timezone.utc)
    engine._broker.get_fills = lambda symbol=None: [
        Fill(order_id="tws-1", symbol="AAPL", side=Side.SHORT, quantity=2, price=102.0, ts=now, commission=0.5),
        Fill(order_id="tws-2", symbol="AAPL", side=Side.SHORT, quantity=3, price=104.0, ts=now, commission=0.5),
        Fill(order_id="old", symbol="AAPL", side=Side.LONG, quantity=5, price=100.0, ts=now),       # the entry
    ]
    assert engine._reconcile_open_trades() == []                            # first miss
    settled = engine._reconcile_open_trades()                               # second miss: it's gone
    assert [s["id"] for s in settled] == [tid] and settled[0]["fills"] == 2
    t = engine.repo.get_trade(tid)
    assert t["status"] == "CLOSED" and t["exit_reason"] == "closed-outside" and t["exit_price"] == 103.2
    assert t["realized_pl"] == pytest.approx(5 * 3.2 - 1.0) and engine.repo.open_trades() == []
    assert engine.snapshot()["mismatches"] == []


def test_an_exit_called_off_after_filling_in_part_is_booked_from_its_tagged_fills(engine):
    import datetime as dt

    from tos_bot.core.models import Fill

    tid = _open(engine, "AAPL", qty=10)                                     # entered at 100
    engine.position_check.GRACE_S = engine.position_check.SETTLE_S = 0.0
    engine._refresh_account()
    engine._account.positions = [Position(symbol="AAPL", quantity=6, avg_price=100.0, market_price=101.0)]
    now, asked = dt.datetime.now(dt.timezone.utc), []

    def fills(symbol=None):
        asked.append(symbol)
        return [Fill(order_id="e1", symbol="AAPL", side=Side.SHORT, quantity=3, price=104.0, ts=now, tag=f"exit:{tid}"),
                Fill(order_id="e1", symbol="AAPL", side=Side.SHORT, quantity=1, price=106.0, ts=now, tag=f"exit:{tid}"),
                Fill(order_id="tws", symbol="AAPL", side=Side.SHORT, quantity=5, price=90.0, ts=now),       # not the app's
                Fill(order_id="x9", symbol="AAPL", side=Side.SHORT, quantity=2, price=80.0, ts=now, tag="exit:trd_other"),
                Fill(order_id="in", symbol="AAPL", side=Side.LONG, quantity=10, price=100.0, ts=now, tag=f"play_{tid}")]

    engine._broker.get_fills = fills
    engine._reconcile_open_trades()
    t = engine.repo.get_trade(tid)
    assert (t["status"], t["quantity"], t["initial_quantity"]) == ("OPEN", 6, 10)
    assert t["banked_pl"] == pytest.approx(3 * 4.0 + 1 * 6.0)                # its own exit's four shares, at their prices
    engine._reconcile_open_trades()
    assert engine.repo.get_trade(tid)["quantity"] == 6                      # booked once: the counts agree now


def test_a_record_over_the_broker_for_a_reason_its_own_fills_dont_explain_is_left_alone(engine):
    import datetime as dt

    from tos_bot.core.models import Fill

    tid = _open(engine, "AAPL", qty=10)
    engine.position_check.GRACE_S = engine.position_check.SETTLE_S = 0.0
    engine._refresh_account()
    engine._account.positions = [Position(symbol="AAPL", quantity=6, avg_price=100.0, market_price=101.0)]
    now, asked = dt.datetime.now(dt.timezone.utc), []
    engine._broker.get_fills = lambda symbol=None: asked.append(symbol) or [
        Fill(order_id="tws", symbol="AAPL", side=Side.SHORT, quantity=4, price=90.0, ts=now)]    # sold by hand in TWS
    engine._reconcile_open_trades()
    engine._reconcile_open_trades()
    assert engine.repo.get_trade(tid)["quantity"] == 10 and len(asked) == 1  # untouched, and the broker asked once
    assert engine.snapshot()["mismatches"]                                  # still reported for the owner to look at

    other = _open(engine, "MSFT", qty=10)                                   # an exit for it still working: its own booking
    engine._account.positions.append(Position(symbol="MSFT", quantity=6, avg_price=100.0, market_price=101.0))
    engine.executor.pending_exit_trade_ids = lambda: {other}
    engine._broker.get_fills = lambda symbol=None: [
        Fill(order_id="e2", symbol="MSFT", side=Side.SHORT, quantity=4, price=104.0, ts=now, tag=f"exit:{other}")]
    engine._refresh_account = lambda *a, **k: True
    engine._reconcile_open_trades()
    assert engine.repo.get_trade(other)["quantity"] == 10


def test_shares_without_a_record_are_listed_and_can_be_exited(engine):
    _open(engine, "MSFT", qty=5)
    engine._refresh_account()
    engine._account.positions = [Position(symbol="MSFT", quantity=20, avg_price=100.0, market_price=101.0),
                                 Position(symbol="AAPL", quantity=-7, avg_price=50.0, market_price=49.0),
                                 Position(symbol="NVDA", quantity=0, avg_price=10.0)]
    rows = {r["symbol"]: r for r in engine.untracked_positions()}
    assert set(rows) == {"MSFT", "AAPL"}
    assert (rows["MSFT"]["side"], rows["MSFT"]["qty"], rows["MSFT"]["recorded"], rows["MSFT"]["held"]) == ("LONG", 15, 5, 20)
    assert (rows["AAPL"]["side"], rows["AAPL"]["qty"], rows["AAPL"]["unrealized_pl"]) == ("SHORT", 7, 7.0)
    assert engine.snapshot()["untracked"] == list(rows.values())

    sent = []
    engine.executor.close_untracked = lambda symbol, side, qty: sent.append((symbol, side, qty)) or {"ok": True, "status": "FILLED"}
    r = engine.close_untracked("AAPL")
    assert r["ok"] and "7 AAPL shares" in r["note"] and sent == [("AAPL", "SHORT", 7.0)]
    assert not engine.close_untracked("NVDA")["ok"]

    engine._account.positions = [Position(symbol="MSFT", quantity=3, avg_price=100.0)]
    assert engine.untracked_positions() == []                               # fewer than recorded: a mismatch, not untracked


# ---------------------------------------------------------------- fixing a share count from its warning
class _FixAccount(_StopBroker):
    """An IBKR account for the share-count fix: what it holds and marks, the executions it reports, and
    stop orders that rest until a test fills them (test_native_stop)."""

    is_connected = True

    def __init__(self, held, mark=0.0):
        super().__init__({"AAA": held})
        self.fills, self.mark = [], mark

    def get_account(self):
        return Account(account_id="DU1", equity=50_000.0, cash=50_000.0, buying_power=100_000.0,
                       positions=[Position(symbol=s, quantity=q, avg_price=100.0, market_price=self.mark)
                                  for s, q in self.positions.items()])

    def get_fills(self, symbol=None):
        return [f for f in self.fills if not symbol or f.symbol == symbol]


def _mismatched(engine, held=40, mark=0.0):
    """A 100-share AAA record (entered at 100, stop 95, target 110) on an IBKR paper account holding ``held``."""
    broker = _FixAccount(held, mark)
    engine._broker, engine._venue = broker, "ibkr-paper"
    engine.executor.rebind(broker, venue="ibkr-paper")
    return broker, _open(engine, "AAA", venue="ibkr-paper", qty=100)


def _sold(tid, *parts, tag="stop"):
    """Executions of the record's own order (``tag``: stop / tgt / exit): (shares, price) each, a second apart."""
    now = dt.datetime.now(dt.timezone.utc)
    return [Fill(order_id="8", symbol="AAA", side=Side.SHORT, quantity=q, price=p, commission=0.5,
                 ts=now + dt.timedelta(seconds=i), tag=f"{tag}:{tid}") for i, (q, p) in enumerate(parts)]


def test_a_record_over_the_account_is_previewed_with_the_brokers_executions_of_the_missing_shares(engine):
    broker, tid = _mismatched(engine, held=40)
    now = dt.datetime.now(dt.timezone.utc)
    broker.fills = _sold(tid, (30, 96.0), (30, 95.0)) + [
        Fill(order_id="7", symbol="AAA", side=Side.LONG, quantity=100, price=100.0, ts=now, tag="play_x"),   # the entry
        Fill(order_id="9", symbol="AAA", side=Side.SHORT, quantity=5, price=90.0, ts=now, tag="exit:trd_other")]
    m = engine.mismatch_preview("AAA")
    assert (m["kind"], m["side"], m["recorded"], m["held"], m["missing"]) == ("fewer", "LONG", 100, 40, 60)
    assert [(r["qty"], r["price"], r["source"]) for r in m["executions"]] == [(30, 96.0, "stop"), (30, 95.0, "stop")]
    b = m["booking"]
    assert (b["qty"], b["price"], b["reason"], b["estimated"], b["commission"]) == (60, 95.5, "stop", False, 1.0)
    assert "its stop order" in b["basis"]
    assert m["actions"]["match"]["ok"] and m["actions"]["close"]["ok"]
    assert engine.repo.get_trade(tid)["quantity"] == 100                    # a preview changes nothing


def test_missing_shares_without_executions_are_estimated_at_the_last_price_and_it_says_so(engine):
    broker, tid = _mismatched(engine, held=40, mark=97.0)
    b = engine.mismatch_preview("AAA")["booking"]
    assert (b["price"], b["reason"], b["estimated"]) == (97.0, "closed-outside", True)
    assert "estimated at the last price" in b["basis"]
    broker.fills = _sold(tid, (20, 95.0))                                   # some are reported: those at their price
    b = engine.mismatch_preview("AAA")["booking"]
    assert b["price"] == pytest.approx((20 * 95.0 + 40 * 97.0) / 60, abs=1e-4) and b["reason"] == "closed-outside"
    broker.mark = 0.0                                                       # and nothing to estimate the rest at
    m = engine.mismatch_preview("AAA")
    assert m["booking"] is None and not m["actions"]["match"]["ok"] and "no price" in m["actions"]["match"]["reason"]


def test_shares_the_record_already_booked_off_today_arent_counted_again(engine):
    broker, tid = _mismatched(engine, held=50)
    engine.repo.reduce_trade(tid, 20, 110.0, exit_reason="target-1")        # the scale-out, booked when it filled
    broker.fills = _sold(tid, (20, 110.0), tag="tgt") + _sold(tid, (30, 94.0))
    broker.fills[-1].ts += dt.timedelta(seconds=5)                          # the stop filled after the target
    m = engine.mismatch_preview("AAA")
    assert m["missing"] == 30 and [(r["qty"], r["source"]) for r in m["executions"]] == [(30, "stop")]
    assert (m["booking"]["price"], m["booking"]["reason"]) == (94.0, "stop")


def test_a_fix_is_refused_with_its_reason_when_it_isnt_safe_or_isnt_the_fix(engine, monkeypatch):
    broker, tid = _mismatched(engine, held=40, mark=97.0)
    assert engine.mismatch_preview("AAA")["actions"]["match"]["ok"]
    engine.executor.pending_exit_trade_ids = lambda: {tid}                  # an exit for it is working
    assert "still working" in engine.mismatch_preview("AAA")["actions"]["match"]["reason"]
    del engine.executor.pending_exit_trade_ids
    broker.is_connected = False
    assert "isn't connected" in engine.mismatch_preview("AAA")["actions"]["close"]["reason"]
    broker.is_connected = True
    broker.positions["AAA"] = 100
    assert engine.mismatch_preview("AAA")["kind"] == "agree"
    broker.positions["AAA"] = 150                                           # more than the record: shares without one
    m = engine.mismatch_preview("AAA")
    assert m["kind"] == "more" and "Shares without a record" in m["actions"]["match"]["reason"]
    assert not engine.fix_mismatch("AAA", "match")["ok"] and engine.repo.get_trade(tid)["quantity"] == 100
    broker.positions["AAA"] = 40
    monkeypatch.setattr(Executor, "_session_now", staticmethod(lambda: clock.Session.CLOSED))
    actions = engine.mismatch_preview("AAA")["actions"]                     # after the close: the record can still match
    assert actions["match"]["ok"] and not actions["close"]["ok"] and "market is closed" in actions["close"]["reason"]
    assert broker.orders == []


def test_no_fix_books_shares_a_resting_stop_has_filled_while_it_still_works(engine):
    broker, tid = _mismatched(engine, held=100)
    engine.executor.sync_open_orders()                                      # the stop goes on for the 100
    oid = engine.executor.protective_stops()[0]["order_id"]
    broker.live[oid].filled_qty = 60                                        # it has sold 60 and still works
    broker.positions["AAA"] = 40
    reason = engine.mismatch_preview("AAA")["actions"]["match"]["reason"]
    assert "filled 60 shares" in reason and "still working" in reason       # it books them itself when it finishes


def test_match_books_the_missing_shares_off_the_record_and_the_stop_follows_the_record(engine):
    broker, tid = _mismatched(engine, held=40)
    broker.fills = _sold(tid, (30, 96.0), (30, 95.0))
    engine.position_check.mismatches = [{"symbol": "AAA", "recorded": 100.0, "held": 40.0, "records": 1, "note": "AAA"}]
    m = engine.mismatch_preview("AAA")
    r = engine.fix_mismatch("AAA", "match", expect={"recorded": m["recorded"], "held": m["held"]})
    assert r["ok"] and "Booked 60 AAA shares" in r["note"] and r["booked"]["reason"] == "stop"
    t = engine.repo.get_trade(tid)
    assert (t["status"], t["quantity"], t["initial_quantity"]) == ("OPEN", 40, 100)
    assert t["banked_pl"] == pytest.approx(60 * (95.5 - 100.0) - 1.0)
    assert engine.snapshot()["mismatches"] == []                            # the warning goes with it
    engine.executor.sync_open_orders()                                      # the next pass: a stop for what the record holds
    assert [(s.quantity, s.stop_price) for s in broker.stops()] == [(40, 95.0)]
    assert engine.mismatch_preview("AAA")["kind"] == "agree"                # booked once


def test_close_matches_the_record_then_sells_the_rest_and_the_record_closes_when_it_fills(engine):
    broker, tid = _mismatched(engine, held=40, mark=97.0)
    r = engine.fix_mismatch("AAA", "close", expect={"recorded": 100, "held": 40})
    assert r["ok"] and "Exit sent for the rest" in r["note"]
    [out] = broker.exits()
    assert (out.side, out.quantity, out.client_tag) == (Side.SHORT, 40, f"exit:{tid}")
    assert engine.repo.get_trade(tid)["quantity"] == 40                     # the missing shares were booked first
    oid = r["exit"]["order_id"]
    broker.reports[oid] = OrderResult(order_id=oid, status="FILLED", symbol="AAA", submitted_qty=40, filled_qty=40,
                                      avg_fill_price=96.0)
    broker.positions.pop("AAA")
    engine.executor.sync_open_orders()
    t = engine.repo.get_trade(tid)
    assert (t["status"], t["exit_price"], t["exit_reason"]) == ("CLOSED", 96.0, "manual")


def test_a_fix_is_refused_when_the_counts_changed_since_the_preview(engine):
    broker, tid = _mismatched(engine, held=40, mark=97.0)
    m = engine.mismatch_preview("AAA")
    broker.positions["AAA"] = 30                                            # more went while the preview was open
    r = engine.fix_mismatch("AAA", "close", expect={"recorded": m["recorded"], "held": m["held"]})
    assert not r["ok"] and r["changed"] and "30" in r["reason"]
    assert engine.repo.get_trade(tid)["quantity"] == 100 and broker.orders == []


def test_resetting_paper_deletes_the_simulators_open_records_only(engine):
    sim = _open(engine, "AAPL")
    other = _open(engine, "MSFT", venue="ibkr-paper")
    r = engine.reset_paper(50_000)
    assert r["ok"] and [x["id"] for x in r["removed"]] == [sim]
    assert engine.repo.get_trade(sim) is None and engine.repo.get_trade(other)
    assert engine._account.cash == pytest.approx(50_000)


def test_a_trade_record_can_be_pulled_up_and_deleted(engine):
    tid = _open(engine, "AAPL")
    rec = engine.trade_record(tid)
    assert rec["trade"]["id"] == tid and rec["play"]["symbol"] == "AAPL"
    assert rec["on_current_venue"] and rec["venue_label"]
    assert isinstance(rec["fills"], list) and isinstance(rec["orders"], list)
    assert engine.repo.delete_trade(tid) and not engine.repo.delete_trade(tid)
    assert engine.trade_record(tid) is None


# ---------------------------------------------------------------- trading capital
def _tight_play(symbol="NVDA"):
    # 0.50 of risk per share, so the position limits bind rather than the risk budget
    return Play(symbol=symbol, side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.SWING, entry=100.0, stop=99.5, targets=[102.0])


def test_trading_capital_shrinks_what_the_bot_uses_not_the_account(engine):
    engine._refresh_account()
    value, risk = engine._account.equity, engine.settings.config.risk
    assert not engine.set_capital(value * 2)["ok"]                        # more than the account holds
    assert not engine.set_capital(-5)["ok"] and not engine.set_capital("lots")["ok"]

    r = engine.set_capital(20_000)
    assert r["ok"] and r["capital"]["limit"] == 20_000 and r["capital"]["effective"] == 20_000
    assert engine.runtime.read()["capital"] == {"paper": 20000.0}
    sizing = engine.sizing_account()
    assert sizing.equity == 20_000 and engine._account.equity == value     # the real account is untouched
    whole = size_play(_tight_play(), engine._account, risk)
    capped = size_play(_tight_play(), sizing, risk)
    assert capped.notional <= 20_000 * risk.max_position_pct_of_equity / 100 < whole.notional

    _open(engine, "MSFT", qty=190)                                         # 19,000 of it now invested
    left = size_play(_tight_play(), engine.sizing_account(), risk)
    assert left.notional <= 1_000 and "trading capital" in left.caps_hit
    assert engine.capital_state()["available"] == pytest.approx(1_000)

    assert engine.set_capital(None)["ok"] and engine.sizing_account() is engine._account
    assert engine.runtime.read()["capital"] == {}


def test_an_order_is_capped_at_a_slice_of_the_stocks_usual_volume_and_a_too_thin_stock_is_refused(engine, monkeypatch):
    engine._refresh_account()
    monkeypatch.setattr(engine.settings.config.risk, "max_adv_pct", 1.0)
    play = _tight_play("AAA")
    play.evidence["adv_shares"] = 2_000                                    # 1% of it: 20 shares
    thin = _tight_play("BBB")
    thin.evidence["adv_shares"] = 50                                       # 1% of it: half a share
    engine.board.replace([play, thin], None)
    pre = engine.assess_play(play.id)
    assert pre["order_preview"]["qty"] == 20
    assert "liquidity: 1% of its usual daily volume" in pre["order_preview"]["caps"]
    pre = engine.assess_play(thin.id)
    assert not pre["can_execute"] and pre["order_preview"]["qty"] == 0
    assert any(r.startswith("BBB is too thin to trade: it usually trades 50 shares a day") for r in pre["reasons"])
    assert "risk budget too small for one share" not in pre["order_preview"]["caps"]   # the cap did it, not the budget


def test_trading_capital_is_split_between_day_trades_and_swing_trades(engine):
    engine._refresh_account()
    risk = engine.settings.config.risk
    assert engine.day_trade_pct == 75.0                                    # the default: 75% day, 25% swing
    assert not engine.set_capital_split(120)["ok"] and not engine.set_capital_split("half")["ok"]
    assert engine.set_capital(40_000)["ok"]
    assert engine.sizing_account(Timeframe.INTRADAY).raw["capital_room"] == 30_000
    assert engine.sizing_account("SWING").raw["capital_room"] == 10_000
    assert engine.sizing_account().raw["capital_room"] == 40_000           # the whole trading capital, as before

    _open(engine, "MSFT", qty=90)                                          # a 9,000 swing position
    assert engine.sizing_account("SWING").raw["capital_room"] == 1_000
    assert engine.sizing_account("INTRADAY").raw["capital_room"] == 30_000
    capped = size_play(_tight_play(), engine.sizing_account("SWING"), risk)
    assert capped.notional <= 1_000 and "trading capital" in capped.caps_hit
    split = engine.capital_state()["split"]
    assert (split["day"]["limit"], split["swing"]["invested"], split["swing"]["available"]) == (30_000, 9_000, 1_000)

    r = engine.set_capital_split(50)
    assert r["ok"] and engine.runtime.read()["capital_split"] == {"day_pct": 50.0}
    assert engine.sizing_account("SWING").raw["capital_room"] == 11_000
    assert engine.set_capital(None)["ok"]                                  # the whole account, still split
    whole = engine._account.equity
    assert engine.sizing_account("INTRADAY").raw["capital_room"] == pytest.approx(min(whole - 9_000, whole / 2))

    assert engine.set_filters(timeframes=["SWING"])["ok"]                  # swing trades only: they get all of it
    assert engine.sizing_account("SWING") is engine._account                # no limit and no split: the whole account
    split = engine.capital_state()["split"]
    assert (split["on"], split["day_pct"], split["set_pct"]) == (False, 0.0, 50.0)
    assert "both switched on" in engine.set_capital_split(60)["note"]
    assert engine.set_filters(timeframes=["INTRADAY", "SWING"])["ok"]      # both again: the split is back
    assert engine.capital_state()["split"]["on"] and engine.effective_day_pct() == 60.0


def test_capital_is_checked_and_shown_in_the_accounts_own_currency(engine, monkeypatch):
    cad = Account(account_id="DU1", equity=720_000.0, cash=720_000.0, buying_power=2_400_000.0,
                  base_currency="CAD", usd_per_base=0.72,
                  raw={"base": {"equity": 1_000_000.0, "cash": 1_000_000.0, "buying_power": 3_333_333.33}})
    engine._account = cad
    monkeypatch.setattr(engine, "_refresh_account", lambda: True)          # keep this account
    r = engine.set_capital(1_200_000)
    assert not r["ok"] and "CA$1,000,000" in r["reason"]
    r = engine.set_capital(500_000)
    assert r["ok"] and r["capital"]["currency"] == "CAD" and r["capital"]["account_value"] == 1_000_000
    assert engine.sizing_account().equity == pytest.approx(360_000)        # CA$500k in US dollars
    assert engine.snapshot()["account"]["base"]["equity"] == 1_000_000

    engine._account = dataclasses.replace(cad, equity=0.0, usd_per_base=0.0)
    assert not engine.set_capital(400_000)["ok"]                            # no rate, nothing to size with


def test_the_dashboard_lists_the_orders_working_at_the_broker(engine, monkeypatch):
    working = []
    monkeypatch.setattr(engine.executor.broker, "list_orders", lambda status=None: list(working))
    assert engine.active_orders(max_age_s=0)["orders"] == []

    working.append(OrderResult(order_id="1", status="WORKING", symbol="AAPL", submitted_qty=5, side=Side.LONG,
                               tag="play_waiting", order_type="LIMIT", limit_price=1.0))
    assert engine.active_orders()["orders"] == []       # the last answer is still fresh
    listed = engine.active_orders(max_age_s=0)
    assert listed["ok"]
    assert [(o["symbol"], o["purpose"], o["play_id"], o["limit_price"]) for o in listed["orders"]] == [
        ("AAPL", "entry", "play_waiting", 1.0)]


def test_a_working_entrys_countdowns_are_sent_with_it_but_never_pushed_on_their_own(engine, monkeypatch):
    """The browser counts down from the times sent with the order. The part-fill cut is worked out again at
    each look; that alone isn't a change to push."""
    from tos_bot.execution.executor import _Pending

    play = Play(symbol="AAA", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.INTRADAY, entry=100.0, stop=95.0, targets=[110.0])
    engine.executor._pending["1"] = _Pending("1", play, "entry", qty=5, submitted_at=dt.datetime.now(dt.timezone.utc),
                                             first_fill_at=time.monotonic())
    working = [OrderResult(order_id="1", status="WORKING", symbol="AAA", submitted_qty=5, filled_qty=2,
                           side=Side.LONG, tag=play.id, order_type="LIMIT", limit_price=100.0)]
    monkeypatch.setattr(engine.executor.broker, "list_orders", lambda status=None: list(working))
    heard = []
    monkeypatch.setattr(engine, "_publish", lambda topic, **payload: heard.append(topic))
    engine._refresh_orders()
    engine._refresh_orders()                                                # nothing the broker says has changed
    (order,) = engine.active_orders()["orders"]
    assert order["expires_at"] and order["cut_at"] and order["filled"] == 2
    assert heard.count("orders.updated") == 1


def test_a_countdown_that_starts_after_its_order_was_sent_is_pushed(engine, monkeypatch):
    """The orders loop can list a part-fill before the order sync notes its first fill, and an entry left
    working by an earlier run is listed before it is taken over. The cut, or the time-out, that follows
    changes nothing else about the order - it is still told to the browser, once."""
    from tos_bot.execution.executor import _Pending

    play = Play(symbol="AAA", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.INTRADAY, entry=100.0, stop=95.0, targets=[110.0])
    working = [OrderResult(order_id="1", status="WORKING", symbol="AAA", submitted_qty=5, filled_qty=0,
                           side=Side.LONG, tag=play.id, order_type="LIMIT", limit_price=100.0)]
    monkeypatch.setattr(engine.executor.broker, "list_orders", lambda status=None: list(working))
    sent = []
    monkeypatch.setattr(engine, "_publish", lambda topic, **payload: sent.append((topic, payload)))
    pushed = lambda: [p["orders"][0] for t, p in sent if t == "orders.updated"]   # noqa: E731

    engine._refresh_orders()                                                # not yet followed: no clock
    assert [(o["purpose"], o["expires_at"]) for o in pushed()] == [("entry", None)]
    engine.executor._pending["1"] = _Pending("1", play, "entry", qty=5, adopted=True,
                                             submitted_at=dt.datetime.now(dt.timezone.utc))
    engine._refresh_orders()                                                # taken over: its time-out starts
    assert len(pushed()) == 2 and pushed()[-1]["expires_at"] and pushed()[-1]["cut_at"] is None

    working[0] = dataclasses.replace(working[0], filled_qty=2)
    engine._refresh_orders()                                                # the fill, before the sync has noted it
    assert len(pushed()) == 3 and pushed()[-1]["filled"] == 2 and pushed()[-1]["cut_at"] is None
    engine.executor._pending["1"].first_fill_at = time.monotonic()          # the order sync notes it
    engine._refresh_orders()
    engine._refresh_orders()
    assert len(pushed()) == 4 and pushed()[-1]["cut_at"]


def test_the_session_review_keeps_each_trade_with_what_it_was_taken_on(engine):
    from tos_bot.util import clock

    today = clock.now_ny().date()
    assert not engine.review_session(today)["ok"]                          # nothing offered or traded yet
    tid = _open(engine, "AAPL")
    engine.repo.close_trade(tid, exit_price=103.0, exit_reason="target")
    out = engine.review_session(today)
    review = out["review"]
    assert out["ok"] and review["day"]["trades"] == 1 and abs(review["trades"][0]["r"] - 0.6) < 1e-9
    # the note says how many setups not taken were followed, and apart from them how many would have filled
    assert "0 of 0 day setups not taken followed on the candles (0 would have filled)" in out["note"]
    assert engine.journal_review(today)["session"] == today.isoformat()
    assert engine.journal_state()["days"][0]["session"] == today.isoformat()


def test_a_rebuild_without_ib_gateway_keeps_the_plays_not_taken_followed_before(engine):
    today = clock.now_ny().date()
    p = _play("AAA")
    p.timeframe = Timeframe.INTRADAY
    engine.repo.record_play(p)                                              # a day setup offered and not taken
    engine.repo.save_shadow_trades(today, [{"play_id": p.id, "symbol": "AAA", "side": "LONG", "strategy": p.strategy,
                                            "filled": True, "r": 1.5, "features": {}}])
    out = engine.review_session(today)                                      # IB Gateway is away: nothing is followed
    assert out["ok"] and out["review"]["shadows"]["note"]
    assert [(r["play_id"], r["r"]) for r in engine.repo.shadow_trades(today)] == [(p.id, 1.5)]


def test_the_session_review_covers_positions_opened_and_still_open(engine, monkeypatch):
    from tos_bot.core.models import Quote

    today = clock.now_ny().date()
    monkeypatch.setattr(clock, "session_date", lambda *a, **k: today)       # on a weekend too, today is the session reviewed
    _open(engine, "AAPL")                                                   # entered at 100 with its stop at 95, still open
    assert engine._review_marks(today, engine.repo.trades_opened_between(today, today)) == {}   # no price source: no mark
    engine.md.attach(fakes.FakeGateway(["AAPL"]))
    monkeypatch.setattr(engine.md, "quote", lambda s: Quote(symbol=s, bid=102.4, ask=102.6, last=102.5))
    out = engine.review_session(today)
    day, [row] = out["review"]["day"], out["review"]["opened"]
    assert out["ok"] and (day["trades"], day["opened"], day["still_open"], day["open_r"]) == (0, 1, 1, 0.5)
    assert (row["symbol"], row["mark"], row["open_r"], row["open_pl"]) == ("AAPL", 102.5, 0.5, 12.5)
    listed = engine.journal_state()["days"][0]
    assert (listed["trades"], listed["opened"], listed["open_r"]) == (0, 1, 0.5) and "1 positions opened" in out["note"]


def test_an_entry_never_chases_the_price_past_the_play(engine, monkeypatch):
    from tos_bot.core.models import Quote

    p = _play("AAPL")                                                       # entry 100, stop 95: 1R is 5
    plan = {"executable": True, "order_type": "LIMIT", "limit_price": 100.05, "order_session": "REGULAR"}
    assert engine._chase_check(p, plan) is None                             # no price source: nothing to check
    from types import SimpleNamespace

    engine.md.attach(SimpleNamespace(quotes_from_bars=False, name="fake"))   # a live quote source
    tape = {"px": 100.5}
    monkeypatch.setattr(engine.md, "quote",
                        lambda s: Quote(symbol=s, bid=tape["px"] - 0.01, ask=tape["px"] + 0.01, last=tape["px"]))
    seen = {}
    assert engine._chase_check(p, plan, seen) is None and plan["limit_price"] == 100.55   # 0.1R past: priced off the quote
    assert seen["mid"] == 100.5 and seen["live"] and abs(seen["spread_bps"] - 1.99) < 0.01  # kept for the shortfall
    wide = Quote(symbol="AAPL", bid=100.0, ask=100.8, last=100.4)             # a spread of 0.16R of the risk
    monkeypatch.setattr(engine.md, "quote", lambda s: wide)
    assert "spread" in engine._chase_check(p, plan)
    monkeypatch.setattr(engine.md, "quote",
                        lambda s: Quote(symbol=s, bid=tape["px"] - 0.01, ask=tape["px"] + 0.01, last=tape["px"]))
    def dead(symbol):
        raise RuntimeError("no price")

    live_quote = engine.md.quote
    monkeypatch.setattr(engine.md, "quote", dead)                           # a price source that answers nothing
    assert "not entering blind" in engine._chase_check(p, plan)
    monkeypatch.setattr(engine.md, "quote", live_quote)
    tape["px"] = 102.0                                                      # 0.4R past the entry: the R:R is gone
    assert "not chasing" in engine._chase_check(p, plan)
    tape["px"], plan["limit_price"] = 99.0, 100.05                          # a pullback under the entry is no chase
    assert engine._chase_check(p, plan) is None and plan["limit_price"] == 100.05
    short = Play(symbol="MSFT", side=Side.SHORT, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                 timeframe=Timeframe.INTRADAY, entry=100.0, stop=105.0, targets=[90.0])
    tape["px"] = 98.5                                                       # 0.3R below a short's entry
    assert "not chasing" in engine._chase_check(short, plan)
    tape["px"] = 99.5
    assert engine._chase_check(short, plan) is None and plan["limit_price"] == 99.45


def test_autopilot_takes_only_what_the_filters_and_its_own_boxes_both_allow(engine):
    ap = engine.autopilot
    ap.enabled, ap.trade_types = True, ["INTRADAY", "SWING"]              # its own boxes: day and swing
    assert engine.set_filters(timeframes=["SWING"])["ok"]                  # the filters: swing plays only
    day = Play(symbol="MSFT", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
               timeframe=Timeframe.INTRADAY, entry=100.0, stop=95.0, targets=[110.0])
    assert ap.play_types() == ["SWING"] and ap.status()["trade_types"] == ["SWING"]
    assert "switched off" not in (ap._pre_gate(_play("AAPL"), 50_000) or "")
    assert "day trades are switched off" in ap._pre_gate(day, 50_000)
    assert not ap.day_mode_active(market_open=True)
    assert engine.set_filters(timeframes=["INTRADAY", "SWING"])["ok"]      # day plays on the board too...
    assert ap.play_types() == ["INTRADAY", "SWING"] and ap.day_mode_active(market_open=True)
    ap.trade_types = ["SWING"]                                             # ...but its own box says no day trades
    assert ap.play_types() == ["SWING"] and not ap.day_mode_active(market_open=True)
    assert "day trades are switched off" in ap._pre_gate(day, 50_000) and ap.status()["own_trade_types"] == ["SWING"]


def test_the_review_judges_a_play_on_autopilots_checks_not_on_its_type_boxes(engine):
    ap = engine.autopilot
    ap.trade_types, ap.skip_noise = ["SWING"], ["against_gap"]              # day trades unticked in its own boxes...
    ap.min_confidence, ap.min_reward_risk, ap.min_confirmations = 0.5, 2.0, 1
    assert engine.set_filters(timeframes=["INTRADAY", "SWING"])["ok"]      # ...while the filters put day plays on the board
    row = {"timeframe": "INTRADAY", "confidence": 0.6, "reward_risk": 2.5, "noise": [], "confirmations": 1}
    assert engine._passes_checks(row)                                      # judged on its merits, not on the box
    assert not engine._passes_checks({**row, "confidence": 0.4})
    assert not engine._passes_checks({**row, "reward_risk": 1.8})
    assert not engine._passes_checks({**row, "noise": ["against_gap", "conflict"]})
    assert engine._passes_checks({**row, "noise": ["conflict"]})            # a flag not skipped is only a flag
    ap.min_confirmations = 2
    assert not engine._passes_checks(row) and engine._passes_checks({**row, "confirmations": 2})
    swing = {**row, "timeframe": "SWING", "confidence": ap.min_swing_confidence}
    assert engine._passes_checks(swing) and not engine._passes_checks({**swing, "confidence": ap.min_swing_confidence - 0.01})


def _entered(at, skipped, by="autopilot", **settings):
    gates = {"min_confidence": 0.5, "min_swing_confidence": 0.5, "min_reward_risk": 2.0, "min_confirmations": 1,
             **settings}
    return {"play": {"evidence": {"at_entry": {"at": at, "by": by, "skipped_noise": skipped, "settings": gates}}}}


def test_the_review_judges_the_checks_with_the_gates_in_force_that_session(engine):
    ap = engine.autopilot
    ap.skip_noise, ap.min_confidence, ap.min_reward_risk, ap.min_confirmations = ["late_flag"], 0.5, 2.0, 1
    opened = [_entered("2026-01-05T10:00:00-05:00", ["old_flag"], min_reward_risk=3.0),
              _entered("2026-01-05T11:00:00-05:00", ["against_gap"]),       # the latest entry's gates win
              _entered("2026-01-05T12:00:00-05:00", ["other"], by="operator")]
    gates = engine._session_gates(opened, None)
    assert gates["skip_noise"] == ["against_gap"] and gates["min_reward_risk"] == 2.0
    assert gates["source"] == "at the last entry" and "replay_losers" not in gates
    row = {"timeframe": "INTRADAY", "confidence": 0.6, "reward_risk": 2.5, "noise": ["late_flag"],
           "confirmations": 1, "strategy": "s1"}
    assert engine._passes_checks(row, gates)                  # a flag learned after the session doesn't count
    assert not engine._passes_checks(row)                     # ...as it would on today's settings
    assert not engine._passes_checks({**row, "noise": ["against_gap"]}, gates)
    # the replay losers the session turned away, only when recorded
    losers = engine._session_gates([_entered("2026-01-05T10:00:00-05:00", [], replay_losers=["s1"])], None)
    assert not engine._passes_checks(row, losers) and engine._passes_checks({**row, "strategy": "s2"}, losers)
    # no entry: the earlier build's gates, else today's settings, labelled
    earlier = {**gates, "source": "at the last entry", "rolling_sessions": 20}
    assert engine._session_gates([], earlier) == {k: v for k, v in earlier.items() if k != "rolling_sessions"}
    now = engine._session_gates([], {"skip_noise": ["x"], "min_confirmations": 2})   # an old review lacks the floors
    assert now["source"] == "at the rebuild" and "late_flag" in now["skip_noise"] and now["min_confidence"] == 0.5


def test_a_rebuilt_review_follows_the_plays_not_taken_on_the_gates_in_force_that_session(engine, monkeypatch):
    import pandas as pd

    today = clock.now_ny().date()
    ap = engine.autopilot
    ap.skip_noise, ap.min_confidence, ap.min_reward_risk, ap.min_confirmations = ["late_flag"], 0.5, 2.0, 1
    missed = Play(symbol="AAA", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                  timeframe=Timeframe.INTRADAY, entry=100.0, stop=99.0, targets=[102.0], confidence=0.6)
    missed.noise = ["late_flag"]                      # flagged only with a flag learned after the session
    engine.repo.record_play(missed)
    session = pd.date_range(pd.Timestamp(f"{today} 09:30", tz="America/New_York"), periods=78, freq="5min")
    candles = pd.DataFrame({"open": 100.0, "high": 100.05, "low": 99.95, "close": 100.0, "volume": 5e5}, index=session)
    monkeypatch.setattr(engine, "_session_bars", lambda day, plays, booked=(): {"AAA": candles})

    def review():
        out = engine.review_session(today)
        [row] = [r for r in out["review"]["shadows"]["plays"] if r["play_id"] == missed.id]
        return out["review"], row

    # no entry and no earlier build: today's settings, and the review says so
    built, row = review()
    assert built["settings"]["source"] == "at the rebuild" and not row["passed_checks"]
    assert ("No Autopilot entry recorded the checks in force this session, so its plays were judged on the settings "
            "at the rebuild.") in built["lessons"]

    # an Autopilot entry recorded the session's checks: the play is judged on them, not on today's
    taken = _play("BBB")
    taken.evidence["at_entry"] = _entered(f"{today}T10:30:00-04:00", ["old_flag"])["play"]["evidence"]["at_entry"]
    taken.suggested_qty = 5
    engine.repo.record_play(taken)
    engine.repo.open_trade(taken, 100.0, 5, "paper")
    built, row = review()
    assert built["settings"]["source"] == "at the last entry" and built["settings"]["skip_noise"] == ["old_flag"]
    assert row["passed_checks"] and not any("judged on the settings at the rebuild" in s for s in built["lessons"])

    # rebuilt with no entry on record: the gates the earlier build kept
    monkeypatch.setattr(engine.repo, "trades_opened_between", lambda first, last: [])
    built, row = review()
    assert built["settings"]["source"] == "at the last entry" and built["settings"]["skip_noise"] == ["old_flag"]
    assert row["passed_checks"]


# ---------------------------------------------------------------- market prices on the dashboard
def test_each_play_carries_the_latest_price_the_app_holds_and_when_its_from(engine):
    p = _play("AAPL")
    engine.md.attach(fakes.FakeGateway(["AAPL"], delayed=True))
    row = engine._decorate(p)
    assert row["last_price"] is None and row["last_at"] is None                 # nothing fetched yet
    engine.md.quote("AAPL")
    row = engine._decorate(p)
    assert row["last_price"] == engine.md.last_seen("AAPL")[0] and row["last_at"].startswith("20")


def test_each_play_carries_its_setups_replay_record_read_once_for_the_board(engine, monkeypatch):
    losing = {"trades": 40, "expectancy_r": -0.09, "win_rate": 0.4, "avg_win_r": 0.45,
              "out_of_sample": {"trades": 12, "expectancy_r": -0.1}}
    proven = {"trades": 36, "expectancy_r": 0.2, "win_rate": 0.5, "avg_win_r": 1.1,
              "out_of_sample": {"trades": 12, "expectancy_r": 0.15}}
    reads = []
    monkeypatch.setattr(engine, "strategy_record",
                        lambda key: reads.append(key) or {"setup_a": losing, "setup_b": proven}.get(key))
    sent = []
    monkeypatch.setattr(engine, "_publish", lambda topic, **payload: sent.append(payload["plays"])
                        if topic == "plays.updated" else None)
    engine.board.replace([_play("AAA", strategy="setup_a"), _play("BBB", strategy="setup_a"),
                          _play("CCC", strategy="setup_b"), _play("DDD", strategy="setup_c")])
    assert not engine.autopilot.enabled            # Autopilot reads no records itself: the reads are the rows'

    engine._publish_plays()
    rows = {r["symbol"]: r["record"] for r in sent[-1]}
    assert rows["AAA"] == rows["BBB"] == {
        "trades": 40, "expectancy_r": -0.09, "win_rate": 0.4, "avg_win_r": 0.45, "held_out_trades": 12,
        "held_out_r": -0.1, "proven": False, "why": engine.autopilot.proof_missing("setup_a", losing)}
    assert rows["CCC"]["proven"] is True and rows["CCC"]["why"] is None and rows["CCC"]["avg_win_r"] == 1.1
    none = rows["DDD"]                             # a setup the replay has no trades from
    assert (none["trades"], none["expectancy_r"], none["held_out_trades"], none["proven"]) == (0, None, 0, False)
    assert "has 0 of the" in none["why"]
    assert sorted(reads) == ["setup_a", "setup_b", "setup_c"]          # once a setup, not once a play
    reads.clear()
    engine._publish_plays()                        # read afresh each time: a new replay shows at once
    assert sorted(reads) == ["setup_a", "setup_b", "setup_c"]
    reads.clear()
    assert len(engine.current_plays()) == 4 and sorted(reads) == ["setup_a", "setup_b", "setup_c"]


#: what the dashboard reads of a play from the board's push: the plays table (plays.js), the notes, the orders,
#: the chart, the Autopilot strip and the event handlers
DASHBOARD_READS = {"id", "symbol", "sector", "side", "strategy", "timeframe", "entry", "stop", "targets", "reward_risk",
                   "suggested_qty", "dollar_risk", "score", "status", "trade_id", "noise", "extended_hours_ok",
                   "confirmations", "rationale", "last_price", "last_at", "autopilot", "record", "evidence"}


def _explained_play(symbol, strategy="setup_a"):
    p = _play(symbol, strategy=strategy)
    p.rationale, p.explanation, p.invalidation = "one line", "THE EDGE ... " * 200, "a close below 95.00"
    p.evidence = {"spark": [100.0 + i / 10 for i in range(60)], "signal_nudge": 0.02, "signal_reasons": "a filing",
                  "expected_r": 0.6, "bar_at": "2026-01-05T15:30:00+00:00", "vol_forecast": {"vol": 0.02}}
    return p


def test_the_board_push_sends_only_what_the_dashboard_reads_of_a_play(engine, monkeypatch):
    sent = []
    monkeypatch.setattr(engine, "_publish", lambda topic, **payload: sent.append(payload["plays"])
                        if topic == "plays.updated" else None)
    engine.board.replace([_explained_play("AAA"), _explained_play("BBB", strategy="setup_b")])
    engine._publish_plays()
    rows = sent[-1]
    assert len(rows) == 2
    for row in rows:
        assert DASHBOARD_READS <= set(row)
        assert not {"explanation", "invalidation", "tags", "probability", "notional"} & set(row)
        assert row["evidence"] == {"signal_nudge": 0.02, "signal_reasons": "a filing", "expected_r": 0.6}
        # Autopilot's verdict and the replay record go whole
        assert set(row["autopilot"]) == {"eligible", "why_not", "waiting", "acted", "skipped", "reason"}
        assert row["autopilot"]["why_not"] == "Autopilot is off" and row["record"]["why"]
    assert engine.current_plays() == rows                            # /api/plays and the socket's hello: the same rows
    whole = engine.current_plays(full=True)                          # ?full=1
    assert whole[0]["explanation"].startswith("THE EDGE") and len(whole[0]["evidence"]["spark"]) == 60
    assert len(str(rows)) * 4 < len(str(whole))


def test_one_play_is_served_whole_for_its_hover_until_it_leaves_the_board(engine):
    p = _explained_play("AAA")
    engine.board.replace([p])
    row = engine.play_row(p.id)                                      # GET /api/plays/{id}
    assert row["explanation"].startswith("THE EDGE") and row["evidence"]["spark"] and row["invalidation"]
    assert row["autopilot"]["why_not"] and row["record"]["trades"] == 0
    assert engine.play_row("play_gone") is None
    engine.board.clear()
    assert engine.play_row(p.id) is None                             # the route answers 404


def test_autopilots_badge_is_handed_the_play_itself_not_only_its_rounded_row(engine, monkeypatch):
    p, handed = _play("AAA"), []
    monkeypatch.setattr(engine.autopilot, "decorate_play", lambda row, play=None: handed.append(play) or row)
    engine._decorate(p)
    assert handed == [p]


def test_refresh_prices_the_plays_and_the_positions_and_sends_the_plays_out_again(engine, monkeypatch):
    p = _play("MSFT")
    engine.board.replace([p])
    engine.md.attach(fakes.FakeGateway(["AAPL", "MSFT"], delayed=True))
    engine._refresh_account()
    engine._account.positions = [Position(symbol="AAPL", quantity=10, avg_price=100.0, market_price=100.0)]
    asked, sent = [], []
    real = engine.md.refresh_prices
    monkeypatch.setattr(engine.md, "refresh_prices", lambda symbols, con_ids=None: asked.append(list(symbols))
                        or real(symbols, con_ids))
    monkeypatch.setattr(engine, "_publish_plays", lambda: sent.append(True))
    assert engine.refresh_prices() == 2
    assert asked == [["AAPL", "MSFT"]] and sent == [True]                      # the position first, one batch
    price, at, _ = engine.md.last_seen("AAPL")
    [pos] = engine.snapshot()["positions"]
    assert pos["market_price"] == round(price, 4) and pos["price_at"] == at.isoformat()   # the app's fresher mark
    assert pos["unrealized_pl"] == round((price - 100.0) * 10, 2)

    engine.APP_MARK_S = -1.0                                                    # a price that has aged: the broker's mark
    [pos] = engine.snapshot()["positions"]
    assert (pos["market_price"], pos["price_at"], pos["unrealized_pl"]) == (100.0, None, 0.0)


class _AfterTheClose(fakes.FakeGateway):
    """A Gateway after the close: its extended-hours candles are the session's, then a trade at 16:30
    a dollar above the last regular-hours price."""

    def history_many(self, requests, con_ids=None, end=None, rth=True):
        import pandas as pd

        out = super().history_many(requests, con_ids, end)
        if not rth:
            for symbol, frame in out.items():
                late = frame.iloc[[-1]].copy()
                late.index = late.index + dt.timedelta(minutes=35)
                late["close"] += 1.0
                out[symbol] = pd.concat([frame, late])
        return out


def test_a_refresh_after_the_close_moves_a_positions_price_but_not_the_price_the_exits_read(engine, monkeypatch):
    engine.md.attach(_AfterTheClose(["AAA"], delayed=True))
    engine._refresh_account()
    engine._account.positions = [Position(symbol="AAA", quantity=10, avg_price=100.0, market_price=100.0)]
    regular = engine.md.quote("AAA")                                            # what the exit manager acts on
    [before] = engine.snapshot()["positions"]
    assert before["market_price"] == pytest.approx(regular.last, abs=1e-4)
    monkeypatch.setattr(engine, "_publish_plays", lambda: None)
    assert engine.refresh_prices() == 1
    [after] = engine.snapshot()["positions"]
    assert after["market_price"] == pytest.approx(regular.last + 1.0, abs=1e-3)
    assert dt.datetime.fromisoformat(after["price_at"]) - dt.datetime.fromisoformat(before["price_at"]) \
        == dt.timedelta(minutes=35)
    assert engine.md.quote("AAA").last == regular.last                          # the exits never see it
    assert engine.price_of("AAA")["session"] == "after-hours"


def test_a_stocks_price_says_when_its_from_and_the_session_it_traded_in(engine, monkeypatch):
    assert engine.price_of("AAA") == {"ok": False, "symbol": "AAA",
                                      "reason": "IB Gateway isn't connected, so there's no price."}
    engine.md.attach(fakes.FakeGateway(["AAA"], delayed=True))
    held, asked = {}, []
    monkeypatch.setattr(engine.md, "price_now", lambda symbol, con_id=None: asked.append(con_id) or held.get(symbol))
    day = dt.date(2026, 9, 16)
    for hhmm, session in (("04:00", "pre-market"), ("09:29", "pre-market"), ("09:30", "regular"),
                          ("15:59", "regular"), ("16:00", "after-hours"), ("19:59", "after-hours")):
        at = dt.datetime.combine(day, dt.time.fromisoformat(hhmm), clock.NY)
        held["AAA"] = (12.3456, at, 3.04)
        assert engine.price_of("AAA") == {"ok": True, "symbol": "AAA", "price": 12.3456, "at": at.isoformat(),
                                          "age_s": 3.0, "session": session}
    held["AAA"] = (12.3456, dt.datetime(2026, 9, 19, 12, 0, tzinfo=clock.NY), 1.0)    # a Saturday
    assert engine.price_of("AAA")["session"] == "closed"
    held.clear()
    assert engine.price_of("AAA") == {"ok": False, "symbol": "AAA", "reason": "No recent price for AAA."}
    engine.scanner.con_ids = lambda symbols: {"AAA": 1234}
    engine.price_of("AAA")
    assert asked[-1] == 1234                                                    # the stock's contract when it's known


def test_the_streams_go_to_the_positions_first_then_the_plays_still_on_offer(engine, monkeypatch):
    from tos_bot.core.enums import PlayStatus

    gateway = fakes.StreamingGateway(fakes.SYMBOLS)
    gateway.connect()
    engine.md.attach(gateway)
    _open(engine, "T01")                                                        # a trade on this venue
    working = {"order_id": "o1", "play_id": "p1", "symbol": "T05", "strategy": "vwap_reclaim",
               "timeframe": "SWING", "qty": 1, "notional": 100.0, "risk": 5.0}
    monkeypatch.setattr(engine, "working_entries", lambda: [working])         # an entry not filled yet
    engine._refresh_account()
    engine._account.positions = [Position(symbol="T02", quantity=10, avg_price=50.0, market_price=50.0)]
    offered, taken, held = _play("T03"), _play("T04"), _play("T01")
    offered.score, taken.score, held.score = 0.5, 0.9, 0.4
    engine.board.replace([offered, taken, held], None)
    taken.status = PlayStatus.ACCEPTED
    assert engine._stream_wanted() == (["T01", "T05", "T02"], ["T03", "T01"])

    execution = engine.settings.config.execution
    monkeypatch.setattr(execution, "stream_lines", 3)
    assert engine._resync_streams() == ["T01", "T05", "T02"]                   # the positions take every line
    monkeypatch.setattr(execution, "stream_lines", 5)
    assert engine._resync_streams() == ["T01", "T05", "T02", "T03"]            # T01 counts once

    gateway.tick("T02", 55.0)
    assert engine._marks()["T02"][0] == 55.0                                    # the position's mark is the stream's
    [pos] = engine.snapshot()["positions"]
    assert pos["unrealized_pl"] == round((55.0 - 50.0) * 10, 2)

    monkeypatch.setattr(execution, "stream_lines", 0)
    monkeypatch.setattr(engine, "_stream_wanted", lambda: pytest.fail("nothing is looked up with the streams off"))
    assert engine._resync_streams() == [] and gateway.streams == []


def test_new_plays_and_an_approval_wake_the_stream_loop_and_stopping_ends_it(engine, monkeypatch):
    engine._stream_wake.clear()
    engine._publish_plays()
    assert engine._stream_wake.is_set()

    p = _play("T06")
    engine.board.replace([p])
    monkeypatch.setattr(engine, "assess_play",
                        lambda pid: {"ok": True, "can_execute": True, "reasons": [], "order_plan": {}})
    monkeypatch.setattr(engine, "_chase_check", lambda p, plan, seen: None)
    monkeypatch.setattr(engine.executor, "execute_play", lambda p, account, **kw: {"ok": True, "status": "SUBMITTED"})
    engine._stream_wake.clear()
    assert engine.approve_play(p.id)["ok"] and engine._stream_wake.is_set()   # its stock streams from the next pass

    loop = threading.Thread(target=engine._stream_loop, daemon=True)
    loop.start()
    engine.stop()
    loop.join(2.0)
    assert not loop.is_alive()


def test_an_entry_is_priced_off_a_fresh_stream_else_a_snapshot_and_the_log_says_which(engine, monkeypatch, caplog):
    import logging

    gateway = fakes.StreamingGateway(fakes.SYMBOLS, delayed=True)
    gateway.connect()
    engine.md.attach(gateway)
    p = _play("T01")                                                        # entry 100, stop 95: 1R is 5
    plan = {"executable": True, "order_type": "LIMIT", "limit_price": 100.05, "order_session": "REGULAR"}
    seen = {}
    engine._chase_check(p, plan, seen)                                      # delayed data: a candle, as before
    assert (seen["quote_source"], seen["live"], gateway.snapshots) == ("candle", False, 0)

    gateway.delayed = False
    assert engine.md.streams.sync(["T01"], [], 5) == ["T01"]
    gateway.tick("T01", 100.5, age_s=0.5)
    seen = {}
    with caplog.at_level(logging.INFO, logger="tos_bot.engine.engine"):
        assert engine._chase_check(p, plan, seen) is None
    assert plan["limit_price"] == 100.55 and gateway.snapshots == 0         # priced off the stream: no round trip
    assert seen["quote_source"] == "stream" and seen["live"] and 500 <= seen["quote_age_ms"] < 2000
    assert "entry check for T01 on a stream quote" in caplog.text

    # the order goes out on it: the limit, and the quote the fill is measured against, are the stream's
    engine.board.replace([p])
    monkeypatch.setattr(engine, "assess_play", lambda pid: {"ok": True, "can_execute": True, "reasons": [],
                                                            "order_plan": {**plan, "limit_price": 100.05}})
    sent = {}
    monkeypatch.setattr(engine.executor, "execute_play",
                        lambda play, account, **kw: sent.update(kw) or {"ok": True, "status": "SUBMITTED"})
    gateway.tick("T01", 100.5)
    assert engine.approve_play(p.id)["ok"]
    assert sent["plan"]["limit_price"] == 100.55 and sent["decision"]["quote_source"] == "stream"
    assert gateway.snapshots == 0

    gateway.tick("T01", 100.5, age_s=2.5)                                   # quiet for over 2 s: never waited on
    seen = {}
    engine._chase_check(p, plan, seen)
    assert seen["quote_source"] == "snapshot" and gateway.snapshots == 1


def test_the_play_opened_on_the_dashboard_streams_first_but_one_autopilot_assesses_doesnt(engine, monkeypatch):
    from fastapi.testclient import TestClient
    from tos_bot.core.enums import PlayStatus
    from tos_bot.server import security
    from tos_bot.server.app import create_app

    gateway = fakes.StreamingGateway(fakes.SYMBOLS)
    gateway.connect()
    engine.md.attach(gateway)
    monkeypatch.setattr(engine.settings.config.execution, "stream_lines", 2)
    best, next_, last, taken = _play("T03"), _play("T04"), _play("T05"), _play("T06")
    best.score, next_.score, last.score, taken.score = 0.9, 0.5, 0.3, 0.1
    engine.board.replace([best, next_, last, taken], None)
    taken.status = PlayStatus.ACCEPTED
    assert engine._resync_streams() == ["T03", "T04"]

    engine._stream_wake.clear()
    engine.assess_play(last.id)                                             # how Autopilot looks at a play
    engine.watch_play(taken.id)                                             # a play already sent isn't on offer
    assert engine.md.streams.preferred() == [] and not engine._stream_wake.is_set()
    assert engine._resync_streams() == ["T03", "T04"]

    monkeypatch.setattr(security, "ALLOWED_CLIENTS", security.ALLOWED_CLIENTS | {"testclient"})
    monkeypatch.setattr(security, "ALLOWED_HOSTS", security.ALLOWED_HOSTS | {"testserver"})
    app = create_app(lambda settings: None)
    app.state.engine = engine
    client = TestClient(app, headers={"X-ATB-Request": "1"})
    assert client.post(f"/api/plays/{last.id}/assess").status_code == 200   # the operator opens it
    assert engine.md.streams.preferred() == ["T05"] and engine._stream_wake.is_set()
    assert engine._resync_streams() == ["T05", "T03"]                       # ahead of plays streaming under a minute


def test_the_plays_autopilot_would_take_stream_ahead_of_the_rest_while_on_offer(engine, monkeypatch):
    from tos_bot.core.enums import PlayStatus

    gateway = fakes.StreamingGateway(fakes.SYMBOLS)
    gateway.connect()
    engine.md.attach(gateway)
    monkeypatch.setattr(engine.settings.config.execution, "stream_lines", 2)
    plays = [_play(s) for s in ("T03", "T04", "T05", "T06")]
    for p, score in zip(plays, (0.9, 0.7, 0.5, 0.3)):
        p.score = score
    engine.board.replace(plays, None)
    verdicts = {"T05": {"eligible": True, "acted": False},                  # Autopilot would take T05...
                "T06": {"eligible": True, "acted": True}}                   # ...and has tried T06 already
    monkeypatch.setattr(engine.autopilot, "decorate_play",
                        lambda row, play=None: {**row, "autopilot": verdicts.get(row["symbol"], {"eligible": False})})
    engine._publish_plays()
    assert engine._ap_candidates == ("T05",)
    assert engine._resync_streams() == ["T05", "T03"]

    plays[2].status = PlayStatus.ACCEPTED                                   # sent since: no longer a play on offer
    assert engine._resync_streams() == ["T03", "T04"]


def test_the_watch_tier_streams_after_the_positions_and_plays(engine, monkeypatch):
    from tos_bot.scanner.watchlist import Candidate, DayWatchlist

    day = dt.date(2026, 9, 24)
    monkeypatch.setattr(clock, "now_ny", lambda: dt.datetime.combine(day, dt.time(10, 0), clock.NY))
    gateway = fakes.StreamingGateway(fakes.SYMBOLS)
    gateway.connect()
    engine.md.attach(gateway)
    _open(engine, "T01")                                                        # a trade on this venue
    best, next_ = _play("T02"), _play("T03")
    best.score, next_.score = 0.9, 0.5
    engine.board.replace([best, next_], None)
    engine.scanner.watchlist = DayWatchlist(
        session=day, bars_through=day - dt.timedelta(days=1), built_at="", universe=40, liquid=40,
        hot=[Candidate("T04", "Technology", 1.0, heat=3.0), Candidate("T03", "Technology", 1.0, heat=2.0),
             Candidate("T05", "Energy", 1.0, heat=1.0)],
        queues={}, kept={"Energy": [Candidate("T06", "Energy", 1.0, heat=0.5)]})
    execution = engine.settings.config.execution
    monkeypatch.setattr(execution, "stream_lines", 6)
    monkeypatch.setattr(execution, "stream_watch", 3)
    # the position, the plays, then the tier's first three - T03 once, as a play; T06 is past stream_watch
    assert engine._resync_streams() == ["T01", "T02", "T03", "T04", "T05"]
    assert gateway.stream_calls[-1] == (["T01", "T02", "T03", "T04", "T05"], 6) and gateway.protect == 1

    monkeypatch.setattr(execution, "stream_watch", 0)
    monkeypatch.setattr(engine.scanner, "watch_symbols", lambda *a, **k: pytest.fail("no watch tier when it's off"))
    assert engine._resync_streams() == ["T01", "T02", "T03"]
    assert gateway.stream_calls[-1] == (["T01", "T02", "T03"], 6)            # as before the watch tier


def _streaming_position(engine, symbol="T01"):
    """A position here whose stock streams, next to a play's stock (T03) that streams too."""
    gateway = fakes.StreamingGateway(fakes.SYMBOLS)
    gateway.connect()
    engine.md.attach(gateway)
    tid = _open(engine, symbol)
    assert engine.md.streams.sync([symbol], ["T03"], 5) == [symbol, "T03"]
    return gateway, tid


def test_a_tick_wakes_the_exits_only_for_a_stock_held_and_only_in_regular_hours(engine):
    gateway, _ = _streaming_position(engine)
    engine.exit_manager.watched = frozenset({"T01"})                  # what the last full pass managed
    engine._tick_exits_on = True
    gateway.tick("T03", 50.0)                                           # a play's stock: not the exits' business
    assert not engine._exit_wake.is_set() and engine._take_ticked() == frozenset()
    gateway.tick("T01", 101.0)
    assert engine._exit_wake.is_set() and engine._take_ticked() == frozenset({"T01"})
    engine._exit_wake.clear()
    engine._tick_exits_on = False                                       # outside regular hours
    gateway.tick("T01", 101.5)
    assert not engine._exit_wake.is_set() and engine._take_ticked() == frozenset()


def test_a_tick_pass_comes_a_second_after_the_last_exits_at_the_soonest_and_asks_for_no_snapshot(engine):
    from tos_bot.engine.engine import tick_exit_wait

    wait, go = tick_exit_wait(now=10.3, last_exit=10.0, deadline=14.0, gap=1.0)
    assert go and abs(wait - 0.7) < 1e-9                                # 0.3 s after the last pass: 0.7 s more
    assert tick_exit_wait(now=11.5, last_exit=10.0, deadline=14.0, gap=1.0) == (0.0, True)
    wait, go = tick_exit_wait(now=13.2, last_exit=13.0, deadline=14.0, gap=1.0)
    assert not go and abs(wait - 0.8) < 1e-9                            # the full pass is due first: wait for it

    gateway, tid = _streaming_position(engine)
    gateway.tick("T01", 107.0)                                          # +1.4R: the stop goes to break-even and a bit
    engine._run_exits(only=frozenset({"T01"}))
    t = engine.repo.get_trade(tid)
    assert t["stop_price"] == 101.55 and "stop->" not in (t["notes"] or "")
    assert gateway.snapshots == 0                                       # priced off the stream


def test_the_sync_loop_runs_the_exits_on_a_tick_between_its_full_passes_and_stops_promptly(engine, monkeypatch):
    from tos_bot.engine import engine as module

    passes, session = [], {"now": clock.Session.REGULAR}
    monkeypatch.setattr(engine, "_sync_orders", lambda: None)
    monkeypatch.setattr(engine, "_check_quit_progress", lambda: None)
    monkeypatch.setattr(engine, "_run_exits", lambda only=None: passes.append((only, time.monotonic())))
    monkeypatch.setattr(module.clock, "current_session", lambda ts=None: session["now"])
    monkeypatch.setattr(engine, "SYNC_S", 0.6)
    monkeypatch.setattr(engine, "EXIT_TICK_GAP_S", 0.2)
    engine.exit_manager.watched = frozenset({"T01"})
    loop = threading.Thread(target=engine._sync_loop, daemon=True)
    loop.start()
    assert _until(lambda: len(passes) == 1) and engine._tick_exits_on   # a full pass, in regular hours
    engine._on_stream_ticks(frozenset({"T01", "T03"}))
    assert _until(lambda: len(passes) == 2)
    (full, t0), (only, t1) = passes
    assert full is None and only == frozenset({"T01"}) and t1 - t0 >= 0.2 - 0.02
    assert _until(lambda: len(passes) == 3)
    assert passes[2][0] is None and passes[2][1] - t0 >= 0.6 - 0.02      # the full pass keeps its turn
    session["now"] = clock.Session.POST
    monkeypatch.setattr(engine, "SYNC_S", 30.0)
    assert _until(lambda: len(passes) == 4) and not engine._tick_exits_on
    engine._on_stream_ticks(frozenset({"T01"}))                         # after hours the full pass reads it
    engine.stop()
    loop.join(2.0)                                                      # not the 30 s to the next full pass
    assert not loop.is_alive() and [only for only, _ in passes[3:]] == [None]


def test_a_tick_pass_comes_a_second_after_the_last_tick_pass_too_not_just_the_last_full_pass(engine, monkeypatch):
    from tos_bot.engine import engine as module

    passes = []
    monkeypatch.setattr(engine, "_sync_orders", lambda: None)
    monkeypatch.setattr(engine, "_check_quit_progress", lambda: None)
    monkeypatch.setattr(engine, "_run_exits", lambda only=None: passes.append((only, time.monotonic())))
    monkeypatch.setattr(module.clock, "current_session", lambda ts=None: clock.Session.REGULAR)
    monkeypatch.setattr(engine, "SYNC_S", 5.0)                          # room for two tick passes before a full one
    monkeypatch.setattr(engine, "EXIT_TICK_GAP_S", 0.4)
    engine.exit_manager.watched = frozenset({"T01"})
    loop = threading.Thread(target=engine._sync_loop, daemon=True)
    loop.start()
    assert _until(lambda: len(passes) == 1)                             # the full pass
    engine._on_stream_ticks(frozenset({"T01"}))
    assert _until(lambda: len(passes) == 2)                             # a tick pass, the gap after the full pass
    engine._on_stream_ticks(frozenset({"T01"}))                         # a busy tape: another tick straight away
    assert _until(lambda: len(passes) == 3)
    engine.stop()
    loop.join(2.0)
    (full, _), (first, t1), (second, t2) = passes
    assert full is None and first == second == frozenset({"T01"})
    assert t2 - t1 >= 0.4 - 0.05                                        # the gap counts from the tick pass as well


def test_the_streamed_prices_that_moved_go_to_the_dashboard_at_most_once_a_second(engine, monkeypatch):
    from tos_bot.engine.engine import SESSION_WORDS

    gateway, _ = _streaming_position(engine)
    sent = []
    monkeypatch.setattr(engine, "_publish", lambda topic, **payload: sent.append((topic, payload, time.monotonic())))
    monkeypatch.setattr(engine, "PRICE_PUSH_S", 0.5)
    loop = threading.Thread(target=engine._price_push_loop, daemon=True)
    loop.start()
    time.sleep(0.1)
    assert sent == []                                                   # nothing streamed: nothing sent
    q = gateway.tick("T01", 101.0)
    assert _until(lambda: len(sent) == 1)
    topic, payload, t0 = sent[0]
    assert topic == "prices.tick" and payload == {"prices": {"T01": {
        "price": 101.0, "at": q.ts.isoformat(), "session": SESSION_WORDS[clock.current_session(q.ts)]}}}
    gateway.tick("T01", 101.5)
    gateway.tick("T03", 50.0)
    gateway.tick("T01", 102.0)
    assert _until(lambda: len(sent) == 2)
    prices, t1 = sent[1][1]["prices"], sent[1][2]
    assert t1 - t0 >= 0.5 - 0.02                                        # one message a second at the most...
    assert {s: p["price"] for s, p in prices.items()} == {"T01": 102.0, "T03": 50.0}   # ...with each stock's latest
    engine.stop()
    loop.join(2.0)
    assert not loop.is_alive() and len(sent) == 2


# ---------------------------------------------------------------- open positions on the dashboard
def test_each_open_position_says_what_rests_at_the_broker_to_close_it(engine):
    """The Open positions tab's protection chip reads the orders the executor placed and follows
    (protective_stops / resting_targets), and its R now and time-stop cells read the record's own fields."""
    engine._venue = "ibkr-paper"
    both, stop_only = _open(engine, "AAA", venue="ibkr-paper"), _open(engine, "BBB", venue="ibkr-paper")
    bare, parked = _open(engine, "CCC", venue="ibkr-paper"), _open(engine, "DDD", venue="paper")
    stop = {"trade_id": both, "symbol": "AAA", "order_id": "11", "qty": 5.0, "stop_price": 95.0}
    target = {"trade_id": both, "symbol": "AAA", "order_id": "12", "qty": 3.0, "limit_price": 110.0}
    engine.executor.native_stops_on = lambda: True
    engine.executor.protective_stops = lambda: [stop, {**stop, "trade_id": stop_only, "symbol": "BBB", "order_id": "21"}]
    engine.executor.resting_targets = lambda: [target]

    rows = {t["id"]: t for t in engine.open_positions()}
    assert rows[both]["protection"] == {"native": True, "stop": stop, "target": target}           # green
    assert rows[stop_only]["protection"]["stop"]["order_id"] == "21" and rows[stop_only]["protection"]["target"] is None
    assert rows[bare]["protection"] == {"native": True, "stop": None, "target": None}              # red: none yet
    assert rows[parked]["protection"] is None                  # another venue's: nothing is placed while it's parked
    read = {"entry_price", "initial_stop_price", "stop_price", "side", "timeframe", "overwatch_at", "managed_exit",
            "pair_id", "broker"}
    assert all(read <= set(t) for t in rows.values())

    engine.executor.native_stops_on = lambda: False            # a venue that rests none: the app watches the price
    engine.executor.protective_stops = lambda: []
    engine.executor.resting_targets = lambda: []
    rows = {t["id"]: t for t in engine.open_positions()}
    assert rows[both]["protection"] == {"native": False, "stop": None, "target": None}
    assert engine.snapshot()["exit_manager"]["intraday_time_stop"] is True     # the countdown shows only while it's on


# ---------------------------------------------------------------- what became of a play, in the play log
def test_the_executor_tells_autopilot_about_an_entry_that_bought_nothing_even_after_a_switch(engine):
    assert engine.executor.on_entry_unfilled == engine.autopilot.entry_unfilled
    engine.executor.rebind(engine._broker, venue=engine._venue)
    assert engine.executor.on_entry_unfilled == engine.autopilot.entry_unfilled


def test_autopilot_hears_of_the_entries_taken_over_after_a_restart_even_after_a_switch(engine):
    # they may be taken over by a later order sync, when the broker couldn't list them at the start
    assert engine.executor.on_entries_adopted == engine.autopilot.recognise_entries
    engine.executor.rebind(engine._broker, venue=engine._venue)
    assert engine.executor.on_entries_adopted == engine.autopilot.recognise_entries


def test_what_became_of_a_sent_play_is_saved_without_losing_who_sent_it(engine):
    from tos_bot.scanner.scanner import ScanResult

    p = _play("AAPL")
    engine.repo.record_play(p)
    engine.repo.set_play_status(p.id, "SUBMITTED", "autopilot")
    assert engine.repo.settle_play(p.id, "CANCELED", {"status": "CANCELED", "reason": "not filled within 10 minutes"})
    row = engine.repo.get_play(p.id)
    assert (row["status"], row["decided_by"]) == ("CANCELED", "autopilot")
    assert row["evidence"]["entry_outcome"]["reason"] == "not filled within 10 minutes"

    copy = _play("AAPL")                                                    # a scan's stale copy of the same play
    copy.id = p.id
    engine.repo.record_scan(ScanResult(kind="cycle", plays=[copy]), plays=[copy])
    assert engine.repo.get_play(p.id)["status"] == "CANCELED"              # doesn't undo what happened

    q = _play("MSFT")
    q.suggested_qty = 5
    engine.repo.record_play(q)
    engine.repo.open_trade(q, 100.0, 5, "paper")
    assert not engine.repo.settle_play(q.id, "CANCELED") and engine.repo.get_play(q.id)["status"] == "FILLED"
    assert not engine.repo.settle_play("play_nothing", "CANCELED")


def test_a_setup_already_acted_on_isnt_logged_again_when_a_scan_sees_it(engine, monkeypatch):
    from tos_bot.core.enums import PlayStatus
    from tos_bot.scanner.scanner import ScanResult

    engine.autopilot.enabled = False
    found = []

    def cycle(fast=False):
        found.append(_play("AAPL"))
        return ScanResult(kind="cycle", symbols=["AAPL"], plays=[found[-1]])

    monkeypatch.setattr(engine.scanner, "run_cycle", cycle)
    engine._run_scan("cycle")
    engine._run_scan("cycle")                                               # the same setup again: one play, confirmed
    pid = found[0].id
    assert found[1].id == pid and engine.repo.get_play(pid)["confirmations"] == 2
    engine.board.get(pid).status = PlayStatus.SUBMITTED                     # sent...
    engine.repo.set_play_status(pid, "SUBMITTED", "autopilot")
    engine._run_scan("cycle")                                               # ...and seen again: not offered, not logged
    assert found[2].id != pid and engine.repo.get_play(found[2].id) is None
    assert engine.repo.get_play(pid)["status"] == "SUBMITTED"


def test_a_play_already_sent_cant_be_dismissed_and_one_never_logged_is_logged_when_it_is(engine):
    from tos_bot.core.enums import PlayStatus

    p = _play("AAPL")
    engine.board.replace([p])                                               # found by a quick re-check: no row yet
    assert engine.repo.get_play(p.id) is None
    assert engine.reject_play(p.id)["ok"] and engine.repo.get_play(p.id)["status"] == "REJECTED"

    q = _play("MSFT")
    engine.board.replace([q])
    q.status = PlayStatus.SUBMITTED
    out = engine.reject_play(q.id)
    assert not out["ok"] and "already gone out" in out["reason"] and q.status is PlayStatus.SUBMITTED

    gone = _play("TSLA")                                                    # off the board, its row says it was sent
    engine.repo.record_play(gone)
    engine.repo.set_play_status(gone.id, "SUBMITTED", "autopilot")
    assert not engine.reject_play(gone.id)["ok"]
    assert (engine.repo.get_play(gone.id)["status"], engine.repo.get_play(gone.id)["decided_by"]) == ("SUBMITTED",
                                                                                                    "autopilot")


def test_a_click_is_answered_once_the_order_is_out_and_autopilot_still_reads_the_account_first(engine, monkeypatch):
    """The dashboard's Yes doesn't wait for an account read: the snapshot loop is woken to read and send it.
    Autopilot's approval, on the scan thread, still reads it before it goes on - its next play is sized on it."""
    web, auto = _play("AAA"), _play("BBB")
    engine.board.replace([web, auto])
    monkeypatch.setattr(engine, "assess_play",
                        lambda pid: {"ok": True, "can_execute": True, "reasons": [], "order_plan": {}})
    monkeypatch.setattr(engine, "_chase_check", lambda p, plan, seen: None)
    monkeypatch.setattr(engine.executor, "execute_play", lambda p, account, **kw: {"ok": True, "status": "SUBMITTED"})
    reads, read_now = [], threading.Event()

    def read():                                                             # a slow account read, as IBKR's can be
        reads.append(threading.current_thread().name)
        read_now.wait(10)
        return False

    monkeypatch.setattr(engine, "_refresh_account", read)
    loop = threading.Thread(target=engine._snapshot_loop, name="snapshot-loop", daemon=True)
    loop.start()
    assert _until(lambda: reads == ["snapshot-loop"])                       # its own first pass, held up
    started = time.monotonic()
    assert engine.approve_play(web.id)["ok"]
    assert time.monotonic() - started < 5 and reads == ["snapshot-loop"]    # answered without a read of its own
    read_now.set()                                                          # the first pass finishes...
    assert _until(lambda: reads == ["snapshot-loop"] * 2)                   # ...and the loop, woken, reads again at once

    assert engine.approve_play(auto.id, operator="autopilot")["ok"]
    assert reads[-1] == threading.current_thread().name != "snapshot-loop"   # read before it returned
    engine._stop.set()
    engine._snapshot_wake.set()
    loop.join(5)
    assert not loop.is_alive()


def _until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- confirmations counted on candles
def test_any_scan_on_a_newer_candle_confirms_a_day_play_and_the_setting_turns_it_off(engine, monkeypatch):
    import pandas as pd
    from tos_bot.scanner.scanner import ScanResult

    engine.autopilot.enabled = False
    opened = pd.Timestamp("2026-03-02 10:00", tz="America/New_York")

    def seen(kind, minutes):
        p = _play("AAA")
        p.timeframe = Timeframe.INTRADAY
        p.evidence["bar_at"] = (opened + pd.Timedelta(minutes=minutes)).isoformat()
        return ScanResult(kind=kind, symbols=["AAA"], plays=[p])

    candle = {"at": 0}
    monkeypatch.setattr(engine.scanner, "run_cycle", lambda fast=False: seen("cycle", candle["at"]))
    monkeypatch.setattr(engine.scanner, "run_plays", lambda symbols: seen("plays", candle["at"]))

    def count():
        [p] = engine.board.plays.values()
        return p.confirmations

    engine._run_scan("cycle")
    engine._run_scan("cycle")                                  # the same candle read again
    assert count() == 1
    candle["at"] = 5
    engine._run_scan("plays")                                  # a newer one, found by the quick re-check
    assert count() == 2
    engine._run_scan("cycle")
    assert count() == 2

    engine.set_autopilot(confirm_on_new_candle=False)          # counting scans, as before
    candle["at"] = 10
    engine._run_scan("plays")
    assert count() == 2                                        # a quick re-check isn't a scan confirming it
    engine._run_scan("cycle")
    engine._run_scan("cycle")
    assert count() == 4


def test_the_strategies_panel_says_when_a_day_setup_fires_on_one_candle(engine, monkeypatch):
    key = next(s.key for s in engine.scanner.strategies if s.timeframe is Timeframe.INTRADAY)
    monkeypatch.setattr(engine.replay, "one_candle_setups", lambda: [key])
    engine.autopilot.min_confirmations, engine.autopilot.confirm_on_new_candle = 2, True
    assert "fires on one candle" in engine.replay_state()["proof"][key]
    engine.autopilot.confirm_on_new_candle = False                        # counting scans: the usual proof text
    assert "fires on one candle" not in engine.replay_state()["proof"][key]
    engine.autopilot.confirm_on_new_candle, engine.autopilot.min_confirmations = True, 1
    assert "fires on one candle" not in engine.replay_state()["proof"][key]


def test_every_topic_the_app_publishes_has_a_handler_in_the_dashboard():
    """A topic the dashboard ignores is news nobody sees - a stop for the day, a disarmed engine, a skipped play."""
    app = Path(__file__).resolve().parents[1] / "tos_bot"
    published = {m.group(1) for f in app.rglob("*.py")
                 for m in re.finditer(r'(?:_publish|\.publish)\(\s*"([\w.]+)"', f.read_text(encoding="utf-8"))}
    handled = set(re.findall(r'case "([\w.]+)"', (app / "web" / "js" / "events.js").read_text(encoding="utf-8")))
    assert len(published) > 50                                    # the search found the app's topics
    assert sorted(published - handled) == []


#: every panel, popup or drawer about a stock: the script that draws it and the functions that open it
PRICE_PANELS = {"plays.js": ["drawDetail"], "chart.js": ["openChart"], "movers.js": ["openMoverChart"],
                "signals.js": ["openStock"], "blotter.js": ["renderRecord", "confirmExit", "confirmUntrackedExit"],
                "pairs.js": ["openChart"]}


def test_every_panel_about_a_stock_keeps_its_market_price_fresh():
    """A play's panel and chart, its stock on the Signals page, an open trade's record, an exit's confirmation,
    a mover's chart and a pair's chart each show the stock's price through the one shared helper, which asks
    the route the server has for it."""
    web = Path(__file__).resolve().parents[1] / "tos_bot" / "web" / "js"
    helper = (web / "price.js").read_text(encoding="utf-8")
    assert "/api/price/${" in helper and "export function watchPrice" in helper
    assert '@app.get("/api/price/{symbol}")' in (web.parents[1] / "server" / "app.py").read_text(encoding="utf-8")
    for script, openers in PRICE_PANELS.items():
        source = (web / script).read_text(encoding="utf-8")
        assert 'import { watchPrice } from "./price.js";' in source, script
        for name in openers:
            # the function's body, up to its closing brace at the start of a line
            body = re.search(rf"^(?:export )?(?:async )?function {name}\(.*?^\}}", source, re.S | re.M)
            assert body and "watchPrice(" in body.group(0), f"{script} {name}"


def test_every_name_a_dashboard_script_imports_is_one_the_script_it_names_exports():
    """The scripts load as ES modules: one missing export and the whole dashboard fails to start."""
    web = Path(__file__).resolve().parents[1] / "tos_bot" / "web" / "js"
    scripts = {f.name: f.read_text(encoding="utf-8") for f in web.glob("*.js")}
    exports = {name: set(re.findall(r"^export (?:async )?(?:function|const) ([\w$]+)", src, re.M))
               for name, src in scripts.items()}
    imports = [(name, target, {n.strip() for n in names.split(",") if n.strip()}) for name, src in scripts.items()
               for names, target in re.findall(r'^import \{([^}]*)\} from "\./([\w.]+)";', src, re.M)]
    assert len(imports) > 80                                           # the search found the scripts' imports
    assert [(name, target, sorted(wanted - exports[target])) for name, target, wanted in imports
            if wanted - exports[target]] == []


def test_a_click_that_wakes_the_snapshot_loop_leaves_the_position_check_to_its_usual_turn(engine, monkeypatch):
    from tos_bot.engine import engine as module

    checks, now = [], {"t": 1_000.0}
    monkeypatch.setattr(engine, "_reconcile_open_trades", lambda force=False: checks.append(now["t"]) or [])
    monkeypatch.setattr(module.time, "monotonic", lambda: now["t"])
    engine._reconcile_if_due()                                     # a scheduled pass
    now["t"] += 1.0
    engine._reconcile_if_due()                                     # a pass a click woke a second later
    assert checks == [1_000.0]
    now["t"] += engine.RECONCILE_MIN_GAP_S
    engine._reconcile_if_due()                                     # the next scheduled pass checks again
    assert checks == [1_000.0, 1_000.0 + 1.0 + engine.RECONCILE_MIN_GAP_S]
