"""Test doubles: a synthetic IB Gateway, a fixed stock listing and no SEC filings.

Prices are a seeded random walk per symbol, so every run sees the same candles.
"""

from __future__ import annotations

import datetime as dt
import functools
import zlib
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from tos_bot.brokers import get_broker
from tos_bot.core.models import Account, Quote
from tos_bot.data.fundamentals import Financials, FundamentalsProvider
from tos_bot.data.listings import Listing
from tos_bot.data.market_data import quote_from_price
from tos_bot.scanner import schedule
from tos_bot.util import clock

NY = "America/New_York"
SYMBOLS = [f"T{i:02d}" for i in range(40)]

_SESSIONS = 300
_BARS_PER_SESSION = 78                    # 09:30-16:00 in 5-minute bars
_INDUSTRIES = (                           # (IBKR industry, category)
    ("Technology", "Semiconductors"), ("Technology", "Software"), ("Financial", "Banks"),
    ("Energy", "Oil&Gas"), ("Consumer, Non-cyclical", "Pharmaceuticals"), ("Industrial", "Aerospace/Defense"),
    ("Communications", "Internet"), ("Consumer, Cyclical", "Retail"), ("Utilities", "Electric"),
    ("Basic Materials", "Chemicals"), ("Financial", "REITS"),
)


def _seed(symbol: str, salt: int) -> int:
    return zlib.crc32(f"{symbol}:{salt}".encode())


@functools.lru_cache(maxsize=256)
def _daily(symbol: str, through: dt.date) -> pd.DataFrame:
    rng = np.random.default_rng(_seed(symbol, 0))
    close = rng.uniform(20, 250) * np.exp(np.cumsum(rng.normal(0.0003, 0.02, _SESSIONS)))
    open_ = np.concatenate([[close[0]], close[:-1]]) * (1 + rng.normal(0, 0.004, _SESSIONS))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.008, _SESSIONS)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.008, _SESSIONS)))
    index = pd.DatetimeIndex([pd.Timestamp(d) for d in clock.last_n_sessions(through, _SESSIONS)]).tz_localize(NY)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": rng.uniform(2e6, 8e6, _SESSIONS)}, index=index)


def daily_bars(symbol: str, sessions: int = _SESSIONS, through: Optional[dt.date] = None) -> pd.DataFrame:
    """Completed daily candles, through the last completed session unless told otherwise."""
    through = through or schedule.last_completed_session(clock.now_ny())
    return _daily(symbol, through).tail(sessions).copy()


@functools.lru_cache(maxsize=256)
def _intraday(symbol: str, today: dt.date) -> pd.DataFrame:
    days = clock.last_n_sessions(today, 5)
    rng = np.random.default_rng(_seed(symbol, 1))
    n = _BARS_PER_SESSION * len(days)
    start = float(daily_bars(symbol)["close"].iloc[-1])
    close = start * np.exp(np.cumsum(rng.normal(0.0, 0.0015, n)))
    open_ = np.concatenate([[start], close[:-1]])
    wiggle = np.abs(rng.normal(0, 0.001, n))
    sessions = [pd.date_range(pd.Timestamp(f"{d} 09:30", tz=NY), periods=_BARS_PER_SESSION, freq="5min") for d in days]
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * (1 + wiggle),
                         "low": np.minimum(open_, close) * (1 - wiggle), "close": close,
                         "volume": rng.uniform(2e4, 9e4, n)}, index=sessions[0].append(sessions[1:]))


def intraday_bars(symbol: str) -> pd.DataFrame:
    """Five sessions of 5-minute candles, today's session last."""
    return _intraday(symbol, clock.session_date(clock.now_ny())).copy()


def fixed_quote(price: float = 100.0) -> Callable[[str], Quote]:
    return lambda symbol: quote_from_price(symbol, price)


def contract_details(symbol: str) -> Dict[str, object]:
    industry, category = _INDUSTRIES[zlib.crc32(symbol.encode()) % len(_INDUSTRIES)]
    return {"con_id": zlib.crc32(symbol.encode()) % 10**8 + 1, "exchange": "NASDAQ",
            "stock_type": "COMMON", "industry": industry, "category": category}


def _sessions(duration: str) -> int:
    amount, unit = duration.split()
    return int(amount) * 252 if unit == "Y" else int(amount)


class FakeGateway:
    """A connected IB Gateway as far as the app can tell: prices, contract
    details and an account. It never takes an order."""

    name = "ibkr"
    paper = False
    supports_bracket_native = False

    def __init__(self, symbols: Sequence[str] = (), delayed: bool = False, equity: float = 50_000.0) -> None:
        self.symbols = set(symbols)
        self.delayed = delayed
        self.equity = equity
        self.connected = False
        self.kw: Dict[str, object] = {}                     # how the app asked for the connection
        self.requests: List[Tuple[str, str, str]] = []      # (symbol, bar size, duration)

    def knows(self, symbol: str) -> bool:
        return not self.symbols or symbol in self.symbols

    @property
    def quotes_from_bars(self) -> bool:
        return self.delayed

    @property
    def is_connected(self) -> bool:
        return self.connected

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def refresh_if_needed(self) -> bool:
        return True

    def session_status(self) -> Dict[str, object]:
        return {"connected": self.connected, "reconnecting": False, "message": "connected", "port": 4002,
                "market_data": "delayed" if self.delayed else "live"}

    def get_account(self) -> Account:
        return Account(account_id="DU1234567", equity=self.equity, cash=self.equity, buying_power=2 * self.equity)

    def get_quote(self, symbol: str) -> Quote:
        return quote_from_price(symbol, float(intraday_bars(symbol)["close"].iloc[-1]))

    def history_many(self, requests: Mapping[str, Tuple[str, str]],
                     con_ids: Optional[Mapping[str, int]] = None, end=None) -> Dict[str, pd.DataFrame]:
        out = {}
        for symbol, (bar, duration) in requests.items():
            self.requests.append((symbol, bar, duration))
            if self.knows(symbol):
                out[symbol] = daily_bars(symbol, _sessions(duration)) if bar == "1 day" else intraday_bars(symbol)
        return out

    def contract_details_many(self, symbols: Sequence[str]) -> Dict[str, Optional[dict]]:
        return {s: contract_details(s) if self.knows(s) else None for s in symbols}

    def list_orders(self, status: Optional[str] = None) -> list:
        return []


def broker_factory(gateway: FakeGateway) -> Callable[..., object]:
    """How the engine opens connections in tests: ``gateway`` is its IBKR
    connection, a Connections-panel test gets a throwaway one, and the
    simulator is the real thing."""
    def make(name: str, **kw):
        if name != "ibkr":
            return get_broker(name, **kw)
        if "client_id" in kw:
            return FakeGateway(sorted(gateway.symbols))
        gateway.kw = kw
        return gateway
    return make


class FakeListings:
    def __init__(self, symbols: Sequence[str]) -> None:
        self.symbols = list(symbols)

    def load(self, today: Optional[dt.date] = None) -> List[Listing]:
        return [Listing(s, f"{s} Inc", "NASDAQ") for s in self.symbols]


class NoFundamentals(FundamentalsProvider):
    def get(self, symbol: str) -> Optional[Financials]:
        return None
