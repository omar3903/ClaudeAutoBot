"""Interactive Brokers adapter — live **and** paper, via ``ib_async``.

`ib_async` (the maintained fork of `ib_insync`) talks to a running
**IB Gateway** or **Trader Workstation** on ``127.0.0.1``:

    | app         | paper port | live port |
    |-------------|-----------|-----------|
    | IB Gateway  | 4002      | 4001      |
    | TWS         | 7497      | 7496      |

There is **no OAuth token and no 60-day expiry** — the "auth" is the Gateway
login, which IBKR force-restarts once a day (and fully once a week). Make that
hands-off with **IBC** (https://github.com/IbcAlpha/IBC), which relaunches
Gateway and re-enters your stored login. This adapter watches the socket and
**auto-reconnects** with backoff whenever the session drops, so an IBC restart
is invisible; if the Gateway is genuinely down it reports that loudly instead.

All `ib_async` work happens on one dedicated asyncio-loop thread
(:class:`_IBSession`); the public methods are plain blocking calls, so the rest
of the engine stays synchronous and unaware of asyncio.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import socket
import threading
import time
from concurrent.futures import Future as _CFuture
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from ..config import get_settings
from ..core.enums import AssetClass, OrderType, Side, TimeInForce
from ..core.models import Account, Fill, OrderRequest, OrderResult, Position, Quote
from .base import AuthError, BrokerAdapter, NotSupported, OrderRejected

log = logging.getLogger(__name__)

# our interval label -> IBKR barSizeSetting
_BAR_SIZE = {
    "1m": "1 min", "5m": "5 mins", "10m": "10 mins", "15m": "15 mins",
    "30m": "30 mins", "1h": "1 hour", "1d": "1 day", "1wk": "1 week",
}
# IBKR market-data-type codes
_MDT = {"live": 1, "frozen": 2, "delayed": 3, "delayed-frozen": 4}
# error codes that mean "no real-time entitlement, delivering delayed instead"
_DELAYED_ERRS = {10167, 10168, 10197, 10089}
# error codes that are just connection chatter, not failures
_INFO_ERRS = {2104, 2106, 2107, 2108, 2158, 2100, 2150, 202}


def port_is_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Cheap 'is IB Gateway/TWS listening?' check with no ib_async import."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
#  one asyncio loop, on its own thread, owning the IB() client                #
# --------------------------------------------------------------------------- #
class _IBSession:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._up = threading.Event()
        self.ib = None  # ib_async.IB

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="ibkr-loop", daemon=True)
        self._thread.start()
        if not self._up.wait(timeout=10):
            raise AuthError("IBKR event loop failed to start")

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            from ib_async import IB, util
            try:
                util.logToConsole(logging.WARNING)
            except Exception:  # noqa: BLE001
                pass
            self.ib = IB()
        except Exception:  # noqa: BLE001
            log.exception("could not create ib_async.IB()")
        finally:
            self._up.set()
        self._loop.run_forever()

    def run_coro(self, factory: Callable[[Any], Any], timeout: float = 30.0):
        """`factory(ib)` returns an awaitable; block for its result."""
        if self._loop is None:
            raise AuthError("IBKR loop not started")
        fut = asyncio.run_coroutine_threadsafe(factory(self.ib), self._loop)
        return fut.result(timeout=timeout)

    def call(self, fn: Callable[[Any], Any], timeout: float = 15.0):
        """`fn(ib)` is a plain (non-async) call executed on the loop thread."""
        cf: _CFuture = _CFuture()

        def _cb() -> None:
            try:
                cf.set_result(fn(self.ib))
            except Exception as e:  # noqa: BLE001
                cf.set_exception(e)

        self._loop.call_soon_threadsafe(_cb)
        return cf.result(timeout=timeout)

    def stop(self) -> None:
        try:
            if self.ib is not None and self.ib.isConnected():
                self.call(lambda ib: ib.disconnect(), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)


# --------------------------------------------------------------------------- #
class IbkrBroker(BrokerAdapter):
    name = "ibkr"
    asset_classes = (AssetClass.EQUITY, AssetClass.ETF, AssetClass.OPTION, AssetClass.FUTURE)
    supports_shorting = True
    supports_fractional = True
    # The tuned automatic exit strategy (ExitManager: stop/target/break-even/
    # R-trail/EOD-flatten) owns exits and sends real close orders. We do NOT
    # also attach a native OCA bracket at IBKR - two managers on one position
    # fight, and a resting child can outlive the position. Trade-off: if this
    # process dies there is no stop resting at IBKR. Keep the bot up (IBC keeps
    # the Gateway up) and the ExitManager polls every ~4s.
    supports_bracket_native = False

    def __init__(
        self,
        token_manager: Any = None,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        client_id: Optional[int] = None,
        account_id: Optional[str] = None,
        market_data: Optional[str] = None,
        readonly: Optional[bool] = None,
        mode: str = "live",
        session_factory: Optional[Callable[[], _IBSession]] = None,
    ) -> None:
        s = get_settings().secrets
        self.mode = mode                              # "paper" | "live" (which IBKR account)
        self.host = host or s.ibkr_host or "127.0.0.1"
        if port:
            self.port = int(port)
        elif s.ibkr_port:
            self.port = int(s.ibkr_port)
        else:
            self.port = int(s.ibkr_live_port if mode == "live" else s.ibkr_paper_port)
        self.client_id = int(client_id if client_id is not None else s.ibkr_client_id)
        self.account_id = (account_id if account_id is not None else s.ibkr_account_id) or ""
        self.readonly = bool(s.ibkr_readonly if readonly is None else readonly)
        self._md_pref = (market_data or s.ibkr_market_data or "auto").lower()

        # IBKR paper is a *real* brokerage paper account, not our simulator, so
        # paper=False: fills, PDT reporting and buying power all come from IBKR.
        self.paper = False
        self.token_manager = token_manager           # unused - IBKR has no token

        self._session_factory = session_factory or _IBSession
        self._session: Optional[_IBSession] = None
        self._connected = False
        self._want_connected = False
        self._reconnecting = False
        self._data_type = _MDT.get(self._md_pref, 1) if self._md_pref != "auto" else 1
        self._data_is_delayed = self._data_type in (3, 4)
        self._contracts: Dict[str, Any] = {}         # symbol -> qualified Contract
        self._last_error: str = ""
        self._lock = threading.RLock()

    # -- connection ------------------------------------------------------ #
    @property
    def _ib(self):
        return self._session.ib if self._session else None

    def connect(self) -> None:
        try:
            import ib_async  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise AuthError(
                "ib_async is not installed. `pip install ib_async` (needs Python 3.10+), "
                "then start IB Gateway / TWS. See scripts/ibkr_setup.py."
            ) from e

        if not port_is_open(self.host, self.port):
            raise AuthError(
                f"nothing is listening on {self.host}:{self.port} - start "
                f"IB {'Gateway' if self.port in (4001, 4002) else 'TWS'} "
                f"({'live' if self.mode == 'live' else 'paper'} port {self.port}) "
                f"with the API enabled, or run IBC. Guide: python scripts/ibkr_setup.py"
            )

        self._want_connected = True
        if self._session is None:
            self._session = self._session_factory()
            self._session.start()
        self._do_connect(initial=True)

    def _do_connect(self, initial: bool = False) -> None:
        async def _c(ib):
            await ib.connectAsync(
                self.host, self.port, clientId=self.client_id,
                timeout=8, readonly=self.readonly,
            )
        self._session.run_coro(_c, timeout=15)

        ib = self._ib
        if initial:
            # wire the connection-health + entitlement listeners once
            try:
                ib.disconnectedEvent += self._on_disconnect
                ib.errorEvent += self._on_error
            except Exception:  # noqa: BLE001
                pass

        # market-data mode
        try:
            self._session.call(lambda ib: ib.reqMarketDataType(self._data_type), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        # let the initial account / portfolio snapshot arrive
        try:
            self._session.run_coro(lambda ib: ib.reqAccountSummaryAsync(), timeout=10)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._session.run_coro(lambda ib: _sleep(1.0), timeout=5)
        except Exception:  # noqa: BLE001
            pass

        self._connected = True
        accts = []
        try:
            accts = list(self._session.call(lambda ib: ib.managedAccounts(), timeout=5) or [])
        except Exception:  # noqa: BLE001
            pass
        if not self.account_id and accts:
            self.account_id = accts[0]
        log.info("IBKR connected  %s:%s  account=%s  data=%s",
                 self.host, self.port, self.account_id or "?",
                 "delayed" if self._data_is_delayed else "live")

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

    # -- auto-reconnect (covers the daily IBC-driven Gateway restart) ---- #
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
        delay, cap = 5, 120
        try:
            while self._want_connected and not self.is_connected:
                if port_is_open(self.host, self.port, timeout=2):
                    try:
                        self._do_connect(initial=False)
                        log.info("IBKR reconnected")
                        return
                    except Exception as e:  # noqa: BLE001
                        self._last_error = f"reconnect: {e}"
                time.sleep(delay)
                delay = min(cap, delay * 2)
        finally:
            self._reconnecting = False

    def refresh_if_needed(self, margin_s: int = 120) -> bool:
        """Called by the engine's watchdog. For IBKR this just nudges a
        reconnect when the socket has dropped; there is no token to refresh."""
        if not self.is_connected and self._want_connected:
            self._start_reconnect()
        return self.is_connected

    def session_status(self) -> Dict[str, Any]:
        return {
            "broker": "ibkr",
            "connected": self.is_connected,
            "reconnecting": self._reconnecting,
            "host": self.host, "port": self.port, "mode": self.mode,
            "account": self.account_id or None,
            "market_data": "delayed" if self._data_is_delayed else "live",
            "readonly": self.readonly,
            "message": (
                "connected" if self.is_connected
                else "reconnecting - Gateway restarting?" if self._reconnecting
                else f"IB Gateway not reachable on {self.host}:{self.port}"
            ),
            "last_error": self._last_error,
        }

    # -- entitlement / error listener ---------------------------------- #
    def _on_error(self, reqId, errorCode, errorString, contract=None) -> None:  # noqa: ANN001
        if errorCode in _INFO_ERRS:
            return
        if errorCode in _DELAYED_ERRS:
            if not self._data_is_delayed:
                log.warning("IBKR: no real-time market-data entitlement - using delayed feed")
            self._data_is_delayed = True
            self._data_type = 3
            try:
                self._session.call(lambda ib: ib.reqMarketDataType(3), timeout=5)
            except Exception:  # noqa: BLE001
                pass
            return
        if errorCode in (1100, 1300, 2110):
            self._connected = False
        self._last_error = f"{errorCode}: {errorString}"
        if errorCode not in (162, 200, 354):     # hist-data / contract noise
            log.debug("IBKR error %s: %s", errorCode, errorString)

    # -- contracts ---------------------------------------------------- #
    def _contract(self, symbol: str):
        c = self._contracts.get(symbol)
        if c is not None:
            return c
        from ib_async import Stock

        stk = Stock(symbol.upper(), "SMART", "USD")
        try:
            qs = self._session.run_coro(lambda ib: ib.qualifyContractsAsync(stk), timeout=10)
            c = qs[0] if qs else stk
        except Exception:  # noqa: BLE001
            c = stk
        self._contracts[symbol] = c
        return c

    # -- account ---------------------------------------------------- #
    def get_account(self) -> Account:
        if not self.is_connected:
            raise AuthError("IBKR not connected")
        ib = self._ib
        acct = self.account_id or ""

        summ = {}
        try:
            for av in self._session.call(lambda ib: ib.accountSummary(acct or ""), timeout=8) or []:
                summ[av.tag] = av.value
        except Exception:  # noqa: BLE001
            pass
        if not summ:
            try:
                for av in self._session.call(lambda ib: ib.accountValues(acct or ""), timeout=8) or []:
                    if av.currency in ("", "USD", "BASE"):
                        summ[av.tag] = av.value
            except Exception:  # noqa: BLE001
                pass

        def _f(*tags: str) -> float:
            for t in tags:
                v = summ.get(t)
                if v not in (None, ""):
                    try:
                        return float(v)
                    except ValueError:
                        pass
            return 0.0

        positions: List[Position] = []
        try:
            port = self._session.call(lambda ib: ib.portfolio(acct or ""), timeout=8) or []
        except Exception:  # noqa: BLE001
            port = []
        for it in port:
            q = float(getattr(it, "position", 0.0) or 0.0)
            if abs(q) < 1e-9:
                continue
            positions.append(Position(
                symbol=getattr(it.contract, "symbol", "?"), quantity=q,
                avg_price=float(getattr(it, "averageCost", 0.0) or 0.0),
                market_price=float(getattr(it, "marketPrice", 0.0) or 0.0),
            ))
        if not positions:
            try:
                for p in self._session.call(lambda ib: ib.positions(acct or ""), timeout=8) or []:
                    q = float(p.position or 0.0)
                    if abs(q) < 1e-9:
                        continue
                    positions.append(Position(symbol=p.contract.symbol, quantity=q,
                                              avg_price=float(p.avgCost or 0.0),
                                              market_price=float(p.avgCost or 0.0)))
            except Exception:  # noqa: BLE001
                pass

        equity = _f("NetLiquidation", "NetLiquidationByCurrency", "EquityWithLoanValue")
        return Account(
            account_id=str(acct or "ibkr"),
            equity=equity,
            cash=_f("TotalCashValue", "CashBalance", "AvailableFunds"),
            buying_power=_f("BuyingPower", "AvailableFunds", "ExcessLiquidity"),
            day_trade_buying_power=_f("DayTradesRemaining") and 0.0 or 0.0,
            is_cash_account=False,
            positions=positions,
            raw={"summary": summ, "delayed_data": self._data_is_delayed},
        )

    # -- market data --------------------------------------------- #
    def get_quote(self, symbol: str) -> Quote:
        if not self.is_connected:
            raise AuthError("IBKR not connected")
        c = self._contract(symbol)

        async def _q(ib):
            tks = await ib.reqTickersAsync(c)
            return tks[0] if tks else None

        tk = self._session.run_coro(_q, timeout=12)
        if tk is None:
            raise RuntimeError(f"IBKR returned no ticker for {symbol}")

        def _n(x):
            try:
                x = float(x)
                return x if not math.isnan(x) else 0.0
            except (TypeError, ValueError):
                return 0.0

        last = _n(tk.last) or _n(tk.close) or _n(getattr(tk, "marketPrice", None))
        bid, ask = _n(tk.bid), _n(tk.ask)
        if not bid and last:
            bid = round(last - max(0.01, last * 0.0005), 2)
        if not ask and last:
            ask = round(last + max(0.01, last * 0.0005), 2)
        return Quote(symbol=symbol, bid=bid, ask=ask, last=last or bid or ask,
                     volume=_n(tk.volume) * (100 if _n(tk.volume) and _n(tk.volume) < 1e5 else 1))

    def get_price_history(
        self, symbol: str, interval: str = "5m", lookback_days: int = 10,
        start: Optional[dt.datetime] = None, end: Optional[dt.datetime] = None,
        extended_hours: bool = False,
    ) -> pd.DataFrame:
        if not self.is_connected:
            raise AuthError("IBKR not connected")
        bar = _BAR_SIZE.get(interval)
        if bar is None:
            raise NotSupported(f"interval {interval} not supported by IBKR adapter")
        c = self._contract(symbol)
        intraday = interval not in ("1d", "1wk")
        if intraday:
            days = min(max(int(lookback_days), 1), 30)
            duration = f"{days} D"
        elif int(lookback_days) > 365:
            duration = f"{math.ceil(lookback_days / 365)} Y"
        else:
            duration = f"{max(int(lookback_days), 1)} D"
        end_dt = end or dt.datetime.now(dt.timezone.utc)

        async def _h(ib):
            return await ib.reqHistoricalDataAsync(
                c, endDateTime=end_dt, durationStr=duration, barSizeSetting=bar,
                whatToShow="TRADES", useRTH=not extended_hours, formatDate=2,
                keepUpToDate=False,
            )

        bars = self._session.run_coro(_h, timeout=40)
        if not bars:
            raise RuntimeError(f"IBKR returned no candles for {symbol}")
        return _bars_to_df(bars)

    # -- orders ------------------------------------------------ #
    def _guard_orders(self) -> None:
        if self.readonly:
            raise OrderRejected("IBKR adapter is in read-only mode (IBKR_READONLY=1)")
        if not self.is_connected:
            raise AuthError("IBKR not connected")

    def place_order(self, req: OrderRequest) -> OrderResult:
        self._guard_orders()
        from ib_async import LimitOrder, MarketOrder

        c = self._contract(req.symbol)
        action = "BUY" if ((req.side is Side.LONG) == bool(req.is_entry)) else "SELL"
        qty = abs(float(req.quantity))
        if req.order_type is OrderType.MARKET or not req.limit_price:
            order = MarketOrder(action, qty)
        else:
            order = LimitOrder(action, qty, float(req.limit_price))
        order.tif = _tif(req.tif)
        order.outsideRth = req.session in ("EXTENDED", "SEAMLESS")
        if self.account_id:
            order.account = self.account_id

        trade = self._session.call(lambda ib: ib.placeOrder(c, order), timeout=10)
        oid = str(getattr(trade.order, "orderId", "") or getattr(trade.order, "permId", ""))
        st = getattr(trade.orderStatus, "status", "") or "Submitted"
        return OrderResult(order_id=oid, status=_norm_status(st), symbol=req.symbol,
                           submitted_qty=req.quantity, raw={"tif": order.tif})

    # place_bracket is intentionally NOT overridden - the base method just
    # places the entry (supports_bracket_native = False). The ExitManager
    # holds and manages the stop/target and sends the real close order.

    def cancel_order(self, order_id: str) -> None:
        self._guard_orders()

        def _cx(ib):
            for t in ib.trades():
                if str(getattr(t.order, "orderId", "")) == str(order_id):
                    ib.cancelOrder(t.order)
                    return True
            return False

        if not self._session.call(_cx, timeout=8):
            raise OrderRejected(f"IBKR: no live order {order_id} to cancel")

    def get_order(self, order_id: str) -> OrderResult:
        if not self.is_connected:
            raise AuthError("IBKR not connected")

        def _find(ib):
            for t in ib.trades():
                if str(getattr(t.order, "orderId", "")) == str(order_id):
                    return t
            return None

        t = self._session.call(_find, timeout=8)
        if t is None:
            return OrderResult(order_id=str(order_id), status="UNKNOWN", symbol="?", submitted_qty=0.0)
        os_ = t.orderStatus
        fills = [
            Fill(order_id=str(order_id), symbol=t.contract.symbol,
                 side=Side.LONG if t.order.action == "BUY" else Side.SHORT,
                 quantity=float(f.execution.shares), price=float(f.execution.price))
            for f in (t.fills or [])
        ]
        return OrderResult(
            order_id=str(order_id), status=_norm_status(os_.status),
            symbol=t.contract.symbol, submitted_qty=float(t.order.totalQuantity or 0.0),
            filled_qty=float(os_.filled or 0.0), avg_fill_price=float(os_.avgFillPrice or 0.0),
            fills=fills, raw={},
        )

    def list_orders(self, status: Optional[str] = None) -> List[OrderResult]:
        if not self.is_connected:
            return []
        trades = self._session.call(lambda ib: list(ib.openTrades() if status in (None, "OPEN", "WORKING")
                                                    else ib.trades()), timeout=8) or []
        out = []
        for t in trades:
            s = _norm_status(getattr(t.orderStatus, "status", ""))
            if status and status.upper() not in (s.upper(), "OPEN", "WORKING"):
                continue
            out.append(OrderResult(order_id=str(getattr(t.order, "orderId", "")), status=s,
                                   symbol=t.contract.symbol,
                                   submitted_qty=float(getattr(t.order, "totalQuantity", 0.0) or 0.0),
                                   filled_qty=float(getattr(t.orderStatus, "filled", 0.0) or 0.0)))
        return out


# --------------------------------------------------------------------------- #
#  helpers                                                                    #
# --------------------------------------------------------------------------- #
async def _sleep(sec: float) -> None:
    await asyncio.sleep(sec)


def _tif(tif: TimeInForce) -> str:
    return {TimeInForce.DAY: "DAY", TimeInForce.GTC: "GTC",
            TimeInForce.IOC: "IOC", TimeInForce.FOK: "FOK"}.get(tif, "DAY")


def _norm_status(s: str) -> str:
    s = (s or "").upper()
    return {
        "PENDINGSUBMIT": "SUBMITTED", "PRESUBMITTED": "SUBMITTED", "APIPENDING": "PENDING",
        "SUBMITTED": "WORKING", "FILLED": "FILLED", "CANCELLED": "CANCELLED",
        "APICANCELLED": "CANCELLED", "PENDINGCANCEL": "WORKING", "INACTIVE": "ERROR",
    }.get(s, s or "SUBMITTED")


def _bars_to_df(bars) -> pd.DataFrame:
    rows = []
    for b in bars:
        d = getattr(b, "date", None)
        rows.append({
            "date": d, "open": float(b.open), "high": float(b.high),
            "low": float(b.low), "close": float(b.close),
            "volume": float(getattr(b, "volume", 0.0) or 0.0),
        })
    df = pd.DataFrame(rows)
    ts = pd.to_datetime(df["date"], utc=True, errors="coerce")
    if ts.isna().all():                       # daily bars come back as date objects
        ts = pd.to_datetime(df["date"].astype(str), errors="coerce").dt.tz_localize("America/New_York")
    df = df.drop(columns=["date"])
    df.index = ts.dt.tz_convert("America/New_York")
    # IBKR TRADES volume is in lots (x100) for US stocks
    df["volume"] = df["volume"] * 100.0
    return df[["open", "high", "low", "close", "volume"]].dropna().sort_index()
