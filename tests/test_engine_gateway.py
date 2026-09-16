"""The engine through IB Gateway's nightly restart: what the dashboard hears, and positions that
aren't trusted until the account has settled again."""

from __future__ import annotations

import time
from types import SimpleNamespace

from test_engine import _connect, _open, engine, gateway, port  # noqa: F401 - pytest fixtures
from tos_bot.engine import engine as engine_module
from tos_bot.engine.reconcile import PositionCheck


def test_the_dashboard_hears_when_the_gateway_drops_comes_back_or_stays_away(engine, port, gateway, monkeypatch):
    _connect(engine, port)
    heard = []
    monkeypatch.setattr(engine_module, "BUS", SimpleNamespace(publish=lambda topic, **p: heard.append(topic)))
    engine._watch_gateway()                                               # up: remembered, nothing to say
    gateway.connected = False                                             # 9 PM: the Gateway restarts
    engine._watch_gateway()
    engine._watch_gateway()
    gateway.connected = True
    engine._watch_gateway()
    assert heard == ["broker.disconnected", "broker.reconnected"]

    heard.clear()
    gateway.connected = False                                             # Sunday: it wants a login
    engine._watch_gateway()
    engine._gateway_down_at -= engine.GATEWAY_DOWN_ALERT_S
    engine._watch_gateway()
    engine._watch_gateway()                                               # said once, not every pass
    assert heard == ["broker.disconnected", "broker.down"]


def test_a_gateway_that_never_came_up_is_only_reported_once_it_has_been_gone_a_while(engine, monkeypatch):
    heard = []
    monkeypatch.setattr(engine_module, "BUS", SimpleNamespace(publish=lambda topic, **p: heard.append(topic)))
    engine._watch_gateway()
    engine._watch_gateway()
    assert heard == []
    engine._gateway_down_at -= engine.GATEWAY_DOWN_ALERT_S
    engine._watch_gateway()
    assert heard == ["broker.down"]


def test_positions_arent_trusted_right_after_the_broker_reconnects(engine):
    tid = _open(engine, "AAPL")
    engine.position_check.GRACE_S = 0.0
    engine._broker_since -= PositionCheck.SETTLE_S + 1
    engine.broker.connected_since = time.monotonic()                     # it has only just come back
    engine._refresh_account()
    assert all(engine._reconcile_open_trades() == [] for _ in range(3)) and engine.repo.get_trade(tid)
