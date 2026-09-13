"""One scan cycle:

    universe slice  ->  sector filter  ->  daily bars  ->  pre-filter
        ->  intraday bars + technical strategies
        ->  fundamentals + peers for the leaders  ->  fundamental strategies
        ->  size every play, apply the dashboard filters, rank, keep the short list

The universe is scanned in rotating slices of ``max_symbols_scanned`` so that
"all of the Nasdaq" is covered over several cycles without hammering the data
provider.

Where the time goes and what runs in parallel: bars arrive in one batched
request per chunk of symbols (not one paced request per symbol), a last-price
feed's quotes are read off those bars instead of fetched again, fundamentals
are network round-trips so they're fetched concurrently, and the strategies
(CPU-bound, so threads don't help) run in sequence. Each stage's wall time is
in ``ScanResult.timings``.
Every step is per-symbol try/except so one bad ticker cannot abort the cycle.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from ..core.eventbus import BUS
from ..core.models import Account, Play, Quote, ScanCandidate
from ..data.fundamentals import FundamentalsProvider
from ..data.market_data import MarketDataService, quote_from_price
from ..data.sectors import SectorLookup, sector_allowed
from ..data.universe import UniverseLoader
from ..risk.position_sizing import size_play
from ..strategies.base import build_context
from ..util import clock
from .filters import TradeFilters, passes_prefilter, rank_score

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    run_id: str
    started_at: dt.datetime
    finished_at: Optional[dt.datetime] = None
    universe_size: int = 0
    scanned: int = 0
    prefiltered: int = 0
    candidates: List[ScanCandidate] = field(default_factory=list)
    plays: List[Play] = field(default_factory=list)          # ranked, all
    shortlist: List[str] = field(default_factory=list)       # top-N symbols
    errors: Dict[str, str] = field(default_factory=dict)
    elapsed_s: float = 0.0
    sector_skipped: int = 0                                  # outside the Sectors filter
    timings: Dict[str, float] = field(default_factory=dict)  # stage -> seconds

    def summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "universe_size": self.universe_size,
            "scanned": self.scanned,
            "prefiltered": self.prefiltered,
            "sector_skipped": self.sector_skipped,
            "n_plays": len(self.plays),
            "shortlist": self.shortlist,
            "n_errors": len(self.errors),
            "elapsed_s": round(self.elapsed_s, 1),
            "timings": {k: round(v, 2) for k, v in self.timings.items()},
        }


class Scanner:
    #: tickers with no known sector yet, looked up online per cycle while a
    #: sector filter is on; the rest are skipped this cycle and resolved later
    SECTOR_LOOKUPS_PER_CYCLE = 20
    #: fundamentals are network round-trips, so these overlap well (19 s -> 11 s
    #: for the leaders + peers). The strategies are CPU-bound pandas work where a
    #: thread pool measured no faster under the GIL, so they run in sequence.
    FUNDAMENTALS_WORKERS = 4

    def __init__(
        self,
        settings,
        market_data: MarketDataService,
        fundamentals: Optional[FundamentalsProvider],
        strategies: list,
    ) -> None:
        self.settings = settings
        self.md = market_data
        self.fund = fundamentals
        self.set_strategies(strategies)

        self._loader = UniverseLoader()
        self._universe: List[str] = []
        self._cursor = 0
        self._last_account: Optional[Account] = None

        self._sectors = SectorLookup(use_yfinance=self.md.is_real)
        #: what to look for (sides, timeframes, sectors). The engine replaces it
        #: when the dashboard filters change; a cycle in flight keeps its copy.
        self.filters = TradeFilters()

    # ------------------------------------------------------------------ #
    def set_strategies(self, strategies: list) -> None:
        """Swap the active setups (strategy panel). A cycle already running
        finishes with the set it started with."""
        self.strategies = list(strategies)
        self._sets = ([s for s in self.strategies if s.kind.value == "TECHNICAL"],
                      [s for s in self.strategies if s.kind.value == "FUNDAMENTAL"])

    @property
    def tech(self) -> list:
        return self._sets[0]

    @property
    def fundamental(self) -> list:
        return self._sets[1]

    def set_account(self, account: Account) -> None:
        self._last_account = account

    def _ensure_universe(self) -> None:
        sc = self.settings.config.scanner
        if not self._universe:
            self._universe = self._loader.load(
                sc.universe, max_symbols=None, universe_file=sc.universe_file
            )
            log.info("scanner universe loaded: %d symbols", len(self._universe))

    def _next_slice(self, size: int) -> List[str]:
        u = self._universe
        if not u:
            return []
        if size >= len(u):
            return list(u)
        end = self._cursor + size
        if end <= len(u):
            sl = u[self._cursor:end]
        else:
            sl = u[self._cursor:] + u[: end - len(u)]
        self._cursor = end % len(u)
        return sl

    def _in_sectors(self, symbols: List[str], sectors) -> List[str]:
        budget = self.SECTOR_LOOKUPS_PER_CYCLE
        keep = []
        for sym in symbols:
            sec = self._sectors.peek(sym)
            if sec is None and budget > 0:
                budget -= 1
                try:
                    sec = self._sectors.get(sym)
                except Exception:  # noqa: BLE001
                    sec = ""
            if sector_allowed(sec, sectors):
                keep.append(sym)
        return keep

    def _quotes_for(self, daily_by_sym: Dict[str, pd.DataFrame]) -> Dict[str, Quote]:
        """Quotes for the pre-filter. A feed whose quote is only a last price
        (yfinance, the demo feed) has it read off the bars just downloaded; a
        broker feed has real bid/ask, fetched concurrently."""
        if self.md.quotes_are_synthetic:
            out = {}
            for sym, df in daily_by_sym.items():
                q = _bar_quote(sym, df)
                if q is not None:
                    out[sym] = q
            return out
        return self.md.get_quotes(list(daily_by_sym))

    # ------------------------------------------------------------------ #
    def run_cycle(self) -> ScanResult:
        t0 = time.time()
        sc = self.settings.config.scanner
        bars = sc.bars or {}
        pf = sc.prefilter or {}
        filters = self.filters                      # one consistent view for the whole cycle
        tech, fundamental = self._sets
        run = ScanResult(run_id=f"scan_{uuid.uuid4().hex[:10]}", started_at=clock.now_ny())
        BUS.publish("scan.started", run_id=run.run_id)

        self._ensure_universe()
        run.universe_size = len(self._universe)
        slice_syms = self._next_slice(int(sc.max_symbols_scanned))
        if filters.sectors:
            in_sector = self._in_sectors(slice_syms, filters.sectors)
            run.sector_skipped = len(slice_syms) - len(in_sector)
            slice_syms = in_sector
        run.scanned = len(slice_syms)

        intraday_iv = bars.get("intraday_interval", "5m")
        intraday_days = int(bars.get("intraday_lookback_days", 10))
        daily_days = int(bars.get("daily_lookback_days", 400))
        equity = self._last_account.equity if self._last_account else 0.0

        # intraday setups need a *live* session - when the market is closed their
        # "intraday" bars would just be yesterday's tail. Both kinds also have to
        # be switched on in the filters.
        intraday_ok = clock.is_market_open()
        active_tech = [s for s in tech if s.timeframe.value in filters.timeframes
                       and (s.timeframe.value != "INTRADAY" or intraday_ok)]
        active_fund = [s for s in fundamental if s.timeframe.value in filters.timeframes]
        if not intraday_ok and "INTRADAY" in filters.timeframes:
            log.info("market closed - running %d swing setups only (skipping intraday)",
                     len(active_tech))

        # -- stage 1: daily bars + prefilter ----------------------------- #
        mark = time.time()
        daily_by_sym = self.md.get_price_histories(slice_syms, "1d", daily_days) if slice_syms else {}
        quotes = self._quotes_for(daily_by_sym)
        cands: List[ScanCandidate] = []
        for sym in slice_syms:
            daily = daily_by_sym.get(sym)
            if daily is None:
                continue                            # no data (delisted, halted, not on the feed)
            try:
                cand = passes_prefilter(sym, daily, None, quotes.get(sym), pf)
            except Exception as e:  # noqa: BLE001
                run.errors[sym] = str(e)
                continue
            if cand is not None:
                cands.append(cand)
        cands.sort(key=lambda c: (c.rvol, c.atr_pct), reverse=True)
        run.prefiltered = len(cands)
        run.candidates = cands
        run.timings["prices"] = time.time() - mark
        BUS.publish("scan.progress", run_id=run.run_id, stage="prefiltered", n=len(cands))

        # -- stage 2: technical strategies on the survivors -------------- #
        cand_by_sym = {c.symbol: c for c in cands}
        syms = [c.symbol for c in cands]
        intraday_by_sym: Dict[str, pd.DataFrame] = {}
        if syms and any(s.timeframe.value == "INTRADAY" for s in active_tech):
            mark = time.time()
            intraday_by_sym = self.md.get_price_histories(syms, intraday_iv, intraday_days)
            run.timings["intraday_bars"] = time.time() - mark

        def _run_tech(sym: str):
            try:
                daily = daily_by_sym[sym]
                intr = intraday_by_sym.get(sym)
                if intr is None or intr.empty:
                    intr = daily                    # swing setups don't care
                q = quotes.get(sym)
                if intr is not daily and self.md.quotes_are_synthetic:
                    q = _bar_quote(sym, intr) or q  # the last 5-minute bar beats the daily one
                _c = cand_by_sym.get(sym)
                ctx = build_context(
                    sym, intr, daily, q, params={"valuation": _valuation_params(self.settings)},
                    account_equity=equity,
                    candidate=asdict(_c) if _c is not None else None,
                )
                spark = _spark(intr)
                out: List[Play] = []
                for strat in active_tech:
                    try:
                        for p in strat.generate(ctx):
                            p.scan_run_id = run.run_id
                            p.score = rank_score(p, _c, strat.weight)
                            p.evidence.setdefault("spark", spark)
                            out.append(p)
                    except Exception as e:  # noqa: BLE001
                        log.debug("%s %s failed: %s", sym, strat.key, e)
                return sym, out, ctx, None
            except Exception as e:  # noqa: BLE001
                return sym, [], None, str(e)

        mark = time.time()
        all_plays: List[Play] = []
        ctx_by_sym: Dict[str, object] = {}
        for sym, plays, ctx, err in map(_run_tech, syms):
            if err:
                run.errors[sym] = err
            if ctx is not None:
                ctx_by_sym[sym] = ctx
            all_plays.extend(plays)
        run.timings["strategies"] = time.time() - mark

        # -- stage 3: fundamentals for the leaders ----------------------- #
        if active_fund and self.fund is not None and ctx_by_sym:
            mark = time.time()
            n_leaders = int(getattr(sc, "fundamentals_leaders", 8) or 8)
            leaders = [s for s in _leaders_for_fundamentals(cands, all_plays, limit=n_leaders)
                       if s in ctx_by_sym]
            for sym in self._load_fundamentals(leaders, ctx_by_sym, peer_n=6, errors=run.errors):
                ctx = ctx_by_sym[sym]
                spark = _spark(ctx.intraday)
                for strat in active_fund:
                    try:
                        for p in strat.generate(ctx):
                            p.scan_run_id = run.run_id
                            p.score = rank_score(p, cand_by_sym.get(sym), strat.weight)
                            if spark:
                                p.evidence.setdefault("spark", spark)
                            all_plays.append(p)
                    except Exception as e:  # noqa: BLE001
                        log.debug("%s %s failed: %s", sym, strat.key, e)
            run.timings["fundamentals"] = time.time() - mark

        # -- stage 4: sector tag, size, filter, rank, shortlist ---------- #
        mark = time.time()
        acct = self._last_account
        sector_by_sym: Dict[str, str] = {}
        for p in all_plays:
            if p.symbol not in sector_by_sym:
                ctx = ctx_by_sym.get(p.symbol)
                known = getattr(getattr(ctx, "fundamentals", None), "sector", "") or ""
                try:
                    sector_by_sym[p.symbol] = self._sectors.get(p.symbol, known or None)
                except Exception:  # noqa: BLE001
                    sector_by_sym[p.symbol] = known
            p.sector = sector_by_sym[p.symbol]
            if acct is not None:
                try:
                    size_play(p, acct, self.settings.config.risk)
                except Exception:  # noqa: BLE001
                    pass
        min_rr = float(self.settings.config.risk.min_reward_risk)
        all_plays = [p for p in all_plays
                     if (p.reward_risk >= min_rr or p.kind.value == "FUNDAMENTAL")
                     and filters.allows(p)]
        all_plays.sort(key=lambda p: p.score, reverse=True)
        run.plays = all_plays

        seen: List[str] = []
        for p in all_plays:
            if p.symbol not in seen:
                seen.append(p.symbol)
            if len(seen) >= int(sc.shortlist_size):
                break
        run.shortlist = seen
        run.timings["rank"] = time.time() - mark

        run.finished_at = clock.now_ny()
        run.elapsed_s = time.time() - t0
        log.info("scan %s: %s", run.run_id, run.summary())
        BUS.publish("scan.completed", run_id=run.run_id, summary=run.summary(),
                    plays=[p.to_row() for p in all_plays[:60]])
        return run

    def _load_fundamentals(self, leaders: List[str], ctx_by_sym: Dict[str, object],
                           peer_n: int, errors: Dict[str, str]) -> List[str]:
        """Financials for the leaders, then one wave for all of their peers.
        Each wave runs concurrently - the provider's limiter still spaces the
        request starts, but the round-trips overlap. Returns the leaders loaded."""
        fund = self.fund

        def leader(sym: str):
            try:
                fin = fund.get(sym)
                peers = list(fund.peers(sym, limit=peer_n)) if fin and fin.has_min_data() else []
                return sym, fin, peers, None
            except Exception as e:  # noqa: BLE001
                return sym, None, [], e

        def peer(sym: str):
            try:
                return sym, fund.get(sym)
            except Exception as e:  # noqa: BLE001
                log.debug("peer %s fundamentals failed: %s", sym, e)
                return sym, None

        loaded: List[str] = []
        peers_of: Dict[str, List[str]] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.FUNDAMENTALS_WORKERS)) as ex:
            for sym, fin, peers, err in ex.map(leader, leaders):
                if err is not None:
                    errors[sym] = f"fundamentals: {err}"
                    continue
                ctx_by_sym[sym].fundamentals = fin
                peers_of[sym] = peers
                loaded.append(sym)
            wanted = list(dict.fromkeys(p for ps in peers_of.values() for p in ps))
            got = {s: f for s, f in ex.map(peer, wanted) if f is not None}
        for sym in loaded:
            if peers_of.get(sym):
                ctx_by_sym[sym].peers = [got[p] for p in peers_of[sym] if p in got]
        return loaded


def _bar_quote(symbol: str, bars: Optional[pd.DataFrame]) -> Optional[Quote]:
    """A last-price quote from the newest bar."""
    try:
        last = bars.iloc[-1]
        price = float(last["close"])
        volume = float(last.get("volume", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        return None
    if not price or price != price:
        return None
    return quote_from_price(symbol, price, volume)


def _spark(bars: Optional[pd.DataFrame]) -> List[float]:
    try:
        return [round(float(x), 3) for x in bars["close"].tail(60).tolist()]
    except Exception:  # noqa: BLE001
        return []


def _valuation_params(settings) -> dict:
    v = settings.config.valuation
    return {
        "risk_free_rate": v.risk_free_rate,
        "market_risk_premium": v.market_risk_premium,
        "tax_rate": v.tax_rate,
        "midyear_convention": v.midyear_convention,
        "perpetuity_growth": getattr(v, "perpetuity_growth", 0.025),
        "projection_years": getattr(v, "projection_years", 5),
    }


def _leaders_for_fundamentals(cands, plays, limit: int) -> List[str]:
    by_score: Dict[str, float] = {}
    for p in plays:
        by_score[p.symbol] = max(by_score.get(p.symbol, 0.0), p.score)
    for c in cands:
        by_score.setdefault(c.symbol, 0.0)
        by_score[c.symbol] += 0.01 * c.atr_pct
    return [s for s, _ in sorted(by_score.items(), key=lambda kv: kv[1], reverse=True)[:limit]]
