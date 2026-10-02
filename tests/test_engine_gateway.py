"""The engine through IB Gateway's nightly restart: what the dashboard hears, and positions that
aren't trusted until the account has settled again."""

from __future__ import annotations

import time
from types import SimpleNamespace

from test_engine import _connect, _open, engine, gateway, port  # noqa: F401 - pytest fixtures
from autotradebot.engine import engine as engine_module
from autotradebot.engine.reconcile import PositionCheck


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


def test_refresh_connects_at_once_when_the_gateway_came_up_after_the_app(engine, port, gateway, monkeypatch):
    heard = []
    monkeypatch.setattr(engine_module, "BUS", SimpleNamespace(publish=lambda topic, **p: heard.append(topic)))
    out = engine.refresh_account_now()                        # the simulator answers, and IBKR's absence is said
    assert out["ok"] and out["warn"] and not out["connected"] and "isn't reachable yet" in out["note"]

    port["open"] = True                                                   # the user starts IB Gateway...
    engine._connect_retry_at = time.monotonic() + 60.0                    # ...and the background retry is a minute away
    out = engine.refresh_account_now()
    assert out["ok"] and out["connected"] and out["note"].startswith("Connected to")
    assert "broker.connected" in heard and engine.connections.connected
    assert engine.refresh_account_now()["note"] == "Account, positions and orders re-read."


def test_refresh_fetches_the_prices_too_and_says_how_many(engine, port, monkeypatch):
    _connect(engine, port)
    asked = []
    monkeypatch.setattr(engine, "refresh_prices", lambda: asked.append(True) or 7)
    out = engine.refresh_account_now()
    assert out["ok"] and asked == [True] and out["note"] == "Account, positions and orders re-read, and 7 prices fetched."


def test_refresh_never_runs_a_second_order_sync_beside_the_loops(engine, monkeypatch):
    import threading

    ex = engine.executor
    passes, mid_pass, finish = [], threading.Event(), threading.Event()
    expire = ex.expire_entries

    def expire_entries(*a, **k):                                          # every order sync runs it
        passes.append(threading.current_thread().name)
        if threading.current_thread().name == "sync-loop":
            mid_pass.set()
            finish.wait(5)                                                # the loop's pass is still going
        return expire(*a, **k)

    monkeypatch.setattr(ex, "expire_entries", expire_entries)
    loop = threading.Thread(target=ex.sync_open_orders, name="sync-loop", daemon=True)
    loop.start()
    assert mid_pass.wait(2)
    began = time.monotonic()
    out = engine.refresh_account_now()                                    # the Refresh button, meanwhile
    assert out["ok"] and time.monotonic() - began < 2 and passes == ["sync-loop"]   # no second pass, no wait
    finish.set()
    loop.join(5)
    assert engine.refresh_account_now()["ok"] and passes == ["sync-loop", threading.current_thread().name]


def test_refresh_says_when_ibkr_doesnt_answer(engine, port, gateway, monkeypatch):
    from autotradebot.brokers.base import BrokerError

    _connect(engine, port)
    assert engine.refresh_account_now()["ok"]

    def slow():
        raise BrokerError("IBKR didn't answer for the account values in time (TimeoutError)")

    monkeypatch.setattr(gateway, "get_account", slow)
    out = engine.refresh_account_now()
    assert not out["ok"] and out["connected"] and "didn't answer" in out["reason"]
    assert out["state"]["account"] is not None                            # the last snapshot is kept
