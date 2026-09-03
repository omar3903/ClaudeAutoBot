"""Legacy / reference adapter for ``tda-api`` (TD Ameritrade).

⚠  The TD Ameritrade Developer API this wraps was decommissioned after the
Charles Schwab acquisition - ``api.tdameritrade.com`` no longer issues
tokens. This file is kept because the call surface it targets is what the
user's original documentation describes, and because ``schwab-py`` mirrors
it almost line-for-line (see :mod:`tos_bot.brokers.schwab_adapter`, which you
should use instead).

If you somehow have working legacy access, set ``BROKER=tda`` and this will
behave like the Schwab adapter.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Dict, List, Optional

import pandas as pd

from ..config import get_settings
from ..core.enums import Side, TimeInForce
from ..core.models import Account, Fill, OrderRequest, OrderResult, Position, Quote
from .base import AuthError, BrokerAdapter, NotSupported, OrderRejected

log = logging.getLogger(__name__)


class TdaBroker(BrokerAdapter):
    name = "tda"
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
        log.warning(
            "TdaBroker selected - the TD Ameritrade API is decommissioned. "
            "Use BROKER=schwab (schwab-py) unless you have confirmed legacy access."
        )
        self._client = None
        self._connected = False
        self.token_manager = token_manager
        self._s = get_settings().secrets
        self._account_id = self._s.schwab_account_id or os.getenv("TDA_ACCOUNT_ID", "")

    def connect(self) -> None:
        try:
            from tda.auth import client_from_token_file, easy_client
        except ImportError as e:  # pragma: no cover
            raise AuthError("tda-api not installed (and note: its API is retired).") from e
        key = self._s.tda_api_key
        redirect = self._s.tda_redirect_uri
        token_path = str(self._s.token_path)
        if not key:
            raise AuthError("TDA_API_KEY missing in .env")
        if os.path.exists(token_path):
            self._client = client_from_token_file(token_path, key)
        else:
            self._client = easy_client(api_key=key, redirect_uri=redirect, token_path=token_path)
        self._connected = True
        log.info("tda-api client created (legacy)")

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None

    def refresh_if_needed(self, margin_s: int = 120) -> bool:
        if not self.is_connected:
            return False
        try:
            self._client.get_user_principals().raise_for_status()
            return True
        except Exception:  # noqa: BLE001
            return False

    def get_account(self) -> Account:
        from tda.client import Client

        r = self._client.get_account(self._account_id, fields=[Client.Account.Fields.POSITIONS])
        r.raise_for_status()
        data = r.json().get("securitiesAccount", r.json())
        bal = data.get("currentBalances", {})
        positions: List[Position] = []
        for p in data.get("positions", []) or []:
            instr = p.get("instrument", {})
            qty = (p.get("longQuantity", 0.0) or 0.0) - (p.get("shortQuantity", 0.0) or 0.0)
            if abs(qty) < 1e-9:
                continue
            positions.append(Position(symbol=instr.get("symbol", "?"), quantity=qty,
                                      avg_price=p.get("averagePrice", 0.0) or 0.0))
        return Account(
            account_id=str(data.get("accountId", self._account_id)),
            equity=float(bal.get("liquidationValue", 0.0) or 0.0),
            cash=float(bal.get("cashBalance", 0.0) or 0.0),
            buying_power=float(bal.get("buyingPower", 0.0) or 0.0),
            day_trade_buying_power=float(bal.get("dayTradingBuyingPower", 0.0) or 0.0),
            is_cash_account=(data.get("type") == "CASH"),
            round_trips=int(data.get("roundTrips", 0) or 0),
            positions=positions, raw=data,
        )

    def get_quote(self, symbol: str) -> Quote:
        r = self._client.get_quote(symbol)
        r.raise_for_status()
        q = r.json().get(symbol, {})
        return Quote(symbol=symbol, bid=float(q.get("bidPrice", 0.0) or 0.0),
                     ask=float(q.get("askPrice", 0.0) or 0.0),
                     last=float(q.get("lastPrice", 0.0) or 0.0),
                     volume=float(q.get("totalVolume", 0.0) or 0.0))

    def get_price_history(self, symbol: str, interval: str = "5m", lookback_days: int = 10,
                          start=None, end=None, extended_hours: bool = False) -> pd.DataFrame:
        name = self._PH_METHODS.get(interval)
        if not name:
            raise NotSupported(f"interval {interval} unsupported")
        end = end or dt.datetime.now(dt.timezone.utc)
        start = start or (end - dt.timedelta(days=lookback_days))
        r = getattr(self._client, name)(symbol, start_datetime=start, end_datetime=end,
                                        need_extended_hours_data=extended_hours)
        r.raise_for_status()
        candles = r.json().get("candles", [])
        if not candles:
            raise RuntimeError(f"no candles for {symbol}")
        df = pd.DataFrame(candles)
        df["datetime"] = pd.to_datetime(df["datetime"], unit="ms", utc=True)
        return (df.set_index("datetime").tz_convert("America/New_York")
                [["open", "high", "low", "close", "volume"]])

    def place_order(self, req: OrderRequest) -> OrderResult:
        from tda.orders.equities import (
            equity_buy_limit, equity_buy_market, equity_sell_limit, equity_sell_market,
            equity_sell_short_limit, equity_sell_short_market,
        )
        from tda.utils import Utils

        qty = int(req.quantity)
        limit = req.limit_price
        if req.side is Side.LONG and req.is_entry:
            b = equity_buy_limit(req.symbol, qty, limit) if limit else equity_buy_market(req.symbol, qty)
        elif req.side is Side.SHORT and req.is_entry:
            b = (equity_sell_short_limit(req.symbol, qty, limit) if limit
                 else equity_sell_short_market(req.symbol, qty))
        elif req.side is Side.SHORT:
            b = equity_sell_limit(req.symbol, qty, limit) if limit else equity_sell_market(req.symbol, qty)
        else:
            b = equity_buy_market(req.symbol, qty)
        r = self._client.place_order(self._account_id, b.build())
        if r.status_code >= 300:
            raise OrderRejected(f"TDA rejected: {r.status_code} {r.text[:200]}")
        try:
            oid = Utils(self._client, self._account_id).extract_order_id(r)
        except Exception:  # noqa: BLE001
            oid = r.headers.get("Location", "").rstrip("/").split("/")[-1]
        return OrderResult(order_id=str(oid), status="SUBMITTED", symbol=req.symbol,
                           submitted_qty=req.quantity)

    def cancel_order(self, order_id: str) -> None:
        r = self._client.cancel_order(order_id, self._account_id)
        if r.status_code >= 300:
            raise OrderRejected(f"cancel failed: {r.status_code}")

    def get_order(self, order_id: str) -> OrderResult:
        r = self._client.get_order(order_id, self._account_id)
        r.raise_for_status()
        o = r.json()
        return OrderResult(order_id=str(order_id), status=o.get("status", "UNKNOWN"),
                           symbol=(o.get("orderLegCollection") or [{}])[0]
                           .get("instrument", {}).get("symbol", "?"),
                           submitted_qty=o.get("quantity", 0.0),
                           filled_qty=o.get("filledQuantity", 0.0), raw=o)
