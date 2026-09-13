"""Dashboard changes apply live, quitting with positions open, and the check
that removes open-trade records the broker no longer holds."""

from __future__ import annotations

import os
import threading

import pytest

import tos_bot.engine as engine_mod
from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.core.models import Play, Position
from tos_bot.data.market_data import SyntheticProvider
from tos_bot.scanner.filters import TradeFilters


@pytest.fixture
def engine(monkeypatch, tmp_path):
    from tos_bot.persistence.db import DB

    # a database of its own, so open trades left by other tests can't leak in
    DB.init(url=f"sqlite:///{(tmp_path / 'engine.sqlite').as_posix()}")
    DB.create_all()
    monkeypatch.setattr(engine_mod, "init_db", lambda: None)
    monkeypatch.setattr(engine_mod, "_RUNTIME_PATH", tmp_path / "runtime.json")
    monkeypatch.setattr(engine_mod, "port_is_open", lambda *a, **k: False)   # no Gateway in tests
    e = engine_mod.TradingEngine()
    e.md.providers, e.md.cache = [SyntheticProvider(seed=5)], False           # offline
    e._bind_trading_broker()
    yield e
    e.stop()
    DB.engine.dispose()
    DB.init(url=os.environ["DATABASE_URL"])


def _open(engine, symbol="AAPL", venue="paper", side=Side.LONG, qty=5):
    p = Play(symbol=symbol, side=side, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
             timeframe=Timeframe.SWING, entry=100.0, stop=95.0, targets=[110.0])
    p.suggested_qty = qty
    engine.repo.record_play(p)
    return engine.repo.open_trade(p, 100.0, qty, venue)


def _play(symbol="AAPL", side=Side.LONG, strategy="vwap_reclaim"):
    return Play(symbol=symbol, side=side, strategy=strategy, kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.SWING, entry=100.0, stop=95.0, targets=[110.0])


def _closes_fill(engine):
    """Stand-in for the broker: every close fills at 101."""
    closed = []

    def close(tid, reason="manual"):
        t = engine.repo.get_trade(tid)
        closed.append(t["symbol"])
        return {"ok": True, "status": "FILLED",
                "trade": engine.repo.close_trade(tid, exit_price=101.0, exit_reason=reason)}

    engine.executor.close_trade = close
    return closed


# ---------------------------------------------------------------- filters
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
    engine._plays = {p.id: p for p in (_play("AAPL"), _play("TSLA", side=Side.SHORT))}
    r = engine.set_filters(sides=["LONG"])
    assert r["ok"] and r["removed_plays"] == 1 and not r["rescanning"]     # narrowing just trims
    assert engine.scanner.filters is engine.filters and engine.filters.sides == ("LONG",)
    assert engine._load_filters(engine._read_runtime()) == engine.filters  # remembered
    assert not engine._scan_now.is_set()

    r = engine.set_filters(sides=["LONG", "SHORT"])
    assert r["ok"] and r["rescanning"] and engine._scan_now.is_set()        # widening rescans
    assert not engine.set_filters(timeframes=[])["ok"]


# ---------------------------------------------------------------- strategy panel
def test_strategy_panel_switches_setups_live_and_remembers_only_changes(engine):
    key = next(r["key"] for r in engine.strategy_state() if r["enabled"])
    engine._plays = {p.id: p for p in (_play(strategy=key),)}

    r = engine.set_strategy(key, enabled=False)
    assert r["ok"] and key not in {s.key for s in engine.scanner.strategies}
    assert engine._plays == {} and not engine._scan_now.is_set()            # its plays leave the board
    assert engine._read_runtime()["strategies"] == {key: {"enabled": False}}

    r = engine.set_strategy(key, weight=2.5)
    assert r["ok"] and next(x for x in r["strategies"] if x["key"] == key)["weight"] == 2.5
    assert not engine.set_strategy(key, weight=9)["ok"]
    assert not engine.set_strategy(key, weight="heavy")["ok"]
    assert not engine.set_strategy("no_such_setup", enabled=True)["ok"]

    engine.set_strategy(key, enabled=True)                                  # back to the config default
    assert engine.strategy_overrides == {key: {"weight": 2.5}} and engine._scan_now.is_set()
    assert key in {s.key for s in engine.scanner.strategies}
    assert engine.reset_strategies()["ok"] and engine.strategy_overrides == {}


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
    assert "quit" not in engine._read_runtime()
    assert done.wait(3)


def test_while_quitting_nothing_but_exits_can_change(engine):
    tid = _open(engine, "AAPL")
    engine.executor.close_trade = lambda tid, reason="manual": {"ok": True, "status": "WORKING", "order_id": "o1"}
    key = engine.strategy_state()[0]["key"]

    assert engine.begin_quit()["ok"] and engine.quit_state
    assert engine.snapshot()["quit"]["left"] == 1
    for refused in (engine.set_mode("live"), engine.set_filters(sides=["LONG"]), engine.trigger_scan(),
                    engine.reset_paper(), engine.set_strategy(key, enabled=False),
                    engine.set_autopilot(enabled=True), engine.set_broker_setup(paper_platform="ibkr"),
                    engine.approve_play("any")):
        assert not refused["ok"] and refused["reason"].startswith("Quitting")
    assert engine.close_position(tid)["ok"]                                 # exits still go through
    assert engine._read_runtime()["quit"]["mode"] == "paper"                # survives a restart


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

    pv = engine.quit_preview()
    assert not pv["paper"] and pv["left"] == 1 and not pv["resets_simulator"]
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
    engine._MISSING_GRACE_S = 0.0
    engine._refresh_account()
    assert engine._reconcile_open_trades() == []                            # connection just came up
    engine._venue_since -= engine._CONNECTION_SETTLE_S + 1
    assert engine._reconcile_open_trades() == [] and engine.repo.get_trade(tid)   # first miss
    removed = engine._reconcile_open_trades()                               # second miss in a row
    assert [r["id"] for r in removed] == [tid] and engine.repo.get_trade(tid) is None


def test_nothing_is_removed_on_an_answer_that_cant_be_trusted(engine, monkeypatch):
    tid = _open(engine, "AAPL")
    engine._MISSING_GRACE_S, engine._CONNECTION_SETTLE_S = 0.0, 0.0

    engine._refresh_account()
    engine._account_at -= 10                                                # stale account
    assert all(engine._reconcile_open_trades() == [] for _ in range(3))

    engine._refresh_account()
    engine.exit_manager._closing.add(tid)                                   # its exit is going through
    assert all(engine._reconcile_open_trades() == [] for _ in range(3))
    engine.exit_manager._closing.discard(tid)

    monkeypatch.setattr(type(engine.broker), "is_connected", property(lambda self: False))
    assert all(engine._reconcile_open_trades() == [] for _ in range(3))     # disconnected
    assert engine.repo.get_trade(tid)


def test_young_trades_and_held_positions_are_kept(engine):
    young = _open(engine, "AAPL")
    held = _open(engine, "MSFT")
    engine._CONNECTION_SETTLE_S = 0.0
    engine._refresh_account()
    engine._account.positions = [Position(symbol="MSFT", quantity=5, avg_price=100.0)]

    assert all(engine._reconcile_open_trades() == [] for _ in range(3))     # AAPL is inside the grace period
    engine._MISSING_GRACE_S = 0.0
    engine._reconcile_open_trades()
    assert [r["id"] for r in engine._reconcile_open_trades()] == [young]
    assert engine.repo.get_trade(held)


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
