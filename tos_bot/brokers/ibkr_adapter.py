"""Interactive Brokers adapter - STUB.

Planned implementation: ``ib_async`` (maintained fork of ib_insync) talking
to a running TWS / IB Gateway on 127.0.0.1:7497 (paper) or :7496 (live).
IBKR has no 60-day-token problem - auth is the Gateway session - so the
:class:`~tos_bot.auth.token_manager.TokenManager` is a no-op for this broker.

Every method raises :class:`NotSupported` until wired up, so selecting
``BROKER=ibkr`` fails fast and loud rather than silently misbehaving.
"""

from __future__ import annotations

import datetime as dt
from typing import List, Optional

import pandas as pd

from ..core.enums import AssetClass
from ..core.models import Account, OrderRequest, OrderResult, Quote
from .base import BrokerAdapter, NotSupported

_TODO = "IBKR adapter not implemented yet - use BROKER=schwab or BROKER=paper."


class IbkrBroker(BrokerAdapter):
    name = "ibkr"
    asset_classes = (AssetClass.EQUITY, AssetClass.ETF, AssetClass.OPTION, AssetClass.FUTURE)
    supports_shorting = True
    supports_fractional = True

    def __init__(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 11,
                 token_manager=None) -> None:
        self.host, self.port, self.client_id = host, port, client_id
        self._ib = None

    def connect(self) -> None:
        # from ib_async import IB
        # self._ib = IB(); self._ib.connect(self.host, self.port, clientId=self.client_id)
        raise NotSupported(_TODO)

    @property
    def is_connected(self) -> bool:
        return False

    def get_account(self) -> Account:
        raise NotSupported(_TODO)

    def get_quote(self, symbol: str) -> Quote:
        raise NotSupported(_TODO)

    def get_price_history(self, symbol: str, interval: str = "5m", lookback_days: int = 10,
                          start: Optional[dt.datetime] = None, end: Optional[dt.datetime] = None,
                          extended_hours: bool = False) -> pd.DataFrame:
        raise NotSupported(_TODO)

    def place_order(self, req: OrderRequest) -> OrderResult:
        raise NotSupported(_TODO)

    def cancel_order(self, order_id: str) -> None:
        raise NotSupported(_TODO)

    def get_order(self, order_id: str) -> OrderResult:
        raise NotSupported(_TODO)
