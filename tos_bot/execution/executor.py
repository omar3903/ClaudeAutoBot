"""Routes an approved Play to the broker and keeps the trade log in sync.

The engine only calls :meth:`execute_play` after the operator has clicked
"Yes" in the dashboard and the PDT / sizing checks have passed. Nothing
here decides *whether* to trade - only *how*.

Every order sent is followed until the broker finishes it. A fill opens or
closes the trade; a rejection, cancellation or expiry is published with the
broker's reason, and whatever part of the order did fill is booked.

Orders outlive the app: after a restart, the orders an earlier run left working
at the broker are taken over (see :meth:`Executor.adopt_working_orders`), so an
exit is never sent twice.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..brokers.base import DONE_STATUSES, BrokerAdapter, BrokerError
from ..brokers.venues import venue_label
from ..core.enums import PlayStatus, Side, StrategyKind, Timeframe
from ..core.eventbus import BUS
from ..core.models import Account, OrderRequest, OrderResult, Play
from ..util import clock
from .order_builder import build_entry_order, build_exit_order, plan_order

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


class Executor:
    #: polls in a row (the sync loop runs every 4 s) a connected broker may not know an order before it's given up
    LOST_AFTER_POLLS = 5
    #: after the broker (re)connects - IB Gateway's nightly restart - its order list takes a while to reload
    RESYNC_GRACE_S = 60.0

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

    def rebind(self, broker: BrokerAdapter, venue: Optional[str] = None) -> None:
        """Point at a different broker (paper <-> live / platform switch).
        In-flight order tracking is broker-specific, so it is dropped; open
        trades in the database are untouched."""
        self.broker = broker
        self.venue = venue or broker.name
        self._pending.clear()
        self._open_by_symbol.clear()

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
                 "strategy": p.play.strategy, "qty": p.qty, "notional": p.play.entry * p.qty,
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
        trade_id = p.trade_id if followed else (o.tag[len("exit:"):] if o.tag.startswith("exit:") else None)
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
                "message": o.message}

    # ------------------------------------------------------------------ #
    def adopt_working_orders(self) -> List[Dict[str, Any]]:
        """Take over the orders an earlier run of the app left working at the broker,
        so a restart never sends a second exit or loses track of an entry.

        An exit is matched to its open trade by its tag, or else by symbol, direction
        and share count. Extra copies of an exit the app itself sent - more shares
        than the open trades hold - are cancelled. An entry is matched to its play by
        its tag."""
        working = self._working_at_broker()
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
                                                         submitted_at=dt.datetime.now(dt.timezone.utc))
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
        return adopted

    def _working_at_broker(self) -> List[OrderResult]:
        try:
            return [o for o in self.broker.list_orders("WORKING")
                    if o.status not in DONE_STATUSES and o.side is not None]
        except Exception:  # noqa: BLE001
            log.debug("could not list the orders working at the broker", exc_info=True)
            return []

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

        # otherwise track it; sync_open_orders() will pick up the fill
        p = _Pending(res.order_id, play, "entry", qty=qty)
        p.order_type, p.order_session = ot, osess
        p.context, p.submitted_at, p.decision = context, submitted_at, decision
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
        target the rest gets once that part has gone (see Repository.reduce_trade)."""
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
        working = self._working_at_broker()
        order = _match_exit(t, working, set(self._pending))
        if order is not None:
            # an earlier run of the app already sent this exit - follow it rather than send another
            self._track_exit(t, order, reason)
            log.warning("exit for %s is already working at the broker (order %s) - following it", trade_id,
                        order.order_id)
            return {"ok": True, "status": order.status, "order_id": order.order_id, "adopted": True}
        wanted = abs(float(t["quantity"]))
        partial = qty is not None and 0 < float(qty) < wanted - 1e-9
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
        try:
            res = self.broker.place_order(req)
        except BrokerError as e:
            self._audit("PLACE", req, {"error": str(e)}, ok=False, trade_id=trade_id, msg=str(e))
            return {"ok": False, "reason": str(e)}
        self._audit("PLACE", req, res.raw or {"status": res.status}, ok=True, trade_id=trade_id)

        if res.status == "FILLED" or res.filled_qty > 0:
            px = res.avg_fill_price or (res.fills[-1].price if res.fills else limit_price)
            out, closed = self._book_exit(t["symbol"], trade_id, float(px), res.filled_qty or qty, reason,
                                          partial, after_fill, decision_price)
            return {"ok": True, "status": "FILLED", "trade": out, "reduced": not closed}

        self._pending[res.order_id] = _Pending(res.order_id, Play(**_min_play(t)), "exit",
                                               trade_id=trade_id, qty=qty, reason=reason,
                                               partial=partial, after_fill=after_fill,
                                               decision_price=decision_price)
        return {"ok": True, "status": res.status or "WORKING", "order_id": res.order_id}

    def _book_exit(self, symbol: str, trade_id: str, price: float, qty: float, reason: str,
                   partial: bool = False, after_fill: Optional[Dict[str, float]] = None,
                   decision_price: Optional[float] = None):
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
            out = self.repo.close_trade(trade_id, float(price), exit_reason=reason, **seen)
        self._open_by_symbol.pop(symbol, None)
        self.bus.publish("trade.closed", trade=out, reason=reason)
        return out, True

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

        # 4) detect broker-side bracket exits (child order filled against an open trade)
        try:
            for o in self.broker.list_orders(status="FILLED"):
                self._maybe_close_from_bracket(o)
        except Exception:  # noqa: BLE001
            pass

    def expire_entries(self, now: Optional[dt.datetime] = None) -> List[str]:
        """Cancel the day-trade entry orders still working after ``execution.entry_timeout_min``
        minutes. A limit the price hasn't come to by then is one the price left behind, and a fill
        later - when the price comes back through it - is the move failing, not the setup (Aziz:
        never chase, and never let a stale order chase for you). Swing entries keep their DAY life.
        Returns the ids cancelled; the broker's answer books whatever part filled (_on_unfilled)."""
        limit = float(getattr(self.cfg, "entry_timeout_min", 0) or 0)
        if limit <= 0:
            return []
        now = now or dt.datetime.now(dt.timezone.utc)
        out: List[str] = []
        for oid, p in list(self._pending.items()):
            if (p.kind != "entry" or p.expired or p.submitted_at is None
                    or p.play.timeframe is not Timeframe.INTRADAY):
                continue
            if (now - p.submitted_at).total_seconds() / 60.0 < limit:
                continue
            p.expired = f"not filled within {limit:g} minutes - cancelled rather than chase the price"
            self._cancel_quietly(oid)
            log.warning("ENTRY TIMED OUT  %s %s order %s: %s", p.play.symbol, p.play.side.value, oid, p.expired)
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
            return
        self._pending.pop(res.order_id, None)
        if res.status == "FILLED":
            self._on_filled(p, res)
        else:
            self._on_unfilled(p, res)

    def _on_filled(self, p: _Pending, res) -> None:
        px = res.avg_fill_price or (res.fills[-1].price if res.fills else 0.0)
        if p.kind == "entry":
            self._open_trade(p.play, px, res.filled_qty or p.qty, res.order_id,
                             p.order_type, p.order_session, context=p.context, submitted_at=p.submitted_at,
                             decision=p.decision)
        else:
            self._book_exit(res.symbol, p.trade_id, float(px), res.filled_qty or p.qty, p.reason or "order",
                            p.partial, p.after_fill, p.decision_price)

    def _on_unfilled(self, p: _Pending, res) -> None:
        """The broker finished an order without filling all of it - rejected,
        cancelled, expired - or no longer knows it. What did fill is booked and the
        reason is published; the exit manager sends an exit again."""
        filled = float(res.filled_qty or 0.0)
        if p.kind == "entry":
            if filled > 0:
                px = res.avg_fill_price or (res.fills[-1].price if res.fills else p.play.entry)
                self._open_trade(p.play, px, filled, res.order_id, p.order_type, p.order_session,
                                 context=p.context, submitted_at=p.submitted_at, decision=p.decision)
            else:
                p.play.status = PlayStatus.CANCELED if res.status in ("CANCELED", "EXPIRED") else PlayStatus.ERROR
        if res.status == "REJECTED":
            self._cancel_quietly(res.order_id)          # an inactive order must stay dead
        what = "is no longer known to the broker" if res.status == "UNKNOWN" else f"was {res.status.lower()}"
        part = f" after {filled:,.0f} of {p.qty:,.0f} shares filled" if filled else ""
        reason = p.expired or res.message or "no reason given"
        msg = f"{p.play.symbol} {p.kind} order {res.order_id} {what}{part}: {reason}"
        log.warning("ORDER NOT FILLED  %s", msg)
        self.bus.publish("order.failed", kind=p.kind, order_id=res.order_id, status=res.status,
                         symbol=p.play.symbol, trade_id=p.trade_id, filled_qty=filled,
                         reason=reason, msg=msg)

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
    return Play(symbol=row["symbol"], side=Side(row["side"]), strategy=row["strategy"],
                kind=StrategyKind(row["kind"]), timeframe=Timeframe(row["timeframe"]),
                entry=float(row["entry"] or 0), stop=float(row["stop"] or 0),
                targets=[float(x) for x in (row.get("targets") or [])],
                confidence=float(row.get("confidence") or 0.5), sector=row.get("sector") or "",
                id=row["id"], status=PlayStatus.SUBMITTED)


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
    if o.tag.startswith("exit:"):
        return "exit"
    if o.tag.startswith("play_"):
        return "entry"
    return "app" if raw.get("mine", True) else "outside"
