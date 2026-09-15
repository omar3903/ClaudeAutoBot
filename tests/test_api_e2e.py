"""End to end: the dashboard API on a synthetic IB Gateway - the full scan and a
cycle, the watchlist and settings, then assess -> approve -> close on the simulator."""

from __future__ import annotations

import json
import time

import pytest

import fakes

pytestmark = pytest.mark.slow


@pytest.fixture
def client(monkeypatch, tmp_path):
    # the flow needs a tradable session: pin the clock to mid regular hours so
    # this doesn't fail every night and weekend (and nothing auto-flattens)
    from tos_bot.util import clock
    monkeypatch.setattr(clock, "current_session", lambda ts=None: clock.Session.REGULAR)
    monkeypatch.setattr(clock, "is_market_open", lambda ts=None: True)
    monkeypatch.setattr(clock, "minutes_to_close", lambda ts=None: 240.0)

    from fastapi.testclient import TestClient
    from tos_bot.engine import TradingEngine
    from tos_bot.scanner.scanner import BENCHMARK
    from tos_bot.server.app import create_app

    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"paper_platform": "simulator"}), encoding="utf-8")
    gateway = fakes.FakeGateway(fakes.SYMBOLS + [BENCHMARK])

    def engine(settings):
        return TradingEngine(settings, data_dir=tmp_path / "data", runtime_path=runtime,
                             broker_factory=fakes.broker_factory(gateway), port_check=lambda host, port: True,
                             listings=fakes.FakeListings(fakes.SYMBOLS), fundamentals=fakes.NoFundamentals())

    with TestClient(create_app(engine)) as c:
        yield c


def _wait_for(check, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.5)
    return None


def test_scan_watchlist_approve_close(client):
    state = client.get("/api/state").json()
    assert state["connected"] and state["armed"] and state["data"]["connected"]
    assert state["venue"]["trading_on"] == "paper"
    assert client.post("/api/scan", json={"kind": "cycle"}).json()["ok"]

    assert _wait_for(lambda: client.get("/api/settings").json()["last_cycle"]), "no cycle ran after the full scan"
    watchlist = client.get("/api/watchlist").json()["watchlist"]
    assert watchlist["hot"] and watchlist["sectors"]

    trade_id = None
    for play in client.get("/api/plays").json()["plays"]:
        assessed = client.post(f"/api/plays/{play['id']}/assess").json()
        if not assessed.get("can_execute"):
            continue
        approved = client.post(f"/api/plays/{play['id']}/approve").json()
        if approved.get("status") == "FILLED":
            trade_id = approved["trade_id"]
            break
    assert trade_id, "no play could be executed"

    assert trade_id in [t["id"] for t in client.get("/api/trades?status=OPEN").json()["trades"]]
    orders = client.get("/api/orders?fresh=true").json()
    assert orders["ok"] and isinstance(orders["orders"], list)
    assert client.post(f"/api/trades/{trade_id}/close").json()["ok"]
    assert client.get("/api/pnl").json()["n_closed"] >= 1
    closed = [t for t in client.get("/api/trades?limit=50").json()["trades"] if t["status"] == "CLOSED"]
    assert closed and closed[0]["realized_pl"] is not None


def test_settings_round_trip_and_the_dashboard_loads(client):
    r = client.post("/api/settings", json={"cycle_minutes": 20, "hot_list_size": 30}).json()
    assert r["ok"] and r["scan"]["settings"]["cycle_minutes"] == 20
    assert client.post("/api/settings", json={"premarket_time": "10:00"}).status_code == 400
    assert client.get("/").status_code == 200
    main = client.get("/static/js/main.js")
    assert "javascript" in main.headers["content-type"]
    assert main.headers["cache-control"] == "no-cache"             # an update never mixes with cached modules
    assert client.get("/").headers["cache-control"] == "no-cache"
