"""End-to-end: dashboard API drives a full scan -> approve -> close on paper."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.slow


@pytest.fixture
def client(monkeypatch):
    # The flow needs a tradable session. Pin the clock to mid regular-hours so
    # this doesn't fail every night and weekend (and nothing auto-flattens).
    from tos_bot.util import clock
    monkeypatch.setattr(clock, "current_session", lambda ts=None: clock.Session.REGULAR)
    monkeypatch.setattr(clock, "is_market_open", lambda ts=None: True)
    monkeypatch.setattr(clock, "minutes_to_close", lambda ts=None: 240.0)
    monkeypatch.setenv("SCANNER_UNIVERSE", "nasdaq100")
    monkeypatch.setenv("SCANNER_MAX_SYMBOLS", "24")
    monkeypatch.setenv("SCANNER_INTERVAL_SECONDS", "9999")
    from tos_bot.config import reload_settings
    reload_settings()
    from fastapi.testclient import TestClient
    from tos_bot.server.app import create_app
    with TestClient(create_app()) as c:
        yield c


def test_scan_assess_approve_close(client):
    st = client.get("/api/state").json()
    assert st["broker"] == "paper" and st["connected"] and st["armed"]

    client.post("/api/scan/now")
    plays = []
    for _ in range(45):
        time.sleep(1)
        plays = client.get("/api/plays").json()["plays"]
        if plays:
            break
    assert plays, "scanner produced no plays"

    # find an executable one
    chosen = None
    for p in plays:
        a = client.post(f"/api/plays/{p['id']}/assess").json()
        assert a["ok"]
        if a["can_execute"]:
            chosen = p
            break
    assert chosen, "no executable play"

    r = client.post(f"/api/plays/{chosen['id']}/approve").json()
    assert r["ok"] and r["status"] == "FILLED"

    open_trades = client.get("/api/trades?status=OPEN").json()["trades"]
    assert open_trades
    tid = open_trades[0]["id"]

    cr = client.post(f"/api/trades/{tid}/close").json()
    assert cr["ok"]
    pnl = client.get("/api/pnl").json()
    assert pnl["n_closed"] >= 1

    hist = [t for t in client.get("/api/trades?limit=50").json()["trades"] if t["status"] == "CLOSED"]
    assert hist and hist[0]["realized_pl"] is not None
