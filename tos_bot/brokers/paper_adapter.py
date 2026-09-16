"""The built-in simulator: fills orders against live prices without a real account.

Enough fidelity to run the whole pipeline: a cash and margin book, market /
limit / stop orders, attached one-cancels-other brackets, slippage,
commissions, realised P/L, and day-trade (round-trip) detection so the PDT
guard has something real to count. Prices come from whatever quote function
it's given - the app hands it IBKR's.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..core.enums import AssetClass, OrderType, Side, TimeInForce
from ..core.models import Account, Fill, OrderRequest, OrderResult, Position, Quote
from ..util import clock
from .base import BrokerAdapter, OrderRejected

log = logging.getLogger(__name__)


class _Order:
    def __init__(self, req: OrderRequest) -> None:
        self.id = f"paper_{uuid.uuid4().hex[:10]}"
        self.req = req
        self.status = "WORKING"
        self.filled_qty = 0.0
        self.avg_price = 0.0
        self.fills: List[Fill] = []
        self.children: List["_Order"] = []       # bracket target / stop
        self.parent_id: Optional[str] = None
        self.oco_group: Optional[str] = None

    def result(self) -> OrderResult:
        return OrderResult(
            order_id=self.id, status=self.status, symbol=self.req.symbol,
            submitted_qty=self.req.quantity, filled_qty=self.filled_qty,
            avg_fill_price=self.avg_price, fills=list(self.fills),
            raw={"parent_id": self.parent_id, "oco_group": self.oco_group,
                 "client_tag": self.req.client_tag},
            side=self.req.side, tag=self.req.client_tag, order_type=self.req.order_type.value,
            limit_price=self.req.limit_price, stop_price=self.req.stop_price, tif=self.req.tif.name,
        )


class PaperBroker(BrokerAdapter):
    name = "paper"
    paper = True
    supports_bracket_native = True

    def __init__(
        self,
        quote: Callable[[str], Quote],
        starting_cash: float = 100000.0,
        slippage_bps: float = 2.0,
        commission_per_share: float = 0.0,
        commission_min: float = 0.0,
        margin_multiplier: float = 2.0,
        state_path: Optional[Path] = None,
    ) -> None:
        self._quote = quote
        self._cash = float(starting_cash)
        self._start_equity = float(starting_cash)
        self.slippage_bps = slippage_bps
        self.commission_per_share = commission_per_share
        self.commission_min = commission_min
        self.margin_multiplier = margin_multiplier
        self.state_path = state_path                 # None = don't persist

        self._positions: Dict[str, Position] = {}
        self._orders: Dict[str, _Order] = {}
        self._realized_pl = 0.0
        self._round_trips: List[dt.date] = []        # one entry per day trade
        self._opened_on: Dict[str, dt.date] = {}     # symbol -> session it was opened
        self._connected = False
        self._lock = threading.RLock()

    # ---- connection + persistence ------------------------------------- #
    def connect(self) -> None:
        self._load_state()
        self._connected = True
        log.info("simulator ready - cash $%.2f, %d position(s)", self._cash, len(self._positions))

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            s = json.loads(self.state_path.read_text())
        except (OSError, ValueError) as e:
            log.warning("could not read simulator state (%s) - starting fresh", e)
            return
        self._cash = float(s.get("cash", self._cash))
        self._start_equity = float(s.get("start_equity", self._start_equity))
        self._realized_pl = float(s.get("realized_pl", 0.0))
        self._round_trips = [dt.date.fromisoformat(d) for d in s.get("round_trips", [])]
        self._opened_on = {k: dt.date.fromisoformat(v) for k, v in (s.get("opened_today") or {}).items()}
        self._positions = {
            p["symbol"]: Position(symbol=p["symbol"], quantity=float(p["quantity"]),
                                  avg_price=float(p["avg_price"]),
                                  asset_class=AssetClass(p.get("asset_class", "EQUITY")))
            for p in s.get("positions", [])
        }

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({
                "cash": round(self._cash, 6),
                "start_equity": round(self._start_equity, 2),
                "realized_pl": round(self._realized_pl, 6),
                "round_trips": [d.isoformat() for d in self._round_trips],
                "opened_today": {k: v.isoformat() for k, v in self._opened_on.items()},
                "positions": [{"symbol": p.symbol, "quantity": p.quantity, "avg_price": p.avg_price,
                               "asset_class": p.asset_class.value}
                              for p in self._positions.values() if abs(p.quantity) > 1e-9],
            }, indent=2))
        except OSError:
            log.debug("simulator state save failed", exc_info=True)

    # ---- account -------------------------------------------------------- #
    def get_quote(self, symbol: str) -> Quote:
        return self._quote(symbol)

    def get_account(self) -> Account:
        with self._lock:
            positions: List[Position] = []
            long_mv = short_mv = 0.0
            for pos in self._positions.values():
                if abs(pos.quantity) < 1e-9:
                    continue
                try:
                    pos.market_price = self._quote(pos.symbol).last
                except Exception:  # noqa: BLE001
                    pos.market_price = pos.market_price or pos.avg_price
                positions.append(pos)
                if pos.quantity > 0:
                    long_mv += pos.quantity * pos.market_price
                else:
                    short_mv += abs(pos.quantity) * pos.market_price
            equity = self._cash + long_mv - short_mv
            return Account(
                account_id="SIMULATOR",
                equity=round(equity, 2),
                cash=round(self._cash, 2),
                buying_power=round(max(0.0, equity * self.margin_multiplier - (long_mv + short_mv)), 2),
                round_trips=self.day_trades_in_last_5_sessions(),
                positions=positions,
                raw={"realized_pl": round(self._realized_pl, 2), "start_equity": self._start_equity},
            )

    def day_trades_in_last_5_sessions(self) -> int:
        window = set(clock.last_n_sessions(clock.session_date(), 5))
        return sum(1 for d in self._round_trips if d in window)

    # ---- orders ------------------------------------------------------------ #
    def place_order(self, req: OrderRequest) -> OrderResult:
        if not self._connected:
            raise OrderRejected("simulator not connected")
        if req.quantity <= 0:
            raise OrderRejected("quantity must be > 0")
        with self._lock:
            order = _Order(req)
            self._orders[order.id] = order
            self._try_fill(order)
            if order.status == "FILLED" and req.is_entry and (req.take_profit or req.stop_loss):
                self._attach_bracket(order)
            return order.result()

    def _attach_bracket(self, parent: _Order) -> None:
        req = parent.req
        exit_side = Side.SHORT if req.side is Side.LONG else Side.LONG
        group = f"oco_{uuid.uuid4().hex[:8]}"
        for price, otype, tag in ((req.take_profit, OrderType.LIMIT, "TP"),
                                  (req.stop_loss, OrderType.STOP, "SL")):
            if not price:
                continue
            child = _Order(OrderRequest(
                symbol=req.symbol, side=exit_side, quantity=parent.filled_qty, order_type=otype,
                limit_price=price if otype is OrderType.LIMIT else None,
                stop_price=price if otype is OrderType.STOP else None,
                tif=TimeInForce.GTC, asset_class=req.asset_class, is_entry=False,
                client_tag=f"{req.client_tag}:{tag}",
            ))
            child.parent_id, child.oco_group = parent.id, group
            self._orders[child.id] = child
            parent.children.append(child)

    def _fill_price(self, req: OrderRequest, q: Quote) -> Optional[float]:
        buying = req.side is Side.LONG
        if req.order_type is OrderType.MARKET:
            return q.ask if buying else q.bid
        if req.order_type is OrderType.LIMIT and req.limit_price:
            if buying and q.ask <= req.limit_price:
                return min(req.limit_price, q.ask)
            if not buying and q.bid >= req.limit_price:
                return max(req.limit_price, q.bid)
            # a limit resting inside the spread counts as marketable here
            if (buying and req.limit_price >= q.bid) or (not buying and req.limit_price <= q.ask):
                return req.limit_price
            return None
        if req.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            trigger = req.stop_price or 0.0
            if (buying and q.last >= trigger) or (not buying and q.last <= trigger):
                return q.last if req.order_type is OrderType.STOP else (req.limit_price or q.last)
        return None

    def _try_fill(self, order: _Order) -> None:
        price = self._fill_price(order.req, self._quote(order.req.symbol))
        if price is None:
            order.status = "WORKING"
            return
        slip = price * self.slippage_bps / 1e4
        self._execute(order, price + slip if order.req.side is Side.LONG else price - slip)

    def _execute(self, order: _Order, price: float) -> None:
        req, qty = order.req, order.req.quantity
        signed = qty if req.side is Side.LONG else -qty
        commission = max(self.commission_min, qty * self.commission_per_share)
        pos = self._positions.get(req.symbol) or Position(symbol=req.symbol, quantity=0.0, avg_price=0.0,
                                                          asset_class=req.asset_class)
        session = clock.session_date()
        prev = pos.quantity
        if prev and (prev > 0) != (signed > 0):                       # reducing or flipping
            closed = min(abs(signed), abs(prev))
            self._realized_pl += (1 if prev > 0 else -1) * (price - pos.avg_price) * closed - commission
            if self._opened_on.get(req.symbol) == session:
                self._round_trips.append(session)
        new_qty = prev + signed
        if prev == 0 or (prev > 0) == (signed > 0):                   # opening or adding
            pos.avg_price = (abs(prev) * pos.avg_price + abs(signed) * price) / (abs(prev) + abs(signed))
            if prev == 0:
                self._opened_on[req.symbol] = session
        elif abs(new_qty) > 1e-9 and (new_qty > 0) != (prev > 0):     # flipped through zero
            pos.avg_price = price
            self._opened_on[req.symbol] = session
        elif abs(new_qty) <= 1e-9:
            pos.avg_price = 0.0

        pos.quantity, pos.market_price = new_qty, price
        self._positions[req.symbol] = pos
        self._cash -= signed * price + commission
        order.filled_qty, order.avg_price, order.status = qty, price, "FILLED"
        order.fills.append(Fill(order_id=order.id, symbol=req.symbol, side=req.side,
                                quantity=qty, price=price, commission=commission))
        log.info("SIM FILL %s %s %s @ %.4f", req.side.value, qty, req.symbol, price)
        self._save_state()

    def poll(self) -> List[OrderResult]:
        """Try working orders against the latest prices (the engine calls this every few seconds)."""
        changed: List[OrderResult] = []
        with self._lock:
            filled_groups = {o.oco_group for o in self._orders.values() if o.oco_group and o.status == "FILLED"}
            for order in [o for o in self._orders.values() if o.status == "WORKING"]:
                if order.oco_group in filled_groups:
                    order.status = "CANCELED"
                    changed.append(order.result())
                    continue
                self._try_fill(order)
                if order.status != "WORKING":
                    changed.append(order.result())
                    if order.oco_group:
                        filled_groups.add(order.oco_group)
        return changed

    def cancel_order(self, order_id: str) -> None:
        with self._lock:
            order = self._orders.get(order_id)
            if order and order.status == "WORKING":
                order.status = "CANCELED"
                for child in order.children:
                    if child.status == "WORKING":
                        child.status = "CANCELED"

    def get_order(self, order_id: str) -> OrderResult:
        order = self._orders.get(order_id)
        if not order:
            raise OrderRejected(f"unknown order {order_id}")
        return order.result()

    def get_fills(self, symbol: Optional[str] = None) -> List[Fill]:
        with self._lock:
            fills = [f for o in self._orders.values() for f in o.fills if not symbol or f.symbol == symbol]
        return sorted(fills, key=lambda f: f.ts)

    def list_orders(self, status: Optional[str] = None) -> List[OrderResult]:
        with self._lock:
            return [o.result() for o in self._orders.values() if status is None or o.status == status]

    def reset(self, cash: float) -> None:
        with self._lock:
            self._cash = self._start_equity = float(cash)
            self._positions.clear()
            self._orders.clear()
            self._realized_pl = 0.0
            self._round_trips.clear()
            self._opened_on.clear()
            self._save_state()
