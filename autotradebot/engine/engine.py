"""The conductor.

Holds the IB Gateway connection and the built-in simulator (connections.py),
the scanner, order execution and the automatic exits, and runs three
background loops:

    scan loop      the daily full scan before the open, the intraday cycles over
                   the hot list and sector buffers, fast hot-list cycles while
                   Autopilot is day-trading (see scanner/schedule.py), and the
                   candle-close check on the watch tier seconds after each
                   5-minute close
    sync loop      every few seconds: fills, automatic exits, quit progress - and in
                   regular hours the exits again within a second of a streamed
                   tick on a stock held
    snapshot loop  every 10-30 s: account, broker-vs-database check, broadcast
    stream loop    points IBKR's real-time streams at the positions, then the plays,
                   then the watch tier (the day's hot list and buffer names)
    price push     the streamed prices that moved, to the dashboard at most once a
                   second (prices.tick)
    candle loop    a moment after each minute: closes the live 1- and 5-minute
                   candles built from the streamed ticks (data/candles.py), and
                   queues the candle-close check or the early movers for the scan
                   loop
    live scan      every minute in regular hours: IBKR's % gainers, % losers and
                   hot-by-volume scans put the movers the morning's ranking missed
                   into the watch tier

Whatever the user changes on the dashboard applies straight away, is
remembered in data/runtime.json (runtime.py) and is broadcast to every tab.

Safety rules: no order without :meth:`approve_play` (or Autopilot, inside its
caps); no venue change while positions are open on the current one; while
quitting with positions open, nothing but exits may change; and an OPEN trade
record is deleted only when a connected broker confirms the position is gone
(reconcile.py).
"""


import datetime as dt
import itertools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from .. import secrets_store
from ..brokers import get_broker
from ..brokers.base import BrokerAdapter
from ..brokers.venues import IBKR_STEPS, PAPER_PLATFORMS, ROUTE_LABELS, normalize_platform, plan_venue, venue_id, venue_label
from ..config import DATA_DIR, RUNTIME_PATH, Secrets, Settings, get_settings
from ..core.enums import PlayStatus, Side, Timeframe
from ..core.eventbus import BUS
from ..core.models import Account, Play
from ..data.bars import DailyBarStore
from ..data.candles import Candle
from ..data.fundamentals import FundamentalsProvider
from ..data.listings import UsListings
from ..data.market_data import INTRADAY_BAR, MarketData, NoDataSource
from ..data.sec_edgar import SecEdgarFundamentals
from ..data.sectors import sector_allowed
from ..data.symbols import SymbolMaster
from ..execution.autopilot import AutoPilot
from ..execution.executor import Executor
from ..execution.exit_manager import ExitManager, scale_out_plan
from ..execution.order_builder import plan_order
from ..execution.protective_stops import TAG as STOP_TAG, TARGET_TAG, stop_exit_reason
from ..indicators import ta
from ..persistence.db import init_db
from ..persistence.repository import Repository
from ..risk.pdt_guard import PdtGuard
from ..risk.position_sizing import liquidity_cap, size_play
from ..research.features import play_features
from ..research.model import Scorer, risk_factor
from ..research.history import IntradayHistory
from ..research.runner import ReplayRunner
from ..research.journal import Journal
from ..signals.earnings import EarningsCalendar
from ..pairs.desk import PairDesk
from ..signals.book import BoostSettings, SignalBook
from ..signals.service import SignalService
from ..signals.store import SignalStore
from .chart import (INTRADAY_MAX_SESSIONS, TRADE_CANDLES_TTL_S, candle_request, chart_payload,
                    since_session_before, trade_chart_payload, trade_sessions)
from .market_regime import MarketRegime
from ..scanner.noise import LABELS as NOISE_LABELS
from ..scanner import schedule
from ..scanner.evaluator import prev_close_known
from ..scanner.filters import TradeFilters
from ..scanner.scanner import Scanner, ScanResult
from ..scanner.schedule import ScanSettings
from ..strategies.registry import REGISTRY, build_strategies, strategy_catalog
from ..util import clock
from ..util.logging_setup import setup_logging
from ..util.net import port_is_open
from . import capital, views
from .board import PlayBoard
from .day_state import DayStateOps
from .research_ops import ResearchOps
from .journal_ops import JournalOps
from .pairs_ops import PairsOps
from .capital_ops import CapitalOps
from .quit_ops import QuitOps
from .connections import Connections
from .reconcile import PositionCheck
from .runtime import (RuntimeFile, load_capital, load_capital_mode, load_day_trade_pct, load_filters,
                      load_position_pct, load_size_factor, load_strategy_overrides)

from .support import _ACTED_ON, duration
log = logging.getLogger(__name__)

#: how often the orders working at the broker are re-checked for the dashboard
ORDERS_POLL_S = 5.0
#: how old that list may be before a dashboard request asks the broker again
ORDERS_MAX_AGE_S = 8.0
#: the session a price was traded in, as a stock's price line says it
SESSION_WORDS = {clock.Session.PRE: "pre-market", clock.Session.REGULAR: "regular",
                 clock.Session.POST: "after-hours", clock.Session.CLOSED: "closed"}


def tick_exit_wait(now: float, last_exit: float, deadline: float, gap: float) -> Tuple[float, bool]:
    """Once a streamed tick wakes the sync loop: how long it waits, then whether an exits-only pass follows. That
    comes ``gap`` after the last exit pass at the soonest, and not at all when the full pass is due by then - the
    full pass reads every price afresh anyway, and keeps its turn (``deadline``)."""
    ready = max(now, last_exit + gap)
    if ready >= deadline:
        return max(0.0, deadline - now), False
    return ready - now, True


class TradingEngine(ResearchOps, JournalOps, PairsOps, CapitalOps, QuitOps, DayStateOps):
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
        self._runtime_lock = threading.Lock()      # the scan, sync and web threads all save it
        saved = self.runtime.read()

        years = max(1, min(5, int(self.settings.config.replay.daily_years or 1)))
        self.md = MarketData(DailyBarStore(data_dir / "bars"),
                             deep=DailyBarStore(data_dir / "research" / "daily", keep_sessions=years * 253 + 10,
                                                full_history=f"{years} Y") if years > 1 else None)
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
        #: with no amount set, what the whole account means, per venue: margin (the default) or cash only
        self.capital_mode = load_capital_mode(saved.get("capital_mode"))
        #: the part of the trading capital day trades may hold; swing trades get the rest (engine/capital.py)
        self.day_trade_pct = load_day_trade_pct(saved.get("capital_split"), cfg.account.day_trade_pct)
        #: every position is the usual size times this, 0-5 (the slider over the plays; CapitalOps.set_size_factor)
        self.size_factor = load_size_factor(saved.get("sizing"))
        #: the most one position may hold, % of the account's value, when set on the dashboard (the slider beside the
        #: size factor) - it then stands in for config.yaml's risk.max_position_pct_of_equity; None leaves that be
        self.position_pct = load_position_pct(saved.get("sizing"))
        if self.position_pct is not None:
            cfg.risk.max_position_pct_of_equity = self.position_pct
        sc = cfg.scanner
        self.scan_settings = ScanSettings.load(saved.get("scan"), ScanSettings(
            premarket_time=sc.premarket_time, gapper_time=sc.gapper_time, cycle_minutes=sc.cycle_minutes,
            hot_list_size=sc.hot_list_size, sector_queue_size=sc.sector_queue_size,
            wide_minutes=sc.wide_minutes, wide_stocks=sc.wide_stocks,
            movers=sc.movers, yesterday_movers=sc.yesterday_movers))

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
        self._init_day_state(data_dir / "day_state.bin")     # the board and the scans' state outlive a restart
        self.position_check = PositionCheck()
        self.replay = ReplayRunner(data_dir / "research" / "replay.json",
                                   IntradayHistory(data_dir / "research" / "intraday"),
                                   workers=self.settings.config.replay.workers or None,
                                   sink=self._keep_sim_trades)
        self.model = Scorer(data_dir / "research" / "models")      # the meta-label model, in shadow until it earns it
        self._training = threading.Lock()
        #: calm or turbulent, from SPY's daily returns (Hamilton's Markov switching model)
        self.regime = MarketRegime(data_dir / "research" / "benchmark_spy.pkl")
        #: when companies reported earnings (SEC 8-K item 2.02), for the replay
        self.earnings = EarningsCalendar(data_dir / "signals" / "earnings", lambda url: self.signals.sec.json(url),
                                         self.signals.company_ciks)
        #: the daily review (research/journal.py)
        self.journal = Journal(data_dir / "journal", self.repo)
        self._journal_checked: Optional[dt.date] = None
        self._movers_retry_at = 0.0
        self._review_lock = threading.Lock()       # the journal loop and the Rebuild button build one at a time
        self._earnings_warned: set = set()
        self._live_stats: Dict[str, Dict[str, Any]] = {}
        self._live_stats_at = float("-inf")
        self._risk_pct: Dict[str, Optional[float]] = {}
        self._risk_pct_for: Optional[tuple] = None
        self._practice: set = set()                        # the strategies sized at practice size
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
        self._account_warned_at = float("-inf")     # the last time a failing account read was logged
        # set to have the snapshot loop read the account now rather than at its next turn - after an order
        # sent from the dashboard, whose reply doesn't wait for that read
        self._snapshot_wake = threading.Event()
        # set to have the stream loop point the streams at what changed now - a new position, a new board
        self._stream_wake = threading.Event()
        # the stocks of the plays Autopilot would take, as of the last board push: they stream ahead of the rest
        self._ap_candidates: Tuple[str, ...] = ()
        # a streamed tick on a stock the exits manage: the stocks, and the sync loop's wake (_on_stream_ticks)
        self._exit_wake = threading.Event()
        self._ticked: set = set()
        self._ticked_lock = threading.Lock()
        self._tick_exits_on = False                 # regular hours - each full pass of the sync loop sets it
        self.md.streams.add_listener(self._on_stream_ticks)
        # a streamed tick has the price push send the dashboard what moved (_price_push_loop)
        self._price_wake = threading.Event()
        self.md.streams.add_listener(lambda symbols: self._price_wake.set())
        self._reconciled_at = float("-inf")         # the loop's last position check (see _reconcile_if_due)
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
        self._replay_session: Optional[dt.date] = None       # the session the daily replay was started for
        self._replay_resumed = False                         # an interrupted replay was looked for (_resume_replay)
        self._last_cycle_at = float("-inf")
        self._last_fast_at = float("-inf")
        self._last_plays_at = float("-inf")
        # the day's first wide scan comes a spacing after the start; after a restart, a spacing after the
        # last one (day_state.py)
        self._last_wide_at = time.monotonic()
        self._noted: Dict[tuple, float] = {}          # Autopilot notes already shown, and when
        self._scan_retry_at = 0.0
        # the candle-close and early-mover checks the candle loop queues for the scan loop (_on_minute). The lock
        # guards the queue only - it is never held across a request, the scanner, the board or Autopilot
        self._close_lock = threading.Lock()
        self._close_due: Optional[Tuple[dt.datetime, float]] = None   # the 5-minute close, and the monotonic time due
        self._close_only: Optional[List[str]] = None       # ...the stocks its second ask is for (None = the whole tier)
        self._movers_due: Dict[str, dt.datetime] = {}      # early movers to check: the minute each was found in
        self._mover_why: Dict[str, str] = {}               # ...and why, for the check's log line
        self._mover_at: Dict[str, float] = {}              # when each stock was last queued as a mover (epoch)
        # scan thread only: when each stock was last asked for its bars by a check, and the last check's context
        self._close_asked: Dict[str, float] = {}
        self._close_ran: Optional[Dict[str, Any]] = None
        # IBKR's live scans (_live_scan_once): the names a close check found too thin on today's volume, left out
        # for the rest of the session, and the session that is
        self._live_thin: set = set()
        self._live_session: Optional[dt.date] = None

        # the orders working at the broker, for the dashboard (see active_orders)
        self._orders: List[Dict[str, Any]] = []
        self._orders_ok = False
        self._orders_at = float("-inf")
        self._orders_checked: Optional[str] = None
        self._orders_lock = threading.Lock()
        # a trade's own 5-minute candles for its record's chart, kept a minute (see trade_chart)
        self._trade_bars: Dict[str, Tuple[float, Any]] = {}

        self._quit_lock = threading.Lock()
        self._fix_lock = threading.Lock()          # a share-count fix books at most once, however many tabs click
        self._quit_retry_at = 0.0
        self._quit_rounds = 0
        self._switch_lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    @property
    def broker(self) -> Optional[BrokerAdapter]:
        return self._broker

    def _publish(self, topic: str, **payload: Any) -> None:
        """Publish on the engine's event bus - the mixins go through here, so swapping BUS reaches them."""
        BUS.publish(topic, **payload)

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                         #
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        log.info("engine starting (mode=%s, paper platform=%s)", self.mode, self.paper_platform)
        self._bind()
        self._refresh_account()
        self._check_arm()
        self._restore_day()
        if self.quit_state:
            log.warning("resuming an unfinished quit - closing the remaining positions first")
        self._threads = [threading.Thread(target=loop, name=name, daemon=True) for name, loop in (
            ("scan-loop", self._scan_loop), ("sync-loop", self._sync_loop), ("snapshot-loop", self._snapshot_loop),
            ("orders-loop", self._orders_loop), ("signals-loop", self._signals_loop),
            ("journal-loop", self._journal_loop), ("pairs-loop", self._pairs_loop),
            ("stream-loop", self._stream_loop), ("price-push", self._price_push_loop),
            ("candle-loop", self._candle_loop), ("live-scan", self._live_scan_loop))]
        for t in self._threads:
            t.start()
        self._publish("engine.started", state=self.snapshot())

    def stop(self) -> None:
        self._stop.set()
        self._scan_wake.set()
        self._snapshot_wake.set()
        self._stream_wake.set()
        self._exit_wake.set()
        self._price_wake.set()
        self._day_changed(now=True)
        self.connections.close_all()
        log.info("engine stopped")

    def _save_runtime(self) -> None:
        # read and written under one lock: a save that read the state earlier can't land last
        with self._runtime_lock:
            payload: Dict[str, Any] = {
                "mode": self.mode, "paper_platform": self.paper_platform, "filters": self.filters.as_dict(),
                "strategies": self.strategy_overrides, "capital": self.capital, "capital_mode": self.capital_mode,
                "capital_split": {"day_pct": self.day_trade_pct},
                "sizing": {"factor": self.size_factor,
                           **({"max_position_pct": self.position_pct} if self.position_pct is not None else {})},
                "scan": self.scan_settings.as_dict(),
                "autopilot": self.autopilot.to_runtime(),
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
            self.executor.exit_cfg = cfg.exit_manager
            self.executor.on_entry_unfilled = self.autopilot.entry_unfilled
            self.executor.on_entries_adopted = self.autopilot.recognise_entries
        else:
            self.executor.rebind(broker, venue=venue)
        # before any exit can be sent twice. A broker that can't list its orders now has them taken over by
        # a later order sync; either way Autopilot hears of the entries it sent (on_entries_adopted)
        self.executor.adopt_working_orders()
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
        self._publish("broker.connected", state=self.snapshot(),
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
                note = (f"IB Gateway has been unreachable for {duration(now - self._gateway_down_at)}. If it's asking "
                        "you to log in - IBKR wants a full login about once a week - log in again; the app reconnects "
                        "by itself. Until then there are no prices, and no automatic exits.")
                log.warning(note)
                self._publish("broker.down", note=note)
            return
        self._gateway_up = up
        if not up:
            self._gateway_down_at, self._gateway_alerted = now, False
            if self._gateway_seen_up:
                log.warning("IB Gateway disconnected - reconnecting by itself")
                self._publish("broker.disconnected", note=("IB Gateway disconnected - its nightly restart or IBKR's "
                                                         "maintenance. The app reconnects by itself."))
            return
        down_for, self._gateway_down_at = now - (self._gateway_down_at or now), None
        if not self._gateway_seen_up:
            self._gateway_seen_up = True                  # the first connection is announced by _retry_connection
            return
        self.position_check.reset()                       # misses counted before the drop don't carry over
        self._refresh_account()
        log.warning("IB Gateway is back after %s", duration(down_for))
        self._publish("broker.reconnected", state=self.snapshot(),
                    note=f"IB Gateway is back after {duration(down_for)}.")

    def _open_trades(self) -> List[Dict[str, Any]]:
        try:
            return self.repo.open_trades()
        except Exception:  # noqa: BLE001
            return []

    def _positions_here(self) -> List[Dict[str, Any]]:
        """OPEN trades held on the venue orders currently go to."""
        return [t for t in self._open_trades() if (t.get("broker") or "paper") == self._venue]

    def open_positions(self) -> List[Dict[str, Any]]:
        """The OPEN trade records for the Open positions tab, each with ``protection``: the stop and target
        orders the executor keeps resting at the broker for it (Executor.protective_stops / resting_targets -
        the ones it placed and follows, the same the exit manager leaves the target to), and whether this venue
        rests them at all (``native``: IBKR does; on the simulator the app watches the price itself). None for
        a trade held on another venue: nothing is placed for it while it's parked."""
        ex = self.executor
        native = bool(ex is not None and ex.native_stops_on())
        stops = {s["trade_id"]: s for s in ex.protective_stops()} if ex is not None else {}
        targets = {s["trade_id"]: s for s in ex.resting_targets()} if ex is not None else {}
        trades = self.repo.open_trades()
        for t in trades:
            here = (t.get("broker") or "paper") == self._venue
            t["protection"] = ({"native": native, "stop": stops.get(t["id"]), "target": targets.get(t["id"])}
                               if here else None)
        return trades

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

    def cancel_working_orders(self, operator: str = "operator") -> Dict[str, Any]:
        """Cancel every working order on the venue except the stops protecting open positions."""
        if self.executor is None:
            return {"ok": False, "reason": "no broker is connected"}
        try:
            counts = self.executor.cancel_working_orders()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": f"the orders couldn't be cancelled: {e}"}
        self._refresh_orders()
        gone = counts["entries"] + counts["exits"] + counts["others"]
        log.warning("%s cancelled %d working order(s)", operator, gone)
        return {"ok": True, "counts": counts,
                "note": (f"Cancelled {gone} working order(s): {counts['entries']} entries, {counts['exits']} exits, "
                         f"{counts['others']} others. {counts['stops_kept']} protective stop(s) stay - they go "
                         "when their position is closed.")}

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
                self._publish("orders.updated", **self._orders_payload())
        finally:
            self._orders_lock.release()

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
        self._settings_changed()                           # another account: other positions, caps and proof rule
        log.warning("routing changed by %s: mode %s -> %s, paper platform %s, orders -> %s",
                    operator, prev_mode, self.mode, self.paper_platform, self._venue)
        self._publish("broker.switched", mode=self.mode, prev=prev_mode, state=self.snapshot())

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
    def prices_refused(self) -> str:
        """Why no price can be read right now, if none can (MarketData.refused). Autopilot takes
        no entries then: the board is stale and the last look before an order would be blind."""
        return self.md.refused

    def entry_pace_seconds(self, timeframe: str) -> float:
        """How long one scan cycle lasts for Autopilot's new-entries-per-cycle cap: the fast hot-list
        cycle for a day trade, the regular cycle for anything else."""
        if timeframe == "INTRADAY":
            return float(self.settings.config.scanner.fast_cycle_seconds)
        return float(self.scan_settings.cycle_minutes) * 60.0

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
            # every other loop guards its body; this one must too - if it stops, no scan runs and Autopilot
            # never takes another entry, with nothing on the dashboard to say so
            kind = None
            try:
                kind = self._due_scan()
                if kind:
                    self._run_scan(kind)
                self._save_day()
                self._resume_replay()
            except Exception:  # noqa: BLE001
                log.exception("scan loop pass failed")
            # a candle-close check due sooner cuts the wait short; one that came due while a scan ran follows it
            # at once - but a pass that ran nothing (quitting, backing off) never spins on it
            wait = self._next_scan_wait()
            self._scan_wake.wait(wait if wait > 0 or kind else 5.0)
            self._scan_wake.clear()

    def _next_scan_wait(self) -> float:
        """How long the scan loop waits before its next pass: 5 s, or until a candle-close check is due if that
        is sooner - so the check starts scanner.close_grace_s after the close (and its second ask CLOSE_RETRY_S
        after it read) without the candle thread sleeping."""
        with self._close_lock:
            due = self._close_due
        return 5.0 if due is None else max(0.0, min(5.0, due[1] - time.monotonic()))

    #: the order sync and a full pass of the exits come this often
    SYNC_S = 4.0
    #: in regular hours a streamed tick on a stock held runs its exits this long after their last pass at the soonest
    EXIT_TICK_GAP_S = 1.0

    def _sync_loop(self) -> None:
        steps = ((self._sync_orders, "order sync"), (self._run_exits, "automatic exits"),
                 (self._check_quit_progress, "quit progress check"))
        while not self._stop.is_set():
            # ticks drive the exits in regular hours only; after-hours prints are read by the full pass, as before
            self._tick_exits_on = clock.current_session() is clock.Session.REGULAR
            self._exit_wake.clear()
            self._take_ticked()                     # the full pass reads every price afresh
            for step, what in steps:
                try:
                    step()
                except Exception:  # noqa: BLE001
                    log.exception("%s failed", what)
            self._exits_on_ticks(time.monotonic())

    def _exits_on_ticks(self, last_exit: float) -> None:
        """Until the next full pass is due, run the exits on the stocks held whose streamed price moved -
        EXIT_TICK_GAP_S apart at the most. Here on the sync thread, so an exit pass never runs beside another
        or beside the order sync."""
        deadline = last_exit + self.SYNC_S
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or self._stop.is_set() or not self._exit_wake.wait(left) or self._stop.is_set():
                return                              # the full pass is due, or the engine is stopping
            self._exit_wake.clear()
            wait, tick_pass = tick_exit_wait(time.monotonic(), last_exit, deadline, self.EXIT_TICK_GAP_S)
            if (wait > 0 and self._stop.wait(wait)) or not tick_pass:
                return
            only = self._take_ticked()
            if only:
                try:
                    self._run_exits(only=only)
                except Exception:  # noqa: BLE001
                    log.exception("automatic exits on ticks failed")
                last_exit = time.monotonic()

    def _on_stream_ticks(self, symbols: FrozenSet[str]) -> None:
        """StreamManager's word that ``symbols`` ticked - on the IB loop thread, so it only notes the stocks the
        exits manage and wakes the sync loop."""
        em = self.exit_manager
        hit = em.watched.intersection(symbols) if em is not None and self._tick_exits_on else frozenset()
        if hit:
            with self._ticked_lock:
                self._ticked |= hit
            self._exit_wake.set()

    def _take_ticked(self) -> FrozenSet[str]:
        with self._ticked_lock:
            hit, self._ticked = frozenset(self._ticked), set()
        return hit

    def _sync_orders(self) -> None:
        if self.executor:
            self.executor.sync_open_orders()

    def _run_exits(self, only: Optional[FrozenSet[str]] = None) -> None:
        """A full pass of the exits, or with ``only`` a tick pass on just those stocks (ExitManager.run_once)."""
        if self.exit_manager and self.exit_manager.run_once(only):
            self._refresh_account()
            self._publish("account.snapshot", state=self.snapshot())

    def _snapshot_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.connections.refresh()
                self._retry_connection()
                self._watch_gateway()
                if self._refresh_account():
                    self._reconcile_if_due()
                state = self.snapshot()
                if self._account:
                    self.repo.snapshot_account(self._account, self._venue,
                                               realized_day=state["pnl"].get("realized_today", 0.0))
                self._publish("account.snapshot", state=state)
            except Exception:  # noqa: BLE001
                log.exception("snapshot failed")
            self._snapshot_wake.wait(10.0 if self._autopilot_day_active() else 30.0)
            self._snapshot_wake.clear()

    #: the least time between two of the loop's position checks
    RECONCILE_MIN_GAP_S = 8.0

    def _reconcile_if_due(self) -> None:
        """The snapshot loop's position check, at most once per RECONCILE_MIN_GAP_S. A click that wakes the
        loop early gets a fresh account read, but the check waits for its usual turn: a record only counts as
        gone after two checks miss it, and two checks a second apart would let a stop filled at the broker be
        booked as closed outside before the order sync books it as the stop it was."""
        now = time.monotonic()
        if now - self._reconciled_at < self.RECONCILE_MIN_GAP_S:
            return
        self._reconciled_at = now
        self._reconcile_open_trades()

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

    #: the streams are pointed at the stocks that matter this often, and sooner when the positions or plays change
    STREAM_RESYNC_S = 5.0

    def _stream_loop(self) -> None:
        """Keeps IBKR's real-time streams on the stocks that matter most (see data/streams.py). A thread of its
        own: a resync waits on the Gateway, and must never hold up the exits or a scan."""
        while not self._stop.is_set():
            try:
                self._resync_streams()
            except Exception:  # noqa: BLE001
                log.debug("stream resync failed", exc_info=True)
            self._stream_wake.wait(self.STREAM_RESYNC_S)
            self._stream_wake.clear()

    def _resync_streams(self) -> List[str]:
        """Stream the stocks held, then the plays on offer - the ones the operator opened and the ones
        Autopilot would take first - then the watch tier (the day's hot list, kept and next buffer names, up to
        execution.stream_watch; 0 = none), all within execution.stream_lines (0 = none, and every price is a
        snapshot as before). Returns the symbols streaming now."""
        execution = self.settings.config.execution
        lines = int(execution.stream_lines or 0)
        held, plays = self._stream_wanted() if lines > 0 else ([], [])
        n = int(execution.stream_watch or 0)
        try:
            watch = self.scanner.watch_symbols(n) if lines > 0 and n > 0 else []
        except Exception:  # noqa: BLE001 - the positions and plays still stream; the tier waits for the next pass
            log.debug("the watch tier couldn't be read", exc_info=True)
            watch = []
        return self.md.streams.sync(held, plays, lines, candidates=self._ap_candidates, watch=watch)

    def _stream_wanted(self) -> Tuple[List[str], List[str]]:
        """The stocks to stream, most needed first: those held - this venue's open trades, the entries still
        working, then anything else the account holds - and the stocks of the plays still on offer, best first."""
        held = [t["symbol"] for t in self._positions_here()]
        held += [w["symbol"] for w in self.working_entries()]
        held += [p.symbol for p in (self._account.positions if self._account else [])]
        try:
            plays = [p.symbol for p in self.board.ranked() if p.status is PlayStatus.PROPOSED]
        except Exception:  # noqa: BLE001 - ranked() reads the board without its lock, and a scan may change it then
            plays = []                        # the positions still stream; the plays wait for the next pass
        return list(dict.fromkeys(held)), list(dict.fromkeys(plays))

    #: the dashboard is sent the streamed prices that moved at most this often
    PRICE_PUSH_S = 1.0

    def _price_push_loop(self) -> None:
        """Sends the dashboard the streamed prices that moved (prices.tick), woken by a tick: one message with
        each stock's latest, then PRICE_PUSH_S before the next - so a busy tape costs one message a second,
        and nothing streaming costs none. Only shown: the exits and the entry checks read their own prices."""
        while not self._stop.is_set():
            self._price_wake.wait()
            self._price_wake.clear()
            if self._stop.is_set():
                return
            try:
                self._push_prices()
            except Exception:  # noqa: BLE001
                log.debug("price push failed", exc_info=True)
            self._stop.wait(self.PRICE_PUSH_S)

    def _push_prices(self) -> int:
        """One prices.tick with the streamed prices the dashboard hasn't been sent, in /api/price's words: the
        price, when it's from and the session it traded in. Returns how many went."""
        moved = self.md.streams.take_moves()
        if moved:
            self._publish("prices.tick", prices={
                symbol: {"price": price, "at": at.isoformat(), "session": SESSION_WORDS[clock.current_session(at)]}
                for symbol, (price, at) in moved.items()})
        return len(moved)

    #: the live candles close this long after each minute, so a tick stamped just before it is in first
    CANDLE_ROLL_DELAY_S = 0.25

    def _candle_loop(self) -> None:
        """Closes the live candles (data/candles.py) on the clock, a moment after each minute - so a quiet stock's
        candle closes on time too, not at its next tick. It never asks IBKR for anything, and never touches the
        scanner, the board or Autopilot."""
        while not self._stop.is_set():
            now = time.time()
            minute = (now // 60 + 1) * 60
            if self._stop.wait(minute - now + self.CANDLE_ROLL_DELAY_S):
                return
            try:
                self._on_minute(minute)
            except Exception:  # noqa: BLE001
                log.exception("live candles: the minute roll failed")

    #: a check that can't start within this long of its close is dropped - a long scan held the thread; the fast
    #: cycle and the next close cover it
    CLOSE_STALE_S = 60.0
    #: no stock is asked for its bars by two checks within this long: IBKR refuses identical requests within 15 s
    CLOSE_ASK_GAP_S = 15.0
    #: the stocks whose new bar a check at a close (it starts scanner.close_grace_s after it) didn't find are asked
    #: once more this long after it read - no sooner than IBKR's 15 s rule lets the same request go again
    CLOSE_RETRY_S = 15.0
    #: a stock is queued as an early mover at most this often
    MOVER_EVERY_S = 300.0
    #: a new high or low of the day counts as a move on this many times the stream's mean minute volume...
    MOVER_VOLUME_X = 3.0
    #: ...once the stream holds this many whole minutes of the stock today
    MOVER_MIN_CANDLES = 5

    def _on_minute(self, minute: float) -> Dict[str, Candle]:
        """The minute ending at ``minute`` (epoch seconds) is over: close its live candles, and the 5-minute ones
        on a :00/:05... boundary. Then, in regular hours on real-time data, queue the candle-close check at a
        5-minute close (due scanner.close_grace_s later, over the whole watch tier) or, between closes, the watch
        stocks whose minute was a move (_find_movers), and wake the scan loop. Here on the candle thread it only reads
        and queues: the scan thread fetches the bars, runs the setups and lets Autopilot enter. Returns {symbol:
        the 1-minute candle just closed}."""
        closed = self.md.candles.roll(minute)
        at = dt.datetime.fromtimestamp(minute, clock.NY)
        if not self._close_checks_on(at):
            return closed
        cfg = self.settings.config
        watch = self.scanner.watch_symbols(int(cfg.execution.stream_watch), at)
        if not watch:
            return closed
        # the 09:30 close has only pre-market behind it, and 16:00 none after it
        boundary = at.minute % 5 == 0 and self._in_session(at, 5)
        movers: Dict[str, str] = {}
        if not boundary and cfg.scanner.mover_atr > 0 and self._in_session(at, 1):
            movers = self._find_movers(closed, watch, at)
        queued = boundary
        with self._close_lock:
            if boundary:                                 # it covers the last close's second ask, if still due
                self._close_due = (at, time.monotonic() + float(cfg.scanner.close_grace_s))
                self._close_only = None
            if movers:
                self._mover_at = {s: t for s, t in self._mover_at.items() if minute - t < self.MOVER_EVERY_S}
            for symbol, why in movers.items():
                if symbol in self._mover_at:
                    continue                             # checked as a mover in the last 5 minutes
                self._movers_due[symbol], self._mover_why[symbol], self._mover_at[symbol] = at, why, minute
                queued = True
        if queued:
            self._scan_wake.set()
        return closed

    def _close_checks_on(self, at: dt.datetime) -> bool:
        """Whether the candle-close and early-mover checks run at ``at``: switched on (scanner.close_check, and a
        watch tier to check), connected on real-time data that flows, in regular hours and not while quitting.
        Connected means the Gateway is up, not only attached: while IBKR reconnects by itself the source stays
        attached, and a check queued then would only fail at the scan thread every 5 minutes."""
        cfg = self.settings.config
        return bool(cfg.scanner.close_check and int(cfg.execution.stream_watch or 0) > 0 and self.md.connected
                    and not self.md.delayed and not self.md.refused and not self.quit_state
                    and clock.is_market_open(at))

    @staticmethod
    def _in_session(at: dt.datetime, minutes: int) -> bool:
        """Whether the candle of ``minutes`` that ended at ``at`` lies in the regular session."""
        return clock.is_market_open(at - dt.timedelta(minutes=minutes)) and clock.is_market_open(at)

    def _find_movers(self, closed: Mapping[str, Candle], watch: Sequence[str], at: dt.datetime) -> Dict[str, str]:
        """The watch stocks whose whole 1-minute candle that just closed was a move: (a) its true range spans at
        least scanner.mover_atr of the stock's 5-minute ATRs (IBKR's bars, as cached - no request), or (b) it made
        a new high or low of the day on at least MOVER_VOLUME_X times the stream's mean minute volume today.
        Biggest range first, each with a few words on why. A candle spanning a whole 5-minute ATR is far outside
        a stock's usual minute, and (b) compares the stream with itself, so whether the stream counts shares or
        lots can't skew either. On the candle thread: it only reads."""
        k, midnight = float(self.settings.config.scanner.mover_atr), at.replace(hour=0, minute=0)
        found: List[Tuple[float, str, str]] = []
        for symbol in watch:
            c = closed.get(symbol)
            if c is None or c.partial:
                continue
            frame = self.md.cached_intraday(symbol)
            if frame is None or len(frame) < 15:
                continue
            atr5 = float(ta.atr(frame.tail(30), 14).iloc[-1])
            if not atr5 > 0:
                continue
            last = self.md.candles.closed(symbol, 1, n=2)
            prev = last[0].close if len(last) == 2 and last[1] is c else None
            high, low = (c.high, c.low) if prev is None else (max(c.high, prev), min(c.low, prev))
            span = (high - low) / atr5
            why = [f"1-minute candle {span:.1f} ATRs"] if span >= k else []
            stats = self.md.candles.day_stats(symbol, c.start)
            if (stats is not None and stats[3] >= self.MOVER_MIN_CANDLES and stats[2] > 0
                    and c.volume >= self.MOVER_VOLUME_X * stats[2]):
                # the day's range so far: the stream's whole minutes, and IBKR's bars that had ended by then
                bars = frame[(frame.index >= midnight)
                             & (frame.index <= c.at - dt.timedelta(minutes=5))]
                day_high = max(stats[0], float(bars["high"].max()) if len(bars) else stats[0])
                day_low = min(stats[1], float(bars["low"].min()) if len(bars) else stats[1])
                if c.high > day_high or c.low < day_low:
                    why.append(f"new {'high' if c.high > day_high else 'low'} on {c.volume / stats[2]:.1f}x volume")
            if why:
                found.append((span, symbol, ", ".join(why)))
        return {symbol: why for _, symbol, why in sorted(found, key=lambda f: -f[0])}

    #: IBKR's live market scans run this often in regular hours, one after another on the live-scan thread
    LIVE_SCAN_S = 60.0
    #: ...these three: the biggest % gainers, the biggest % losers and the stocks hottest by volume
    LIVE_SCAN_CODES = ("TOP_PERC_GAIN", "TOP_PERC_LOSE", "HOT_BY_VOLUME")
    #: names the scans bring that the app has never seen are looked up at most this many a round
    LIVE_LOOKUPS = 20

    def _live_scan_loop(self) -> None:
        """Runs IBKR's live market scans every LIVE_SCAN_S (_live_scan_once). A thread of its own: each scan waits
        a couple of seconds on the Gateway, and must never hold up a scan, the streams or the exits."""
        if self._stop.wait(30.0):                    # the connection and the day's watchlist come first
            return
        while not self._stop.is_set():
            try:
                self._live_scan_once()
            except Exception:  # noqa: BLE001
                log.exception("live scan failed")
            self._stop.wait(self.LIVE_SCAN_S)

    def _live_scan_once(self, now: Optional[dt.datetime] = None) -> List[str]:
        """One round of IBKR's live scans (LIVE_SCAN_CODES, one after another): their names interleaved by rank -
        each scan's first, then each one's second... - that SymbolMaster calls ordinary tradable shares, in a sector
        the filters allow, with daily candles (the morning's download covers every tradable listing) that reach the
        last session - or 5-minute candles in hand that do, as the setups would take yesterday's close from them
        (scanner/evaluator.py prev_close_known) - not on the hot list already and not found too thin today
        (_thin_live_names). The first scanner.live_scan of them take watch-tier slots right after the hot list
        (Scanner.set_live_names), so a stock too quiet for the morning's ranking is streamed and checked once it
        moves. Names never seen before are looked up first, LIVE_LOOKUPS a round, and kept in symbols.json. Only in
        regular hours on real-time data, with today's watchlist and the watch tier on, and not while quitting -
        otherwise nothing is asked and the names held are let go. Returns the names held now."""
        cfg = self.settings.config
        now = (now or clock.now_ny()).astimezone(clock.NY)
        n, wl = int(cfg.scanner.live_scan or 0), self.scanner.watchlist
        if not (n > 0 and int(cfg.execution.stream_watch or 0) > 0 and self.md.attached and not self.md.delayed
                and not self.md.refused and not self.quit_state and clock.is_market_open(now)
                and wl is not None and wl.session == now.date()):
            if self.scanner.live_names:
                self.scanner.set_live_names([])
                self._stream_wake.set()
            return []
        if self._live_session != now.date():
            self._live_session, self._live_thin = now.date(), set()     # a new session's volume is judged afresh
        scans = []
        for code in self.LIVE_SCAN_CODES:
            if self._stop.is_set():
                return list(self.scanner.live_names)
            scans.append(self.md.market_scan(code))
        if not any(scans):
            # in regular hours IBKR always has movers: nothing at all is the scans failing, and dropping the names
            # held would only end their streams until the next round brings them back
            return list(self.scanner.live_names)
        found = list(dict.fromkeys(s for rank in itertools.zip_longest(*scans) for s in rank if s))
        master = self.scanner.symbols
        unknown = master.unknown(found)[:self.LIVE_LOOKUPS]
        if unknown:
            try:
                master.record(self.md.source.contract_details_many(unknown))
            except Exception as e:  # noqa: BLE001 - the names already known still count; the rest wait a round
                log.debug("live scan: contract details unavailable: %s", e)
        hot, thin, sectors = set(wl.hot_symbols()), set(self._live_thin), self.scanner.filters.sectors
        names: List[str] = []
        for symbol in master.tradable(found):
            if len(names) >= n:
                break
            if (symbol not in hot and symbol not in thin and sector_allowed(master.sector(symbol), sectors)
                    and (daily := self.md.daily_frame(symbol)) is not None
                    and prev_close_known(daily, self.md.cached_intraday(symbol), now.date())):
                names.append(symbol)
        before = list(self.scanner.live_names)
        if names != before:
            self.scanner.set_live_names(names)
            moved = [f"+{s}" for s in names if s not in before] + [f"-{s}" for s in before if s not in names]
            if moved:
                log.info("live scan: %s", " ".join(moved))
            self._stream_wake.set()                  # the streams follow the watch tier
        return names

    def _thin_live_names(self, result: ScanResult) -> List[str]:
        """After a candle-close check: the live-scan names it read whose dollar volume today is short of the
        morning's liquidity floor (scanner.prefilter's min_dollar_volume, 5,000,000 when unset) pro rata for the
        time of day - max(30, minutes since the open) of 390 - are left out from the live scan's next round, for
        the rest of the session: judged on today's volume, as the morning's ranking judges 20 sessions'. Sizing's
        cap on the median daily volume (risk.max_adv_pct) still limits any order on them. Returns those names."""
        live = set(self.scanner.live_names)
        if not live:
            return []
        least = self.settings.config.scanner.prefilter.get("min_dollar_volume")
        floor = float(5_000_000 if least is None else least) * max(30.0, clock.minutes_since_open()) / 390.0
        stats = self.scanner.live_stats
        thin = [s for s in result.symbols if s in live and s in stats and stats[s][1] < floor]
        if thin:
            self._live_thin.update(thin)
            log.debug("live scan: %s too thin today (under $%.0f so far) - left out from the next round",
                      ", ".join(thin), floor)
        return thin

    # ------------------------------------------------------------------ #
    #  Scans                                                             #
    # ------------------------------------------------------------------ #
    def _due_scan(self) -> Optional[str]:
        """full | gappers | close | cycle | wide | fast | plays | None - see scanner/schedule.py."""
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
        due, waiting = self._close_check_due(mono)
        if due:
            return "close"
        # a candle-close check (or its second ask) starts within seconds (waiting): it stands in for the fast cycle,
        # and the quick re-check waits for it rather than hold it up
        if mono - self._last_cycle_at >= self.scan_settings.cycle_minutes * 60:
            return "cycle"
        if self.scan_settings.wide_on and mono - self._last_wide_at >= self.scan_settings.wide_minutes * 60:
            return "wide"
        if (not waiting and self._autopilot_day_active()
                and mono - self._last_fast_at >= self.settings.config.scanner.fast_cycle_seconds):
            return "fast"
        if not waiting and self._plays_due(mono):
            return "plays"
        return None

    def _close_check_due(self, mono: float) -> Tuple[bool, bool]:
        """(the candle-close or early-mover check _on_minute queued is due now, a 5-minute close's check is queued
        at all) - for _due_scan and the wide scan's pauses (_between_wide_chunks)."""
        with self._close_lock:
            close_due, movers = self._close_due, bool(self._movers_due)
        # the early movers wait for a 5-minute close queued behind them: it covers them, and run now it would read
        # IBKR's bars before scanner.close_grace_s has let IBKR finish them (a close's second ask takes them along)
        due = (close_due is not None and mono >= close_due[1]) or (close_due is None and movers)
        return due, close_due is not None

    def _plays_due(self, mono: float) -> bool:
        """The quick re-check of the plays on the board is due (scanner.plays_refresh_seconds, 0 = off)."""
        refresh = self.settings.config.scanner.plays_refresh_seconds
        return bool(refresh and mono - self._last_plays_at >= refresh and self._board_symbols())

    def _between_wide_chunks(self) -> List[str]:
        """Scanner.run_wide's pause between two chunks, on the scan thread the wide scan holds: a candle-close or
        early-mover check that came due runs now, else the quick re-check of the plays if it is due - each ends in
        Autopilot's pass, so a sweep of minutes holds up neither the check (CLOSE_STALE_S would drop it) nor an
        entry. Only those light checks run here: never a cycle, a full scan or another wide scan, and a scan asked
        for waits for the sweep to end. A failure is logged and the sweep goes on. Returns the stocks it read."""
        if self.quit_state or not clock.is_market_open():
            return []
        mono = time.monotonic()
        due, waiting = self._close_check_due(mono)
        kind = "close" if due else "plays" if not waiting and self._plays_due(mono) else None
        if kind is None:
            return []
        try:
            result = self._run_scan(kind)
        except Exception:  # noqa: BLE001 - the check's failure, not the sweep's
            log.exception("the %s between the wide scan's chunks failed",
                          "candle-close check" if kind == "close" else "quick re-check")
            return []
        return list(result.symbols) if result is not None else []

    def _settings_changed(self) -> None:
        """Called by everything that changes a setting while the app runs - the day/swing split, the
        trading capital, the filters, the strategies, Autopilot's own settings, where orders go, a new
        replay. Autopilot reads every setting afresh on each pass, so its *decisions* already follow;
        this makes the rest follow at once: plays it had refused for the day are handed back, the plays
        are sized again, the dashboard gets the plays (with Autopilot's verdict on each) and Autopilot's
        state, and the quick re-check of the board is pulled forward so the next pass isn't up to a
        cycle away. It enters nothing itself: entries stay on the scan thread, in a scan's own pass -
        a web thread placing orders would race it."""
        self.autopilot.settings_changed()
        self._risk_pct_for = None                          # the half-Kelly shares follow Autopilot's terms
        self._size_plays([p for p in self.board.plays.values() if p.status not in _ACTED_ON])
        self._publish_plays()
        self.autopilot.publish_status()
        self._last_plays_at = float("-inf")
        self._scan_wake.set()

    def _queue_scan(self, kind: str) -> None:
        with self._scan_lock:
            if self._scan_request != "full":           # a queued full scan covers a cycle too
                self._scan_request = kind
        self._scan_wake.set()

    def _run_scan(self, kind: str) -> Optional[ScanResult]:
        """Run the ``kind`` of scan and put what it found on the board, then Autopilot's pass. Returns the result -
        None when it ran nothing or failed, or for the gap check."""
        quick = kind == "plays"                         # the quick re-check of the plays on the board
        # ...and the candle-close check are light: seconds matter, so no lead-in, and a failure is only logged
        light = quick or kind == "close"
        if not light:
            self._scan_running = {"kind": kind, "started_at": clock.now_ny().isoformat()}
            self._publish("scan.started", kind=kind)
        # the candle-close check takes what the candle loop queued before anything can fail: a failure in the
        # lead-in below that left it queued would find it due again at once - the scan loop would spin on it,
        # logging, with every cycle starved behind it
        queued = self._take_close_queue() if kind == "close" else None
        try:
            if not light:
                self._refresh_account()
            self.scanner.account = self.sizing_account()
            if not light:
                self._refresh_regime()
            self.scanner.market = self.regime.context()
            self.scanner.evidence_weights = self.evidence_weights()
            self.scanner.strategy_records = self.strategy_odds()
            if kind == "full":
                result = self.scanner.run_full(self.scan_settings)
            elif kind == "gappers":
                result = self.scanner.run_gappers()
            elif kind == "wide":
                result = self.scanner.run_wide(self.scan_settings.wide_stocks, self.scan_settings.movers,
                                               between=self._between_wide_chunks)
            elif kind == "close":
                result = self._close_check(queued)
                if result is None:
                    return                                 # nothing left to check
            elif quick:
                result = self.scanner.run_plays(self._board_symbols())
            else:
                result = self.scanner.run_cycle(fast=kind == "fast")
        except NoDataSource as e:
            if quick:
                self._last_plays_at = time.monotonic()
            elif kind == "close":
                log.warning("the candle-close check failed: %s", e)
            else:
                self._scan_failed(kind, str(e))
            return
        except Exception as e:  # noqa: BLE001
            if quick:
                log.debug("quick re-check of the plays failed", exc_info=True)
                self._last_plays_at = time.monotonic()
            elif kind == "close":                        # no back-off: the fast cycle and the next close go on
                log.warning("the candle-close check failed: %s", e)
            else:
                log.exception("%s scan failed", kind)
                self._scan_failed(kind, f"The {kind} scan failed: {e}")
            return
        finally:
            if not light:
                self._scan_running = None

        mono = time.monotonic()
        if quick:
            self._last_plays_at = mono
        elif kind == "close":
            # a check at a 5-minute close that read at least half the stocks it asked stood in for the fast cycle:
            # the next one comes fast_cycle_seconds on. One that found most new bars not printed yet leaves the
            # fast cycle due. An early-mover check covered a few stocks only, so it moves no timer
            ran = self._close_ran
            if ran is not None and ran["boundary"] is not None and 2 * len(result.symbols) >= ran["asked"]:
                self._last_fast_at = mono
        else:
            self._scan_retry_at = 0.0
            if kind == "full":
                self._last_cycle_at = float("-inf")     # in the session, a cycle follows straight away
                try:
                    self._replay_after_full_scan()
                except Exception:  # noqa: BLE001 - the Gateway can drop between its checks; the scan goes on
                    log.exception("the day's replay couldn't be started")
            else:
                self._last_fast_at = mono
                if kind == "cycle":
                    self._last_cycle_at = mono
                elif kind == "wide":                     # it covered the hot list and buffers too
                    self._last_wide_at = self._last_cycle_at = mono
                    self._last_wide_done = clock.now_ny()
        if kind == "gappers":                    # it moves names between the lists; the plays are untouched
            wl = self.scanner.watchlist
            self._gappers_session = wl.session if wl else None
            self._last_scans[kind] = result.summary()
            self._day_changed()
            self._publish("watchlist.updated", **self.watchlist_state())
            return
        self._size_plays(result.plays)
        # the cycles don't re-check valuation setups, so those stay. A quick re-check isn't a scan confirming
        # a setup - but with confirm_on_new_candle a day play counts candles, not scans, and a newer candle
        # counts whichever scan read it
        self._score_plays(result.plays)
        changes = self.board.replace(result.plays, None if kind == "full" else result.symbols,
                                     keep=lambda p: p.kind.value == "FUNDAMENTAL", confirm=not quick,
                                     new_candle=self.autopilot.confirm_on_new_candle)
        self._last_scans[kind] = result.summary()
        self._day_changed()
        if not quick:
            try:
                held = [p for p in result.plays if self.board.holds(p)]      # not the setups already acted on
                self.repo.record_scan(result, keep_rejected=self.settings.config.database.record_rejected_plays,
                                      plays=held)
            except Exception:  # noqa: BLE001
                log.exception("could not save the scan")
        if not light:                                      # the light checks make no watchlist decision
            self._publish("watchlist.updated", **self.watchlist_state())
        if not self.quit_state:
            try:
                self.autopilot.consider(self.board.plays)
            except Exception:  # noqa: BLE001
                log.exception("autopilot pass failed")
        self._publish_plays()                              # after the pass: each play carries Autopilot's verdict on it
        if kind == "close":
            self._log_close_check(result, changes)
            self._thin_live_names(result)
        self._note_changes(changes)
        return result

    def _take_close_queue(self) -> Tuple[Optional[dt.datetime], Dict[str, dt.datetime], Dict[str, str],
                                         Optional[List[str]]]:
        """Take what the candle loop queued (_on_minute): the 5-minute close once it is due - with the stocks it
        is for when it is a close's second ask, None for the whole tier - and the early movers with why. A close
        not due yet stays queued, and the movers with it - it covers them, and IBKR is still finishing its bar
        (scanner.close_grace_s); the scan loop wakes for it when it is due."""
        with self._close_lock:
            due = self._close_due
            if due is not None and time.monotonic() < due[1]:
                return None, {}, {}, None
            movers, why, only = self._movers_due, self._mover_why, self._close_only
            self._close_due, self._movers_due, self._mover_why, self._close_only = None, {}, {}, None
        return (due[0] if due is not None else None), movers, why, (only if due is not None else None)

    def _close_check(self, queued: Optional[Tuple[Optional[dt.datetime], Dict[str, dt.datetime], Dict[str, str],
                                                  Optional[List[str]]]] = None) -> Optional[ScanResult]:
        """The check the candle loop queued (_on_minute), on the scan thread - ``queued`` as _take_close_queue
        took it (taken here when not given): at a 5-minute close the whole watch tier, else the early movers still
        in it, on IBKR's newest 5-minute bars (Scanner.run_close). The stocks whose new bar a close's check didn't
        find are queued for a second ask CLOSE_RETRY_S after it read - once: that ask leaves the rest to the fast
        cycle and the next close. A check that couldn't start within CLOSE_STALE_S of its close is dropped, and no
        stock is asked twice within CLOSE_ASK_GAP_S. None when nothing is left to check."""
        boundary, movers, why, only = queued if queued is not None else self._take_close_queue()
        now = clock.now_ny()
        if boundary is not None and (now - boundary).total_seconds() > self.CLOSE_STALE_S:
            log.debug("the %s ET candle-close check%s is dropped: it couldn't start until %.0f s after the close",
                      boundary.strftime("%H:%M"), "'s second ask" if only is not None else "",
                      (now - boundary).total_seconds())
            boundary = only = None
        movers = {s: m for s, m in movers.items() if (now - m).total_seconds() <= self.CLOSE_STALE_S}
        watch = self.scanner.watch_symbols(int(self.settings.config.execution.stream_watch or 0))
        if boundary is not None:
            # a second ask is for the stocks still in the tier - and any early mover that waited behind it
            again = None if only is None else set(only) | set(movers)
            symbols, since = [s for s in watch if again is None or s in again], boundary
        else:
            tier = set(watch)
            symbols = [s for s in movers if s in tier]
            since = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
        mono = time.monotonic()
        self._close_asked = {s: t for s, t in self._close_asked.items() if mono - t < 60.0}    # long past IBKR's 15 s
        symbols = [s for s in symbols if mono - self._close_asked.get(s, float("-inf")) >= self.CLOSE_ASK_GAP_S]
        if not symbols:
            return None
        self._close_asked.update(dict.fromkeys(symbols, mono))
        self._close_ran = {"boundary": boundary, "asked": len(symbols), "again": only is not None,
                           "minute": max((movers[s] for s in symbols if s in movers), default=since),
                           "why": {s: why[s] for s in symbols if s in why}}
        result = self.scanner.run_close(symbols, since)
        if boundary is not None and only is None:
            # IBKR prints some stocks' new bars later than others: those are asked once more, past its 15 s rule
            read = set(result.symbols)
            late = [s for s in symbols if s not in read]
            with self._close_lock:
                if late and self._close_due is None:       # a newer close queued meanwhile covers them
                    self._close_due, self._close_only = (boundary, time.monotonic() + self.CLOSE_RETRY_S), late
        return result

    def _log_close_check(self, result: ScanResult, changes: List[Any]) -> None:
        """The check's one INFO line: what it read and found, and how long after the close its plays were out."""
        ran = self._close_ran or {}
        now = clock.now_ny()
        n, new = len(result.plays), sum(c.kind == "added" for c in changes)
        plays = f"{n} play{'' if n == 1 else 's'}"
        boundary = ran.get("boundary")
        if boundary is not None:
            log.info("close check %s ET%s: %d of %d stocks read, %s (%d new) - published %.1f s after the candle "
                     "closed (candles %.1f s, setups %.1f s)", boundary.strftime("%H:%M"),
                     " (second ask)" if ran.get("again") else "", len(result.symbols),
                     ran.get("asked", 0), plays, new, (now - boundary).total_seconds(),
                     result.timings.get("intraday_candles", 0.0), result.timings.get("setups", 0.0))
            return
        minute, asked = ran.get("minute") or now, int(ran.get("asked", 0))
        why = "; ".join(f"{s}: {w}" for s, w in (ran.get("why") or {}).items())
        log.info("early-mover check %s ET (%s): %d stock%s, %s - published %.1f s after the minute closed",
                 minute.strftime("%H:%M"), why, asked, "" if asked == 1 else "s", plays,
                 (now - minute).total_seconds())

    def _replay_after_full_scan(self) -> None:
        """The morning's full scan has just built today's watchlist - replay the strategies on it now
        (replay.daily). It is the one moment in the day when that is free: the watchlist is fresh, the
        market is still an hour off, and the replay wants the Gateway for the candles it downloads.
        Once a session, never while one is already running, and never while quitting. A failure is the
        replay's own to report - it only ever refreshes records, and nothing waits on it."""
        if not bool(getattr(self.settings.config.replay, "daily", False)) or self.quit_state:
            return
        session = clock.session_date()
        if self._replay_session == session or self.replay.running:
            return
        if clock.is_market_open():
            # the morning scan is an hour before the open, which is the point of doing it then. A full scan
            # during the session (a fresh start, a widened filter) must not hand the replay's downloads the
            # Gateway while the cycles need it - it waits for the next morning.
            return
        out = self.start_replay()
        if out.get("ok"):
            self._replay_session = session              # done for today - a restart won't start it again
            self._day_changed()
            log.info("the day's replay started by itself after the full scan: %s", out.get("note", ""))
        else:
            # not marked done: the next full scan before the open (a restart, say) tries again
            log.warning("the day's replay didn't start: %s", out.get("reason", ""))
        self._publish("replay.started", auto=True, **{k: out[k] for k in ("ok", "note", "reason") if k in out})

    def _scan_failed(self, kind: str, reason: str) -> None:
        self._scan_retry_at = time.monotonic() + self.SCAN_RETRY_S
        log.warning("%s scan skipped: %s", kind, reason)
        self._publish("scan.failed", kind=kind, reason=reason)

    def request_scan(self, kind: str = "cycle") -> Dict[str, Any]:
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        if kind not in ("full", "cycle", "gappers", "wide"):
            return {"ok": False, "reason": "scan must be 'full', 'cycle', 'gappers' or 'wide'"}
        if kind != "full" and self.scanner.watchlist is None:
            kind = "full"
        self._queue_scan(kind)
        note = {"full": "Full scan queued: every US stock gets ranked and today's hot list and sector buffers are rebuilt.",
                "cycle": "Rescanning the hot list and the next buffer names.",
                "gappers": "Reading the hot list and buffer names' pre-market candles: the stocks gapping on volume "
                           "join the hot list.",
                "wide": "Wide scan queued: every liquid stock's 5-minute candles, one request each - it takes a few "
                        "minutes, and the setups run on all of them."}[kind]
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
            "last_wide": self._last_scans.get("wide"),
            "wide_minimum_minutes": ScanSettings.WIDE_MIN_MINUTES,
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
        if (new.movers, new.yesterday_movers) != (old.movers, old.yesterday_movers):
            parts = ([f"today's {new.movers} biggest movers after each wide scan"] if new.movers else []) + (
                [f"yesterday's {new.yesterday_movers} from the full scan"] if new.yesterday_movers else [])
            notes.append("Hot-list slots for " + " and ".join(parts) + "." if parts
                         else "Movers no longer hold hot-list slots of their own.")
        if (new.wide_minutes, new.wide_stocks) != (old.wide_minutes, old.wide_stocks):
            which = f"the hottest {new.wide_stocks:,}" if new.wide_stocks else "every liquid stock"
            notes.append(f"The wide scan is off." if not new.wide_on
                         else f"The wide scan reads {which} every {new.wide_minutes} minutes.")
        if (new.hot_list_size, new.sector_queue_size) != (old.hot_list_size, old.sector_queue_size):
            notes.append("List sizes apply from the next full scan - Run full scan now rebuilds today's lists.")
        state = self.scan_status()
        self._publish("settings.updated", scan=state)
        return {"ok": True, "scan": state, "note": " ".join(notes)}

    def watchlist_state(self) -> Dict[str, Any]:
        return {"watchlist": self.scanner.watchlist_state(), "scan": self.scan_status()}

    def _publish_plays(self) -> None:
        records: Dict[str, Dict[str, Any]] = {}                # each setup's record read once for the board
        rows = [self._slim(self._decorate(p, records)) for p in self.board.ranked()[:self.BOARD_ROWS]]
        self._publish("plays.updated", plays=rows)
        # the plays Autopilot would take - its badge's reading: on offer, passing its checks, not tried yet -
        # stream ahead of the rest, so an entry it sends is priced off the stream
        self._ap_candidates = tuple(dict.fromkeys(
            r["symbol"] for r in rows
            if r.get("status") == PlayStatus.PROPOSED.value and (ap := r.get("autopilot") or {}).get("eligible")
            and not ap.get("acted")))
        self._stream_wake.set()                                 # the streams follow the new ranking

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
            self._publish("plays.changes", notes=notes)

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

    def trade_chart(self, trade_id: str) -> Dict[str, Any]:
        """The chart behind a trade record - GET /api/trades/{id}/chart: the candles from the session
        before its entry, its levels and marks, and how it stands (see engine/chart.py). The 5-minute
        candles are the scans' cached ones while those cover the trade, else one request of its own kept
        a minute; a trade past the intraday window, open or closed, shows the daily candles on hand."""
        rec = self.repo.trade_record(trade_id)
        if rec is None:
            return {"ok": False, "reason": "That trade record is gone."}
        t = rec["trade"]
        sym, sessions = t["symbol"], trade_sessions(t, clock.session_date())
        intraday = sessions <= INTRADAY_MAX_SESSIONS
        if intraday and not self.md.attached:
            return {"ok": False, "reason": "IB Gateway isn't connected, so there are no candles."}
        frame = None
        if intraday:
            span = candle_request(sessions)
            try:
                frame = (self._trade_bars_for(trade_id, sym, span) if span
                         else self.md.intraday([sym], self.scanner.con_ids([sym])).get(sym))
            except Exception:  # noqa: BLE001
                log.debug("chart candles for %s failed", sym, exc_info=True)
            if frame is not None and len(frame):
                frame = since_session_before(frame, t)
        if frame is None or not len(frame):
            frame, intraday = self.md.daily_frame(sym), False
        mark = self._trade_mark(sym) if t["status"] == "OPEN" else None
        return trade_chart_payload(rec, frame, intraday, self.settings.config.exit_manager, mark)

    def _trade_bars_for(self, trade_id: str, symbol: str, span: str) -> Optional[Any]:
        """A trade's own 5-minute candles over ``span`` (an IBKR duration) - a hold too long for the scans'
        cached window - kept TRADE_CANDLES_TTL_S, so repeated clicks on its record don't each reach IBKR."""
        now = time.monotonic()
        hit = self._trade_bars.get(trade_id)
        if hit and now - hit[0] < TRADE_CANDLES_TTL_S:
            return hit[1]
        frame = self.md.source.history_many({symbol: (INTRADAY_BAR, span)}, self.scanner.con_ids([symbol]),
                                            rth=True).get(symbol)
        self._trade_bars = {k: v for k, v in self._trade_bars.items() if now - v[0] < TRADE_CANDLES_TTL_S}
        self._trade_bars[trade_id] = (now, frame)
        return frame

    def _trade_mark(self, symbol: str) -> Optional[Tuple[float, Optional[str]]]:
        """The price the blotter shows a position at, and when it's from: the app's own when fresh
        (_marks), else the broker's mark, else the newest price the app holds at all."""
        mark = self._marks().get(symbol)
        if mark:
            return mark
        pos = self._account.position(symbol) if self._account else None
        if pos is not None and pos.market_price:
            return (float(pos.market_price), None)
        seen = self.md.last_seen(symbol)
        return (round(seen[0], 4), seen[1].isoformat()) if seen else None

    # ------------------------------------------------------------------ #
    #  Account, arming, broker vs database                              #
    # ------------------------------------------------------------------ #
    #: how often a failing account read is worth a warning in the log
    ACCOUNT_WARN_S = 300.0

    def _refresh_account(self) -> bool:
        """Read the account; when the broker can't answer, keep the last snapshot - it then reads as
        stale, so nothing is deleted or closed on its say-so until a fresh one arrives."""
        try:
            if self._broker:
                self._account = self._broker.get_account()
                self._account_at = time.monotonic()
                return True
        except Exception as e:  # noqa: BLE001
            now = time.monotonic()
            if now - self._account_warned_at >= self.ACCOUNT_WARN_S:
                self._account_warned_at = now
                log.warning("the account couldn't be read - keeping the last snapshot (%.0f s old): %s",
                            now - self._account_at if self._account_at > 0 else 0.0, e)
            else:
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
            self._publish("engine.disarmed",
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
            self._publish("trades.removed", trades=removed, venue=venue, venue_label=venue_label(venue))

        removed_ids = {r["id"] for r in removed} | {c["id"] for c in closed}
        if account_age_s <= self.position_check.FRESH_ACCOUNT_S and connection_age_s >= self.position_check.SETTLE_S:
            self._settle_short([t for t in mine if t["id"] not in removed_ids], held)
        new = self.position_check.share_counts(
            venue, venue_label(venue), [t for t in mine if t["id"] not in removed_ids], held=held,
            in_flight=self.executor.symbols_in_flight(),
            account_age_s=account_age_s, connection_age_s=connection_age_s)
        for m in new:
            log.warning("share counts disagree: %s", m["note"])
        if new:
            self._publish("positions.mismatch", mismatches=new)
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
                self._publish("trade.closed", trade=out, reason="closed outside the app, booked from the broker's fills")
            elif self.repo.delete_trade(t["id"]):
                removed.append(row)
        return closed, removed

    def _settle_short(self, trades: List[Mapping[str, Any]], held: Mapping[str, float]) -> List[Dict[str, Any]]:
        """A record holding more shares than the broker, the same way round, because an exit the app sent
        filled in part before it was called off - "Stop quitting", an exit cancelled - and the app stopped
        before it heard: the part that filled is booked, from the broker's fills tagged with that trade's
        own exit orders, at their prices. Only fills of its ``exit:`` orders, never more than the record
        is over by, never while an exit for it is still working (the executor books those), and only for a
        symbol with one record. A stop or target filling is booked by its own watcher. Returns what it booked."""
        get = getattr(self._broker, "get_fills", None)
        if not callable(get) or self.executor is None:
            return []
        busy, flying = self.executor.pending_exit_trade_ids(), self.executor.symbols_in_flight()
        per_symbol: Dict[str, int] = {}
        for t in trades:
            per_symbol[t["symbol"]] = per_symbol.get(t["symbol"], 0) + 1
        booked: List[Dict[str, Any]] = []
        for t in trades:
            sym, tid = t["symbol"], t["id"]
            if t.get("pair_id") or tid in busy or sym in flying or per_symbol[sym] != 1:
                continue
            record, sign = abs(float(t.get("quantity") or 0.0)), (1.0 if t["side"] == "LONG" else -1.0)
            now_held = float(held.get(sym, 0.0)) * sign                       # positive: held the trade's way round
            short = record - now_held
            if not (0.0 < now_held < record) or short <= 1e-9:
                continue
            seen = self.__dict__.setdefault("_short_checked", {})
            if seen.get(tid) == (record, now_held):
                continue                                 # looked already: the fills don't explain it (sold in TWS, say)
            seen[tid] = (record, now_held)
            try:
                fills = [f for f in get(sym) if getattr(f, "tag", "") == f"exit:{tid}"
                         and (f.side is Side.SHORT) == (t["side"] == "LONG")]
            except Exception:  # noqa: BLE001 - the broker couldn't say: nothing is booked
                continue
            # the shares its record already took off were booked when they filled: the rest of the fills weren't
            done = max(0.0, abs(float(t.get("initial_quantity") or record)) - record)
            left, qty, value = done, 0.0, 0.0
            for f in sorted(fills, key=lambda f: f.ts):
                take = float(f.quantity)
                if left > 0:
                    skip = min(left, take)
                    left, take = left - skip, take - skip
                take = min(take, short - qty)
                if take > 0:
                    qty, value = qty + take, value + take * float(f.price)
            if qty <= 1e-9:
                continue
            price = round(value / qty, 6)
            out = self.repo.reduce_trade(tid, qty, price, exit_reason="exit")
            if not out:
                continue
            booked.append({"id": tid, "symbol": sym, "qty": qty, "price": price})
            log.warning("booked %s shares of %s sold by an exit that was called off after filling in part (@ %.4f) - "
                        "the record now matches the %s shares held", f"{qty:,.0f}", sym, price, f"{abs(now_held):,.0f}")
            self._publish("trade.reduced", trade=out, reason="an exit that filled in part, booked from the broker's fills",
                          qty=qty, price=price)
        return booked

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
        self._publish("account.snapshot", state=self.snapshot())
        if out.get("ok"):
            status = str(out.get("status") or "")
            tail = "" if status == "FILLED" else f" ({status.lower()})"
            out["note"] = f"Exit sent for {row['qty']:,.0f} {symbol} shares that had no record{tail}."
        return out

    # ------------------------------------------------------------------ #
    #  A share count that disagrees: the warning's Fix button            #
    # ------------------------------------------------------------------ #
    #: what sent an execution a fix can book, in the preview's words
    EXEC_SOURCES = {"stop": "its stop order", "target": "its target order", "exit": "an exit the app sent",
                    "other": "an order from outside the app"}

    def mismatch_preview(self, symbol: str) -> Dict[str, Any]:
        """What the Fix button on a share-count warning shows before anything changes: what the open record and
        the account hold, the broker's executions of the stock no record has booked, the price and reason the
        shares the account no longer holds would be booked at, and whether each fix is allowed now - with the
        reason when it isn't. The account is read afresh: a warning can be minutes old."""
        return self._mismatch(symbol, self._refresh_account())

    def fix_mismatch(self, symbol: str, action: str, expect: Optional[Mapping[str, Any]] = None,
                     operator: str = "operator") -> Dict[str, Any]:
        """One of the Fix button's two choices, for a record holding more shares than the account, the same way
        round. ``match`` books the shares the account no longer holds off the record - at the broker's executions
        of them, or the estimate the preview stated - so the record matches the account, and the next protect
        pass sizes the stop at the broker from it. ``close`` does that, then sends a market exit for what's left;
        the record closes when it fills. The counts are read again first: ``expect`` is what the preview showed
        ({recorded, held}), and when either has changed nothing is done. Only the record is changed."""
        if action not in ("match", "close"):
            return {"ok": False, "reason": "Choose 'match' or 'close'."}
        with self._fix_lock:
            m = self._mismatch(symbol, self._refresh_account())
            if any(expect and expect.get(k) is not None and abs(float(expect[k]) - m[k]) > 1e-6
                   for k in ("recorded", "held")):
                return {"ok": False, "changed": True, "preview": m,
                        "reason": (f"The counts changed since the preview: the record holds {m['recorded']:,.0f} and "
                                   f"{m['venue_label']} {m['held']:,.0f} now. Open Fix again to see them.")}
            allowed = m["actions"][action]
            if not allowed["ok"]:
                return {"ok": False, "reason": allowed["reason"], "preview": m}
            tid, b = m["records"][0]["id"], m["booking"]
            out = self.repo.reduce_trade(tid, b["qty"], b["price"], exit_reason=b["reason"],
                                         commission=b["commission"], **b["after"])
            if not out or out.get("status") != "OPEN":
                return {"ok": False, "reason": "The record couldn't be changed - it may have closed just now."}
            log.warning("share counts fixed by %s (%s): booked %s %s shares off %s at %.4f (%s; %s) - it now holds %s, "
                        "as %s does", operator, action, f"{b['qty']:,.0f}", symbol, tid, b["price"], b["reason"],
                        b["basis"], f"{m['held']:,.0f}", m["venue_label"])
            self._publish("trade.reduced", trade=out, reason=b["reason"], qty=b["qty"], price=b["price"])
            # the warning goes now rather than at the next position check: the counts agree, or an exit is working
            self.position_check.mismatches = [x for x in self.position_check.mismatches if x.get("symbol") != symbol]
            note = (f"Booked {b['qty']:,.0f} {symbol} shares off the record at {b['price']:.2f} ({b['reason']}); "
                    f"it now holds {m['held']:,.0f}, as {m['venue_label']} does")
            res: Dict[str, Any] = {"ok": True, "trade": out,
                                   "booked": {k: b[k] for k in ("qty", "price", "reason", "estimated")}}
            if action == "match":
                res["note"] = note + (", and its stop at the broker follows the record."
                                      if self.executor.native_stops_on() else ".")
            else:
                sent = self.executor.close_trade(tid, reason="manual")
                res["exit"] = sent
                if sent.get("ok"):
                    status = str(sent.get("status") or "")
                    tail = "" if status == "FILLED" else f" ({status.lower()})"
                    res["note"] = note + f". Exit sent for the rest{tail}."
                else:
                    res.update(ok=False, reason=note + ", but the exit wasn't sent: "
                               + (sent.get("reason") or "no reason given"))
                    log.warning("the exit after a share-count fix of %s wasn't sent: %s", symbol, sent.get("reason"))
        self._refresh_account()
        self._publish("account.snapshot", state=self.snapshot())
        return res

    def _mismatch(self, symbol: str, read: bool) -> Dict[str, Any]:
        """The Fix preview for ``symbol``, from the account as last read (``read``: whether that read just worked)."""
        where, acc, ex = venue_label(self._venue), self._account, self.executor
        mine = [t for t in self._positions_here() if t["symbol"] == symbol]
        recorded = sum((1.0 if t["side"] == "LONG" else -1.0) * abs(float(t.get("quantity") or 0.0)) for t in mine)
        pos = acc.position(symbol) if acc is not None else None
        held = float(pos.quantity) if pos is not None else 0.0
        if not mine:
            kind = "no-record"
        elif abs(held - recorded) < 1e-6:
            kind = "agree"
        elif abs(held) < 1e-9:
            kind = "none"
        elif (held > 0) != (recorded > 0):
            kind = "other-side"
        else:
            kind = "more" if abs(held) > abs(recorded) else "fewer"
        t = mine[0] if len(mine) == 1 and kind == "fewer" else None
        missing = abs(recorded) - abs(held) if kind == "fewer" else 0.0
        executions = self._unbooked_exits(t) if t is not None else []
        mark = self._marks().get(symbol)
        last = (mark[0] if mark else 0.0) or (float(pos.market_price or 0.0) if pos is not None else 0.0)
        booking = (self._mismatch_booking(t, missing, executions, last)
                   if t is not None and executions is not None else None)

        # why neither fix may run now - the first reason that applies
        why = ""
        if ex is None or self._broker is None or not self._broker.is_connected:
            why = f"{where} isn't connected, so its count can't be checked."
        elif not read:
            why = f"{where} didn't answer for the account just now - try again in a moment."
        elif self.quit_state:
            why = "The app is quitting - nothing but its exits can change until that's done."
        elif kind == "no-record":
            why = f"The app has no open record of {symbol} on {where}."
        elif kind == "agree":
            why = "The counts agree now - there's nothing to fix."
        elif kind == "none":
            why = (f"{where} shows no {symbol} shares. The position check books the record closed from the broker's "
                   "fills, or removes it, by itself once it has seen that twice in a row.")
        elif kind == "other-side":
            why = f"{where} holds {symbol} the other way round - check the position there; the app won't guess."
        elif kind == "more":
            why = (f"{where} holds {abs(held) - abs(recorded):,.0f} shares more than the record. They're listed under "
                   "Shares without a record in Open positions, with their own Exit.")
        elif t is None:
            why = (f"{symbol} has {len(mine)} open records ({', '.join(x['id'] for x in mine)}). The fix works on a "
                   "stock with one - check them in Open positions.")
        elif t.get("pair_id"):
            why = "It's one leg of a pair trade - the pair desk closes both legs together (Pairs tab)."
        elif symbol in ex.symbols_in_flight() or t["id"] in ex.pending_exit_trade_ids():
            why = (f"An order for {symbol} is still working at {where} - its fills change the counts. Try again "
                   "once it's done.")
        elif executions is None:
            why = f"{where} didn't say what it executed just now - try again in a moment."
        else:
            filled = ex.resting_filled(t["id"])
            if filled is None:
                why = f"The stop and target resting at {where} couldn't be checked just now - try again in a moment."
            elif filled > 1e-9:
                why = (f"The stop or target resting at {where} has filled {filled:,.0f} shares and is still working - "
                       "the app books them itself when it finishes.")
            elif booking is None:
                why = (f"There's no price to book the {missing:,.0f} missing shares at: {where} reports no executions "
                       "for them and the app has no last price.")
        shut = "" if why else self._exits_cant_fill()
        close_why = why or ("The market is closed, so an exit can't fill now. Match the record now, and exit what's left "
                            "from Open positions once the regular session opens." if shut else "")
        return {
            "ok": True, "symbol": symbol, "kind": kind, "venue_label": where, "live": self.mode == "live",
            "side": mine[0]["side"] if mine else "", "held_side": "LONG" if held > 0 else "SHORT" if held < 0 else "",
            "recorded": abs(recorded), "held": abs(held), "missing": missing,
            "records": [{k: x.get(k) for k in ("id", "quantity", "entry_price", "stop_price", "target_price")}
                        for x in mine],
            "executions": [{**{k: r[k] for k in ("qty", "price", "at", "source", "order_id")},
                            "label": self.EXEC_SOURCES[r["source"]]} for r in executions or []],
            "booking": booking, "last_price": last or None,
            "actions": {"match": {"ok": not why, "reason": why}, "close": {"ok": not close_why, "reason": close_why}},
        }

    def _unbooked_exits(self, t: Mapping[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """The exit-side executions the broker reports for a record's stock since it was entered that no record
        has booked, oldest first, each with what sent it (EXEC_SOURCES). Another trade's orders and the ones
        closing shares without a record are left out, and the shares the record itself booked off today are
        taken to be the first of its own orders' executions (IBKR reports only today's). None when the broker
        couldn't say."""
        get = getattr(self._broker, "get_fills", None)
        if not callable(get):
            return []
        try:
            fills = list(get(t["symbol"]) or [])
        except Exception:  # noqa: BLE001
            log.debug("fills for %s unavailable", t["symbol"], exc_info=True)
            return None
        tid, entered, today = t["id"], _utc(t.get("entry_time")), clock.session_date()
        exit_side = Side.SHORT if t["side"] == "LONG" else Side.LONG
        own = {f"{STOP_TAG}{tid}": "stop", f"{TARGET_TAG}{tid}": "target", f"exit:{tid}": "exit"}
        booked = sum(float(f.get("quantity") or 0.0) for f in (self.repo.trade_record(tid) or {}).get("fills") or []
                     if f.get("leg") == "EXIT" and _utc(f.get("ts")) and clock.session_date(_utc(f["ts"])) == today)
        out: List[Dict[str, Any]] = []
        for f in sorted(fills, key=lambda f: _utc(f.ts) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)):
            tag, qty, at = getattr(f, "tag", "") or "", float(f.quantity), _utc(f.ts)
            if f.side != exit_side or qty <= 0 or (entered and at and at < entered - dt.timedelta(minutes=1)):
                continue
            source = own.get(tag) or ("" if tag.startswith((STOP_TAG, TARGET_TAG, "exit:", "unwind:")) else "other")
            if not source:
                continue                             # another trade's order, or one closing shares without a record
            fee = float(f.commission or 0.0)
            if source != "other" and booked > 1e-9:
                skip = min(booked, qty)
                booked, fee, qty = booked - skip, fee * (qty - skip) / qty, qty - skip
                if qty <= 1e-9:
                    continue
            out.append({"qty": qty, "price": float(f.price), "commission": fee, "source": source,
                        "order_id": str(f.order_id), "at": at.isoformat() if at else None})
        return out

    def _mismatch_booking(self, t: Mapping[str, Any], missing: float, executions: List[Dict[str, Any]],
                          last: float) -> Optional[Dict[str, Any]]:
        """The price and reason the shares a record holds beyond the account are booked at: the broker's executions
        of them, oldest first, and the last price for any it no longer reports (IBKR keeps only today's) - said
        so. None when some are left with neither."""
        qty = value = fees = 0.0
        sources = set()
        for r in executions:
            take = min(r["qty"], missing - qty)
            if take <= 1e-9:
                break
            qty, value, fees = qty + take, value + take * r["price"], fees + r["commission"] * take / r["qty"]
            sources.add(r["source"])
        rest = max(0.0, missing - qty)
        if rest > 1e-9 and last <= 0:
            return None
        after: Dict[str, float] = {}
        if rest > 1e-9 or len(sources) != 1:
            reason = "closed-outside"
        elif sources == {"stop"}:
            # the stop order rests at the record's stop: moved from where it began, it was a trailing stop
            reason = stop_exit_reason(t.get("initial_stop_price"),
                                      float(t.get("stop_price") or t.get("initial_stop_price") or 0.0))
        elif sources == {"target"}:
            # the scale-out's part came off at the first target: the rest gets the stop and target it would have
            reason = "target-1"
            plan = scale_out_plan(t, self.settings.config.exit_manager)
            after = dict(plan[1]) if plan else {}
        else:
            reason = "closed-outside"
        whose = self.EXEC_SOURCES[next(iter(sources))] if len(sources) == 1 else "several orders"
        if rest <= 1e-9:
            basis = f"the broker's executions: {qty:,.0f} at {value / qty:.2f}, from {whose}"
        elif qty > 0:
            basis = (f"the broker's executions cover {qty:,.0f} at {value / qty:.2f} (from {whose}); the other "
                     f"{rest:,.0f} are estimated at the last price, {last:.2f} - it reports only today's executions")
        else:
            basis = (f"estimated at the last price, {last:.2f} - the broker reports no executions for these shares "
                     "(IBKR keeps only today's)")
        return {"qty": missing, "price": round((value + rest * last) / missing, 4), "commission": round(fees, 2),
                "reason": reason, "after": after, "estimated": rest > 1e-9, "basis": basis}

    def refresh_account_now(self) -> Dict[str, Any]:
        """The dashboard's Refresh. When IB Gateway came up after the app it connects right away
        (the background retry would get there within a minute or two; this doesn't wait), then
        reads the account, positions and orders again and says what happened - a read that
        didn't come back is reported, not passed off as a refresh."""
        connected_now = False
        if not self.connections.connected:
            self.connections.refresh()
            connected_now = self._retry_connection(force=True)
        read = self._refresh_account()
        self._check_arm()
        if self.executor:
            try:
                self.executor.sync_open_orders()
            except Exception:  # noqa: BLE001
                log.debug("order sync failed", exc_info=True)
        priced = self.refresh_prices() if read else 0
        state = self.snapshot()
        self._publish("account.snapshot", state=state)
        if read and connected_now:
            return {"ok": True, "state": state, "connected": True,
                    "note": f"Connected to {venue_label(self._venue)} and read the account."}
        if read and not self.connections.connected:                 # the simulator answered; IBKR is still away
            why = " ".join(self.connections.blockers) or "the app keeps trying by itself."
            return {"ok": True, "state": state, "connected": False, "warn": True,
                    "note": f"Re-read {venue_label(self._venue)}. IB Gateway isn't reachable yet: {why}"}
        if read:
            return {"ok": True, "state": state, "connected": True,
                    "note": "Account, positions and orders re-read" + (f", and {priced} prices fetched." if priced else ".")}
        if self.connections.connected:
            reason = "IBKR didn't answer in time - keeping the last snapshot; the app tries again by itself."
        elif self.connections.blockers:
            reason = "IB Gateway isn't reachable yet: " + " ".join(self.connections.blockers)
        else:
            reason = "Not connected yet - the app keeps trying by itself."
        return {"ok": False, "state": state, "reason": reason, "connected": self.connections.connected}

    # ------------------------------------------------------------------ #
    #  Plays and positions                                               #
    # ------------------------------------------------------------------ #
    def watch_play(self, play_id: str) -> None:
        """The operator opened a play (the dashboard's assess): its stock streams ahead of the other plays for
        a while (StreamManager.prefer), so an Execute click is priced off the stream. Only the web route calls
        it - Autopilot assesses every play it gets to, and would crowd out the one on the screen. Nothing waits
        for the stream: until it ticks, the entry check asks for a snapshot as before."""
        p = self.board.get(play_id)
        if p is not None and p.status is PlayStatus.PROPOSED:
            self.md.streams.prefer(p.symbol)
            self._stream_wake.set()

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
                           risk_pct=self._play_risk_pct(p), risk_why=self.strategy_risk_why(p.strategy),
                           size_factor=self.size_factor)
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
            reasons.append("the position size factor is 0, so new positions are sized at nothing"
                           if self.size_factor <= 0
                           else f"{p.symbol} already takes up the {cfg.risk.max_symbol_pct_of_equity:.0f}% of equity "
                           "allowed in one stock" if "max exposure per stock" in sizing.caps_hit
                           else self._too_thin_reason(p, cfg.risk) if liquidity_cap(p, cfg.risk) == 0
                           else self._no_room_reason(p) if "trading capital" in sizing.caps_hit
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
                "caps": list(sizing.caps_hit),
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
            seen: Dict[str, Any] = {}
            chased = self._chase_check(p, pre["order_plan"], seen)
            if chased:
                return {"ok": False, "reason": chased}

            p.status = PlayStatus.ACCEPTED
            p.evidence["at_entry"] = at_entry = self._entry_context(p, operator)
            context = play_features(p, now=clock.now_ny(), market=self.regime.context(), at_entry=at_entry,
                                    by=operator)
            self.repo.record_play(p)
            self.repo.set_play_status(p.id, p.status.value, operator)
            try:
                out = self.executor.execute_play(p, self._account, plan=pre["order_plan"], context=context,
                                                 decision=seen or None)
            except Exception as e:  # noqa: BLE001
                p.status = PlayStatus.ERROR
                self.repo.set_play_status(p.id, p.status.value, operator)
                log.exception("execute_play crashed")
                self._publish("play.decided", play_id=p.id, decision="error", result={"reason": str(e)})
                return {"ok": False, "reason": f"execution error: {e}"}
            if not out.get("ok"):
                p.status = PlayStatus.PROPOSED            # let them try again once the reason clears
                self.repo.set_play_status(p.id, p.status.value, operator)
            # sent: the executor has saved it SUBMITTED (or the fill FILLED) - and the sync loop may already
            # have saved how it ended, which a write here would overwrite
            self._publish("play.decided", play_id=p.id, decision="approved", result=out, play=self._decorate(p))
            self._stream_wake.set()                       # a working entry or a new position streams ahead of the plays
            if operator == "autopilot":
                # on the scan thread: its next play, and the next pass, read the account this order changed
                self._refresh_account()
            else:
                # a click waits for its reply, and the order is out: the snapshot loop reads the account and
                # sends it a moment later - the loop is woken rather than a thread started, so no second read
                # runs alongside its own
                self._snapshot_wake.set()
            self._day_changed(now=True)                   # a restart mustn't offer this setup again today
            return {"ok": out.get("ok", False), **out}

    @staticmethod
    def _too_thin_reason(p: Play, risk) -> str:
        """Why a play sized to nothing under the liquidity cap: the stock trades too few shares a day."""
        return (f"{p.symbol} is too thin to trade: it usually trades {float(p.evidence['adv_shares']):,.0f} "
                f"shares a day, and the liquidity cap of {float(risk.max_adv_pct):g}% of that "
                "(risk.max_adv_pct) is less than one share")

    def _no_room_reason(self, p: Play) -> str:
        """Why a play sized to nothing under the trading capital: its kind's share is full, or all of it is."""
        state = self.capital_state() or {}
        split = state.get("split") or {}
        if split.get("on"):
            kind = "day" if capital.kind_of(p.timeframe) == capital.DAY else "swing"
            part = split.get(kind) or {}
            if part.get("available", 1.0) <= 0 < state.get("available", 0.0):
                return (f"{kind} trades already hold their {part.get('pct', 0):g}% share of the trading capital "
                        "(the day / swing split) - no room for another")
        return "the trading capital is fully invested - no room for another position"

    def _score_plays(self, plays) -> None:
        """The learned model's odds on each fresh play (research/model.py), kept in its evidence so
        they are logged with it - in shadow mode that record is how the model earns trust."""
        try:
            if self.model.card is None:
                return
            now, market = clock.now_ny(), self.regime.context()
            for p in plays:
                score = self.model.score(play_features(p, now=now, market=market))
                if score:
                    p.evidence["model"] = score
        except Exception:  # noqa: BLE001
            log.debug("scoring the plays failed", exc_info=True)

    def model_card(self) -> Optional[Dict[str, Any]]:
        """The trained model's card, trimmed for the dashboard."""
        card = self.model.card
        if not card:
            return None
        return {k: card.get(k) for k in ("id", "trained_at", "rows", "by_source", "usable", "verdict")}

    def _play_risk_pct(self, p: Play) -> Optional[float]:
        """The risk a play is sized with: the strategy's half-Kelly share, scaled by the learned
        model's odds when Autopilot is set to size by them and the model is usable (AFML ch. 10)."""
        pct = self.strategy_risk_pct(p.strategy)
        score = (p.evidence or {}).get("model") or {}
        if self.autopilot.model_mode == "size" and score.get("usable") and score.get("p") is not None:
            base = pct if pct is not None else float(self.settings.config.risk.max_risk_per_trade_pct)
            return round(base * risk_factor(float(score["p"])), 4)
        return pct

    def _chase_check(self, p: Play, plan: Dict[str, Any], seen: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """The last look before an order goes out, at the live quote. Returns why the entry is
        refused, if it is; ``seen`` is filled with the quote (mid, bid, ask, spread_bps, live), which
        the fill is later measured against - Harris's implementation shortfall.

        * Harris: the spread is the price of immediacy, paid going in and again coming out. On live
          quotes an entry is refused when the spread is more than ``execution.max_spread_r`` of the
          distance to the stop.
        * Aziz: never chase. Once the price has run past the play's entry by more than
          ``execution.max_chase_r`` of the distance to the stop, the reward:risk the play was judged
          on is gone. Within that, a limit entry is priced off the quote so it fills now instead of
          waiting for the price to come back through the entry, which is the move failing. A
          pullback under the entry is not a chase.

        With no price source attached there is nothing to check (and no plays to take). With one
        attached, an entry whose price can't be read is refused - an order is never sent blind.

        The quote is quote()'s: a stream's latest when it ticked in the last 2 s, else a snapshot - an
        entry never waits for a stream. Where it came from and how old it was are logged, and kept in
        ``seen`` as quote_source / quote_age_ms (the trade record keeps only the mid and the spread)."""
        cfg = self.settings.config.execution
        risk = abs(float(p.entry) - float(p.stop))
        if risk <= 0 or not self.md.attached:
            return None
        blind = (f"no current price for {p.symbol} - not entering blind"
                 + (f" ({self.md.refused})" if self.md.refused else ""))
        try:
            q = self.md.quote(p.symbol)
            px = float(q.last or q.mid or 0.0)
            live = not bool(getattr(self.md, "delayed", True))
        except Exception as e:  # noqa: BLE001
            log.warning("no quote for %s at the entry check (%s) - the entry is refused", p.symbol, e)
            return blind
        if px <= 0:
            return blind
        bid, ask = float(q.bid or 0.0), float(q.ask or 0.0)
        spread = ask - bid if 0 < bid < ask else 0.0
        mid = (bid + ask) / 2.0 if spread else px
        source, age_ms = _quote_origin(q)
        log.info("entry check for %s on a %s quote %s old: last %.4f, bid %.4f, ask %.4f (%s data)", p.symbol,
                 source, f"{age_ms} ms" if age_ms is not None else "of unknown age", px, bid, ask,
                 "live" if live else "delayed")
        if seen is not None:
            seen.update(mid=round(mid, 4), bid=bid or None, ask=ask or None, live=live,
                        spread_bps=round(spread / mid * 1e4, 2) if spread and mid else None,
                        quote_source=source, quote_age_ms=age_ms)
        max_spread = float(getattr(cfg, "max_spread_r", 0.0) or 0.0)
        if live and max_spread > 0 and spread / risk > max_spread:
            return (f"the spread ({bid:.2f} x {ask:.2f}) is {spread / risk:.2f}R of this trade's risk - too dear to "
                    f"cross (execution.max_spread_r {max_spread:g})")
        max_r = float(getattr(cfg, "max_chase_r", 0.0) or 0.0)
        if max_r <= 0:
            return None
        sign = 1.0 if p.side is Side.LONG else -1.0
        run = (px - float(p.entry)) * sign / risk
        if run > max_r:
            return (f"the price ({px:.2f}) has run {run:.2f}R past the entry {p.entry:.2f} - not chasing "
                    f"(execution.max_chase_r {max_r:g})")
        if run > 0 and plan.get("order_type") == "LIMIT" and plan.get("limit_price"):
            offset = float(getattr(cfg, "limit_offset_bps", 5.0)) / 1e4
            plan["limit_price"] = round(px * (1 + sign * offset), 2)
        return None

    def reject_play(self, play_id: str, operator: str = "operator") -> Dict[str, Any]:
        p = self.board.get(play_id)
        row = self.repo.get_play(play_id)
        sent = (p is not None and p.status in _ACTED_ON) or (
            row is not None and row.get("status") in {s.value for s in _ACTED_ON})
        if sent:
            return {"ok": False, "reason": "an order has already gone out for this play - it can't be dismissed"}
        if p:
            p.status = PlayStatus.REJECTED
            if row is None:
                self.repo.record_play(p)                  # found by a quick re-check, which isn't logged
        self.repo.set_play_status(play_id, PlayStatus.REJECTED.value, operator)
        self._day_changed(now=True)
        self._publish("play.decided", play_id=play_id, decision="rejected")
        return {"ok": True}

    def close_position(self, trade_id: str, reason: str = "manual") -> Dict[str, Any]:
        """Exit one position at the market - always allowed, including while quitting."""
        out = self.executor.close_trade(trade_id, reason=reason)
        self._refresh_account()
        self._publish("account.snapshot", state=self.snapshot())
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
        self._publish("account.snapshot", state=self.snapshot())
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
        self._settings_changed()
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
        self._publish("account.snapshot", state=self.snapshot())
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
            self._publish("capital.updated", capital=self.capital_state())
        self._settings_changed()                           # Autopilot's types and the split follow the filters
        self._publish("filters.updated", filters=new.as_dict())
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
        self._settings_changed()
        self._publish("strategies.updated", strategies=self.strategy_state())
        if rescan:
            self._queue_scan(rescan)

    # ------------------------------------------------------------------ #
    #  Views                                                             #
    # ------------------------------------------------------------------ #
    def _decorate(self, p: Play, records: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
        """A play as the dashboard shows it. ``records``: the setups' records already read for this
        board of plays (_play_record)."""
        row = p.to_row()
        row["executable_hint"] = p.suggested_qty > 0 and self._armed
        seen = self.md.last_seen(p.symbol)
        row["last_price"], row["last_at"] = (round(seen[0], 4), seen[1].isoformat()) if seen else (None, None)
        row["record"] = self._play_record(p.strategy, {} if records is None else records)
        return self.autopilot.decorate_play(row, p)

    #: what the dashboard reads of a play on the board - the table, the notes, the orders, the chart and the
    #: Autopilot strip - and all the board's push and /api/plays send. The explanation and the evidence
    #: behind a play are most of its size and only its hover and the detail panel show them, so those load
    #: the play whole (play_row, assess_play). Autopilot's verdict and the replay record go whole.
    ROW_FIELDS = ("id", "symbol", "sector", "side", "strategy", "kind", "timeframe", "entry", "stop", "targets",
                  "reward_risk", "confidence", "score", "rationale", "suggested_qty", "dollar_risk",
                  "extended_hours_ok", "status", "trade_id", "noise", "confirmations", "created_at", "expires_at",
                  "last_price", "last_at", "autopilot", "record")
    #: the evidence the table reads: the signals' nudge to the score and why, and the expected R
    ROW_EVIDENCE = ("signal_nudge", "signal_reasons", "expected_r")

    @classmethod
    def _slim(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        """A decorated play cut to what the board's push sends (ROW_FIELDS)."""
        evidence = row.get("evidence") or {}
        return {**{k: row.get(k) for k in cls.ROW_FIELDS},
                "evidence": {k: evidence[k] for k in cls.ROW_EVIDENCE if k in evidence}}

    def _play_record(self, strategy: str, records: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """The setup's replayed record over the trades Autopilot would take, for the play's chip: its
        average and trades, the held-out sessions', what its wins average - set beside the play's expected
        R, which counts a win at the full target - and whether it is proven, in proof_missing's words.
        Read once per setup into ``records``, which a whole board of plays shares."""
        if strategy not in records:
            rec = self.strategy_record(strategy) or {}
            held = rec.get("out_of_sample") or {}
            why = self.autopilot.proof_missing(strategy, rec)
            records[strategy] = {
                "trades": int(rec.get("trades", 0)), "expectancy_r": rec.get("expectancy_r"),
                "win_rate": rec.get("win_rate"), "avg_win_r": rec.get("avg_win_r"),
                "held_out_trades": int(held.get("trades", 0)), "held_out_r": held.get("expectancy_r"),
                "proven": why is None, "why": why,
            }
        return records[strategy]

    #: a price the app fetched this recently is newer than the broker's portfolio mark, which IBKR updates
    #: every few minutes; the exit manager keeps every open position's this fresh
    APP_MARK_S = 120.0

    def refresh_prices(self) -> int:
        """A fresh price for every play on the board and every open position, in one batched request -
        the dashboard's Refresh. Pre-market and after-hours trades count, so outside regular hours they
        still move; they're only shown (MarketData.refresh_prices). The plays go out again with them.
        Returns how many came back."""
        if not self.md.attached:
            return 0
        plays = [p.symbol for p in self.board.ranked()[:self.BOARD_ROWS] if p.status not in _ACTED_ON]
        held = [p.symbol for p in (self._account.positions if self._account else [])]
        held += [t["symbol"] for t in self._positions_here()]
        symbols = list(dict.fromkeys(held + plays))                  # the positions first, should the list be cut
        try:
            n = self.md.refresh_prices(symbols, self.scanner.con_ids(symbols))
        except Exception as e:  # noqa: BLE001
            log.warning("prices couldn't be refreshed: %s", e)
            return 0
        self._publish_plays()
        return n

    def price_of(self, symbol: str) -> Dict[str, Any]:
        """One stock's latest price for a panel about it, pre-market and after-hours included, with when
        it's from and the session it traded in - GET /api/price/{symbol}. Fetched when the broker wasn't
        asked for it in the last 15 s (MarketData.price_now). Shown only: nothing the app acts on reads it."""
        if not self.md.attached:
            return {"ok": False, "symbol": symbol, "reason": "IB Gateway isn't connected, so there's no price."}
        seen = self.md.price_now(symbol, self.scanner.con_ids([symbol]).get(symbol))
        if seen is None:
            return {"ok": False, "symbol": symbol, "reason": f"No recent price for {symbol}."}
        price, at, age = seen
        return {"ok": True, "symbol": symbol, "price": round(price, 4), "at": at.isoformat(), "age_s": round(age, 1),
                "session": SESSION_WORDS[clock.current_session(at)]}

    def _marks(self) -> Dict[str, Tuple[float, str]]:
        """The app's own price for each stock held, where it's fresher than the broker's mark."""
        out: Dict[str, Tuple[float, str]] = {}
        for pos in (self._account.positions if self._account else []):
            seen = self.md.last_seen(pos.symbol)
            if seen is not None and seen[2] <= self.APP_MARK_S:
                out[pos.symbol] = (seen[0], seen[1].isoformat())
        return out

    def _venue_state(self) -> Dict[str, Any]:
        plan = plan_venue(self.mode, self.paper_platform)
        status = self.connections.session_status()
        if status:
            # the account id is a secret (the Connections panel shows only its end), and this goes to
            # every open tab in every snapshot: it gets the same
            status = {**status, "account": secrets_store.mask(status.get("account")) or None}
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
            "positions": views.positions(acc, self._marks()),
            "untracked": self.untracked_positions(),
            "mismatches": self.position_check.mismatches,
            "day_trades_5d": self.repo.count_day_trades(5),
            "day_trade_limit": cfg.account.max_day_trades_under_threshold,
            "pnl": self._pnl(),
            "scan": self.scan_status(),
        }

    def current_plays(self, full: bool = False) -> List[Dict[str, Any]]:
        """The board's plays as the push sends them (_slim), or whole with ``full``."""
        records: Dict[str, Dict[str, Any]] = {}
        rows = [self._decorate(p, records) for p in self.board.ranked()]
        return rows if full else [self._slim(r) for r in rows]

    def play_row(self, play_id: str) -> Optional[Dict[str, Any]]:
        """One play whole - its explanation and all the evidence - for the row's hover. None once it has
        left the board."""
        p = self.board.get(play_id)
        return None if p is None else self._decorate(p)


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


def _quote_origin(q: Any) -> Tuple[str, Optional[int]]:
    """Where a quote came from - "stream", "snapshot", or "candle" for a price read off a candle (Quote.source
    empty) - and how many milliseconds old it is by its own time; None when it has no time."""
    at = _utc(getattr(q, "ts", None))
    age = None if at is None else max(0, round((dt.datetime.now(dt.timezone.utc) - at).total_seconds() * 1000))
    return getattr(q, "source", "") or "candle", age


def _order_signature(orders: List[Dict[str, Any]]) -> tuple:
    """What has to change for the dashboard to be told about the working orders. A countdown counts when it
    starts or stops, not by its time: the part-fill cut is worked out again at each look, and the first fill
    it runs from can be noted by the order sync after this loop has already sent the fill."""
    keys = ("order_id", "status", "filled", "remaining", "limit_price", "stop_price", "purpose", "trade_id")

    def row(o: Dict[str, Any]) -> tuple:
        clocks = (o.get("expires_at") is not None, o.get("cut_at") is not None, bool(o.get("calling_off")))
        return tuple(o.get(k) for k in keys) + clocks

    return tuple(sorted((row(o) for o in orders), key=lambda r: str(r[0])))
