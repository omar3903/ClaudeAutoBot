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
import math
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from ..core.eventbus import BUS
from ..scanner.noise import LABELS as NOISE_LABELS
from ..research.journal import DRIFT_R, MIN_LIVE
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
#: which plays the replay-loser check covers (AutoPilot.replay_loser): none, day trades, or swing trades too
LOSER_SCOPES = ("off", "day", "all")


def kind_of(timeframe: Any) -> str:
    return DAY if str(getattr(timeframe, "value", timeframe) or "").upper() == DAY else SWING


def _mode(value: Any) -> str:
    value = str(value or "shadow").lower()
    return value if value in MODEL_MODES else "shadow"


def _loser_scope(value: Any) -> str:
    if isinstance(value, bool):                 # YAML reads a bare off / on as false / true
        return "day" if value else "off"
    value = str(value or "day").lower()
    return value if value in LOSER_SCOPES else "day"


def _setup_list(value: Any) -> List[str]:
    """The setups Autopilot may take, by strategy key, each once; not a list = none named (every setup)."""
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(s for s in (str(v).strip() for v in value) if s))


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
        self.strategies: List[str] = _setup_list(getattr(cfg, "strategies", None))   # empty = every setup
        self.min_confidence: float = float(cfg.min_confidence)
        self.min_swing_confidence: float = float(getattr(cfg, "min_swing_confidence", 0.5))
        self.min_reward_risk: float = float(cfg.min_reward_risk)
        self.max_auto_positions: int = int(cfg.max_auto_positions)
        self.max_auto_trades_per_day: int = int(cfg.max_auto_trades_per_day)
        self.max_per_strategy: int = int(getattr(cfg, "max_per_strategy", 2))
        self.max_new_per_cycle: int = max(1, int(getattr(cfg, "max_new_per_cycle", 1)))   # as configure() allows
        self.cooldown_after_loss: bool = bool(getattr(cfg, "cooldown_after_loss", True))
        self.max_daily_loss_pct: float = float(getattr(cfg, "max_daily_loss_pct", 2.0))
        self.max_giveback_pct: float = float(getattr(cfg, "max_giveback_pct", 30.0))
        self.giveback_floor_pct: float = float(getattr(cfg, "giveback_floor_pct", 0.25))
        self.max_gross_exposure_pct: float = float(getattr(cfg, "max_gross_exposure_pct", 100.0))
        self.min_confirmations: int = int(getattr(cfg, "min_confirmations", 2))
        self.confirm_on_new_candle: bool = bool(getattr(cfg, "confirm_on_new_candle", True))
        self.min_minutes_to_close: int = int(getattr(cfg, "min_minutes_to_close", 30))
        self.skip_noise: List[str] = [str(n) for n in getattr(cfg, "skip_noise", list(NOISE_LABELS))]
        self.require_proven: bool = bool(getattr(cfg, "require_proven", True))
        self.min_replay_trades: int = int(getattr(cfg, "min_replay_trades", 30))
        self.min_replay_expectancy_r: float = float(getattr(cfg, "min_replay_expectancy_r", 0.05))
        self.proof_p_value: float = float(getattr(cfg, "proof_p_value", 0.10))
        self.skip_replay_losers: str = _loser_scope(getattr(cfg, "skip_replay_losers", "day"))
        self.replay_loser_r: float = float(getattr(cfg, "replay_loser_r", 0.05))
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
        self._loss_stop_reason: str = ""        # ...and why, in the words of daily_loss_reason, for the status strip
        self._peak_realized: float = 0.0        # the best the day's realized P/L has been
        self._realized: tuple = (float("-inf"), 0.0)   # (monotonic time read, realized P/L today)
        #: today's closed trades by setup, read with the realized P/L above (_realized_today)
        self._tally: List[Dict[str, Any]] = []
        self._entries_at: List[float] = []      # monotonic times of the latest entries, for the per-cycle cap
        #: (monotonic time read, {strategy: replayed record}) - the dashboard's rows share one read (_board_record)
        self._record_cache: Optional[tuple] = None
        #: (monotonic time, {strategy: replayed record}, [real records]) - one read per pass of consider()
        self._pass_cache: Optional[tuple] = None

    # ------------------------------------------------------------------ #
    #  Persistable slice (goes into data/runtime.json alongside `mode`)  #
    # ------------------------------------------------------------------ #
    def to_runtime(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "trade_types": self.trade_types,
            "strategies": list(self.strategies),
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
            "confirm_on_new_candle": self.confirm_on_new_candle,
            "min_minutes_to_close": self.min_minutes_to_close,
            "skip_noise": list(self.skip_noise),
            "require_proven": self.require_proven,
            "skip_replay_losers": self.skip_replay_losers,
            "replay_loser_r": self.replay_loser_r,
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
        if isinstance(d.get("strategies"), list):
            self.strategies = _setup_list(d["strategies"])
        for k in ("min_confidence", "min_swing_confidence", "min_reward_risk", "max_gross_exposure_pct",
                  "max_daily_loss_pct", "max_giveback_pct"):
            if isinstance(d.get(k), (int, float)):
                setattr(self, k, float(d[k]))
        for k in ("max_auto_positions", "max_auto_trades_per_day",
                  "max_per_strategy", "max_new_per_cycle", "min_confirmations", "min_minutes_to_close"):
            if isinstance(d.get(k), int):
                setattr(self, k, int(d[k]))
        self.max_new_per_cycle = max(1, self.max_new_per_cycle)          # as configure() allows
        if isinstance(d.get("skip_noise"), list):
            self.skip_noise = [str(n) for n in d["skip_noise"] if str(n) in NOISE_LABELS]
        if "cooldown_after_loss" in d:
            self.cooldown_after_loss = bool(d["cooldown_after_loss"])
        if "require_proven" in d:
            self.require_proven = bool(d["require_proven"])
        if "confirm_on_new_candle" in d:
            self.confirm_on_new_candle = bool(d["confirm_on_new_candle"])
        if "skip_replay_losers" in d:
            self.skip_replay_losers = _loser_scope(d["skip_replay_losers"])
        if isinstance(d.get("replay_loser_r"), (int, float)):
            self.replay_loser_r = max(0.0, min(1.0, float(d["replay_loser_r"])))
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
        if isinstance(kw.get("strategies"), list):
            self.strategies = _setup_list(kw["strategies"])
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
        if "confirm_on_new_candle" in kw:
            self.confirm_on_new_candle = bool(kw["confirm_on_new_candle"])
        if "skip_replay_losers" in kw:
            self.skip_replay_losers = _loser_scope(kw["skip_replay_losers"])
        if isinstance(kw.get("replay_loser_r"), (int, float)):
            self.replay_loser_r = max(0.0, min(1.0, float(kw["replay_loser_r"])))
        if isinstance(kw.get("min_minutes_to_close"), int):
            self.min_minutes_to_close = max(0, min(120, int(kw["min_minutes_to_close"])))
        if "model_mode" in kw:
            self.model_mode = _mode(kw["model_mode"])
        if isinstance(kw.get("model_min_p"), (int, float)):
            self.model_min_p = min(0.9, max(0.5, float(kw["model_min_p"])))
        if isinstance(kw.get("skip_noise"), list):
            self.skip_noise = [str(n) for n in kw["skip_noise"] if str(n) in NOISE_LABELS]
        self._record_cache = self._pass_cache = None   # the records are over the trades these settings would take
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
        self._record_cache = self._pass_cache = None   # the rows read the records afresh too - a new replay lands here

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
        opens = self._open_auto_trades() + self._working_auto_entries()
        losers = self.replay_losers()
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
            "strategies": list(self.strategies),
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
            "confirm_on_new_candle": self.confirm_on_new_candle,
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
            "skip_replay_losers": self.skip_replay_losers,
            "replay_loser_r": self.replay_loser_r,
            "replay_losers": losers,
            "model_mode": self.model_mode,
            "model_min_p": round(self.model_min_p, 2),
            "model": self._model_card(),
            "open_auto_positions": len(opens),
            "auto_trades_today": self._count_today,
            "sent_today": self._sent_today,
            "sent_ceiling": self.SENT_CEILING * self.max_auto_trades_per_day,
            "slots": self._slots_card(),
            "mode": getattr(self.engine, "mode", "paper"),
            "allow_live": bool(getattr(self.cfg, "allow_live", False)),
            "blocked_note": blocked,
            "headline": self._headline(opens, losers, blocked),
            "today": self._today_by_setup(),
        }

    # ---- the status strip under the dashboard's header ------------------- #
    #: the states Autopilot comes out of by itself later in the session - the ones the strip's text also
    #: says it trades at practice size, in a dry run, or skips the replay losers
    RESUMING = ("full", "closed", "kind-full", "late", "pacing", "taking")

    def _headline(self, opens: List[Dict[str, Any]], losers: List[Dict[str, str]], blocked: str) -> Dict[str, Any]:
        """What Autopilot is doing right now and why, in one line. The first state that holds wins: off;
        blocked (paper-only in Live); blind (no prices); stopped (the daily loss or give-back rule); done
        (the day's entries used); ceiling (the orders sent); full (every position taken); then, for each
        kind of trade it takes, closed (day trades wait for the open), kind-full (the kind's share of the
        day / swing split) or late (the last minutes before the close) - and while a kind is free, pacing
        (the per-cycle cap, with the seconds to the next entry) or taking. It only reads: status() runs on
        the web threads as well as the scan thread, which writes the counts and the entry times."""
        practice = not self.proof_required
        skipping = [row["strategy"] for row in losers]

        def line(state: str, text: str, wait: float = 0.0) -> Dict[str, Any]:
            if state in self.RESUMING:
                text += " · dry run: it places nothing" if self.dry_run else ""
                text += " · unproven setups at practice size (a quarter of the risk)" if practice else ""
                text += (" · skipping the replay losers: " + ", ".join(skipping)) if skipping else ""
            return {"state": state, "text": text, "next_entry_in_s": math.ceil(wait), "practice": practice,
                    "skipping": skipping}

        if not self.enabled:
            return line("off", "Off - you click every entry; exits stay automatic")
        if blocked:
            return line("blocked", blocked)
        refused = getattr(self.engine, "prices_refused", None)
        blind = refused() if callable(refused) else ""
        if blind:
            return line("blind", "No entries: prices can't be read - " + blind)
        if self.stopped_for_the_day:
            return line("stopped", "Stopped for the day: " + (self._loss_stop_reason or "the daily loss limit or "
                                                               "the give-back rule was reached"))
        cap = self.max_auto_trades_per_day
        if self._count_today >= cap:
            back = self._sent_today - self._count_today
            return line("done", f"Done for today: {self._count_today} of the {cap} entries a day are used"
                        + (f" ({back} more bought nothing and gave their slots back)" if back > 0 else ""))
        if self._sent_today >= self.SENT_CEILING * cap:
            return line("ceiling", f"No more today: {self._sent_today} entry orders sent - {self.SENT_CEILING} times "
                        "the daily cap, counting the ones that bought nothing")
        if len(opens) >= self.max_auto_positions:
            return line("full", f"Full: {len(opens)} of {self.max_auto_positions} positions taken, open or being "
                        "entered - no new entry until one closes")
        waiting, free = [], []                  # (state, why) for each kind it can't take now; the kinds it can
        for tf in self.play_types():
            full = self._kind_full(tf, opens)
            if tf == DAY and not clock.is_market_open():
                waiting.append(("closed", "day trades wait for the open"))
            elif full:
                waiting.append(("kind-full", full))
            elif tf == DAY and self.min_minutes_to_close > 0 and clock.minutes_to_close() < self.min_minutes_to_close:
                waiting.append(("late", f"no new day trades in the last {self.min_minutes_to_close} minutes"))
            else:
                free.append(tf)
        why = "; ".join(w for _, w in waiting)
        if not free:
            if not waiting:
                return line("idle", ("Pairs only - the pair desk enters them; " if "PAIRS" in self.trade_types else "")
                            + "day and swing trades are switched off, in the Intraday / Swing filters or its own boxes")
            state = min((s for s, _ in waiting), key=("closed", "kind-full", "late").index)
            return line(state, why[:1].upper() + why[1:])
        rest = f" · {why}" if why else ""
        wait = min(self._next_entry_in(tf) for tf in free)
        if wait > 0:
            n = self.max_new_per_cycle
            return line("pacing", f"Pacing: {n} new entr{'y' if n == 1 else 'ies'} per scan cycle{rest}", wait)
        kinds = " + ".join("day" if tf == DAY else "swing" for tf in free)
        return line("taking", f"Taking {kinds} trades - {len(opens)} of {self.max_auto_positions} positions, "
                    f"{self._count_today} of {cap} entries today{rest}")

    def _today_by_setup(self) -> List[Dict[str, Any]]:
        """Today's closed trades on the venue Autopilot trades on, by setup - taken by hand or by
        Autopilot: how many closed, how many won, their R and their P/L. Read with the realized P/L of the
        day, so once every REALIZED_CACHE_S."""
        self._realized_today()
        return list(self._tally)

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
            self._loss_stop_reason = stopped
            if self._loss_stop_day != self._day:
                self._loss_stop_day = self._day
                log.warning("autopilot stopped for the day: %s", stopped)
                self.bus.publish("autopilot.daily_loss", reason=stopped, day=self._day)
            for p in plays.values():
                if p.id not in self._acted:
                    self._last_reason[p.id] = stopped
            return []

        # highest-conviction first; the records the checks read are read once for the pass
        self._pass_cache = (time.monotonic(), {}, [])
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

        self._pass_cache = None
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
        window = self._pace_window(timeframe)
        if window <= 0:
            return 0.0 if taken >= self.max_new_per_cycle else None
        now = time.monotonic()
        self._entries_at = [at for at in self._entries_at if now - at < 3600.0]
        return self._next_entry_in(timeframe, window) or None

    def _pace_window(self, timeframe: str) -> float:
        pace = getattr(self.engine, "entry_pace_seconds", None)
        return float(pace(timeframe)) if callable(pace) else 0.0

    def _next_entry_in(self, timeframe: str, window: Optional[float] = None) -> float:
        """Seconds until the per-cycle cap lets the next entry of this kind through - 0 when it would now.
        It only reads the entry times: the status strip asks from the web threads while the scan thread
        appends to them, so dropping the old ones is left to _pace_wait, on the scan thread."""
        window = self._pace_window(timeframe) if window is None else window
        if window <= 0:
            return 0.0
        now, n = time.monotonic(), max(1, self.max_new_per_cycle)
        recent = sorted(at for at in list(self._entries_at) if now - at < window)
        if len(recent) < n:
            return 0.0
        return max(0.0, window - (now - recent[-n]))

    # ------------------------------------------------------------------ #
    #: how long the realized P/L of the day is kept before it is read again
    REALIZED_CACHE_S = 20.0

    def _realized_today(self) -> float:
        """Realized P/L of the trades closed this session on the venue Autopilot trades on, by
        hand or by Autopilot - they drain the same account. The same read tallies them by setup for
        the status strip (_today_by_setup); a pair's legs count in the P/L but not in the tally, where
        two legs with no stop of their own would read as two trades without an R."""
        mono = time.monotonic()
        if mono - self._realized[0] < self.REALIZED_CACHE_S:
            return self._realized[1]
        venue = getattr(self.engine, "_venue", None)
        total, by_setup = 0.0, {}
        try:
            for t in self.engine.repo.trades_on(clock.session_date()):
                if t.get("status") == "CLOSED" and (not venue or (t.get("broker") or "paper") == venue):
                    pl = float(t.get("realized_pl") or 0.0)
                    total += pl
                    if t.get("pair_id"):
                        continue
                    row = by_setup.setdefault(t.get("strategy") or "?", {"closed": 0, "wins": 0, "r": 0.0, "pl": 0.0})
                    row["closed"] += 1
                    row["wins"] += int(pl > 0)
                    row["r"] += float(t.get("r_multiple") or 0.0)
                    row["pl"] += pl
        except Exception:  # noqa: BLE001
            log.debug("could not read today's closed trades", exc_info=True)
        # the tally first: a reader on another thread that finds the fresh time finds the fresh tally too
        self._tally = [{"strategy": key, "closed": row["closed"], "wins": row["wins"], "r": round(row["r"], 2),
                        "pl": round(row["pl"], 2)}
                       for key, row in sorted(by_setup.items(), key=lambda kv: (-kv[1]["closed"], kv[0]))]
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

    def _play_check(self, tf: str, confidence: float, reward_risk: float, kind: str, status: str,
                    noise: Iterable[str], confirmations: int, strategy: str, *, board: bool = False) -> Optional[str]:
        """Why Autopilot won't take a play, from the play itself and Autopilot's settings - None when it
        passes. The gate (_pre_gate) and the dashboard's badge (decorate_play) both ask here, so the reason
        a row shows is the gate's own words, not a second copy of the rules kept by hand. ``board``: for
        the dashboard's rows - the clock is left to the gate, like the other checks that change from pass
        to pass (the model, what is held, the cooling off), and reaches a row as the last pass's reason;
        the replayed records are read once for the whole board."""
        if status != "PROPOSED":
            return "not a fresh proposed play"
        if tf not in self.play_types():
            return f"{'day' if tf == 'INTRADAY' else tf.lower()} trades are switched off - in the Intraday / Swing filters or in Autopilot's own boxes"
        if self.strategies and strategy not in self.strategies:
            return f"not one of the setups Autopilot takes ({', '.join(self.strategies)}) - Autopilot settings"
        floor = self.confidence_floor(tf)
        if confidence < floor:
            return f"confidence {confidence:.2f} < {floor:.2f}{'' if tf == 'INTRADAY' else ' (the swing floor)'}"
        if reward_risk < self.min_reward_risk:
            return f"reward:risk {reward_risk:.2f} < {self.min_reward_risk:.2f}"
        if kind == "FUNDAMENTAL":
            return "valuation plays are not day/swing entries - not auto-traded"
        skipped = self.skipped_noise()
        noisy = [n for n in noise if n in skipped]
        if noisy:
            return "noise: " + ", ".join(NOISE_LABELS.get(n, n) for n in noisy)
        if tf == "INTRADAY" and confirmations < self.min_confirmations:
            seen = "on {} of {} five-minute candles" if self.confirm_on_new_candle else "in {} of {} scans"
            return f"not confirmed yet - seen {seen.format(confirmations, self.min_confirmations)} in a row"
        if not board and tf == "INTRADAY" and self.min_minutes_to_close > 0:
            left = clock.minutes_to_close()
            if left < self.min_minutes_to_close:
                return (f"{left:.0f} minutes to the close - a day trade needs {self.min_minutes_to_close} "
                        "(Aziz keeps the last half hour for closing, and the exit manager flattens before the bell)")
        unproven = self._unproven(strategy, board=board)
        if unproven:
            return unproven
        loser = self._losing(strategy, tf, board)
        if loser:
            return loser
        return None

    @staticmethod
    def _facts(p: Any) -> tuple:
        """What _play_check reads off a play, unrounded: a play row rounds reward:risk and confidence for
        show, so a 1.996 reads 2.00 there and would pass a 2.0 floor that the gate holds it to."""
        return (p.timeframe.value, p.confidence, p.reward_risk, p.kind.value,
                getattr(p.status, "value", str(p.status)), p.noise, p.confirmations, p.strategy)

    def _pre_gate(self, p: Any, equity: float) -> Optional[str]:
        """Cheap filters before we spend an engine assessment. Returns a reason
        string to skip, or None to proceed. The play's own checks come first (_play_check, which
        the dashboard's badge shares); the ones that change from pass to pass follow."""
        why = self._play_check(*self._facts(p))
        if why:
            return why
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

    def _unproven(self, strategy: str, board: bool = False) -> Optional[str]:
        """Why a strategy's replayed record isn't good enough to auto-trade, if it isn't. ``board``: for the
        dashboard's rows, which share one read of each record (_board_record)."""
        if not self.proof_required:
            return None
        return self.proof_missing(strategy, self._board_record(strategy) if board else None)

    def _board_record(self, strategy: str) -> Dict[str, Any]:
        """A strategy's replayed record for the dashboard's rows: read once for a whole board of plays
        (ROOM_CACHE_S) and afresh after a change of settings or a new replay (settings_changed). The gate
        reads it afresh on every pass."""
        now = time.monotonic()
        cached = self._record_cache
        if cached is None or now - cached[0] > self.ROOM_CACHE_S:
            cached = (now, {})
            self._record_cache = cached
        if strategy not in cached[1]:
            cached[1][strategy] = self.engine.strategy_record(strategy) or {}
        return cached[1][strategy]

    def proof_missing(self, strategy: str, record: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Why a strategy's replayed record doesn't prove it, whether or not Autopilot asks for proof.
        ``record``: one already read; otherwise it is read now."""
        record = (self.engine.strategy_record(strategy) if record is None else record) or {}
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

    def _losing(self, strategy: str, timeframe: str, board: bool) -> Optional[str]:
        """replay_loser, on records read once: for a board of rows (_board_record), or for the pass of
        consider() under way - otherwise read now."""
        if self.skip_replay_losers == "off" or self.proof_required:
            return None                         # asked before anything is read
        if board:
            return self.replay_loser(strategy, timeframe, self._board_record(strategy))
        cached = self._pass_cache
        if cached is None or time.monotonic() - cached[0] > self.ROOM_CACHE_S:
            return self.replay_loser(strategy, timeframe)
        if strategy not in cached[1]:
            cached[1][strategy] = self.engine.strategy_record(strategy) or {}
        if not cached[2]:
            cached[2].append(self._live_stats())
        return self.replay_loser(strategy, timeframe, cached[1][strategy], cached[2][0])

    def replay_loser(self, strategy: str, timeframe: str, record: Optional[Dict[str, Any]] = None,
                     live: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Why a setup is skipped as a loser, if it is. Only while proof isn't asked for - on paper with
        require_proven off, where unproven setups are practised; proof_missing is stricter anyway. Not
        proven is one thing, evidence that it loses another: its replayed record the way Autopilot takes
        it averages -replay_loser_r (-0.05R) a trade or worse over min_replay_trades, and its held-out
        sessions say the same over MIN_HELD_OUT_TRADES - or its own closed trades (live_stats) average
        DRIFT_R or worse over MIN_LIVE. The records are read afresh, so a replay that recovers lifts it by
        itself. ``skip_replay_losers`` says which plays it covers: day trades by default - in the replay,
        skipping the swing losers left the kept swing trades no better."""
        scope = self.skip_replay_losers
        if scope == "off" or self.proof_required or (scope == "day" and kind_of(timeframe) != DAY):
            return None
        record = (self.engine.strategy_record(strategy) if record is None else record) or {}
        bar = -abs(self.replay_loser_r)
        trades, held = int(record.get("trades", 0)), record.get("out_of_sample") or {}
        n = int(held.get("trades", 0))
        if (trades >= self.min_replay_trades and n >= self.MIN_HELD_OUT_TRADES
                and float(record.get("expectancy_r", 0.0)) <= bar and float(held.get("expectancy_r", 0.0)) <= bar):
            return (f"{strategy} loses in the replay the way Autopilot takes it: {float(record['expectancy_r']):+.2f}R "
                    f"a trade over {trades} trades, {float(held['expectancy_r']):+.2f}R over the {n} in the held-out "
                    "sessions - skipped while that holds (Autopilot settings: skip replay losers)")
        real = ((self._live_stats() if live is None else live) or {}).get(strategy) or {}
        m = int(real.get("trades", 0))
        if m >= MIN_LIVE and float(real.get("expectancy_r", 0.0)) <= -DRIFT_R:
            # live_stats counts the in-app simulator's trades as well as the broker's - said so
            return (f"{strategy} is losing in the app's own trades (the simulator's and the broker's, over the recent "
                    f"sessions): {float(real['expectancy_r']):+.2f}R a trade over its last {m} - skipped while that "
                    "holds (Autopilot settings: skip replay losers)")
        return None

    def replay_losers(self) -> List[Dict[str, str]]:
        """The setups replay_loser skips right now - for the settings, and kept with each trade taken."""
        out: List[Dict[str, str]] = []
        if self.skip_replay_losers == "off" or self.proof_required:
            return out
        live = self._live_stats()
        for s in getattr(getattr(self.engine, "scanner", None), "strategies", None) or []:
            try:
                why = self.replay_loser(s.key, getattr(s.timeframe, "value", s.timeframe), live=live)
            except Exception:  # noqa: BLE001
                continue
            if why:
                out.append({"strategy": s.key, "why": why})
        return out

    def _live_stats(self) -> Dict[str, Any]:
        stats = getattr(self.engine, "live_stats", None)
        try:
            return stats() if callable(stats) else {}
        except Exception:  # noqa: BLE001
            return {}

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
    def decorate_play(self, row: Dict[str, Any], play: Any = None) -> Dict[str, Any]:
        """Tag a play row so the dashboard can show a '🤖 auto' badge - and, on a play Autopilot won't
        take, why not, in the gate's own words. ``play``: the Play the row was made from, when the caller
        has it - its numbers are the gate's, where the row's are rounded."""
        pid = row.get("id")
        why_not = self._why_not(row, play)
        # a play the engine's assessment refused waits for no cap: the refusal, in reason, is why
        waiting = (self._waiting_for(str(row.get("timeframe") or ""), str(row.get("strategy") or ""))
                   if why_not is None and pid not in self._refused else None)
        row["autopilot"] = {
            "eligible": why_not is None,
            # the first of Autopilot's checks the play fails (_play_check) - None when it passes them all
            "why_not": why_not,
            # it passes Autopilot's checks, but a cap has no room for it right now
            "waiting": waiting,
            "acted": pid in self._acted,
            # it tried the play and the engine's assessment or the order was refused (autopilot.skipped) -
            # not tried again today unless a setting changes; the reason below says why
            "skipped": pid in self._refused,
            # what the last pass said - the checks that change from pass to pass reach the row here
            "reason": self._last_reason.get(pid, ""),
        }
        return row

    def _why_not(self, row: Dict[str, Any], play: Any = None) -> Optional[str]:
        """Why Autopilot won't take a play row, if it won't: switched off, stopped for the day - which
        consider() says before any play is looked at - or the play's own checks."""
        if not self.enabled:
            return "Autopilot is off"
        if not self._live_ok():
            return "Autopilot is paper-only until autopilot.allow_live is set in config/config.yaml"
        if self.stopped_for_the_day:
            return "stopped for the day - the daily loss limit or the give-back rule was reached"
        if play is not None:
            return self._play_check(*self._facts(play), board=True)
        return self._play_check(str(row.get("timeframe") or ""), float(row.get("confidence", 0)),
                                float(row.get("reward_risk", 0)), str(row.get("kind") or ""),
                                str(row.get("status") or ""), row.get("noise") or [],
                                int(row.get("confirmations", 1)), str(row.get("strategy") or ""), board=True)

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
        if self._sent_today >= self.SENT_CEILING * self.max_auto_trades_per_day:
            return (f"{self._sent_today} entry orders sent today, counting the ones that bought nothing - "
                    f"{self.SENT_CEILING} times the daily cap")
        if len(opens) >= self.max_auto_positions:
            return f"all {self.max_auto_positions} auto positions are taken"
        full = self._kind_full(timeframe, opens)
        if full:
            return full
        same = sum(1 for t in opens if t.get("strategy") == strategy)
        if same >= self.max_per_strategy:
            return f"already holding {same} of this setup (max {self.max_per_strategy})"
        return None
