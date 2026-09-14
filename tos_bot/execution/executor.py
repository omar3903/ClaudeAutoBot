"""Routes an approved Play to the broker and keeps the trade log in sync.

The engine only calls :meth:`execute_play` after the operator has clicked
"Yes" in the dashboard and the PDT / sizing checks have passed. Nothing
here decides *whether* to trade - only *how*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..brokers.base import BrokerAdapter, BrokerError
from ..brokers.venues import venue_label
from ..core.enums import PlayStatus, Side, StrategyKind, Timeframe
from ..core.eventbus import BUS
from ..core.models import Account, OrderRequest, Play
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


class Executor:
    def __init__(self, broker: BrokerAdapter, repo, cfg, bus=BUS,
                 venue: Optional[str] = None) -> None:
        self.broker = broker
        self.venue = venue or broker.name      # stamped on trades - see brokers/venues.py
        self.repo = repo
        self.cfg = cfg
        self.bus = bus
        self._pending: Dict[str, _Pending] = {}
        self._open_by_symbol: Dict[str, str] = {}   # symbol -> trade_id

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
            try:
                self.broker.cancel_order(oid)
            except Exception:  # noqa: BLE001
                log.debug("cancel %s failed", oid, exc_info=True)
            self._pending.pop(oid, None)
            n += 1
        return n

    def pending_exit_trade_ids(self) -> set:
        """Trades whose close order is still working at the broker."""
        return {p.trade_id for p in list(self._pending.values()) if p.kind == "exit" and p.trade_id}

    # ------------------------------------------------------------------ #
    def execute_play(self, play: Play, account: Account,
                     plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
        try:
            if native_bracket:
                res = self.broker.place_bracket(entry, play.primary_target, play.stop)
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
            tid = self._open_trade(play, fill_price, res.filled_qty or qty, res.order_id, ot, osess)
            return {"ok": True, "status": "FILLED", "trade_id": tid,
                    "fill_price": round(fill_price, 4), "qty": res.filled_qty or qty,
                    "order_id": res.order_id, "order_type": ot, "order_session": osess,
                    "bracket_mode": plan.get("bracket_mode")}

        # otherwise track it; sync_open_orders() will pick up the fill
        p = _Pending(res.order_id, play, "entry", qty=qty)
        p.order_type, p.order_session = ot, osess
        self._pending[res.order_id] = p
        return {"ok": True, "status": res.status or "WORKING", "order_id": res.order_id,
                "order_type": ot, "order_session": osess,
                "note": "order working - will confirm on fill"}

    # ------------------------------------------------------------------ #
    def close_trade(self, trade_id: str, reason: str = "manual",
                    limit_price: Optional[float] = None) -> Dict[str, Any]:
        t = self.repo.get_trade(trade_id)
        if not t or t["status"] == "CLOSED":
            return {"ok": False, "reason": "trade not open"}
        held_on = t.get("broker") or "paper"
        if held_on != self.venue:
            # never send an exit to an account that doesn't hold the position
            return {"ok": False, "reason": f"This position is on {venue_label(held_on)} - "
                                           f"switch back to that platform to close it."}
        qty = abs(float(t["quantity"]))
        held = self._held_quantity(t["symbol"])
        if held is not None:
            if abs(held) < 1e-9 or (held > 0) != (t["side"] == "LONG"):
                # closed or removed outside the app - an exit now would open the other side
                return {"ok": False, "not_held": True,
                        "reason": f"{venue_label(held_on)} doesn't show a {t['side'].lower()} {t['symbol']} "
                                  f"position (closed or removed outside the app?) - no exit sent."}
            qty = min(qty, abs(held))           # never sell more than is there
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
            out = self.repo.close_trade(trade_id, float(px), exit_reason=reason)
            self._open_by_symbol.pop(t["symbol"], None)
            self.bus.publish("trade.closed", trade=out)
            return {"ok": True, "status": "FILLED", "trade": out}

        self._pending[res.order_id] = _Pending(res.order_id, Play(**_min_play(t)), "exit",
                                               trade_id=trade_id, qty=qty)
        return {"ok": True, "status": res.status or "WORKING", "order_id": res.order_id}

    def _held_quantity(self, symbol: str) -> Optional[float]:
        """Signed quantity the broker reports for ``symbol`` (0.0 if none), or
        None when it can't say - then the exit goes ahead, as a missed stop is worse."""
        try:
            pos = self.broker.get_account().position(symbol)
        except Exception:  # noqa: BLE001
            return None
        return float(pos.quantity) if pos is not None else 0.0

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

        # 2) reconcile tracked live orders
        for oid in list(self._pending):
            try:
                self._on_order_update(self.broker.get_order(oid))
            except Exception:  # noqa: BLE001
                continue

        # 3) detect broker-side bracket exits (child order filled against an open trade)
        try:
            for o in self.broker.list_orders(status="FILLED"):
                self._maybe_close_from_bracket(o)
        except Exception:  # noqa: BLE001
            pass

    def _on_order_update(self, res) -> None:
        p = self._pending.get(res.order_id)
        if p is None:
            return
        if res.status not in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            return
        self._pending.pop(res.order_id, None)
        if res.status != "FILLED":
            self.bus.publish("order.done", order_id=res.order_id, status=res.status,
                             symbol=res.symbol)
            return
        px = res.avg_fill_price or (res.fills[-1].price if res.fills else 0.0)
        if p.kind == "entry":
            tid = self._open_trade(p.play, px, res.filled_qty or p.qty, res.order_id,
                                   p.order_type, p.order_session)
            self.bus.publish("order.filled", kind="entry", trade_id=tid, symbol=res.symbol,
                             price=round(px, 4), qty=res.filled_qty or p.qty)
        else:
            out = self.repo.close_trade(p.trade_id, float(px), exit_reason="order")
            self._open_by_symbol.pop(res.symbol, None)
            self.bus.publish("trade.closed", trade=out)

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
                    order_type: str = "LIMIT", order_session: str = "REGULAR") -> str:
        tid = self.repo.open_trade(play, float(price), float(qty), self.venue, order_id,
                                   order_type=order_type, order_session=order_session)
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
