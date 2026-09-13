"""Broker adapters. ``get_broker(name, **kwargs)`` is the only entry point the
rest of the app uses; every adapter implements :class:`BrokerAdapter`.

* ``paper``  - the built-in simulator, no credentials, always available
* ``ibkr``   - Interactive Brokers via ``ib_async`` -> IB Gateway / TWS (paper + live)
* ``schwab`` - Charles Schwab / thinkorswim via ``schwab-py`` (live + real-time data)

Which one trades is decided by :mod:`tos_bot.brokers.venues`.
"""

from __future__ import annotations

from typing import Any

from .base import BrokerAdapter, BrokerError, OrderRejected


def get_broker(name: str, **kwargs: Any) -> BrokerAdapter:
    name = (name or "paper").lower()
    if name == "paper":
        from .paper_adapter import PaperBroker

        return PaperBroker(**kwargs)
    if name == "ibkr":
        from .ibkr_adapter import IbkrBroker

        return IbkrBroker(**kwargs)
    if name == "schwab":
        from .schwab_adapter import SchwabBroker

        return SchwabBroker(**kwargs)
    raise BrokerError(f"unknown broker '{name}' (use paper, ibkr or schwab)")


__all__ = ["BrokerAdapter", "BrokerError", "OrderRejected", "get_broker"]
