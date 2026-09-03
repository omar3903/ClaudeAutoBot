"""Broker adapters.

`get_broker(name, ...)` is the only entry point the rest of the app uses.
Adapters implement :class:`BrokerAdapter`. Live trading currently ships:

* ``paper``  - fully simulated, no credentials, always available
* ``schwab`` - live, via ``schwab-py`` (successor to the decommissioned tda-api)
* ``tda``    - reference only; TD Ameritrade's API no longer issues tokens
* ``ibkr`` / ``crypto`` - stubs with the interface mapped out for later
"""

from __future__ import annotations

from typing import Any

from .base import BrokerAdapter, BrokerError, OrderRejected


def get_broker(name: str, **kwargs: Any) -> BrokerAdapter:
    name = (name or "paper").lower()
    if name == "paper":
        from .paper_adapter import PaperBroker

        return PaperBroker(**kwargs)
    if name == "schwab":
        from .schwab_adapter import SchwabBroker

        return SchwabBroker(**kwargs)
    if name == "tda":
        from .tda_adapter import TdaBroker

        return TdaBroker(**kwargs)
    if name == "ibkr":
        from .ibkr_adapter import IbkrBroker

        return IbkrBroker(**kwargs)
    if name == "crypto":
        from .crypto_adapter import CryptoBroker

        return CryptoBroker(**kwargs)
    raise BrokerError(f"unknown broker '{name}'")


__all__ = ["BrokerAdapter", "BrokerError", "OrderRejected", "get_broker"]
