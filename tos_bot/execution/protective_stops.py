"""Orders resting at the broker for every open position - protection, and profit-taking, that outlive the app.

The exit manager watches prices and sends its own exits, which is no protection at all while the
app, the computer or the connection is down, and a slow one on delayed quotes. On a venue that can
hold them (``supports_native_stop``), the executor keeps at the broker, for each open trade:

* a good-till-cancelled **STOP** at the trade's working stop, for exactly the shares it holds;
* a good-till-cancelled **LIMIT at the target** (``execution.native_target``) - for the part that
  comes off at the first target when the position scales out, for all of it otherwise.

The two share a **one-cancels-all group** at the broker (IBKR's OCA type 3: when one fills, the
other is reduced by the shares filled). So the target taking half off shrinks the stop to the
other half, the stop filling cancels the target, and the two can never both fill for the whole
position. The broker works them on real prices, which is what makes the target worth having on
delayed quotes: the app would see the price fifteen minutes late.

Resting orders bring two dangers, and the rules here exist for them:

* **Two exits on one position.** Before the app sends any exit of its own it *stands the resting
  orders down*: cancels them and waits for the broker to confirm. If one filled first, that fill
  is booked, and if it closed the position no second exit goes out. If the broker can't confirm in
  time, no exit goes out on this pass - the exit manager tries again in seconds. While a target
  rests at the broker the exit manager leaves the target to it.
* **An order that outlives its position** would open a position the other way when it triggers.
  Orders are only placed while the broker shows the shares and its order list could be read for
  certain; every pass cancels a tracked order whose trade record is no longer open; and a sweep
  cancels any ``stop:<trade id>`` / ``tgt:<trade id>`` order at the broker whose trade isn't open,
  or that duplicates another. Before placing, the broker's working orders are searched for orders
  an earlier run left - they are adopted, never doubled.

The trade record is the single source of truth: each pass compares the record's shares, stop and
target with the orders at the broker. The stop's price is moved in place (the break-even and
trailing ratchets), at most once every ``STOP_MOVE_S``. Anything structural - no target resting
where one belongs, a target for the wrong shares or price, the scale-out just taken - has the pair
stood down and placed afresh in a new group, because an order can't change its group. A broker
that refuses the pair gets a plain stop, and the app works the target itself as before.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..brokers.base import DONE_STATUSES, BrokerError
from ..core.enums import OrderType, Side, TimeInForce
from ..core.models import OrderRequest, OrderResult
from .exit_manager import scale_out_plan

log = logging.getLogger(__name__)

TAG = "stop:"
TARGET_TAG = "tgt:"
OCA_REDUCE = 3                     # IBKR: the others are reduced by what fills, no routing block


@dataclass
class _Stop:
    """One order resting at the broker: a stop (``price`` is its trigger) or a target (its limit)."""
    order_id: str
    trade_id: str
    symbol: str
    qty: float
    price: float
    moved_at: float = 0.0
    unseen: int = 0
    group: str = ""                # the one-cancels-all group it was placed in; "" = on its own


def stop_tag(trade_id: str) -> str:
    return f"{TAG}{trade_id}"


def target_tag(trade_id: str) -> str:
    return f"{TARGET_TAG}{trade_id}"


def tick(price: float) -> float:
    """US stocks quote in cents from a dollar up, and in hundredths of a cent below."""
    return 0.01 if price >= 1.0 else 0.0001


def on_tick(price: float) -> float:
    return round(float(price), 2 if price >= 1.0 else 4)


class ProtectiveStops:
    """Mixed into the Executor (it uses its broker, repo, bus, venue and booking)."""

    #: seconds between moves of one stop's price - a trailing stop would otherwise send an order a tick
    STOP_MOVE_S = 15.0
    #: seconds between sweeps of the broker's orders for resting orders without an open trade
    SWEEP_S = 60.0
    #: how long a stand-down waits for the broker to confirm the cancel, and how often it asks
    STAND_DOWN_S, STAND_DOWN_POLL_S = 3.0, 0.25
    #: seconds before a stop that couldn't be placed, or was lost, is tried again
    STOP_RETRY_S = 30.0
    #: ...and when the broker's share count is only catching up with a fill that arrived in pieces
    SHARES_RETRY_S = 5.0
    #: seconds before a target that couldn't be placed, or was lost, is tried again
    TARGET_RETRY_S = 300.0
    #: a pair the broker finishes unfilled this soon after placing it was refused, not lost
    REFUSED_WITHIN_S = 20.0

    def _init_stops(self) -> None:
        self._stops: Dict[str, _Stop] = {}
        self._targets: Dict[str, _Stop] = {}
        self._stop_retry: Dict[str, float] = {}
        self._target_retry: Dict[str, float] = {}
        self._stop_notes: Dict[str, str] = {}
        self._plain: set = set()          # trades whose one-cancels-all pair the broker refused: a stop alone
        self._group_seq = getattr(self, "_group_seq", 0)
        self._swept_at = 0.0

    def native_stops_on(self) -> bool:
        return bool(getattr(self.cfg, "native_stop", True)) and bool(getattr(self.broker, "supports_native_stop", False))

    def native_targets_on(self) -> bool:
        """A target rests at the broker only beside a stop, and only once the engine has said how
        positions scale out (``exit_cfg``) - the shares a target covers depend on it."""
        return (self.native_stops_on() and bool(getattr(self.cfg, "native_target", True))
                and getattr(self, "exit_cfg", None) is not None)

    def protective_stops(self) -> List[Dict[str, Any]]:
        """The stops resting at the broker, for the dashboard and the tests."""
        return [{"trade_id": s.trade_id, "symbol": s.symbol, "order_id": s.order_id, "qty": s.qty, "stop_price": s.price}
                for s in self._stops.values()]

    def resting_targets(self) -> List[Dict[str, Any]]:
        return [{"trade_id": s.trade_id, "symbol": s.symbol, "order_id": s.order_id, "qty": s.qty, "limit_price": s.price}
                for s in self._targets.values()]

    def target_resting(self, trade_id: str) -> bool:
        """Whether the broker is working this trade's target - the exit manager then leaves it to it."""
        return trade_id in self._targets

    # ------------------------------------------------------------------ #
    #  Each sync pass                                                    #
    # ------------------------------------------------------------------ #
    def _watch_stops(self) -> None:
        """Book what the broker filled; notice what it cancelled, rejected or lost. Targets first:
        a target that closed the position takes the stop with it, which is no stop lost."""
        for book, fill in ((self._targets, self._book_target_fill), (self._stops, self._book_stop_fill)):
            for tid, o in list(book.items()):
                if book.get(tid) is not o:
                    continue                             # booked or dropped while the other order was handled
                try:
                    res = self.broker.get_order(o.order_id)
                except Exception:  # noqa: BLE001
                    continue
                if res.status == "FILLED":
                    fill(o, res)
                elif res.status in DONE_STATUSES:
                    if float(res.filled_qty or 0.0) > 0:
                        fill(o, res)
                    self._lose(book, o, f"the broker {res.status.lower()} it: {res.message or 'no reason given'}")
                elif res.status == "UNKNOWN":
                    if self._broker_resyncing():
                        continue
                    o.unseen += 1
                    if o.unseen >= self.LOST_AFTER_POLLS:
                        self._lose(book, o, "the broker no longer knows it")
                else:
                    o.unseen = 0

    def _protect_positions(self) -> None:
        """Make the broker's resting orders match the trade records: place, resize, move, cancel."""
        if not self.native_stops_on() or getattr(self.broker, "is_connected", True) is False:
            return
        trades = [t for t in self.repo.open_trades()
                  if (t.get("broker") or "paper") == self.venue and not t.get("pair_id")]
        open_ids = {t["id"] for t in trades}
        for book in (self._stops, self._targets):
            for tid in [k for k in book if k not in open_ids]:
                # the record closed some other way (by hand in TWS, by the position check): nothing may outlive it
                self._cancel_quietly(book.pop(tid).order_id)
        exiting, now, working, placed = self.pending_exit_trade_ids(), time.monotonic(), None, False
        for t in trades:
            tid, qty, price = t["id"], abs(float(t.get("quantity") or 0.0)), self._record_stop(t)
            if tid in exiting or qty <= 0 or not price:
                continue
            st = self._stops.get(tid)
            if st is None:
                if now < self._stop_retry.get(tid, 0.0) or self._broker_resyncing():
                    continue                             # just (re)connected: its order list is still loading
                if working is None:
                    working = self._orders_for_certain()
                if working is None:
                    return                               # the broker couldn't be asked: never place blind
                self._place_stop(t, qty, price, working)
                placed = True
                continue
            if self._pair_is_wrong(t, now):
                self._target_retry[tid] = now + self.TARGET_RETRY_S     # one try per while, whatever comes of it
                placed = self._rebuild(t) or placed
                continue
            if abs(st.qty - qty) > 1e-9 or (abs(st.price - price) >= tick(price) and now - st.moved_at >= self.STOP_MOVE_S):
                self._move_stop(st, qty, price)
        if now - self._swept_at >= self.SWEEP_S:
            self._swept_at = now
            # a list read before this pass placed or cancelled anything is stale: read the broker's orders again
            self._sweep_stops(open_ids, working if working is not None and not placed else self._working_at_broker())

    def _pair_is_wrong(self, t: Dict[str, Any], now: float) -> bool:
        """Whether what rests for a trade has the wrong *shape* - something a price move can't fix:
        no target where one belongs, or a target for other shares or another price."""
        tid = t["id"]
        tg = self._targets.get(tid)
        plan = self._target_plan(t)
        if plan is None:
            return tg is not None                        # a target rests that the record no longer has
        if tg is None:
            return now >= self._target_retry.get(tid, 0.0) and not self._broker_resyncing()
        return abs(tg.qty - plan[0]) > 1e-9 or abs(tg.price - plan[1]) >= tick(plan[1])

    def _target_plan(self, t: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        """The shares and the price of the target that should rest for a trade: the part that
        comes off at the first target when it scales out, all of it otherwise. None when no target
        rests for it - none on the record, targets off, or a broker that refused the pair."""
        if not self.native_targets_on() or t["id"] in self._plain or not t.get("target_price"):
            return None
        qty = abs(float(t.get("quantity") or 0.0))
        if qty <= 0:
            return None
        part = scale_out_plan(t, self.exit_cfg)
        return (part[0] if part else qty), on_tick(float(t["target_price"]))

    def _orders_for_certain(self) -> Optional[List[OrderResult]]:
        """The broker's working orders, or None when it couldn't be asked - an empty answer must mean
        "nothing is resting", or a second order gets placed beside one that is."""
        try:
            return [o for o in self.broker.list_orders("WORKING") if o.status not in DONE_STATUSES]
        except Exception:  # noqa: BLE001
            log.debug("could not list the broker's orders before placing a stop", exc_info=True)
            return None

    @staticmethod
    def _record_stop(t: Dict[str, Any]) -> float:
        price = t.get("stop_price") or t.get("initial_stop_price")
        return on_tick(float(price)) if price else 0.0

    # ------------------------------------------------------------------ #
    #  Placing, moving, losing                                           #
    # ------------------------------------------------------------------ #
    def _place_stop(self, t: Dict[str, Any], qty: float, price: float, working: List[OrderResult]) -> None:
        """Rest the stop for a trade - and, in one one-cancels-all group with it, its target."""
        tid, symbol = t["id"], t["symbol"]
        left = [o for o in working if o.tag == stop_tag(tid)]
        if left:                                         # an earlier run's orders: follow them, never double them
            keep = left[0]
            targets = [o for o in working if o.tag == target_tag(tid)]
            self._stops[tid] = _Stop(keep.order_id, tid, symbol, float(keep.submitted_qty or qty),
                                     float(keep.stop_price or price), group="adopted" if targets else "")
            if targets:
                first = targets[0]
                self._targets[tid] = _Stop(first.order_id, tid, symbol, float(first.submitted_qty or 0.0),
                                           float(first.limit_price or 0.0), group="adopted")
            kept = {keep.order_id} | ({targets[0].order_id} if targets else set())
            for extra in left[1:] + targets[1:]:
                if extra.order_id not in kept:           # the same order listed twice is not a second order
                    self._cancel_quietly(extra.order_id)
            log.info("following the order(s) already resting at the broker for %s: stop %s%s", tid, keep.order_id,
                     f", target {targets[0].order_id}" if targets else "")
            return
        held = self._held_quantity(symbol)
        long = t["side"] == "LONG"
        if held is not None and ((held > 0) != long or abs(held) < qty - 1e-9):
            # never rest an order the account can't cover - triggered, it would open a position the other way
            self._note_once(tid, f"{symbol}: no stop placed - the broker shows {held:,.0f} shares, the record {qty:,.0f}")
            arriving = (held > 0) == long and abs(held) > 0            # a fill still landing in pieces: look again soon
            self._stop_retry[tid] = time.monotonic() + (self.SHARES_RETRY_S if arriving else self.STOP_RETRY_S)
            return
        plan = self._target_plan(t)
        self._group_seq += 1                             # a group's name is never used twice: a finished one can't take orders
        group = f"oca:{tid}:{int(time.time())}:{self._group_seq}" if plan else ""
        exit_side = Side.SHORT if long else Side.LONG
        req = OrderRequest(symbol=symbol, side=exit_side, quantity=qty, order_type=OrderType.STOP, stop_price=price,
                           tif=TimeInForce.GTC, is_entry=False, client_tag=stop_tag(tid), oca_group=group,
                           oca_type=OCA_REDUCE if group else 0)
        try:
            res = self.broker.place_order(req)
        except BrokerError as e:
            self._audit("PLACE", req, {"error": str(e)}, ok=False, trade_id=tid, msg=str(e))
            self._stop_retry[tid] = time.monotonic() + self.STOP_RETRY_S
            if group:
                self._refused_pair(tid, symbol, str(e))
            else:
                self._note_once(tid, f"{symbol}: the broker refused the protective stop: {e}")
            return
        self._audit("PLACE", req, res.raw or {"status": res.status}, ok=True, trade_id=tid, msg="protective stop")
        self._stops[tid] = _Stop(res.order_id, tid, symbol, qty, price, moved_at=time.monotonic(), group=group)
        self._stop_notes.pop(tid, None)
        log.info("STOP AT BROKER  %s %s x%s @ %.2f (order %s)", symbol, "sell" if long else "buy", qty, price, res.order_id)
        self.bus.publish("stop.placed", trade_id=tid, symbol=symbol, qty=qty, stop_price=price, order_id=res.order_id)
        if plan:
            self._place_target(tid, symbol, exit_side, min(plan[0], qty), plan[1], group)

    def _place_target(self, tid: str, symbol: str, exit_side: Side, qty: float, price: float, group: str) -> None:
        req = OrderRequest(symbol=symbol, side=exit_side, quantity=qty, order_type=OrderType.LIMIT, limit_price=price,
                           tif=TimeInForce.GTC, is_entry=False, client_tag=target_tag(tid), oca_group=group,
                           oca_type=OCA_REDUCE)
        try:
            res = self.broker.place_order(req)
        except BrokerError as e:
            self._audit("PLACE", req, {"error": str(e)}, ok=False, trade_id=tid, msg=str(e))
            self._target_retry[tid] = time.monotonic() + self.TARGET_RETRY_S
            log.warning("TARGET AT BROKER refused for %s: %s - the app works the target itself", symbol, e)
            return
        self._audit("PLACE", req, res.raw or {"status": res.status}, ok=True, trade_id=tid, msg="resting target")
        self._targets[tid] = _Stop(res.order_id, tid, symbol, qty, price, moved_at=time.monotonic(), group=group)
        log.info("TARGET AT BROKER  %s %s x%s @ %.2f (order %s, one-cancels-all with the stop)", symbol,
                 "sell" if exit_side is Side.SHORT else "buy", qty, price, res.order_id)
        self.bus.publish("target.placed", trade_id=tid, symbol=symbol, qty=qty, limit_price=price, order_id=res.order_id)

    def _rebuild(self, t: Dict[str, Any]) -> bool:
        """Stand down what rests for a trade and place the pair afresh, in a new group - an order
        can't change the group it is in. Returns whether anything was placed."""
        tid = t["id"]
        if self._stand_down(tid) in ("filled", "busy"):
            return False                                 # booked, or not confirmed yet: look again next pass
        fresh = self.repo.get_trade(tid)
        if not fresh or fresh.get("status") == "CLOSED":
            return False
        working = self._orders_for_certain()
        qty, price = abs(float(fresh.get("quantity") or 0.0)), self._record_stop(fresh)
        if working is None or qty <= 0 or not price:
            return False
        self._place_stop(fresh, qty, price, working)
        return True

    def _move_stop(self, st: _Stop, qty: float, price: float) -> bool:
        """Change the resting stop's shares and trigger. A broker that can't modify an order has
        it cancelled; the next pass places a fresh one."""
        try:
            self.broker.modify_stop(st.order_id, stop_price=price, quantity=qty)
        except Exception as e:  # noqa: BLE001 - no modify on this venue, or the broker refused
            log.warning("could not move the stop for %s (%s) - replacing it", st.trade_id, e)
            self._drop_resting(st.trade_id)
            self._stop_retry[st.trade_id] = time.monotonic() + 5.0      # let the cancel land before looking again
            return False
        moved = abs(st.price - price) >= tick(price)
        st.qty, st.price, st.moved_at = qty, price, time.monotonic()
        if moved:
            self.bus.publish("stop.moved", trade_id=st.trade_id, symbol=st.symbol, qty=qty, stop_price=price)
        return True

    def _drop_resting(self, trade_id: str) -> None:
        """Cancel and forget whatever rests for a trade."""
        for book in (self._targets, self._stops):
            o = book.pop(trade_id, None)
            if o is not None:
                self._cancel_quietly(o.order_id)

    def _lose(self, book: Dict[str, _Stop], o: _Stop, why: str) -> None:
        book.pop(o.trade_id, None)
        now = time.monotonic()
        if book is self._targets:
            # the app works the target itself meanwhile - nothing is unprotected, so this is no alarm
            self._target_retry[o.trade_id] = now + self.TARGET_RETRY_S
            log.info("the target order for %s is gone (%s)", o.symbol, why)
            return
        if o.group and o.group != "adopted" and now - o.moved_at < self.REFUSED_WITHIN_S:
            self._refused_pair(o.trade_id, o.symbol, why)
            self._stop_retry[o.trade_id] = now + self.SHARES_RETRY_S
            return
        self._stop_retry[o.trade_id] = now + self.STOP_RETRY_S
        log.warning("STOP AT BROKER LOST  %s (order %s): %s - it will be placed again", o.symbol, o.order_id, why)
        self.bus.publish("stop.lost", trade_id=o.trade_id, symbol=o.symbol, order_id=o.order_id, reason=why)

    def _refused_pair(self, tid: str, symbol: str, why: str) -> None:
        """The broker wouldn't take the one-cancels-all pair: from here this trade gets a stop on
        its own, and the app works its target itself, as before there were resting targets."""
        self._plain.add(tid)
        target = self._targets.pop(tid, None)
        if target is not None:
            self._cancel_quietly(target.order_id)
        self._note_once(tid, f"{symbol}: the broker refused the stop-and-target pair ({why}) - resting a stop alone")

    def _note_once(self, tid: str, note: str) -> None:
        if self._stop_notes.get(tid) != note:
            self._stop_notes[tid] = note
            log.warning("PROTECTIVE STOP  %s", note)
            self.bus.publish("stop.failed", trade_id=tid, reason=note)

    def _sweep_stops(self, open_ids: set, working: List[OrderResult]) -> None:
        """Cancel every resting order of the app's at the broker that has no open trade, or doubles one."""
        tracked = {o.order_id for book in (self._stops, self._targets) for o in book.values()}
        for o in working:
            prefix, book = (TAG, self._stops) if o.tag.startswith(TAG) else (TARGET_TAG, self._targets)
            if not o.tag.startswith(prefix) or o.order_id in tracked:
                continue
            tid = o.tag[len(prefix):]
            if tid not in open_ids or tid in book:
                log.warning("cancelling the resting order %s (%s): %s", o.order_id, o.symbol,
                            "its trade is no longer open" if tid not in open_ids else "the trade already has one")
                self._cancel_quietly(o.order_id)

    # ------------------------------------------------------------------ #
    #  Before the app's own exits                                        #
    # ------------------------------------------------------------------ #
    def _stand_down(self, trade_id: str) -> str:
        """Take a trade's resting orders out of the way of an exit the app is about to send.
        Returns "none" (nothing rested), "cancelled" (the way is clear), "filled" (one of them got
        there first and closed the position: send nothing) or "busy" (the broker hasn't confirmed:
        send nothing yet). A fill that left part of the position open is booked, and the way is
        cleared for the rest."""
        if trade_id not in self._stops and trade_id not in self._targets:
            return "none"
        deadline = time.monotonic() + self.STAND_DOWN_S
        asked: set = set()
        while True:
            waiting, just_asked = False, False
            for book, fill in ((self._targets, self._book_target_fill), (self._stops, self._book_stop_fill)):
                o = book.get(trade_id)
                if o is None:
                    continue
                try:
                    res = self.broker.get_order(o.order_id)
                except Exception:  # noqa: BLE001
                    return "busy"
                done = res.status in DONE_STATUSES or (res.status == "UNKNOWN" and not self._broker_resyncing())
                if res.status == "FILLED" or (done and float(res.filled_qty or 0.0) > 0):
                    fill(o, res)
                    book.pop(trade_id, None)
                    t = self.repo.get_trade(trade_id)
                    if not t or t.get("status") == "CLOSED":
                        self._drop_resting(trade_id)
                        return "filled"
                    continue
                if done:
                    book.pop(trade_id, None)
                    continue
                if o.order_id not in asked:
                    self._cancel_quietly(o.order_id)
                    asked.add(o.order_id)
                    just_asked = True
                waiting = True
            if not waiting:
                return "cancelled"
            if not just_asked and time.monotonic() >= deadline:
                return "busy"
            if self.STAND_DOWN_POLL_S > 0:
                time.sleep(self.STAND_DOWN_POLL_S)

    def _shrink_stop(self, trade_id: str, remaining: float) -> bool:
        """Before the app takes part of a position off itself: the stop covers only what will remain."""
        st = self._stops.get(trade_id)
        if st is None or remaining >= st.qty - 1e-9:
            return True
        return self._move_stop(st, remaining, st.price)

    # ------------------------------------------------------------------ #
    #  Booking what the broker filled                                    #
    # ------------------------------------------------------------------ #
    def _book_stop_fill(self, st: _Stop, res: OrderResult) -> None:
        self._stops.pop(st.trade_id, None)
        t = self.repo.get_trade(st.trade_id)
        if not t or t.get("status") == "CLOSED":
            return
        price = float(res.avg_fill_price or (res.fills[-1].price if res.fills else 0.0) or st.price)
        filled = float(res.filled_qty or st.qty)
        first = t.get("initial_stop_price")
        reason = "trailing-stop" if first and abs(float(first) - st.price) > 1e-6 else "stop"
        partial = filled < abs(float(t["quantity"])) - 1e-9
        log.warning("STOP AT BROKER FILLED  %s x%s @ %.4f (%s)", st.symbol, filled, price, reason)
        self._book_exit(st.symbol, st.trade_id, price, filled, reason, partial, {} if partial else None, st.price)
        if not partial:
            target = self._targets.pop(st.trade_id, None)       # the broker cancels it with the stop; make sure
            if target is not None:
                self._cancel_quietly(target.order_id)

    def _book_target_fill(self, tg: _Stop, res: OrderResult) -> None:
        """The broker filled the target: the scale-out when part of the position is left - the
        record gets the stop and target the rest now has, and the pair is placed afresh for it -
        or the whole exit."""
        self._targets.pop(tg.trade_id, None)
        t = self.repo.get_trade(tg.trade_id)
        if not t or t.get("status") == "CLOSED":
            return
        price = float(res.avg_fill_price or (res.fills[-1].price if res.fills else 0.0) or tg.price)
        filled = float(res.filled_qty or tg.qty)
        partial = filled < abs(float(t["quantity"])) - 1e-9
        log.warning("TARGET AT BROKER FILLED  %s x%s @ %.4f (%s)", tg.symbol, filled, price,
                    "part of the position" if partial else "the whole position")
        if partial:
            plan = scale_out_plan(t, getattr(self, "exit_cfg", None))
            self._book_exit(tg.symbol, tg.trade_id, price, filled, "target-1", True, plan[1] if plan else {}, tg.price)
            st = self._stops.get(tg.trade_id)
            if st is not None:
                st.qty = max(0.0, st.qty - filled)       # the broker's group has already shrunk the stop by this much
            self._target_retry.pop(tg.trade_id, None)    # the rest gets its own pair on the next pass
            return
        self._book_exit(tg.symbol, tg.trade_id, price, filled, "target", False, None, tg.price)
        st = self._stops.pop(tg.trade_id, None)          # the broker cancels it with the target; make sure
        if st is not None:
            self._cancel_quietly(st.order_id)
