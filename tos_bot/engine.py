"""The conductor.

Owns the broker(s), scanner, executor, risk guard and token watchdog, and
runs three background loops:

    scan_loop      - every ``scanner.interval_seconds`` -> new short list of plays
    sync_loop      - a few seconds -> reconcile fills, drive the paper clock
    snapshot_loop  - ~30s -> write an account snapshot, broadcast state

It runs in one of two **modes**, toggled at runtime from the dashboard and
persisted to ``data/runtime.json``:

    paper  - simulated fills against **real** market data (default; no floor,
             starts from ``account.paper_start_cash``, default $100k)
    live   - real orders through the configured live broker (``schwab``)

It never routes an order without an explicit :meth:`approve_play`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .auth.token_manager import AuthWatchdog, TokenManager
from .brokers import get_broker
from .brokers.base import BrokerAdapter
from .config import PROJECT_ROOT, Settings, get_settings
from .core.eventbus import BUS
from .core.enums import PlayStatus
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
from .scanner.scanner import Scanner
from .strategies import build_enabled_strategies
from .strategies.registry import describe_all
from .util import clock
from .util.logging_setup import setup_logging

log = logging.getLogger(__name__)

# where the paper/live mode + autopilot knobs persist between runs.
# Overridable (tests point it at a temp file so a run never rewrites the user's).
_RUNTIME_PATH = Path(os.getenv("TOS_RUNTIME_PATH") or (PROJECT_ROOT / "data" / "runtime.json"))
_LIVE_BROKERS = {"schwab", "tda", "ibkr", "crypto"}


class TradingEngine:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        setup_logging(self.settings.config.app.log_level)

        init_db()
        self.repo = Repository()

        # market data feed: a real feed if we have one, else the synthetic demo
        # feed - never both (see MarketDataService). A live broker, if it
        # connects, is prepended in start().
        yfp = YFinanceProvider()
        self.md = MarketDataService(
            providers=[yfp] if yfp.available else [SyntheticProvider()]
        )
        if not yfp.available:
            log.warning("yfinance not installed - running on SYNTHETIC data. "
                        "`pip install yfinance` for real (delayed) quotes.")

        self.fundamentals = YFinanceFundamentals()
        self.strategies = build_enabled_strategies(self.settings)
        self.scanner = Scanner(self.settings, self.md, self.fundamentals, self.strategies)

        self.token_manager = TokenManager(self.settings, repo=self.repo, bus=BUS,
                                          on_reauth_required=self._on_reauth)

        # which live venue the toggle targets ("schwab" unless overridden)
        sec_broker = self.settings.secrets.broker
        self.live_name: Optional[str] = (
            sec_broker if sec_broker in _LIVE_BROKERS
            else (self.settings.secrets.live_broker if self.settings.secrets.live_broker in _LIVE_BROKERS
                  else None)
        )
        # starting mode: runtime.json wins; else "live" if .env named a live broker
        self.mode: str = self._load_runtime_mode(
            default="live" if sec_broker in _LIVE_BROKERS else "paper"
        )
        if self.mode == "live" and not self.live_name:
            self.mode = "paper"

        self._paper_broker: Optional[BrokerAdapter] = None
        self._live_broker: Optional[BrokerAdapter] = None
        self._trading_broker: Optional[BrokerAdapter] = None

        self.executor: Optional[Executor] = None
        self.exit_manager: Optional[ExitManager] = None
        self.pdt: Optional[PdtGuard] = None

        self._plays: Dict[str, Play] = {}
        self._last_scan_summary: Dict[str, Any] = {}
        self._account: Optional[Account] = None
        self._armed = False
        self._reauth_hint: Dict[str, Any] = {}
        self._live_blockers: List[str] = []

        # hands-off entry (exits are already automatic via ExitManager)
        self.autopilot = AutoPilot(self, self.settings.config.autopilot,
                                   bus=BUS, persist=self._save_runtime)
        self.autopilot.load_runtime(self._read_runtime().get("autopilot", {}))

        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._scan_now = threading.Event()
        self._switch_lock = threading.RLock()

    # ------------------------------------------------------------------ #
    @property
    def broker(self) -> BrokerAdapter:
        return self._trading_broker  # type: ignore[return-value]

    # ------------------------------------------------------------------ #
    #  Runtime state file                                               #
    # ------------------------------------------------------------------ #
    def _read_runtime(self) -> Dict[str, Any]:
        try:
            if _RUNTIME_PATH.exists():
                d = json.loads(_RUNTIME_PATH.read_text())
                if isinstance(d, dict):
                    return d
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _load_runtime_mode(self, default: str) -> str:
        m = self._read_runtime().get("mode")
        return m if m in ("paper", "live") else default

    def _save_runtime(self) -> None:
        try:
            _RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {"mode": self.mode}
            if getattr(self, "autopilot", None) is not None:
                payload["autopilot"] = self.autopilot.to_runtime()
            _RUNTIME_PATH.write_text(json.dumps(payload, indent=2))
        except Exception:  # noqa: BLE001
            log.debug("could not persist runtime state", exc_info=True)

    # ------------------------------------------------------------------ #
    #  Broker construction                                              #
    # ------------------------------------------------------------------ #
    def _ensure_paper_broker(self) -> BrokerAdapter:
        if self._paper_broker is None:
            import os

            self._paper_broker = get_broker(
                "paper",
                starting_cash=self.settings.config.account.paper_start_cash,
                data_service=self.md,
                persist=os.getenv("PAPER_PERSIST", "1") != "0",
            )
            self._paper_broker.connect()
        return self._paper_broker

    def _ensure_live_broker(self) -> Optional[BrokerAdapter]:
        """Build + connect the live broker once. Also used as the top market-
        data provider even while trading on paper."""
        if self._live_broker is not None:
            return self._live_broker
        self._live_blockers = []
        if not self.live_name:
            self._live_blockers.append("no live broker configured (BROKER / LIVE_BROKER in .env)")
            return None

        sec = self.settings.secrets
        if self.live_name == "schwab" and not (sec.schwab_api_key and sec.schwab_app_secret):
            self._live_blockers.append("SCHWAB_API_KEY / SCHWAB_APP_SECRET missing in .env")
        if self.live_name in ("schwab", "tda") and not sec.token_path.exists():
            self._live_blockers.append(
                f"no OAuth token - run  python scripts/authenticate.py  (BROKER={self.live_name})"
            )
        if self._live_blockers:
            return None

        try:
            b = get_broker(self.live_name, token_manager=self.token_manager)
            b.connect()
            self._live_broker = b
            log.info("live broker '%s' connected", self.live_name)
            return b
        except Exception as e:  # noqa: BLE001
            self._live_blockers.append(str(e))
            log.warning("live broker unavailable: %s", e)
            return None

    def _bind_trading_broker(self) -> None:
        """Point the executor + PDT guard at the broker for the current mode."""
        if self.mode == "live":
            lb = self._ensure_live_broker()
            self._trading_broker = lb or self._ensure_paper_broker()
            if lb is None:
                self.mode = "paper"
                log.warning("falling back to paper - live broker not ready: %s",
                            "; ".join(self._live_blockers))
        else:
            self._trading_broker = self._ensure_paper_broker()

        is_paper = self._trading_broker.paper
        self.pdt = PdtGuard(self.settings.config.account, trade_repo=self.repo, paper=is_paper)
        if self.executor is None:
            self.executor = Executor(self._trading_broker, self.repo,
                                     self.settings.config.execution, bus=BUS)
        else:
            self.executor.rebind(self._trading_broker)

        # the automatic exit strategy - quotes come from the active trading broker
        self.exit_manager = ExitManager(
            self.repo, self.executor,
            quote_fn=lambda s: self._trading_broker.get_quote(s),
            cfg=self.settings.config.exit_manager, bus=BUS,
        )

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        log.info("engine starting  (mode=%s, live_target=%s, app_mode=%s)",
                 self.mode, self.live_name, self.settings.config.app.mode)

        # a Schwab client (if creds exist) becomes the top data feed in BOTH
        # modes, so paper fills happen against real quotes.
        lb = self._ensure_live_broker()
        if lb is not None and lb.is_connected:
            self.md.providers.insert(0, _BrokerProvider(lb))
            log.info("market data feed: %s (real)", lb.name)

        self._bind_trading_broker()
        self._refresh_account()
        self._check_arm()

        self._threads = [
            threading.Thread(target=self._scan_loop, name="scan-loop", daemon=True),
            threading.Thread(target=self._sync_loop, name="sync-loop", daemon=True),
            threading.Thread(target=self._snapshot_loop, name="snapshot-loop", daemon=True),
        ]
        for t in self._threads:
            t.start()

        self._auth_watchdog = AuthWatchdog(
            self.token_manager, broker_provider=lambda: self._live_broker,
            interval_s=self.settings.config.auth.check_interval_seconds,
        )
        if self._live_broker is not None:
            self._auth_watchdog.start()

        BUS.publish("engine.started", state=self.snapshot())

    def stop(self) -> None:
        self._stop.set()
        try:
            self._auth_watchdog.stop()
        except Exception:  # noqa: BLE001
            pass
        for b in (self._live_broker, self._paper_broker):
            try:
                if b:
                    b.close()
            except Exception:  # noqa: BLE001
                pass
        log.info("engine stopped")

    # ------------------------------------------------------------------ #
    #  Background loops                                                  #
    # ------------------------------------------------------------------ #
    def _scan_loop(self) -> None:
        interval = int(self.settings.config.scanner.interval_seconds)
        self._stop.wait(2.0)
        while not self._stop.is_set():
            triggered = self._scan_now.is_set()
            self._scan_now.clear()
            if triggered or self._should_scan():
                try:
                    self._run_scan()
                except Exception:  # noqa: BLE001
                    log.exception("scan cycle failed")
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
                if self.exit_manager:
                    acted = self.exit_manager.run_once()
                    if acted:
                        self._refresh_account()
                        BUS.publish("account.snapshot", state=self.snapshot())
            except Exception:  # noqa: BLE001
                log.exception("exit manager tick failed")
            self._stop.wait(4.0)

    def _snapshot_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_account()
                if self._account:
                    self.repo.snapshot_account(
                        self._account, self.broker.name,
                        realized_day=self.repo.pnl_summary().get("realized_today", 0.0),
                    )
                BUS.publish("account.snapshot", state=self.snapshot())
            except Exception:  # noqa: BLE001
                log.exception("snapshot failed")
            self._stop.wait(30.0)

    def _should_scan(self) -> bool:
        return clock.is_market_open()

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

        try:
            self.repo.record_scan(result,
                                  keep_rejected=self.settings.config.database.record_rejected_plays)
        except Exception:  # noqa: BLE001
            log.exception("failed to persist scan")

        BUS.publish("plays.updated",
                    plays=[self._decorate(p) for p in list(self._plays.values())[:80]],
                    scan=self._last_scan_summary)

        # hands-off entry: let the pilot act on the fresh plays (no-op unless armed)
        try:
            self.autopilot.consider(self._plays)
        except Exception:  # noqa: BLE001
            log.exception("autopilot pass failed")

    # ------------------------------------------------------------------ #
    #  Account / arming                                                 #
    # ------------------------------------------------------------------ #
    def _refresh_account(self) -> None:
        try:
            if self._trading_broker:
                self._account = self._trading_broker.get_account()
        except Exception as e:  # noqa: BLE001
            log.debug("get_account failed: %s", e)

    def _check_arm(self) -> None:
        acc = self._account
        if self.broker.paper:
            self._armed = True                      # paper has no equity floor
            return
        floor = self.settings.config.account.min_start_equity
        if acc and acc.equity >= floor:
            self._armed = True
        else:
            self._armed = False
            if acc:
                BUS.publish("engine.disarmed",
                            reason=f"equity ${acc.equity:,.0f} < ${floor:,.0f} live floor")

    # ------------------------------------------------------------------ #
    #  Operator actions (called by the API)                             #
    # ------------------------------------------------------------------ #
    #: play statuses that must never be re-executed
    _DONE_STATUSES = {"ACCEPTED", "SUBMITTED", "WORKING", "PARTIAL", "FILLED", "ERROR"}

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
        can = (
            not already_done and plan.get("executable", False)
            and decision.allowed and p.suggested_qty > 0 and self._armed and rr_ok
        )
        reasons: List[str] = []
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
                "routes_to": ("SIMULATED (paper)" if self.broker.paper
                              else f"LIVE {self.broker.name.upper()}"),
            },
        }

    def approve_play(self, play_id: str, operator: str = "operator") -> Dict[str, Any]:
        with self._switch_lock:
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
        out = self.executor.close_trade(trade_id, reason=reason)
        self._refresh_account()
        BUS.publish("account.snapshot", state=self.snapshot())
        return out

    def set_trade_managed(self, trade_id: str, on: bool) -> Dict[str, Any]:
        self.repo.update_trade_risk(trade_id, managed_exit=bool(on))
        return {"ok": True, "trade_id": trade_id, "managed_exit": bool(on)}

    # ---- autopilot (hands-off entry) --------------------------------- #
    def set_autopilot(self, **kw: Any) -> Dict[str, Any]:
        """Toggle / tune hands-off entry from the dashboard. Exits are already
        automatic; this governs whether the bot also takes the entry."""
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

    def reconcile_paper(self) -> Dict[str, Any]:
        """Rebuild the paper broker's positions from the OPEN trades in the
        database - fixes any drift (e.g. from a double-submit)."""
        if not self.broker.paper:
            return {"ok": False, "reason": "only available in paper mode"}
        pb = self._ensure_paper_broker()
        from .core.models import Position
        book: Dict[str, list] = {}
        for t in self.repo.open_trades():
            sym = t["symbol"]
            qty = float(t["quantity"]) * (1 if t["side"] == "LONG" else -1)
            px = float(t["entry_price"])
            b = book.setdefault(sym, [0.0, 0.0])
            b[0] += qty
            b[1] += qty * px
        new_positions = {}
        for sym, (q, notional) in book.items():
            if abs(q) < 1e-9:
                continue
            new_positions[sym] = Position(symbol=sym, quantity=q,
                                          avg_price=abs(notional / q) if q else 0.0)
        before = {p.symbol: p.quantity for p in pb.get_account().positions}
        pb._positions = new_positions            # type: ignore[attr-defined]
        pb._save_state()                         # type: ignore[attr-defined]
        self.executor.rebind(pb)
        self._refresh_account()
        after = {s: p.quantity for s, p in new_positions.items()}
        BUS.publish("account.snapshot", state=self.snapshot())
        log.warning("paper positions reconciled from DB: %s -> %s", before, after)
        return {"ok": True, "before": before, "after": after,
                "note": "paper positions rebuilt from open trades in the database"}

    def trigger_scan(self) -> Dict[str, Any]:
        self._scan_now.set()
        return {"ok": True, "note": "scan queued"}

    # ---- paper / live toggle ------------------------------------- #
    def set_mode(self, mode: str, operator: str = "operator") -> Dict[str, Any]:
        mode = (mode or "").lower()
        if mode not in ("paper", "live"):
            return {"ok": False, "reason": "mode must be 'paper' or 'live'"}
        with self._switch_lock:
            if mode == self.mode:
                return {"ok": True, "mode": self.mode, "note": "already in that mode"}
            if mode == "live":
                if self._ensure_live_broker() is None:
                    return {"ok": False, "reason": "live broker not ready",
                            "blockers": self._live_blockers}
            prev = self.mode
            self.mode = mode
            self._bind_trading_broker()
            self._save_runtime()
            self._refresh_account()
            self._check_arm()
            log.warning("mode switched %s -> %s by %s", prev, self.mode, operator)
            BUS.publish("broker.switched", mode=self.mode, prev=prev, state=self.snapshot())
            return {"ok": True, "mode": self.mode,
                    "note": ("Now routing REAL orders." if self.mode == "live"
                             else "Back to simulated fills on real data.")}

    def reset_paper(self, cash: Optional[float] = None) -> Dict[str, Any]:
        pb = self._ensure_paper_broker()
        amount = float(cash) if cash is not None else self.settings.config.account.paper_start_cash
        pb.reset(amount)  # type: ignore[attr-defined]
        if self.executor and self.broker.paper:
            self.executor.rebind(pb)
        self._refresh_account()
        BUS.publish("account.snapshot", state=self.snapshot())
        return {"ok": True, "cash": round(amount, 2),
                "note": f"paper account reset to ${amount:,.0f}"}

    def reauthenticate(self) -> Dict[str, Any]:
        if not self.live_name:
            return {"ok": False, "reason": "no live broker configured"}
        self.token_manager.rotate_now("operator-initiated re-authentication")
        return {"ok": True, "note": "re-auth started - follow the console / browser prompt",
                "hint": self._reauth_hint}

    # ------------------------------------------------------------------ #
    def _on_reauth(self, status) -> None:
        self._reauth_hint = {
            "message": "Broker re-authentication required.",
            "how": "Run  python scripts/authenticate.py  (opens a browser once), "
                   "or click Re-authenticate.",
            "status": status.as_dict(),
        }
        log.warning("re-auth required: %s", status.message)

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

    def snapshot(self) -> Dict[str, Any]:
        acc = self._account
        paper = self.broker.paper if self._trading_broker else True
        pnl = {}
        try:
            pnl = self.repo.pnl_summary()
        except Exception:  # noqa: BLE001
            pass
        tok = ({"exists": True, "broker": "paper", "message": "paper - no token",
                "needs_reauth": False} if paper
               else self.token_manager.status().as_dict())
        data_src = self.md.providers[0].name if self.md.providers else "none"
        em = self.settings.config.exit_manager
        return {
            "ts": clock.now_ny().isoformat(),
            "broker": self.broker.name if self._trading_broker else "paper",
            "mode": self.mode,                       # paper | live
            "app_mode": self.settings.config.app.mode,  # suggest (never auto-trades)
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
            "live": {
                "target": self.live_name,
                "available": bool(self.live_name),
                "ready": self._live_broker is not None and self._live_broker.is_connected,
                "blockers": self._live_blockers,
                "account_hint": (self.settings.secrets.schwab_account_id[-4:]
                                 if self.settings.secrets.schwab_account_id else None),
            },
            "account": {
                "equity": round(acc.equity, 2) if acc else None,
                "cash": round(acc.cash, 2) if acc else None,
                "buying_power": round(acc.buying_power, 2) if acc else None,
                "is_cash_account": acc.is_cash_account if acc else None,
                "realized_pl_session": (acc.raw.get("realized_pl") if acc and acc.raw else None),
                "start_equity": (acc.raw.get("start_equity") if acc and acc.raw else None),
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
            "token": tok,
            "reauth_hint": self._reauth_hint,
        }

    def current_plays(self) -> List[Dict[str, Any]]:
        return [self._decorate(p) for p in sorted(self._plays.values(),
                                                  key=lambda x: x.score, reverse=True)]

    def get_play(self, play_id: str) -> Optional[Dict[str, Any]]:
        p = self._plays.get(play_id)
        return self._decorate(p) if p else self.repo.get_play(play_id)

    def strategy_catalog(self) -> List[Dict[str, str]]:
        return describe_all()


class _BrokerProvider:
    """Adapt a connected broker to the MarketDataService provider protocol."""

    def __init__(self, broker: BrokerAdapter) -> None:
        self.broker = broker
        self.name = f"broker:{broker.name}"

    def history(self, symbol, interval, lookback_days, extended_hours):
        return self.broker.get_price_history(symbol, interval, lookback_days,
                                             extended_hours=extended_hours)

    def quote(self, symbol):
        return self.broker.get_quote(symbol)
