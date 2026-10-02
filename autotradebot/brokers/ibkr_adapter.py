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
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from typing import Any, Callable, Deque, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from ..config import get_settings
from ..core.enums import OrderType, Side, TimeInForce
from ..core.models import Account, Fill, OrderRequest, OrderResult, Position, Quote
from ..secrets_store import mask
from ..util.net import port_is_open
from .base import AuthError, BrokerAdapter, BrokerError, OrderRejected, WrongAccount

log = logging.getLogger(__name__)

_MARKET_DATA_TYPES = {"live": 1, "frozen": 2, "delayed": 3, "delayed-frozen": 4}
# error codes meaning "no real-time data for this login" (354 / 10168: not subscribed; 10197: a competing session)
_DELAYED_ERRS = {10167, 10168, 10197, 10089, 354}
COMPETING_SESSION = 10197
_COMPETING_WORDS = "different ip address"          # error 162: historical data refused for the same reason
_SCAN_CANCELLED = "scanner subscription cancelled"  # error 162 again: IBKR's echo of a market scan's cancel
#: what the dashboard says about delayed or refused data
COMPETING_REASON = ("IBKR sends no market data to this login while your live account is logged in somewhere else "
                    "(IBKR Mobile, Client Portal, TWS or the web trader) - with market data shared to the paper "
                    "account, only one session gets it. Log out there; the app switches back to real-time data by "
                    "itself within a few minutes.")
NOT_SUBSCRIBED_REASON = ("IBKR says this login has no real-time subscription for US stocks (error {code}). After "
                         "subscribing, or turning on sharing with the paper account, log IB Gateway out and back in; "
                         "the app checks again every few minutes.")
# IB Gateway reached IBKR's servers again (1101: subscriptions lost, 1102: kept)
_SERVERS_BACK = {1101, 1102}
# a data farm reporting OK - after a 2110 outage, that's the all-clear
_FARMS_OK = {2104, 2106, 2158}
# connection chatter, not failures
_INFO_ERRS = {2104, 2106, 2107, 2108, 2158, 2100, 2150, 202}
# part of a stock's real-time data isn't subscribed
_PARTIAL_DATA_ERRS = {10090, 10091}
# a stream refused for its stock (354 / 10089 / 10167 / 10168 not subscribed, 10090 / 10091 partly, 200 no such
# stock): the stock is quoted by snapshot instead, and only a burst of them counts against the login's data
_STREAM_REFUSALS = {354, 10089, 10167, 10168, 200} | _PARTIAL_DATA_ERRS
# the refusals that leave the request running - on part of the data, or on delayed data - and so holding a line
_REFUSED_BUT_OPEN = {10167} | _PARTIAL_DATA_ERRS
# every market-data line in use (101); a cancel of a request IBKR had already dropped (300)
_LINES_FULL, _UNKNOWN_TICKER = 101, 300
# what a new stream clears off a Ticker it shares with earlier requests, so none of their prices is taken for its own
_STREAM_RESET_FIELDS = ("bid", "ask", "last", "close", "bidSize", "askSize", "lastSize", "halted")


class _QuietDataErrors(logging.Filter):
    """ib_async logs every "not subscribed" reply as an ERROR. _on_error handles
    those and explains them once, so keep them out of the console - and the
    echo of every market scan's cancel (market_scan), which is no error at all."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if msg.startswith("Error 162,") and _SCAN_CANCELLED in msg.lower():
            return False
        return not any(msg.startswith(f"Error {code},") for code in _DELAYED_ERRS | _PARTIAL_DATA_ERRS)


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
        """``factory(ib)`` returns an awaitable; block for its result. One that takes longer than
        ``timeout`` is cancelled on the loop - left running, it would hold its request open with no
        one waiting for the answer."""
        if self._loop is None:
            raise AuthError("IBKR loop not started")
        fut = asyncio.run_coroutine_threadsafe(factory(self.ib), self._loop)
        try:
            return fut.result(timeout=timeout)
        except FutureTimeout:
            fut.cancel()
            raise

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


class Candles(dict):
    """history_many's answer, symbol -> candles. ``failed``: the symbols whose request timed out or
    errored - not the same as IBKR having nothing for them."""

    def __init__(self, *args, failed=(), **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.failed = set(failed)


class _Stream:
    """One stock's real-time stream: its contract, the Ticker ib_async keeps up to date, the request's id,
    when it was asked for, and whether a full quote (bid, ask and a trade) has arrived since - until then the
    Ticker may still hold nothing, or only yesterday's close, which must never pass for a price."""

    __slots__ = ("contract", "ticker", "req_id", "subscribed_at", "ready")

    def __init__(self, contract: Any, ticker: Any, req_id: int) -> None:
        self.contract, self.ticker, self.req_id = contract, ticker, req_id
        self.subscribed_at, self.ready = time.monotonic(), False


class IbkrBroker(BrokerAdapter):
    name = "ibkr"
    # The ExitManager owns exits and sends real close orders. No native bracket
    # is attached at IBKR - two exit managers on one position fight, and a
    # resting child can outlive the position. What rests at IBKR instead is a
    # stand-alone stop the executor keeps in step with the trade record and
    # stands down before any exit of its own (execution/protective_stops.py).
    supports_bracket_native = False
    supports_native_stop = True

    #: candle requests in flight at once. The Gateway takes about half a second over each whatever its
    #: length, so the rate is set by how many are open: 6 ran ~12 a second, 12 runs ~17 (measured over
    #: 700 requests, none refused); 32 timed out
    HISTORY_CONCURRENCY = 12
    DETAILS_CONCURRENCY = 16
    #: waits between reconnect attempts: quick at first - a Gateway restart takes a minute or two - then every 2 min
    RECONNECT_DELAYS_S = (5, 10, 15, 30, 30, 60, 60, 120)
    #: how long IB Gateway may stay up without IBKR's servers before its socket is dropped and made again
    SERVER_OUTAGE_RECONNECT_S = 600.0
    #: seconds the account's open orders may take to arrive before the request is called off - the list is
    #: then unknown, never empty
    OPEN_ORDERS_TIMEOUT_S = 10.0
    #: real-time streams held at most, whatever the settings ask: the account has about 100 market-data lines,
    #: and the snapshots still asked for need some of them
    STREAM_LINES_MAX = 90
    #: lines given back when IBKR says every one is in use (error 101)
    STREAM_BACKOFF = 10
    #: new streams asked for (and contracts looked up) per call: ib_async sends at most 45 messages a second,
    #: and an order would queue behind a longer burst
    STREAM_ADDS_PER_CALL = 20
    #: how long a stock whose stream was refused, or that IBKR has no contract for, is quoted by snapshot instead
    UNSTREAMABLE_S = 1800.0
    #: this many streams refused within this many seconds is the login's data, not a stock or two: the whole app
    #: falls back to delayed data as it would for a refused snapshot
    STREAM_REFUSAL_BURST = 5
    STREAM_REFUSAL_WINDOW_S = 60.0

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
        #: the account was named (IBKR_ACCOUNT_ID) rather than taken from the first login (_do_connect)
        self._account_named = bool(self.account_id)
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
        #: when the connection last came up (time.monotonic) - positions and orders reload after each time
        self.connected_since = 0.0
        #: when it went down, and when IB Gateway lost IBKR's servers while staying up itself (and the code it gave)
        self.down_since: Optional[float] = None
        self._servers_lost_at: Optional[float] = None
        self._servers_lost_code = 0
        self._contracts: Dict[str, Any] = {}              # symbol -> qualified Contract
        self._last_error = ""
        #: the last refusal of real-time data (IBKR's code), how many there have been, and when live data was last
        #: tried again; and when IBKR last refused data because the live account was logged in elsewhere
        self._data_refused_code = 0
        self._data_refusals = 0
        self._live_checked_at = 0.0
        self._competing_at: Optional[float] = None
        self._lock = threading.RLock()
        #: the request for the open orders that is out now, which any other caller waits on (see _open_orders)
        self._orders_lock = threading.Lock()
        self._orders_asked: Optional[Future] = None

        # real-time streams (set_streams). The loop thread changes them; any thread reads them. Everything below
        # is under _stream_lock, which is never held while waiting on the loop or calling on_tick
        self._stream_lock = threading.Lock()
        self._streams: Dict[str, _Stream] = {}           # symbol -> its stream
        self._by_ticker: Dict[Any, str] = {}             # Ticker -> symbol (a Ticker hashes by identity)
        self._stream_reqs: Dict[int, str] = {}           # request id -> symbol
        #: the ids of streams ended lately: IBKR's late answers to them are no news
        self._ended_reqs: Deque[int] = deque(maxlen=512)
        self._latest: Dict[str, Tuple[Quote, float]] = {}   # symbol -> its latest streamed quote, time.monotonic()
        #: the priority order last asked for, and how many streams may be held (lowered by error 101)
        self._stream_order: List[str] = []
        self._stream_cap = self.STREAM_LINES_MAX
        #: how many at the head of that order error 101 never takes: the positions and working entries
        self._stream_protect = 0
        #: request ids below this were sent before the last cut for error 101: their 101s are that same burst
        self._cut_below = -1
        self._unstreamable: Dict[str, float] = {}        # symbol -> when it may be tried again (time.monotonic)
        self._refusals: Deque[float] = deque()           # when streams were refused lately
        #: real-time data proven on this connection: the connect probe went through, or a recheck did
        self._live_verified = False
        self._tick_failed = False
        #: called on the IB loop with the symbols whose streamed quote changed; it must only note them
        self.on_tick: Optional[Callable[[FrozenSet[str]], None]] = None

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
        try:
            self._do_connect(first=True)
        except Exception:
            self.close()                     # no event loop left running for a connection that never came up
            self._session = None
            raise

    def _do_connect(self, first: bool = False) -> None:
        # nothing streams until this connection's data has been proven real-time (_check_data_entitlement); a
        # new connection has all its lines, and a stock refused on the last one may be covered now
        self._live_verified = False
        with self._stream_lock:
            self._stream_cap, self._cut_below, self._stream_protect = self.STREAM_LINES_MAX, -1, 0
            self._unstreamable.clear()
            self._refusals.clear()
            self._ended_reqs.clear()                  # request ids start again with the connection

        async def _connect(ib):
            await ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=8,
                                  readonly=self.readonly)
        self._session.run_coro(_connect, timeout=15)
        accounts = self._check_accounts()
        if not self._account_loaded():
            # a restarting Gateway takes connections a little before it has loaded the account; trusting it
            # then would make the open positions look closed
            self._drop_socket()
            raise AuthError("IB Gateway answered but hasn't finished loading the account - trying again shortly")
        if first:
            try:
                self._ib.disconnectedEvent += self._on_disconnect
                self._ib.errorEvent += self._on_error
            except Exception:  # noqa: BLE001
                pass
            try:
                self._ib.pendingTickersEvent += self._on_tickers
            except Exception:  # noqa: BLE001
                pass
        if self._md_pref == "auto":
            # every connection tries real-time data again: a subscription may have been added, or a competing
            # live session logged out, since the last one
            self._data_type, self._data_is_delayed = 1, False
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
        self.connected_since, self.down_since = time.monotonic(), None
        self._servers_lost_at, self._servers_lost_code = None, 0
        if not self.account_id:
            self.account_id = accounts[0]
        self._check_data_entitlement()
        log.info("IBKR connected  %s:%s  account=%s  data=%s", self.host, self.port,
                 mask(self.account_id) or "?", "delayed" if self._data_is_delayed else "live")

    def _check_data_entitlement(self) -> None:
        """One quick live quote at connect. Without a real-time subscription IBKR
        refuses at once, which switches to delayed data (see _on_error), so the
        dashboard's data label is right from the start."""
        if self._md_pref == "auto" and not self._data_is_delayed:
            self._live_checked_at = time.monotonic()
            try:
                probe = self._contract("SPY")             # IBKR only refuses a qualified contract
                self._session.run_coro(lambda ib: ib.reqTickersAsync(probe), timeout=4)
                self._session.run_coro(lambda ib: _sleep(0.3), timeout=2)    # let the refusal land
            except Exception:  # noqa: BLE001
                pass
        # streams wait for this: a refused probe has switched to delayed data by now. Only real-time data
        # streams - a chosen "frozen" or delayed type never does
        if self._data_type == 1 and not self._data_is_delayed:
            self._live_verified = True

    #: how often a login that fell back to delayed data asks for real-time data again
    LIVE_RECHECK_S = 300.0

    def recheck_live_data(self) -> bool:
        """Ask for real-time data again after a refusal; returns whether it's live now. Without this a
        subscription added, or a competing live session logged out, would only count after a restart."""
        self._live_checked_at = time.monotonic()
        if self._md_pref != "auto" or not self._data_is_delayed or not self.is_connected:
            return not self._data_is_delayed
        refused = self._data_refusals
        try:
            probe = self._contract("SPY")
            self._session.call(lambda ib: ib.reqMarketDataType(1), timeout=5)
            self._session.run_coro(lambda ib: ib.reqTickersAsync(probe), timeout=6)
            self._session.run_coro(lambda ib: _sleep(0.5), timeout=3)
        except Exception:  # noqa: BLE001
            refused = -1                                  # couldn't tell: stay on delayed data
        if refused == self._data_refusals:
            self._data_type, self._data_is_delayed, self._data_refused_code = 1, False, 0
            self._live_verified = True
            with self._stream_lock:
                self._unstreamable.clear()               # stocks refused before may be covered now
            log.warning("IBKR: real-time market data is available again")
            return True
        try:
            self._session.call(lambda ib: ib.reqMarketDataType(3), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        return False

    @property
    def market_data_reason(self) -> str:
        """Why IBKR isn't sending real-time data (or any), in words - empty when it is."""
        if self._competing_at is not None and time.monotonic() - self._competing_at < self.LIVE_RECHECK_S * 2:
            return COMPETING_REASON
        if not self._data_is_delayed:
            return ""
        if self._data_refused_code == COMPETING_SESSION:
            return COMPETING_REASON
        if self._data_refused_code:
            return NOT_SUBSCRIBED_REASON.format(code=self._data_refused_code)
        return "Delayed data was chosen in the IBKR settings (IBKR_MARKET_DATA)." if self._md_pref != "auto" else ""

    @property
    def candles_refused(self) -> str:
        """Why IBKR is refusing candles right now, if it is: the login is active somewhere else, and
        until that session ends every price request fails - the app is blind. Empty otherwise."""
        recent = self._competing_at is not None and time.monotonic() - self._competing_at < self.LIVE_RECHECK_S
        return COMPETING_REASON if recent else ""

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

    def _check_accounts(self) -> List[str]:
        """The accounts behind the port, once they are the kind this route is for; otherwise the socket is
        dropped and the connection refused. The port is all that tells paper from live, so a live login on the
        paper port would be traded as paper - past autopilot.allow_live, the equity floor and the day-trade cap.
        IBKR's paper account ids start with D (DU..., DF... for an advisor's), live ones never do. Runs on every
        connection, the nightly reconnect included: the Gateway may have been logged in to another account since.
        ib_async has the list before connectAsync returns - its handshake waits for it."""
        try:
            accounts = [str(a) for a in (self._session.call(lambda ib: ib.managedAccounts(), timeout=5) or [])]
        except Exception:  # noqa: BLE001
            accounts = []
        if not accounts:
            self._drop_socket()
            raise AuthError(f"IB Gateway on port {self.port} didn't say which account it is logged in to - "
                            "trying again shortly")
        live = [a for a in accounts if not a.upper().startswith("D")]
        paper = [a for a in accounts if a.upper().startswith("D")]
        problem = ""
        if self.mode == "paper" and live:
            problem = (f"port {self.port} is logged in to a LIVE account ({mask(live[0])}) - the paper route "
                       "refuses it. Log the paper Gateway in with your paper username (DU...), or point "
                       "IBKR_PAPER_PORT at the Gateway that is.")
        elif self.mode == "live" and paper:
            problem = (f"port {self.port} is logged in to a paper account ({mask(paper[0])}) - the live route "
                       "refuses it. Log the live Gateway in with your live username, or point IBKR_LIVE_PORT "
                       "at the Gateway that is.")
        elif self.account_id and self.account_id not in accounts and self._account_named:
            problem = (f"account {mask(self.account_id)} isn't on the login at port {self.port} (it has "
                       f"{', '.join(mask(a) for a in accounts)}) - set IBKR_ACCOUNT_ID to one of those, or log "
                       "IB Gateway in to that account.")
        elif self.account_id and self.account_id not in accounts:
            # taken from the first login, and the Gateway has been logged in to another account since: the open
            # positions, their stops and the orders are the first one's, so the app keeps to it rather than send
            # their exits to an account that doesn't hold them
            problem = (f"the login at port {self.port} changed account since the app connected (it was "
                       f"{mask(self.account_id)}, now {', '.join(mask(a) for a in accounts)}) - log IB Gateway back "
                       "in to the first, or restart the app to trade the new one.")
        if problem:
            self._drop_socket()
            raise WrongAccount(problem)
        return accounts

    def _account_loaded(self) -> bool:
        """The account's values have arrived - a Gateway that is still starting up connects before they do."""
        try:
            values = self._session.call(lambda ib: ib.accountValues(self.account_id or ""), timeout=5) or []
        except Exception:  # noqa: BLE001
            return False
        return any(getattr(v, "tag", "") == "NetLiquidation" for v in values)

    def _socket_up(self) -> bool:
        try:
            return bool(self._ib is not None and self._ib.isConnected())
        except Exception:  # noqa: BLE001
            return False

    def _drop_socket(self) -> None:
        try:
            self._session.call(lambda ib: ib.disconnect(), timeout=5)
        except Exception:  # noqa: BLE001
            pass

    def _on_disconnect(self) -> None:
        try:
            # the streams went with the socket: forget them without a word to IBKR. ib_async forgets its side
            # just before or just after this, depending on which end closed the socket - either is fine
            self._drop_streams(self._ib, cancel=False)
        except Exception:  # noqa: BLE001
            pass
        self._connected = False
        self.down_since = self.down_since or time.monotonic()
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
        """Until the session is usable again: while IB Gateway is up but has lost IBKR's servers, wait for
        it to reach them by itself (IBKR's nightly maintenance); otherwise - the Gateway restarting, or an
        outage that drags on - connect afresh once its port answers."""
        attempt = 0
        try:
            while self._want_connected and not self.is_connected:
                if self._socket_up():
                    lost = self._servers_lost_at
                    if lost is not None and time.monotonic() - lost < self.SERVER_OUTAGE_RECONNECT_S:
                        time.sleep(5)
                        continue
                    log.warning("IBKR: the Gateway session isn't usable - dropping it and connecting again")
                    self._servers_lost_at, self._servers_lost_code = None, 0
                    self._drop_socket()
                if port_is_open(self.host, self.port, timeout=2):
                    try:
                        self._do_connect()
                        log.info("IBKR reconnected")
                        return
                    except Exception as e:  # noqa: BLE001
                        if isinstance(e, WrongAccount) and self._last_error != f"reconnect: {e}":
                            log.warning("IBKR: %s", e)           # once, not at every retry
                        self._last_error = f"reconnect: {e}"
                time.sleep(self.RECONNECT_DELAYS_S[min(attempt, len(self.RECONNECT_DELAYS_S) - 1)])
                attempt += 1
        finally:
            self._reconnecting = False

    def refresh_if_needed(self) -> bool:
        """Nudge a reconnect when the socket has dropped (the daily Gateway restart)."""
        if not self.is_connected and self._want_connected:
            self._start_reconnect()
        elif self._data_is_delayed and time.monotonic() - self._live_checked_at >= self.LIVE_RECHECK_S:
            self.recheck_live_data()
        return self.is_connected

    def session_status(self) -> Dict[str, Any]:
        connected = self.is_connected
        servers_lost = self._servers_lost_at is not None and self._socket_up()
        return {
            "connected": connected,
            "reconnecting": self._reconnecting,
            "host": self.host, "port": self.port, "mode": self.mode,
            "account": self.account_id or None,
            "market_data": "delayed" if self._data_is_delayed else "live",
            "market_data_reason": self.market_data_reason,
            "readonly": self.readonly,
            "servers_lost": servers_lost,
            "down_for_s": round(time.monotonic() - self.down_since) if self.down_since and not connected else 0,
            "message": ("connected" if connected
                        else "IB Gateway is up but has lost IBKR's servers - waiting for them" if servers_lost
                        else "reconnecting - Gateway restarting?" if self._reconnecting
                        else f"IB Gateway not reachable on {self.host}:{self.port}"),
            "last_error": self._last_error,
        }

    def _on_error(self, reqId, errorCode, errorString, contract=None) -> None:  # noqa: ANN001
        if errorCode in _SERVERS_BACK or (errorCode in _FARMS_OK and self._servers_lost_code == 2110):
            self._servers_back(errorCode)
            return
        if errorCode in _INFO_ERRS:
            return
        if errorCode == 162 and _SCAN_CANCELLED in str(errorString).lower():
            return                                   # a market scan's cancel confirmed - every scan sends one
        if self._stream_error(reqId, errorCode, errorString):
            return
        if errorCode in _DELAYED_ERRS:
            self._data_refusals += 1
            self._data_refused_code = errorCode
            if errorCode == COMPETING_SESSION:
                self._competing_at = time.monotonic()
            if not self._data_is_delayed:
                log.warning("IBKR: no real-time market data for this login (%s: %s) - using delayed data",
                            errorCode, errorString)
            self._data_is_delayed = True
            self._data_type = 3
            self._live_verified = False
            try:
                # this runs on the IB loop thread, so call directly: session.call()
                # would wait on this same thread and time out
                self._ib.reqMarketDataType(3)
            except Exception:  # noqa: BLE001
                pass
            self._drop_streams(self._ib, cancel=True)      # delayed prices never stream
            return
        if errorCode in (1100, 2110):
            if self._servers_lost_at is None:
                log.warning("IBKR: IB Gateway lost its connection to IBKR's servers (%s) - waiting for it", errorCode)
                self._servers_lost_at, self._servers_lost_code = time.monotonic(), errorCode
            self.down_since = self.down_since or time.monotonic()
            self._connected = False
            if self._want_connected:
                self._start_reconnect()          # watches for the servers, and starts afresh if they stay away
        elif errorCode == 1300:
            self._connected = False
        elif errorCode == 162 and _COMPETING_WORDS in str(errorString).lower():
            if self._competing_at is None or time.monotonic() - self._competing_at > self.LIVE_RECHECK_S:
                log.warning("IBKR: candles refused - the live account is logged in from another place")
            self._competing_at = time.monotonic()
        self._last_error = f"{errorCode}: {errorString}"
        if errorCode not in (162, 200):        # historical-data / unknown-contract noise
            log.debug("IBKR error %s: %s", errorCode, errorString)

    def _servers_back(self, code: int) -> None:
        """IB Gateway reached IBKR's servers again. Runs on the loop thread, so it calls ib_async directly."""
        was_lost, self._servers_lost_at, self._servers_lost_code = self._servers_lost_at, None, 0
        if code == 1101:
            # IBKR dropped the market-data lines with its connection (with or without a 1100 first): forget the
            # streams, so the next set_streams asks for them afresh. After 1102 they carry on
            self._drop_streams(self._ib, cancel=True)
        if not self._socket_up() or (self._connected and was_lost is None):
            return
        if code == 1101:
            # the subscriptions were lost with the connection - ask for the account and positions again
            try:
                self._ib.reqMarketDataType(self._data_type)
                self._ib.client.reqAccountUpdates(True, self.account_id or "")
                self._ib.client.reqPositions()
            except Exception:  # noqa: BLE001
                pass
        self._connected = True
        self.connected_since, self.down_since = time.monotonic(), None
        log.warning("IBKR: IB Gateway reached IBKR's servers again (%s)", code)

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
        # A read the loop doesn't answer in time (it is busy with a big download, say) raises,
        # so the caller keeps its last snapshot: an answer of "no values, no positions" would
        # read as an empty account, and positions must never look gone because IBKR was slow.
        try:
            values = list(self._session.call(lambda ib: ib.accountValues(acct), timeout=8) or [])
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"IBKR didn't answer for the account values in time ({type(e).__name__})") from e
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
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"IBKR didn't answer for the positions in time ({type(e).__name__})") from e
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

        async def _quote(ib):
            # a refused snapshot still hands back the stock's one Ticker, with what its stream or an earlier snapshot
            # left on it. ib_async stamps each packet's Tickers with a new time object: the very same one, nothing
            # came for this one. Not an equal or earlier one - a coarse or reset wall clock gives those to new ticks
            before = getattr(ib.ticker(contract), "time", None)
            tickers = await ib.reqTickersAsync(contract)
            if not tickers:
                raise RuntimeError(f"IBKR returned no ticker for {symbol}")
            when = getattr(tickers[0], "time", None)
            if when is None or when is before:
                return None
            # read here, on the loop thread: a stream of the same stock shares the Ticker and writes to it there
            return _quote_from_ticker(symbol, tickers[0], "snapshot")

        q = self._session.run_coro(_quote, timeout=12)
        if q is None:
            raise RuntimeError(f"IBKR has no price for {symbol} "
                               f"({'delayed' if self._data_is_delayed else 'real-time'} data)")
        return q

    # ---- real-time streams ------------------------------------------------ #
    @property
    def can_stream(self) -> bool:
        """Streams are held only on real-time data this connection has proven - never delayed or frozen data,
        and not before the connect probe is through."""
        return self.is_connected and self._data_type == 1 and not self._data_is_delayed and self._live_verified

    def set_streams(self, symbols: Sequence[str], limit: int, protect: int = 0) -> List[str]:
        """Hold real-time streams for the first ``limit`` of ``symbols`` (in priority order) that can stream -
        never more than STREAM_LINES_MAX, or than error 101 has left room for - and end the others; the first
        ``protect`` of them (the positions) are never given back for error 101. Returns the symbols streaming
        now; asking for the same again sends nothing. It waits on the loop, so never call it from the loop
        thread."""
        if not self.is_connected:
            return []                                 # the streams already went with the connection
        if not self.can_stream or limit <= 0:
            with self._stream_lock:
                held = bool(self._streams)
            if held:
                try:
                    self._session.call(lambda ib: self._drop_streams(ib, cancel=True), timeout=10)
                except Exception as e:  # noqa: BLE001
                    log.debug("IBKR streams not ended: %s", e)
            return []
        now = time.monotonic()
        with self._stream_lock:
            self._unstreamable = {s: t for s, t in self._unstreamable.items() if t > now}
            n = min(int(limit), self.STREAM_LINES_MAX, self._stream_cap)
            wanted = [s for s in dict.fromkeys(symbols) if s and s not in self._unstreamable][:n]
        held = set(list(dict.fromkeys(symbols))[:max(0, int(protect))])
        # a stream needs a qualified contract (ib_async keys its Tickers by the contract id): look up the
        # missing ones here, off the loop, most wanted first
        todo = [s for s in wanted if not _qualified(self._contracts.get(s))][:self.STREAM_ADDS_PER_CALL]
        if todo:
            from ib_async import Stock

            stocks = [Stock(s, "SMART", "USD") for s in todo]
            try:
                self._session.run_coro(lambda ib: ib.qualifyContractsAsync(*stocks), timeout=15)
                answered = True
            except Exception as e:  # noqa: BLE001
                log.debug("IBKR contracts for streaming not found: %s", e)
                answered = False
            with self._stream_lock:
                for s, stock in zip(todo, stocks):
                    # a contract is qualified in place; ib_async answers None for one IBKR doesn't know
                    if _qualified(stock):
                        self._contracts[s] = stock
                    elif answered:
                        self._unstreamable[s] = now + self.UNSTREAMABLE_S
        order = [s for s in wanted if _qualified(self._contracts.get(s))]
        # the positions head the order; how many is set on the loop with the order itself - set sooner (before the
        # contract lookups above), a 101 meanwhile would read fewer positions against the order held till then,
        # and a position that is still held but no longer first could lose its stream
        protected = sum(1 for s in order if s in held)
        try:
            return self._session.call(lambda ib: self._sync_streams(ib, order, protected), timeout=10)
        except Exception as e:  # noqa: BLE001
            log.debug("IBKR streams not updated: %s", e)
            with self._stream_lock:
                return [s for s in order if s in self._streams]

    def streamed_quote(self, symbol: str) -> Optional[Tuple[Quote, float]]:
        """The latest streamed quote for ``symbol`` and how many seconds ago it arrived - None when it isn't
        streaming, or the data isn't proven real-time right now."""
        if not self.can_stream:
            return None
        with self._stream_lock:
            got = self._latest.get(symbol)
        if got is None:
            return None
        return got[0], max(0.0, time.monotonic() - got[1])

    def _sync_streams(self, ib, order: List[str], protect: int = 0) -> List[str]:
        """Make the streams held match ``order``, whose first ``protect`` (the positions) error 101 never takes.
        Runs on the loop thread - the one that changes them, and the one _on_error runs on, so a refusal can't
        land between the check below and a request."""
        if not (self.can_stream and ib.isConnected()):
            self._drop_streams(ib, cancel=True)
            return []
        with self._stream_lock:
            self._stream_order = list(order)
            self._stream_protect = max(0, min(int(protect), len(order)))
            keep = set(order)
            gone = [s for s in self._streams if s not in keep]
            new = [s for s in order if s not in self._streams]
        for s in gone:
            self._end_stream(ib, s, cancel=True)
        added = 0
        for s in new:
            if added >= self.STREAM_ADDS_PER_CALL:
                break
            contract = self._contracts.get(s)
            try:
                tk = ib.ticker(contract)
                if tk is not None:
                    if tk in ib.wrapper.ticker2ReqId["snapshot"]:
                        continue              # a snapshot of it is out and would land on the cleared fields: next pass
                    if tk in ib.wrapper.ticker2ReqId["mktData"]:
                        ib.cancelMktData(contract)      # left over: a second request on it would hold a line for good
                    for field in _STREAM_RESET_FIELDS:
                        setattr(tk, field, math.nan)
                tk = ib.reqMktData(contract, "", False, False)
                req_id = ib.wrapper.ticker2ReqId["mktData"][tk]
            except ConnectionError:
                break
            except Exception as e:  # noqa: BLE001
                log.debug("IBKR stream for %s not started: %s", s, e)
                continue
            with self._stream_lock:
                self._streams[s] = _Stream(contract, tk, req_id)
                self._by_ticker[tk] = s
                self._stream_reqs[req_id] = s
            added += 1
        with self._stream_lock:
            return [s for s in order if s in self._streams]

    def _end_stream(self, ib, symbol: str, cancel: bool) -> None:
        """Forget ``symbol``'s stream, and with ``cancel`` tell IBKR to stop it. Without, only ib_async's own
        bookkeeping is closed - for a stream IBKR has already dropped or refused. Runs on the loop thread."""
        with self._stream_lock:
            st = self._streams.pop(symbol, None)
            self._latest.pop(symbol, None)
            if st is None:
                return
            self._by_ticker.pop(st.ticker, None)
            self._stream_reqs.pop(st.req_id, None)
            self._ended_reqs.append(st.req_id)
        try:
            if cancel and ib.isConnected() and st.ticker in ib.wrapper.ticker2ReqId["mktData"]:
                ib.cancelMktData(st.contract)
            else:
                ib.wrapper.endTicker(st.ticker, "mktData")
        except Exception as e:  # noqa: BLE001
            log.debug("IBKR stream for %s not ended cleanly: %s", symbol, e)
        # reqId2Ticker keeps the id: a late tick on an id ib_async doesn't know is logged as an error

    def _drop_streams(self, ib, cancel: bool) -> None:
        """End every stream (see _end_stream). Runs on the loop thread."""
        with self._stream_lock:
            symbols = list(self._streams)
            self._stream_order = []
        for s in symbols:
            self._end_stream(ib, s, cancel)

    def _on_tickers(self, tickers) -> None:
        """ib_async's word that Tickers changed, on the loop thread after each packet: keep each stream's quote.
        It only notes them - on_tick is told once the lock is let go."""
        moved = []
        try:
            with self._stream_lock:
                for tk in tickers:                    # a set ib_async replaces with every packet: never kept
                    symbol = self._by_ticker.get(tk)
                    st = self._streams.get(symbol) if symbol else None
                    if st is None or st.ticker is not tk or getattr(tk, "marketDataType", 1) != 1:
                        continue                      # not a stream, or not real-time data
                    if getattr(tk, "halted", None) in (1, 2):
                        self._latest.pop(symbol, None)       # a halted stock has no price to act on
                        continue
                    if not st.ready:
                        # the first full quote since the request - a close alone is yesterday's price
                        if not (_price(tk.bid) and _price(tk.ask) and _price(tk.last)):
                            continue
                        st.ready = True
                    q = _quote_from_ticker(symbol, tk, "stream")
                    if q is not None:
                        self._latest[symbol] = (q, time.monotonic())
                        moved.append(symbol)
        except Exception:  # noqa: BLE001 - raised to eventkit, it would log a traceback for every packet
            if not self._tick_failed:
                self._tick_failed = True
                log.exception("IBKR: a streamed price couldn't be read (later failures are not logged)")
        listener = self.on_tick
        if moved and listener is not None:
            try:
                listener(frozenset(moved))
            except Exception:  # noqa: BLE001
                log.debug("IBKR: the stream listener failed", exc_info=True)

    def _stream_error(self, req_id: int, code: int, text: str) -> bool:
        """IBKR's errors about the streams; True when handled here. Runs on the loop thread, from _on_error."""
        if code not in _STREAM_REFUSALS and code not in (_LINES_FULL, _UNKNOWN_TICKER):
            return False
        with self._stream_lock:
            symbol = self._stream_reqs.get(req_id)
            ended = req_id in self._ended_reqs
            streaming = bool(self._streams)
        if code == _UNKNOWN_TICKER:
            return symbol is not None or ended        # a cancel of a stream IBKR had already dropped
        if code == _LINES_FULL:
            if symbol is None and not ended and not streaming:
                return False                          # not about streams: as before
            self._lines_full(req_id, symbol)
            return True
        if ended:
            return True                               # a late answer about a stream already given up
        if symbol is None or not self._live_verified:
            return False
        now = time.monotonic()
        with self._stream_lock:
            while self._refusals and now - self._refusals[0] > self.STREAM_REFUSAL_WINDOW_S:
                self._refusals.popleft()
            if code != 200:                           # no such stock says nothing about the login's data
                self._refusals.append(now)
            if len(self._refusals) >= self.STREAM_REFUSAL_BURST:
                return False                          # the login's data, not a stock or two: as before
            self._unstreamable[symbol] = now + self.UNSTREAMABLE_S
        self._end_stream(self._ib, symbol, cancel=code in _REFUSED_BUT_OPEN)
        log.warning("IBKR: no real-time stream for %s (%s: %s) - it's quoted by snapshot for the next %d min",
                    symbol, code, text, self.UNSTREAMABLE_S // 60)
        return True

    def _lines_full(self, req_id: int, symbol: Optional[str]) -> None:
        """Error 101: every market-data line the account has is in use. Give STREAM_BACKOFF of the held streams
        back from the end of the priority order - the watch tier goes first, then the plays; the positions (the
        first ``protect`` set_streams was told of) never - and hold no more than that until the next connection.
        The rest of a burst of 101s - requests sent before the cut - cuts nothing more."""
        ib = self._ib
        if symbol is not None:
            self._end_stream(ib, symbol, cancel=False)    # refused: IBKR holds no line for it
        with self._stream_lock:
            if req_id < self._cut_below:
                return
            self._stream_cap = max(min(self._stream_protect, len(self._streams)),
                                   min(self._stream_cap, len(self._streams) - self.STREAM_BACKOFF))
            keep = set([s for s in self._stream_order if s in self._streams][:self._stream_cap])
            tail = [s for s in self._streams if s not in keep]
            cap = self._stream_cap
            try:
                self._cut_below = int(ib.client._reqIdSeq)     # the id the next request will get
            except Exception:  # noqa: BLE001
                self._cut_below = req_id + 1
        for s in tail:
            self._end_stream(ib, s, cancel=True)
        log.warning("IBKR: every market-data line is in use (error 101) - streaming at most %d stocks "
                    "until the next connection", cap)

    def history_many(self, requests: Mapping[str, Tuple[str, str]],
                     con_ids: Optional[Mapping[str, int]] = None,
                     timeout: float = 30.0, end: Optional[dt.datetime] = None,
                     rth: bool = True) -> Dict[str, pd.DataFrame]:
        """Candles for many symbols at once. ``requests`` maps a symbol to
        (bar size, duration), e.g. ("1 day", "1 Y") or ("5 mins", "5 D"), ending at
        ``end`` (default: now); ``rth`` False includes the pre-market and after-hours
        candles. Symbols IBKR has nothing for are left out of the result; the ones whose request
        timed out or failed are left out too and named in the result's ``failed`` - they may well
        have candles, so a caller that remembers what IBKR lacks mustn't count them."""
        if not self.is_connected:
            raise AuthError("IBKR not connected")
        if not requests:
            return Candles()
        con_ids = con_ids or {}
        failed: set = set()

        async def one(ib, gate: asyncio.Semaphore, symbol: str, bar: str, duration: str):
            async with gate:
                asked_at = time.monotonic()
                try:
                    bars = await ib.reqHistoricalDataAsync(
                        self._contract_for_history(symbol, con_ids.get(symbol)), endDateTime=end or "",
                        durationStr=duration, barSizeSetting=bar, whatToShow="TRADES", useRTH=rth,
                        formatDate=2, keepUpToDate=False, timeout=timeout)
                except Exception:  # noqa: BLE001
                    failed.add(symbol)
                    return symbol, None
                if not bars and time.monotonic() - asked_at >= timeout - 0.5:
                    failed.add(symbol)                   # ib_async answers a timeout with an empty list
                return symbol, _bars_to_df(bars) if bars else None

        async def run(ib):
            gate = asyncio.Semaphore(self.HISTORY_CONCURRENCY)
            return await asyncio.gather(*(one(ib, gate, s, bar, dur) for s, (bar, dur) in requests.items()))

        budget = timeout * (len(requests) / self.HISTORY_CONCURRENCY + 1) + 30
        got = self._session.run_coro(run, timeout=budget)
        return Candles({s: f for s, f in got if f is not None and len(f)}, failed=failed)

    def get_fills(self, symbol: Optional[str] = None, timeout: float = 15.0) -> List[Fill]:
        """This session's executions on the account (IBKR keeps the current day's), oldest first.
        They book a record whose position was closed in TWS, or by an exit that filled while the
        app was down."""
        if not self.is_connected:
            return []
        from ib_async import ExecutionFilter

        wanted = ExecutionFilter(symbol=symbol or "", acctCode=self.account_id or "")

        async def run(ib):
            return await ib.reqExecutionsAsync(wanted)

        try:
            reported = self._session.run_coro(run, timeout=timeout) or []
        except Exception as e:  # noqa: BLE001
            log.debug("executions for %s unavailable: %s", symbol or "the account", e)
            return []
        out: List[Fill] = []
        for item in reported:
            execution, contract = getattr(item, "execution", None), getattr(item, "contract", None)
            if execution is None or contract is None or (symbol and contract.symbol != symbol):
                continue
            shares = float(getattr(execution, "shares", 0.0) or 0.0)
            if shares <= 0:
                continue
            when = getattr(execution, "time", None)
            if not isinstance(when, dt.datetime):
                when = dt.datetime.now(dt.timezone.utc)
            elif when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            report = getattr(item, "commissionReport", None)
            commission = float(getattr(report, "commission", 0.0) or 0.0)
            out.append(Fill(order_id=str(getattr(execution, "orderId", "") or getattr(execution, "execId", "")),
                            symbol=contract.symbol,
                            side=Side.LONG if str(getattr(execution, "side", "")).upper().startswith("B") else Side.SHORT,
                            quantity=shares, price=float(execution.price), ts=when,
                            commission=commission if commission == commission else 0.0,
                            tag=str(getattr(execution, "orderRef", "") or "")))
        return sorted(out, key=lambda f: f.ts)

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

    def market_scan(self, code: str, rows: int = 50, above_price: float = 3.0, below_price: float = 600.0,
                    timeout: float = 8.0) -> List[str]:
        """The US stocks topping one of IBKR's live market scans (``code``: TOP_PERC_GAIN, TOP_PERC_LOSE,
        HOT_BY_VOLUME...) between ``above_price`` and ``below_price``, in rank order - one answer, then the
        subscription is cancelled, answered or not: IBKR allows 10 open at once, and ib_async's
        reqScannerDataAsync would leave one open on a timeout. No market-data line is used. Empty when not
        connected or when the scan fails."""
        if not self.is_connected:
            return []
        from ib_async import ScannerSubscription

        wanted = ScannerSubscription(instrument="STK", locationCode="STK.US.MAJOR", scanCode=code,
                                     numberOfRows=rows, abovePrice=above_price, belowPrice=below_price)

        async def run(ib):
            data = ib.reqScannerSubscription(wanted)
            try:
                try:
                    await asyncio.wait_for(ib.wrapper.startReq(data.reqId, container=data), timeout)
                except Exception:  # noqa: BLE001 - a timeout or a refusal: whatever rows came are read below
                    pass
            finally:
                ib.cancelScannerSubscription(data)
            ranked = sorted(data, key=lambda row: row.rank)
            return [row.contractDetails.contract.symbol for row in ranked
                    if getattr(row.contractDetails.contract, "secType", "") == "STK"]

        try:
            return list(dict.fromkeys(self._session.run_coro(run, timeout=timeout + 2)))
        except Exception as e:  # noqa: BLE001
            log.debug("IBKR's %s scan failed: %s", code, e)
            return []

    # ---- orders ------------------------------------------------------------ #
    def _guard_orders(self) -> None:
        if self.readonly:
            raise OrderRejected("IBKR adapter is in read-only mode (IBKR_READONLY=1)")
        if not self.is_connected:
            raise AuthError("IBKR not connected")

    def place_order(self, req: OrderRequest) -> OrderResult:
        self._guard_orders()
        from ib_async import LimitOrder, MarketOrder, StopOrder

        contract = self._contract(req.symbol)
        action = "BUY" if req.side is Side.LONG else "SELL"      # the side is the order's direction, exits too
        qty = abs(float(req.quantity))
        if req.order_type is OrderType.STOP:
            if not req.stop_price:
                raise OrderRejected("a stop order needs its trigger price")
            order = StopOrder(action, qty, float(req.stop_price))   # a market order once the price trades through it
        else:
            order = (MarketOrder(action, qty) if req.order_type is OrderType.MARKET or not req.limit_price
                     else LimitOrder(action, qty, float(req.limit_price)))
        order.tif = _tif(req.tif)
        order.outsideRth = req.session in ("EXTENDED", "SEAMLESS")
        order.orderRef = req.client_tag            # lets a restarted app recognise its own working orders
        if req.oca_group:
            order.ocaGroup, order.ocaType = req.oca_group, int(req.oca_type or 3)
        if self.account_id:
            order.account = self.account_id
        trade = self._session.call(lambda ib: ib.placeOrder(contract, order), timeout=10)
        oid = str(getattr(trade.order, "orderId", "") or getattr(trade.order, "permId", ""))
        status = getattr(trade.orderStatus, "status", "") or "Submitted"
        return OrderResult(order_id=oid, status=_norm_status(status), symbol=req.symbol,
                           submitted_qty=req.quantity, raw={"tif": order.tif}, side=req.side, tag=req.client_tag)

    def modify_stop(self, order_id: str, stop_price: Optional[float] = None,
                    quantity: Optional[float] = None) -> OrderResult:
        """Change a resting stop's trigger and shares in place - IBKR takes the same order id again."""
        self._guard_orders()
        trade = self._find_trade(order_id)
        if trade is None:
            raise OrderRejected(f"IBKR: no live order {order_id} to modify")
        order = trade.order
        if stop_price is not None:
            order.auxPrice = float(stop_price)
        if quantity is not None:
            order.totalQuantity = abs(float(quantity))
        changed = self._session.call(lambda ib: ib.placeOrder(trade.contract, order), timeout=10)
        return self._result(changed)

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
            trades = self._open_orders()
        else:
            trades = self._session.call(lambda ib: list(ib.trades()), timeout=8) or []
        results = [self._result(t) for t in trades]
        return [r for r in results if not status or status.upper() in (r.status, "OPEN", "WORKING")]

    def _open_orders(self) -> list:
        """Every open order on the account, including ones an earlier run of the app left working.

        One request at a time: ib_async keeps a single slot for it, so a second request sent while the
        first is still being answered leaves the first waiting for an answer that never comes, and can
        cut the second one short. A caller arriving while a request is out waits for that same answer
        instead. A request that takes too long is called off and raises - an unanswered request must
        never read as "no orders"."""
        with self._orders_lock:
            asked, lead = self._orders_asked, self._orders_asked is None
            if lead:
                asked = self._orders_asked = Future()
        if lead:
            try:
                # a few seconds past the request's own limit: time for a busy loop to get to it
                answer = self._session.run_coro(self._ask_open_orders, timeout=self.OPEN_ORDERS_TIMEOUT_S + 5)
                asked.set_result(answer)
            except BaseException as e:  # noqa: BLE001 - whatever ended it, every caller waiting hears the same
                asked.set_exception(e)
            finally:
                with self._orders_lock:
                    self._orders_asked = None
        try:
            return asked.result(timeout=self.OPEN_ORDERS_TIMEOUT_S + 10) or []
        except (FutureTimeout, asyncio.TimeoutError) as e:     # asyncio's timeout, or what it becomes crossing threads
            raise BrokerError(f"IBKR's open orders didn't arrive within {self.OPEN_ORDERS_TIMEOUT_S:.0f} s") from e

    async def _ask_open_orders(self, ib):
        # reqAllOpenOrdersAsync hands back a future, not a coroutine, so it is awaited in one. Timing out
        # cancels that future; an answer arriving after it finds no one waiting
        return await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), self.OPEN_ORDERS_TIMEOUT_S)

    def news_headlines(self, con_ids: Mapping[str, int], days: int = 3, per_symbol: int = 10,
                       since: Optional[dt.datetime] = None,
                       until: Optional[dt.datetime] = None) -> Dict[str, List[Tuple[dt.datetime, str, str, str]]]:
        """Headlines per stock from every news feed this account can read - the last ``days``, or from
        ``since`` to ``until`` (UTC) - as (time in UTC, provider code, article id, raw headline). Empty when
        not connected."""
        if not self.is_connected or not con_ids:
            return {}
        # ib_async reads a naive time as the computer's local time, so the times carry their zone
        since = since or dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)

        async def run(ib):
            # reqNewsProvidersAsync hands back a future, not a coroutine, so it is awaited in one
            codes = "+".join(p.code for p in (await ib.reqNewsProvidersAsync()) or [])
            out: Dict[str, List[Tuple[dt.datetime, str, str, str]]] = {}
            for symbol, con_id in (con_ids.items() if codes else ()):
                try:
                    found = await asyncio.wait_for(ib.reqHistoricalNewsAsync(con_id, codes, since, until or "", per_symbol), 10)
                except Exception:  # noqa: BLE001
                    continue
                out[symbol] = [(h.time, h.providerCode, h.articleId, h.headline) for h in (found or [])]
            return out

        return self._session.run_coro(run, timeout=15 + 10 * len(con_ids)) or {}

    def _result(self, t) -> OrderResult:
        o, os_ = t.order, t.orderStatus
        kind = getattr(o, "orderType", "") or ""
        side = Side.LONG if o.action == "BUY" else Side.SHORT
        fills = [Fill(order_id=str(o.orderId), symbol=t.contract.symbol, side=side,
                      quantity=float(f.execution.shares), price=float(f.execution.price))
                 for f in (t.fills or [])]
        return OrderResult(order_id=str(o.orderId), status=_norm_status(os_.status), symbol=t.contract.symbol,
                           submitted_qty=float(o.totalQuantity or 0.0), filled_qty=float(os_.filled or 0.0),
                           avg_fill_price=float(os_.avgFillPrice or 0.0), fills=fills, message=_order_message(t),
                           side=side, tag=getattr(o, "orderRef", "") or "",
                           order_type=_ORDER_TYPES.get(kind, kind), limit_price=_price(getattr(o, "lmtPrice", None)),
                           stop_price=_price(getattr(o, "trailStopPrice" if kind == "TRAIL" else "auxPrice", None)),
                           tif=getattr(o, "tif", "") or "",
                           raw={"mine": getattr(o, "clientId", None) == self.client_id,
                                "parent_id": str(getattr(o, "parentId", 0) or "") or None})


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


_ORDER_TYPES = {"LMT": "LIMIT", "MKT": "MARKET", "STP": "STOP", "STP LMT": "STOP_LIMIT", "TRAIL": "TRAILING_STOP",
                "MOC": "MARKET_ON_CLOSE", "LOC": "LIMIT_ON_CLOSE", "MIT": "MARKET_IF_TOUCHED", "LIT": "LIMIT_IF_TOUCHED"}


def _price(value) -> Optional[float]:
    """An order's price, or None where IBKR leaves it unset (zero, or its huge "unset" number)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if 0 < v < 1e300 else None


def _quote_from_ticker(symbol: str, tk: Any, source: str) -> Optional[Quote]:
    """The quote a Ticker holds - the one reading a snapshot and a stream both go through, so the two give the
    same price. Call it on the loop thread, where ib_async writes the Ticker: read from another thread, the
    price could come from one packet and the spread from the next. None when there is no price to give."""
    last = _price(tk.last) or _price(tk.close) or _price(getattr(tk, "marketPrice", None))
    bid, ask = _price(tk.bid), _price(tk.ask)
    if not last and not (bid and ask):
        return None                   # no price at all, or one side of the book and no trade to make up the other
    spread = max(0.01, (last or bid or ask) * 0.0005)
    bid = bid or round(last - spread, 2)
    ask = ask or round(last + spread, 2)
    when = getattr(tk, "time", None)
    if not isinstance(when, dt.datetime):
        when = dt.datetime.now(dt.timezone.utc)
    elif when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return Quote(symbol=symbol, bid=bid, ask=ask, last=last or (bid + ask) / 2, volume=_price(tk.volume),
                 ts=when, source=source)


def _qualified(contract: Any) -> bool:
    """IBKR knows the contract: it carries IBKR's contract id."""
    try:
        return int(getattr(contract, "conId", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


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
