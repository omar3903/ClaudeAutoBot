"""Hands-off entry.

The exit side of the bot is *already* fully automatic - :class:`ExitManager`
manages every open trade with no input. ``AutoPilot`` is the piece that also
takes the **entry** without a human click: after each scan it walks the fresh
plays, applies a deliberately strict gate, and for the survivors calls the
exact same ``engine.approve_play`` path the dashboard's "Yes" button uses.

Design intent (Aziz Rule 5 / Rule 10, Douglas "act on your edge without
hesitation, but predefine the risk"):

* **Paper-first.**  Auto-routing a *real* order needs ``autopilot.allow_live:
  true`` in ``config.yaml`` - a stray UI toggle can never spend real money.
* **Narrow by design.**  Only trade types you opted into, only above a
  confidence floor, only >= 2:1 reward:risk, hard caps on concurrent
  positions, per-day count and aggregate open risk.
* **Every decision is an event.**  ``autopilot.entered`` / ``autopilot.skipped``
  / ``autopilot.blocked`` on the bus so the UI and the log can show exactly
  what it did and why.

It holds no broker or DB handles of its own; it drives the engine.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..core.eventbus import BUS
from ..util import clock

log = logging.getLogger(__name__)


class AutoPilot:
    def __init__(self, engine: Any, cfg: Any, *, bus: Any = BUS,
                 persist: Optional[Callable[[], None]] = None) -> None:
        self.engine = engine
        self.cfg = cfg                          # AutopilotCfg (mutated live by the UI)
        self.bus = bus
        self._persist = persist or (lambda: None)

        # runtime state (the UI can override the config values below)
        self.enabled: bool = bool(cfg.enabled)
        self.trade_types: List[str] = [t.upper() for t in (cfg.trade_types or ["INTRADAY"])]
        self.min_confidence: float = float(cfg.min_confidence)
        self.min_reward_risk: float = float(cfg.min_reward_risk)
        self.max_auto_positions: int = int(cfg.max_auto_positions)
        self.max_auto_trades_per_day: int = int(cfg.max_auto_trades_per_day)
        self.max_per_strategy: int = int(getattr(cfg, "max_per_strategy", 2))
        self.max_new_per_cycle: int = int(getattr(cfg, "max_new_per_cycle", 1))
        self.cooldown_after_loss: bool = bool(getattr(cfg, "cooldown_after_loss", True))
        self.dry_run: bool = bool(cfg.dry_run)

        self._acted: set[str] = set()          # play ids already handled
        self._auto_trade_ids: set[str] = set()  # trades this pilot opened (this session)
        self._day: str = ""
        self._count_today: int = 0
        self._last_reason: Dict[str, str] = {}  # play_id -> why skipped (for the UI)
        self._blocked_note: str = ""

    # ------------------------------------------------------------------ #
    #  Persistable slice (goes into data/runtime.json alongside `mode`)  #
    # ------------------------------------------------------------------ #
    def to_runtime(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "trade_types": self.trade_types,
            "min_confidence": self.min_confidence,
            "min_reward_risk": self.min_reward_risk,
            "max_auto_positions": self.max_auto_positions,
            "max_auto_trades_per_day": self.max_auto_trades_per_day,
            "max_per_strategy": self.max_per_strategy,
            "max_new_per_cycle": self.max_new_per_cycle,
            "cooldown_after_loss": self.cooldown_after_loss,
            "dry_run": self.dry_run,
            "day": self._day,
            "count_today": self._count_today,
        }

    def load_runtime(self, d: Dict[str, Any]) -> None:
        if not isinstance(d, dict):
            return
        self.enabled = bool(d.get("enabled", self.enabled))
        tt = d.get("trade_types")
        if isinstance(tt, list) and tt:
            self.trade_types = [str(x).upper() for x in tt if str(x).upper() in ("INTRADAY", "SWING")] or self.trade_types
        for k in ("min_confidence", "min_reward_risk"):
            if isinstance(d.get(k), (int, float)):
                setattr(self, k, float(d[k]))
        for k in ("max_auto_positions", "max_auto_trades_per_day",
                  "max_per_strategy", "max_new_per_cycle"):
            if isinstance(d.get(k), int):
                setattr(self, k, int(d[k]))
        if "cooldown_after_loss" in d:
            self.cooldown_after_loss = bool(d["cooldown_after_loss"])
        self.dry_run = bool(d.get("dry_run", self.dry_run))
        # only restore the day counter if it is still the same session
        if d.get("day") == clock.session_date().isoformat():
            self._day = d["day"]
            self._count_today = int(d.get("count_today", 0))

    # ------------------------------------------------------------------ #
    def configure(self, **kw: Any) -> Dict[str, Any]:
        """Update the live knobs from the UI. Unknown keys are ignored."""
        if "enabled" in kw:
            self.enabled = bool(kw["enabled"])
        if "dry_run" in kw:
            self.dry_run = bool(kw["dry_run"])
        tt = kw.get("trade_types")
        if isinstance(tt, list):
            clean = [str(x).upper() for x in tt if str(x).upper() in ("INTRADAY", "SWING")]
            if clean:
                self.trade_types = clean
        if isinstance(kw.get("min_confidence"), (int, float)):
            self.min_confidence = max(0.0, min(1.0, float(kw["min_confidence"])))
        if isinstance(kw.get("min_reward_risk"), (int, float)):
            self.min_reward_risk = max(1.0, float(kw["min_reward_risk"]))
        if isinstance(kw.get("max_auto_positions"), int):
            self.max_auto_positions = max(0, int(kw["max_auto_positions"]))
        if isinstance(kw.get("max_auto_trades_per_day"), int):
            self.max_auto_trades_per_day = max(0, int(kw["max_auto_trades_per_day"]))
        if isinstance(kw.get("max_per_strategy"), int):
            self.max_per_strategy = max(1, int(kw["max_per_strategy"]))
        if isinstance(kw.get("max_new_per_cycle"), int):
            self.max_new_per_cycle = max(1, int(kw["max_new_per_cycle"]))
        if "cooldown_after_loss" in kw:
            self.cooldown_after_loss = bool(kw["cooldown_after_loss"])
        self._persist()
        self.bus.publish("autopilot.config", **self.status())
        log.info("autopilot reconfigured: %s", self.status())
        return self.status()

    # ------------------------------------------------------------------ #
    def _roll_day(self) -> None:
        today = clock.session_date().isoformat()
        if today != self._day:
            self._day = today
            self._count_today = 0
            self._acted.clear()

    def _live_ok(self) -> bool:
        """Real orders only when the config file explicitly allows it."""
        if getattr(self.engine, "mode", "paper") != "live":
            return True
        return bool(getattr(self.cfg, "allow_live", False))

    def day_mode_active(self, market_open: bool) -> bool:
        """True when the pilot is armed, cleared to act, day-trading is one of
        its enabled types, and the regular session is open. The engine uses
        this to scan (and refresh the account) much more often."""
        return bool(
            self.enabled and self._live_ok() and market_open
            and "INTRADAY" in self.trade_types
        )

    # ------------------------------------------------------------------ #
    def status(self) -> Dict[str, Any]:
        self._roll_day()
        open_auto = len(self._open_auto_trades())
        blocked = ""
        if self.enabled and not self._live_ok():
            blocked = ("Autopilot is paper-only until you set  autopilot.allow_live: true  "
                       "in config/config.yaml. It will not route live orders.")
        return {
            "enabled": self.enabled,
            "effective": self.enabled and self._live_ok(),
            "dry_run": self.dry_run,
            "trade_types": list(self.trade_types),
            "min_confidence": round(self.min_confidence, 2),
            "min_reward_risk": round(self.min_reward_risk, 2),
            "max_auto_positions": self.max_auto_positions,
            "max_auto_trades_per_day": self.max_auto_trades_per_day,
            "max_per_strategy": self.max_per_strategy,
            "max_new_per_cycle": self.max_new_per_cycle,
            "cooldown_after_loss": self.cooldown_after_loss,
            "open_auto_positions": open_auto,
            "auto_trades_today": self._count_today,
            "mode": getattr(self.engine, "mode", "paper"),
            "allow_live": bool(getattr(self.cfg, "allow_live", False)),
            "blocked_note": blocked,
        }

    # ------------------------------------------------------------------ #
    def _open_auto_trades(self) -> List[Dict[str, Any]]:
        try:
            opens = self.engine.repo.open_trades()
        except Exception:  # noqa: BLE001
            return []
        return [t for t in opens if t["id"] in self._auto_trade_ids]

    def _open_risk_dollars(self) -> float:
        total = 0.0
        for t in self._open_auto_trades():
            entry = t.get("entry_price") or 0.0
            stp = t.get("initial_stop_price") or t.get("stop_price") or 0.0
            qty = t.get("quantity") or 0.0
            if entry and stp and qty:
                total += abs(entry - stp) * qty
        return total

    # ------------------------------------------------------------------ #
    def consider(self, plays: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Called by the engine right after a scan publishes fresh plays.
        Returns the list of actions taken (for tests / logging)."""
        self._roll_day()
        if not self.enabled:
            return []
        if not self._live_ok():
            if self._blocked_note != "live":
                self._blocked_note = "live"
                self.bus.publish("autopilot.blocked", reason=self.status()["blocked_note"])
            return []
        self._blocked_note = ""

        acct = getattr(self.engine, "_account", None)
        equity = float(getattr(acct, "equity", 0.0) or 0.0)
        actions: List[Dict[str, Any]] = []
        taken = 0                              # new entries opened this cycle

        # highest-conviction first
        ordered = sorted(plays.values(), key=lambda p: getattr(p, "score", 0.0), reverse=True)
        for p in ordered:
            if p.id in self._acted:
                continue
            if taken >= self.max_new_per_cycle:
                self._last_reason[p.id] = f"one entry per scan cycle (max_new_per_cycle={self.max_new_per_cycle})"
                continue
            gate = self._pre_gate(p, equity)
            if gate is not None:
                self._last_reason[p.id] = gate
                continue

            # per-day + concurrency caps (re-checked every pass)
            if self._count_today >= self.max_auto_trades_per_day:
                self._last_reason[p.id] = f"daily auto-trade cap ({self.max_auto_trades_per_day}) reached"
                continue
            opens = self._open_auto_trades()
            if len(opens) >= self.max_auto_positions:
                self._last_reason[p.id] = f"max concurrent auto positions ({self.max_auto_positions}) reached"
                continue
            same_strat = sum(1 for t in opens if t.get("strategy") == p.strategy)
            if same_strat >= self.max_per_strategy:
                self._last_reason[p.id] = (f"already holding {same_strat} auto "
                                           f"'{p.strategy}' (max_per_strategy={self.max_per_strategy})")
                continue

            # full engine assessment (session validity, PDT, sizing, arm, RR)
            try:
                pre = self.engine.assess_play(p.id)
            except Exception as e:  # noqa: BLE001
                self._last_reason[p.id] = f"assess failed: {e}"
                continue
            if not pre.get("ok") or not pre.get("can_execute"):
                reason = "; ".join(pre.get("reasons", [])) or "not executable"
                self._last_reason[p.id] = reason
                self.bus.publish("autopilot.skipped", play_id=p.id, symbol=p.symbol,
                                 strategy=p.strategy, reason=reason)
                self._acted.add(p.id)
                continue

            est_risk = float(pre["order_preview"].get("est_risk", 0.0) or 0.0)
            if equity and (self._open_risk_dollars() + est_risk) > equity * float(self.cfg.max_open_risk_pct) / 100.0:
                self._last_reason[p.id] = (f"would exceed {self.cfg.max_open_risk_pct:.0f}% aggregate open "
                                           f"auto-risk")
                continue

            self._acted.add(p.id)
            if self.dry_run:
                taken += 1
                actions.append({"play_id": p.id, "symbol": p.symbol, "action": "would_enter",
                                "qty": pre["order_preview"]["qty"], "risk": est_risk})
                self.bus.publish("autopilot.would_enter", play_id=p.id, symbol=p.symbol,
                                 strategy=p.strategy, side=p.side.value,
                                 qty=pre["order_preview"]["qty"], est_risk=est_risk,
                                 note="dry-run - no order placed")
                log.info("autopilot DRY-RUN would enter %s %s x%s (risk $%.0f)",
                         p.side.value, p.symbol, pre["order_preview"]["qty"], est_risk)
                continue

            out = self.engine.approve_play(p.id, operator="autopilot")
            if out.get("ok"):
                self._count_today += 1
                taken += 1
                tid = out.get("trade_id") or getattr(p, "trade_id", None)
                if tid:
                    self._auto_trade_ids.add(tid)
                self._persist()
                actions.append({"play_id": p.id, "symbol": p.symbol, "action": "entered",
                                "trade_id": tid, "qty": pre["order_preview"]["qty"],
                                "risk": est_risk})
                self.bus.publish("autopilot.entered", play_id=p.id, symbol=p.symbol,
                                 strategy=p.strategy, side=p.side.value, trade_id=tid,
                                 qty=pre["order_preview"]["qty"], est_risk=est_risk,
                                 confidence=round(p.confidence, 2),
                                 reward_risk=round(p.reward_risk, 2),
                                 count_today=self._count_today)
                log.warning("autopilot ENTERED %s %s x%s -> trade %s (risk $%.0f, %d/%d today)",
                            p.side.value, p.symbol, pre["order_preview"]["qty"], tid,
                            est_risk, self._count_today, self.max_auto_trades_per_day)
            else:
                self._last_reason[p.id] = out.get("reason", "execution failed")
                self.bus.publish("autopilot.skipped", play_id=p.id, symbol=p.symbol,
                                 strategy=p.strategy, reason=self._last_reason[p.id])

        return actions

    # ------------------------------------------------------------------ #
    def _pre_gate(self, p: Any, equity: float) -> Optional[str]:
        """Cheap filters before we spend an engine assessment. Returns a reason
        string to skip, or None to proceed."""
        if getattr(p.status, "value", str(p.status)) != "PROPOSED":
            return "not a fresh proposed play"
        tf = p.timeframe.value
        if tf not in self.trade_types:
            return f"trade type {tf} not enabled for autopilot"
        if p.confidence < self.min_confidence:
            return f"confidence {p.confidence:.2f} < {self.min_confidence:.2f}"
        if p.reward_risk < self.min_reward_risk:
            return f"reward:risk {p.reward_risk:.1f} < {self.min_reward_risk:.1f}"
        if p.kind.value == "FUNDAMENTAL":
            return "valuation plays are not day/swing entries - not auto-traded"
        blocked = {s.lower() for s in (self.cfg.block_sectors or [])}
        if p.sector and p.sector.lower() in blocked:
            return f"sector '{p.sector}' is on the autopilot block list"
        if self.cfg.require_catalyst and not any(t in ("catalyst", "gap") for t in (p.tags or [])):
            return "no catalyst tag (autopilot.require_catalyst is on)"
        try:
            if self.engine.repo.get_open_trade_for_symbol(p.symbol):
                return f"already holding {p.symbol}"
        except Exception:  # noqa: BLE001
            pass
        # cooldown: a name that already stopped out today is not a re-entry -
        # this is what turned QCOM/DLTR into long<->short chop machines.
        if self.cooldown_after_loss:
            try:
                today = clock.session_date().isoformat()
                for t in self.engine.repo.recent_trades(60):
                    if (t.get("symbol") == p.symbol and t.get("status") == "CLOSED"
                            and str(t.get("session_date") or "").startswith(today)
                            and float(t.get("realized_pl") or 0.0) < 0):
                        return f"{p.symbol} already stopped out today - cooling off"
            except Exception:  # noqa: BLE001
                pass
        return None

    # ------------------------------------------------------------------ #
    def decorate_play(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Tag a play row so the dashboard can show a '🤖 auto' badge."""
        pid = row.get("id")
        will = (
            self.enabled and self._live_ok()
            and row.get("timeframe") in self.trade_types
            and float(row.get("confidence", 0)) >= self.min_confidence
            and float(row.get("reward_risk", 0)) >= self.min_reward_risk
            and row.get("kind") != "FUNDAMENTAL"
            and row.get("status") == "PROPOSED"
        )
        row["autopilot"] = {
            "eligible": bool(will),
            "acted": pid in self._acted,
            "reason": self._last_reason.get(pid, ""),
        }
        return row
