"""The engine: where orders go, dashboard changes applying live, scans on
schedule, quitting with positions open, trading capital, and the check that
removes open-trade records the broker no longer holds."""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import threading
import time

import pytest

import fakes
from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.core.models import Account, OrderResult, Play, Position
from tos_bot.engine import TradingEngine
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
    out = engine.set_autopilot(enabled=True, trade_types=["INTRADAY", "SWING"])
    assert out["ok"] and out["autopilot"]["enabled"] is True
    assert set(out["autopilot"]["trade_types"]) == {"INTRADAY", "SWING"}
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


def test_autopilot_takes_the_trade_types_the_filters_switch_on(engine):
    ap = engine.autopilot
    ap.enabled, ap.trade_types = True, ["INTRADAY"]                        # its own setting said day trades only
    assert engine.set_filters(timeframes=["SWING"])["ok"]                  # the filters say swing trades only
    day = Play(symbol="MSFT", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
               timeframe=Timeframe.INTRADAY, entry=100.0, stop=95.0, targets=[110.0])
    assert ap.play_types() == ["SWING"] and ap.status()["trade_types"] == ["SWING"]
    assert "switched off" not in (ap._pre_gate(_play("AAPL"), 50_000) or "")
    assert "day trades are switched off" in ap._pre_gate(day, 50_000)
    assert not ap.day_mode_active(market_open=True)
    assert engine.set_filters(timeframes=["INTRADAY", "SWING"])["ok"]
    assert ap.play_types() == ["INTRADAY", "SWING"] and ap.day_mode_active(market_open=True)
