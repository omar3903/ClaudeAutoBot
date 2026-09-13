"""Engine routing: platform switches, the open-position guard, the sector filter."""

from __future__ import annotations

import pytest

import tos_bot.engine as engine_mod


@pytest.fixture
def engine(monkeypatch, tmp_path):
    monkeypatch.setattr(engine_mod, "_RUNTIME_PATH", tmp_path / "runtime.json")
    monkeypatch.setattr(engine_mod, "port_is_open", lambda *a, **k: False)   # no Gateway in tests
    e = engine_mod.TradingEngine()
    e._bind_trading_broker()
    yield e
    e.stop()


def test_starts_on_the_simulator(engine):
    snap = engine.snapshot()
    assert snap["venue"]["trading_on"] == "paper" and engine.broker.name == "paper"
    assert snap["connection"]["label"] == "Simulator"


def test_ibkr_paper_platform_falls_back_to_simulator_while_gateway_is_down(engine):
    r = engine.set_broker_setup(paper_platform="ibkr")
    assert r["ok"] and engine.paper_platform == "ibkr"
    assert engine._trading_venue == "paper"
    assert any("IB Gateway" in b for b in engine._venue_blockers)
    conn = engine.snapshot()["connection"]
    assert conn["cls"] == "bad" and conn["action"] == "connections"


def test_live_switch_is_refused_when_the_broker_is_unreachable(engine):
    r = engine.set_mode("live")
    assert not r["ok"] and r["blockers"] and engine.mode == "paper"


def test_switch_is_blocked_while_positions_are_open_on_the_current_venue(engine, monkeypatch):
    monkeypatch.setattr(engine.repo, "open_trades",
                        lambda: [{"id": "trd_1", "symbol": "AAPL", "broker": "paper"}])
    r = engine.set_mode("live")
    assert not r["ok"] and "AAPL" in r["reason"] and engine.mode == "paper"
    # a change that keeps orders on the simulator is still fine
    assert engine.set_broker_setup(paper_platform="schwab")["ok"]


def test_invalid_platform_is_rejected(engine):
    assert not engine.set_broker_setup(paper_platform="tda")["ok"]
    assert not engine.set_broker_setup(live_broker="crypto")["ok"]


def test_sector_filter_is_normalised_persisted_and_shared_with_the_scanner(engine):
    r = engine.set_sectors(["Energy", "Consumer Cyclical"])
    assert r["ok"] and engine.sectors == ["Consumer Discretionary", "Energy"]
    assert engine.scanner.sectors_allowed == engine.sectors
    assert engine._read_runtime()["sectors"] == engine.sectors
    assert engine.set_sectors([])["sectors"] == []


def test_bad_secret_input_is_rejected(engine):
    r = engine.save_secrets({"IBKR_PAPER_PORT": "nope"})
    assert not r["ok"] and "whole number" in r["reason"]
