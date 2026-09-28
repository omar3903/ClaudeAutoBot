"""Broker adapters: the built-in simulator and Interactive Brokers.

``get_broker(name, **kwargs)`` is the one entry point; which one trades is
decided by :mod:`autotradebot.brokers.venues`.
"""

from __future__ import annotations

from typing import Any

from .base import BrokerAdapter, BrokerError, OrderRejected


def get_broker(name: str, **kwargs: Any) -> BrokerAdapter:
    if name == "paper":
        from .paper_adapter import PaperBroker

        return PaperBroker(**kwargs)
    if name == "ibkr":
        from .ibkr_adapter import IbkrBroker

        return IbkrBroker(**kwargs)
    raise BrokerError(f"unknown broker '{name}' (use paper or ibkr)")


__all__ = ["BrokerAdapter", "BrokerError", "OrderRejected", "get_broker"]
