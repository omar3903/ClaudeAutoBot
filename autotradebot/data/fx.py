"""USD value of an account currency, from the European Central Bank's daily
reference rates (free, no key, published every working day).

US stocks trade in dollars, so an account kept in another currency - an IBKR
Canada account is in CAD - is converted to USD before a trade is sized. Rates
are cached for six hours; if a refresh fails the last good rate is kept, and if
there has never been one callers get ``None`` and must not size off a guess.
"""

from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET
from typing import Callable, Dict, Optional, Tuple

import requests

log = logging.getLogger(__name__)

_ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_TTL_S = 6 * 3600
_RETRY_S = 60.0

_lock = threading.Lock()
_cache: Dict[str, float] = {}          # currency -> units per EUR
_fetched_at = 0.0
_failed_at = -_RETRY_S


def ecb_rates() -> Dict[str, float]:
    """Units of each currency per 1 EUR."""
    r = requests.get(_ECB_URL, timeout=20)
    r.raise_for_status()
    rates = {c.get("currency"): float(c.get("rate"))
             for c in ET.fromstring(r.content).iter() if c.get("currency")}
    return {**rates, "EUR": 1.0}


def usd_per(currency: str, fetch: Callable[[], Dict[str, float]] = ecb_rates) -> Optional[float]:
    """USD value of one unit of ``currency``, or None if no rate is known."""
    cur = (currency or "USD").upper()
    if cur == "USD":
        return 1.0
    rates, ok = _rates(fetch)
    if not ok or "USD" not in rates or not rates.get(cur):
        return None
    return rates["USD"] / rates[cur]


def _rates(fetch: Callable[[], Dict[str, float]]) -> Tuple[Dict[str, float], bool]:
    global _fetched_at, _failed_at
    now = time.monotonic()
    with _lock:
        fresh = _cache and now - _fetched_at < _TTL_S
        backing_off = now - _failed_at < _RETRY_S
    if fresh or backing_off:
        return dict(_cache), bool(_cache)
    try:
        rates = fetch()
    except Exception as e:  # noqa: BLE001
        log.warning("exchange-rate download failed: %s", e)
        rates = {}
    with _lock:
        if rates:
            _cache.clear()
            _cache.update(rates)
            _fetched_at = now
        else:
            _failed_at = now
        return dict(_cache), bool(_cache)


def clear_cache() -> None:
    global _fetched_at, _failed_at
    with _lock:
        _cache.clear()
        _fetched_at, _failed_at = 0.0, -_RETRY_S
