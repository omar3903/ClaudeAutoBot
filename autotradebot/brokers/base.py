"""The contract every broker adapter satisfies.

The surface is small and synchronous. Anything the engine needs that a venue
can't do raises :class:`NotSupported` rather than silently doing nothing.
"""

from __future__ import annotations

import abc
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Dict, List, Optional

from ..core.models import Account, Fill, OrderRequest, OrderResult, Quote


#: order states after which an order never fills any further
DONE_STATUSES = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED"})


class BrokerError(RuntimeError):
    pass


class NotSupported(BrokerError):
    pass


class OrderRejected(BrokerError):
    pass


class OrderInDoubt(OrderRejected):
    """The broker won't change an order until it says what became of it - a cancel or a change it refused has left
    it working, as far as anyone knows. No refusal of the change: the order may well still rest, so it is waited
    for, never replaced."""


class AuthError(BrokerError):
    pass


class OrderOutcomeUnknown(BrokerError):
    """An order call that ran out of time after it had started: the order may have reached the broker, or not. It is
    never taken for "not sent" - it is looked for at the broker by its tag (``order_ref``, IBKR's orderRef) before
    anything is sent in its place."""

    def __init__(self, msg: str, order_ref: Optional[str] = None) -> None:
        super().__init__(msg)
        self.order_ref = order_ref


class OrderNotSent(BrokerError):
    """An order call that ran out of time before it started: it was called off unsent, so the order is certainly not
    at the broker and may be sent again."""


#: what an order call raises when whether the order went out isn't known: OrderOutcomeUnknown, or the bare timeout
#: of an adapter that doesn't translate it (concurrent.futures' and asyncio's are TimeoutError's own only from 3.11)
OUTCOME_UNKNOWN = (OrderOutcomeUnknown, TimeoutError, FutureTimeout, asyncio.TimeoutError)


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

    def order_errors(self) -> List[Dict[str, Any]]:
        """What the venue has said went wrong with the app's orders since the last call, oldest first, each told
        once: ``order_id``, ``code``, ``message``, ``what`` (in words), ``tag``, ``symbol``, ``at`` (UTC) and, for a
        cancel it refused, the ``state`` it named the order in. The executor writes them into the order audit.
        Venues that answer every order call on the spot have nothing more to tell."""
        return []
