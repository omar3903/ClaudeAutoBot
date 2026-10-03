"""The engine through IB Gateway's nightly restart: what the dashboard hears, and positions that
aren't trusted until the account has settled again."""

from __future__ import annotations

import datetime as dt
import time
from types import SimpleNamespace

import pytest

from test_engine import _connect, _on_ibkr, _open, engine, gateway, port  # noqa: F401 - pytest fixtures
from autotradebot.config import ScannerCfg
from autotradebot.engine import engine as engine_module
from autotradebot.engine.reconcile import PositionCheck
from autotradebot.util import clock


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


def _ny(day, hour, minute):
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=clock.NY)


def test_a_gateway_still_down_at_the_morning_alert_time_is_said_once_each_trading_day(engine, monkeypatch):
    heard = []
    publish = lambda topic, **p: heard.append((topic, p["note"]))           # noqa: E731
    monkeypatch.setattr(engine_module, "BUS", SimpleNamespace(publish=publish))
    wed, thu, fri, sat = (dt.date(2026, 9, 30) + dt.timedelta(days=n) for n in range(4))
    engine._watch_gateway(at=_ny(wed, 21, 0))                             # the nightly restart never came back
    engine._watch_gateway(at=_ny(thu, 7, 44))
    assert heard == []
    engine._watch_gateway(at=_ny(thu, 7, 45))
    engine._watch_gateway(at=_ny(thu, 8, 0))                              # once a day
    [(topic, note)] = heard
    assert topic == "broker.down" and "still isn't connected at 07:45 ET" in note and "since Wed 21:00" in note
    assert "The full scan is due at 08:30." in note
    engine._watch_gateway(at=_ny(sat, 9, 0))                              # no trading on Saturday
    assert len(heard) == 1
    engine._watch_gateway(at=_ny(fri, 9, 45))                             # the next trading day: said again
    assert len(heard) == 2 and "full scan" not in heard[1][1]             # (past the scan: no word of it)


def test_the_morning_alert_leaves_a_later_drop_to_the_ten_minute_alert_and_can_be_switched_off(
        engine, port, gateway, monkeypatch):
    _connect(engine, port)
    heard = []
    monkeypatch.setattr(engine_module, "BUS", SimpleNamespace(publish=lambda topic, **p: heard.append(topic)))
    thu = dt.date(2026, 10, 1)
    engine._watch_gateway(at=_ny(thu, 7, 30))
    engine._watch_gateway(at=_ny(thu, 7, 45))                             # connected at the time: nothing to say
    gateway.connected = False
    engine._watch_gateway(at=_ny(thu, 10, 0))                             # a drop mid-morning...
    engine._watch_gateway(at=_ny(thu, 10, 1))
    assert heard == ["broker.disconnected"]                               # ...isn't "still gone at 07:45"

    monkeypatch.setattr(engine.settings.config.scanner, "gateway_alert_time", "")
    engine._watch_gateway(at=_ny(dt.date(2026, 10, 2), 0, 0))
    engine._gateway_down_since = _ny(thu, 21, 0)
    engine._watch_gateway(at=_ny(dt.date(2026, 10, 2), 8, 0))             # off: never said
    assert heard == ["broker.disconnected"]
    assert ScannerCfg(gateway_alert_time=" 08:05 ").gateway_alert_time == "08:05"
    with pytest.raises(ValueError):
        ScannerCfg(gateway_alert_time="quarter to eight")                 # refused, never taken for off


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

    _on_ibkr(engine)
    _connect(engine, port)
    assert engine.refresh_account_now()["ok"]

    def slow():
        raise BrokerError("IBKR didn't answer for the account values in time (TimeoutError)")

    monkeypatch.setattr(gateway, "get_account", slow)
    out = engine.refresh_account_now()
    assert not out["ok"] and out["connected"] and "didn't answer" in out["reason"]
    assert out["state"]["account"] is not None                            # the last snapshot is kept
