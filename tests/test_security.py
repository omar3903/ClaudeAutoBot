"""Same-machine guard on the secrets / sign-in endpoints."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
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
