"""Routes an approved Play to the broker and keeps the trade log in sync.

The engine only calls :meth:`execute_play` after the operator has clicked
"Yes" in the dashboard and the PDT / sizing checks have passed. Nothing
here decides *whether* to trade - only *how*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..brokers.base import BrokerAdapter, BrokerError
from ..core.enums import PlayStatus, Side
from ..core.eventbus import BUS
from ..core.models import Account, OrderRequest, Play
from .order_builder import build_entry_order, build_exit_order

log = logging.getLogger(__name__)


@dataclass
class _Pending:
    order_id: str
    play: Play
    kind: str                       # "entry" | "exit"
    trade_id: Optional[str] = None
    qty: float = 0.0


class Executor:
    def __init__(self, broker: BrokerAdapter, repo, cfg, bus=BUS) -> None:
        self.broker = broker
        self.repo = repo
        self.cfg = cfg
        self.bus = bus
        self._pending: Dict[str, _Pending] = {}
        self._open_by_symbol: Dict[str, str] = {}   # symbol -> trade_id

    # ------------------------------------------------------------------ #
    def execute_play(self, play: Play, account: Account) -> Dict[str, Any]:
        qty = int(play.suggested_qty or 0)
        if qty <= 0:
            return {"ok": False, "reason": "position size is zero (risk budget / buying power)"}

        entry = build_entry_order(play, qty, self.cfg)
        bracket = bool(getattr(self.cfg, "bracket_orders", True))
        try:
            if bracket and (self.broker.supports_bracket_native or self.broker.paper):
                res = self.broker.place_bracket(entry, play.primary_target, play.stop)
            else:
                res = self.broker.place_order(entry)
        except BrokerError as e:
            self._audit("PLACE", entry, {"error": str(e)}, ok=False, play_id=play.id, msg=str(e))
            play.status = PlayStatus.ERROR
            return {"ok": False, "reason": str(e)}

        self._audit("PLACE", entry, res.raw or {"status": res.status}, ok=True,
                    play_id=play.id, msg=res.message)
        play.status = PlayStatus.SUBMITTED

        # immediate fill (paper / marketable) -> open the trade now
        if res.status in ("FILLED",) or res.filled_qty >= qty > 0:
            fill_price = res.avg_fill_price or (res.fills[-1].price if res.fills else play.entry)
            tid = self._open_trade(play, fill_price, res.filled_qty or qty, res.order_id)
            return {"ok": True, "status": "FILLED", "trade_id": tid,
                    "fill_price": round(fill_price, 4), "qty": res.filled_qty or qty,
                    "order_id": res.order_id}

        # otherwise track it; sync_open_orders() will pick up the fill
        self._pending[res.order_id] = _Pending(res.order_id, play, "entry", qty=qty)
        return {"ok": True, "status": res.status or "WORKING", "order_id": res.order_id,
                "note": "order working - will confirm on fill"}

    # ------------------------------------------------------------------ #
    def close_trade(self, trade_id: str, reason: str = "manual",
                    limit_price: Optional[float] = None) -> Dict[str, Any]:
        t = self.repo.get_trade(trade_id)
        if not t or t["status"] == "CLOSED":
            return {"ok": False, "reason": "trade not open"}
        req = build_exit_order(t["symbol"], t["side"], abs(float(t["quantity"])),
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
                                               trade_id=trade_id, qty=abs(float(t["quantity"])))
        return {"ok": True, "status": res.status or "WORKING", "order_id": res.order_id}

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
                tag = (o.raw or {}).get("client_tag") or ""
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
            tid = self._open_trade(p.play, px, res.filled_qty or p.qty, res.order_id)
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
    def _open_trade(self, play: Play, price: float, qty: float, order_id: str) -> str:
        tid = self.repo.open_trade(play, float(price), float(qty), self.broker.name, order_id)
        self._open_by_symbol[play.symbol] = tid
        play.status = PlayStatus.FILLED
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
    from ..core.enums import Side, StrategyKind, Timeframe

    return dict(symbol=t["symbol"], side=Side(t["side"]), strategy=t["strategy"],
                kind=StrategyKind(t["kind"]), timeframe=Timeframe(t["timeframe"]),
                entry=float(t["entry_price"] or 0), stop=float(t["stop_price"] or 0),
                targets=[float(t["target_price"] or 0)] if t.get("target_price") else [])
