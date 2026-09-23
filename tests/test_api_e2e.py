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
    from tos_bot.server import security
    from tos_bot.server.app import create_app

    # every request must come from the dashboard on this computer: TestClient reports client
    # "testclient" and Host "testserver", and sends the dashboard's header as its post() does
    monkeypatch.setattr(security, "ALLOWED_CLIENTS", security.ALLOWED_CLIENTS | {"testclient"})
    monkeypatch.setattr(security, "ALLOWED_HOSTS", security.ALLOWED_HOSTS | {"testserver"})
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"paper_platform": "simulator"}), encoding="utf-8")
    gateway = fakes.FakeGateway(fakes.SYMBOLS + [BENCHMARK])

    def engine(settings):
        return TradingEngine(settings, data_dir=tmp_path / "data", runtime_path=runtime,
                             broker_factory=fakes.broker_factory(gateway), port_check=lambda host, port: True,
                             listings=fakes.FakeListings(fakes.SYMBOLS), fundamentals=fakes.NoFundamentals())

    with TestClient(create_app(engine), headers={"X-ATB-Request": "1"}) as c:
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
    plays = client.get("/api/plays").json()["plays"]
    if plays:
        chart = client.get(f"/api/plays/{plays[0]['id']}/chart").json()
        assert chart["ok"] and chart["candles"] and {r["key"] for r in chart["routes"]} >= {"stop"}
    assert client.get("/api/plays/missing/chart").json()["ok"] is False
    # the listing, like the board's push, carries what the table shows; the hover loads a play whole
    assert plays and all("explanation" not in p and "spark" not in p["evidence"] for p in plays)
    whole = client.get(f"/api/plays/{plays[0]['id']}").json()
    assert whole["id"] == plays[0]["id"] and whole["explanation"] and "autopilot" in whole and "record" in whole
    gone = client.get("/api/plays/missing")
    assert gone.status_code == 404 and "expired" in gone.json()["detail"]
    assert "content-encoding" not in gone.headers                        # a small answer isn't worth compressing
    listing = client.get("/api/plays", params={"full": 1}, headers={"Accept-Encoding": "gzip"})
    assert listing.headers["content-encoding"] == "gzip" and listing.json()["plays"][0]["explanation"]
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["topic"] == "hello"
        pushed = ws.receive_json()
    assert pushed["topic"] == "plays.updated" and pushed["payload"]["plays"]
    assert all("explanation" not in p for p in pushed["payload"]["plays"])
    for play in plays:
        assessed = client.post(f"/api/plays/{play['id']}/assess").json()
        if not assessed.get("can_execute"):
            continue
        approved = client.post(f"/api/plays/{play['id']}/approve").json()
        if approved.get("status") == "FILLED":
            trade_id = approved["trade_id"]
            # the reply comes once the order is out, and the board already says so
            listed = {p["id"]: p for p in client.get("/api/plays").json()["plays"]}
            assert listed[play["id"]]["status"] == "FILLED" and listed[play["id"]]["trade_id"] == trade_id
            break
    assert trade_id, "no play could be executed"

    open_rows = {t["id"]: t for t in client.get("/api/trades?status=OPEN").json()["trades"]}
    # the simulator rests no orders at the broker: the Open positions tab says the app watches the price
    assert open_rows[trade_id]["protection"] == {"native": False, "stop": None, "target": None}
    orders = client.get("/api/orders?fresh=true").json()
    assert orders["ok"] and isinstance(orders["orders"], list)
    assert client.post(f"/api/trades/{trade_id}/close").json()["ok"]
    assert client.get("/api/pnl").json()["n_closed"] >= 1
    closed = [t for t in client.get("/api/trades?limit=50").json()["trades"] if t["status"] == "CLOSED"]
    assert closed and closed[0]["realized_pl"] is not None


def test_settings_round_trip_and_the_dashboard_loads(client):
    r = client.post("/api/settings", json={"cycle_minutes": 4, "hot_list_size": 30}).json()
    assert r["ok"] and r["scan"]["settings"]["cycle_minutes"] == 4
    wide = client.post("/api/settings", json={"wide_minutes": 45, "wide_stocks": 500, "movers": 4,
                                              "yesterday_movers": 6}).json()["scan"]["settings"]
    assert (wide["wide_minutes"], wide["wide_stocks"], wide["movers"], wide["yesterday_movers"]) == (45, 500, 4, 6)
    assert client.post("/api/settings", json={"premarket_time": "10:00"}).status_code == 400
    assert client.get("/").status_code == 200
    main = client.get("/static/js/main.js")
    assert "javascript" in main.headers["content-type"]
    assert main.headers["cache-control"] == "no-cache"             # an update never mixes with cached modules
    assert client.get("/").headers["cache-control"] == "no-cache"
    journal = client.get("/api/journal").json()
    assert journal["review_at"] and isinstance(journal["days"], list)
    assert client.get("/api/journal/2020-01-02").status_code == 404
    assert client.get("/api/journal/someday").status_code == 400
    replay = client.get("/api/replay").json()
    assert replay["defaults"]["sessions"] == 60 and "evidence" in replay and "learned_skips" in replay
    assert client.get("/api/replay/history").json()["runs"] == []
    pairs = client.get("/api/pairs").json()
    assert pairs["enabled"] and isinstance(pairs["watch"], list) and isinstance(pairs["trades"], list)
    assert client.post("/api/pairs/enter", json={"pair": "NOPE/PAIR"}).status_code == 400
    assert client.get("/api/pairs/chart", params={"pair": "NOPE/PAIR"}).json()["ok"] is False
    price = client.get("/api/price/t01").json()                        # a panel's price line
    assert price["ok"] and price["symbol"] == "T01" and price["price"] > 0 and price["at"] and price["session"]


def test_an_open_tab_is_told_when_the_dashboards_files_have_changed(client, monkeypatch, tmp_path):
    import importlib

    server = importlib.import_module("tos_bot.server.app")        # the module, not the app the package exports

    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
    assert hello["topic"] == "hello" and hello["payload"]["web_build"] == server.web_build()
    assert "mode" in hello["payload"]                                  # the snapshot is still all there
    account = (hello["payload"]["venue"]["ibkr_session"] or {}).get("account")
    assert account is None or account.startswith("…")                 # never the whole account id

    (tmp_path / "web" / "js").mkdir(parents=True)
    (tmp_path / "web" / "js" / "main.js").write_text("// one", encoding="utf-8")
    monkeypatch.setattr(server, "WEB_DIR", tmp_path / "web")
    before = server.web_build()
    assert before == server.web_build()                                # the same files, the same stamp
    (tmp_path / "web" / "js" / "main.js").write_text("// another script", encoding="utf-8")
    assert server.web_build() != before                                # a tab that started on `before` reloads
