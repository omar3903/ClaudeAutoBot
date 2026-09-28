"""Running around the clock: IB Gateway's nightly restart, IBKR's maintenance, orders and replays
that outlast a dropped connection, and caches that don't grow for as long as the app runs."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import fakes
from test_ibkr_adapter import FakeSession
from autotradebot.brokers import ibkr_adapter as mod
from autotradebot.brokers.base import AuthError
from autotradebot.core.enums import PlayStatus, Side, StrategyKind, Timeframe
from autotradebot.core.models import OrderResult, Play
from autotradebot.data.bars import DailyBarStore
from autotradebot.data.market_data import MarketData
from autotradebot.execution.executor import Executor, _Pending
from autotradebot.research import history as history_module
from autotradebot.research.history import IntradayHistory

SILENT = SimpleNamespace(publish=lambda *a, **k: None)


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setattr(mod, "port_is_open", lambda *a, **k: True)

    async def _fast(*_a):
        return None

    monkeypatch.setattr(mod, "_sleep", _fast)
    b = mod.IbkrBroker(port=4002, mode="paper", session_factory=FakeSession)
    b.connect()
    return b


# ---------------------------------------------------------------- the adapter
def test_when_the_gateway_loses_ibkrs_servers_the_same_connection_comes_back(broker, monkeypatch):
    monkeypatch.setattr(broker, "_start_reconnect", lambda: None)
    broker.connected_since = 0.0
    broker._on_error(-1, 1100, "Connectivity between IBKR and Trader Workstation has been lost.")
    status = broker.session_status()
    assert not broker.is_connected and status["servers_lost"] and "lost IBKR's servers" in status["message"]
    broker._on_error(-1, 2104, "Market data farm connection is OK:usfarm")
    assert not broker.is_connected                                       # after 1100 only IBKR's all-clear counts
    broker._on_error(-1, 1102, "Connectivity between IBKR and Trader Workstation has been restored - data maintained.")
    assert broker.is_connected and broker.connected_since > 0 and not broker.session_status()["servers_lost"]


def test_after_a_2110_outage_a_data_farm_reporting_ok_is_the_all_clear(broker, monkeypatch):
    monkeypatch.setattr(broker, "_start_reconnect", lambda: None)
    broker._on_error(-1, 2110, "Connectivity between Trader Workstation and server is broken.")
    assert not broker.is_connected
    broker._on_error(-1, 2106, "HMDS data farm connection is OK:ushmds")
    assert broker.is_connected


def test_a_gateway_that_hasnt_loaded_the_account_yet_isnt_trusted(monkeypatch):
    monkeypatch.setattr(mod, "port_is_open", lambda *a, **k: True)
    session = FakeSession()
    session.ib.account_values = []                                       # still starting up
    stopped = []
    monkeypatch.setattr(session, "stop", lambda: stopped.append(True))
    b = mod.IbkrBroker(port=4002, mode="paper", session_factory=lambda: session)
    with pytest.raises(AuthError, match="loading the account"):
        b.connect()
    assert not b.is_connected and not session.ib.isConnected() and stopped   # and no event loop left running


def test_the_reconnect_waits_out_a_server_outage_then_starts_afresh(broker, monkeypatch):
    broker._connected, broker._reconnecting = False, True
    broker._servers_lost_at, broker._servers_lost_code = time.monotonic(), 1100
    naps = []

    def nap(seconds):
        naps.append(seconds)
        if len(naps) == 3:
            broker._servers_lost_at -= broker.SERVER_OUTAGE_RECONNECT_S          # the outage drags on
    monkeypatch.setattr(mod.time, "sleep", nap)
    broker._reconnect_loop()
    assert naps == [5, 5, 5] and broker.is_connected and broker._servers_lost_at is None and not broker._reconnecting


def test_a_dropped_gateway_is_retried_quickly_at_first(broker, monkeypatch):
    broker._connected, broker._reconnecting = False, True
    broker._session.ib.disconnect()                                      # the Gateway is restarting
    answers = iter([False, False, False, True])
    monkeypatch.setattr(mod, "port_is_open", lambda *a, **k: next(answers))
    naps = []
    monkeypatch.setattr(mod.time, "sleep", naps.append)
    broker._reconnect_loop()
    assert naps == [5, 10, 15] and broker.is_connected and broker.down_since is None


# ---------------------------------------------------------------- orders
class _Reloading:
    """A broker that has just reconnected and doesn't list an order yet."""

    name, paper, supports_bracket_native = "ibkr", False, False

    def __init__(self):
        self.connected_since = time.monotonic()

    def get_order(self, order_id):
        return OrderResult(order_id=order_id, status="UNKNOWN", symbol="?", submitted_qty=0.0)

    def list_orders(self, status=None):
        return []


def test_an_order_isnt_given_up_while_the_broker_reloads_its_orders():
    executor = Executor(_Reloading(), SimpleNamespace(), SimpleNamespace(), bus=SILENT, venue="ibkr-paper")
    play = Play(symbol="AAA", side=Side.LONG, strategy="x", kind=StrategyKind.TECHNICAL, timeframe=Timeframe.INTRADAY,
                entry=10.0, stop=9.0, targets=[12.0])
    executor._pending["o1"] = _Pending("o1", play, "entry", qty=10)
    for _ in range(10):
        executor.sync_open_orders()
    assert "o1" in executor._pending
    executor.broker.connected_since -= Executor.RESYNC_GRACE_S + 1
    for _ in range(Executor.LOST_AFTER_POLLS):
        executor.sync_open_orders()
    assert "o1" not in executor._pending and play.status is PlayStatus.ERROR


# ---------------------------------------------------------------- replays and caches
def test_a_replay_download_waits_for_the_gateway_to_come_back(tmp_path, monkeypatch):
    gateway = fakes.FakeGateway(["RPA"])
    gateway.connected = True
    real, calls = gateway.history_many, []

    def flaky(requests, con_ids=None, end=None):
        calls.append(1)
        if len(calls) == 1:
            gateway.connected = False                                    # the nightly restart
            raise AuthError("IBKR not connected")
        return real(requests, con_ids, end)

    gateway.history_many = flaky
    monkeypatch.setattr(history_module.time, "sleep", lambda s: setattr(gateway, "connected", True))
    assert "RPA" in IntradayHistory(tmp_path / "a").load(gateway, ["RPA"], 3) and len(calls) == 2

    def broken(*a, **k):
        raise RuntimeError("bad request")
    gateway.history_many = broken                                        # connected, so a real error
    with pytest.raises(RuntimeError):
        IntradayHistory(tmp_path / "b").load(gateway, ["RPA"], 3)


def test_candles_and_quotes_nothing_asks_for_are_let_go(tmp_path):
    gateway, md = fakes.FakeGateway(delayed=True), MarketData(DailyBarStore(tmp_path))
    md.attach(gateway)
    md.intraday(["AAA", "BBB"])
    md.quote("AAA")
    old = time.monotonic() - 3600
    md._intraday["AAA"] = (old, md._intraday["AAA"][1])
    md._quotes["AAA"] = (old, md._quotes["AAA"][1])
    md._pruned_at = 0.0
    md.intraday(["BBB"])
    assert "AAA" not in md._intraday and "BBB" in md._intraday and "AAA" not in md._quotes
