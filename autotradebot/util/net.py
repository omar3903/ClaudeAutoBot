"""Tiny network helpers."""

from __future__ import annotations

import socket
from typing import Optional

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def port_is_open(host: str, port: int, timeout: Optional[float] = None) -> bool:
    """True if something accepts TCP connections on ``host:port`` (nothing is sent).

    Windows doesn't refuse a closed localhost port straight away - the connect
    waits out the whole timeout - so loopback checks default to a short one. A
    listening local socket answers in microseconds anyway."""
    if timeout is None:
        timeout = 0.25 if host in _LOOPBACK else 1.5
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False
