"""One scan cycle:

    universe slice  ->  daily bars (cached)  ->  pre-filter
        ->  intraday bars + strategies (technical)
        ->  fundamentals + peers for the leaders  ->  strategies (fundamental)
        ->  size every play, rank, keep the short list

The universe is scanned in rotating slices of ``max_symbols_scanned`` so that
"all of the Nasdaq" is covered over several cycles without hammering the data
provider. Every step is per-symbol try/except so one bad ticker cannot abort
the cycle.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from ..core.eventbus import BUS
from ..core.models import Account, Play, ScanCandidate
from ..data.fundamentals import FundamentalsProvider
from ..data.market_data import MarketDataService
from ..data.sectors import SectorLookup, sector_allowed
from ..data.universe import UniverseLoader
from ..risk.position_sizing import size_play
from ..strategies.base import build_context
from ..util import clock
from .filters import passes_prefilter, rank_score

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
        }


class Scanner:
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
        self.strategies = strategies
        self.tech = [s for s in strategies if s.kind.value == "TECHNICAL"]
        self.fundamental = [s for s in strategies if s.kind.value == "FUNDAMENTAL"]

        self._loader = UniverseLoader()
        self._universe: List[str] = []
        self._cursor = 0
        self._last_account: Optional[Account] = None

        self._sectors = SectorLookup(use_yfinance=self.md.is_real)
        #: only scan / trade these sectors ([] = all); the engine keeps it in sync
        self.sectors_allowed: List[str] = []

    # ------------------------------------------------------------------ #
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

    #: tickers with no known sector yet, looked up online per cycle while a
    #: sector filter is on; the rest are skipped this cycle and resolved later
    SECTOR_LOOKUPS_PER_CYCLE = 20

    def _in_sectors(self, symbols: List[str]) -> List[str]:
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
            if sector_allowed(sec, self.sectors_allowed):
                keep.append(sym)
        return keep

    # ------------------------------------------------------------------ #
    def run_cycle(self) -> ScanResult:
        t0 = time.time()
        sc = self.settings.config.scanner
        bars = sc.bars or {}
        pf = sc.prefilter or {}
        run = ScanResult(run_id=f"scan_{uuid.uuid4().hex[:10]}", started_at=clock.now_ny())
        BUS.publish("scan.started", run_id=run.run_id)

        self._ensure_universe()
        run.universe_size = len(self._universe)
        slice_syms = self._next_slice(int(sc.max_symbols_scanned))
        if self.sectors_allowed:
            in_sector = self._in_sectors(slice_syms)
            run.sector_skipped = len(slice_syms) - len(in_sector)
            slice_syms = in_sector
        run.scanned = len(slice_syms)

        intraday_iv = bars.get("intraday_interval", "5m")
        intraday_days = int(bars.get("intraday_lookback_days", 10))
        daily_days = int(bars.get("daily_lookback_days", 400))
        equity = self._last_account.equity if self._last_account else 0.0

        # -- stage 1: daily bars + prefilter (parallel I/O) ------------- #
        cands: List[ScanCandidate] = []

        def _prefilter(sym: str):
            try:
                daily = self.md.get_price_history(sym, "1d", daily_days)
                quote = None
                try:
                    quote = self.md.get_quote(sym)
                except Exception:  # noqa: BLE001
                    pass
                cand = passes_prefilter(sym, daily, None, quote, pf)
                return sym, cand, daily, quote, None
            except Exception as e:  # noqa: BLE001
                return sym, None, None, None, str(e)

        daily_cache: Dict[str, object] = {}
        quote_cache: Dict[str, object] = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            for sym, cand, daily, quote, err in ex.map(_prefilter, slice_syms):
                if err:
                    # "no data" for a ticker (delisted, halted, not on the feed)
                    # is routine, not an error worth surfacing.
                    if "no data" not in err and "no market data" not in err and "no quote" not in err:
                        run.errors[sym] = err
                    continue
                if cand is not None:
                    cands.append(cand)
                    daily_cache[sym] = daily
                    quote_cache[sym] = quote
        cands.sort(key=lambda c: (c.rvol, c.atr_pct), reverse=True)
        run.prefiltered = len(cands)
        run.candidates = cands
        BUS.publish("scan.progress", run_id=run.run_id, stage="prefiltered", n=len(cands))

        # -- stage 2: technical strategies on the survivors ------------ #
        all_plays: List[Play] = []
        cand_by_sym = {c.symbol: c for c in cands}

        # intraday setups need a *live* session - skip them when the market is
        # closed (their "intraday" bars would just be yesterday's tail).
        intraday_ok = clock.is_market_open()
        active_tech = [s for s in self.tech
                       if s.timeframe.value != "INTRADAY" or intraday_ok]
        if not intraday_ok:
            log.info("market closed - running %d swing setups only (skipping intraday)",
                     len(active_tech))

        # when the market is closed, skip the per-symbol intraday fetch entirely
        # (no intraday setups run, and a daily sparkline is fine for swings)
        need_intraday = intraday_ok

        def _run_tech(sym: str):
            try:
                daily = daily_cache[sym]
                intr = daily
                if need_intraday:
                    try:
                        intr = self.md.get_price_history(sym, intraday_iv, intraday_days)
                    except Exception:  # noqa: BLE001
                        intr = daily            # fall back to daily; swing setups don't care
                q = quote_cache.get(sym) or None
                _c = cand_by_sym.get(sym)
                ctx = build_context(
                    sym, intr, daily, q, params={"valuation": _valuation_params(self.settings)},
                    account_equity=equity,
                    candidate=asdict(_c) if _c is not None else None,
                )
                src = intr if intr is not daily else daily
                spark = [round(float(x), 3) for x in src["close"].tail(60).tolist()]
                out: List[Play] = []
                for strat in active_tech:
                    try:
                        for p in strat.generate(ctx):
                            p.scan_run_id = run.run_id
                            p.score = rank_score(p, cand_by_sym.get(sym), strat.weight)
                            p.evidence.setdefault("spark", spark)
                            out.append(p)
                    except Exception as e:  # noqa: BLE001
                        log.debug("%s %s failed: %s", sym, strat.key, e)
                return sym, out, ctx, None
            except Exception as e:  # noqa: BLE001
                return sym, [], None, str(e)

        ctx_by_sym: Dict[str, object] = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(_run_tech, c.symbol) for c in cands[: int(sc.max_symbols_scanned)]]
            for fut in as_completed(futs):
                sym, plays, ctx, err = fut.result()
                if err and "no data" not in err and "no market data" not in err:
                    run.errors[sym] = err
                if ctx is not None:
                    ctx_by_sym[sym] = ctx
                all_plays.extend(plays)

        # -- stage 3: fundamentals for the leaders -------------------- #
        if self.fundamental and self.fund is not None:
            n_leaders = int(getattr(sc, "fundamentals_leaders", 8) or 8)
            peer_n = 6
            leaders = _leaders_for_fundamentals(cands, all_plays, limit=n_leaders)
            for sym in leaders:
                try:
                    ctx = ctx_by_sym.get(sym)
                    if ctx is None:
                        continue
                    ctx.fundamentals = self.fund.get(sym)
                    if ctx.fundamentals and ctx.fundamentals.has_min_data():
                        peer_syms = self.fund.peers(sym, limit=peer_n)
                        ctx.peers = [self.fund.get(p) for p in peer_syms]
                    spark = []
                    try:
                        spark = [round(float(x), 3) for x in ctx.intraday["close"].tail(60).tolist()]
                    except Exception:  # noqa: BLE001
                        pass
                    for strat in self.fundamental:
                        try:
                            for p in strat.generate(ctx):
                                p.scan_run_id = run.run_id
                                p.score = rank_score(p, cand_by_sym.get(sym), strat.weight)
                                if spark:
                                    p.evidence.setdefault("spark", spark)
                                all_plays.append(p)
                        except Exception as e:  # noqa: BLE001
                            log.debug("%s %s failed: %s", sym, strat.key, e)
                except Exception as e:  # noqa: BLE001
                    run.errors[sym] = f"fundamentals: {e}"

        # -- stage 4: sector tag, size, rank, shortlist ------------- #
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
                     and sector_allowed(p.sector, self.sectors_allowed)]
        all_plays.sort(key=lambda p: p.score, reverse=True)
        run.plays = all_plays

        seen: List[str] = []
        for p in all_plays:
            if p.symbol not in seen:
                seen.append(p.symbol)
            if len(seen) >= int(sc.shortlist_size):
                break
        run.shortlist = seen

        run.finished_at = clock.now_ny()
        run.elapsed_s = time.time() - t0
        log.info("scan %s: %s", run.run_id, run.summary())
        BUS.publish("scan.completed", run_id=run.run_id, summary=run.summary(),
                    plays=[p.to_row() for p in all_plays[:60]])
        return run


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
