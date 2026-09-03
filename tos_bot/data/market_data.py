"""Price history + quotes with a pluggable provider and on-disk cache.

Providers, in preference order used by the engine:
    1. the connected broker (best - real-time, entitled)
    2. YFinanceProvider  (free, delayed, good enough for a 5-min scanner)
    3. SyntheticProvider (deterministic random walk - offline / tests / demo)

Strategies and the scanner depend on :class:`MarketDataService`, never on a
provider directly, so swapping data sources changes one line.
"""

from __future__ import annotations

import datetime as dt
import datetime as _dt
import hashlib
import logging
import pickle
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Protocol

import numpy as np
import pandas as pd

from ..config import PROJECT_ROOT
from ..core.models import Quote

log = logging.getLogger(__name__)

_CACHE_DIR = PROJECT_ROOT / "data" / "cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# how long a cached bar frame stays fresh, by interval
_CACHE_TTL = {
    "1m": 60, "5m": 180, "10m": 300, "15m": 300, "30m": 600,
    "1h": 900, "1d": 3600, "1wk": 6 * 3600,
}

_YF_INTERVAL = {
    "1m": "1m", "5m": "5m", "10m": "15m", "15m": "15m", "30m": "30m",
    "1h": "60m", "1d": "1d", "1wk": "1wk",
}


class PriceProvider(Protocol):
    name: str

    def history(
        self, symbol: str, interval: str, lookback_days: int, extended_hours: bool
    ) -> pd.DataFrame: ...

    def quote(self, symbol: str) -> Quote: ...


# --------------------------------------------------------------------------- #
#  Synthetic (offline)                                                       #
# --------------------------------------------------------------------------- #
class SyntheticProvider:
    name = "synthetic"

    def __init__(self, seed: int = 7, base_price: float = 100.0) -> None:
        self._seed = seed
        self._base = base_price

    def _rng(self, symbol: str, interval: str) -> np.random.Generator:
        h = int(hashlib.md5(f"{symbol}:{interval}:{self._seed}".encode()).hexdigest(), 16)
        return np.random.default_rng(h % (2**32))

    def _sessions(self, count: int) -> list:
        from ..util import clock

        out, d = [], clock.session_date()
        while len(out) < count:
            if clock.is_trading_day(d):
                out.append(d)
            d = d - _dt.timedelta(days=1)
        return list(reversed(out))

    def history(
        self, symbol: str, interval: str, lookback_days: int, extended_hours: bool
    ) -> pd.DataFrame:
        """Build a realistic index: regular-hours intraday bars per session, or
        one bar per session for daily/weekly. Timestamps are made in UTC then
        converted to NY so DST spring-forward gaps never bite."""
        rng = self._rng(symbol, interval)
        step_min = {"1m": 1, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60}.get(interval)

        stamps: list = []
        if step_min is not None:                       # intraday
            sessions = self._sessions(min(max(lookback_days, 3), 20))
            for d in sessions:
                t = _dt.datetime(d.year, d.month, d.day, 9, 30)
                end_t = _dt.datetime(d.year, d.month, d.day, 16, 0)
                while t <= end_t:
                    stamps.append(t)
                    t += _dt.timedelta(minutes=step_min)
        elif interval == "1wk":
            sessions = self._sessions(min(max(lookback_days, 20), 500))
            seen = set()
            for d in sessions:
                wk = d.isocalendar()[:2]
                if wk not in seen:
                    seen.add(wk)
                    stamps.append(_dt.datetime(d.year, d.month, d.day, 16, 0))
        else:                                          # daily
            sessions = self._sessions(min(max(lookback_days, 20), 500))
            stamps = [_dt.datetime(d.year, d.month, d.day, 16, 0) for d in sessions]

        idx = pd.DatetimeIndex(stamps).tz_localize("America/New_York",
                                                   nonexistent="shift_forward",
                                                   ambiguous="NaT")
        idx = idx[~idx.isna()]
        n = len(idx)
        if n < 10:
            raise RuntimeError("synthetic index too short")

        start_price = self._base * float(rng.uniform(0.3, 3.0))
        bar_vol = {"1m": 0.0009, "5m": 0.0016, "10m": 0.0022, "15m": 0.0027,
                   "30m": 0.0035, "1h": 0.005, "1d": 0.018, "1wk": 0.04}.get(interval, 0.002)
        drift = float(rng.normal(0, bar_vol * 0.05))
        rets = rng.normal(drift, bar_vol, n)
        for k in rng.choice(n, size=max(1, n // 120), replace=False):
            rets[k] += rng.normal(0, bar_vol * 8)
        close = start_price * np.exp(np.cumsum(rets))
        wig = close * bar_vol
        high = close + np.abs(rng.normal(wig, wig * 0.5))
        low = close - np.abs(rng.normal(wig, wig * 0.5))
        open_ = np.concatenate([[close[0]], close[:-1]])
        per_bar = {"1m": 1, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60,
                   "1d": 390, "1wk": 1950}.get(interval, 5)
        base_vol = float(rng.uniform(3e5, 5e6)) * (per_bar / 390.0)
        volume = np.abs(rng.normal(base_vol, base_vol * 0.4, n))
        return pd.DataFrame(
            {"open": open_, "high": np.maximum.reduce([high, open_, close]),
             "low": np.minimum.reduce([low, open_, close]), "close": close,
             "volume": volume},
            index=idx,
        )

    def quote(self, symbol: str) -> Quote:
        h = self.history(symbol, "1m", 1, False)
        last = float(h["close"].iloc[-1])
        sp = max(0.01, last * 0.0005)
        return Quote(symbol=symbol, bid=round(last - sp, 2), ask=round(last + sp, 2),
                     last=round(last, 2), volume=float(h["volume"].iloc[-1]))


# --------------------------------------------------------------------------- #
#  yfinance                                                                  #
# --------------------------------------------------------------------------- #
class YFinanceProvider:
    name = "yfinance"

    def __init__(self) -> None:
        try:
            import yfinance  # noqa: F401
            self._ok = True
        except Exception:  # noqa: BLE001
            self._ok = False
            log.warning("yfinance not installed - falling back to synthetic data")

    @property
    def available(self) -> bool:
        return self._ok

    def history(
        self, symbol: str, interval: str, lookback_days: int, extended_hours: bool
    ) -> pd.DataFrame:
        if not self._ok:
            raise RuntimeError("yfinance unavailable")
        import yfinance as yf

        yf_int = _YF_INTERVAL.get(interval, "5m")
        # yfinance caps intraday history; clamp period accordingly
        if yf_int in ("1m",):
            period = f"{min(lookback_days, 7)}d"
        elif yf_int in ("5m", "15m", "30m", "60m"):
            period = f"{min(lookback_days, 59)}d"
        else:
            period = f"{max(lookback_days, 5)}d"
        df = yf.download(
            symbol, period=period, interval=yf_int, prepost=extended_hours,
            auto_adjust=False, progress=False, threads=False,
        )
        if df is None or df.empty:
            raise RuntimeError(f"yfinance returned nothing for {symbol}")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert("America/New_York")
        return df.dropna()

    def quote(self, symbol: str) -> Quote:
        if not self._ok:
            raise RuntimeError("yfinance unavailable")
        import yfinance as yf

        t = yf.Ticker(symbol)
        last = bid = ask = vol = 0.0
        try:
            fi = t.fast_info
            last = float(fi.get("last_price") or fi.get("lastPrice") or 0.0)
            vol = float(fi.get("last_volume") or 0.0)
        except Exception:  # noqa: BLE001
            pass
        if not last:
            h = self.history(symbol, "1m", 1, False)
            last = float(h["close"].iloc[-1])
        sp = max(0.01, last * 0.0005)
        return Quote(symbol=symbol, bid=bid or round(last - sp, 2),
                     ask=ask or round(last + sp, 2), last=round(last, 4), volume=vol)


# --------------------------------------------------------------------------- #
#  Service                                                                   #
# --------------------------------------------------------------------------- #
class MarketDataService:
    def __init__(
        self,
        providers: Optional[List[PriceProvider]] = None,
        cache: bool = True,
        min_interval_between_calls: float = 0.15,
    ) -> None:
        if providers is None:
            providers = []
            yfp = YFinanceProvider()
            if yfp.available:
                providers.append(yfp)
            providers.append(SyntheticProvider())
        self.providers = providers
        self.cache = cache
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._min_gap = min_interval_between_calls
        self._mem: Dict[str, tuple] = {}

    # -- cache plumbing --------------------------------------------- #
    def _key(self, symbol: str, interval: str, lookback_days: int, ext: bool) -> str:
        return f"{symbol}_{interval}_{lookback_days}_{int(ext)}"

    def _cache_path(self, key: str) -> Path:
        return _CACHE_DIR / f"{key}.pkl"

    def _read_cache(self, key: str, ttl: int):
        now = time.time()
        hit = self._mem.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        p = self._cache_path(key)
        if p.exists() and now - p.stat().st_mtime < ttl:
            try:
                df = pickle.loads(p.read_bytes())
                self._mem[key] = (now, df)
                return df
            except Exception:  # noqa: BLE001
                return None
        return None

    def _write_cache(self, key: str, df: pd.DataFrame) -> None:
        self._mem[key] = (time.time(), df)
        try:
            self._cache_path(key).write_bytes(pickle.dumps(df))
        except Exception:  # noqa: BLE001
            pass

    def _throttle(self) -> None:
        with self._lock:
            wait = self._min_gap - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()

    # -- public api ---------------------------------------------- #
    def get_price_history(
        self,
        symbol: str,
        interval: str = "5m",
        lookback_days: int = 10,
        extended_hours: bool = False,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        key = self._key(symbol, interval, lookback_days, extended_hours)
        ttl = _CACHE_TTL.get(interval, 300)
        if self.cache and use_cache:
            cached = self._read_cache(key, ttl)
            if cached is not None:
                return cached

        last_err: Optional[Exception] = None
        for prov in self.providers:
            try:
                self._throttle()
                df = prov.history(symbol, interval, lookback_days, extended_hours)
                if df is not None and not df.empty:
                    df = df[~df.index.duplicated(keep="last")].sort_index()
                    if self.cache:
                        self._write_cache(key, df)
                    return df
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.debug("provider %s failed for %s: %s", prov.name, symbol, e)
        raise RuntimeError(f"no market data for {symbol}: {last_err}")

    def get_daily(self, symbol: str, lookback_days: int = 400) -> pd.DataFrame:
        return self.get_price_history(symbol, "1d", lookback_days)

    def get_quote(self, symbol: str) -> Quote:
        last_err: Optional[Exception] = None
        for prov in self.providers:
            try:
                self._throttle()
                return prov.quote(symbol)
            except Exception as e:  # noqa: BLE001
                last_err = e
        raise RuntimeError(f"no quote for {symbol}: {last_err}")

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        for s in symbols:
            try:
                out[s] = self.get_quote(s)
            except Exception:  # noqa: BLE001
                continue
        return out
