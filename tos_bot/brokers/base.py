"""The contract every brokerage / exchange adapter must satisfy.

Keep this surface small and synchronous. Adapters translate to/from their
native SDK (schwab-py, ib_async, ccxt, ...). Anything the engine needs that a
venue cannot do should raise :class:`NotSupported` rather than silently no-op.
"""

from __future__ import annotations

import abc
import datetime as dt
from typing import Dict, List, Optional

import pandas as pd

from ..core.enums import AssetClass, OrderType, Side, TimeInForce
from ..core.models import Account, OrderRequest, OrderResult, Position, Quote
from ..util import clock


class BrokerError(RuntimeError):
    pass


class NotSupported(BrokerError):
    pass


class OrderRejected(BrokerError):
    pass


class AuthError(BrokerError):
    pass


class BrokerAdapter(abc.ABC):
    #: short identifier, matches config BROKER value
    name: str = "base"
    asset_classes: tuple = (AssetClass.EQUITY, AssetClass.ETF)
    supports_shorting: bool = True
    supports_fractional: bool = False
    supports_bracket_native: bool = False   # can the venue attach OCO children itself?
    paper: bool = False

    # -- connection ---------------------------------------------------- #
    @abc.abstractmethod
    def connect(self) -> None:
        ...

    def close(self) -> None:  # optional
        pass

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool:
        ...

    # -- account ----------------------------------------------------- #
    @abc.abstractmethod
    def get_account(self) -> Account:
        ...

    def get_positions(self) -> List[Position]:
        return self.get_account().positions

    # -- market data ----------------------------------------------- #
    @abc.abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        ...

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        return {s: self.get_quote(s) for s in symbols}

    @abc.abstractmethod
    def get_price_history(
        self,
        symbol: str,
        interval: str = "5m",
        lookback_days: int = 10,
        start: Optional[dt.datetime] = None,
        end: Optional[dt.datetime] = None,
        extended_hours: bool = False,
    ) -> pd.DataFrame:
        """Return an OHLCV frame: columns open/high/low/close/volume,
        tz-aware DatetimeIndex, oldest first. ``interval`` in
        {1m,5m,10m,15m,30m,1h,1d,1wk}."""

    # -- orders ---------------------------------------------------- #
    @abc.abstractmethod
    def place_order(self, req: OrderRequest) -> OrderResult:
        ...

    def place_bracket(
        self,
        entry: OrderRequest,
        take_profit: Optional[float],
        stop_loss: Optional[float],
    ) -> OrderResult:
        """Default: place the entry, then (once the venue reports it) the
        caller is responsible for attaching children. Adapters whose venue
        supports OCO natively should override and set
        ``supports_bracket_native = True``."""
        entry.take_profit = take_profit
        entry.stop_loss = stop_loss
        return self.place_order(entry)

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> None:
        ...

    @abc.abstractmethod
    def get_order(self, order_id: str) -> OrderResult:
        ...

    def list_orders(self, status: Optional[str] = None) -> List[OrderResult]:
        return []

    # -- misc ---------------------------------------------------- #
    def market_is_open(self) -> bool:
        return clock.is_market_open()

    def normalize_price(self, price: float, tick: float = 0.01) -> float:
        return round(round(price / tick) * tick, 6)
