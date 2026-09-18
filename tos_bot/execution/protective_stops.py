"""A stop order resting at the broker for every open position - protection that outlives the app.

The exit manager watches prices and sends its own exits, which is no protection at all while the
app, the computer or the connection is down, and a slow one on delayed quotes. On a venue that can
hold one (``supports_native_stop``), the executor keeps a good-till-cancelled STOP order at the
broker for each open trade, at the trade's working stop and for exactly the shares it holds.

A resting stop brings two dangers, and the rules here exist for them:

* **Two exits on one position.** Before the app sends any exit of its own it *stands the stop
  down*: cancels it and waits for the broker to confirm. If the stop filled first, that fill is
  booked and no second exit goes out. If the broker can't confirm in time, no exit goes out on
  this pass - the exit manager tries again in seconds. A partial exit first shrinks the stop to
  the shares that will remain.
* **A stop that outlives its position** would open a position the other way when it triggers. A
  stop is only placed while the broker shows the shares; every pass cancels a tracked stop whose
  trade record is no longer open; and a sweep cancels any ``stop:<trade id>`` order at the broker
  whose trade isn't open, or that duplicates another. Before placing, the broker's working orders
  are searched for a stop an earlier run left - it is adopted, never doubled.

The trade record is the single source of truth: each pass compares the record's shares and stop
with the order at the broker and moves the order to match (the break-even and trailing ratchets,
the scale-out), at most once every ``STOP_MOVE_S`` for a price change.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..brokers.base import DONE_STATUSES, BrokerError
from ..core.enums import OrderType, Side, TimeInForce
from ..core.models import OrderRequest, OrderResult

log = logging.getLogger(__name__)

TAG = "stop:"


@dataclass
class _Stop:
    order_id: str
    trade_id: str
    symbol: str
    qty: float
    price: float
    moved_at: float = 0.0
    unseen: int = 0


def stop_tag(trade_id: str) -> str:
    return f"{TAG}{trade_id}"


def tick(price: float) -> float:
    """US stocks quote in cents from a dollar up, and in hundredths of a cent below."""
    return 0.01 if price >= 1.0 else 0.0001


def on_tick(price: float) -> float:
    return round(float(price), 2 if price >= 1.0 else 4)


class ProtectiveStops:
    """Mixed into the Executor (it uses its broker, repo, bus, venue and booking)."""

    #: seconds between moves of one stop's price - a trailing stop would otherwise send an order a tick
    STOP_MOVE_S = 15.0
    #: seconds between sweeps of the broker's orders for stops without an open trade
    SWEEP_S = 60.0
    #: how long a stand-down waits for the broker to confirm the cancel, and how often it asks
    STAND_DOWN_S, STAND_DOWN_POLL_S = 3.0, 0.25
    #: seconds before a stop that couldn't be placed, or was lost, is tried again
    STOP_RETRY_S = 30.0
    #: ...and when the broker's share count is only catching up with a fill that arrived in pieces
    SHARES_RETRY_S = 5.0

    def _init_stops(self) -> None:
        self._stops: Dict[str, _Stop] = {}
        self._stop_retry: Dict[str, float] = {}
        self._stop_notes: Dict[str, str] = {}
        self._swept_at = 0.0

    def native_stops_on(self) -> bool:
        return bool(getattr(self.cfg, "native_stop", True)) and bool(getattr(self.broker, "supports_native_stop", False))

    def protective_stops(self) -> List[Dict[str, Any]]:
        """The stops resting at the broker, for the dashboard and the tests."""
        return [{"trade_id": s.trade_id, "symbol": s.symbol, "order_id": s.order_id, "qty": s.qty, "stop_price": s.price}
                for s in self._stops.values()]

    # ------------------------------------------------------------------ #
    #  Each sync pass                                                    #
    # ------------------------------------------------------------------ #
    def _watch_stops(self) -> None:
        """Book a stop the broker filled; notice one it cancelled, rejected or lost."""
        for tid, st in list(self._stops.items()):
            try:
                res = self.broker.get_order(st.order_id)
            except Exception:  # noqa: BLE001
                continue
            if res.status == "FILLED":
                self._book_stop_fill(st, res)
            elif res.status in DONE_STATUSES:
                if float(res.filled_qty or 0.0) > 0:
                    self._book_stop_fill(st, res)
                self._lose_stop(st, f"the broker {res.status.lower()} it: {res.message or 'no reason given'}")
            elif res.status == "UNKNOWN":
                if self._broker_resyncing():
                    continue
                st.unseen += 1
                if st.unseen >= self.LOST_AFTER_POLLS:
                    self._lose_stop(st, "the broker no longer knows it")
            else:
                st.unseen = 0

    def _protect_positions(self) -> None:
        """Make the broker's stops match the trade records: place, resize, move, cancel."""
        if not self.native_stops_on() or getattr(self.broker, "is_connected", True) is False:
            return
        trades = [t for t in self.repo.open_trades()
                  if (t.get("broker") or "paper") == self.venue and not t.get("pair_id")]
        open_ids = {t["id"] for t in trades}
        for tid in [k for k in self._stops if k not in open_ids]:
            # the record closed some other way (by hand in TWS, by the position check): the stop must not outlive it
            self._cancel_quietly(self._stops.pop(tid).order_id)
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
            elif abs(st.qty - qty) > 1e-9 or (abs(st.price - price) >= tick(price) and now - st.moved_at >= self.STOP_MOVE_S):
                self._move_stop(st, qty, price)
        if now - self._swept_at >= self.SWEEP_S:
            self._swept_at = now
            # a list read before this pass placed or cancelled anything is stale: read the broker's orders again
            self._sweep_stops(open_ids, working if working is not None and not placed else self._working_at_broker())

    def _orders_for_certain(self) -> Optional[List[OrderResult]]:
        """The broker's working orders, or None when it couldn't be asked - an empty answer must mean
        "no stop is resting", or a second stop gets placed beside one that is."""
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
        tid, symbol = t["id"], t["symbol"]
        tag = stop_tag(tid)
        left = [o for o in working if o.tag == tag]
        if left:                                         # an earlier run's stop: follow it, never double it
            keep = left[0]
            self._stops[tid] = _Stop(keep.order_id, tid, symbol, float(keep.submitted_qty or qty),
                                     float(keep.stop_price or price))
            for extra in left[1:]:
                self._cancel_quietly(extra.order_id)
            log.info("following the stop order %s already at the broker for %s", keep.order_id, tid)
            return
        held = self._held_quantity(symbol)
        long = t["side"] == "LONG"
        if held is not None and ((held > 0) != long or abs(held) < qty - 1e-9):
            # never rest a stop the account can't cover - triggered, it would open a position the other way
            self._note_once(tid, f"{symbol}: no stop placed - the broker shows {held:,.0f} shares, the record {qty:,.0f}")
            arriving = (held > 0) == long and abs(held) > 0            # a fill still landing in pieces: look again soon
            self._stop_retry[tid] = time.monotonic() + (self.SHARES_RETRY_S if arriving else self.STOP_RETRY_S)
            return
        req = OrderRequest(symbol=symbol, side=Side.SHORT if long else Side.LONG, quantity=qty,
                           order_type=OrderType.STOP, stop_price=price, tif=TimeInForce.GTC, is_entry=False,
                           client_tag=tag)
        try:
            res = self.broker.place_order(req)
        except BrokerError as e:
            self._audit("PLACE", req, {"error": str(e)}, ok=False, trade_id=tid, msg=str(e))
            self._stop_retry[tid] = time.monotonic() + self.STOP_RETRY_S
            self._note_once(tid, f"{symbol}: the broker refused the protective stop: {e}")
            return
        self._audit("PLACE", req, res.raw or {"status": res.status}, ok=True, trade_id=tid, msg="protective stop")
        self._stops[tid] = _Stop(res.order_id, tid, symbol, qty, price, moved_at=time.monotonic())
        self._stop_notes.pop(tid, None)
        log.info("STOP AT BROKER  %s %s x%s @ %.2f (order %s)", symbol, "sell" if long else "buy", qty, price, res.order_id)
        self.bus.publish("stop.placed", trade_id=tid, symbol=symbol, qty=qty, stop_price=price, order_id=res.order_id)

    def _move_stop(self, st: _Stop, qty: float, price: float) -> bool:
        """Change the resting order's shares and trigger. A broker that can't modify an order has
        it cancelled; the next pass places a fresh one."""
        try:
            self.broker.modify_stop(st.order_id, stop_price=price, quantity=qty)
        except Exception as e:  # noqa: BLE001 - no modify on this venue, or the broker refused
            log.warning("could not move the stop for %s (%s) - replacing it", st.trade_id, e)
            self._cancel_quietly(st.order_id)
            self._stops.pop(st.trade_id, None)
            self._stop_retry[st.trade_id] = time.monotonic() + 5.0      # let the cancel land before looking again
            return False
        moved = abs(st.price - price) >= tick(price)
        st.qty, st.price, st.moved_at = qty, price, time.monotonic()
        if moved:
            self.bus.publish("stop.moved", trade_id=st.trade_id, symbol=st.symbol, qty=qty, stop_price=price)
        return True

    def _lose_stop(self, st: _Stop, why: str) -> None:
        self._stops.pop(st.trade_id, None)
        self._stop_retry[st.trade_id] = time.monotonic() + self.STOP_RETRY_S
        log.warning("STOP AT BROKER LOST  %s (order %s): %s - it will be placed again", st.symbol, st.order_id, why)
        self.bus.publish("stop.lost", trade_id=st.trade_id, symbol=st.symbol, order_id=st.order_id, reason=why)

    def _note_once(self, tid: str, note: str) -> None:
        if self._stop_notes.get(tid) != note:
            self._stop_notes[tid] = note
            log.warning("PROTECTIVE STOP  %s", note)
            self.bus.publish("stop.failed", trade_id=tid, reason=note)

    def _sweep_stops(self, open_ids: set, working: List[OrderResult]) -> None:
        """Cancel every stop order of the app's at the broker that has no open trade, or doubles one."""
        tracked = {s.order_id for s in self._stops.values()}
        for o in working:
            if not o.tag.startswith(TAG) or o.order_id in tracked:
                continue
            tid = o.tag[len(TAG):]
            if tid not in open_ids or tid in self._stops:
                log.warning("cancelling the stop order %s (%s): %s", o.order_id, o.symbol,
                            "its trade is no longer open" if tid not in open_ids else "the trade already has a stop")
                self._cancel_quietly(o.order_id)

    # ------------------------------------------------------------------ #
    #  Before the app's own exits                                        #
    # ------------------------------------------------------------------ #
    def _stand_down(self, trade_id: str) -> str:
        """Take a trade's stop out of the way of an exit the app is about to send. Returns "none"
        (there was no stop), "cancelled" (the way is clear), "filled" (the stop got there first and
        is booked: send nothing) or "busy" (the broker hasn't confirmed: send nothing yet)."""
        st = self._stops.get(trade_id)
        if st is None:
            return "none"
        try:
            res = self.broker.get_order(st.order_id)
        except Exception:  # noqa: BLE001
            return "busy"
        deadline = time.monotonic() + self.STAND_DOWN_S
        asked = False
        while True:
            if res.status == "FILLED":
                self._book_stop_fill(st, res)
                return "filled"
            if res.status in DONE_STATUSES or (res.status == "UNKNOWN" and not self._broker_resyncing()):
                if float(res.filled_qty or 0.0) > 0:
                    self._book_stop_fill(st, res)             # part of it filled before the cancel landed
                self._stops.pop(trade_id, None)
                return "cancelled"
            if not asked:
                self._cancel_quietly(st.order_id)
                asked = True
            elif time.monotonic() >= deadline:
                return "busy"
            if self.STAND_DOWN_POLL_S > 0:
                time.sleep(self.STAND_DOWN_POLL_S)
            try:
                res = self.broker.get_order(st.order_id)
            except Exception:  # noqa: BLE001
                return "busy"

    def _shrink_stop(self, trade_id: str, remaining: float) -> bool:
        """Before part of a position is taken off: the stop covers only what will remain."""
        st = self._stops.get(trade_id)
        if st is None or remaining >= st.qty - 1e-9:
            return True
        return self._move_stop(st, remaining, st.price)

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
