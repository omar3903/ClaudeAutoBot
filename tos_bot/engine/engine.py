"""The conductor.

Holds the IB Gateway connection and the built-in simulator (connections.py),
the scanner, order execution and the automatic exits, and runs three
background loops:

    scan loop      the daily full scan before the open, the intraday cycles over
                   the hot list and sector buffers, and fast hot-list cycles while
                   Autopilot is day-trading (see scanner/schedule.py)
    sync loop      every few seconds: fills, automatic exits, quit progress
    snapshot loop  every 10-30 s: account, broker-vs-database check, broadcast

Whatever the user changes on the dashboard applies straight away, is
remembered in data/runtime.json (runtime.py) and is broadcast to every tab.

Safety rules: no order without :meth:`approve_play` (or Autopilot, inside its
caps); no venue change while positions are open on the current one; while
quitting with positions open, nothing but exits may change; and an OPEN trade
record is deleted only when a connected broker confirms the position is gone
(reconcile.py).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .. import secrets_store
from ..brokers import get_broker
from ..brokers.base import BrokerAdapter
from ..brokers.venues import (
    IBKR_STEPS, PAPER_PLATFORMS, ROUTE_LABELS, normalize_platform, plan_venue, venue_id, venue_label,
)
from ..config import DATA_DIR, RUNTIME_PATH, Secrets, Settings, get_settings
from ..core.enums import PlayStatus, Side, Timeframe
from ..core.eventbus import BUS
from ..core.models import Account, Play
from ..data.bars import DailyBarStore
from ..data.fundamentals import FundamentalsProvider
from ..data.listings import UsListings
from ..data.market_data import MarketData, NoDataSource
from ..data.sec_edgar import SecEdgarFundamentals
from ..data.symbols import SymbolMaster
from ..execution.autopilot import AutoPilot
from ..execution.executor import Executor
from ..execution.exit_manager import ExitManager
from ..execution.order_builder import plan_order
from ..persistence.db import init_db
from ..persistence.repository import Repository
from ..risk.pdt_guard import PdtGuard
from ..risk.position_sizing import size_play
from ..research.history import IntradayHistory, replay_symbols
from ..research.replay import ReplaySettings
from ..research.runner import ReplayRunner
from ..research.journal import ROLLING_SESSIONS, Journal, build_review, first_sightings, live_records, review_day
from ..research.movers import build_movers, read_session, rolling_capture, session_bounds
from ..research.weights import evidence_multiplier, pooled_odds
from ..quant.sizing import MIN_TRADES as KELLY_MIN_TRADES, half_kelly_risk_pct
from ..signals.earnings import EarningsCalendar
from ..pairs.desk import PairDesk, decision_window
from ..pairs.finder import FinderSettings
from ..pairs.model import KEY as PAIRS_KEY, PairRules
from ..signals.book import BoostSettings, SignalBook
from ..signals.calendar import describe as describe_report, next_report, sessions_until
from ..signals.service import SignalService
from ..signals.store import SignalStore
from .chart import candles, chart_payload
from .market_regime import BENCHMARK, MarketRegime
from ..scanner.noise import LABELS as NOISE_LABELS, NoiseSettings
from ..scanner import schedule
from ..scanner.filters import TradeFilters
from ..scanner.scanner import Scanner
from ..scanner.schedule import ScanSettings
from ..scanner.watchlist import DayWatchlist
from ..strategies.registry import REGISTRY, build_strategies, strategy_catalog
from ..util import clock
from ..util.logging_setup import setup_logging
from ..util.net import port_is_open
from . import capital, views
from .board import PlayBoard
from .connections import Connections
from .reconcile import PositionCheck
from .runtime import RuntimeFile, load_capital, load_day_trade_pct, load_filters, load_strategy_overrides

log = logging.getLogger(__name__)

#: how often the orders working at the broker are re-checked for the dashboard
ORDERS_POLL_S = 5.0
#: how old that list may be before a dashboard request asks the broker again
ORDERS_MAX_AGE_S = 8.0

#: play statuses that must never be executed again
_ACTED_ON = frozenset({PlayStatus.ACCEPTED, PlayStatus.SUBMITTED, PlayStatus.WORKING,
                       PlayStatus.PARTIAL, PlayStatus.FILLED, PlayStatus.ERROR})


class TradingEngine:
    #: retry the Gateway the switches want this often (longer after a failed connect)
    CONNECT_RETRY_S = 15.0
    CONNECT_RETRY_AFTER_FAIL_S = 60.0
    #: after a scan fails (usually: no Gateway yet), wait this long before the next scheduled one
    SCAN_RETRY_S = 60.0
    #: while quitting, re-send closes that haven't taken this often
    QUIT_RETRY_S = 30.0
    WEIGHT_RANGE = (0.1, 3.0)
    BOARD_ROWS = 80

    def __init__(self, settings: Optional[Settings] = None, *, data_dir: Optional[Path] = None,
                 runtime_path: Optional[Path] = None,
                 broker_factory: Callable[..., BrokerAdapter] = get_broker,
                 port_check: Callable[[str, int], bool] = port_is_open,
                 listings: Optional[UsListings] = None, fundamentals: Optional[FundamentalsProvider] = None,
                 init_database: bool = True) -> None:
        self.settings = settings or get_settings()
        cfg = self.settings.config
        setup_logging(cfg.app.log_level)
        if init_database:
            init_db()
        self.repo = Repository()
        data_dir = data_dir or DATA_DIR
        self.runtime = RuntimeFile(runtime_path or RUNTIME_PATH)
        saved = self.runtime.read()

        self.md = MarketData(DailyBarStore(data_dir / "bars"))
        self.connections = Connections(
            self.settings, self.md, broker_factory=broker_factory, port_check=port_check,
            simulator_state=(data_dir / "paper_state.json") if self.settings.secrets.paper_persist else None)

        # the dashboard's remembered choices win over config.yaml and .env
        self.mode: str = saved["mode"] if saved.get("mode") in ("paper", "live") else "paper"
        self.paper_platform = normalize_platform(saved.get("paper_platform") or self.settings.secrets.paper_platform)
        self.filters = load_filters(saved.get("filters"), cfg.scanner.sectors)
        self.strategy_overrides = load_strategy_overrides(saved.get("strategies"))
        #: how much of the account the bot may use, per venue, in the account's currency
        self.capital = load_capital(saved.get("capital"))
        #: the part of the trading capital day trades may hold; swing trades get the rest (engine/capital.py)
        self.day_trade_pct = load_day_trade_pct(saved.get("capital_split"), cfg.account.day_trade_pct)
        sc = cfg.scanner
        self.scan_settings = ScanSettings.load(saved.get("scan"), ScanSettings(
            premarket_time=sc.premarket_time, gapper_time=sc.gapper_time, cycle_minutes=sc.cycle_minutes,
            hot_list_size=sc.hot_list_size, sector_queue_size=sc.sector_queue_size))

        self.scanner = Scanner(
            self.settings, self.md, SymbolMaster(data_dir / "symbols.json"),
            listings or UsListings(data_dir / "cache" / "listings"),
            fundamentals or SecEdgarFundamentals(data_dir / "cache" / "sec"),
            data_dir / "watchlists", build_strategies(self.settings, self.strategy_overrides))
        self.scanner.filters = self.filters

        # insider trades and company news (see signals/): they nudge play scores and create insider-buying plays
        self.signal_book = SignalBook(BoostSettings.from_config(cfg.signals))
        self.scanner.signals = self.signal_book if cfg.signals.enabled else None
        self.signals = SignalService(cfg.signals, SignalStore(), self.signal_book, data_dir / "signals" / "state.json",
                                     watched=self._signal_watchlist,
                                     news_source=lambda: self.md.source if self.md.attached else None,
                                     con_ids=self.scanner.con_ids)

        #: set while quitting with positions still open - everything but exits is locked
        self.quit_state: Optional[Dict[str, Any]] = saved["quit"] if isinstance(saved.get("quit"), dict) else None
        #: called once quitting has finished (the server wires it to its own shutdown)
        self.on_shutdown: Optional[Callable[[], None]] = None

        self.board = PlayBoard()
        self.position_check = PositionCheck()
        self.replay = ReplayRunner(data_dir / "research" / "replay.json",
                                   IntradayHistory(data_dir / "research" / "intraday"))
        #: calm or turbulent, from SPY's daily returns (Hamilton's Markov switching model)
        self.regime = MarketRegime(data_dir / "research" / "benchmark_spy.pkl")
        #: when companies reported earnings (SEC 8-K item 2.02), for the replay
        self.earnings = EarningsCalendar(data_dir / "signals" / "earnings", lambda url: self.signals.sec.json(url),
                                         self.signals.company_ciks)
        #: the daily review (research/journal.py)
        self.journal = Journal(data_dir / "journal", self.repo)
        self._journal_checked: Optional[dt.date] = None
        self._movers_retry_at = 0.0
        self._earnings_warned: set = set()
        self._live_stats: Dict[str, Dict[str, Any]] = {}
        self._live_stats_at = float("-inf")
        self._risk_pct: Dict[str, Optional[float]] = {}
        self._risk_pct_for: Optional[tuple] = None
        self._started_at = time.monotonic()
        #: pairs trading (pairs/): the watch list, and both legs of every pair trade
        self.pairs = PairDesk(self.repo, cfg.pairs, data_dir / "pairs" / "watch.json")
        self._pairs_live_until = float("-inf")
        self._pair_etfs_on: Optional[dt.date] = None
        self._pairs_dry_noted: set = set()
        self._pairs_next_refresh = float("-inf")
        self._pairs_published = ""
        self.executor: Optional[Executor] = None
        self.exit_manager: Optional[ExitManager] = None
        self.pdt: Optional[PdtGuard] = None
        self._broker: Optional[BrokerAdapter] = None        # where orders go
        self._venue = "paper"                                # venue id stamped on new trades
        self._broker_since = 0.0
        self._live_blockers: List[str] = []
        self._connect_retry_at = 0.0
        #: IB Gateway as last seen by the snapshot loop (see _watch_gateway)
        self._gateway_up: Optional[bool] = None
        self._gateway_seen_up = False
        self._gateway_down_at: Optional[float] = None
        self._gateway_alerted = False
        self._account: Optional[Account] = None
        self._account_at = 0.0
        self._armed = False

        # hands-off entry (exits are always automatic)
        self.autopilot = AutoPilot(self, cfg.autopilot, bus=BUS, persist=self._save_runtime)
        self.autopilot.load_runtime(saved.get("autopilot", {}))

        self._scan_lock = threading.Lock()
        self._scan_request: Optional[str] = None
        self._scan_wake = threading.Event()
        self._scan_running: Optional[Dict[str, Any]] = None
        self._last_scans: Dict[str, Dict[str, Any]] = {}
        self._gappers_session: Optional[dt.date] = None      # the session the gap check last ran for
        self._last_cycle_at = float("-inf")
        self._last_fast_at = float("-inf")
        self._last_plays_at = float("-inf")
        self._noted: Dict[tuple, float] = {}          # Autopilot notes already shown, and when
        self._scan_retry_at = 0.0

        # the orders working at the broker, for the dashboard (see active_orders)
        self._orders: List[Dict[str, Any]] = []
        self._orders_ok = False
        self._orders_at = float("-inf")
        self._orders_checked: Optional[str] = None
        self._orders_lock = threading.Lock()

        self._quit_lock = threading.Lock()
        self._quit_retry_at = 0.0
        self._quit_rounds = 0
        self._switch_lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    @property
    def broker(self) -> Optional[BrokerAdapter]:
        return self._broker

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                         #
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        log.info("engine starting (mode=%s, paper platform=%s)", self.mode, self.paper_platform)
        self._bind()
        self._refresh_account()
        self._check_arm()
        if self.quit_state:
            log.warning("resuming an unfinished quit - closing the remaining positions first")
        self._threads = [threading.Thread(target=loop, name=name, daemon=True) for name, loop in (
            ("scan-loop", self._scan_loop), ("sync-loop", self._sync_loop), ("snapshot-loop", self._snapshot_loop),
            ("orders-loop", self._orders_loop), ("signals-loop", self._signals_loop),
            ("journal-loop", self._journal_loop), ("pairs-loop", self._pairs_loop))]
        for t in self._threads:
            t.start()
        BUS.publish("engine.started", state=self.snapshot())

    def stop(self) -> None:
        self._stop.set()
        self._scan_wake.set()
        self.connections.close_all()
        log.info("engine stopped")

    def _save_runtime(self) -> None:
        payload: Dict[str, Any] = {
            "mode": self.mode, "paper_platform": self.paper_platform, "filters": self.filters.as_dict(),
            "strategies": self.strategy_overrides, "capital": self.capital,
            "capital_split": {"day_pct": self.day_trade_pct}, "scan": self.scan_settings.as_dict(), "autopilot": self.autopilot.to_runtime(),
        }
        if self.quit_state:
            payload["quit"] = self.quit_state
        self.runtime.write(payload)

    # ------------------------------------------------------------------ #
    #  Where orders go                                                   #
    # ------------------------------------------------------------------ #
    def _bind(self) -> None:
        """Hold the Gateway connection the switches need and point order handling
        at the right broker. Live falls back to paper when it can't connect."""
        plan = plan_venue(self.mode, self.paper_platform)
        ibkr = self.connections.ensure(plan)
        if self.mode == "live" and ibkr is None:
            self._live_blockers = list(self.connections.blockers)
            log.warning("falling back to paper - your live IBKR account isn't reachable: %s",
                        "; ".join(self._live_blockers))
            self.mode = "paper"
            self._bind()
            return
        if self.mode == "live":
            self._live_blockers = []

        if plan.trade and ibkr is not None:
            broker, venue = ibkr, venue_id(plan)
        else:
            broker, venue = self.connections.simulator(), "paper"
        if broker is not self._broker:
            self._broker_since = time.monotonic()
        self._broker, self._venue = broker, venue

        cfg = self.settings.config
        self.pdt = PdtGuard(cfg.account, trade_repo=self.repo, paper=self.mode == "paper")
        if self.executor is None:
            self.executor = Executor(broker, self.repo, cfg.execution, bus=BUS, venue=venue)
            self.executor.scale_out = 0.0 < float(getattr(cfg.exit_manager, "scale_out_pct", 0.0) or 0.0) < 100.0
        else:
            self.executor.rebind(broker, venue=venue)
        self.executor.adopt_working_orders()          # before any exit can be sent twice
        self.exit_manager = ExitManager(self.repo, self.executor, quote_fn=self.md.quote,
                                        cfg=cfg.exit_manager, bus=BUS, venue=venue)
        self.position_check.reset()

    def _retry_connection(self, force: bool = False) -> bool:
        """Connect the account the switches want once it's reachable - IB Gateway
        started after the app, say. Never touches Live (a live switch that couldn't
        connect already put you back on paper), never acts while quitting, and
        never moves orders away from open positions."""
        plan = plan_venue(self.mode, self.paper_platform)
        if self.mode == "live" or self.quit_state or self.connections.holds(plan):
            return False
        now = time.monotonic()
        if not force and now < self._connect_retry_at:
            return False
        self._connect_retry_at = now + self.CONNECT_RETRY_S
        blockers = self.connections.prereqs(plan)
        if blockers:
            self.connections.blockers = blockers          # still unreachable - the pill says why
            return False
        if not self._switch_lock.acquire(blocking=False):
            return False                                  # a switch is running; try next time
        try:
            target = venue_id(plan) if plan.trade else self._venue
            if target == self._venue:
                ok = self.connections.ensure(plan) is not None     # prices only - orders stay put
            else:
                blocked = self._switch_blocked(target)
                if blocked:
                    self.connections.blockers = [f"{venue_label(target)} is reachable, but orders can't "
                                                 f"move there yet. {blocked}"]
                    return False
                prev = self.mode
                self._bind()
                ok = self.connections.connected
                if ok:
                    self._after_switch(prev, "auto-connect")
            if not ok:
                self._connect_retry_at = time.monotonic() + self.CONNECT_RETRY_AFTER_FAIL_S
                return False
        finally:
            self._switch_lock.release()
        self._scan_retry_at = 0.0
        where = venue_label(self._venue) if plan.trade else "IBKR prices"
        log.warning("connected to %s once it became reachable", where)
        BUS.publish("broker.connected", state=self.snapshot(),
                    note=f"Connected to {where} - " + ("orders now go there." if plan.trade
                                                       else "the simulator and the scans use them."))
        return True

    #: after this long without IB Gateway, the dashboard is told what to check
    GATEWAY_DOWN_ALERT_S = 600.0

    def _watch_gateway(self) -> None:
        """Say when IB Gateway drops and when it's back - its nightly restart, IBKR's maintenance - and,
        once it has been gone a while, what to check. Positions are only trusted again once the account
        has settled (see _reconcile_open_trades)."""
        up, now = self.connections.connected, time.monotonic()
        if self._gateway_up is None or up == self._gateway_up:
            if self._gateway_up is None:
                self._gateway_up, self._gateway_seen_up = up, up
                self._gateway_down_at = None if up else now
            elif (not up and self._gateway_down_at is not None and not self._gateway_alerted
                  and now - self._gateway_down_at >= self.GATEWAY_DOWN_ALERT_S):
                self._gateway_alerted = True
                note = (f"IB Gateway has been unreachable for {_duration(now - self._gateway_down_at)}. If it's asking "
                        "you to log in - IBKR wants a full login about once a week - log in again; the app reconnects "
                        "by itself. Until then there are no prices, and no automatic exits.")
                log.warning(note)
                BUS.publish("broker.down", note=note)
            return
        self._gateway_up = up
        if not up:
            self._gateway_down_at, self._gateway_alerted = now, False
            if self._gateway_seen_up:
                log.warning("IB Gateway disconnected - reconnecting by itself")
                BUS.publish("broker.disconnected", note=("IB Gateway disconnected - its nightly restart or IBKR's "
                                                         "maintenance. The app reconnects by itself."))
            return
        down_for, self._gateway_down_at = now - (self._gateway_down_at or now), None
        if not self._gateway_seen_up:
            self._gateway_seen_up = True                  # the first connection is announced by _retry_connection
            return
        self.position_check.reset()                       # misses counted before the drop don't carry over
        self._refresh_account()
        log.warning("IB Gateway is back after %s", _duration(down_for))
        BUS.publish("broker.reconnected", state=self.snapshot(),
                    note=f"IB Gateway is back after {_duration(down_for)}.")

    def _open_trades(self) -> List[Dict[str, Any]]:
        try:
            return self.repo.open_trades()
        except Exception:  # noqa: BLE001
            return []

    def _positions_here(self) -> List[Dict[str, Any]]:
        """OPEN trades held on the venue orders currently go to."""
        return [t for t in self._open_trades() if (t.get("broker") or "paper") == self._venue]

    def working_entries(self) -> List[Dict[str, Any]]:
        """Entry orders sent but not filled yet (see Executor.working_entries)."""
        return self.executor.working_entries() if self.executor else []

    def exposure_by_symbol(self) -> Dict[str, float]:
        """Dollars at work per stock on the current account: every position at its
        market price - whether or not the app has a record of it - plus entry
        orders still working."""
        out: Dict[str, float] = {}
        for pos in (self._account.positions if self._account else []):
            out[pos.symbol] = out.get(pos.symbol, 0.0) + abs(pos.quantity * (pos.market_price or pos.avg_price))
        for w in self.working_entries():
            out[w["symbol"]] = out.get(w["symbol"], 0.0) + w["notional"]
        return out

    def gross_exposure(self) -> float:
        return sum(self.exposure_by_symbol().values())

    def active_orders(self, max_age_s: Optional[float] = None) -> Dict[str, Any]:
        """The orders still working at the broker orders go to, and what each is for (see
        Executor.active_orders). The broker is asked again when the last answer is older
        than ``max_age_s`` seconds."""
        if time.monotonic() - self._orders_at >= (ORDERS_MAX_AGE_S if max_age_s is None else max_age_s):
            self._refresh_orders()
        return self._orders_payload()

    def _orders_payload(self) -> Dict[str, Any]:
        return {"orders": list(self._orders), "ok": self._orders_ok, "checked_at": self._orders_checked,
                "venue_label": venue_label(self._venue)}

    def _refresh_orders(self) -> None:
        """Ask the broker for its working orders, and publish orders.updated when they
        changed. When it can't be asked, the last list is kept and marked as not current."""
        if not self._orders_lock.acquire(blocking=False):
            return                                      # another thread is asking right now
        try:
            orders, ok = self._orders, False
            if self.executor and self._broker and self._broker.is_connected:
                try:
                    orders, ok = self.executor.active_orders(), True
                except Exception:  # noqa: BLE001
                    log.debug("could not list the orders working at the broker", exc_info=True)
            changed = (ok, _order_signature(orders)) != (self._orders_ok, _order_signature(self._orders))
            self._orders, self._orders_ok = orders, ok
            self._orders_at, self._orders_checked = time.monotonic(), clock.now_ny().isoformat()
            if changed:
                BUS.publish("orders.updated", **self._orders_payload())
        finally:
            self._orders_lock.release()

    # ------------------------------------------------------------------ #
    #  Strategy replay                                                   #
    # ------------------------------------------------------------------ #
    def start_replay(self, sessions: Optional[int] = None, swing_sessions: Optional[int] = None) -> Dict[str, Any]:
        """Replay the strategies over recent candles in the background (see research/)."""
        if not self.md.attached:
            return {"ok": False, "reason": "IB Gateway isn't connected, so there are no candles to replay."}
        symbols = replay_symbols(self.scanner.watchlist)
        if not symbols["swing"]:
            return {"ok": False, "reason": "Run the full scan first - the replay uses the stocks on its watchlist."}
        cfg = self.settings.config
        sessions = cfg.replay.sessions if sessions is None else sessions
        swing_sessions = cfg.replay.swing_sessions if swing_sessions is None else swing_sessions
        return self.replay.start(
            strategies=self.scanner.strategies, source=self.md.source, daily_frame=self.md.daily_frame,
            intraday_symbols=symbols["intraday"], swing_symbols=symbols["swing"],
            sessions=max(5, min(120, int(sessions))), swing_sessions=max(20, min(250, int(swing_sessions))),
            settings=ReplaySettings.from_exit_rules(cfg.exit_manager, cfg.replay),
            noise=NoiseSettings.from_config(cfg.noise), con_ids=self.scanner.con_ids(symbols["intraday"]),
            market=self._regime_history, earnings=self.earnings.times if cfg.signals.enabled else None,
            pairs=self._pair_replay_inputs() if cfg.pairs.enabled else None,
            held_out_fraction=float(cfg.replay.held_out_fraction),
            news=self._news_history if cfg.signals.enabled else None, benchmark=BENCHMARK)

    def _news_history(self, symbols: Sequence[str], first_day: dt.date) -> Dict[str, List[Dict[str, Any]]]:
        """Each stock's stored stories since ``first_day`` - as far back as the app has been reading
        the news - shaped like the signal book's, for the replay's news checks."""
        since = dt.datetime.combine(first_day, dt.time(0, 0), tzinfo=dt.timezone.utc) - dt.timedelta(days=5)
        out: Dict[str, List[Dict[str, Any]]] = {}
        for row in self.signals.store.news(list(symbols), since):
            out.setdefault(row["symbol"], []).append({"at": row["published_at"], "headline": row.get("headline"),
                                                      "kind": row.get("kind"), "source": row.get("source")})
        return out

    def _regime_history(self, first_day: dt.date) -> Dict[dt.date, float]:
        self._refresh_regime()
        return self.regime.history(first_day)

    def replay_state(self) -> Dict[str, Any]:
        cfg = self.settings.config.replay
        state = self.replay.state(self.autopilot.skipped_noise(), self.autopilot.min_confirmations)
        return {**state, "evidence": self.evidence_state(), "look_ahead_regime": self.regime.look_ahead,
                "defaults": {"sessions": cfg.sessions, "swing_sessions": cfg.swing_sessions,
                             "held_out_fraction": cfg.held_out_fraction}}

    def replay_history(self, limit: int = 30) -> List[Dict[str, Any]]:
        return self.replay.runs(limit)

    def strategy_record(self, key: str) -> Optional[Dict[str, Any]]:
        """A strategy's replayed record over the trades Autopilot would have taken."""
        return self.replay.records(self.autopilot.skipped_noise(), self.autopilot.min_confirmations).get(key)

    def learned_skips(self) -> List[str]:
        """Noise checks the replay has shown are worth skipping (see replay.learned_skips)."""
        return self.replay.learned_skips()

    # ------------------------------------------------------------------ #
    #  What the evidence says: the market, the weights, the risk         #
    # ------------------------------------------------------------------ #
    #: how long the closed real trades are kept before they're read again
    LIVE_STATS_S = 300.0
    #: how far back real trades count toward the weights and the half-Kelly risk
    LIVE_SESSIONS = 60

    def _refresh_regime(self) -> None:
        try:
            source = self.md.source if self.md.attached else None
            self.regime.refresh(source, self.scanner.con_ids([BENCHMARK]) if source is not None else None)
        except Exception:  # noqa: BLE001
            log.debug("market regime refresh failed", exc_info=True)

    def live_stats(self) -> Dict[str, Dict[str, Any]]:
        """Each strategy's closed real trades over the last sessions, in R."""
        mono = time.monotonic()
        if mono - self._live_stats_at >= self.LIVE_STATS_S:
            today = clock.session_date()
            try:
                first = min(clock.last_n_sessions(today, self.LIVE_SESSIONS))
                closed = self.repo.closed_trades_between(first, today)
                closed += [{"strategy": PAIRS_KEY, "r_multiple": r["r_multiple"]}
                           for r in self.repo.pair_trades_closed_between(first, today)]
                self._live_stats = live_records(closed)
            except Exception:  # noqa: BLE001
                log.debug("could not read the closed trades", exc_info=True)
            self._live_stats_at = mono
        return self._live_stats

    def evidence_state(self) -> Dict[str, Dict[str, Any]]:
        """Each active strategy's evidence multiplier and what it rests on (research/weights.py)."""
        records = self.replay.records(self.autopilot.skipped_noise(), self.autopilot.min_confirmations)
        live = self.live_stats()
        return {s.key: evidence_multiplier(records.get(s.key), live.get(s.key)).as_dict() for s in self.scanner.strategies}

    def evidence_weights(self) -> Dict[str, float]:
        return {key: row["multiplier"] for key, row in self.evidence_state().items()}

    def strategy_odds(self) -> Dict[str, Dict[str, Any]]:
        """Each active strategy's pooled win rate and trade count (research/weights.py pooled_odds),
        which calibrate the odds its plays state (strategies/base.py calibrated_probability)."""
        records = self.replay.records(self.autopilot.skipped_noise(), self.autopilot.min_confirmations)
        live = self.live_stats()
        odds = ((s.key, pooled_odds(records.get(s.key), live.get(s.key))) for s in self.scanner.strategies)
        return {key: row for key, row in odds if row is not None}

    def strategy_risk_pct(self, key: str) -> Optional[float]:
        """Half-Kelly risk per trade from the strategy's record (quant/sizing.py): its real trades
        once there are enough - paper trading is the true out-of-sample test - otherwise the
        replayed trades Autopilot would have taken. A record with no edge still leaves a quarter
        of the usual risk for a trade taken by hand; Autopilot doesn't take those at all."""
        stats, ap = self.live_stats(), self.autopilot
        skipped = ap.skipped_noise()
        records_for = (self.replay.ran_at, tuple(skipped), ap.min_confirmations, self._live_stats_at)
        if records_for != self._risk_pct_for:                  # sized once per strategy until a record changes
            self._risk_pct, self._risk_pct_for = {}, records_for
        if key not in self._risk_pct:
            cap = float(self.settings.config.risk.max_risk_per_trade_pct)
            live = stats.get(key, {}).get("r", [])
            rs = live if len(live) >= KELLY_MIN_TRADES else self.replay.r_multiples(key, skipped, ap.min_confirmations)
            pct = half_kelly_risk_pct(rs, cap)
            self._risk_pct[key] = None if pct is None else max(pct, 0.25 * cap)
        return self._risk_pct[key]

    def _entry_context(self, p: Play, operator: str) -> Dict[str, Any]:
        """What a play was taken on, kept in its evidence for the journal."""
        return {"at": clock.now_ny().isoformat(), "by": operator, "noise": list(p.noise),
                "confirmations": p.confirmations, "score": round(p.score, 4), "confidence": round(p.confidence, 3),
                "probability": round(p.probability, 3), "reward_risk": round(p.reward_risk, 2),
                "market_regime": self.regime.context(), "skipped_noise": self.autopilot.skipped_noise(),
                "unproven": self.autopilot.proof_missing(p.strategy), "replay_record": self.strategy_record(p.strategy),
                "evidence_weight": self.evidence_weights().get(p.strategy, 1.0),
                "risk_pct": self.strategy_risk_pct(p.strategy)}

    # ------------------------------------------------------------------ #
    #  The journal                                                       #
    # ------------------------------------------------------------------ #
    JOURNAL_POLL_S = 60.0
    #: how long after starting the review waits for IB Gateway, to follow the plays not taken
    JOURNAL_GATEWAY_WAIT_S = 600.0
    #: how soon a session's movers are tried again when they couldn't be built (IB Gateway away)
    MOVERS_RETRY_S = 900.0
    #: a mover's chart: daily candles before the session, and after it once they exist
    MOVER_CHART_BEFORE, MOVER_CHART_AFTER = 60, 10

    def _journal_loop(self) -> None:
        self._stop.wait(20.0)
        while not self._stop.is_set():
            try:
                self._warn_earnings_ahead()
            except Exception:  # noqa: BLE001
                log.exception("the earnings check on open positions failed")
            try:
                self._review_if_due()
            except Exception:  # noqa: BLE001
                log.exception("the daily review failed")
            self._stop.wait(self.JOURNAL_POLL_S)

    def _review_if_due(self) -> None:
        cfg = self.settings.config.journal
        if not cfg.enabled:
            return
        day = review_day(clock.now_ny(), cfg.review_at)
        if self._journal_checked == day or time.monotonic() < self._movers_retry_at:
            return
        review = self.journal.get(day)
        wants_movers = cfg.movers > 0 and not _movers_built(review)
        if review is not None and not wants_movers:
            self._journal_checked = day
            return
        if not self.md.attached and time.monotonic() - self._started_at < self.JOURNAL_GATEWAY_WAIT_S:
            return
        if wants_movers and self.md.attached:
            try:
                self.scanner.update_market_daily(day)
            except Exception:  # noqa: BLE001
                log.warning("could not download the session's daily candles for the movers", exc_info=True)
        out = self.review_session(day) if review is None else self.add_movers(day)
        if wants_movers and not _movers_built(out.get("review")):
            self._movers_retry_at = time.monotonic() + self.MOVERS_RETRY_S
        else:
            self._journal_checked = day

    def _warn_earnings_ahead(self) -> None:
        """Say once when a swing position is held into an earnings report due by the next session:
        the report can gap the price straight through its stop (signals/calendar.py)."""
        if not self.settings.config.signals.enabled:
            return
        now = clock.now_ny()
        for t in self._open_trades():
            if t.get("timeframe") != "SWING" or t.get("pair_id"):
                continue
            upcoming = next_report(self.signal_book.earnings_for(t["symbol"]), now)
            if upcoming is None or sessions_until(upcoming, now) > 1 or (t["id"], upcoming["date"]) in self._earnings_warned:
                continue
            self._earnings_warned.add((t["id"], upcoming["date"]))
            note = (f"{t['symbol']} reports earnings {describe_report(upcoming)} and a swing position is open - "
                    "a report can gap the price straight through its stop.")
            log.warning(note)
            BUS.publish("position.earnings_ahead", symbol=t["symbol"], trade_id=t["id"], report=dict(upcoming), note=note)

    def review_session(self, day: Optional[dt.date] = None) -> Dict[str, Any]:
        """Build one session's review (again, if it exists) and keep it. Its movers are rebuilt once every
        stock's candles for the session are on disk; until then the ones built before are kept."""
        cfg = self.settings.config
        day = day or review_day(clock.now_ny(), cfg.journal.review_at)
        trades = [t for t in self.repo.closed_trades_between(day, day) if not t.get("pair_id")]
        plays = self.repo.plays_on(day)
        pair_trades = self.repo.pair_trades_closed_between(day, day)
        if not trades and not plays and not pair_trades and not self._movers_ready(day):
            return {"ok": False, "reason": f"Nothing was offered or traded on {day.isoformat()}."}
        first = min(clock.last_n_sessions(day, ROLLING_SESSIONS))
        ap = self.autopilot
        skipped = ap.skipped_noise()
        review = build_review(
            day, trades=trades, plays=plays, rolling=self.repo.closed_trades_between(first, day),
            replay_records=self.replay.records(skipped, ap.min_confirmations), evidence=self.evidence_state(),
            regime=self.regime.reading(), bars=self._session_bars(day, plays),
            settings=ReplaySettings.from_exit_rules(cfg.exit_manager, cfg.replay), skip_noise=skipped,
            min_confirmations=ap.min_confirmations, passes=self._passes_checks,
            styles={k: c.style for k, c in REGISTRY.items()}, titles={k: c.title for k, c in REGISTRY.items()},
            breakeven_at_r=float(cfg.exit_manager.breakeven_at_r))
        if pair_trades:
            review["pairs"] = [{k: r.get(k) for k in ("id", "pair", "side", "opened_at", "closed_at", "entry_z",
                                                   "exit_z_at", "exit_reason", "realized_pl", "r_multiple", "by")}
                              for r in pair_trades]
            total = sum(float(r.get("r_multiple") or 0.0) for r in pair_trades)
            review["lessons"].append(f"{len(pair_trades)} pair trade{'s' if len(pair_trades) != 1 else ''} "
                                     f"closed: {total:+.2f}R in all.")
        if cfg.journal.movers > 0:
            earlier = (self.journal.get(day) or {}).get("movers")
            review["movers"] = self._movers(day, review, plays) or (earlier if _movers_built({"movers": earlier})
                                                                    else self._movers_pending())
            if not trades and not plays and not pair_trades and not _movers_built(review):
                return {"ok": False, "reason": f"Nothing was offered or traded on {day.isoformat()}."}
            if not _movers_built(review):
                self._journal_checked, self._movers_retry_at = None, 0.0     # the journal loop adds them
        self.journal.save(review)
        self._live_stats_at = float("-inf")
        BUS.publish("journal.updated", session=review["session"], mistakes=len(review["mistakes"]),
                    lessons=len(review["lessons"]))
        shadows = review["shadows"]
        return {"ok": True, "review": review,
                "note": (f"Reviewed {review['session']}: {review['day'].get('trades', 0)} closed trades, "
                         f"{len(review['mistakes'])} things to learn from, {shadows.get('filled', 0)} plays not taken "
                         "followed to their outcome.")}

    def add_movers(self, day: dt.date) -> Dict[str, Any]:
        """Add the movers to a session's review written without them."""
        review = self.journal.get(day)
        if review is None:
            return self.review_session(day)
        movers = self._movers(day, review, self.repo.plays_on(day))
        if movers is None:
            return {"ok": False, "reason": "The session's movers can't be built yet."}
        review["movers"] = movers
        self.journal.save(review)
        BUS.publish("journal.updated", session=review["session"], mistakes=len(review["mistakes"]),
                    lessons=len(review["lessons"]))
        return {"ok": True, "review": review}

    def _movers_ready(self, day: dt.date) -> bool:
        have = self.scanner.market_daily
        return self.settings.config.journal.movers > 0 and have is not None and have[0] >= day

    def _movers_pending(self) -> Dict[str, Any]:
        return {"ok": False, "note": "The market's biggest movers are added once every stock's candles for the session "
                                     "are in - a few minutes after the close with IB Gateway connected."
                                     if self.md.attached else
                                     "IB Gateway isn't connected - the market's biggest movers are added once it is."}

    def _movers(self, day: dt.date, review: Mapping[str, Any], plays: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The session's biggest movers across the market (research/movers.py) - None until every stock's
        candles for the session are on disk."""
        if not self._movers_ready(day):
            return None
        cfg = self.settings.config
        _, tradable = self.scanner.market_daily
        try:
            universe = read_session(((s, self.md.daily_frame(s)) for s in tradable + [BENCHMARK]), day,
                                    sector_of=self.scanner.symbols.sector, prefilter=cfg.scanner.prefilter,
                                    sectors_allowed=self.filters.sectors, benchmark=BENCHMARK)
            ranked = sorted(universe.moves, key=lambda m: -abs(m.change_pct))
            symbols = [m.symbol for m in ranked if m.change_pct > 0][:cfg.journal.movers] + \
                      [m.symbol for m in ranked if m.change_pct < 0][:cfg.journal.movers]
            news, note = self._mover_news(day, symbols)
            if symbols and self.md.attached:                    # the session's 5-minute candles, for the charts
                try:
                    self.replay.history.load(self.md.source, symbols, 1, self.scanner.con_ids(symbols),
                                             today=clock.next_trading_day(day))
                except Exception:  # noqa: BLE001
                    log.debug("could not download the movers' 5-minute candles", exc_info=True)
            return build_movers(universe, per_side=cfg.journal.movers, sector_of=self.scanner.symbols.sector, news=news,
                                trades=self.repo.trades_on(day), plays=plays,
                                shadows=(review.get("shadows") or {}).get("plays") or [],
                                saved_watchlist=DayWatchlist.saved(self.scanner.watchlist_dir, day),
                                hot_size=self.scan_settings.hot_list_size, queue_size=self.scan_settings.sector_queue_size,
                                prefilter=cfg.scanner.prefilter, news_note=note)
        except Exception:  # noqa: BLE001
            log.exception("could not build the movers for %s", day)
            return None

    def _mover_news(self, day: dt.date, symbols: List[str]):
        if not self.settings.config.signals.enabled:
            return None, "News isn't read while the signals are off (signals.enabled in config.yaml)."
        start, _, end = session_bounds(day)
        try:
            news = self.signals.stories_between(symbols, start, end)
        except Exception:  # noqa: BLE001
            log.warning("could not read the movers' news", exc_info=True)
            return None, "The news couldn't be read."
        return news, "" if self.md.attached else "IB Gateway wasn't connected, so IBKR's news feeds weren't read."

    def mover_chart(self, day: dt.date, symbol: str) -> Dict[str, Any]:
        """A mover's daily candles around the session and the session's 5-minute candles, with what the bot
        did and the news - drawn by the Reports page."""
        movers = (self.journal.get(day) or {}).get("movers") or {}
        row = next((r for r in movers.get("gainers", []) + movers.get("losers", []) if r["symbol"] == symbol), None)
        if row is None:
            return {"ok": False, "reason": f"{symbol} isn't among that session's movers."}
        daily = self.md.daily_frame(symbol)
        if daily is not None and len(daily):
            at = int((daily.index.date <= day).sum())
            daily = daily.iloc[max(0, at - self.MOVER_CHART_BEFORE):at + self.MOVER_CHART_AFTER]
        frame = self.replay.history.stored(symbol)
        if (frame is None or not (frame.index.date == day).any()) and self.md.attached:
            try:
                frame = self.replay.history.load(self.md.source, [symbol], 1, self.scanner.con_ids([symbol]),
                                                 today=clock.next_trading_day(day)).get(symbol)
            except Exception:  # noqa: BLE001
                log.debug("chart candles for %s on %s failed", symbol, day, exc_info=True)
        session = frame[frame.index.date == day] if frame is not None else None
        return {"ok": True, "symbol": symbol, "session": day.isoformat(), "row": row,
                "daily": candles(daily, self.MOVER_CHART_BEFORE + self.MOVER_CHART_AFTER),
                "intraday": candles(session, 200)}

    def _session_bars(self, day: dt.date, plays: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The session's 5-minute candles for the plays not taken (kept with the replay's candles)."""
        symbols = list(dict.fromkeys(p["symbol"] for p in first_sightings(plays)))
        if not symbols:
            return {}
        if not self.md.attached:
            return None
        try:
            return self.replay.history.load(self.md.source, symbols, 1, self.scanner.con_ids(symbols),
                                            today=clock.next_trading_day(day))
        except Exception:  # noqa: BLE001
            log.warning("could not download the session's candles for the review", exc_info=True)
            return None

    def _passes_checks(self, row: Mapping[str, Any]) -> bool:
        """Whether a recorded play clears Autopilot's own checks on it (not the account's caps)."""
        ap = self.autopilot
        return (row.get("timeframe") in ap.play_types() and float(row.get("confidence") or 0) >= ap.min_confidence
                and float(row.get("reward_risk") or 0) >= ap.min_reward_risk
                and not set(ap.skipped_noise()).intersection(row.get("noise") or [])
                and (row.get("timeframe") != "INTRADAY" or int(row.get("confirmations") or 1) >= ap.min_confirmations))

    # ------------------------------------------------------------------ #
    #  Pairs trading (pairs/)                                            #
    # ------------------------------------------------------------------ #
    PAIRS_POLL_S = 30.0
    #: the watch list's live prices are read for this long after the Pairs tab asks
    PAIRS_LIVE_S = 120.0
    #: while the stored candles lag the last session, the pairs are looked for again this often
    PAIRS_REFRESH_RETRY_S = 300.0

    def _pairs_loop(self) -> None:
        self._stop.wait(15.0)
        while not self._stop.is_set():
            try:
                self._pairs_pass()
            except Exception:  # noqa: BLE001
                log.exception("pairs pass failed")
            self._stop.wait(self.PAIRS_POLL_S)

    def _pairs_pass(self) -> None:
        cfg = self.settings.config.pairs
        if not cfg.enabled or self.executor is None:
            return
        self._refresh_pairs()
        self.pairs.sync(self.executor)
        now = clock.now_ny()
        window = decision_window(now, cfg)
        watch = (window or time.monotonic() < self._pairs_live_until
                 or clock.minutes_to_close(now) <= float(cfg.window_minutes[0]) + 10)
        prices = self._pair_prices(watch)
        self.pairs.manage(self.executor, prices, self.md.daily_frame, in_window=window, now=now)
        if window and not self.quit_state:
            self._autopilot_pairs(prices)
        view = self.pairs_state(prices=prices)
        signature = json.dumps(view, sort_keys=True, default=str)
        if signature != self._pairs_published:                 # only when something on the Pairs tab changed
            self._pairs_published = signature
            BUS.publish("pairs.updated", **view)

    def _pair_groups(self) -> Dict[str, str]:
        """The stocks pairs are looked for among - the watchlist's, grouped by IBKR industry -
        plus the ETF pairs in config.yaml."""
        groups: Dict[str, str] = {}
        for symbol in replay_symbols(self.scanner.watchlist)["swing"]:
            info = self.scanner.symbols.get(symbol)
            group = (info.industry or info.sector) if info is not None else ""
            if group:
                groups[symbol] = group
        for pair in self.settings.config.pairs.etf_pairs or []:
            if len(pair) == 2:
                for symbol in pair:
                    groups.setdefault(str(symbol).upper(), "ETF " + "/".join(str(s).upper() for s in pair))
        return groups

    def _refresh_pairs(self) -> None:
        if self.scanner.watchlist is None:
            return
        through = schedule.last_completed_session(clock.now_ny())
        if self.pairs.refreshed_for == through.isoformat() or time.monotonic() < self._pairs_next_refresh:
            return                                     # already fitted on the latest session, or tried a moment ago
        self._pairs_next_refresh = time.monotonic() + self.PAIRS_REFRESH_RETRY_S
        groups = self._pair_groups()
        etfs = [s for s, g in groups.items() if g.startswith("ETF ")]
        if etfs and self.md.attached and self._pair_etfs_on != through:
            try:
                self.md.update_daily(etfs, through)
            except Exception:  # noqa: BLE001
                log.debug("could not bring the ETF pairs' candles up to date", exc_info=True)
            self._pair_etfs_on = through
        frames = {s: f for s in groups if (f := self.md.daily_frame(s)) is not None}
        if frames:
            self.pairs.refresh(frames, groups)

    def _pair_replay_inputs(self) -> Optional[Dict[str, Any]]:
        groups = self._pair_groups()
        if not groups:
            return None
        cfg = self.settings.config
        return {"groups": groups, "finder": FinderSettings.from_config(cfg.pairs),
                "rules": PairRules.from_config(cfg.pairs, cost_bps=cfg.replay.slippage_bps + cfg.replay.commission_bps)}

    def _quotes(self, symbols: List[str]) -> Dict[str, float]:
        """The latest price of each symbol that has one."""
        if not symbols or not self.md.attached:
            return {}

        def one(symbol: str):
            try:
                q = self.md.quote(symbol)
                px = getattr(q, "last", 0.0) or getattr(q, "mid", 0.0)
                return symbol, float(px) if px else None
            except Exception:  # noqa: BLE001
                return symbol, None

        with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
            return {s: px for s, px in pool.map(one, symbols) if px}

    def _pair_prices(self, watch: bool) -> Dict[str, float]:
        """Live prices for the legs of the pair trades on, and for the watched pairs when asked -
        in the regular session only; outside it the desk works from the closes."""
        if clock.current_session() is not clock.Session.REGULAR:
            return {}
        symbols = {t["symbol"] for t in self._open_trades() if t.get("pair_id")}
        if watch:
            symbols |= {s for m in self.pairs.models for s in (m.first, m.second)}
        return self._quotes(sorted(symbols))

    def pairs_state(self, prices: Optional[Mapping[str, float]] = None, live: bool = False) -> Dict[str, Any]:
        cfg = self.settings.config.pairs
        if live:
            self._pairs_live_until = time.monotonic() + self.PAIRS_LIVE_S
            if prices is None:
                prices = self._pair_prices(watch=True)
        prices = prices or {}
        now = clock.now_ny()
        ap = self.autopilot
        return {
            "enabled": bool(cfg.enabled), "refreshed_for": self.pairs.refreshed_for,
            "window": {"open": decision_window(now, cfg), "minutes": list(cfg.window_minutes),
                       "regular": clock.current_session(now) is clock.Session.REGULAR},
            "watch": self.pairs.watch(self.md.daily_frame, prices),
            "trades": self.pairs.trade_rows(prices, self.md.daily_frame),
            "recent": self.pairs.trade_rows({}, self.md.daily_frame, statuses=["CLOSED", "FAILED"], limit=20),
            "record": self.strategy_record(PAIRS_KEY),
            "autopilot": {"on": bool(ap.enabled and "PAIRS" in ap.trade_types), "proof": ap.proof_missing(PAIRS_KEY)},
            "limits": {"max_open_pairs": cfg.max_open_pairs, "max_new_per_day": cfg.max_new_per_day,
                       "emergency_loss_r": cfg.emergency_loss_r},
        }

    def enter_pair(self, pair_id: str, operator: str = "operator") -> Dict[str, Any]:
        """Put a watched pair on: both legs at the market, sized off the distance to its stop."""
        cfg = self.settings.config
        with self._switch_lock:
            locked = self._locked()
            if locked:
                return {"ok": False, "reason": locked}
            if not cfg.pairs.enabled:
                return {"ok": False, "reason": "Pairs trading is off (pairs.enabled in config.yaml)."}
            if self.executor is None:
                return {"ok": False, "reason": "No broker to send the orders to."}
            self._refresh_account()
            acc = self._account
            if acc is None:
                return {"ok": False, "reason": "no account data"}
            if self.mode == "live":
                if not self._armed:
                    return {"ok": False, "reason": f"engine not armed - live equity below "
                                                   f"${cfg.account.min_start_equity:,.0f} floor"}
                if cfg.account.cash_account:
                    return {"ok": False, "reason": "A pair shorts one of its stocks, which needs a margin account."}
                if acc.equity < cfg.account.pdt_equity_threshold:
                    return {"ok": False, "reason": (
                        f"Live pairs need ${cfg.account.pdt_equity_threshold:,.0f} in a margin account: both legs can "
                        "close the same day, and under that the pattern-day-trader rule would block them.")}
            if not acc.usd_per_base:
                return {"ok": False, "reason": f"no {acc.base_currency}->USD exchange rate yet, so the pair can't be sized"}
            model = self.pairs.model(pair_id)
            if model is None:
                return {"ok": False, "reason": f"{pair_id} isn't on the pairs watch list."}
            sizing = self.sizing_account(capital.SWING) or acc
            holding = ({t["symbol"] for t in self._open_trades()} | {w["symbol"] for w in self.working_entries()}
                       | {p.symbol for p in acc.positions if abs(p.quantity) > 1e-9})
            cap = float(cfg.risk.max_risk_per_trade_pct)
            kelly = self.strategy_risk_pct(PAIRS_KEY)
            pct = min(cap, kelly) if kelly is not None else cap
            buying_power = float(sizing.buying_power or sizing.equity)
            room = (getattr(sizing, "raw", None) or {}).get("capital_room")
            if room is not None:
                buying_power = min(buying_power, max(0.0, float(room)))
            out = _attempt(self.pairs.enter,
                pair_id, executor=self.executor, account=acc, prices=self._quotes([model.first, model.second]),
                closes=self.md.daily_frame, risk_dollars=sizing.equity * pct / 100.0,
                max_leg_value=sizing.equity * float(cfg.risk.max_position_pct_of_equity) / 100.0,
                buying_power=buying_power, holding=holding, venue=self._venue, by=operator)
        self._refresh_account()
        BUS.publish("pairs.updated", **self.pairs_state(prices=self._pair_prices(watch=True)))
        return out

    def close_pair(self, pair_trade_id: str, reason: str = "manual") -> Dict[str, Any]:
        """Exit both legs of a pair at the market - allowed while quitting too."""
        if self.executor is None:
            return {"ok": False, "reason": "No broker to send the orders to."}
        out = self.pairs.close(pair_trade_id, self.executor, reason=reason)
        self._refresh_account()
        BUS.publish("account.snapshot", state=self.snapshot())
        return out

    def pair_chart(self, pair_id: str) -> Dict[str, Any]:
        model = self.pairs.model(pair_id)
        prices = self._pair_prices(watch=False)
        if model is not None and clock.current_session() is clock.Session.REGULAR:
            prices = {**prices, **self._quotes([model.first, model.second])}
        return self.pairs.chart(pair_id, self.md.daily_frame, prices)

    def _autopilot_pairs(self, prices: Mapping[str, float]) -> None:
        """Autopilot's pairs: only with PAIRS among its trade types, only once the replay has proven
        the pair rules, a few a day, inside its exposure cap."""
        ap, cfg = self.autopilot, self.settings.config.pairs
        status = ap.status()
        if not (status["enabled"] and status["effective"] and "PAIRS" in ap.trade_types):
            return
        if ap.require_proven and ap.proof_missing(PAIRS_KEY):
            return
        if self.repo.pair_trades_opened_on(clock.session_date()) >= int(cfg.max_new_per_day):
            return
        acc = self.sizing_account(capital.SWING)
        if acc is None or (acc.equity and self.gross_exposure() > acc.equity * ap.max_gross_exposure_pct / 100.0):
            return
        rows = [r for r in self.pairs.watch(self.md.daily_frame, prices) if r["signal"] and r["live"]]
        for row in sorted(rows, key=lambda r: abs(r["z"] or 0.0) - r["entry_z"], reverse=True):
            if ap.dry_run:
                noted = (clock.session_date().isoformat(), row["id"])
                self._pairs_dry_noted = {n for n in self._pairs_dry_noted if n[0] == noted[0]}
                if noted not in self._pairs_dry_noted:
                    self._pairs_dry_noted.add(noted)
                    log.info("autopilot DRY-RUN would enter pair %s (%s)", row["id"], row["side"])
                return
            out = self.enter_pair(row["id"], operator="autopilot")
            if out.get("ok"):
                log.warning("autopilot ENTERED pair %s: %s", row["id"], out.get("note"))
                return

    def journal_state(self, limit: int = 60) -> Dict[str, Any]:
        cfg = self.settings.config.journal
        return {"enabled": cfg.enabled, "review_at": cfg.review_at, "days": self.journal.days(limit)}

    def journal_review(self, day: dt.date) -> Optional[Dict[str, Any]]:
        review = self.journal.get(day)
        if _movers_built(review):
            sessions = [d["session"] for d in self.journal.days(3 * ROLLING_SESSIONS) if d["session"] <= day.isoformat()]
            review["capture"] = rolling_capture(r for s in sessions if (r := self.journal.get(dt.date.fromisoformat(s))))
        return review

    def _switch_blocked(self, target_venue: str) -> Optional[str]:
        """Refuse to move orders to another venue while positions are open on the
        current one - their automatic exits would go to the wrong account."""
        if target_venue == self._venue:
            return None
        try:
            held = [t for t in self.repo.open_trades() if (t.get("broker") or "paper") == self._venue]
        except Exception:  # noqa: BLE001
            return "Couldn't check your open positions, so the switch was cancelled."
        if not held:
            return None
        symbols = ", ".join(sorted({t["symbol"] for t in held}))
        return (f"You have {len(held)} open position(s) on {venue_label(self._venue)} ({symbols}). "
                "Close them, or let their automatic exits finish, before switching.")

    def _locked(self) -> Optional[str]:
        """While quitting with positions open, only exits may happen."""
        if not self.quit_state:
            return None
        n = len(self._positions_here())
        return (f"Quitting: closing {n} open position{'' if n == 1 else 's'} first. "
                "Nothing else can change until they're all closed.")

    def _after_switch(self, prev_mode: str, operator: str) -> None:
        self._save_runtime()
        self._refresh_account()
        self._check_arm()
        self._scan_retry_at = 0.0
        log.warning("routing changed by %s: mode %s -> %s, paper platform %s, orders -> %s",
                    operator, prev_mode, self.mode, self.paper_platform, self._venue)
        BUS.publish("broker.switched", mode=self.mode, prev=prev_mode, state=self.snapshot())

    def set_mode(self, mode: str, operator: str = "operator") -> Dict[str, Any]:
        mode = (mode or "").lower()
        if mode not in ("paper", "live"):
            return {"ok": False, "reason": "mode must be 'paper' or 'live'"}
        with self._switch_lock:
            locked = self._locked()
            if locked:
                return {"ok": False, "reason": locked}
            if mode == self.mode:
                return {"ok": True, "mode": self.mode, "note": "already in that mode"}
            blocked = self._switch_blocked(venue_id(plan_venue(mode, self.paper_platform)))
            if blocked:
                return {"ok": False, "reason": blocked}
            prev, self.mode = self.mode, mode
            self._bind()                                   # knocks mode back to paper if live isn't reachable
            if mode == "live" and self.mode != "live":
                return {"ok": False, "reason": "Your live IBKR account isn't reachable.",
                        "blockers": self._live_blockers}
            self._after_switch(prev, operator)
            where = venue_label(self._venue)
            return {"ok": True, "mode": self.mode,
                    "note": f"LIVE - orders now go to {where}." if self.mode == "live" else f"Paper - orders go to {where}."}

    def set_paper_platform(self, platform: Optional[str], operator: str = "operator") -> Dict[str, Any]:
        if platform not in PAPER_PLATFORMS:
            return {"ok": False, "reason": f"paper platform must be one of {', '.join(PAPER_PLATFORMS)}"}
        with self._switch_lock:
            locked = self._locked()
            if locked:
                return {"ok": False, "reason": locked}
            if platform == self.paper_platform:
                return {"ok": True, "note": "No change.", "venue": self._venue_state()}
            blocked = self._switch_blocked(venue_id(plan_venue(self.mode, platform)))
            if blocked:
                return {"ok": False, "reason": blocked}
            prev, self.paper_platform = self.mode, platform
            self._bind()
            self._after_switch(prev, operator)
            note = f"{'Live' if self.mode == 'live' else 'Paper'} orders go to {venue_label(self._venue)}."
            if self.connections.blockers:
                note += " Not connected yet: " + "; ".join(self.connections.blockers)
            return {"ok": True, "note": note, "venue": self._venue_state()}

    def reconnect(self, operator: str = "reconnect") -> Dict[str, Any]:
        """Drop and re-open the Gateway connection - after starting the Gateway or
        changing its settings. Allowed while quitting (exits may need it)."""
        with self._switch_lock:
            prev = self.mode
            self.connections.close()
            self._bind()
            self._after_switch(prev, operator)
        state = self._venue_state()
        problems = self.connections.blockers or (self._live_blockers if prev != self.mode else [])
        if problems:
            return {"ok": False, "reason": "; ".join(problems), "venue": state}
        return {"ok": True, "venue": state,
                "note": f"Connected - {'live' if self.mode == 'live' else 'paper'} orders go to "
                        f"{venue_label(self._venue)}."}

    def save_secrets(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        """Write the Gateway settings from the Connections panel to .env, then reconnect."""
        try:
            changed = secrets_store.write(values or {})
        except ValueError as e:
            return {"ok": False, "reason": str(e)}
        self.settings.secrets = Secrets()             # the same Settings object everyone holds
        out: Dict[str, Any] = {"ok": True, "changed": changed, "fields": secrets_store.describe(),
                               "signal_fields": secrets_store.describe(fields=secrets_store.SIGNAL_FIELDS)}
        if not changed:
            out["note"] = "Nothing changed."
            return out
        log.info("dashboard updated .env: %s", ", ".join(changed))    # names only, never values
        if any(k.startswith("IBKR_") for k in changed):
            r = self.reconnect(operator="settings saved")
            out["venue"] = r["venue"]
            out["note"] = "Saved. " + (r.get("note") or f"Not connected yet: {r.get('reason')}")
        else:
            out["note"] = "Saved."
        return out

    def probe_ibkr(self, account: str = "paper") -> Dict[str, Any]:
        """Read-only IBKR check for the Connections panel. Never places an order."""
        out = self.connections.probe(account)
        if out["ok"] and not self.connections.connected:
            if plan_venue(self.mode, self.paper_platform).account == out["account_type"] \
                    and self._retry_connection(force=True):
                out["note"] += " The app is now connected to it."
        return out

    def setup_state(self) -> Dict[str, Any]:
        """Everything the Connections panel shows. Probes the ports, so it's
        served on demand rather than in every snapshot."""
        sec = self.settings.secrets
        ports = {a: sec.ibkr_port_for(a) for a in ("paper", "live")}
        return {
            "venue": self._venue_state(),
            "fields": secrets_store.describe(),
            "signal_fields": secrets_store.describe(fields=secrets_store.SIGNAL_FIELDS),
            "ibkr": {"host": sec.ibkr_host, "ports": ports,
                     "listening": {a: self.connections.port_open(p) for a, p in ports.items()},
                     "installed": find_spec("ib_async") is not None, "steps": list(IBKR_STEPS)},
        }

    # ------------------------------------------------------------------ #
    #  Background loops                                                  #
    # ------------------------------------------------------------------ #
    def _autopilot_day_active(self) -> bool:
        """Autopilot day-trading an open session -> fast hot-list cycles and a
        tighter account refresh."""
        try:
            return self.autopilot.day_mode_active(clock.is_market_open())
        except Exception:  # noqa: BLE001
            return False

    def _signals_loop(self) -> None:
        if self.settings.config.signals.enabled:
            self.signals.run(self._stop)

    def signals_state(self) -> Dict[str, Any]:
        """The Signals page: the signal service's state, where its news comes from, and whether the
        strategies built on it are switched on."""
        on = {r["key"]: r["enabled"] for r in self.strategy_state()}
        return {**self.signals.state(), "ibkr_news": self.md.attached, "followed": self._signal_watchlist(),
                "strategies": {k: {"enabled": bool(on.get(k)), "title": REGISTRY[k].title}
                               for k in ("insider_buying", "earnings_drift") if k in REGISTRY}}

    def signal_detail(self, symbol: str) -> Dict[str, Any]:
        return self.signals.symbol_state(symbol)

    def check_signals(self) -> Dict[str, Any]:
        return self.signals.check_now()

    def _signal_watchlist(self) -> List[str]:
        """The stocks whose news the signals follow: those held, then the day's hot list."""
        wl = self.scanner.watchlist
        return list(dict.fromkeys([t["symbol"] for t in self._open_trades()] + (wl.hot_symbols() if wl else [])))

    def _scan_loop(self) -> None:
        self._stop.wait(2.0)
        while not self._stop.is_set():
            kind = self._due_scan()
            if kind:
                self._run_scan(kind)
            self._scan_wake.wait(5.0)
            self._scan_wake.clear()

    def _sync_loop(self) -> None:
        steps = ((self._sync_orders, "order sync"), (self._run_exits, "automatic exits"),
                 (self._check_quit_progress, "quit progress check"))
        while not self._stop.is_set():
            for step, what in steps:
                try:
                    step()
                except Exception:  # noqa: BLE001
                    log.exception("%s failed", what)
            self._stop.wait(4.0)

    def _sync_orders(self) -> None:
        if self.executor:
            self.executor.sync_open_orders()

    def _run_exits(self) -> None:
        if self.exit_manager and self.exit_manager.run_once():
            self._refresh_account()
            BUS.publish("account.snapshot", state=self.snapshot())

    def _snapshot_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.connections.refresh()
                self._retry_connection()
                self._watch_gateway()
                if self._refresh_account():
                    self._reconcile_open_trades()
                state = self.snapshot()
                if self._account:
                    self.repo.snapshot_account(self._account, self._venue,
                                               realized_day=state["pnl"].get("realized_today", 0.0))
                BUS.publish("account.snapshot", state=state)
            except Exception:  # noqa: BLE001
                log.exception("snapshot failed")
            self._stop.wait(10.0 if self._autopilot_day_active() else 30.0)

    def _orders_loop(self) -> None:
        """Keeps the dashboard's list of working orders current. It runs on its own, so a
        slow answer from the broker never holds up the automatic exits."""
        self._stop.wait(3.0)
        while not self._stop.is_set():
            try:
                self._refresh_orders()
            except Exception:  # noqa: BLE001
                log.exception("working orders check failed")
            self._stop.wait(ORDERS_POLL_S)

    # ------------------------------------------------------------------ #
    #  Scans                                                             #
    # ------------------------------------------------------------------ #
    def _due_scan(self) -> Optional[str]:
        """full | cycle | fast | None - see scanner/schedule.py."""
        if self.quit_state:
            return None
        with self._scan_lock:
            requested, self._scan_request = self._scan_request, None
        if requested:
            return requested
        mono = time.monotonic()
        if mono < self._scan_retry_at:
            return None
        now = clock.now_ny()
        wl = self.scanner.watchlist
        if schedule.full_scan_due(now, self.scan_settings, wl.session if wl else None, wl is not None):
            return "full"
        if wl is not None and schedule.gap_check_due(now, self.scan_settings, wl.session, self._gappers_session):
            return "gappers"
        if wl is None or not clock.is_market_open(now):
            return None
        if mono - self._last_cycle_at >= self.scan_settings.cycle_minutes * 60:
            return "cycle"
        if self._autopilot_day_active() and mono - self._last_fast_at >= self.settings.config.scanner.fast_cycle_seconds:
            return "fast"
        refresh = self.settings.config.scanner.plays_refresh_seconds
        if refresh and mono - self._last_plays_at >= refresh and self._board_symbols():
            return "plays"
        return None

    def _queue_scan(self, kind: str) -> None:
        with self._scan_lock:
            if self._scan_request != "full":           # a queued full scan covers a cycle too
                self._scan_request = kind
        self._scan_wake.set()

    def _run_scan(self, kind: str) -> None:
        quick = kind == "plays"                         # the quick re-check of the plays on the board
        if not quick:
            self._scan_running = {"kind": kind, "started_at": clock.now_ny().isoformat()}
            BUS.publish("scan.started", kind=kind)
        try:
            if not quick:
                self._refresh_account()
            self.scanner.account = self.sizing_account()
            if not quick:
                self._refresh_regime()
            self.scanner.market = self.regime.context()
            self.scanner.evidence_weights = self.evidence_weights()
            self.scanner.strategy_records = self.strategy_odds()
            if kind == "full":
                result = self.scanner.run_full(self.scan_settings)
            elif kind == "gappers":
                result = self.scanner.run_gappers()
            elif quick:
                result = self.scanner.run_plays(self._board_symbols())
            else:
                result = self.scanner.run_cycle(fast=kind == "fast")
        except NoDataSource as e:
            if quick:
                self._last_plays_at = time.monotonic()
            else:
                self._scan_failed(kind, str(e))
            return
        except Exception as e:  # noqa: BLE001
            if quick:
                log.debug("quick re-check of the plays failed", exc_info=True)
                self._last_plays_at = time.monotonic()
            else:
                log.exception("%s scan failed", kind)
                self._scan_failed(kind, f"The {kind} scan failed: {e}")
            return
        finally:
            if not quick:
                self._scan_running = None

        mono = time.monotonic()
        if quick:
            self._last_plays_at = mono
        else:
            self._scan_retry_at = 0.0
            if kind == "full":
                self._last_cycle_at = float("-inf")     # in the session, a cycle follows straight away
            else:
                self._last_fast_at = mono
                if kind == "cycle":
                    self._last_cycle_at = mono
        if kind == "gappers":                    # it moves names between the lists; the plays are untouched
            wl = self.scanner.watchlist
            self._gappers_session = wl.session if wl else None
            self._last_scans[kind] = result.summary()
            BUS.publish("watchlist.updated", **self.watchlist_state())
            return
        self._size_plays(result.plays)
        # the cycles don't re-check valuation setups, so those stay; a quick re-check isn't a confirmation
        changes = self.board.replace(result.plays, None if kind == "full" else result.symbols,
                                     keep=lambda p: p.kind.value == "FUNDAMENTAL", confirm=not quick)
        self._last_scans[kind] = result.summary()
        if not quick:
            try:
                self.repo.record_scan(result, keep_rejected=self.settings.config.database.record_rejected_plays)
            except Exception:  # noqa: BLE001
                log.exception("could not save the scan")
        self._publish_plays()
        if not quick:
            BUS.publish("watchlist.updated", **self.watchlist_state())
        if not self.quit_state:
            try:
                self.autopilot.consider(self.board.plays)
            except Exception:  # noqa: BLE001
                log.exception("autopilot pass failed")
        self._note_changes(changes)

    def _scan_failed(self, kind: str, reason: str) -> None:
        self._scan_retry_at = time.monotonic() + self.SCAN_RETRY_S
        log.warning("%s scan skipped: %s", kind, reason)
        BUS.publish("scan.failed", kind=kind, reason=reason)

    def request_scan(self, kind: str = "cycle") -> Dict[str, Any]:
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        if kind not in ("full", "cycle", "gappers"):
            return {"ok": False, "reason": "scan must be 'full', 'cycle' or 'gappers'"}
        if kind != "full" and self.scanner.watchlist is None:
            kind = "full"
        self._queue_scan(kind)
        note = {"full": "Full scan queued: every US stock gets ranked and today's hot list and sector buffers are rebuilt.",
                "cycle": "Rescanning the hot list and the next buffer names.",
                "gappers": "Reading the hot list and buffer names' pre-market candles: the stocks gapping on volume "
                           "join the hot list."}[kind]
        running = self._scan_running
        if running:
            note += f" It starts once the {running['kind']} scan that's running finishes."
        return {"ok": True, "kind": kind, "note": note}

    def scan_status(self) -> Dict[str, Any]:
        wl = self.scanner.watchlist
        session = wl.session if wl else None
        cycles = [s for s in (self._last_scans.get("cycle"), self._last_scans.get("fast")) if s]
        return {
            "settings": self.scan_settings.as_dict(),
            "limits": {k: list(v) for k, v in ScanSettings.LIMITS.items()},
            "full_scan_window": [schedule.EARLIEST_FULL_SCAN.strftime("%H:%M"),
                                 schedule.LATEST_FULL_SCAN.strftime("%H:%M")],
            "gap_check_window": [schedule.EARLIEST_GAP_CHECK.strftime("%H:%M"),
                                 schedule.LATEST_GAP_CHECK.strftime("%H:%M")],
            "running": self._scan_running,
            "last_full": self._last_scans.get("full"),
            "last_gappers": self._last_scans.get("gappers"),
            "last_cycle": max(cycles, key=lambda s: s["started_at"]) if cycles else None,
            "watchlist_session": session.isoformat() if session else None,
            "next_full_scan": schedule.next_full_scan_at(clock.now_ny(), self.scan_settings, session).isoformat(),
            "fast": self._autopilot_day_active(),
            "fast_cycle_seconds": self.settings.config.scanner.fast_cycle_seconds,
        }

    def set_scan_settings(self, **changes: Any) -> Dict[str, Any]:
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        try:
            new = self.scan_settings.changed(**changes)
        except (TypeError, ValueError) as e:
            return {"ok": False, "reason": str(e)}
        old = self.scan_settings
        if new == old:
            return {"ok": True, "scan": self.scan_status(), "note": "No change."}
        self.scan_settings = new
        self._save_runtime()
        notes = []
        if new.premarket_time != old.premarket_time:
            notes.append(f"The full scan now runs at {new.premarket_time} ET.")
        if new.gapper_time != old.gapper_time:
            notes.append(f"The pre-open gap check now runs at {new.gapper_time} ET.")
        if new.cycle_minutes != old.cycle_minutes:
            notes.append(f"The hot list and buffers are rescanned every {new.cycle_minutes} minutes.")
        if (new.hot_list_size, new.sector_queue_size) != (old.hot_list_size, old.sector_queue_size):
            notes.append("List sizes apply from the next full scan - Run full scan now rebuilds today's lists.")
        state = self.scan_status()
        BUS.publish("settings.updated", scan=state)
        return {"ok": True, "scan": state, "note": " ".join(notes)}

    def watchlist_state(self) -> Dict[str, Any]:
        return {"watchlist": self.scanner.watchlist_state(), "scan": self.scan_status()}

    def _publish_plays(self) -> None:
        BUS.publish("plays.updated", plays=[self._decorate(p) for p in self.board.ranked()[:self.BOARD_ROWS]])

    #: stocks the quick re-check looks at, best plays first
    PLAYS_REFRESH_MAX = 25
    #: a setup that flickers in and out isn't announced again for this long
    NOTE_QUIET_S = 600.0

    def _board_symbols(self) -> List[str]:
        """The stocks whose plays are still on offer, best first."""
        offered = (p.symbol for p in self.board.ranked() if p.status is PlayStatus.PROPOSED)
        return list(dict.fromkeys(offered))[:self.PLAYS_REFRESH_MAX]

    def _note_changes(self, changes: List[Any]) -> None:
        """While Autopilot is on, tell the dashboard why plays joined or left the board, and
        what Autopilot makes of a new one."""
        if not changes or not self.autopilot.enabled:
            return
        mono, notes = time.monotonic(), []
        for i, c in enumerate(changes):
            p = c.play
            key = (c.kind, p.symbol, p.strategy, p.side.value, p.timeframe.value)
            if mono - self._noted.get(key, float("-inf")) < self.NOTE_QUIET_S:
                continue
            self._noted[key] = mono
            note = {"id": f"note_{time.time_ns()}_{i}", "at": clock.now_ny().isoformat(), "kind": c.kind,
                    "play_id": p.id, "symbol": p.symbol, "side": p.side.value, "strategy": p.strategy,
                    "timeframe": p.timeframe.value, "why": c.why}
            if c.kind == "added":
                try:
                    note["autopilot"] = self.autopilot.verdict(p)
                except Exception:  # noqa: BLE001
                    log.debug("autopilot verdict failed", exc_info=True)
            notes.append(note)
        self._noted = {k: t for k, t in self._noted.items() if mono - t < self.NOTE_QUIET_S}
        if notes:
            BUS.publish("plays.changes", notes=notes)

    def play_chart(self, play_id: str) -> Dict[str, Any]:
        """Candles and the ways out of a play on the board (see engine/chart.py)."""
        p = self.board.get(play_id)
        if p is None:
            return {"ok": False, "reason": "That play is no longer on the board."}
        frame, intraday = None, p.timeframe is Timeframe.INTRADAY
        if intraday and self.md.attached:
            try:
                frame = self.md.intraday([p.symbol], self.scanner.con_ids([p.symbol])).get(p.symbol)
            except Exception:  # noqa: BLE001
                log.debug("chart candles for %s failed", p.symbol, exc_info=True)
        if frame is None or not len(frame):
            frame, intraday = self.md.daily_frame(p.symbol), False
        return chart_payload(p, frame, intraday, self.settings.config.exit_manager)

    # ------------------------------------------------------------------ #
    #  Account, arming, broker vs database                              #
    # ------------------------------------------------------------------ #
    def _refresh_account(self) -> bool:
        try:
            if self._broker:
                self._account = self._broker.get_account()
                self._account_at = time.monotonic()
                return True
        except Exception as e:  # noqa: BLE001
            log.debug("get_account failed: %s", e)
        return False

    def _check_arm(self) -> None:
        if self.mode == "paper":
            self._armed = True                         # paper has no equity floor
            return
        acc = self._account
        floor = self.settings.config.account.min_start_equity
        self._armed = bool(acc and acc.equity >= floor)
        if acc and not self._armed:
            BUS.publish("engine.disarmed",
                        reason=(f"no {acc.base_currency}->USD exchange rate yet" if not acc.usd_per_base
                                else f"equity ${acc.equity:,.0f} < ${floor:,.0f} live floor"))

    def _reconcile_open_trades(self, force: bool = False) -> List[Dict[str, Any]]:
        """Delete OPEN trade records whose position no longer exists at the broker
        that holds it, and report positions of a different size than their records
        add up to (see reconcile.py for when an answer is trusted)."""
        broker, venue, acc = self._broker, self._venue, self._account
        if broker is None or not broker.is_connected or acc is None or self.executor is None:
            return []
        now = time.monotonic()
        # the adapter reconnects by itself (the Gateway's nightly restart): count from whichever came last
        since = max(self._broker_since, float(getattr(broker, "connected_since", 0.0) or 0.0))
        account_age_s, connection_age_s = now - self._account_at, now - since
        mine = [t for t in self._open_trades() if (t.get("broker") or "paper") == venue]
        held = {p.symbol: float(p.quantity) for p in acc.positions if abs(p.quantity) > 1e-9}
        gone = self.position_check.gone(
            venue, mine, held=set(held), busy=self.executor.pending_exit_trade_ids(),
            account_age_s=account_age_s, connection_age_s=connection_age_s, force=force)
        closed, removed = self._settle_gone(gone)
        if closed:
            log.warning("booked %d record(s) closed outside the app from %s's fills: %s", len(closed), venue,
                        ", ".join(f"{c['symbol']} at {c['exit_price']}" for c in closed))
        if removed:
            log.warning("removed %d trade record(s) no longer held at %s: %s", len(removed), venue,
                        ", ".join(r["symbol"] for r in removed))
            BUS.publish("trades.removed", trades=removed, venue=venue, venue_label=venue_label(venue))

        removed_ids = {r["id"] for r in removed} | {c["id"] for c in closed}
        new = self.position_check.share_counts(
            venue, venue_label(venue), [t for t in mine if t["id"] not in removed_ids], held=held,
            in_flight=self.executor.symbols_in_flight(),
            account_age_s=account_age_s, connection_age_s=connection_age_s)
        for m in new:
            log.warning("share counts disagree: %s", m["note"])
        if new:
            BUS.publish("positions.mismatch", mismatches=new)
        return closed + removed

    def _settle_gone(self, gone: List[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Each record whose position is gone: closed at the price the broker's fills say it went
        for, so the journal, the strategy records and the sizing learn its outcome - or, when the
        broker reports no such fill, deleted as before. Returns (closed, removed)."""
        closed: List[Dict[str, Any]] = []
        removed: List[Dict[str, Any]] = []
        for t in gone:
            row = {"id": t["id"], "symbol": t["symbol"], "side": t["side"], "quantity": t["quantity"]}
            fill = self._exit_fill(t)
            out = None
            if fill is not None:
                out = self.repo.close_trade(t["id"], fill["price"], exit_reason="closed-outside",
                                            commission=fill["commission"], exit_time=fill["at"])
            if out:
                if self.executor is not None:
                    self.executor.forget_open(t["symbol"])
                closed.append({**row, "exit_price": fill["price"], "fills": fill["fills"],
                               "realized_pl": out.get("realized_pl")})
                BUS.publish("trade.closed", trade=out, reason="closed outside the app, booked from the broker's fills")
            elif self.repo.delete_trade(t["id"]):
                removed.append(row)
        return closed, removed

    def _exit_fill(self, t: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """What closed a position outside the app, from the broker's executions: the exit-side fills
        of the symbol since the trade was entered, averaged by size. None when the broker reports
        none (IBKR keeps only the current session's)."""
        get = getattr(self._broker, "get_fills", None)
        if not callable(get):
            return None
        try:
            fills = get(t["symbol"]) or []
        except Exception:  # noqa: BLE001
            log.debug("fills for %s unavailable", t["symbol"], exc_info=True)
            return None
        entered = _utc(t.get("entry_time"))
        exit_side = Side.SHORT if t["side"] == "LONG" else Side.LONG
        picked = [f for f in fills if f.side == exit_side and float(f.quantity) > 0
                  and (entered is None or _utc(f.ts) >= entered - dt.timedelta(minutes=1))]
        if not picked:
            return None
        qty = sum(float(f.quantity) for f in picked)
        price = sum(float(f.price) * float(f.quantity) for f in picked) / qty
        return {"price": round(price, 4), "quantity": qty, "fills": len(picked),
                "commission": round(sum(float(f.commission or 0.0) for f in picked), 2),
                "at": max(_utc(f.ts) for f in picked)}

    def untracked_positions(self) -> List[Dict[str, Any]]:
        """Shares the current venue's account holds beyond what its open-trade records cover:
        opened or changed outside the app, or a fill the app couldn't book. Shown so they can be
        exited - the app doesn't manage their exits. Shares an entry order is still working for
        aren't counted (their record follows the fill)."""
        acc = self._account
        if acc is None or self._broker is None or not self._broker.is_connected:
            return []
        recorded: Dict[str, float] = {}
        for t in self._positions_here():
            sign = -1.0 if t.get("side") == "SHORT" else 1.0
            recorded[t["symbol"]] = recorded.get(t["symbol"], 0.0) + sign * abs(float(t.get("quantity") or 0.0))
        working = {w["symbol"] for w in self.working_entries()}
        out: List[Dict[str, Any]] = []
        for p in acc.positions:
            if abs(p.quantity) < 1e-9 or p.symbol in working:
                continue
            extra = p.quantity - recorded.get(p.symbol, 0.0)
            if abs(extra) < 1e-9 or (recorded.get(p.symbol) and (extra > 0) != (p.quantity > 0)):
                continue                 # fewer than recorded is a mismatch, reported by the position check
            mark = p.market_price or None
            out.append({"symbol": p.symbol, "side": "LONG" if extra > 0 else "SHORT", "qty": abs(extra),
                        "held": p.quantity, "recorded": recorded.get(p.symbol, 0.0),
                        "avg_price": round(p.avg_price, 4), "market_price": round(mark, 4) if mark else None,
                        "unrealized_pl": round((mark - p.avg_price) * extra, 2) if mark else None})
        return out

    def close_untracked(self, symbol: str) -> Dict[str, Any]:
        """Exit the shares of ``symbol`` held without a record, at the market - allowed at any time."""
        row = next((r for r in self.untracked_positions() if r["symbol"] == symbol), None)
        if row is None:
            return {"ok": False, "reason": f"{venue_label(self._venue)} shows no {symbol} shares without a record."}
        out = self.executor.close_untracked(symbol, row["side"], row["qty"])
        self._refresh_account()
        BUS.publish("account.snapshot", state=self.snapshot())
        if out.get("ok"):
            status = str(out.get("status") or "")
            tail = "" if status == "FILLED" else f" ({status.lower()})"
            out["note"] = f"Exit sent for {row['qty']:,.0f} {symbol} shares that had no record{tail}."
        return out

    def refresh_account_now(self) -> Dict[str, Any]:
        self._refresh_account()
        self._check_arm()
        if self.executor:
            try:
                self.executor.sync_open_orders()
            except Exception:  # noqa: BLE001
                log.debug("order sync failed", exc_info=True)
        state = self.snapshot()
        BUS.publish("account.snapshot", state=state)
        return {"ok": True, "state": state}

    # ------------------------------------------------------------------ #
    #  Plays and positions                                               #
    # ------------------------------------------------------------------ #
    def assess_play(self, play_id: str) -> Dict[str, Any]:
        p = self.board.get(play_id)
        if p is None:
            return {"ok": False, "reason": "play not found (it may have expired)"}
        self._refresh_account()
        acc = self._account
        if acc is None:
            return {"ok": False, "reason": "no account data"}
        cfg = self.settings.config
        # sized against the trading capital; the PDT rule and the floor see the real account
        sizing = size_play(p, self.sizing_account(p.timeframe) or acc, cfg.risk,
                           symbol_notional=self.exposure_by_symbol().get(p.symbol, 0.0),
                           risk_pct=self.strategy_risk_pct(p.strategy))
        decision = self.pdt.assess(acc, p)
        session = clock.current_session()
        plan = plan_order(p, session, cfg.execution)
        acted_on = p.status in _ACTED_ON

        reasons: List[str] = []
        locked = self._locked()
        if locked:
            reasons.append(locked)
        if acted_on:
            reasons.append(f"already {p.status.value.lower()}" + (f" - trade {p.trade_id}" if p.trade_id else ""))
        if not plan.get("executable", False):
            reasons.append(plan.get("reason", "not executable in this session"))
        if not self._armed:
            reasons.append(f"engine not armed - live equity below ${cfg.account.min_start_equity:,.0f} floor")
        if not decision.allowed:
            reasons.append(decision.reason)
        if not acc.usd_per_base:
            reasons.append(f"no {acc.base_currency}->USD exchange rate yet, so the trade can't be sized")
        elif p.suggested_qty <= 0:
            reasons.append(f"{p.symbol} already takes up the {cfg.risk.max_symbol_pct_of_equity:.0f}% of equity "
                           "allowed in one stock" if "max exposure per stock" in sizing.caps_hit
                           else "position size rounds to zero for this risk budget")
        if p.reward_risk < cfg.risk.min_reward_risk and p.kind.value != "FUNDAMENTAL":
            reasons.append(f"reward:risk {p.reward_risk:.1f} below minimum")
        filtered = self.filters.refusal(p.side.value, p.timeframe.value, p.sector)
        if filtered:
            reasons.append(filtered)

        hold_unit = "min" if p.timeframe is Timeframe.INTRADAY else "trading days"
        return {
            "ok": True, "can_execute": not reasons, "already_executed": acted_on,
            "reasons": reasons, "mode": self.mode, "session": session.value,
            "noise": [NOISE_LABELS.get(n, n) for n in p.noise],
            "play": self._decorate(p), "pdt": decision.as_dict(), "order_plan": plan,
            "order_preview": {
                "side": p.side.entry_action, "qty": p.suggested_qty, "order_type": plan.get("order_type"),
                "session_label": plan.get("session_label"), "limit_price": plan.get("limit_price"),
                "stop_price": plan.get("stop_price"), "tif": plan.get("tif", "DAY"),
                "bracket_mode": plan.get("bracket_mode"), "take_profit": p.primary_target, "stop_loss": p.stop,
                "exit_manager": bool(cfg.exit_manager.enabled),
                "expected_hold": (f"~{p.expected_hold_typical:.0f} {hold_unit} "
                                  f"(review after {p.expected_hold_max:.0f})"),
                "est_cost": round(p.notional, 2), "est_risk": round(p.dollar_risk, 2),
                "note": plan.get("note", ""), "routes_to": ROUTE_LABELS.get(self._venue, self._venue.upper()),
            },
        }

    def approve_play(self, play_id: str, operator: str = "operator") -> Dict[str, Any]:
        with self._switch_lock:
            locked = self._locked()
            if locked:
                return {"ok": False, "reason": locked}
            p = self.board.get(play_id)
            if p is None:
                return {"ok": False, "reason": "play not found (it may have expired)"}
            if p.status in _ACTED_ON:
                return {"ok": False, "already_executed": True, "trade_id": p.trade_id,
                        "reason": f"already {p.status.value.lower()}" + (f" - trade {p.trade_id}" if p.trade_id else "")}
            pre = self.assess_play(play_id)
            if not pre.get("ok"):
                return pre
            if not pre["can_execute"]:
                return {"ok": False, "reason": "; ".join(pre["reasons"]) or "not executable"}

            p.status = PlayStatus.ACCEPTED
            p.evidence["at_entry"] = self._entry_context(p, operator)
            self.repo.record_play(p)
            self.repo.set_play_status(p.id, p.status.value, operator)
            try:
                out = self.executor.execute_play(p, self._account, plan=pre["order_plan"])
            except Exception as e:  # noqa: BLE001
                p.status = PlayStatus.ERROR
                self.repo.set_play_status(p.id, p.status.value, operator)
                log.exception("execute_play crashed")
                BUS.publish("play.decided", play_id=p.id, decision="error", result={"reason": str(e)})
                return {"ok": False, "reason": f"execution error: {e}"}
            if not out.get("ok"):
                p.status = PlayStatus.PROPOSED            # let them try again once the reason clears
            self.repo.set_play_status(p.id, p.status.value, operator)
            BUS.publish("play.decided", play_id=p.id, decision="approved", result=out, play=self._decorate(p))
            self._refresh_account()
            return {"ok": out.get("ok", False), **out}

    def reject_play(self, play_id: str, operator: str = "operator") -> Dict[str, Any]:
        p = self.board.get(play_id)
        if p:
            p.status = PlayStatus.REJECTED
        self.repo.set_play_status(play_id, PlayStatus.REJECTED.value, operator)
        BUS.publish("play.decided", play_id=play_id, decision="rejected")
        return {"ok": True}

    def close_position(self, trade_id: str, reason: str = "manual") -> Dict[str, Any]:
        """Exit one position at the market - always allowed, including while quitting."""
        out = self.executor.close_trade(trade_id, reason=reason)
        self._refresh_account()
        BUS.publish("account.snapshot", state=self.snapshot())
        return out

    def close_all_positions(self, reason: str = "manual-all") -> Dict[str, Any]:
        held = self._positions_here()
        if not held:
            return {"ok": True, "note": "No open positions to exit.", "results": []}
        results = self._close_all(held, reason=reason)
        failed = [r for r in results if not r["ok"]]
        note = (f"Exit sent for {len(held) - len(failed)} of {len(held)} position(s)."
                + (f" Not sent: {', '.join(r['symbol'] for r in failed)}." if failed else ""))
        return {"ok": not failed, "note": note, "results": results}

    def _close_all(self, trades: List[Dict[str, Any]], reason: str) -> List[Dict[str, Any]]:
        """Send every close at once - the broker calls are independent, so a thread
        per position turns N round-trips into about one."""
        if not trades:
            return []

        def close(t: Dict[str, Any]) -> Dict[str, Any]:
            try:
                out = self.executor.close_trade(t["id"], reason=reason)
            except Exception as e:  # noqa: BLE001
                out = {"ok": False, "reason": str(e)}
            return {"trade_id": t["id"], "symbol": t["symbol"], "ok": bool(out.get("ok")),
                    "status": out.get("status"), "reason": out.get("reason", "")}

        with ThreadPoolExecutor(max_workers=min(8, len(trades))) as pool:
            results = list(pool.map(close, trades))
        self._refresh_account()
        BUS.publish("account.snapshot", state=self.snapshot())
        return results

    def set_trade_managed(self, trade_id: str, on: bool) -> Dict[str, Any]:
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        self.repo.update_trade_risk(trade_id, managed_exit=bool(on))
        return {"ok": True, "trade_id": trade_id, "managed_exit": bool(on)}

    def trade_record(self, trade_id: str) -> Optional[Dict[str, Any]]:
        """The stored record of one trade, plus what the broker holds for it now."""
        rec = self.repo.trade_record(trade_id)
        if rec is None:
            return None
        t = rec["trade"]
        venue = t.get("broker") or "paper"
        rec["venue_label"] = venue_label(venue)
        rec["on_current_venue"] = venue == self._venue
        rec["broker_position"] = None
        if t["status"] == "OPEN" and rec["on_current_venue"] and self._account is not None:
            pos = self._account.position(t["symbol"])
            if pos is not None:
                rec["broker_position"] = {"quantity": pos.quantity, "market_price": round(pos.market_price, 4),
                                          "unrealized_pl": round(pos.unrealized_pl, 2)}
        return rec

    def set_autopilot(self, **kw: Any) -> Dict[str, Any]:
        """Toggle / tune hands-off entry. Exits are automatic either way."""
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        want_on = kw.get("enabled")
        st = self.autopilot.configure(**kw)
        note = ""
        if want_on and self.mode == "live" and not st["allow_live"]:
            note = ("Autopilot will NOT place live orders: set  autopilot.allow_live: true  in "
                    "config/config.yaml first. It is armed for paper only.")
        elif want_on and st["effective"]:
            types = ", ".join(t.lower() for t in st["trade_types"])
            note = (f"Autopilot ON ({types}). It will enter up to {st['max_auto_positions']} positions / "
                    f"{st['max_auto_trades_per_day']} per day at >= {st['min_reward_risk']:.0f}:1 and "
                    f">= {st['min_confidence']:.2f} confidence. Exits stay automatic.")
        elif want_on is False:
            note = "Autopilot OFF - back to click-to-enter. Open trades keep their automatic exits."
        return {"ok": True, "autopilot": st, "note": note}

    def reset_paper(self, cash: Optional[float] = None) -> Dict[str, Any]:
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        if self._venue != "paper":
            return {"ok": False, "reason": f"You're trading on {venue_label(self._venue)} - "
                                           "its balance and positions are kept by the broker."}
        amount = float(cash) if cash is not None else self.settings.config.account.paper_start_cash
        removed = self._reset_simulator(amount)
        dropped = self.capital.get("paper", 0.0) > amount
        if dropped:
            self.capital.pop("paper")
            self._save_runtime()
        BUS.publish("account.snapshot", state=self.snapshot())
        note = f"Paper account reset to ${amount:,.0f}."
        if removed:
            note += f" Removed {len(removed)} open trade record(s) whose positions were wiped."
        if dropped:
            note += " Your trading capital was more than the new balance, so the bot uses the whole account again."
        return {"ok": True, "cash": round(amount, 2), "removed": removed, "note": note}

    def _reset_simulator(self, amount: float) -> List[Dict[str, Any]]:
        sim = self.connections.simulator()
        if self.executor is not None:
            self.executor.cancel_pending_entries()
        sim.reset(amount)  # type: ignore[attr-defined]
        self.executor.rebind(sim, venue="paper")
        self._refresh_account()
        # the simulator's positions are gone, so their OPEN trade records go too
        return self._reconcile_open_trades(force=True)

    # ------------------------------------------------------------------ #
    #  Filters and strategies (apply at once: board, scanner, execution) #
    # ------------------------------------------------------------------ #
    def set_filters(self, sides: Optional[List[str]] = None, timeframes: Optional[List[str]] = None,
                    sectors: Optional[List[str]] = None) -> Dict[str, Any]:
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        cur = self.filters
        try:
            new = TradeFilters.build(cur.sides if sides is None else sides,
                                     cur.timeframes if timeframes is None else timeframes,
                                     cur.sectors if sectors is None else sectors)
        except ValueError as e:
            return {"ok": False, "reason": str(e)}
        if new == cur:
            return {"ok": True, "filters": new.as_dict(), "note": "No change.", "rescanning": False}
        self.filters = self.scanner.filters = new
        self._save_runtime()
        dropped = self.board.drop(new.allows, "the Long/Short, timeframe or sector filters no longer allow it")
        removed = len(dropped)
        self._note_changes(dropped)
        if set(new.timeframes) != set(cur.timeframes):             # the day/swing split turns on or off with them
            self._size_plays([p for p in self.board.plays.values() if p.status not in _ACTED_ON])
            BUS.publish("capital.updated", capital=self.capital_state())
        self._publish_plays()
        BUS.publish("filters.updated", filters=new.as_dict())
        # narrowing just trims the board; widening needs a scan to find the new plays
        widened = bool(set(new.sides) - set(cur.sides) or set(new.timeframes) - set(cur.timeframes)
                       or (cur.sectors and (not new.sectors or set(new.sectors) - set(cur.sectors))))
        if widened:
            self._queue_scan("full")
        return {"ok": True, "filters": new.as_dict(), "removed_plays": removed, "rescanning": widened,
                "note": new.describe()}

    def strategy_state(self) -> List[Dict[str, Any]]:
        return strategy_catalog(self.settings, self.strategy_overrides)

    def set_strategy(self, key: str, enabled: Optional[bool] = None, weight: Optional[float] = None) -> Dict[str, Any]:
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        if key not in REGISTRY:
            return {"ok": False, "reason": f"unknown strategy '{key}'"}
        override = dict(self.strategy_overrides.get(key, {}))
        if enabled is not None:
            override["enabled"] = bool(enabled)
        if weight is not None:
            try:
                w = float(weight)
            except (TypeError, ValueError):
                return {"ok": False, "reason": "weight must be a number"}
            lo, hi = self.WEIGHT_RANGE
            if not lo <= w <= hi:
                return {"ok": False, "reason": f"weight must be between {lo} and {hi}"}
            override["weight"] = round(w, 2)
        default = next(r for r in strategy_catalog(self.settings) if r["key"] == key)
        if override.get("enabled") == default["default_enabled"]:
            override.pop("enabled")
        if "weight" in override and abs(override["weight"] - default["default_weight"]) < 1e-9:
            override.pop("weight")
        overrides = {k: v for k, v in self.strategy_overrides.items() if k != key}
        if override:
            overrides[key] = override
        rescan = weight is not None or bool(enabled)
        intraday_only = REGISTRY[key].timeframe is Timeframe.INTRADAY and self.scanner.watchlist is not None
        self._apply_strategies(overrides, rescan=("cycle" if intraday_only else "full") if rescan else None)
        row = next(r for r in self.strategy_state() if r["key"] == key)
        return {"ok": True, "strategies": self.strategy_state(), "rescanning": rescan,
                "note": f"{row['title']}: {'on' if row['enabled'] else 'off'}, weight {row['weight']:g}."}

    def reset_strategies(self) -> Dict[str, Any]:
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        self._apply_strategies({}, rescan="full")
        return {"ok": True, "strategies": self.strategy_state(), "note": "Strategies reset to config.yaml.",
                "rescanning": True}

    def _apply_strategies(self, overrides: Dict[str, Dict[str, Any]], rescan: Optional[str]) -> None:
        self.strategy_overrides = overrides
        self.scanner.set_strategies(build_strategies(self.settings, overrides))
        self._save_runtime()
        active = {s.key for s in self.scanner.strategies}
        self._note_changes(self.board.drop(lambda p: p.strategy in active, "its strategy was switched off"))
        self._publish_plays()
        BUS.publish("strategies.updated", strategies=self.strategy_state())
        if rescan:
            self._queue_scan(rescan)

    # ------------------------------------------------------------------ #
    #  Trading capital                                                   #
    # ------------------------------------------------------------------ #
    def _invested_usd(self) -> float:
        return capital.invested_usd(self._account, self._positions_here())

    def sizing_account(self, timeframe: Any = None) -> Optional[Account]:
        """The account as position sizing sees it - see capital.py. With a ``timeframe`` (a day trade or a
        swing trade), only that kind's share of the trading capital is open to it."""
        acc, limit = self._account, self.capital.get(self._venue)
        share = 1.0 if timeframe is None else capital.share_of(capital.kind_of(timeframe), self.effective_day_pct())
        if acc is None or (not limit and share >= 1.0):
            return acc
        trades = self._positions_here()
        held = capital.invested_by_kind(acc, trades)
        return capital.sizing_account(acc, limit, sum(held.values()), share=share,
                                      invested_in_kind=held[capital.kind_of(timeframe)] if timeframe is not None else 0.0)

    def capital_state(self) -> Optional[Dict[str, Any]]:
        if self._account is None:
            return None
        state = capital.state(self._account, self._venue, venue_label(self._venue), self.capital.get(self._venue),
                              self._invested_usd(), self.effective_day_pct(),
                              capital.invested_by_kind(self._account, self._positions_here()))
        state["split"].update(on=self._both_kinds(), set_pct=self.day_trade_pct)
        return state

    def _both_kinds(self) -> bool:
        return {"INTRADAY", "SWING"} <= set(self.filters.timeframes)

    def effective_day_pct(self) -> float:
        """The day-trade share in force: the split while the filters allow day trades and swing trades
        both, otherwise all of the trading capital for the one kind that's switched on."""
        if self._both_kinds():
            return self.day_trade_pct
        return 100.0 if "INTRADAY" in self.filters.timeframes else 0.0

    def set_capital_split(self, day_pct: Any) -> Dict[str, Any]:
        """The part of the trading capital day trades may hold at once, in percent; swing trades get the rest."""
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        try:
            value = capital.parse_day_pct(day_pct)
        except ValueError as e:
            return {"ok": False, "reason": str(e)}
        self.day_trade_pct = value
        self._save_runtime()
        self._resize_plays()
        state = self.capital_state()
        BUS.publish("capital.updated", capital=state)
        note = f"Day trades may now hold up to {value:g}% of the trading capital at once, swing trades {100 - value:g}%."
        if not self._both_kinds():
            note += " It applies while Intraday and Swing are both switched on."
        return {"ok": True, "capital": state, "note": note}

    def set_capital(self, amount: Any = None) -> Dict[str, Any]:
        """How much of the account on the current platform the bot may use, in the
        account's own currency. Empty = the whole account; never more than it holds."""
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        venue, label = self._venue, venue_label(self._venue)
        if amount is None or amount == "":
            self.capital.pop(venue, None)
            note = f"The bot can use all of {label} again."
        else:
            try:
                value = capital.parse_amount(amount)
            except ValueError as e:
                return {"ok": False, "reason": str(e)}
            if not self._refresh_account() or self._account is None:
                return {"ok": False, "reason": f"Couldn't read your account on {label}, so the amount can't be "
                                               "checked. Try again once it's connected."}
            try:
                worth = capital.check_fits(value, self._account, label)
            except ValueError as e:
                return {"ok": False, "reason": str(e)}
            self.capital[venue] = value
            currency = worth["currency"]
            note = (f"The bot will use {capital.money(value, currency)} of the "
                    f"{capital.money(worth['equity'], currency)} in {label}. Position sizes now use this amount.")
        self._save_runtime()
        self._resize_plays()
        state = self.capital_state()
        BUS.publish("capital.updated", capital=state)
        return {"ok": True, "capital": state, "note": note}

    def _resize_plays(self) -> None:
        self._size_plays([p for p in self.board.plays.values() if p.status not in _ACTED_ON])
        self._publish_plays()

    def _size_plays(self, plays: Iterable[Play]) -> None:
        """Suggested sizes for plays on the board, each against its own share of the trading capital."""
        accounts = {kind: self.sizing_account(kind) for kind in (capital.DAY, capital.SWING)}
        if accounts[capital.DAY] is None:
            return
        exposure = self.exposure_by_symbol()
        for p in plays:
            size_play(p, accounts[capital.kind_of(p.timeframe)], self.settings.config.risk,
                      symbol_notional=exposure.get(p.symbol, 0.0), risk_pct=self.strategy_risk_pct(p.strategy))

    # ------------------------------------------------------------------ #
    #  Quitting                                                          #
    # ------------------------------------------------------------------ #
    def quit_preview(self) -> Dict[str, Any]:
        brief = [{"id": t["id"], "symbol": t["symbol"], "side": t["side"], "quantity": t["quantity"],
                  "entry_price": t["entry_price"], "venue": t.get("broker") or "paper"}
                 for t in self._open_trades()]
        here = [b for b in brief if b["venue"] == self._venue]
        return {
            "mode": self.mode, "paper": self.mode == "paper",
            "venue": self._venue, "venue_label": venue_label(self._venue),
            "positions": here, "left": len(here), "parked": [b for b in brief if b["venue"] != self._venue],
            "resets_simulator": self._venue == "paper",
            "reset_cash": self.settings.config.account.paper_start_cash,
            "quitting": bool(self.quit_state),
        }

    def request_quit_dialog(self) -> None:
        """Ask the dashboard to show the quit choices (Ctrl+C with live positions open)."""
        BUS.publish("quit.requested", **self.quit_preview())

    def begin_quit(self, close_all: bool = True, operator: str = "operator") -> Dict[str, Any]:
        """Paper: close everything, reset the simulator, shut down. Live: close
        everything and shut down once flat (``close_all=False`` cancels). Until the
        last position is out, nothing but exits may change."""
        with self._switch_lock:
            if self.quit_state:
                return {"ok": True, "note": "Already closing out before quitting.", "quit": self._quit_status()}
            held = self._positions_here()
            if self.mode == "live" and held and not close_all:
                return {"ok": False, "reason": "Quit cancelled - your live positions stay open and managed."}
            self.quit_state = {"started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "mode": self.mode,
                               "venue": self._venue, "by": operator, "reset_sim": self._venue == "paper"}
            self._quit_rounds = 1
            self._save_runtime()
            cancelled = self.executor.cancel_pending_entries() if self.executor else 0
            log.warning("quit by %s: closing %d position(s) on %s, cancelled %d working entr%s",
                        operator, len(held), self._venue, cancelled, "y" if cancelled == 1 else "ies")
            BUS.publish("quit.started", quit=self._quit_status())
            results = self._close_all(held, reason="quit")
            self._quit_retry_at = time.monotonic() + self.QUIT_RETRY_S
        self._check_quit_progress()
        failed = [r for r in results if not r["ok"]]
        if not held:
            note = "No open positions - shutting down."
        elif failed:
            note = (f"Exits sent for {len(held) - len(failed)} of {len(held)} positions; retrying "
                    f"{', '.join(r['symbol'] for r in failed)}. The app stays locked until all are out.")
        else:
            note = f"Exit sent for {len(held)} position(s). Shutting down once they've all closed."
        return {"ok": True, "note": note, "results": results, "quit": self._quit_status()}

    def _quit_status(self) -> Optional[Dict[str, Any]]:
        if not self.quit_state:
            return None
        left = self._positions_here()
        return {**self.quit_state, "left": len(left), "symbols": sorted({t["symbol"] for t in left})}

    def _check_quit_progress(self) -> None:
        if not self.quit_state:
            return
        with self._quit_lock:
            if not self.quit_state:
                return
            left = self._positions_here()
            if left and time.monotonic() >= self._quit_retry_at:
                # a simulator close can only fail on a missing price; after a retry the
                # reset wipes those positions anyway, so don't stay stuck
                if self.quit_state.get("reset_sim") and self._quit_rounds >= 2:
                    left = []
                else:
                    busy = self.executor.pending_exit_trade_ids()
                    retry = [t for t in left if t["id"] not in busy]
                    if retry:
                        self._close_all(retry, reason="quit")
                        self._quit_rounds += 1
                    self._quit_retry_at = time.monotonic() + self.QUIT_RETRY_S
                    left = self._positions_here()
            if left:
                BUS.publish("quit.progress", quit=self._quit_status())
                return

            state, self.quit_state = self.quit_state, None
            note = "All positions are closed."
            if state.get("reset_sim") and self._venue == "paper":
                cash = self.settings.config.account.paper_start_cash
                self._reset_simulator(cash)
                self.board.clear()
                note += f" Paper account reset to ${cash:,.0f}."
            self._save_runtime()
        log.warning("quit finished: %s", note)
        BUS.publish("quit.done", note=note)
        if self.on_shutdown is not None:
            threading.Timer(1.5, self.on_shutdown).start()      # let the message reach the browser

    # ------------------------------------------------------------------ #
    #  Views                                                             #
    # ------------------------------------------------------------------ #
    def _decorate(self, p: Play) -> Dict[str, Any]:
        row = p.to_row()
        row["executable_hint"] = p.suggested_qty > 0 and self._armed
        return self.autopilot.decorate_play(row)

    def _venue_state(self) -> Dict[str, Any]:
        plan = plan_venue(self.mode, self.paper_platform)
        status = self.connections.session_status()
        connected = self.connections.connected
        return {
            "mode": self.mode, "paper_platform": self.paper_platform, "paper_platforms": PAPER_PLATFORMS,
            "trading_on": self._venue, "trading_on_label": venue_label(self._venue),
            "wants": {"account": plan.account, "trade": plan.trade},
            "connected": connected, "blockers": list(self.connections.blockers),
            "live_blockers": list(self._live_blockers), "ibkr_session": status,
            "market_data": (status or {}).get("market_data", "none") if connected else "none",
        }

    def _pnl(self) -> Dict[str, Any]:
        try:
            return self.repo.pnl_summary()
        except Exception:  # noqa: BLE001
            return {}

    def snapshot(self) -> Dict[str, Any]:
        acc = self._account
        cfg = self.settings.config
        return {
            "ts": clock.now_ny().isoformat(),
            "mode": self.mode,
            "connected": bool(self._broker and self._broker.is_connected),
            "armed": self._armed,
            "market_open": clock.is_market_open(),
            "market": clock.market_status(),
            "regime": self.regime.reading(),
            "exit_manager": views.exit_rules(cfg.exit_manager),
            "autopilot": self.autopilot.status(),
            "data": views.data_feed(self.md),
            "venue": self._venue_state(),
            "connection": views.connection_pill(self.mode, self.paper_platform, self.connections),
            "filters": self.filters.as_dict(),
            "strategies_on": len(self.scanner.strategies),
            "quit": self._quit_status(),
            "capital": self.capital_state(),
            "account": views.account(acc, cfg.account, paper=self.mode == "paper") if acc else None,
            "positions": views.positions(acc),
            "untracked": self.untracked_positions(),
            "mismatches": self.position_check.mismatches,
            "day_trades_5d": self.repo.count_day_trades(5),
            "day_trade_limit": cfg.account.max_day_trades_under_threshold,
            "pnl": self._pnl(),
            "scan": self.scan_status(),
        }

    def current_plays(self) -> List[Dict[str, Any]]:
        return [self._decorate(p) for p in self.board.ranked()]


def _utc(value: Any) -> Optional[dt.datetime]:
    """An aware UTC datetime from an ISO string or a datetime (naive = UTC); None when there isn't one."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, dt.datetime):
        return None
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)


def _order_signature(orders: List[Dict[str, Any]]) -> tuple:
    """What has to change for the dashboard to be told about the working orders."""
    keys = ("order_id", "status", "filled", "remaining", "limit_price", "stop_price", "purpose", "trade_id")
    return tuple(sorted((tuple(o.get(k) for k in keys) for o in orders), key=lambda row: str(row[0])))


def _attempt(action: Callable[..., Dict[str, Any]], *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Run an operator action; an unexpected error comes back as a reason instead of escaping."""
    try:
        return action(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        log.exception("%s failed", getattr(action, "__name__", "action"))
        return {"ok": False, "reason": f"It didn't go through: {e}"}


def _movers_built(review: Optional[Mapping[str, Any]]) -> bool:
    return bool(((review or {}).get("movers") or {}).get("ok"))


def _duration(seconds: float) -> str:
    """A span of time in words: "45 s", "3 min", "1 h 5 min"."""
    s = int(max(0.0, seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h {(s % 3600) // 60} min"
