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
from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.core.models import Account, OrderResult, Play, Position
from tos_bot.engine import TradingEngine
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
    assert engine.journal_review(today)["session"] == today.isoformat()
    assert engine.journal_state()["days"][0]["session"] == today.isoformat()


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
