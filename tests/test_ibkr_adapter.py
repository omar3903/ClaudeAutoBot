"""IBKR adapter - exercised against a fake ``ib_async.IB`` (no socket, no loop).

Covers the translation layer: interval -> barSize, order action (an order's
side is its direction, exits included), order states and IBKR's rejection
reasons, account parsing, quote NaN fallback, the delayed-data downgrade,
bar-frame shaping, and connection state. The real Gateway path is not tested
here (it needs a running IB Gateway).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest
from ib_async.order import OrderStatus

from tos_bot.brokers import ibkr_adapter as mod
from tos_bot.brokers.base import DONE_STATUSES, AuthError, OrderRejected
from tos_bot.brokers.paper_adapter import PaperBroker
from tos_bot.core.enums import OrderType, Side, TimeInForce
from tos_bot.core.models import OrderRequest, Quote
from tos_bot.execution.order_builder import build_exit_order


# --------------------------------------------------------------------------- #
#  fakes
# --------------------------------------------------------------------------- #
class FakeIB:
    def __init__(self):
        self._connected = False
        self.market_data_type = None
        self.placed = []
        self.history_requests = []

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

    # account - what reqAccountUpdates streams (tag, currency, value); a USD account by default
    account_values = [
        ("AccountCode", "", "DU111111"),
        ("NetLiquidation", "USD", "101234.50"),
        ("TotalCashValue", "USD", "40000"),
        ("BuyingPower", "USD", "200000"),
        ("NetLiquidationByCurrency", "BASE", "101234.50"),
    ]

    def accountValues(self, acct=""):
        return [SimpleNamespace(tag=t, currency=c, value=v, account="DU111111")
                for t, c, v in self.account_values]

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

    async def reqContractDetailsAsync(self, contract):
        if contract.symbol == "NOPE":
            return []
        if contract.symbol == "SLOW":
            raise asyncio.TimeoutError()
        return [SimpleNamespace(contract=SimpleNamespace(conId=265598, primaryExchange="NASDAQ"),
                                stockType="COMMON", industry="Technology", category="Computers")]

    async def reqHistoricalDataAsync(self, c, **kw):
        self.history_requests.append((c, kw))
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

    def reqAllOpenOrdersAsync(self):
        return _Finished(self.trades())          # like ib_async: an awaitable, not a coroutine

    def cancelOrder(self, order):
        order._cancelled = True

    # news
    def reqNewsProvidersAsync(self):
        return _Finished([SimpleNamespace(code="BRFG", name="Briefing.com General Market Columns"),
                          SimpleNamespace(code="BRFUPDN", name="Briefing.com Analyst Actions")])

    async def reqHistoricalNewsAsync(self, conId, providerCodes, startDateTime, endDateTime, totalResults):
        self.__dict__.setdefault("news_requests", []).append((conId, providerCodes, totalResults))
        return [SimpleNamespace(time=dt.datetime(2026, 9, 14, 13, 0), providerCode="BRFG", articleId="BRFG$1",
                                headline="{A:800015:L:en}Example Holdings beats on revenue")]


class _Finished:
    """An awaitable that has already finished - what several ib_async request methods return."""

    def __init__(self, value):
        self.value = value

    def __await__(self):
        if False:
            yield
        return self.value


class FakeSession:
    def __init__(self):
        self.ib = FakeIB()

    def start(self):
        pass

    def run_coro(self, factory, timeout=30.0):
        coro = factory(self.ib)
        if not asyncio.iscoroutine(coro):          # asyncio.run_coroutine_threadsafe refuses anything else
            raise TypeError("A coroutine object is required")
        return asyncio.new_event_loop().run_until_complete(coro)

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
    assert mod._norm_status("Cancelled") == "CANCELED" and mod._norm_status("Inactive") == "REJECTED"
    assert mod._tif(TimeInForce.GTC) == "GTC"
    assert mod._tif(TimeInForce.DAY) == "DAY"


def test_ibkrs_finished_order_states_are_the_apps_finished_states():
    assert {mod._norm_status(s) for s in OrderStatus.DoneStates} <= DONE_STATUSES
    assert not {mod._norm_status(s) for s in OrderStatus.ActiveStates} & DONE_STATUSES


def test_bars_to_df_shape_and_tz():
    base = dt.datetime(2026, 9, 3, 13, 30, tzinfo=dt.timezone.utc)
    bars = [SimpleNamespace(date=base + dt.timedelta(minutes=5 * i), open=1.0 + i,
                            high=2.0 + i, low=0.5 + i, close=1.5 + i, volume=3 + i)
            for i in range(6)]
    df = mod._bars_to_df(bars)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert str(df.index.tz) == "America/New_York"
    assert df.index.is_monotonic_increasing
    assert df["volume"].iloc[0] == 3.0                # IBKR reports stock volume in shares


def test_daily_bars_stay_on_their_session_date():
    # IBKR sends daily bars as plain dates; read as UTC midnight they'd land on
    # the previous evening in New York, a whole session early
    bars = [SimpleNamespace(date=dt.date(2026, 9, day), open=1.0, high=2.0, low=0.5, close=1.5, volume=100)
            for day in (10, 11)]
    df = mod._bars_to_df(bars)
    assert [t.date() for t in df.index] == [dt.date(2026, 9, 10), dt.date(2026, 9, 11)]
    assert (df.index.hour == 0).all() and str(df.index.tz) == "America/New_York"


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


def test_history_for_many_symbols_at_once(broker):
    frames = broker.history_many({"AAPL": ("5 mins", "2 D"), "MSFT": ("1 day", "1 Y")}, con_ids={"MSFT": 272093})
    assert set(frames) == {"AAPL", "MSFT"} and len(frames["AAPL"]) == 20
    assert list(frames["MSFT"].columns) == ["open", "high", "low", "close", "volume"]
    asked = {getattr(c, "symbol", "") or c.conId: kw for c, kw in broker._session.ib.history_requests}
    assert asked["AAPL"]["barSizeSetting"] == "5 mins" and asked["AAPL"]["durationStr"] == "2 D"
    assert asked[272093]["durationStr"] == "1 Y"                  # a known contract id skips the lookup


def test_a_failed_history_request_is_left_out(broker):
    async def pacing_violation(c, **kw):
        raise RuntimeError("pacing violation")

    broker._session.ib.reqHistoricalDataAsync = pacing_violation
    assert broker.history_many({"AAPL": ("1 day", "5 D")}) == {}


def test_contract_details_tell_stocks_from_unknown_symbols(broker):
    details = broker.contract_details_many(["AAPL", "NOPE", "SLOW"])
    assert details["AAPL"] == {"con_id": 265598, "exchange": "NASDAQ", "stock_type": "COMMON",
                               "industry": "Technology", "category": "Computers"}
    assert details["NOPE"] is None                                # IBKR has no such stock
    assert "SLOW" not in details                                  # a failed lookup is asked again later


@pytest.mark.parametrize("side,is_entry,expect", [
    (Side.LONG, True, "BUY"),      # open a long
    (Side.SHORT, True, "SELL"),    # open a short
    (Side.SHORT, False, "SELL"),   # close a long
    (Side.LONG, False, "BUY"),     # cover a short
])
def test_an_orders_side_is_its_direction(broker, side, is_entry, expect):
    req = OrderRequest(symbol="AAPL", side=side, quantity=10,
                       order_type=OrderType.MARKET, is_entry=is_entry)
    res = broker.place_order(req)
    assert res.symbol == "AAPL" and res.order_id
    _, order = broker._session.ib.placed[-1]
    assert order.action == expect


@pytest.mark.parametrize("position", [Side.LONG, Side.SHORT])
def test_an_exit_closes_the_position_at_ibkr_and_in_the_simulator(broker, position):
    # one exit order has to flatten the position on both brokers - IBKR once read
    # the side as the position's and bought more instead of selling
    exit_order = build_exit_order("AAPL", position.value, 10)
    broker.place_order(exit_order)
    assert broker._session.ib.placed[-1][1].action == ("SELL" if position is Side.LONG else "BUY")

    sim = PaperBroker(quote=lambda s: Quote(symbol=s, bid=99.9, ask=100.1, last=100.0))
    sim.connect()
    sim.place_order(OrderRequest(symbol="AAPL", side=position, quantity=10, order_type=OrderType.MARKET))
    sim.place_order(exit_order)
    assert sim.get_account().position("AAPL") is None


def test_a_rejected_order_is_finished_with_ibkrs_reason(broker):
    res = broker.place_order(build_exit_order("AAPL", "LONG", 10))
    contract, order = broker._session.ib.placed[-1]
    rejected = SimpleNamespace(
        order=order, contract=contract, fills=[],
        orderStatus=SimpleNamespace(status="Cancelled", filled=0, avgFillPrice=0.0),
        log=[SimpleNamespace(errorCode=0, message=""),
             SimpleNamespace(errorCode=201, message="Error 201, reqId 1: Order rejected - reason:Your "
                                                    "Available Funds are in sufficient<br>to cover it.")])
    broker._session.ib.trades = lambda: [rejected]
    got = broker.get_order(res.order_id)
    assert got.status == "CANCELED" and got.status in DONE_STATUSES
    assert got.message == "Order rejected - reason:Your Available Funds are in sufficient to cover it."


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
    assert st["connected"] is True and st["reconnecting"] is False
    assert st["port"] == 4002 and st["mode"] == "paper"
    assert st["market_data"] == "live"


# --------------------------------------------------------------------------- #
#  accounts in another currency (an IBKR Canada paper account is in CAD)
# --------------------------------------------------------------------------- #
def _cad_account(ib, rate_tag=None):
    ib.account_values = [
        ("NetLiquidation", "CAD", "1000000.00"),
        ("TotalCashValue", "CAD", "1000000.00"),
        ("BuyingPower", "CAD", "3333333.33"),
        ("NetLiquidationByCurrency", "BASE", "1000000.00"),
    ] + ([("ExchangeRate", "USD", rate_tag)] if rate_tag else [])


def test_an_account_in_another_currency_is_sized_in_usd(broker):
    _cad_account(broker._session.ib)
    broker._fx_fn = lambda cur: 0.72 if cur == "CAD" else None
    acc = broker.get_account()
    assert acc.base_currency == "CAD" and acc.usd_per_base == pytest.approx(0.72)
    assert acc.equity == pytest.approx(720_000.0) and acc.buying_power == pytest.approx(2_400_000.0)
    assert acc.raw["base"]["equity"] == pytest.approx(1_000_000.0) and not acc.raw["fx_missing"]


def test_ibkrs_own_exchange_rate_is_used_when_it_sends_one(broker):
    _cad_account(broker._session.ib, rate_tag="1.25")            # 1 USD = 1.25 CAD
    broker._fx_fn = lambda cur: pytest.fail("no lookup needed")
    assert broker.get_account().equity == pytest.approx(800_000.0)


def test_without_an_exchange_rate_nothing_can_be_sized(broker):
    _cad_account(broker._session.ib)
    broker._fx_fn = lambda cur: None
    acc = broker.get_account()
    assert acc.equity == 0.0 and acc.usd_per_base == 0.0 and acc.raw["fx_missing"]
    assert acc.raw["base"]["cash"] == pytest.approx(1_000_000.0)


# --------------------------------------------------------------------------- #
#  no market-data subscription
# --------------------------------------------------------------------------- #
def test_a_quote_with_no_price_raises_so_the_next_feed_answers(broker):
    async def empty(c):
        return [SimpleNamespace(bid=-1.0, ask=-1.0, last=float("nan"), close=float("nan"),
                                volume=float("nan"), marketPrice=float("nan"))]

    broker._session.ib.reqTickersAsync = empty
    with pytest.raises(RuntimeError):
        broker.get_quote("AAPL")


def test_not_subscribed_switches_to_delayed_data(broker):
    broker._on_error(5, 354, "Requested market data is not subscribed. Delayed market data is available.", None)
    assert broker._data_is_delayed and broker.quotes_from_bars
    assert broker._session.ib.market_data_type == 3


def test_open_orders_carry_direction_and_tag_so_a_restart_can_match_them(broker):
    broker.place_order(build_exit_order("AAPL", "LONG", 10, tag="exit:trd_1"))
    [order] = broker.list_orders("WORKING")
    assert (order.symbol, order.side, order.tag, order.submitted_qty) == ("AAPL", Side.SHORT, "exit:trd_1", 10)
    assert broker._session.ib.placed[-1][1].orderRef == "exit:trd_1"


def test_news_headlines_come_from_every_feed_the_account_can_read(broker):
    got = broker.news_headlines({"AAPL": 265598}, days=3, per_symbol=5)
    assert got == {"AAPL": [(dt.datetime(2026, 9, 14, 13, 0), "BRFG", "BRFG$1",
                             "{A:800015:L:en}Example Holdings beats on revenue")]}
    assert broker._session.ib.news_requests == [(265598, "BRFG+BRFUPDN", 5)]


def test_open_orders_report_their_type_prices_and_time_in_force(broker):
    broker.place_order(OrderRequest(symbol="AAPL", side=Side.LONG, quantity=5, order_type=OrderType.LIMIT,
                                    limit_price=123.45, tif=TimeInForce.GTC, client_tag="play_1"))
    [order] = broker.list_orders("WORKING")
    assert order.order_type == "LIMIT" and order.limit_price == pytest.approx(123.45)
    assert order.stop_price is None                     # IBKR's "unset" price is not a price
    assert order.tif == "GTC" and order.raw["parent_id"] is None
