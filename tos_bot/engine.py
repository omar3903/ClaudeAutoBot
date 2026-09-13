"""The conductor.

Owns the broker connection, the built-in simulator, scanner, executor, risk
guard and the Schwab token watchdog, and runs three background loops:

    scan_loop      - every ``scanner.interval_seconds`` -> new short list of plays
    sync_loop      - a few seconds -> reconcile fills, run the automatic exits
    snapshot_loop  - ~30s -> account snapshot, broker-vs-database check, broadcast

Everything the user changes on the dashboard - where orders go, the filters,
which strategies run, Autopilot - applies straight away to the scanner and the
execution path, is remembered in ``data/runtime.json``, and is broadcast so
every open tab updates.

Where orders go (routing table in :mod:`tos_bot.brokers.venues`):

    mode            paper | live
    paper_platform  simulator | ibkr | schwab     what Paper trades on
    live_broker     ibkr | schwab                 what Live trades on

Safety rules: no order without an explicit :meth:`approve_play` (or the opt-in
Autopilot, inside its caps); no venue change while positions are open on the
current one; while quitting with positions open, nothing but exits may change;
and an OPEN trade record is deleted only when a connected broker confirms the
position no longer exists.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import secrets_store
from .auth.schwab_login import SchwabLogin
from .auth.token_manager import AuthWatchdog, TokenManager
from .brokers import get_broker
from .brokers.base import BrokerAdapter
from .brokers.venues import (
    IBKR_STEPS, LIVE_BROKERS, PAPER_PLATFORMS, ROUTE_LABELS, SCHWAB_STEPS, VenuePlan,
    normalize, plan_venue, venue_id, venue_label,
)
from .config import PROJECT_ROOT, Secrets, Settings, get_settings
from .core.enums import PlayStatus
from .core.eventbus import BUS
from .core.models import Account, Play
from .data.fundamentals import YFinanceFundamentals
from .data.market_data import MarketDataService, SyntheticProvider, YFinanceProvider
from .execution.autopilot import AutoPilot
from .execution.executor import Executor
from .execution.exit_manager import ExitManager
from .execution.order_builder import plan_order
from .persistence.db import init_db
from .persistence.repository import Repository
from .risk.pdt_guard import PdtGuard
from .risk.position_sizing import size_play
from .scanner.filters import TradeFilters
from .scanner.scanner import Scanner
from .strategies.registry import REGISTRY, build_strategies, strategy_catalog
from .util import clock
from .util.logging_setup import setup_logging
from .util.net import port_is_open

log = logging.getLogger(__name__)

# where the dashboard's choices persist between runs. Overridable (tests point
# it at a temp file so a run never rewrites the user's).
_RUNTIME_PATH = Path(os.getenv("TOS_RUNTIME_PATH") or (PROJECT_ROOT / "data" / "runtime.json"))


class TradingEngine:
    #: play statuses that must never be re-executed
    _DONE_STATUSES = {"ACCEPTED", "SUBMITTED", "WORKING", "PARTIAL", "FILLED", "ERROR"}
    #: an OPEN trade is only treated as gone at the broker after this many
    #: consecutive checks, once it's older than the grace period and the
    #: broker connection has been up long enough to have reported positions
    _MISSING_CHECKS = 2
    _MISSING_GRACE_S = 90.0
    _CONNECTION_SETTLE_S = 60.0
    #: while quitting, re-send closes that haven't taken this often
    _QUIT_RETRY_S = 30.0
    _WEIGHT_RANGE = (0.1, 3.0)

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        setup_logging(self.settings.config.app.log_level)

        init_db()
        self.repo = Repository()

        # market data: a real feed if we have one, else the synthetic demo feed -
        # never both (see MarketDataService). A connected broker goes on top.
        yfp = YFinanceProvider()
        self.md = MarketDataService(providers=[yfp] if yfp.available else [SyntheticProvider()])
        if not yfp.available:
            log.warning("yfinance not installed - running on SYNTHETIC data. "
                        "`pip install yfinance` for real (delayed) quotes.")
        self.fundamentals = YFinanceFundamentals()

        # the dashboard's remembered choices win over config.yaml / .env
        rt = self._read_runtime()
        self.filters = self._load_filters(rt)
        self.strategy_overrides = self._clean_overrides(rt.get("strategies"))
        self.strategies = build_strategies(self.settings, self.strategy_overrides)
        self.scanner = Scanner(self.settings, self.md, self.fundamentals, self.strategies)
        self.scanner.filters = self.filters

        self.token_manager = TokenManager(self.settings, repo=self.repo, bus=BUS)
        self.schwab_login = SchwabLogin(self.settings, bus=BUS, on_success=self._on_schwab_login)

        sec = self.settings.secrets
        env_broker = (sec.broker or "paper").lower()
        self.paper_platform, self.live_broker = normalize(
            rt.get("paper_platform", sec.paper_platform),
            rt.get("live_broker", env_broker if env_broker in LIVE_BROKERS else sec.live_broker),
        )
        mode = rt.get("mode")
        self.mode: str = mode if mode in ("paper", "live") else (
            "live" if env_broker in LIVE_BROKERS else "paper")

        #: set while quitting with positions still open - everything but exits is locked
        self.quit_state: Optional[Dict[str, Any]] = rt.get("quit") if isinstance(rt.get("quit"), dict) else None
        #: called once quitting has finished (the server wires this to its own shutdown)
        self.on_shutdown: Optional[Callable[[], None]] = None
        self._quit_lock = threading.Lock()
        self._quit_retry_at = 0.0
        self._quit_rounds = 0

        self._sim: Optional[BrokerAdapter] = None          # the built-in simulator
        self._venue: Optional[BrokerAdapter] = None        # the one broker connection held
        self._venue_plan = VenuePlan()
        self._venue_since = 0.0
        self._venue_blockers: List[str] = []
        self._live_blockers: List[str] = []
        self._trading_broker: Optional[BrokerAdapter] = None
        self._trading_venue = "paper"                       # venue id stamped on new trades

        self.executor: Optional[Executor] = None
        self.exit_manager: Optional[ExitManager] = None
        self.pdt: Optional[PdtGuard] = None

        self._plays: Dict[str, Play] = {}
        self._last_scan_summary: Dict[str, Any] = {}
        self._last_scan_elapsed: float = 0.0
        self._account: Optional[Account] = None
        self._account_at = 0.0
        self._armed = False
        self._missing: Dict[Tuple[str, str], int] = {}      # (venue, symbol) -> misses in a row

        # hands-off entry (exits are already automatic via ExitManager)
        self.autopilot = AutoPilot(self, self.settings.config.autopilot,
                                   bus=BUS, persist=self._save_runtime)
        self.autopilot.load_runtime(rt.get("autopilot", {}))

        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._scan_now = threading.Event()
        self._switch_lock = threading.RLock()
        self._auth_watchdog: Optional[AuthWatchdog] = None

    @property
    def broker(self) -> BrokerAdapter:
        return self._trading_broker  # type: ignore[return-value]

    # ------------------------------------------------------------------ #
    #  Runtime state file                                               #
    # ------------------------------------------------------------------ #
    def _read_runtime(self) -> Dict[str, Any]:
        try:
            d = json.loads(_RUNTIME_PATH.read_text()) if _RUNTIME_PATH.exists() else {}
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _save_runtime(self) -> None:
        try:
            _RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload: Dict[str, Any] = {
                "mode": self.mode, "paper_platform": self.paper_platform,
                "live_broker": self.live_broker, "filters": self.filters.as_dict(),
                "strategies": self.strategy_overrides,
            }
            if self.quit_state:
                payload["quit"] = self.quit_state
            if getattr(self, "autopilot", None) is not None:
                payload["autopilot"] = self.autopilot.to_runtime()
            _RUNTIME_PATH.write_text(json.dumps(payload, indent=2))
        except Exception:  # noqa: BLE001
            log.debug("could not persist runtime state", exc_info=True)

    def _load_filters(self, rt: Dict[str, Any]) -> TradeFilters:
        f = rt.get("filters") if isinstance(rt.get("filters"), dict) else {}
        sectors = f.get("sectors", rt.get("sectors", self.settings.config.scanner.sectors))
        try:
            return TradeFilters.build(f.get("sides"), f.get("timeframes"), sectors)
        except ValueError:
            return TradeFilters.build(sectors=sectors)

    @staticmethod
    def _clean_overrides(raw: Any) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for key, ov in (raw or {}).items() if isinstance(raw, dict) else []:
            if key in REGISTRY and isinstance(ov, dict):
                clean = {}
                if isinstance(ov.get("enabled"), bool):
                    clean["enabled"] = ov["enabled"]
                if isinstance(ov.get("weight"), (int, float)):
                    clean["weight"] = float(ov["weight"])
                if clean:
                    out[key] = clean
        return out

    # ------------------------------------------------------------------ #
    #  Broker connection + routing                                      #
    # ------------------------------------------------------------------ #
    def _ensure_sim(self) -> BrokerAdapter:
        if self._sim is None:
            self._sim = get_broker(
                "paper",
                starting_cash=self.settings.config.account.paper_start_cash,
                data_service=self.md,
                persist=os.getenv("PAPER_PERSIST", "1") != "0",
            )
            self._sim.connect()
        return self._sim

    def _ibkr_port(self, account: str) -> int:
        sec = self.settings.secrets
        return int(sec.ibkr_port or (sec.ibkr_live_port if account == "live" else sec.ibkr_paper_port))

    def _venue_prereqs(self, plan: VenuePlan) -> List[str]:
        """Cheap checks before trying to connect, each with its fix."""
        sec = self.settings.secrets
        if plan.broker == "ibkr":
            port = self._ibkr_port(plan.account)
            if not port_is_open(sec.ibkr_host, port):
                return [f"IB Gateway / TWS isn't reachable on {sec.ibkr_host}:{port} "
                        f"({plan.account} port) - start it (or IBC) with the API enabled. "
                        "See Connections."]
        elif plan.broker == "schwab":
            if not (sec.schwab_api_key and sec.schwab_app_secret):
                return ["Schwab app key / secret not set - add them under Connections."]
            if not sec.token_path.exists():
                return ["Not signed in to Schwab - use Sign in with Schwab under Connections."]
        return []

    def _ensure_venue(self, plan: VenuePlan) -> Optional[BrokerAdapter]:
        """Hold exactly the one broker connection ``plan`` needs."""
        self._venue_blockers = []
        if (self._venue is not None and self._venue_plan.key == plan.key
                and self._venue.is_connected):
            return self._venue
        self._close_venue()
        if plan.broker is None:
            return None
        self._venue_blockers = self._venue_prereqs(plan)
        if self._venue_blockers:
            return None
        try:
            if plan.broker == "ibkr":
                b = get_broker("ibkr", port=self._ibkr_port(plan.account), mode=plan.account,
                               readonly=(not plan.trade) or self.settings.secrets.ibkr_readonly)
            else:
                b = get_broker("schwab", token_manager=self.token_manager)
            b.connect()
        except Exception as e:  # noqa: BLE001
            self._venue_blockers = [str(e)]
            log.warning("%s (%s) unavailable: %s", plan.broker, plan.account, e)
            return None
        self._venue, self._venue_plan, self._venue_since = b, plan, time.monotonic()
        log.info("connected to %s %s (%s)", plan.broker, plan.account,
                 "trading" if plan.trade else "data only")
        return b

    def _close_venue(self) -> None:
        if self._venue is not None:
            try:
                self._venue.close()
            except Exception:  # noqa: BLE001
                pass
        self._venue, self._venue_plan = None, VenuePlan()

    def _bind_trading_broker(self) -> None:
        """Connect what the switches need and point the executor, exit manager
        and PDT guard at it. Live falls back to paper if it isn't reachable."""
        plan = plan_venue(self.mode, self.paper_platform, self.live_broker)
        venue = self._ensure_venue(plan)
        if self.mode == "live" and venue is None:
            self._live_blockers = list(self._venue_blockers)
            log.warning("falling back to paper - live broker not ready: %s",
                        "; ".join(self._live_blockers))
            self.mode = "paper"
            self._bind_trading_broker()
            return
        if self.mode == "live":
            self._live_blockers = []

        if plan.trade and venue is not None:
            self._trading_broker, self._trading_venue = venue, venue_id(plan)
        else:
            self._trading_broker, self._trading_venue = self._ensure_sim(), "paper"
            if self._trading_venue == "paper":
                self._venue_since = self._venue_since or time.monotonic()

        self.pdt = PdtGuard(self.settings.config.account, trade_repo=self.repo,
                            paper=self.mode == "paper")
        if self.executor is None:
            self.executor = Executor(self._trading_broker, self.repo,
                                     self.settings.config.execution, bus=BUS,
                                     venue=self._trading_venue)
        else:
            self.executor.rebind(self._trading_broker, venue=self._trading_venue)
        # automatic exits - quotes from the active broker, only for trades held there
        self.exit_manager = ExitManager(
            self.repo, self.executor,
            quote_fn=lambda s: self._trading_broker.get_quote(s),
            cfg=self.settings.config.exit_manager, bus=BUS, venue=self._trading_venue,
        )
        self._missing.clear()
        self._sync_data_feed()

    def _sync_data_feed(self) -> None:
        """The connected broker (real exchange quotes/candles) goes on top of the feed."""
        self.md.providers = [p for p in self.md.providers if not isinstance(p, _BrokerProvider)]
        if self._venue is not None and self._venue.is_connected:
            self.md.providers.insert(0, _BrokerProvider(self._venue))

    def _open_trades(self) -> List[Dict[str, Any]]:
        try:
            return self.repo.open_trades()
        except Exception:  # noqa: BLE001
            return []

    def _positions_here(self) -> List[Dict[str, Any]]:
        """OPEN trades held on the venue orders currently go to."""
        return [t for t in self._open_trades() if (t.get("broker") or "paper") == self._trading_venue]

    def _switch_blocked(self, target_venue: str) -> Optional[str]:
        """Refuse to move orders to another venue while positions are open on
        the current one - their automatic exits would go to the wrong account."""
        if target_venue == self._trading_venue:
            return None
        try:
            held = [t for t in self.repo.open_trades()
                    if (t.get("broker") or "paper") == self._trading_venue]
        except Exception:  # noqa: BLE001
            return "Couldn't check your open positions, so the switch was cancelled."
        if not held:
            return None
        syms = ", ".join(sorted({t["symbol"] for t in held}))
        return (f"You have {len(held)} open position(s) on {venue_label(self._trading_venue)} "
                f"({syms}). Close them, or let their automatic exits finish, before switching.")

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
        log.warning("routing changed by %s: mode %s -> %s, paper=%s, live=%s, orders -> %s",
                    operator, prev_mode, self.mode, self.paper_platform, self.live_broker,
                    self._trading_venue)
        BUS.publish("broker.switched", mode=self.mode, prev=prev_mode, state=self.snapshot())

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        log.info("engine starting (mode=%s, paper=%s, live=%s)",
                 self.mode, self.paper_platform, self.live_broker)
        self._bind_trading_broker()
        self._refresh_account()
        self._check_arm()
        if self.quit_state:
            log.warning("resuming an unfinished quit - closing the remaining positions first")

        self._threads = [
            threading.Thread(target=self._scan_loop, name="scan-loop", daemon=True),
            threading.Thread(target=self._sync_loop, name="sync-loop", daemon=True),
            threading.Thread(target=self._snapshot_loop, name="snapshot-loop", daemon=True),
        ]
        for t in self._threads:
            t.start()

        # keeps a Schwab token fresh; a no-op until you've signed in
        self._auth_watchdog = AuthWatchdog(
            self.token_manager,
            broker_provider=lambda: self._venue if self._venue_plan.broker == "schwab" else None,
            interval_s=self.settings.config.auth.check_interval_seconds,
        )
        self._auth_watchdog.start()
        BUS.publish("engine.started", state=self.snapshot())

    def stop(self) -> None:
        self._stop.set()
        if self._auth_watchdog is not None:
            self._auth_watchdog.stop()
        self._close_venue()
        if self._sim is not None:
            try:
                self._sim.close()
            except Exception:  # noqa: BLE001
                pass
        log.info("engine stopped")

    # ------------------------------------------------------------------ #
    #  Background loops                                                  #
    # ------------------------------------------------------------------ #
    def _autopilot_day_active(self) -> bool:
        """Autopilot armed for day trades + regular session open -> the engine
        scans (and refreshes the account) much more aggressively."""
        try:
            return self.autopilot.day_mode_active(clock.is_market_open())
        except Exception:  # noqa: BLE001
            return False

    def _effective_scan_interval(self) -> int:
        """Seconds between scans: the normal cadence, unless Autopilot is
        day-trading an open session - then the fast cadence, but never starting a
        new cycle before the last one finished (+3s) or below the floor."""
        sc = self.settings.config.scanner
        base = int(sc.interval_seconds)
        if not self._autopilot_day_active():
            return base
        target = int(getattr(sc, "autopilot_interval_seconds", 45) or 45)
        floor = int(getattr(sc, "min_interval_seconds", 20) or 20)
        return max(floor, max(target, int(self._last_scan_elapsed) + 3))

    def _scan_loop(self) -> None:
        self._stop.wait(2.0)
        while not self._stop.is_set():
            triggered = self._scan_now.is_set()
            self._scan_now.clear()
            if (triggered or clock.is_market_open()) and not self.quit_state:
                try:
                    self._run_scan()
                except Exception:  # noqa: BLE001
                    log.exception("scan cycle failed")
            interval = self._effective_scan_interval()
            waited = 0
            while waited < interval and not self._stop.is_set() and not self._scan_now.is_set():
                self._stop.wait(1.0)
                waited += 1

    def _sync_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.executor:
                    self.executor.sync_open_orders()
            except Exception:  # noqa: BLE001
                log.exception("order sync failed")
            try:
                if self.exit_manager and self.exit_manager.run_once():
                    self._refresh_account()
                    BUS.publish("account.snapshot", state=self.snapshot())
            except Exception:  # noqa: BLE001
                log.exception("exit manager tick failed")
            try:
                self._check_quit_progress()
            except Exception:  # noqa: BLE001
                log.exception("quit progress check failed")
            self._stop.wait(4.0)

    def _snapshot_loop(self) -> None:
        while not self._stop.is_set():
            try:
                # nudge IBKR back up if its Gateway session dropped (daily restart)
                if self._venue_plan.broker == "ibkr" and self._venue is not None:
                    try:
                        self._venue.refresh_if_needed()
                    except Exception:  # noqa: BLE001
                        pass
                if self._refresh_account():
                    self._reconcile_open_trades()
                if self._account:
                    self.repo.snapshot_account(
                        self._account, self._trading_venue,
                        realized_day=self.repo.pnl_summary().get("realized_today", 0.0),
                    )
                BUS.publish("account.snapshot", state=self.snapshot())
            except Exception:  # noqa: BLE001
                log.exception("snapshot failed")
            # refresh the account far more often while Autopilot is day-trading
            self._stop.wait(10.0 if self._autopilot_day_active() else 30.0)

    def _run_scan(self) -> None:
        self._refresh_account()
        if self._account:
            self.scanner.set_account(self._account)
        result = self.scanner.run_cycle()

        self._plays = {}
        for p in result.plays:
            if self._account:
                size_play(p, self._account, self.settings.config.risk)
            self._plays[p.id] = p
        self._last_scan_summary = result.summary()
        self._last_scan_elapsed = float(getattr(result, "elapsed_s", 0.0) or 0.0)

        try:
            self.repo.record_scan(result,
                                  keep_rejected=self.settings.config.database.record_rejected_plays)
        except Exception:  # noqa: BLE001
            log.exception("failed to persist scan")

        self._publish_plays()

        # hands-off entry: let the pilot act on the fresh plays (no-op unless armed)
        if not self.quit_state:
            try:
                self.autopilot.consider(self._plays)
            except Exception:  # noqa: BLE001
                log.exception("autopilot pass failed")

    def _publish_plays(self) -> None:
        BUS.publish("plays.updated",
                    plays=[self._decorate(p) for p in list(self._plays.values())[:80]],
                    scan=self._last_scan_summary)

    # ------------------------------------------------------------------ #
    #  Account / arming / broker-vs-database                           #
    # ------------------------------------------------------------------ #
    def _refresh_account(self) -> bool:
        try:
            if self._trading_broker:
                self._account = self._trading_broker.get_account()
                self._account_at = time.monotonic()
                return True
        except Exception as e:  # noqa: BLE001
            log.debug("get_account failed: %s", e)
        return False

    def _check_arm(self) -> None:
        acc = self._account
        if self.mode == "paper":
            self._armed = True                      # paper (sim or IBKR paper) has no equity floor
            return
        floor = self.settings.config.account.min_start_equity
        self._armed = bool(acc and acc.equity >= floor)
        if acc and not self._armed:
            BUS.publish("engine.disarmed",
                        reason=f"equity ${acc.equity:,.0f} < ${floor:,.0f} live floor")

    def _reconcile_open_trades(self, force: bool = False) -> List[Dict[str, Any]]:
        """Delete OPEN trade records whose position no longer exists at the
        broker that holds it - closed outside the app, removed, or wiped by a
        paper reset. Only ever acts on a connected broker's fresh answer, and
        (unless ``force``) only after repeated misses on an established
        connection, so a slow or dropped feed can never erase a live position."""
        broker, venue = self._trading_broker, self._trading_venue
        if broker is None or not broker.is_connected or self._account is None:
            return []
        now = time.monotonic()
        if now - self._account_at > 5.0:
            return []                                     # stale account - don't judge
        if not force and now - self._venue_since < self._CONNECTION_SETTLE_S:
            return []
        held = {p.symbol for p in self._account.positions if abs(p.quantity) > 1e-9}
        trades = [t for t in self._open_trades() if (t.get("broker") or "paper") == venue]
        busy = set(self.executor.pending_exit_trade_ids()) if self.executor else set()
        busy |= set(getattr(self.exit_manager, "_closing", set()))

        missing_now = set()
        removed: List[Dict[str, Any]] = []
        for t in trades:
            sym = t["symbol"]
            if sym in held:
                self._missing.pop((venue, sym), None)
                continue
            if t["id"] in busy:
                continue                                  # its close is still going through
            if not force:
                if _age_s(t.get("entry_time")) < self._MISSING_GRACE_S:
                    continue
                if (venue, sym) not in missing_now:
                    missing_now.add((venue, sym))
                    self._missing[(venue, sym)] = self._missing.get((venue, sym), 0) + 1
                if self._missing[(venue, sym)] < self._MISSING_CHECKS:
                    continue
            if self.repo.delete_trade(t["id"]):
                removed.append({"id": t["id"], "symbol": sym, "side": t["side"],
                                "quantity": t["quantity"]})
        for r in removed:
            self._missing.pop((venue, r["symbol"]), None)
        if removed:
            log.warning("removed %d trade record(s) no longer held at %s: %s", len(removed), venue,
                        ", ".join(r["symbol"] for r in removed))
            BUS.publish("trades.removed", trades=removed, venue=venue,
                        venue_label=venue_label(venue))
        return removed

    # ------------------------------------------------------------------ #
    #  Operator actions (called by the API)                             #
    # ------------------------------------------------------------------ #
    def assess_play(self, play_id: str) -> Dict[str, Any]:
        p = self._plays.get(play_id)
        if not p:
            return {"ok": False, "reason": "play not found (it may have expired)"}
        self._refresh_account()
        acc = self._account
        if acc is None:
            return {"ok": False, "reason": "no account data"}
        size_play(p, acc, self.settings.config.risk)
        decision = self.pdt.assess(acc, p)

        session = clock.current_session()
        plan = plan_order(p, session, self.settings.config.execution)
        already_done = p.status.value in self._DONE_STATUSES
        rr_ok = p.reward_risk >= self.settings.config.risk.min_reward_risk or p.kind.value == "FUNDAMENTAL"

        reasons: List[str] = []
        locked = self._locked()
        if locked:
            reasons.append(locked)
        if already_done:
            reasons.append(f"already {p.status.value.lower()}"
                           + (f" - trade {p.trade_id}" if p.trade_id else ""))
        if not plan.get("executable", False):
            reasons.append(plan.get("reason", "not executable in this session"))
        if not self._armed:
            reasons.append(f"engine not armed - live equity below "
                           f"${self.settings.config.account.min_start_equity:,.0f} floor")
        if not decision.allowed:
            reasons.append(decision.reason)
        if p.suggested_qty <= 0:
            reasons.append("position size rounds to zero for this risk budget")
        if not rr_ok:
            reasons.append(f"reward:risk {p.reward_risk:.1f} below minimum")
        filtered = self.filters.refusal(p.side.value, p.timeframe.value, p.sector)
        if filtered:
            reasons.append(filtered)
        can = not reasons

        return {
            "ok": True, "can_execute": can, "already_executed": already_done,
            "reasons": reasons, "mode": self.mode, "session": session.value,
            "play": self._decorate(p),
            "pdt": decision.as_dict(),
            "order_plan": plan,
            "order_preview": {
                "side": p.side.entry_action, "qty": p.suggested_qty,
                "order_type": plan.get("order_type"),
                "session_label": plan.get("session_label"),
                "limit_price": plan.get("limit_price"), "stop_price": plan.get("stop_price"),
                "tif": plan.get("tif", "DAY"),
                "bracket_mode": plan.get("bracket_mode"),
                "take_profit": p.primary_target, "stop_loss": p.stop,
                "exit_manager": bool(self.settings.config.exit_manager.enabled),
                "expected_hold": (
                    f"~{p.expected_hold_typical:.0f} min (review after {p.expected_hold_max:.0f})"
                    if p.timeframe.value == "INTRADAY"
                    else f"~{p.expected_hold_typical:.0f} trading days (review after {p.expected_hold_max:.0f})"
                ),
                "est_cost": round(p.notional, 2), "est_risk": round(p.dollar_risk, 2),
                "note": plan.get("note", ""),
                "routes_to": ROUTE_LABELS.get(self._trading_venue, self._trading_venue.upper()),
            },
        }

    def approve_play(self, play_id: str, operator: str = "operator") -> Dict[str, Any]:
        with self._switch_lock:
            locked = self._locked()
            if locked:
                return {"ok": False, "reason": locked}
            p = self._plays.get(play_id)
            if p is None:
                return {"ok": False, "reason": "play not found (it may have expired)"}
            if p.status.value in self._DONE_STATUSES:
                return {"ok": False, "reason": f"already {p.status.value.lower()}"
                        + (f" - trade {p.trade_id}" if p.trade_id else ""),
                        "already_executed": True, "trade_id": p.trade_id}

            pre = self.assess_play(play_id)
            if not pre.get("ok"):
                return pre
            if not pre["can_execute"]:
                return {"ok": False, "reason": "; ".join(pre["reasons"]) or "not executable"}

            p.status = PlayStatus.ACCEPTED
            self.repo.set_play_status(p.id, "ACCEPTED", operator)
            try:
                out = self.executor.execute_play(p, self._account, plan=pre["order_plan"])
            except Exception as e:  # noqa: BLE001
                p.status = PlayStatus.ERROR
                self.repo.set_play_status(p.id, "ERROR", operator)
                log.exception("execute_play crashed")
                BUS.publish("play.decided", play_id=p.id, decision="error", result={"reason": str(e)})
                return {"ok": False, "reason": f"execution error: {e}"}
            if not out.get("ok"):
                p.status = PlayStatus.PROPOSED     # let them try again once the reason clears
            self.repo.set_play_status(p.id, p.status.value, operator)
            BUS.publish("play.decided", play_id=p.id, decision="approved", result=out,
                        play=self._decorate(p))
            self._refresh_account()
            return {"ok": out.get("ok", False), **out}

    def reject_play(self, play_id: str, operator: str = "operator") -> Dict[str, Any]:
        p = self._plays.get(play_id)
        if p:
            p.status = PlayStatus.REJECTED
        self.repo.set_play_status(play_id, "REJECTED", operator)
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
        failed = [r for r in results if not r.get("ok")]
        note = (f"Exit sent for {len(held) - len(failed)} of {len(held)} position(s)."
                + (f" Not sent: {', '.join(r['symbol'] for r in failed)}." if failed else ""))
        return {"ok": not failed, "note": note, "results": results}

    def _close_all(self, trades: List[Dict[str, Any]], reason: str) -> List[Dict[str, Any]]:
        """Send every close at once - the broker calls are independent, so a
        thread per position turns N round-trips into about one."""
        if not trades:
            return []

        def one(t: Dict[str, Any]) -> Dict[str, Any]:
            try:
                out = self.executor.close_trade(t["id"], reason=reason)
            except Exception as e:  # noqa: BLE001
                out = {"ok": False, "reason": str(e)}
            return {"trade_id": t["id"], "symbol": t["symbol"], "ok": bool(out.get("ok")),
                    "status": out.get("status"), "reason": out.get("reason", "")}

        with ThreadPoolExecutor(max_workers=min(8, len(trades))) as ex:
            results = list(ex.map(one, trades))
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
        rec["on_current_venue"] = venue == self._trading_venue
        rec["broker_position"] = None
        if t["status"] == "OPEN" and rec["on_current_venue"] and self._account is not None:
            pos = next((p for p in self._account.positions if p.symbol == t["symbol"]), None)
            if pos is not None:
                rec["broker_position"] = {"quantity": pos.quantity,
                                          "market_price": round(pos.market_price, 4),
                                          "unrealized_pl": round(pos.unrealized_pl, 2)}
        return rec

    # ---- autopilot (hands-off entry) --------------------------------- #
    def set_autopilot(self, **kw: Any) -> Dict[str, Any]:
        """Toggle / tune hands-off entry from the dashboard. Exits are already
        automatic; this governs whether the bot also takes the entry."""
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        want_on = kw.get("enabled")
        st = self.autopilot.configure(**kw)
        note = ""
        if want_on and self.mode == "live" and not st["allow_live"]:
            note = ("Autopilot will NOT place live orders: set  autopilot.allow_live: "
                    "true  in config/config.yaml first. It is armed for paper only.")
        elif want_on and st["effective"]:
            tt = ", ".join(t.lower() for t in st["trade_types"])
            note = (f"Autopilot ON ({tt}). It will enter up to {st['max_auto_positions']} "
                    f"positions / {st['max_auto_trades_per_day']} per day at "
                    f">= {st['min_reward_risk']:.0f}:1 and >= {st['min_confidence']:.2f} "
                    f"confidence. Exits stay automatic.")
        elif want_on is False:
            note = "Autopilot OFF - back to click-to-enter. Open trades keep their automatic exits."
        BUS.publish("autopilot.config", **st)
        return {"ok": True, "autopilot": st, "note": note}

    def refresh_account_now(self) -> Dict[str, Any]:
        self._refresh_account()
        self._check_arm()
        if self.executor:
            try:
                self.executor.sync_open_orders()
            except Exception:  # noqa: BLE001
                pass
        snap = self.snapshot()
        BUS.publish("account.snapshot", state=snap)
        return {"ok": True, "state": snap}

    def reset_paper(self, cash: Optional[float] = None) -> Dict[str, Any]:
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        if self._trading_venue != "paper":
            return {"ok": False, "reason": f"You're trading on {venue_label(self._trading_venue)} - "
                                           "its balance and positions are kept by the broker."}
        amount = float(cash) if cash is not None else self.settings.config.account.paper_start_cash
        removed = self._reset_sim(amount)
        BUS.publish("account.snapshot", state=self.snapshot())
        note = f"Paper account reset to ${amount:,.0f}."
        if removed:
            note += f" Removed {len(removed)} open trade record(s) whose positions were wiped."
        return {"ok": True, "cash": round(amount, 2), "removed": removed, "note": note}

    def _reset_sim(self, amount: float) -> List[Dict[str, Any]]:
        pb = self._ensure_sim()
        if self.executor is not None:
            self.executor.cancel_pending_entries()
        pb.reset(amount)  # type: ignore[attr-defined]
        self.executor.rebind(pb, venue="paper")
        self._refresh_account()
        # the simulator's positions are gone, so their OPEN trade records go too
        return self._reconcile_open_trades(force=True)

    def trigger_scan(self) -> Dict[str, Any]:
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        self._scan_now.set()
        return {"ok": True, "note": "scan queued"}

    # ---- filters + strategies (live: next scan, execution, every tab) -- #
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
        self.filters = new
        self.scanner.filters = new
        self._save_runtime()
        before = len(self._plays)
        self._plays = {k: p for k, p in self._plays.items() if new.allows(p)}
        self._publish_plays()
        BUS.publish("filters.updated", filters=new.as_dict())
        # narrowing just trims the board; widening needs a scan to find the new plays
        widened = (bool(set(new.sides) - set(cur.sides)) or bool(set(new.timeframes) - set(cur.timeframes))
                   or (bool(cur.sectors) and (not new.sectors or bool(set(new.sectors) - set(cur.sectors)))))
        if widened:
            self._scan_now.set()
        return {"ok": True, "filters": new.as_dict(), "removed_plays": before - len(self._plays),
                "rescanning": widened, "note": new.describe()}

    def strategy_state(self) -> List[Dict[str, Any]]:
        return strategy_catalog(self.settings, self.strategy_overrides)

    def set_strategy(self, key: str, enabled: Optional[bool] = None,
                     weight: Optional[float] = None) -> Dict[str, Any]:
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        if key not in REGISTRY:
            return {"ok": False, "reason": f"unknown strategy '{key}'"}
        ov = dict(self.strategy_overrides.get(key, {}))
        if enabled is not None:
            ov["enabled"] = bool(enabled)
        if weight is not None:
            try:
                w = float(weight)
            except (TypeError, ValueError):
                return {"ok": False, "reason": "weight must be a number"}
            lo, hi = self._WEIGHT_RANGE
            if not lo <= w <= hi:
                return {"ok": False, "reason": f"weight must be between {lo} and {hi}"}
            ov["weight"] = round(w, 2)
        default = next(r for r in strategy_catalog(self.settings) if r["key"] == key)
        if ov.get("enabled") == default["default_enabled"]:
            ov.pop("enabled")
        if "weight" in ov and abs(ov["weight"] - default["default_weight"]) < 1e-9:
            ov.pop("weight")
        overrides = {k: v for k, v in self.strategy_overrides.items() if k != key}
        if ov:
            overrides[key] = ov
        rescan = weight is not None or bool(enabled)
        self._apply_strategies(overrides, rescan=rescan)
        row = next(r for r in self.strategy_state() if r["key"] == key)
        return {"ok": True, "strategies": self.strategy_state(), "rescanning": rescan,
                "note": f"{row['title']}: {'on' if row['enabled'] else 'off'}, weight {row['weight']:g}."}

    def reset_strategies(self) -> Dict[str, Any]:
        locked = self._locked()
        if locked:
            return {"ok": False, "reason": locked}
        self._apply_strategies({}, rescan=True)
        return {"ok": True, "strategies": self.strategy_state(),
                "note": "Strategies reset to config.yaml.", "rescanning": True}

    def _apply_strategies(self, overrides: Dict[str, Dict[str, Any]], rescan: bool) -> None:
        self.strategy_overrides = overrides
        self.strategies = build_strategies(self.settings, overrides)
        self.scanner.set_strategies(self.strategies)
        self._save_runtime()
        active = {s.key for s in self.strategies}
        self._plays = {k: p for k, p in self._plays.items() if p.strategy in active}
        self._publish_plays()
        BUS.publish("strategies.updated", strategies=self.strategy_state())
        if rescan:
            self._scan_now.set()

    # ---- quitting ----------------------------------------------------- #
    def quit_preview(self) -> Dict[str, Any]:
        trades = self._open_trades()
        brief = [{"id": t["id"], "symbol": t["symbol"], "side": t["side"],
                  "quantity": t["quantity"], "entry_price": t["entry_price"],
                  "venue": t.get("broker") or "paper"} for t in trades]
        here = [b for b in brief if b["venue"] == self._trading_venue]
        return {
            "mode": self.mode, "paper": self.mode == "paper",
            "venue": self._trading_venue, "venue_label": venue_label(self._trading_venue),
            "positions": here, "left": len(here),
            "parked": [b for b in brief if b["venue"] != self._trading_venue],
            "resets_simulator": self.mode == "paper" and self._trading_venue == "paper",
            "reset_cash": self.settings.config.account.paper_start_cash,
            "quitting": bool(self.quit_state),
        }

    def request_quit_dialog(self) -> None:
        """Ask the dashboard to show the quit choices (used by Ctrl+C in live)."""
        BUS.publish("quit.requested", **self.quit_preview())

    def begin_quit(self, close_all: bool = True, operator: str = "operator") -> Dict[str, Any]:
        """Paper: close everything, reset the simulator, shut down. Live: close
        everything and shut down once flat (``close_all=False`` cancels).
        Until the last position is out, nothing but exits may change."""
        with self._switch_lock:
            if self.quit_state:
                return {"ok": True, "note": "Already closing out before quitting.",
                        "quit": self._quit_status()}
            held = self._positions_here()
            if self.mode == "live" and held and not close_all:
                return {"ok": False, "reason": "Quit cancelled - your live positions stay open and managed."}
            self.quit_state = {
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "mode": self.mode, "venue": self._trading_venue, "by": operator,
                "reset_sim": self.mode == "paper" and self._trading_venue == "paper",
            }
            self._quit_rounds = 1
            self._save_runtime()
            cancelled = self.executor.cancel_pending_entries() if self.executor else 0
            log.warning("quit by %s: closing %d position(s) on %s, cancelled %d working entr%s",
                        operator, len(held), self._trading_venue, cancelled,
                        "y" if cancelled == 1 else "ies")
            BUS.publish("quit.started", quit=self._quit_status())
            results = self._close_all(held, reason="quit")
            self._quit_retry_at = time.monotonic() + self._QUIT_RETRY_S
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
                # a simulator close can only fail on a missing quote; after a retry
                # the reset wipes those positions anyway, so don't stay stuck
                if self.quit_state.get("reset_sim") and self._quit_rounds >= 2:
                    left = []
                else:
                    busy = self.executor.pending_exit_trade_ids()
                    retry = [t for t in left if t["id"] not in busy]
                    if retry:
                        self._close_all(retry, reason="quit")
                        self._quit_rounds += 1
                    self._quit_retry_at = time.monotonic() + self._QUIT_RETRY_S
                    left = self._positions_here()
            if left:
                BUS.publish("quit.progress", quit=self._quit_status())
                return

            state, self.quit_state = self.quit_state, None
            note = "All positions are closed."
            if state.get("reset_sim") and self._trading_venue == "paper":
                cash = self.settings.config.account.paper_start_cash
                self._reset_sim(cash)
                self._plays = {}
                note += f" Paper account reset to ${cash:,.0f}."
            self._save_runtime()
        log.warning("quit finished: %s", note)
        BUS.publish("quit.done", note=note)
        if self.on_shutdown is not None:
            # give the "shut down" message a moment to reach the browser
            threading.Timer(1.5, self.on_shutdown).start()

    # ---- routing: paper / live, platforms, connections ----------------- #
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
            blocked = self._switch_blocked(venue_id(plan_venue(mode, self.paper_platform, self.live_broker)))
            if blocked:
                return {"ok": False, "reason": blocked}
            prev = self.mode
            self.mode = mode
            self._bind_trading_broker()          # knocks mode back to paper if live isn't reachable
            if mode == "live" and self.mode != "live":
                return {"ok": False, "reason": f"{LIVE_BROKERS[self.live_broker]} isn't ready",
                        "blockers": self._live_blockers}
            self._after_switch(prev, operator)
            where = venue_label(self._trading_venue)
            return {"ok": True, "mode": self.mode,
                    "note": f"LIVE - orders now go to {where}." if self.mode == "live"
                    else f"Paper - orders go to {where}."}

    def set_broker_setup(self, paper_platform: Optional[str] = None,
                         live_broker: Optional[str] = None,
                         operator: str = "operator") -> Dict[str, Any]:
        if paper_platform is not None and paper_platform not in PAPER_PLATFORMS:
            return {"ok": False, "reason": f"paper platform must be one of {', '.join(PAPER_PLATFORMS)}"}
        if live_broker is not None and live_broker not in LIVE_BROKERS:
            return {"ok": False, "reason": f"live broker must be one of {', '.join(LIVE_BROKERS)}"}
        with self._switch_lock:
            locked = self._locked()
            if locked:
                return {"ok": False, "reason": locked}
            pp = paper_platform or self.paper_platform
            lb = live_broker or self.live_broker
            if (pp, lb) == (self.paper_platform, self.live_broker):
                return {"ok": True, "note": "No change.", "venue": self._venue_state()}
            blocked = self._switch_blocked(venue_id(plan_venue(self.mode, pp, lb)))
            if blocked:
                return {"ok": False, "reason": blocked}
            prev = self.mode
            self.paper_platform, self.live_broker = pp, lb
            self._bind_trading_broker()
            self._after_switch(prev, operator)
            note = f"{'Live' if self.mode == 'live' else 'Paper'} orders go to {venue_label(self._trading_venue)}."
            if prev == "live" and self.mode == "paper":
                note = f"{LIVE_BROKERS[lb]} isn't ready, so you're back on paper. {note}"
            elif self._venue_blockers:
                note += " Not connected yet: " + "; ".join(self._venue_blockers)
            return {"ok": True, "note": note, "venue": self._venue_state()}

    def reconnect(self, operator: str = "reconnect") -> Dict[str, Any]:
        """Drop and re-open the broker connection - after starting the Gateway,
        changing keys or signing in. Allowed while quitting (exits may need it)."""
        with self._switch_lock:
            prev = self.mode
            self._close_venue()
            self._bind_trading_broker()
            self._after_switch(prev, operator)
        state = self._venue_state()
        problems = self._venue_blockers or (self._live_blockers if prev != self.mode else [])
        if problems:
            return {"ok": False, "reason": "; ".join(problems), "venue": state}
        return {"ok": True, "venue": state,
                "note": f"Connected - {'live' if self.mode == 'live' else 'paper'} orders go to "
                        f"{venue_label(self._trading_venue)}."}

    def save_secrets(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """Write broker settings from the Connections panel to .env, then
        reconnect if they belong to the broker in use."""
        try:
            changed = secrets_store.write(values or {})
        except ValueError as e:
            return {"ok": False, "reason": str(e)}
        self.settings.secrets = Secrets()           # the same Settings object everyone holds
        out: Dict[str, Any] = {"ok": True, "changed": changed, "fields": secrets_store.describe()}
        if not changed:
            out["note"] = "Nothing changed."
            return out
        log.info("dashboard updated .env: %s", ", ".join(changed))    # names only, never values
        in_use = plan_venue(self.mode, self.paper_platform, self.live_broker).broker
        if in_use and any(k.startswith(in_use.upper() + "_") for k in changed):
            r = self.reconnect(operator="settings saved")
            out["venue"] = r["venue"]
            out["note"] = "Saved. " + (r.get("note") or f"Not connected yet: {r.get('reason')}")
        else:
            out["note"] = "Saved."
        return out

    def _on_schwab_login(self) -> None:
        self.token_manager.note_full_auth(source="dashboard")
        BUS.publish("auth.reauth_ok", broker="schwab", source="dashboard")
        if plan_venue(self.mode, self.paper_platform, self.live_broker).broker == "schwab":
            self.reconnect(operator="Schwab sign-in")

    def probe_ibkr(self, account: str = "paper") -> Dict[str, Any]:
        """Read-only IBKR check for the Connections panel. Never places an order."""
        account = "live" if account == "live" else "paper"
        sec = self.settings.secrets
        port = self._ibkr_port(account)
        out: Dict[str, Any] = {"ok": False, "account_type": account, "host": sec.ibkr_host, "port": port}
        if not port_is_open(sec.ibkr_host, port):
            out["reason"] = (f"Nothing is listening on {sec.ibkr_host}:{port}. Start IB Gateway logged "
                             f"in to your {account} account, with the API enabled on that port.")
            return out
        plan = self._venue_plan
        shared = (plan.broker == "ibkr" and plan.account == account
                  and self._venue is not None and self._venue.is_connected)
        b = self._venue if shared else None
        try:
            if b is None:
                b = get_broker("ibkr", port=port, mode=account, readonly=True,
                               client_id=int(sec.ibkr_client_id) + 50)
                b.connect()
            acc = b.get_account()
            data = b.session_status()["market_data"]
            out.update(ok=True, account=secrets_store.mask(acc.account_id), market_data=data,
                       equity=round(acc.equity, 2), positions=len(acc.positions),
                       note=f"Connected to {account} account {secrets_store.mask(acc.account_id)} - "
                            f"{'real-time' if data == 'live' else '15-minute delayed'} market data.")
        except Exception as e:  # noqa: BLE001
            out["reason"] = f"The Gateway is up but the API connection failed: {e}"
        finally:
            if b is not None and not shared:
                try:
                    b.close()
                except Exception:  # noqa: BLE001
                    pass
        return out

    def setup_state(self) -> Dict[str, Any]:
        """Everything the Connections panel shows. Probes the IBKR ports, so
        it's served on demand rather than in every snapshot."""
        sec = self.settings.secrets
        ports = {a: self._ibkr_port(a) for a in ("paper", "live")}
        return {
            "venue": self._venue_state(),
            "fields": secrets_store.describe(),
            "ibkr": {
                "host": sec.ibkr_host, "ports": ports,
                "listening": {a: port_is_open(sec.ibkr_host, p) for a, p in ports.items()},
                "installed": find_spec("ib_async") is not None,
                "steps": list(IBKR_STEPS),
            },
            "schwab": {
                "installed": find_spec("schwab") is not None,
                "keys_set": bool(sec.schwab_api_key and sec.schwab_app_secret),
                "callback_url": sec.schwab_callback_url,
                "token": self.token_manager.status().as_dict(),
                "login": self.schwab_login.status(),
                "steps": list(SCHWAB_STEPS),
            },
        }

    # ------------------------------------------------------------------ #
    #  Views                                                            #
    # ------------------------------------------------------------------ #
    def _decorate(self, p: Play) -> Dict[str, Any]:
        row = p.to_row()
        row["executable_hint"] = p.suggested_qty > 0 and self._armed
        try:
            self.autopilot.decorate_play(row)
        except Exception:  # noqa: BLE001
            pass
        return row

    def _data_source(self) -> str:
        return self.md.providers[0].name if self.md.providers else "none"

    def _venue_state(self) -> Dict[str, Any]:
        want = plan_venue(self.mode, self.paper_platform, self.live_broker)
        connected = self._venue is not None and self._venue.is_connected
        ib = None
        if connected and self._venue_plan.broker == "ibkr":
            try:
                ib = self._venue.session_status()
            except Exception:  # noqa: BLE001
                ib = None
        src = self._data_source()
        return {
            "mode": self.mode,
            "paper_platform": self.paper_platform,
            "live_broker": self.live_broker,
            "paper_platforms": PAPER_PLATFORMS,
            "live_brokers": LIVE_BROKERS,
            "trading_on": self._trading_venue,
            "trading_on_label": venue_label(self._trading_venue),
            "wants": {"broker": want.broker, "account": want.account, "trade": want.trade},
            "connected": connected,
            "blockers": list(self._venue_blockers),
            "live_blockers": list(self._live_blockers),
            "ibkr_session": ib,
            "market_data": (ib or {}).get("market_data") or (
                "live" if src.startswith("broker:") else "delayed" if src == "yfinance" else "none"),
        }

    def _connection(self) -> Dict[str, str]:
        """The header pill: what orders go to, and whether that's healthy."""
        if self.mode == "paper" and self.paper_platform == "simulator":
            return {"label": "Simulator", "cls": "good",
                    "detail": f"Built-in simulator · data: {self._data_source().replace('broker:', '')}"}
        want = plan_venue(self.mode, self.paper_platform, self.live_broker)
        role = "live" if self.mode == "live" else ("paper" if want.trade else "data")
        ok = self._venue is not None and self._venue.is_connected
        why = "; ".join(self._venue_blockers)
        if want.broker == "ibkr":
            if ok:
                s = self._venue.session_status()
                return {"label": f"IBKR {role} ●", "cls": "good",
                        "detail": f"{s['message']} · port {s['port']} · {s['market_data']} data"}
            rec = bool(self._venue is not None and self._venue.session_status().get("reconnecting"))
            return {"label": f"IBKR {role} {'↻' if rec else '✕'}", "cls": "warn" if rec else "bad",
                    "detail": why or "IB Gateway isn't connected", "action": "connections"}
        st = self.token_manager.status()
        sec = self.settings.secrets
        if ok:
            if st.needs_rotation:
                return {"label": f"Schwab {role} · {max(0.0, st.days_until_expiry or 0):.0f}d",
                        "cls": "warn", "detail": st.message, "action": "schwab_login"}
            return {"label": f"Schwab {role} ●", "cls": "good", "detail": st.message}
        return {"label": f"Schwab {role} ✕", "cls": "bad", "detail": why or st.message,
                "action": "schwab_login" if (sec.schwab_api_key and sec.schwab_app_secret) else "connections"}

    def snapshot(self) -> Dict[str, Any]:
        acc = self._account
        paper = self.mode == "paper"
        try:
            pnl = self.repo.pnl_summary()
        except Exception:  # noqa: BLE001
            pnl = {}
        data_src = self._data_source()
        em = self.settings.config.exit_manager
        return {
            "ts": clock.now_ny().isoformat(),
            "broker": self.broker.name if self._trading_broker else "paper",
            "mode": self.mode,                       # paper | live
            "app_mode": self.settings.config.app.mode,
            "connected": self.broker.is_connected if self._trading_broker else False,
            "armed": self._armed,
            "market_open": clock.is_market_open(),
            "market": clock.market_status(),
            "exit_manager": {
                "enabled": bool(em.enabled),
                "breakeven_at_r": em.breakeven_at_r,
                "trail_start_r": em.trail_start_r,
                "trail_lock_ratio": em.trail_lock_ratio,
                "flatten_intraday_before_close_min": em.flatten_intraday_before_close_min,
                "max_swing_hold_days": em.max_swing_hold_days,
            },
            "autopilot": self.autopilot.status(),
            "data_source": data_src,
            "data_is_real": data_src != "synthetic",
            "venue": self._venue_state(),
            "connection": self._connection(),
            "filters": self.filters.as_dict(),
            "strategies_on": len(self.strategies),
            "quit": self._quit_status(),
            "account": {
                "equity": round(acc.equity, 2),
                "cash": round(acc.cash, 2),
                "buying_power": round(acc.buying_power, 2),
                "is_cash_account": acc.is_cash_account,
                "realized_pl_session": acc.raw.get("realized_pl") if acc.raw else None,
                "start_equity": acc.raw.get("start_equity") if acc.raw else None,
                "floor_enforced": not paper,
                "min_start_equity": self.settings.config.account.min_start_equity,
                "paper_start_cash": self.settings.config.account.paper_start_cash,
                "pdt_threshold": self.settings.config.account.pdt_equity_threshold,
            } if acc else None,
            "positions": [
                {"symbol": p.symbol, "qty": p.quantity, "avg_price": round(p.avg_price, 4),
                 "market_price": round(p.market_price, 4),
                 "unrealized_pl": round(p.unrealized_pl, 2)}
                for p in (acc.positions if acc else [])
            ],
            "day_trades_5d": self.repo.count_day_trades(5),
            "day_trade_limit": self.settings.config.account.max_day_trades_under_threshold,
            "pnl": pnl,
            "scan": self._last_scan_summary,
            "scan_interval_s": self._effective_scan_interval(),
            "scan_fast": self._autopilot_day_active(),
        }

    def current_plays(self) -> List[Dict[str, Any]]:
        return [self._decorate(p) for p in sorted(self._plays.values(),
                                                  key=lambda x: x.score, reverse=True)]

    def get_play(self, play_id: str) -> Optional[Dict[str, Any]]:
        p = self._plays.get(play_id)
        return self._decorate(p) if p else self.repo.get_play(play_id)


def _age_s(entry_time: Optional[str]) -> float:
    """Seconds since a trade's (naive-UTC) entry time; infinite if unknown."""
    if not entry_time:
        return float("inf")
    try:
        et = dt.datetime.fromisoformat(entry_time).replace(tzinfo=None)
    except ValueError:
        return float("inf")
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - et).total_seconds()


class _BrokerProvider:
    """Adapt a connected broker to the MarketDataService provider protocol."""

    #: brokers pace their own API (IBKR's loop, Schwab's client) - no extra spacing here
    min_request_gap = 0.0

    def __init__(self, broker: BrokerAdapter) -> None:
        self.broker = broker
        self.name = f"broker:{broker.name}"

    def history(self, symbol, interval, lookback_days, extended_hours):
        return self.broker.get_price_history(symbol, interval, lookback_days,
                                             extended_hours=extended_hours)

    def quote(self, symbol):
        return self.broker.get_quote(symbol)
