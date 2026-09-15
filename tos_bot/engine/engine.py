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
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from .. import secrets_store
from ..brokers import get_broker
from ..brokers.base import BrokerAdapter
from ..brokers.venues import (
    IBKR_STEPS, PAPER_PLATFORMS, ROUTE_LABELS, normalize_platform, plan_venue, venue_id, venue_label,
)
from ..config import DATA_DIR, RUNTIME_PATH, Secrets, Settings, get_settings
from ..core.enums import PlayStatus, Timeframe
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
from ..signals.book import BoostSettings, SignalBook
from ..signals.service import SignalService
from ..signals.store import SignalStore
from ..scanner.noise import LABELS as NOISE_LABELS, NoiseSettings
from ..scanner import schedule
from ..scanner.filters import TradeFilters
from ..scanner.scanner import Scanner
from ..scanner.schedule import ScanSettings
from ..strategies.registry import REGISTRY, build_strategies, strategy_catalog
from ..util import clock
from ..util.logging_setup import setup_logging
from ..util.net import port_is_open
from . import capital, views
from .board import PlayBoard
from .connections import Connections
from .reconcile import PositionCheck
from .runtime import RuntimeFile, load_capital, load_filters, load_strategy_overrides

log = logging.getLogger(__name__)

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
        sc = cfg.scanner
        self.scan_settings = ScanSettings.load(saved.get("scan"), ScanSettings(
            premarket_time=sc.premarket_time, cycle_minutes=sc.cycle_minutes,
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
        self.executor: Optional[Executor] = None
        self.exit_manager: Optional[ExitManager] = None
        self.pdt: Optional[PdtGuard] = None
        self._broker: Optional[BrokerAdapter] = None        # where orders go
        self._venue = "paper"                                # venue id stamped on new trades
        self._broker_since = 0.0
        self._live_blockers: List[str] = []
        self._connect_retry_at = 0.0
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
        self._last_cycle_at = float("-inf")
        self._last_fast_at = float("-inf")
        self._scan_retry_at = 0.0

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
            ("signals-loop", self._signals_loop))]
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
            "scan": self.scan_settings.as_dict(), "autopilot": self.autopilot.to_runtime(),
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

    # ------------------------------------------------------------------ #
    #  Strategy replay                                                   #
    # ------------------------------------------------------------------ #
    def start_replay(self, sessions: int = 20, swing_sessions: int = 120) -> Dict[str, Any]:
        """Replay the strategies over recent candles in the background (see research/)."""
        if not self.md.attached:
            return {"ok": False, "reason": "IB Gateway isn't connected, so there are no candles to replay."}
        symbols = replay_symbols(self.scanner.watchlist)
        if not symbols["swing"]:
            return {"ok": False, "reason": "Run the full scan first - the replay uses the stocks on its watchlist."}
        cfg = self.settings.config
        return self.replay.start(
            strategies=self.scanner.strategies, source=self.md.source, daily_frame=self.md.daily_frame,
            intraday_symbols=symbols["intraday"], swing_symbols=symbols["swing"],
            sessions=max(5, min(60, int(sessions))), swing_sessions=max(20, min(250, int(swing_sessions))),
            settings=ReplaySettings.from_exit_rules(cfg.exit_manager), noise=NoiseSettings.from_config(cfg.noise),
            con_ids=self.scanner.con_ids(symbols["intraday"]))

    def replay_state(self) -> Dict[str, Any]:
        return self.replay.state(self.autopilot.skip_noise, self.autopilot.min_confirmations)

    def strategy_record(self, key: str) -> Optional[Dict[str, Any]]:
        """A strategy's replayed record over the trades Autopilot would have taken."""
        return self.replay.records(self.autopilot.skip_noise, self.autopilot.min_confirmations).get(key)

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
        if wl is None or not clock.is_market_open(now):
            return None
        if mono - self._last_cycle_at >= self.scan_settings.cycle_minutes * 60:
            return "cycle"
        if self._autopilot_day_active() and mono - self._last_fast_at >= self.settings.config.scanner.fast_cycle_seconds:
            return "fast"
        return None

    def _queue_scan(self, kind: str) -> None:
        with self._scan_lock:
            if self._scan_request != "full":           # a queued full scan covers a cycle too
                self._scan_request = kind
        self._scan_wake.set()

    def _run_scan(self, kind: str) -> None:
        self._scan_running = {"kind": kind, "started_at": clock.now_ny().isoformat()}
        BUS.publish("scan.started", kind=kind)
        try:
            self._refresh_account()
            self.scanner.account = self.sizing_account()
            result = (self.scanner.run_full(self.scan_settings) if kind == "full"
                      else self.scanner.run_cycle(fast=kind == "fast"))
        except NoDataSource as e:
            self._scan_failed(kind, str(e))
            return
        except Exception as e:  # noqa: BLE001
            log.exception("%s scan failed", kind)
            self._scan_failed(kind, f"The {kind} scan failed: {e}")
            return
        finally:
            self._scan_running = None

        mono = time.monotonic()
        self._scan_retry_at = 0.0
        if kind == "full":
            self._last_cycle_at = float("-inf")         # in the session, a cycle follows straight away
        else:
            self._last_fast_at = mono
            if kind == "cycle":
                self._last_cycle_at = mono
        sizing = self.sizing_account()
        if sizing is not None:
            exposure = self.exposure_by_symbol()
            for p in result.plays:
                size_play(p, sizing, self.settings.config.risk, symbol_notional=exposure.get(p.symbol, 0.0))
        self.board.replace(result.plays, None if kind == "full" else result.symbols)
        self._last_scans[kind] = result.summary()
        try:
            self.repo.record_scan(result, keep_rejected=self.settings.config.database.record_rejected_plays)
        except Exception:  # noqa: BLE001
            log.exception("could not save the scan")
        self._publish_plays()
        BUS.publish("watchlist.updated", **self.watchlist_state())
        if not self.quit_state:
            try:
                self.autopilot.consider(self.board.plays)
            except Exception:  # noqa: BLE001
                log.exception("autopilot pass failed")

    def _scan_failed(self, kind: str, reason: str) -> None:
        self._scan_retry_at = time.monotonic() + self.SCAN_RETRY_S
        log.warning("%s scan skipped: %s", kind, reason)
        BUS.publish("scan.failed", kind=kind, reason=reason)

    def request_scan(self, kind: str = "cycle") -> Dict[str, Any]:
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        if kind not in ("full", "cycle"):
            return {"ok": False, "reason": "scan must be 'full' or 'cycle'"}
        if kind == "cycle" and self.scanner.watchlist is None:
            kind = "full"
        self._queue_scan(kind)
        note = ("Full scan queued: every US stock gets ranked and today's hot list and sector buffers are rebuilt."
                if kind == "full" else "Rescanning the hot list and the next buffer names.")
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
            "running": self._scan_running,
            "last_full": self._last_scans.get("full"),
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
        account_age_s, connection_age_s = now - self._account_at, now - self._broker_since
        mine = [t for t in self._open_trades() if (t.get("broker") or "paper") == venue]
        held = {p.symbol: float(p.quantity) for p in acc.positions if abs(p.quantity) > 1e-9}
        gone = self.position_check.gone(
            venue, mine, held=set(held), busy=self.executor.pending_exit_trade_ids(),
            account_age_s=account_age_s, connection_age_s=connection_age_s, force=force)
        removed = [{"id": t["id"], "symbol": t["symbol"], "side": t["side"], "quantity": t["quantity"]}
                   for t in gone if self.repo.delete_trade(t["id"])]
        if removed:
            log.warning("removed %d trade record(s) no longer held at %s: %s", len(removed), venue,
                        ", ".join(r["symbol"] for r in removed))
            BUS.publish("trades.removed", trades=removed, venue=venue, venue_label=venue_label(venue))

        removed_ids = {r["id"] for r in removed}
        new = self.position_check.share_counts(
            venue, venue_label(venue), [t for t in mine if t["id"] not in removed_ids], held=held,
            in_flight=self.executor.symbols_in_flight(),
            account_age_s=account_age_s, connection_age_s=connection_age_s)
        for m in new:
            log.warning("share counts disagree: %s", m["note"])
        if new:
            BUS.publish("positions.mismatch", mismatches=new)
        return removed

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
        sizing = size_play(p, self.sizing_account() or acc, cfg.risk,
                           symbol_notional=self.exposure_by_symbol().get(p.symbol, 0.0))
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
        removed = self.board.keep_only(new.allows)
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
        self.board.keep_only(lambda p: p.strategy in active)
        self._publish_plays()
        BUS.publish("strategies.updated", strategies=self.strategy_state())
        if rescan:
            self._queue_scan(rescan)

    # ------------------------------------------------------------------ #
    #  Trading capital                                                   #
    # ------------------------------------------------------------------ #
    def _invested_usd(self) -> float:
        return capital.invested_usd(self._account, self._positions_here())

    def sizing_account(self) -> Optional[Account]:
        """The account as position sizing sees it - see capital.py."""
        acc, limit = self._account, self.capital.get(self._venue)
        if acc is None or not limit:
            return acc
        return capital.sizing_account(acc, limit, self._invested_usd())

    def capital_state(self) -> Optional[Dict[str, Any]]:
        if self._account is None:
            return None
        return capital.state(self._account, self._venue, venue_label(self._venue),
                             self.capital.get(self._venue), self._invested_usd())

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
        sizing = self.sizing_account()
        if sizing is not None:
            exposure = self.exposure_by_symbol()
            for p in self.board.plays.values():
                if p.status not in _ACTED_ON:
                    size_play(p, sizing, self.settings.config.risk, symbol_notional=exposure.get(p.symbol, 0.0))
        self._publish_plays()

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
            "mismatches": self.position_check.mismatches,
            "day_trades_5d": self.repo.count_day_trades(5),
            "day_trade_limit": cfg.account.max_day_trades_under_threshold,
            "pnl": self._pnl(),
            "scan": self.scan_status(),
        }

    def current_plays(self) -> List[Dict[str, Any]]:
        return [self._decorate(p) for p in self.board.ranked()]
