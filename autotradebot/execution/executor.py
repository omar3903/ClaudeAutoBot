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
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..brokers.base import DONE_STATUSES, BrokerAdapter, BrokerError
from ..brokers.venues import venue_label
from ..core.enums import PlayStatus, Side, StrategyKind, Timeframe
from ..core.eventbus import BUS
from ..core.models import Account, OrderRequest, OrderResult, Play
from ..util import clock
from .order_builder import build_entry_order, build_exit_order, plan_order
from .protective_stops import TAG as STOP_TAG, TARGET_TAG, ProtectiveStops, shares_and_price

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

    def __init__(self, broker: BrokerAdapter, repo, cfg, bus=BUS,
                 venue: Optional[str] = None) -> None:
        self.broker = broker
        self.venue = venue or broker.name      # stamped on trades - see brokers/venues.py
        self.repo = repo
        self.cfg = cfg
        self.bus = bus
        self._pending: Dict[str, _Pending] = {}
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
        self._init_stops()

    def rebind(self, broker: BrokerAdapter, venue: Optional[str] = None) -> None:
        """Point at a different broker (paper <-> live / platform switch).
        In-flight order tracking is broker-specific, so it is dropped; open
        trades in the database are untouched."""
        self.broker = broker
        self.venue = venue or broker.name
        self._pending.clear()
        self._open_by_symbol.clear()
        self._entries_due, self._bound_at = True, dt.datetime.now(dt.timezone.utc)
        self._entries_retry_at = 0.0
        self._init_stops()              # the other venue's stops stay where they are; they are found again by their tags

    def cancel_pending_entries(self) -> int:
        """Cancel entry orders still working at the broker (used when quitting)."""
        n = 0
        for oid, p in list(self._pending.items()):
            if p.kind != "entry":
                continue
            self._cancel_quietly(oid)
            self._pending.pop(oid, None)
            n += 1
        return n

    def flatten_untracked(self, symbol: str, position_side: str, qty: float) -> bool:
        """Close shares the broker holds that have no trade record - what's left when an order filled
        but booking it failed. A market order, audited like any other."""
        return bool(self.close_untracked(symbol, position_side, qty).get("ok"))

    def close_untracked(self, symbol: str, position_side: str, qty: float) -> Dict[str, Any]:
        """Send the market order that closes ``qty`` shares held without a record, and say how it went."""
        req = build_exit_order(symbol, position_side, qty, cfg=self.cfg, tag=f"unwind:{symbol}")
        try:
            res = self.broker.place_order(req)
        except BrokerError as e:
            self._audit("PLACE", req, {"error": str(e)}, ok=False, msg=str(e))
            log.error("could not close %s %s shares that have no trade record: %s", qty, symbol, e)
            return {"ok": False, "reason": str(e)}
        self._audit("PLACE", req, res.raw or {"status": res.status}, ok=True,
                    msg="closing shares without a trade record")
        log.warning("closing %s %s shares held without a trade record (order %s, %s)", qty, symbol, res.order_id,
                    res.status)
        return {"ok": True, "status": res.status or "WORKING", "order_id": res.order_id}

    def forget_open(self, symbol: str) -> None:
        """Drop the note that ``symbol`` is held - its record was closed without an exit going through here."""
        self._open_by_symbol.pop(symbol, None)
        self._swept_at = 0.0            # ...so its stop at the broker goes on the very next pass

    def cancel_entries_for(self, play_id: str) -> int:
        """Call off the entry orders still working for one play - a pair leg whose other leg failed."""
        n = 0
        for oid, p in list(self._pending.items()):
            if p.kind == "entry" and p.play.id == play_id:
                self._cancel_quietly(oid)
                self._pending.pop(oid, None)
                p.play.status = PlayStatus.CANCELED
                n += 1
        return n

    def pending_exit_trade_ids(self) -> set:
        """Trades whose close order is still working at the broker."""
        return {p.trade_id for p in list(self._pending.values()) if p.kind == "exit" and p.trade_id}

    def working_entries(self) -> List[Dict[str, Any]]:
        """Entry orders sent but not filled yet. Anything that limits positions has
        to count these too, or a slow fill gets doubled up."""
        return [{"order_id": oid, "play_id": p.play.id, "symbol": p.play.symbol,
                 "strategy": p.play.strategy, "timeframe": p.play.timeframe.value,
                 "qty": p.qty, "notional": p.play.entry * p.qty,
                 "risk": abs(p.play.entry - p.play.stop) * p.qty}
                for oid, p in list(self._pending.items()) if p.kind == "entry"]

    def symbols_in_flight(self) -> set:
        """Symbols with an order still working - their share counts are about to change."""
        return {p.play.symbol for p in list(self._pending.values())}

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
        working: each order sync tries again until the list comes back."""
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
            play = self._play_for(order) if order.order_id not in self._pending else None
            if play is not None:
                # its clock starts again from here: a day-trade entry gets entry_timeout_min more minutes
                self._pending[order.order_id] = _Pending(order.order_id, play, "entry", qty=_remaining(order),
                                                         submitted_at=dt.datetime.now(dt.timezone.utc), adopted=True)
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
        followed = {p.play.id for p in list(self._pending.values()) if p.kind == "entry"}
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
                found.append((row, side, qty, price, str(mine[-1].order_id)))
        self._entries_due = False
        booked: List[str] = []
        for row, side, qty, price, order_id in found:
            try:
                tid = self._book_entry_filled_while_off(row, side, qty, price, order_id)
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
                                     order_id: str) -> Optional[str]:
        """Book one entry found filled while the app was off. Returns its trade id, "" when the account holds none
        of its shares beyond the records (nothing booked), None when the account couldn't be read."""
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
        tid = self._open_trade(_play_from_row(row), price, take, order_id)
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
            if o.order_id in self._pending or o.symbol not in recorded or o.side is not sides[o.symbol] or not mine:
                continue
            if o.tag.startswith((STOP_TAG, TARGET_TAG)):
                continue                                 # a resting stop or target is not an exit; protective_stops.py owns it
            if self._exiting_quantity(o.symbol) + _remaining(o) > recorded[o.symbol] + 1e-9:
                self._cancel_quietly(o.order_id)
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
        submitted_at = dt.datetime.now(dt.timezone.utc)
        try:
            if native_bracket:
                res = self.broker.place_bracket(entry, None if self.scale_out else play.primary_target, play.stop)
            else:
                res = self.broker.place_order(entry)      # exit manager will protect it
        except BrokerError as e:
            self._audit("PLACE", entry, {"error": str(e)}, ok=False, play_id=play.id, msg=str(e))
            play.status = PlayStatus.ERROR
            return {"ok": False, "reason": str(e)}

        self._audit("PLACE", entry, res.raw or {"status": res.status}, ok=True,
                    play_id=play.id, msg=res.message)
        play.status = PlayStatus.SUBMITTED
        ot, osess = plan.get("order_type", "LIMIT"), plan.get("order_session", "REGULAR")

        # immediate fill (paper / marketable) -> open the trade now
        if res.status in ("FILLED",) or res.filled_qty >= qty > 0:
            fill_price = res.avg_fill_price or (res.fills[-1].price if res.fills else play.entry)
            tid = self._open_trade(play, fill_price, res.filled_qty or qty, res.order_id, ot, osess,
                                   context=context, submitted_at=submitted_at, decision=decision)
            return {"ok": True, "status": "FILLED", "trade_id": tid,
                    "fill_price": round(fill_price, 4), "qty": res.filled_qty or qty,
                    "order_id": res.order_id, "order_type": ot, "order_session": osess,
                    "bracket_mode": plan.get("bracket_mode")}

        # otherwise track it; sync_open_orders() will pick up the fill. The play log says it went out
        # before the sync loop can hear how it ended, so the ending is never overwritten by this
        p = _Pending(res.order_id, play, "entry", qty=qty)
        p.order_type, p.order_session = ot, osess
        p.context, p.submitted_at, p.decision = context, submitted_at, decision
        self._note(play, PlayStatus.SUBMITTED)
        self._pending[res.order_id] = p
        return {"ok": True, "status": res.status or "WORKING", "order_id": res.order_id,
                "order_type": ot, "order_session": osess,
                "note": "order working - will confirm on fill"}

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
        again in seconds."""
        t = self.repo.get_trade(trade_id)
        if not t or t["status"] == "CLOSED":
            return {"ok": False, "reason": "trade not open"}
        held_on = t.get("broker") or "paper"
        if held_on != self.venue:
            # never send an exit to an account that doesn't hold the position
            return {"ok": False, "reason": f"This position is on {venue_label(held_on)} - "
                                           f"switch back to that platform to close it."}
        if trade_id in self.pending_exit_trade_ids():
            return {"ok": False, "reason": f"An exit order for this {t['symbol']} position is already working."}
        closed = self._exchange_closed()
        if closed:
            # a market exit would be rejected, and standing the stop down for it would leave the position
            # with nothing at the broker - so nothing is touched until the session opens
            return {"ok": False, "market_closed": True, "reason": closed}
        # one caller at a time stands the trade's resting orders down - a manual close or a quit can come from
        # another thread - and the order sync leaves them alone until the exit is placed or refused (and holds them
        # itself for the moment it places, moves or books one: a close waits a moment for that)
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
            return self._send_exit(fresh, reason, limit_price, qty, after_fill, decision_price)
        finally:
            self._release_resting(trade_id)

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
            # the app's own, and any other closing orders at the broker
            untracked = sum(_remaining(o) for o in working if o.order_id not in self._pending
                            and o.symbol == t["symbol"] and o.side is _exit_side(t["side"]) and _may_close(o))
            qty = min(qty, abs(held) - self._exiting_quantity(t["symbol"]) - untracked)
            if qty <= 0:
                return {"ok": False, "reason": f"Exit orders already working cover all {abs(held):,.0f} "
                                               f"{t['symbol']} shares held - no exit sent."}
        req = build_exit_order(t["symbol"], t["side"], qty,
                               limit_price=limit_price, cfg=self.cfg, tag=f"exit:{trade_id}")
        sent_at = dt.datetime.now(dt.timezone.utc)
        try:
            res = self.broker.place_order(req)
        except BrokerError as e:
            self._audit("PLACE", req, {"error": str(e)}, ok=False, trade_id=trade_id, msg=str(e))
            return {"ok": False, "reason": str(e)}
        self._audit("PLACE", req, res.raw or {"status": res.status}, ok=True, trade_id=trade_id)

        if res.status == "FILLED" or res.filled_qty > 0:
            px = res.avg_fill_price or (res.fills[-1].price if res.fills else limit_price)
            out, closed = self._book_exit(t["symbol"], trade_id, float(px), res.filled_qty or qty, reason,
                                          partial, after_fill, decision_price, submitted_at=sent_at)
            return {"ok": True, "status": "FILLED", "trade": out, "reduced": not closed}

        self._pending[res.order_id] = _Pending(res.order_id, Play(**_min_play(t)), "exit",
                                               trade_id=trade_id, qty=qty, reason=reason,
                                               partial=partial, after_fill=after_fill,
                                               decision_price=decision_price, submitted_at=sent_at)
        return {"ok": True, "status": res.status or "WORKING", "order_id": res.order_id}

    def _book_exit(self, symbol: str, trade_id: str, price: float, qty: float, reason: str,
                   partial: bool = False, after_fill: Optional[Dict[str, float]] = None,
                   decision_price: Optional[float] = None, submitted_at: Optional[dt.datetime] = None):
        """Book an exit fill: the whole position closes the record, part of it (the scale-out)
        reduces it. Returns (the record, whether it is now closed)."""
        if partial:
            out = self.repo.reduce_trade(trade_id, float(qty), float(price), exit_reason=reason,
                                         **(after_fill or {}))
            if out and out.get("status") == "OPEN":
                self.bus.publish("trade.reduced", trade=out, reason=reason, qty=qty, price=round(price, 4))
                return out, False
        else:
            seen = {"decision_price": float(decision_price)} if decision_price else {}
            if submitted_at is not None:
                seen["submitted_at"] = submitted_at          # an exit the app sent: how long it took to fill
            out = self.repo.close_trade(trade_id, float(price), exit_reason=reason, **seen)
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
        for oid, p in list(self._pending.items()):
            if p.kind == "exit" and p.reason in reasons:
                self._cancel_quietly(oid)
                n += 1
        return n

    def cancel_working_orders(self) -> Dict[str, int]:
        """Cancel every order working at the broker - entries, the app's own exits, anything else on
        the account - except the stops protecting open positions: those go when their position
        does. A cancelled order's fills, if it had any, are booked when the broker reports it."""
        counts = {"entries": 0, "exits": 0, "others": 0, "stops_kept": len(self._stops)}
        for oid, p in list(self._pending.items()):
            self._cancel_quietly(oid)
            counts["entries" if p.kind == "entry" else "exits"] += 1
        counts["stops_kept"] += len(self._targets)       # a target resting with a stop is part of the same protection
        followed = set(self._pending) | {s.order_id for book in (self._stops, self._targets) for s in book.values()}
        for o in self._working_at_broker():
            if o.order_id in followed:
                continue
            if o.tag.startswith((STOP_TAG, TARGET_TAG)):
                counts["stops_kept"] += 1
                continue
            self._cancel_quietly(o.order_id)
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
        """Shares of ``symbol`` that exit orders still working are already selling (or covering)."""
        return sum(p.qty for p in list(self._pending.values()) if p.kind == "exit" and p.play.symbol == symbol)

    # ------------------------------------------------------------------ #
    def sync_open_orders(self) -> None:
        """Poll the broker for fills on anything we're tracking. Also drives
        the paper broker's internal clock and detects bracket stop/target hits."""
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

        # 3) reconcile tracked live orders
        for oid in list(self._pending):
            try:
                self._on_order_update(self.broker.get_order(oid))
            except Exception:  # noqa: BLE001
                continue

        # 4) the stops resting at the broker: book the ones that filled, keep the rest in step with the records
        try:
            self._watch_stops()
            self._protect_positions()
        except Exception:  # noqa: BLE001
            log.exception("protective stops check failed")

        # 5) detect broker-side bracket exits (child order filled against an open trade)
        try:
            for o in self.broker.list_orders(status="FILLED"):
                self._maybe_close_from_bracket(o)
        except Exception:  # noqa: BLE001
            pass

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
                    self._cancel_quietly(oid)           # still working: the first cancel didn't take
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
            self._cancel_quietly(oid)
            log.warning("%s  %s %s order %s: %s", label, p.play.symbol, p.play.side.value, oid, p.expired)
            out.append(oid)
        return out

    def _on_order_update(self, res) -> None:
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
            return
        if self._pending.pop(res.order_id, None) is None:
            return                                      # another pass (the Refresh button's) has just handled it
        if res.status == "UNKNOWN" and p.kind == "entry":
            found = self._found_in_executions(p, res)
            if found is None:
                # the executions couldn't be read: not known is no "none bought" - followed on, and looked for again
                # once the broker has gone another few polls without knowing it
                p.unseen = 0
                self._pending.setdefault(res.order_id, p)
                return
            res = found
        if res.status == "FILLED":
            self._on_filled(p, res)
        else:
            self._on_unfilled(p, res)

    def _found_in_executions(self, p: _Pending, res: OrderResult) -> Optional[OrderResult]:
        """An entry the broker no longer knows, before it is given up: what the broker's executions show it bought
        - it may have filled while the app wasn't following it (the connection was down when it finished, say).
        All of it reads as filled, part of it as the order ended with that part filled. None when the executions
        couldn't be read: the entry isn't given up on that."""
        executed = self._executed(p, res.order_id)
        if executed is None:
            return None
        got, price = executed
        if got <= float(res.filled_qty or 0.0) + 1e-9:
            return res
        log.warning("ENTRY FOUND IN THE EXECUTIONS  %s order %s is no longer known to the broker, but its executions "
                    "show %s of %s shares bought @ %.4f - booked", p.play.symbol, res.order_id, got, p.qty, price)
        return replace(res, symbol=p.play.symbol, filled_qty=got, avg_fill_price=price,
                       status="FILLED" if got >= p.qty - 1e-9 else res.status)

    def _executed(self, p: _Pending, order_id: str) -> Optional[Tuple[float, float]]:
        """The shares, and their average price, the broker's executions show for order ``order_id`` - and, for an
        entry, for any order tagged with its play's id (an entry's tag; an exit's is shared by every exit its trade
        has had). (0, 0) when they show none; None when they can't be read."""
        fills = self._executions(p.play.symbol)
        if fills is None:
            return None
        side = p.play.side if p.kind == "entry" else _exit_side(p.play.side.value)
        tag = p.play.id if p.kind == "entry" else ""
        return shares_and_price([f for f in fills if f.side is side
                                 and (str(f.order_id) == str(order_id) or (tag and getattr(f, "tag", "") == tag))])

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
                             submitted_at=None if p.adopted else p.submitted_at, decision=p.decision)
        else:
            self._book_exit(res.symbol, p.trade_id, float(px), res.filled_qty or p.qty, p.reason or "order",
                            p.partial, p.after_fill, p.decision_price, submitted_at=p.submitted_at)

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
                                 context=p.context, submitted_at=p.submitted_at, decision=p.decision)
            else:
                self._note(p.play, PlayStatus.CANCELED if res.status in ("CANCELED", "EXPIRED") else PlayStatus.ERROR,
                           {"status": res.status, "reason": reason,
                            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
                # bought nothing for certain: the broker ended it and says none filled. A lost order
                # (UNKNOWN) may have filled while the app wasn't looking, so it keeps its slot
                if res.status in ("CANCELED", "EXPIRED", "REJECTED") and not res.fills:
                    self._entry_unfilled(p.play.id)
        if res.status == "REJECTED":
            self._cancel_quietly(res.order_id)          # an inactive order must stay dead
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

    def _cancel_quietly(self, order_id: str) -> None:
        try:
            self.broker.cancel_order(order_id)
        except Exception:  # noqa: BLE001
            log.debug("cancel %s failed", order_id, exc_info=True)

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
                out = self.repo.close_trade(tid, float(px), exit_reason=reason)
                self._open_by_symbol.pop(sym, None)
                self.bus.publish("trade.closed", trade=out, reason=reason)

    # ------------------------------------------------------------------ #
    def _open_trade(self, play: Play, price: float, qty: float, order_id: str,
                    order_type: str = "LIMIT", order_session: str = "REGULAR",
                    context: Optional[Dict[str, Any]] = None,
                    submitted_at: Optional[dt.datetime] = None,
                    decision: Optional[Dict[str, Any]] = None) -> str:
        seen = {"decision": decision} if decision else {}
        tid = self.repo.open_trade(play, float(price), float(qty), self.venue, order_id,
                                   order_type=order_type, order_session=order_session,
                                   entry_context=context, submitted_at=submitted_at, **seen)
        self._open_by_symbol[play.symbol] = tid
        self._left_looked.add(tid)                      # a record this run made: no earlier run left orders for it
        play.status = PlayStatus.FILLED
        play.trade_id = tid
        self.bus.publish("order.filled", kind="entry", trade_id=tid, symbol=play.symbol,
                         price=round(price, 4), qty=qty, play=play.to_row())
        return tid

    def _audit(self, action: str, req: OrderRequest, response: dict, ok: bool,
               play_id: str = "", trade_id: str = "", msg: str = "") -> None:
        try:
            self.repo.record_order_audit(
                action, _req_dict(req), response, ok, self.broker.name,
                play_id=play_id, trade_id=trade_id, message=msg,
            )
        except Exception:  # noqa: BLE001
            log.debug("order audit failed", exc_info=True)


def _req_dict(r: OrderRequest) -> dict:
    return {"symbol": r.symbol, "side": r.side.value, "qty": r.quantity,
            "type": r.order_type.value, "limit": r.limit_price, "stop": r.stop_price,
            "tif": r.tif.value, "is_entry": r.is_entry, "tp": r.take_profit,
            "sl": r.stop_loss, "tag": r.client_tag}


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


def _pair_leg(play: Play) -> bool:
    """One leg of a pair trade - the pairs desk works its entry, not the executor's rules."""
    return bool(getattr(play, "pair_id", None)) or "pair-leg" in (getattr(play, "tags", None) or [])


def _remaining(o: OrderResult) -> float:
    return max(0.0, float(o.submitted_qty or 0.0) - float(o.filled_qty or 0.0))


def _exit_side(position_side: str) -> Side:
    return Side.SHORT if position_side == "LONG" else Side.LONG


def _may_close(o: OrderResult) -> bool:
    """An order that may be closing a position: one the app tagged as an exit, or an
    untagged one (sent by hand, or before orders were tagged). Never a bracket's
    target or stop child - that belongs to its entry."""
    return o.tag.startswith("exit:") or (not o.tag and not (o.raw or {}).get("parent_id"))


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
