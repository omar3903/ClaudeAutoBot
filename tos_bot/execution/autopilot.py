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
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..core.eventbus import BUS
from ..scanner.noise import LABELS as NOISE_LABELS
from ..research.significance import SPEED_LIMIT
from ..util import clock

log = logging.getLogger(__name__)

#: what Autopilot can be told to take: day trades, swing trades, pairs (pairs/desk.py)
TRADE_TYPES = ("INTRADAY", "SWING", "PAIRS")
#: the two kinds the trading capital is split between (engine/capital.py says the same: a day trade is
#: INTRADAY, everything held overnight is a swing trade)
DAY, SWING = "INTRADAY", "SWING"
KINDS = (DAY, SWING)


MODEL_MODES = ("shadow", "gate", "size")


def kind_of(timeframe: Any) -> str:
    return DAY if str(getattr(timeframe, "value", timeframe) or "").upper() == DAY else SWING


def _mode(value: Any) -> str:
    value = str(value or "shadow").lower()
    return value if value in MODEL_MODES else "shadow"


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
        self.min_swing_confidence: float = float(getattr(cfg, "min_swing_confidence", 0.5))
        self.min_reward_risk: float = float(cfg.min_reward_risk)
        self.max_auto_positions: int = int(cfg.max_auto_positions)
        self.max_auto_trades_per_day: int = int(cfg.max_auto_trades_per_day)
        self.max_per_strategy: int = int(getattr(cfg, "max_per_strategy", 2))
        self.max_new_per_cycle: int = int(getattr(cfg, "max_new_per_cycle", 1))
        self.cooldown_after_loss: bool = bool(getattr(cfg, "cooldown_after_loss", True))
        self.max_daily_loss_pct: float = float(getattr(cfg, "max_daily_loss_pct", 2.0))
        self.max_giveback_pct: float = float(getattr(cfg, "max_giveback_pct", 30.0))
        self.giveback_floor_pct: float = float(getattr(cfg, "giveback_floor_pct", 0.25))
        self.max_gross_exposure_pct: float = float(getattr(cfg, "max_gross_exposure_pct", 100.0))
        self.min_confirmations: int = int(getattr(cfg, "min_confirmations", 2))
        self.min_minutes_to_close: int = int(getattr(cfg, "min_minutes_to_close", 30))
        self.skip_noise: List[str] = [str(n) for n in getattr(cfg, "skip_noise", list(NOISE_LABELS))]
        self.require_proven: bool = bool(getattr(cfg, "require_proven", True))
        self.min_replay_trades: int = int(getattr(cfg, "min_replay_trades", 30))
        self.min_replay_expectancy_r: float = float(getattr(cfg, "min_replay_expectancy_r", 0.05))
        self.proof_p_value: float = float(getattr(cfg, "proof_p_value", 0.10))
        self.model_mode: str = _mode(getattr(cfg, "model_mode", "shadow"))
        self.model_min_p: float = float(getattr(cfg, "model_min_p", 0.55))
        self.dry_run: bool = bool(cfg.dry_run)

        self._acted: set[str] = set()          # play ids already handled
        #: the handled ids that were refusals a change of settings could lift (no room in the capital share,
        #: a size of zero, a last look that failed) - settings_changed() hands them back to the next pass
        self._refused: set[str] = set()
        self._count_by_kind: Dict[str, int] = {}   # today's entries, day trades and swing trades apart
        #: the entries counted today, by play id, with their kind: one that ends with nothing bought hands
        #: its slot back (entry_unfilled), once
        self._counted: Dict[str, str] = {}
        self._sent_today: int = 0               # entry orders sent today - never handed back (see SENT_CEILING)
        self._count_lock = threading.Lock()     # the counts change on the scan thread and the order sync's
        self._auto_trade_ids: set[str] = set()  # trades this pilot opened (this session)
        self._auto_play_ids: set[str] = set()   # plays it sent entries for - a late fill carries only this
        self._day: str = ""
        self._count_today: int = 0
        self._last_reason: Dict[str, str] = {}  # play_id -> why skipped (for the UI)
        self._blocked_note: str = ""
        self._loss_stop_day: str = ""           # the session the daily loss limit was reached on
        self._peak_realized: float = 0.0        # the best the day's realized P/L has been
        self._realized: tuple = (float("-inf"), 0.0)   # (monotonic time read, realized P/L today)
        self._entries_at: List[float] = []      # monotonic times of the latest entries, for the per-cycle cap

    # ------------------------------------------------------------------ #
    #  Persistable slice (goes into data/runtime.json alongside `mode`)  #
    # ------------------------------------------------------------------ #
    def to_runtime(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "trade_types": self.trade_types,
            "min_confidence": self.min_confidence,
            "min_swing_confidence": self.min_swing_confidence,
            "min_reward_risk": self.min_reward_risk,
            "max_auto_positions": self.max_auto_positions,
            "max_auto_trades_per_day": self.max_auto_trades_per_day,
            "max_per_strategy": self.max_per_strategy,
            "max_new_per_cycle": self.max_new_per_cycle,
            "cooldown_after_loss": self.cooldown_after_loss,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_giveback_pct": self.max_giveback_pct,
            "peak_realized": self._peak_realized,
            "max_gross_exposure_pct": self.max_gross_exposure_pct,
            "min_confirmations": self.min_confirmations,
            "min_minutes_to_close": self.min_minutes_to_close,
            "skip_noise": list(self.skip_noise),
            "require_proven": self.require_proven,
            "model_mode": self.model_mode,
            "model_min_p": self.model_min_p,
            "dry_run": self.dry_run,
            "day": self._day,
            **self._counts_saved(),
        }

    def _counts_saved(self) -> Dict[str, Any]:
        with self._count_lock:
            return {"count_today": self._count_today, "count_today_by_kind": dict(self._count_by_kind),
                    "counted_today": dict(self._counted), "sent_today": self._sent_today}

    def load_runtime(self, d: Dict[str, Any]) -> None:
        if not isinstance(d, dict):
            return
        self.enabled = bool(d.get("enabled", self.enabled))
        tt = d.get("trade_types")
        if isinstance(tt, list) and tt:
            self.trade_types = [str(x).upper() for x in tt if str(x).upper() in TRADE_TYPES] or self.trade_types
        for k in ("min_confidence", "min_swing_confidence", "min_reward_risk", "max_gross_exposure_pct",
                  "max_daily_loss_pct", "max_giveback_pct"):
            if isinstance(d.get(k), (int, float)):
                setattr(self, k, float(d[k]))
        for k in ("max_auto_positions", "max_auto_trades_per_day",
                  "max_per_strategy", "max_new_per_cycle", "min_confirmations", "min_minutes_to_close"):
            if isinstance(d.get(k), int):
                setattr(self, k, int(d[k]))
        if isinstance(d.get("skip_noise"), list):
            self.skip_noise = [str(n) for n in d["skip_noise"] if str(n) in NOISE_LABELS]
        if "cooldown_after_loss" in d:
            self.cooldown_after_loss = bool(d["cooldown_after_loss"])
        if "require_proven" in d:
            self.require_proven = bool(d["require_proven"])
        if "model_mode" in d:
            self.model_mode = _mode(d["model_mode"])
        if isinstance(d.get("model_min_p"), (int, float)):
            self.model_min_p = min(0.9, max(0.5, float(d["model_min_p"])))
        self.dry_run = bool(d.get("dry_run", self.dry_run))
        # only restore the day counter if it is still the same session
        if d.get("day") == clock.session_date().isoformat():
            self._day = d["day"]
            self._count_today = int(d.get("count_today", 0))
            by_kind = d.get("count_today_by_kind")
            self._count_by_kind = ({k: int(v) for k, v in by_kind.items() if k in KINDS}
                                   if isinstance(by_kind, dict) else {})
            counted = d.get("counted_today")
            self._counted = ({str(k): v for k, v in counted.items() if v in KINDS}
                             if isinstance(counted, dict) else {})
            sent = d.get("sent_today")
            self._sent_today = int(sent) if isinstance(sent, int) else self._count_today
            self._peak_realized = float(d.get("peak_realized", 0.0) or 0.0)

    # ------------------------------------------------------------------ #
    def configure(self, **kw: Any) -> Dict[str, Any]:
        """Update the live knobs from the UI. Unknown keys are ignored."""
        if "enabled" in kw:
            self.enabled = bool(kw["enabled"])
        if "dry_run" in kw:
            self.dry_run = bool(kw["dry_run"])
        tt = kw.get("trade_types")
        if isinstance(tt, list):
            clean = [str(x).upper() for x in tt if str(x).upper() in TRADE_TYPES]
            if clean:
                self.trade_types = clean
        if isinstance(kw.get("min_confidence"), (int, float)):
            self.min_confidence = max(0.0, min(1.0, float(kw["min_confidence"])))
        if isinstance(kw.get("min_swing_confidence"), (int, float)):
            self.min_swing_confidence = max(0.0, min(1.0, float(kw["min_swing_confidence"])))
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
        if "require_proven" in kw:
            self.require_proven = bool(kw["require_proven"])
        if isinstance(kw.get("max_gross_exposure_pct"), (int, float)):
            self.max_gross_exposure_pct = max(10.0, min(400.0, float(kw["max_gross_exposure_pct"])))
        if isinstance(kw.get("max_daily_loss_pct"), (int, float)):
            self.max_daily_loss_pct = max(0.0, min(50.0, float(kw["max_daily_loss_pct"])))
        if isinstance(kw.get("max_giveback_pct"), (int, float)):
            self.max_giveback_pct = max(0.0, min(100.0, float(kw["max_giveback_pct"])))
        if isinstance(kw.get("min_confirmations"), int):
            self.min_confirmations = max(1, min(10, int(kw["min_confirmations"])))
        if isinstance(kw.get("min_minutes_to_close"), int):
            self.min_minutes_to_close = max(0, min(120, int(kw["min_minutes_to_close"])))
        if "model_mode" in kw:
            self.model_mode = _mode(kw["model_mode"])
        if isinstance(kw.get("model_min_p"), (int, float)):
            self.model_min_p = min(0.9, max(0.5, float(kw["model_min_p"])))
        if isinstance(kw.get("skip_noise"), list):
            self.skip_noise = [str(n) for n in kw["skip_noise"] if str(n) in NOISE_LABELS]
        self._persist()
        self.bus.publish("autopilot.config", **self.status())
        log.info("autopilot reconfigured: %s", self.status())
        return self.status()

    # ------------------------------------------------------------------ #
    def _roll_day(self) -> None:
        today = clock.session_date().isoformat()
        if today != self._day:
            with self._count_lock:
                if today == self._day:
                    return                      # another thread has just rolled it
                self._day = today
                self._count_today = 0
                self._count_by_kind = {}
                self._counted = {}
                self._sent_today = 0
            self._peak_realized = 0.0
            self._acted.clear()
            self._refused.clear()
            self._last_reason.clear()

    def settings_changed(self) -> None:
        """Something changed while the app runs - the day/swing split, the trading capital, the filters,
        the strategies, Autopilot's own settings, the account orders go to. Every gate reads its setting
        afresh on each pass; what would not follow by itself is a play already refused for the day and
        the reason shown for it. Both are dropped here, so the next pass judges the board on the settings
        as they are now. Plays it entered stay handled. It never enters anything itself."""
        self._acted -= self._refused
        self._refused.clear()
        self._last_reason.clear()

    def publish_status(self) -> None:
        """Tell the dashboard what Autopilot is set to and holds - without saving or logging anything."""
        self.bus.publish("autopilot.config", **self.status())

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
            and "INTRADAY" in self.play_types()
        )

    def play_types(self) -> List[str]:
        """The plays Autopilot takes: day trades and swing trades only where both the Intraday / Swing
        filters over the plays and its own day / swing boxes allow them. So the filters can put day-trade
        plays on the board - for the review to follow, say - while Autopilot's own box keeps it from
        taking any. (Pairs keep their own switch in Autopilot's settings.) Without the engine's
        filters, its own trade types."""
        own = [t for t in self.trade_types if t != "PAIRS"]
        kinds = getattr(getattr(self.engine, "filters", None), "timeframes", None)
        if kinds:
            return [t for t in ("INTRADAY", "SWING") if t in kinds and t in own]
        return own

    def effective_trade_types(self) -> List[str]:
        return self.play_types() + (["PAIRS"] if "PAIRS" in self.trade_types else [])

    # ---- the day / swing split ------------------------------------------ #
    def day_share(self) -> Optional[float]:
        """The day-trade share of the trading capital in force, in percent (engine.effective_day_pct) -
        None when the engine doesn't say, and then nothing is divided."""
        share = getattr(self.engine, "effective_day_pct", None)
        if not callable(share):
            return None
        try:
            return max(0.0, min(100.0, float(share())))
        except Exception:  # noqa: BLE001
            return None

    def kind_slots(self, total: int) -> Optional[Dict[str, int]]:
        """``total`` slots - open positions, or entries in a day - divided between day trades and swing
        trades the way the trading capital is: at 70 / 30, seven and three of ten. A kind with a share
        keeps at least one slot, a kind with none gets none, and the two never add up to more than
        ``total``. The split is the owner's statement of how much of the account each kind of trading
        gets; without this, the kind that fires first (swing setups, before the open and after 15:30)
        took every slot and left the other kind's capital idle."""
        pct = self.day_share()
        if pct is None:
            return None
        total = max(0, int(total))
        if pct >= 100.0:
            return {DAY: total, SWING: 0}
        if pct <= 0.0:
            return {DAY: 0, SWING: total}
        if total <= 1:
            return {DAY: total, SWING: total}                 # one slot: whichever kind comes first has it
        day = min(total - 1, max(1, int(total * pct / 100.0 + 0.5)))
        return {DAY: day, SWING: total - day}

    def _held_by_kind(self, opens: Optional[List[Dict[str, Any]]] = None) -> Dict[str, int]:
        """Autopilot's open positions and working entries, day trades and swing trades apart."""
        rows = self._open_auto_trades() + self._working_auto_entries() if opens is None else opens
        held = {DAY: 0, SWING: 0}
        for t in rows:
            held[kind_of(t.get("timeframe"))] += 1
        return held

    def _slots_card(self) -> Optional[Dict[str, Any]]:
        """The split as the dashboard shows it: per kind, the positions held of the slots it has, today's
        entries of its daily share, and whether Autopilot takes that kind at all right now."""
        positions, entries = self.kind_slots(self.max_auto_positions), self.kind_slots(self.max_auto_trades_per_day)
        if positions is None or entries is None:
            return None
        held, taking = self._held_by_kind(), set(self.play_types())
        return {"day_pct": self.day_share(),
                **{kind: {"open": held[kind], "max": positions[kind], "today": int(self._count_by_kind.get(kind, 0)),
                          "max_today": entries[kind], "taking": kind in taking} for kind in KINDS}}

    # ------------------------------------------------------------------ #
    def status(self) -> Dict[str, Any]:
        self._roll_day()
        open_auto = len(self._open_auto_trades()) + len(self._working_auto_entries())
        blocked = ""
        if self.enabled and not self._live_ok():
            blocked = ("Autopilot is paper-only until you set  autopilot.allow_live: true  "
                       "in config/config.yaml. It will not route live orders.")
        return {
            "enabled": self.enabled,
            "effective": self.enabled and self._live_ok(),
            "dry_run": self.dry_run,
            "trade_types": self.effective_trade_types(),
            "own_trade_types": list(self.trade_types),
            "min_confidence": round(self.min_confidence, 2),
            "min_swing_confidence": round(self.min_swing_confidence, 2),
            "min_reward_risk": round(self.min_reward_risk, 2),
            "max_auto_positions": self.max_auto_positions,
            "max_auto_trades_per_day": self.max_auto_trades_per_day,
            "max_per_strategy": self.max_per_strategy,
            "max_new_per_cycle": self.max_new_per_cycle,
            "cooldown_after_loss": self.cooldown_after_loss,
            "max_daily_loss_pct": round(self.max_daily_loss_pct, 2),
            "max_giveback_pct": round(self.max_giveback_pct, 1),
            "realized_today": round(self._realized_today(), 2),
            "peak_realized": round(self._peak_realized, 2),
            "daily_loss_stop": self.stopped_for_the_day,
            "max_gross_exposure_pct": round(self.max_gross_exposure_pct, 1),
            "min_confirmations": self.min_confirmations,
            "min_minutes_to_close": self.min_minutes_to_close,
            "skip_noise": list(self.skip_noise),
            "learned_skip_noise": self._learned_skips(),
            "noise_labels": NOISE_LABELS,
            "require_proven": self.require_proven,
            "proof_required": self.proof_required,          # what is in force: always True in Live
            "proof_forced": self.proof_required and not self.require_proven,
            "min_replay_trades": self.min_replay_trades,
            "min_replay_expectancy_r": self.min_replay_expectancy_r,
            "proof_p_value": self.proof_p_value,
            "model_mode": self.model_mode,
            "model_min_p": round(self.model_min_p, 2),
            "model": self._model_card(),
            "open_auto_positions": open_auto,
            "auto_trades_today": self._count_today,
            "slots": self._slots_card(),
            "mode": getattr(self.engine, "mode", "paper"),
            "allow_live": bool(getattr(self.cfg, "allow_live", False)),
            "blocked_note": blocked,
        }

    # ------------------------------------------------------------------ #
    def _open_auto_trades(self) -> List[Dict[str, Any]]:
        """The open trades Autopilot entered, on the account orders go to. The ids it remembers cover
        this run; after a restart the trade's own record says who took it (``entry_context.by``) -
        otherwise every cap would start from nothing with its positions still open. Pair legs are the
        pair desk's."""
        try:
            opens = self.engine.repo.open_trades()
        except Exception:  # noqa: BLE001
            return []
        venue = getattr(self.engine, "_venue", None)
        mine = []
        for t in opens:
            if t.get("pair_id") or (venue is not None and (t.get("broker") or "paper") != venue):
                continue
            taken_by = (t.get("entry_context") or {}).get("by") if isinstance(t.get("entry_context"), dict) else None
            if t["id"] in self._auto_trade_ids or t.get("play_id") in self._auto_play_ids or taken_by == "autopilot":
                mine.append(t)
        return mine

    def recognise_entries(self, play_ids: List[str]) -> None:
        """Entry orders an earlier run of the app left working, taken back after a restart: the ones
        Autopilot sent are its own again, so every cap counts them - a swing limit can rest all day."""
        for pid in play_ids:
            try:
                row = self.engine.repo.get_play(pid)
            except Exception:  # noqa: BLE001
                continue
            if row and row.get("decided_by") == "autopilot":
                self._auto_play_ids.add(pid)

    #: entry orders sent in a day, as a multiple of the day's entries, after which no more go out - the
    #: slots handed back by entries that bought nothing would otherwise let a broker refusing every order
    #: (they come back cancelled) or a run of entries timing out turn the cap into an order a minute
    SENT_CEILING = 2

    def entry_unfilled(self, play_id: str) -> bool:
        """An entry Autopilot sent ended with nothing bought - timed out, cancelled, refused: the day's
        slot it took comes back, once. The setup itself isn't offered again today (never chase). Called
        by the executor, from the order sync; says whether a slot came back."""
        self._roll_day()
        if not self._release(play_id):
            return False                        # not one it counted today, or already handed back
        self._auto_play_ids.discard(play_id)
        self._room_cache = None
        log.info("autopilot: the entry for %s bought nothing - its slot is back (%d/%d today)",
                 play_id, self._count_today, self.max_auto_trades_per_day)
        try:
            self._persist()
            self.publish_status()
        except Exception:  # noqa: BLE001
            log.debug("saving the handed-back slot failed", exc_info=True)
        return True

    def _reserve(self, play_id: str, kind: str) -> None:
        with self._count_lock:
            self._count_today += 1
            self._count_by_kind[kind] = int(self._count_by_kind.get(kind, 0)) + 1
            self._counted[play_id] = kind

    def _release(self, play_id: str) -> bool:
        with self._count_lock:
            kind = self._counted.pop(play_id, None)
            if kind is None:
                return False
            self._count_today = max(0, self._count_today - 1)
            left = int(self._count_by_kind.get(kind, 0)) - 1
            if left > 0:
                self._count_by_kind[kind] = left
            else:
                self._count_by_kind.pop(kind, None)
            return True

    def _working_auto_entries(self) -> List[Dict[str, Any]]:
        """Auto entries sent but not filled yet - they count against every cap."""
        return [w for w in self.engine.working_entries() if w["play_id"] in self._auto_play_ids]

    def _open_risk_dollars(self) -> float:
        total = sum(w["risk"] for w in self._working_auto_entries())
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
        refused = getattr(self.engine, "prices_refused", None)
        blind = refused() if callable(refused) else ""
        if blind:
            # the scans are reading nothing, so the board is stale, and no quote can be had for a last look
            if self._blocked_note != "blind":
                self._blocked_note = "blind"
                log.warning("autopilot is taking no entries: prices can't be read - %s", blind)
                self.bus.publish("autopilot.blocked", reason="No entries while prices can't be read: " + blind)
            for p in plays.values():
                if p.id not in self._acted:
                    self._last_reason[p.id] = "no entries while prices can't be read - " + blind
            return []
        self._blocked_note = ""

        # the account as sizing sees it - a trading-capital limit shrinks it
        sizing = getattr(self.engine, "sizing_account", None)
        acct = sizing() if callable(sizing) else getattr(self.engine, "_account", None)
        equity = float(getattr(acct, "equity", 0.0) or 0.0)
        actions: List[Dict[str, Any]] = []
        taken = 0                              # new entries opened this cycle

        stopped = self.daily_loss_reason(equity)
        if not stopped:
            self._loss_stop_day = ""               # the limit was raised or switched off: it isn't stopped any more
        if stopped:
            if self._loss_stop_day != self._day:
                self._loss_stop_day = self._day
                log.warning("autopilot stopped for the day: %s", stopped)
                self.bus.publish("autopilot.daily_loss", reason=stopped, day=self._day)
            for p in plays.values():
                if p.id not in self._acted:
                    self._last_reason[p.id] = stopped
            return []

        # highest-conviction first
        ordered = sorted(plays.values(), key=lambda p: getattr(p, "score", 0.0), reverse=True)
        for p in ordered:
            if p.id in self._acted:
                continue
            wait = self._pace_wait(p.timeframe.value, taken)
            if wait is not None:
                self._last_reason[p.id] = (f"{self.max_new_per_cycle} new entr{'y' if self.max_new_per_cycle == 1 else 'ies'} "
                                           "per scan cycle" + (f" - the next in {wait:.0f}s" if wait > 0 else ""))
                continue
            gate = self._pre_gate(p, equity)
            if gate is not None:
                self._last_reason[p.id] = gate
                continue

            # per-day + concurrency caps (re-checked every pass)
            if self._count_today >= self.max_auto_trades_per_day:
                self._last_reason[p.id] = f"daily auto-trade cap ({self.max_auto_trades_per_day}) reached"
                continue
            if self._sent_today >= self.SENT_CEILING * self.max_auto_trades_per_day:
                self._last_reason[p.id] = (f"{self._sent_today} entry orders sent today - {self.SENT_CEILING} times "
                                           "the daily cap, counting the ones that bought nothing; no more today")
                continue
            opens = self._open_auto_trades() + self._working_auto_entries()   # orders still working count too
            if len(opens) >= self.max_auto_positions:
                self._last_reason[p.id] = f"max concurrent auto positions ({self.max_auto_positions}) reached"
                continue
            full = self._kind_full(p.timeframe.value, opens)
            if full:
                self._last_reason[p.id] = full
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
                self._refused.add(p.id)               # a change of settings may make it executable
                continue

            est_risk = float(pre["order_preview"].get("est_risk", 0.0) or 0.0)
            if equity and (self._open_risk_dollars() + est_risk) > equity * float(self.cfg.max_open_risk_pct) / 100.0:
                self._last_reason[p.id] = (f"would exceed {self.cfg.max_open_risk_pct:.0f}% aggregate open "
                                           f"auto-risk")
                continue
            est_cost = float(pre["order_preview"].get("est_cost", 0.0) or 0.0)
            if equity and self.engine.gross_exposure() + est_cost > equity * self.max_gross_exposure_pct / 100.0:
                self._last_reason[p.id] = (f"would put more than {self.max_gross_exposure_pct:.0f}% of equity "
                                           "into positions")
                continue

            self._acted.add(p.id)
            if self.dry_run:
                taken += 1
                self._entries_at.append(time.monotonic())
                actions.append({"play_id": p.id, "symbol": p.symbol, "action": "would_enter",
                                "qty": pre["order_preview"]["qty"], "risk": est_risk})
                self.bus.publish("autopilot.would_enter", play_id=p.id, symbol=p.symbol,
                                 strategy=p.strategy, side=p.side.value,
                                 qty=pre["order_preview"]["qty"], est_risk=est_risk,
                                 note="dry-run - no order placed")
                log.info("autopilot DRY-RUN would enter %s %s x%s (risk $%.0f)",
                         p.side.value, p.symbol, pre["order_preview"]["qty"], est_risk)
                continue

            # the slot is taken before the order goes out: the order sync can hear it ended - and hand the
            # slot back - before approve_play returns. Saved first, so a crash errs on a slot taken
            self._reserve(p.id, kind_of(p.timeframe.value))
            self._persist()
            try:
                out = self.engine.approve_play(p.id, operator="autopilot")
            except Exception:
                # the order may be out already: it keeps its slot and is Autopilot's to count
                self._sent_today += 1
                self._auto_play_ids.add(p.id)
                self._persist()
                raise
            if out.get("ok"):
                self._sent_today += 1
                taken += 1
                self._entries_at.append(time.monotonic())
                self._auto_play_ids.add(p.id)
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
                self._release(p.id)                   # nothing went out
                self._persist()
                self._refused.add(p.id)
                self._last_reason[p.id] = out.get("reason", "execution failed")
                self.bus.publish("autopilot.skipped", play_id=p.id, symbol=p.symbol,
                                 strategy=p.strategy, reason=self._last_reason[p.id])

        return actions

    def _kind_full(self, timeframe: str, opens: List[Dict[str, Any]]) -> Optional[str]:
        """Why this kind of trade has no slot left under the day/swing split, if it hasn't: its share of
        the open positions, then of the day's entries."""
        kind, pct = kind_of(timeframe), self.day_share()
        positions, entries = self.kind_slots(self.max_auto_positions), self.kind_slots(self.max_auto_trades_per_day)
        if pct is None or positions is None or entries is None:
            return None
        name = "day" if kind == DAY else "swing"
        share = f"{pct:g}% / {100 - pct:g}% day / swing split of the trading capital"
        held = self._held_by_kind(opens)[kind]
        if held >= positions[kind]:
            return (f"{name} trades hold {held} of the {positions[kind]} positions the {share} gives them "
                    f"(of {self.max_auto_positions})")
        today = int(self._count_by_kind.get(kind, 0))
        if today >= entries[kind]:
            return (f"{name} trades have made {today} of the {entries[kind]} entries a day the {share} gives them "
                    f"(of {self.max_auto_trades_per_day})")
        return None

    def _pace_wait(self, timeframe: str, taken: int) -> Optional[float]:
        """Seconds until the per-cycle cap lets another entry through, or None when it does now. A
        cycle is a scan of the market - the engine says how long one lasts for this kind of trade
        (``entry_pace_seconds``: the fast cycle for a day trade, the regular cycle otherwise) - and
        not the 15-second re-check of the board, which would turn "one per cycle" into ten entries
        in three minutes, all the same bet on that moment. Without an engine that says, a cycle is
        one call."""
        pace = getattr(self.engine, "entry_pace_seconds", None)
        window = float(pace(timeframe)) if callable(pace) else 0.0
        if window <= 0:
            return 0.0 if taken >= self.max_new_per_cycle else None
        now = time.monotonic()
        self._entries_at = [at for at in self._entries_at if now - at < 3600.0]
        recent = sorted(at for at in self._entries_at if now - at < window)
        if len(recent) < self.max_new_per_cycle:
            return None
        return max(0.0, window - (now - recent[-self.max_new_per_cycle]))

    # ------------------------------------------------------------------ #
    #: how long the realized P/L of the day is kept before it is read again
    REALIZED_CACHE_S = 20.0

    def _realized_today(self) -> float:
        """Realized P/L of the trades closed this session on the venue Autopilot trades on, by
        hand or by Autopilot - they drain the same account."""
        mono = time.monotonic()
        if mono - self._realized[0] < self.REALIZED_CACHE_S:
            return self._realized[1]
        venue = getattr(self.engine, "_venue", None)
        total = 0.0
        try:
            for t in self.engine.repo.trades_on(clock.session_date()):
                if t.get("status") == "CLOSED" and (not venue or (t.get("broker") or "paper") == venue):
                    total += float(t.get("realized_pl") or 0.0)
        except Exception:  # noqa: BLE001
            log.debug("could not read today's closed trades", exc_info=True)
        self._realized = (mono, total)
        return total

    @property
    def stopped_for_the_day(self) -> bool:
        return bool(self._loss_stop_day) and self._loss_stop_day == self._day

    def daily_loss_reason(self, equity: float) -> Optional[str]:
        """Why today's results stop new entries, if they do. Aziz's daily maximum loss - "live to
        trade another day" - and his give-back rule: he stops once he has lost 30% of what the
        morning made. Chan's version: cut exposure after losses, never add."""
        if equity <= 0:
            return None
        realized = self._realized_today()
        self._peak_realized = max(self._peak_realized, realized)
        if self.max_daily_loss_pct > 0:
            limit = equity * self.max_daily_loss_pct / 100.0
            if realized <= -limit:
                return (f"today's closed trades have lost {-realized:,.0f}, past the daily limit of "
                        f"{self.max_daily_loss_pct:g}% of equity ({limit:,.0f}) - no more entries this session")
        peak = self._peak_realized
        if self.max_giveback_pct > 0 and peak >= equity * self.giveback_floor_pct / 100.0:
            kept = realized / peak if peak else 1.0
            if kept <= 1.0 - self.max_giveback_pct / 100.0:
                return (f"today's realized gain has fallen from {peak:,.0f} to {realized:,.0f}, giving back more "
                        f"than {self.max_giveback_pct:g}% of it - no more entries this session (Aziz's give-back rule)")
        return None

    def _pre_gate(self, p: Any, equity: float) -> Optional[str]:
        """Cheap filters before we spend an engine assessment. Returns a reason
        string to skip, or None to proceed."""
        if getattr(p.status, "value", str(p.status)) != "PROPOSED":
            return "not a fresh proposed play"
        tf = p.timeframe.value
        if tf not in self.play_types():
            return f"{'day' if tf == 'INTRADAY' else tf.lower()} trades are switched off - in the Intraday / Swing filters or in Autopilot's own boxes"
        floor = self.confidence_floor(tf)
        if p.confidence < floor:
            return f"confidence {p.confidence:.2f} < {floor:.2f}{'' if tf == 'INTRADAY' else ' (the swing floor)'}"
        if p.reward_risk < self.min_reward_risk:
            return f"reward:risk {p.reward_risk:.2f} < {self.min_reward_risk:.2f}"
        if p.kind.value == "FUNDAMENTAL":
            return "valuation plays are not day/swing entries - not auto-traded"
        skipped = self.skipped_noise()
        noisy = [n for n in p.noise if n in skipped]
        if noisy:
            return "noise: " + ", ".join(NOISE_LABELS.get(n, n) for n in noisy)
        if tf == "INTRADAY" and p.confirmations < self.min_confirmations:
            return f"not confirmed yet - seen in {p.confirmations} of {self.min_confirmations} scans in a row"
        if tf == "INTRADAY" and self.min_minutes_to_close > 0:
            left = clock.minutes_to_close()
            if left < self.min_minutes_to_close:
                return (f"{left:.0f} minutes to the close - a day trade needs {self.min_minutes_to_close} "
                        "(Aziz keeps the last half hour for closing, and the exit manager flattens before the bell)")
        unproven = self._unproven(p.strategy)
        if unproven:
            return unproven
        doubted = self.model_refusal(p)
        if doubted:
            return doubted
        if self.cfg.require_catalyst and not any(t in ("catalyst", "gap") for t in (p.tags or [])):
            return "no catalyst tag (autopilot.require_catalyst is on)"
        try:
            if self.engine.repo.get_open_trade_for_symbol(p.symbol):
                return f"already holding {p.symbol}"
        except Exception:  # noqa: BLE001
            pass
        if any(w["symbol"] == p.symbol for w in self.engine.working_entries()):
            return f"an entry order for {p.symbol} is still working"
        account = getattr(self.engine, "_account", None)
        pos = account.position(p.symbol) if account is not None else None
        if pos is not None and abs(pos.quantity) > 1e-9:
            return f"the account already holds {abs(pos.quantity):,.0f} {p.symbol} shares"
        # cooldown: a name that already stopped out today is not a re-entry -
        # going straight back in turns one loss into long<->short chop.
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

    def model_refusal(self, p: Any) -> Optional[str]:
        """Why the learned model keeps Autopilot out of a play, if it does: only in gate or size
        mode, and only while the model's own walk-forward judgement calls it usable."""
        score = (getattr(p, "evidence", None) or {}).get("model") or {}
        if self.model_mode == "shadow" or not score.get("usable") or score.get("p") is None:
            return None
        if float(score["p"]) < self.model_min_p:
            return (f"the learned model gives plays like this {float(score['p']):.0%} to pay - under the "
                    f"{self.model_min_p:.0%} it is asked for")
        return None

    def _model_card(self) -> Optional[Dict[str, Any]]:
        card = getattr(self.engine, "model_card", None)
        try:
            return card() if callable(card) else None
        except Exception:  # noqa: BLE001
            return None

    def confidence_floor(self, timeframe: str) -> float:
        """The conviction a play must state: day trades use min_confidence; swing plays their own,
        lower floor - the swing setups state flat, modest confidences, and the replay's proof is
        what really vets them."""
        return self.min_confidence if timeframe == "INTRADAY" else self.min_swing_confidence

    def verdict(self, p: Any) -> str:
        """What Autopilot makes of a play, in words, for the dashboard's notes."""
        if not self.enabled:
            return "Autopilot is off"
        if p.id in self._acted:
            return "Autopilot has acted on it"
        sizing = getattr(self.engine, "sizing_account", None)
        acct = sizing() if callable(sizing) else getattr(self.engine, "_account", None)
        gate = self._last_reason.get(p.id) or self._pre_gate(p, float(getattr(acct, "equity", 0.0) or 0.0))
        return f"won't take it: {gate}" if gate else "passes its checks - it can take it on the next pass"

    @property
    def proof_required(self) -> bool:
        """With real money Autopilot only trades strategies the replay has proven - always. On paper
        it is the ``require_proven`` setting: practising unproven setups there costs nothing, and
        their trades are what the records and the learned model are built from."""
        return True if getattr(self.engine, "mode", "paper") == "live" else self.require_proven

    def _unproven(self, strategy: str) -> Optional[str]:
        """Why a strategy's replayed record isn't good enough to auto-trade, if it isn't."""
        return self.proof_missing(strategy) if self.proof_required else None

    def proof_missing(self, strategy: str) -> Optional[str]:
        """Why a strategy's replayed record doesn't prove it, whether or not Autopilot asks for proof."""
        record = self.engine.strategy_record(strategy) or {}
        trades = int(record.get("trades", 0))
        if trades < self.min_replay_trades:
            return (f"{strategy} isn't proven yet: the replay has {trades} of the {self.min_replay_trades} "
                    "trades it needs (Strategies -> Run replay)")
        if record["expectancy_r"] < self.min_replay_expectancy_r:
            return f"{strategy} averaged {record['expectancy_r']:+.2f}R over {trades} replayed trades"
        edge = record.get("edge_r")
        if edge is not None and edge < self.min_replay_expectancy_r:
            return (f"{strategy} averaged {edge:+.2f}R over {trades} replayed trades once the stocks' own drift is "
                    "taken out - being on the right side of the market isn't the setup's edge (Aronson)")
        p = record.get("p_adjusted")
        if self.proof_p_value > 0 and p is not None and p > self.proof_p_value:
            tried = int(record.get("setups_tested") or 1)
            return (f"{strategy}'s {record['expectancy_r']:+.2f}R could be luck: p = {p:.2f} once the {tried} setups "
                    f"tried are allowed for, and proof needs {self.proof_p_value:g} or less (Aronson's reality check)")
        share = record.get("cost_share")
        if share is not None and share > SPEED_LIMIT:
            return (f"costs take {share:.0%} of {strategy}'s pre-cost edge - Carver's speed limit is a third, "
                    "past which a rule is trading too fast for what it earns")
        held = record.get("out_of_sample")
        if held is not None:
            n = int(held.get("trades", 0))
            if n < self.MIN_HELD_OUT_TRADES:
                return (f"{strategy} isn't proven yet: {n} of its replayed trades fall in the held-out "
                        f"sessions and it needs {self.MIN_HELD_OUT_TRADES} there")
            if held["expectancy_r"] <= 0:
                return (f"{strategy} averaged {held['expectancy_r']:+.2f}R over the {n} trades in the "
                        "replay's held-out sessions - its record doesn't hold up out of sample")
        return None

    #: replayed trades a strategy needs in the held-out sessions (Chan: test out of sample)
    MIN_HELD_OUT_TRADES = 10

    def _learned_skips(self) -> List[str]:
        """The checks from the books' statistics that the replay shows are worth skipping."""
        learned = getattr(self.engine, "learned_skips", None)
        try:
            return [c for c in learned() if c not in self.skip_noise] if callable(learned) else []
        except Exception:  # noqa: BLE001
            return []

    def skipped_noise(self) -> List[str]:
        """Every noise flag Autopilot won't trade: the ones chosen, and the ones the replay taught it."""
        return list(self.skip_noise) + self._learned_skips()

    # ------------------------------------------------------------------ #
    def decorate_play(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Tag a play row so the dashboard can show a '🤖 auto' badge."""
        pid = row.get("id")
        skipped = self.skipped_noise()
        will = (
            self.enabled and self._live_ok()
            and row.get("timeframe") in self.play_types()
            and float(row.get("confidence", 0)) >= self.confidence_floor(str(row.get("timeframe") or "INTRADAY"))
            and float(row.get("reward_risk", 0)) >= self.min_reward_risk
            and row.get("kind") != "FUNDAMENTAL"
            and row.get("status") == "PROPOSED"
            and not any(n in skipped for n in row.get("noise", []))
            and (row.get("timeframe") != "INTRADAY" or int(row.get("confirmations", 1)) >= self.min_confirmations)
            and not self._unproven(row.get("strategy", ""))
            and not self.stopped_for_the_day
        )
        waiting = self._waiting_for(str(row.get("timeframe") or ""), str(row.get("strategy") or "")) if will else None
        row["autopilot"] = {
            "eligible": bool(will),
            # it passes Autopilot's checks, but a cap has no room for it right now
            "waiting": waiting,
            "acted": pid in self._acted,
            "reason": self._last_reason.get(pid, ""),
        }
        return row

    #: seconds the caps are read once for a whole board of plays
    ROOM_CACHE_S = 2.0

    def _waiting_for(self, timeframe: str, strategy: str) -> Optional[str]:
        """Which cap keeps Autopilot from taking a play that passes its checks, right now: the day's
        entries, the open positions, the day / swing slots, the setup's own. None when there is room."""
        now = time.monotonic()
        cached = getattr(self, "_room_cache", None)
        if cached is None or now - cached[0] > self.ROOM_CACHE_S:
            opens = self._open_auto_trades() + self._working_auto_entries()
            cached = (now, opens)
            self._room_cache = cached
        opens = cached[1]
        if self._count_today >= self.max_auto_trades_per_day:
            return f"the day's {self.max_auto_trades_per_day} auto entries are used"
        if len(opens) >= self.max_auto_positions:
            return f"all {self.max_auto_positions} auto positions are taken"
        full = self._kind_full(timeframe, opens)
        if full:
            return full
        same = sum(1 for t in opens if t.get("strategy") == strategy)
        if same >= self.max_per_strategy:
            return f"already holding {same} of this setup (max {self.max_per_strategy})"
        return None
