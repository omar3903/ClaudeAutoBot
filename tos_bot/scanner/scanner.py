"""The scans: the daily full scan before the open, and the intraday cycles.

Full scan (once a day - see schedule.py):
    US listings -> IBKR contract details for symbols not seen before
    -> daily candles topped up -> daily heat for every stock (no requests)
    -> the day's hot list and sector buffers
    -> swing setups on the hottest liquid stocks
    -> valuation setups on the leaders (SEC EDGAR financials)

Cycle (every few minutes in the regular session):
    5-minute candles for the hot list, the kept buffer names and the next
    buffer picks -> day-trade and swing setups -> intraday heat -> buffer decisions

A fast cycle (while Autopilot day-trades) rescans only the hot list.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

import pandas as pd

from ..config import Settings
from ..core.enums import StrategyKind, Timeframe
from ..core.eventbus import BUS
from ..core.models import Account, Play
from ..data.fundamentals import Financials, FundamentalsProvider
from ..data.listings import UsListings
from ..data.market_data import MarketData
from ..data.sectors import sector_allowed
from ..data.symbols import SymbolMaster
from ..indicators import ta
from ..strategies.base import Strategy
from ..util import clock
from . import schedule
from .evaluator import evaluate
from .filters import TradeFilters
from .heat import DailyMetrics, daily_metrics, intraday_metrics, liquid, rank_by_daily_heat
from .schedule import ScanSettings
from .watchlist import Decision, DayWatchlist

log = logging.getLogger(__name__)

BENCHMARK = "SPY"                  # for beta
_CONTRACT_CHUNK = 500
#: a buffer stock whose setups fired this cycle counts as this much hotter
_PLAY_BONUS = 0.15


@dataclass
class ScanResult:
    kind: str                                   # full | cycle | fast
    run_id: str = field(default_factory=lambda: f"scan_{uuid.uuid4().hex[:10]}")
    started_at: dt.datetime = field(default_factory=clock.now_ny)
    finished_at: Optional[dt.datetime] = None
    universe_size: int = 0
    scanned: int = 0
    liquid: int = 0
    #: the symbols whose setups were looked at
    symbols: List[str] = field(default_factory=list)
    plays: List[Play] = field(default_factory=list)
    hot: List[str] = field(default_factory=list)
    decisions: List[Decision] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    timings: Dict[str, float] = field(default_factory=dict)
    elapsed_s: float = 0.0

    def summary(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "universe_size": self.universe_size, "scanned": self.scanned, "liquid": self.liquid,
            "n_plays": len(self.plays), "hot": self.hot,
            "decisions": {a: sum(d.action == a for d in self.decisions) for a in ("adopted", "kept", "dropped")},
            "errors": dict(list(self.errors.items())[:5]), "n_errors": len(self.errors),
            "elapsed_s": round(self.elapsed_s, 1),
            "timings": {k: round(v, 2) for k, v in self.timings.items()},
        }


class Scanner:
    SWING_LEADERS = 400
    PEERS = 6

    def __init__(self, settings: Settings, market_data: MarketData, symbols: SymbolMaster,
                 listings: UsListings, fundamentals: FundamentalsProvider, watchlist_dir: Path,
                 strategies: Sequence[Strategy]) -> None:
        self.settings = settings
        self.md = market_data
        self.symbols = symbols
        self.listings = listings
        self.fundamentals = fundamentals
        self.watchlist_dir = watchlist_dir
        self._watchlist_lock = threading.Lock()
        self.watchlist: Optional[DayWatchlist] = DayWatchlist.load_latest(watchlist_dir)
        self.filters = TradeFilters()
        self.account: Optional[Account] = None
        self.strategies: List[Strategy] = list(strategies)
        self._params = {"valuation": settings.config.valuation.model_dump()}

    def set_strategies(self, strategies: Sequence[Strategy]) -> None:
        """Swap the active setups. A scan already running keeps the set it started with."""
        self.strategies = list(strategies)

    def watchlist_state(self) -> Optional[Dict[str, Any]]:
        with self._watchlist_lock:
            return self.watchlist.state() if self.watchlist else None

    # ---- the full scan ----------------------------------------------------- #
    def run_full(self, scan: ScanSettings, now: Optional[dt.datetime] = None) -> ScanResult:
        now = now or clock.now_ny()
        cfg = self.settings.config.scanner
        filters, strategies = self.filters, list(self.strategies)
        result = ScanResult("full")

        with self._timed(result, "listings"):
            symbols = [listing.symbol for listing in self.listings.load(now.date())]
            if cfg.max_universe:
                symbols = symbols[:cfg.max_universe]
        result.universe_size = len(symbols)
        with self._timed(result, "contracts"):
            self._learn_contracts(symbols + [BENCHMARK], result)
        tradable = self.symbols.tradable(symbols)
        through = schedule.last_completed_session(now)
        with self._timed(result, "daily_candles"):
            everyone = tradable + [BENCHMARK]
            self.md.update_daily(everyone, through, self._con_ids(everyone),
                                 progress=lambda done, total: self._progress(result, "daily candles", done, total))
        with self._timed(result, "ranking"):
            ranked = self._rank(tradable, through, cfg.prefilter, result)
        result.liquid = len(ranked)
        ranked = [m for m in ranked if sector_allowed(self.symbols.sector(m.symbol), filters.sectors)]
        result.symbols = [m.symbol for m in ranked]

        watchlist = DayWatchlist.build(
            schedule.watchlist_session(now), through, ranked, self.symbols.sector,
            scan.hot_list_size, scan.sector_queue_size, universe=len(symbols), liquid=result.liquid)
        with self._watchlist_lock:
            self.watchlist = watchlist
            watchlist.save(self.watchlist_dir)
        result.hot = watchlist.hot_symbols()

        swing_on = "SWING" in filters.timeframes
        with self._timed(result, "swing_setups"):
            swing = [s for s in strategies if swing_on and s.kind is StrategyKind.TECHNICAL
                     and s.timeframe is Timeframe.SWING]
            for m in ranked[:self.SWING_LEADERS] if swing else []:
                daily = self.md.daily_frame(m.symbol)
                if daily is not None:
                    result.plays += evaluate(m.symbol, swing, daily, None, run_id=result.run_id,
                                             equity=self._equity, params=self._params, activity=m)
        with self._timed(result, "valuation_setups"):
            valuation = [s for s in strategies if swing_on and s.kind is StrategyKind.FUNDAMENTAL]
            if valuation:
                result.plays += self._valuation_plays(valuation, ranked, result.run_id)
        return self._finish(result, filters)

    def _learn_contracts(self, symbols: List[str], result: ScanResult) -> None:
        unknown = self.symbols.unknown(symbols)
        for i in range(0, len(unknown), _CONTRACT_CHUNK):
            self.symbols.record(self.md.source.contract_details_many(unknown[i:i + _CONTRACT_CHUNK]))
            self._progress(result, "contract details", min(i + _CONTRACT_CHUNK, len(unknown)), len(unknown))

    def _rank(self, symbols: Iterable[str], through: dt.date, prefilter: Mapping[str, float],
              result: ScanResult) -> List[DailyMetrics]:
        """Daily heat for every liquid stock whose candles are current, hottest first.
        Reads one stock at a time, so the whole universe never sits in memory."""
        oldest = clock.prev_trading_day(through)          # allow one missing session
        metrics = []
        for symbol in symbols:
            daily = self.md.daily_frame(symbol)
            if daily is None or not len(daily) or daily.index[-1].date() < oldest:
                continue
            result.scanned += 1
            m = daily_metrics(symbol, daily)
            if m is not None and liquid(m, prefilter):
                metrics.append(m)
        return rank_by_daily_heat(metrics)

    def _valuation_plays(self, strategies: List[Strategy], ranked: List[DailyMetrics], run_id: str) -> List[Play]:
        leaders = self.settings.config.scanner.fundamentals_leaders
        benchmark = self.md.daily_frame(BENCHMARK)
        pool = [m.symbol for m in ranked]
        plays: List[Play] = []
        found = 0
        for m in ranked[:leaders * 3]:
            if found >= leaders:
                break
            fin = self._financials(m.symbol, benchmark)
            if fin is None:
                continue
            found += 1
            peers = [f for p in self.symbols.peers(m.symbol, pool, self.PEERS)
                     if (f := self._financials(p, benchmark)) is not None]
            plays += evaluate(m.symbol, strategies, self.md.daily_frame(m.symbol), None, run_id=run_id,
                              equity=self._equity, params=self._params, activity=m, fundamentals=fin, peers=peers)
        return plays

    def _financials(self, symbol: str, benchmark: Optional[pd.DataFrame]) -> Optional[Financials]:
        """SEC statements with today's price, market cap and beta filled in."""
        statements = self.fundamentals.get(symbol)
        daily = self.md.daily_frame(symbol)
        if statements is None or daily is None or not len(daily):
            return None
        price = float(daily["close"].iloc[-1])
        beta = ta.beta(daily["close"], benchmark["close"]) if benchmark is not None else math.nan
        return dataclasses.replace(statements, price=price, market_cap=price * statements.shares_out, beta=beta)

    # ---- the intraday cycle ------------------------------------------------ #
    def run_cycle(self, fast: bool = False) -> ScanResult:
        cfg = self.settings.config.scanner
        filters, strategies = self.filters, list(self.strategies)
        result = ScanResult("fast" if fast else "cycle")
        wl = self.watchlist
        if wl is None:
            result.errors["watchlist"] = "no watchlist yet - the full scan hasn't run"
            return self._finish(result, filters)

        with self._watchlist_lock:
            picks = {} if fast else wl.next_picks(cfg.buffer_picks_per_sector, filters.sectors)
            buffer = [] if fast else wl.kept_symbols() + [c.symbol for cs in picks.values() for c in cs]
            symbols = list(dict.fromkeys(wl.hot_symbols() + buffer))
        with self._timed(result, "intraday_candles"):
            intraday = self.md.intraday(symbols, self._con_ids(symbols))
        daily = self.md.daily(symbols)
        result.universe_size, result.scanned, result.symbols = len(symbols), len(intraday), symbols

        market_open = clock.is_market_open()
        active = [s for s in strategies if s.kind is StrategyKind.TECHNICAL and s.timeframe.value in filters.timeframes
                  and (s.timeframe is Timeframe.SWING or market_open)]
        heat: Dict[str, float] = {}
        with self._timed(result, "setups"):
            for symbol in symbols:
                if symbol not in intraday or symbol not in daily:
                    continue
                activity = intraday_metrics(symbol, intraday[symbol], daily[symbol])
                plays = evaluate(symbol, active, daily[symbol], intraday[symbol], run_id=result.run_id,
                                 equity=self._equity, params=self._params, activity=activity)
                result.plays += plays
                if activity is not None:
                    heat[symbol] = activity.heat + (_PLAY_BONUS if plays else 0.0)
        with self._watchlist_lock:
            if not fast:
                result.decisions = wl.apply_cycle(heat, picks, cfg.kept_per_sector)
                wl.save(self.watchlist_dir)
            result.hot = wl.hot_symbols()
        return self._finish(result, filters)

    # ---- shared ------------------------------------------------------------- #
    @property
    def _equity(self) -> float:
        return self.account.equity if self.account else 0.0

    def _con_ids(self, symbols: Iterable[str]) -> Dict[str, int]:
        return {s: info.con_id for s in symbols if (info := self.symbols.get(s)) is not None and info.found}

    def _finish(self, result: ScanResult, filters: TradeFilters) -> ScanResult:
        min_rr = float(self.settings.config.risk.min_reward_risk)
        for p in result.plays:
            p.sector = self.symbols.sector(p.symbol)
        result.plays = sorted((p for p in result.plays
                               if (p.reward_risk >= min_rr or p.kind is StrategyKind.FUNDAMENTAL) and filters.allows(p)),
                              key=lambda p: p.score, reverse=True)
        result.finished_at = clock.now_ny()
        result.elapsed_s = (result.finished_at - result.started_at).total_seconds()
        log.info("scan %s: %s", result.kind, result.summary())
        BUS.publish("scan.completed", summary=result.summary())
        return result

    @staticmethod
    def _progress(result: ScanResult, stage: str, done: int, total: int) -> None:
        BUS.publish("scan.progress", kind=result.kind, run_id=result.run_id, stage=stage, done=done, total=total)

    @staticmethod
    @contextlib.contextmanager
    def _timed(result: ScanResult, stage: str) -> Iterator[None]:
        start = time.monotonic()
        try:
            yield
        finally:
            result.timings[stage] = time.monotonic() - start
