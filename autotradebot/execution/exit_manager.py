"""Automatic exit strategy - runs on every sync tick with no manual input.

For each OPEN trade it:
  1. marks the position to the current quote and records MAE / MFE. A quote printed before
     the entry filled is skipped whole - the first pass after a fill can still get the last
     print from before it, a price the trade never saw - and the exit fill joins the
     excursions when the trade closes (Repository.close_trade);
  2. closes it at the working stop (cut losses) or target (take profit) - or, at the
     first target of a play that has a second, takes ``scale_out_pct`` of it off, moves
     the stop to break-even and lets the rest run to the second target (Aziz: sell
     half at the target and bring the stop to the entry);
  3. closes an INTRADAY trade that isn't working once its setup's own window has passed
     (``intraday_time_stop``: the play's longest expected hold, its stop not yet at break-even),
     and flattens every INTRADAY trade a few minutes before the (holiday-aware) close;
  4. closes SWING trades held longer than ``max_swing_hold_days``;
  5. ratchets the protective stop:
        - to break-even (+ a small buffer) once the trade is +``breakeven_at_r`` R
        - then trails so the stop keeps ``trail_lock_ratio`` of the open R once
          the trade is past ``trail_start_r`` R
     The stop only ever moves in your favour and never past the last price.

A trade whose close order is still working is left alone. An exit that can't
be sent, or that the broker rejects or cancels, is sent again - waiting a
little longer after each try (``RETRY_DELAYS_S``). One that only had to wait on
the broker (a stop's cancel to confirm, its orders reloading after a connect) is
no failed try: it goes again in ``WAIT_RETRY_S`` - and a wait that goes on past
``WAIT_WARN_S`` is reported like a failed exit.

Between full passes, a streamed tick on a stock held runs a tick pass on just that
stock (``run_once(only=...)``, engine._sync_loop): steps 1, 2 and 5 on the fresh
price - the stop and target checks (and the first-target scale-out) and the ratchet;
the time exits (3, 4) and the overdue note wait for the next full pass. It closes at
the stop or target and moves the stop on the record at once, but keeps the excursions
in memory and leaves the note and the message about the move to the next full pass -
or to the exit, if one goes out first: they're written just before it.

Entries always need your click; exits never do.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Collection, Dict, FrozenSet, List, Optional, Tuple

from ..core.eventbus import BUS
from ..util import clock

log = logging.getLogger(__name__)


def scale_out_plan(t: Dict[str, Any], cfg) -> Optional[Tuple[float, Dict[str, float]]]:
    """At the first target of a play that has a second: the shares to take off, and the stop and
    target the rest gets - break-even (plus ``scale_out_lock_r`` R and the usual buffer) and the
    second target. None when the position is exited whole: no second target, the scale-out switched
    off, exits by hand, too few shares, or already taken. The exit manager and the target order
    resting at the broker (execution/protective_stops.py) share it, so both take off the same part."""
    if cfg is None:
        return None
    pct = float(getattr(cfg, "scale_out_pct", 0.0) or 0.0)
    target2 = t.get("target2_price")
    qty = abs(float(t.get("quantity") or 0.0))
    initial = abs(float(t.get("initial_quantity") or qty))
    entry = float(t.get("entry_price") or 0.0)
    if (not bool(t.get("managed_exit", True)) or not 0.0 < pct < 100.0 or not target2 or qty < 2
            or qty < initial - 1e-9 or entry <= 0):
        return None
    sign = 1.0 if t.get("side") == "LONG" else -1.0
    first_stop = t.get("initial_stop_price") or t.get("stop_price")
    risk_ps = abs(entry - float(first_stop)) if first_stop else 0.0
    part = float(max(1, min(int(qty) - 1, round(qty * pct / 100.0))))
    lock_r = float(getattr(cfg, "scale_out_lock_r", 0.0) or 0.0)
    buf = entry * float(getattr(cfg, "breakeven_buffer_bps", 5) or 0) / 1e4
    return part, {"stop_price": round(entry + sign * (lock_r * risk_ps + buf), 4), "target_price": float(target2)}


def stop_locked(side: str, stop: Any, entry: float) -> bool:
    """Whether a trade's stop has reached break-even or better - it can no longer lose, and it is
    "working": the exit manager moves it there once the trade has gone far enough its way."""
    if not stop or not entry:
        return False
    return float(stop) >= entry if side == "LONG" else float(stop) <= entry


class ExitManager:
    #: seconds to wait before the next exit for a trade, after its 1st, 2nd, ... one
    RETRY_DELAYS_S = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0)
    #: seconds before an exit that waited on the broker is tried again - no longer after each wait
    WAIT_RETRY_S = 5.0
    #: an exit still waiting on the broker after this long - past the minute its orders take to reload after a
    #: connect - is reported, and again every WAIT_REPEAT_S while it waits
    WAIT_WARN_S, WAIT_REPEAT_S = 90.0, 300.0

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
        self._waiting: Dict[str, float] = {}                 # trade id -> when its exit began waiting on the broker
        self._wait_warned: Dict[str, float] = {}             # trade id -> when that wait was last reported
        self._overdue_seen: set = set()     # trade ids we've already flagged as overdue
        self._not_held: set = set()         # trade ids whose position the broker doesn't show
        # this pass's quotes: each stock's price, and when the quote was printed
        self._prices: Dict[str, Tuple[Optional[float], Optional[dt.datetime]]] = {}
        #: the stocks the last full pass managed - a streamed tick on one of them wakes a tick pass (engine)
        self.watched: FrozenSet[str] = frozenset()
        # what tick passes leave for the next full pass: the low and high they saw, and a stop move not yet told
        self._extreme: Dict[str, Tuple[float, float]] = {}   # trade id -> (low, high)
        self._unannounced: Dict[str, Tuple[float, float]] = {}   # trade id -> (the stop a tick pass moved it to, R then)

    # ------------------------------------------------------------------ #
    def run_once(self, only: Optional[Collection[str]] = None) -> List[Dict[str, Any]]:
        """A full pass over the open trades, or with ``only`` a tick pass over the trades in just those stocks,
        whose streamed price just moved."""
        full = only is None
        if not bool(getattr(self.cfg, "enabled", True)):
            if full:
                self.watched = frozenset()
            return []
        if not full and not only:
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
        if full:
            self.watched = frozenset(t["symbol"] for t in mine)
        else:
            # a trade the broker doesn't hold waits for the engine's broker check - trying its exit every
            # second would ask the broker for its orders and account each time, for nothing
            mine = [t for t in mine if t["symbol"] in only and t["id"] not in self._not_held]
        self._prices = self._fetch_prices({t["symbol"] for t in mine})
        acted: List[Dict[str, Any]] = []
        try:
            for t in mine:
                try:
                    r = self._manage(t, full)
                    if r:
                        acted.append(r)
                except Exception:  # noqa: BLE001
                    log.exception("exit manager failed for %s", t.get("id"))
        finally:
            self._prices = {}
        return acted

    def _fetch_prices(self, symbols) -> Dict[str, Tuple[Optional[float], Optional[dt.datetime]]]:
        """One quote per symbol, fetched concurrently - with several positions a
        sequential pass would delay the last one's stop check by seconds."""
        syms = sorted(symbols)
        if len(syms) < 2:
            return {s: self._fetch_price(s) for s in syms}
        with ThreadPoolExecutor(max_workers=min(8, len(syms))) as ex:
            return dict(zip(syms, ex.map(self._fetch_price, syms)))

    # ------------------------------------------------------------------ #
    def _quote(self, symbol: str) -> Tuple[Optional[float], Optional[dt.datetime]]:
        if symbol in self._prices:
            return self._prices[symbol]
        return self._fetch_price(symbol)

    def _fetch_price(self, symbol: str) -> Tuple[Optional[float], Optional[dt.datetime]]:
        """A stock's price, and when the quote was printed - None when the quote doesn't say."""
        try:
            q = self.quote_fn(symbol)
        except Exception as e:  # noqa: BLE001
            log.debug("exit manager: no quote for %s (%s)", symbol, e)
            return None, None
        px = getattr(q, "last", 0.0) or getattr(q, "mid", 0.0)
        at = getattr(q, "ts", None)
        return (float(px) if px else None), (at if isinstance(at, dt.datetime) else None)

    def _close(self, tid: str, reason: str, qty: Optional[float] = None,
               after_fill: Optional[Dict[str, float]] = None, seen: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Send the exit - the whole position, or with ``qty`` the part taken off at the first target.
        ``seen``: the price that triggered it, which the fill is measured against."""
        tries, next_at = self._tries.get(tid, (0, 0.0))
        now = time.monotonic()
        if now < next_at:
            return None                     # the last exit didn't take - wait before sending another
        extra = {"qty": qty, "after_fill": after_fill} if qty is not None else {}
        if seen:
            extra["decision_price"] = float(seen)
        out = self.executor.close_trade(tid, reason=reason, **extra) or {}
        if out.get("not_held"):
            # nothing to sell: say it once; the engine's broker check removes the record once confirmed
            if tid not in self._not_held:
                self._not_held.add(tid)
                log.warning("AUTO-EXIT %s skipped: %s", tid, out.get("reason"))
                self.bus.publish("exit.not_held", trade_id=tid, reason=out.get("reason"))
            return None
        if out.get("wait"):
            # the broker has yet to answer (Executor.close_trade): no failed exit - tried again shortly, the
            # back-off untouched, so it goes out within seconds of the answer, not minutes
            self._tries[tid] = (tries, now + self.WAIT_RETRY_S)
            why = out.get("reason") or "waiting on the broker"
            self._note_wait(tid, reason, why, tries, now)
            return None
        self._waiting.pop(tid, None)
        self._wait_warned.pop(tid, None)
        tries += 1
        wait = self.RETRY_DELAYS_S[min(tries, len(self.RETRY_DELAYS_S)) - 1]
        self._tries[tid] = (tries, now + wait)
        if out.get("ok"):
            trade = out.get("trade") or {}
            if qty is not None:
                log.info("AUTO-EXIT %s: %s - %s of the position off, %s left, %.2f banked%s", tid, reason, qty,
                         trade.get("quantity"), trade.get("banked_pl") or 0.0, f"  (try {tries})" if tries > 1 else "")
                self.bus.publish("exit.scaled", trade_id=tid, reason=reason, qty=qty, trade=trade,
                                 status=out.get("status"))
                return {"trade_id": tid, "reason": reason, "trade": trade, "reduced": True}
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

    def _note_wait(self, tid: str, reason: str, why: str, tries: int, now: float) -> None:
        """Log an exit's wait on the broker - and once it has gone on past WAIT_WARN_S, report it like a failed exit,
        again every WAIT_REPEAT_S: a wait is no failed try, but one that doesn't end leaves the position without its
        exit, and that must not go unseen. It ends when a full pass wants no exit for the trade (the price came back:
        _manage) - never for the time between tries, which one waiting out an order list that times out stretches."""
        since = self._waiting.setdefault(tid, now)
        if self._last_failure.get(tid) != why:
            self._last_failure[tid] = why
            log.info("AUTO-EXIT %s (%s) waits - next try in %.0fs: %s", tid, reason, self.WAIT_RETRY_S, why)
        waited = now - since
        if waited < self.WAIT_WARN_S or now - self._wait_warned.get(tid, float("-inf")) < self.WAIT_REPEAT_S:
            return
        self._wait_warned[tid] = now
        log.warning("AUTO-EXIT %s (%s) has waited on the broker for %.0fs and is still not sent: %s", tid, reason,
                    waited, why)
        self.bus.publish("exit.failed", trade_id=tid, reason=f"waiting on the broker for {waited:.0f}s - {why}",
                         attempt=tries + 1, retry_in_s=round(self.WAIT_RETRY_S))

    def _scale_out(self, t: Dict[str, Any], managed: bool, entry: float, sign: float,
                   risk_ps: float) -> Optional[Tuple[float, Dict[str, float]]]:
        """At the first target of a play that has a second: the shares to take off and the stop
        and target the rest gets - break-even (plus ``scale_out_lock_r`` R and the usual buffer)
        and the second target. None when the position is exited whole: no second target, the
        scale-out switched off, exits by hand, too few shares, or already taken."""
        return scale_out_plan({**t, "managed_exit": managed}, self.cfg)

    def _forget_all_but(self, open_ids: set) -> None:
        """Drop what's remembered about trades that are no longer open."""
        for book in (self._tries, self._last_failure, self._extreme, self._unannounced, self._waiting,
                     self._wait_warned):
            for tid in [k for k in book if k not in open_ids]:
                del book[tid]
        self._not_held &= open_ids
        self._overdue_seen &= open_ids

    # ------------------------------------------------------------------ #
    def _manage(self, t: Dict[str, Any], full: bool = True) -> Optional[Dict[str, Any]]:
        """One trade's exits and stop. ``full`` False: a tick pass - the stop and target checks and the stop's
        move on the record only (see the module docstring)."""
        sym, side = t["symbol"], t["side"]
        entry = float(t["entry_price"] or 0.0)
        if entry <= 0:
            return None
        px, at = self._quote(sym)
        if px is None:
            return None
        if not _since_entry(at, t.get("entry_time")):
            # a quote printed before the entry filled is a price the trade never saw - the first pass after a fill
            # can still get the last print from before it, from the quote cache or a snapshot's Ticker. Nothing is
            # read off it: not the excursions, not the stop or target, not the ratchet. The next quote is the trade's
            log.debug("exit manager: %s quote of %s is from before the entry at %s - skipped",
                      sym, at, t.get("entry_time"))
            return None

        sign = 1.0 if side == "LONG" else -1.0
        init_stop = t.get("initial_stop_price") or t.get("stop_price")
        work_stop = t.get("stop_price") or init_stop
        target = t.get("target_price")
        risk_ps = abs(entry - float(init_stop)) if init_stop else 0.0
        r_now = ((px - entry) * sign / risk_ps) if risk_ps else 0.0

        # --- excursions + high-water mark ------------------------------ #
        # a tick pass keeps the low and high it saw in memory; the next full pass (or the tick pass that sends an
        # exit) folds them in and writes the record, only when something changed - not a database write per
        # position every second
        # (the exit fill joins the excursions when the trade closes: Repository.close_trade)
        fav = max(0.0, (px - entry) * sign)
        adv = max(0.0, (entry - px) * sign)
        low, high = self._extreme.pop(t["id"], (px, px))
        low, high = min(low, px), max(high, px)
        best, worst = (high, low) if side == "LONG" else (low, high)
        mfe = max(float(t.get("mfe") or 0.0), fav, (best - entry) * sign)
        mae = max(float(t.get("mae") or 0.0), adv, (entry - worst) * sign)
        hwm = t.get("hwm_price") or entry
        hwm = max(hwm, best) if side == "LONG" else min(hwm, best)

        def excursions() -> Dict[str, float]:
            if any(_changed(new, t.get(k)) for new, k in ((hwm, "hwm_price"), (mae, "mae"), (mfe, "mfe"))):
                return dict(hwm_price=hwm, mae=mae, mfe=mfe)
            return {}

        def tell(new_stop: float, r: float, **write: Any) -> None:
            """The note on the record and the message for a stop the exit manager moved; ``r``: where the
            trade stood when it moved."""
            self.repo.update_trade_risk(t["id"], note_append=f"stop->{new_stop:.2f} @ {r:.1f}R", **write)
            # what the stop now keeps if it's hit is not where the trade stands: a +1.4R trade whose
            # stop goes to break-even locks about +0.3R. Send both (r stays, the same as r_now, for
            # older readers)
            kept_r = (round(new_stop, 4) - entry) * sign / risk_ps
            self.bus.publish("exit.stop_moved", trade_id=t["id"], symbol=sym,
                             new_stop=round(new_stop, 4), locked_r=round(kept_r, 2),
                             r_now=round(r, 2), r=round(r, 2))

        def close(reason: str, **kw: Any) -> Optional[Dict[str, Any]]:
            # a trade whose exit goes out gets no next full pass - it's left alone while the exit works,
            # then closed - so the record gets what the tick passes saw first, as a full pass writes it: the
            # excursions, and a stop move not yet told (still the record's: see step 3), in one write
            try:
                write = {} if full else excursions()
                told = self._unannounced.pop(t["id"], None)
                if told is not None and risk_ps > 0 and work_stop and abs(told[0] - float(work_stop)) <= 0.01:
                    tell(*told, **write)
                elif write:
                    self.repo.update_trade_risk(t["id"], **write)
            except Exception:  # noqa: BLE001 - the exit goes out all the same
                log.exception("exit manager: could not write %s before its exit", t["id"])
            return self._close(t["id"], reason, seen=px, **kw)

        if full:
            write = excursions()
            if write:
                self.repo.update_trade_risk(t["id"], **write)
        else:
            self._extreme[t["id"]] = (low, high)

        managed = bool(t.get("managed_exit", True))
        overdue = t.get("time_status") == "overdue"
        # a day trade past its window that isn't working is closed below (2.); say so rather than "review it"
        timing_out = (overdue and managed and t.get("timeframe") == "INTRADAY"
                      and bool(getattr(self.cfg, "intraday_time_stop", False)) and not stop_locked(side, work_stop, entry))

        # --- 0. expected-exit overwatch ------------------------------------ #
        if (full and overdue and not t.get("overdue_notified")
                and t["id"] not in self._overdue_seen):
            self._overdue_seen.add(t["id"])
            self.repo.note_overdue(t["id"])
            winning = mfe > 0.5 * risk_ps if risk_ps else fav > 0
            self.bus.publish(
                "trade.overdue", trade_id=t["id"], symbol=sym, r=round(r_now, 2),
                held=t.get("held_label"), winning=bool(winning),
                msg=(f"{sym} {side} is past its expected exit "
                     f"({t.get('held_label')} held, {r_now:+.1f}R now) - "
                     + ("its window has passed without it working, so it is being closed." if timing_out
                        else "let it run or take the gain." if winning else "review it.")),
            )
            log.info("OVERDUE %s %s: %s held, %.1fR", sym, side, t.get("held_label"), r_now)

        # --- 1. hard exits ------------------------------------------- #
        if work_stop:
            if (side == "LONG" and px <= float(work_stop)) or (side == "SHORT" and px >= float(work_stop)):
                moved = init_stop is not None and abs(float(work_stop) - float(init_stop)) > 1e-6
                return close("trailing-stop" if moved else "stop")
        resting = getattr(self.executor, "target_resting", None)
        if target and not (callable(resting) and resting(t["id"])):
            # (a target order resting at the broker is the broker's to fill, on prices the app may see late)
            if (side == "LONG" and px >= float(target)) or (side == "SHORT" and px <= float(target)):
                part = self._scale_out(t, managed, entry, sign, risk_ps)
                if part is not None:
                    return close("target-1", qty=part[0], after_fill=part[1])
                return close("target")

        # --- 2. time / session exits ------------------------------- #
        # (full passes only: a tick pass is about the price, and a time-stop goes out after the overdue note)
        if full and timing_out:
            # Aziz: a day trade that hasn't moved in its time is wrong - its setup's window has passed, and
            # it only holds a slot and capital a fresh setup could use. One that is working (its stop at
            # break-even or better) keeps its trail until the flatten
            return close("time-stop")
        flat_min = float(getattr(self.cfg, "flatten_intraday_before_close_min", 10) or 0)
        if full and managed and t.get("timeframe") == "INTRADAY" and flat_min > 0:
            if clock.minutes_to_close() <= flat_min:
                return close("eod-flatten")

        max_hold = int(getattr(self.cfg, "max_swing_hold_days", 0) or 0)
        if full and managed and max_hold > 0 and t.get("timeframe") == "SWING" and t.get("entry_time"):
            try:
                et = dt.datetime.fromisoformat(t["entry_time"])
                age_days = (dt.datetime.utcnow() - et.replace(tzinfo=None)).days
                if age_days >= max_hold:
                    return close("time-stop")
            except Exception:  # noqa: BLE001
                pass

        if full:
            # no exit wanted on a full pass: a wait its exit had on the broker is over - wanted again, it starts afresh
            self._waiting.pop(t["id"], None)
            self._wait_warned.pop(t["id"], None)

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

        was = float(work_stop or init_stop)
        told = self._unannounced.pop(t["id"], None) if full else None
        if abs(new_stop - was) > 0.01:
            if not full:
                # the exit check reads the record, so the stop moves there now; its note and message wait for
                # the next full pass, or the exit if one goes first - a trending stock would add one every second
                self.repo.update_trade_risk(t["id"], stop_price=round(new_stop, 4))
                self._unannounced[t["id"]] = (round(new_stop, 4), r_now)
                return None
            tell(new_stop, r_now, stop_price=round(new_stop, 4))
        elif told is not None and abs(told[0] - was) <= 0.01:
            # a move a tick pass made, still on the record, is told now. A stop something else set since (the
            # break-even after a part came off at the first target) isn't the exit manager's move to tell
            tell(*told)
        return None


def _since_entry(at: Optional[dt.datetime], entry_time: Any) -> bool:
    """Whether a quote printed at ``at`` is from the trade's life - at or after its entry fill. A quote with no
    usable time, or a trade with no entry time, counts - a trade is never left unmanaged for want of a time."""
    if at is None or not entry_time:
        return True
    try:
        entered = dt.datetime.fromisoformat(str(entry_time))
    except ValueError:
        return True
    if entered.tzinfo is None:
        entered = entered.replace(tzinfo=dt.timezone.utc)      # the record keeps naive UTC
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.timezone.utc)
    return at >= entered


def _changed(new: float, old: Any) -> bool:
    """Whether a value differs from the record's - which keeps six decimals, so a float the same to within
    that reads as unchanged."""
    return old is None or abs(float(new) - float(old)) > 1e-6
