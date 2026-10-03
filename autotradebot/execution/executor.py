"""Routes an approved Play to the broker and keeps the trade log in sync.

The engine only calls :meth:`execute_play` after the operator has clicked
"Yes" in the dashboard and the PDT / sizing checks have passed. Nothing
here decides *whether* to trade - only *how*.

Every order sent is followed until the broker finishes it. A fill opens or
closes the trade; a rejection, cancellation or expiry is published with the
broker's reason, and whatever part of the order did fill is booked.

Orders outlive the app: after a restart, the orders an earlier run left working
at the broker are taken over (see :meth:`Executor.adopt_working_orders`), so an
exit is never sent twice. An entry that finished while the app was off, or that
the broker no longer knows (the connection was down when it filled), is booked
from the broker's executions, so its shares get their record and their stop.

The sync loop, the dashboard's buttons, a quit's closes and Autopilot call in from
threads of their own. They take turns: the order sync, an entry being placed, a
take-over, a cancel and an exit each run under the executor's lock (``_lock``), so
an order is always followed before anything else looks for it.

An order the broker doesn't answer in time (OrderOutcomeUnknown) is never taken for
one not sent: it may be working, or have filled. The next order syncs look for it at
the broker by its tag (:meth:`Executor._look_for_unknown`) - it is followed if it
works, booked if it filled, and taken as never sent only once the broker shows it
neither working nor filled. Nothing is sent in its place meanwhile: an entry's play
stays sent (Autopilot keeps its slot) and the position's exit waits. The dashboard
hears of an exit's at once (order.unconfirmed) - its stop at the broker may be stood
down already - and of any order still not found a minute and a half on, every few
minutes, whether or not the broker can be read (:meth:`Executor._warn_unknown`).

A fill whose booking the database refuses (busy with another writer, say) is never lost: the booking raises
BookingFailed once it has been logged and the dashboard told (order.unbooked, again every few minutes while it keeps
failing), and the order stays followed - in ``_pending``, ``_unknown`` or its stop's book - so the next order sync
books it. Meanwhile it counts as working: an entry keeps Autopilot's slot, and no second exit goes out for the
position. An entry stays listed among the working entries while its fill is being booked, too (``_booking``), until
its record is saved: the open risk sizing counts, and Autopilot's caps, are read on other threads, with no lock, and
would otherwise find it neither working nor recorded for as long as the database takes. An entry's shares are no
longer in flight though (``symbols_in_flight(unbooked=False)``): its order is done, and the checks on shares no record
explains see them. Closing them as shares without a record (close_untracked) takes the entry off the books, so no
record appears later for shares already sold; a quit's cancels leave it followed.

Each fill is booked with the fees the broker has reported for it (OrderResult.commission, or its fills') and the id of
the order that filled. IBKR's commission report comes a moment after each execution, so the order sync adds what it
reports later to the day's fills booked before it had all come in (:meth:`Executor._top_up_fees`): a closed trade's
P/L and R are after every fee.

The order audit (``order_audit``, :meth:`Executor._audit`) records what happened to each order: every order placed,
with the broker's order id, status and message (PLACE); every cancel the app asks for, and why (CANCEL); every move of
a stop resting at the broker (MODIFY, protective_stops.py); and every error the broker sends about one of the app's
orders - a rejection, a cancel it refused with the state it names (IBKR's 10148), a cancel it made without the app
asking (an unrequested 202) - written failed on the next order sync (ERROR, :meth:`Executor._audit_order_errors`). A
call that failed or got no answer in time is written failed, with why. Nothing secret goes in: the order's details,
the broker's answer, and its words with the account number taken out.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..brokers.base import OUTCOME_UNKNOWN, DONE_STATUSES, BrokerAdapter, BrokerError, OrderNotSent
from ..brokers.venues import venue_label
from ..core.enums import PlayStatus, Side, StrategyKind, Timeframe
from ..core.eventbus import BUS
from ..core.models import Account, OrderRequest, OrderResult, Play
from ..util import clock
from .order_builder import build_entry_order, build_exit_order, plan_order
from .protective_stops import (TAG as STOP_TAG, TARGET_TAG, BookingFailed, ProtectiveStops, fees_of, order_fees,
                               shares_and_price)

log = logging.getLogger(__name__)


@dataclass
class _Pending:
    order_id: str
    play: Play
    kind: str                       # "entry" | "exit"
    trade_id: Optional[str] = None
    qty: float = 0.0
    order_type: str = "LIMIT"
    order_session: str = "REGULAR"
    reason: str = ""                # why an exit was sent: stop, target, manual...
    unseen: int = 0                 # polls in a row the broker didn't know the order
    partial: bool = False           # an exit for part of the position (the scale-out) - the record stays open
    after_fill: Optional[Dict[str, float]] = None   # the stop and target the rest gets once the part is off
    context: Optional[Dict[str, Any]] = None        # an entry's features at the decision (research/features.py)
    submitted_at: Optional[dt.datetime] = None
    expired: str = ""               # why the app cancelled this entry itself (a day trade not filled in time)
    decision: Optional[Dict[str, Any]] = None       # an entry: the quote at the decision (mid, spread_bps)
    decision_price: Optional[float] = None          # an exit: the price that triggered it
    adopted: bool = False           # left working by an earlier run: when it was really sent isn't known
    first_fill_at: Optional[float] = None   # an entry: when (monotonic) the broker first reported part of it filled
    filled_seen: float = 0.0        # an entry: the most the broker has reported filled while it worked...
    avg_seen: float = 0.0           # ...and at what average price
    cancel_at: float = 0.0          # when (monotonic) the app last asked the broker to cancel it
    left_seen: Optional[float] = None   # an exit: the shares the broker last said it still has to sell (None: not said
                                        # yet - all of ``qty``); 0 once it is done, its booking waiting or not
    sized_at: Optional[Tuple[float, float]] = None  # an entry: the price it was sized at and its risk per share there,
                                                    # as it was sent (_sized_at) - its play may be sized again since


class Executor(ProtectiveStops):
    #: polls in a row (the sync loop runs every 4 s) a connected broker may not know an order before it's given up
    LOST_AFTER_POLLS = 5
    #: after the broker (re)connects - IB Gateway's nightly restart - its order list takes a while to reload
    RESYNC_GRACE_S = 60.0
    #: seconds after which a cancel the app sent for an entry that is still working is sent again
    CANCEL_AGAIN_S = 30.0
    #: hours before the start that an earlier run's entry may have gone out and still be looked for in the
    #: broker's executions (IBKR reports the current day's)
    ENTRY_LOOKBACK_H = 24.0
    #: seconds before that look is tried again, when the broker's orders, executions or account couldn't be read
    ENTRY_LOOK_RETRY_S = 30.0
    #: seconds after an order call ran out of time (OrderOutcomeUnknown) before an order the broker shows neither
    #: working nor filled is taken as never sent - IBKR lists an order it has taken within a moment
    UNKNOWN_GIVE_UP_S = 30.0
    #: seconds an exit's executions may read as earlier than the send they belong to - the broker's clock and this
    #: computer's differ a little. (An exit's tag is shared by every exit its trade has had: the earlier ones' fills,
    #: booked before this one went out, are left out by their time)
    CLOCK_SLACK_S = 2.0
    #: seconds between looks in IBKR's executions for the fees of the day's fills booked before all of them were
    #: reported (its commission report comes a moment after each execution)
    FEES_CHECK_S = 60.0
    #: seconds after its booking that a fill's fee is settled: every report is in by then, and the fill is looked up
    #: no more (an account charged nothing has none to send)
    FEES_WAIT_S = 900.0

    def __init__(self, broker: BrokerAdapter, repo, cfg, bus=BUS,
                 venue: Optional[str] = None) -> None:
        self.broker = broker
        self.venue = venue or broker.name      # stamped on trades - see brokers/venues.py
        self.repo = repo
        self.cfg = cfg
        self.bus = bus
        #: one caller at a time sends, follows or calls off orders: the order sync (the loop's, or the dashboard's
        #: Refresh), an entry being placed, a take-over, the cancels, a close of shares without a record and an exit
        #: each hold it throughout. It is taken before a trade's claim on its resting orders (_claim_resting) and every
        #: claim is let go before it is, so while one caller holds it no other thread holds a claim: a claim is never
        #: waited for across threads. The engine's locks come before it (a quit or an approval holds its own lock while
        #: it calls in here); nothing done while it is held waits on one of those - the hooks below reach only
        #: Autopilot's counts, the runtime file and the database, whose locks are never held around a call in here
        self._lock = threading.RLock()
        #: trades whose exit a thread is sending this moment -> that thread (close_trade): a second close for one comes
        #: back at once instead of queueing behind the first, whose outcome settles it
        self._sending: Dict[str, int] = {}
        self._pending: Dict[str, _Pending] = {}
        #: orders whose send ran out of time with the outcome unknown (OrderOutcomeUnknown), by their tag -> what they
        #: were sent for (no order id yet) and when (monotonic) the call gave up: looked for at the broker by the order
        #: syncs (_look_for_unknown), and nothing is sent in their place meanwhile
        self._unknown: Dict[str, Tuple[_Pending, float]] = {}
        #: ...and, of those still not found a while on, when (monotonic) each was last said (_warn_unknown)
        self._unknown_said: Dict[str, Tuple[_Pending, float]] = {}
        #: entries whose fill is being booked this moment, taken out of ``_pending`` or ``_unknown`` for it (by id):
        #: listed with the working entries (_sent) until their record is saved, or they go back on a refusal
        self._booking: Dict[int, _Pending] = {}
        #: fills the database refused to book, by what they were for ("entry:<play id>", "exit:<trade id>") -> the
        #: tries that failed so far: the dashboard hears of the first, and again every few minutes while they keep
        #: failing (when, monotonic: ``_unbooked_said``), the log of each, till one takes (_booking_failed)
        self._unbooked: Dict[str, int] = {}
        self._unbooked_said: Dict[str, float] = {}
        self._open_by_symbol: Dict[str, str] = {}   # symbol -> trade_id
        #: the exit manager takes part of a position off at the first target, so a native bracket
        #: (the simulator's) carries the stop only - a take-profit child would close all of it there
        self.scale_out: bool = False
        #: the exit manager's settings - how a position scales out decides the shares a resting target covers
        self.exit_cfg: Any = None
        #: told the play id of an entry that ended with nothing bought - Autopilot hands back the day's slot
        #: it took. Set once by the engine; a rebind keeps it
        self.on_entry_unfilled: Optional[Callable[[str], Any]] = None
        #: told the play ids of the entries an earlier run left working, once they are taken over - Autopilot
        #: counts the ones it sent. Set once by the engine; a rebind keeps it
        self.on_entries_adopted: Optional[Callable[[List[str]], Any]] = None
        #: the broker's orders couldn't be listed when they were to be taken over: each order sync tries again
        self._adopt_due = False
        #: the entries sent before ``_bound_at`` that may have filled while the app was off are still to be
        #: looked for in the broker's executions (_book_entries_filled_while_off)
        self._entries_due, self._bound_at = True, dt.datetime.now(dt.timezone.utc)
        self._entries_retry_at = 0.0
        #: when (monotonic) the day's fills are next looked up in the broker's executions for fees reported after their
        #: booking, and the fills (row ids) whose fees are settled - looked up FEES_WAIT_S after it (_top_up_fees)
        self._fees_due_at, self._fees_settled = 0.0, set()
        self._init_stops()

    def rebind(self, broker: BrokerAdapter, venue: Optional[str] = None) -> None:
        """Point at a different broker (paper <-> live / platform switch).
        In-flight order tracking is broker-specific, so it is dropped; open
        trades in the database are untouched."""
        with self._lock:                # never under an order sync's feet, nor an exit's
            self._audit_order_errors()  # what the broker left behind said, written before it is let go
            self.broker = broker
            self.venue = venue or broker.name
            self._pending.clear()
            self._unknown.clear()       # an entry's play stays sent: the look for entries sent before finds it
            self._unknown_said.clear()
            self._unbooked.clear()
            self._unbooked_said.clear()
            self._open_by_symbol.clear()
            self._entries_due, self._bound_at = True, dt.datetime.now(dt.timezone.utc)
            self._entries_retry_at = 0.0
            self._fees_due_at = 0.0     # the new venue's fills are looked up on the next pass
            self._init_stops()          # the other venue's stops stay where they are; found again by their tags

    def cancel_pending_entries(self) -> int:
        """Cancel entry orders still working at the broker (used when quitting). One that filled whose booking the
        database has refused so far has nothing left to cancel: it stays followed, so the next pass books it - its
        shares are held - and the quit waits for that record (QuitOps._unbooked_left) to close it like any other."""
        n = 0
        with self._lock:
            for oid, p in list(self._pending.items()):
                if p.kind != "entry" or self._entry_unbooked(p):
                    continue
                self._cancel_quietly(oid, "cancelled when quitting", p)
                self._pending.pop(oid, None)
                n += 1
            n += self._call_off_unknown(lambda p: True, "cancelled when quitting")
        return n

    def _call_off_unknown(self, which: Callable[[_Pending], bool], why: str) -> int:
        """Mark the entries whose send got no answer in time (``which`` of them) to be cancelled once they are found
        working at the broker - followed then, so what they filled meanwhile is still booked. (One found filled whose
        booking the database refused is no order to call off.) Returns how many."""
        n = 0
        for p, _ in list(self._unknown.values()):
            if p.kind == "entry" and which(p) and not p.expired and not self._entry_unbooked(p):
                p.expired = why
                n += 1
        return n

    def flatten_untracked(self, symbol: str, position_side: str, qty: float) -> bool:
        """Close shares the broker holds that have no trade record - what's left when an order filled
        but booking it failed. A market order, audited like any other."""
        return bool(self.close_untracked(symbol, position_side, qty).get("ok"))

    def close_untracked(self, symbol: str, position_side: str, qty: float) -> Dict[str, Any]:
        """Send the market order that closes ``qty`` shares held without a record, and say how it went. The entries
        among those shares whose fill the database refused to book so far are taken off the books in the same step
        (_unwound): booked once it takes, each would open a record for shares already sold - or still being sold by
        this order - and get a stop and an exit of its own beside it. Never more than the account holds beyond the
        records, read again here: a booking that took since the shares were listed has a record, and a stop, of its
        own."""
        with self._lock:                # not beside an order sync, nor an exit counting the shares held
            beyond = self._held_beyond_records(symbol, position_side)
            if beyond is not None and beyond < qty - 1e-9:
                if beyond <= 1e-9:
                    return {"ok": False, "reason": f"{venue_label(self.venue)} holds no {symbol} shares beyond the "
                                                   f"trade records now - nothing sent."}
                qty = beyond
            req = build_exit_order(symbol, position_side, qty, cfg=self.cfg, tag=f"unwind:{symbol}")
            sent_at = dt.datetime.now(dt.timezone.utc)
            try:
                res = self.broker.place_order(req)
            except OUTCOME_UNKNOWN as e:
                # it may have reached the broker: another sent now could close the shares twice, and open a position
                # the other way - the account says, once it shows whether these went. (An entry booked meanwhile could
                # sell them twice as well; let go, its shares are listed again if these didn't go)
                why = str(e) or "no answer from the broker in time"
                self._audit("PLACE", req, {"error": why, "outcome": "unknown"}, ok=False, msg=why, ts=sent_at)
                self._unwound(symbol, position_side)
                log.error("the order closing %s %s shares without a trade record got no answer in time - it may have "
                          "reached the broker: %s", qty, symbol, why)
                return {"ok": False, "sent_unknown": True,
                        "reason": f"{why}. Check the {symbol} position before closing it again."}
            except BrokerError as e:
                self._audit("PLACE", req, {"error": str(e)}, ok=False, msg=str(e), ts=sent_at)
                log.error("could not close %s %s shares that have no trade record: %s", qty, symbol, e)
                return {"ok": False, "reason": str(e)}
            self._audit("PLACE", req, res, ok=True, msg="closing shares without a trade record", ts=sent_at)
            self._unwound(symbol, position_side)
        log.warning("closing %s %s shares held without a trade record (order %s, %s)", qty, symbol, res.order_id,
                    res.status)
        return {"ok": True, "status": res.status or "WORKING", "order_id": res.order_id, "qty": qty}

    def _held_beyond_records(self, symbol: str, position_side: str) -> Optional[float]:
        """Shares of ``symbol`` the account holds on ``position_side`` beyond this venue's open records of that side
        (negative when it holds fewer), or None when the account or the records can't be read."""
        held = self._held_quantity(symbol)
        if held is None:
            return None
        try:
            trades = self.repo.open_trades()
        except Exception:  # noqa: BLE001 - the database busy, say: not known
            log.debug("could not read the open records before closing shares without one", exc_info=True)
            return None
        recorded = sum(abs(float(t.get("quantity") or 0.0)) for t in trades if t["symbol"] == symbol
                       and t["side"] == position_side and (t.get("broker") or "paper") == self.venue)
        return (held if position_side == "LONG" else -held) - recorded

    def _unwound(self, symbol: str, position_side: str) -> List[str]:
        """Take off the books the entries of ``symbol`` on ``position_side`` that filled and whose booking the database
        has refused so far, once their shares are being closed as shares without a record (close_untracked): they are
        followed no more, so no record is opened for them, and their plays are marked ERROR - never sent again, nor
        booked from the broker's executions after a restart. Under the executor's lock. Returns their play ids."""
        followed = [(self._pending, oid, p) for oid, p in self._pending.items()] + \
            [(self._unknown, ref, p) for ref, (p, _) in self._unknown.items()]
        out: List[str] = []
        for book, key, p in followed:
            if p.play.symbol != symbol or p.play.side.value != position_side or not self._entry_unbooked(p):
                continue
            del book[key]
            self._let_go_unbooked(p)
            self._note(p.play, PlayStatus.ERROR, {
                "status": "UNWOUND", "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "reason": "it filled, its booking couldn't be saved, and its shares were closed as shares without a "
                          "record"})
            log.warning("UNBOOKED ENTRY LET GO  %s (play %s): its shares are being closed as shares without a record - "
                        "no record is opened for them", symbol, p.play.id)
            out.append(p.play.id)
        return out

    def _let_go_unbooked(self, p: _Pending) -> None:
        """Forget that the booking of an entry no longer followed keeps failing."""
        self._unbooked.pop(f"entry:{p.play.id}", None)
        self._unbooked_said.pop(f"entry:{p.play.id}", None)

    def forget_open(self, symbol: str) -> None:
        """Drop the note that ``symbol`` is held - its record was closed without an exit going through here."""
        self._open_by_symbol.pop(symbol, None)
        self._swept_at = 0.0            # ...so its stop at the broker goes on the very next pass

    def cancel_entries_for(self, play_id: str) -> int:
        """Call off the entry orders still working for one play - a pair leg whose other leg failed. A leg that filled
        whose booking the database refused so far has nothing to cancel: it stays followed, its play marked called off
        all the same, so the next pass books it once the database takes it - a leg whose pair is over, which the desk
        closes at its next pass (PairDesk.manage, "pair-unwind"). Let go instead, its shares would stay at the broker
        with no record, no stop and no exit. (A leg whose shares the desk closes as shares without a record - _flatten -
        is taken off the books by that close: _unwound.)"""
        n = 0
        with self._lock:
            for oid, p in list(self._pending.items()):
                if p.kind == "entry" and p.play.id == play_id:
                    if self._entry_unbooked(p):
                        log.warning("UNBOOKED ENTRY KEPT  %s (play %s): the pairs desk called its pair off - the leg "
                                    "is booked once the database takes it, and closed then", p.play.symbol, play_id)
                    else:
                        self._cancel_quietly(oid, "called off by the pairs desk", p)
                        self._pending.pop(oid, None)
                    p.play.status = PlayStatus.CANCELED
                    n += 1
            n += self._call_off_unknown(lambda p: p.play.id == play_id, "called off by the pairs desk")
        return n

    def _sent(self) -> List[_Pending]:
        """Every order sent and not finished: the ones followed, the ones whose send got no answer in time, which
        may be working too (_unknown), and the entries whose fill is being booked this moment (_booking). Read with no
        lock - sizing on the scan thread, say, while an order sync books a fill - so ``_booking`` is read before and
        after the other two, and an entry moving into it or back out (put in its new place before it leaves the old)
        is always found in one of them."""
        booking = list(self._booking.values())
        sent = list(self._pending.values()) + [p for p, _ in list(self._unknown.values())]
        seen = {id(p) for p in sent}
        for p in booking + list(self._booking.values()):
            if id(p) not in seen:
                seen.add(id(p))
                sent.append(p)
        return sent

    def pending_exit_trade_ids(self) -> set:
        """Trades whose close order is still working at the broker - or may be: one whose send got no answer in time
        is looked for there before anything else goes out for the trade."""
        return {p.trade_id for p in self._sent() if p.kind == "exit" and p.trade_id}

    def working_entries(self) -> List[Dict[str, Any]]:
        """Entry orders sent but not filled yet - one whose send got no answer in time too (no order id yet), and one
        that filled whose booking the database refused so far (``unbooked``: its shares are at the broker, with no
        record yet). Anything that limits positions has to count these too, or a slow fill gets doubled up.
        ``notional`` and ``risk`` are at the price the entry was sized at (_sized_at): the limit the last look
        re-priced it to, if it did - as it was sent, whatever its play is sized at since. ``pair_leg``: one leg of a
        pair trade, whose stop - and so its ``risk`` - is a placeholder."""
        out = []
        for p in self._sent():
            if p.kind != "entry":
                continue
            price, per_share = p.sized_at or _sized_at(p.play)
            out.append({"order_id": p.order_id, "play_id": p.play.id, "symbol": p.play.symbol,
                        "strategy": p.play.strategy, "timeframe": p.play.timeframe.value,
                        "qty": p.qty, "notional": price * p.qty, "risk": per_share * p.qty,
                        "unbooked": self._entry_unbooked(p), "pair_leg": _pair_leg(p.play)})
        return out

    def symbols_in_flight(self, unbooked: bool = True) -> set:
        """Symbols with an order still working - their share counts are about to change. With ``unbooked`` False, an
        entry that filled whose booking the database refused is left out: its order is done, so its shares are at the
        broker for good - with no record and no stop until the booking takes - and the checks on shares no record
        explains must see them, however long the database keeps refusing."""
        return {p.play.symbol for p in self._sent() if unbooked or not self._entry_unbooked(p)}

    def _entry_unbooked(self, p: _Pending) -> bool:
        """Whether ``p`` is an entry that filled and whose booking the database has refused so far (_booking_failed)."""
        return p.kind == "entry" and f"entry:{p.play.id}" in self._unbooked

    def active_orders(self) -> List[Dict[str, Any]]:
        """Every order still working at the broker and what it is for: an entry, an exit,
        a bracket's stop or target, or an order placed outside the app. Raises when the
        broker can't be asked, so a failed check never reads as "no orders"."""
        trades = {t["id"]: t for t in self.repo.open_trades()}
        return [self._describe(o, trades) for o in self.broker.list_orders("WORKING")
                if o.status not in DONE_STATUSES]

    def _describe(self, o: OrderResult, trades: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        p = self._pending.get(o.order_id)
        followed = p is not None
        trade_id = p.trade_id if followed else next((o.tag[len(prefix):] for prefix in ("exit:", STOP_TAG, TARGET_TAG)
                                                     if o.tag.startswith(prefix)), None)
        play_id = (p.play.id if p.kind == "entry" else None) if followed else \
            (o.tag.split(":")[0] if o.tag.startswith("play_") else None)
        trade = trades.get(trade_id or "") or {}
        return {"order_id": o.order_id, "symbol": o.symbol,
                "action": {Side.LONG: "BUY", Side.SHORT: "SELL"}.get(o.side, ""),
                "qty": float(o.submitted_qty or 0.0), "filled": float(o.filled_qty or 0.0),
                "remaining": _remaining(o), "order_type": o.order_type, "limit_price": o.limit_price,
                "stop_price": o.stop_price, "tif": o.tif, "status": o.status,
                "purpose": p.kind if followed else _purpose(o), "reason": p.reason if followed else "",
                "trade_id": trade_id, "play_id": play_id,
                "strategy": (p.play.strategy if followed else "") or trade.get("strategy") or "",
                "message": o.message, **self._entry_clock(p)}

    def _entry_clock(self, p: Optional[_Pending]) -> Dict[str, Any]:
        """When a followed entry's time is up, for the dashboard's countdowns - by the rules expire_entries
        acts on: ``expires_at`` a day-trade entry's time-out (entry_timeout_min after it went out, or after it
        was taken over), ``cut_at`` the rest of a part-filled one cancelled (partial_entry_wait_s after its
        first fill; never a pair leg's), ``calling_off`` why the app has asked the broker to cancel it and is
        waiting to hear it has. Times are UTC. engine._order_signature counts only whether each one is there,
        so one appearing is pushed and the browser counts down - nothing is pushed each second."""
        out: Dict[str, Any] = {"submitted_at": None, "expires_at": None, "cut_at": None, "calling_off": None}
        if p is None or p.kind != "entry":
            return out
        limit = float(getattr(self.cfg, "entry_timeout_min", 0) or 0)
        wait = float(getattr(self.cfg, "partial_entry_wait_s", 0) or 0)
        if p.submitted_at is not None and not p.adopted:
            out["submitted_at"] = p.submitted_at.isoformat(timespec="milliseconds")   # an adopted one's isn't known
        if limit > 0 and p.submitted_at is not None and p.play.timeframe is Timeframe.INTRADAY:
            out["expires_at"] = (p.submitted_at + dt.timedelta(minutes=limit)).isoformat(timespec="milliseconds")
        if wait > 0 and p.first_fill_at is not None and not _pair_leg(p.play):
            # the first fill was noted on the monotonic clock: it came this long before now, and the wait runs from it
            due = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=p.first_fill_at + wait - time.monotonic())
            out["cut_at"] = due.isoformat(timespec="milliseconds")
        out["calling_off"] = p.expired or None
        return out

    # ------------------------------------------------------------------ #
    def adopt_working_orders(self) -> List[Dict[str, Any]]:
        """Take over the orders an earlier run of the app left working at the broker,
        so a restart never sends a second exit or loses track of an entry.

        An exit is matched to its open trade by its tag, or else by symbol, direction
        and share count. Extra copies of an exit the app itself sent - more shares
        than the open trades hold - are cancelled. An entry is matched to its play by
        its tag.

        A broker whose orders can't be listed has told us nothing, not that none are
        working: each order sync tries again until the list comes back.

        Under the executor's lock: an entry or an exit being placed this moment is followed by the
        caller placing it before this looks - never taken over as an earlier run's beside it."""
        with self._lock:
            working = self._working_or_none()
            if working is None:
                if not self._adopt_due:
                    log.warning("the orders working at %s couldn't be listed - taking them over is tried again at "
                                "each order sync", venue_label(self.venue))
                self._adopt_due = True
                return []
            self._adopt_due = False
            if not working:
                return []
            trades = [t for t in self.repo.open_trades() if (t.get("broker") or "paper") == self.venue]
            adopted: List[Dict[str, Any]] = []
            for t in sorted(trades, key=lambda t: t.get("entry_time") or ""):
                order = _match_exit(t, working, set(self._pending))
                if order is not None and t["id"] not in self.pending_exit_trade_ids():
                    self._track_exit(t, order, reason="exit")
                    adopted.append({"kind": "exit", "symbol": t["symbol"], "order_id": order.order_id,
                                    "trade_id": t["id"], "qty": _remaining(order)})
            for order in working:
                # (one whose send got no answer in time is the order syncs' to find: _look_for_unknown)
                play = self._play_for(order) if order.order_id not in self._pending and \
                    order.tag not in self._unknown else None
                if play is not None:
                    # its clock starts again from here: a day-trade entry gets entry_timeout_min more minutes
                    self._pending[order.order_id] = _Pending(order.order_id, play, "entry", qty=_remaining(order),
                                                             submitted_at=dt.datetime.now(dt.timezone.utc),
                                                             adopted=True)
                    adopted.append({"kind": "entry", "symbol": play.symbol, "order_id": order.order_id,
                                    "play_id": play.id, "qty": _remaining(order)})
            cancelled = self._cancel_extra_exits(working, trades)
        if adopted or cancelled:
            msg = (f"Following {len(adopted)} order(s) already working at {venue_label(self.venue)}"
                   + (f"; cancelled {len(cancelled)} duplicate exit(s): "
                      + ", ".join(f"{o.symbol} {_remaining(o):,.0f}" for o in cancelled) if cancelled else "")
                   + ".")
            log.warning("%s %s", msg, adopted)
            self.bus.publish("orders.adopted", adopted=adopted,
                             cancelled=[o.order_id for o in cancelled], msg=msg)
        entries = [a["play_id"] for a in adopted if a["kind"] == "entry"]
        if entries and self.on_entries_adopted is not None:
            try:
                self.on_entries_adopted(entries)
            except Exception:  # noqa: BLE001 - never let it stop the order sync
                log.exception("telling Autopilot about the entries taken over failed")
        return adopted

    def _working_at_broker(self) -> List[OrderResult]:
        """The orders working at the broker, or none when it can't be asked - for the sweeps, which
        then cancel nothing this time."""
        return self._working_or_none() or []

    def _working_or_none(self) -> Optional[List[OrderResult]]:
        """The orders working at the broker, or None when it couldn't be asked - for the callers that
        must tell "nothing is working" from "not known". A broker that isn't connected isn't asked:
        it can say nothing of its orders then."""
        if getattr(self.broker, "is_connected", True) is False:
            return None
        try:
            return [o for o in self.broker.list_orders("WORKING")
                    if o.status not in DONE_STATUSES and o.side is not None]
        except Exception:  # noqa: BLE001
            log.debug("could not list the orders working at the broker", exc_info=True)
            return None

    def _track_exit(self, t: Dict[str, Any], order: OrderResult, reason: str) -> None:
        left = _remaining(order)
        self._pending[order.order_id] = _Pending(order.order_id, Play(**_min_play(t)), "exit",
                                                 trade_id=t["id"], qty=left, reason=reason,
                                                 partial=left < abs(float(t["quantity"])) - 1e-9)

    def _play_for(self, order: OrderResult) -> Optional[Play]:
        """The play an entry order left working was sent for, from the play log."""
        if not order.tag.startswith("play_"):
            return None
        row = self.repo.get_play(order.tag)
        if row is None or Side(row["side"]) is not order.side:
            return None
        return _play_from_row(row)

    def _book_entries_filled_while_off(self) -> List[str]:
        """Book the entries an earlier run sent that finished while the app was off - their plays still SUBMITTED
        in the play log, with no trade record, no order working for them and none followed here: what the broker's
        executions tagged with a play's id show it bought, up to what the account holds beyond the records, becomes
        its trade through the same booking as a fill heard live, so the position gets its stop on the next pass.
        Looked for once, when the broker is connected, past its re-sync after the connect, and its orders,
        executions and account could be read - a read that failed is no "none": the look is tried again every
        ENTRY_LOOK_RETRY_S. Pair legs are left to the desk: the play log doesn't keep their pair. Returns the trades
        booked."""
        if (getattr(self.broker, "is_connected", True) is False or self._broker_resyncing()
                or time.monotonic() < self._entries_retry_at):
            return []                                   # looked for once it is connected and its lists have reloaded
        find = getattr(self.repo, "submitted_plays", None)
        if not callable(find):
            self._entries_due = False
            return []
        try:
            rows = find(self._bound_at - dt.timedelta(hours=self.ENTRY_LOOKBACK_H), self._bound_at)
        except Exception:  # noqa: BLE001
            log.debug("could not read the plays sent before the start", exc_info=True)
            self._entries_retry_at = time.monotonic() + self.ENTRY_LOOK_RETRY_S
            return []
        followed = {p.play.id for p in self._sent() if p.kind == "entry"}
        rows = [r for r in rows if r["id"] not in followed and "pair-leg" not in (r.get("tags") or [])]
        working = self._working_or_none() if rows else []
        fills = self._executions(None) if rows else []
        if working is None or fills is None:
            # not known: looked for again shortly (IBKR's executions are read strictly - a request that failed or
            # timed out is no "none", or the entry would never be looked for again)
            self._entries_retry_at = time.monotonic() + self.ENTRY_LOOK_RETRY_S
            return []
        working_for, found = {o.tag for o in working}, []
        for row in rows:
            if row["id"] in working_for:
                continue                                # still working: followed once it is taken over
            side = Side(row["side"])
            mine = [f for f in fills if (getattr(f, "tag", "") or "") == row["id"] and f.side is side
                    and f.symbol == row["symbol"]]
            qty, price = shares_and_price(mine)
            if qty > 1e-9:
                found.append((row, side, qty, price, str(mine[-1].order_id), fees_of(mine)))
        self._entries_due = False
        booked: List[str] = []
        for row, side, qty, price, order_id, fee in found:
            try:
                tid = self._book_entry_filled_while_off(row, side, qty, price, order_id, fee)
            except BookingFailed:
                tid = None                              # (said as it failed) looked for again shortly
            except Exception:  # noqa: BLE001 - the database busy, say: this one is looked for again, the rest go on
                log.exception("booking the entry for %s (play %s) that filled while the app was off failed - looked "
                              "for again shortly", row["symbol"], row["id"])
                tid = None
            if tid is None:
                # the account couldn't be read, or the booking failed: looked for again (one booked by then has its
                # trade, and is no longer among the plays looked for)
                self._entries_due = True
                self._entries_retry_at = time.monotonic() + self.ENTRY_LOOK_RETRY_S
            elif tid:
                booked.append(tid)
        return booked

    def _book_entry_filled_while_off(self, row: Dict[str, Any], side: Side, qty: float, price: float,
                                     order_id: str, commission: float = 0.0) -> Optional[str]:
        """Book one entry found filled while the app was off (``commission``: what its executions paid). Returns its
        trade id, "" when the account holds none of its shares beyond the records (nothing booked), None when the
        account couldn't be read."""
        # never more than the account holds beyond the records: shares sold by hand meanwhile get no record,
        # and the record that is booked matches the account, so its stop can go on
        held = self._held_quantity(row["symbol"])
        if held is None:
            return None
        recorded = sum(abs(float(t.get("quantity") or 0.0)) for t in self.repo.open_trades()
                       if t["symbol"] == row["symbol"] and t["side"] == side.value
                       and (t.get("broker") or "paper") == self.venue)
        take = min(qty, (held if side is Side.LONG else -held) - recorded)
        if take <= 1e-9:
            log.warning("an entry for %s (play %s) filled while the app was off, but the account holds none of "
                        "its shares beyond the records - not booked", row["symbol"], row["id"])
            return ""
        tid = self._open_trade(_play_from_row(row), price, take, order_id, commission=commission * take / qty)
        log.warning("ENTRY FILLED WHILE THE APP WAS OFF  %s %s x%s @ %.4f (play %s) - booked from the broker's "
                    "executions", row["symbol"], side.value, take, price, row["id"])
        return tid

    def _cancel_extra_exits(self, working: List[OrderResult], trades: List[Dict[str, Any]]) -> List[OrderResult]:
        recorded: Dict[str, float] = {}
        for t in trades:
            recorded[t["symbol"]] = recorded.get(t["symbol"], 0.0) + abs(float(t["quantity"]))
        sides = {t["symbol"]: _exit_side(t["side"]) for t in trades}
        extra: List[OrderResult] = []
        for o in working:
            mine = o.tag.startswith("exit:") or (o.raw or {}).get("mine")
            if (o.order_id in self._pending or o.tag in self._unknown or o.symbol not in recorded
                    or o.side is not sides[o.symbol] or not mine):
                continue
            if o.tag.startswith((STOP_TAG, TARGET_TAG)):
                continue                                 # a resting stop or target is not an exit; protective_stops.py owns it
            if self._exiting_quantity(o.symbol) + _remaining(o) > recorded[o.symbol] + 1e-9:
                self._cancel_quietly(o.order_id, "a second exit for shares another exit already sells", o)
                extra.append(o)
        return extra

    # ------------------------------------------------------------------ #
    def execute_play(self, play: Play, account: Account,
                     plan: Optional[Dict[str, Any]] = None,
                     context: Optional[Dict[str, Any]] = None,
                     decision: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """``context``: what the play looked like at the decision (research/features.py), kept on
        the trade record for learning; ``decision``: the quote then, which the fill is measured
        against (Harris's implementation shortfall)."""
        qty = int(play.suggested_qty or 0)
        if qty <= 0:
            return {"ok": False, "reason": "position size is zero (risk budget / buying power)"}

        if plan is None:
            plan = plan_order(play, clock.current_session(), self.cfg)
        if not plan.get("executable"):
            return {"ok": False, "reason": plan.get("reason", "not executable in this session")}

        entry = build_entry_order(play, qty, self.cfg, plan)
        native_bracket = plan.get("bracket_mode") == "native" and (
            self.broker.supports_bracket_native or self.broker.paper
        )
        ot, osess = plan.get("order_type", "LIMIT"), plan.get("order_session", "REGULAR")
        # placed and followed (or booked) in one go: an order sync or a take-over never finds the order at the
        # broker before it is followed here, and never books its fill beside this
        with self._lock:
            submitted_at = dt.datetime.now(dt.timezone.utc)
            try:
                if native_bracket:
                    res = self.broker.place_bracket(entry, None if self.scale_out else play.primary_target, play.stop)
                else:
                    res = self.broker.place_order(entry)      # exit manager will protect it
            except OUTCOME_UNKNOWN as e:
                return self._entry_unknown(play, entry, qty, ot, osess, context, submitted_at, decision, e)
            except BrokerError as e:
                self._audit("PLACE", entry, {"error": str(e)}, ok=False, play_id=play.id, msg=str(e),
                            ts=submitted_at)
                play.status = PlayStatus.ERROR
                return {"ok": False, "reason": str(e)}

            self._audit("PLACE", entry, res, ok=True, play_id=play.id, msg=res.message, ts=submitted_at)
            play.status = PlayStatus.SUBMITTED

            # immediate fill (paper / marketable) -> open the trade now
            if res.status in ("FILLED",) or res.filled_qty >= qty > 0:
                fill_price = res.avg_fill_price or (res.fills[-1].price if res.fills else play.entry)
                try:
                    tid = self._open_trade(play, fill_price, res.filled_qty or qty, res.order_id, ot, osess,
                                           context=context, submitted_at=submitted_at, decision=decision,
                                           commission=order_fees(res))
                except BookingFailed:
                    pass        # followed below instead: the next order sync reads the fill and books it then
                else:
                    return {"ok": True, "status": "FILLED", "trade_id": tid,
                            "fill_price": round(fill_price, 4), "qty": res.filled_qty or qty,
                            "order_id": res.order_id, "order_type": ot, "order_session": osess,
                            "bracket_mode": plan.get("bracket_mode")}

            # otherwise track it; sync_open_orders() will pick up the fill. The play log says it went out
            # before the sync loop can hear how it ended, so the ending is never overwritten by this
            p = _Pending(res.order_id, play, "entry", qty=qty)
            p.order_type, p.order_session = ot, osess
            p.context, p.submitted_at, p.decision = context, submitted_at, decision
            p.sized_at = _sized_at(play)
            self._note(play, PlayStatus.SUBMITTED)
            self._pending[res.order_id] = p
            return {"ok": True, "status": res.status or "WORKING", "order_id": res.order_id,
                    "order_type": ot, "order_session": osess,
                    "note": "order working - will confirm on fill"}

    def _entry_unknown(self, play: Play, entry: OrderRequest, qty: int, ot: str, osess: str,
                       context: Optional[Dict[str, Any]], submitted_at: dt.datetime,
                       decision: Optional[Dict[str, Any]], e: BaseException) -> Dict[str, Any]:
        """An entry the broker didn't answer in time: it may be working, or have filled. The play log says it was sent
        first - so a restart looks for it too - and the next order syncs look for it at the broker by its tag (the
        play's id): followed if it works, booked if it filled (_look_for_unknown). Under the executor's lock."""
        why = str(e) or "no answer from the broker in time"
        self._audit("PLACE", entry, {"error": why, "outcome": "unknown"}, ok=False, play_id=play.id, msg=why,
                    ts=submitted_at)
        p = _Pending("", play, "entry", qty=qty, order_type=ot, order_session=osess, context=context,
                     submitted_at=submitted_at, decision=decision, sized_at=_sized_at(play))
        self._note(play, PlayStatus.SUBMITTED)
        self._unknown[entry.client_tag or play.id] = (p, time.monotonic())
        log.warning("ENTRY NOT CONFIRMED  %s %s x%s (play %s): %s - looked for at %s by its tag before anything else "
                    "is sent", play.symbol, play.side.value, qty, play.id, why, venue_label(self.venue))
        return {"ok": False, "sent_unknown": True, "order_type": ot, "order_session": osess,
                "reason": f"{why}. The order may be working - the app looks for it at the broker and follows it "
                          f"if it is there."}

    # ------------------------------------------------------------------ #
    def close_trade(self, trade_id: str, reason: str = "manual", limit_price: Optional[float] = None,
                    qty: Optional[float] = None, after_fill: Optional[Dict[str, float]] = None,
                    decision_price: Optional[float] = None) -> Dict[str, Any]:
        """Send the order that closes a position - or, with ``qty`` short of the whole position, the
        part of it the exit manager takes off at the first target; ``after_fill`` is the stop and
        target the rest gets once that part has gone (see Repository.reduce_trade). The stop and target
        resting at the broker are stood down first - one an earlier run left too, before this run's first
        pass has taken it over (_take_over_for_exit). An exit that must wait on the broker for that (a cancel
        to confirm, its orders reloading after a connect) comes back ``wait``: not a failed exit, one to try
        again in seconds. So does one the broker didn't answer in time (``sent_unknown`` too): it may be working,
        or have filled, and no other exit goes out for the trade until the order syncs have looked for it at the
        broker by its tag (_look_for_unknown).

        One exit per trade at a time: a close for a trade whose exit another thread is sending this moment comes
        back at once, ``wait`` too - that one's outcome settles it, and one queued behind it would only find its exit
        working. The rest runs under the executor's lock, from the first check until the exit is followed."""
        me = threading.get_ident()
        with self._claims_lock:
            sender = self._sending.get(trade_id)
            if sender is None:
                self._sending[trade_id] = me
        if sender not in (None, me):
            return {"ok": False, "wait": True, "reason": "An exit for this position is already being sent"}
        try:
            with self._lock:
                return self._close_trade(trade_id, reason, limit_price, qty, after_fill, decision_price)
        finally:
            if sender is None:          # (a close within a close, on one thread, leaves it to the outer one)
                with self._claims_lock:
                    self._sending.pop(trade_id, None)

    def _close_trade(self, trade_id: str, reason: str, limit_price: Optional[float], qty: Optional[float],
                     after_fill: Optional[Dict[str, float]], decision_price: Optional[float]) -> Dict[str, Any]:
        """close_trade's checks and its exit, under the executor's lock."""
        t = self.repo.get_trade(trade_id)
        if not t or t["status"] == "CLOSED":
            return {"ok": False, "reason": "trade not open"}
        held_on = t.get("broker") or "paper"
        if held_on != self.venue:
            # never send an exit to an account that doesn't hold the position
            return {"ok": False, "reason": f"This position is on {venue_label(held_on)} - "
                                           f"switch back to that platform to close it."}
        if exit_tag(trade_id) in self._unknown:
            # the last exit got no answer in time: it may be working, or have filled - nothing more goes out until the
            # order syncs have looked for it at the broker (_look_for_unknown)
            return {"ok": False, "wait": True, "sent_unknown": True,
                    "reason": f"The last exit for this {t['symbol']} position got no answer from the broker in time - "
                              f"it is being looked for there before another is sent."}
        if trade_id in self.pending_exit_trade_ids():
            return {"ok": False, "reason": f"An exit order for this {t['symbol']} position is already working."}
        if self.fill_unbooked(trade_id):
            return self._wait_for_booking(t["symbol"])
        closed = self._exchange_closed()
        if closed:
            # a market exit would be rejected, and standing the stop down for it would leave the position
            # with nothing at the broker - so nothing is touched until the session opens
            return {"ok": False, "market_closed": True, "reason": closed}
        # one caller at a time stands the trade's resting orders down, and the order sync leaves them alone until the
        # exit is placed or refused. (Claims are taken only under the executor's lock, which this holds, so no other
        # thread holds one now; the short wait stays as a guard)
        if not self._claim_resting(trade_id, wait_s=self.STAND_DOWN_S):
            return {"ok": False, "wait": True,
                    "reason": f"An exit for this {t['symbol']} position is already being sent, or its orders at the "
                              f"broker are being changed this moment - try again in a few seconds."}
        try:
            # whoever held the trade's orders while this close waited for them may have sent its exit, or booked the
            # stop's fill: the record and the exits working are read again before anything goes to the broker
            fresh = self.repo.get_trade(trade_id)
            if not fresh:
                return {"ok": False, "reason": "trade not open"}
            if fresh["status"] == "CLOSED":
                return {"ok": True, "status": "FILLED", "trade": fresh}
            if trade_id in self.pending_exit_trade_ids():
                return {"ok": False,
                        "reason": f"An exit order for this {t['symbol']} position is already working."}
            try:
                return self._send_exit(fresh, reason, limit_price, qty, after_fill, decision_price)
            except BookingFailed:
                # what one of its resting orders filled couldn't be saved: followed again and booked on the next pass,
                # when the record says what is left to sell - the exit waits (a wait, not a failed try)
                return self._wait_for_booking(t["symbol"])
        finally:
            self._release_resting(trade_id)

    @staticmethod
    def _wait_for_booking(symbol: str) -> Dict[str, Any]:
        """close_trade's answer while a fill of the trade's resting orders waits to be saved (fill_unbooked)."""
        return {"ok": False, "wait": True,
                "reason": f"A fill at the broker for this {symbol} position couldn't be saved yet - it is booked on "
                          f"the next order sync, before any exit goes out."}

    def _send_exit(self, t: Dict[str, Any], reason: str, limit_price: Optional[float], qty: Optional[float],
                   after_fill: Optional[Dict[str, float]], decision_price: Optional[float]) -> Dict[str, Any]:
        """close_trade's work once it holds the trade's resting orders: stand them down, then send the exit."""
        trade_id, held_on = t["id"], t.get("broker") or "paper"
        working = self._working_or_none()
        # a stop or target an earlier run left, not taken over yet (just after a start), is stood down like this run's
        wait = self._take_over_for_exit(t, working)
        if wait:
            fresh = self.repo.get_trade(trade_id)
            if not fresh or fresh["status"] == "CLOSED":     # one filled while the app was off, for all of it
                return {"ok": True, "status": "FILLED", "trade": fresh or t, "by": "broker-stop"}
            return {"ok": False, "wait": True, "reason": wait}
        if working is None:
            # not known is not "none working": an exit an earlier run left, or one placed by hand, can't be
            # seen - the exits this run is following and the shares the broker holds still cap this one
            log.warning("order list unavailable - the exit for %s goes out without checking %s for one already "
                        "working", trade_id, venue_label(held_on))
            working = []
        order = _match_exit(t, working, set(self._pending))
        if order is not None:
            # an earlier run of the app already sent this exit - follow it rather than send another
            self._track_exit(t, order, reason)
            log.warning("exit for %s is already working at the broker (order %s) - following it", trade_id,
                        order.order_id)
            return {"ok": True, "status": order.status, "order_id": order.order_id, "adopted": True}
        wanted = abs(float(t["quantity"]))
        partial = qty is not None and 0 < float(qty) < wanted - 1e-9
        if partial and trade_id in self._targets:
            # the target resting at the broker takes this part off at that price - one an earlier run left, just
            # taken over, too: an exit of the app's own beside it would sell the same shares twice
            return {"ok": False, "wait": True,
                    "reason": "The target resting at the broker takes this part off - it is left to the target."}
        # the stop resting at the broker first: two exits on one position must never both fill
        if partial:
            if not self._shrink_stop(trade_id, wanted - float(qty)):
                return {"ok": False, "wait": True,
                        "reason": "The stop at the broker couldn't be resized for the part coming off - "
                                  "trying again shortly."}
        else:
            stood = self._stand_down(trade_id)
            if stood == "busy":
                return {"ok": False, "wait": True,
                        "reason": "Waiting for the broker to confirm the protective stop is cancelled "
                                  "before sending the exit."}
            if stood in ("filled", "cancelled"):
                t = self.repo.get_trade(trade_id) or t      # the stop, or part of it, may have filled first
                if stood == "filled" or t["status"] == "CLOSED":
                    return {"ok": True, "status": "FILLED", "trade": t, "by": "broker-stop"}
                wanted = abs(float(t["quantity"]))
        qty = min(float(qty), wanted) if partial else wanted
        held = self._held_quantity(t["symbol"])
        if held is not None:
            if abs(held) < 1e-9 or (held > 0) != (t["side"] == "LONG"):
                # closed or removed outside the app - an exit now would open the other side
                return {"ok": False, "not_held": True,
                        "reason": f"{venue_label(held_on)} doesn't show a {t['side'].lower()} {t['symbol']} "
                                  f"position (closed or removed outside the app?) - no exit sent."}
            # never sell more than is there, counting exits already working on the same shares -
            # the app's own, and any other closing orders at the broker (a close of shares without a record too)
            qty = min(qty, abs(held) - self._closing_quantity(t["symbol"], _exit_side(t["side"]), working))
            if qty <= 0:
                return {"ok": False, "reason": f"Exit orders already working cover all {abs(held):,.0f} "
                                               f"{t['symbol']} shares held - no exit sent."}
        req = build_exit_order(t["symbol"], t["side"], qty,
                               limit_price=limit_price, cfg=self.cfg, tag=exit_tag(trade_id))
        sent_at = dt.datetime.now(dt.timezone.utc)
        try:
            res = self.broker.place_order(req)
        except OUTCOME_UNKNOWN as e:
            # it may be working, or have filled: nothing is followed for it (there is no order id) and nothing more
            # goes out for the trade until the order syncs have looked for it at the broker by its tag - the exit
            # manager waits meanwhile (a wait, not a failed try)
            why = str(e) or "no answer from the broker in time"
            self._audit("PLACE", req, {"error": why, "outcome": "unknown"}, ok=False, trade_id=trade_id, msg=why,
                        ts=sent_at)
            self._unknown[req.client_tag or exit_tag(trade_id)] = (
                _Pending("", Play(**_min_play(t)), "exit", trade_id=trade_id, qty=qty, reason=reason,
                         partial=partial, after_fill=after_fill, decision_price=decision_price, submitted_at=sent_at),
                time.monotonic())
            log.warning("EXIT NOT CONFIRMED  %s %s x%s (%s): %s - looked for at %s by its tag before another is sent",
                        t["symbol"], trade_id, qty, reason, why, venue_label(held_on))
            # the dashboard is told now: the exit manager, the stop placing and the missing-stop warning all leave a
            # trade with an exit that may be working to it - and a whole exit has stood the stop at the broker down
            bare = self._stop_gone(trade_id)
            self.bus.publish("order.unconfirmed", kind="exit", symbol=t["symbol"], trade_id=trade_id, play_id=None,
                             minutes=0.0, bare=bare,
                             msg=f"The {t['symbol']} exit ({reason}) got no answer from the broker in time - it may be "
                                 f"working. The app looks for it at {venue_label(held_on)} before sending another"
                                 + (", and until then the position has no stop there." if bare else "."))
            return {"ok": False, "wait": True, "sent_unknown": True,
                    "reason": f"{why}. The exit may be working - the app looks for it at the broker before sending "
                              f"another."}
        except BrokerError as e:
            self._audit("PLACE", req, {"error": str(e)}, ok=False, trade_id=trade_id, msg=str(e), ts=sent_at)
            return {"ok": False, "reason": str(e)}
        self._audit("PLACE", req, res, ok=True, trade_id=trade_id, ts=sent_at)

        if res.status == "FILLED" or res.filled_qty > 0:
            px = res.avg_fill_price or (res.fills[-1].price if res.fills else limit_price)
            try:
                out, closed = self._book_exit(t["symbol"], trade_id, float(px), res.filled_qty or qty, reason,
                                              partial, after_fill, decision_price, submitted_at=sent_at,
                                              commission=order_fees(res), order_id=res.order_id)
            except BookingFailed:
                pass            # followed below instead: the next order sync reads the fill and books it then
            else:
                return {"ok": True, "status": "FILLED", "trade": out, "reduced": not closed}

        p = self._pending[res.order_id] = _Pending(res.order_id, Play(**_min_play(t)), "exit",
                                                   trade_id=trade_id, qty=qty, reason=reason,
                                                   partial=partial, after_fill=after_fill,
                                                   decision_price=decision_price, submitted_at=sent_at)
        if res.status in DONE_STATUSES:
            p.left_seen = 0.0           # filled at once, its booking refused: those shares are out of the account
        elif res.filled_qty > 0 and float(res.submitted_qty or 0.0) > 0:
            p.left_seen = min(qty, _remaining(res))
        return {"ok": True, "status": res.status or "WORKING", "order_id": res.order_id}

    def _book_exit(self, symbol: str, trade_id: str, price: float, qty: float, reason: str,
                   partial: bool = False, after_fill: Optional[Dict[str, float]] = None,
                   decision_price: Optional[float] = None, submitted_at: Optional[dt.datetime] = None,
                   commission: float = 0.0, order_id: str = ""):
        """Book an exit fill: the whole position closes the record, part of it (the scale-out)
        reduces it. ``commission``: the fees the broker has reported for it so far; ``order_id``: the order
        that filled, which a fee reported later is put down to (_top_up_fees). Returns (the record, whether
        it is now closed). Raises BookingFailed when the database refuses it - the caller keeps the order
        followed, and books it again on the next pass."""
        key = f"exit:{trade_id}"
        paid = {"commission": float(commission or 0.0), "broker_order_id": str(order_id or "")}
        try:
            if partial:
                out = self.repo.reduce_trade(trade_id, float(qty), float(price), exit_reason=reason,
                                             **(after_fill or {}), **paid)
            else:
                seen = {"decision_price": float(decision_price)} if decision_price else {}
                if submitted_at is not None:
                    seen["submitted_at"] = submitted_at          # an exit the app sent: how long it took to fill
                out = self.repo.close_trade(trade_id, float(price), exit_reason=reason, **seen, **paid)
        except Exception as e:  # noqa: BLE001 - the database busy, say
            raise self._booking_failed(key, symbol, "exit", e) from e
        self._booked(key, symbol, "exit")
        if partial and out and out.get("status") == "OPEN":
            self.bus.publish("trade.reduced", trade=out, reason=reason, qty=qty, price=round(price, 4))
            return out, False
        self._open_by_symbol.pop(symbol, None)
        self.bus.publish("trade.closed", trade=out, reason=reason)
        return out, True

    @staticmethod
    def _session_now() -> "clock.Session":
        return clock.current_session()

    def _exchange_closed(self) -> Optional[str]:
        """Why an exit can't be sent right now, on a venue that only fills in the regular session."""
        if getattr(self.broker, "name", "") != "ibkr" or self._session_now() is clock.Session.REGULAR:
            return None
        return ("The market is closed, so an exit can't fill now. The position keeps its stop order at the broker; "
                "the exit goes out when the regular session opens.")

    def cancel_exits(self, reasons=("quit", "exit")) -> int:
        """Call off the app's own exit orders still working that were sent for ``reasons`` - the
        exits a quit sent ("exit" is what one taken over after a restart is called). After the
        close IBKR holds a market exit for the next open; stopping the quit must not leave it
        there to sell the position on Monday."""
        n = 0
        with self._lock:
            for oid, p in list(self._pending.items()):
                if p.kind == "exit" and p.reason in reasons:
                    self._cancel_quietly(oid, "the quit that sent it was stopped", p)
                    n += 1
        return n

    def cancel_working_orders(self) -> Dict[str, int]:
        """Cancel every order working at the broker - entries, the app's own exits, anything else on
        the account - except the stops protecting open positions: those go when their position
        does. A cancelled order's fills, if it had any, are booked when the broker reports it."""
        with self._lock:                # an order being placed is followed first, and a stop placed now is kept
            counts = {"entries": 0, "exits": 0, "others": 0, "stops_kept": len(self._stops)}
            for oid, p in list(self._pending.items()):
                self._cancel_quietly(oid, "every working order cancelled", p)
                counts["entries" if p.kind == "entry" else "exits"] += 1
            counts["stops_kept"] += len(self._targets)   # a target resting with a stop is part of the same protection
            followed = set(self._pending) | {s.order_id for book in (self._stops, self._targets)
                                             for s in book.values()}
            for o in self._working_at_broker():
                if o.order_id in followed:
                    continue
                if o.tag.startswith((STOP_TAG, TARGET_TAG)):
                    counts["stops_kept"] += 1
                    continue
                self._cancel_quietly(o.order_id, "every working order cancelled", o)
                counts["others"] += 1
        log.warning("cancelled the working orders: %s", counts)
        return counts

    def _held_quantity(self, symbol: str) -> Optional[float]:
        """Signed quantity the broker reports for ``symbol`` (0.0 if none), or
        None when it can't say - then the exit goes ahead, as a missed stop is worse."""
        try:
            pos = self.broker.get_account().position(symbol)
        except Exception:  # noqa: BLE001
            return None
        return float(pos.quantity) if pos is not None else 0.0

    def _exiting_quantity(self, symbol: str) -> float:
        """Shares of ``symbol`` the exits followed here were sent to sell (or cover) - all of each: the records they
        close count them until their fills are booked, so this is what is weighed against the records."""
        return sum(p.qty for p in list(self._pending.values()) if p.kind == "exit" and p.play.symbol == symbol)

    def _exits_left(self, symbol: str) -> float:
        """Shares of ``symbol`` the exits followed here still have to sell (or cover) - what is weighed against the
        shares held. What the broker has said they filled is out of the account already: an exit that filled whose
        booking the database has refused so far sells nothing more, nor does the part filled of one still working.
        Counted again, those shares would leave another record of the stock with neither a stop nor an exit."""
        return sum(p.qty if p.left_seen is None else min(p.qty, p.left_seen) for p in list(self._pending.values())
                   if p.kind == "exit" and p.play.symbol == symbol)

    def _closing_quantity(self, symbol: str, side: Side, working: List[OrderResult]) -> float:
        """Shares of ``symbol`` still to be sold (or bought back: ``side``) by closing orders - the exits followed here,
        and the other orders in ``working`` that may be closing a position: an exit an earlier run left, the close of
        shares without a record, one sent by hand. An exit is capped at the shares held less these, and a stop (sized
        from its record) rests only where they still cover it (_place_stop)."""
        return self._exits_left(symbol) + _closing_left(working, symbol, side, skip=set(self._pending))

    # ------------------------------------------------------------------ #
    def sync_open_orders(self, wait: bool = True) -> bool:
        """Poll the broker for fills on anything we're tracking. Also drives
        the paper broker's internal clock and detects bracket stop/target hits.

        One pass at a time, under the executor's lock: two passes side by side (the sync loop's and the
        dashboard's Refresh) could each book the same fill. With ``wait`` False a pass is skipped, and
        False returned, while another caller holds the lock - the sync loop mid-pass, which covers it."""
        if not self._lock.acquire(blocking=wait):
            return False
        try:
            self._sync_pass()
        finally:
            self._lock.release()
        return True

    def _sync_pass(self) -> None:
        """One order sync, under the executor's lock."""
        # 0) take over the orders an earlier run left working, if the broker couldn't list them before
        if self._adopt_due:
            try:
                self.adopt_working_orders()
            except Exception:  # noqa: BLE001
                log.exception("taking over the orders already working failed")
        # ...and, once they are, book the entries it sent that filled while the app was off
        if self._entries_due and not self._adopt_due:
            try:
                self._book_entries_filled_while_off()
            except Exception:  # noqa: BLE001
                log.exception("looking for the entries that filled while the app was off failed")
        # ...and look for the orders whose send got no answer in time: followed from here on if they work
        if self._unknown:
            try:
                self._look_for_unknown()
            except Exception:  # noqa: BLE001
                log.exception("looking for the orders the broker didn't answer in time failed")
        # ...and say so of one still not found a while on - whatever holds the look up, every pass
        self._warn_unknown()

        # 1) advance the simulator
        if hasattr(self.broker, "poll"):
            try:
                for changed in self.broker.poll():
                    self._on_order_update(changed)
            except Exception:  # noqa: BLE001
                log.exception("paper poll failed")

        # 2) call off the day-trade entries the price has left behind
        try:
            self.expire_entries()
        except Exception:  # noqa: BLE001
            log.exception("entry time-out check failed")

        # 3) reconcile tracked live orders. Only the broker not answering is skipped quietly (asked again next pass): a
        # booking the database refused has said so and keeps the order followed, and anything else is a fault to log
        for oid in list(self._pending):
            try:
                res = self.broker.get_order(oid)
            except Exception:  # noqa: BLE001
                log.debug("could not read order %s - asked again on the next pass", oid, exc_info=True)
                continue
            try:
                self._on_order_update(res)
            except Exception:  # noqa: BLE001 - one order's fault never stops the rest
                log.exception("following order %s failed", oid)

        # 4) the stops resting at the broker: book the ones that filled, keep the rest in step with the records
        try:
            self._watch_stops()
            self._protect_positions()
        except BookingFailed:
            pass                                        # said as it failed: booked again on the next pass
        except Exception:  # noqa: BLE001
            log.exception("protective stops check failed")

        # 5) detect broker-side bracket exits (child order filled against an open trade)
        try:
            for o in self.broker.list_orders(status="FILLED"):
                self._maybe_close_from_bracket(o)
        except Exception:  # noqa: BLE001
            pass

        # 6) what the broker has said went wrong with the app's orders since the last pass, into the order audit
        self._audit_order_errors()

        # 7) the fees IBKR reported after the fills were booked, onto their records
        try:
            self._top_up_fees()
        except Exception:  # noqa: BLE001
            log.exception("adding the fees the broker reported to the day's fills failed")

    def _look_for_unknown(self) -> List[str]:
        """Look for the orders whose send got no answer in time (OrderOutcomeUnknown) at the broker, by their tag. One
        working is followed from here like any other (one called off meanwhile - a quit, the pairs desk - is cancelled
        as it is found); one whose executions show it filled is booked as that fill (one the database refuses to book
        stays looked for, and is booked on the next pass); one the broker shows neither
        working nor filled UNKNOWN_GIVE_UP_S after the call is taken as never sent: an entry's play is marked ERROR and
        Autopilot's slot handed back, and the trade's exit may go out again. Nothing is decided while the broker's
        orders or executions can't be read, nor while it is still reloading its orders after a connect. Under the
        executor's lock (the order sync). Returns the tags settled."""
        if getattr(self.broker, "is_connected", True) is False or self._broker_resyncing():
            return []
        working = self._working_or_none()
        if working is None:
            return []
        settled: List[str] = []
        for ref, (p, at) in list(self._unknown.items()):
            side = p.play.side if p.kind == "entry" else _exit_side(p.play.side.value)
            order = next((o for o in working if o.tag == ref and o.side is side), None)
            if order is not None:
                del self._unknown[ref]
                settled.append(ref)
                if order.order_id not in self._pending:
                    p.order_id = order.order_id
                    self._pending[order.order_id] = p
                    if p.expired:
                        p.cancel_at = time.monotonic()
                        self._cancel_quietly(order.order_id, p.expired, p)   # its answer books what part filled
                    self._found_unknown(p, f"is working at {venue_label(self.venue)} after all (order "
                                           f"{order.order_id}) - followed" + (", and cancelled" if p.expired else ""))
                continue
            fills = self._executions(p.play.symbol)
            if fills is None:
                continue                                        # not known: looked for again next pass
            mine = [f for f in fills if (getattr(f, "tag", "") or "") == ref and f.side is side
                    and (p.kind == "entry" or self._filled_since(f, p.submitted_at))]
            got, price = shares_and_price(mine)
            if got <= 1e-9 and time.monotonic() - at < self.UNKNOWN_GIVE_UP_S:
                continue                                        # not seen yet: looked for again next pass
            if got <= 1e-9:
                del self._unknown[ref]
                settled.append(ref)
                self._unknown_not_sent(p)
                continue
            p.order_id = str(mine[-1].order_id)
            done = got >= p.qty - 1e-9
            res = OrderResult(order_id=p.order_id, status="FILLED" if done else "CANCELED", symbol=p.play.symbol,
                              submitted_qty=p.qty, filled_qty=got, avg_fill_price=price, commission=fees_of(mine),
                              message="" if done else "it ended with only part of it filled")
            if p.kind == "entry":
                self._booking[id(p)] = p        # listed while it is booked, like a fill heard live (_on_order_update)
            del self._unknown[ref]
            settled.append(ref)
            try:
                if done:
                    self._on_filled(p, res)
                else:
                    self._on_unfilled(p, res)
            except BookingFailed:
                # the database refused it: looked for again on the next pass and booked then - and until it is, it
                # still counts as sent, so nothing goes out in its place
                self._unknown[ref] = (p, at)
                settled.remove(ref)
                continue
            finally:
                self._booking.pop(id(p), None)
            self._found_unknown(p, f"filled at {venue_label(self.venue)} after all (order {p.order_id}): {got:,.0f} "
                                   f"of {p.qty:,.0f} shares @ {price:.4f} - booked")
        return settled

    def _warn_unknown(self, now: Optional[float] = None) -> List[str]:
        """Say so - in the log and on the dashboard (order.unconfirmed), and again every UNPROTECTED_REPEAT_S - of an
        order whose send got no answer in time that is still not found UNPROTECTED_WARN_S after the call. The look for
        it (_look_for_unknown) decides nothing while the broker is disconnected or reloading its orders, or its orders
        or executions can't be read, and nothing goes out in its place meanwhile: the trade of an exit gets no other
        exit - nor a stop at the broker, which a whole exit stood down, and which the stop placing and the missing-stop
        warning leave to the exit - and the shares an entry may have bought have no record and no stop. Every order
        sync, whether or not the broker can be read. Returns the tags said."""
        now = time.monotonic() if now is None else now
        said: List[str] = []
        for ref, (p, at) in list(self._unknown.items()):
            last = self._unknown_said.get(ref)
            last_at = last[1] if last is not None and last[0] is p else float("-inf")   # (a tag an exit used before)
            if now - at < self.UNPROTECTED_WARN_S or now - last_at < self.UNPROTECTED_REPEAT_S:
                continue
            self._unknown_said[ref] = (p, now)
            said.append(ref)
            where, minutes = venue_label(self.venue), (now - at) / 60.0
            bare = p.kind == "exit" and self._stop_gone(p.trade_id or "")
            if p.kind == "exit":
                after = ("The position has no stop at the broker, and no other exit goes out until this one is found"
                         if bare else "No other exit goes out for the position until it is found")
            else:
                after = "If it filled, its shares have no trade record and no stop at the broker until it is found"
            msg = (f"The {p.play.symbol} {p.kind} order the broker didn't answer {minutes:.0f} min ago is still not "
                   f"confirmed - {where} hasn't shown it working, filled or not sent (its orders or executions couldn't "
                   f"be read, or it was reconnecting). {after}. Check it at {where}.")
            log.warning("ORDER NOT CONFIRMED  %s", msg)
            self.bus.publish("order.unconfirmed", kind=p.kind, symbol=p.play.symbol, trade_id=p.trade_id,
                             play_id=p.play.id if p.kind == "entry" else None, minutes=round(minutes, 1), bare=bare,
                             msg=msg)
        for ref in [k for k in self._unknown_said if k not in self._unknown]:
            del self._unknown_said[ref]
        return said

    def _stop_gone(self, trade_id: str) -> bool:
        """Whether a trade that ought to have a stop resting at the broker (stops kept there) has none now."""
        return self.native_stops_on() and trade_id not in self._stops

    def _filled_since(self, f: Any, sent_at: Optional[dt.datetime]) -> bool:
        """Whether an execution came after an exit was sent (CLOCK_SLACK_S allowed for the two clocks)."""
        ts = getattr(f, "ts", None)
        if sent_at is None or not isinstance(ts, dt.datetime):
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return ts >= sent_at - dt.timedelta(seconds=self.CLOCK_SLACK_S)

    def _found_unknown(self, p: _Pending, what: str) -> None:
        msg = f"The {p.play.symbol} {p.kind} order that got no answer in time {what}."
        log.warning("ORDER FOUND  %s", msg)
        self.bus.publish("orders.adopted", adopted=[{"kind": p.kind, "symbol": p.play.symbol, "order_id": p.order_id,
                                                     "trade_id": p.trade_id, "qty": p.qty,
                                                     "play_id": p.play.id if p.kind == "entry" else None}],
                         cancelled=[], msg=msg)

    def _unknown_not_sent(self, p: _Pending) -> None:
        """An order whose send got no answer in time that the broker shows neither working nor filled: it never went
        out (or went and was refused, with nothing filled). An entry bought nothing for certain - its play is marked
        ERROR and Autopilot's slot handed back; the trade's exit may go out again."""
        reason = ("the broker didn't answer the order in time, and shows it neither working nor filled - taken as not "
                  "sent")
        if p.kind == "entry":
            self._note(p.play, PlayStatus.ERROR, {"status": "NOT_SENT", "reason": reason,
                                                  "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
            self._entry_unfilled(p.play.id)
        msg = f"{p.play.symbol} {p.kind} order: {reason}" + (" - the exit goes out again" if p.kind == "exit" else "")
        log.warning("ORDER NOT FILLED  %s", msg)
        self.bus.publish("order.failed", kind=p.kind, order_id="", status="NOT_SENT", symbol=p.play.symbol,
                         trade_id=p.trade_id, play_id=p.play.id if p.kind == "entry" else None, filled_qty=0.0,
                         reason=reason, msg=msg)

    def expire_entries(self, now: Optional[dt.datetime] = None, mono: Optional[float] = None) -> List[str]:
        """Call off the entry orders that shouldn't keep working:

        * a day-trade entry still working after ``execution.entry_timeout_min`` minutes. A limit the
          price hasn't come to by then is one the price left behind, and a fill later - when the price
          comes back through it - is the move failing, not the setup (Aziz: never chase, and never let
          a stale order chase for you). Swing entries keep their DAY life;
        * any entry that filled in part ``execution.partial_entry_wait_s`` seconds ago and is still
          working. Until an order is done the shares it bought have no record, so no stop at the broker;
          cancelling the rest books them and the stop goes on in the same pass. Pair legs are the desk's.

        A cancel that doesn't take is sent again every ``CANCEL_AGAIN_S``. Returns the ids called off;
        the broker's answer books whatever part filled (_on_unfilled)."""
        limit = float(getattr(self.cfg, "entry_timeout_min", 0) or 0)
        wait = float(getattr(self.cfg, "partial_entry_wait_s", 0) or 0)
        now = now or dt.datetime.now(dt.timezone.utc)
        mono = time.monotonic() if mono is None else mono
        out: List[str] = []
        for oid, p in list(self._pending.items()):
            if p.kind != "entry":
                continue
            if p.expired:
                if p.cancel_at and mono - p.cancel_at >= self.CANCEL_AGAIN_S:
                    p.cancel_at = mono
                    self._cancel_quietly(oid, p.expired, p)     # still working: the first cancel didn't take
                continue
            if wait > 0 and p.first_fill_at is not None and mono - p.first_fill_at >= wait and not _pair_leg(p.play):
                p.expired = (f"filled in part and not complete {wait:g} seconds later - the rest is cancelled so "
                             "the shares bought get their stop at the broker")
                label = "ENTRY CUT SHORT"
            elif (limit > 0 and p.submitted_at is not None and p.play.timeframe is Timeframe.INTRADAY
                  and (now - p.submitted_at).total_seconds() / 60.0 >= limit):
                p.expired = f"not filled within {limit:g} minutes - cancelled rather than chase the price"
                label = "ENTRY TIMED OUT"
            else:
                continue
            p.cancel_at = mono
            self._cancel_quietly(oid, p.expired, p)
            log.warning("%s  %s %s order %s: %s", label, p.play.symbol, p.play.side.value, oid, p.expired)
            out.append(oid)
        return out

    def _on_order_update(self, res) -> None:
        """What the broker says of a followed order. One that is done is let go once what it filled is booked: a booking
        the database refuses (BookingFailed) puts it back, so the next pass reads it again and books it then."""
        p = self._pending.get(res.order_id)
        if p is None:
            return
        if res.status == "UNKNOWN":
            if self._broker_resyncing():
                return                                  # it has only just reconnected and is still reloading orders
            p.unseen += 1
            if p.unseen < self.LOST_AFTER_POLLS:
                return
        elif res.status not in DONE_STATUSES:
            p.unseen = 0
            if p.kind == "entry" and float(res.filled_qty or 0.0) > p.filled_seen:
                p.filled_seen, p.avg_seen = float(res.filled_qty), float(res.avg_fill_price or p.avg_seen)
                if p.first_fill_at is None:
                    p.first_fill_at = time.monotonic()  # part of it is bought: expire_entries cuts it short if it stalls
            elif p.kind == "exit" and float(res.submitted_qty or 0.0) > 0:
                p.left_seen = min(p.qty, _remaining(res))   # the part it has sold is out of the account (_exits_left)
            return
        if p.kind == "entry":
            # an entry is listed with the working entries until its record is saved (_sent) - put there before it
            # leaves _pending, so the open risk and the caps, read on other threads, always find it in one or the other
            self._booking[id(p)] = p
        try:
            if self._pending.pop(res.order_id, None) is None:
                return                                  # another pass (the Refresh button's) has just handled it
            if p.kind == "exit":
                p.left_seen = 0.0       # done: it sells nothing more, while its booking waits on the database too
            if res.status == "UNKNOWN" and p.kind == "entry":
                found = self._found_in_executions(p, res)
                if found is None:
                    # the executions couldn't be read: not known is no "none bought" - followed on, and looked for
                    # again once the broker has gone another few polls without knowing it
                    p.unseen = 0
                    self._pending.setdefault(res.order_id, p)
                    return
                res = found
            try:
                if res.status == "FILLED":
                    self._on_filled(p, res)
                else:
                    self._on_unfilled(p, res)
            except BookingFailed:
                self._pending.setdefault(res.order_id, p)  # still followed: booked on the next pass, never lost
        finally:
            self._booking.pop(id(p), None)

    def _found_in_executions(self, p: _Pending, res: OrderResult) -> Optional[OrderResult]:
        """An entry the broker no longer knows, before it is given up: what the broker's executions show it bought
        - it may have filled while the app wasn't following it (the connection was down when it finished, say).
        All of it reads as filled, part of it as the order ended with that part filled. None when the executions
        couldn't be read: the entry isn't given up on that."""
        mine = self._executions_of(p, res.order_id)
        if mine is None:
            return None
        got, price = shares_and_price(mine)
        if got <= float(res.filled_qty or 0.0) + 1e-9:
            return res
        log.warning("ENTRY FOUND IN THE EXECUTIONS  %s order %s is no longer known to the broker, but its executions "
                    "show %s of %s shares bought @ %.4f - booked", p.play.symbol, res.order_id, got, p.qty, price)
        return replace(res, symbol=p.play.symbol, filled_qty=got, avg_fill_price=price, commission=fees_of(mine),
                       status="FILLED" if got >= p.qty - 1e-9 else res.status)

    def _executed(self, p: _Pending, order_id: str) -> Optional[Tuple[float, float]]:
        """The shares, and their average price, the broker's executions show for order ``order_id`` - and, for an
        entry, for any order tagged with its play's id (an entry's tag; an exit's is shared by every exit its trade
        has had). (0, 0) when they show none; None when they can't be read."""
        mine = self._executions_of(p, order_id)
        return None if mine is None else shares_and_price(mine)

    def _executions_of(self, p: _Pending, order_id: str) -> Optional[List[Any]]:
        """The broker's executions _executed adds up; None when they can't be read."""
        fills = self._executions(p.play.symbol)
        if fills is None:
            return None
        side = p.play.side if p.kind == "entry" else _exit_side(p.play.side.value)
        tag = p.play.id if p.kind == "entry" else ""
        return [f for f in fills if f.side is side
                and (str(f.order_id) == str(order_id) or (tag and getattr(f, "tag", "") == tag))]

    def _on_filled(self, p: _Pending, res) -> None:
        px = res.avg_fill_price or (res.fills[-1].price if res.fills else 0.0)
        if not px:
            # finished with no price on the report - an order rebuilt after a reconnect with no executions on it
            px = (self._executed(p, res.order_id) or (0.0, 0.0))[1] or (
                (p.avg_seen or p.play.entry) if p.kind == "entry" else (p.decision_price or 0.0))
        if p.kind == "entry":
            # an adopted entry's clock restarted at the restart (for its time-out) - it isn't when it went out
            self._open_trade(p.play, px, res.filled_qty or p.qty, res.order_id,
                             p.order_type, p.order_session, context=p.context,
                             submitted_at=None if p.adopted else p.submitted_at, decision=p.decision,
                             commission=order_fees(res))
        else:
            self._book_exit(res.symbol, p.trade_id, float(px), res.filled_qty or p.qty, p.reason or "order",
                            p.partial, p.after_fill, p.decision_price, submitted_at=p.submitted_at,
                            commission=order_fees(res), order_id=res.order_id)

    def _on_unfilled(self, p: _Pending, res) -> None:
        """The broker finished an order without filling all of it - rejected,
        cancelled, expired - or no longer knows it. What did fill is booked and the
        reason is published; the exit manager sends an exit again."""
        filled = float(res.filled_qty or 0.0)
        reason = p.expired or res.message or "no reason given"
        if p.kind == "entry" and res.status == "UNKNOWN" and p.filled_seen > filled and not _pair_leg(p.play):
            filled = p.filled_seen      # lost track of: it bought at least what was seen, and those shares need a stop
        if p.kind == "entry":
            if filled > 0:
                px = res.avg_fill_price or (res.fills[-1].price if res.fills else (p.avg_seen or p.play.entry))
                self._open_trade(p.play, px, filled, res.order_id, p.order_type, p.order_session,
                                 context=p.context, submitted_at=p.submitted_at, decision=p.decision,
                                 commission=order_fees(res))
            else:
                self._note(p.play, PlayStatus.CANCELED if res.status in ("CANCELED", "EXPIRED") else PlayStatus.ERROR,
                           {"status": res.status, "reason": reason,
                            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
                # bought nothing for certain: the broker ended it and says none filled. A lost order
                # (UNKNOWN) may have filled while the app wasn't looking, so it keeps its slot
                if res.status in ("CANCELED", "EXPIRED", "REJECTED") and not res.fills:
                    self._entry_unfilled(p.play.id)
        if res.status == "REJECTED":
            self._cancel_quietly(res.order_id, "rejected - an inactive order must stay dead", p)
        what = "is no longer known to the broker" if res.status == "UNKNOWN" else f"was {res.status.lower()}"
        part = f" after {filled:,.0f} of {p.qty:,.0f} shares filled" if filled else ""
        msg = f"{p.play.symbol} {p.kind} order {res.order_id} {what}{part}: {reason}"
        log.warning("ORDER NOT FILLED  %s", msg)
        self.bus.publish("order.failed", kind=p.kind, order_id=res.order_id, status=res.status,
                         symbol=p.play.symbol, trade_id=p.trade_id,
                         play_id=p.play.id if p.kind == "entry" else None, filled_qty=filled,
                         reason=reason, msg=msg)

    def _entry_unfilled(self, play_id: str) -> None:
        hook = self.on_entry_unfilled
        if hook is None:
            return
        try:
            hook(play_id)
        except Exception:  # noqa: BLE001 - never let it stop the order sync
            log.exception("handing back the entry slot of %s failed", play_id)

    def _note(self, play: Play, status: PlayStatus, outcome: Optional[Dict[str, Any]] = None) -> None:
        """What became of a play that was sent, in memory and in the play log - its status (and why)
        only: who decided and when stay as they were, and a play that became a trade stays FILLED."""
        play.status = status
        settle = getattr(self.repo, "settle_play", None)
        if not callable(settle):
            return
        try:
            settle(play.id, status.value, outcome)
        except Exception:  # noqa: BLE001 - the order handling goes on either way
            log.debug("could not save what became of %s", play.id, exc_info=True)

    def _broker_resyncing(self) -> bool:
        since = float(getattr(self.broker, "connected_since", 0.0) or 0.0)
        return since > 0 and time.monotonic() - since < self.RESYNC_GRACE_S

    def _cancel_quietly(self, order_id: str, why: str = "", of: Any = None) -> None:
        """Ask the broker to cancel an order; a failure is logged, never raised. Each ask is audited (CANCEL, with
        ``why``): ok once the broker has taken the ask - its word on it comes later, and a cancel it refuses is an
        ERROR row of its own (_audit_order_errors) - failed, with the reason, when it hasn't. ``of``: what the caller
        holds for the order (a _Pending, a resting _Stop, the broker's OrderResult), which says what it was for -
        else what follows it here says."""
        play_id, trade_id, symbol = self._order_for(order_id, of)
        req, asked_at = {"order_id": order_id, "symbol": symbol, "why": why}, dt.datetime.now(dt.timezone.utc)
        try:
            self.broker.cancel_order(order_id)
        except Exception as e:  # noqa: BLE001
            log.debug("cancel %s failed", order_id, exc_info=True)
            outcome = ("not sent" if isinstance(e, OrderNotSent) else "unknown" if isinstance(e, OUTCOME_UNKNOWN)
                       else "refused")
            self._audit("CANCEL", req, {"error": str(e), "outcome": outcome}, ok=False, play_id=play_id,
                        trade_id=trade_id, msg=f"{why}: {e}" if why else str(e), ts=asked_at)
            return
        self._audit("CANCEL", req, {"status": "sent"}, ok=True, play_id=play_id, trade_id=trade_id, msg=why,
                    ts=asked_at)

    def _order_for(self, order_id: str, of: Any = None) -> Tuple[str, str, str]:
        """The play id, trade id and symbol of an order, for its audit rows: from ``of`` (a _Pending, a _Stop or an
        OrderResult), else from the order followed here under that id. "" for what isn't known."""
        if of is None:
            of = self._pending.get(order_id) or next(
                (o for book in (self._stops, self._targets) for o in list(book.values()) if o.order_id == order_id),
                None)
        if of is None:
            return "", "", ""
        if isinstance(of, _Pending):
            return (of.play.id if of.kind == "entry" else ""), of.trade_id or "", of.play.symbol
        play_id, trade_id = _tag_ids(getattr(of, "tag", "") or "")
        return play_id, getattr(of, "trade_id", "") or trade_id, getattr(of, "symbol", "") or ""

    def _audit_order_errors(self) -> int:
        """Write what the broker has said went wrong with the app's orders since the last look (order_errors:
        IBKR's rejections, the cancels it refused - with the state it names the order in - the cancels it made
        without the app asking, the changes it refused) into the order audit: an ERROR row each, failed, under the
        play or trade the order was for, at the time the broker said it. Returns how many."""
        take = getattr(self.broker, "order_errors", None)
        if not callable(take):
            return 0
        try:
            errors = list(take() or [])
        except Exception:  # noqa: BLE001
            log.debug("could not read the broker's order errors", exc_info=True)
            return 0
        for e in errors:
            oid, tag = str(e.get("order_id") or ""), str(e.get("tag") or "")
            play_id, trade_id = _tag_ids(tag)
            if not (play_id or trade_id):
                play_id, trade_id, _ = self._order_for(oid)
            what, code, text = e.get("what") or "error", e.get("code"), str(e.get("message") or "")
            answer = {"code": code, "message": text, "what": what}
            if "state" in e:
                answer["state"] = e["state"]
            at = e.get("at")
            self._audit("ERROR", {"order_id": oid, "symbol": e.get("symbol") or "", "tag": tag}, answer, ok=False,
                        play_id=play_id, trade_id=trade_id, msg=f"{what} ({code}): {text}",
                        ts=at if isinstance(at, dt.datetime) else None)
        return len(errors)

    def _top_up_fees(self, now: Optional[float] = None) -> List[str]:
        """Put the fees IBKR reports on the day's fills booked before it had reported all of them: its commission
        report for each execution comes a moment after it, so a fill is mostly booked with no fee, or part of it. What
        each fill's order has paid by now, by the broker's executions - shared by shares among the fills booked from
        that order - beyond what the fill was booked with goes on the record (add_fill_fees: the fill, the trade's
        fees, and a closed trade's P/L, % and R). Every FEES_CHECK_S on an IBKR venue, while the day has fills not yet
        settled: a fill is settled at the first look FEES_WAIT_S or more after its booking (every report is in by then;
        an account charged nothing has none to send). A read that failed is tried again on the next look. Under the
        executor's lock (the order sync), so nothing is booked meanwhile. Returns the trades whose fees changed."""
        now = time.monotonic() if now is None else now
        if (not str(self.venue).startswith("ibkr") or getattr(self.broker, "is_connected", True) is False
                or now < self._fees_due_at):
            return []
        find, add = getattr(self.repo, "fills_on", None), getattr(self.repo, "add_fill_fees", None)
        if not (callable(find) and callable(add)):
            return []
        self._fees_due_at = now + self.FEES_CHECK_S
        rows = [r for r in find(self.venue, clock.now_ny().date()) if r["fill_id"] not in self._fees_settled]
        if not rows:
            return []
        executions = self._executions(None)
        if executions is None:
            return []
        reported: Dict[Tuple[str, str], List[Any]] = {}
        for f in executions:
            reported.setdefault((str(f.order_id), f.symbol), []).append(f)
        booked: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for r in rows:
            booked.setdefault((str(r["order_id"]), r["symbol"]), []).append(r)
        settled = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(seconds=self.FEES_WAIT_S)
        fees: Dict[int, float] = {}
        for key, fills in booked.items():
            mine = reported.get(key, [])
            if mine:
                # what the order has paid, by the shares each fill booked from it holds - never more than all of it.
                # A fee only grows: an order the executions show in part never takes back what was booked
                total = fees_of(mine)
                shares = max(sum(float(f.quantity) for f in mine), sum(r["quantity"] for r in fills))
                for r in fills:
                    more = round(total * r["quantity"] / shares - r["commission"], 6)
                    if more > 1e-6:
                        fees[r["fill_id"]] = more
            self._fees_settled.update(r["fill_id"] for r in fills if r["ts"] is not None and r["ts"] < settled)
        changed = add(fees) if fees else []
        if changed:
            log.info("the fees %s reported added to %d fill(s) of %s", venue_label(self.venue), len(fees),
                     ", ".join(sorted(set(changed))))
        return changed

    def _maybe_close_from_bracket(self, o) -> None:
        sym = o.symbol
        tid = self._open_by_symbol.get(sym)
        if not tid:
            return
        tag = (o.raw or {}).get("client_tag", "")
        if ":TP" in tag or ":SL" in tag:
            reason = "target" if ":TP" in tag else "stop"
            px = o.avg_fill_price or (o.fills[-1].price if o.fills else 0.0)
            if px:
                out = self.repo.close_trade(tid, float(px), exit_reason=reason, commission=order_fees(o),
                                            broker_order_id=str(o.order_id or ""))
                self._open_by_symbol.pop(sym, None)
                self.bus.publish("trade.closed", trade=out, reason=reason)

    # ------------------------------------------------------------------ #
    def _open_trade(self, play: Play, price: float, qty: float, order_id: str,
                    order_type: str = "LIMIT", order_session: str = "REGULAR",
                    context: Optional[Dict[str, Any]] = None,
                    submitted_at: Optional[dt.datetime] = None,
                    decision: Optional[Dict[str, Any]] = None, commission: float = 0.0) -> str:
        """Book an entry fill as an open trade (``commission``: the fees the broker has reported for it so far - one
        reported later is added by _top_up_fees). Raises BookingFailed when the database refuses it - the caller
        keeps the order followed, and books it again on the next pass."""
        seen = {"decision": decision} if decision else {}
        key = f"entry:{play.id}"
        try:
            tid = self.repo.open_trade(play, float(price), float(qty), self.venue, order_id,
                                       commission=float(commission or 0.0),
                                       order_type=order_type, order_session=order_session,
                                       entry_context=context, submitted_at=submitted_at, **seen)
        except Exception as e:  # noqa: BLE001 - the database busy, say
            raise self._booking_failed(key, play.symbol, "entry", e) from e
        self._booked(key, play.symbol, "entry")
        self._open_by_symbol[play.symbol] = tid
        self._left_looked.add(tid)                      # a record this run made: no earlier run left orders for it
        play.status = PlayStatus.FILLED
        play.trade_id = tid
        self.bus.publish("order.filled", kind="entry", trade_id=tid, symbol=play.symbol,
                         price=round(price, 4), qty=qty, play=play.to_row())
        return tid

    def _booking_failed(self, key: str, symbol: str, kind: str, e: BaseException) -> BookingFailed:
        """A fill the database refused to book (busy with another writer, say), called from inside the ``except``: the
        dashboard told the first time (order.unbooked) - and again every UNPROTECTED_REPEAT_S while it keeps failing (a
        full disk, a file another program holds): an entry's shares have no record and no stop meanwhile, and a toast
        is soon gone. Logged with its traceback each time the dashboard is told, and as one line on the tries between
        (one every order sync): a traceback a pass for hours would rotate the log's history away. Returns the
        BookingFailed to raise - its caller keeps the order followed, so the next pass books the fill again."""
        tries = self._unbooked[key] = self._unbooked.get(key, 0) + 1
        why = (str(e).strip().splitlines() or [type(e).__name__])[0][:200]    # the database's first line, not its SQL
        now = time.monotonic()
        if now - self._unbooked_said.get(key, float("-inf")) < self.UNPROTECTED_REPEAT_S:
            log.warning("BOOKING FAILED  the %s %s fill still couldn't be saved (try %d): %s", symbol, kind, tries, why)
        else:
            log.exception("BOOKING FAILED  the %s %s fill couldn't be saved (try %d) - its order stays followed and "
                          "the booking is tried again shortly", symbol, kind, tries)
            self._unbooked_said[key] = now
            after = ("until then its shares have no trade record, nor a stop at the broker" if kind == "entry"
                     else "no other exit goes out for the position meanwhile")
            if tries == 1:
                msg = (f"The {symbol} {kind} fill couldn't be saved ({why}). The app keeps following the order and "
                       f"saves it shortly - {after}.")
            else:
                msg = (f"The {symbol} {kind} fill still couldn't be saved after {tries} tries ({why}). The app keeps "
                       f"trying - {after}" + (". Open positions lists them under Shares without a record, with their "
                                              "own Exit." if kind == "entry" else "."))
            self.bus.publish("order.unbooked", kind=kind, symbol=symbol, reason=why, tries=tries, msg=msg)
        return BookingFailed(f"the {symbol} {kind} fill couldn't be saved: {why}")

    def _booked(self, key: str, symbol: str, kind: str) -> None:
        """A fill booked: one whose booking failed before says so."""
        self._unbooked_said.pop(key, None)
        tries = self._unbooked.pop(key, 0)
        if tries:
            log.warning("BOOKED  the %s %s fill, saved on try %d", symbol, kind, tries + 1)

    def _audit(self, action: str, req: Any, response: Any, ok: bool,
               play_id: str = "", trade_id: str = "", msg: str = "", ts: Optional[dt.datetime] = None) -> None:
        """One row of the order audit: what was asked of the broker (``req``: the OrderRequest, or a dict - a cancel,
        a change, an error's order) and what came back (``response``: the broker's OrderResult - its order id,
        status and message - or a dict). ``ts``: when it happened - when the call went out (the broker's answer, or
        its error on the order, can come back before the row is written), or when the broker said it. The account
        number is taken out of the broker's words, and anything the database can't keep as JSON is kept as text.
        Never raises: the order goes on whether or not its row could be written."""
        try:
            row = [_req_dict(req) if isinstance(req, OrderRequest) else dict(req),
                   response if isinstance(response, dict) else _answer(response), msg]
            text, account = json.dumps(row, default=str), str(getattr(self.broker, "account_id", "") or "")
            request, answer, msg = json.loads(text.replace(account, "<account>") if account else text)
            self.repo.record_order_audit(
                action, request, answer, ok, self.broker.name,
                play_id=play_id, trade_id=trade_id, message=msg, **({"ts": ts} if ts is not None else {}),
            )
        except Exception:  # noqa: BLE001
            log.debug("order audit failed", exc_info=True)


def _req_dict(r: OrderRequest) -> dict:
    return {"symbol": r.symbol, "side": r.side.value, "qty": r.quantity,
            "type": r.order_type.value, "limit": r.limit_price, "stop": r.stop_price,
            "tif": r.tif.value, "is_entry": r.is_entry, "tp": r.take_profit,
            "sl": r.stop_loss, "tag": r.client_tag}


def _answer(res: Any) -> dict:
    """The broker's answer to an order call, for the audit: its order id, status and message - and what it filled,
    the shares and prices it holds the order at, when it says - beside the venue's own details (``raw``)."""
    out = dict(getattr(res, "raw", None) or {})
    out.update(order_id=str(getattr(res, "order_id", "") or ""), status=getattr(res, "status", "") or "",
               message=getattr(res, "message", "") or "")
    filled = float(getattr(res, "filled_qty", 0.0) or 0.0)
    if filled > 0:
        out.update(filled_qty=filled, avg_fill_price=float(getattr(res, "avg_fill_price", 0.0) or 0.0))
    for key, name in (("submitted_qty", "qty"), ("limit_price", "limit"), ("stop_price", "stop")):
        if getattr(res, key, None):
            out[name] = float(getattr(res, key))
    return out


def _tag_ids(tag: str) -> Tuple[str, str]:
    """The play id and trade id an order's tag names: an entry's is its play's id, an exit's, stop's or target's
    ``exit:`` / ``stop:`` / ``tgt:`` and the trade's id. ("", "") for any other."""
    for prefix in ("exit:", STOP_TAG, TARGET_TAG):
        if tag.startswith(prefix):
            return "", tag[len(prefix):]
    return (tag.split(":")[0], "") if tag.startswith("play_") else ("", "")


def _min_play(t: dict) -> dict:
    return dict(symbol=t["symbol"], side=Side(t["side"]), strategy=t["strategy"],
                kind=StrategyKind(t["kind"]), timeframe=Timeframe(t["timeframe"]),
                entry=float(t["entry_price"] or 0), stop=float(t["stop_price"] or 0),
                targets=[float(t["target_price"] or 0)] if t.get("target_price") else [])


def _play_from_row(row: dict) -> Play:
    from ..research.journal import held_for          # the hold its time stop runs on, from the play log

    typical, longest = held_for(row)
    return Play(expected_hold_typical=typical, expected_hold_max=longest,
                symbol=row["symbol"], side=Side(row["side"]), strategy=row["strategy"],
                kind=StrategyKind(row["kind"]), timeframe=Timeframe(row["timeframe"]),
                entry=float(row["entry"] or 0), stop=float(row["stop"] or 0),
                targets=[float(x) for x in (row.get("targets") or [])],
                confidence=float(row.get("confidence") or 0.5), sector=row.get("sector") or "",
                tags=list(row.get("tags") or []), id=row["id"], status=PlayStatus.SUBMITTED)


def _sized_at(play: Play) -> Tuple[float, float]:
    """The price an entry was sized at, and its risk per share there: the play's entry - or, for an entry the last
    look re-priced, its limit, where engine._repriced_check sized it again, leaving the play's entry as it was and
    its risk per share from that limit to the stop. A play never sized (a pair leg, one taken back after a restart)
    is at its entry."""
    base = abs(play.entry - play.stop)
    per_share = float(play.risk_per_share or 0.0) or base
    sign = 1.0 if play.side is Side.LONG else -1.0
    return round(play.entry + sign * (per_share - base), 4), per_share


def _pair_leg(play: Play) -> bool:
    """One leg of a pair trade - the pairs desk works its entry, not the executor's rules."""
    return bool(getattr(play, "pair_id", None)) or "pair-leg" in (getattr(play, "tags", None) or [])


def _remaining(o: OrderResult) -> float:
    return max(0.0, float(o.submitted_qty or 0.0) - float(o.filled_qty or 0.0))


def exit_tag(trade_id: str) -> str:
    """The tag (IBKR's orderRef) on every exit the app sends for a trade."""
    return f"exit:{trade_id}"


def _exit_side(position_side: str) -> Side:
    return Side.SHORT if position_side == "LONG" else Side.LONG


def _may_close(o: OrderResult) -> bool:
    """An order that may be closing a position: one the app tagged as an exit, or as the close
    of shares without a record (``unwind:`` - they may be a record's by the time it fills), or an
    untagged one (sent by hand, or before orders were tagged). Never a bracket's target or stop
    child - that belongs to its entry."""
    return o.tag.startswith(("exit:", "unwind:")) or (not o.tag and not (o.raw or {}).get("parent_id"))


def _closing_left(working: List[OrderResult], symbol: str, side: Side, skip: Optional[set] = None) -> float:
    """Shares of ``symbol`` the orders in ``working`` that may be closing a position (_may_close) on ``side`` still
    have to sell (or buy back) - leaving out the ids in ``skip``."""
    return sum(_remaining(o) for o in working if o.symbol == symbol and o.side is side and _may_close(o)
               and o.order_id not in (skip or ()))


def _match_exit(t: dict, orders: List[OrderResult], taken: set) -> Optional[OrderResult]:
    """The working order that is this trade's exit: tagged with its id, or else an
    untagged closing order for exactly its share count."""
    side, qty = _exit_side(t["side"]), abs(float(t["quantity"]))
    candidates = [o for o in orders if o.order_id not in taken and o.symbol == t["symbol"]
                  and o.side is side and _may_close(o)]
    tagged = next((o for o in candidates if o.tag == f"exit:{t['id']}"), None)
    return tagged or next((o for o in candidates if not o.tag and abs(_remaining(o) - qty) < 1e-6), None)


def _purpose(o: OrderResult) -> str:
    """What an order the app isn't following is for, read from its tag: an entry, an
    exit, the stop or target of a bracket, "app" for an untagged order the app sent,
    or "outside" for one placed by hand or by another program."""
    raw = o.raw or {}
    if raw.get("parent_id"):
        return "stop" if o.order_type in ("STOP", "STOP_LIMIT", "TRAILING_STOP") else "target"
    if o.tag.startswith(STOP_TAG):
        return "stop"
    if o.tag.startswith(TARGET_TAG):
        return "target"
    if o.tag.startswith("exit:"):
        return "exit"
    if o.tag.startswith("play_"):
        return "entry"
    return "app" if raw.get("mine", True) else "outside"
