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

A fast cycle (while Autopilot day-trades) rescans only the hot list. The
candle-close check (run_close) reads the watch tier's newest 5-minute candles
seconds after each close, in place of the fast cycle due then.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import logging
import math
import statistics
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from ..config import Settings
from ..core.enums import StrategyKind, Timeframe
from ..core.eventbus import BUS
from ..core.models import Account, Play
from ..data.fundamentals import Financials, FundamentalsProvider
from ..data.listings import UsListings
from ..data.market_data import CLOSE_DURATION, MarketData, NoDataSource
from ..data.sectors import sector_allowed
from ..data.symbols import SymbolMaster
from ..indicators import ta
from ..strategies.base import Strategy
from ..util import clock
from . import schedule
from ..signals.book import SignalBook
from .evaluator import evaluate, with_today
from .noise import NoiseSettings
from .filters import TradeFilters
from .heat import (DailyMetrics, daily_metrics, intraday_metrics, liquid, premarket_metrics, rank_by_daily_heat,
                   rank_gappers)
from .schedule import ScanSettings
from .watchlist import Candidate, Decision, DayWatchlist

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
    gappers: List[Dict[str, Any]] = field(default_factory=list)      # the gap check's findings, hottest first
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
            "gappers": [{"symbol": g["symbol"], "gap_pct": g["gap_pct"]} for g in self.gappers[:10]],
            "errors": dict(list(self.errors.items())[:5]), "n_errors": len(self.errors),
            "elapsed_s": round(self.elapsed_s, 1),
            "timings": {k: round(v, 2) for k, v in self.timings.items()},
        }


class Scanner:
    SWING_LEADERS = 400
    PEERS = 6
    #: the wide scan reads candles this many stocks at a time, so a chunk that fails is skipped, not the sweep
    WIDE_CHUNK = 250

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
        self._noise = NoiseSettings.from_config(settings.config.noise)
        #: insider and news signals (filled by the signals service); None leaves scores alone
        self.signals: Optional[SignalBook] = None
        #: the market's regime (engine/market_regime.py), handed to every play's context
        self.market: Dict[str, Any] = {}
        #: each strategy's evidence multiplier (research/weights.py)
        self.evidence_weights: Dict[str, float] = {}
        #: each strategy's pooled win rate and trade count, calibrating the odds its plays state
        self.strategy_records: Dict[str, Dict[str, Any]] = {}
        #: (the session every tradable stock's daily candles reach, those stocks) after a full download
        self.market_daily: Optional[Tuple[dt.date, List[str]]] = None
        #: what the gap check saw per stock today (heat.GapperMetrics.as_dict): the gap, the pre-market
        #: high and low the setups use as levels
        self.premarket: Dict[str, Dict[str, Any]] = {}
        #: the session the live candles were last compared with IBKR's bars in (_compare_candles)
        self._candles_compared: Optional[dt.date] = None

    def set_strategies(self, strategies: Sequence[Strategy]) -> None:
        """Swap the active setups. A scan already running keeps the set it started with."""
        self.strategies = list(strategies)

    def watchlist_state(self) -> Optional[Dict[str, Any]]:
        with self._watchlist_lock:
            return self.watchlist.state() if self.watchlist else None

    def watch_symbols(self, n: int, now: Optional[dt.datetime] = None) -> List[str]:
        """The watch tier: the day's ``n`` most relevant stocks, streamed after the plays - the hot list, then the
        kept buffer names (both by their latest heat, the daily heat until a cycle has seen them), then the buffer
        names the next cycle samples (by daily heat), each stock once and only in the sectors the filters allow.
        Nothing outside the pre-market and the regular session, or from a watchlist left from another day. A pure
        read - no request, no disk, no change to the watchlist: the stream thread asks every few seconds."""
        if n <= 0:
            return []
        now = (now or clock.now_ny()).astimezone(clock.NY)
        if clock.current_session(now) not in (clock.Session.PRE, clock.Session.REGULAR):
            return []
        per_sector, sectors = self.settings.config.scanner.buffer_picks_per_sector, self.filters.sectors

        def heat(c: Candidate) -> float:
            return c.heat if c.heat is not None else c.daily_heat

        with self._watchlist_lock:
            wl = self.watchlist
            if wl is None or wl.session != now.date():
                return []
            hot = sorted(wl.hot, key=heat, reverse=True)
            kept = sorted((c for cs in wl.kept.values() for c in cs), key=heat, reverse=True)
            picks = sorted((c for cs in wl.next_picks(per_sector, sectors).values() for c in cs),
                           key=lambda c: c.daily_heat, reverse=True)
            symbols = [c.symbol for c in (*hot, *kept, *picks)]
        return [s for s in dict.fromkeys(symbols) if s and sector_allowed(self.symbols.sector(s), sectors)][:n]

    # ---- the full scan ----------------------------------------------------- #
    def run_full(self, scan: ScanSettings, now: Optional[dt.datetime] = None) -> ScanResult:
        now = now or clock.now_ny()
        cfg = self.settings.config.scanner
        filters, strategies = self.filters, list(self.strategies)
        result = ScanResult("full")

        with self._timed(result, "listings"):
            symbols = self._listed(now)
        result.universe_size = len(symbols)
        with self._timed(result, "contracts"):
            self._learn_contracts(symbols + [BENCHMARK], result)
        tradable = self.symbols.tradable(symbols)
        through = schedule.last_completed_session(now)
        with self._timed(result, "daily_candles"):
            everyone = tradable + [BENCHMARK]
            self.md.update_daily(everyone, through, self.con_ids(everyone),
                                 progress=lambda done, total: self._progress(result, "daily candles", done, total))
        self.market_daily = (through, tradable)
        with self._timed(result, "ranking"):
            ranked = self._rank(tradable, through, cfg.prefilter, result)
        result.liquid = len(ranked)
        ranked = [m for m in ranked if sector_allowed(self.symbols.sector(m.symbol), filters.sectors)]
        result.symbols = [m.symbol for m in ranked]

        watchlist = DayWatchlist.build(
            schedule.watchlist_session(now), through, ranked, self.symbols.sector,
            scan.hot_list_size, scan.sector_queue_size, universe=len(symbols), liquid=result.liquid)
        if scan.yesterday_movers:
            # the last session's biggest movers hold slots, in case they move for a second day
            moved = sorted((m for m in ranked if m.rvol >= cfg.movers_min_rvol), key=lambda m: -m.move_atr)
            result.decisions += watchlist.apply_movers(
                [(m.symbol, self.symbols.sector(m.symbol), m.heat,
                  f"moved {m.move_atr:.1f} ATRs on {m.rvol:.1f}x volume last session") for m in moved],
                scan.yesterday_movers)
        with self._watchlist_lock:
            self.watchlist = watchlist
            watchlist.save(self.watchlist_dir)
            self.premarket = {}
        result.hot = watchlist.hot_symbols()

        swing_on = "SWING" in filters.timeframes
        with self._timed(result, "swing_setups"):
            swing = [s for s in strategies if swing_on and s.kind is StrategyKind.TECHNICAL
                     and s.timeframe is Timeframe.SWING]
            for m in ranked[:self.SWING_LEADERS] if swing else []:
                daily = self.md.daily_frame(m.symbol)
                if daily is not None:
                    result.plays += evaluate(m.symbol, swing, daily, None, run_id=result.run_id,
                                             equity=self._equity, params=self._params, activity=m, noise=self._noise,
                                             signals=self.signals, market=self.market,
                                             evidence_weights=self.evidence_weights,
                                             records=self.strategy_records, benchmark=self._benchmark(False))
            result.plays += self._signal_plays(swing, {m.symbol for m in ranked[:self.SWING_LEADERS]},
                                               result.run_id)[0]
        with self._timed(result, "valuation_setups"):
            valuation = [s for s in strategies if swing_on and s.kind is StrategyKind.FUNDAMENTAL]
            if valuation:
                result.plays += self._valuation_plays(valuation, ranked, result.run_id)
        return self._finish(result, filters)

    def update_market_daily(self, through: dt.date) -> List[str]:
        """Every tradable stock's daily candles up to ``through``: the download the morning's full scan
        makes, made after the close for the report on the session's movers (research/movers.py).
        Returns the tradable stocks."""
        symbols = self._listed(clock.now_ny())
        self._learn_contracts(symbols + [BENCHMARK])                   # quietly: it isn't a scan
        tradable = self.symbols.tradable(symbols)
        everyone = tradable + [BENCHMARK]
        self.md.update_daily(everyone, through, self.con_ids(everyone))
        self.market_daily = (through, tradable)
        return tradable

    def _listed(self, now: dt.datetime) -> List[str]:
        symbols = [listing.symbol for listing in self.listings.load(now.date())]
        cap = self.settings.config.scanner.max_universe
        return symbols[:cap] if cap else symbols

    def _learn_contracts(self, symbols: List[str], result: Optional[ScanResult] = None) -> None:
        unknown = self.symbols.unknown(symbols)
        for i in range(0, len(unknown), _CONTRACT_CHUNK):
            self.symbols.record(self.md.source.contract_details_many(unknown[i:i + _CONTRACT_CHUNK]))
            if result is not None:
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
                              equity=self._equity, params=self._params, activity=m, fundamentals=fin, peers=peers,
                              noise=self._noise, signals=self.signals, market=self.market,
                              evidence_weights=self.evidence_weights, records=self.strategy_records)
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
            intraday = self.md.intraday(symbols, self.con_ids(symbols))
        daily = self.md.daily(symbols)
        benchmark = self._benchmark(True)
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
                                 equity=self._equity, params=self._params, activity=activity, noise=self._noise,
                                 signals=self.signals, market=self.market,
                                 evidence_weights=self.evidence_weights,
                                 records=self.strategy_records, benchmark=benchmark,
                                 premarket=self.premarket.get(symbol))
                result.plays += plays
                if activity is not None:
                    heat[symbol] = activity.heat + (_PLAY_BONUS if plays else 0.0)
            extra, looked_at = self._signal_plays([s for s in active if s.timeframe is Timeframe.SWING], set(symbols),
                                                  result.run_id)
            result.plays += extra
            result.symbols = symbols + looked_at
        with self._watchlist_lock:
            if not fast:
                result.decisions = wl.apply_cycle(heat, picks, cfg.kept_per_sector)
                wl.save(self.watchlist_dir)
            result.hot = wl.hot_symbols()
        return self._finish(result, filters)

    # ---- the wide scan: every liquid stock ------------------------------- #
    def run_wide(self, stocks: int = 0, movers: int = 0) -> ScanResult:
        """Every liquid stock the full scan ranked - or the hottest ``stocks`` of them - on its
        5-minute candles: the day-trade and swing setups the filters allow run on all of them, with
        today's partial candle, and the hot list is refreshed from what has heated up since the
        morning; today's ``movers`` biggest movers on volume hold slots outright. One request per
        stock, in chunks; a chunk that fails is noted and skipped."""
        cfg = self.settings.config.scanner
        filters, strategies = self.filters, list(self.strategies)
        result = ScanResult("wide")
        wl = self.watchlist
        if wl is None:
            result.errors["watchlist"] = "no watchlist yet - the full scan hasn't run"
            return self._finish(result, filters)
        with self._watchlist_lock:
            symbols = [s for s in wl.leaders(stocks) if sector_allowed(self.symbols.sector(s), filters.sectors)]
            if not symbols:                                  # a watchlist saved before the ranking was kept
                symbols = wl.hot_symbols() + wl.kept_symbols()
        result.universe_size = len(symbols)
        market_open = clock.is_market_open()
        active = [s for s in strategies if s.kind is StrategyKind.TECHNICAL and s.timeframe.value in filters.timeframes
                  and (s.timeframe is Timeframe.SWING or market_open)]
        benchmark = self._benchmark(True)
        heat: Dict[str, float] = {}
        seen: Dict[str, Any] = {}                       # each stock's intraday metrics, for the movers
        scanned: List[str] = []
        with self._timed(result, "setups"):
            for start in range(0, len(symbols), self.WIDE_CHUNK):
                chunk = symbols[start:start + self.WIDE_CHUNK]
                self._progress(result, "wide scan", start, len(symbols))
                try:
                    intraday = self.md.intraday(chunk, self.con_ids(chunk))
                except NoDataSource:
                    raise
                except Exception as e:  # noqa: BLE001 - one chunk's candles, not the sweep
                    result.errors[f"stocks {start + 1}-{start + len(chunk)}"] = str(e)
                    log.warning("wide scan: candles for stocks %d-%d failed: %s", start + 1, start + len(chunk), e)
                    continue
                daily = self.md.daily(chunk)
                for symbol in chunk:
                    if symbol not in intraday or symbol not in daily:
                        continue
                    scanned.append(symbol)
                    activity = intraday_metrics(symbol, intraday[symbol], daily[symbol])
                    plays = evaluate(symbol, active, daily[symbol], intraday[symbol], run_id=result.run_id,
                                     equity=self._equity, params=self._params, activity=activity, noise=self._noise,
                                     signals=self.signals, market=self.market,
                                     evidence_weights=self.evidence_weights,
                                     records=self.strategy_records, benchmark=benchmark,
                                     premarket=self.premarket.get(symbol))
                    result.plays += plays
                    if activity is not None:
                        heat[symbol] = activity.heat + (_PLAY_BONUS if plays else 0.0)
                        seen[symbol] = activity
        result.symbols, result.scanned = scanned, len(scanned)
        with self._watchlist_lock:
            result.decisions = wl.apply_wide(heat, self.symbols.sector, cfg.kept_per_sector)
            if movers:
                moved = sorted((m for m in seen.values() if m.rvol >= cfg.movers_min_rvol),
                               key=lambda m: -abs(m.change_pct))
                result.decisions += wl.apply_movers(
                    [(m.symbol, self.symbols.sector(m.symbol), heat.get(m.symbol, m.heat),
                      f"moved {m.change_pct:+.1f}% today on {m.rvol:.1f}x volume") for m in moved], movers)
            wl.save(self.watchlist_dir)
            result.hot = wl.hot_symbols()
        return self._finish(result, filters)

    # ---- the gap check, just before the open ----------------------------- #
    def run_gappers(self, now: Optional[dt.datetime] = None) -> ScanResult:
        """Aziz's gappers watchlist: the hot list and buffer names' pre-market candles, read once
        before the open. The stocks gapping on volume take hot-list slots; every stock's pre-market
        high and low are kept as the day's first levels (see strategies/base.py levels)."""
        cfg = self.settings.config.scanner
        filters = self.filters
        result = ScanResult("gappers")
        wl = self.watchlist
        if wl is None:
            result.errors["watchlist"] = "no watchlist yet - the full scan hasn't run"
            return self._finish(result, filters, quiet=True)
        with self._watchlist_lock:
            queued = [c.symbol for sector, q in wl.queues.items() if sector_allowed(sector, filters.sectors) for c in q]
            symbols = list(dict.fromkeys(wl.hot_symbols() + wl.kept_symbols() + queued))[:cfg.gapper_symbols]
        with self._timed(result, "premarket_candles"):
            pre = self.md.premarket(symbols, self.con_ids(symbols))
        daily = self.md.daily(symbols)
        metrics = [m for s in symbols if (m := premarket_metrics(s, pre.get(s), daily.get(s))) is not None]
        gappers = rank_gappers(metrics, cfg.gapper_min_gap_pct, cfg.gapper_min_volume)
        self.premarket = {m.symbol: m.as_dict() for m in metrics}
        with self._watchlist_lock:
            result.decisions = wl.apply_gappers(gappers, len(wl.hot))
            wl.save(self.watchlist_dir)
            result.hot = wl.hot_symbols()
        result.universe_size, result.scanned, result.symbols = len(symbols), len(pre), symbols
        result.gappers = [{"symbol": g.symbol, **g.as_dict()} for g in gappers]
        return self._finish(result, filters)

    def _signal_plays(self, strategies: Sequence[Strategy], done: Collection[str],
                      run_id: str) -> Tuple[List[Play], List[str]]:
        """Swing setups on the stocks with unusual insider buying that this scan hasn't
        looked at already - most of them aren't among the day's hottest. Returns the
        plays and the stocks looked at."""
        if self.signals is None or not strategies:
            return [], []
        plays: List[Play] = []
        looked_at: List[str] = []
        for symbol in self.signals.unusual_buying_symbols():
            if symbol in done or not sector_allowed(self.symbols.sector(symbol), self.filters.sectors):
                continue
            daily = self.md.daily_frame(symbol)
            if daily is None or len(daily) < 20:
                continue
            looked_at.append(symbol)
            plays += evaluate(symbol, strategies, daily, None, run_id=run_id, equity=self._equity, params=self._params,
                              activity=daily_metrics(symbol, daily), noise=self._noise, signals=self.signals,
                              market=self.market, evidence_weights=self.evidence_weights, records=self.strategy_records)
        return plays, looked_at

    def run_plays(self, symbols: Sequence[str]) -> ScanResult:
        """The quick re-check: the setups on ``symbols`` - the stocks with plays on the board -
        against their newest candles. Only the last half hour of candles is fetched, no
        watchlist decision is made, and a stock whose candles didn't come keeps its plays."""
        filters, strategies = self.filters, list(self.strategies)
        result = ScanResult("plays")
        wanted = list(dict.fromkeys(symbols))
        intraday = self.md.refresh_intraday(wanted, self.con_ids(wanted)) if wanted else {}
        daily = self.md.daily(wanted) if wanted else {}
        result.symbols = [s for s in wanted if s in intraday and s in daily]
        result.universe_size, result.scanned = len(wanted), len(result.symbols)
        self._evaluate_intraday(result, result.symbols, intraday, daily, filters, strategies)
        for p in result.plays:
            p.scan_run_id = None               # a quick re-check isn't recorded as a scan
        return self._finish(result, filters, quiet=True)

    def run_close(self, symbols: Sequence[str], since: dt.datetime) -> ScanResult:
        """The candle-close check: the setups on ``symbols`` - the watch tier, or the stocks moving early - on
        IBKR's 5-minute bars, seconds after the candle that closed at ``since``. The last hour of bars is fetched
        for each stock, past the 60 s cache (a stock with nothing cached gets its full history once), and a stock
        is looked at only once the bar starting at ``since`` is printing: the setups read the bar before the
        newest as the last closed one. A stock whose new bar isn't in yet is left out, so it keeps its plays -
        the next check or the fast cycle gets it. No watchlist decision; the plays are recorded like a cycle's."""
        filters, strategies = self.filters, list(self.strategies)
        result = ScanResult("close")
        wanted = list(dict.fromkeys(symbols))
        with self._timed(result, "intraday_candles"):
            intraday = self.md.refresh_intraday(wanted, self.con_ids(wanted), duration=CLOSE_DURATION) if wanted else {}
        daily = self.md.daily(wanted) if wanted else {}
        printing = pd.Timestamp(since)
        result.symbols = [s for s in wanted if s in daily and (f := intraday.get(s)) is not None and len(f)
                          and f.index[-1] >= printing]
        result.universe_size, result.scanned = len(wanted), len(result.symbols)
        self._evaluate_intraday(result, result.symbols, intraday, daily, filters, strategies)
        self._compare_candles({s: intraday[s] for s in result.symbols}, since)
        return self._finish(result, filters, quiet=True)

    def _evaluate_intraday(self, result: ScanResult, symbols: Sequence[str], intraday: Mapping[str, pd.DataFrame],
                           daily: Mapping[str, pd.DataFrame], filters: TradeFilters,
                           strategies: Sequence[Strategy]) -> None:
        """The setups the filters allow on each of ``symbols``' newest candles, into ``result`` - the quick
        re-check's pass and the candle-close check's. Day setups only while the market is open; the swing setups
        come along, so the board doesn't drop the swing plays on those stocks."""
        market_open = clock.is_market_open()
        active = [s for s in strategies if s.kind is StrategyKind.TECHNICAL and s.timeframe.value in filters.timeframes
                  and (s.timeframe is Timeframe.SWING or market_open)]
        benchmark = self._benchmark(True) if symbols else None
        with self._timed(result, "setups"):
            for symbol in symbols:
                activity = intraday_metrics(symbol, intraday[symbol], daily[symbol])
                result.plays += evaluate(symbol, active, daily[symbol], intraday[symbol], run_id=result.run_id,
                                         equity=self._equity, params=self._params, activity=activity,
                                         noise=self._noise, signals=self.signals, market=self.market,
                                         evidence_weights=self.evidence_weights,
                                         records=self.strategy_records, benchmark=benchmark,
                                         premarket=self.premarket.get(symbol))

    #: stocks a comparison of the live candles with IBKR's bars wants before it says anything
    COMPARE_MIN_STOCKS = 5

    def _compare_candles(self, frames: Mapping[str, pd.DataFrame], since: dt.datetime) -> None:
        """Once a session: the live 5-minute candles that closed at ``since`` against IBKR's bars for the same
        5 minutes, logged. The volume ratio settles whether the stream counts shares or lots of 100
        (data/candles.py); nothing acts on it. A stock counts only when its live candle is whole."""
        since = since.astimezone(clock.NY)
        if self._candles_compared == since.date():
            return
        start = since - dt.timedelta(minutes=5)
        bar_at, ratios, gaps = pd.Timestamp(start), [], []
        for symbol, frame in frames.items():
            live = self.md.candles.latest(symbol, 5)
            if live is None or live.start != start.timestamp() or live.partial or bar_at not in frame.index:
                continue
            volume, close = float(frame.at[bar_at, "volume"]), float(frame.at[bar_at, "close"])
            if volume > 0 and close > 0:
                ratios.append(live.volume / volume)
                gaps.append(abs(live.close - close) / close * 100.0)
        if len(ratios) < self.COMPARE_MIN_STOCKS:
            return                                       # too few to say: the next check tries again
        self._candles_compared = since.date()
        log.info("live candles vs IBKR 5-minute bars at %s: median volume ratio %.3g (about 1 = the stream counts "
                 "shares, about 0.01 = lots of 100), median close difference %.2f%%", since.strftime("%H:%M"),
                 statistics.median(ratios), statistics.median(gaps))

    # ---- shared ------------------------------------------------------------- #
    def _benchmark(self, intraday: bool) -> Optional[pd.Series]:
        """The S&P 500 ETF's closes for the market model, with today's latest price while the market
        is open (one small request, shared by the cycle's stocks)."""
        daily = self.md.daily_frame(BENCHMARK)
        if daily is None or not len(daily):
            return None
        today = None
        if intraday and clock.is_market_open():
            try:
                today = self.md.intraday([BENCHMARK], self.con_ids([BENCHMARK])).get(BENCHMARK)
            except Exception:  # noqa: BLE001
                log.debug("the benchmark's intraday candles failed", exc_info=True)
        return with_today(daily, today)["close"]

    @property
    def _equity(self) -> float:
        return self.account.equity if self.account else 0.0

    def con_ids(self, symbols: Iterable[str]) -> Dict[str, int]:
        return {s: info.con_id for s in symbols if (info := self.symbols.get(s)) is not None and info.found}

    def _finish(self, result: ScanResult, filters: TradeFilters, quiet: bool = False) -> ScanResult:
        min_rr = float(self.settings.config.risk.min_reward_risk)
        for p in result.plays:
            p.sector = self.symbols.sector(p.symbol)
        result.plays = sorted((p for p in result.plays
                               if (p.reward_risk >= min_rr or p.kind is StrategyKind.FUNDAMENTAL) and filters.allows(p)),
                              key=lambda p: p.score, reverse=True)
        result.finished_at = clock.now_ny()
        result.elapsed_s = (result.finished_at - result.started_at).total_seconds()
        if quiet:
            log.debug("scan %s: %s", result.kind, result.summary())
        else:
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
