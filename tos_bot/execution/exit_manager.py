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

A trade whose close order is still working is left alone. An exit that can't
be sent, or that the broker rejects or cancels, is sent again - waiting a
little longer after each try (``RETRY_DELAYS_S``).

Entries always need your click; exits never do.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.eventbus import BUS
from ..util import clock

log = logging.getLogger(__name__)


class ExitManager:
    #: seconds to wait before the next exit for a trade, after its 1st, 2nd, ... one
    RETRY_DELAYS_S = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0)

    def __init__(self, repo, executor, quote_fn: Callable[[str], Any], cfg, bus=BUS,
                 venue: Optional[str] = None) -> None:
        self.repo = repo
        self.venue = venue                  # only manage trades held on this venue
        self.executor = executor
        self.quote_fn = quote_fn
        self.cfg = cfg
        self.bus = bus
        self._tries: Dict[str, Tuple[int, float]] = {}  # trade id -> (exits sent, monotonic time the next may go)
        self._last_failure: Dict[str, str] = {}         # trade id -> the failure last published
        self._overdue_seen: set = set()     # trade ids we've already flagged as overdue
        self._not_held: set = set()         # trade ids whose position the broker doesn't show
        self._prices: Dict[str, Optional[float]] = {}   # this pass's quotes

    # ------------------------------------------------------------------ #
    def run_once(self) -> List[Dict[str, Any]]:
        if not bool(getattr(self.cfg, "enabled", True)):
            return []
        try:
            open_trades = self.repo.open_trades()
        except Exception:  # noqa: BLE001
            log.exception("exit manager: could not list open trades")
            return []
        self._forget_all_but({t["id"] for t in open_trades})
        # only trades held on this venue - an exit can't be sent anywhere else -
        # and not while one of their close orders is still working
        in_flight = self.executor.pending_exit_trade_ids()
        mine = [t for t in open_trades if t["id"] not in in_flight and not t.get("pair_id")   # the pair desk's
                and not (self.venue and (t.get("broker") or self.venue) != self.venue)]
        self._prices = self._fetch_prices({t["symbol"] for t in mine})
        acted: List[Dict[str, Any]] = []
        try:
            for t in mine:
                try:
                    r = self._manage(t)
                    if r:
                        acted.append(r)
                except Exception:  # noqa: BLE001
                    log.exception("exit manager failed for %s", t.get("id"))
        finally:
            self._prices = {}
        return acted

    def _fetch_prices(self, symbols) -> Dict[str, Optional[float]]:
        """One quote per symbol, fetched concurrently - with several positions a
        sequential pass would delay the last one's stop check by seconds."""
        syms = sorted(symbols)
        if len(syms) < 2:
            return {s: self._fetch_price(s) for s in syms}
        with ThreadPoolExecutor(max_workers=min(8, len(syms))) as ex:
            return dict(zip(syms, ex.map(self._fetch_price, syms)))

    # ------------------------------------------------------------------ #
    def _quote_price(self, symbol: str) -> Optional[float]:
        if symbol in self._prices:
            return self._prices[symbol]
        return self._fetch_price(symbol)

    def _fetch_price(self, symbol: str) -> Optional[float]:
        try:
            q = self.quote_fn(symbol)
        except Exception as e:  # noqa: BLE001
            log.debug("exit manager: no quote for %s (%s)", symbol, e)
            return None
        px = getattr(q, "last", 0.0) or getattr(q, "mid", 0.0)
        return float(px) if px else None

    def _close(self, tid: str, reason: str) -> Optional[Dict[str, Any]]:
        tries, next_at = self._tries.get(tid, (0, 0.0))
        now = time.monotonic()
        if now < next_at:
            return None                     # the last exit didn't take - wait before sending another
        out = self.executor.close_trade(tid, reason=reason) or {}
        if out.get("not_held"):
            # nothing to sell: say it once; the engine's broker check removes the record once confirmed
            if tid not in self._not_held:
                self._not_held.add(tid)
                log.warning("AUTO-EXIT %s skipped: %s", tid, out.get("reason"))
                self.bus.publish("exit.not_held", trade_id=tid, reason=out.get("reason"))
            return None
        tries += 1
        wait = self.RETRY_DELAYS_S[min(tries, len(self.RETRY_DELAYS_S)) - 1]
        self._tries[tid] = (tries, now + wait)
        if out.get("ok"):
            trade = out.get("trade") or {}
            log.info("AUTO-EXIT %s: %s  P/L %.2f%s", tid, reason, trade.get("realized_pl") or 0.0,
                     f"  (try {tries})" if tries > 1 else "")
            self.bus.publish("exit.triggered", trade_id=tid, reason=reason, trade=trade)
            return {"trade_id": tid, "reason": reason, "trade": trade}
        why = out.get("reason") or "unknown error"
        log.warning("AUTO-EXIT %s (%s) not sent, try %d - next in %.0fs: %s", tid, reason, tries, wait, why)
        if self._last_failure.get(tid) != why:
            self._last_failure[tid] = why
            self.bus.publish("exit.failed", trade_id=tid, reason=why, attempt=tries, retry_in_s=round(wait))
        return None

    def _forget_all_but(self, open_ids: set) -> None:
        """Drop what's remembered about trades that are no longer open."""
        for book in (self._tries, self._last_failure):
            for tid in [k for k in book if k not in open_ids]:
                del book[tid]
        self._not_held &= open_ids
        self._overdue_seen &= open_ids

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

        # --- 0. expected-exit overwatch (informational, never closes) ----- #
        if (t.get("time_status") == "overdue" and not t.get("overdue_notified")
                and t["id"] not in self._overdue_seen):
            self._overdue_seen.add(t["id"])
            self.repo.note_overdue(t["id"])
            winning = mfe > 0.5 * risk_ps if risk_ps else fav > 0
            self.bus.publish(
                "trade.overdue", trade_id=t["id"], symbol=sym, r=round(r_now, 2),
                held=t.get("held_label"), winning=bool(winning),
                msg=(f"{sym} {side} is past its expected exit "
                     f"({t.get('held_label')} held, {r_now:+.1f}R now) - "
                     f"{'let it run or take the gain' if winning else 'review it'}."),
            )
            log.info("OVERDUE %s %s: %s held, %.1fR", sym, side, t.get("held_label"), r_now)

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

        be_r = float(getattr(self.cfg, "breakeven_at_r", 1.3) or 0.0)
        if be_r > 0 and r_now >= be_r:
            # lock a small profit rather than a pure scratch - a +1.3R trade
            # that pulls back should still book something, not go to zero.
            lock_r = float(getattr(self.cfg, "breakeven_lock_r", 0.3) or 0.0)
            buf = entry * float(getattr(self.cfg, "breakeven_buffer_bps", 5) or 0) / 1e4
            be = entry + sign * (lock_r * risk_ps + buf)
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
