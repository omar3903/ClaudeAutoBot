"""IBKR adapter - exercised against a fake ``ib_async.IB`` (no socket, no loop).

Covers the translation layer: interval -> barSize, order action (LONG/SHORT x
entry/exit -> BUY/SELL), account parsing, quote NaN fallback, the delayed-data
downgrade, bar-frame shaping, and connection state. The real Gateway path is
not tested here (it needs a running IB Gateway).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest

from tos_bot.brokers import ibkr_adapter as mod
from tos_bot.brokers.base import AuthError, NotSupported, OrderRejected
from tos_bot.core.enums import OrderType, Side, TimeInForce
from tos_bot.core.models import OrderRequest


# --------------------------------------------------------------------------- #
#  fakes
# --------------------------------------------------------------------------- #
class FakeIB:
    def __init__(self):
        self._connected = False
        self.market_data_type = None
        self.placed = []

    # connection
    def isConnected(self):
        return self._connected

    async def connectAsync(self, host, port, clientId, timeout, readonly):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def reqMarketDataType(self, t):
        self.market_data_type = t

    def managedAccounts(self):
        return ["DU111111"]

    # account
    async def reqAccountSummaryAsync(self):
        return []

    def accountSummary(self, acct=""):
        AV = SimpleNamespace
        return [
            AV(tag="NetLiquidation", value="101234.50", currency="USD", account="DU111111"),
            AV(tag="TotalCashValue", value="40000", currency="USD", account="DU111111"),
            AV(tag="BuyingPower", value="200000", currency="USD", account="DU111111"),
        ]

    def portfolio(self, acct=""):
        return [SimpleNamespace(contract=SimpleNamespace(symbol="MSFT"),
                                position=10.0, averageCost=390.0, marketPrice=400.0)]

    def positions(self, acct=""):
        return []

    # contracts / data
    async def qualifyContractsAsync(self, c):
        return [c]

    async def reqTickersAsync(self, c):
        return [SimpleNamespace(bid=100.0, ask=100.1, last=float("nan"),
                                close=99.9, volume=1234.0, marketPrice=100.0)]

    async def reqHistoricalDataAsync(self, c, **kw):
        base = dt.datetime(2026, 9, 3, 13, 30, tzinfo=dt.timezone.utc)
        out = []
        for i in range(20):
            out.append(SimpleNamespace(
                date=base + dt.timedelta(minutes=5 * i),
                open=100 + i * 0.1, high=100.5 + i * 0.1,
                low=99.5 + i * 0.1, close=100.2 + i * 0.1, volume=50 + i))
        return out

    # orders
    def placeOrder(self, contract, order):
        order.orderId = len(self.placed) + 1
        self.placed.append((contract, order))
        return SimpleNamespace(order=order, contract=contract,
                               orderStatus=SimpleNamespace(status="PreSubmitted", filled=0,
                                                           avgFillPrice=0.0),
                               fills=[])

    def bracketOrder(self, action, qty, limitPrice, takeProfitPrice, stopLossPrice):
        return [SimpleNamespace(action=a, tif="DAY", transmit=False, orderId=0,
                                totalQuantity=qty, account="")
                for a in (action, "SELL" if action == "BUY" else "BUY",
                          "SELL" if action == "BUY" else "BUY")]

    def trades(self):
        return [SimpleNamespace(order=o, contract=c,
                                orderStatus=SimpleNamespace(status="Submitted", filled=0,
                                                            avgFillPrice=0.0),
                                fills=[]) for c, o in self.placed]

    def openTrades(self):
        return self.trades()

    def cancelOrder(self, order):
        order._cancelled = True


class FakeSession:
    def __init__(self):
        self.ib = FakeIB()

    def start(self):
        pass

    def run_coro(self, factory, timeout=30.0):
        return asyncio.new_event_loop().run_until_complete(factory(self.ib))

    def call(self, fn, timeout=15.0):
        return fn(self.ib)

    def stop(self):
        pass


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setattr(mod, "port_is_open", lambda *a, **k: True)

    async def _fast(*_a):
        return None

    monkeypatch.setattr(mod, "_sleep", _fast)
    b = mod.IbkrBroker(port=4002, mode="paper", session_factory=FakeSession)
    b.connect()
    return b


# --------------------------------------------------------------------------- #
#  pure helpers
# --------------------------------------------------------------------------- #
def test_norm_status_and_tif():
    assert mod._norm_status("PreSubmitted") == "SUBMITTED"
    assert mod._norm_status("Submitted") == "WORKING"
    assert mod._norm_status("Filled") == "FILLED"
    assert mod._tif(TimeInForce.GTC) == "GTC"
    assert mod._tif(TimeInForce.DAY) == "DAY"


def test_bars_to_df_shape_and_tz():
    base = dt.datetime(2026, 9, 3, 13, 30, tzinfo=dt.timezone.utc)
    bars = [SimpleNamespace(date=base + dt.timedelta(minutes=5 * i), open=1.0 + i,
                            high=2.0 + i, low=0.5 + i, close=1.5 + i, volume=3 + i)
            for i in range(6)]
    df = mod._bars_to_df(bars)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert str(df.index.tz) == "America/New_York"
    assert df.index.is_monotonic_increasing
    assert df["volume"].iloc[0] == 3 * 100.0          # lots -> shares


def test_port_is_open_false_on_dead_port():
    assert mod.port_is_open("127.0.0.1", 59999, timeout=0.3) is False


# --------------------------------------------------------------------------- #
#  adapter behaviour
# --------------------------------------------------------------------------- #
def test_connect_requires_a_listening_port(monkeypatch):
    monkeypatch.setattr(mod, "port_is_open", lambda *a, **k: False)
    b = mod.IbkrBroker(port=4002, mode="paper", session_factory=FakeSession)
    with pytest.raises(AuthError):
        b.connect()


def test_connected_and_account(broker):
    assert broker.is_connected
    acc = broker.get_account()
    assert acc.equity == pytest.approx(101234.50)
    assert acc.cash == pytest.approx(40000.0)
    assert acc.buying_power == pytest.approx(200000.0)
    assert acc.is_cash_account is False
    assert len(acc.positions) == 1 and acc.positions[0].symbol == "MSFT"
    assert acc.positions[0].market_price == 400.0


def test_quote_falls_back_to_close_when_last_is_nan(broker):
    q = broker.get_quote("AAPL")
    assert q.last == pytest.approx(99.9)              # last was NaN -> close
    assert q.bid == 100.0 and q.ask == 100.1


def test_price_history_interval_map_and_reject(broker):
    df = broker.get_price_history("AAPL", "5m", 2)
    assert len(df) == 20 and list(df.columns)[0] == "open"
    with pytest.raises(NotSupported):
        broker.get_price_history("AAPL", "3m", 2)


@pytest.mark.parametrize("side,is_entry,expect", [
    (Side.LONG, True, "BUY"),      # open long
    (Side.SHORT, True, "SELL"),    # open short
    (Side.LONG, False, "SELL"),    # close long
    (Side.SHORT, False, "BUY"),    # cover short
])
def test_order_action_mapping(broker, side, is_entry, expect):
    req = OrderRequest(symbol="AAPL", side=side, quantity=10,
                       order_type=OrderType.MARKET, is_entry=is_entry)
    res = broker.place_order(req)
    assert res.symbol == "AAPL" and res.order_id
    _, order = broker._session.ib.placed[-1]
    assert order.action == expect


def test_limit_order_carries_price_and_tif(broker):
    req = OrderRequest(symbol="AAPL", side=Side.LONG, quantity=5,
                       order_type=OrderType.LIMIT, limit_price=123.45,
                       tif=TimeInForce.GTC, session="EXTENDED")
    broker.place_order(req)
    _, order = broker._session.ib.placed[-1]
    assert order.lmtPrice == pytest.approx(123.45)
    assert order.tif == "GTC"
    assert order.outsideRth is True


def test_readonly_blocks_orders(monkeypatch):
    monkeypatch.setattr(mod, "port_is_open", lambda *a, **k: True)

    async def _fast(*_a):
        return None

    monkeypatch.setattr(mod, "_sleep", _fast)
    b = mod.IbkrBroker(port=4002, mode="paper", readonly=True, session_factory=FakeSession)
    b.connect()
    with pytest.raises(OrderRejected):
        b.place_order(OrderRequest(symbol="AAPL", side=Side.LONG, quantity=1,
                                   order_type=OrderType.MARKET))


def test_delayed_data_downgrade(broker):
    assert broker._data_is_delayed is False
    broker._on_error(1, 10167, "Market data farm ... delayed", None)
    assert broker._data_is_delayed is True
    assert broker._data_type == 3
    assert broker._session.ib.market_data_type == 3
    assert broker.session_status()["market_data"] == "delayed"


def test_info_errors_are_ignored(broker):
    broker._on_error(1, 2104, "Market data farm connection is OK", None)
    assert broker._data_is_delayed is False
    assert broker._last_error == ""


def test_session_status_shape(broker):
    st = broker.session_status()
    assert st["broker"] == "ibkr" and st["connected"] is True
    assert st["port"] == 4002 and st["mode"] == "paper"
    assert st["market_data"] == "live"
