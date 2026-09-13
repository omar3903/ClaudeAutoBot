"""Price history + quotes with a pluggable provider and on-disk cache.

Providers, in preference order used by the engine:
    1. the connected broker (best - real-time, entitled)
    2. YFinanceProvider  (free, delayed, good enough for a 5-min scanner)
    3. SyntheticProvider (deterministic random walk - offline / tests / demo)

Strategies and the scanner depend on :class:`MarketDataService`, never on a
provider directly, so swapping data sources changes one line.

Many symbols at once: :meth:`MarketDataService.get_price_histories` serves the
cache, then asks a provider that can batch (yfinance downloads dozens of
tickers per request) and fetches anything left concurrently. Request starts
are paced per provider (:class:`~tos_bot.util.ratelimit.RateLimiter`).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import pickle
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Protocol

import numpy as np
import pandas as pd

from ..config import PROJECT_ROOT
from ..core.models import Quote
from ..util.ratelimit import RateLimiter

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
_OHLCV = ["open", "high", "low", "close", "volume"]


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
    min_request_gap = 0.0
    quotes_are_synthetic = True

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
    #: Yahoo throttles aggressive clients - stagger request starts
    min_request_gap = 0.15
    #: its bid/ask are estimates around the last price, so a bar close is as good a quote
    quotes_are_synthetic = True
    #: tickers per batched download request
    batch_size = 40

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

    @staticmethod
    def _period(yf_int: str, lookback_days: int) -> str:
        # yfinance caps intraday history; clamp the period accordingly
        if yf_int == "1m":
            return f"{min(lookback_days, 7)}d"
        if yf_int in ("5m", "15m", "30m", "60m"):
            return f"{min(lookback_days, 59)}d"
        return f"{max(lookback_days, 5)}d"

    def history(
        self, symbol: str, interval: str, lookback_days: int, extended_hours: bool
    ) -> pd.DataFrame:
        if not self._ok:
            raise RuntimeError("yfinance unavailable")
        import yfinance as yf

        yf_int = _YF_INTERVAL.get(interval, "5m")
        df = yf.download(
            symbol, period=self._period(yf_int, lookback_days), interval=yf_int,
            prepost=extended_hours, auto_adjust=False, progress=False, threads=False,
        )
        if df is None or df.empty:
            raise RuntimeError(f"yfinance returned nothing for {symbol}")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = _normalize(df)
        if df.empty:
            raise RuntimeError(f"yfinance returned nothing for {symbol}")
        return df

    def history_many(
        self, symbols: List[str], interval: str, lookback_days: int, extended_hours: bool
    ) -> Dict[str, pd.DataFrame]:
        """One download request per ``batch_size`` tickers (yfinance fetches the
        tickers inside a request in parallel). Tickers with no data are absent."""
        if not self._ok:
            raise RuntimeError("yfinance unavailable")
        import yfinance as yf

        yf_int = _YF_INTERVAL.get(interval, "5m")
        out: Dict[str, pd.DataFrame] = {}
        for i in range(0, len(symbols), self.batch_size):
            chunk = symbols[i:i + self.batch_size]
            df = yf.download(
                chunk, period=self._period(yf_int, lookback_days), interval=yf_int,
                prepost=extended_hours, auto_adjust=False, progress=False, threads=True,
                group_by="ticker",
            )
            if df is None or df.empty:
                continue
            for sym in chunk:
                sub = _frame_for(df, sym)
                if sub is None:
                    continue
                sub = _normalize(sub)
                if not sub.empty:
                    out[sym] = sub
        return out

    def quote(self, symbol: str) -> Quote:
        if not self._ok:
            raise RuntimeError("yfinance unavailable")
        import yfinance as yf

        t = yf.Ticker(symbol)
        last = vol = 0.0
        try:
            fi = t.fast_info
            last = float(fi.get("last_price") or fi.get("lastPrice") or 0.0)
            vol = float(fi.get("last_volume") or 0.0)
        except Exception:  # noqa: BLE001
            pass
        if not last:
            h = self.history(symbol, "1m", 1, False)
            last = float(h["close"].iloc[-1])
        return quote_from_price(symbol, last, vol)


def quote_from_price(symbol: str, last: float, volume: float = 0.0) -> Quote:
    """A quote with an estimated spread, for feeds that only give a last price."""
    sp = max(0.01, last * 0.0005)
    return Quote(symbol=symbol, bid=round(last - sp, 2), ask=round(last + sp, 2),
                 last=round(last, 4), volume=volume)


def _frame_for(df: pd.DataFrame, symbol: str) -> Optional[pd.DataFrame]:
    """One ticker's columns out of a multi-ticker download (either level order)."""
    if not isinstance(df.columns, pd.MultiIndex):
        return df
    if symbol in df.columns.get_level_values(0):
        return df[symbol]
    if symbol in df.columns.get_level_values(1):
        return df.xs(symbol, axis=1, level=1)
    return None


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=lambda c: str(c).lower())
    if any(c not in df.columns for c in _OHLCV):
        return pd.DataFrame(columns=_OHLCV)
    df = df[_OHLCV].dropna()
    if df.empty:
        return df
    idx = df.index if df.index.tz is not None else df.index.tz_localize("UTC")
    df.index = idx.tz_convert("America/New_York")
    return df


# --------------------------------------------------------------------------- #
#  Service                                                                   #
# --------------------------------------------------------------------------- #
class MarketDataService:
    def __init__(
        self,
        providers: Optional[List[PriceProvider]] = None,
        cache: bool = True,
        min_interval_between_calls: float = 0.15,
        negative_cache_seconds: float = 1800.0,
        max_workers: int = 8,
    ) -> None:
        if providers is None:
            # Real feed if we have one, otherwise the synthetic demo feed - but
            # NEVER both. Mixing them silently fabricates data for symbols the
            # real feed can't return (e.g. a delisted ticker), which then turns
            # into a fake "play". Offline demo only when yfinance is absent.
            yfp = YFinanceProvider()
            providers = [yfp] if yfp.available else [SyntheticProvider()]
        self.providers = providers
        self.cache = cache
        self.max_workers = max_workers
        self._default_gap = min_interval_between_calls
        self._limiters: Dict[int, RateLimiter] = {}
        self._limiters_lock = threading.Lock()
        self._mem: Dict[str, tuple] = {}
        self._neg: Dict[str, float] = {}          # symbol -> ts of last "no data"
        self._neg_ttl = negative_cache_seconds

    @property
    def is_real(self) -> bool:
        return any(getattr(p, "name", "") != "synthetic" for p in self.providers)

    @property
    def quotes_are_synthetic(self) -> bool:
        """True when the top feed's quote is just a last price with an estimated spread."""
        return bool(self.providers) and bool(getattr(self.providers[0], "quotes_are_synthetic", False))

    def _neg_hit(self, symbol: str) -> bool:
        ts = self._neg.get(symbol)
        return ts is not None and (time.time() - ts) < self._neg_ttl

    def _limiter_for(self, prov) -> RateLimiter:
        with self._limiters_lock:
            lim = self._limiters.get(id(prov))
            if lim is None:
                gap = getattr(prov, "min_request_gap", None)
                lim = RateLimiter(self._default_gap if gap is None else gap)
                self._limiters[id(prov)] = lim
            return lim

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

    def _store(self, symbol: str, interval: str, lookback_days: int, ext: bool,
               df: pd.DataFrame) -> pd.DataFrame:
        df = df[~df.index.duplicated(keep="last")].sort_index()
        if self.cache:
            self._write_cache(self._key(symbol, interval, lookback_days, ext), df)
        return df

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
        if self._neg_hit(symbol):
            raise RuntimeError(f"{symbol}: no data (negative-cached)")

        last_err: Optional[Exception] = None
        for prov in self.providers:
            try:
                self._limiter_for(prov).wait()
                df = prov.history(symbol, interval, lookback_days, extended_hours)
                if df is not None and not df.empty:
                    return self._store(symbol, interval, lookback_days, extended_hours, df)
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.debug("provider %s failed for %s: %s", prov.name, symbol, e)
        self._neg[symbol] = time.time()          # don't hammer a dead ticker
        raise RuntimeError(f"no market data for {symbol}: {last_err}")

    def get_price_histories(
        self,
        symbols: Iterable[str],
        interval: str = "1d",
        lookback_days: int = 400,
        extended_hours: bool = False,
    ) -> Dict[str, pd.DataFrame]:
        """Many symbols at once: cache hits first, then one batched request per
        chunk when the top provider can batch, then concurrent single fetches
        for whatever is left. Symbols with no data are absent from the result."""
        out: Dict[str, pd.DataFrame] = {}
        ttl = _CACHE_TTL.get(interval, 300)
        todo: List[str] = []
        for sym in dict.fromkeys(symbols):
            if self._neg_hit(sym):
                continue
            cached = (self._read_cache(self._key(sym, interval, lookback_days, extended_hours), ttl)
                      if self.cache else None)
            if cached is not None:
                out[sym] = cached
            else:
                todo.append(sym)
        if not todo:
            return out

        top = self.providers[0] if self.providers else None
        if top is not None and hasattr(top, "history_many"):
            try:
                self._limiter_for(top).wait()
                got = top.history_many(todo, interval, lookback_days, extended_hours)
            except Exception as e:  # noqa: BLE001
                log.debug("batched history failed on %s: %s", top.name, e)
                got = {}
            for sym, df in got.items():
                out[sym] = self._store(sym, interval, lookback_days, extended_hours, df)
            missed = [s for s in todo if s not in got]
            if len(self.providers) == 1:
                now = time.time()
                for sym in missed:                # the only feed has nothing - remember that
                    self._neg[sym] = now
                return out
            todo = missed

        def one(sym: str) -> Optional[pd.DataFrame]:
            try:
                return self.get_price_history(sym, interval, lookback_days, extended_hours)
            except Exception:  # noqa: BLE001
                return None

        with ThreadPoolExecutor(max_workers=max(1, min(self.max_workers, len(todo)))) as ex:
            for sym, df in zip(todo, ex.map(one, todo)):
                if df is not None:
                    out[sym] = df
        return out

    def get_daily(self, symbol: str, lookback_days: int = 400) -> pd.DataFrame:
        return self.get_price_history(symbol, "1d", lookback_days)

    def get_quote(self, symbol: str) -> Quote:
        if self._neg_hit(symbol):
            raise RuntimeError(f"{symbol}: no data (negative-cached)")
        last_err: Optional[Exception] = None
        for prov in self.providers:
            try:
                self._limiter_for(prov).wait()
                return prov.quote(symbol)
            except Exception as e:  # noqa: BLE001
                last_err = e
        raise RuntimeError(f"no quote for {symbol}: {last_err}")

    def get_quotes(self, symbols: Iterable[str]) -> Dict[str, Quote]:
        """Quotes for many symbols, fetched concurrently. Failures are skipped."""
        syms = [s for s in dict.fromkeys(symbols) if not self._neg_hit(s)]
        if not syms:
            return {}

        def one(sym: str) -> Optional[Quote]:
            try:
                return self.get_quote(sym)
            except Exception:  # noqa: BLE001
                return None

        with ThreadPoolExecutor(max_workers=max(1, min(self.max_workers, len(syms)))) as ex:
            return {s: q for s, q in zip(syms, ex.map(one, syms)) if q is not None}
