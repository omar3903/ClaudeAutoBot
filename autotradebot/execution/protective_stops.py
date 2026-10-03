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
  time, no exit goes out on this pass - the exit manager tries again in seconds. A cancel the broker
  refused (IBKR's 10148: the stop is already filling) is no confirmation, nor is an order the app
  asked to cancel that reads cancelled without the broker's word on it (waited for, pass after pass,
  for ``CANCEL_UNCONFIRMED_S``). While an exit stands a trade's orders down - and while a cancel it
  asked for awaits the broker's word - the order sync leaves them alone; and the sync holds a trade's
  orders itself while it places, moves or books one. (Both take their turn under the executor's
  lock, and a resting order that is done is taken out of its book before its fill is booked, so it
  is booked once.) An exit in the minute after a start, before the first
  pass has taken over what an earlier run left, takes that run's orders for the trade over and stands
  them down the same way; while the broker's orders can't be read (the connection down too), or are
  still reloading after a connect with none found, the exit waits - until a full list has shown
  nothing of an earlier run's for the trade (or this run opened it). A close that waited for a trade's
  orders while another caller had them reads the record and the exits working again first. While a
  target rests at the broker the exit manager leaves the target to it, and a part it would take off
  beside one is never sent.
* **An order that outlives its position** would open a position the other way when it triggers.
  Orders are only placed while the broker shows the shares and its order list could be read for
  certain; every pass cancels a tracked order whose trade record is no longer open; and a sweep
  cancels any ``stop:<trade id>`` / ``tgt:<trade id>`` order at the broker whose trade isn't open,
  or that duplicates another. Before placing, the broker's working orders are searched for orders
  an earlier run left - they are adopted, never doubled. One that filled while the app was off has
  what it filled booked first, and is replaced by a fresh pair for what the record then holds. One
  the broker no longer knows at all is looked for in its executions before it is given up, or stood
  down for an exit: what it filled (the connection down as it finished, say) is booked like a fill
  heard live. A fill the database refuses to save (BookingFailed: busy, say) puts the order back in
  its book: the next pass books it, and until then nothing else is done for the trade - no exit of
  the app's own, no move, no fresh pair.

The trade record is the single source of truth: each pass compares the record's shares, stop and
target with the orders at the broker. The stop's price is moved in place (the break-even and
trailing ratchets), at most once every ``STOP_MOVE_S``, leaving its size as the broker holds it: a
target that filled in part has already taken its shares off the stop. A resize (a scale-out booked)
asks for the record's shares less what the target has filled, and grows the stop past what the broker
holds only with no target resting and for shares the account shows. Anything structural - no target resting
where one belongs, a target for the wrong shares or price, the scale-out just taken - has the pair
stood down and placed afresh in a new group, because an order can't change its group (only once the
broker's orders have been read: never a stop cancelled that can't be replaced). A broker that refuses
the pair gets a plain stop, and the app works the target itself as before. A move the broker can't
make until it says what became of the stop (OrderInDoubt) leaves the stop resting where it is. A stop,
target or move the broker doesn't answer in time is no refusal either: the order may rest all the same,
so it is looked for among the broker's orders by its tag before another is placed, and taken over if
it is there.
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..brokers.base import OUTCOME_UNKNOWN, DONE_STATUSES, BrokerError, OrderInDoubt, OrderNotSent
from ..core.enums import OrderType, Side, TimeInForce
from ..core.models import OrderRequest, OrderResult
from .exit_manager import scale_out_plan

log = logging.getLogger(__name__)

TAG = "stop:"
TARGET_TAG = "tgt:"
OCA_REDUCE = 3                     # IBKR: the others are reduced by what fills, no routing block


class BookingFailed(Exception):
    """The database refused to save a fill the broker reported (busy with another writer, say). Raised by the
    executor's bookings (_open_trade, _book_exit) once they have logged it and told the dashboard: whoever followed
    the order keeps following it - or puts it back where it was followed - so the next pass books the fill again,
    and it is never lost."""


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
    unbooked: bool = False         # done, and its fill couldn't be saved: back in its book until it is (_book_resting)


def stop_tag(trade_id: str) -> str:
    return f"{TAG}{trade_id}"


def target_tag(trade_id: str) -> str:
    return f"{TARGET_TAG}{trade_id}"


def tick(price: float) -> float:
    """US stocks quote in cents from a dollar up, and in hundredths of a cent below."""
    return 0.01 if price >= 1.0 else 0.0001


def on_tick(price: float) -> float:
    return round(float(price), 2 if price >= 1.0 else 4)


def _unconfirmed_cancel(res: OrderResult) -> bool:
    """The order reads cancelled, but the broker never said it cancelled it (IBKR's 202, or its own order
    status) - an error read as the order's end. Brokers that don't say count as having confirmed."""
    return res.status == "CANCELED" and (res.raw or {}).get("cancel_confirmed") is False


def _takes_strict(get_fills) -> bool:
    """Whether a broker's ``get_fills`` takes ``strict`` - raise on a read that failed, rather than answer []."""
    try:
        return "strict" in inspect.signature(get_fills).parameters
    except (TypeError, ValueError):
        return False


def shares_and_price(fills: List[Any]) -> Tuple[float, float]:
    """The shares the executions in ``fills`` add up to, and their average price; (0, 0) for none."""
    qty = sum(float(f.quantity) for f in fills)
    return (qty, sum(float(f.price) * float(f.quantity) for f in fills) / qty) if qty > 1e-9 else (0.0, 0.0)


def stop_exit_reason(initial_stop: Optional[float], trigger: float) -> str:
    """A stop that filled is booked "stop" if it was still where the trade began, "trailing-stop" once
    it had been moved. The order rests at the initial stop rounded to the tick, so the two are compared
    on the tick: a difference of less than half a tick is the rounding, not a move."""
    if not initial_stop:
        return "stop"
    moved = abs(on_tick(float(initial_stop)) - float(trigger)) >= tick(float(trigger)) / 2
    return "trailing-stop" if moved else "stop"


class ProtectiveStops:
    """Mixed into the Executor (it uses its broker, repo, bus, venue and booking)."""

    #: seconds between moves of one stop's price - a trailing stop would otherwise send an order a tick
    STOP_MOVE_S = 15.0
    #: seconds between sweeps of the broker's orders for resting orders without an open trade
    SWEEP_S = 60.0
    #: how long a stand-down waits for the broker to confirm the cancel, and how often it asks
    STAND_DOWN_S, STAND_DOWN_POLL_S = 3.0, 0.25
    #: seconds an order the app asked to cancel, that reads cancelled without the broker's word on it, is still
    #: waited for like one working - a fill racing the cancel is heard well within it
    CANCEL_UNCONFIRMED_S = 30.0
    #: seconds before a stop that couldn't be placed, or was lost, is tried again
    STOP_RETRY_S = 30.0
    #: a position with no stop at the broker for this long is reported, and again every UNPROTECTED_REPEAT_S
    UNPROTECTED_WARN_S = 90.0
    UNPROTECTED_REPEAT_S = 300.0
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
        self._bare_since: Dict[str, float] = {}      # when each position was first seen with no stop at the broker
        self._bare_warned: Dict[str, float] = {}
        self._plain: set = set()          # trades whose one-cancels-all pair the broker refused: a stop alone
        #: trades whose target got no answer in time (OrderOutcomeUnknown) -> the group it was sent in: looked for at
        #: the broker before the pair is placed afresh (_adopt_unknown_target)
        self._targets_unknown: Dict[str, str] = {}
        self._group_seq = getattr(self, "_group_seq", 0)
        self._swept_at = 0.0
        self._cancels_sent: Dict[str, float] = {}   # order id -> when a stand-down first asked the broker to cancel it
        #: trades whose resting orders an exit (or a rebuild) is standing down right now - the order sync leaves
        #: them alone meanwhile; a manual close or a quit can run beside it on another thread (_claim_resting)
        self._standing_down: set = set()
        self._claims_lock = getattr(self, "_claims_lock", None) or threading.Lock()
        #: trades whose orders an earlier run may have left have been looked for in the broker's full order list (past
        #: the re-sync after a connect), or that this run opened: an exit for one no longer waits on a list that can't
        #: be read - nothing this run doesn't follow can rest for it (_take_over_for_exit)
        self._left_looked: set = set()

    def native_stops_on(self) -> bool:
        return bool(getattr(self.cfg, "native_stop", True)) and bool(getattr(self.broker, "supports_native_stop", False))

    def native_targets_on(self) -> bool:
        """A target rests at the broker only beside a stop, and only once the engine has said how
        positions scale out (``exit_cfg``) - the shares a target covers depend on it."""
        return (self.native_stops_on() and bool(getattr(self.cfg, "native_target", True))
                and getattr(self, "exit_cfg", None) is not None)

    def protective_stops(self) -> List[Dict[str, Any]]:
        """The stops resting at the broker, for the dashboard and the tests. (Read from a copy: the dashboard
        asks from its own thread while a sync pass may be placing or dropping one.)"""
        return [{"trade_id": s.trade_id, "symbol": s.symbol, "order_id": s.order_id, "qty": s.qty, "stop_price": s.price}
                for s in list(self._stops.values())]

    def stops_protecting(self, trades: List[Dict[str, Any]]) -> set:
        """The ids of ``trades`` that a stop order resting at the broker protects: one this run follows, or - in
        the minute after a start, before the first pass has taken them over - one an earlier run left, tagged
        for the trade and still working for all its shares. The broker is asked only when a trade isn't
        followed yet; when it can't be asked, only the followed ones count."""
        ids = {t["id"] for t in trades}
        protected = {s["trade_id"] for s in self.protective_stops()} & ids
        rest = [t for t in trades if t["id"] not in protected]
        if not rest:
            return protected
        working = self._orders_for_certain() or []
        for t in rest:
            need = abs(float(t.get("quantity") or 0.0))
            closing = Side.SHORT if t.get("side") == "LONG" else Side.LONG
            if need > 0 and any(o.tag == stop_tag(t["id"]) and (o.side is None or o.side is closing)
                                and float(o.submitted_qty or 0.0) - float(o.filled_qty or 0.0) >= need - 1e-9
                                for o in working):
                protected.add(t["id"])
        return protected

    def resting_targets(self) -> List[Dict[str, Any]]:
        return [{"trade_id": s.trade_id, "symbol": s.symbol, "order_id": s.order_id, "qty": s.qty, "limit_price": s.price}
                for s in list(self._targets.values())]

    def target_resting(self, trade_id: str) -> bool:
        """Whether the broker is working this trade's target - the exit manager then leaves it to it."""
        return trade_id in self._targets

    def fill_unbooked(self, trade_id: str) -> bool:
        """Whether this trade's stop or target filled and the database refused the booking: it is followed again until
        the next pass books it, and meanwhile nothing else is done for the trade - no exit, no move, no fresh pair."""
        return any(o is not None and o.unbooked for o in (self._stops.get(trade_id), self._targets.get(trade_id)))

    def resting_filled(self, trade_id: str) -> Optional[float]:
        """Shares the stop and target resting for a trade have filled while still working. They are booked when
        the order finishes (_watch_stops), so nothing else may book them first. None when the broker can't say."""
        filled = 0.0
        for book in (self._stops, self._targets):
            o = book.get(trade_id)
            if o is None:
                continue
            try:
                filled += float(self.broker.get_order(o.order_id).filled_qty or 0.0)
            except Exception:  # noqa: BLE001
                return None
        return filled

    # ------------------------------------------------------------------ #
    #  Each sync pass                                                    #
    # ------------------------------------------------------------------ #
    def _watch_stops(self) -> None:
        """Book what the broker filled; notice what it cancelled, rejected or lost. Targets first:
        a target that closed the position takes the stop with it, which is no stop lost."""
        for book, fill in ((self._targets, self._book_target_fill), (self._stops, self._book_stop_fill)):
            for tid, o in list(book.items()):
                if book.get(tid) is not o or not self._claim_resting(tid):
                    continue                             # booked or dropped meanwhile, or an exit is standing it down
                try:
                    self._watch_one(book, fill, o)
                except BookingFailed:
                    pass                                 # back in its book: its fill is booked again on the next pass
                finally:
                    self._release_resting(tid)

    def _watch_one(self, book: Dict[str, _Stop], fill, o: _Stop) -> None:
        """One resting order's pass, while the sync holds its trade's orders: an exit on another thread neither
        books what it filled a second time nor stands it down meanwhile."""
        if book.get(o.trade_id) is not o:
            return                                       # an exit booked or dropped it just before the claim
        try:
            res = self.broker.get_order(o.order_id)
        except Exception:  # noqa: BLE001
            return
        if res.status == "FILLED":
            if self._take_resting(book, o):              # its fill is booked once, by whoever takes it out of its book
                fill(o, res)
        elif _unconfirmed_cancel(res) and self._cancel_unconfirmed(o.order_id):
            # an exit asked for its cancel and the broker never said it went through: the stand-down settles it,
            # pass after pass, and until then it is followed like one working - it may yet fill
            return
        elif res.status in DONE_STATUSES:
            if not self._take_resting(book, o):
                return                                   # another caller has taken it, to book or drop it
            if float(res.filled_qty or 0.0) > 0:
                fill(o, res)
            self._lose(book, o, f"the broker {res.status.lower()} it: {res.message or 'no reason given'}")
        elif res.status == "UNKNOWN":
            if self._broker_resyncing():
                return
            o.unseen += 1
            if o.unseen >= self.LOST_AFTER_POLLS and not self._filled_unseen(book, fill, o):
                self._lose(book, o, "the broker no longer knows it")
        else:
            o.unseen = 0
            if book is self._stops:
                self._in_step(o, res)

    @staticmethod
    def _in_step(st: _Stop, res: OrderResult) -> None:
        """Bring a followed stop's trigger and shares in step with what the broker holds. A move the broker refused
        after the app took it for done (IBKR's late word on a stop it holds until its trigger) leaves the order as
        it was: the stop reads where it really rests, and the next pass sends the move again."""
        held_price, held_qty = float(res.stop_price or 0.0), float(res.submitted_qty or 0.0)
        if held_price > 0 and abs(held_price - st.price) >= tick(held_price) / 2:
            st.price = held_price
        if held_qty > 0:
            st.qty = held_qty

    def _filled_unseen(self, book: Dict[str, _Stop], fill, o: _Stop) -> Optional[bool]:
        """Before a resting order the broker no longer knows is given up: whether the broker's executions show it
        filled while the app wasn't following it (the connection was down when it finished, say). What they show
        is booked as the order's own fill would have been - one still in its book is taken out of it first, so it is
        booked once (_take_resting). Only the executions of this order count - every stop a trade has had carries the
        same tag. False when they show none (or the record is closed already), None when they can't be read."""
        t = self.repo.get_trade(o.trade_id)
        if not t or t.get("status") == "CLOSED":
            return False
        executions = self._executions(o.symbol)
        if executions is None:
            return None
        tag = stop_tag(o.trade_id) if book is self._stops else target_tag(o.trade_id)
        exit_side = Side.SHORT if t["side"] == "LONG" else Side.LONG
        qty, price = shares_and_price([f for f in executions
                                       if str(f.order_id) == str(o.order_id) and f.side is exit_side
                                       and (getattr(f, "tag", "") or tag) == tag])
        if qty <= 1e-9:
            return False
        if book.get(o.trade_id) is o and not self._take_resting(book, o):
            return True                                  # another caller took it out of its book just now, to book it
        log.warning("%s AT BROKER FILLED UNSEEN  %s order %s is no longer known to the broker, but its executions "
                    "show %s shares @ %.4f - booked", "STOP" if book is self._stops else "TARGET", o.symbol,
                    o.order_id, qty, price)
        fill(o, OrderResult(order_id=o.order_id, status="FILLED", symbol=o.symbol, submitted_qty=o.qty,
                            filled_qty=qty, avg_fill_price=price))
        return True

    def _executions(self, symbol: Optional[str]) -> Optional[List[Any]]:
        """The broker's executions this session - of ``symbol``, or the whole account's - oldest first; None when
        the read failed. A broker that can tell a read that failed from one that found nothing (IBKR answers [] for
        both unless asked ``strict``) is asked to: here "none" must mean none. One that keeps no executions at all
        answers [], as the base adapter's does - it will never say more."""
        get = getattr(self.broker, "get_fills", None)
        if not callable(get):
            return []
        try:
            return list((get(symbol, strict=True) if _takes_strict(get) else get(symbol)) or [])
        except Exception:  # noqa: BLE001
            log.debug("executions for %s unavailable", symbol or "the account", exc_info=True)
            return None

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
        # a trade whose exit is working, or whose orders an exit is standing down this moment - or asked the broker to
        # cancel on an earlier try, with its word on that still to come - is the exit's
        exiting = self.pending_exit_trade_ids() | self._claimed()
        now, working, placed, unreadable = time.monotonic(), None, False, False
        for t in trades:
            tid, qty, price = t["id"], abs(float(t.get("quantity") or 0.0)), self._record_stop(t)
            if tid in exiting or qty <= 0 or not price or self._cancel_asked(tid) or self.fill_unbooked(tid):
                continue                                 # (an order that filled is never moved or replaced unbooked)
            if tid in self._stops and self._pair_is_wrong(t, now):
                # a pair is stood down only once the broker's orders have been read for its fresh one: never a stop
                # cancelled that can't be placed afresh (the list is read again next pass)
                if working is None and not unreadable:
                    working = self._orders_for_certain()
                    unreadable = working is None
                if unreadable:
                    continue
                if self._adopt_unknown_target(t, working):
                    continue                             # the target that got no answer rests after all: followed
                self._target_retry[tid] = now + self.TARGET_RETRY_S     # one try per while, whatever comes of it
                placed = self._rebuild(t, working) or placed             # (it holds the trade's orders itself)
                continue
            # the pass holds the trade's orders while it places or moves them: a manual close or a quit on another
            # thread waits meanwhile - and one that came and went since the pass began is seen here
            if not self._claim_resting(tid):
                continue
            try:
                if tid in self.pending_exit_trade_ids():
                    continue
                st = self._stops.get(tid)
                if st is None:
                    if unreadable or now < self._stop_retry.get(tid, 0.0) or self._broker_resyncing():
                        continue                         # just (re)connected: its order list is still loading
                    if working is None:
                        working = self._orders_for_certain()
                    if working is None:
                        unreadable = True                # the broker couldn't be asked: never place blind
                        continue
                    fresh = self.repo.get_trade(tid)
                    if not fresh or fresh.get("status") == "CLOSED":
                        continue                         # an exit that filled at once, since the pass began
                    self._place_stop(t, qty, price, working)
                    placed = True
                    continue
                if abs(st.qty - qty) > 1e-9:
                    # the record's shares changed (a scale-out booked) - or the target's group took some off at the broker
                    size = self._stop_size(st, qty, t["side"])
                    if size is not None and abs(size - st.qty) > 1e-9:
                        self._move_stop(st, price, size)
                        continue
                if abs(st.price - price) >= tick(price) and now - st.moved_at >= self.STOP_MOVE_S:
                    self._move_stop(st, price)           # the price alone: the size stays as the broker holds it
            finally:
                self._release_resting(tid)
        self._watch_unprotected(trades, exiting, now, unreadable)
        if unreadable:
            return                                       # the sweep reads the list again next pass
        if now - self._swept_at >= self.SWEEP_S:
            self._swept_at = now
            # a list read before this pass placed or cancelled anything is stale: read the broker's orders again
            self._sweep_stops(open_ids, working if working is not None and not placed else self._working_at_broker())

    def _watch_unprotected(self, trades: List[Dict[str, Any]], exiting: set, now: float,
                           unreadable: bool = False) -> None:
        """Say so - in the log and on the dashboard, and again every few minutes - when a position has
        had no stop at the broker for a while. It places nothing: the reason is always one where an
        order would be unsafe or impossible (the broker shows no shares, or fewer than the record; its
        orders couldn't be read - ``unreadable``, this pass; it refused the stop), and while the app runs its
        exit manager still watches the price and sends the exit itself. What it can't do is protect it while
        the app is off - nor, while the orders can't be read, send the exit of a trade never looked for in a
        full list (_take_over_for_exit holds it back: an earlier run's stop may rest), which it says too."""
        bare = set()
        for t in trades:
            tid = t["id"]
            if tid in exiting or tid in self._stops or not abs(float(t.get("quantity") or 0.0)) or not self._record_stop(t):
                continue
            bare.add(tid)
            since = self._bare_since.setdefault(tid, now)
            if (self._broker_resyncing() or now - since < self.UNPROTECTED_WARN_S
                    or now - self._bare_warned.get(tid, float("-inf")) < self.UNPROTECTED_REPEAT_S):
                continue
            self._bare_warned[tid] = now
            minutes = (now - since) / 60.0
            reason = self._stop_notes.get(tid) or (
                f"{t['symbol']}: the broker's working orders couldn't be read, so none could be placed" if unreadable
                else f"{t['symbol']}: the broker hasn't taken one yet")
            held = unreadable and tid not in self._left_looked
            log.warning("NO STOP AT THE BROKER  %s has had none for %.1f min - %s. %s", t["symbol"], minutes, reason,
                        "Its exit waits too, until the broker's orders can be read - an earlier run's stop may rest "
                        "there" if held else "The app still watches the price and exits it itself while it runs")
            self.bus.publish("stop.missing", trade_id=tid, symbol=t["symbol"], minutes=round(minutes, 1), reason=reason,
                             exit_held=held)
        for book in (self._bare_since, self._bare_warned):
            for tid in [k for k in book if k not in bare]:
                del book[tid]

    def unprotected(self) -> List[str]:
        """The open trades with no stop at the broker right now (after the first pass has looked)."""
        return sorted(self._bare_since)

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
        "nothing is resting", or a second order gets placed beside one that is. A broker that isn't
        connected isn't asked: it can say nothing of its orders then."""
        if getattr(self.broker, "is_connected", True) is False:
            return None
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
        if self._follow_left(t, working, qty, price):    # an earlier run's orders: follow them, never double them
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
        except (OrderNotSent,) + OUTCOME_UNKNOWN as e:
            # no refusal: it never went out, or may rest at the broker all the same - the next try, in a few seconds,
            # first looks for it among the broker's orders by its tag and takes it over (_follow_left), never doubles it
            self._audit("PLACE", req, {"error": str(e), "outcome": "not sent" if isinstance(e, OrderNotSent)
                                       else "unknown"}, ok=False, trade_id=tid, msg=str(e))
            self._stop_retry[tid] = time.monotonic() + self.SHARES_RETRY_S
            log.warning("PROTECTIVE STOP  %s: the stop got no answer from the broker in time (%s) - looked for there "
                        "before another is placed", symbol, e)
            return
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

    def _follow_left(self, t: Dict[str, Any], working: List[OrderResult], qty: float, price: float,
                     target_alone: bool = False) -> str:
        """Take over the stop - and the target - an earlier run left resting at the broker for a trade. Returns
        "followed" (they are this run's now), "booked" (one had filled while the app was off: what it filled is
        booked, and the pair is cancelled, to be placed afresh once the cancels have landed) or "" (nothing was
        left). With ``target_alone`` a target left without its stop is taken over too - for an exit about to go
        out, which must stand it down; a pass that places stops leaves it to the sweep once the fresh pair rests."""
        tid, symbol = t["id"], t["symbol"]
        left = [o for o in working if o.tag == stop_tag(tid)]
        targets = [o for o in working if o.tag == target_tag(tid)]
        if not left and not (target_alone and targets):
            if not targets:
                self._looked_for_left(tid)               # nothing an earlier run left rests for it
            return ""
        keep = left[0] if left else None
        if self._book_filled_while_off(t, keep, targets[0] if targets else None):
            # one that has filled isn't followed - finishing, it would report the shares just booked again: the
            # pair is cancelled and, once the cancels have landed, placed afresh for what the record now holds
            self._left_looked.discard(tid)               # until the cancels show in a full list (see _drop_resting)
            for oid in {o.order_id for o in left + targets}:
                self._cancel_quietly(oid)
            self._stop_retry[tid] = time.monotonic() + self.SHARES_RETRY_S
            log.info("the order(s) resting at the broker for %s filled while the app was off: booked, and "
                     "replaced by a fresh pair for what the record holds", tid)
            return "booked"
        if keep is not None:
            self._stops[tid] = _Stop(keep.order_id, tid, symbol, float(keep.submitted_qty or qty),
                                     float(keep.stop_price or price), group="adopted" if targets else "")
        if targets:
            first = targets[0]
            self._targets[tid] = _Stop(first.order_id, tid, symbol, float(first.submitted_qty or 0.0),
                                       float(first.limit_price or 0.0), group="adopted")
        kept = ({keep.order_id} if keep is not None else set()) | ({targets[0].order_id} if targets else set())
        extras = [o for o in left[1:] + targets[1:] if o.order_id not in kept]   # one listed twice is no second one
        for extra in extras:
            self._cancel_quietly(extra.order_id)
        if not extras:
            self._looked_for_left(tid)                   # all an earlier run left for it is this run's now
        followed = ([f"stop {keep.order_id}"] if keep is not None else []) + \
            ([f"target {targets[0].order_id}"] if targets else [])
        log.info("following the order(s) already resting at the broker for %s: %s", tid, ", ".join(followed))
        return "followed"

    def _looked_for_left(self, trade_id: str) -> None:
        """Note that the broker's order list - a full one, past the re-sync after a connect - holds nothing for a trade
        that this run doesn't follow: no exit for it waits on a list that can't be read from here on."""
        if not self._broker_resyncing():
            self._left_looked.add(trade_id)

    def _take_over_for_exit(self, t: Dict[str, Any], working: Optional[List[OrderResult]]) -> str:
        """Before the app's own exit for a trade this run follows no resting order for - the minute after a start,
        before the first pass has taken over what an earlier run left: a ``stop:``/``tgt:`` order for it in the
        broker's ``working`` orders is taken over, so the stand-down cancels it (and books any fill) like one of
        this run's. Returns "" when the exit may go on, or why it must wait. It waits when one of them filled
        while the app was off (booked here; the rest is being cancelled), and - for a trade that would have a stop
        at the broker - when nothing was found but the order list couldn't be read (``working`` None) or the
        broker is still reloading it after a connect: never a market exit blind beside a stop that may rest. A list
        that can't be read no longer holds back the exit of a trade this run has looked for an earlier run's orders
        for already, or opened itself: whatever rests for it is this run's own (it goes out capped by the shares
        held, as before there were resting orders)."""
        tid = t["id"]
        if tid in self._stops or tid in self._targets:
            return ""                                    # this run's own: the stand-down sees to them
        if working is not None:
            qty, price = abs(float(t.get("quantity") or 0.0)), self._record_stop(t)
            taken = self._follow_left(t, working, qty, price, target_alone=True)
            if taken == "booked":
                return ("An order resting at the broker for this position filled while the app was off - booked; "
                        "waiting for the broker to cancel the rest before sending the exit.")
            if taken:
                return ""
        guarded = self.native_stops_on() and not t.get("pair_id") and bool(self._record_stop(t))
        if not guarded or tid in self._left_looked:
            return ""
        if working is None:
            return ("The broker's working orders couldn't be read - waiting to be sure no stop is resting for this "
                    "position before sending the exit.")
        if self._broker_resyncing():
            return ("The broker has only just connected and is still reloading its orders - waiting to be sure no "
                    "stop is resting for this position before sending the exit.")
        return ""

    def _place_target(self, tid: str, symbol: str, exit_side: Side, qty: float, price: float, group: str) -> None:
        req = OrderRequest(symbol=symbol, side=exit_side, quantity=qty, order_type=OrderType.LIMIT, limit_price=price,
                           tif=TimeInForce.GTC, is_entry=False, client_tag=target_tag(tid), oca_group=group,
                           oca_type=OCA_REDUCE)
        try:
            res = self.broker.place_order(req)
        except (OrderNotSent,) + OUTCOME_UNKNOWN as e:
            # no refusal: it never went out, or may rest at the broker in the stop's group - the next pass looks for it
            # there by its tag before the pair is placed afresh (_adopt_unknown_target)
            unknown = not isinstance(e, OrderNotSent)
            self._audit("PLACE", req, {"error": str(e), "outcome": "unknown" if unknown else "not sent"}, ok=False,
                        trade_id=tid, msg=str(e))
            if unknown:
                self._targets_unknown[tid] = group
            self._target_retry[tid] = time.monotonic() + self.SHARES_RETRY_S
            log.warning("TARGET AT BROKER  %s: the target got no answer from the broker in time (%s) - looked for "
                        "there before another is placed", symbol, e)
            return
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

    def _adopt_unknown_target(self, t: Dict[str, Any], working: List[OrderResult]) -> bool:
        """A target whose placing got no answer in time (OrderOutcomeUnknown), found resting at the broker after all -
        in the group of the stop it went out with, which still rests: followed from here, never doubled by a fresh
        pair. Looked for once; not found, the pair is placed afresh as for any missing target."""
        tid = t["id"]
        group = self._targets_unknown.pop(tid, None)
        st = self._stops.get(tid)
        if group is None or st is None or not group or st.group != group or tid in self._targets:
            return False
        found = next((o for o in working if o.tag == target_tag(tid) and o.status not in DONE_STATUSES), None)
        if found is None:
            return False
        self._targets[tid] = _Stop(found.order_id, tid, t["symbol"], float(found.submitted_qty or 0.0),
                                   float(found.limit_price or 0.0), moved_at=time.monotonic(), group=group)
        log.info("the target for %s that got no answer in time rests at the broker after all (order %s) - followed",
                 tid, found.order_id)
        return True

    def _rebuild(self, t: Dict[str, Any], working: List[OrderResult]) -> bool:
        """Stand down what rests for a trade and place the pair afresh, in a new group - an order
        can't change the group it is in. ``working``: the broker's orders, read before the stand-down
        (so nothing is stood down that can't be replaced). Returns whether anything was placed."""
        tid = t["id"]
        if not self._claim_resting(tid):
            return False                                 # an exit is standing them down this moment
        try:
            if tid in self.pending_exit_trade_ids():
                return False                             # an exit came and went since the pass began: it is the exit's
            gone = {o.order_id for o in (self._stops.get(tid), self._targets.get(tid)) if o is not None}
            if self._stand_down(tid) in ("filled", "busy"):
                return False                             # booked, or not confirmed yet: look again next pass
            fresh = self.repo.get_trade(tid)
            if not fresh or fresh.get("status") == "CLOSED":
                return False
            qty, price = abs(float(fresh.get("quantity") or 0.0)), self._record_stop(fresh)
            if qty <= 0 or not price:
                return False
            # the orders just cancelled no longer rest, though the list read before the stand-down still shows them
            self._place_stop(fresh, qty, price, [o for o in working if o.order_id not in gone])
            return True
        finally:
            self._release_resting(tid)

    def _move_stop(self, st: _Stop, price: float, qty: Optional[float] = None) -> bool:
        """Change the resting stop's trigger - and its shares, when ``qty`` is given. Without it the size
        is left as the broker holds it: a target in the stop's group that filled in part has already taken
        those shares off the stop, and resending the app's count would put them back. A broker that can't
        modify an order, or refuses the change, has it cancelled; the next pass places a fresh one. One that
        can't change it until it says what became of it (OrderInDoubt: a change it refused a moment after
        seeming to take it, say) leaves it resting where it is - it may well still protect the position - and
        its full order list, which settles it, is read on this pass: the move goes again after STOP_MOVE_S. So does a
        move the broker didn't answer in time: the stop rests, moved or not, and the next pass reads where."""
        try:
            res = self.broker.modify_stop(st.order_id, stop_price=price, quantity=qty)
        except (OrderInDoubt, OrderNotSent) + OUTCOME_UNKNOWN as e:
            log.info("the stop for %s isn't moved yet (%s) - it rests where it is meanwhile", st.trade_id, e)
            st.moved_at = time.monotonic()
            self._swept_at = 0.0
            return False
        except Exception as e:  # noqa: BLE001 - no modify on this venue, or the broker refused
            log.warning("could not move the stop for %s (%s) - replacing it", st.trade_id, e)
            self._drop_resting(st.trade_id)
            self._stop_retry[st.trade_id] = time.monotonic() + 5.0      # let the cancel land before looking again
            return False
        moved = abs(st.price - price) >= tick(price)
        held = float(getattr(res, "submitted_qty", 0.0) or 0.0)
        st.qty = float(qty) if qty is not None else (held or st.qty)  # in step with what the broker now holds
        st.price, st.moved_at = price, time.monotonic()
        if moved:
            self.bus.publish("stop.moved", trade_id=st.trade_id, symbol=st.symbol, qty=st.qty, stop_price=price)
        return True

    def _stop_size(self, st: _Stop, qty: float, side: Optional[str] = None) -> Optional[float]:
        """The shares a trade's stop should hold for ``qty`` shares on the record: those less what its resting
        target has filled while still working (the broker's group has already taken them off the stop; the record
        hears of them only when the target finishes), and no more than the broker holds the stop for now. Reading
        the stop brings ``st.qty`` in step with the broker. None when the broker can't say, or nothing would be
        left for the stop.

        A stop is grown back past what the broker holds - one shrunk for a part exit that never went - only given
        the trade's ``side``, with no target resting (its group may have cut the stop further than the target's
        fill read here shows) and while the account shows the shares: never an order the account can't cover."""
        try:
            held = float(self.broker.get_order(st.order_id).submitted_qty or 0.0)
            tg = self._targets.get(st.trade_id)
            filled = float(self.broker.get_order(tg.order_id).filled_qty or 0.0) if tg is not None else 0.0
        except Exception:  # noqa: BLE001
            return None
        size = qty - filled
        if held > 0:
            st.qty = held
            if size > held + 1e-9:
                account = self._held_quantity(st.symbol) if side and tg is None else None
                if account is None or (account > 0) != (side == "LONG") or abs(account) < size - 1e-9:
                    size = held
        return size if size > 1e-9 else None

    def _drop_resting(self, trade_id: str) -> None:
        """Cancel and forget whatever rests for a trade - until a full order list shows it gone, an exit that finds the
        list unreadable waits for it (_take_over_for_exit): the cancel may not land."""
        self._left_looked.discard(trade_id)
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
            self._left_looked.discard(tid)               # its cancel may not land: see _drop_resting
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
    def _claim_resting(self, trade_id: str, wait_s: float = 0.0) -> bool:
        """Take a trade's resting orders for an exit (or a rebuild) about to stand them down - False when another
        caller has them (still, after ``wait_s`` seconds). While claimed, the order sync neither moves, places, books
        nor loses them: a move the broker refused there would drop the stop from under the stand-down, which would
        then take it for cancelled. The sync holds them itself only for the moment it places, moves or books one.

        A claim is only ever taken under the executor's lock (Executor._lock: the order sync, an exit), and let go
        before it: the lock always comes first, so the two can't deadlock - and while a caller holds the lock, no
        other thread holds a claim."""
        deadline = time.monotonic() + wait_s
        while True:
            with self._claims_lock:
                if trade_id not in self._standing_down:
                    self._standing_down.add(trade_id)
                    return True
            if time.monotonic() >= deadline or self.STAND_DOWN_POLL_S <= 0:
                return False
            time.sleep(self.STAND_DOWN_POLL_S)

    def _release_resting(self, trade_id: str) -> None:
        with self._claims_lock:
            self._standing_down.discard(trade_id)

    def _claimed(self) -> set:
        with self._claims_lock:
            return set(self._standing_down)

    def _take_resting(self, book: Dict[str, _Stop], o: _Stop) -> bool:
        """Take a resting order that is done out of its book, before what it filled is booked: True for the one
        caller that takes it - the order sync's watch or an exit's stand-down - so a fill is booked once. False when
        it is no longer there (another caller has taken it, to book or drop it), and then nothing else - another
        order placed for the trade since - is touched."""
        with self._claims_lock:
            if book.get(o.trade_id) is not o:
                return False
            book.pop(o.trade_id, None)
            return True

    def _stand_down(self, trade_id: str) -> str:
        """Take a trade's resting orders out of the way of an exit the app is about to send.
        Returns "none" (nothing rested), "cancelled" (the way is clear), "filled" (one of them got
        there first and closed the position: send nothing) or "busy" (the broker hasn't confirmed:
        send nothing yet). A fill that left part of the position open is booked, and the way is
        cleared for the rest.

        A cancel asked for counts only once the broker confirms it: IBKR refusing it (the stop
        already filling) reads as working, and an order the app asked to cancel that reads cancelled
        without IBKR's word on it (``cancel_confirmed`` False) is waited for like one still working -
        on the next stand-down too, for up to ``CANCEL_UNCONFIRMED_S``. The orders it began with are
        read by their ids until each is done: one gone from the books meanwhile is no more cancelled
        for that. A done one is taken out of its book before what it filled is booked (_take_resting): one
        another caller took out first is theirs to book, and the exit waits for its next try. One the
        broker no longer knows at all is looked for in its executions (_filled_unseen):
        what it filled is booked, and while they can't be read the exit waits. And before the way is
        called clear, each order found cancelled is read once more, for a fill that landed just behind
        the cancel."""
        books = ((self._targets, self._book_target_fill), (self._stops, self._book_stop_fill))
        left: List[Tuple[Dict[str, _Stop], Any, _Stop]] = [     # the orders still to see done: book, booking, order
            (book, fill, o) for book, fill in books for o in [book.get(trade_id)] if o is not None]
        if not left:
            return "none"
        deadline = time.monotonic() + self.STAND_DOWN_S
        asked: set = set()
        cleared: List[Tuple[Dict[str, _Stop], Any, _Stop]] = []     # the orders found cancelled
        while True:
            just_asked, still = False, []
            for i, (book, fill, o) in enumerate(left):
                try:
                    res = self.broker.get_order(o.order_id)
                except Exception:  # noqa: BLE001
                    return self._still_resting(trade_id, still + left[i:])
                done = res.status in DONE_STATUSES or (res.status == "UNKNOWN" and not self._broker_resyncing())
                if done and _unconfirmed_cancel(res) and self._cancel_unconfirmed(o.order_id):
                    done = False                         # the broker never said the cancel went through: it may fill
                if done and res.status == "UNKNOWN":
                    seen = self._filled_unseen(book, fill, o)      # one the broker no longer knows may have filled
                    if seen is None:
                        return self._still_resting(trade_id, still + left[i:])   # not known: the exit waits
                    if seen:
                        self._cancels_sent.pop(o.order_id, None)
                        t = self.repo.get_trade(trade_id)
                        if not t or t.get("status") == "CLOSED":
                            self._drop_resting(trade_id)
                            return "filled"
                        continue                         # booked (popped from the books); the rest of it is gone
                if done or res.status == "FILLED":
                    self._cancels_sent.pop(o.order_id, None)
                    if not self._take_resting(book, o):
                        # another caller took it out of its book, to book what it filled or to drop it: theirs, never
                        # booked twice. The exit waits for its next try, by when the record says what was booked
                        t = self.repo.get_trade(trade_id)
                        if not t or t.get("status") == "CLOSED":
                            self._drop_resting(trade_id)
                            return "filled"
                        return self._still_resting(trade_id, still + left[i + 1:])
                if res.status == "FILLED" or (done and float(res.filled_qty or 0.0) > 0):
                    fill(o, res)
                    t = self.repo.get_trade(trade_id)
                    if not t or t.get("status") == "CLOSED":
                        self._drop_resting(trade_id)
                        return "filled"
                    continue
                if done:
                    cleared.append((book, fill, o))
                    continue
                if o.order_id not in asked:
                    self._cancel_quietly(o.order_id)
                    asked.add(o.order_id)
                    self._cancels_sent.setdefault(o.order_id, time.monotonic())
                    just_asked = True
                still.append((book, fill, o))
            left = still
            if not left:
                return self._last_look(trade_id, cleared)
            if not just_asked and time.monotonic() >= deadline:
                return self._still_resting(trade_id, left)
            if self.STAND_DOWN_POLL_S > 0:
                time.sleep(self.STAND_DOWN_POLL_S)

    def _cancel_unconfirmed(self, order_id: str) -> bool:
        """Whether an order that reads cancelled without the broker's word on it is still to be waited for: the app
        asked for its cancel, less than ``CANCEL_UNCONFIRMED_S`` ago. One cancelled with no ask of the app's (IBKR
        rejecting it, say) is done; so is one whose fill would have been heard by now."""
        asked_at = self._cancels_sent.get(order_id)
        return asked_at is not None and time.monotonic() - asked_at < self.CANCEL_UNCONFIRMED_S

    def _cancel_asked(self, trade_id: str) -> bool:
        """Whether an exit's stand-down has asked the broker to cancel an order resting for the trade, with its word on
        that still to come (``_cancel_unconfirmed``): the order sync then leaves them to the exit's next try - a move
        sent to one the broker took for cancelled would drop it from under the stand-down."""
        return any(o is not None and self._cancel_unconfirmed(o.order_id)
                   for o in (self._stops.get(trade_id), self._targets.get(trade_id)))

    def _still_resting(self, trade_id: str, left: List[Tuple[Dict[str, _Stop], Any, _Stop]]) -> str:
        """A stand-down that couldn't see its orders done: they stay followed - one dropped from the books meanwhile
        is followed again - and the exit waits."""
        for book, _, o in left:
            book.setdefault(trade_id, o)
        return "busy"

    def _last_look(self, trade_id: str, cleared: List[Tuple[Dict[str, _Stop], Any, _Stop]]) -> str:
        """One more read of the orders a stand-down found cancelled, before the way is called clear: a fill
        that landed just behind the cancel is booked - "filled" if it closed the position - and an order that
        reads as working again, or can't be read, is followed again and the exit waits ("busy"). One the broker no
        longer knows is looked for in its executions first, as in the stand-down."""
        booked: set = set()
        for book, fill, o in cleared:
            try:
                res = self.broker.get_order(o.order_id)
            except Exception:  # noqa: BLE001
                res = None
            seen: Optional[bool] = False
            if res is not None and res.status == "UNKNOWN":
                # (not while the broker is reloading its orders: it may know it again in a moment)
                seen = None if self._broker_resyncing() else self._filled_unseen(book, fill, o)
            if seen or (res is not None and (res.status == "FILLED" or float(res.filled_qty or 0.0) > 0)):
                if not seen:                             # (what its executions showed is booked already)
                    fill(o, res)
                booked.add(o.order_id)
                t = self.repo.get_trade(trade_id)
                if not t or t.get("status") == "CLOSED":
                    self._drop_resting(trade_id)
                    return "filled"
                continue
            if res is None or seen is None or (res.status not in DONE_STATUSES and res.status != "UNKNOWN"):
                for again, _, order in cleared:
                    if order.order_id not in booked:      # what was booked here is never booked twice
                        again.setdefault(trade_id, order)
                return "busy"
        return "cancelled"

    def _shrink_stop(self, trade_id: str, remaining: float) -> bool:
        """Before the app takes part of a position off itself: the stop covers only what will remain."""
        st = self._stops.get(trade_id)
        if st is None or remaining >= st.qty - 1e-9:
            return True
        size = self._stop_size(st, remaining)
        if size is None:
            return False                                 # not known what the broker holds: no exit beside it yet
        if size >= st.qty - 1e-9:
            return True                                  # the broker's group has already cut it that far
        return self._move_stop(st, st.price, size)

    # ------------------------------------------------------------------ #
    #  Booking what the broker filled                                    #
    # ------------------------------------------------------------------ #
    def _book_resting(self, book: Dict[str, _Stop], o: _Stop, *booking: Any) -> None:
        """Book what a resting order that is done filled (_book_exit with ``booking``). A booking the database refuses
        puts the order back in its book, marked ``unbooked``, before BookingFailed goes on: the next pass reads it again
        and books its fill then, and until it has, the trade is left as it is (fill_unbooked)."""
        try:
            self._book_exit(*booking)
        except BookingFailed:
            o.unbooked = True
            book.setdefault(o.trade_id, o)
            raise

    def _book_stop_fill(self, st: _Stop, res: OrderResult) -> None:
        self._stops.pop(st.trade_id, None)
        t = self.repo.get_trade(st.trade_id)
        if not t or t.get("status") == "CLOSED":
            return
        price = float(res.avg_fill_price or (res.fills[-1].price if res.fills else 0.0) or st.price)
        filled = float(res.filled_qty or st.qty)
        reason = stop_exit_reason(t.get("initial_stop_price"), st.price)
        partial = filled < abs(float(t["quantity"])) - 1e-9
        log.warning("STOP AT BROKER FILLED  %s x%s @ %.4f (%s)", st.symbol, filled, price, reason)
        self._book_resting(self._stops, st, st.symbol, st.trade_id, price, filled, reason, partial,
                           {} if partial else None, st.price)
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
            self._book_resting(self._targets, tg, tg.symbol, tg.trade_id, price, filled, "target-1", True,
                               plan[1] if plan else {}, tg.price)
            st = self._stops.get(tg.trade_id)
            if st is not None:
                # the broker's group has already shrunk the stop to what the position has left - some of it maybe
                # seen already, when a move read the stop while the target was still filling
                st.qty = min(st.qty, max(0.0, abs(float(t["quantity"])) - filled))
            self._target_retry.pop(tg.trade_id, None)    # the rest gets its own pair on the next pass
            return
        self._book_resting(self._targets, tg, tg.symbol, tg.trade_id, price, filled, "target", False, None, tg.price)
        st = self._stops.pop(tg.trade_id, None)          # the broker cancels it with the target; make sure
        if st is not None:
            self._cancel_quietly(st.order_id)

    def _book_filled_while_off(self, t: Dict[str, Any], stop: Optional[OrderResult],
                               target: Optional[OrderResult]) -> bool:
        """Whether the stop or target an earlier run left at the broker has filled any shares. A resting order's
        fills are booked once it is done, so what one still working has filled came while the app was off: it is
        booked here, before the orders are judged against the record - the target first, from IBKR's executions
        of each order (or the order's own count, where the executions don't reach back that far), and never more
        than the records hold beyond what the account shows, so nothing is booked twice. The record then matches
        the account. When the executions or the account can't be read nothing is booked and False is returned:
        the orders are followed as before, and the share-count warning says what disagrees."""
        tid, symbol = t["id"], t["symbol"]
        get = getattr(self.broker, "get_fills", None)
        try:
            executions = list(get(symbol) or []) if callable(get) else []
        except Exception:  # noqa: BLE001
            log.debug("executions for %s unavailable", symbol, exc_info=True)
            return False
        long = t["side"] == "LONG"
        exit_side = Side.SHORT if long else Side.LONG
        found = []
        for o, tag in ((target, target_tag(tid)), (stop, stop_tag(tid))):
            if o is None:
                continue
            mine = [f for f in executions if str(f.order_id) == str(o.order_id) and f.side is exit_side
                    and (getattr(f, "tag", "") or tag) == tag]
            shares = sum(float(f.quantity) for f in mine)
            qty = max(shares, float(o.filled_qty or 0.0))
            if qty <= 1e-9:
                continue
            if shares >= qty - 1e-9:
                price = sum(float(f.price) * float(f.quantity) for f in mine) / shares
            else:
                price = float(o.avg_fill_price or 0.0) or float(o.stop_price or o.limit_price or 0.0)
            found.append((o, tag, qty, price))
        if not found:
            return False
        held = self._held_quantity(symbol)
        if held is None or self._broker_resyncing():
            return False                                 # nothing to check a booking against: change nothing
        recorded = sum(abs(float(x.get("quantity") or 0.0)) for x in self.repo.open_trades()
                       if x["symbol"] == symbol and x["side"] == t["side"]
                       and (x.get("broker") or "paper") == self.venue)
        over = recorded - max(0.0, held if long else -held)       # shares on record that the account no longer holds
        for o, tag, qty, price in found:
            fresh = self.repo.get_trade(tid)
            if not fresh or fresh.get("status") == "CLOSED":
                break
            on_record = abs(float(fresh.get("quantity") or 0.0))
            take = min(qty, over, on_record)
            if take <= 1e-9:
                continue
            partial = take < on_record - 1e-9
            if tag == stop_tag(tid):
                decision = float(o.stop_price or 0.0) or self._record_stop(fresh)
                reason, after = stop_exit_reason(fresh.get("initial_stop_price"), decision), {}
            else:
                plan = scale_out_plan(fresh, getattr(self, "exit_cfg", None)) if partial else None
                decision, reason, after = o.limit_price, ("target-1" if partial else "target"), (plan[1] if plan else {})
            log.warning("%s AT BROKER FILLED WHILE THE APP WAS OFF  %s x%s @ %.4f (%s)",
                        "STOP" if tag == stop_tag(tid) else "TARGET", symbol, take, price, reason)
            self._book_exit(symbol, tid, price, take, reason, partial, after if partial else None, decision)
            over -= take
        return True
