"""Same-machine guard: on every request, and again (header included) on the secrets endpoints."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from tos_bot.server import security

OK = {"X-ATB-Request": "1"}


def _app() -> FastAPI:
    app = FastAPI()

    @app.post("/secret", dependencies=[Depends(security.require_local)])
    def secret():
        return {"ok": True}

    return app


@pytest.fixture
def client(monkeypatch):
    # TestClient reports client "testclient" and Host "testserver"
    monkeypatch.setattr(security, "ALLOWED_CLIENTS", security.ALLOWED_CLIENTS | {"testclient"})
    monkeypatch.setattr(security, "ALLOWED_HOSTS", security.ALLOWED_HOSTS | {"testserver"})
    return TestClient(_app())


def test_dashboard_request_is_allowed(client):
    assert client.post("/secret", headers=OK).status_code == 200


def test_missing_header_is_refused(client):
    assert client.post("/secret").status_code == 403


def test_foreign_origin_is_refused(client):
    assert client.post("/secret", headers={**OK, "Origin": "https://evil.example"}).status_code == 403


def test_local_origin_is_allowed(client):
    assert client.post("/secret", headers={**OK, "Origin": "http://127.0.0.1:8787"}).status_code == 200


def test_rebound_host_is_refused(client):
    assert client.post("/secret", headers={**OK, "Host": "evil.example:8787"}).status_code == 403


def test_remote_client_is_refused(monkeypatch):
    monkeypatch.setattr(security, "ALLOWED_HOSTS", security.ALLOWED_HOSTS | {"testserver"})
    assert TestClient(_app()).post("/secret", headers=OK).status_code == 403


def test_the_share_count_fix_is_same_machine_only():
    # it books P/L and can send a market exit; the guard answers before the handler needs an engine
    from tos_bot.server.app import create_app

    client = TestClient(create_app(lambda settings: None))
    assert client.get("/api/positions/mismatch/AAA", headers=OK).status_code == 403
    assert client.post("/api/positions/mismatch/AAA/fix", json={"action": "match"}, headers=OK).status_code == 403


# ---- every request: the app's middleware ------------------------------------------------------ #
class _Engine:
    """Records what reached it: a refused request must be answered before the engine is asked."""

    def __init__(self):
        self.calls = []

    def _ok(self, *call):
        self.calls.append(call)
        return {"ok": True}

    def approve_play(self, play_id):
        return self._ok("approve", play_id)

    def close_untracked(self, symbol):
        return self._ok("close_untracked", symbol)

    def set_mode(self, mode):
        return self._ok("mode", mode)

    def snapshot(self):
        return self._ok("snapshot")

    def price_of(self, symbol):
        return self._ok("price", symbol)

    def current_plays(self):
        self.calls.append(("plays",))
        return []


def _dashboard(monkeypatch, *, local_client=True):
    """The real app on a stub engine, TestClient's host allowed as this computer (and its client too,
    unless the test wants a remote one)."""
    from tos_bot.server.app import create_app

    if local_client:
        monkeypatch.setattr(security, "ALLOWED_CLIENTS", security.ALLOWED_CLIENTS | {"testclient"})
    monkeypatch.setattr(security, "ALLOWED_HOSTS", security.ALLOWED_HOSTS | {"testserver"})
    engine = _Engine()
    app = create_app(lambda settings: None)
    app.state.engine = engine
    return TestClient(app), engine


def test_the_dashboards_own_requests_get_through(monkeypatch):
    client, engine = _dashboard(monkeypatch)
    assert client.post("/api/plays/P01/approve", headers=OK).status_code == 200
    assert client.get("/api/state").status_code == 200                 # a read needs no header
    assert client.get("/api/price/AAA", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert client.get("/").status_code == 200
    assert engine.calls == [("approve", "P01"), ("snapshot",), ("price", "AAA")]


def test_a_cross_site_form_post_is_refused(monkeypatch):
    # a plain HTML form on another website: no custom header, a foreign Origin
    client, engine = _dashboard(monkeypatch)
    evil = {"Origin": "https://evil.example"}
    answer = client.post("/api/plays/P01/approve", data={"x": "1"}, headers=evil)
    assert answer.status_code == 403
    assert isinstance(answer.json()["detail"], str)         # the dashboard shows the reason
    # a foreign Origin is refused even with the header
    assert client.post("/api/plays/P01/approve", headers={**OK, **evil}).status_code == 403
    assert engine.calls == []


def test_a_post_without_the_dashboard_header_is_refused(monkeypatch):
    client, engine = _dashboard(monkeypatch)
    assert client.post("/api/positions/untracked/AAA/close").status_code == 403
    assert engine.calls == []


def test_a_rebound_host_is_refused_on_every_route(monkeypatch):
    client, engine = _dashboard(monkeypatch)
    rebound = {"Host": "rebind.evil:8787"}
    assert client.post("/api/mode", json={"mode": "live"}, headers={**OK, **rebound}).status_code == 403
    assert client.get("/api/state", headers=rebound).status_code == 403
    assert engine.calls == []


def test_another_sites_page_cant_load_the_api(monkeypatch):
    # an <img> or <script> on another website sends no Origin, but the browser marks it cross-site
    client, engine = _dashboard(monkeypatch)
    cross = {"Sec-Fetch-Site": "cross-site"}
    assert client.get("/api/price/AAA", headers=cross).status_code == 403
    assert engine.calls == []
    # a link from another website still opens the dashboard itself
    assert client.get("/", headers=cross).status_code == 200


def test_a_remote_client_is_refused_on_every_route(monkeypatch):
    client, engine = _dashboard(monkeypatch, local_client=False)
    assert client.get("/api/state").status_code == 403
    assert client.get("/").status_code == 403
    assert engine.calls == []


# ---- the live feed: the WebSocket, which the HTTP middleware never sees ------------------------ #
def test_the_dashboards_own_live_feed_opens(monkeypatch):
    client, engine = _dashboard(monkeypatch)
    with client.websocket_connect("/ws", headers={"Origin": "http://127.0.0.1:8787"}) as ws:
        assert ws.receive_json()["topic"] == "hello"
        assert ws.receive_json()["topic"] == "plays.updated"
    assert engine.calls == [("snapshot",), ("plays",)]


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.example"},      # another website's page
                                     {"Host": "rebind.evil:8787"}])           # a rebound domain
def test_a_foreign_live_feed_is_refused_before_the_snapshot(monkeypatch, headers):
    client, engine = _dashboard(monkeypatch)
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/ws", headers=headers):
            pass
    assert refused.value.code == 1008
    assert engine.calls == []


def test_a_remote_live_feed_is_refused(monkeypatch):
    client, engine = _dashboard(monkeypatch, local_client=False)
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/ws"):
            pass
    assert refused.value.code == 1008 and engine.calls == []


def test_an_ipv6_loopback_request_is_allowed():
    host = {"host": "[::1]:8787"}
    assert security.refusal("::1", host, "POST", "/api/mode") == "Missing dashboard request header."
    assert security.refusal("::1", {**host, "x-atb-request": "1"}, "POST", "/api/mode") is None
