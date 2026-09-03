"""The conductor.

Owns the broker, scanner, executor, risk guard and token watchdog, and runs
three background loops:

    scan_loop      - every ``scanner.interval_seconds`` -> new short list of plays
    sync_loop      - a few seconds -> reconcile fills, drive the paper clock
    snapshot_loop  - ~30s -> write an account snapshot, broadcast state

It never routes an order without an explicit :meth:`approve_play`, which the
dashboard calls when the operator clicks "Yes".
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from .auth.token_manager import AuthWatchdog, TokenManager
from .brokers import get_broker
from .brokers.base import BrokerAdapter
from .config import Settings, get_settings
from .core.eventbus import BUS
from .core.enums import PlayStatus
from .core.models import Account, Play
from .data.fundamentals import YFinanceFundamentals
from .data.market_data import MarketDataService, SyntheticProvider, YFinanceProvider
from .execution.executor import Executor
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


class TradingEngine:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        setup_logging(self.settings.config.app.log_level)

        init_db()
        self.repo = Repository()

        # market data: broker first (added after connect), then yfinance, then synthetic
        providers = []
        yfp = YFinanceProvider()
        if yfp.available:
            providers.append(yfp)
        providers.append(SyntheticProvider())
        self.md = MarketDataService(providers=providers)

        self.fundamentals = YFinanceFundamentals()
        self.strategies = build_enabled_strategies(self.settings)
        self.scanner = Scanner(self.settings, self.md, self.fundamentals, self.strategies)
        self.pdt = PdtGuard(self.settings.config.account, trade_repo=self.repo)

        self.broker: BrokerAdapter = get_broker(
            self.settings.secrets.broker,
            **self._broker_kwargs(self.settings.secrets.broker),
        )
        self.token_manager = TokenManager(self.settings, repo=self.repo, bus=BUS,
                                          on_reauth_required=self._on_reauth)
        self.executor = Executor(self.broker, self.repo, self.settings.config.execution, bus=BUS)

        self._plays: Dict[str, Play] = {}
        self._last_scan_summary: Dict[str, Any] = {}
        self._account: Optional[Account] = None
        self._armed = False
        self._reauth_hint: Dict[str, Any] = {}

        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._scan_now = threading.Event()

    # ------------------------------------------------------------------ #
    def _broker_kwargs(self, name: str) -> dict:
        if name == "paper":
            return {"starting_cash": max(2000.0, self.settings.config.account.min_start_equity),
                    "data_service": self.md}
        return {"token_manager": self.token_manager}

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        log.info("engine starting  (broker=%s, mode=%s)",
                 self.settings.secrets.broker, self.settings.config.app.mode)
        try:
            self.broker.connect()
        except Exception as e:  # noqa: BLE001
            log.error("broker connect failed: %s", e)
            BUS.publish("broker.error", message=str(e))

        # if a real broker connected, prefer its data feed
        if self.broker.is_connected and not self.broker.paper:
            self.md.providers.insert(0, _BrokerProvider(self.broker))

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
            self.token_manager, broker_provider=lambda: self.broker,
            interval_s=self.settings.config.auth.check_interval_seconds,
        )
        if not self.broker.paper:
            self._auth_watchdog.start()

        BUS.publish("engine.started", state=self.snapshot())

    def stop(self) -> None:
        self._stop.set()
        try:
            self._auth_watchdog.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.broker.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("engine stopped")

    # ------------------------------------------------------------------ #
    #  Background loops                                                  #
    # ------------------------------------------------------------------ #
    def _scan_loop(self) -> None:
        interval = int(self.settings.config.scanner.interval_seconds)
        self._stop.wait(2.0)                       # let the UI come up first
        while not self._stop.is_set():
            triggered = self._scan_now.is_set()
            self._scan_now.clear()
            if triggered or self._should_scan():
                try:
                    self._run_scan()
                except Exception:  # noqa: BLE001
                    log.exception("scan cycle failed")
            # sleep the interval in 1s slices so a manual trigger stays responsive
            waited = 0
            while waited < interval and not self._stop.is_set() and not self._scan_now.is_set():
                self._stop.wait(1.0)
                waited += 1

    def _sync_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.executor.sync_open_orders()
            except Exception:  # noqa: BLE001
                log.exception("order sync failed")
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

    # ------------------------------------------------------------------ #
    def _should_scan(self) -> bool:
        # scan during RTH; also allow a warm-up 15 min before the open
        if clock.is_market_open():
            return True
        return False

    def _run_scan(self) -> None:
        self._refresh_account()
        if self._account:
            self.scanner.set_account(self._account)
        result = self.scanner.run_cycle()

        # size + guard every play, keep them addressable by id
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

    # ------------------------------------------------------------------ #
    #  Account / arming                                                 #
    # ------------------------------------------------------------------ #
    def _refresh_account(self) -> None:
        try:
            self._account = self.broker.get_account()
        except Exception as e:  # noqa: BLE001
            log.debug("get_account failed: %s", e)

    def _check_arm(self) -> None:
        acc = self._account
        floor = self.settings.config.account.min_start_equity
        if acc and acc.equity >= floor:
            self._armed = True
        else:
            self._armed = False
            if acc:
                BUS.publish("engine.disarmed",
                            reason=f"equity ${acc.equity:,.0f} < ${floor:,.0f} floor")

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
        rr_ok = p.reward_risk >= self.settings.config.risk.min_reward_risk or p.kind.value == "FUNDAMENTAL"
        can = decision.allowed and p.suggested_qty > 0 and self._armed and rr_ok
        reasons = []
        if not self._armed:
            reasons.append(f"engine not armed - equity below ${self.settings.config.account.min_start_equity:,.0f}")
        if not decision.allowed:
            reasons.append(decision.reason)
        if p.suggested_qty <= 0:
            reasons.append("position size rounds to zero for this risk budget")
        if not rr_ok:
            reasons.append(f"reward:risk {p.reward_risk:.1f} below minimum")
        return {
            "ok": True, "can_execute": can, "reasons": reasons,
            "play": self._decorate(p),
            "pdt": decision.as_dict(),
            "order_preview": {
                "side": p.side.entry_action, "qty": p.suggested_qty,
                "type": self.settings.config.execution.default_order_type,
                "limit_hint": p.entry, "bracket": self.settings.config.execution.bracket_orders,
                "take_profit": p.primary_target, "stop_loss": p.stop,
                "est_cost": round(p.notional, 2), "est_risk": round(p.dollar_risk, 2),
            },
        }

    def approve_play(self, play_id: str, operator: str = "operator") -> Dict[str, Any]:
        pre = self.assess_play(play_id)
        if not pre.get("ok"):
            return pre
        if not pre["can_execute"]:
            return {"ok": False, "reason": "; ".join(pre["reasons"]) or "not executable"}
        p = self._plays[play_id]
        p.status = PlayStatus.ACCEPTED
        self.repo.set_play_status(p.id, "ACCEPTED", operator)
        out = self.executor.execute_play(p, self._account)
        self.repo.set_play_status(p.id, p.status.value, operator)
        BUS.publish("play.decided", play_id=p.id, decision="approved", result=out)
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
        return out

    def trigger_scan(self) -> Dict[str, Any]:
        self._scan_now.set()
        return {"ok": True, "note": "scan queued"}

    def reauthenticate(self) -> Dict[str, Any]:
        if self.broker.paper:
            return {"ok": True, "note": "paper broker needs no auth"}
        self.token_manager.rotate_now("operator-initiated re-authentication")
        return {"ok": True, "note": "re-auth started - follow the console / browser prompt",
                "hint": self._reauth_hint}

    # ------------------------------------------------------------------ #
    def _on_reauth(self, status) -> None:
        self._reauth_hint = {
            "message": "Broker re-authentication required.",
            "how": "Run  python scripts/authenticate.py  (opens a browser once), "
                   "or click Re-authenticate to launch it.",
            "status": status.as_dict(),
        }
        log.warning("re-auth required: %s", status.message)

    # ------------------------------------------------------------------ #
    #  Views                                                            #
    # ------------------------------------------------------------------ #
    def _decorate(self, p: Play) -> Dict[str, Any]:
        row = p.to_row()
        row["executable_hint"] = p.suggested_qty > 0 and self._armed
        return row

    def snapshot(self) -> Dict[str, Any]:
        acc = self._account
        pnl = {}
        try:
            pnl = self.repo.pnl_summary()
        except Exception:  # noqa: BLE001
            pass
        tok = self.token_manager.status().as_dict() if not self.broker.paper else {
            "exists": True, "broker": "paper", "message": "paper - no token", "needs_reauth": False,
        }
        return {
            "ts": clock.now_ny().isoformat(),
            "broker": self.broker.name,
            "connected": self.broker.is_connected,
            "armed": self._armed,
            "market_open": clock.is_market_open(),
            "mode": self.settings.config.app.mode,
            "account": {
                "equity": round(acc.equity, 2) if acc else None,
                "cash": round(acc.cash, 2) if acc else None,
                "buying_power": round(acc.buying_power, 2) if acc else None,
                "is_cash_account": acc.is_cash_account if acc else None,
                "min_start_equity": self.settings.config.account.min_start_equity,
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
