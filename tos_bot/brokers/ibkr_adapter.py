"""Interactive Brokers - paper and live accounts, orders and market data, via ``ib_async``.

``ib_async`` talks to a running IB Gateway (or Trader Workstation):

    app          paper port   live port
    IB Gateway   4002         4001
    TWS          7497         7496

There's no token and no expiry: the Gateway login is the authentication, and
IBKR restarts the Gateway once a day (IBC can log it back in unattended). The
adapter reconnects with backoff whenever the socket drops.

All ``ib_async`` work runs on one asyncio loop in its own thread
(:class:`_IBSession`); the public methods block, so the rest of the app stays
synchronous.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import re
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from ..config import get_settings
from ..core.enums import OrderType, Side, TimeInForce
from ..core.models import Account, Fill, OrderRequest, OrderResult, Position, Quote
from ..util.net import port_is_open
from .base import AuthError, BrokerAdapter, OrderRejected

log = logging.getLogger(__name__)

_MARKET_DATA_TYPES = {"live": 1, "frozen": 2, "delayed": 3, "delayed-frozen": 4}
# error codes meaning "no real-time subscription" (354 / 10168: not subscribed)
_DELAYED_ERRS = {10167, 10168, 10197, 10089, 354}
# connection chatter, not failures
_INFO_ERRS = {2104, 2106, 2107, 2108, 2158, 2100, 2150, 202}


class _QuietDataErrors(logging.Filter):
    """ib_async logs every "not subscribed" reply as an ERROR. _on_error handles
    those and explains them once, so keep them out of the console."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(msg.startswith(f"Error {code},") for code in _DELAYED_ERRS)


logging.getLogger("ib_async.wrapper").addFilter(_QuietDataErrors())


class _IBSession:
    """One asyncio loop on its own thread, owning the ``ib_async.IB`` client."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self.ib = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="ibkr-loop", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise AuthError("IBKR event loop failed to start")

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            from ib_async import IB

            self.ib = IB()
        except Exception:  # noqa: BLE001
            log.exception("could not create ib_async.IB()")
        finally:
            self._ready.set()
        self._loop.run_forever()

    def run_coro(self, factory: Callable[[Any], Any], timeout: float = 30.0):
        """``factory(ib)`` returns an awaitable; block for its result."""
        if self._loop is None:
            raise AuthError("IBKR loop not started")
        return asyncio.run_coroutine_threadsafe(factory(self.ib), self._loop).result(timeout=timeout)

    def call(self, fn: Callable[[Any], Any], timeout: float = 15.0):
        """``fn(ib)`` is a plain call run on the loop thread."""
        done: Future = Future()

        def _run() -> None:
            try:
                done.set_result(fn(self.ib))
            except Exception as e:  # noqa: BLE001
                done.set_exception(e)

        self._loop.call_soon_threadsafe(_run)
        return done.result(timeout=timeout)

    def stop(self) -> None:
        try:
            if self.ib is not None and self.ib.isConnected():
                self.call(lambda ib: ib.disconnect(), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)


class IbkrBroker(BrokerAdapter):
    name = "ibkr"
    # The ExitManager owns exits and sends real close orders. No native bracket
    # is attached at IBKR - two exit managers on one position fight, and a
    # resting child can outlive the position. The cost: no stop rests at IBKR
    # while the app isn't running.
    supports_bracket_native = False

    #: IBKR paces historical requests; about six at a time runs ~10 symbols a second
    HISTORY_CONCURRENCY = 6
    DETAILS_CONCURRENCY = 16

    def __init__(
        self,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        client_id: Optional[int] = None,
        account_id: Optional[str] = None,
        market_data: Optional[str] = None,
        readonly: Optional[bool] = None,
        mode: str = "live",
        session_factory: Optional[Callable[[], _IBSession]] = None,
        fx_fn: Optional[Callable[[str], Optional[float]]] = None,
    ) -> None:
        s = get_settings().secrets
        self.mode = mode                                  # which IBKR login: paper | live
        self.host = host or s.ibkr_host or "127.0.0.1"
        self.port = int(port or s.ibkr_port_for(mode))
        self.client_id = int(client_id if client_id is not None else s.ibkr_client_id)
        self.account_id = (account_id if account_id is not None else s.ibkr_account_id) or ""
        self.readonly = bool(s.ibkr_readonly if readonly is None else readonly)
        self._md_pref = (market_data or s.ibkr_market_data or "auto").lower()
        self._data_type = _MARKET_DATA_TYPES.get(self._md_pref, 1)
        self._data_is_delayed = self._data_type in (3, 4)
        self._fx_fn = fx_fn
        self._fx_warned = False

        self._session_factory = session_factory or _IBSession
        self._session: Optional[_IBSession] = None
        self._connected = False
        self._want_connected = False
        self._reconnecting = False
        self._contracts: Dict[str, Any] = {}              # symbol -> qualified Contract
        self._last_error = ""
        self._lock = threading.RLock()

    # ---- connection --------------------------------------------------- #
    @property
    def _ib(self):
        return self._session.ib if self._session else None

    def connect(self) -> None:
        try:
            import ib_async  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise AuthError("ib_async is not installed - `pip install ib_async` (Python 3.10+).") from e
        if not port_is_open(self.host, self.port):
            raise AuthError(
                f"nothing is listening on {self.host}:{self.port} - start IB "
                f"{'Gateway' if self.port in (4001, 4002) else 'TWS'} ({self.mode} port {self.port}) "
                "with the API enabled. Guide: python scripts/ibkr_setup.py --guide")
        self._want_connected = True
        if self._session is None:
            self._session = self._session_factory()
            self._session.start()
        self._do_connect(first=True)

    def _do_connect(self, first: bool = False) -> None:
        async def _connect(ib):
            await ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=8,
                                  readonly=self.readonly)
        self._session.run_coro(_connect, timeout=15)
        if first:
            try:
                self._ib.disconnectedEvent += self._on_disconnect
                self._ib.errorEvent += self._on_error
            except Exception:  # noqa: BLE001
                pass
        try:
            self._session.call(lambda ib: ib.reqMarketDataType(self._data_type), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        # connectAsync has already synced account values and positions; give the
        # first portfolio updates a moment to land
        try:
            self._session.run_coro(lambda ib: _sleep(1.0), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        self._connected = True
        if not self.account_id:
            try:
                accounts = list(self._session.call(lambda ib: ib.managedAccounts(), timeout=5) or [])
                self.account_id = accounts[0] if accounts else ""
            except Exception:  # noqa: BLE001
                pass
        self._check_data_entitlement()
        log.info("IBKR connected  %s:%s  account=%s  data=%s", self.host, self.port,
                 self.account_id or "?", "delayed" if self._data_is_delayed else "live")

    def _check_data_entitlement(self) -> None:
        """One quick live quote at connect. Without a real-time subscription IBKR
        refuses at once, which switches to delayed data (see _on_error), so the
        dashboard's data label is right from the start."""
        if self._md_pref != "auto" or self._data_is_delayed:
            return
        try:
            probe = self._contract("SPY")             # IBKR only refuses a qualified contract
            self._session.run_coro(lambda ib: ib.reqTickersAsync(probe), timeout=4)
            self._session.run_coro(lambda ib: _sleep(0.3), timeout=2)    # let the refusal land
        except Exception:  # noqa: BLE001
            pass

    @property
    def quotes_from_bars(self) -> bool:
        """Without real-time data a quote is a slow delayed snapshot, so prices
        are read off the latest candles instead."""
        return self._data_is_delayed

    @property
    def is_connected(self) -> bool:
        try:
            return bool(self._connected and self._ib is not None and self._ib.isConnected())
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        self._want_connected = False
        if self._session is not None:
            self._session.stop()
        self._connected = False

    def _on_disconnect(self) -> None:
        self._connected = False
        if self._want_connected:
            log.warning("IBKR session dropped - reconnecting")
            self._start_reconnect()

    def _start_reconnect(self) -> None:
        with self._lock:
            if self._reconnecting:
                return
            self._reconnecting = True
        threading.Thread(target=self._reconnect_loop, name="ibkr-reconnect", daemon=True).start()

    def _reconnect_loop(self) -> None:
        delay = 5
        try:
            while self._want_connected and not self.is_connected:
                if port_is_open(self.host, self.port, timeout=2):
                    try:
                        self._do_connect()
                        log.info("IBKR reconnected")
                        return
                    except Exception as e:  # noqa: BLE001
                        self._last_error = f"reconnect: {e}"
                time.sleep(delay)
                delay = min(120, delay * 2)
        finally:
            self._reconnecting = False

    def refresh_if_needed(self) -> bool:
        """Nudge a reconnect when the socket has dropped (the daily Gateway restart)."""
        if not self.is_connected and self._want_connected:
            self._start_reconnect()
        return self.is_connected

    def session_status(self) -> Dict[str, Any]:
        return {
            "connected": self.is_connected,
            "reconnecting": self._reconnecting,
            "host": self.host, "port": self.port, "mode": self.mode,
            "account": self.account_id or None,
            "market_data": "delayed" if self._data_is_delayed else "live",
            "readonly": self.readonly,
            "message": ("connected" if self.is_connected
                        else "reconnecting - Gateway restarting?" if self._reconnecting
                        else f"IB Gateway not reachable on {self.host}:{self.port}"),
            "last_error": self._last_error,
        }

    def _on_error(self, reqId, errorCode, errorString, contract=None) -> None:  # noqa: ANN001
        if errorCode in _INFO_ERRS:
            return
        if errorCode in _DELAYED_ERRS:
            if not self._data_is_delayed:
                log.warning("IBKR: no real-time market-data subscription - using delayed data")
            self._data_is_delayed = True
            self._data_type = 3
            try:
                # this runs on the IB loop thread, so call directly: session.call()
                # would wait on this same thread and time out
                self._ib.reqMarketDataType(3)
            except Exception:  # noqa: BLE001
                pass
            return
        if errorCode in (1100, 1300, 2110):
            self._connected = False
        self._last_error = f"{errorCode}: {errorString}"
        if errorCode not in (162, 200):        # historical-data / unknown-contract noise
            log.debug("IBKR error %s: %s", errorCode, errorString)

    # ---- contracts ------------------------------------------------------ #
    def _contract(self, symbol: str):
        contract = self._contracts.get(symbol)
        if contract is not None:
            return contract
        from ib_async import Stock

        stock = Stock(symbol, "SMART", "USD")
        try:
            qualified = self._session.run_coro(lambda ib: ib.qualifyContractsAsync(stock), timeout=10)
            contract = qualified[0] if qualified else stock
        except Exception:  # noqa: BLE001
            contract = stock
        self._contracts[symbol] = contract
        return contract

    @staticmethod
    def _contract_for_history(symbol: str, con_id: Optional[int]):
        from ib_async import Contract, Stock

        return Contract(conId=con_id, exchange="SMART") if con_id else Stock(symbol, "SMART", "USD")

    # ---- account ---------------------------------------------------------- #
    def get_account(self) -> Account:
        """Balances converted to USD - US stocks are priced in dollars - whatever
        the account's own currency (an IBKR Canada account is in CAD). The
        account-currency figures are in ``raw["base"]``; with no exchange rate
        the USD figures are 0, so nothing is sized off a guess."""
        if not self.is_connected:
            raise AuthError("IBKR not connected")
        acct = self.account_id or ""
        # accountValues() is the stream ib_async subscribes to at connect. Not
        # accountSummary(): called on the loop thread it tries to run the loop.
        try:
            values = list(self._session.call(lambda ib: ib.accountValues(acct), timeout=8) or [])
        except Exception:  # noqa: BLE001
            values = []
        base = next((v.currency for v in values
                     if v.tag == "NetLiquidation" and v.currency not in ("", "BASE")), "USD")
        summary: Dict[str, str] = {}
        base_per_usd = 0.0
        for v in values:
            if v.currency == base:
                summary[v.tag] = v.value
            elif v.tag == "ExchangeRate" and v.currency == "USD":
                base_per_usd = _num(v.value)             # 1 USD = this many units of base

        def first(*tags: str) -> float:
            return next((x for x in (_num(summary.get(t)) for t in tags) if x), 0.0)

        in_base = {"equity": first("NetLiquidation", "EquityWithLoanValue"),
                   "cash": first("TotalCashValue", "CashBalance", "AvailableFunds"),
                   "buying_power": first("BuyingPower", "AvailableFunds", "ExcessLiquidity")}
        if base == "USD":
            usd_per_base: Optional[float] = 1.0
        elif base_per_usd > 0:
            usd_per_base = 1.0 / base_per_usd
        else:
            usd_per_base = (self._fx_fn or _default_fx)(base)
        k = float(usd_per_base or 0.0)
        if not k and not self._fx_warned:
            self._fx_warned = True
            log.warning("IBKR account is in %s and no USD exchange rate was found - "
                        "no trade can be sized until there is one", base)

        try:
            portfolio = self._session.call(lambda ib: ib.portfolio(acct), timeout=8) or []
        except Exception:  # noqa: BLE001
            portfolio = []
        positions = [Position(symbol=getattr(it.contract, "symbol", "?"), quantity=float(it.position),
                              avg_price=float(it.averageCost or 0.0), market_price=float(it.marketPrice or 0.0))
                     for it in portfolio if abs(float(getattr(it, "position", 0.0) or 0.0)) > 1e-9]
        return Account(
            account_id=str(acct or "ibkr"),
            equity=round(in_base["equity"] * k, 2),
            cash=round(in_base["cash"] * k, 2),
            buying_power=round(in_base["buying_power"] * k, 2),
            positions=positions,
            base_currency=base,
            usd_per_base=k,
            raw={"base": in_base, "fx_missing": not k, "delayed_data": self._data_is_delayed},
        )

    # ---- market data ------------------------------------------------------ #
    def get_quote(self, symbol: str) -> Quote:
        if not self.is_connected:
            raise AuthError("IBKR not connected")
        contract = self._contract(symbol)

        async def _ticker(ib):
            tickers = await ib.reqTickersAsync(contract)
            return tickers[0] if tickers else None

        tk = self._session.run_coro(_ticker, timeout=12)
        if tk is None:
            raise RuntimeError(f"IBKR returned no ticker for {symbol}")
        last = _price(tk.last) or _price(tk.close) or _price(getattr(tk, "marketPrice", None))
        bid, ask = _price(tk.bid), _price(tk.ask)
        if not (last or bid or ask):
            raise RuntimeError(f"IBKR has no price for {symbol} "
                               f"({'delayed' if self._data_is_delayed else 'real-time'} data)")
        spread = max(0.01, (last or bid or ask) * 0.0005)
        bid = bid or round(last - spread, 2)
        ask = ask or round(last + spread, 2)
        return Quote(symbol=symbol, bid=bid, ask=ask, last=last or (bid + ask) / 2, volume=_price(tk.volume))

    def history_many(self, requests: Mapping[str, Tuple[str, str]],
                     con_ids: Optional[Mapping[str, int]] = None,
                     timeout: float = 30.0, end: Optional[dt.datetime] = None) -> Dict[str, pd.DataFrame]:
        """Candles for many symbols at once. ``requests`` maps a symbol to
        (bar size, duration), e.g. ("1 day", "1 Y") or ("5 mins", "5 D"), ending at
        ``end`` (default: now). Symbols IBKR has nothing for are left out of the result."""
        if not self.is_connected:
            raise AuthError("IBKR not connected")
        if not requests:
            return {}
        con_ids = con_ids or {}

        async def one(ib, gate: asyncio.Semaphore, symbol: str, bar: str, duration: str):
            async with gate:
                try:
                    bars = await ib.reqHistoricalDataAsync(
                        self._contract_for_history(symbol, con_ids.get(symbol)), endDateTime=end or "",
                        durationStr=duration, barSizeSetting=bar, whatToShow="TRADES", useRTH=True,
                        formatDate=2, keepUpToDate=False, timeout=timeout)
                except Exception:  # noqa: BLE001
                    return symbol, None
                return symbol, _bars_to_df(bars) if bars else None

        async def run(ib):
            gate = asyncio.Semaphore(self.HISTORY_CONCURRENCY)
            return await asyncio.gather(*(one(ib, gate, s, bar, dur) for s, (bar, dur) in requests.items()))

        budget = timeout * (len(requests) / self.HISTORY_CONCURRENCY + 1) + 30
        return {s: f for s, f in self._session.run_coro(run, timeout=budget) if f is not None and len(f)}

    def contract_details_many(self, symbols: Sequence[str], timeout: float = 20.0) -> Dict[str, Optional[dict]]:
        """Contract id, primary exchange, stock type and IBKR's industry / category
        for each symbol. A symbol IBKR has no stock for maps to None; one whose
        lookup failed is left out, so it's asked about again later."""
        if not self.is_connected:
            raise AuthError("IBKR not connected")
        from ib_async import Stock

        async def one(ib, gate: asyncio.Semaphore, symbol: str):
            async with gate:
                try:
                    found = await asyncio.wait_for(ib.reqContractDetailsAsync(Stock(symbol, "SMART", "USD")), timeout)
                except Exception:  # noqa: BLE001
                    return symbol, None, False
            if not found:
                return symbol, None, True
            d = found[0]
            return symbol, {"con_id": int(d.contract.conId), "exchange": d.contract.primaryExchange or "",
                            "stock_type": d.stockType or "", "industry": d.industry or "",
                            "category": d.category or ""}, True

        async def run(ib):
            gate = asyncio.Semaphore(self.DETAILS_CONCURRENCY)
            return await asyncio.gather(*(one(ib, gate, s) for s in symbols))

        budget = timeout * (len(symbols) / self.DETAILS_CONCURRENCY + 1) + 30
        return {s: d for s, d, answered in self._session.run_coro(run, timeout=budget) if answered}

    # ---- orders ------------------------------------------------------------ #
    def _guard_orders(self) -> None:
        if self.readonly:
            raise OrderRejected("IBKR adapter is in read-only mode (IBKR_READONLY=1)")
        if not self.is_connected:
            raise AuthError("IBKR not connected")

    def place_order(self, req: OrderRequest) -> OrderResult:
        self._guard_orders()
        from ib_async import LimitOrder, MarketOrder

        contract = self._contract(req.symbol)
        action = "BUY" if req.side is Side.LONG else "SELL"      # the side is the order's direction, exits too
        qty = abs(float(req.quantity))
        order = (MarketOrder(action, qty) if req.order_type is OrderType.MARKET or not req.limit_price
                 else LimitOrder(action, qty, float(req.limit_price)))
        order.tif = _tif(req.tif)
        order.outsideRth = req.session in ("EXTENDED", "SEAMLESS")
        order.orderRef = req.client_tag            # lets a restarted app recognise its own working orders
        if self.account_id:
            order.account = self.account_id
        trade = self._session.call(lambda ib: ib.placeOrder(contract, order), timeout=10)
        oid = str(getattr(trade.order, "orderId", "") or getattr(trade.order, "permId", ""))
        status = getattr(trade.orderStatus, "status", "") or "Submitted"
        return OrderResult(order_id=oid, status=_norm_status(status), symbol=req.symbol,
                           submitted_qty=req.quantity, raw={"tif": order.tif}, side=req.side, tag=req.client_tag)

    def _find_trade(self, order_id: str):
        def _find(ib):
            return next((t for t in ib.trades() if str(getattr(t.order, "orderId", "")) == str(order_id)), None)
        return self._session.call(_find, timeout=8)

    def cancel_order(self, order_id: str) -> None:
        self._guard_orders()
        trade = self._find_trade(order_id)
        if trade is None:
            raise OrderRejected(f"IBKR: no live order {order_id} to cancel")
        self._session.call(lambda ib: ib.cancelOrder(trade.order), timeout=8)

    def get_order(self, order_id: str) -> OrderResult:
        if not self.is_connected:
            raise AuthError("IBKR not connected")
        t = self._find_trade(order_id)
        if t is None:
            return OrderResult(order_id=str(order_id), status="UNKNOWN", symbol="?", submitted_qty=0.0)
        return self._result(t)

    def list_orders(self, status: Optional[str] = None) -> List[OrderResult]:
        if not self.is_connected:
            return []
        if status in (None, "OPEN", "WORKING"):
            # every open order on the account, including ones an earlier run of the app left working.
            # reqAllOpenOrdersAsync hands back a future, not a coroutine, so it is awaited in one.
            async def open_orders(ib):
                return await ib.reqAllOpenOrdersAsync()
            trades = self._session.run_coro(open_orders, timeout=15) or []
        else:
            trades = self._session.call(lambda ib: list(ib.trades()), timeout=8) or []
        results = [self._result(t) for t in trades]
        return [r for r in results if not status or status.upper() in (r.status, "OPEN", "WORKING")]

    def _result(self, t) -> OrderResult:
        o, os_ = t.order, t.orderStatus
        side = Side.LONG if o.action == "BUY" else Side.SHORT
        fills = [Fill(order_id=str(o.orderId), symbol=t.contract.symbol, side=side,
                      quantity=float(f.execution.shares), price=float(f.execution.price))
                 for f in (t.fills or [])]
        return OrderResult(order_id=str(o.orderId), status=_norm_status(os_.status), symbol=t.contract.symbol,
                           submitted_qty=float(o.totalQuantity or 0.0), filled_qty=float(os_.filled or 0.0),
                           avg_fill_price=float(os_.avgFillPrice or 0.0), fills=fills, message=_order_message(t),
                           side=side, tag=getattr(o, "orderRef", "") or "",
                           raw={"mine": getattr(o, "clientId", None) == self.client_id})


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _num(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) else 0.0


def _price(v: Any) -> float:
    """IBKR marks a missing price as NaN or -1."""
    x = _num(v)
    return x if x > 0 else 0.0


def _default_fx(currency: str) -> Optional[float]:
    from ..data.fx import usd_per

    return usd_per(currency)


def _tif(tif: TimeInForce) -> str:
    return {TimeInForce.DAY: "DAY", TimeInForce.GTC: "GTC",
            TimeInForce.IOC: "IOC", TimeInForce.FOK: "FOK"}.get(tif, "DAY")


def _norm_status(s: str) -> str:
    """IBKR's order states in the app's words. Cancelled, ApiCancelled and Inactive
    are final (an order IBKR rejects arrives as Cancelled); ValidationError is only
    a warning on an order that's still working."""
    s = (s or "").upper()
    return {
        "PENDINGSUBMIT": "SUBMITTED", "PRESUBMITTED": "SUBMITTED", "APIPENDING": "PENDING",
        "SUBMITTED": "WORKING", "FILLED": "FILLED", "CANCELLED": "CANCELED",
        "APICANCELLED": "CANCELED", "PENDINGCANCEL": "WORKING", "INACTIVE": "REJECTED",
        "VALIDATIONERROR": "WORKING", "APIUPDATE": "WORKING",
    }.get(s, s or "SUBMITTED")


def _order_message(trade) -> str:
    """IBKR's latest complaint about an order - why it was rejected, say."""
    for entry in reversed(getattr(trade, "log", None) or []):
        if getattr(entry, "errorCode", 0):
            text = re.sub(r"^(Error|Warning) -?\d+, reqId -?\d+: ", "", entry.message or "")
            return " ".join(text.replace("<br>", " ").split())
    return ""


def _bars_to_df(bars) -> pd.DataFrame:
    """ib_async bars -> an OHLCV frame on a New York DatetimeIndex. Daily bars
    arrive as session dates and sit at midnight New York time; intraday bars
    arrive as UTC times. IBKR reports US stock volume in shares."""
    df = pd.DataFrame([{"date": b.date, "open": float(b.open), "high": float(b.high), "low": float(b.low),
                        "close": float(b.close), "volume": float(getattr(b, "volume", 0.0) or 0.0)}
                       for b in bars])
    if isinstance(bars[0].date, dt.datetime):
        df.index = pd.DatetimeIndex(pd.to_datetime(df["date"], utc=True)).tz_convert("America/New_York")
    else:
        df.index = pd.DatetimeIndex(pd.to_datetime(df["date"])).tz_localize("America/New_York")
    return df[["open", "high", "low", "close", "volume"]].dropna().sort_index()
