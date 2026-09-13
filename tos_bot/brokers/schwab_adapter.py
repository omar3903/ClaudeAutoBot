"""Charles Schwab / thinkorswim adapter via ``schwab-py``.

Sign in from the dashboard (Connections -> Sign in with Schwab) or with
``python scripts/authenticate.py``; that writes the token this adapter loads.
Schwab has no paper-trading API, so this is used for live orders and for
real-time data (the "thinkorswim" paper platform simulates fills on it).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Dict, List, Optional

import pandas as pd

from ..config import get_settings
from ..core.enums import AssetClass, OrderType, Side, TimeInForce
from ..core.models import Account, Fill, OrderRequest, OrderResult, Position, Quote
from .base import AuthError, BrokerAdapter, NotSupported, OrderRejected

log = logging.getLogger(__name__)


class SchwabBroker(BrokerAdapter):
    name = "schwab"
    supports_shorting = True
    supports_fractional = False
    supports_bracket_native = True

    _PH_METHODS = {
        "1m": "get_price_history_every_minute",
        "5m": "get_price_history_every_five_minutes",
        "10m": "get_price_history_every_ten_minutes",
        "15m": "get_price_history_every_fifteen_minutes",
        "30m": "get_price_history_every_thirty_minutes",
        "1d": "get_price_history_every_day",
        "1wk": "get_price_history_every_week",
    }

    def __init__(self, token_manager=None) -> None:
        self._client = None
        self._account_hash: Optional[str] = None
        self._connected = False
        self.token_manager = token_manager
        self._s = get_settings().secrets

    # -- connection -------------------------------------------------- #
    def connect(self) -> None:
        try:
            from schwab.auth import client_from_token_file
        except ImportError as e:  # pragma: no cover
            raise AuthError("schwab-py is not installed - pip install schwab-py") from e

        s = self._s
        if not (s.schwab_api_key and s.schwab_app_secret):
            raise AuthError("Schwab app key / secret not set - add them under Connections")
        if not s.token_path.exists():
            # never start an interactive login here - it would block the engine
            raise AuthError("not signed in to Schwab - use Connections -> Sign in with Schwab")
        self._client = client_from_token_file(str(s.token_path), s.schwab_api_key,
                                              s.schwab_app_secret)
        self._resolve_account_hash()
        self._connected = True
        log.info("Schwab connected (account …%s)", (self._s.schwab_account_id or "?")[-4:])

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None

    def _resolve_account_hash(self) -> None:
        want = (self._s.schwab_account_id or "").strip()
        r = self._client.get_account_numbers()
        r.raise_for_status()
        pairs = r.json()
        # [{"accountNumber": "...", "hashValue": "..."}]
        if not pairs:
            raise AuthError("no accounts returned by Schwab")
        if want:
            for p in pairs:
                if str(p.get("accountNumber")) == want:
                    self._account_hash = p["hashValue"]
                    return
        self._account_hash = pairs[0]["hashValue"]

    # -- token upkeep -------------------------------------------- #
    def refresh_if_needed(self, margin_s: int = 120) -> bool:
        """schwab-py refreshes the access token inside its httpx auth flow;
        a cheap authenticated call forces that when it is close to expiry."""
        if not self.is_connected:
            return False
        try:
            self._client.get_account_numbers().raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("schwab keepalive failed: %s", e)
            return False

    # -- account ---------------------------------------------- #
    def get_account(self) -> Account:
        from schwab.client import Client

        r = self._client.get_account(self._account_hash, fields=[Client.Account.Fields.POSITIONS])
        r.raise_for_status()
        data = r.json().get("securitiesAccount", r.json())
        bal = data.get("currentBalances", {})
        equity = bal.get("liquidationValue") or bal.get("equity") or 0.0
        positions: List[Position] = []
        for p in data.get("positions", []) or []:
            instr = p.get("instrument", {})
            qty = (p.get("longQuantity", 0.0) or 0.0) - (p.get("shortQuantity", 0.0) or 0.0)
            if abs(qty) < 1e-9:
                continue
            positions.append(Position(
                symbol=instr.get("symbol", "?"), quantity=qty,
                avg_price=p.get("averagePrice", 0.0) or 0.0,
                market_price=(p.get("marketValue", 0.0) or 0.0) / qty if qty else 0.0,
            ))
        return Account(
            account_id=str(data.get("accountNumber", self._s.schwab_account_id or "schwab")),
            equity=float(equity), cash=float(bal.get("cashBalance", 0.0) or 0.0),
            buying_power=float(bal.get("buyingPower", 0.0) or bal.get("cashAvailableForTrading", 0.0) or 0.0),
            day_trade_buying_power=float(bal.get("dayTradingBuyingPower", 0.0) or 0.0),
            is_cash_account=(data.get("type") == "CASH"),
            round_trips=int(data.get("roundTrips", 0) or 0),
            positions=positions, raw=data,
        )

    # -- market data --------------------------------------- #
    def get_quote(self, symbol: str) -> Quote:
        r = self._client.get_quote(symbol)
        r.raise_for_status()
        blob = r.json().get(symbol, {})
        q = blob.get("quote", blob)
        return Quote(
            symbol=symbol, bid=float(q.get("bidPrice", 0.0) or 0.0),
            ask=float(q.get("askPrice", 0.0) or 0.0),
            last=float(q.get("lastPrice", q.get("mark", 0.0)) or 0.0),
            volume=float(q.get("totalVolume", 0.0) or 0.0),
        )

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        if not symbols:
            return {}
        r = self._client.get_quotes(symbols)
        r.raise_for_status()
        out: Dict[str, Quote] = {}
        for sym, blob in r.json().items():
            q = blob.get("quote", blob)
            out[sym] = Quote(symbol=sym, bid=float(q.get("bidPrice", 0.0) or 0.0),
                             ask=float(q.get("askPrice", 0.0) or 0.0),
                             last=float(q.get("lastPrice", 0.0) or 0.0),
                             volume=float(q.get("totalVolume", 0.0) or 0.0))
        return out

    def get_price_history(
        self, symbol: str, interval: str = "5m", lookback_days: int = 10,
        start: Optional[dt.datetime] = None, end: Optional[dt.datetime] = None,
        extended_hours: bool = False,
    ) -> pd.DataFrame:
        method_name = self._PH_METHODS.get(interval)
        if method_name is None:
            raise NotSupported(f"interval {interval} not supported by Schwab adapter")
        method = getattr(self._client, method_name)
        end = end or dt.datetime.now(dt.timezone.utc)
        start = start or (end - dt.timedelta(days=lookback_days))
        r = method(symbol, start_datetime=start, end_datetime=end,
                   need_extended_hours_data=extended_hours)
        r.raise_for_status()
        candles = r.json().get("candles", [])
        if not candles:
            raise RuntimeError(f"Schwab returned no candles for {symbol}")
        df = pd.DataFrame(candles)
        df["datetime"] = pd.to_datetime(df["datetime"], unit="ms", utc=True)
        df = df.set_index("datetime").tz_convert("America/New_York")
        return df.rename(columns={"open": "open", "high": "high", "low": "low",
                                  "close": "close", "volume": "volume"})[
            ["open", "high", "low", "close", "volume"]]

    # -- orders ------------------------------------------ #
    def place_order(self, req: OrderRequest) -> OrderResult:
        spec = self._build_spec(req)
        r = self._client.place_order(self._account_hash, spec)
        if r.status_code >= 300:
            raise OrderRejected(f"Schwab rejected order: {r.status_code} {r.text[:300]}")
        try:
            from schwab.utils import Utils

            order_id = Utils(self._client, self._account_hash).extract_order_id(r)
        except Exception:  # noqa: BLE001
            order_id = r.headers.get("Location", "").rstrip("/").split("/")[-1]
        return OrderResult(order_id=str(order_id), status="SUBMITTED", symbol=req.symbol,
                           submitted_qty=req.quantity, raw={"location": r.headers.get("Location", "")})

    def place_bracket(self, entry: OrderRequest, take_profit, stop_loss) -> OrderResult:
        from schwab.orders.common import one_cancels_other

        try:
            from schwab.orders.equities import (
                equity_buy_limit, equity_sell_limit, equity_sell_short_limit,
                equity_buy_to_cover_limit,
            )
        except ImportError as e:  # pragma: no cover
            raise NotSupported("schwab-py order templates unavailable") from e

        qty = int(entry.quantity)
        px = entry.limit_price or 0.0
        if entry.side is Side.LONG:
            parent = equity_buy_limit(entry.symbol, qty, px)
            tp = equity_sell_limit(entry.symbol, qty, take_profit) if take_profit else None
            sl = equity_sell_limit(entry.symbol, qty, stop_loss) if stop_loss else None
        else:
            parent = equity_sell_short_limit(entry.symbol, qty, px)
            tp = equity_buy_to_cover_limit(entry.symbol, qty, take_profit) if take_profit else None
            sl = equity_buy_to_cover_limit(entry.symbol, qty, stop_loss) if stop_loss else None

        parent = parent.set_duration(self._duration(entry.tif))
        if tp is not None and sl is not None:
            parent = parent.add_child_order_strategy(one_cancels_other(tp, sl))
        elif tp is not None:
            parent = parent.add_child_order_strategy(tp)
        elif sl is not None:
            parent = parent.add_child_order_strategy(sl)

        r = self._client.place_order(self._account_hash, parent.build())
        if r.status_code >= 300:
            raise OrderRejected(f"Schwab rejected bracket: {r.status_code} {r.text[:300]}")
        loc = r.headers.get("Location", "")
        return OrderResult(order_id=loc.rstrip("/").split("/")[-1] or "unknown",
                           status="SUBMITTED", symbol=entry.symbol,
                           submitted_qty=entry.quantity, raw={"location": loc})

    def cancel_order(self, order_id: str) -> None:
        r = self._client.cancel_order(order_id, self._account_hash)
        if r.status_code >= 300:
            raise OrderRejected(f"cancel failed: {r.status_code} {r.text[:200]}")

    def get_order(self, order_id: str) -> OrderResult:
        r = self._client.get_order(order_id, self._account_hash)
        r.raise_for_status()
        o = r.json()
        leg = (o.get("orderLegCollection") or [{}])[0]
        act = o.get("orderActivityCollection") or []
        fills = []
        for a in act:
            for ex in a.get("executionLegs", []) or []:
                fills.append(Fill(order_id=str(order_id), symbol=leg.get("instrument", {}).get("symbol", "?"),
                                  side=Side.LONG if leg.get("instruction", "BUY").startswith("BUY") else Side.SHORT,
                                  quantity=ex.get("quantity", 0.0), price=ex.get("price", 0.0)))
        return OrderResult(order_id=str(order_id), status=o.get("status", "UNKNOWN"),
                           symbol=leg.get("instrument", {}).get("symbol", "?"),
                           submitted_qty=o.get("quantity", 0.0),
                           filled_qty=o.get("filledQuantity", 0.0),
                           avg_fill_price=(fills[-1].price if fills else 0.0),
                           fills=fills, raw=o)

    def list_orders(self, status: Optional[str] = None) -> List[OrderResult]:
        r = self._client.get_orders_for_account(self._account_hash)
        r.raise_for_status()
        out = []
        for o in r.json():
            if status and o.get("status") != status:
                continue
            out.append(OrderResult(order_id=str(o.get("orderId")), status=o.get("status", ""),
                                   symbol=(o.get("orderLegCollection") or [{}])[0]
                                   .get("instrument", {}).get("symbol", "?"),
                                   submitted_qty=o.get("quantity", 0.0),
                                   filled_qty=o.get("filledQuantity", 0.0), raw=o))
        return out

    # -- helpers -------------------------------------- #
    def _duration(self, tif: TimeInForce):
        from schwab.orders.common import Duration

        return {TimeInForce.DAY: Duration.DAY, TimeInForce.GTC: Duration.GOOD_TILL_CANCEL,
                TimeInForce.IOC: Duration.IMMEDIATE_OR_CANCEL,
                TimeInForce.FOK: Duration.FILL_OR_KILL}.get(tif, Duration.DAY)

    def _build_spec(self, req: OrderRequest):
        from schwab.orders.equities import (
            equity_buy_limit, equity_buy_market, equity_sell_limit, equity_sell_market,
            equity_sell_short_limit, equity_sell_short_market, equity_buy_to_cover_limit,
            equity_buy_to_cover_market,
        )

        qty = int(req.quantity)
        entry = req.is_entry
        limit = req.limit_price
        if req.side is Side.LONG and entry:
            b = equity_buy_limit(req.symbol, qty, limit) if limit else equity_buy_market(req.symbol, qty)
        elif req.side is Side.SHORT and entry:
            b = (equity_sell_short_limit(req.symbol, qty, limit) if limit
                 else equity_sell_short_market(req.symbol, qty))
        elif req.side is Side.SHORT and not entry:   # closing a long
            b = equity_sell_limit(req.symbol, qty, limit) if limit else equity_sell_market(req.symbol, qty)
        else:                                        # buy to cover a short
            b = (equity_buy_to_cover_limit(req.symbol, qty, limit) if limit
                 else equity_buy_to_cover_market(req.symbol, qty))
        return b.set_duration(self._duration(req.tif)).build()
