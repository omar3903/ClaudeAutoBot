"""The contract every broker adapter satisfies.

The surface is small and synchronous. Anything the engine needs that a venue
can't do raises :class:`NotSupported` rather than silently doing nothing.
"""

from __future__ import annotations

import abc
from typing import List, Optional

from ..core.models import Account, OrderRequest, OrderResult, Quote


class BrokerError(RuntimeError):
    pass


class NotSupported(BrokerError):
    pass


class OrderRejected(BrokerError):
    pass


class AuthError(BrokerError):
    pass


class BrokerAdapter(abc.ABC):
    name: str = "base"
    #: can the venue hold the stop and target itself as a one-cancels-other bracket?
    supports_bracket_native: bool = False
    #: fills are simulated (the built-in simulator)
    paper: bool = False

    @abc.abstractmethod
    def connect(self) -> None:
        ...

    def close(self) -> None:
        pass

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool:
        ...

    @abc.abstractmethod
    def get_account(self) -> Account:
        ...

    @abc.abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        ...

    @abc.abstractmethod
    def place_order(self, req: OrderRequest) -> OrderResult:
        ...

    def place_bracket(self, entry: OrderRequest, take_profit: Optional[float],
                      stop_loss: Optional[float]) -> OrderResult:
        """Place the entry with its target and stop attached. Venues that can't
        hold a native bracket leave the exit to the ExitManager."""
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
