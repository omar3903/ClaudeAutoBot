"""The contract every broker adapter satisfies.

The surface is small and synchronous. Anything the engine needs that a venue
can't do raises :class:`NotSupported` rather than silently doing nothing.
"""

from __future__ import annotations

import abc
from typing import List, Optional

from ..core.models import Account, Fill, OrderRequest, OrderResult, Quote


#: order states after which an order never fills any further
DONE_STATUSES = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED"})


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
    #: can it rest a stand-alone stop order for a position (execution/protective_stops.py)?
    supports_native_stop: bool = False
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

    def modify_stop(self, order_id: str, stop_price: Optional[float] = None,
                    quantity: Optional[float] = None) -> OrderResult:
        """Change a resting stop order's trigger and shares in place; a ``quantity`` of None leaves the
        shares as the venue holds them. Venues that can't raise, and the stop is cancelled and placed again."""
        raise NotImplementedError(f"{self.name} can't modify a resting order")

    @abc.abstractmethod
    def get_order(self, order_id: str) -> OrderResult:
        ...

    def list_orders(self, status: Optional[str] = None) -> List[OrderResult]:
        return []

    def get_fills(self, symbol: Optional[str] = None) -> List[Fill]:
        """The fills the venue reports for the current session, oldest first - what books a
        position that was closed outside the app. Venues that can't say return []."""
        return []
