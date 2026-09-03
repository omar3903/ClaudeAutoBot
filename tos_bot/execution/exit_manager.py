"""Automatic exit strategy - runs on every sync tick with no manual input.

For each OPEN trade it:
  1. marks the position to the current quote and records MAE / MFE;
  2. closes it at the working stop (cut losses) or target (take profit);
  3. flattens INTRADAY trades a few minutes before the (holiday-aware) close;
  4. closes SWING trades held longer than ``max_swing_hold_days``;
  5. ratchets the protective stop:
        - to break-even (+ a small buffer) once the trade is +``breakeven_at_r`` R
        - then trails so the stop keeps ``trail_lock_ratio`` of the open R once
          the trade is past ``trail_start_r`` R
     The stop only ever moves in your favour and never past the last price.

Entries always need your click; exits never do.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Dict, List, Optional

from ..core.eventbus import BUS
from ..util import clock

log = logging.getLogger(__name__)


class ExitManager:
    def __init__(self, repo, executor, quote_fn: Callable[[str], Any], cfg, bus=BUS) -> None:
        self.repo = repo
        self.executor = executor
        self.quote_fn = quote_fn
        self.cfg = cfg
        self.bus = bus
        self._closing: set = set()          # trade ids we've already sent a close for

    # ------------------------------------------------------------------ #
    def run_once(self) -> List[Dict[str, Any]]:
        if not bool(getattr(self.cfg, "enabled", True)):
            return []
        acted: List[Dict[str, Any]] = []
        try:
            open_trades = self.repo.open_trades()
        except Exception:  # noqa: BLE001
            log.exception("exit manager: could not list open trades")
            return []
        for t in open_trades:
            if t["id"] in self._closing:
                continue
            try:
                r = self._manage(t)
                if r:
                    acted.append(r)
            except Exception:  # noqa: BLE001
                log.exception("exit manager failed for %s", t.get("id"))
        return acted

    # ------------------------------------------------------------------ #
    def _quote_price(self, symbol: str) -> Optional[float]:
        try:
            q = self.quote_fn(symbol)
        except Exception as e:  # noqa: BLE001
            log.debug("exit manager: no quote for %s (%s)", symbol, e)
            return None
        px = getattr(q, "last", 0.0) or getattr(q, "mid", 0.0)
        return float(px) if px else None

    def _close(self, tid: str, reason: str) -> Optional[Dict[str, Any]]:
        self._closing.add(tid)
        out = self.executor.close_trade(tid, reason=reason)
        if out and out.get("ok"):
            trade = out.get("trade") or {}
            log.info("AUTO-EXIT %s: %s  P/L %.2f", tid, reason, trade.get("realized_pl") or 0.0)
            self.bus.publish("exit.triggered", trade_id=tid, reason=reason, trade=trade)
            return {"trade_id": tid, "reason": reason, "trade": trade}
        # close didn't take (order working / market shut) - allow a retry next tick
        self._closing.discard(tid)
        return None

    # ------------------------------------------------------------------ #
    def _manage(self, t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        sym, side = t["symbol"], t["side"]
        entry = float(t["entry_price"] or 0.0)
        if entry <= 0:
            return None
        px = self._quote_price(sym)
        if px is None:
            return None

        sign = 1.0 if side == "LONG" else -1.0
        init_stop = t.get("initial_stop_price") or t.get("stop_price")
        work_stop = t.get("stop_price") or init_stop
        target = t.get("target_price")
        risk_ps = abs(entry - float(init_stop)) if init_stop else 0.0
        r_now = ((px - entry) * sign / risk_ps) if risk_ps else 0.0

        # --- excursions + high-water mark ------------------------------ #
        fav = max(0.0, (px - entry) * sign)
        adv = max(0.0, (entry - px) * sign)
        mfe = max(float(t.get("mfe") or 0.0), fav)
        mae = max(float(t.get("mae") or 0.0), adv)
        hwm = t.get("hwm_price") or entry
        hwm = max(hwm, px) if side == "LONG" else min(hwm, px)
        self.repo.update_trade_risk(t["id"], hwm_price=hwm, mae=mae, mfe=mfe)

        managed = bool(t.get("managed_exit", True))

        # --- 1. hard exits ------------------------------------------- #
        if work_stop:
            if (side == "LONG" and px <= float(work_stop)) or (side == "SHORT" and px >= float(work_stop)):
                moved = init_stop is not None and abs(float(work_stop) - float(init_stop)) > 1e-6
                return self._close(t["id"], "trailing-stop" if moved else "stop")
        if target:
            if (side == "LONG" and px >= float(target)) or (side == "SHORT" and px <= float(target)):
                return self._close(t["id"], "target")

        # --- 2. time / session exits ------------------------------- #
        flat_min = float(getattr(self.cfg, "flatten_intraday_before_close_min", 10) or 0)
        if managed and t.get("timeframe") == "INTRADAY" and flat_min > 0:
            if clock.minutes_to_close() <= flat_min:
                return self._close(t["id"], "eod-flatten")

        max_hold = int(getattr(self.cfg, "max_swing_hold_days", 0) or 0)
        if managed and max_hold > 0 and t.get("timeframe") == "SWING" and t.get("entry_time"):
            try:
                et = dt.datetime.fromisoformat(t["entry_time"])
                age_days = (dt.datetime.utcnow() - et.replace(tzinfo=None)).days
                if age_days >= max_hold:
                    return self._close(t["id"], "time-stop")
            except Exception:  # noqa: BLE001
                pass

        # --- 3. move the protective stop in our favour ------------ #
        if not managed or risk_ps <= 0:
            return None
        new_stop = float(work_stop) if work_stop else float(init_stop)

        be_r = float(getattr(self.cfg, "breakeven_at_r", 1.0) or 0.0)
        if be_r > 0 and r_now >= be_r:
            buf = entry * float(getattr(self.cfg, "breakeven_buffer_bps", 5) or 0) / 1e4
            be = entry + sign * buf
            new_stop = max(new_stop, be) if side == "LONG" else min(new_stop, be)

        trail_start = float(getattr(self.cfg, "trail_start_r", 1.5) or 0.0)
        lock = float(getattr(self.cfg, "trail_lock_ratio", 0.5) or 0.0)
        if trail_start > 0 and lock > 0 and r_now >= trail_start:
            locked_r = (r_now * lock)
            trail = entry + sign * locked_r * risk_ps
            new_stop = max(new_stop, trail) if side == "LONG" else min(new_stop, trail)

        # never move the stop through the current price
        if side == "LONG":
            new_stop = min(new_stop, px - 0.01)
        else:
            new_stop = max(new_stop, px + 0.01)

        if abs(new_stop - float(work_stop or init_stop)) > 0.01:
            self.repo.update_trade_risk(
                t["id"], stop_price=round(new_stop, 4),
                note_append=f"stop->{new_stop:.2f} @ {r_now:.1f}R",
            )
            self.bus.publish("exit.stop_moved", trade_id=t["id"], symbol=sym,
                             new_stop=round(new_stop, 4), r=round(r_now, 2))
        return None
