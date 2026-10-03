"""IBKR adapter - exercised against a fake ``ib_async.IB`` (no socket, no loop).

Covers the translation layer: interval -> barSize, order action (an order's
side is its direction, exits included), order states and IBKR's rejection
reasons, account parsing, quote NaN fallback, the delayed-data downgrade,
bar-frame shaping, connection state, real-time streams (the lines held,
when a stream's quote can be trusted, and IBKR's refusals) and the live market
scans (always cancelled). The real Gateway path is not tested
here (it needs a running IB Gateway). Where the app's threads overlap, the real
session's loop runs around the fake (ThreadedSession).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
import time
from collections import defaultdict
from concurrent.futures import TimeoutError as FutureTimeout
from types import SimpleNamespace

import pytest
from ib_async.order import OrderStatus

from autotradebot.brokers import ibkr_adapter as mod
from autotradebot.brokers.base import DONE_STATUSES, AuthError, BrokerError, OrderInDoubt, OrderRejected
from autotradebot.brokers.paper_adapter import PaperBroker
from autotradebot.core.enums import OrderType, Side, TimeInForce
from autotradebot.core.models import OrderRequest, Quote
from autotradebot.execution.order_builder import build_exit_order


# --------------------------------------------------------------------------- #
#  fakes
# --------------------------------------------------------------------------- #
class FakeTicker:
    """Like ib_async's Ticker: one per contract, shared by its stream and its snapshots, hashing by identity.
    A price it hasn't had is NaN."""

    def __init__(self, contract):
        self.contract, self.marketDataType, self.time = contract, 1, None
        self.bid = self.ask = self.last = self.close = self.volume = self.halted = float("nan")
        self.bidSize = self.askSize = self.lastSize = float("nan")


class FakeWrapper:
    """ib_async's bookkeeping of market-data requests: a Ticker per contract, and its request ids by kind - and of
    one-off requests (startReq), which the fake answers with ``scan_rows`` on the loop's next turn, or never when
    that is None."""

    def __init__(self):
        self.scan_rows = []
        self.reset()

    def reset(self):                                   # what ib_async does when the socket closes
        self.tickers, self.reqId2Ticker, self.ticker2ReqId = {}, {}, defaultdict(dict)

    def startTicker(self, req_id, contract, kind):
        tk = self.tickers.setdefault(contract.conId, FakeTicker(contract))
        self.reqId2Ticker[req_id] = tk
        self.ticker2ReqId[kind][tk] = req_id
        return tk

    def endTicker(self, tk, kind):
        return self.ticker2ReqId[kind].pop(tk, 0)

    def startReq(self, key, contract=None, container=None):
        future = asyncio.get_running_loop().create_future()
        rows = self.scan_rows

        def answer():
            if not future.done():
                container.extend(rows)
                future.set_result(container)

        if rows is not None:
            asyncio.get_running_loop().call_soon(answer)
        return future


class _ScanDataList(list):
    """Like ib_async's ScanDataList: the rows of one scanner subscription, and its request id."""

    reqId = 0


def _scan_row(rank, symbol, sec_type="STK"):
    return SimpleNamespace(rank=rank, contractDetails=SimpleNamespace(contract=SimpleNamespace(symbol=symbol,
                                                                                               secType=sec_type)))


class _FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def emit(self, *args):
        for handler in self.handlers:
            handler(*args)


class FakeIB:
    def __init__(self):
        self._connected = False
        self.market_data_type = None
        self.placed = []
        self.history_requests = []
        # streams: every request and cancel, by symbol, and each batch of contracts looked up
        self.wrapper, self.client = FakeWrapper(), SimpleNamespace(_reqIdSeq=100)
        self.pendingTickersEvent, self.errorEvent, self.disconnectedEvent = _FakeEvent(), _FakeEvent(), _FakeEvent()
        self.subscribed, self.cancelled, self.stray_cancels, self.qualified, self.con_ids = [], [], [], [], {}
        self.scans, self.scan_cancels = [], []     # market scans: each subscription asked for, each one cancelled
        self.last_time = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        self.clock = None                          # a wall clock to stamp packets by instead (see ticks_arrived)
        # snapshots: the ticks one sends, and the error IBKR refuses one with instead (0: none)
        self.snapshot = dict(bid=100.0, ask=100.1, last=float("nan"), close=99.9, volume=1234.0)
        self.refuse_snapshots = 0

    # connection
    def isConnected(self):
        return self._connected

    async def connectAsync(self, host, port, clientId, timeout, readonly):
        self._connected = True
        self.client._reqIdSeq = 100                    # like ib_async's, request ids start again with the connection

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
    async def qualifyContractsAsync(self, *contracts):
        # like ib_async 2.1: a contract IBKR knows is filled in place and handed back; an unknown one is None
        self.qualified.append([c.symbol for c in contracts])
        out = []
        for c in contracts:
            if c.symbol != "NOPE":
                c.conId = self.con_ids.setdefault(c.symbol, 1000 + len(self.con_ids))
            out.append(c if c.conId else None)
        return out

    def ticker(self, contract):
        return self.wrapper.tickers.get(contract.conId)

    def reqMktData(self, contract, genericTickList="", snapshot=False, regulatorySnapshot=False):
        if not self._connected:
            raise ConnectionError("Not connected")
        req_id, self.client._reqIdSeq = self.client._reqIdSeq, self.client._reqIdSeq + 1
        self.subscribed.append(contract.symbol)
        return self.wrapper.startTicker(req_id, contract, "mktData")

    def cancelMktData(self, contract):
        tk = self.ticker(contract)
        req_id = self.wrapper.endTicker(tk, "mktData") if tk else 0
        # a cancel with no request behind it is an error ib_async logs: the broker fixture fails the test on one
        (self.cancelled if req_id else self.stray_cancels).append(contract.symbol)
        return bool(req_id)

    def ticks_arrived(self, *tickers):
        """A packet's ticks landing, as ib_async's wrapper.tcpDataProcessed hands them on: each Ticker that got one
        is stamped with the packet's time - a new datetime for each packet, as ib_async's datetime.now() gives -
        then pendingTickersEvent. The fake's packet times always move on, unless a test sets ``clock`` to stamp
        them as a coarse or reset wall clock would."""
        self.last_time = (self.clock() if self.clock else
                          max(dt.datetime.now(dt.timezone.utc), self.last_time + dt.timedelta(microseconds=1)))
        for tk in tickers:
            tk.time = self.last_time
        self.pendingTickersEvent.emit(set(tickers))

    async def reqTickersAsync(self, c):
        # like ib_async 2.1: a snapshot lands on the stock's one Ticker - the one its stream and earlier snapshots
        # share - and a refusal ends the request with no error raised, so the Ticker comes back all the same, as
        # the last ticks to reach it left it
        req_id, self.client._reqIdSeq = self.client._reqIdSeq, self.client._reqIdSeq + 1
        tk = self.wrapper.startTicker(req_id, c, "snapshot")
        if self.refuse_snapshots:
            self.errorEvent.emit(req_id, self.refuse_snapshots, "Snapshot refused", c)
        else:
            for name, value in self.snapshot.items():
                setattr(tk, name, value)
            self.ticks_arrived(tk)
        self.wrapper.endTicker(tk, "snapshot")
        return [tk]

    def reqScannerSubscription(self, subscription):
        data, self.client._reqIdSeq = _ScanDataList(), self.client._reqIdSeq + 1
        data.reqId = self.client._reqIdSeq - 1
        self.scans.append(subscription)
        return data

    def cancelScannerSubscription(self, data):
        self.scan_cancels.append(data.reqId)

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
    yield b
    assert b._session.ib.stray_cancels == []


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


def test_a_slow_answer_raises_instead_of_reading_as_an_empty_account(broker):
    from autotradebot.brokers.base import BrokerError

    real = broker._session.call

    def all_slow(fn, timeout=15.0):
        raise TimeoutError("the loop is busy")

    def positions_slow(fn, timeout=15.0):
        if "portfolio" in fn.__code__.co_names:
            raise TimeoutError("the loop is busy")
        return real(fn, timeout)

    broker._session.call = all_slow
    with pytest.raises(BrokerError, match="account values"):
        broker.get_account()
    broker._session.call = positions_slow
    with pytest.raises(BrokerError, match="positions"):
        broker.get_account()
    broker._session.call = real
    assert [p.symbol for p in broker.get_account().positions] == ["MSFT"]


def test_quote_falls_back_to_close_when_last_is_nan(broker):
    q = broker.get_quote("AAPL")
    assert q.last == pytest.approx(99.9)              # last was NaN -> close
    assert q.bid == 100.0 and q.ask == 100.1 and q.source == "snapshot"


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
    got = broker.history_many({"AAPL": ("1 day", "5 D")})
    assert got == {} and got.failed == {"AAPL"}                   # it failed - not the same as IBKR having nothing


def test_a_request_that_times_out_counts_as_failed_and_an_empty_answer_doesnt(broker):
    async def nothing(c, **kw):
        return []                                                  # how ib_async answers both

    broker._session.ib.reqHistoricalDataAsync = nothing
    quick = broker.history_many({"AAPL": ("5 mins", "5 D")})
    assert quick == {} and quick.failed == set()                  # answered at once: IBKR has nothing for it
    slow = broker.history_many({"AAPL": ("5 mins", "5 D")}, timeout=0.2)
    assert slow == {} and slow.failed == {"AAPL"}                 # took the whole timeout: it timed out
    assert broker.history_many({}).failed == set()


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


def test_a_stop_limit_entry_rests_as_a_stop_limit_and_an_unknown_order_type_is_refused(broker):
    # a breakout entry waits for its trigger - sent as a plain limit it would have filled at once
    broker.place_order(OrderRequest(symbol="AAA", side=Side.LONG, quantity=10, order_type=OrderType.STOP_LIMIT,
                                    stop_price=50.0, limit_price=50.1, client_tag="play_1"))
    _, order = broker._session.ib.placed[-1]
    assert (order.orderType, order.action, order.auxPrice, order.lmtPrice, order.totalQuantity, order.orderRef) == \
        ("STP LMT", "BUY", 50.0, 50.1, 10, "play_1")
    [working] = broker.list_orders("WORKING")
    assert (working.order_type, working.stop_price, working.limit_price) == ("STOP_LIMIT", 50.0, 50.1)

    sent = len(broker._session.ib.placed)
    for missing in ({"stop_price": 50.0}, {"limit_price": 50.1}):
        with pytest.raises(OrderRejected, match="stop-limit"):
            broker.place_order(OrderRequest(symbol="AAA", side=Side.LONG, quantity=10,
                                            order_type=OrderType.STOP_LIMIT, **missing))
    with pytest.raises(OrderRejected, match="TRAILING_STOP"):
        broker.place_order(OrderRequest(symbol="AAA", side=Side.SHORT, quantity=10, order_type="TRAILING_STOP",
                                        limit_price=49.0, stop_price=49.5))
    assert len(broker._session.ib.placed) == sent              # nothing went to IBKR in place of them


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
    broker._session.ib.snapshot = dict(bid=-1.0, ask=-1.0, last=float("nan"), close=float("nan"),
                                       volume=float("nan"))
    with pytest.raises(RuntimeError):
        broker.get_quote("AAPL")


def test_real_time_data_comes_back_once_the_competing_session_logs_out(broker, monkeypatch):
    broker.set_streams(["AAA"], 1)
    broker._on_error(broker._streams["AAA"].req_id, 354, "Requested market data is not subscribed.", None)
    assert "AAA" in broker._unstreamable                                   # one stock refused a stream, on live data
    broker._on_error(4, 10197, "No market data during competing live session", None)
    status = broker.session_status()
    assert status["market_data"] == "delayed" and "logged in somewhere else" in status["market_data_reason"]
    assert not broker.can_stream

    ib = broker._session.ib
    real = ib.reqTickersAsync

    async def refused(contract):
        broker._on_error(9, 10197, "No market data during competing live session", None)
        return await real(contract)
    ib.reqTickersAsync = refused                                           # still logged in on the phone
    broker.refresh_if_needed()                                             # too soon to ask again
    assert broker._data_is_delayed and ib.market_data_type == 3
    broker._live_checked_at -= broker.LIVE_RECHECK_S
    broker.refresh_if_needed()
    assert broker._data_is_delayed and ib.market_data_type == 3 and not broker.can_stream

    ib.reqTickersAsync = real                                              # logged out there
    broker._live_checked_at -= broker.LIVE_RECHECK_S
    broker._competing_at -= 2 * broker.LIVE_RECHECK_S
    broker.refresh_if_needed()
    assert not broker._data_is_delayed and ib.market_data_type == 1 and broker.session_status()["market_data_reason"] == ""
    # the recheck's probe proved the data real-time: streams again, the stock refused before among them
    assert broker.can_stream and broker.set_streams(["AAA"], 1) == ["AAA"]


def test_every_connection_tries_real_time_data_again(broker):
    broker._on_error(5, 354, "Requested market data is not subscribed.", None)
    assert "no real-time subscription" in broker.market_data_reason
    broker._do_connect()                                                  # IB Gateway logged out and in
    assert not broker._data_is_delayed and broker._session.ib.market_data_type == 1


def test_candles_refused_for_a_competing_session_are_explained(broker):
    broker._on_error(7, 162, "Historical Market Data Service error message:Trading TWS session is connected from a "
                             "different IP address", None)
    assert not broker._data_is_delayed and "logged in somewhere else" in broker.market_data_reason


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


def test_a_stop_order_rests_good_till_cancelled_and_can_be_moved_in_place(broker):
    req = OrderRequest(symbol="AAPL", side=Side.SHORT, quantity=10, order_type=OrderType.STOP, stop_price=98.5,
                       tif=TimeInForce.GTC, is_entry=False, client_tag="stop:trd_1")
    res = broker.place_order(req)
    _, order = broker._session.ib.placed[-1]
    assert (order.orderType, order.action, order.auxPrice, order.tif, order.orderRef) == ("STP", "SELL", 98.5, "GTC",
                                                                                         "stop:trd_1")
    assert order.outsideRth is False and broker.supports_native_stop
    broker.modify_stop(res.order_id, stop_price=100.25, quantity=5)
    _, again = broker._session.ib.placed[-1]
    assert again.orderId == order.orderId and again.auxPrice == 100.25 and again.totalQuantity == 5
    with pytest.raises(Exception):
        broker.place_order(OrderRequest(symbol="AAPL", side=Side.SHORT, quantity=10, order_type=OrderType.STOP))


def test_a_resting_target_joins_the_stops_one_cancels_all_group(broker):
    group = "oca:trd_1:1:1"
    broker.place_order(OrderRequest(symbol="AAPL", side=Side.SHORT, quantity=10, order_type=OrderType.STOP, stop_price=98.5,
                                    tif=TimeInForce.GTC, is_entry=False, client_tag="stop:trd_1", oca_group=group, oca_type=3))
    broker.place_order(OrderRequest(symbol="AAPL", side=Side.SHORT, quantity=5, order_type=OrderType.LIMIT, limit_price=104.0,
                                    tif=TimeInForce.GTC, is_entry=False, client_tag="tgt:trd_1", oca_group=group, oca_type=3))
    (_, stop), (_, target) = broker._session.ib.placed[-2:]
    assert (stop.ocaGroup, stop.ocaType, target.ocaGroup, target.ocaType) == (group, 3, group, 3)
    assert (target.orderType, target.action, target.lmtPrice, target.tif, target.orderRef) == ("LMT", "SELL", 104.0, "GTC",
                                                                                               "tgt:trd_1")
    broker.place_order(OrderRequest(symbol="AAPL", side=Side.LONG, quantity=5, order_type=OrderType.LIMIT, limit_price=100.0))
    assert not getattr(broker._session.ib.placed[-1][1], "ocaGroup", "")       # an ordinary order is in no group


def test_each_fill_carries_the_tag_of_the_order_it_filled(broker):
    import datetime as dt

    execution = SimpleNamespace(orderId=7, execId="x.1", side="SLD", shares=40.0, price=12.5, orderRef="exit:trd_1",
                                time=dt.datetime(2026, 9, 3, 15, 0, tzinfo=dt.timezone.utc))
    item = SimpleNamespace(execution=execution, contract=SimpleNamespace(symbol="AAPL"),
                           commissionReport=SimpleNamespace(commission=1.0))

    async def executions(wanted):
        return [item]

    broker._session.ib.reqExecutionsAsync = executions
    [fill] = broker.get_fills("AAPL")
    assert (fill.tag, fill.quantity, fill.price, fill.side) == ("exit:trd_1", 40.0, 12.5, Side.SHORT)


def test_executions_that_cant_be_read_raise_when_asked_strictly_rather_than_read_as_none(broker):
    async def timed_out(wanted):
        raise asyncio.TimeoutError("no answer from IB Gateway")

    broker._session.ib.reqExecutionsAsync = timed_out
    assert broker.get_fills("AAA") == []                                       # the old answer, for the old callers
    with pytest.raises(BrokerError, match="couldn't be read"):
        broker.get_fills("AAA", strict=True)
    broker._connected = False
    assert broker.get_fills() == []
    with pytest.raises(BrokerError, match="not connected"):
        broker.get_fills(strict=True)


# --------------------------------------------------------------------------- #
#  IBKR's live market scans
# --------------------------------------------------------------------------- #
def test_a_market_scan_gives_its_stocks_in_rank_order_and_is_cancelled_once_answered(broker):
    ib = broker._session.ib
    ib.wrapper.scan_rows = [_scan_row(1, "BBB"), _scan_row(0, "AAA"), _scan_row(2, "CCC", "WAR"),
                            _scan_row(3, "DDD"), _scan_row(4, "AAA")]
    assert broker.market_scan("TOP_PERC_GAIN") == ["AAA", "BBB", "DDD"]          # stocks only, each once
    [sub] = ib.scans
    assert (sub.instrument, sub.locationCode, sub.scanCode, sub.numberOfRows, sub.abovePrice, sub.belowPrice) == (
        "STK", "STK.US.MAJOR", "TOP_PERC_GAIN", 50, 3.0, 600.0)
    assert ib.scan_cancels == [ib.client._reqIdSeq - 1]                           # no scan stays open
    assert ib.subscribed == []                                                    # and no market-data line is used


def test_a_market_scan_that_never_answers_is_cancelled_all_the_same(broker):
    ib = broker._session.ib
    ib.wrapper.scan_rows = None
    assert broker.market_scan("HOT_BY_VOLUME", timeout=0.05) == []
    assert [s.scanCode for s in ib.scans] == ["HOT_BY_VOLUME"] and ib.scan_cancels == [ib.client._reqIdSeq - 1]


def test_a_market_scan_asks_nothing_when_not_connected_and_a_failure_is_no_names(broker):
    ib = broker._session.ib

    def refused(subscription):
        raise ConnectionError("Not connected")

    ib.reqScannerSubscription = refused
    assert broker.market_scan("TOP_PERC_LOSE") == [] and ib.scan_cancels == []
    del ib.reqScannerSubscription
    ib.disconnect()
    assert broker.market_scan("TOP_PERC_LOSE") == [] and ib.scans == []


def test_the_echo_of_a_scans_cancel_is_no_error(broker):
    import logging

    broker._on_error(101, 162, "API scanner subscription cancelled: 101", None)
    assert broker._last_error == ""
    quiet = mod._QuietDataErrors()
    echo = logging.LogRecord("ib_async.wrapper", logging.ERROR, __file__, 1,
                             "Error 162, reqId 101: API scanner subscription cancelled: 101", None, None)
    assert not quiet.filter(echo)
    # a 162 about candles is still noted
    other = "Historical Market Data Service error message:HMDS query returned no data"
    broker._on_error(7, 162, other, None)
    assert broker._last_error == f"162: {other}"
    assert quiet.filter(logging.LogRecord("ib_async.wrapper", logging.ERROR, __file__, 1,
                                          f"Error 162, reqId 7: {other}", None, None))


# --------------------------------------------------------------------------- #
#  what IBKR answers about an order, as ib_async records it
# --------------------------------------------------------------------------- #
class IbOrders:
    """The fake's orders kept the way ib_async keeps them, in its own Trade, OrderStatus and TradeLogEntry: a
    modify writes "Modify" in the trade's log, a cancel PendingCancel, a fill "Fill n@price" under whatever
    status the order has then, and an error on an order ib_async doesn't have down as finished marks it
    Cancelled with the error in the log - whatever IBKR meant, a refused cancel or modify too - before
    errorEvent hears of it, as ib_async's wrapper.error does (a warning only marks it ValidationError).
    IBKR answers a cancel with ``answer_cancel`` and a modify with ``answer_modify`` - (code, text) - or, when
    None, says nothing to a cancel and takes a modify: ib_async logs "Modified" for a Submitted order, and no word
    at all for one IBKR holds until its trigger (PreSubmitted), whose status the change leaves as it was. IBKR's list
    of the orders it works comes with its own status for each, which ib_async takes over: ``listed_as`` gives it
    for an order whose status here has gone wrong."""

    WARNINGS = {105, 110, 165, 321, 329, 399, 404, 434, 492, 10167}

    def __init__(self, ib):
        from ib_async import OrderStatus as Status, Trade
        from ib_async.objects import TradeLogEntry

        self.ib, self.book, self.closed, self.listed_as = ib, {}, set(), {}
        self.answer_cancel = self.answer_modify = None
        self._Trade, self._Status, self._Entry = Trade, Status, TradeLogEntry
        ib.placeOrder, ib.cancelOrder = self.place, self.cancel
        ib.trades = ib.openTrades = lambda: list(self.book.values())
        ib.reqAllOpenOrdersAsync = self._open_orders

    def _open_orders(self):
        # IBKR lists what it still works - an order ib_async took for cancelled among them - with its status
        listed = [t for oid, t in self.book.items() if oid not in self.closed]
        for t in listed:
            if t.order.orderId in self.listed_as:
                t.orderStatus.status = self.listed_as[t.order.orderId]
        return _Finished(listed)

    def _log(self, trade, status, message="", code=0):
        trade.log.append(self._Entry(dt.datetime.now(dt.timezone.utc), status, message, code))

    def place(self, contract, order):
        trade = self.book.get(order.orderId) if order.orderId else None
        if trade is None:
            order.orderId = len(self.book) + 1
            trade = self._Trade(contract, order, self._Status(orderId=order.orderId, status="Submitted"), [], [])
            self._log(trade, "Submitted")
            self.book[order.orderId] = trade
            return trade
        assert not trade.isDone()                     # ib_async's own check before it sends a modify
        self._log(trade, trade.orderStatus.status, "Modify")
        if self.answer_modify:
            self.error(order.orderId, *self.answer_modify)
        elif trade.orderStatus.status == "Submitted":
            self._log(trade, trade.orderStatus.status, "Modified")
        return trade

    def cancel(self, order):
        trade = self.book[order.orderId]
        if not trade.isDone():
            trade.orderStatus.status = "PendingCancel"
            self._log(trade, "PendingCancel")
        if self.answer_cancel:
            self.error(order.orderId, *self.answer_cancel)
        return trade

    def error(self, order_id, code, text):
        trade = self.book[order_id]
        if code in self.WARNINGS:
            trade.orderStatus.status = "ValidationError"
            self._log(trade, "ValidationError", f"Warning {code}, reqId {order_id}: {text}", code)
        elif not trade.isDone():
            trade.orderStatus.status = "Cancelled"
            self._log(trade, "Cancelled", f"Error {code}, reqId {order_id}: {text}", code)
        if code == 202:
            self.closed.add(order_id)
        self.ib.errorEvent.emit(order_id, code, text, None)

    def fill(self, order_id, shares, price):
        trade = self.book[order_id]
        trade.fills.append(SimpleNamespace(execution=SimpleNamespace(shares=shares, price=price)))
        self._log(trade, trade.orderStatus.status, f"Fill {shares}@{price}")

    def status(self, order_id, status, filled=0.0, avg=0.0):
        """IBKR's own order status."""
        trade = self.book[order_id]
        trade.orderStatus.status, trade.orderStatus.filled, trade.orderStatus.avgFillPrice = status, filled, avg
        if status in ("Filled", "Cancelled"):
            self.closed.add(order_id)
        self._log(trade, status)


def _resting_stop(broker, qty=10, price=98.5):
    return broker.place_order(OrderRequest(symbol="AAA", side=Side.SHORT, quantity=qty, order_type=OrderType.STOP,
                                           stop_price=price, tif=TimeInForce.GTC, is_entry=False,
                                           client_tag="stop:trd_1"))


@pytest.mark.parametrize("state,reads", [("PendingCancel", "WORKING"), ("Submitted", "WORKING"),
                                         ("PreSubmitted", "WORKING"), ("Filled", "WORKING"),
                                         ("Cancelled", "CANCELED")])
def test_a_cancel_ibkr_refuses_never_reads_as_cancelled(broker, caplog, state, reads):
    orders = IbOrders(broker._session.ib)
    res = _resting_stop(broker)
    orders.answer_cancel = (10148, f"OrderId {res.order_id} that needs to be cancelled cannot be cancelled, "
                                   f"state: {state}.")
    with caplog.at_level("INFO", logger=mod.__name__):
        broker.cancel_order(res.order_id)
    assert orders.book[1].orderStatus.status == "Cancelled"                   # ib_async's guess
    got = broker.get_order(res.order_id)
    assert (got.status, got.filled_qty) == (reads, 0.0)
    assert got.raw["cancel_confirmed"] is (reads == "CANCELED")
    if reads == "WORKING":
        assert any(r.levelname == "WARNING" and res.order_id in r.getMessage() and state in r.getMessage()
                   for r in caplog.records)                                    # the race shows in the log
        with pytest.raises(OrderRejected):
            broker.modify_stop(res.order_id, stop_price=99.0)                  # not until IBKR says what became of it


def test_a_stop_ibkr_had_filled_reads_filled_once_its_shares_arrive(broker):
    orders = IbOrders(broker._session.ib)
    res = _resting_stop(broker)
    orders.answer_cancel = (10148, "OrderId 1 that needs to be cancelled cannot be cancelled, state: Filled.")
    broker.cancel_order(res.order_id)
    orders.fill(1, 10.0, 98.4)                                                 # the execution lands first...
    assert broker.get_order(res.order_id).status == "WORKING"
    orders.status(1, "Filled", filled=10.0, avg=98.4)                          # ...then IBKR's own status
    got = broker.get_order(res.order_id)
    assert (got.status, got.filled_qty, got.avg_fill_price) == ("FILLED", 10.0, 98.4)


def test_a_refusal_naming_no_state_is_no_cancel_but_ibkrs_202_is(broker):
    orders = IbOrders(broker._session.ib)
    res = _resting_stop(broker)
    orders.answer_cancel = (161, "Cancel attempted when order is not in a cancellable state. Order permId =1234")
    broker.cancel_order(res.order_id)
    assert broker.get_order(res.order_id).status == "WORKING"
    orders.error(1, 202, "Order Canceled - reason:")                           # ib_async logs nothing: it's "done"
    assert orders.book[1].log[-1].errorCode == 161
    got = broker.get_order(res.order_id)
    assert got.status == "CANCELED" and got.raw["cancel_confirmed"] is True


def test_a_cancel_reads_confirmed_only_on_ibkrs_word(broker):
    orders = IbOrders(broker._session.ib)
    first, second, third = _resting_stop(broker), _resting_stop(broker), _resting_stop(broker)
    orders.answer_cancel = (202, "Order Canceled - reason:")
    broker.cancel_order(first.order_id)                                        # IBKR's 202
    orders.answer_cancel = None
    broker.cancel_order(second.order_id)
    orders.status(2, "Cancelled")                                              # IBKR's own order status
    orders.error(3, 201, "Order rejected - reason:no such order")              # an error ib_async took for the end
    confirmed = [broker.get_order(r.order_id).raw["cancel_confirmed"] for r in (first, second, third)]
    assert [broker.get_order(r.order_id).status for r in (first, second, third)] == ["CANCELED"] * 3
    assert confirmed == [True, True, False]


@pytest.mark.parametrize("code,text", [
    (201, "Order rejected - reason:The order would exceed a limit"),            # an error: ib_async says Cancelled
    (321, "Error validating request.-'bN' : cause - The order can't be changed"),   # a warning
])
def test_a_stop_move_ibkr_refuses_raises_and_the_stop_still_reads_working_where_it_was(broker, code, text):
    orders = IbOrders(broker._session.ib)
    res = _resting_stop(broker, price=98.5)
    broker.modify_stop(res.order_id, stop_price=99.0, quantity=10)            # taken
    assert broker.get_order(res.order_id).stop_price == 99.0
    orders.answer_modify = (code, text)
    with pytest.raises(OrderRejected, match="can't be changed|would exceed a limit"):
        broker.modify_stop(res.order_id, stop_price=99.5, quantity=10)
    got = broker.get_order(res.order_id)
    assert (got.status, got.stop_price, got.submitted_qty) == ("WORKING", 99.0, 10.0)   # what IBKR still holds


def test_a_stop_moved_without_a_size_keeps_the_shares_ibkr_holds(broker):
    orders = IbOrders(broker._session.ib)
    res = _resting_stop(broker, qty=10)
    trade = orders.book[1]
    trade.orderStatus.remaining = 7.0                    # IBKR's status: the target's group has cut it to seven...
    broker.modify_stop(res.order_id, stop_price=99.0)    # ...ahead of the order's own update
    assert (trade.order.totalQuantity, trade.order.auxPrice) == (7.0, 99.0)
    trade.order.totalQuantity = trade.orderStatus.remaining = 6.0     # the order's update, and IBKR's status
    got = broker.modify_stop(res.order_id, stop_price=99.5)
    assert (trade.order.totalQuantity, got.submitted_qty, got.stop_price) == (6.0, 6.0, 99.5)


def _moved_silently(broker, orders):
    """A stop IBKR holds until its trigger (PreSubmitted), moved: IBKR takes the change, and ib_async logs no word
    of it - so the move stays noted, for a refusal that may yet come."""
    res = _resting_stop(broker, price=98.5)
    orders.book[1].orderStatus.status = "PreSubmitted"
    broker.MODIFY_ANSWER_S = 0.05
    assert broker.modify_stop(res.order_id, stop_price=99.0).stop_price == 99.0
    assert res.order_id in broker._modifying
    return res


def _a_minute_on(broker, order_id):
    """As if the move noted for ``order_id`` had gone out a minute ago."""
    status, at, sent, before = broker._modifying[order_id]
    broker._modifying[order_id] = (status, at, sent - 60.0, before)


@pytest.mark.parametrize("a_minute_on,reads", [(False, "SUBMITTED"), (True, "CANCELED")])
def test_an_error_after_a_silent_stop_move_is_its_refusal_only_within_seconds_of_it(broker, a_minute_on, reads):
    orders = IbOrders(broker._session.ib)
    res = _moved_silently(broker, orders)
    if a_minute_on:
        _a_minute_on(broker, res.order_id)
    orders.error(1, 201, "Order rejected - reason: the stop could not be routed")
    got = broker.get_order(res.order_id)
    assert got.status == reads                         # PreSubmitted as it was - or ended: IBKR rejected it, triggered
    if a_minute_on:
        orders.answer_cancel = (161, "Cancel attempted when order is not in a cancellable state. Order permId =1")
        broker.cancel_order(res.order_id)                                      # a stand-down's cancel changes nothing
        assert broker.get_order(res.order_id).status == "CANCELED" and not got.raw["cancel_confirmed"]


def test_an_error_long_after_a_refused_stop_move_ends_the_stop(broker):
    orders = IbOrders(broker._session.ib)
    res = _resting_stop(broker, price=98.5)
    orders.answer_modify = (201, "Order rejected - reason: the order can't be changed")
    with pytest.raises(OrderRejected):
        broker.modify_stop(res.order_id, stop_price=99.0, quantity=10)
    assert broker.get_order(res.order_id).status == "WORKING"
    _a_minute_on(broker, res.order_id)
    orders.error(1, 201, "Order rejected - reason: the stop could not be routed")     # ib_async logs nothing: "done"
    assert broker.get_order(res.order_id).status == "CANCELED"


def test_a_stop_move_ibkr_refuses_after_its_silence_reads_back_where_ibkr_still_holds_the_stop(broker):
    orders = IbOrders(broker._session.ib)
    res = _moved_silently(broker, orders)                                      # 98.5 -> 99, and no word from IBKR
    orders.error(1, 201, "Order rejected - reason: the order can't be changed")   # its no, a moment late
    got = broker.get_order(res.order_id)
    assert (got.status, got.stop_price, got.submitted_qty) == ("SUBMITTED", 98.5, 10.0)


@pytest.mark.parametrize("code,text,held", [
    (399, "Order Message: SELL 10 AAA. Warning: your order will not be placed at the exchange until the open", 99.0),
    (321, "Error validating request.-'bN' : cause - The order can't be changed", 98.5),
])
def test_an_order_message_after_a_silent_stop_move_leaves_it_moved_and_only_a_no_puts_it_back(broker, code, text,
                                                                                            held):
    orders = IbOrders(broker._session.ib)
    res = _moved_silently(broker, orders)                                      # 98.5 -> 99, and no word from IBKR
    orders.error(1, code, text)                                                # a message on the change it took, or a no
    got = broker.get_order(res.order_id)
    assert (got.status, got.stop_price) == ("WORKING", held)


def test_a_stop_whose_move_ibkr_refused_late_is_in_doubt_until_its_list_of_orders_puts_ib_async_right(broker):
    orders = IbOrders(broker._session.ib)
    res = _moved_silently(broker, orders)
    orders.error(1, 201, "Order rejected - reason: the order can't be changed")   # its no, a moment late
    with pytest.raises(OrderInDoubt):                                          # ib_async has it down as cancelled
        broker.modify_stop(res.order_id, stop_price=99.0)
    orders.listed_as[1] = "PreSubmitted"
    broker.list_orders("WORKING")                                              # IBKR: still working, as it was
    assert broker.modify_stop(res.order_id, stop_price=99.0).stop_price == 99.0


def test_the_order_list_is_no_empty_one_while_ibkr_is_disconnected(broker):
    IbOrders(broker._session.ib)
    _resting_stop(broker)
    broker._connected = False                                                  # IBKR's servers lost (1100)
    with pytest.raises(AuthError):                                             # not known - never "nothing works"
        broker.list_orders("WORKING")


def test_a_move_of_a_stop_ibkr_has_cancelled_is_refused_not_held_in_doubt(broker):
    orders = IbOrders(broker._session.ib)
    res = _resting_stop(broker)
    orders.status(1, "Cancelled")                                              # IBKR's own word
    with pytest.raises(OrderRejected) as refused:
        broker.modify_stop(res.order_id, stop_price=99.0)
    assert not isinstance(refused.value, OrderInDoubt)


def test_ibkrs_full_list_of_its_orders_settles_a_stop_move_it_answered_with_an_error(broker):
    orders = IbOrders(broker._session.ib)
    broker.MODIFY_ANSWER_S = 0.05
    for oid, price in ((1, 98.5), (2, 97.5)):
        _resting_stop(broker, price=price)
        orders.book[oid].orderStatus.status = "PreSubmitted"
        broker.modify_stop(str(oid), stop_price=99.0)                         # IBKR says nothing...
        orders.error(oid, 201, "Order rejected - reason: the stop could not be routed")   # ...then: a no, or its end?
    assert [broker.get_order(o).status for o in ("1", "2")] == ["SUBMITTED", "SUBMITTED"]
    orders.closed.add(1)                                                       # IBKR no longer works the first
    broker.list_orders("WORKING")
    assert [broker.get_order(o).status for o in ("1", "2")] == ["CANCELED", "SUBMITTED"]   # ended, and working


def test_moving_a_stop_ibkr_holds_until_its_trigger_waits_only_a_moment_for_a_word_that_never_comes(broker):
    orders = IbOrders(broker._session.ib)
    res = _resting_stop(broker, price=98.5)
    orders.book[1].orderStatus.status = "PreSubmitted"                         # IBKR takes a change to it silently
    broker.MODIFY_ANSWER_S, broker.MODIFY_QUIET_S = 5.0, 0.05
    began = time.monotonic()
    assert broker.modify_stop(res.order_id, stop_price=99.0).stop_price == 99.0
    assert time.monotonic() - began < 2.0                                      # the order sync isn't held up
    assert res.order_id in broker._modifying                                   # a refusal may yet come


def _rebuilt(orders, contract, perm, status, fills=(), **order):
    """What ib_async holds after a reconnect for an order that finished while the connection was down: the order
    rebuilt from IBKR's list of finished orders, with no order id (0) and nothing in its log - its permId, and the
    executions the connect's request for them hands it."""
    from ib_async import Order

    trade = orders._Trade(contract, Order(orderId=0, permId=perm, **order), orders._Status(orderId=0, status=status),
                          list(fills), [])
    orders.book[f"perm:{perm}"] = trade
    return trade


def _execution(order_id, shares, price, client_id):
    return SimpleNamespace(execution=SimpleNamespace(orderId=order_id, clientId=client_id, shares=shares, price=price))


def test_a_stop_cancelled_while_the_connection_was_down_is_found_by_its_permid_on_ibkrs_word(broker):
    orders = IbOrders(broker._session.ib)
    res = _resting_stop(broker)
    contract = orders.book[1].contract
    orders.book[1].order.permId = 9001                         # IBKR gives the order its permId...
    assert broker.get_order(res.order_id).status == "WORKING"   # ...noted when the app reads it
    orders.book.clear()                                         # the connection drops: ib_async starts afresh
    _rebuilt(orders, contract, 9001, "Cancelled", action="SELL", totalQuantity=10, orderType="STP", auxPrice=98.5,
             orderRef="stop:trd_1")
    got = broker.get_order(res.order_id)
    assert (got.order_id, got.status, got.tag, got.raw["cancel_confirmed"]) == (res.order_id, "CANCELED", "stop:trd_1",
                                                                                True)
    with pytest.raises(OrderRejected, match="finished"):
        broker.cancel_order(res.order_id)                       # no cancel goes out for order 0


def test_an_entry_that_filled_while_the_connection_was_down_is_found_by_its_executions(broker):
    orders = IbOrders(broker._session.ib)
    res = broker.place_order(OrderRequest(symbol="AAA", side=Side.LONG, quantity=10, order_type=OrderType.LIMIT,
                                          limit_price=100.0, client_tag="play_t01"))
    contract, me = orders.book[1].contract, broker.client_id
    orders.book.clear()                                         # dropped before the app read its permId
    _rebuilt(orders, contract, 9002, "Filled", [_execution(1, 5.0, 7.0, me + 1)], action="BUY",
             totalQuantity=5)                                   # another client's order 1: not the app's
    _rebuilt(orders, contract, 9001, "Filled", [_execution(1, 6.0, 99.9, me), _execution(1, 4.0, 100.0, me)],
             action="BUY", totalQuantity=10, orderType="LMT", lmtPrice=100.0, orderRef="play_t01")
    got = broker.get_order(res.order_id)
    assert (got.order_id, got.status, got.filled_qty, got.tag) == ("1", "FILLED", 10.0, "play_t01")
    assert got.avg_fill_price == pytest.approx(99.94) and [f.order_id for f in got.fills] == ["1", "1"]
    assert "1" in [o.order_id for o in broker.list_orders("FILLED")]           # listed under its own id too



# --------------------------------------------------------------------------- #
#  the open orders, asked for by several threads at once
# --------------------------------------------------------------------------- #
class ThreadedSession(mod._IBSession):
    """The real session - its asyncio loop on a thread of its own - around the fake IB, for what
    happens when the app's threads ask at the same time."""

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self.ib = FakeIB()
        self._ready.set()
        self._loop.run_forever()


class _CountingLock:
    """A lock that counts the callers that have been through it."""

    def __init__(self):
        self._lock, self.entered = threading.Lock(), 0

    def __enter__(self):
        self._lock.acquire()
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self._lock.release()


def _wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.01)


@pytest.fixture
def threaded(monkeypatch):
    monkeypatch.setattr(mod, "port_is_open", lambda *a, **k: True)

    async def _fast(*_a):
        return None

    monkeypatch.setattr(mod, "_sleep", _fast)
    b = mod.IbkrBroker(port=4002, mode="paper", session_factory=ThreadedSession)
    b.connect()
    for symbol in ("AAA", "BBB"):
        b.place_order(OrderRequest(symbol=symbol, side=Side.LONG, quantity=5, order_type=OrderType.LIMIT,
                                   limit_price=10.0))
    yield b
    b.close()


def test_threads_asking_for_the_open_orders_at_once_share_one_request(threaded):
    # ib_async keeps one slot for this request: a second one sent while the first is out would leave
    # the first waiting for an answer that never comes
    ib, asked = threaded._session.ib, []

    def answered_later():                       # like ib_async's: a future the loop resolves when the list ends
        asked.append(asyncio.get_running_loop().create_future())
        return asked[-1]

    ib.reqAllOpenOrdersAsync = answered_later
    threaded._orders_lock = counting = _CountingLock()
    got = {}
    callers = [threading.Thread(target=lambda n=n: got.__setitem__(n, threaded.list_orders("WORKING")))
               for n in range(2)]
    for c in callers:
        c.start()
    _wait_for(lambda: counting.entered >= 2 and asked)          # both callers are in and a request is out
    threaded._session.call(lambda ib: asked[0].set_result(ib.trades()))
    for c in callers:
        c.join(timeout=5)
    assert len(asked) == 1
    assert [sorted(o.symbol for o in got[n]) for n in range(2)] == [["AAA", "BBB"]] * 2
    # the answer is spent once both callers have it: the next call asks afresh and sees a newer order
    assert threaded._orders_asked is None
    threaded.place_order(OrderRequest(symbol="CCC", side=Side.LONG, quantity=5, order_type=OrderType.LIMIT,
                                      limit_price=10.0))
    later = threading.Thread(target=lambda: got.__setitem__("later", threaded.list_orders("WORKING")))
    later.start()
    _wait_for(lambda: len(asked) == 2)
    threaded._session.call(lambda ib: asked[1].set_result(ib.trades()))
    later.join(timeout=5)
    assert sorted(o.symbol for o in got["later"]) == ["AAA", "BBB", "CCC"]


def test_open_orders_that_never_arrive_raise_and_leave_nothing_waiting_on_the_loop(threaded, monkeypatch):
    monkeypatch.setattr(mod.IbkrBroker, "OPEN_ORDERS_TIMEOUT_S", 0.3)
    ib, asked = threaded._session.ib, []
    answer = ib.reqAllOpenOrdersAsync

    def never_then_at_once():
        asked.append(asyncio.get_running_loop().create_future() if not asked else None)
        return asked[-1] if len(asked) == 1 else answer()

    ib.reqAllOpenOrdersAsync = never_then_at_once
    with pytest.raises(BrokerError, match="didn't arrive"):       # unknown - never an empty list
        threaded.list_orders("WORKING")
    assert asked[0].cancelled()                                   # the request was called off...
    assert threaded._session.call(lambda ib: [t for t in asyncio.all_tasks() if not t.done()]) == []   # ...not left
    assert sorted(o.symbol for o in threaded.list_orders("WORKING")) == ["AAA", "BBB"]
    assert len(asked) == 2                                        # the next call asked afresh


def test_a_request_that_outlasts_its_wait_is_cancelled_on_the_loop():
    session = ThreadedSession()
    session.start()
    cancelled = threading.Event()

    async def forever(ib):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    try:
        with pytest.raises(FutureTimeout):
            session.run_coro(forever, timeout=0.2)
        assert cancelled.wait(timeout=5)
    finally:
        session.stop()


# --------------------------------------------------------------------------- #
#  real-time streams
# --------------------------------------------------------------------------- #
FULL = dict(bid=10.0, ask=10.02, last=10.01)          # a bid, an ask and a trade: a quote a stream can be trusted on


def _tick(broker, symbol, **fields):
    """A packet for ``symbol``'s Ticker, handed on as ib_async does: the fields set, then the Ticker stamped and
    pendingTickersEvent."""
    ib = broker._session.ib
    tk = ib.ticker(broker._contracts[symbol])
    for name, value in fields.items():
        setattr(tk, name, value)
    ib.ticks_arrived(tk)
    return tk


def test_the_first_symbols_stream_in_order_and_the_others_are_ended(broker):
    ib = broker._session.ib
    assert broker.can_stream
    assert broker.set_streams(["AAA", "BBB", "AAA", "CCC", "DDD"], 3) == ["AAA", "BBB", "CCC"]
    assert ib.subscribed == ["AAA", "BBB", "CCC"]
    assert broker.set_streams(["AAA", "BBB", "CCC"], 3) == ["AAA", "BBB", "CCC"]
    assert ib.subscribed == ["AAA", "BBB", "CCC"] and ib.cancelled == []          # the same again sends nothing
    assert broker.set_streams(["DDD", "AAA"], 3) == ["DDD", "AAA"]
    assert ib.subscribed[-1] == "DDD" and sorted(ib.cancelled) == ["BBB", "CCC"]
    assert set(ib.wrapper.ticker2ReqId["mktData"]) == {broker._streams[s].ticker for s in ("DDD", "AAA")}
    assert broker.set_streams(["DDD", "AAA"], 0) == [] and not ib.wrapper.ticker2ReqId["mktData"]


def test_new_streams_come_twenty_at_a_time_and_never_more_than_ninety(broker):
    ib = broker._session.ib
    symbols = [f"T{i:03d}" for i in range(120)]
    for _ in range(6):
        asked = len(ib.subscribed)
        held = broker.set_streams(symbols, 200)
        assert len(ib.subscribed) - asked <= broker.STREAM_ADDS_PER_CALL
    assert held == symbols[:90] and len(ib.subscribed) == 90
    assert max(len(batch) for batch in ib.qualified) <= broker.STREAM_ADDS_PER_CALL


def test_a_stock_ibkr_has_no_contract_for_is_left_to_snapshots(broker):
    ib = broker._session.ib
    assert broker.set_streams(["NOPE", "AAA"], 1) == []
    assert broker.set_streams(["NOPE", "AAA"], 1) == ["AAA"]                    # its line goes to the next one
    assert ib.qualified[1:] == [["NOPE"], ["AAA"]]                              # after the connect probe's


def test_a_new_stream_publishes_nothing_until_it_has_a_full_quote(broker):
    moved = []
    broker.on_tick = moved.append
    broker.set_streams(["AAA"], 1)
    _tick(broker, "AAA", close=9.5)                               # yesterday's close comes first
    _tick(broker, "AAA", bid=10.0, ask=10.02)                     # a book, but no trade yet
    assert broker.streamed_quote("AAA") is None and moved == []
    _tick(broker, "AAA", last=10.01)
    q, age = broker.streamed_quote("AAA")
    assert (q.bid, q.ask, q.last, q.source) == (10.0, 10.02, 10.01, "stream") and 0 <= age < 5
    assert moved == [frozenset({"AAA"})]


def test_a_stream_asked_for_again_ignores_the_prices_its_ticker_held_before(broker):
    broker.set_streams(["AAA"], 1)
    _tick(broker, "AAA", **FULL)
    broker.set_streams([], 1)                                     # ended - but ib_async keeps the Ticker
    assert broker.set_streams(["AAA"], 1) == ["AAA"]
    _tick(broker, "AAA", close=9.5)
    assert broker.streamed_quote("AAA") is None                   # the old bid, ask and trade don't count
    _tick(broker, "AAA", bid=10.5, ask=10.52, last=10.51)
    assert broker.streamed_quote("AAA")[0].last == 10.51


def test_a_streamed_quote_is_the_quote_a_snapshot_gives(broker):
    ib = broker._session.ib
    broker.set_streams(["AAA"], 1)
    _tick(broker, "AAA", bid=100.0, ask=100.1, last=100.05, volume=1234.0)

    async def snapshot(c):
        # a snapshot of the stock shares its Ticker: its answer is a packet the stream hears too
        return [_tick(broker, "AAA", last=float("nan"), close=99.9)]          # IBKR cleared the last trade

    ib.reqTickersAsync = snapshot
    snap = broker.get_quote("AAA")
    streamed, _ = broker.streamed_quote("AAA")
    assert streamed == snap and streamed.last == 99.9             # the same fallbacks: no trade -> the close
    assert (streamed.source, snap.source) == ("stream", "snapshot")


def test_one_side_of_the_book_and_no_trade_is_no_price(broker):
    broker._session.ib.snapshot = dict(bid=-1.0, ask=100.1, last=float("nan"), close=float("nan"), volume=0.0)
    with pytest.raises(RuntimeError, match="no price"):
        broker.get_quote("AAA")


@pytest.mark.parametrize("code", [101, 10089])
def test_a_refused_snapshot_never_passes_off_an_ended_streams_prices(broker, code):
    # IBKR refused it (every line in use, or not subscribed), yet ib_async hands back the stock's Ticker all the
    # same - still holding what the stream left on it
    ib = broker._session.ib
    broker.set_streams(["AAA"], 1)
    _tick(broker, "AAA", **FULL)
    broker.set_streams([], 1)
    ib.refuse_snapshots = code
    with pytest.raises(RuntimeError, match="no price"):
        broker.get_quote("AAA")
    assert ib.ticker(broker._contracts["AAA"]).last == 10.01                     # the old prices are still there


def test_a_refused_snapshot_never_passes_off_an_earlier_snapshots_prices(broker):
    ib = broker._session.ib
    first = broker.get_quote("AAA")
    assert (first.bid, first.ask, first.last, first.source) == (100.0, 100.1, 99.9, "snapshot")
    ib.snapshot = dict(bid=101.0, ask=101.1, last=101.05)
    assert broker.get_quote("AAA").last == 101.05                                # each answer is read afresh
    ib.refuse_snapshots = 101
    with pytest.raises(RuntimeError, match="no price"):
        broker.get_quote("AAA")                                                   # not the 101.05 left on the Ticker
    ib.refuse_snapshots = 0
    again = broker.get_quote("AAA")
    assert (again.last, again.source) == (101.05, "snapshot")


@pytest.mark.parametrize("step_s", [0, -2])
def test_a_snapshot_is_read_though_the_wall_clock_stamped_it_no_later_than_the_last_ticks(broker, step_s):
    # ib_async stamps a packet with datetime.now(): on a coarse clock an answer that comes back at once shares the
    # last packet's time, and a clock set back gives it an earlier one - it answered all the same
    ib = broker._session.ib
    broker.set_streams(["AAA"], 1)
    at = _tick(broker, "AAA", **FULL).time
    ib.clock = lambda: at + dt.timedelta(seconds=step_s)                         # a new datetime each packet
    ib.snapshot = dict(bid=14.0, ask=14.02, last=14.01)
    q = broker.get_quote("AAA")
    assert (q.bid, q.ask, q.last, q.source) == (14.0, 14.02, 14.01, "snapshot")
    ib.refuse_snapshots = 101
    with pytest.raises(RuntimeError, match="no price"):
        broker.get_quote("AAA")                                                   # nothing came: still no price


def test_a_refused_snapshot_is_priced_as_a_failed_one_from_the_quote_kept_or_the_latest_candle(broker, tmp_path):
    from autotradebot.data.bars import DailyBarStore
    from autotradebot.data.market_data import _QUOTE_TTL_S, MarketData

    ib, md = broker._session.ib, MarketData(DailyBarStore(tmp_path))
    md.attach(broker)
    ib.snapshot = dict(bid=11.0, ask=11.02, last=11.01)
    kept = md.quote("AAA")
    assert (kept.last, kept.source) == (11.01, "snapshot")
    assert md.streams.sync(["AAA"], [], 1) == ["AAA"]
    _tick(broker, "AAA", **FULL)                                                  # 10.01 - then the stream ends
    assert md.streams.sync([], [], 1) == []
    ib.refuse_snapshots = 101
    assert md.quote("AAA") is kept                                                # not the stream's 10.01
    at, q = md._quotes["AAA"]
    md._quotes["AAA"] = (at - _QUOTE_TTL_S, q)                                    # too old to serve
    candle = md.quote("AAA")
    assert candle.source == "" and candle.last == pytest.approx(102.1)           # the latest one-minute candle's close
    assert ib.history_requests[-1][1]["barSizeSetting"] == "1 min"


def test_delayed_ticks_are_ignored_and_a_halt_takes_the_price_away(broker):
    broker.set_streams(["AAA"], 1)
    _tick(broker, "AAA", marketDataType=3, **FULL)
    assert broker.streamed_quote("AAA") is None
    _tick(broker, "AAA", marketDataType=1)
    assert broker.streamed_quote("AAA")[0].last == 10.01
    _tick(broker, "AAA", halted=1.0)
    assert broker.streamed_quote("AAA") is None


def test_nothing_streams_before_the_connect_probe_is_through_or_on_frozen_data(monkeypatch):
    monkeypatch.setattr(mod, "port_is_open", lambda *a, **k: True)

    async def _fast(*_a):
        return None

    monkeypatch.setattr(mod, "_sleep", _fast)
    seen = []
    probe = mod.IbkrBroker._check_data_entitlement

    def probing(self):
        seen.append((self.is_connected, self.can_stream, self.set_streams(["AAA"], 1)))
        probe(self)

    monkeypatch.setattr(mod.IbkrBroker, "_check_data_entitlement", probing)
    auto = mod.IbkrBroker(port=4002, mode="paper", session_factory=FakeSession)
    auto.connect()
    assert seen == [(True, False, [])] and auto._session.ib.subscribed == [] and auto.can_stream
    frozen = mod.IbkrBroker(port=4002, mode="paper", market_data="frozen", session_factory=FakeSession)
    frozen.connect()
    assert not frozen.can_stream and frozen.set_streams(["AAA"], 1) == [] and frozen._session.ib.subscribed == []
    live = mod.IbkrBroker(port=4002, mode="paper", market_data="live", session_factory=FakeSession)
    live.connect()
    assert live.set_streams(["AAA"], 1) == ["AAA"]                # real-time data chosen outright streams


def test_a_competing_session_ends_every_stream(broker):
    ib = broker._session.ib
    broker.set_streams(["AAA", "BBB"], 2)
    _tick(broker, "AAA", **FULL)
    broker._on_error(broker._streams["AAA"].req_id, 10197, "No market data during competing live session", None)
    assert broker._data_is_delayed and not broker.can_stream
    assert sorted(ib.cancelled) == ["AAA", "BBB"] and broker.streamed_quote("AAA") is None
    asked = len(ib.subscribed)
    assert broker.set_streams(["AAA", "BBB"], 2) == [] and len(ib.subscribed) == asked


def test_a_refused_snapshot_still_turns_the_whole_app_delayed(broker):
    ib = broker._session.ib
    broker.set_streams(["AAA"], 1)
    broker._on_error(7, 354, "Requested market data is not subscribed.", None)       # not a stream's request
    assert broker._data_is_delayed and ib.market_data_type == 3
    assert ib.cancelled == ["AAA"] and not broker.can_stream


def test_error_101_gives_lines_back_from_the_end_once_per_burst(broker):
    ib = broker._session.ib
    broker._on_error(5, 101, "Max number of tickers has been reached", None)         # nothing streaming: as before
    assert broker._stream_cap == broker.STREAM_LINES_MAX and broker._last_error.startswith("101")
    symbols = [f"T{i:02d}" for i in range(1, 26)]
    broker.set_streams(symbols, 25)
    broker.set_streams(symbols, 25)
    ids = {s: broker._streams[s].req_id for s in symbols}
    broker._on_error(ids["T25"], 101, "Max number of tickers has been reached", None)
    # refused, so forgotten without a cancel; then ten more lines given back from the end of the list
    assert "T25" not in ib.cancelled and ids["T25"] not in ib.wrapper.ticker2ReqId["mktData"].values()
    assert broker._stream_cap == 14 and sorted(ib.cancelled) == symbols[14:24]
    assert list(broker._streams) == symbols[:14]
    # the rest of the burst - refusals of requests sent before the cut, a snapshot's among them - cuts no more
    for req_id in (ids["T24"], ids["T20"], ids["T03"], 7):
        broker._on_error(req_id, 101, "Max number of tickers has been reached", None)
    assert broker._stream_cap == 14 and "T03" not in ib.cancelled
    assert list(broker._streams) == [s for s in symbols[:14] if s != "T03"]
    assert broker.set_streams(symbols, 25) == symbols[:14]                            # asked again, within the cap
    # a request sent after the cut is news: the lines are still short
    broker._on_error(ib.client._reqIdSeq, 101, "Max number of tickers has been reached", None)
    assert broker._stream_cap == 4 and sorted(broker._streams) == symbols[:4]


def test_error_101_cuts_the_watch_tier_first_and_never_the_positions(broker, monkeypatch):
    ib, symbols = broker._session.ib, [f"T{i:02d}" for i in range(1, 26)]
    broker.set_streams(symbols, 25, protect=3)                                        # three positions first
    broker.set_streams(symbols, 25, protect=3)
    broker._on_error(broker._streams["T25"].req_id, 101, "Max number of tickers has been reached", None)
    caps = [broker._stream_cap]
    while True:                                   # every request sent after a cut is refused again: news each time
        broker._on_error(ib.client._reqIdSeq, 101, "Max number of tickers has been reached", None)
        if broker._stream_cap == caps[-1]:
            break
        caps.append(broker._stream_cap)
    assert caps == [14, 4, 3] and list(broker._streams) == symbols[:3]              # the tail went, the positions stay
    assert broker.set_streams(symbols, 25, protect=3) == symbols[:3]

    # a new connection has every line again, and nothing held yet to keep
    monkeypatch.setattr(broker, "_start_reconnect", lambda: None)
    ib.disconnect()
    broker._on_disconnect()
    ib.wrapper.reset()
    broker._do_connect()
    assert broker._stream_protect == 0 and broker._stream_cap == broker.STREAM_LINES_MAX


def test_error_101_while_a_resync_looks_up_contracts_never_cuts_a_position_still_held(broker, monkeypatch):
    ib, symbols = broker._session.ib, [f"T{i:02d}" for i in range(1, 26)]
    broker.set_streams(symbols, 25, protect=3)
    broker.set_streams(symbols, 25, protect=3)
    broker._on_error(broker._streams["T25"].req_id, 101, "Max number of tickers has been reached", None)
    for _ in range(3):                                   # down to the three positions
        broker._on_error(ib.client._reqIdSeq, 101, "Max number of tickers has been reached", None)
    assert list(broker._streams) == symbols[:3] and broker._stream_cap == 3
    # T01's position closes: the next resync leads with T02 and T03 and first looks up a new name's contract -
    # and the lines are refused again meanwhile, before the streams have followed the new order
    qualify = ib.qualifyContractsAsync

    async def refused_meanwhile(*contracts):
        broker._on_error(ib.client._reqIdSeq, 101, "Max number of tickers has been reached", None)
        return await qualify(*contracts)

    monkeypatch.setattr(ib, "qualifyContractsAsync", refused_meanwhile)
    cancelled = len(ib.cancelled)
    assert broker.set_streams(["T02", "T03", "T30"], 25, protect=2) == ["T02", "T03", "T30"]
    assert ib.cancelled[cancelled:] == ["T01"]              # the closed one's stream ended; T03's never did
    assert broker._stream_cap == 3 and broker._stream_protect == 2


def test_a_stock_refused_a_stream_is_quoted_by_snapshot_and_only_a_burst_turns_the_data_delayed(broker):
    ib = broker._session.ib
    symbols = [f"T{i:02d}" for i in range(1, 8)]
    broker.set_streams(symbols, 7)
    ids = {s: broker._streams[s].req_id for s in symbols}
    broker._on_error(ids["T01"], 354, "Requested market data is not subscribed.", None)
    assert not broker._data_is_delayed and broker.can_stream                          # still real-time data
    assert "T01" not in broker._streams and len(broker._streams) == 6 and ib.cancelled == []
    assert broker.set_streams(symbols, 7) == symbols[1:]                              # snapshots for a while
    # a late answer about a stream already ended changes nothing
    broker._on_error(ids["T01"], 354, "Requested market data is not subscribed.", None)
    broker._on_error(ids["T01"], 300, "Can't find EId with tickerId", None)
    assert not broker._data_is_delayed and broker._last_error == ""
    # five refused within a minute is the login's data, not a stock or two
    for s in symbols[1:4]:
        broker._on_error(ids[s], 10089, "Requested market data requires additional subscription for API.", None)
    assert not broker._data_is_delayed and len(broker._streams) == 3
    broker._on_error(ids["T05"], 10089, "Requested market data requires additional subscription for API.", None)
    assert broker._data_is_delayed and not broker._streams and not broker.can_stream


def test_a_stream_refused_only_in_part_is_cancelled_so_its_line_comes_back(broker):
    ib = broker._session.ib
    broker.set_streams(["AAA", "BBB"], 2)
    broker._on_error(broker._streams["AAA"].req_id, 10090, "Part of requested market data is not subscribed.", None)
    broker._on_error(broker._streams["BBB"].req_id, 354, "Requested market data is not subscribed.", None)
    assert ib.cancelled == ["AAA"] and not broker._streams and not ib.wrapper.ticker2ReqId["mktData"]
    assert not broker._data_is_delayed


@pytest.mark.parametrize("reset_first", [True, False])
def test_a_dropped_connection_forgets_the_streams_without_a_word_to_ibkr(broker, monkeypatch, reset_first):
    monkeypatch.setattr(broker, "_start_reconnect", lambda: None)
    ib, symbols = broker._session.ib, [f"T{i:02d}" for i in range(1, 26)]
    broker.set_streams(symbols, 25)
    broker.set_streams(symbols, 25)
    # on this connection error 101 cut the lines to 14, and four stocks were refused a stream
    broker._on_error(broker._streams["T25"].req_id, 101, "Max number of tickers has been reached", None)
    for s in symbols[:4]:
        broker._on_error(broker._streams[s].req_id, 354, "Requested market data is not subscribed.", None)
    assert broker._stream_cap == 14 and sorted(broker._unstreamable) == symbols[:4] and not broker._data_is_delayed
    _tick(broker, "T05", **FULL)
    cancelled = list(ib.cancelled)
    ib.disconnect()
    # ib_async forgets its requests before or after it says so, depending on which end closed the socket
    if reset_first:
        ib.wrapper.reset()
    broker._on_disconnect()
    ib.wrapper.reset()
    assert ib.cancelled == cancelled and not broker._streams and not broker._latest and not broker._stream_reqs
    assert broker.streamed_quote("T05") is None and broker.set_streams(symbols, 25) == []

    # the new connection streams nothing until its own probe is through...
    seen, probe = [], broker._check_data_entitlement

    def probing():
        seen.append((broker.can_stream, broker.set_streams(symbols, 25)))
        probe()

    monkeypatch.setattr(broker, "_check_data_entitlement", probing)
    asked = len(ib.subscribed)
    broker._do_connect()
    assert seen == [(False, [])] and len(ib.subscribed) == asked
    # ...then has every line again, the stocks refused on the last one included, all asked for afresh
    broker.set_streams(symbols, 25)
    assert broker.set_streams(symbols, 25) == symbols and ib.subscribed[asked:] == symbols
    # its request ids start again, and IBKR's answers are about this connection's requests: a 101 cuts the lines
    # afresh, and a stock refused is that stock's alone - the refusals on the last connection don't count
    broker._on_error(broker._streams["T25"].req_id, 101, "Max number of tickers has been reached", None)
    assert broker._stream_cap == 14 and list(broker._streams) == symbols[:14]
    broker._on_error(broker._streams["T01"].req_id, 354, "Requested market data is not subscribed.", None)
    assert list(broker._streams) == symbols[1:14] and "T01" in broker._unstreamable
    assert not broker._data_is_delayed and broker.can_stream


def test_after_ibkrs_servers_come_back_the_streams_are_asked_for_again_unless_ibkr_kept_them(broker, monkeypatch):
    monkeypatch.setattr(broker, "_start_reconnect", lambda: None)
    ib = broker._session.ib
    broker.set_streams(["AAA"], 1)
    _tick(broker, "AAA", **FULL)
    broker._on_error(-1, 1100, "Connectivity between IBKR and Trader Workstation has been lost.")
    assert broker.streamed_quote("AAA") is None and broker.set_streams(["AAA"], 1) == []
    assert "AAA" in broker._streams and ib.cancelled == []                            # kept, in case of a 1102
    broker._on_error(-1, 1102, "Connectivity between IBKR and Trader Workstation has been restored - data maintained.")
    assert broker.streamed_quote("AAA") is not None
    assert broker.set_streams(["AAA"], 1) == ["AAA"] and ib.subscribed == ["AAA"]
    for lost_first in (True, False):
        if lost_first:
            broker._on_error(-1, 1100, "Connectivity between IBKR and Trader Workstation has been lost.")
        broker._on_error(-1, 1101, "Connectivity between IBKR and Trader Workstation has been restored - data lost.")
        assert broker.streamed_quote("AAA") is None
        assert broker.set_streams(["AAA"], 1) == ["AAA"]
    assert ib.subscribed == ["AAA"] * 3


def test_a_stream_waits_for_a_snapshot_of_the_same_stock_to_finish(broker):
    ib = broker._session.ib
    tk = ib.wrapper.startTicker(7, broker._contract("AAA"), "snapshot")               # a snapshot is out
    assert broker.set_streams(["AAA"], 1) == [] and ib.subscribed == []
    ib.wrapper.endTicker(tk, "snapshot")                                               # and answered
    assert broker.set_streams(["AAA"], 1) == ["AAA"] and broker._streams["AAA"].ticker is tk


def test_a_request_left_on_the_ticker_is_cancelled_before_a_new_one(broker):
    ib = broker._session.ib
    ib.wrapper.startTicker(8, broker._contract("AAA"), "mktData")                     # left over
    broker.set_streams(["AAA"], 1)
    assert ib.cancelled == ["AAA"] and ib.subscribed == ["AAA"]
    assert list(ib.wrapper.ticker2ReqId["mktData"].values()) == [broker._streams["AAA"].req_id]


def test_a_snapshot_quote_is_read_on_the_loop_thread(threaded, monkeypatch):
    # a stream of the same stock writes to the same Ticker on the loop: read anywhere else, a quote could tear
    read_on = []
    real = mod._quote_from_ticker

    def reading(*args):
        read_on.append(threading.current_thread().name)
        return real(*args)

    monkeypatch.setattr(mod, "_quote_from_ticker", reading)
    assert threaded.get_quote("AAA").source == "snapshot"
    assert read_on == ["ibkr-loop"]


def test_streams_change_from_another_thread_while_ticks_arrive(threaded):
    ib, symbols = threaded._session.ib, [f"T{i:02d}" for i in range(1, 31)]
    seen = []
    threaded.on_tick = lambda moved: seen.extend(threaded.streamed_quote(s) for s in moved)   # on the loop
    stop, failed = threading.Event(), []

    def packet(ib, n):
        live = list(ib.wrapper.ticker2ReqId["mktData"])
        for tk in live:
            tk.bid, tk.ask, tk.last = 10.0 + n / 100, 10.02 + n / 100, 10.01 + n / 100
            tk.time = dt.datetime.now(dt.timezone.utc)
        ib.pendingTickersEvent.emit(set(live))

    def ticking():
        n = 0
        try:
            while not stop.is_set():
                n += 1
                threaded._session.call(lambda ib: packet(ib, n), timeout=5)
        except Exception as e:  # noqa: BLE001
            failed.append(e)

    def resyncing():
        try:
            for k in range(30):
                threaded.set_streams(symbols[k % 10:], 20)
        except Exception as e:  # noqa: BLE001
            failed.append(e)

    workers = [threading.Thread(target=ticking, daemon=True), threading.Thread(target=resyncing, daemon=True)]
    for w in workers:
        w.start()
    workers[1].join(timeout=20)
    stop.set()
    workers[0].join(timeout=5)
    assert not any(w.is_alive() for w in workers) and failed == []
    assert seen and all(got is not None for got in seen)
    # nothing leaked: every request ib_async holds is a stream the adapter knows about
    streams = threaded._streams
    assert {ib.wrapper.ticker2ReqId["mktData"][st.ticker] for st in streams.values()} == set(threaded._stream_reqs)
    assert len(ib.wrapper.ticker2ReqId["mktData"]) == len(streams) == 20
